"""로컬 파일 스토리지 백엔드 — 모듈 함수가 실제 파일로 동작하고, 버킷 밖으로 나가지 못한다."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from biznotes_core.services import storage


def test_put_get_stat_delete_round_trip(local_storage) -> None:
    storage.put_object("sources", "s1/raw.pdf", b"%PDF-1.7 body")

    assert storage.get_object("sources", "s1/raw.pdf") == b"%PDF-1.7 body"
    assert storage.stat_object_size("sources", "s1/raw.pdf") == 13
    assert (local_storage.root / "sources" / "s1" / "raw.pdf").read_bytes() == b"%PDF-1.7 body"

    storage.delete_object("sources", "s1/raw.pdf")
    with pytest.raises(storage.ObjectNotFound):
        storage.get_object("sources", "s1/raw.pdf")
    # 빈 디렉터리는 버킷 루트까지 걷어낸다 — 버킷 디렉터리는 남는다
    assert not (local_storage.root / "sources" / "s1").exists()
    assert (local_storage.root / "sources").is_dir()


def test_put_overwrites_atomically_and_leaves_no_temp_files(local_storage) -> None:
    storage.put_object("parsed", "s/parsed.md", b"old")
    storage.put_object("parsed", "s/parsed.md", b"new body")

    assert storage.get_object("parsed", "s/parsed.md") == b"new body"
    assert list((local_storage.root / ".tmp").iterdir()) == []


def test_missing_object_is_object_not_found_with_s3_code(local_storage) -> None:
    for call in (
        lambda: storage.get_object("parsed", "nope/parsed.md"),
        lambda: storage.stat_object_size("parsed", "nope/parsed.md"),
        lambda: storage.open_object_stream("parsed", "nope/parsed.md"),
        lambda: storage.copy_object("parsed", "nope/a", "nope/b"),
    ):
        with pytest.raises(storage.ObjectNotFound) as exc:
            call()
        # 인제스트는 S3 오류 코드로 「없음」을 가린다(_MISSING_OBJECT_CODES)
        assert exc.value.code == "NoSuchKey"
        assert isinstance(exc.value, FileNotFoundError)


def test_missing_object_under_a_file_component_is_not_found(local_storage) -> None:
    storage.put_object("parsed", "s/parsed.md", b"x")
    with pytest.raises(storage.ObjectNotFound):
        storage.get_object("parsed", "s/parsed.md/child")


def test_delete_of_missing_key_is_not_an_error(local_storage) -> None:
    storage.delete_object("artifacts", "없는/키")
    assert storage.delete_objects("artifacts", ["없는/키", "또/없음"]) == []


def test_stream_reads_in_chunks_reports_length_and_closes(local_storage) -> None:
    body = b"x" * 10_000
    storage.put_object("artifacts", "run/out.bin", body)

    stream = storage.open_object_stream("artifacts", "run/out.bin", chunk_size=4096)
    assert stream.length == 10_000
    chunks = list(stream.chunks())
    assert b"".join(chunks) == body
    assert [len(c) for c in chunks] == [4096, 4096, 1808]
    stream.close()  # 멱등


def test_list_uses_string_prefix_semantics_like_s3(local_storage) -> None:
    for key in ("skills/a/draft/SKILL.md", "skills/a/draft/scripts/x.py", "skills/ab/v1/f", "z"):
        storage.put_object("skill-packages", key, b"1")

    assert storage.list_objects("skill-packages", "skills/a/") == [
        "skills/a/draft/SKILL.md",
        "skills/a/draft/scripts/x.py",
    ]
    # 마지막 성분이 미완인 접두사도 S3처럼 문자열로 맞춘다
    assert storage.list_objects("skill-packages", "skills/a") == [
        "skills/a/draft/SKILL.md",
        "skills/a/draft/scripts/x.py",
        "skills/ab/v1/f",
    ]
    assert storage.list_objects("skill-packages", "") == [
        "skills/a/draft/SKILL.md",
        "skills/a/draft/scripts/x.py",
        "skills/ab/v1/f",
        "z",
    ]
    assert storage.list_objects("skill-packages", "없음/") == []
    assert storage.list_objects("no-such-bucket", "") == []


def test_list_object_times_returns_utc_mtime(local_storage) -> None:
    storage.put_object("skill-packages", "p/f", b"1")
    path = local_storage.root / "skill-packages" / "p" / "f"
    os.utime(path, (1_700_000_000, 1_700_000_000))

    assert storage.list_object_times("skill-packages", "p/") == [
        ("p/f", datetime.fromtimestamp(1_700_000_000, UTC))
    ]


def test_copy_and_delete_prefix(local_storage) -> None:
    storage.put_object("skill-packages", "d/SKILL.md", b"body")
    storage.copy_object("skill-packages", "d/SKILL.md", "v/1/SKILL.md")

    assert storage.get_object("skill-packages", "v/1/SKILL.md") == b"body"
    assert storage.delete_prefix("skill-packages", "d/") == []
    assert storage.list_objects("skill-packages", "") == ["v/1/SKILL.md"]


def test_ensure_buckets_creates_bucket_directories(local_storage) -> None:
    storage.ensure_buckets()
    names = sorted(p.name for p in local_storage.root.iterdir())
    assert names == ["artifacts", "parsed", "skill-packages", "sources"]


@pytest.mark.parametrize(
    "key",
    [
        "",
        "../escape",
        "a/../../escape",
        "a/./b",
        "/abs/path",
        "a//b",
        "a/",
        "a\\..\\b",
        "nul\x00byte",
        "..",
    ],
)
def test_unsafe_keys_are_rejected_before_touching_disk(local_storage, key: str) -> None:
    with pytest.raises(storage.InvalidObjectKey):
        storage.put_object("artifacts", key, b"x")
    with pytest.raises(storage.InvalidObjectKey):
        storage.get_object("artifacts", key)
    with pytest.raises(storage.InvalidObjectKey):
        storage.delete_object("artifacts", key)
    # 지우지 못한 키로 돌려준다(일괄 삭제는 예외 대신 결과)
    assert storage.delete_objects("artifacts", [key]) == [key]
    assert not (local_storage.root.parent / "escape").exists()


@pytest.mark.parametrize("bucket", ["", "..", "../x", "a/b", ".tmp", "UPPER", "a" * 64])
def test_unsafe_bucket_names_are_rejected(local_storage, bucket: str) -> None:
    with pytest.raises(storage.InvalidObjectKey):
        storage.put_object(bucket, "k", b"x")


@pytest.mark.parametrize("prefix", ["../", "a/../", "/abs", "a//b/"])
def test_unsafe_prefixes_are_rejected(local_storage, prefix: str) -> None:
    with pytest.raises(storage.InvalidObjectKey):
        storage.list_objects("artifacts", prefix)


def test_symlink_inside_the_volume_cannot_leak_files_outside(
    local_storage, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("다른 사용자의 파일")
    bucket_dir = local_storage.root / "artifacts"
    bucket_dir.mkdir(parents=True)
    (bucket_dir / "link").symlink_to(tmp_path)

    with pytest.raises(storage.InvalidObjectKey):
        storage.get_object("artifacts", "link/secret.txt")


def test_default_backend_is_local_under_data_dir(monkeypatch, tmp_path: Path) -> None:
    from biznotes_core.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path / "vol"))
    monkeypatch.setattr(get_settings(), "storage_backend", "local")
    storage.get_backend.cache_clear()
    try:
        backend = storage.get_backend()
        assert isinstance(backend, storage.LocalStorage)
        assert backend.root == (tmp_path / "vol").resolve()
    finally:
        storage.get_backend.cache_clear()
