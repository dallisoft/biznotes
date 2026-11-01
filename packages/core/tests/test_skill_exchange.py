"""가져오기·내보내기 교환 — 표준 계획 D3·D4·D10(수락 기준 1·4·6·7).

가져오기 판정은 `unpack_zip_detailed` → `evaluate_package` → `root_mismatch_warning`을
그대로 탄다. 서비스의 DB 부분(`import_zip_detailed`)은 이 셋을 순서대로 부르고 결과를
보고서에 담을 뿐이다.
"""

import io
import json
import stat
import zipfile
from pathlib import Path

import pytest
import yaml
from starlette.responses import Response

from biznotes_core.api.v1.skills import _safe_file_response, content_disposition_attachment
from biznotes_core.services.skills import package as pkg
from biznotes_core.services.skills import report as rpt
from biznotes_core.services.skills.parser import SkillParseError
from biznotes_core.services.skills.validator import SkillValidationError

CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "skills" / "golden-corpus.json").read_text("utf-8")
)
GOOD = "---\nname: sales-summary\ndescription: Summarize sales calls.\n---\n\n# Sales\n"


def make_zip(entries: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content.encode("utf-8") if isinstance(content, str) else content)
    return buf.getvalue()


def _case_skill_md(case: dict) -> str:
    """golden corpus 하네스가 oracle에 넣은 SKILL.md를 재구성한다."""
    fm = {
        "name": case.get("name", "a" * case.get("name_repeat", 0)),
        "description": case.get("description", "d" * case.get("description_repeat", 0)),
    }
    for field in ("compatibility", "license", "allowed-tools", "metadata", "unknown"):
        if field in case:
            fm[field] = case[field]
    if "compatibility_repeat" in case:
        fm["compatibility"] = "x" * case["compatibility_repeat"]
    return "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\nbody\n"


def _import(data: bytes) -> tuple[rpt.PackageEvaluation, dict | None]:
    unpacked = pkg.unpack_zip_detailed(data)
    evaluation = rpt.evaluate_package(unpacked.files)
    return evaluation, rpt.root_mismatch_warning(unpacked.root_name, evaluation.name)


# ---------- 수락 기준 1: golden corpus 24건의 가져오기 기대값 ----------


@pytest.mark.parametrize("case", CORPUS["cases"], ids=[c["id"] for c in CORPUS["cases"]])
def test_golden_corpus_import_expectations(case: dict) -> None:
    """root-mismatch는 경고 후 수용, 빈 name·description은 거부, 나머지는 oracle 오류와 같다."""
    root = case.get("root", "a" * case.get("root_repeat", 0))
    data = make_zip({f"{root}/SKILL.md": _case_skill_md(case)})

    if case["id"] in ("empty-name", "empty-description"):
        with pytest.raises(SkillParseError) as exc:
            _import(data)
        assert exc.value.reason == (
            "missing_name" if case["id"] == "empty-name" else "missing_description"
        )
        return

    evaluation, mismatch = _import(data)
    if case["id"] == "root-mismatch":
        assert evaluation.report["standard"]["errors"] == []
        assert mismatch is not None and mismatch["code"] == "root_mismatch"
        return
    assert evaluation.report["standard"]["errors"] == case["oracle_errors"]
    assert mismatch is None


def test_corpus_still_has_24_cases() -> None:
    assert len(CORPUS["cases"]) == 24


# ---------- 수락 기준 4: 정해진 reason으로 거부 ----------


def test_normalized_path_collision_is_rejected() -> None:
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip_detailed(make_zip({"root/SKILL.md": GOOD, "root/a/../SKILL.md": GOOD}))
    assert exc.value.reason == "path_collision"


def test_root_shadow_file_is_rejected() -> None:
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip_detailed(make_zip({"root/SKILL.md": GOOD, "root": "shadow"}))
    assert exc.value.reason == "root_shadow_entry"


def test_multiple_skills_in_one_zip_are_rejected() -> None:
    """D4: SKILL.md가 여럿인 ZIP은 1차에서 거부한다(선택 가져오기는 Phase 2)."""
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip_detailed(make_zip({"a/SKILL.md": GOOD, "b/SKILL.md": GOOD}))
    assert exc.value.reason == "multiple_skills"


def test_flat_package_with_a_nested_skill_is_rejected() -> None:
    """리뷰 F-02: 평면 패키지에 다른 스킬 폴더가 딸려 와도 거부한다."""
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip_detailed(make_zip({"SKILL.md": GOOD, "nested/SKILL.md": GOOD}))
    assert exc.value.reason == "multiple_skills"


def test_multiple_skills_can_be_listed_and_selected_without_mixing_files() -> None:
    data = make_zip(
        {
            "alpha/SKILL.md": "---\nname: alpha\ndescription: a\n---\n",
            "alpha/assets/a.bin": b"alpha",
            "beta/SKILL.md": "---\nname: beta\ndescription: b\n---\n",
            "beta/assets/b.bin": b"beta",
        }
    )
    assert pkg.skill_zip_candidates(data) == ["alpha/SKILL.md", "beta/SKILL.md"]

    selected = pkg.unpack_zip_detailed(data, selected_skill_path="beta/SKILL.md")
    assert selected.root_name == "beta"
    assert selected.files == {
        "SKILL.md": b"---\nname: beta\ndescription: b\n---\n",
        "assets/b.bin": b"beta",
    }


def test_selected_skill_with_a_nested_skill_folder_is_rejected() -> None:
    data = make_zip(
        {
            "alpha/SKILL.md": "---\nname: alpha\ndescription: a\n---\n",
            "beta/SKILL.md": "---\nname: beta\ndescription: b\n---\n",
            "beta/gamma/SKILL.md": "---\nname: gamma\ndescription: g\n---\n",
        }
    )
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip_detailed(data, selected_skill_path="beta/SKILL.md")
    assert exc.value.reason == "multiple_skills"


def test_selecting_the_root_skill_excludes_sibling_skill_folders() -> None:
    data = make_zip(
        {
            "SKILL.md": "---\nname: root\ndescription: r\n---\n",
            "references/r.md": "root",
            "beta/SKILL.md": "---\nname: beta\ndescription: b\n---\n",
        }
    )
    selected = pkg.unpack_zip_detailed(data, selected_skill_path="SKILL.md")
    assert set(selected.files) == {"SKILL.md", "references/r.md"}
    assert selected.root_name is None


def test_multiple_skills_reject_unknown_selection() -> None:
    data = make_zip(
        {
            "alpha/SKILL.md": "---\nname: alpha\ndescription: a\n---\n",
            "beta/SKILL.md": "---\nname: beta\ndescription: b\n---\n",
        }
    )
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip_detailed(data, selected_skill_path="missing/SKILL.md")
    assert exc.value.reason == "invalid_skill_selection"


@pytest.mark.parametrize(
    ("path", "reason"),
    [("references/" + "a" * 600 + ".md", "path_too_long"), ("references/a\x01b.md", "bad_path")],
    ids=["too-long", "control-char"],
)
def test_hostile_entry_names_are_rejected(path: str, reason: str) -> None:
    """리뷰 F-06: 저장소 키 상한·제어문자를 입력 경계에서 막는다."""
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip_detailed(make_zip({"SKILL.md": GOOD, path: "x"}))
    assert exc.value.reason == reason


def test_corrupt_entry_is_a_policy_error_not_a_crash() -> None:
    data = bytearray(make_zip({"SKILL.md": GOOD * 20}))
    # 압축된 본문 한가운데를 뒤집는다 — CRC 검사가 실패해야 한다
    offset = data.index(b"SKILL.md") + len(b"SKILL.md") + 10
    data[offset] ^= 0xFF
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip_detailed(bytes(data))
    assert exc.value.reason == "bad_zip"


@pytest.mark.parametrize(
    ("frontmatter", "reason"),
    [
        ("name: a\nname: b\ndescription: d", "yaml_duplicate_key"),
        ("name: &x a\ndescription: *x", "yaml_alias"),
        ("name: !!int 1\ndescription: d", "yaml_tag"),
        ("name: x\ndescription: d\nm: " + "[" * 100, "yaml_too_deep"),
    ],
    ids=["duplicate-key", "alias", "tag", "too-deep"],
)
def test_frontmatter_rejections_surface_through_import(frontmatter: str, reason: str) -> None:
    with pytest.raises(SkillParseError) as exc:
        _import(make_zip({"SKILL.md": f"---\n{frontmatter}\n---\nbody\n"}))
    assert exc.value.reason == reason


def test_dot_skill_archive_is_an_ordinary_zip() -> None:
    """`.skill`은 확장자만 다른 ZIP이다 — 같은 경로로 읽는다."""
    evaluation, _ = _import(make_zip({"sales-summary/SKILL.md": GOOD}))
    assert evaluation.name == "sales-summary"


# ---------- 수락 기준 6: 왕복·결정성·모드 ----------

FILES = {
    "SKILL.md": GOOD.encode(),
    "references/guide.md": "# guide\n본문".encode(),
    "scripts/pack.py": b"print('x')\n",
    "assets/logo.png": b"\x89PNG\r\n\x1a\n\x00binary",
    "skill.json": json.dumps({"mode": "instruction"}).encode(),
}


@pytest.mark.parametrize("layout", ["wrapped", "flat"])
def test_export_import_roundtrip_keeps_the_package_hash(layout: str) -> None:
    data = pkg.export_zip(FILES, "sales-summary", layout=layout)  # type: ignore[arg-type]
    unpacked = pkg.unpack_zip_detailed(data)
    assert unpacked.files == FILES
    assert pkg.package_hash(unpacked.files) == pkg.package_hash(FILES)
    assert unpacked.root_name == ("sales-summary" if layout == "wrapped" else None)


def test_export_bytes_are_deterministic() -> None:
    assert pkg.export_zip(FILES, "sales-summary") == pkg.export_zip(dict(FILES), "sales-summary")


def test_export_modes_mark_only_scripts_executable() -> None:
    with zipfile.ZipFile(io.BytesIO(pkg.export_zip(FILES, "sales-summary"))) as zf:
        modes = {info.filename: (info.external_attr >> 16) for info in zf.infolist()}
        systems = {info.create_system for info in zf.infolist()}
    assert systems == {3}  # Unix — unzip이 모드를 존중한다
    assert stat.S_IMODE(modes["sales-summary/scripts/pack.py"]) == 0o755
    assert stat.S_IMODE(modes["sales-summary/SKILL.md"]) == 0o644
    assert all(stat.S_ISREG(mode) for mode in modes.values())


def test_portable_export_drops_only_the_product_manifest() -> None:
    data = pkg.export_zip(FILES, "sales-summary", layout="flat", exclude=("skill.json",))
    unpacked = pkg.unpack_zip_detailed(data)
    assert set(unpacked.files) == set(FILES) - {"skill.json"}


def test_unknown_layout_is_rejected() -> None:
    with pytest.raises(SkillValidationError) as exc:
        pkg.export_zip(FILES, "sales-summary", layout="nested")  # type: ignore[arg-type]
    assert exc.value.reason == "bad_export_layout"


@pytest.mark.parametrize("path", ["assets/attack.html", "assets/attack.svg"])
def test_skill_file_serving_never_uses_active_content_types(path: str) -> None:
    response = _safe_file_response(b"<script>alert(1)</script>", path)
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_skill_file_serving_keeps_text_contract_and_inlines_safe_images() -> None:
    text = _safe_file_response(b"print('hi')\n", "scripts/run.py")
    assert text.headers["content-type"] == "text/plain; charset=utf-8"
    assert "content-disposition" not in text.headers

    png = _safe_file_response(b"\x89PNG\r\n\x1a\n\xff", "assets/logo.png")
    assert png.headers["content-type"] == "image/png"
    assert "content-disposition" not in png.headers
    assert png.body == b"\x89PNG\r\n\x1a\n\xff"


def test_skill_file_serving_does_not_inline_pdf() -> None:
    response = _safe_file_response(b"%PDF-1.7\n\xff\xfe", "assets/doc.pdf")
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")


@pytest.mark.parametrize("wrapper", ["bundle/", "repo-main/skills/"])
def test_wrapped_multi_skill_zip_lists_and_selects_full_paths(wrapper: str) -> None:
    data = make_zip(
        {
            f"{wrapper}alpha/SKILL.md": "---\nname: alpha\ndescription: a\n---\n",
            f"{wrapper}alpha/references/a.md": "alpha",
            f"{wrapper}beta/SKILL.md": "---\nname: beta\ndescription: b\n---\n",
        }
    )
    assert pkg.skill_zip_candidates(data) == [f"{wrapper}alpha/SKILL.md", f"{wrapper}beta/SKILL.md"]
    selected = pkg.unpack_zip_detailed(data, selected_skill_path=f"{wrapper}alpha/SKILL.md")
    assert selected.root_name == "alpha"
    assert set(selected.files) == {"SKILL.md", "references/a.md"}
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip_detailed(data)
    assert exc.value.reason == "multiple_skills"


def test_single_wrapped_skill_zip_is_unchanged_by_bundle_unwrapping() -> None:
    data = make_zip({"tool/SKILL.md": "---\nname: tool\ndescription: d\n---\n"})
    assert pkg.skill_zip_candidates(data) == ["tool/SKILL.md"]
    assert pkg.unpack_zip_detailed(data).root_name == "tool"


def test_skill_file_serving_sends_other_binaries_as_attachment() -> None:
    response = _safe_file_response(b"\xff\xfe\x00binary", "assets/file.한글")
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-content-type-options"] == "nosniff"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''file.%ED%95%9C%EA%B8%80" in disposition
    assert "assets%2F" not in disposition


# ---------- 수락 기준 7: 헤더 안전성 (sl 보류 테스트 2개 이식) ----------


@pytest.mark.parametrize(
    "filename",
    [
        "도구-v1.zip",
        "sales-summary-v1.zip",
        "🙂-v1.zip",
        "noext",
        "",
        'a"b-v1.zip',
        "---.zip",
        "bad\r\nX-Injected: 1-v1.zip",
        "tab\tname.zip",
    ],
    ids=[
        "unicode",
        "ascii",
        "emoji",
        "no-extension",
        "empty",
        "quote",
        "punctuation-only-stem",
        "crlf-injection",
        "tab",
    ],
)
def test_content_disposition_is_always_a_valid_header(filename: str) -> None:
    """어떤 입력에도 예외 없이 latin-1로 인코딩 가능한 헤더가 나온다(sl `2-03`·`2-16`)."""
    value = content_disposition_attachment(filename)
    assert "\r" not in value and "\n" not in value
    Response(content=b"x", media_type="application/zip", headers={"Content-Disposition": value})
    assert value.startswith("attachment; filename=")
    assert "filename*=UTF-8''" in value


def test_control_characters_are_stripped_before_both_halves() -> None:
    value = content_disposition_attachment("bad\r\nX-Injected: 1-v1.zip")
    assert "X-Injected" in value  # 문자는 남되 개행만 제거된다(내용 보존)
    assert "%0D%0A" not in value
    assert "\r" not in value and "\n" not in value
