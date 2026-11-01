"""Phase 5 — evaluation runner와 URL/tar.gz 가져오기 경계."""

import hashlib
import io
import tarfile

import pytest

from biznotes_core.services.skills import package as pkg
from biznotes_core.services.skills import service as svc
from biznotes_core.services.skills.validator import SkillValidationError

SKILL_MD = b"---\nname: release-notes\ndescription: Create release notes.\n---\n# Notes\n"


def make_tar_gz(entries: dict[str, bytes], *, member_type: bytes | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for name, data in entries.items():
            member = tarfile.TarInfo(name)
            member.type = member_type or tarfile.REGTYPE
            member.size = len(data) if member.type == tarfile.REGTYPE else 0
            archive.addfile(member, io.BytesIO(data) if member.type == tarfile.REGTYPE else None)
    return buf.getvalue()


def test_tar_gz_uses_the_same_root_and_path_rules_as_zip() -> None:
    unpacked = pkg.unpack_import_archive_detailed(
        make_tar_gz(
            {
                "release-notes/SKILL.md": SKILL_MD,
                "release-notes/references/template.md": b"template",
            }
        )
    )
    assert unpacked.root_name == "release-notes"
    assert unpacked.files == {"SKILL.md": SKILL_MD, "references/template.md": b"template"}


@pytest.mark.parametrize(
    "member_type",
    [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE],
)
def test_tar_gz_rejects_links_and_device_members(member_type: bytes) -> None:
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_tar_gz_detailed(
            make_tar_gz({"release-notes/SKILL.md": SKILL_MD}, member_type=member_type)
        )
    assert exc.value.reason == "unsafe_tar_member"


def test_tar_gz_rejects_traversal_directory_member() -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        directory = tarfile.TarInfo("../outside/")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        skill = tarfile.TarInfo("SKILL.md")
        skill.size = len(SKILL_MD)
        archive.addfile(skill, io.BytesIO(SKILL_MD))

    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_tar_gz_detailed(buf.getvalue())
    assert exc.value.reason == "path_traversal"


def test_tar_gz_rejects_absolute_directory_member() -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        directory = tarfile.TarInfo("/outside/")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        skill = tarfile.TarInfo("SKILL.md")
        skill.size = len(SKILL_MD)
        archive.addfile(skill, io.BytesIO(SKILL_MD))

    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_tar_gz_detailed(buf.getvalue())
    assert exc.value.reason == "absolute_path"


def test_tar_gz_enforces_member_and_total_size_before_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Settings:
        skill_import_max_files = 2
        skill_import_max_file_bytes = 4
        skill_import_max_total_bytes = 5
        skill_import_max_archive_entries = 2000

    monkeypatch.setattr(pkg, "get_settings", lambda: Settings())
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_tar_gz_detailed(make_tar_gz({"SKILL.md": b"x" * 5}))
    assert exc.value.reason == "file_too_large"

    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_tar_gz_detailed(make_tar_gz({"SKILL.md": b"abc", "x.txt": b"def"}))
    assert exc.value.reason == "package_too_large"


def test_remote_archive_detector_keeps_zip_support() -> None:
    data = pkg.export_zip({"SKILL.md": SKILL_MD}, "release-notes")
    assert pkg.unpack_import_archive_detailed(data).files == {"SKILL.md": SKILL_MD}


def test_remote_fetch_uses_ssrf_gate_and_upload_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict]] = []

    class Settings:
        ssrf_allow_private = False
        skill_import_max_upload_bytes = 123

    class Response:
        status_code = 200
        content = b"archive"

    def fetch(url: str, **kwargs: object) -> Response:
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(svc, "get_settings", lambda: Settings())
    monkeypatch.setattr(svc, "ssrf_safe_fetch", fetch)

    assert svc._fetch_import_archive("https://example.test/skill.tgz") == b"archive"
    assert calls == [
        (
            "https://example.test/skill.tgz",
            {"allow_private": False, "max_bytes": 123},
        )
    ]


async def test_url_import_rejects_digest_mismatch_before_db_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(svc, "_fetch_import_archive", lambda _: b"archive")
    with pytest.raises(SkillValidationError) as exc:
        await svc.import_url_detailed(
            object(), object(), "https://example.test/skill.zip", sha256="0" * 64
        )
    assert exc.value.reason == "digest_mismatch"


async def test_url_import_unpacks_tar_and_does_not_persist_signed_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = make_tar_gz({"release-notes/SKILL.md": SKILL_MD})
    stored: dict[str, object] = {}

    async def persist(
        _db: object,
        _owner: object,
        unpacked: pkg.UnpackedPackage,
        *,
        origin_detail: dict[str, object],
    ) -> object:
        stored["files"] = unpacked.files
        stored["origin_detail"] = origin_detail
        return object()

    monkeypatch.setattr(svc, "_fetch_import_archive", lambda _: archive)
    monkeypatch.setattr(svc, "_import_unpacked", persist)
    signed_url = "https://example.test/skill.tgz?token=not-for-storage"

    result = await svc.import_url_detailed(
        object(), object(), signed_url, sha256=hashlib.sha256(archive).hexdigest()
    )

    assert result is not None
    assert stored["files"] == {"SKILL.md": SKILL_MD}
    assert stored["origin_detail"] == {
        "source": "url",
        "archive_sha256": hashlib.sha256(archive).hexdigest(),
    }
