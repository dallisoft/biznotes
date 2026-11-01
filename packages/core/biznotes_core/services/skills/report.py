"""스킬 패키지 평가 — 네 층 검증 보고서(D5)와 실행 자격(D6).

계획은 `docs/plans/agent-skills-standard/plan.md`다. 한 패키지를 한 번 읽어 다음을 만든다.

- `standard`: 표준 적합성(oracle 판정). 보고만 한다.
- `portability`: 다른 클라이언트에서의 문제. 경고만 한다.
- `policy`: 제품 정책. `errors`가 있으면 가져오기·freeze를 거부한다.
- `capability`: 이 제품이 실제로 해 줄 수 있는 것(`runtime_profile`과 경고).

정책 위반은 예외(`SkillValidationError`·`SkillParseError`)로 올라간다. 보고서 모양이
필요한 호출부는 `failure_report`로 같은 네 층 모양을 만든다.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

from biznotes_core.config import get_settings
from biznotes_core.models.skill import (
    SKILL_RUNTIME_LEGACY,
    SKILL_RUNTIME_STANDARD,
)
from biznotes_core.services.skills import standard as std
from biznotes_core.services.skills.authoring import evals_warnings
from biznotes_core.services.skills.parser import (
    SkillParseError,
    parse_skill_md_detailed,
    validate_frontmatter,
)
from biznotes_core.services.skills.stages import validate_stages
from biznotes_core.services.skills.validator import (
    SkillValidationError,
    validate_files,
    validate_manifest,
)

# 재배포 제한을 뜻하는 표현(S-HCI-9: 표시·경고만). 판정이 아니라 사람에게 보여 줄 신호다.
_LICENSE_RESTRICTION_RE = re.compile(
    r"all rights reserved"
    r"|proprietary"
    r"|(?:may|must|shall) not (?:be )?(?:copied|copy|reproduced|reproduce|"
    r"distributed|distribute|redistributed|redistribute|sublicensed|sublicense)"
    r"|not for (?:re)?distribution"
    r"|no redistribution"
    r"|do not (?:re)?distribute"
    r"|재배포|배포(?:를|하는 것을)? 금지|무단 (?:복제|배포)",
    re.IGNORECASE,
)
_LICENSE_FILE_RE = re.compile(r"^(?:LICEN[SC]E|COPYING)(?:[.\-_].*)?$", re.IGNORECASE)

# L2 샌드박스는 네트워크·런타임 설치가 없다(D7). 이런 관례를 쓰는 스킬은 그대로는 돌지 않는다.
_SCRIPT_RUNTIME_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PEP 723 inline metadata", re.compile(r"^# /// script\s*$", re.MULTILINE)),
    ("uv run", re.compile(r"\buv run\b")),
    ("npx", re.compile(r"\bnpx\s")),
    ("deno", re.compile(r"\bdeno (?:run|task)\b")),
    ("bun", re.compile(r"\bbun (?:run|x|install)\b")),
)

# 코드를 실행해 결과를 만들라는 지침(#127). instruction 프로필에는 실행 경로가 없고, 표준
# 프로필도 번들 스크립트만 실행한다 — LLM은 이런 지침을 코드 텍스트로 답할 뿐이다.
# 오탐을 줄이려고 자연어 문구는 보지 않는다.
_CODE_EXECUTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "실행 코드 블록",
        re.compile(
            r"^[ \t]*(?:```|~~~)[ \t]*(?:python3?|py|bash|sh|shell|zsh|console|"
            r"javascript|js|node|typescript|ts)\b",
            re.MULTILINE | re.IGNORECASE,
        ),
    ),
    (
        "패키지 설치",
        re.compile(
            r"\b(?:pip3?|npm|pnpm|yarn|uv|apt(?:-get)?|brew|conda)\s+(?:install|add|i)\b"
        ),
    ),
    (
        "파일 생성 라이브러리",
        re.compile(
            r"^[ \t]*(?:from|import)[ \t]+(?:reportlab|pypdf|PyPDF2|fpdf|docx|openpyxl|pptx|"
            r"xlsxwriter|matplotlib|PIL|weasyprint)\b",
            re.MULTILINE,
        ),
    ),
    (
        "스크립트 실행 명령",
        re.compile(r"\b(?:python3?|node|bash)[ \t]+[\w./-]+\.(?:py|m?js|sh)\b"),
    ),
)


@dataclass
class PackageEvaluation:
    """정책을 통과한 패키지의 판정 결과."""

    name: str
    description: str
    frontmatter: dict[str, Any]
    body: str
    # 기본값을 채운 매니페스트(버전 행에 저장하는 값)
    manifest: dict[str, Any]
    # 파일에 있던 원문 매니페스트 — 노드 빌드는 기존 경로와 같은 입력을 받아야 한다
    raw_manifest: dict[str, Any]
    has_product_manifest: bool
    references: list[str]
    validation_profile: str
    runtime_profile: str
    standard_metadata: dict[str, Any]
    report: dict[str, Any] = field(default_factory=dict)


def _layer(errors: list[Any] | None = None, warnings: list[Any] | None = None) -> dict[str, Any]:
    return {"errors": errors or [], "warnings": warnings or []}


def failure_report(exc: SkillValidationError | SkillParseError) -> dict[str, Any]:
    """정책 위반(거부) 하나를 네 층 보고서 모양으로 만든다."""
    return {
        "standard": {"profile": None, "errors": []},
        "portability": {"warnings": []},
        "policy": {
            **_layer([std.finding(exc.reason, str(exc))]),
            "license": {"field": None, "files": []},
        },
        "capability": {"runtime_profile": None, "warnings": []},
    }


def _decode_text(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _load_product_manifest(files: dict[str, bytes], manifest_name: str) -> dict[str, Any]:
    data = files.get(manifest_name)
    if data is None:
        return {}
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        # `json.JSONDecodeError`는 ValueError의 하위다. 깊게 중첩된 JSON은 `RecursionError`를
        # 내는데, 그대로 새면 가져오기가 500이 된다(YAML 쪽 #69와 같은 계열).
        raise SkillValidationError(
            "bad_manifest", f"{manifest_name}을 JSON으로 읽을 수 없습니다: {type(e).__name__}"
        ) from e
    if not isinstance(manifest, dict):
        raise SkillValidationError("bad_manifest", f"{manifest_name}은 JSON 객체여야 합니다")
    return manifest


def _license_facts(
    frontmatter: dict[str, Any], files: dict[str, bytes]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    field_value = frontmatter.get("license")
    license_field = field_value if isinstance(field_value, str) else None
    license_files = sorted(
        path for path in files if "/" not in path and _LICENSE_FILE_RE.match(path)
    )
    texts = [license_field or ""]
    texts.extend(_decode_text(files[path]) or "" for path in license_files)
    warnings: list[dict[str, str]] = []
    if any(_LICENSE_RESTRICTION_RE.search(text) for text in texts):
        warnings.append(
            std.finding(
                "license_restricts_redistribution",
                "라이선스에 복제·재배포를 제한하는 표현이 있습니다. 가져오기·공개 전에 "
                "사용 조건을 확인하세요.",
            )
        )
    return {"field": license_field, "files": license_files}, warnings


def _capability_warnings(
    frontmatter: dict[str, Any],
    body: str,
    files: dict[str, bytes],
    *,
    manifest_name: str,
    has_product_manifest: bool,
    runtime_enabled: bool,
    runtime_profile: str,
) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    if has_product_manifest:
        message = (
            f"제품 매니페스트({manifest_name})가 있어 기존 instruction 실행 프로필로 실행합니다."
        )
        if runtime_enabled:
            # 플래그가 꺼져 있으면 매니페스트를 지워도 legacy다 — 그때는 안내하지 않는다
            message += (
                " 소유자가 매니페스트를 지우고 새 버전을 만들면 표준 프로필로 옮길 수 있습니다"
                "(docs/SKILL_AUTHORING.md)."
            )
        warnings.append(std.finding("product_manifest", message))
    elif not runtime_enabled:
        warnings.append(
            std.finding(
                "standard_runtime_disabled",
                "표준 런타임이 꺼져 있어 기존 instruction 실행 프로필로 실행합니다.",
            )
        )
    scripts = sorted(path for path in files if path.startswith("scripts/"))
    if scripts:
        warnings.append(
            std.finding(
                "scripts_not_executable",
                "번들 스크립트는 이 제품에서 실행되지 않습니다(스크립트 실행이 꺼져 있거나 "
                f"검토되지 않음): {', '.join(scripts[:10])}",
            )
        )
    detected: list[str] = []
    if any(path.endswith(".ts") for path in files):
        detected.append(".ts")
    scanned = [body, *(_decode_text(files[path]) or "" for path in scripts)]
    for label, pattern in _SCRIPT_RUNTIME_PATTERNS:
        if any(pattern.search(text) for text in scanned):
            detected.append(label)
    if detected:
        warnings.append(
            std.finding(
                "script_runtime_unavailable",
                "스크립트 실행 환경에는 네트워크와 런타임 설치가 없습니다. 다음 관례는 "
                f"그대로 동작하지 않습니다: {', '.join(detected)}",
            )
        )
    code_instructions = [
        label for label, pattern in _CODE_EXECUTION_PATTERNS if pattern.search(body)
    ]
    if code_instructions and runtime_profile == SKILL_RUNTIME_LEGACY:
        warnings.append(
            std.finding(
                "instruction_cannot_execute_code",
                "이 스킬은 instruction 실행 프로필로 실행되어 코드를 실행하거나 패키지를 "
                "설치하거나 파일을 만들 수 없습니다. 실행을 전제한 지침이라면 코드 텍스트로만 "
                f"답하게 됩니다. 감지: {', '.join(code_instructions)}",
            )
        )
    elif code_instructions and not any(std.is_runnable_script(path) for path in scripts):
        warnings.append(
            std.finding(
                "no_bundled_scripts",
                "실행할 수 있는 번들 스크립트(scripts/)가 없어 run_skill_script를 쓸 수 "
                "없습니다. 코드를 직접 작성해 실행하라는 지침이라면 이 제품에서는 실행되지 "
                f"않습니다. 감지: {', '.join(code_instructions)}",
            )
        )
    compatibility = frontmatter.get("compatibility")
    if isinstance(compatibility, str) and compatibility.strip():
        warnings.append(
            std.finding(
                "compatibility_not_evaluated",
                "이 제품은 compatibility 요구를 확인하지 않습니다: " + compatibility[:200],
            )
        )
    if "allowed-tools" in frontmatter:
        warnings.append(
            std.finding(
                "allowed_tools_not_enforced",
                "allowed-tools는 기록·표시만 합니다. 이 제품에서는 도구 권한을 주지도, "
                "제한하지도 않습니다.",
            )
        )
    return warnings


def runtime_profile_for(*, has_product_manifest: bool) -> str:
    """실행 자격(D6, S-HCI-11) — 적합성과 무관하다.

    파싱·필수 필드 통과는 호출 전에 보장된다. 제품 매니페스트가 없고 표준 런타임
    플래그가 켜져 있을 때만 `standard_agent_v1`이다.
    """
    if not has_product_manifest and get_settings().skill_standard_runtime_enabled:
        return SKILL_RUNTIME_STANDARD
    return SKILL_RUNTIME_LEGACY


def evaluate_package(
    files: dict[str, bytes], *, manifest_filename: str | None = None
) -> PackageEvaluation:
    """패키지 파일 dict → 판정 결과. 정책 위반은 예외로 올린다.

    저장된 패키지의 부모 디렉터리는 정의상 표준 `name`이므로(D3) 디렉터리 일치
    검사는 하지 않는다. 가져올 때의 루트 불일치는 `root_mismatch_warning`이 따로 본다.
    """
    settings = get_settings()
    manifest_name = manifest_filename or settings.skill_manifest_filename

    validate_files(files)
    text = _decode_text(files["SKILL.md"])
    if text is None:
        raise SkillParseError("SKILL.md는 UTF-8 텍스트여야 합니다")
    parsed = parse_skill_md_detailed(text)
    name, description = validate_frontmatter(parsed.frontmatter)

    raw_manifest = _load_product_manifest(files, manifest_name)
    has_product_manifest = manifest_name in files
    manifest = validate_manifest(raw_manifest)

    # [#202] 다단계 선언 검증 — 가져오기/평가 경로의 첫 방어선. 실행 경로도 다시 검증하지만
    # 여기가 가장 이른 거부 지점이다. 실패는 `SkillValidationError`로 가져오기를 막는다
    validate_stages(parsed.frontmatter)

    standard_report = std.evaluate_frontmatter(
        parsed.frontmatter,
        parsed.body,
        flow_style=parsed.flow_style,
        had_bom=parsed.had_bom,
    )
    standard_metadata = std.build_persisted_metadata(standard_report)
    license_summary, policy_warnings = _license_facts(parsed.frontmatter, files)
    policy_warnings.extend(evals_warnings(files, name))
    runtime_profile = runtime_profile_for(has_product_manifest=has_product_manifest)

    references = sorted(
        path
        for path, data in files.items()
        if path.startswith("references/") and _decode_text(data) is not None
    )
    report = {
        "standard": {
            "profile": standard_report.validation_profile,
            "errors": list(standard_report.errors),
        },
        "portability": {"warnings": list(standard_report.portability)},
        "policy": {**_layer([], policy_warnings), "license": license_summary},
        "capability": {
            "runtime_profile": runtime_profile,
            "warnings": _capability_warnings(
                parsed.frontmatter,
                parsed.body,
                files,
                manifest_name=manifest_name,
                has_product_manifest=has_product_manifest,
                runtime_enabled=settings.skill_standard_runtime_enabled,
                runtime_profile=runtime_profile,
            ),
        },
    }
    return PackageEvaluation(
        name=name,
        description=description,
        frontmatter=parsed.frontmatter,
        body=parsed.body,
        manifest=manifest,
        raw_manifest=raw_manifest,
        has_product_manifest=has_product_manifest,
        references=references,
        validation_profile=standard_report.validation_profile,
        runtime_profile=runtime_profile,
        standard_metadata=standard_metadata,
        report=report,
    )


def root_mismatch_warning(root_name: str | None, name: str) -> dict[str, str] | None:
    """가져온 ZIP의 루트 폴더가 표준 `name`과 다를 때의 경고(S-HCI-2: 경고 후 수용).

    저장된 패키지의 디렉터리는 `name`으로 정의되므로 적합성 판정에는 들어가지 않는다.
    원래 루트 이름은 `import_report.root_name`에 남는다.
    """
    if not root_name:
        return None
    if std.validate_standard_name(name, root_name) == std.validate_standard_name(name, None):
        return None
    return std.finding(
        "root_mismatch",
        f"ZIP 루트 폴더 '{root_name}'가 스킬 이름 '{name}'과 다릅니다. 저장된 패키지의 "
        "폴더 이름은 스킬 이름으로 바뀝니다(내보낼 때 적용).",
    )


@dataclass
class ImportCandidate:
    """후보 조회의 사전 점검 요약(#402) — `ok`여도 가져오기 성공을 보장하지 않는다.

    비밀정보·제품 매니페스트·stages 검사는 가져올 때 확정된다. `errors`는 가져오기 거부 사유
    코드, `warnings`는 이식성·루트 불일치 경고 코드다. 표시용 name·description은 표준
    description 길이 상한에서 자른다(응답 크기를 묶는다).
    """

    path: str
    name: str | None = None
    description: str | None = None
    file_count: int = 0
    ok: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    validation_profile: str | None = None


def summarize_import_candidate(
    path: str,
    skill_md: bytes | None,
    *,
    file_count: int,
    errors: list[str],
    root_name: str | None,
) -> ImportCandidate:
    """구조 점검 결과에 그 후보의 SKILL.md frontmatter 판정을 더한다(읽는 파일은 이것뿐이다)."""
    found = set(errors)
    candidate = ImportCandidate(path=path, file_count=file_count)
    if skill_md is not None:
        text = _decode_text(skill_md)
        try:
            if text is None:
                raise SkillParseError("SKILL.md는 UTF-8 텍스트여야 합니다")
            parsed = parse_skill_md_detailed(text)
            name, description = validate_frontmatter(parsed.frontmatter)
        except SkillParseError as e:
            found.add(e.reason)
        else:
            standard = std.evaluate_frontmatter(
                parsed.frontmatter,
                parsed.body,
                flow_style=parsed.flow_style,
                had_bom=parsed.had_bom,
            )
            candidate.name = name[: std.MAX_DESCRIPTION_LENGTH]
            candidate.description = description[: std.MAX_DESCRIPTION_LENGTH]
            candidate.validation_profile = standard.validation_profile
            candidate.warnings = [item["code"] for item in standard.portability]
            if root_mismatch_warning(root_name, name) is not None:
                candidate.warnings.append("root_mismatch")
    candidate.errors = sorted(found)
    candidate.ok = not found
    return candidate
