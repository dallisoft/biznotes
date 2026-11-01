"""#402 — 저장소 archive에서 중첩 스킬을 골라 가져온다.

GitHub 저장소 archive는 래퍼(`repo-main/`) 아래 `skills/<name>/SKILL.md`에 스킬을 두고, 루트에
`.github/`·플러그인 매니페스트·테스트를 함께 둔다. 픽스처는 그 구조를 `zipfile`·`tarfile`로
합성한다(실제 저장소 데이터는 쓰지 않는다). 본문을 mock으로 바꾸지 않고, 압축 해제 여부는
`ZipFile.open`·`TarFile.extractfile`에 위임하는 spy로만 관측한다.
"""

import gzip
import hashlib
import io
import math
import tarfile
import zipfile

import pytest
from fastapi import HTTPException, UploadFile
from pydantic import Field

from biznotes_core.config import CoreSettings, get_settings, reset_settings
from biznotes_core.core.ssrf import SSRFBlockedError
from biznotes_core.schemas.skill import (
    SkillImportCandidateOut,
    SkillImportCandidatesOut,
    SkillImportUrlCandidatesRequest,
)
from biznotes_core.services.skills import package as pkg
from biznotes_core.services.skills import report as rpt
from biznotes_core.services.skills import service as svc
from biznotes_core.services.skills import standard as std
from biznotes_core.services.skills.validator import MAX_PATH_BYTES, SkillValidationError

WRAPPER = "repo-main/"
NESTED = f"{WRAPPER}skills/humanize-korean/SKILL.md"
NESTED_PREFIX = f"{WRAPPER}skills/humanize-korean/"
CODEX = f"{WRAPPER}codex/skills/humanize-korean/SKILL.md"

Item = tuple[str, str, bytes]  # (종류 dir|file|symlink, archive 경로, 내용)


def skill_md(name: str) -> bytes:
    return f"---\nname: {name}\ndescription: {name} 스킬입니다.\n---\n\n# {name}\n".encode()


def github_layout(*, extra: list[Item] | None = None) -> list[Item]:
    """GitHub형 저장소 archive — 파일 159개(심링크 1 포함, max_files 100 초과)."""
    items: list[Item] = []
    seen: set[str] = set()

    def add(path: str, body: bytes, kind: str = "file") -> None:
        parts = path.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            directory = "/".join(parts[:depth]) + "/"
            if directory not in seen:  # GitHub archive처럼 디렉터리 항목을 명시한다
                seen.add(directory)
                items.append(("dir", directory, b""))
        items.append((kind, path, body))

    add(f"{WRAPPER}.claude-plugin/marketplace.json", b'{"plugins": []}\n')
    add(f"{WRAPPER}.github/workflows/test.yml", b"on: push\n")
    add(f"{WRAPPER}.gitignore", b"*.pyc\n")
    add(NESTED, skill_md("humanize-korean"))
    for i in range(15):
        add(f"{NESTED_PREFIX}references/ref{i:02d}.md", f"# 참고 {i}\n".encode())
    for i in range(3):
        add(f"{NESTED_PREFIX}references/roles/role{i}.md", f"# 역할 {i}\n".encode())
    for name in ("humanize", "humanize-redo", "humanize-scan"):
        add(f"{WRAPPER}skills/{name}/SKILL.md", skill_md(name))
    add(f"{WRAPPER}extras/skills/commit-ko/SKILL.md", skill_md("commit-ko"))
    add(f"{WRAPPER}extras/skills/commit-ko/references/commit-lexicon.md", b"# lexicon\n")
    codex = f"{WRAPPER}codex/skills/humanize-korean/"
    add(f"{codex}SKILL.md", skill_md("humanize-korean"))
    add(f"{codex}agents/openai.yaml", b"model: x\n")
    add(f"{codex}scripts/verify_gates.py", b"print('ok')\n")
    add(f"{codex}references", b"../../../skills/humanize-korean/references", kind="symlink")
    add(f"{WRAPPER}copilot/skills/humanize-korean/SKILL.md", skill_md("humanize-korean"))
    for i in range(127):
        add(f"{WRAPPER}tests/f{i}.txt", b"x\n")
    for kind, path, body in extra or []:
        add(path, body, kind)
    return items


def build_zip(items: list[Item], *, methods: dict[str, int] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for kind, path, body in items:
            if kind == "dir":
                zf.writestr(path, b"")
            elif kind == "symlink":
                info = zipfile.ZipInfo(path)
                info.create_system = 3  # Unix — 상위 16비트가 모드로 해석된다
                info.external_attr = 0o120777 << 16
                zf.writestr(info, body)
            else:
                zf.writestr(path, body, compress_type=(methods or {}).get(path))
    return buf.getvalue()


def build_tar_gz(items: list[Item]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for kind, path, body in items:
            member = tarfile.TarInfo(path.rstrip("/"))
            if kind == "dir":
                member.type = tarfile.DIRTYPE
                tf.addfile(member)
            elif kind == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = body.decode()
                tf.addfile(member)
            else:
                member.size = len(body)
                tf.addfile(member, io.BytesIO(body))
    return gzip.compress(buf.getvalue(), mtime=0)


def use_settings(**overrides: int) -> None:
    namespace: dict[str, object] = {"__annotations__": {name: int for name in overrides}}
    namespace.update({name: Field(default=value) for name, value in overrides.items()})
    reset_settings(type("Settings", (CoreSettings,), namespace))


@pytest.fixture(autouse=True)
def _restore_settings():
    yield
    reset_settings(CoreSettings)


@pytest.fixture
def zip_opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`ZipFile.open`에 위임하며 연 항목 이름을 기록한다 — 압축 해제 여부의 관측점."""
    opened: list[str] = []
    real = zipfile.ZipFile.open

    def spy(self, name, mode="r", *args, **kwargs):
        if mode == "r":  # 픽스처를 만드는 쓰기(`writestr`)는 세지 않는다
            opened.append(name.filename if isinstance(name, zipfile.ZipInfo) else name)
        return real(self, name, mode, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", spy)
    return opened


@pytest.fixture
def tar_extracts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    extracted: list[str] = []
    real = tarfile.TarFile.extractfile

    def spy(self, member):
        extracted.append(member.name if isinstance(member, tarfile.TarInfo) else member)
        return real(self, member)

    monkeypatch.setattr(tarfile.TarFile, "extractfile", spy)
    return extracted


def _entry_count(data: bytes) -> int:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return len(zf.infolist())


# ---------- 수락 기준 1: 고른 서브트리만 패키지가 된다 ----------


def test_nested_selection_imports_only_the_chosen_subtree(zip_opens: list[str]) -> None:
    data = build_zip(github_layout())
    assert _entry_count(data) > get_settings().skill_import_max_files

    unpacked = pkg.unpack_zip_detailed(data, selected_skill_path=NESTED)

    assert unpacked.root_name == "humanize-korean"
    assert len(unpacked.files) == 19
    assert {"SKILL.md", "references/ref00.md", "references/roles/role2.md"} <= set(unpacked.files)
    evaluation = rpt.evaluate_package(unpacked.files)
    assert evaluation.name == "humanize-korean"
    assert rpt.root_mismatch_warning(unpacked.root_name, evaluation.name) is None
    # 서브트리 밖(심링크·`.claude-plugin/`·채움 파일)은 한 번도 풀지 않는다
    assert len(zip_opens) == 19
    assert all(name.startswith(NESTED_PREFIX) for name in zip_opens)


def test_tar_gz_nested_selection_skips_outside_members(tar_extracts: list[str]) -> None:
    data = build_tar_gz(github_layout())

    unpacked = pkg.unpack_import_archive_detailed(data, selected_skill_path=NESTED)

    assert unpacked.root_name == "humanize-korean"
    assert len(unpacked.files) == 19
    assert len(tar_extracts) == 19
    assert all(name.startswith(NESTED_PREFIX) for name in tar_extracts)


def test_prefix_boundary_does_not_mix_sibling_folders() -> None:
    """`skills/humanize/`는 `skills/humanize-korean/`의 파일을 가져가지 않는다."""
    unpacked = pkg.unpack_zip_detailed(
        build_zip(github_layout()), selected_skill_path=f"{WRAPPER}skills/humanize/SKILL.md"
    )
    assert unpacked.files == {"SKILL.md": skill_md("humanize")}
    assert unpacked.root_name == "humanize"


def test_deep_selection_outside_the_wrapper_skills_folder() -> None:
    unpacked = pkg.unpack_zip_detailed(
        build_zip(github_layout()),
        selected_skill_path=f"{WRAPPER}extras/skills/commit-ko/SKILL.md",
    )
    assert set(unpacked.files) == {"SKILL.md", "references/commit-lexicon.md"}
    assert unpacked.root_name == "commit-ko"


def test_unpack_entries_accepts_a_deep_selection_over_the_whole_archive() -> None:
    """공개 `unpack_entries_detailed`도 같은 서브트리 규칙을 쓴다(임의 깊이)."""
    raw = {
        "repo-main/skills/a/SKILL.md": skill_md("a"),
        "repo-main/skills/a/references/r.md": b"r",
        "repo-main/skills/b/SKILL.md": skill_md("b"),
        "repo-main/.github/x.yml": b"x",
    }
    unpacked = pkg.unpack_entries_detailed(raw, selected_skill_path="repo-main/skills/a/SKILL.md")
    assert set(unpacked.files) == {"SKILL.md", "references/r.md"}
    assert unpacked.root_name == "a"


@pytest.mark.parametrize("builder", [build_zip, build_tar_gz], ids=["zip", "tar.gz"])
def test_hidden_folders_above_the_selection_do_not_matter(builder) -> None:
    """`.claude/skills/<name>/`처럼 선택 위쪽 경로의 숨김 폴더는 패키지 경로가 아니다."""
    items: list[Item] = [
        ("dir", "repo-main/", b""),
        ("file", "repo-main/.claude/skills/tool/SKILL.md", skill_md("tool")),
        ("file", "repo-main/.claude/skills/tool/references/r.md", b"r"),
        ("file", "repo-main/.claude/settings.json", b"{}"),
    ]
    unpacked = pkg.unpack_import_archive_detailed(
        builder(items), selected_skill_path="repo-main/.claude/skills/tool/SKILL.md"
    )
    assert set(unpacked.files) == {"SKILL.md", "references/r.md"}
    assert unpacked.root_name == "tool"


# ---------- 수락 기준 2: 서브트리 안의 위반은 같은 사유로 거부 ----------


@pytest.mark.parametrize(
    ("builder", "reason"),
    [(build_zip, "symlink"), (build_tar_gz, "unsafe_tar_member")],
    ids=["zip", "tar.gz"],
)
def test_symlink_inside_the_selected_subtree_is_rejected(builder, reason: str) -> None:
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_import_archive_detailed(builder(github_layout()), selected_skill_path=CODEX)
    assert exc.value.reason == reason


@pytest.mark.parametrize("builder", [build_zip, build_tar_gz], ids=["zip", "tar.gz"])
def test_hidden_path_inside_the_selected_subtree_is_rejected(builder) -> None:
    items = github_layout(extra=[("file", f"{NESTED_PREFIX}.private/notes.md", b"n")])
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_import_archive_detailed(builder(items), selected_skill_path=NESTED)
    assert exc.value.reason == "hidden_path"


@pytest.mark.parametrize("builder", [build_zip, build_tar_gz], ids=["zip", "tar.gz"])
def test_file_count_limit_applies_to_the_subtree_only(builder) -> None:
    """서브트리 19개 — 상한 19는 통과(밖의 140개는 세지 않는다), 18은 `too_many_files`."""
    data = builder(github_layout())
    use_settings(skill_import_max_files=19)
    assert len(pkg.unpack_import_archive_detailed(data, selected_skill_path=NESTED).files) == 19
    use_settings(skill_import_max_files=18)
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_import_archive_detailed(data, selected_skill_path=NESTED)
    assert exc.value.reason == "too_many_files"


@pytest.mark.parametrize("builder", [build_zip, build_tar_gz], ids=["zip", "tar.gz"])
@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"skill_import_max_file_bytes": 40}, "file_too_large"),
        ({"skill_import_max_total_bytes": 200}, "package_too_large"),
    ],
)
def test_size_limits_apply_inside_the_subtree(builder, overrides, reason: str) -> None:
    big = b"y" * 4096
    items = github_layout(extra=[("file", f"{WRAPPER}tests/big.bin", big)])
    data = builder(items)
    use_settings(**overrides)
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_import_archive_detailed(data, selected_skill_path=NESTED)
    assert exc.value.reason == reason


def test_size_limits_ignore_files_outside_the_subtree() -> None:
    items = github_layout(extra=[("file", f"{WRAPPER}tests/big.bin", b"y" * 4096)])
    use_settings(skill_import_max_file_bytes=1024)
    unpacked = pkg.unpack_zip_detailed(build_zip(items), selected_skill_path=NESTED)
    assert len(unpacked.files) == 19


def test_nested_skill_inside_the_selected_folder_is_rejected() -> None:
    """고른 폴더 안의 중첩(상대 깊이 1)은 지금처럼 `multiple_skills`."""
    items = github_layout(extra=[("file", f"{NESTED_PREFIX}inner/SKILL.md", skill_md("inner"))])
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip_detailed(build_zip(items), selected_skill_path=NESTED)
    assert exc.value.reason == "multiple_skills"


# ---------- 수락 기준 3: archive 전체 관문은 선택과 무관 ----------


@pytest.mark.parametrize("builder", [build_zip, build_tar_gz], ids=["zip", "tar.gz"])
def test_archive_entry_cap_applies_to_the_whole_archive_with_a_selection(builder) -> None:
    items = github_layout()
    data = builder(items)
    use_settings(skill_import_max_archive_entries=len(items))
    assert len(pkg.unpack_import_archive_detailed(data, selected_skill_path=NESTED).files) == 19
    use_settings(skill_import_max_archive_entries=len(items) - 1)
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_import_archive_detailed(data, selected_skill_path=NESTED)
    assert exc.value.reason == "too_many_archive_entries"


@pytest.mark.parametrize("builder", [build_zip, build_tar_gz], ids=["zip", "tar.gz"])
def test_archive_entry_cap_applies_without_a_selection(builder) -> None:
    """디렉터리 항목도 센다 — 선택 없는 기존 경로에도 같은 상한이 걸린다."""
    items: list[Item] = [
        ("dir", "tool/", b""),
        ("file", "tool/SKILL.md", skill_md("tool")),
        ("file", "tool/references/r.md", b"r"),
    ]
    data = builder(items)
    use_settings(skill_import_max_archive_entries=3)
    assert pkg.unpack_import_archive_detailed(data).root_name == "tool"
    use_settings(skill_import_max_archive_entries=2)
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_import_archive_detailed(data)
    assert exc.value.reason == "too_many_archive_entries"


def test_archive_entry_cap_default_is_2000() -> None:
    assert CoreSettings().skill_import_max_archive_entries == 2000


def test_outside_entry_with_an_unreadable_method_is_rejected(zip_opens: list[str]) -> None:
    path = f"{WRAPPER}tests/blob.bin"
    data = build_zip(
        github_layout(extra=[("file", path, b"z" * 2048)]), methods={path: zipfile.ZIP_BZIP2}
    )
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip_detailed(data, selected_skill_path=NESTED)
    assert exc.value.reason == "unsupported_compression"
    assert zip_opens == []  # 중앙 디렉터리만 보고 거부한다


def test_outside_entry_declared_ratio_is_checked_against_the_cap(zip_opens: list[str]) -> None:
    path = f"{WRAPPER}tests/data.json"
    payload = b'{"k": "' + b"ab" * 40_000 + b'"}\n'
    data = build_zip(github_layout(extra=[("file", path, payload)]))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        info = zf.getinfo(path)
        ratio = info.file_size / max(info.compress_size, 1)
    assert ratio > 10 and ratio != math.floor(ratio)

    use_settings(skill_import_max_compression_ratio=math.ceil(ratio))
    assert len(pkg.unpack_zip_detailed(data, selected_skill_path=NESTED).files) == 19
    zip_opens.clear()
    use_settings(skill_import_max_compression_ratio=math.floor(ratio))
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip_detailed(data, selected_skill_path=NESTED)
    assert exc.value.reason == "bomb_suspected"
    assert zip_opens == []


async def test_upload_cap_applies_before_the_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    """업로드 크기 상한은 서비스(선택 처리) 전에 라우터에서 걸린다."""
    from biznotes_core.api.v1 import skills as skills_api

    data = build_zip(github_layout())

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("업로드 상한을 넘으면 서비스에 닿지 않는다")

    monkeypatch.setattr(skills_api.svc, "import_zip_detailed", must_not_run)
    use_settings(skill_import_max_upload_bytes=len(data))
    upload = UploadFile(file=io.BytesIO(data), filename="repo.zip")
    assert await skills_api._read_skill_zip_upload(upload) == data
    use_settings(skill_import_max_upload_bytes=len(data) - 1)
    with pytest.raises(HTTPException) as exc:
        await skills_api.import_skill_zip(
            file=UploadFile(file=io.BytesIO(data), filename="repo.zip"),
            selected_skill_path=NESTED,
            user=object(),  # type: ignore[arg-type]
            db=object(),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 413


# ---------- 선택값은 후보와 정확히 같아야 한다 ----------


@pytest.mark.parametrize(
    "selection",
    [
        "skills/humanize-korean/SKILL.md",  # 래퍼 누락
        f"{NESTED_PREFIX}",
        f"{WRAPPER}skills/humanize-korean",
        f"{WRAPPER}skills/./humanize-korean/SKILL.md",
        f"{WRAPPER}/skills/humanize-korean/SKILL.md",
        f"{WRAPPER}skills/humanize/../humanize-korean/SKILL.md",
        f"/{NESTED}",
        NESTED.replace("/", "\\"),
        f"{WRAPPER}tests/SKILL.md",  # 형식은 맞지만 archive에 없다
        f"{WRAPPER}missing/SKILL.md",
        f"{WRAPPER}skills/humanize-korean/SKILL.MD",
    ],
)
def test_selection_must_be_an_exact_candidate(selection: str, zip_opens: list[str]) -> None:
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip_detailed(build_zip(github_layout()), selected_skill_path=selection)
    assert exc.value.reason == "invalid_skill_selection"
    assert zip_opens == []


def test_macosx_and_overlong_paths_are_not_selectable() -> None:
    long_dir = "d" * (MAX_PATH_BYTES - len("x/SKILL.md") + 1)
    long_path = f"x/{long_dir}/SKILL.md"
    assert len(long_path.encode()) > MAX_PATH_BYTES
    items = github_layout(
        extra=[
            ("file", f"__MACOSX/{NESTED}", skill_md("humanize-korean")),
            ("file", long_path, skill_md("long")),
        ]
    )
    data = build_zip(items)
    for selection in (f"__MACOSX/{NESTED}", long_path):
        with pytest.raises(SkillValidationError) as exc:
            pkg.unpack_zip_detailed(data, selected_skill_path=selection)
        assert exc.value.reason == "invalid_skill_selection"


def test_archive_candidates_list_every_depth_in_normal_form() -> None:
    names = [path for kind, path, _ in github_layout() if kind != "dir"] + [
        f"__MACOSX/{NESTED}",
        "a/../SKILL.md",
        "b//SKILL.md",
        "/abs/SKILL.md",
        "c\\d/SKILL.md",
    ]
    assert pkg._archive_skill_candidates(names) == [
        CODEX,
        f"{WRAPPER}copilot/skills/humanize-korean/SKILL.md",
        f"{WRAPPER}extras/skills/commit-ko/SKILL.md",
        f"{WRAPPER}skills/humanize-korean/SKILL.md",
        f"{WRAPPER}skills/humanize-redo/SKILL.md",
        f"{WRAPPER}skills/humanize-scan/SKILL.md",
        f"{WRAPPER}skills/humanize/SKILL.md",
    ]


# ---------- 수락 기준 7: 선택 없음·루트 선택은 기존 경로 ----------


def test_github_archive_without_a_selection_keeps_the_old_reason() -> None:
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip_detailed(build_zip(github_layout()))
    assert exc.value.reason == "symlink"


def test_tar_budget_entry_term_uses_the_larger_limit() -> None:
    """서브트리 밖 member도 같은 예산으로 흘려보낸다 — 항목당 항은 두 상한 중 큰 쪽."""
    default = CoreSettings()
    assert pkg._tar_gz_budget(default) == default.skill_import_max_total_bytes * 2
    use_settings(skill_import_max_archive_entries=5000)
    assert pkg._tar_gz_budget(get_settings()) == (
        pkg._TAR_GZ_STRUCTURE_BASE_BYTES + pkg._TAR_GZ_STRUCTURE_PER_ENTRY_BYTES * 5001
    )


# ---------- tar 확장 헤더(리뷰 H2) ----------
# `tarfile`은 PAX·GNU longname 헤더를 member로 내보내지 않고 다음 헤더를 재귀로 읽는다. 그래서
# member 수 상한에 잡히지 않았고, 연속 수백 개면 처리되지 않은 RecursionError(500)가 났다.


def pax_record(key: str, value: str) -> bytes:
    """PAX 레코드 `"<길이> key=value\\n"` — 길이는 자기 자신을 포함한다."""
    body = f" {key}={value}\n"
    size = len(body) + 1
    while len(str(size)) + len(body) != size:
        size += 1
    return f"{size}{body}".encode()


def raw_tar_gz(items: list[Item]) -> bytes:
    """writer가 스스로 만들지 않는 모양(연속 PAX 헤더 등)을 그대로 쓴다. kind는 pax·file."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for kind, path, body in items:
            member = tarfile.TarInfo(path)
            if kind == "pax":
                member.type = tarfile.XHDTYPE
            member.size = len(body)
            tf.addfile(member, io.BytesIO(body))
    return gzip.compress(buf.getvalue(), mtime=0)


PAX: Item = ("pax", "././@PaxHeader", pax_record("comment", "x"))
TOOL = "repo/skills/tool/SKILL.md"
TOOL_FILES: list[Item] = [
    ("file", TOOL, skill_md("tool")),
    ("file", "repo/skills/tool/references/r.md", b"r"),
]


@pytest.mark.parametrize("selection", [TOOL, None], ids=["selected", "no-selection"])
def test_pax_headers_spread_over_few_members_are_rejected(selection: str | None) -> None:
    """리뷰 사례 1 — PAX 3000개 + 일반 31개(member 수로는 상한 안)."""
    items: list[Item] = []
    for i in range(29):
        items += [PAX] * 104 + [("file", f"repo/tests/f{i}.txt", b"x")]
    items += [PAX] * 3 + TOOL_FILES
    assert sum(1 for kind, _, _ in items if kind == "pax") >= 3000
    data = raw_tar_gz(items)
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_import_archive_detailed(data, selected_skill_path=selection)
    assert exc.value.reason == "bad_tar_gz"


@pytest.mark.parametrize("selection", [TOOL, None], ids=["selected", "no-selection"])
def test_extension_headers_count_against_the_entry_cap(selection: str | None) -> None:
    """확장 헤더 총수는 member 수와 따로 세어 같은 상한을 건다(==상한 통과, +1 거부)."""
    members = TOOL_FILES + [("file", f"repo/skills/tool/r{i}.md", b"r") for i in range(4)]
    items: list[Item] = []
    for member in members:
        items += [PAX, PAX, member]
    data = raw_tar_gz(items)  # member 6개, 확장 헤더 12개
    use_settings(skill_import_max_archive_entries=12)
    assert len(pkg.unpack_import_archive_detailed(data, selected_skill_path=selection).files) == 6
    use_settings(skill_import_max_archive_entries=11)
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_import_archive_detailed(data, selected_skill_path=selection)
    assert exc.value.reason == "too_many_archive_entries"


@pytest.mark.parametrize("selection", [TOOL, None], ids=["selected", "no-selection"])
def test_consecutive_pax_chain_is_a_policy_error(selection: str | None) -> None:
    """리뷰 사례 2 — 연속 PAX 400개는 재귀 오류(500)가 아니라 정책 사유다."""
    data = raw_tar_gz([PAX] * 400 + TOOL_FILES)
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_import_archive_detailed(data, selected_skill_path=selection)
    assert exc.value.reason == "bad_tar_gz"


def test_outside_files_before_a_pax_chain() -> None:
    """리뷰 사례 3 — 밖의 파일 101개 뒤 PAX 사슬. 선택 없으면 전처럼 `too_many_files`다."""
    items: list[Item] = [("file", f"repo/tests/f{i}.txt", b"x") for i in range(101)]
    data = raw_tar_gz(items + [PAX] * 400 + TOOL_FILES)
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_import_archive_detailed(data, selected_skill_path=TOOL)
    assert exc.value.reason == "bad_tar_gz"
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_import_archive_detailed(data)
    assert exc.value.reason == "too_many_files"


def test_parser_recursion_is_a_policy_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """연속 상한이 없더라도 재귀 오류는 `bad_tar_gz`로 바뀐다(마지막 방어선)."""
    monkeypatch.setattr(pkg, "_TAR_MAX_EXTENSION_RUN", 10**6)
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_import_archive_detailed(raw_tar_gz([PAX] * 1000 + TOOL_FILES))
    assert exc.value.reason == "bad_tar_gz"


def test_malformed_pax_value_is_a_policy_error() -> None:
    """`tarfile`이 그대로 내는 ValueError(잘못된 GNU sparse map)도 `bad_tar_gz`다."""
    bad = ("pax", "././@PaxHeader", pax_record("GNU.sparse.map", "a,b"))
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_import_archive_detailed(raw_tar_gz([bad, *TOOL_FILES]))
    assert exc.value.reason == "bad_tar_gz"


@pytest.mark.parametrize(
    ("fmt", "name"),
    [(tarfile.PAX_FORMAT, "참고-문서.md"), (tarfile.GNU_FORMAT, "r" * 150 + ".md")],
    ids=["pax-non-ascii", "gnu-longname"],
)
def test_ordinary_extension_headers_still_pass(fmt: int, name: str) -> None:
    """정상 archive의 확장 헤더(전역 PAX + member PAX, GNU longname)는 그대로 통과한다."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=fmt, pax_headers={"comment": "abc"}) as tf:
        for path, body in [(TOOL, skill_md("tool")), (f"repo/skills/tool/references/{name}", b"r")]:
            member = tarfile.TarInfo(path)
            member.size = len(body)
            tf.addfile(member, io.BytesIO(body))
    data = gzip.compress(buf.getvalue(), mtime=0)
    unpacked = pkg.unpack_import_archive_detailed(data, selected_skill_path=TOOL)
    assert set(unpacked.files) == {"SKILL.md", f"references/{name}"}


# ---------- 수락 기준 4: 후보 조회(URL·ZIP)와 sha256 고정 ----------

URL = "https://example.test/repo/zip/0123abcd"
ALL_CANDIDATES = [
    CODEX,
    f"{WRAPPER}copilot/skills/humanize-korean/SKILL.md",
    f"{WRAPPER}extras/skills/commit-ko/SKILL.md",
    NESTED,
    f"{WRAPPER}skills/humanize-redo/SKILL.md",
    f"{WRAPPER}skills/humanize-scan/SKILL.md",
    f"{WRAPPER}skills/humanize/SKILL.md",
]


def _by_path(result: svc.ImportCandidates) -> dict[str, rpt.ImportCandidate]:
    return {candidate.path: candidate for candidate in result.candidates}


class _Response:
    status_code = 200

    def __init__(self, content: bytes) -> None:
        self.content = content


def serve(monkeypatch: pytest.MonkeyPatch, *bodies: bytes) -> list[tuple[str, dict]]:
    """`ssrf_safe_fetch`만 바꾼다 — 후보 조회도 `_fetch_import_archive` 관문을 그대로 탄다."""
    calls: list[tuple[str, dict]] = []
    queue = list(bodies)

    def fetch(url: str, **kwargs: object) -> _Response:
        calls.append((url, kwargs))
        return _Response(queue.pop(0) if len(queue) > 1 else queue[0])

    monkeypatch.setattr(svc, "ssrf_safe_fetch", fetch)
    return calls


def test_zip_candidates_list_every_nested_skill_with_a_summary(zip_opens: list[str]) -> None:
    data = build_zip(github_layout())

    result = svc.zip_import_candidates(data)

    assert [candidate.path for candidate in result.candidates] == ALL_CANDIDATES
    assert result.archive_sha256 == hashlib.sha256(data).hexdigest()
    assert result.truncated == 0
    nested = _by_path(result)[NESTED]
    assert nested.name == "humanize-korean"
    assert nested.description == "humanize-korean 스킬입니다."
    assert (nested.file_count, nested.ok, nested.errors) == (19, True, [])
    assert nested.validation_profile == std.VALIDATION_PROFILE_STANDARD
    codex = _by_path(result)[CODEX]
    assert (codex.ok, codex.errors, codex.name) == (False, ["symlink"], "humanize-korean")
    assert _by_path(result)[f"{WRAPPER}extras/skills/commit-ko/SKILL.md"].file_count == 2
    assert all(
        candidate.ok for candidate in result.candidates if candidate.path != CODEX
    )
    # 후보의 SKILL.md만 푼다 — 채움 파일·심링크·숨김 파일은 열지 않는다
    assert sorted(zip_opens) == sorted(ALL_CANDIDATES)


async def test_url_candidates_use_the_ssrf_gate_and_list_tar_gz(
    monkeypatch: pytest.MonkeyPatch, tar_extracts: list[str]
) -> None:
    data = build_tar_gz(github_layout())
    calls = serve(monkeypatch, data)

    result = await svc.url_import_candidates(URL)

    assert calls == [
        (URL, {"allow_private": False, "max_bytes": get_settings().skill_import_max_upload_bytes})
    ]
    assert [candidate.path for candidate in result.candidates] == ALL_CANDIDATES
    assert result.archive_sha256 == hashlib.sha256(data).hexdigest()
    assert _by_path(result)[CODEX].errors == ["unsafe_tar_member"]
    assert (_by_path(result)[NESTED].file_count, _by_path(result)[NESTED].ok) == (19, True)
    assert sorted(tar_extracts) == sorted(ALL_CANDIDATES)


async def test_import_after_candidates_rejects_a_changed_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """조회와 가져오기 사이에 archive가 바뀌면 unpack 전에 `digest_mismatch`다."""
    first = build_zip(github_layout())
    changed = build_zip(github_layout(extra=[("file", f"{NESTED_PREFIX}references/new.md", b"n")]))
    serve(monkeypatch, first, changed)

    listing = await svc.url_import_candidates(URL)

    async def must_not_persist(*_args, **_kwargs):
        raise AssertionError("digest가 다르면 저장하지 않는다")

    monkeypatch.setattr(svc, "_import_unpacked", must_not_persist)
    with pytest.raises(SkillValidationError) as exc:
        await svc.import_url_detailed(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            URL,
            sha256=listing.archive_sha256,
            selected_skill_path=NESTED,
        )
    assert exc.value.reason == "digest_mismatch"


async def test_import_after_candidates_with_the_same_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = build_zip(github_layout())
    serve(monkeypatch, data)
    stored: dict[str, object] = {}

    async def persist(_db, _owner, unpacked, *, origin_detail):
        stored["files"] = unpacked.files
        stored["origin_detail"] = origin_detail
        return object()

    monkeypatch.setattr(svc, "_import_unpacked", persist)
    listing = await svc.url_import_candidates(URL)
    await svc.import_url_detailed(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        URL,
        sha256=listing.archive_sha256,
        selected_skill_path=NESTED,
    )
    assert len(stored["files"]) == 19  # type: ignore[arg-type]
    assert stored["origin_detail"] == {"source": "url", "archive_sha256": listing.archive_sha256}


def test_candidate_listing_is_capped(zip_opens: list[str]) -> None:
    items: list[Item] = [
        ("file", f"pack/s{i:03d}/SKILL.md", skill_md(f"s{i:03d}")) for i in range(105)
    ]
    result = svc.zip_import_candidates(build_zip(items))
    assert len(result.candidates) == pkg.MAX_IMPORT_CANDIDATES == 100
    assert result.truncated == 5
    assert result.candidates[-1].path == "pack/s099/SKILL.md"
    assert len(zip_opens) == 100  # 요약하는 후보만 읽는다


def test_root_candidate_summary_follows_the_root_selection_rule() -> None:
    items: list[Item] = [
        ("file", "SKILL.md", skill_md("root")),
        ("file", "references/r.md", b"r"),
        ("file", "beta/SKILL.md", skill_md("gamma")),
        ("file", "beta/inner/SKILL.md", skill_md("inner")),
    ]
    by_path = _by_path(svc.zip_import_candidates(build_zip(items)))
    root = by_path["SKILL.md"]
    assert (root.name, root.file_count, root.ok) == ("root", 2, True)
    beta = by_path["beta/SKILL.md"]
    assert beta.errors == ["multiple_skills"]  # 상대 깊이 1 중첩
    assert "root_mismatch" in beta.warnings  # 폴더 beta ≠ name gamma
    assert by_path["beta/inner/SKILL.md"].ok is True


@pytest.mark.parametrize(
    ("extra", "overrides", "reason"),
    [
        ([("file", "t/tool/.private/n.md", b"n")], {}, "hidden_path"),
        ([("file", f"t/tool/r{i}.md", b"r") for i in range(3)], {"skill_import_max_files": 3},
         "too_many_files"),
        ([("file", "t/tool/big.bin", b"b" * 600)], {"skill_import_max_file_bytes": 500},
         "file_too_large"),
        ([("file", "t/tool/big.bin", b"b" * 400)], {"skill_import_max_total_bytes": 450},
         "package_too_large"),
    ],
    ids=["hidden", "count", "file-size", "total-size"],
)
def test_candidate_summary_uses_the_import_reason_codes(extra, overrides, reason: str) -> None:
    items: list[Item] = [("file", "t/tool/SKILL.md", skill_md("tool")), *extra]
    data = build_zip(items)
    use_settings(**overrides)
    candidate = _by_path(svc.zip_import_candidates(data))["t/tool/SKILL.md"]
    assert (candidate.ok, candidate.errors) == (False, [reason])
    with pytest.raises(SkillValidationError) as exc:  # 가져오기도 같은 사유다
        pkg.unpack_zip_detailed(data, selected_skill_path="t/tool/SKILL.md")
    assert exc.value.reason == reason


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"---\ndescription: d\n---\n", "missing_name"),
        (b"---\nname: a\nname: b\ndescription: d\n---\n", "yaml_duplicate_key"),
        (b"\xff\xfe not utf-8", "parse_error"),
    ],
    ids=["missing-name", "duplicate-key", "not-utf8"],
)
def test_candidate_summary_reports_frontmatter_rejections(body: bytes, reason: str) -> None:
    result = svc.zip_import_candidates(build_zip([("file", "x/SKILL.md", body)]))
    candidate = _by_path(result)["x/SKILL.md"]
    assert (candidate.ok, candidate.errors, candidate.name) == (False, [reason], None)


def test_oversized_skill_md_is_flagged_without_reading_it(zip_opens: list[str]) -> None:
    items: list[Item] = [
        ("file", "a/SKILL.md", skill_md("a") + b"x" * 600),
        ("file", "b/SKILL.md", skill_md("b")),
    ]
    data = build_zip(items)
    use_settings(skill_import_max_file_bytes=500)
    by_path = _by_path(svc.zip_import_candidates(data))
    assert (by_path["a/SKILL.md"].errors, by_path["a/SKILL.md"].name) == (["file_too_large"], None)
    assert zip_opens == ["b/SKILL.md"]


def test_summary_display_fields_are_capped() -> None:
    long = "d" * (std.MAX_DESCRIPTION_LENGTH + 50)
    body = f"---\nname: tool\ndescription: {long}\n---\n".encode()
    candidate = svc.zip_import_candidates(build_zip([("file", "tool/SKILL.md", body)]))
    assert len(candidate.candidates[0].description or "") == std.MAX_DESCRIPTION_LENGTH


@pytest.mark.parametrize(
    ("build", "overrides", "reason"),
    [
        (lambda: build_zip(github_layout()), "entries-1", "too_many_archive_entries"),
        (
            lambda: build_zip(
                github_layout(extra=[("file", f"{WRAPPER}tests/b.bin", b"z" * 2048)]),
                methods={f"{WRAPPER}tests/b.bin": zipfile.ZIP_BZIP2},
            ),
            {},
            "unsupported_compression",
        ),
        (
            lambda: build_zip(
                github_layout(extra=[("file", f"{WRAPPER}tests/z.bin", b"\0" * (1 << 20))])
            ),
            {},
            "bomb_suspected",
        ),
        (lambda: b"PK\x03\x04 not a zip", {}, "bad_zip"),
        (lambda: build_zip([("dir", "empty/", b"")]), {}, "empty_zip"),
        # 후보 SKILL.md를 모두 읽어도 합계는 압축 해제 합계 상한 안이다
        (lambda: build_zip(github_layout()), {"skill_import_max_total_bytes": 300},
         "package_too_large"),
    ],
    ids=["entries", "method", "ratio", "corrupt", "empty", "read-total"],
)
def test_candidate_listing_keeps_the_archive_gates(build, overrides, reason: str) -> None:
    data = build()
    if overrides == "entries-1":
        overrides = {"skill_import_max_archive_entries": _entry_count(data) - 1}
    use_settings(**overrides)
    with pytest.raises(SkillValidationError) as exc:
        svc.zip_import_candidates(data)
    assert exc.value.reason == reason


async def test_url_candidates_route_returns_the_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    from biznotes_core.api.v1 import skills as skills_api

    data = build_zip(github_layout())
    serve(monkeypatch, data)
    out = await skills_api.list_skill_url_candidates(
        body=SkillImportUrlCandidatesRequest(url=URL),
        user=object(),  # type: ignore[arg-type]
    )
    assert isinstance(out, SkillImportCandidatesOut)
    assert out.archive_sha256 == hashlib.sha256(data).hexdigest()
    assert [candidate.path for candidate in out.candidates] == ALL_CANDIDATES
    nested = next(candidate for candidate in out.candidates if candidate.path == NESTED)
    assert (nested.name, nested.file_count, nested.ok) == ("humanize-korean", 19, True)


async def test_url_candidates_route_maps_policy_errors_to_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from biznotes_core.api.v1 import skills as skills_api

    def blocked(url: str, **_kwargs: object) -> _Response:
        raise SSRFBlockedError("내부망 주소로의 요청이 차단되었습니다: example.test")

    monkeypatch.setattr(svc, "ssrf_safe_fetch", blocked)
    with pytest.raises(HTTPException) as exc:
        await skills_api.list_skill_url_candidates(
            body=SkillImportUrlCandidatesRequest(url=URL),
            user=object(),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 422
    assert str(exc.value.detail).startswith("[ssrf_blocked]")


async def test_zip_candidates_route_lists_nested_skills_despite_outside_symlink() -> None:
    """이전에는 서브트리 밖 심링크 하나로 후보 조회 전체가 422였다."""
    from biznotes_core.api.v1 import skills as skills_api

    data = build_zip(github_layout())
    out = await skills_api.list_skill_zip_candidates(
        file=UploadFile(file=io.BytesIO(data), filename="repo.zip"),
        user=object(),  # type: ignore[arg-type]
    )
    assert [candidate.path for candidate in out.candidates] == ALL_CANDIDATES
    assert out.archive_sha256 == hashlib.sha256(data).hexdigest()
    assert out.truncated == 0


def test_candidate_schema_additions_are_backward_compatible() -> None:
    out = SkillImportCandidatesOut(candidates=[SkillImportCandidateOut(path="a/SKILL.md")])
    assert out.model_dump() == {
        "candidates": [
            {
                "path": "a/SKILL.md",
                "name": None,
                "description": None,
                "file_count": 0,
                "ok": False,
                "errors": [],
                "warnings": [],
                "validation_profile": None,
            }
        ],
        "archive_sha256": None,
        "truncated": 0,
    }


def test_public_zip_candidates_function_is_unchanged() -> None:
    """공개 `skill_zip_candidates`는 손대지 않는다 — 깊이 1 목록·전체 읽기 관문 그대로."""
    data = build_zip(github_layout())
    with pytest.raises(SkillValidationError) as exc:
        pkg.skill_zip_candidates(data)
    assert exc.value.reason == "symlink"


# ---------- 후보 조회의 tar 확장 헤더(리뷰 H2) ----------


def _spread_pax() -> list[Item]:
    items: list[Item] = []
    for i in range(29):
        items += [PAX] * 104 + [("file", f"repo/tests/f{i}.txt", b"x")]
    return items + [PAX] * 3 + TOOL_FILES


@pytest.mark.parametrize(
    ("items", "reason"),
    [
        (_spread_pax(), "bad_tar_gz"),  # 리뷰 사례 1 — PAX 3000 + 일반 31
        ([PAX] * 400 + TOOL_FILES, "bad_tar_gz"),  # 사례 2 — 연속 400
        (  # 사례 3 — 밖의 파일 101개 뒤 연속 400
            [("file", f"repo/tests/f{i}.txt", b"x") for i in range(101)] + [PAX] * 400 + TOOL_FILES,
            "bad_tar_gz",
        ),
        ([("pax", "././@PaxHeader", pax_record("GNU.sparse.map", "a,b")), *TOOL_FILES],
         "bad_tar_gz"),
    ],
    ids=["spread-3000", "chain-400", "outside-101-then-chain", "bad-sparse-map"],
)
async def test_url_candidates_reject_hostile_tar_extension_headers(
    items: list[Item], reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, raw_tar_gz(items))
    with pytest.raises(SkillValidationError) as exc:
        await svc.url_import_candidates(URL)
    assert exc.value.reason == reason


def test_candidate_scan_counts_extension_headers_against_the_entry_cap() -> None:
    items: list[Item] = []
    for member in TOOL_FILES:
        items += [PAX, PAX, PAX, member]  # member 2개, 확장 헤더 6개
    data = raw_tar_gz(items)
    use_settings(skill_import_max_archive_entries=6)
    assert pkg.scan_import_archive(data).candidates == [TOOL]
    use_settings(skill_import_max_archive_entries=5)
    with pytest.raises(SkillValidationError) as exc:
        pkg.scan_import_archive(data)
    assert exc.value.reason == "too_many_archive_entries"


def test_candidate_scan_turns_parser_recursion_into_a_policy_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pkg, "_TAR_MAX_EXTENSION_RUN", 10**6)
    with pytest.raises(SkillValidationError) as exc:
        pkg.scan_import_archive(raw_tar_gz([PAX] * 1000 + TOOL_FILES))
    assert exc.value.reason == "bad_tar_gz"
