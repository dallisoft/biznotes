"""스킬 DB 오케스트레이션 (skill_plan.md §2.4, §9.4, §13 · 표준 계획 D4·D5·D8·D10).

생성/ZIP import/freeze(+wrapper Agent upsert)/clone/export/권한 헬퍼.
공개(포인터 이동)는 이 모듈이 하지 않는다 — `exposure.py`의 관문만 한다(D8).
스토리지 접근(package.py)은 동기 — 비동기 호출부에서 anyio.to_thread로 감싼다.
"""

import hashlib
import hmac
import json
import re
import uuid
import warnings
from dataclasses import dataclass
from functools import partial
from typing import Any

import anyio
import httpx
from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.config import get_settings
from biznotes_core.core.ssrf import SSRFBlockedError, ssrf_safe_fetch
from biznotes_core.models import (
    Agent,
    AgentRun,
    Skill,
    SkillVersion,
    User,
)
from biznotes_core.models.skill import SKILL_RUNTIME_LEGACY
from biznotes_core.services.skills import builder, exposure
from biznotes_core.services.skills import package as pkg
from biznotes_core.services.skills import runtime as skill_runtime
from biznotes_core.services.skills import stages as skill_stages
from biznotes_core.services.skills.parser import SkillParseError, parse_skill_md
from biznotes_core.services.skills.report import (
    ImportCandidate,
    PackageEvaluation,
    evaluate_package,
    failure_report,
    root_mismatch_warning,
    summarize_import_candidate,
)
from biznotes_core.services.skills.validator import (
    SkillValidationError,
    validate_path,
    validate_slug,
)

STARTER_SKILL_MD = """---
name: {slug}
description: {description}
---

# {name}

Describe the workflow this skill should follow. The notebook context will be provided
under <notebook_context> and user parameters under <user_params>.
"""

# 표준 런타임이 켜진 배포의 새 스킬 기본값(S-HCI-6, D11) — 제품 매니페스트 없이 표준 스킬로
# 시작한다. 동결하면 `standard_agent_v1`이 된다.
STANDARD_STARTER_SKILL_MD = """---
name: {slug}
description: {description_yaml}
---

# {name}

## 언제 쓰나

이 스킬이 맡을 요청을 적는다.

## 절차

1. 요청을 확인한다.
2. 노트북 자료가 필요하면 rag_search·source_load로 찾는다.
3. 결과를 정리해 답한다.

긴 참고 자료는 references/ 아래에 두고, 필요할 때만 read_skill_file로 읽게 한다.
"""

STARTER_MANIFEST = {
    "schema_version": "1.0",
    "mode": "instruction",
    "use_rag": True,
    "rag_query": "{params.topic}",
    # top_k는 넣지 않는다 — 생략하면 어드민 RAG 설정을 따른다(#45). 스킬이 발췌 수를
    # 고정해야 할 때만 작성자가 명시한다.
    "input": {"mode": "rag", "scope": "all", "max_sources": 5, "max_chars": 60000},
    "params_schema": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "title": "주제 / 지시사항",
                "default": "선택한 소스의 핵심 내용",
            }
        },
        "required": ["topic"],
    },
    "artifact": {"name": "result.md", "content_type": "text/markdown"},
    "permissions": {"rag": True, "llm": True, "tts": False},
}

# 시드 version의 표식(`created_by IS NULL AND changelog='seed'`) — 사용자 freeze가 흉내 내지
# 못하게 한다. 백필이 이 표식으로 시드 스킬을 식별한다(D13, R4-13).
SEED_CHANGELOG = "seed"
_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# 출처 키(#402) — 제품이 넘기는 불투명 문자열(예: `github:owner/repo@sha:path`). URL·토큰 원문이
# 실수로 저장되지 않게 `scheme:`으로 시작하고 공백·`?`·`#`·`://`가 없는 모양만 받는다.
_SOURCE_REF_RE = re.compile(r"[a-z][a-z0-9+.-]{0,31}:")
_SOURCE_REF_MAX_BYTES = 512
# 다시 가져오기가 보존하는 계보 키 — 나머지 `origin_detail`은 새 가져오기 출처로 바뀐다
_LINEAGE_KEYS = ("lineage", "cloned_from")


class SkillError(ValueError):
    """스킬 도메인 오류 (권한/상태 위반)."""


class SkillCloneUnavailable(SkillError):
    """비소유자가 복제할 공개 version이 없다 — draft로 대체하지 않는다(R2-01)."""


class SkillGone(SkillError):
    """잠그려던 스킬이 이미 없다 — 같은 스킬을 동시에 지운 요청이 먼저 끝났다(#116)."""


class SkillNotImported(SkillError):
    """다시 가져오기 대상이 가져온(`imported`) 스킬이 아니다(#402 D2)."""


@dataclass
class ImportResult:
    skill: Skill
    import_report: dict[str, Any]


@dataclass
class ImportCandidates:
    """후보 조회 결과(#402) — 저장하지 않는다."""

    candidates: list[ImportCandidate]
    truncated: int  # 후보 상한을 넘어 요약하지 않은 후보 수
    archive_sha256: str


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "skill"


async def _unique_slug(db: AsyncSession, owner_id: uuid.UUID | None, base: str) -> str:
    """새 스킬의 slug — 소유자 범위와 org·system 슬롯 양쪽에서 비어 있는 값(-2, -3 접미).

    org·system slug는 전역 유일이라, 공개 slug와 겹치게 만들면 나중에 공개할 때 409
    `slug_conflict`가 된다(#412). 다른 사용자의 private slug는 피하지 않는다. 기존 스킬의
    slug는 바꾸지 않는다(slug는 불변이다).
    """
    candidate = base
    n = 1
    while True:
        existing = await db.scalar(
            select(func.count())
            .select_from(Skill)
            .where(
                Skill.slug == candidate,
                or_(
                    Skill.owner_id == owner_id,
                    Skill.visibility.in_(exposure.EXPOSED_VISIBILITIES),
                ),
            )
        )
        if not existing:
            return candidate
        n += 1
        candidate = f"{base}-{n}"


async def get_skill_wrapper_map(
    db: AsyncSession, skill_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[uuid.UUID, dict[str, Any], dict[str, Any]]]:
    """Return wrapper facts derived from the persisted skill-version relation.

    wrapper의 params_schema·input은 **가장 최근 freeze** 기준이다. 비소유자에게 보일 값은
    `get_view_versions`로 노출 version에서 읽는다(D8).
    """
    if not skill_ids:
        return {}
    rows = await db.execute(
        select(SkillVersion.skill_id, Agent.id, Agent.params_schema, SkillVersion.manifest)
        .join(Agent, Agent.skill_version_id == SkillVersion.id)
        .where(SkillVersion.skill_id.in_(skill_ids), Agent.source_kind == "skill_package")
    )
    return {
        skill_id: (agent_id, schema, (manifest or {}).get("input") or {})
        for skill_id, agent_id, schema, manifest in rows.all()
    }


async def get_wrapper_ids(
    db: AsyncSession, skill_ids: list[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """skill_id → wrapper agent_id (실행 핸들)."""
    if not skill_ids:
        return {}
    rows = await db.execute(
        select(SkillVersion.skill_id, Agent.id)
        .join(Agent, Agent.skill_version_id == SkillVersion.id)
        .where(SkillVersion.skill_id.in_(skill_ids), Agent.source_kind == "skill_package")
    )
    return dict(rows.tuples().all())


async def get_view_versions(
    db: AsyncSession, skills: list[Skill], user: User
) -> dict[uuid.UUID, SkillVersion]:
    """skill_id → 이 사용자가 보는 version(소유자는 최신 freeze, 그 밖은 노출 version)."""
    wanted = {
        skill.id: vid
        for skill in skills
        if (vid := exposure.visible_version_id(skill, user.id)) is not None
    }
    if not wanted:
        return {}
    rows = await db.execute(select(SkillVersion).where(SkillVersion.id.in_(wanted.values())))
    by_id = {version.id: version for version in rows.scalars().all()}
    return {
        skill_id: by_id[vid]
        for skill_id, vid in wanted.items()
        if vid in by_id and by_id[vid].skill_id == skill_id
    }


# ---------- 권한 (§13, D8) ----------


def can_view(skill: Skill, user: User) -> bool:
    """소유자이거나 비소유자에게 노출된 version이 있다(enabled·org/system·포인터)."""
    return exposure.can_view(skill, user.id)


def can_run(skill: Skill, user: User) -> bool:
    """실행할 version이 있다 — `resolve_run_version_id(skill, user) is not None`(D8)."""
    return exposure.resolve_run_version_id(skill, user.id) is not None


def can_edit(skill: Skill, user: User) -> bool:
    return skill.owner_id == user.id


async def lock_skill_row(db: AsyncSession, skill_id: uuid.UUID) -> None:
    """스킬 행을 `FOR UPDATE`로 잠근다 — draft를 읽고 쓰는 경로를 직렬화한다(#402 리뷰).

    freeze·다시 가져오기·draft 파일 쓰기가 draft를 만지기 **전에** 이 잠금을 잡는다. 그래야 freeze가
    검증·해시한 파일과 동결한 파일이 같다. 순서는 스킬 행 → wrapper 행으로 삭제·run staging과
    같다. 잠금은 커밋·롤백까지 간다. 스킬이 이미 없으면 `SkillGone`이다.
    """
    locked = await db.scalar(select(Skill.id).where(Skill.id == skill_id).with_for_update())
    if locked is None:
        raise SkillGone("스킬을 찾을 수 없습니다")


# ---------- 생성 / import ----------


async def create_skill(
    db: AsyncSession, owner: User, name: str, description: str
) -> Skill:
    base_slug = slugify(name)
    slug = await _unique_slug(db, owner.id, base_slug)
    skill = Skill(
        owner_id=owner.id,
        name=name,
        description=description,
        slug=slug,
        visibility="private",
        status="draft",
        origin="authored",
    )
    db.add(skill)
    await db.flush()

    settings = get_settings()
    if settings.skill_standard_runtime_enabled:
        files = {
            # description은 사용자 입력이다 — YAML 큰따옴표 스칼라로 인용해야 `:`·`#`·줄바꿈이
            # frontmatter를 깨거나 키를 주입하지 못한다. 비면 표준 자격(description 필수)을 잃으므로
            # 기본 문구를 넣는다(리뷰 H-02).
            "SKILL.md": STANDARD_STARTER_SKILL_MD.format(
                slug=slug,
                name=name,
                description_yaml=json.dumps(
                    description.strip() or f"{name} 스킬", ensure_ascii=False
                ),
            ).encode("utf-8")
        }
    else:
        files = {
            "SKILL.md": STARTER_SKILL_MD.format(
                slug=slug, name=name, description=description
            ).encode("utf-8"),
            settings.skill_manifest_filename: json.dumps(
                STARTER_MANIFEST, ensure_ascii=False, indent=2
            ).encode("utf-8"),
        }
    await anyio.to_thread.run_sync(pkg.save_draft, skill.id, files)
    return skill


async def _create_draft_from_files(
    db: AsyncSession,
    owner: User,
    files: dict[str, bytes],
    *,
    origin: str = "authored",
    origin_detail: dict[str, Any] | None = None,
) -> tuple[Skill, PackageEvaluation]:
    """검증된 파일 dict → 사용자 private draft 스킬 생성 (ZIP import·아티팩트 변환 공용)."""
    evaluation = evaluate_package(files)

    base_slug = slugify(evaluation.name) or "skill"
    try:
        validate_slug(base_slug)
    except SkillValidationError:
        base_slug = "skill"
    slug = await _unique_slug(db, owner.id, base_slug)

    skill = Skill(
        owner_id=owner.id,
        name=evaluation.name,
        description=evaluation.description,
        slug=slug,
        visibility="private",
        status="draft",
        origin=origin,
        origin_detail=origin_detail or {},
    )
    db.add(skill)
    await db.flush()
    await anyio.to_thread.run_sync(pkg.save_draft, skill.id, files)
    return skill, evaluation


async def import_zip(
    db: AsyncSession, owner: User, data: bytes, *, selected_skill_path: str | None = None
) -> Skill:
    """ZIP → 검증 → draft 스킬 생성 (§8.2). 보고서가 필요하면 `import_zip_detailed`."""
    result = await import_zip_detailed(
        db, owner, data, selected_skill_path=selected_skill_path
    )
    return result.skill


async def import_zip_detailed(
    db: AsyncSession,
    owner: User,
    data: bytes,
    *,
    selected_skill_path: str | None = None,
    target_skill_id: uuid.UUID | None = None,
    source_ref: str | None = None,
) -> ImportResult:
    """ZIP·`.skill` → 검증 → draft 스킬 + 가져오기 보고서(D4).

    표준 위반은 거부하지 않고 `import_report.standard`로 보고한다. 루트 폴더가 `name`과
    다르면 경고 후 수용하고 원래 이름을 남긴다(S-HCI-2). 정책 위반·파싱 불가·필수 필드
    누락은 예외로 거부한다. ``target_skill_id``가 있으면 새 스킬 대신 그 스킬의 draft를
    바꾼다(#402, 호출부가 권한을 먼저 확인한다).
    """
    origin_detail = _import_origin_detail({"source": "zip"}, source_ref)
    unpacked = await anyio.to_thread.run_sync(
        partial(pkg.unpack_zip_detailed, data, selected_skill_path=selected_skill_path)
    )
    return await _store_import(
        db, owner, unpacked, origin_detail=origin_detail, target_skill_id=target_skill_id
    )


def _import_origin_detail(detail: dict[str, Any], source_ref: str | None) -> dict[str, Any]:
    """가져오기 출처 — `source_ref`는 값이 있을 때만 검증해 담는다(없으면 기존 모양 그대로)."""
    if source_ref is None:
        return detail
    # 사유 문구에 입력값을 담지 않는다 — 422 본문으로 그대로 돌아간다.
    if (
        not source_ref.isprintable()
        or len(source_ref.encode("utf-8")) > _SOURCE_REF_MAX_BYTES
        or _SOURCE_REF_RE.match(source_ref) is None
        or any(ch.isspace() or ch in "?#" for ch in source_ref)
        or "://" in source_ref
    ):
        raise SkillValidationError(
            "bad_source_ref",
            "source_ref는 `scheme:값` 형식이어야 합니다(512B 이하, 공백·?·#·:// 불가)",
        )
    return {**detail, "source_ref": source_ref}


async def _store_import(
    db: AsyncSession,
    owner: User,
    unpacked: pkg.UnpackedPackage,
    *,
    origin_detail: dict[str, Any],
    target_skill_id: uuid.UUID | None,
) -> ImportResult:
    if target_skill_id is None:
        return await _import_unpacked(db, owner, unpacked, origin_detail=origin_detail)
    return await _reimport_unpacked(
        db, owner, target_skill_id, unpacked, origin_detail=origin_detail
    )


def _import_report(evaluation: PackageEvaluation, root_name: str | None) -> dict[str, Any]:
    import_report = dict(evaluation.report)
    import_report["root_name"] = root_name
    import_report["root_mismatch"] = root_mismatch_warning(root_name, evaluation.name)
    return import_report


async def _import_unpacked(
    db: AsyncSession,
    owner: User,
    unpacked: pkg.UnpackedPackage,
    *,
    origin_detail: dict[str, Any],
) -> ImportResult:
    """이미 안전하게 언팩한 파일을 저장한다.

    로컬 ZIP과 원격 archive는 저장·표준 보고 규칙을 공유한다. origin_detail에는
    인증 정보가 섞일 수 있는 원격 URL 원문을 저장하지 않는다.
    """
    skill, evaluation = await _create_draft_from_files(
        db,
        owner,
        unpacked.files,
        origin="imported",
        origin_detail={**origin_detail, "root_name": unpacked.root_name},
    )
    return ImportResult(skill=skill, import_report=_import_report(evaluation, unpacked.root_name))


async def _reimport_unpacked(
    db: AsyncSession,
    owner: User,
    target_skill_id: uuid.UUID,
    unpacked: pkg.UnpackedPackage,
    *,
    origin_detail: dict[str, Any],
) -> ImportResult:
    """검증한 파일로 소유자의 가져온 스킬 draft를 바꾼다(#402 D3) — 동결하지 않는다.

    검증(`evaluate_package`)을 통과한 **뒤에** 스킬 행을 잠근다. slug·visibility·enabled·
    published/current 포인터는 그대로다. 새 version은 사용자가 freeze할 때 생기고, `scripts`·
    공개 승인은 version 단위라 그때 다시 검토된다. 잠금 뒤에도 권한을 다시 본다(라우터 확인과
    잠금 사이의 변경). draft 교체(`save_draft`)는 원자적이지 않다.
    """
    evaluation = evaluate_package(unpacked.files)
    skill = await db.scalar(
        select(Skill)
        .where(Skill.id == target_skill_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if skill is None or not can_edit(skill, owner):
        raise SkillGone("스킬을 찾을 수 없습니다")
    if skill.origin != "imported":
        raise SkillNotImported("가져온 스킬만 다시 가져올 수 있습니다")
    previous = skill.origin_detail or {}
    kept = {key: previous[key] for key in _LINEAGE_KEYS if key in previous}
    # JSONB는 제자리 수정이 감지되지 않는다 — 새 dict를 대입한다
    skill.origin_detail = {**kept, **origin_detail, "root_name": unpacked.root_name}
    await anyio.to_thread.run_sync(pkg.save_draft, skill.id, unpacked.files)
    import_report = _import_report(evaluation, unpacked.root_name)
    import_report["reimported"] = True
    return ImportResult(skill=skill, import_report=import_report)


def _fetch_import_archive(url: str) -> bytes:
    """원격 archive를 SSRF 관문을 거쳐 제한된 크기로 받는다."""
    settings = get_settings()
    try:
        response = ssrf_safe_fetch(
            url,
            allow_private=settings.ssrf_allow_private,
            max_bytes=settings.skill_import_max_upload_bytes,
        )
    except SSRFBlockedError as e:
        raise SkillValidationError("ssrf_blocked", str(e)) from e
    except httpx.HTTPError as e:
        raise SkillValidationError(
            "remote_fetch_failed", "원격 archive를 가져올 수 없습니다"
        ) from e
    if not 200 <= response.status_code < 300:
        raise SkillValidationError(
            "remote_http_status", f"원격 archive 요청이 HTTP {response.status_code}로 실패했습니다"
        )
    return response.content


def _summarize_candidates(scan: pkg.ArchiveScan, data: bytes) -> ImportCandidates:
    shown = scan.candidates[: pkg.MAX_IMPORT_CANDIDATES]
    candidates = []
    for path in shown:
        layout = pkg.inspect_candidate(scan, path)
        candidates.append(
            summarize_import_candidate(
                path,
                scan.skill_md.get(path),
                file_count=layout.file_count,
                errors=layout.errors,
                root_name=layout.root_name,
            )
        )
    return ImportCandidates(
        candidates=candidates,
        truncated=len(scan.candidates) - len(shown),
        archive_sha256=hashlib.sha256(data).hexdigest(),
    )


def zip_import_candidates(data: bytes) -> ImportCandidates:
    """업로드 ZIP·`.skill`의 후보와 사전 점검 요약(#402). 동기 — 호출부가 스레드로 감싼다."""
    return _summarize_candidates(pkg.scan_zip_archive(data), data)


def _remote_import_candidates(data: bytes) -> ImportCandidates:
    return _summarize_candidates(pkg.scan_import_archive(data), data)


async def url_import_candidates(url: str) -> ImportCandidates:
    """URL archive를 가져오기와 같은 SSRF 관문으로 받아 후보를 요약한다(#402). 저장하지 않는다.

    응답의 `archive_sha256`을 이어지는 `import_url_detailed`의 ``sha256``으로 넘기면 조회와
    가져오기 사이에 archive가 바뀐 경우 unpack 전에 `digest_mismatch`로 거부된다.
    """
    data = await anyio.to_thread.run_sync(_fetch_import_archive, url)
    return await anyio.to_thread.run_sync(_remote_import_candidates, data)


async def import_url_detailed(
    db: AsyncSession,
    owner: User,
    url: str,
    *,
    sha256: str | None = None,
    selected_skill_path: str | None = None,
    target_skill_id: uuid.UUID | None = None,
    source_ref: str | None = None,
) -> ImportResult:
    """URL의 ZIP·`.skill`·tar.gz를 SSRF/다이제스트 검증 뒤 private draft로 가져온다.

    ``sha256``은 선택적인 64자리 hex digest다. 원격 URL은 query에 credential을 담을 수 있어
    DB origin detail과 감사 detail에 보존하지 않는다. ``target_skill_id``가 있으면 그 스킬의
    draft를 바꾼다(#402, 호출부가 fetch 전에 권한을 확인한다).
    """
    if sha256 is not None and not _SHA256_HEX_RE.fullmatch(sha256):
        raise SkillValidationError("bad_digest", "sha256은 64자리 16진수여야 합니다")
    origin_detail = _import_origin_detail({"source": "url"}, source_ref)
    data = await anyio.to_thread.run_sync(_fetch_import_archive, url)
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if sha256 is not None and not hmac.compare_digest(actual_sha256, sha256.lower()):
        raise SkillValidationError("digest_mismatch", "원격 archive의 SHA-256이 일치하지 않습니다")
    unpacked = await anyio.to_thread.run_sync(
        partial(pkg.unpack_import_archive_detailed, data, selected_skill_path=selected_skill_path)
    )
    return await _store_import(
        db,
        owner,
        unpacked,
        origin_detail={**origin_detail, "archive_sha256": actual_sha256},
        target_skill_id=target_skill_id,
    )


def parse_package_artifact(content: bytes) -> dict[str, bytes]:
    """스킬 생성 도우미 아티팩트(JSON {skill_md, <매니페스트>_json}) → 파일 dict.

    LLM이 코드펜스로 감싸거나 약간 빗나가도 견디게 폴백 파싱한다.
    """
    raw = content.decode("utf-8", errors="replace").strip()

    skill_md: str | None = None
    manifest_obj: Any = None

    def _try_json(s: str) -> None:
        nonlocal skill_md, manifest_obj
        try:
            data = json.loads(s)
        except json.JSONDecodeError:
            return
        if isinstance(data, dict):
            skill_md = data.get("skill_md") or data.get("SKILL.md")
            manifest_name = get_settings().skill_manifest_filename
            manifest_obj = data.get(manifest_name.replace(".", "_")) or data.get(manifest_name)

    # 1) 통짜 JSON, 2) 전체가 ```json ... ``` 펜스로 감싸인 JSON
    _try_json(raw)
    if not skill_md:
        whole_fence = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", raw, re.DOTALL)
        if whole_fence:
            _try_json(whole_fence.group(1).strip())

    # 3) 폴백: 원문에서 ```markdown 블록(SKILL.md) + ```json 블록(manifest) 추출
    if not skill_md:
        md = re.search(r"```(?:markdown|md)?\s*\n(---\n.*?)\n```", raw, re.DOTALL)
        if md:
            skill_md = md.group(1)
    if manifest_obj is None:
        mj = re.findall(r"```json\s*\n(\{.*?\})\n```", raw, re.DOTALL)
        if mj:
            try:
                manifest_obj = json.loads(mj[-1])
            except json.JSONDecodeError:
                manifest_obj = None

    if not skill_md or not isinstance(skill_md, str):
        raise SkillValidationError(
            "bad_artifact", "아티팩트에서 SKILL.md를 추출하지 못했습니다"
        )
    files: dict[str, bytes] = {"SKILL.md": skill_md.encode("utf-8")}
    if isinstance(manifest_obj, dict):
        files[get_settings().skill_manifest_filename] = json.dumps(
            manifest_obj, ensure_ascii=False, indent=2
        ).encode("utf-8")
    return files


async def create_skill_from_package(
    db: AsyncSession, owner: User, files: dict[str, bytes]
) -> Skill:
    """파싱된 패키지 파일 dict → private draft 스킬 (1클릭 생성 브리지)."""
    skill, _ = await _create_draft_from_files(db, owner, files)
    return skill


# ---------- 검증 ----------


def _success_payload(evaluation: PackageEvaluation, file_count: int) -> dict[str, Any]:
    return {
        "ok": True,
        "name": evaluation.name,
        "description": evaluation.description,
        "file_count": file_count,
        "manifest": evaluation.manifest,
        "references": evaluation.references,
    }


async def validate_draft(skill: Skill) -> dict[str, Any]:
    """draft 파일을 로드해 검증만 수행 (저장 X). 정책 위반은 예외로 올린다."""
    files = await anyio.to_thread.run_sync(pkg.load_draft_files, skill.id)
    evaluation = evaluate_package(files)
    return _success_payload(evaluation, len(files))


def validate_files_report(files: dict[str, bytes]) -> dict[str, Any]:
    """저장 여부와 무관한 패키지 검증 결과. 폼 미리보기와 draft 검증이 공유한다."""
    try:
        evaluation = evaluate_package(files)
    except (SkillValidationError, SkillParseError) as e:
        return {"ok": False, "reason": e.reason, "message": str(e), "report": failure_report(e)}
    return {**_success_payload(evaluation, len(files)), "report": evaluation.report}


async def validate_draft_report(skill: Skill) -> dict[str, Any]:
    """draft 검증 결과 + 네 층 보고서(D5). 예외를 던지지 않는다.

    기존 응답 키를 모두 유지하고 `report`를 더한다. `ok`는 "정책 오류 없음"이다 —
    표준 위반(legacy)이어도 `ok=true`다.
    """
    files = await anyio.to_thread.run_sync(pkg.load_draft_files, skill.id)
    return validate_files_report(files)


# ---------- freeze (+ wrapper Agent upsert §2.4) ----------


async def _find_wrapper(db: AsyncSession, skill_id: uuid.UUID) -> Agent | None:
    """스킬의 안정적인 wrapper Agent (현재 어느 버전을 가리키든 skill_id로 역추적)."""
    return await exposure.find_wrapper(db, skill_id)


async def _materialize_version(
    db: AsyncSession,
    skill: Skill,
    *,
    created_by_id: uuid.UUID | None,
    version_status: str,
    changelog: str | None,
) -> tuple[SkillVersion, Agent]:
    """draft 파일을 검증·해시·동결해 SkillVersion + wrapper Agent를 만든다(권한 검사 없음).

    호출부가 권한을 책임진다. 시드(system)와 사용자 freeze가 공유. 공개 포인터는 옮기지
    않는다 — 공개는 `exposure.py`의 관문만 한다(D8). draft를 두 번(검증·복사) 읽으므로 그 전에
    스킬 행을 잠가 다시 가져오기·draft 쓰기와 직렬화한다.
    """
    await lock_skill_row(db, skill.id)
    files = await anyio.to_thread.run_sync(pkg.load_draft_files, skill.id)
    evaluation = evaluate_package(files)
    if evaluation.runtime_profile == SKILL_RUNTIME_LEGACY:
        # 노드 빌드가 가능한지(권한 게이트 포함) 사전 검증
        builder.build_nodes(builder.loaded_from_evaluation(evaluation, files))
    else:
        # [#202] 표준 경로에도 대응하는 사전 검증 — 그래프를 1회 조립해 본다.
        # legacy와 같은 위치(동결 직전)라 가져오기와 동결 사이에 같은 실패가 난다
        stages = skill_stages.parse_stages(dict(evaluation.standard_metadata))
        skill_runtime.staged_run_definition(
            stages, skill_name=evaluation.name, skill_body=evaluation.body
        )

    max_version = await db.scalar(
        select(func.coalesce(func.max(SkillVersion.version), 0)).where(
            SkillVersion.skill_id == skill.id
        )
    )
    version = SkillVersion(
        skill_id=skill.id,
        version=(max_version or 0) + 1,
        package_uri="",  # flush 후 id로 채움
        skill_md_hash=pkg.file_hash(files["SKILL.md"]),
        package_hash=pkg.package_hash(files),
        manifest=evaluation.manifest,
        file_tree=pkg.build_file_tree(files),
        security_report=evaluation.report,
        status=version_status,
        created_by=created_by_id,
        changelog=changelog,
        validation_profile=evaluation.validation_profile,
        runtime_profile=evaluation.runtime_profile,
        standard_metadata=evaluation.standard_metadata,
    )
    db.add(version)
    await db.flush()
    version.package_uri = pkg.version_prefix(version.id)

    # draft 트리를 version prefix로 동결 복사
    await anyio.to_thread.run_sync(pkg.freeze_draft_to_version, skill.id, version.id)

    skill.current_version_id = version.id
    wrapper = await _upsert_wrapper(db, skill, version, evaluation.manifest)
    return version, wrapper


async def freeze_skill(
    db: AsyncSession,
    skill: Skill,
    user: User,
    *,
    publish: bool = False,
    changelog: str | None = None,
) -> tuple[SkillVersion, Agent]:
    """draft → 불변 version 동결 + 스킬의 wrapper Agent upsert (§9.4).

    `publish=True`는 deprecated다(D8 경로 6). 동결한 뒤 version publish 관문
    (`exposure.publish_version`)에 위임하고, 검토가 필요하면 `exposure.ReviewPending`을
    던진다. `0.13.0` 이상에서 두 제품의 이관을 확인한 뒤 제거한다 — 대신
    `freeze_skill(publish=False)` 뒤 `exposure.publish_version`을 쓴다.
    """
    if not can_edit(skill, user):
        raise SkillError("스킬을 편집할 권한이 없습니다")
    if changelog is not None and changelog.strip() == SEED_CHANGELOG:
        raise SkillValidationError(
            "reserved_changelog", f"changelog '{SEED_CHANGELOG}'는 시드 전용입니다"
        )
    if publish:
        warnings.warn(
            "freeze_skill(publish=True)는 deprecated입니다. freeze 뒤 "
            "exposure.publish_version을 쓰세요(0.13.0 이상에서 제거 예정).",
            DeprecationWarning,
            stacklevel=2,
        )
    version, wrapper = await _materialize_version(
        db,
        skill,
        created_by_id=user.id,
        version_status="draft_frozen",
        changelog=changelog,
    )
    if publish:
        outcome = await exposure.publish_version(db, skill, version, actor_id=user.id)
        if outcome.request is not None:
            raise exposure.ReviewPending(outcome.request)
    return version, wrapper


async def _upsert_wrapper(
    db: AsyncSession, skill: Skill, version: SkillVersion, manifest: dict[str, Any]
) -> Agent:
    """스킬마다 안정적인 wrapper Agent 1개 — 실행 핸들이다(D8 wrapper 행).

    비소유자가 소비하는 이름·설명·params_schema·version은 wrapper가 아니라 노출
    version에서 읽는다. wrapper 필드는 구 경로 호환용으로만 갱신한다.
    """
    from biznotes_core.services.skills.runtime import params_schema_for_version

    wrapper = await _find_wrapper(db, skill.id)
    # 표준 version은 매니페스트가 없고 `request` 하나를 받는다(D6-6)
    params_schema = (
        params_schema_for_version(version)
        if version.runtime_profile != SKILL_RUNTIME_LEGACY
        else manifest.get("params_schema") or {}
    )
    scope = "system" if skill.visibility == "system" else "user"
    if wrapper is None:
        wrapper = Agent(
            name=skill.name,
            description=skill.description,
            icon="🧩",
            kind="skill",
            source_kind="skill_package",
            definition={},
            params_schema=params_schema,
            scope=scope,
            owner_id=skill.owner_id,
            enabled=skill.enabled,
            skill_version_id=version.id,
        )
        db.add(wrapper)
        await db.flush()
    else:
        wrapper.name = skill.name
        wrapper.description = skill.description
        wrapper.params_schema = params_schema
        wrapper.scope = scope
        wrapper.owner_id = skill.owner_id
        wrapper.enabled = skill.enabled
        wrapper.skill_version_id = version.id
        wrapper.version += 1
    return wrapper


# ---------- 삭제 (wrapper 처분, #116) ----------


@dataclass(frozen=True)
class WrapperRemoval:
    """스킬 삭제 때 처분한 wrapper Agent id. audit `skill.delete` detail에 그대로 남긴다."""

    deleted: tuple[uuid.UUID, ...]  # 참조가 없어 지운 wrapper
    disabled: tuple[uuid.UUID, ...]  # run 참조가 있어 비활성으로 보존한 wrapper


async def remove_skill_wrappers(db: AsyncSession, skill_id: uuid.UUID) -> WrapperRemoval:
    """스킬을 지우기 **전에** 그 스킬의 wrapper를 모두 처분한다(계획 `skill-delete-wrappers`).

    지우지 않으면 version CASCADE와 `skill_version_id` SET NULL로 wrapper가 활성 고아로 남는다.
    `agent_runs` 참조가 없으면 지우고, 있으면 실행 이력을 잃지 않도록 `enabled=false`로
    보존한다. 커밋하지 않는다 — 같은 트랜잭션에서 스킬을 지운다.
    스킬을 지운 뒤에는 조인으로 wrapper를 찾을 수 없으므로 반드시 먼저 부른다.

    잠금 순서는 **스킬 행 → wrapper 행**이다. run staging(`agent_runs._pin_skill_version`)도 run을
    INSERT하기 전에 스킬 행을 잠가 순서가 같으므로 교착하지 않는다(재리뷰 M-3). 진행 중인 freeze가
    wrapper를 새 version으로 옮기면 그 커밋을 기다린 뒤 찾는다(리뷰 M-2). 참조 확인은 잠금을 얻은
    뒤의 별도 문장이라 이미 커밋된 참조를 본다. 스킬이 이미 없으면(동시 삭제) `SkillGone`이다.

    bulk DELETE·UPDATE는 세션의 Agent 객체에 반영한다(`synchronize_session="fetch"`). 비활성 행의
    `skill_version_id`는 스킬 삭제가 커밋될 때까지 메모리에 옛 값이 남는다.
    """
    locked = await db.scalar(select(Skill.id).where(Skill.id == skill_id).with_for_update())
    if locked is None:
        raise SkillGone("스킬을 찾을 수 없습니다")
    wrapper_ids = list(
        (
            await db.execute(
                select(Agent.id)
                .join(SkillVersion, Agent.skill_version_id == SkillVersion.id)
                .where(SkillVersion.skill_id == skill_id, Agent.source_kind == "skill_package")
                .order_by(Agent.id)
                .with_for_update(of=Agent)
            )
        ).scalars()
    )
    if not wrapper_ids:
        return WrapperRemoval(deleted=(), disabled=())
    referenced = set(
        (
            await db.execute(
                select(Agent.id).where(
                    Agent.id.in_(wrapper_ids),
                    exists().where(AgentRun.agent_id == Agent.id),
                )
            )
        ).scalars()
    )
    deleted = tuple(i for i in wrapper_ids if i not in referenced)
    disabled = tuple(i for i in wrapper_ids if i in referenced)
    if deleted:
        await db.execute(
            delete(Agent)
            .where(Agent.id.in_(deleted))
            .execution_options(synchronize_session="fetch")
        )
    if disabled:
        await db.execute(
            update(Agent)
            .where(Agent.id.in_(disabled))
            .values(enabled=False)
            .execution_options(synchronize_session="fetch")
        )
    return WrapperRemoval(deleted=deleted, disabled=disabled)


# ---------- 시드 (system 빌트인, §16.1 · D8 경로 7) ----------


async def seed_system_skill(
    db: AsyncSession,
    *,
    slug: str,
    name: str,
    description: str,
    skill_md: str,
    manifest: dict[str, Any],
) -> Skill:
    """시드 prompt skill을 system skill_package로 변환(멱등).

    - 조회는 `origin='system_seed' AND owner_id IS NULL AND slug=…`이고 visibility와
      무관하다. 강등된 시드가 있어도 같은 slug의 새 system 스킬을 만들지 않는다(R4-01).
    - 패키지 내용이 같으면 새 버전을 만들지 않는다(package_hash 비교).
    - 새 version의 공개는 `exposure.publish_seed_version`이 정한다(보류·강등 존중).
    - 새 시드의 slug를 다른 system 스킬이 쥐고 있으면 409 `slug_conflict`다(#412). 제품이 시드
      slug를 바꿔야 한다.
    """
    # 매니페스트 파일명은 제품 설정이다 — builder.load_skill_files가 같은 이름으로 읽는다(#68)
    files = {
        "SKILL.md": skill_md.encode("utf-8"),
        get_settings().skill_manifest_filename: json.dumps(
            manifest, ensure_ascii=False, indent=2
        ).encode("utf-8"),
    }
    new_hash = pkg.package_hash(files)

    skill = (
        await db.execute(
            select(Skill).where(
                Skill.origin == "system_seed",
                Skill.owner_id.is_(None),
                Skill.slug == slug,
            )
        )
    ).scalar_one_or_none()

    if skill is None:
        skill = Skill(
            owner_id=None,
            name=name,
            description=description,
            slug=slug,
            visibility="system",
            status="draft",
            origin="system_seed",
        )
        # 승격된 사용자 스킬이 같은 slug의 system 슬롯을 쥐고 있으면 INSERT가 전역 유니크
        # 위반으로 깨진다. 원인이 드러나게 409 `slug_conflict`로 거부한다(#412, 건너뛰지 않는다)
        await exposure._add_system_skill(db, skill)
    else:
        skill.name = name
        skill.description = description
        # 동일 패키지면 재동결 생략
        latest = (
            await db.execute(
                select(SkillVersion)
                .where(SkillVersion.skill_id == skill.id)
                .order_by(SkillVersion.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest is not None and latest.package_hash == new_hash:
            return skill

    await anyio.to_thread.run_sync(pkg.save_draft, skill.id, files)
    version, _ = await _materialize_version(
        db, skill, created_by_id=None, version_status="draft_frozen", changelog=SEED_CHANGELOG
    )
    await exposure.publish_seed_version(db, skill, version)
    return skill


# ---------- clone (§13.3 · D8 접근 지점) ----------


async def clone_skill(db: AsyncSession, src: Skill, user: User) -> Skill:
    """스킬 복제. 비소유자는 **노출 version만** 복사한다(R2-01).

    노출 version이 없으면 `SkillCloneUnavailable` — 소유자의 draft를 대신 복사하지 않는다.
    출처는 원본이 imported일 때만 imported를 물려받고, 계보는 `origin_detail.lineage`에
    남긴다(R3-10).
    """
    if not can_view(src, user):
        raise SkillError("클론할 권한이 없습니다")
    if exposure.is_owner(src, user.id):
        source_version_id = src.current_version_id
    else:
        source_version_id = exposure.exposed_version_id(src)
        if source_version_id is None:
            raise SkillCloneUnavailable("복제할 공개 version이 없습니다")

    lineage = list((src.origin_detail or {}).get("lineage") or [])
    if source_version_id is not None:
        lineage.append(str(source_version_id))
    base_slug = await _unique_slug(db, user.id, slugify(src.name))
    clone = Skill(
        owner_id=user.id,
        name=src.name,
        description=src.description,
        slug=base_slug,
        tags=list(src.tags or []),
        visibility="private",
        status="draft",
        forked_from_version_id=source_version_id,
        origin="imported" if src.origin == "imported" else "authored",
        origin_detail={"lineage": lineage, "cloned_from": str(src.id)},
    )
    db.add(clone)
    await db.flush()

    # 소스 파일: 동결 버전이 있으면 그 트리, 없으면(소유자 본인만) draft 트리를 복사
    if source_version_id is not None:
        files = await anyio.to_thread.run_sync(pkg.load_version_files, source_version_id)
    else:
        files = await anyio.to_thread.run_sync(pkg.load_draft_files, src.id)
    await anyio.to_thread.run_sync(pkg.save_draft, clone.id, files)

    src.clone_count += 1
    return clone


# ---------- 내보내기 (D10) ----------


def _export_root(files: dict[str, bytes], skill: Skill) -> str:
    """저장된 패키지의 디렉터리 이름 — 표준 `name`, 쓸 수 없으면 slug(D3)."""
    try:
        frontmatter, _ = parse_skill_md(files["SKILL.md"].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, SkillParseError):
        return skill.slug
    name = frontmatter.get("name")
    if isinstance(name, str) and name.strip():
        try:
            candidate = validate_path(name.strip())
        except SkillValidationError:
            return skill.slug
        if "/" not in candidate:
            return candidate
    return skill.slug


async def export_version_zip(
    version: SkillVersion,
    skill: Skill,
    *,
    layout: pkg.ExportLayout = "wrapped",
    portable: bool = False,
) -> tuple[str, bytes]:
    """동결 version → (파일명, ZIP 바이트). 저장된 바이트를 그대로 담는다.

    `portable=True`면 제품 매니페스트를 뺀다(S-HCI-8 기본은 포함). `portable=False`면
    다시 가져왔을 때 `package_hash`가 같다.
    """
    files = await anyio.to_thread.run_sync(pkg.load_version_files, version.id)
    root = _export_root(files, skill)
    exclude = (get_settings().skill_manifest_filename,) if portable else ()
    data = pkg.export_zip(files, root, layout=layout, exclude=exclude)
    return f"{root}-v{version.version}.zip", data


def version_metadata(version: SkillVersion) -> dict[str, Any]:
    """version 메타데이터 — 카탈로그 단계(1단계)가 읽는 값. 저장소를 읽지 않는다."""
    frontmatter = dict(version.standard_metadata or {})
    return {
        "version_id": version.id,
        "skill_id": version.skill_id,
        "version": version.version,
        "name": frontmatter.get("name") if isinstance(frontmatter.get("name"), str) else None,
        "description": (
            frontmatter.get("description")
            if isinstance(frontmatter.get("description"), str)
            else None
        ),
        "frontmatter": frontmatter,
        "validation_profile": version.validation_profile,
        "runtime_profile": version.runtime_profile,
        "report": version.security_report or {},
        "package_hash": version.package_hash,
    }
