"""스킬 패키지 검증 (skill_plan.md §5.3, §10.1).

파일 제한 / 경로 안전성 / manifest schema / permissions / secrets 스캔.
1차는 내부 신뢰 모델 — 외부 import는 백로그(§10).
"""

import posixpath
import re
from typing import Any

import structlog

from biznotes_core.config import get_settings
from biznotes_core.services.agents.validators import _JSON_VALIDATE_SCHEMAS
from biznotes_core.services.rag_chat import (
    _PER_SOURCE_LIMIT_MAX,
    _PER_SOURCE_LIMIT_MIN,
    _RAG_COVERAGES,
)

logger = structlog.get_logger()

# 파일 제한 (§5.3) — 기본값. 실제 한도는 설정(`skill_import_max_*`, 표준 계획 D14)에서 읽는다.
MAX_TOTAL_BYTES = 10 * 1024 * 1024  # 압축 해제 후 10MB
MAX_FILE_COUNT = 100
MAX_SINGLE_FILE_BYTES = 1 * 1024 * 1024  # 단일 파일 1MB

SUPPORTED_MODES = ("instruction",)  # workflow/mcp/script는 백로그(§5.2)

# 입력 소스 정책 (agent-input-source-plan.md) — mode×scope 두 직교 축
INPUT_MODES = ("rag", "full", "none")
INPUT_SCOPES = ("all", "selected")
MAX_INPUT_CHARS = 200_000  # full 모드 토큰 폭발 가드
# 한 실행이 끌어올 소스 상한 가드. legacy 스킬 `input.mode: full`의 `source_load` 노드 상한이다.
# 채팅 `source_load` 도구의 1회 상한(`nodes/llm.py` `SOURCE_LOAD_TOOL_HARD_CAPS`, 20개·
# 200,000자)과는 다른 경로라 개수를 맞추지 않는다. 노드는 한 번에 다 읽고(최대 50개·
# `MAX_INPUT_CHARS`자), 도구는 여러 번 나눠 읽는다(`offset`·`source_ids`, #447). 길이 상한은 같다.
MAX_INPUT_SOURCES = 50
DEFAULT_INPUT_MAX_CHARS = 60_000
DEFAULT_INPUT_MAX_SOURCES = 5
# `mode: rag` 전용 선택 키(#461). `coverage`·`per_source_limit`는 rag_search 노드 키(#446)와
# 이름·값이 같다. `digest`는 검색 앞에 source_digest 노드를 둔다. 생략하면 해석 결과에 키가 없다
INPUT_RAG_OPTIONS = ("coverage", "per_source_limit", "digest")

# secrets 후보 패턴 (§10.1) — 발견 시 거부
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github_token", re.compile(r"ghp_[A-Za-z0-9]{36}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("generic_bearer", re.compile(r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._\-]{20,}")),
]

# 텍스트로 취급하는 확장자 — secrets 스캔 대상. 번들 스크립트도 자격증명을 담기 쉬우므로
# 함께 본다(표준 계획 D5).
TEXT_SUFFIXES = (
    ".md", ".json", ".txt", ".yaml", ".yml", ".csv", ".html", ".xml",
    ".py", ".sh", ".js", ".mjs", ".ts", ".rb",
)


class SkillValidationError(ValueError):
    """패키지 검증 실패. reason 코드로 분류(관측성·테스트용)."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


# 저장소 키 상한(S3 1024B)과 파일시스템 한계 아래로 잡는다 — 넘으면 저장 시점에
# 알 수 없는 오류(500)가 되므로 입력 경계에서 거부한다.
MAX_PATH_BYTES = 512


def validate_path(path: str) -> str:
    """상대 경로만 허용하고 정규화된 POSIX 경로를 반환.

    절대 경로/`..`/백슬래시/숨김 실행파일/빈 경로/제어문자/과도한 길이를 거부한다.
    """
    if not path or path.strip() != path:
        raise SkillValidationError("bad_path", f"빈/공백 경로 불가: {path!r}")
    if any(ch.isprintable() is False for ch in path):
        raise SkillValidationError("bad_path", "경로에 제어문자를 쓸 수 없습니다")
    if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        raise SkillValidationError("absolute_path", f"절대 경로 불가: {path}")
    if "\\" in path:
        raise SkillValidationError("bad_path", f"백슬래시 경로 불가: {path}")
    norm = posixpath.normpath(path)
    if norm.startswith("..") or "/../" in f"/{norm}/" or norm.startswith("/"):
        raise SkillValidationError("path_traversal", f"경로 이탈(..) 불가: {path}")
    if norm in (".", ""):
        raise SkillValidationError("bad_path", f"잘못된 경로: {path}")
    if len(norm.encode("utf-8")) > MAX_PATH_BYTES:
        raise SkillValidationError(
            "path_too_long", f"경로가 너무 깁니다({len(norm.encode('utf-8'))}B > {MAX_PATH_BYTES}B)"
        )
    # 숨김 실행/스크립트 차단 (.sh/.exe 등은 1차에서 실행 안 함이나 저장도 거부)
    for seg in norm.split("/"):
        if seg.startswith(".") and seg not in (".",):
            raise SkillValidationError("hidden_path", f"숨김 경로 불가: {path}")
    return norm


def validate_files(files: dict[str, bytes]) -> None:
    """파일 개수/크기/경로 제한 + SKILL.md 존재 + secrets 스캔."""
    settings = get_settings()
    if "SKILL.md" not in files:
        raise SkillValidationError("missing_skill_md", "SKILL.md가 패키지에 없습니다")
    if len(files) > settings.skill_import_max_files:
        raise SkillValidationError(
            "too_many_files", f"파일 수 초과: {len(files)} > {settings.skill_import_max_files}"
        )
    total = 0
    for path, data in files.items():
        validate_path(path)
        size = len(data)
        if size > settings.skill_import_max_file_bytes:
            raise SkillValidationError(
                "file_too_large", f"단일 파일 크기 초과: {path} ({size}B)"
            )
        total += size
        if should_scan_secrets(path):
            scan_secrets(path, data)
    if total > settings.skill_import_max_total_bytes:
        raise SkillValidationError("package_too_large", f"패키지 크기 초과: {total}B")


def should_scan_secrets(path: str) -> bool:
    """비밀정보 스캔 대상 판정.

    확장자 목록은 대소문자를 구분하지 않고, `scripts/` 아래는 **확장자와 무관하게** 본다 —
    번들 스크립트에는 확장자 없는 실행 파일(`scripts/run`)도 들어오고, 자격증명을 담기 쉬운
    자리가 바로 거기다(D5).
    """
    return path.startswith("scripts/") or path.lower().endswith(TEXT_SUFFIXES)


def scan_secrets(path: str, data: bytes) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise SkillValidationError(
                "secret_detected", f"{path}에서 비밀로 의심되는 값({label}) 발견"
            )


SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def validate_slug(slug: str) -> None:
    if not SLUG_RE.match(slug):
        raise SkillValidationError(
            "bad_slug", f"slug은 lowercase kebab-case여야 합니다: {slug!r}"
        )


def _check_rag_option(key: str, value: Any, mode: str) -> Any:
    """`input`의 rag 선택 키 하나(#461). 위반은 `bad_input_<키>`다."""
    reason = f"bad_input_{key}"
    if mode != "rag":
        raise SkillValidationError(reason, f"input.{key}는 mode: rag에서만 쓸 수 있습니다")
    if key == "coverage":
        if value not in _RAG_COVERAGES:
            raise SkillValidationError(
                reason, f"input.coverage는 {_RAG_COVERAGES} 중 하나여야 합니다 (got {value!r})"
            )
    elif key == "per_source_limit":
        if type(value) is not int or not (
            _PER_SOURCE_LIMIT_MIN <= value <= _PER_SOURCE_LIMIT_MAX
        ):
            raise SkillValidationError(
                reason,
                f"input.per_source_limit는 {_PER_SOURCE_LIMIT_MIN}..{_PER_SOURCE_LIMIT_MAX} "
                f"정수여야 합니다 (got {value!r})",
            )
    elif type(value) is not bool:
        raise SkillValidationError(reason, f"input.digest는 true·false여야 합니다 (got {value!r})")
    return value


def _resolve_rag_options(raw: dict[str, Any], mode: str, *, strict: bool) -> dict[str, Any]:
    """`input`의 rag 선택 키(#461). 생략(없음·null)한 키는 결과에 없다.

    실행 경로(`strict=False`)는 위반한 키를 경고하고 버린다 — 이 키들이 생기기 전에는 `input`의
    모르는 키를 조용히 버렸다. 이미 게시된 패키지가 업그레이드 뒤 실행 때 깨지지 않게 한다
    (`artifact` 블록 #302와 같은 태도). 가져오기·freeze·평가·시드는 같은 위반을 거부한다.
    """
    out: dict[str, Any] = {}
    for key in INPUT_RAG_OPTIONS:
        value = raw.get(key)
        if value is None:
            continue
        try:
            out[key] = _check_rag_option(key, value, mode)
        except SkillValidationError as e:
            if strict:
                raise
            # 사유 코드만 남긴다 — 문구는 패키지 값을 담는다
            logger.warning("skill.manifest.input_option_invalid", reason=e.reason)
    return out


def resolve_input(manifest: dict[str, Any], *, strict: bool = True) -> dict[str, Any]:
    """`input` 블록을 파싱·기본값 병합·검증 (agent-input-source-plan.md §Phase1).

    하위호환: `input` 부재 시 기존 `use_rag`로 유도
    (`use_rag:true`→`mode:rag`, `false`→`mode:none`), `scope:all`.
    기존 `rag_query`/`top_k`는 `mode:rag`의 하위 필드로 유지(builder가 사용).
    `mode: rag` 선택 키 `coverage`·`per_source_limit`·`digest`(#461)는 둔 것만 결과에 싣는다.
    `strict=False`(실행 경로)는 그 키들의 위반만 경고로 낮춘다(`_resolve_rag_options`).
    """
    use_rag = bool(manifest.get("use_rag", True))
    raw = manifest.get("input")
    if raw is None:
        return {
            "mode": "rag" if use_rag else "none",
            "scope": "all",
            "max_sources": DEFAULT_INPUT_MAX_SOURCES,
            "max_chars": DEFAULT_INPUT_MAX_CHARS,
        }
    if not isinstance(raw, dict):
        raise SkillValidationError("bad_input", "input은 객체여야 합니다")

    mode = raw.get("mode", "rag" if use_rag else "none")
    if mode not in INPUT_MODES:
        raise SkillValidationError(
            "bad_input_mode", f"input.mode는 {INPUT_MODES} 중 하나여야 합니다 (got {mode!r})"
        )
    scope = raw.get("scope", "all")
    if scope not in INPUT_SCOPES:
        raise SkillValidationError(
            "bad_input_scope", f"input.scope는 {INPUT_SCOPES} 중 하나여야 합니다 (got {scope!r})"
        )

    try:
        max_chars = int(raw.get("max_chars", DEFAULT_INPUT_MAX_CHARS))
        max_sources = int(raw.get("max_sources", DEFAULT_INPUT_MAX_SOURCES))
    except (TypeError, ValueError) as e:
        raise SkillValidationError(
            "bad_input", "input.max_chars/max_sources는 정수여야 합니다"
        ) from e
    if not 0 < max_chars <= MAX_INPUT_CHARS:
        raise SkillValidationError(
            "bad_input_max_chars", f"input.max_chars는 1..{MAX_INPUT_CHARS} 범위여야 합니다"
        )
    if not 0 < max_sources <= MAX_INPUT_SOURCES:
        raise SkillValidationError(
            "bad_input_max_sources", f"input.max_sources는 1..{MAX_INPUT_SOURCES} 범위여야 합니다"
        )
    return {
        "mode": mode,
        "scope": scope,
        "max_sources": max_sources,
        "max_chars": max_chars,
        **_resolve_rag_options(raw, mode, strict=strict),
    }


# 레거시 매니페스트 `artifact` 블록(#302). 이 밖의 키는 거부한다 — 오타·미지원 선언이 조용히
# 무시되지 않게 한다(다단계 단계의 `_ARTIFACT_KEYS`와 같은 태도).
ARTIFACT_KEYS = frozenset({"name", "content_type", "schema"})
# 산출물 서빙(`api/v1/artifacts.py`)이 인라인·passive로 다루는 타입 중 텍스트 산출물만 받는다
ARTIFACT_CONTENT_TYPES = ("text/markdown", "text/plain", "application/json", "text/csv")
ARTIFACT_JSON_CONTENT_TYPE = "application/json"
DEFAULT_ARTIFACT_NAME = "result.md"
DEFAULT_ARTIFACT_CONTENT_TYPE = "text/markdown"
# storage key `{run_id}/{name}`와 제품 조회 경로가 된다 — 파일 이름 수준으로 묶는다
MAX_ARTIFACT_NAME_BYTES = 255


def _check_artifact_name(raw: str, *, label: str, reason: str) -> str:
    """산출물 이름 규칙 — 다단계 단계(`stages._artifact_name`)와 레거시 매니페스트가 함께 쓴다.

    `save_artifact` 노드가 `name`을 **`render`로 템플릿 치환한다**(`nodes/composite.py`).
    그래서 이름에 `{...}`가 들어가면 모델 출력·사용자 파라미터가 storage key를 결정하고,
    `/`·`..`가 들어가면 run prefix를 벗어난다(#202 리뷰 M3, #302).
    """
    if "{" in raw or "}" in raw:
        raise SkillValidationError(
            reason, f"{label}에 템플릿 {{...}}를 쓸 수 없습니다: {raw!r}"
        )
    if "/" in raw or "\\" in raw or ".." in raw:
        raise SkillValidationError(reason, f"{label}에 경로 구분자를 쓸 수 없습니다: {raw!r}")
    if raw != raw.strip() or raw.startswith("."):
        raise SkillValidationError(
            reason, f"{label}은 앞뒤 공백이나 점으로 시작할 수 없습니다: {raw!r}"
        )
    if not raw:
        raise SkillValidationError(reason, f"{label}이 비어 있습니다")
    return raw


def _check_artifact(raw: Any) -> dict[str, Any]:
    """레거시 매니페스트 `artifact` 블록을 검증하고 기본값을 채운 블록을 돌려준다(#302)."""
    if not isinstance(raw, dict):
        raise SkillValidationError("bad_artifact_block", "artifact는 객체여야 합니다")
    unknown = sorted(str(k) for k in raw if k not in ARTIFACT_KEYS)
    if unknown:
        raise SkillValidationError(
            "bad_artifact_key",
            f"artifact에 알 수 없는 키가 있습니다: {unknown} (허용: {sorted(ARTIFACT_KEYS)})",
        )

    name = raw.get("name", DEFAULT_ARTIFACT_NAME)
    if not isinstance(name, str):
        raise SkillValidationError("bad_artifact_name", "artifact.name은 문자열이어야 합니다")
    _check_artifact_name(name, label="artifact.name", reason="bad_artifact_name")
    # 레거시 전용 규칙 — 단계 이름은 실행 때도 다시 검증하므로 넓히지 않는다(게시 버전 보존)
    if not name.isprintable():
        raise SkillValidationError(
            "bad_artifact_name",
            f"artifact.name에 제어·보이지 않는 문자를 쓸 수 없습니다: {name!r}",
        )
    if len(name.encode("utf-8")) > MAX_ARTIFACT_NAME_BYTES:
        raise SkillValidationError(
            "bad_artifact_name", f"artifact.name이 {MAX_ARTIFACT_NAME_BYTES}바이트를 넘습니다"
        )

    schema = raw.get("schema")
    if "schema" in raw and (not isinstance(schema, str) or schema not in _JSON_VALIDATE_SCHEMAS):
        raise SkillValidationError(
            "bad_artifact_schema",
            f"artifact.schema는 {sorted(_JSON_VALIDATE_SCHEMAS)} 중 하나여야 합니다 "
            f"(got {schema!r})",
        )
    default_type = ARTIFACT_JSON_CONTENT_TYPE if schema else DEFAULT_ARTIFACT_CONTENT_TYPE
    declared = raw.get("content_type", default_type)
    # 서빙(`artifact_media_type`)과 같이 매개변수(`; charset=…`)를 떼고 소문자로 판정·저장한다
    content_type = declared.split(";")[0].strip().lower() if isinstance(declared, str) else None
    if content_type not in ARTIFACT_CONTENT_TYPES:
        raise SkillValidationError(
            "bad_artifact_content_type",
            f"artifact.content_type은 {ARTIFACT_CONTENT_TYPES} 중 하나여야 합니다 "
            f"(got {declared!r})",
        )
    if schema and content_type != ARTIFACT_JSON_CONTENT_TYPE:
        raise SkillValidationError(
            "bad_artifact_content_type",
            f"artifact.schema를 선언하면 content_type은 {ARTIFACT_JSON_CONTENT_TYPE}이어야 "
            f"합니다 (got {content_type!r})",
        )

    resolved: dict[str, Any] = {"name": name, "content_type": content_type}
    if schema:
        resolved["schema"] = schema
    return resolved


def _resolve_artifact(manifest: dict[str, Any], *, strict: bool) -> Any:
    """`artifact` 블록 해석. 실행 경로(`strict=False`)는 위반을 경고만 하고 기존 동작을 둔다.

    기존 동작은 블록을 검사 없이 그대로 쓰고 `schema`는 무시하는 것이다(#302 1단계 호환 —
    이미 게시된 버전이 업그레이드 직후 실행 시점에 깨지지 않게 한다). 가져오기·freeze·평가·시드는
    엄격 경로라 같은 위반을 거부한다.
    """
    if "artifact" not in manifest:
        return {"name": DEFAULT_ARTIFACT_NAME, "content_type": DEFAULT_ARTIFACT_CONTENT_TYPE}
    raw = manifest["artifact"]
    try:
        return _check_artifact(raw)
    except SkillValidationError as e:
        if strict:
            raise
        # 사유 코드만 남긴다 — 문구는 패키지 값(이름)을 담는다
        logger.warning("skill.manifest.artifact_invalid", reason=e.reason)
        if isinstance(raw, dict):
            return {k: v for k, v in raw.items() if k != "schema"}
        return raw


def _int_or_error(value: Any, field: str) -> int | None:
    """매니페스트의 정수 필드 — 잘못된 값은 500이 아니라 정책 오류다."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise SkillValidationError(
            f"bad_{field}", f"{field}는 정수여야 합니다 (got {value!r})"
        ) from e


def validate_manifest(manifest: dict[str, Any], *, strict: bool = True) -> dict[str, Any]:
    """제품 매니페스트(`Settings.skill_manifest_filename`) 검증 후
    기본값을 채운 manifest 반환 (§5.2).

    `strict=False`는 실행 경로(`builder.build_nodes`)용이다. `artifact` 블록 규칙(#302)과
    `input`의 rag 선택 키 규칙(#461)만 경고로 낮추고 기존 동작을 둔다. 다른 규칙은 두 경로가 같다.
    """
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    mode = manifest.get("mode", "instruction")
    if mode not in SUPPORTED_MODES:
        raise SkillValidationError(
            "unsupported_mode", f"1차 지원 mode는 {SUPPORTED_MODES} 뿐입니다 (got {mode!r})"
        )

    params_schema = manifest.get("params_schema", {})
    if params_schema:
        try:
            Draft202012Validator.check_schema(params_schema)
        except SchemaError as e:
            raise SkillValidationError(
                "bad_params_schema", f"params_schema가 유효한 JSON Schema가 아닙니다: {e.message}"
            ) from e

    permissions = manifest.get("permissions", {})
    if not isinstance(permissions, dict):
        raise SkillValidationError("bad_permissions", "permissions는 객체여야 합니다")

    # 기본값 병합 (§5.2)
    resolved: dict[str, Any] = {
        "schema_version": manifest.get("schema_version", "1.0"),
        "mode": mode,
        "use_rag": bool(manifest.get("use_rag", True)),
        "rag_query": manifest.get("rag_query", "{params.topic}"),
        # 생략(None)은 실행 시점에 어드민 RAG 설정으로 해석한다 — 여기서 6을 채우면
        # 노드 단계에서 "생략"과 "명시적 6"을 구분할 수 없다(#45)
        "top_k": _int_or_error(manifest.get("top_k"), "top_k"),
        "input": resolve_input(manifest, strict=strict),
        "params_schema": params_schema,
        "artifact": _resolve_artifact(manifest, strict=strict),
        "permissions": {
            "rag": bool(permissions.get("rag", True)),
            "llm": bool(permissions.get("llm", True)),
            "tts": bool(permissions.get("tts", False)),
            # mcp/http/scripts는 선언만 저장, 1차 항상 비활성(§5.2, §10.3)
            "mcp": bool(permissions.get("mcp", False)),
            "http": bool(permissions.get("http", False)),
            "scripts": bool(permissions.get("scripts", False)),
        },
    }
    if manifest.get("system"):
        resolved["system"] = str(manifest["system"])
    return resolved
