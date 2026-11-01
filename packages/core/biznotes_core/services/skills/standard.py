"""Agent Skills 표준 적합성 판정 — 코어 계획 D1.

계획은 `docs/plans/agent-skills-standard/plan.md`다. 이 모듈은 **적합성**
(`validation_profile`)만 판정한다. 실행 자격(`runtime_profile`)은 D6이 따로 정하고
`report.py`가 계산한다 — 적합성 실패가 실행을 막지 않는다(S-HCI-11).

**oracle 정본**: strict 규칙은 고정한 공식 `skills-ref 0.1.0`
(`tests/fixtures/skills/oracle-manifest.json`, commit `69ef37e9…`)의
`validator.py` 본문을 그대로 옮긴 것이다. 프로젝트가 임의로 만든 규칙을
"표준 strict"라고 부르지 않는다(sl R1-06 승계). oracle 원문 메시지를 그대로
재현하므로 `tests/fixtures/skills/golden-corpus.json::oracle_errors`와
문자열 단위로 대조할 수 있다.

**경계 분리(D5)**
- 표준 필수 형식 위반 → 이 모듈의 `errors`(보고만 한다, 가져오기·freeze는 성공)
- oracle은 `metadata` 값을 `str()`로 바꾸지만 이 모듈은 **원문을 그대로 보존**한다. 문자열 스칼라
  로더가 이미 스칼라를 문자열로 주므로 차이는 중첩 값뿐이고, 그것은 이식성 경고로 알린다
- 다른 클라이언트에서 문제가 될 수 있는 것 → `portability`(경고, 프로필 불변)
- 제품 파일 크기·보안 제한 → `validator.py`의 `SkillValidationError`(정책 오류, 거부)

**이름 3분리(D2)**
- 표준 name: SKILL.md frontmatter의 `name` — 이 모듈이 소유. 절대 개명하지 않는다.
- 제품 표시 name: `skills.name`
- DB slug: `skills.slug` (충돌 시 `-2` 접미) — 표준 name에 영향 없음
"""

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import structlog

from biznotes_core.services.skills.validator import SkillValidationError

logger = structlog.get_logger()

# --- pinned oracle constants (skills-ref 0.1.0 validator.py) ---
MAX_SKILL_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_COMPATIBILITY_LENGTH = 500

ALLOWED_FIELDS = frozenset(
    {
        "name",
        "description",
        "license",
        "allowed-tools",
        "metadata",
        "compatibility",
    }
)

# oracle은 strictyaml이 거부한 frontmatter를 `Invalid YAML in frontmatter: {e}` 한 줄로
# 보고한다. 이 제품은 flow 스타일을 파싱은 하되(D1 YAML 처리 표) 같은 모양의 표준
# 오류 하나로 보고한다.
FLOW_STYLE_ERROR = "Invalid YAML in frontmatter: flow style"

# --- 적합성 프로필 식별자 (D13 CHECK 값) ---
VALIDATION_PROFILE_LEGACY = "legacy_v1"
VALIDATION_PROFILE_STANDARD = "standard_v1"
VALIDATION_PROFILES = (VALIDATION_PROFILE_LEGACY, VALIDATION_PROFILE_STANDARD)

# 명세 권고 — 본문 500줄·약 5000토큰 이하. 넘으면 이식성 경고다.
BODY_MAX_LINES = 500
BODY_MAX_TOKENS = 5000

# 다른 클라이언트가 거부하는 이름 모양(Claude 제품군의 이름 규칙 포함).
PORTABLE_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
RESERVED_NAME_WORDS = ("anthropic", "claude")
_XML_TAG_RE = re.compile(r"<[^<>]+>")

# `skill_versions.standard_metadata`(JSONB)에 영속화할 수 있는 직렬화 크기 상한(D3).
# 표준이 정한 값이 아니라 **제품 정책**이므로 초과는 policy error다.
# `parser.py`가 YAML alias를 거부해 증폭 경로를 막았고, 이 상한은 alias 없이도
# 거대한 frontmatter를 넣는 경로를 막는 두 번째 방어선이다.
MAX_STANDARD_METADATA_BYTES = 64 * 1024

# `run_skill_script`가 실행하는 번들 스크립트(D7). 평가 보고서(files dict)와 실행 도구(file_tree)가
# 같은 규칙을 쓰도록 무거운 의존성이 없는 이 모듈에 둔다(#127).
SCRIPT_EXTENSIONS: tuple[str, ...] = (".py", ".sh", ".js", ".mjs")


def is_runnable_script(path: str) -> bool:
    """`scripts/` 아래이고 확장자가 허용 목록에 있는가."""
    return path.startswith("scripts/") and os.path.splitext(path)[1].lower() in SCRIPT_EXTENSIONS


def finding(code: str, message: str) -> dict[str, str]:
    """보고서 항목 한 개 — `{code, message}`(D5)."""
    return {"code": code, "message": message}


@dataclass
class StandardReport:
    """한 패키지의 표준 적합성 판정 결과."""

    validation_profile: str
    errors: list[str] = field(default_factory=list)
    portability: list[dict[str, str]] = field(default_factory=list)
    # frontmatter 원문 — 모르는 필드까지 그대로 담는다(보존과 판정의 분리)
    metadata: dict[str, Any] = field(default_factory=dict)
    standard_name: str = ""
    description: str = ""
    unknown_fields: list[str] = field(default_factory=list)

    @property
    def is_standard(self) -> bool:
        return self.validation_profile == VALIDATION_PROFILE_STANDARD


def _nfkc(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


def validate_standard_name(name: Any, root_name: str | None) -> list[str]:
    """oracle `_validate_name` 이식. 메시지 문자열까지 동일하다."""
    errors: list[str] = []

    if not name or not isinstance(name, str) or not name.strip():
        errors.append("Field 'name' must be a non-empty string")
        return errors

    normalized = _nfkc(name.strip())

    if len(normalized) > MAX_SKILL_NAME_LENGTH:
        errors.append(
            f"Skill name '{normalized}' exceeds {MAX_SKILL_NAME_LENGTH} character limit "
            f"({len(normalized)} chars)"
        )

    if normalized != normalized.lower():
        errors.append(f"Skill name '{normalized}' must be lowercase")

    if normalized.startswith("-") or normalized.endswith("-"):
        errors.append("Skill name cannot start or end with a hyphen")

    if "--" in normalized:
        errors.append("Skill name cannot contain consecutive hyphens")

    if not all(c.isalnum() or c == "-" for c in normalized):
        errors.append(
            f"Skill name '{normalized}' contains invalid characters. "
            "Only letters, digits, and hyphens are allowed."
        )

    if root_name:
        dir_name = _nfkc(root_name)
        if dir_name != normalized:
            errors.append(
                f"Directory name '{root_name}' must match skill name '{normalized}'"
            )

    return errors


def validate_standard_description(description: Any) -> list[str]:
    """oracle `_validate_description` 이식."""
    errors: list[str] = []
    if not description or not isinstance(description, str) or not description.strip():
        errors.append("Field 'description' must be a non-empty string")
        return errors
    if len(description) > MAX_DESCRIPTION_LENGTH:
        errors.append(
            f"Description exceeds {MAX_DESCRIPTION_LENGTH} character limit "
            f"({len(description)} chars)"
        )
    return errors


def validate_standard_compatibility(compatibility: Any) -> list[str]:
    """oracle `_validate_compatibility` 이식."""
    errors: list[str] = []
    if not isinstance(compatibility, str):
        errors.append("Field 'compatibility' must be a string")
        return errors
    if len(compatibility) > MAX_COMPATIBILITY_LENGTH:
        errors.append(
            f"Compatibility exceeds {MAX_COMPATIBILITY_LENGTH} character limit "
            f"({len(compatibility)} chars)"
        )
    return errors


def validate_standard_fields(metadata: dict[str, Any]) -> list[str]:
    """oracle `_validate_metadata_fields` 이식 — unknown 필드 거부."""
    extra = set(metadata.keys()) - set(ALLOWED_FIELDS)
    if extra:
        return [
            f"Unexpected fields in frontmatter: {', '.join(sorted(extra))}. "
            f"Only {sorted(ALLOWED_FIELDS)} are allowed."
        ]
    return []


def validate_standard_metadata(
    metadata: dict[str, Any], root_name: str | None = None
) -> list[str]:
    """oracle `validate_metadata` 이식 — 검사 순서까지 동일하다.

    `root_name`은 oracle의 `skill_dir.name`에 대응한다. 저장된 패키지의 디렉터리는
    정의상 `name`이므로(D3) 호출부는 대개 `None`을 넘긴다. 가져올 때의 루트
    불일치는 표준 오류가 아니라 가져오기 경고로 다룬다(S-HCI-2).
    """
    errors: list[str] = []
    errors.extend(validate_standard_fields(metadata))

    if "name" not in metadata:
        errors.append("Missing required field in frontmatter: name")
    else:
        errors.extend(validate_standard_name(metadata["name"], root_name))

    if "description" not in metadata:
        errors.append("Missing required field in frontmatter: description")
    else:
        errors.extend(validate_standard_description(metadata["description"]))

    if "compatibility" in metadata:
        errors.extend(validate_standard_compatibility(metadata["compatibility"]))

    return errors


def _approx_tokens(text: str) -> int:
    """토큰 수 근사 — UTF-8 4바이트당 1토큰. 경고 판정에만 쓴다."""
    return len(text.encode("utf-8")) // 4


def portability_warnings(
    frontmatter: dict[str, Any], body: str, *, had_bom: bool = False
) -> list[dict[str, str]]:
    """다른 클라이언트에서 문제가 될 수 있는 모양 — 경고만 내고 프로필을 바꾸지 않는다(D1)."""
    warnings: list[dict[str, str]] = []
    name = frontmatter.get("name")
    if isinstance(name, str) and name.strip():
        stripped = name.strip()
        if not stripped.isascii():
            warnings.append(
                finding(
                    "non_ascii_name",
                    f"name '{stripped}'에 ASCII가 아닌 문자가 있습니다. 표준 판정은 통과해도 "
                    "a-z·0-9·하이픈만 받는 클라이언트가 있습니다.",
                )
            )
        elif not PORTABLE_NAME_RE.match(stripped):
            warnings.append(
                finding(
                    "name_not_portable",
                    f"name '{stripped}'은 소문자·숫자·하이픈(kebab-case) 형식이 아닙니다.",
                )
            )
        lowered = stripped.lower()
        reserved = [w for w in RESERVED_NAME_WORDS if w in lowered]
        if reserved:
            warnings.append(
                finding(
                    "reserved_word_in_name",
                    f"name에 예약어({', '.join(reserved)})가 있어 일부 클라이언트가 거부합니다.",
                )
            )
    for key in ("name", "description"):
        value = frontmatter.get(key)
        if isinstance(value, str) and _XML_TAG_RE.search(value):
            warnings.append(
                finding(
                    "xml_tag_in_field",
                    f"{key}에 XML 태그가 있습니다. 일부 클라이언트는 태그를 허용하지 않습니다.",
                )
            )
    meta = frontmatter.get("metadata")
    if isinstance(meta, dict) and any(isinstance(v, dict | list) for v in meta.values()):
        warnings.append(
            finding(
                "nested_metadata",
                "metadata 값은 문자열이어야 합니다. 중첩된 값은 다른 클라이언트가 문자열로 "
                "바꾸거나 버립니다.",
            )
        )
    if isinstance(frontmatter.get("allowed-tools"), list):
        warnings.append(
            finding(
                "allowed_tools_sequence",
                "allowed-tools는 공백으로 구분한 문자열이어야 합니다(목록 형식은 비표준).",
            )
        )
    lines = body.count("\n") + 1 if body else 0
    tokens = _approx_tokens(body)
    if lines > BODY_MAX_LINES or tokens > BODY_MAX_TOKENS:
        warnings.append(
            finding(
                "body_too_long",
                f"SKILL.md 본문이 {lines}줄·약 {tokens}토큰입니다. 명세는 {BODY_MAX_LINES}줄·"
                f"{BODY_MAX_TOKENS}토큰 이하를 권장합니다. 긴 내용은 references/로 나누세요.",
            )
        )
    if had_bom:
        warnings.append(
            finding(
                "bom", "SKILL.md가 BOM으로 시작합니다. 일부 파서는 frontmatter를 찾지 못합니다."
            )
        )
    return warnings


def evaluate_frontmatter(
    frontmatter: dict[str, Any],
    body: str,
    *,
    root_name: str | None = None,
    flow_style: bool = False,
    had_bom: bool = False,
) -> StandardReport:
    """frontmatter+본문 → StandardReport (예외를 던지지 않는다).

    flow 스타일이 있으면 oracle처럼 YAML 오류 한 줄만 보고한다 — strictyaml은
    파싱 단계에서 실패해 필드 검사까지 가지 않기 때문이다.
    """
    if flow_style:
        errors = [FLOW_STYLE_ERROR]
    else:
        errors = validate_standard_metadata(frontmatter, root_name)
    unknown = sorted(set(frontmatter.keys()) - set(ALLOWED_FIELDS))

    raw_name = frontmatter.get("name")
    standard_name = _nfkc(raw_name.strip()) if isinstance(raw_name, str) else ""
    raw_desc = frontmatter.get("description")
    description = raw_desc.strip() if isinstance(raw_desc, str) else ""

    profile = VALIDATION_PROFILE_STANDARD if not errors else VALIDATION_PROFILE_LEGACY
    return StandardReport(
        validation_profile=profile,
        errors=errors,
        portability=portability_warnings(frontmatter, body, had_bom=had_bom),
        metadata=dict(frontmatter),
        standard_name=standard_name,
        description=description,
        unknown_fields=unknown,
    )


def build_persisted_metadata(report: StandardReport) -> dict[str, Any]:
    """`skill_versions.standard_metadata`에 넣을 frontmatter 원문을 만들고 크기를 강제한다.

    상한을 넘으면 조용히 자르지 않고 **policy error**로 거부한다. 잘라서 저장하면
    "원문 보존"이 거짓이 되기 때문이다. SKILL.md byte 원문은 스토리지에 불변으로
    남아 있으므로 정보가 사라지지도 않는다.
    """
    payload = dict(report.metadata)
    try:
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as e:
        # 문자열 스칼라 로더는 str·list·dict만 만든다 — 여기 오면 로더 계약이 깨진 것이다.
        logger.warning(
            "skill.standard_metadata.not_serializable",
            reason="not_json_serializable",
            error=str(e),
        )
        raise SkillValidationError(
            "standard_metadata_not_serializable",
            f"frontmatter를 JSON으로 저장할 수 없습니다: {e}",
        ) from e
    if size > MAX_STANDARD_METADATA_BYTES:
        logger.warning(
            "skill.standard_metadata.too_large",
            reason="standard_metadata_too_large",
            size=size,
            limit=MAX_STANDARD_METADATA_BYTES,
        )
        raise SkillValidationError(
            "standard_metadata_too_large",
            f"frontmatter 메타데이터가 너무 큽니다: {size}B > "
            f"{MAX_STANDARD_METADATA_BYTES}B",
        )
    return payload
