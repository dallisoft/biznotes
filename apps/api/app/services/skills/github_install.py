"""GitHub 저장소 URL로 스킬 설치(#259 PR-2) — 감지 · 조회 · 확인 카드 · 설치.

설계 노트 `docs/plans/skill-install-github.md` §4의 경계를 코드로 옮긴다.

- **감지**(`detect_github_skill_install`)는 이번 요청의 사용자 글만 본다. 이력·소스·도구 결과·모델
  출력은 보지 않는다(프롬프트 인젝션 차단). 채팅 도구는 더하지 않는다(`CHAT_SAFE_TOOLS` 그대로).
- **외부 요청**: 사용자 URL은 파싱만 하고 받지 않는다. 제품이 직접 내는 GET은 api.github.com
  (저장소·커밋 SHA)과 codeload.github.com(후보용 archive)이다. 모두 코어 SSRF 관문에 호스트를
  고정한 받아오기 전용 정책(`EgressPolicy(fetch_only=True, allowed_hosts=(host,))`)을 건다.
- **설치**(`install_from_github`)는 카드 버튼이 부르는 `POST /skill-installs/github`에서만 돈다.
  코어 `import_url_detailed`가 archive를 다시 받고 `archive_sha256`으로 같은 archive임을 고정한다.
  소유자는 항상 요청자이고, 다시 설치할 대상은 요청자 본인의 가져온 스킬에서만 찾는다.
"""

import hashlib
import json
import re
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from functools import partial
from typing import Any, Literal
from urllib.parse import urlsplit

import anyio
import httpx
import structlog
from biznotes_core.core.ssrf import EgressPolicy, SSRFBlockedError, ssrf_safe_request
from biznotes_core.models import Skill, SkillVersion, User
from biznotes_core.services.audit import record
from biznotes_core.services.skills import package as pkg
from biznotes_core.services.skills import service as skill_service
from biznotes_core.services.skills import standard as std
from biznotes_core.services.skills.parser import SkillParseError
from biznotes_core.services.skills.report import (
    evaluate_package,
    root_mismatch_warning,
    summarize_import_candidate,
)
from biznotes_core.services.skills.runtime import SkillCatalog
from biznotes_core.services.skills.validator import SkillValidationError
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.schemas.skill_install import (
    MAX_INSTALL_PATHS,
    OWNER_PATTERN,
    REPO_PATTERN,
    SkillInstallCandidateOut,
    SkillInstallGithubOut,
    SkillInstallGithubRequest,
    SkillInstallItemOut,
    SkillInstallLicenseOut,
    SkillInstallOfferOut,
    SkillInstallReinstallOut,
    SkillInstallWarningOut,
    is_skill_dir_path,
)

logger = structlog.get_logger()

GITHUB_WEB_HOSTS = frozenset({"github.com", "www.github.com"})
GITHUB_API_HOST = "api.github.com"
CODELOAD_HOST = "codeload.github.com"
# (owner, repo, ref) 조회 캐시 — GitHub API 비인증 한도(IP당 60회/시) 때문이다(§4 「한도」)
CACHE_TTL_SECONDS = 600
CACHE_PREFIX = "skill_install:github:"
_API_MAX_BYTES = 1024 * 1024
_API_TIMEOUT = 15.0
_ARCHIVE_TIMEOUT = 60.0
# 코어 SSRF 관문의 크기 초과 문구 접두(호출처가 이 접두에 기대도록 코어가 고정했다)
_OVERSIZE_PREFIX = "응답이 너무 큽니다"

# 감지 규칙(§4): GitHub URL 정확히 1개 + 설치 의도 키워드
_INSTALL_KEYWORD_RE = re.compile(r"설치|install|가져와|추가해", re.IGNORECASE)
# GitHub 밖 URL·여러 저장소에 고정 안내를 줄 때만 본다
# — 일반 대화(「이 글을 노트에 추가해」)를 가로채지 않게
_SKILL_WORD_RE = re.compile(r"스킬|skill", re.IGNORECASE)
# 글 속 URL — RFC 3986 문자만 잇는다. 바로 붙은 한글 조사·괄호·따옴표는 URL이 아니다
_URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&*+,;=%-]+", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?"
_OWNER_RE = re.compile(OWNER_PATTERN)
_REPO_RE = re.compile(REPO_PATTERN)
_REF_RE = re.compile(r"[A-Za-z0-9._-]{1,255}")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
# 스킬 폴더 밖 의존 휴리스틱(AC-10) — 저장소 루트 `scripts/`·`agents/` 참조
_OUTSIDE_REF_RE = re.compile(r"(?<![\w.-])(scripts|agents)/")

# ---------- 고정 문구 — URL·응답 원문을 담지 않는다 ----------

MSG_GITHUB_ONLY = (
    "스킬은 GitHub 공개 저장소 주소(https://github.com/<owner>/<repo>)로만 설치할 수 있습니다."
)
MSG_ONE_REPO = "한 번에 GitHub 저장소 주소 하나만 보내 주세요."
MSG_URL_SHAPE = (
    "지원하는 주소는 https://github.com/<owner>/<repo> 또는 "
    "https://github.com/<owner>/<repo>/tree/<브랜치·태그·커밋>/<경로>입니다. "
    "'/'가 든 브랜치 이름은 지원하지 않습니다."
)
MSG_REPO_NOT_FOUND = "GitHub 저장소를 찾을 수 없습니다. 공개 저장소만 설치할 수 있습니다."
MSG_REF_NOT_FOUND = (
    "브랜치·태그·커밋을 찾을 수 없습니다. '/'가 든 브랜치 이름은 지원하지 않습니다."
)
MSG_RATE_LIMITED = "GitHub 조회 한도에 걸렸습니다. 잠시 뒤 다시 시도해 주세요."
MSG_GITHUB_UNAVAILABLE = "GitHub에서 저장소 정보를 받지 못했습니다. 잠시 뒤 다시 시도해 주세요."
MSG_BLOCKED = "허용되지 않는 주소로 연결되어 요청을 막았습니다."
MSG_ARCHIVE_TOO_LARGE = "저장소 archive가 크기 상한을 넘어 설치할 수 없습니다."
MSG_LIMIT = "크기·파일 수·항목 수 한도를 넘어 설치할 수 없습니다."
MSG_NO_SKILLS = "이 저장소(경로)에서 스킬(SKILL.md)을 찾지 못했습니다."
MSG_INSTALL_FAILED = "설치하지 못했습니다. 잠시 뒤 다시 시도해 주세요."

_LIMIT_REASONS = frozenset(
    {
        "too_many_files",
        "file_too_large",
        "package_too_large",
        "too_many_archive_entries",
        "bomb_suspected",
    }
)
_REASON_MESSAGES = {
    "symlink": "선택한 폴더에 심볼릭 링크가 있어 설치할 수 없습니다.",
    "digest_mismatch": (
        "확인 카드를 띄운 뒤 저장소 archive가 바뀌었습니다. "
        "같은 요청을 다시 보내 카드를 새로 띄워 주세요."
    ),
    "ssrf_blocked": MSG_BLOCKED,
    "remote_fetch_failed": "GitHub에서 저장소 archive를 받지 못했습니다.",
    "remote_http_status": "GitHub에서 저장소 archive를 받지 못했습니다.",
    "invalid_skill_selection": "선택한 경로에서 SKILL.md를 찾지 못했습니다.",
    "unsupported_path": "이 경로는 설치할 수 없습니다(영문·숫자·._- 폴더 이름만 지원합니다).",
    "skill_gone": "다시 설치할 스킬을 찾을 수 없습니다.",
    "install_failed": MSG_INSTALL_FAILED,
}
# archive 자체가 문제인 사유 — 다음 경로도 같은 archive라 다시 받지 않고 같은 사유로 끝낸다
_ARCHIVE_REASONS = frozenset(
    {
        "digest_mismatch",
        "ssrf_blocked",
        "remote_fetch_failed",
        "remote_http_status",
        "unsupported_archive",
        "bad_zip",
        "too_many_archive_entries",
        "bomb_suspected",
    }
)


def reason_message(reason: str) -> str:
    """실패 사유 코드(코어 검증 사유) → 고정 문구."""
    if reason in _LIMIT_REASONS:
        return MSG_LIMIT
    return _REASON_MESSAGES.get(reason, f"스킬 패키지 검증에 실패했습니다({reason}).")


class GithubInstallError(Exception):
    """제안 단계의 고정 문구 오류 — `message`만 사용자에게 보인다."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(code)


# ---------- 감지 · URL 파싱 ----------


@dataclass(frozen=True)
class GithubTarget:
    owner: str
    repo: str
    ref: str | None  # None이면 기본 브랜치
    path: str | None  # `/tree/<ref>/<path>`의 경로 — 이 아래 후보만 보인다


@dataclass(frozen=True)
class InstallDetection:
    """감지 결과 — `target`이면 확인 카드, `guidance`면 LLM 없이 고정 안내로 답한다."""

    target: GithubTarget | None = None
    guidance: str | None = None


def _url_host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def parse_github_url(url: str) -> GithubTarget:
    """`https://github.com/<owner>/<repo>[/tree/<ref>[/<path>]]`만 받는다. 네트워크를 쓰지 않는다.

    query·fragment(`?tab=readme` 등)는 무시한다 — 이 URL로는 아무것도 받지 않는다.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise GithubInstallError("unsupported_url", MSG_URL_SHAPE) from e
    if (parts.hostname or "").lower() not in GITHUB_WEB_HOSTS:
        raise GithubInstallError("github_only", MSG_GITHUB_ONLY)
    if (
        parts.scheme.lower() != "https"
        or parts.username is not None
        or parts.password is not None
        or port is not None
    ):
        raise GithubInstallError("unsupported_url", MSG_URL_SHAPE)
    raw = parts.path[:-1] if parts.path.endswith("/") else parts.path
    segments = raw.split("/")[1:] if raw else []
    if len(segments) < 2 or any(not segment for segment in segments):
        raise GithubInstallError("unsupported_url", MSG_URL_SHAPE)
    owner, repo = segments[0], segments[1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not _OWNER_RE.fullmatch(owner) or not _REPO_RE.fullmatch(repo) or repo in (".", ".."):
        raise GithubInstallError("unsupported_url", MSG_URL_SHAPE)
    rest = segments[2:]
    ref: str | None = None
    path: str | None = None
    if rest:
        if rest[0] != "tree" or len(rest) < 2:
            raise GithubInstallError("unsupported_url", MSG_URL_SHAPE)
        ref = rest[1]
        if not _REF_RE.fullmatch(ref) or ref in (".", ".."):
            raise GithubInstallError("unsupported_url", MSG_URL_SHAPE)
        if len(rest) > 2:
            path = "/".join(rest[2:])
            if not is_skill_dir_path(path):
                raise GithubInstallError("unsupported_url", MSG_URL_SHAPE)
    return GithubTarget(owner=owner, repo=repo, ref=ref, path=path)


def detect_github_skill_install(content: str) -> InstallDetection | None:
    """이번 사용자 글(`body.content`)만 보고 설치 요청인지 판정한다. None이면 일반 채팅이다.

    - 설치 키워드 + GitHub URL 정확히 1개 → 그 URL을 파싱한다(모양이 틀리면 고정 안내).
    - 설치 키워드 + 「스킬」 + GitHub URL 여러 개 또는 GitHub 밖 URL → 고정 안내.
    """
    if not _INSTALL_KEYWORD_RE.search(content):
        return None
    urls = [url.rstrip(_TRAILING_PUNCT) for url in _URL_RE.findall(content)]
    github = [url for url in urls if _url_host(url) in GITHUB_WEB_HOSTS]
    if len(github) == 1:
        try:
            return InstallDetection(target=parse_github_url(github[0]))
        except GithubInstallError as e:
            return InstallDetection(guidance=e.message)
    if not _SKILL_WORD_RE.search(content):
        return None
    if github:
        return InstallDetection(guidance=MSG_ONE_REPO)
    if urls:
        return InstallDetection(guidance=MSG_GITHUB_ONLY)
    return None


# ---------- GitHub 조회 (SSRF 관문 + 호스트 고정 받아오기 전용) ----------


@dataclass(frozen=True)
class ResolvedRepo:
    owner: str  # GitHub 정식 표기(full_name)
    repo: str
    sha: str
    ref: str  # 요청한 ref, 없으면 기본 브랜치
    license_spdx: str | None
    license_name: str | None


def codeload_url(owner: str, repo: str, sha: str) -> str:
    return f"https://{CODELOAD_HOST}/{owner}/{repo}/zip/{sha}"


def _egress(host: str) -> EgressPolicy:
    settings = get_settings()
    return EgressPolicy(
        fetch_only=True,
        allowed_hosts=(host,),
        max_url_chars=settings.agent_http_max_url_chars,
        max_query_chars=settings.agent_http_max_query_chars,
    )


async def _github_get(
    host: str,
    url: str,
    *,
    accept: str | None,
    max_bytes: int,
    timeout: float,  # noqa: ASYNC109 — 관문의 httpx 타임아웃으로 넘긴다
) -> httpx.Response:
    settings = get_settings()
    try:
        return await ssrf_safe_request(
            "GET",
            url,
            allow_private=settings.ssrf_allow_private,
            timeout=timeout,
            headers={"Accept": accept} if accept else None,
            max_bytes=max_bytes,
            egress=_egress(host),
        )
    except SSRFBlockedError as e:
        if str(e).startswith(_OVERSIZE_PREFIX):
            if host == CODELOAD_HOST:
                raise GithubInstallError("archive_too_large", MSG_ARCHIVE_TOO_LARGE) from e
            raise GithubInstallError("github_unavailable", MSG_GITHUB_UNAVAILABLE) from e
        # 로그에는 호스트·예외 타입만 — URL 경로·응답은 남기지 않는다
        logger.warning("skill_install_github_blocked", host=host, error_type=type(e).__name__)
        raise GithubInstallError("ssrf_blocked", MSG_BLOCKED) from e
    except httpx.HTTPError as e:
        logger.warning("skill_install_github_failed", host=host, error_type=type(e).__name__)
        raise GithubInstallError("github_unavailable", MSG_GITHUB_UNAVAILABLE) from e


def _raise_for_status(resp: httpx.Response, not_found_message: str) -> None:
    if resp.status_code in (403, 429):
        raise GithubInstallError("rate_limited", MSG_RATE_LIMITED)
    if resp.status_code in (404, 422):
        raise GithubInstallError("not_found", not_found_message)
    if not 200 <= resp.status_code < 300:
        raise GithubInstallError("github_unavailable", MSG_GITHUB_UNAVAILABLE)


def _short_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        return None
    return value.strip()[:limit]


def _cache_key(target: GithubTarget) -> str:
    return f"{CACHE_PREFIX}{target.owner.lower()}/{target.repo.lower()}@{target.ref or ''}"


def _valid_resolved(data: Any, target: GithubTarget) -> ResolvedRepo | None:
    """캐시 값 재검증 — 요청한 저장소·ref와 맞지 않으면 쓰지 않는다(Redis 값도 믿지 않는다)."""
    try:
        resolved = ResolvedRepo(**data)
    except TypeError:
        return None
    if (
        not isinstance(resolved.owner, str)
        or not isinstance(resolved.repo, str)
        or not isinstance(resolved.sha, str)
        or not isinstance(resolved.ref, str)
        or not _OWNER_RE.fullmatch(resolved.owner)
        or not _REPO_RE.fullmatch(resolved.repo)
        or not _SHA_RE.fullmatch(resolved.sha)
        or resolved.owner.lower() != target.owner.lower()
        or resolved.repo.lower() != target.repo.lower()
        or (target.ref is not None and resolved.ref != target.ref)
        # 고정 커밋 요청이면 캐시의 SHA가 그 커밋이어야 한다
        or (
            target.ref is not None
            and _SHA_RE.fullmatch(target.ref.lower()) is not None
            and resolved.sha != target.ref.lower()
        )
    ):
        return None
    return resolved


async def _cache_get(target: GithubTarget) -> ResolvedRepo | None:
    try:
        redis = Redis.from_url(get_settings().redis_url)
        try:
            raw = await redis.get(_cache_key(target))
        finally:
            await redis.aclose()
    except Exception as e:  # noqa: BLE001 — 캐시는 best-effort다
        logger.warning("skill_install_cache_unavailable", error_type=type(e).__name__)
        return None
    if raw is None:
        return None
    try:
        return _valid_resolved(json.loads(raw), target)
    except ValueError:
        return None


async def _cache_set(target: GithubTarget, resolved: ResolvedRepo) -> None:
    try:
        redis = Redis.from_url(get_settings().redis_url)
        try:
            await redis.set(
                _cache_key(target), json.dumps(asdict(resolved)), ex=CACHE_TTL_SECONDS
            )
        finally:
            await redis.aclose()
    except Exception as e:  # noqa: BLE001 — 캐시는 best-effort다
        logger.warning("skill_install_cache_unavailable", error_type=type(e).__name__)


async def resolve_repo(target: GithubTarget) -> ResolvedRepo:
    """저장소 정식 이름·기본 브랜치·라이선스와 ref의 커밋 SHA. 결과는 10분 캐시한다."""
    cached = await _cache_get(target)
    if cached is not None:
        return cached
    resp = await _github_get(
        GITHUB_API_HOST,
        f"https://{GITHUB_API_HOST}/repos/{target.owner}/{target.repo}",
        accept="application/vnd.github+json",
        max_bytes=_API_MAX_BYTES,
        timeout=_API_TIMEOUT,
    )
    _raise_for_status(resp, MSG_REPO_NOT_FOUND)
    try:
        meta = resp.json()
    except ValueError as e:
        raise GithubInstallError("github_unavailable", MSG_GITHUB_UNAVAILABLE) from e
    if not isinstance(meta, dict):
        raise GithubInstallError("github_unavailable", MSG_GITHUB_UNAVAILABLE)
    if meta.get("private") is True:
        raise GithubInstallError("not_found", MSG_REPO_NOT_FOUND)
    full_name = meta.get("full_name")
    owner, _, repo = full_name.partition("/") if isinstance(full_name, str) else ("", "", "")
    if not _OWNER_RE.fullmatch(owner) or not _REPO_RE.fullmatch(repo):
        raise GithubInstallError("github_unavailable", MSG_GITHUB_UNAVAILABLE)
    ref = target.ref or meta.get("default_branch")
    if not isinstance(ref, str) or not _REF_RE.fullmatch(ref):
        raise GithubInstallError("not_found", MSG_REF_NOT_FOUND)
    if _SHA_RE.fullmatch(ref.lower()):
        sha = ref.lower()  # 고정 커밋 — 조회 없이 쓴다(없으면 archive가 404다)
    else:
        resp = await _github_get(
            GITHUB_API_HOST,
            f"https://{GITHUB_API_HOST}/repos/{owner}/{repo}/commits/{ref}",
            accept="application/vnd.github.sha",
            max_bytes=_API_MAX_BYTES,
            timeout=_API_TIMEOUT,
        )
        _raise_for_status(resp, MSG_REF_NOT_FOUND)
        sha = resp.text.strip().lower()
        if not _SHA_RE.fullmatch(sha):
            raise GithubInstallError("github_unavailable", MSG_GITHUB_UNAVAILABLE)
    license_info = meta.get("license")
    license_info = license_info if isinstance(license_info, dict) else {}
    resolved = ResolvedRepo(
        owner=owner,
        repo=repo,
        sha=sha,
        ref=ref,
        license_spdx=_short_text(license_info.get("spdx_id"), 64),
        license_name=_short_text(license_info.get("name"), 200),
    )
    await _cache_set(target, resolved)
    return resolved


async def fetch_archive(resolved: ResolvedRepo) -> bytes:
    """후보용 archive(커밋 SHA 고정)를 받는다. 크기 상한은 코어 업로드 상한과 같다."""
    resp = await _github_get(
        CODELOAD_HOST,
        codeload_url(resolved.owner, resolved.repo, resolved.sha),
        accept=None,
        max_bytes=get_settings().skill_import_max_upload_bytes,
        timeout=_ARCHIVE_TIMEOUT,
    )
    _raise_for_status(resp, MSG_REF_NOT_FOUND)
    return resp.content


# ---------- 확인 카드(후보 미리보기) ----------


def _warning(
    layer: Literal["standard", "portability", "policy", "capability", "product"],
    code: Any,
    message: Any,
) -> SkillInstallWarningOut:
    return SkillInstallWarningOut(layer=layer, code=str(code)[:64], message=str(message)[:500])


def _report_warnings(
    report: dict[str, Any], root_mismatch: dict | None
) -> list[SkillInstallWarningOut]:
    """코어 네 층 보고서(D5) → 카드 경고. 정책 오류는 이미 예외로 거부됐다."""
    out = [
        _warning("standard", "standard_violation", message)
        for message in (report.get("standard") or {}).get("errors") or []
    ]
    for layer in ("portability", "policy", "capability"):
        for item in (report.get(layer) or {}).get("warnings") or []:
            if isinstance(item, dict):
                out.append(_warning(layer, item.get("code"), item.get("message")))
    if root_mismatch:
        out.append(_warning("portability", root_mismatch["code"], root_mismatch["message"]))
    return out


def outside_folder_warning(body: str, files: dict[str, bytes]) -> SkillInstallWarningOut | None:
    """스킬 폴더 밖 의존(AC-10) — `../` 또는 패키지에 없는 저장소 루트 `scripts/`·`agents/` 참조."""
    found: list[str] = []
    if "../" in body:
        found.append("../")
    for top in sorted(set(_OUTSIDE_REF_RE.findall(body))):
        if not any(path.startswith(f"{top}/") for path in files):
            found.append(f"{top}/")
    if not found:
        return None
    return _warning(
        "product",
        "references_outside_skill",
        "스킬 폴더 밖 경로를 참조합니다(" + ", ".join(found) + "). 저장소 루트의 스크립트·"
        "서브에이전트는 설치되는 패키지에 들어가지 않아 원본처럼 동작하지 않을 수 있습니다.",
    )


@dataclass
class _Preview:
    archive_sha256: str
    archive_root: str
    candidates: list[SkillInstallCandidateOut]
    truncated: int


def _preview_candidate(
    data: bytes, archive_path: str, rel_dir: str, summary: Any
) -> SkillInstallCandidateOut:
    errors = list(summary.errors)
    ok = bool(summary.ok)
    warnings: list[SkillInstallWarningOut] = []
    runtime_profile: str | None = None
    has_scripts = False
    evaluation = None
    if not is_skill_dir_path(rel_dir):
        ok, errors = False, sorted({*errors, "unsupported_path"})
    if ok:
        try:
            unpacked = pkg.unpack_import_archive_detailed(data, selected_skill_path=archive_path)
            evaluation = evaluate_package(unpacked.files)
        except (SkillValidationError, SkillParseError) as e:
            ok, errors = False, sorted({*errors, e.reason})
        else:
            warnings = _report_warnings(
                evaluation.report, root_mismatch_warning(unpacked.root_name, evaluation.name)
            )
            outside = outside_folder_warning(evaluation.body, unpacked.files)
            if outside is not None:
                warnings.append(outside)
            runtime_profile = evaluation.runtime_profile
            has_scripts = any(std.is_runnable_script(path) for path in unpacked.files)
    if ok and evaluation is not None:
        # 전역 후보 상한 밖의 후보는 코어 스캔이 SKILL.md를 읽지 않아 name이 없다
        # — 평가 값으로 채운다
        name = summary.name or evaluation.name[: std.MAX_DESCRIPTION_LENGTH]
        description = summary.description or evaluation.description[: std.MAX_DESCRIPTION_LENGTH]
    else:
        name, description = summary.name, summary.description
    return SkillInstallCandidateOut(
        path=rel_dir,
        name=name,
        description=description,
        file_count=summary.file_count,
        ok=ok,
        errors=errors,
        warnings=warnings,
        runtime_profile=runtime_profile,
        has_scripts=has_scripts,
        copy_of=None,
        reinstall=None,
    )


def _preview_archive(data: bytes, subpath: str | None) -> _Preview:
    """후보 목록과 후보별 미리보기(코어 후보 스캔·점검 + 오프라인 `evaluate_package`).

    코어 `zip_import_candidates`와 같은 공개 부품(`scan_import_archive`·`inspect_candidate`·
    `summarize_import_candidate`)을 쓰되, URL 경로(`/tree/<ref>/<path>`) 필터를 후보 상한
    (`MAX_IMPORT_CANDIDATES`)보다 **먼저** 건다 — 큰 저장소에서 경로를 좁히면 상한 밖 후보도 보인다.
    동기·CPU 작업이다 — 호출부가 스레드로 감싼다. 저장하지 않는다.
    """
    try:
        scan = pkg.scan_import_archive(data)
    except SkillValidationError as e:
        raise GithubInstallError(e.reason, reason_message(e.reason)) from e
    roots = {path.split("/", 1)[0] for path in scan.candidates}
    # codeload archive는 최상위 폴더(`<repo>-<sha>`) 하나에 담긴다
    if len(roots) != 1 or any("/" not in path for path in scan.candidates):
        raise GithubInstallError("no_skills", MSG_NO_SKILLS)
    root = roots.pop()
    selected: list[tuple[str, str]] = []
    for path in scan.candidates:
        rel_dir = path[len(root) + 1 : -len("SKILL.md")].rstrip("/")
        if subpath and rel_dir != subpath and not rel_dir.startswith(f"{subpath}/"):
            continue
        selected.append((path, rel_dir))
    shown = selected[: pkg.MAX_IMPORT_CANDIDATES]
    candidates: list[SkillInstallCandidateOut] = []
    for path, rel_dir in shown:
        layout = pkg.inspect_candidate(scan, path)
        summary = summarize_import_candidate(
            path,
            scan.skill_md.get(path),
            file_count=layout.file_count,
            errors=layout.errors,
            root_name=layout.root_name,
        )
        candidates.append(_preview_candidate(data, path, rel_dir, summary))
    return _Preview(
        archive_sha256=hashlib.sha256(data).hexdigest(),
        archive_root=root,
        candidates=candidates,
        truncated=len(selected) - len(shown),
    )


def _mark_copies(candidates: list[SkillInstallCandidateOut]) -> None:
    """같은 name의 후보는 대표 하나만 남기고 나머지를 사본으로 표시한다.

    대표: 점검 통과 → 저장소 `skills/` 아래 → 얕은 경로 → 경로 사전순.
    """
    groups: dict[str, list[SkillInstallCandidateOut]] = defaultdict(list)
    for candidate in candidates:
        if candidate.name:
            groups[candidate.name].append(candidate)
    for group in groups.values():
        if len(group) < 2:
            continue
        representative = min(
            group,
            key=lambda c: (not c.ok, not c.path.startswith("skills/"), c.path.count("/"), c.path),
        )
        for candidate in group:
            if candidate is not representative:
                candidate.copy_of = representative.path


def _source_ref(owner: str, repo: str, sha: str, path: str) -> str:
    """출처 키 `github:<owner>/<repo>@<sha>:<path>`(코어 예시 형식) — owner/repo는 소문자(AC-8)."""
    return f"github:{owner.lower()}/{repo.lower()}@{sha}:{path}"


def _source_path(source_ref: Any, prefix: str) -> str | None:
    if not isinstance(source_ref, str) or not source_ref.startswith(prefix):
        return None
    sha, sep, path = source_ref[len(prefix) :].partition(":")
    if not sep or not _SHA_RE.fullmatch(sha):
        return None
    return path


async def own_github_installs(
    db: AsyncSession, user_id: uuid.UUID, owner: str, repo: str
) -> dict[str, Skill]:
    """요청자 본인의 가져온 스킬 중 이 저장소에서 온 것 — 저장소 안 경로 → 스킬(먼저 만든 것)."""
    prefix = f"github:{owner.lower()}/{repo.lower()}@"
    rows = await db.execute(
        select(Skill)
        .where(
            Skill.owner_id == user_id,
            Skill.origin == "imported",
            Skill.origin_detail["source_ref"].astext.startswith(prefix, autoescape=True),
        )
        .order_by(Skill.created_at, Skill.id)
    )
    out: dict[str, Skill] = {}
    for skill in rows.scalars().all():
        path = _source_path((skill.origin_detail or {}).get("source_ref"), prefix)
        if path is not None:
            out.setdefault(path, skill)
    return out


async def _latest_versions(db: AsyncSession, skill_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not skill_ids:
        return {}
    rows = await db.execute(
        select(SkillVersion.skill_id, func.max(SkillVersion.version))
        .where(SkillVersion.skill_id.in_(skill_ids))
        .group_by(SkillVersion.skill_id)
    )
    return {skill_id: int(version) for skill_id, version in rows.tuples().all()}


async def _shared_catalog_names(db: AsyncSession, user_id: uuid.UUID) -> dict[str, str]:
    """요청자 카탈로그의 org·system 항목 name → visibility.

    같은 name을 설치하면 본인 것이 그 항목을 가린다(코어 순위: 본인 > system > org).
    """
    catalog = await SkillCatalog.for_user(db, user_id)
    entries = [*catalog.entries, *(shadowed for shadowed, _winner in catalog.shadowed)]
    if not entries:
        return {}
    rows = await db.execute(
        select(Skill.id, Skill.visibility, Skill.owner_id).where(
            Skill.id.in_({entry.skill_id for entry in entries})
        )
    )
    info = {skill_id: (visibility, owner_id) for skill_id, visibility, owner_id in rows.all()}
    out: dict[str, str] = {}
    for entry in entries:
        visibility, owner_id = info.get(entry.skill_id, (None, None))
        if owner_id != user_id and visibility in ("org", "system"):
            out.setdefault(entry.name, visibility)
    return out


async def build_offer(db: AsyncSession, user: User, target: GithubTarget) -> SkillInstallOfferOut:
    """확인 카드 — archive를 1회 받아 후보·미리보기·사본·재설치·가림 경고를 만든다(저장 없음)."""
    resolved = await resolve_repo(target)
    data = await fetch_archive(resolved)
    preview = await anyio.to_thread.run_sync(partial(_preview_archive, data, target.path))
    if not preview.candidates:
        raise GithubInstallError("no_skills", MSG_NO_SKILLS)
    _mark_copies(preview.candidates)
    installs = await own_github_installs(db, user.id, resolved.owner, resolved.repo)
    latest = await _latest_versions(db, [skill.id for skill in installs.values()])
    shared = await _shared_catalog_names(db, user.id)
    for candidate in preview.candidates:
        skill = installs.get(candidate.path)
        if skill is not None:
            candidate.reinstall = SkillInstallReinstallOut(
                skill_id=skill.id, name=skill.name, latest_version=latest.get(skill.id)
            )
        visibility = shared.get(candidate.name or "")
        if visibility is not None:
            kind = "시스템" if visibility == "system" else "조직"
            candidate.warnings.append(
                _warning(
                    "product",
                    "shadows_catalog_skill",
                    f"같은 이름의 {kind} 스킬이 있습니다. 설치하면 본인 카탈로그에서 그 스킬을 "
                    "가립니다(그 스킬과 다른 사용자에게는 영향이 없습니다).",
                )
            )
    has_license = resolved.license_spdx or resolved.license_name
    return SkillInstallOfferOut(
        owner=resolved.owner,
        repo=resolved.repo,
        sha=resolved.sha,
        ref=resolved.ref,
        archive_sha256=preview.archive_sha256,
        archive_root=preview.archive_root,
        subpath=target.path,
        license=(
            SkillInstallLicenseOut(spdx_id=resolved.license_spdx, name=resolved.license_name)
            if has_license
            else None
        ),
        candidates=preview.candidates,
        truncated=preview.truncated,
        max_paths=MAX_INSTALL_PATHS,
    )


def offer_summary(offer: SkillInstallOfferOut) -> str:
    """채팅에 남는 답변 문장 — 카드는 저장하지 않으므로 새로고침 뒤에는 이 문장만 남는다."""
    representatives = [c for c in offer.candidates if c.copy_of is None]
    installable = sum(1 for c in representatives if c.ok)
    copies = len(offer.candidates) - len(representatives)
    folded = f", 같은 이름의 사본 {copies}개는 접어 두었습니다" if copies else ""
    return (
        f"{offer.owner}/{offer.repo}@{offer.sha[:7]}에서 스킬 후보 {len(representatives)}개를 "
        f"찾았습니다(설치 가능 {installable}개{folded}). 카드에서 설치할 스킬을 고른 뒤 "
        "「설치」를 누르세요. 누르기 전에는 아무것도 설치하지 않습니다. 카드는 이 화면에서만 "
        "보입니다 — 새로고침했다면 같은 요청을 다시 보내세요."
    )


# ---------- 설치 ----------


def _failed(path: str, reason: str) -> SkillInstallItemOut:
    return SkillInstallItemOut(
        path=path,
        status="failed",
        skill_id=None,
        name=None,
        version=None,
        has_scripts=False,
        reason=reason,
        message=reason_message(reason),
    )


async def _undo_draft(skill_id: uuid.UUID, backup: dict[str, bytes] | None) -> None:
    """실패한 경로의 draft 정리 — DB는 세이브포인트로 되돌아가지만 스토리지 draft는 그렇지 않다.

    코어 `save_draft`는 prefix를 지우고 다시 쓴다. 재설치면 읽어 둔 원래 draft를 되쓰고, 새 스킬이면
    행이 사라졌으므로 고아 draft를 지운다. 정리 실패는 로그만 남긴다(원래 실패 사유를 덮지 않는다).
    """
    try:
        if backup is not None:
            await anyio.to_thread.run_sync(pkg.save_draft, skill_id, backup)
        else:
            await anyio.to_thread.run_sync(pkg.delete_draft, skill_id)
    except Exception:  # noqa: BLE001
        logger.exception("skill_install_github_draft_undo_failed")


async def _install_one(
    db: AsyncSession,
    user: User,
    user_id: uuid.UUID,
    body: SkillInstallGithubRequest,
    path: str,
) -> SkillInstallItemOut:
    """경로 하나 — 대상 찾기 → 가져오기(digest 고정) → 동결 → 감사 → 커밋.

    DB 변경은 세이브포인트 안에서 하고, 실패하면 draft도 원래대로 되돌린다(`_undo_draft`).
    """
    selected = f"{body.archive_root}/{path}/SKILL.md" if path else f"{body.archive_root}/SKILL.md"
    source_ref = _source_ref(body.owner, body.repo, body.sha, path)
    repo_key = f"{body.owner.lower()}/{body.repo.lower()}"
    draft_owner: uuid.UUID | None = None
    backup: dict[str, bytes] | None = None
    try:
        async with db.begin_nested():
            # 같은 출처의 동시 설치를 직렬화한다
            # — 둘 다 「대상 없음」으로 보고 스킬을 둘 만들지 않게
            await db.execute(
                select(func.pg_advisory_xact_lock(func.hashtext(f"{user_id}:{repo_key}:{path}")))
            )
            target = (await own_github_installs(db, user_id, body.owner, body.repo)).get(path)
            if target is not None:
                backup = await anyio.to_thread.run_sync(pkg.load_draft_files, target.id)
                draft_owner = target.id
            result = await skill_service.import_url_detailed(
                db,
                user,
                codeload_url(body.owner, body.repo, body.sha),
                sha256=body.archive_sha256,
                selected_skill_path=selected,
                target_skill_id=target.id if target is not None else None,
                source_ref=source_ref,
            )
            skill = result.skill
            draft_owner = skill.id
            version, _wrapper = await skill_service.freeze_skill(db, skill, user, publish=False)
            reimport = target is not None
            # URL 원문은 남기지 않는다(AC-4) — 출처는 저장소·SHA·경로·archive digest로 남긴다
            await record(
                db,
                user_id,
                "skill.import",
                skill.name,
                {
                    "source": "github",
                    "skill_id": str(skill.id),
                    "repo": repo_key,
                    "sha": body.sha,
                    "path": path,
                    "archive_sha256": body.archive_sha256,
                    "reimport": reimport,
                },
            )
            await record(
                db,
                user_id,
                "skill.freeze",
                skill.name,
                {
                    "skill_id": str(skill.id),
                    "version_id": str(version.id),
                    "version": version.version,
                    "source": "github",
                },
            )
            files = [entry.get("path", "") for entry in version.file_tree or []]
            item = SkillInstallItemOut(
                path=path,
                status="reinstalled" if reimport else "installed",
                skill_id=skill.id,
                name=skill.name,
                version=version.version,
                has_scripts=any(std.is_runnable_script(str(file)) for file in files),
                reason=None,
                message=None,
            )
        await db.commit()
    except Exception:
        if draft_owner is not None:
            await _undo_draft(draft_owner, backup)
        raise
    return item


async def install_from_github(
    db: AsyncSession, user: User, body: SkillInstallGithubRequest
) -> SkillInstallGithubOut:
    """고른 경로를 요청자 private 스킬로 설치하고 표준 version을 동결한다(경로별 커밋).

    archive는 코어 `import_url_detailed`가 경로마다 다시 받는다 — 호스트 정책은 없지만 사설 IP
    검사를 홉마다 하고, 내용은 `archive_sha256`으로 고정된다(`digest_mismatch`).
    """
    user_id = user.id  # 세이브포인트 롤백 뒤에도 쓴다
    items: list[SkillInstallItemOut] = []
    archive_failure: str | None = None
    for path in body.paths:
        if archive_failure is not None:
            items.append(_failed(path, archive_failure))
            continue
        try:
            item = await _install_one(db, user, user_id, body, path)
        except (SkillValidationError, SkillParseError) as e:
            items.append(_failed(path, e.reason))
            if e.reason in _ARCHIVE_REASONS:
                archive_failure = e.reason
            continue
        except skill_service.SkillError:
            # 대상 스킬이 그사이 지워졌거나 가져온 스킬이 아니다(코어가 잠금 뒤 다시 본다)
            items.append(_failed(path, "skill_gone"))
            continue
        except Exception:  # noqa: BLE001 — 한 경로의 저장소 장애가 다른 경로의 결과를 지우지 않게
            logger.exception("skill_install_github_path_failed")
            items.append(_failed(path, "install_failed"))
            continue
        items.append(item)
    return SkillInstallGithubOut(owner=body.owner, repo=body.repo, sha=body.sha, items=items)
