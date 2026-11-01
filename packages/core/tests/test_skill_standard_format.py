"""AC3 — Agent Skills 표준(strict) 형식 검증 · safe ZIP · export roundtrip.

정본 oracle은 P1이 고정한 공식 `skills-ref 0.1.0`
(`tests/fixtures/skills/oracle-manifest.json` — 원본 소스 사본은 biznotes에 두지 않는다)이며,
케이스별 기대 결과는 `golden-corpus.json::cases[].oracle_errors`의 **원문**이다.
이 파일은 그 문자열과 제품 판정을 1:1로 대조한다 — 프로젝트가 만든 규칙을
"표준"이라고 부르지 않는다(PLAN §9.6).

제품 본문은 어떤 케이스에서도 mock/monkeypatch로 대체하지 않는다
(AGENTS.md §3.3-3).
"""

import io
import json
import zipfile
from pathlib import Path

import pytest

from biznotes_core.services.skills import package as pkg
from biznotes_core.services.skills import standard as std
from biznotes_core.services.skills.parser import (
    SkillParseError,
    parse_skill_md,
    parse_skill_md_detailed,
)
from biznotes_core.services.skills.validator import SkillValidationError

SKILLS_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "skills"

GOOD_SKILL_MD = """---
name: sales-summary
description: Summarize sales call notes into action items.
---

# Sales Summary

Body with {braces} preserved.
"""


def _corpus():
    return json.loads(
        (SKILLS_FIXTURE_ROOT / "golden-corpus.json").read_text(encoding="utf-8")
    )


def _corpus_cases():
    return [pytest.param(case, id=case["id"]) for case in _corpus()["cases"]]


def _package_cases():
    return [
        pytest.param(case, id=case["id"]) for case in _corpus()["package_cases"]
    ]


def _frontmatter_for(case):
    name = case.get("name", "a" * case.get("name_repeat", 0))
    description = case.get("description", "d" * case.get("description_repeat", 0))
    fm = {"name": name, "description": description}
    for field in ("compatibility", "license", "allowed-tools", "metadata", "unknown"):
        if field in case:
            fm[field] = case[field]
    if "compatibility_repeat" in case:
        fm["compatibility"] = "x" * case["compatibility_repeat"]
    return fm


def make_zip(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content.encode("utf-8") if isinstance(content, str) else content)
    return buf.getvalue()


# ========== oracle differential — name / description / compatibility / fields ==========


def _corpus_skill_md(case) -> str:
    """pinned oracle harness가 이 케이스에 넣었던 SKILL.md 원문을 재구성한다."""
    import yaml

    return (
        "---\n"
        + yaml.safe_dump(_frontmatter_for(case), allow_unicode=True, sort_keys=False)
        + "---\nfixture body\n"
    )


@pytest.mark.parametrize("case", _corpus_cases())
def test_standard_validator_matches_pinned_oracle_verbatim(case):
    """제품 strict 판정 == pinned oracle 출력(문자열 단위, 순서 포함).

    **전체 제품 경로를 통과시킨다**(리뷰 `2-05`): ZIP byte →
    `pkg.unpack_zip_detailed` → `parse_skill_md`(제품 YAML 파서) →
    `std.validate_standard_metadata`. 손으로 만든 dict를 검증기에 직접 넣으면
    제품 파서가 대조 대상에서 빠져 P1의 differential이 약해진다.

    corpus의 `root`는 oracle이 skill name과 대조하는 디렉토리 이름이며,
    `unpack_zip_detailed`가 보존한 `root_name`을 그대로 넘긴다 — 이것이 P1
    `root-mismatch` xfail의 해소 조건이었다.
    """
    root = case.get("root", "a" * case.get("root_repeat", 0))
    unpacked = pkg.unpack_zip_detailed(
        make_zip({f"{root}/SKILL.md": _corpus_skill_md(case)})
    )
    assert unpacked.root_name == root, "root가 검증기에 도달하지 않는다"

    parsed, _ = parse_skill_md(unpacked.files["SKILL.md"].decode())
    errors = std.validate_standard_metadata(parsed, unpacked.root_name)

    assert errors == case["oracle_errors"], (
        f"{case['id']}: 제품 strict 판정이 pinned oracle 원문과 다릅니다"
    )


# --- 타입 스칼라: 문자열 스칼라 로더로 oracle과 같은 판정 (D1, 수락 기준 2) ---


@pytest.mark.parametrize(
    ("literal", "field"),
    [
        ("2026-01-01", "description"),
        ("1.0", "compatibility"),
        ("true", "compatibility"),
        ("3.12", "compatibility"),
        ("yes", "description"),
    ],
    ids=[
        "date-description",
        "float-compatibility",
        "bool-compatibility",
        "version-compatibility",
        "yes-description",
    ],
)
def test_typed_block_scalars_match_the_oracle(literal, field):
    """strictyaml(oracle)은 모든 스칼라를 문자열로 읽는다. 제품 로더도 같다.

    예전(`yaml.safe_load`) 제품은 인용부호 없는 `description: 2026-01-01`을 date로 읽어
    "must be a non-empty string"으로 판정했다 — 그 델타가 사라졌음을 고정한다.
    """
    fields = {"name": "delta", "description": "d", field: literal}
    skill_md = "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items()) + "---\nbody\n"
    parsed, _ = parse_skill_md(skill_md)

    assert parsed[field] == literal
    assert std.validate_standard_metadata(parsed, "delta") == []


def test_flow_mapping_is_a_standard_error_not_a_rejection():
    """flow 매핑은 strictyaml이 거부한다 — 제품은 파싱하되 oracle 모양의 오류 한 줄로 보고한다."""
    parsed = parse_skill_md_detailed(
        "---\nname: flow\ndescription: d\nmetadata: {version: 1.0}\n---\nbody\n"
    )
    report = std.evaluate_frontmatter(
        parsed.frontmatter, parsed.body, flow_style=parsed.flow_style
    )
    assert report.errors == ["Invalid YAML in frontmatter: flow style"]
    assert report.validation_profile == std.VALIDATION_PROFILE_LEGACY


def test_yaml_alias_is_rejected_before_expansion():
    """리뷰 `2-01` 회귀: 325 byte alias 폭탄이 제품 파서에서 거부된다.

    입력은 리뷰어가 제출한 것과 **동일한 바이트**다. 이전 구현에서는
    `evaluate_frontmatter` → `standard_metadata` 직렬화가 254 MB로 팽창했다.
    """
    bomb = (
        "---\n"
        'a: &a ["x","x","x","x","x","x","x","x","x"]\n'
        "b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]\n"
        "c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]\n"
        "d: &d [*c,*c,*c,*c,*c,*c,*c,*c,*c]\n"
        "e: &e [*d,*d,*d,*d,*d,*d,*d,*d,*d]\n"
        "f: &f [*e,*e,*e,*e,*e,*e,*e,*e,*e]\n"
        "g: &g [*f,*f,*f,*f,*f,*f,*f,*f,*f]\n"
        "h: &h [*g,*g,*g,*g,*g,*g,*g,*g,*g]\n"
        "name: x\n"
        "description: d\n"
        "---\n"
        "body\n"
    )
    assert len(bomb.encode("utf-8")) == 325, "리뷰어가 제출한 입력과 byte가 달라졌다"

    with pytest.raises(SkillParseError) as excinfo:
        parse_skill_md(bomb)
    assert excinfo.value.reason == "yaml_alias"


def test_anchor_without_alias_is_rejected():
    """anchor만 있어도 거부한다 — strictyaml(oracle)은 anchor 자체를 받지 않는다(D1 YAML 처리 표).

    이전 판은 과잉 거부를 피하려 anchor만 있는 입력을 통과시켰다. 표준 계획은 oracle과의
    정합을 택했다 — anchor를 받으면 다른 클라이언트가 같은 SKILL.md를 거부한다.
    """
    with pytest.raises(SkillParseError) as excinfo:
        parse_skill_md("---\nname: anchored\ndescription: &d text\n---\nbody\n")
    assert excinfo.value.reason == "yaml_alias"


def test_standard_metadata_size_limit_is_a_policy_error():
    """alias 없이도 거대한 frontmatter는 policy error로 거부된다(심층 방어)."""
    big = "y" * (std.MAX_STANDARD_METADATA_BYTES + 1)
    report = std.evaluate_frontmatter(
        {"name": "big", "description": "d", "metadata": {"blob": big}},
        "body",
        root_name="big",
    )
    with pytest.raises(SkillValidationError) as excinfo:
        std.build_persisted_metadata(report)
    assert excinfo.value.reason == "standard_metadata_too_large"


def test_standard_metadata_within_limit_is_preserved_verbatim():
    report = std.evaluate_frontmatter(
        {"name": "keep", "description": "d", "unknown": "preserve-me"},
        "body",
        root_name="keep",
    )
    payload = std.build_persisted_metadata(report)
    assert payload == {"name": "keep", "description": "d", "unknown": "preserve-me"}


@pytest.mark.parametrize(
    ("length", "expected"),
    [
        (1, []),
        (64, []),
        (
            65,
            [
                "Skill name '" + "a" * 65 + "' exceeds 64 character limit (65 chars)"
            ],
        ),
    ],
    ids=["name-1", "name-64", "name-65"],
)
def test_name_length_boundaries(length, expected):
    name = "a" * length
    assert std.validate_standard_name(name, name) == expected


@pytest.mark.parametrize(
    ("length", "expected"),
    [
        (1, []),
        (1024, []),
        (1025, ["Description exceeds 1024 character limit (1025 chars)"]),
    ],
    ids=["description-1", "description-1024", "description-1025"],
)
def test_description_length_boundaries(length, expected):
    assert std.validate_standard_description("d" * length) == expected


@pytest.mark.parametrize(
    ("length", "expected"),
    [
        (1, []),
        (500, []),
        (501, ["Compatibility exceeds 500 character limit (501 chars)"]),
    ],
    ids=["compatibility-1", "compatibility-500", "compatibility-501"],
)
def test_compatibility_length_boundaries(length, expected):
    assert std.validate_standard_compatibility("x" * length) == expected


def test_compatibility_must_be_a_string():
    assert std.validate_standard_compatibility({"claude": "1"}) == [
        "Field 'compatibility' must be a string"
    ]


@pytest.mark.parametrize(
    "name",
    ["-tool", "tool-", "tool--name"],
    ids=["leading-hyphen", "trailing-hyphen", "consecutive-hyphen"],
)
def test_hyphen_rules_are_enforced(name):
    errors = std.validate_standard_name(name, name)
    assert errors, f"{name!r}가 strict에서 통과했습니다"


def test_unicode_name_is_accepted_by_the_pinned_oracle():
    """oracle은 Unicode 문자를 허용한다 — ASCII 전용 규칙을 임의로 만들지 않는다."""
    assert std.validate_standard_name("도구", "도구") == []


def test_ascii_name_is_accepted():
    assert std.validate_standard_name("sales-summary", "sales-summary") == []


def test_uppercase_name_is_rejected():
    assert std.validate_standard_name("Sales-Summary", "Sales-Summary") == [
        "Skill name 'Sales-Summary' must be lowercase"
    ]


def test_underscore_name_is_rejected():
    assert std.validate_standard_name("sales_summary", "sales_summary") == [
        "Skill name 'sales_summary' contains invalid characters. "
        "Only letters, digits, and hyphens are allowed."
    ]


def test_root_mismatch_is_rejected():
    """P1 `root-mismatch` xfail의 해소 — root를 검증기가 실제로 본다."""
    assert std.validate_standard_name("sales-summary", "other-root") == [
        "Directory name 'other-root' must match skill name 'sales-summary'"
    ]


def test_stored_package_has_no_directory_mismatch():
    """저장된 패키지의 디렉터리는 정의상 `name`이다(D3) — 판정에 root를 넘기지 않는다."""
    report = std.evaluate_frontmatter(
        {"name": "sales-summary", "description": "d"}, "body", root_name=None
    )
    assert report.errors == []
    assert report.validation_profile == std.VALIDATION_PROFILE_STANDARD


def test_metadata_values_are_preserved_and_nesting_is_a_portability_warning():
    """oracle은 metadata 값을 `str()`로 바꾸지만, 여기서는 원문을 보존하고 중첩만 경고한다."""
    report = std.evaluate_frontmatter(
        {"name": "meta", "description": "d", "metadata": {"author": "fixture", "version": "1"}},
        "body",
    )
    assert report.errors == []
    assert std.build_persisted_metadata(report)["metadata"] == {
        "author": "fixture",
        "version": "1",
    }

    nested = std.evaluate_frontmatter(
        {"name": "meta", "description": "d", "metadata": {"authors": ["a", "b"]}}, "body"
    )
    assert [w["code"] for w in nested.portability] == ["nested_metadata"]


def test_license_and_allowed_tools_are_allowed_fields():
    fm = {
        "name": "tool-policy",
        "description": "d",
        "license": "Apache-2.0",
        "allowed-tools": "Read Grep",
    }
    assert std.validate_standard_metadata(fm, "tool-policy") == []


def test_allowed_tools_is_recorded_verbatim_without_granting_or_limiting():
    """S-HCI-4: `allowed-tools`는 기록·표시 전용이다. 해석해 권한을 만들지 않는다."""
    fm = {"name": "tool-policy", "description": "d", "allowed-tools": "Read Bash(git:*)"}
    report = std.evaluate_frontmatter(fm, "body")
    assert report.errors == []
    assert std.build_persisted_metadata(report)["allowed-tools"] == "Read Bash(git:*)"
    assert not hasattr(report, "known_allowed_tools")


def test_allowed_tools_sequence_is_a_portability_warning():
    report = std.evaluate_frontmatter(
        {"name": "tool-policy", "description": "d", "allowed-tools": ["Read"]}, "body"
    )
    assert report.errors == []  # oracle은 allowed-tools를 검사하지 않는다
    assert [w["code"] for w in report.portability] == ["allowed_tools_sequence"]


def test_unknown_field_is_rejected_by_strict_but_preserved_verbatim():
    """strict 거부와 raw 보존을 분리한다(P1-STATUS §3 `unknown-field` 해소 조건)."""
    fm = {"name": "unknown", "description": "d", "unknown": "preserve-me"}
    report = std.evaluate_frontmatter(fm, "body", root_name="unknown")
    assert report.errors == [
        "Unexpected fields in frontmatter: unknown. "
        "Only ['allowed-tools', 'compatibility', 'description', 'license', "
        "'metadata', 'name'] are allowed."
    ]
    assert report.validation_profile == std.VALIDATION_PROFILE_LEGACY
    # 판정이 실패해도 원문은 손실 없이 남는다.
    assert report.metadata["unknown"] == "preserve-me"
    assert report.unknown_fields == ["unknown"]


# ========== §9.6 warning / policy error 분리 ==========


@pytest.mark.parametrize(
    "body",
    ["line\n" * std.BODY_MAX_LINES, "x" * (std.BODY_MAX_TOKENS * 4 + 4)],
    ids=["501-lines", "over-5000-tokens"],
)
def test_long_body_is_a_portability_warning_not_an_error(body):
    """명세 권고(500줄·약 5000토큰)를 넘으면 경고만 한다 — 프로필은 그대로다."""
    report = std.evaluate_frontmatter({"name": "long-body", "description": "d"}, body)
    assert report.errors == []
    assert report.validation_profile == std.VALIDATION_PROFILE_STANDARD
    assert [w["code"] for w in report.portability] == ["body_too_long"]


def test_body_within_the_recommendation_has_no_warning():
    body = "line\n" * (std.BODY_MAX_LINES - 2)
    report = std.evaluate_frontmatter({"name": "short-body", "description": "d"}, body)
    assert report.portability == []


def test_product_size_limit_is_a_policy_error_not_a_standard_error():
    """제품 파일 크기 제한은 strict 판정이 아니라 policy error다(§9.6)."""
    from biznotes_core.services.skills.validator import MAX_SINGLE_FILE_BYTES, validate_files

    files = {
        "SKILL.md": GOOD_SKILL_MD.encode(),
        "references/big.md": b"x" * (MAX_SINGLE_FILE_BYTES + 1),
    }
    with pytest.raises(SkillValidationError) as excinfo:
        validate_files(files)
    assert excinfo.value.reason == "file_too_large"
    # 같은 패키지가 표준 strict 축에서는 유효하다 — 두 축이 섞이지 않는다.
    report = std.evaluate_frontmatter(
        {"name": "sales-summary", "description": "d"}, "body", root_name=None
    )
    assert report.errors == []


# ========== safe ZIP: 충돌 / 경로 탈출 ==========


@pytest.mark.parametrize("case", _package_cases())
def test_pinned_package_cases_are_rejected(case):
    """corpus의 `package_cases`(경로 탈출·정규화 충돌)는 모두 거부된다."""
    entries = {
        name: (GOOD_SKILL_MD if name.endswith("SKILL.md") else "x")
        for name in case["entries"]
    }
    assert case["expected"] == "reject"
    with pytest.raises(SkillValidationError):
        pkg.unpack_zip(make_zip(entries))


def test_zip_absolute_path_is_rejected():
    with pytest.raises(SkillValidationError) as excinfo:
        pkg.unpack_zip(make_zip({"SKILL.md": GOOD_SKILL_MD, "/etc/passwd": "x"}))
    assert excinfo.value.reason == "absolute_path"


def test_zip_symlink_is_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", GOOD_SKILL_MD)
        info = zipfile.ZipInfo("link.md")
        info.external_attr = (0o120777 << 16) | 0o120000
        zf.writestr(info, "../../etc/passwd")
    with pytest.raises(SkillValidationError) as excinfo:
        pkg.unpack_zip(buf.getvalue())
    assert excinfo.value.reason == "symlink"


def test_duplicate_zip_entries_are_rejected():
    """같은 이름이 두 번 들어간 ZIP은 마지막 항목으로 조용히 덮이지 않는다."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", GOOD_SKILL_MD)
        zf.writestr("SKILL.md", GOOD_SKILL_MD.replace("sales-summary", "shadow"))
    with pytest.raises(SkillValidationError) as excinfo:
        pkg.unpack_zip(buf.getvalue())
    assert excinfo.value.reason == "path_collision"


def test_unpack_preserves_the_package_root_name():
    unpacked = pkg.unpack_zip_detailed(
        make_zip({"sales-summary/SKILL.md": GOOD_SKILL_MD})
    )
    assert unpacked.root_name == "sales-summary"
    assert set(unpacked.files) == {"SKILL.md"}


def test_flat_zip_has_no_root_name():
    unpacked = pkg.unpack_zip_detailed(make_zip({"SKILL.md": GOOD_SKILL_MD}))
    assert unpacked.root_name is None


# ========== export roundtrip ==========


def test_export_zip_roundtrip_preserves_bytes_and_hash():
    files = {
        "SKILL.md": GOOD_SKILL_MD.encode(),
        "references/guide.md": "# guide\n본문".encode(),
    }
    before = pkg.package_hash(files)

    data = pkg.export_zip(files, "sales-summary")
    unpacked = pkg.unpack_zip_detailed(data)

    assert unpacked.root_name == "sales-summary"
    assert unpacked.files == files
    assert pkg.package_hash(unpacked.files) == before


def test_export_zip_is_deterministic():
    files = {"SKILL.md": GOOD_SKILL_MD.encode()}
    assert pkg.export_zip(files, "sales-summary") == pkg.export_zip(
        files, "sales-summary"
    )


def test_export_zip_rejects_a_traversal_root():
    with pytest.raises(SkillValidationError):
        pkg.export_zip({"SKILL.md": GOOD_SKILL_MD.encode()}, "../escape")


def test_export_zip_rejects_a_nested_root():
    with pytest.raises(SkillValidationError) as excinfo:
        pkg.export_zip({"SKILL.md": GOOD_SKILL_MD.encode()}, "a/b")
    assert excinfo.value.reason == "bad_export_root"


def test_export_zip_requires_skill_md():
    with pytest.raises(SkillValidationError) as excinfo:
        pkg.export_zip({"references/a.md": b"x"}, "sales-summary")
    assert excinfo.value.reason == "missing_skill_md"


# ========== root shadow entry (리뷰 `2-10`) ==========


def test_root_shadow_entry_is_rejected_instead_of_silently_dropped():
    """root 디렉토리와 이름이 같은 최상위 항목이 조용히 사라지지 않는다."""
    data = make_zip({"root/SKILL.md": GOOD_SKILL_MD, "root": "나는 사라지면 안 된다"})
    with pytest.raises(SkillValidationError) as excinfo:
        pkg.unpack_zip_detailed(data)
    assert excinfo.value.reason == "root_shadow_entry"
