"""SKILL.md frontmatter 로더 — 표준 계획 D1의 YAML 처리 표(수락 기준 2·3).

문자열 스칼라 로더는 strictyaml(oracle)처럼 스칼라를 문자열로 읽고, strictyaml이 거부하는
구문(alias·anchor, 중복 키, 태그)과 깊은 중첩을 reason 코드와 함께 거부한다. flow 스타일은
파싱은 하되 표시만 한다(표준 오류로 보고, 거부하지 않음).

오류는 모두 `SkillParseError`로 나온다 — 새어 나가면 가져오기·검증·freeze 핸들러가
잡지 못해 500이 된다(#69).
"""

import inspect
import sys

import pytest
import yaml
from conftest import make_settings

from biznotes_core.services.skills import parser
from biznotes_core.services.skills.builder import load_skill_files
from biznotes_core.services.skills.parser import (
    SkillParseError,
    parse_skill_md,
    parse_skill_md_detailed,
    validate_frontmatter,
)


def _skill_md(frontmatter: str) -> str:
    return f"---\n{frontmatter}\n---\n\n# body"


# ---------- 문자열 스칼라 (oracle과 같은 판정) ----------


@pytest.mark.parametrize(
    ("line", "key", "expected"),
    [
        ("compatibility: 3.12", "compatibility", "3.12"),
        ("license: yes", "license", "yes"),
        ("license: 2026-01-01", "license", "2026-01-01"),
        ("license: ~", "license", "~"),
        ("compatibility: 0x1F", "compatibility", "0x1F"),
    ],
    ids=["float", "bool-yes", "date", "null-tilde", "hex-int"],
)
def test_block_scalars_are_read_as_strings(line: str, key: str, expected: str) -> None:
    fm, _ = parse_skill_md(_skill_md(f"name: demo\ndescription: d\n{line}"))
    assert fm[key] == expected


def test_nested_block_metadata_values_are_strings() -> None:
    fm, _ = parse_skill_md(_skill_md("name: demo\ndescription: d\nmetadata:\n  version: 1.0"))
    assert fm["metadata"] == {"version": "1.0"}


def test_safe_load_would_have_typed_the_same_scalars() -> None:
    """음성 대조: 예전 로더(`yaml.safe_load`)는 같은 입력을 타입으로 읽어 oracle과 갈렸다."""
    typed = yaml.safe_load("compatibility: 3.12\ndescription: yes\nd: 2026-01-01")
    assert typed["compatibility"] == 3.12
    assert typed["description"] is True
    assert not isinstance(typed["d"], str)


# ---------- flow 스타일 — 파싱은 하되 표시 ----------


@pytest.mark.parametrize(
    "frontmatter",
    [
        "name: demo\ndescription: d\nmetadata: {version: 1.0}",
        "name: demo\ndescription: d\nx: [a, b]",
    ],
    ids=["flow-mapping", "flow-sequence"],
)
def test_flow_style_is_parsed_and_flagged(frontmatter: str) -> None:
    parsed = parse_skill_md_detailed(_skill_md(frontmatter))
    assert parsed.flow_style is True
    assert parsed.frontmatter["name"] == "demo"


def test_block_style_is_not_flagged() -> None:
    parsed = parse_skill_md_detailed(
        _skill_md("name: demo\ndescription: d\nallowed-tools:\n  - Read\n  - Grep")
    )
    assert parsed.flow_style is False
    assert parsed.frontmatter["allowed-tools"] == ["Read", "Grep"]


# ---------- 거부 ----------


@pytest.mark.parametrize(
    ("frontmatter", "reason"),
    [
        ("name: demo\nname: other\ndescription: d", "yaml_duplicate_key"),
        ("name: demo\ndescription: d\nmetadata:\n  a: 1\n  a: 2", "yaml_duplicate_key"),
        ("name: !!int 3\ndescription: d", "yaml_tag"),
        ("name: demo\ndescription: !!python/object:os.system x", "yaml_tag"),
        ("name: ! demo\ndescription: d", "yaml_tag"),
        ("name: &n demo\ndescription: d", "yaml_alias"),
        ("name: &n demo\ndescription: *n", "yaml_alias"),
    ],
    ids=[
        "duplicate-key",
        "duplicate-nested-key",
        "int-tag",
        "python-object-tag",
        "non-specific-tag",
        "anchor",
        "alias",
    ],
)
def test_strictyaml_rejected_constructs_are_rejected(frontmatter: str, reason: str) -> None:
    with pytest.raises(SkillParseError) as exc:
        parse_skill_md(_skill_md(frontmatter))
    assert exc.value.reason == reason


def test_safe_load_accepts_what_the_loader_rejects() -> None:
    """음성 대조: 예전 로더는 중복 키를 마지막 값으로 덮고 `!!int`를 받아들였다."""
    assert yaml.safe_load("name: a\nname: b") == {"name": "b"}
    assert yaml.safe_load("name: !!int 3") == {"name": 3}


@pytest.mark.parametrize(
    "frontmatter",
    [
        pytest.param("a: " + "[" * 5000, id="unclosed-flow"),
        pytest.param("a: " + "[" * 2000 + "]" * 2000, id="closed-flow"),
        pytest.param("a: " + "{b: " * 2000 + "c" + "}" * 2000, id="nested-mapping"),
    ],
)
def test_deeply_nested_frontmatter_raises_parse_error(frontmatter: str) -> None:
    """#69 회귀: 깊은 중첩은 500이 아니라 `yaml_too_deep`이다."""
    with pytest.raises(SkillParseError, match="중첩이 너무 깊습니다") as exc:
        parse_skill_md(_skill_md(frontmatter))
    assert exc.value.reason == "yaml_too_deep"


def _block_nesting(depth: int) -> str:
    lines = [("  " * i) + f"k{i}:" for i in range(depth - 1)]
    lines.append(("  " * (depth - 1)) + "leaf: x")
    return "\n".join(lines)


def test_depth_limit_boundary() -> None:
    """루트 매핑이 깊이 1이다 — 상한(32)까지는 받고 하나 더 깊으면 거부한다."""
    parse_skill_md(_skill_md(_block_nesting(32)))
    with pytest.raises(SkillParseError) as exc:
        parse_skill_md(_skill_md(_block_nesting(33)))
    assert exc.value.reason == "yaml_too_deep"


def test_recursion_error_is_still_reported_when_the_limit_is_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """상한을 크게 잡은 배포에서도 `RecursionError`가 500으로 새지 않는다."""
    monkeypatch.setattr(parser, "get_settings", lambda: make_settings(skill_yaml_max_depth=256))
    original = sys.getrecursionlimit()
    sys.setrecursionlimit(len(inspect.stack()) + 100)
    try:
        with pytest.raises(SkillParseError) as exc:
            parse_skill_md(_skill_md("a: " + "[" * 250))
    finally:
        sys.setrecursionlimit(original)
    assert exc.value.reason == "yaml_too_deep"
    assert isinstance(exc.value.__cause__, RecursionError)


def test_load_skill_files_surfaces_parse_error() -> None:
    """가져오기·검증·freeze가 거치는 load 경로에서도 같은 예외가 나온다."""
    files = {"SKILL.md": _skill_md("a: " + "[" * 5000).encode("utf-8")}
    with pytest.raises(SkillParseError):
        load_skill_files(files)


def test_ordinary_yaml_error_carries_a_quoting_hint() -> None:
    """자동 복구 대신 수정 힌트를 준다(비목표: YAML 자동 복구)."""
    with pytest.raises(SkillParseError, match="YAML 파싱 실패") as exc:
        parse_skill_md(_skill_md("name: demo\ndescription: note: colon inside"))
    assert exc.value.reason == "parse_error"
    assert "따옴표" in str(exc.value)


def test_bom_is_stripped_and_flagged() -> None:
    parsed = parse_skill_md_detailed("﻿" + _skill_md("name: demo\ndescription: d"))
    assert parsed.had_bom is True
    assert parsed.frontmatter == {"name": "demo", "description": "d"}


def test_indented_dashes_do_not_close_the_frontmatter() -> None:
    """리뷰 F-05: 블록 스칼라 안의 들여쓴 `---`에서 frontmatter가 잘리지 않는다."""
    fm, body = parse_skill_md(
        "---\ndescription: |\n  본문 앞부분\n  ---\n  본문 뒷부분\nname: demo\n---\n\n# body"
    )
    assert fm["name"] == "demo"
    assert "---" in fm["description"]
    assert body == "# body"


def test_valid_frontmatter_unchanged() -> None:
    fm, body = parse_skill_md(_skill_md("name: demo\ndescription: 설명\nmetadata:\n  k: v"))
    assert fm == {"name": "demo", "description": "설명", "metadata": {"k": "v"}}
    assert body == "# body"


# ---------- 필수 필드 ----------


@pytest.mark.parametrize(
    ("frontmatter", "reason"),
    [
        ({"description": "d"}, "missing_name"),
        ({"name": "", "description": "d"}, "missing_name"),
        ({"name": ["a"], "description": "d"}, "missing_name"),
        ({"name": "demo"}, "missing_description"),
        ({"name": "demo", "description": "  "}, "missing_description"),
    ],
    ids=["no-name", "empty-name", "list-name", "no-description", "blank-description"],
)
def test_required_fields_have_reason_codes(frontmatter: dict, reason: str) -> None:
    with pytest.raises(SkillParseError) as exc:
        validate_frontmatter(frontmatter)
    assert exc.value.reason == reason
