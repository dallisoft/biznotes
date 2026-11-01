"""#119 — 스킬 archive(ZIP·tar.gz) 전개량 제한.

`_read_zip_entries`가 항목마다 `zf.read(info)`(= `read(-1)`)를 쓰면 신고 크기와 무관하게
끝까지 풀어 API 프로세스 메모리를 소모했다. tar.gz 경로도 `tarfile._Stream._read`가 압축
청크를 상한 없이 풀어 같은 결함을 가졌다.

테스트는 본문을 mock으로 대체하지 않는다(AGENTS.md §8). 실제 zlib에 위임하는 래퍼로
`max_length`와 실제 출력을 관측해 "풀리는 양이 제한된다"를 고정한다. 피크 RSS는 CI에서
흔들리므로 단위 테스트가 아니라 계획 세션의 실측으로 확인한다.
"""

import gzip
import io
import json
import math
import random
import struct
import tarfile
import zipfile
import zlib
from zlib import crc32

import pytest
from pydantic import Field

from biznotes_core.config import CoreSettings, reset_settings
from biznotes_core.services.skills import package as pkg
from biznotes_core.services.skills.validator import SkillValidationError

GOOD = b"---\nname: sales-summary\ndescription: Summarize sales call notes.\n---\n\n# S\n"


# ---------- fixture 도우미 ----------


def make_zip(
    entries: dict[str, bytes] | list[tuple[str, bytes]],
    method: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    """순서와 중복을 보존해 만든다 — dict로 넘기면 중복 경로 fixture를 표현할 수 없다."""
    items = list(entries.items()) if isinstance(entries, dict) else entries
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in items:
            zf.writestr(name, body, compress_type=method)
    return buf.getvalue()


def forge_central_directory(
    raw: bytes, *, size: int | None = None, crc: int | None = None,
    method: int | None = None, flag: int | None = None, version: int | None = None,
) -> bytes:
    """마지막 중앙 디렉터리 레코드의 필드를 위조한다 (신고 값만 조작)."""
    data = bytearray(raw)
    cd = data.rfind(b"PK\x01\x02")
    if version is not None:
        struct.pack_into("<H", data, cd + 6, version)  # extract_version
    if method is not None:
        struct.pack_into("<H", data, cd + 10, method)  # compression_method
    if crc is not None:
        struct.pack_into("<I", data, cd + 16, crc)  # crc-32
    if size is not None:
        struct.pack_into("<I", data, cd + 24, size)  # uncompressed size
    if flag is not None:
        struct.pack_into("<H", data, cd + 8, flag)  # general purpose flag
    return bytes(data)


def make_tar(entries: list[tuple[str, bytes]], fmt: int = tarfile.USTAR_FORMAT) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=fmt) as tf:
        for name, body in entries:
            member = tarfile.TarInfo(name)
            member.size = len(body)
            tf.addfile(member, io.BytesIO(body))
    return buf.getvalue()


def make_tar_gz(entries: list[tuple[str, bytes]], fmt: int = tarfile.USTAR_FORMAT) -> bytes:
    return gzip.compress(make_tar(entries, fmt), mtime=0)


def use_settings(**overrides: int) -> None:
    """제품의 Settings 서브클래스처럼 기본값만 바꿔 등록한다.

    pydantic은 베이스 필드를 재정의할 때 타입 주석을 요구하므로 `Field`로 선언한다.
    """
    namespace: dict[str, object] = {"__annotations__": {name: int for name in overrides}}
    namespace.update({name: Field(default=value) for name, value in overrides.items()})
    reset_settings(type("Settings", (CoreSettings,), namespace))


@pytest.fixture(autouse=True)
def _restore_settings():
    yield
    reset_settings(CoreSettings)


class InflateSpy:
    """`zlib.decompressobj`에 위임하며 호출 `(max_length, 실제 출력)`을 기록한다.

    `zipfile`은 모듈 수준 `import zlib` + `zlib.decompressobj(-15)`, `tarfile`은 `_Stream`
    안에서 지역 import 후 `self.zlib.decompressobj(...)`를 쓴다. 공유 factory 하나를
    감싸면 양쪽을 모두 관측할 수 있다.
    """

    def __init__(self, factory) -> None:
        self._factory = factory
        self.calls: list[tuple[int | None, int]] = []
        self.feeds: list[int] = []
        self.flushes: list[int] = []

    def output_total(self) -> list[int]:
        return [size for _, size in self.calls]

    def __call__(self, *args, **kwargs):
        spy = self

        class _Wrapper:
            def __init__(self, real) -> None:
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def decompress(self, data, max_length=0):
                out = self._real.decompress(data, max_length)
                spy.calls.append((max_length, len(out)))
                spy.feeds.append(len(data))
                return out

            def flush(self, *args, **kwargs):
                out = self._real.flush(*args, **kwargs)
                spy.flushes.append(len(out))
                return out

        return _Wrapper(self._factory(*args, **kwargs))


@pytest.fixture
def inflate_spy(monkeypatch: pytest.MonkeyPatch) -> InflateSpy:
    spy = InflateSpy(zlib.decompressobj)
    monkeypatch.setattr(zlib, "decompressobj", spy)
    return spy


# ---------- ZIP: 신고 크기 축소 ----------


def test_zip_declared_size_smaller_than_actual_is_bounded(inflate_spy: InflateSpy):
    """신고 크기를 축소한 항목이 정책 오류로 거부되고, 전개량이 요청 크기로 묶인다."""
    bomb = forge_central_directory(
        make_zip({"SKILL.md": b"\0" * (2 * 1024 * 1024)}), size=1024
    )
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip(bomb)
    assert exc.value.reason == "bad_zip"
    # `zf.read(info)`는 `read(-1)`이라 `MAX_N`(2GiB-1)을 넘긴다. 그 호출이 없어야 한다.
    assert inflate_spy.calls, "DEFLATED 항목이 실제로 해제되었는지 관측이 안 됐다"
    assert all(limit != 2147483647 for limit, _ in inflate_spy.calls)
    assert all(limit > 0 for limit, _ in inflate_spy.calls)
    # CPython 최소 청크(MIN_READ_SIZE=4096) 이내로 묶인다.
    assert max(size for _, size in inflate_spy.calls) <= 4096
    assert not inflate_spy.flushes


def test_zip_forged_size_and_crc_are_bounded(inflate_spy: InflateSpy):
    """크기·CRC를 **둘 다** 위조해도 전개량은 제한된다.

    잘린 내용이 그대로 반환될 수 있지만 메모리 우회는 아니다. 이 한계는 계획에 기록된
    의도된 선택이다 — 원본 전체 무결성 증명을 목표로 하지 않는다.
    """
    payload = b"\0" * (2 * 1024 * 1024)
    forged = forge_central_directory(
        make_zip({"SKILL.md": payload}), size=1024, crc=crc32(b"\0" * 1024)
    )
    unpacked = pkg.unpack_zip(forged)
    # 잘린 내용이 반환된다 — 이 사실도 함께 고정한다.
    assert len(unpacked["SKILL.md"]) == 1024
    assert max(size for _, size in inflate_spy.calls) <= 4096
    assert all(limit != 2147483647 for limit, _ in inflate_spy.calls)
    assert not inflate_spy.flushes


def test_zip_over_declared_size_is_rejected():
    """내용 1000B를 1100B로 신고한 항목은 길이 검사로 거부된다(과대 신고 축소)."""
    forged = forge_central_directory(make_zip({"SKILL.md": b"x" * 1000}), size=1100)
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip(forged)
    assert exc.value.reason == "bad_zip"


# ---------- ZIP: 압축 방식 allowlist ----------


@pytest.mark.parametrize(
    "method",
    [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA, zipfile.ZIP_STORED, 99],
    ids=["bzip2", "lzma", "stored-is-allowed", "unknown-99"],
)
def test_zip_allowed_methods_are_read_and_others_are_not(method, monkeypatch):
    """허용 방식은 실제로 읽히고, 그 밖은 `zf.open`이 **호출되기 전에** 거부된다.

    BZIP2·LZMA·ZSTANDARD는 `read(n)`도 압축 청크를 통째로 푼다(#112와 같은 근거).
    정상 항목 뒤에 대상 항목을 둔다 — 앞 항목은 실제로 읽히고 대상 항목만 0회여야 한다.
    """
    writeable = method in (zipfile.ZIP_STORED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", GOOD, compress_type=zipfile.ZIP_DEFLATED)
        if writeable:
            zf.writestr("other.bin", b"x" * 100, compress_type=method)
        else:  # zipfile이 쓸 수 없는 방식 — STORED로 쓴 뒤 신고값만 바꾼다
            zf.writestr("other.bin", b"x" * 100, compress_type=zipfile.ZIP_STORED)
    data = bytearray(buf.getvalue())
    if not writeable:
        cd = data.rfind(b"PK\x01\x02", 0, data.rfind(b"other.bin"))
        struct.pack_into("<H", data, cd + 10, method)
        lh = data.rfind(b"PK\x03\x04", 0, data.rfind(b"other.bin"))
        struct.pack_into("<H", data, lh + 8, method)

    # fixture를 **만든 뒤** spy를 설치하고, 항목 이름을 직접 비교한다.
    opened: list[str] = []
    real_open = zipfile.ZipFile.open

    def spy_open(self, name, *args, **kwargs):
        target = name if isinstance(name, str) else getattr(name, "filename", str(name))
        opened.append(target)
        return real_open(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", spy_open)
    if method == zipfile.ZIP_STORED:
        assert set(pkg.unpack_zip(bytes(data))) == {"SKILL.md", "other.bin"}
        assert sorted(opened) == ["SKILL.md", "other.bin"]
    else:
        with pytest.raises(SkillValidationError) as exc:
            pkg.unpack_zip(bytes(data))
        assert exc.value.reason == "unsupported_compression"
        assert "other.bin" not in opened, "미지원 항목은 한 번도 열리면 안 된다"
        assert opened == ["SKILL.md"], "정상 항목까지 열리지 않았다"


# ---------- ZIP: 압축률 정책 ----------


def _repeated_json(rows: int = 20000) -> bytes:
    row = json.dumps({"name": "sample", "enabled": True})
    body = ("[" + ",".join([row] * rows) + "]").encode()
    json.loads(body.decode())  # 정상 JSON임을 확인한다
    return body


def test_zip_repeated_json_within_ratio_cap_is_accepted():
    """비율 334.9의 정상 반복 JSON은 기본 상한 1000 안에서 통과한다."""
    files = {"SKILL.md": GOOD, "references/data.json": _repeated_json()}
    data = pkg.export_zip(files, "sales-summary")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        ratio = zf.getinfo("sales-summary/references/data.json").file_size / max(
            zf.getinfo("sales-summary/references/data.json").compress_size, 1
        )
    assert 300 < ratio < 1000
    assert set(pkg.unpack_zip(data)) == {"SKILL.md", "references/data.json"}


def test_zip_compression_ratio_cap_rejects_near_zero_file():
    """1MiB 근-전영 0바이트 파일(1015배)은 **의도적으로** 거부된다."""
    files = {"SKILL.md": GOOD, "references/empty.bin": b"\0" * (1024 * 1024)}
    data = pkg.export_zip(files, "sales-summary")
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip(data)
    assert exc.value.reason == "bomb_suspected"


def test_zip_compression_ratio_cap_is_configurable():
    """상한이 같으면 통과하고 초과하면 거부된다 — 설정이 실제로 쓰인다."""
    data = pkg.export_zip(
        {"SKILL.md": GOOD, "references/data.json": _repeated_json()}, "sales-summary"
    )
    use_settings(skill_import_max_compression_ratio=1000)
    assert pkg.unpack_zip(data)
    use_settings(skill_import_max_compression_ratio=100)
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip(data)
    assert exc.value.reason == "bomb_suspected"


def test_zip_empty_entry_passes_the_ratio_check():
    """빈 항목은 `0/1 = 0`이라 상한을 넘지 않는다."""
    data = make_zip({"SKILL.md": GOOD, "references/empty.md": b""})
    assert set(pkg.unpack_zip(data)) == {"SKILL.md", "references/empty.md"}


def test_zip_ratio_exactly_at_the_cap_is_accepted():
    """비율이 상한과 **정확히 같으면** 통과한다 — `>` 비교다."""
    # 반복 데이터로 알려진 비율을 만들고, 그 비율을 상한으로 둔다.
    payload = _repeated_json()
    data = pkg.export_zip({"SKILL.md": GOOD, "references/d.json": payload}, "sales-summary")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        info = zf.getinfo("sales-summary/references/d.json")
        ratio = info.file_size / max(info.compress_size, 1)
    use_settings(skill_import_max_compression_ratio=math.ceil(ratio))
    assert set(pkg.unpack_zip(data)) == {"SKILL.md", "references/d.json"}
    # 한 단계 더 엄격하게(상한이 실제 비율보다 작게) 내리면 거부된다
    use_settings(skill_import_max_compression_ratio=math.floor(ratio))
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip(data)
    assert exc.value.reason == "bomb_suspected"


@pytest.mark.parametrize(
    "method",
    [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED],
    ids=["stored", "deflated"],
)
def test_zip_encrypted_entry_is_a_policy_error(method):
    """암호화 항목은 허용 방식과 무관하게 정책 오류다.

    `ZipFile.open`이 비밀번호를 요구해 `RuntimeError`를 던진다. 이를 catch에서 빼면
    500으로 새므로 방식 allowlist와 별개로 고정한다(1차 리뷰 B1).
    """
    data = forge_central_directory(
        make_zip({"SKILL.md": GOOD}, method), flag=0x1
    )
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip(data)
    assert exc.value.reason == "bad_zip"


def test_zip_truncated_deflate_body_is_a_policy_error():
    """중앙 디렉터리는 유효한데 deflate 본문이 잘린 ZIP도 정책 오류다.

    archive 자체를 자르는 fixture와 달리 CD는 온전하므로 `ZipExtFile`이 실제로
    해제를 시도하다 에러가 난다.
    """
    data = bytearray(make_zip({"SKILL.md": GOOD * 20}))
    lh = data.find(b"PK\x03\x04")
    # 실제 압축 데이터는 그대로 두고 중앙 디렉터리의 압축 크기만 줄인다 → CD는 유효하지만
    # `ZipExtFile`이 압축 입력을 다 읽기 전에 EOF를 만난다.
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as zf:
        info = zf.getinfo("SKILL.md")
        real_compressed = info.compress_size
    cd = data.rfind(b"PK\x01\x02")
    struct.pack_into("<I", data, cd + 20, real_compressed - 20)  # CD 압축 크기 축소
    struct.pack_into("<I", data, lh + 18, real_compressed - 20)  # local 헤더도 맞춰준다
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip(bytes(data))
    assert exc.value.reason == "bad_zip"


# ---------- ZIP: 예외 경계 ----------


@pytest.mark.parametrize(
    ("label", "build", "reason"),
    [
        ("손상 archive", lambda: b"PK\x03\x04" + b"\0" * 40, "bad_zip"),
        (
            "암호화 항목",
            lambda: forge_central_directory(make_zip({"SKILL.md": GOOD}), flag=0x1),
            "bad_zip",
        ),
        (
            "extract_version 25.5",
            lambda: forge_central_directory(
                make_zip({"SKILL.md": GOOD}), version=255
            ),
            "bad_zip",
        ),
        (
            "잘린 archive",
            lambda: make_zip({"SKILL.md": GOOD})[:20],
            "bad_zip",
        ),
        (
            "UTF-8 플래그 + 비ASCII 이름",
            lambda: _invalid_utf8_name(),
            "bad_zip",
        ),
    ],
)
def test_zip_broken_inputs_are_policy_errors_not_crashes(label, build, reason):
    """생성·해제 오류가 500으로 새지 않고 정책 오류가 된다."""
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip(build())
    assert exc.value.reason == reason


def _invalid_utf8_name() -> bytes:
    """CD에 UTF-8 플래그가 서 있는데 이름 바이트가 유효 UTF-8이 아닌 ZIP.

    `zipfile._RealGetContents`는 플래그를 보고 `filename.decode('utf-8')`를 호출하므로
    `UnicodeDecodeError`가 난다. 이는 `ValueError` 하위 타입이라 넓은 catch에 없으면 500으로
    샌다(1차 리뷰 M1).
    """
    raw = bytearray(make_zip({"SKILL.md": GOOD}))
    cd = raw.rfind(b"PK\x01\x02")
    struct.pack_into("<H", raw, cd + 8, 0x800)  # UTF-8 플래그
    raw[cd + 46] = 0xFF  # CD 이름 첫 바이트 (오프셋 46 = 파일 이름 필드 시작)
    return bytes(raw)


@pytest.mark.parametrize(
    ("label", "build", "reason"),
    [
        ("symlink", lambda: _symlink_zip(), "symlink"),
        (
            "path_collision",
            lambda: make_zip([("SKILL.md", GOOD), ("SKILL.md", GOOD.replace(b"sales", b"shadow"))]),
            "path_collision",
        ),
        ("absolute_path", lambda: make_zip({"SKILL.md": GOOD, "/etc/x": b"x"}), "absolute_path"),
        (
            "unsupported_compression",
            lambda: forge_central_directory(
                make_zip({"SKILL.md": GOOD}, zipfile.ZIP_BZIP2),
                method=zipfile.ZIP_BZIP2,
            ),
            "unsupported_compression",
        ),
    ],
)
def test_zip_policy_reasons_survive_the_wide_catch(label, build, reason):
    """넓은 `_ZIP_READ_ERRORS`가 기존·신규 정책 사유를 `bad_zip`으로 덮지 않는다.

    `SkillValidationError`가 `ValueError` 하위 타입이라 순서가 틀리면 모든 사유가
    `bad_zip`으로 뭉개진다.
    """
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip(build())
    assert exc.value.reason == reason


def _symlink_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", GOOD)
        info = zipfile.ZipInfo("link.md")
        info.external_attr = (0o120777 << 16) | 0o120000
        zf.writestr(info, b"../../etc/passwd")
    return buf.getvalue()


@pytest.mark.parametrize(
    ("label", "overrides", "entries", "reason"),
    [
        ("too_many_files", {"skill_import_max_files": 1}, {"a.md": b"x"}, "too_many_files"),
        (
            "file_too_large",
            {"skill_import_max_file_bytes": 1024},
            {"SKILL.md": b"x" * 5000},
            "file_too_large",
        ),
        (
            "package_too_large",
            {"skill_import_max_total_bytes": 100},
            {"SKILL.md": b"x" * 80, "a.md": b"y" * 80},
            "package_too_large",
        ),
    ],
)
def test_zip_size_policy_reasons_survive(label, overrides, entries, reason):
    use_settings(**overrides)
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_zip(make_zip({"SKILL.md": GOOD, **entries}))
    assert exc.value.reason == reason


# ---------- ZIP: 정상 통과 ----------


def test_zip_export_roundtrip_and_stored_entries_still_pass():
    files = {"SKILL.md": GOOD, "references/g.md": "# 가이드\n본문\n".encode()}
    data = pkg.export_zip(files, "sales-summary")
    unpacked = pkg.unpack_zip_detailed(data)
    assert unpacked.files == files
    assert unpacked.root_name == "sales-summary"
    # STORED(무압축) 항목도 허용 목록 안이다.
    assert set(pkg.unpack_zip(make_zip({"SKILL.md": GOOD}, zipfile.ZIP_STORED))) == {"SKILL.md"}


# ---------- tar.gz ----------


def test_tar_gz_hidden_payload_is_rejected_within_budget(inflate_spy: InflateSpy):
    """신고 1KiB 뒤 64MiB를 숨긴 tar.gz는 `package_too_large`로 거부된다.

    `tarfile._Stream._read`는 압축 청크를 상한 없이 풀어 65KiB 입력이 피크 RSS를
    +40MiB 밀었다. drain이 남은 gzip을 같은 예산으로 소진해 이걸 막는다.
    """
    hidden = gzip.compress(
        make_tar([("SKILL.md", b"A" * 1024)]) + b"\0" * (64 * 1024 * 1024) + b"\0" * 1024,
        mtime=0,
    )
    assert len(hidden) < 100 * 1024  # 업로드 한도 안의 작은 입력
    with pytest.raises(SkillValidationError) as exc:
        pkg._read_tar_gz_entries(hidden)
    assert exc.value.reason == "package_too_large"
    # 모든 inflate 호출이 양수 상한 안에 있다 — `decompress(data, 0)`은 무제한이다.
    assert inflate_spy.calls
    assert all(limit is not None and limit > 0 for limit, _ in inflate_spy.calls)
    assert all(size <= (limit or 0) for limit, size in inflate_spy.calls)
    assert not inflate_spy.flushes, "무제한 flush를 불러서는 안 된다"
    # 입력은 한 번에 고정 청크만 공급한다 — 캐리어까지 합쳐도 그 안에 든다.
    assert inflate_spy.feeds, "해제가 관측되지 않았다"
    assert max(inflate_spy.feeds) <= pkg._GZIP_FEED_BYTES
    # 누적 출력은 예산 + 센티널 1바이트를 넘지 않는다.
    assert sum(inflate_spy.output_total()) <= pkg._tar_gz_budget(CoreSettings()) + 1


def test_tar_gz_read_does_not_decompress_ahead():
    """`read(n)`이 요청량보다 많이 전개하지 않는다 — 무제한 read-ahead 방지."""
    reader = pkg._BoundedGzipReader(gzip.compress(b"Z" * 100000, mtime=0), 200000)
    assert len(reader.read(1)) == 1
    assert len(reader._out) == 0, "요청 1바이트에 64KiB를 전개했다"


def test_tar_gz_payload_over_limit_is_rejected():
    """payload 합계가 `skill_import_max_total_bytes`를 넘으면 거부된다."""
    use_settings(skill_import_max_total_bytes=100)
    entries = [(f"references/{i}", b"A" * 80) for i in range(4)]
    with pytest.raises(SkillValidationError) as exc:
        pkg._read_tar_gz_entries(make_tar_gz(entries))
    assert exc.value.reason == "package_too_large"


def test_tar_gz_trailer_is_verified():
    """gzip 트레일러 CRC32·ISIZE를 검증한다 — 현행 raw deflate 경로는 검증하지 않았다."""
    data = bytearray(make_tar_gz([("SKILL.md", GOOD)]))
    crc_broken = bytearray(data)
    crc_broken[-8] ^= 0xFF  # CRC32 첫 바이트
    size_broken = bytearray(data)
    size_broken[-4] ^= 0xFF  # ISIZE 첫 바이트
    truncated = bytes(data[:-8])
    for label, payload in (
        ("CRC32", bytes(crc_broken)),
        ("ISIZE", bytes(size_broken)),
        ("잘린 트레일러", truncated),
    ):
        with pytest.raises(SkillValidationError) as exc:
            pkg._read_tar_gz_entries(payload)
        assert exc.value.reason == "bad_tar_gz", label


@pytest.mark.parametrize("feed", [1, 7, 64 * 1024], ids=["1B", "7B", "64KiB"])
def test_tar_gz_trailing_nul_padding_is_accepted(feed, monkeypatch):
    """정상 gzip 뒤 NUL 패딩은 허용한다(Python gzip과 동일).

    판정이 **입력 청크 경계에 의존하지 않아야** 한다 — "전부 NUL인지" 검사만 두면
    멤버+NUL+다음 멤버가 작은 청크에서 성공하고 큰 청크에서 실패한다. 나눠 압축한 모든
    조각에 파일이 있는 입력을 쓴다(구현 리뷰 R1).
    """
    monkeypatch.setattr(pkg, "_GZIP_FEED_BYTES", feed)
    entries = [
        ("SKILL.md", b"A" * 400),
        ("references/data.bin", b"B" * 400),
        ("references/notes.md", b"C" * 400),
    ]
    whole = make_tar(entries)
    single = gzip.compress(whole, mtime=0)
    expected = pkg._read_tar_gz_entries(single)
    assert len(expected) == 3
    assert pkg._read_tar_gz_entries(single + b"\0" * 512) == expected
    assert pkg._read_tar_gz_entries(single + b"\0" * 70000) == expected
    # 멤버 사이 NUL — 두 조각 모두에 파일이 있다
    multi = gzip.compress(whole[:1024], mtime=0) + b"\0" * 8 + gzip.compress(whole[1024:], mtime=0)
    assert pkg._read_tar_gz_entries(multi) == expected


def test_tar_gz_multimember_is_restored():
    """멀티 멤버 gzip을 전부 복원한다 — 현행 raw deflate 경로는 뒤 멤버를 유실했다."""
    base = make_tar([("SKILL.md", GOOD)])
    single = gzip.compress(base, mtime=0)
    multi = gzip.compress(base[:1024], mtime=0) + gzip.compress(base[1024:], mtime=0)
    assert pkg._read_tar_gz_entries(multi) == pkg._read_tar_gz_entries(single)


def test_tar_gz_multimember_with_file_in_every_part_is_restored():
    """나눠 압축한 **모든 조각에 파일이 있어야** 후속 멤버 유실을 잡을 수 있다.

    앞 멤버가 파일 하나 + 종료 패딩뿐인 입력을 쓰면 두 번째 멤버를 통째로 유실해도
    결과 dict가 같아 테스트가 통과해 버린다.
    """
    entries = [
        ("SKILL.md", b"A" * 400),
        ("references/data.bin", bytes(range(256)) * 4),
        ("references/notes.md", b"tail content\n"),
    ]
    whole = make_tar(entries)
    single = gzip.compress(whole, mtime=0)
    expected = pkg._read_tar_gz_entries(single)
    assert len(expected) == 3
    for split in (1024, 4096, len(whole) // 2, len(whole) - 512):
        multi = gzip.compress(whole[:split], mtime=0) + gzip.compress(whole[split:], mtime=0)
        assert pkg._read_tar_gz_entries(multi) == expected, f"split={split}"


def test_tar_gz_multimember_incompressible_second_part_is_restored():
    """출력 cap을 넘긴 첫 멤버 뒤의 두 번째 멤버도 정확히 복원한다.

    `eof`에서 `unconsumed_tail`과 `unused_data`를 합치면 같은 바이트가 두 번 들어가
    출력이 1바이트 늘어 정상 입력이 예산 초과로 오거부된다(구현 리뷰 R2).
    """
    rng = random.Random(119)
    entries = [
        ("SKILL.md", b"A" * 65537),  # 첫 멤버가 64KiB 출력 cap을 넘긴다
        ("references/data.bin", rng.randbytes(100000)),
    ]
    whole = make_tar(entries)
    expected = pkg._read_tar_gz_entries(gzip.compress(whole, mtime=0))
    assert len(expected) == 2
    assert expected["references/data.bin"] == entries[1][1]
    for split in (1024, 66560, 100000):
        multi = gzip.compress(whole[:split], mtime=0) + gzip.compress(whole[split:], mtime=0)
        assert gzip.decompress(multi) == whole, "fixture 자체가 유효해야 한다"
        assert pkg._read_tar_gz_entries(multi) == expected, f"split={split}"


def test_tar_gz_reader_exact_budget_is_accepted():
    """reader 단위에서 예산과 정확히 같은 출력이면 통과한다(센티널이 정상 입자를 거르지 않음)."""
    payload = b"A" * 65537
    data = gzip.compress(payload, mtime=0)
    reader = pkg._BoundedGzipReader(data, len(payload))
    out = b""
    while chunk := reader.read(10240):
        out += chunk
    reader.drain()
    assert out == payload


def test_tar_gz_reader_budget_plus_one_is_rejected():
    """예산을 1바이트 넘기면 거부된다."""
    payload = b"A" * 65537
    data = gzip.compress(payload, mtime=0)
    reader = pkg._BoundedGzipReader(data, len(payload) - 1)
    with pytest.raises(SkillValidationError) as exc:
        reader.read(len(payload))
        reader.drain()
    assert exc.value.reason == "package_too_large"


def test_tar_gz_padding_only_chunk_does_not_hide_later_input():
    """패딩만 있는 청크를 입력이 끝난 것으로 보지 않는다(구현 리뷰 R1).

    패딩 뒤에 붙은 초과 멤버·JUNK·잘린 gzip은 모두 거부되어야 한다. 여기서 종료 처리하면
    뒤 멤버를 아예 검사하지 않아 버그와 숨김 payload가 함께 통과한다.
    """
    base = gzip.compress(make_tar([("SKILL.md", GOOD)]), mtime=0)
    oversized = gzip.compress(b"X" * (21 * 1024 * 1024), mtime=0)
    with pytest.raises(SkillValidationError) as exc:
        pkg._read_tar_gz_entries(base + b"\0" * 131072 + oversized)
    assert exc.value.reason == "package_too_large"
    with pytest.raises(SkillValidationError) as exc:
        pkg._read_tar_gz_entries(base + b"\0" * 131072 + b"JUNK")
    assert exc.value.reason == "bad_tar_gz"
    with pytest.raises(SkillValidationError) as exc:
        pkg._read_tar_gz_entries(base + b"\0" * 131072 + base[:-8])
    assert exc.value.reason == "bad_tar_gz"


@pytest.mark.parametrize(
    ("label", "build"),
    [
        ("첫 멤버 앞 NUL", lambda single: b"\0" * 8 + single),
        ("JUNK 뒤", lambda single: single + b"JUNKJUNK"),
        ("JUNK 앞", lambda single: b"JUNKJUNK" + single),
    ],
)
def test_tar_gz_non_gzip_trailing_bytes_are_rejected(label, build):
    """gzip 헤더가 아닌 바이트는 어떤 위치도 통과시키지 않는다."""
    with pytest.raises(SkillValidationError) as exc:
        pkg._read_tar_gz_entries(build(make_tar_gz([("SKILL.md", GOOD)])))
    assert exc.value.reason == "bad_tar_gz"


def test_tar_gz_broken_partial_header_is_rejected():
    """gzip 헤더가 잘린 입력은 zlib 즉시 에러가 아니라 EOF 검사로 거절된다."""
    single = make_tar_gz([("SKILL.md", GOOD)])
    with pytest.raises(SkillValidationError) as exc:
        pkg._read_tar_gz_entries(single + b"\x1f")
    assert exc.value.reason == "bad_tar_gz"


def test_tar_gz_uses_bounded_reader_not_tarfile_gzip(monkeypatch):
    """해제는 코어 reader가 맡는다 — `tarfile`을 비압축 `r|` 모드로만 연다.

    `tarfile`의 `r|gz`는 `_Stream._read`가 압축 청크를 상한 없이 풀고 트레일러 CRC도
    검증하지 않는다. 압축 모드로 열면 이 테스트가 실패한다.
    """
    modes: list[str] = []
    real_open = tarfile.open

    def spy_open(name=None, mode="r", *args, **kwargs):
        modes.append(mode)
        return real_open(name, mode, *args, **kwargs)

    # fixture를 만든 **뒤** spy를 설치한다 — `make_tar_gz`도 `tarfile.open("w")`를 쓴다.
    data = make_tar_gz([("SKILL.md", GOOD)])
    monkeypatch.setattr(pkg.tarfile, "open", spy_open)
    pkg._read_tar_gz_entries(data)
    assert modes == ["r|"]


# ---------- tar.gz: 정상 archive 보존 ----------


def _long_name(i: int) -> str:
    return "references/" + "x" * 180 + f"/{i}"


@pytest.mark.parametrize(
    "fmt", [tarfile.GNU_FORMAT, tarfile.PAX_FORMAT],
    ids=["gnu-longname", "pax-longname"],
)
def test_tar_gz_long_names_roundtrip(fmt):
    """GNU/PAX 확장 헤더가 붙는 긴 경로가 예산 안에서 복원된다."""
    data = make_tar_gz([(_long_name(0), b"A" * 100)], fmt)
    assert pkg._read_tar_gz_entries(data)[_long_name(0)] == b"A" * 100


def test_tar_gz_many_entries_roundtrip():
    entries = [(f"references/{i}", b"A" * 500) for i in range(100)]
    assert len(pkg._read_tar_gz_entries(make_tar_gz(entries))) == 100


def test_tar_gz_valid_skill_with_small_total_limit_is_accepted():
    """유효 SKILL.md + 99개 파일, `max_total_bytes`가 작아도 구조 여유 안에서 통과한다.

    이전 산식(`1024*N+1024`)은 이 입력을 거부했다 — 항목당 512B 헤더 + 512B 패딩과
    10240B record 패딩을 정확히 덮지 못한다.
    """
    use_settings(skill_import_max_total_bytes=51200)
    entries = [("SKILL.md", GOOD.ljust(512, b"#"))] + [
        (f"references/{i}", b"A" * 512) for i in range(99)
    ]
    assert len(pkg._read_tar_gz_entries(make_tar_gz(entries))) == 100


def test_tar_gz_budget_scales_with_entry_limit():
    """`skill_import_max_files`가 커지면 구조 여유도 커진다."""
    use_settings(skill_import_max_files=2000)
    budget = pkg._tar_gz_budget(CoreSettings())
    entries = [(f"references/{i}", b"A" * 5242) for i in range(2000)]
    assert len(make_tar(entries)) <= budget
    assert len(pkg._read_tar_gz_entries(make_tar_gz(entries))) == 2000


def test_tar_gz_unknown_format_still_rejected():
    """형식 감지와 정책 오류는 그대로다."""
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_import_archive_detailed(b"not an archive at all")
    assert exc.value.reason == "unsupported_archive"


def test_tar_gz_remote_import_path_uses_the_bounded_reader():
    """원격 archive 경로도 같은 reader를 거친다 — `import_url_detailed` 진입점 확인."""
    data = gzip.compress(
        make_tar([("release-notes/SKILL.md", GOOD)]) + b"\0" * (64 * 1024 * 1024),
        mtime=0,
    )
    with pytest.raises(SkillValidationError) as exc:
        pkg.unpack_import_archive_detailed(data)
    assert exc.value.reason == "package_too_large"
