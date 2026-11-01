"""SKILL.md 안전 작성과 ``evals/evals.json`` 정적 검사 (표준 계획 Phase 2).

폼 편집은 YAML을 완전히 보존할 수 있는 경우에만 허용한다. 알 수 없는 키나 YAML
주석을 재직렬화하면 사용자가 작성한 의미·서식을 잃을 수 있으므로, 그런 문서는 원문
편집기로만 바꿀 수 있다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml
from yaml.tokens import ScalarToken

from biznotes_core.services.skills import standard as std
from biznotes_core.services.skills.parser import (
    SkillParseError,
    parse_skill_md_detailed,
    validate_frontmatter,
)
from biznotes_core.services.skills.validator import SkillValidationError, validate_path

FORM_FRONTMATTER_KEYS = frozenset(
    {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
)
EVALS_PATH = "evals/evals.json"


@dataclass(frozen=True)
class FrontmatterForm:
    """폼에 안전하게 노출할 수 있는 frontmatter 상태."""

    editable: bool
    fields: dict[str, Any]
    reason: str | None = None


def _split_frontmatter_source(content: str) -> tuple[str, str, str]:
    """``(BOM, frontmatter 원문, 본문 원문)``을 손실 없이 분리한다."""

    bom = "\ufeff" if content.startswith("\ufeff") else ""
    text = content[len(bom) :]
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n").strip() != "---":
        raise SkillParseError("SKILL.md는 '---'로 시작해야 합니다")
    for index, line in enumerate(lines[1:], start=1):
        # 파서(`parse_skill_md_detailed`)와 같은 규칙이어야 한다 — 닫는 줄 끝의 공백·탭을
        # 다르게 보면 두 쪽이 다른 줄을 닫는 줄로 골라 본문 일부가 frontmatter로 먹힌다.
        if line.rstrip() == "---":
            return bom, "".join(lines[1:index]), "".join(lines[index + 1 :])
    raise SkillParseError("frontmatter 닫는 '---'를 찾을 수 없습니다")


def _has_yaml_comment(frontmatter: str) -> bool:
    """YAML scalar 토큰 밖의 ``#``(주석)을 찾는다.

    PyYAML은 주석 토큰을 내놓지 않지만 ScalarToken의 mark 범위는 보존한다. 따라서
    인용 문자열과 block scalar 안의 ``#``은 안전하게 통과시키고, plain scalar 내부의
    따옴표·apostrophe 뒤에 붙은 실제 주석도 빠뜨리지 않는다.
    """

    # PyYAML ScalarToken은 block scalar의 본문뿐 아니라 `| # comment` 같은 헤더의
    # 주석까지 함께 덮는다. 헤더 줄의 주석은 재직렬화하면 사라지므로 먼저 잡는다.
    if any(
        re.search(r"(?:^|:)\s*[|>][0-9+-]*\s*#", line) for line in frontmatter.splitlines()
    ):
        return True
    try:
        protected = [
            (token.start_mark.index, token.end_mark.index)
            for token in yaml.scan(frontmatter)
            if isinstance(token, ScalarToken)
        ]
    except yaml.YAMLError:
        # 스캔할 수 없으면 주석 유무를 증명할 수 없다 — 폼을 닫는 쪽(안전한 쪽)으로 본다.
        return True
    return any(
        char == "#" and not any(start <= index < end for start, end in protected)
        for index, char in enumerate(frontmatter)
    )


def frontmatter_form(content: str) -> FrontmatterForm:
    """원문에서 폼 상태를 만든다. 파싱 불가 문서는 원문 편집으로 유도한다."""

    try:
        _, raw_frontmatter, _ = _split_frontmatter_source(content)
        parsed = parse_skill_md_detailed(content)
    except SkillParseError:
        return FrontmatterForm(editable=False, fields={}, reason="invalid_frontmatter")
    unknown = sorted(set(parsed.frontmatter) - FORM_FRONTMATTER_KEYS)
    if unknown:
        return FrontmatterForm(editable=False, fields={}, reason="unknown_frontmatter_keys")
    if _has_yaml_comment(raw_frontmatter):
        return FrontmatterForm(editable=False, fields={}, reason="frontmatter_comments")
    return FrontmatterForm(editable=True, fields=dict(parsed.frontmatter))


def _validate_form_value(value: Any) -> None:
    """폼 JSON이 YAML safe_dump로 안전하게 보존될 수 있는 구조인지 확인한다."""

    if isinstance(value, str):
        return
    if isinstance(value, list):
        for item in value:
            _validate_form_value(item)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SkillValidationError(
                    "form_invalid_value", "frontmatter 키는 문자열이어야 합니다"
                )
            _validate_form_value(item)
        return
    raise SkillValidationError(
        "form_invalid_value", "frontmatter 폼 값은 문자열·문자열 목록·문자열 매핑이어야 합니다"
    )


def _drop_empty_top_level(fields: dict[str, Any]) -> dict[str, Any]:
    """빈 최상위 선택 필드는 키를 없앤 것으로 본다.

    ``safe_dump``는 빈 매핑·시퀀스를 ``{}``·``[]`` flow 스타일로 쓰는데, D1은 flow 스타일을
    표준 오류로 보고하므로 폼에서 비운 칸 하나가 스킬을 ``legacy_v1``로 떨어뜨린다.
    중첩 값(``metadata``의 빈 문자열 등)은 사용자가 쓴 원문이므로 건드리지 않는다.
    """

    return {key: value for key, value in fields.items() if value not in ("", {}, [])}


def rewrite_frontmatter(content: str, fields: dict[str, Any]) -> str:
    """폼 필드로 frontmatter만 다시 쓰고 본문·BOM·본문 줄바꿈은 그대로 둔다."""

    state = frontmatter_form(content)
    if not state.editable:
        raise SkillValidationError(
            "frontmatter_form_unavailable",
            "주석, 알 수 없는 키 또는 파싱 오류가 있는 frontmatter는 원문으로 편집해야 합니다",
        )
    unknown = sorted(set(fields) - FORM_FRONTMATTER_KEYS)
    if unknown:
        raise SkillValidationError(
            "form_unknown_key", "폼에서 지원하지 않는 키: " + ", ".join(unknown)
        )
    for value in fields.values():
        _validate_form_value(value)
    fields = _drop_empty_top_level(fields)
    # 필수 필드와 표준 파서의 문자열 규칙을 미리 확인한다.
    name, description = validate_frontmatter(fields)
    if len(name) > 120:
        raise SkillValidationError("frontmatter_name_too_long", "name은 120자 이하여야 합니다")
    if len(description) > 2000:
        raise SkillValidationError(
            "frontmatter_description_too_long", "description은 2000자 이하여야 합니다"
        )
    bom, _, body = _split_frontmatter_source(content)
    rendered = yaml.safe_dump(
        fields,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=1000,
    )
    updated = f"{bom}---\n{rendered}---\n{body}"
    # 왕복 검증: 파서가 읽는 값이 폼 값과 같고 본문이 그대로여야 저장한다. 중첩 한도
    # 초과(`yaml_too_deep`)는 여기서 파싱 오류로 올라가고, 파서가 줄바꿈으로 보는 문자
    # (U+2028 등)가 값에 있으면 다르게 읽히므로 거부한다.
    reparsed = parse_skill_md_detailed(updated)
    if reparsed.frontmatter != fields or reparsed.body != parse_skill_md_detailed(content).body:
        raise SkillValidationError(
            "form_roundtrip_mismatch",
            "폼 값을 SKILL.md에 그대로 쓸 수 없습니다. 원문 편집기로 수정하세요",
        )
    return updated


def _eval_warning(code: str, message: str) -> dict[str, str]:
    return std.finding(code, message)


def _eval_value_is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def evals_warnings(files: dict[str, bytes], skill_name: str) -> list[dict[str, str]]:
    """공식 skill-creator eval schema의 정적 오류를 policy 경고로 돌려준다.

    eval 실행은 Phase 5의 책임이다. 이 단계에서는 잘못된 JSON이나 나중에 실행기가
    해석할 수 없는 구조가 freeze를 막지는 않되, 검증 보고서에서 보이게 한다.
    """

    data = files.get(EVALS_PATH)
    if data is None:
        return []
    try:
        raw = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        return [_eval_warning("evals_not_utf8", f"{EVALS_PATH}는 UTF-8 JSON이어야 합니다")]
    except json.JSONDecodeError as exc:
        return [
            _eval_warning(
                "evals_invalid_json",
                f"{EVALS_PATH} JSON 파싱 실패: 줄 {exc.lineno}, 열 {exc.colno}",
            )
        ]
    if not isinstance(raw, dict):
        return [_eval_warning("evals_schema", f"{EVALS_PATH} 루트는 객체여야 합니다")]

    warnings: list[dict[str, str]] = []
    allowed_root = {"skill_name", "evals"}
    extra_root = sorted(set(raw) - allowed_root)
    if extra_root:
        warnings.append(
            _eval_warning("evals_schema", "지원하지 않는 evals 루트 키: " + ", ".join(extra_root))
        )
    if raw.get("skill_name") != skill_name:
        warnings.append(
            _eval_warning("evals_skill_name", "evals.skill_name은 SKILL.md의 name과 같아야 합니다")
        )
    cases = raw.get("evals")
    if not isinstance(cases, list):
        return warnings + [_eval_warning("evals_schema", "evals.evals는 배열이어야 합니다")]

    seen_ids: set[int] = set()
    allowed_case = {"id", "prompt", "expected_output", "files", "expectations"}
    for index, case in enumerate(cases, start=1):
        prefix = f"evals.evals[{index}]"
        if not isinstance(case, dict):
            warnings.append(_eval_warning("evals_schema", f"{prefix}는 객체여야 합니다"))
            continue
        extras = sorted(set(case) - allowed_case)
        if extras:
            warnings.append(
                _eval_warning("evals_schema", f"{prefix}의 지원하지 않는 키: " + ", ".join(extras))
            )
        case_id = case.get("id")
        if not isinstance(case_id, int) or isinstance(case_id, bool):
            warnings.append(_eval_warning("evals_schema", f"{prefix}.id는 정수여야 합니다"))
        elif case_id in seen_ids:
            warnings.append(_eval_warning("evals_duplicate_id", f"중복 eval id: {case_id}"))
        else:
            seen_ids.add(case_id)
        for key in ("prompt", "expected_output"):
            if not isinstance(case.get(key), str) or not case[key].strip():
                warnings.append(
                    _eval_warning(
                        "evals_schema", f"{prefix}.{key}는 비어 있지 않은 문자열이어야 합니다"
                    )
                )
        if "files" in case:
            if not _eval_value_is_string_list(case["files"]):
                warnings.append(
                    _eval_warning("evals_schema", f"{prefix}.files는 문자열 배열이어야 합니다")
                )
            else:
                for path in case["files"]:
                    try:
                        normalized = validate_path(path)
                    except SkillValidationError:
                        warnings.append(
                            _eval_warning(
                                "evals_file_path",
                                f"{prefix}.files 경로가 올바르지 않습니다: {path!r}",
                            )
                        )
                        continue
                    if normalized not in files:
                        warnings.append(
                            _eval_warning(
                                "evals_missing_file", f"{prefix}.files 파일이 없습니다: {path}"
                            )
                        )
        if "expectations" in case and not _eval_value_is_string_list(case["expectations"]):
            warnings.append(
                _eval_warning("evals_schema", f"{prefix}.expectations는 문자열 배열이어야 합니다")
            )
    return warnings
