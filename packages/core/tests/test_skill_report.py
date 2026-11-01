"""스킬 패키지 네 층 보고서와 실행 자격 — 표준 계획 D5·D6(수락 기준 5·15).

`evaluate_package`는 정책 위반을 예외로 올리고, 표준 위반·이식성·실행 능력은 보고서로만
남긴다. 제품 본문은 대체하지 않는다 — 설정만 테스트 값으로 바꾼다.
"""

import json
from collections.abc import Iterator

import pytest

from biznotes_core.config import reset_settings
from biznotes_core.services.skills import report as rpt
from biznotes_core.services.skills.parser import SkillParseError
from biznotes_core.services.skills.validator import SkillValidationError

MANIFEST_NAME = "skill.json"  # 코어 기본 설정의 제품 매니페스트 파일명


def skill_md(frontmatter: str, body: str = "# body\n") -> bytes:
    return f"---\n{frontmatter}\n---\n\n{body}".encode()


STANDARD = skill_md("name: sales-summary\ndescription: Summarize sales calls.")


@pytest.fixture
def runtime_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SKILL_STANDARD_RUNTIME_ENABLED", "true")
    reset_settings()
    yield
    monkeypatch.delenv("SKILL_STANDARD_RUNTIME_ENABLED")
    reset_settings()


def codes(items: list[dict[str, str]]) -> list[str]:
    return [item["code"] for item in items]


def test_report_has_four_layers_for_a_standard_package() -> None:
    evaluation = rpt.evaluate_package({"SKILL.md": STANDARD})
    report = evaluation.report
    assert set(report) == {"standard", "portability", "policy", "capability"}
    assert report["standard"] == {"profile": "standard_v1", "errors": []}
    assert report["policy"]["errors"] == []
    assert evaluation.validation_profile == "standard_v1"
    assert evaluation.standard_metadata == {
        "name": "sales-summary",
        "description": "Summarize sales calls.",
    }


def test_extension_field_is_legacy_but_not_rejected() -> None:
    """수락 기준 5: 확장 필드 패키지는 받아들이고 legacy_v1로 보고한다."""
    evaluation = rpt.evaluate_package(
        {"SKILL.md": skill_md("name: ext\ndescription: d\nversion: 2\nauthor: me")}
    )
    assert evaluation.validation_profile == "legacy_v1"
    assert evaluation.report["standard"]["errors"]
    assert evaluation.report["policy"]["errors"] == []
    # 확장 필드는 원문 그대로 보존한다
    assert evaluation.standard_metadata["version"] == "2"


def test_runtime_profile_is_legacy_by_default() -> None:
    """수락 기준 15: 플래그 기본값에서 새 freeze는 legacy다(매니페스트가 없어도)."""
    evaluation = rpt.evaluate_package({"SKILL.md": STANDARD})
    assert evaluation.runtime_profile == "legacy_instruction_v1"
    assert "standard_runtime_disabled" in codes(evaluation.report["capability"]["warnings"])


def test_runtime_profile_is_standard_when_enabled_without_manifest(runtime_enabled: None) -> None:
    evaluation = rpt.evaluate_package({"SKILL.md": STANDARD})
    assert evaluation.runtime_profile == "standard_agent_v1"
    assert evaluation.report["capability"]["runtime_profile"] == "standard_agent_v1"


def test_runtime_eligibility_is_independent_of_conformance(runtime_enabled: None) -> None:
    """S-HCI-11: 확장 필드(적합성 실패)여도 매니페스트가 없으면 표준 런타임 자격이 있다."""
    evaluation = rpt.evaluate_package({"SKILL.md": skill_md("name: ext\ndescription: d\nx: y")})
    assert evaluation.validation_profile == "legacy_v1"
    assert evaluation.runtime_profile == "standard_agent_v1"


def test_product_manifest_keeps_the_legacy_runtime(runtime_enabled: None) -> None:
    files = {"SKILL.md": STANDARD, MANIFEST_NAME: json.dumps({"mode": "instruction"}).encode()}
    evaluation = rpt.evaluate_package(files)
    assert evaluation.runtime_profile == "legacy_instruction_v1"
    assert evaluation.has_product_manifest is True
    assert "product_manifest" in codes(evaluation.report["capability"]["warnings"])


@pytest.mark.parametrize(
    ("content", "reason"),
    [(b"not json", "bad_manifest"), (b"[1, 2]", "bad_manifest")],
    ids=["invalid-json", "not-an-object"],
)
def test_broken_manifest_is_a_policy_error(content: bytes, reason: str) -> None:
    with pytest.raises(SkillValidationError) as exc:
        rpt.evaluate_package({"SKILL.md": STANDARD, MANIFEST_NAME: content})
    assert exc.value.reason == reason


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b'{"top_k": "abc"}', "bad_top_k"),
        (b'{"top_k": []}', "bad_top_k"),
        (("[" * 100_000).encode(), "bad_manifest"),
    ],
    ids=["top_k-string", "top_k-list", "deeply-nested-json"],
)
def test_hostile_manifest_values_are_policy_errors_not_500(content: bytes, reason: str) -> None:
    """리뷰 F-01: 매니페스트 경로의 `ValueError`·`TypeError`·`RecursionError`가 새면 500이 된다."""
    with pytest.raises(SkillValidationError) as exc:
        rpt.evaluate_package({"SKILL.md": STANDARD, MANIFEST_NAME: content})
    assert exc.value.reason == reason


def test_non_utf8_skill_md_is_a_parse_error() -> None:
    with pytest.raises(SkillParseError):
        rpt.evaluate_package({"SKILL.md": b"---\nname: \xff\n---\n"})


@pytest.mark.parametrize(
    "path",
    ["scripts/run.py", "scripts/run.PY", "scripts/run.bash", "scripts/run", "notes.MD"],
    ids=["py", "uppercase-ext", "bash", "no-extension", "uppercase-md"],
)
def test_secret_in_a_bundled_file_is_rejected(path: str) -> None:
    """D5: 스캔은 `scripts/` 전체 + 확장자 대소문자 무시다(리뷰 F-03)."""
    files = {"SKILL.md": STANDARD, path: b"KEY = 'AKIAABCDEFGHIJKLMNOP'\n"}
    with pytest.raises(SkillValidationError) as exc:
        rpt.evaluate_package(files)
    assert exc.value.reason == "secret_detected"


def test_binary_asset_is_not_scanned() -> None:
    """텍스트가 아닌 자산은 스캔 대상이 아니다(과잉 거부 방지)."""
    files = {"SKILL.md": STANDARD, "assets/logo.png": b"\x89PNG\r\n\x1a\nAKIAABCDEFGHIJKLMNOP"}
    assert rpt.evaluate_package(files).report["policy"]["errors"] == []


def test_standard_metadata_over_64kib_is_rejected() -> None:
    """수락 기준 4: metadata 64KiB 초과는 정해진 reason으로 거부된다."""
    big = "x" * (64 * 1024)
    with pytest.raises(SkillValidationError) as exc:
        rpt.evaluate_package({"SKILL.md": skill_md(f"name: big\ndescription: d\nlicense: {big}")})
    assert exc.value.reason == "standard_metadata_too_large"


def test_capability_warnings_describe_what_the_product_cannot_do() -> None:
    files = {
        "SKILL.md": skill_md(
            "name: docx\ndescription: d\ncompatibility: Requires python3 and network\n"
            "allowed-tools: Read Bash",
            body="Run `uv run scripts/pack.py` then `npx prettier`.\n",
        ),
        "scripts/pack.py": b"# /// script\n# dependencies = ['lxml']\n# ///\nprint(1)\n",
        "scripts/tool.ts": b"console.log(1)\n",
    }
    warnings = rpt.evaluate_package(files).report["capability"]["warnings"]
    assert codes(warnings) == [
        "standard_runtime_disabled",
        "scripts_not_executable",
        "script_runtime_unavailable",
        "compatibility_not_evaluated",
        "allowed_tools_not_enforced",
    ]
    runtime = next(w for w in warnings if w["code"] == "script_runtime_unavailable")
    for label in (".ts", "PEP 723", "uv run", "npx"):
        assert label in runtime["message"]


# #127: sl-agents `pdf` 스킬처럼 코드를 실행해 파일을 만들라는 지침
CODE_BODIES = {
    "python-fence": "Create the PDF:\n\n```python\nprint('pdf')\n```\n",
    "pip-install": "First run `pip install reportlab`.\n",
    "library-import": "Use this:\n\n    from reportlab.pdfgen import canvas\n",
    "script-command": "Run `python3 make_pdf.py --out report.pdf`.\n",
}
CODE_LABELS = {
    "python-fence": "실행 코드 블록",
    "pip-install": "패키지 설치",
    "library-import": "파일 생성 라이브러리",
    "script-command": "스크립트 실행 명령",
}
PLAIN_BODIES = {
    "summary": "# 요약\n\n노트를 읽고 핵심을 세 줄로 요약하세요. 파일을 만들 필요는 없습니다.\n",
    "json-fence": "출력 형식:\n\n```json\n{\"title\": \"...\"}\n```\n",
    "prose-python": "Explain what a Python import statement does in plain words.\n",
}


def _capability_codes(body: str, extra: dict[str, bytes] | None = None) -> list[dict[str, str]]:
    files = {"SKILL.md": skill_md("name: pdf\ndescription: d", body=body), **(extra or {})}
    return rpt.evaluate_package(files).report["capability"]["warnings"]


@pytest.mark.parametrize("case", sorted(CODE_BODIES))
def test_legacy_profile_warns_that_code_instructions_cannot_run(case: str) -> None:
    warnings = _capability_codes(CODE_BODIES[case])
    found = next(w for w in warnings if w["code"] == "instruction_cannot_execute_code")
    assert CODE_LABELS[case] in found["message"]
    assert "no_bundled_scripts" not in codes(warnings)


def test_product_manifest_skill_with_code_instructions_warns(runtime_enabled: None) -> None:
    """sl-agents 재현: 매니페스트가 있으면 플래그를 켜도 legacy이고 코드를 실행하지 못한다."""
    manifest = {MANIFEST_NAME: json.dumps({"mode": "instruction"}).encode()}
    warnings = _capability_codes(CODE_BODIES["library-import"], manifest)
    assert codes(warnings)[:2] == ["product_manifest", "instruction_cannot_execute_code"]
    assert "docs/SKILL_AUTHORING.md" in warnings[0]["message"]


@pytest.mark.parametrize("case", sorted(CODE_BODIES))
def test_standard_profile_without_bundled_scripts_warns(runtime_enabled: None, case: str) -> None:
    warnings = _capability_codes(CODE_BODIES[case])
    found = next(w for w in warnings if w["code"] == "no_bundled_scripts")
    assert CODE_LABELS[case] in found["message"]
    assert "instruction_cannot_execute_code" not in codes(warnings)


def test_standard_profile_with_non_runnable_scripts_still_warns(runtime_enabled: None) -> None:
    """`.ts`는 run_skill_script가 실행하지 않는 확장자다 — 실행할 번들 스크립트가 없는 셈이다."""
    warnings = _capability_codes(CODE_BODIES["pip-install"], {"scripts/make.ts": b"1\n"})
    assert "no_bundled_scripts" in codes(warnings)


def test_standard_profile_with_bundled_script_does_not_warn(runtime_enabled: None) -> None:
    warnings = _capability_codes(CODE_BODIES["script-command"], {"scripts/make_pdf.py": b"1\n"})
    assert "no_bundled_scripts" not in codes(warnings)
    assert "instruction_cannot_execute_code" not in codes(warnings)


@pytest.mark.parametrize("case", sorted(PLAIN_BODIES))
@pytest.mark.parametrize("enabled", [False, True], ids=["legacy", "standard"])
def test_plain_instructions_do_not_trigger_code_warnings(
    monkeypatch: pytest.MonkeyPatch, case: str, enabled: bool
) -> None:
    monkeypatch.setenv("SKILL_STANDARD_RUNTIME_ENABLED", str(enabled).lower())
    reset_settings()
    try:
        found = set(codes(_capability_codes(PLAIN_BODIES[case])))
    finally:
        monkeypatch.delenv("SKILL_STANDARD_RUNTIME_ENABLED")
        reset_settings()
    assert not found & {"instruction_cannot_execute_code", "no_bundled_scripts"}


def test_manifest_migration_hint_needs_the_standard_runtime() -> None:
    """플래그가 꺼져 있으면 매니페스트를 지워도 legacy다 — 이관 안내를 붙이지 않는다."""
    manifest = {MANIFEST_NAME: json.dumps({"mode": "instruction"}).encode()}
    warnings = _capability_codes("# body\n", manifest)
    found = next(w for w in warnings if w["code"] == "product_manifest")
    assert "SKILL_AUTHORING" not in found["message"]


def test_report_and_run_tool_agree_on_runnable_scripts() -> None:
    """보고서(files dict)와 run_skill_script(file_tree)는 같은 판정을 쓴다."""
    from types import SimpleNamespace

    from biznotes_core.services.skills import sandbox
    from biznotes_core.services.skills import standard as std

    paths = [
        "scripts/a.py", "scripts/b.SH", "scripts/c.mjs", "scripts/d.js", "scripts/e.ts",
        "scripts/sub/f.py", "scripts/README.md", "tools/g.py", "SKILL.md",
    ]  # fmt: skip
    version = SimpleNamespace(file_tree=[{"path": p, "size": 1} for p in paths])
    assert sandbox.script_paths(version) == sorted(p for p in paths if std.is_runnable_script(p))
    assert sandbox.script_paths(version) == [
        "scripts/a.py", "scripts/b.SH", "scripts/c.mjs", "scripts/d.js", "scripts/sub/f.py",
    ]  # fmt: skip
    assert sandbox.SCRIPT_EXTENSIONS is std.SCRIPT_EXTENSIONS


def test_report_does_not_import_the_sandbox_tool() -> None:
    """보고서는 CLI(validate)에서도 쓴다 — 실행 도구 모듈을 끌고 오지 않는다(리뷰 M1)."""
    import subprocess
    import sys

    probe = (
        "import sys, biznotes_core.services.skills.report\n"
        "print('biznotes_core.services.skills.sandbox' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True, text=True)
    assert out.stdout.strip() == "False"


def test_code_instruction_warning_does_not_block_import() -> None:
    """capability 층은 경고만 한다 — 정책 오류가 아니다."""
    evaluation = rpt.evaluate_package(
        {"SKILL.md": skill_md("name: pdf\ndescription: d", body=CODE_BODIES["pip-install"])}
    )
    assert evaluation.report["policy"]["errors"] == []


@pytest.mark.parametrize(
    ("frontmatter", "files"),
    [
        ("license: Proprietary. All rights reserved.", {}),
        ("license: see LICENSE.txt", {"LICENSE.txt": b"You may not redistribute this."}),
    ],
    ids=["field", "license-file"],
)
def test_redistribution_restrictions_are_warned_not_blocked(
    frontmatter: str, files: dict[str, bytes]
) -> None:
    """S-HCI-9: 재배포 제한은 표시·경고만 한다."""
    evaluation = rpt.evaluate_package(
        {"SKILL.md": skill_md(f"name: lic\ndescription: d\n{frontmatter}"), **files}
    )
    policy = evaluation.report["policy"]
    assert codes(policy["warnings"]) == ["license_restricts_redistribution"]
    assert policy["errors"] == []
    assert policy["license"]["files"] == sorted(files)


def test_permissive_license_has_no_warning() -> None:
    evaluation = rpt.evaluate_package(
        {"SKILL.md": skill_md("name: lic\ndescription: d\nlicense: Apache-2.0")}
    )
    assert evaluation.report["policy"]["warnings"] == []
    assert evaluation.report["policy"]["license"] == {"field": "Apache-2.0", "files": []}


def test_failure_report_has_the_same_shape() -> None:
    report = rpt.failure_report(SkillValidationError("secret_detected", "found"))
    assert set(report) == {"standard", "portability", "policy", "capability"}
    assert report["policy"]["errors"] == [{"code": "secret_detected", "message": "found"}]


@pytest.mark.parametrize(
    ("root", "name", "expected"),
    [
        (None, "sales-summary", None),
        ("sales-summary", "sales-summary", None),
        ("other-root", "sales-summary", "root_mismatch"),
    ],
    ids=["flat", "matching", "mismatch"],
)
def test_root_mismatch_is_a_warning(root: str | None, name: str, expected: str | None) -> None:
    warning = rpt.root_mismatch_warning(root, name)
    assert (warning["code"] if warning else None) == expected
