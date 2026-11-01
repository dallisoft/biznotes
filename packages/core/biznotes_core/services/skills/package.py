"""스킬 패키지 저장/해시/파일트리 (skill_plan.md §7, §12.3).

in-memory 패키지 표현: dict[str, bytes] (정규화된 상대경로 → 내용).
스토리지 접근 함수는 동기 — 비동기 호출부에서 anyio.to_thread로 감싼다(런타임 패턴 일치).
"""

import hashlib
import io
import mimetypes
import posixpath
import tarfile
import uuid
import zipfile
import zlib
from collections.abc import Collection, Iterable
from typing import Any, Literal, NamedTuple

import structlog

from biznotes_core.config import CoreSettings, get_settings
from biznotes_core.services import storage
from biznotes_core.services.skills.validator import (
    MAX_PATH_BYTES,
    SkillValidationError,
    validate_path,
)

DRAFT_PREFIX = "drafts"
VERSION_PREFIX = "versions"

logger = structlog.get_logger()

# export ZIP의 고정 타임스탬프 — 같은 패키지가 항상 같은 byte로 나오게 한다
# (roundtrip 회귀와 hash 비교를 시각에 의존시키지 않는다).
EXPORT_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
# 내보내기 파일 모드 — `scripts/` 아래만 실행 비트를 준다(D10).
EXPORT_SCRIPT_MODE = 0o755
EXPORT_FILE_MODE = 0o644
# ZIP external_attr 상위 16비트가 유닉스 모드로 해석되려면 생성 시스템이 Unix(3)여야 한다.
_ZIP_CREATE_SYSTEM_UNIX = 3

# `read(n)`의 출력이 n으로 묶이는 압축 방식만 허용한다(#119). CPython `ZipExtFile._read1`은
# DEFLATED만 `decompress(data, max(n, MIN_READ_SIZE))`로 묶고, 그 밖(BZIP2·LZMA·ZSTANDARD)은
# `decompress(data)`를 인자 없이 불러 압축 청크를 통째로 푼 뒤 자른다. 실측: 신고 1KiB인
# 256MiB BZIP2 항목이 API 프로세스 피크 RSS를 +256MiB 밀었다. 판정은 `ZipExtFile`이 쓰는
# 중앙 디렉터리 값이다. 코어 export는 DEFLATED만 쓴다.
_SKILL_ZIP_READABLE_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
# 해제·손상 오류 → 정책 오류. `RuntimeError`는 암호화 항목(`ZipFile.open`이 비밀번호를 요구해
# 던진다)을 담기 위해 필요하다 — 빼면 암호화 ZIP이 500으로 새는다. `SkillValidationError`가
# `ValueError` 하위 타입이므로 아래 catch에서 반드시 먼저 재발생시킨다.
_ZIP_READ_ERRORS = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    NotImplementedError,
    EOFError,
    ValueError,
    OSError,
    RuntimeError,
    zlib.error,
)

# gzip 해제 예산(#119). `tarfile._Stream._read`는 압축 청크를 `self.cmp.decompress(buf)`로
# 출력 상한 없이 풀어 dbuf에 모은다 — 실측: 65KiB 입력이 피크 RSS +40MiB. 그래서 해제를
# `tarfile`에 맡기지 않고 여기서 예산을 건다. 예산은 max(신고 payload 예산의 2배, 구조 여유).
# tar 프레임은 정확히 모델링하지 않는다: 항목마다 512B 헤더 + 512B 패딩, `tarfile` writer는
# 10240B record로 끝까지 패딩하며(1B 파일 1개도 tar 10,240B), GNU/PAX 긴 이름은 항목당
# 1,024B 추가, PAX comment는 본문만 1,024B로 패딩, sparse 확장 블록 수는 항목 수와 무관하다.
# 그래서 "구조를 정확히 계산"과 "예산을 작게 잡아 숨김 payload를 빨리 거절"은 한 산식으로
# 동시에 만족시킬 수 없다 — 정상 입성을 먼저 통과시키는 쪽을 택한다.
_TAR_GZ_STRUCTURE_BASE_BYTES = 1024 * 1024  # record 패딩 + 종료 블록 + 여유
_TAR_GZ_STRUCTURE_PER_ENTRY_BYTES = 8 * 1024  # 헤더·패딩 + GNU/PAX 확장 + PAX comment 여유
_TAR_GZ_MIN_BUDGET_BYTES = 64 * 1024
_GZIP_WBITS = 16 + zlib.MAX_WBITS  # zlib가 gzip 헤더·트레일러를 직접 다룬다
_GZIP_FEED_BYTES = 64 * 1024  # 압축 입력을 한 번에 공급하는 양
# 와이어 바이트 상한은 아니고 inflater 누적 출력 상한이다(잔여 위험 — 계획 참조).

# tar 확장 헤더(PAX·GNU longname/longlink). `tarfile`은 이것을 member로 내보내지 않고 다음 헤더를
# 재귀로 읽는다(`_proc_pax`·`_proc_gnulong` → `fromtarfile`). 그래서 member 수 상한에 잡히지
# 않고, 연속 수백 개면 RecursionError가 된다(#402 리뷰). 정상 archive는 member 하나 앞에 많아야
# 전역 PAX + PAX + GNU longname·longlink 정도다.
_TAR_EXTENSION_TYPES = frozenset(
    {
        tarfile.XHDTYPE,
        tarfile.XGLTYPE,
        tarfile.SOLARIS_XHDTYPE,
        tarfile.GNUTYPE_LONGNAME,
        tarfile.GNUTYPE_LONGLINK,
    }
)
_TAR_MAX_EXTENSION_RUN = 8
# tar 파서가 내는 손상·비정상 입력 오류. `tarfile`은 잘못된 GNU sparse map에 ValueError를 그대로
# 던지고, RecursionError는 확장 헤더 재귀의 마지막 방어선이다. `SkillValidationError`(ValueError
# 하위 타입)는 이보다 먼저 다시 던진다.
_TAR_READ_ERRORS = (
    tarfile.TarError,
    EOFError,
    OSError,
    zlib.error,
    RecursionError,
    ValueError,
    OverflowError,
)

ExportLayout = Literal["wrapped", "flat"]


def _bucket() -> str:
    return get_settings().storage_bucket_skill_packages


def draft_prefix(skill_id: uuid.UUID) -> str:
    return f"{DRAFT_PREFIX}/{skill_id}/"


def version_prefix(version_id: uuid.UUID) -> str:
    return f"{VERSION_PREFIX}/{version_id}/"


# ---------- 해시 / 파일 트리 ----------


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package_hash(files: dict[str, bytes]) -> str:
    """경로+내용의 결정적 머클 해시 (정렬 후 path\\0contenthash 누적)."""
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(path.encode("utf-8"))
        h.update(b"\0")
        h.update(file_hash(files[path]).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def _role_for(path: str) -> str:
    if path == "SKILL.md":
        return "skill_md"
    if path == get_settings().skill_manifest_filename:
        return "manifest"
    top = path.split("/", 1)[0]
    return {"references": "reference", "assets": "asset", "scripts": "script"}.get(top, "other")


def build_file_tree(files: dict[str, bytes]) -> list[dict]:
    """[{path, role, size, media_type, hash}] — skill_files 테이블 대체(§6.1)."""
    tree: list[dict] = []
    for path in sorted(files):
        data = files[path]
        media_type, _ = mimetypes.guess_type(path)
        tree.append(
            {
                "path": path,
                "role": _role_for(path),
                "size": len(data),
                "media_type": media_type or "application/octet-stream",
                "hash": file_hash(data),
            }
        )
    return tree


# ---------- ZIP 언팩 ----------


class UnpackedPackage(NamedTuple):
    """언팩 결과 — 파일 dict와 **원본 ZIP의 root 디렉토리 이름**.

    P1까지 `unpack_zip`은 root 이름을 버렸기 때문에 표준 oracle의
    directory-name 규칙이 작용할 대상 자체가 없었다(P1-STATUS §3
    `root-mismatch`). P2는 root를 보존해 strict 검증에 전달한다.

    `root_name`이 None이면 flat 패키지(최상위에 SKILL.md)이며, HCI-2 승인에
    따라 frontmatter name을 canonical root로 해석한다.
    """

    files: dict[str, bytes]
    root_name: str | None


def unpack_zip(data: bytes) -> dict[str, bytes]:
    """ZIP → 정규화된 파일 dict (기존 호출 계약 유지)."""
    return unpack_zip_detailed(data).files


def _invalid_selection() -> SkillValidationError:
    return SkillValidationError(
        "invalid_skill_selection", "선택한 SKILL.md를 ZIP에서 찾을 수 없습니다"
    )


def _reject_too_many_archive_entries(count: int, settings: CoreSettings) -> None:
    """archive 전체 항목 수 상한(#402) — 선택과 무관하고 디렉터리 항목도 센다."""
    if count > settings.skill_import_max_archive_entries:
        raise SkillValidationError(
            "too_many_archive_entries",
            f"archive 항목 수가 상한({settings.skill_import_max_archive_entries})을 넘습니다",
        )


def _reject_unreadable_method(info: zipfile.ZipInfo) -> None:
    # 압축 방식을 여는 **전에** 판정한다 — 미지원 항목은 한 번도 해제되지 않는다.
    if info.compress_type not in _SKILL_ZIP_READABLE_METHODS:
        raise SkillValidationError(
            "unsupported_compression",
            f"지원하지 않는 압축 방식입니다: {info.filename}",
        )


def _reject_bomb_ratio(info: zipfile.ZipInfo, settings: CoreSettings) -> None:
    if info.file_size / max(info.compress_size, 1) > settings.skill_import_max_compression_ratio:
        raise SkillValidationError(
            "bomb_suspected", f"압축률이 허용 상한을 넘습니다: {info.filename}"
        )


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    # 외부 속성 상위 16비트가 유닉스 모드다
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _read_zip_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    # `zf.read(info)`는 내부적으로 `read(-1)`이라 끝까지 풀어 버린다(실측: 신고 1KiB인 256MiB
    # 항목이 RSS를 +256MiB 밀었다). `read(size + 1)`은 출력을 요청 크기로 묶는다. 선두 CRC까지
    # 위조하면 잘린 내용이 그대로 반환될 수 있지만 전개량은 제한된다 — 원본 무결성 증명을
    # 목표로 하지는 않는다.
    with zf.open(info) as fp:
        content = fp.read(info.file_size + 1)
    if len(content) != info.file_size:
        raise SkillValidationError(
            "bad_zip", f"ZIP 항목 크기가 일치하지 않습니다: {info.filename}"
        )
    return content


def _read_zip_entries(data: bytes, *, keep: str | None = None) -> dict[str, bytes]:
    """ZIP 항목을 안전하게 읽는다. 선택 가져오기도 같은 입력 한계를 쓴다.

    ``keep``은 폴더 선택의 서브트리 접두사(`P/`, #402)다. 주면 폭탄 관문(압축 방식·신고
    압축률)은 중앙 디렉터리 값으로 archive 전체에 걸고, 패키지 한도(심링크·충돌·개수·크기)와
    해제는 접두사 아래 항목에만 건다 — 밖의 항목은 열지 않는다. None이면 기존 순서 그대로다.
    항목 수 상한은 두 경우 모두 archive 전체에 건다.
    """
    settings = get_settings()
    raw: dict[str, bytes] = {}
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            _reject_too_many_archive_entries(len(infos), settings)
            if keep is not None:
                files = [info for info in infos if not info.is_dir()]
                for info in files:
                    _reject_unreadable_method(info)
                    _reject_bomb_ratio(info, settings)
                if not any(info.filename == f"{keep}SKILL.md" for info in files):
                    raise _invalid_selection()
            for info in infos:
                if info.is_dir():
                    continue
                if keep is not None and not info.filename.startswith(keep):
                    continue  # 서브트리 밖 — 검사도 해제도 하지 않는다
                if _is_zip_symlink(info):
                    raise SkillValidationError("symlink", f"symlink 불가: {info.filename}")
                name = info.filename
                if name.startswith("__MACOSX/") or name.endswith("/.DS_Store"):
                    continue
                if name in raw:
                    raise SkillValidationError(
                        "path_collision", f"ZIP에 같은 경로 항목이 둘 이상 있습니다: {name}"
                    )
                if len(raw) >= settings.skill_import_max_files:
                    raise SkillValidationError("too_many_files", "ZIP 파일 수 초과")
                _reject_unreadable_method(info)
                size = info.file_size
                if size > settings.skill_import_max_file_bytes:
                    raise SkillValidationError("file_too_large", f"단일 파일 초과: {name}")
                _reject_bomb_ratio(info, settings)
                total += size
                if total > settings.skill_import_max_total_bytes:
                    raise SkillValidationError("package_too_large", "압축 해제 크기 초과")
                raw[name] = _read_zip_member(zf, info)
    except SkillValidationError:
        # 신규 정책 사유(unsupported_compression·bomb_suspected 등)는 bad_zip으로 덮지 않는다.
        raise
    except _ZIP_READ_ERRORS as e:
        # 손상·암호화·미지원 압축·생성 오류 — 500으로 새지 않게 정책 오류로 보고한다.
        raise SkillValidationError("bad_zip", "유효한 ZIP을 읽을 수 없습니다") from e

    if not raw:
        raise SkillValidationError("empty_zip", "ZIP에 파일이 없습니다")
    return raw


def skill_zip_candidates(data: bytes) -> list[str]:
    """선택 가져오기 UI가 제시할 스킬 후보 경로(D4 Phase 2). ZIP 안의 전체 경로다."""
    raw = _read_zip_entries(data)
    prefix, inner = _unwrap_bundle(raw)
    inner_candidates = _skill_candidate_paths(inner)
    if prefix and len(inner_candidates) > 1:
        return [prefix + path for path in inner_candidates]
    return _skill_candidate_paths(raw)


def _unwrap_bundle(raw: dict[str, bytes]) -> tuple[str, dict[str, bytes]]:
    """스킬 폴더들을 감싼 단일 폴더(`bundle/`, `repo-main/skills/`)를 벗긴다.

    SKILL.md가 없는 층에서 최상위 폴더가 하나뿐인 동안만 내려간다. `root/SKILL.md`가 있는
    층(단일 래퍼 스킬)에서는 멈추므로 기존 래퍼형 처리와 겹치지 않는다.
    """
    prefix = ""
    current = raw
    while "SKILL.md" not in current:
        tops = {name.split("/", 1)[0] for name in current}
        if len(tops) != 1:
            break
        top = next(iter(tops))
        if f"{top}/SKILL.md" in current or not all(name.startswith(f"{top}/") for name in current):
            break
        current = {name[len(top) + 1 :]: content for name, content in current.items()}
        prefix += f"{top}/"
    return prefix, current


def unpack_zip_detailed(
    data: bytes, *, selected_skill_path: str | None = None
) -> UnpackedPackage:
    """ZIP → (파일 dict, root 이름). ``selected_skill_path``는 여러 스킬 중 하나를 고른다."""
    raw = _read_zip_entries(data, keep=_subtree_prefix(selected_skill_path))
    return unpack_entries_detailed(raw, selected_skill_path=selected_skill_path)


def unpack_entries_detailed(
    raw: dict[str, bytes], *, selected_skill_path: str | None = None
) -> UnpackedPackage:
    """안전하게 읽힌 archive 항목을 스킬 패키지로 정규화한다.

    ZIP과 tar.gz는 바깥 컨테이너만 다르고, root 선택·경로 충돌·SKILL.md 규칙은
    반드시 같은 경계를 지나야 한다. 새 archive reader는 이 함수에만 raw 파일을 넘긴다.
    """
    stripped, root_name = _select_skill_package(raw, selected_skill_path)
    # 경로 정규화·검증. 정규화 **후** 충돌하는 항목은 조용히 덮어쓰지 않고 거부한다
    # (`root/SKILL.md`와 `root/a/../SKILL.md`가 같은 경로로 접히는 사례).
    files: dict[str, bytes] = {}
    for name, content in stripped.items():
        normalized = validate_path(name)
        if normalized in files:
            raise SkillValidationError(
                "path_collision",
                f"정규화 후 경로가 충돌합니다: {name} → {normalized}",
            )
        files[normalized] = content
    return UnpackedPackage(files=files, root_name=root_name)


class _BoundedGzipReader(io.RawIOBase):
    """gzip 스트림을 정해진 총량 안에서만 푸는 읽기 전용 파일 객체.

    `tarfile`은 `mode="r|"`에서 이 객체의 `read(size)`만 호출한다. 여기서
    `decompress(data, max_length)`로 출력을 묶으므로, 신고 크기와 무관하게 전개량이
    예산을 넘지 못한다. `tarfile`의 gzip 경로를 쓰지 않는 이유: `_Stream._read`가
    `self.cmp.decompress(buf)`를 상한 없이 부르고, 현행 raw deflate는 gzip 트레일러의
    CRC32·ISIZE도 검증하지 않는다(#119).
    """

    def __init__(self, data: bytes, budget: int) -> None:
        self._source = io.BytesIO(data)
        self._budget = budget
        self._out = b""  # 해제됐지만 아직 소비되지 않은 출력
        self._carry = b""  # 아직 inflater에 넣지 못한 압축 입력
        # zlib은 타입 힌트(stub)가 없어 `_Decompress`를 노출하지 않는다. 실제 반환 객체는
        # `decompress`·`eof`·`unused_data`·`unconsumed_tail`을 갖는다.
        self._inflater: Any = None
        self._completed = False  # eof가 확인된 gzip 멤버를 하나라도 끝냈는가
        self._done = False

    # --- io.RawIOBase 계약 ---
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def _next_feed(self) -> bytes | None:
        """inflater에 다음으로 공급할 압축 입력을 얻는다.

        압축 입력과 해제 출력을 **별도 버퍼**로 둔다 — 둘을 한 버퍼에 섞으면 재투입한
        압축 바이트 사이에 이미 해제된 출력이 끼어 archive가 깨진다.

        `None`은 **raw source EOF**다. 완료된 멤버 뒤에는 앞 NUL을 제거해 본다. 제거
        결과가 비었다면 그것은 패딩이므로 source를 계속 읽어야 한다 — 여기서 종료 처리하면
        패딩 뒤에 붙은 다음 멤버를 아예 검사하지 않아 버그와 숨김 payload가 통과한다.
        `None`(진짜 EOF)과 "패딩만 있는 청크"를 구분하는 것이 이 분기의 전부다.
        """
        if self._carry:
            return self._carry
        if self._inflater is None and self._completed:
            while True:
                chunk = self._source.read(_GZIP_FEED_BYTES)
                if not chunk:
                    return None  # 진짜 입력 끝
                # 앞 NUL만 제거한다 — 양끝 제거는 gzip 헤더·트레일러의 끝 NUL까지 지워
                # 다음 멤버의 `incorrect header check`를 낸다.
                stripped = chunk.lstrip(b"\0")
                if stripped:
                    return stripped
                # 패딩뿐인 청크다. 다음 청크로 넘어간다.
        chunk = self._source.read(_GZIP_FEED_BYTES)
        return chunk or None

    def _pump(self, want: int) -> None:
        """버퍼에 `want`바이트가 모이거나 입력이 끝날 때까지 해제한다."""
        while not self._done and len(self._out) < want:
            feed = self._next_feed()
            if feed is None:
                if self._inflater is None and self._completed:
                    self._done = True
                    return
                raise EOFError("잘린 gzip 스트림")
            if self._inflater is None:
                self._inflater = zlib.decompressobj(_GZIP_WBITS)
            # `max_length`는 **양수**로 유지한다. `decompress(data, 0)`은 무제한이라
            # bound가 뚫린다(실측: 한 번에 2MiB). 세 상한의 최소값을 쓴다:
            # 남은 요청량(무제한 read-ahead 방지) · 입력 청크 · 예산 + 1(초과 센티널).
            limit = max(1, min(want - len(self._out), _GZIP_FEED_BYTES, self._budget + 1))
            out = self._inflater.decompress(feed, limit)
            self._budget -= len(out)
            if self._budget < 0:
                self._budget = 0
                raise SkillValidationError("package_too_large", "압축 해제 크기 초과")
            self._out += out
            if self._inflater.eof:
                # 다음 멤버의 입력은 `unused_data` **하나**뿐이다. `unconsumed_tail`을
                # 같이 더하면 같은 바이트가 두 번 들어가 데이터가 뒤에 덧붙는다(실측:
                # 65538B 예상 → 65539B, 끝이 `AAABB`).
                self._carry = self._inflater.unused_data.lstrip(b"\0")
                self._inflater = None
                self._completed = True
            else:
                # `max_length`로 끊겨 소모되지 않은 압축 입력은 다음 호출에 먼저 넣는다.
                self._carry = self._inflater.unconsumed_tail

    def read(self, size: int = -1) -> bytes:
        if self.closed:
            raise ValueError("read from closed file")
        if size is None or size < 0:
            raise SkillValidationError(
                "bad_tar_gz", "tar.gz 스트림은 순차적으로만 읽을 수 있습니다"
            )
        if size == 0:
            return b""
        self._pump(size)
        chunk, self._out = self._out[:size], self._out[size:]
        return chunk

    def drain(self) -> None:
        """남은 gzip을 같은 전역 예산으로 버리면서 소진한다.

        `TarFile.next`는 첫 zero 블록에서 끝나고 `TarFile.close()`는 남은 데이터를 읽지
        않는다. 그래서 이 drain이 숨김 payload를 예산 밖으로 밀어내고, 잘린 트레일러와
        CRC/ISIZE 오류도 여기서 드러난다.
        """
        while not self._done:
            self._pump(_GZIP_FEED_BYTES)
            self._out = b""


def _tar_gz_budget(settings: CoreSettings) -> int:
    """gzip 해제 예산 — 와이어 바이트 상한이 아니라 inflater 누적 출력 상한이다.

    폴더 선택(#402)이면 서브트리 밖 member도 같은 예산으로 흘려보내므로, 항목당 항은
    파일 수 상한과 archive 항목 수 상한 중 큰 쪽이다(기본값이면 예산이 그대로다).
    """
    entries = max(settings.skill_import_max_files, settings.skill_import_max_archive_entries)
    return max(
        settings.skill_import_max_total_bytes * 2,
        _TAR_GZ_MIN_BUDGET_BYTES,
        _TAR_GZ_STRUCTURE_BASE_BYTES + _TAR_GZ_STRUCTURE_PER_ENTRY_BYTES * (entries + 1),
    )


def _bounded_tar_info(settings: CoreSettings) -> type[tarfile.TarInfo]:
    """확장 헤더를 세는 `TarInfo` — 총수는 archive 항목 수 상한, 연속 수는 `_TAR_MAX_EXTENSION_RUN`.

    `tarfile`은 헤더 하나마다 `_proc_member`를 지난다(3.12~3.14 같은 비공개 경로). 총수를 member
    수와 따로 세는 이유: 이름이 길거나 비ASCII인 정상 archive는 member마다 PAX 헤더를 하나씩
    가져, 같은 카운터에 넣으면 상한을 절반만 쓰게 된다.
    """
    seen = 0
    run = 0

    class _BoundedTarInfo(tarfile.TarInfo):
        def _proc_member(self, tarfile_obj: tarfile.TarFile) -> tarfile.TarInfo:
            nonlocal seen, run
            if self.type not in _TAR_EXTENSION_TYPES:
                run = 0
            else:
                seen += 1
                run += 1
                if run > _TAR_MAX_EXTENSION_RUN:
                    raise SkillValidationError(
                        "bad_tar_gz", "tar 확장 헤더가 연속으로 너무 많습니다"
                    )
                _reject_too_many_archive_entries(seen, settings)
            result: tarfile.TarInfo = super()._proc_member(tarfile_obj)  # type: ignore[misc]
            return result

    return _BoundedTarInfo


def _open_tar_gz(
    data: bytes, settings: CoreSettings
) -> tuple[_BoundedGzipReader, tarfile.TarFile]:
    """예산 안에서 푸는 gzip 위에 비압축 tar 스트림을 연다(첫 헤더를 여기서 읽는다)."""
    try:
        reader = _BoundedGzipReader(data, _tar_gz_budget(settings))
        # 해제를 코어가 예산 안에서 끝내므로 `tarfile`은 비압축 스트림만 읽는다.
        archive = tarfile.open(fileobj=reader, mode="r|", tarinfo=_bounded_tar_info(settings))
    except SkillValidationError:
        raise
    except _TAR_READ_ERRORS as e:
        raise SkillValidationError("bad_tar_gz", "유효한 tar.gz 파일이 아닙니다") from e
    return reader, archive


def _check_tar_member_path(local: str, *, is_dir: bool) -> None:
    """추출 전 tar member 경로 관문 — 실제 추출은 하지 않아도 archive 경계는 ZIP과 같다."""
    if not is_dir:
        validate_path(local)
        return
    # 흔한 `./` root marker만 허용하고, traversal/absolute directory entry는 거부한다.
    if local.startswith("/"):
        validate_path(local)
    directory_name = local.rstrip("/")
    if directory_name not in ("", "."):
        validate_path(directory_name)


def _read_tar_member(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    extracted = archive.extractfile(member)
    if extracted is None:
        raise SkillValidationError(
            "bad_tar_gz", f"tar.gz 항목을 읽을 수 없습니다: {member.name}"
        )
    with extracted:
        content = extracted.read(member.size + 1)
    if len(content) != member.size:
        raise SkillValidationError(
            "bad_tar_gz", f"tar.gz 항목 크기가 일치하지 않습니다: {member.name}"
        )
    return content


def _read_tar_gz_entries(data: bytes, *, keep: str | None = None) -> dict[str, bytes]:
    """tar.gz 항목을 메모리 dict로 읽는다.

    extraction을 하지 않으므로 링크 대상이나 device node가 호스트에 만들어질 수 없다.
    regular file 이외의 member는 directory를 제외하고 모두 거부한다. ``keep``(폴더 선택의
    서브트리 접두사, #402)을 주면 그 밖의 member는 검사·`extractfile` 없이 흘려보낸다.
    member 수 상한은 항상 archive 전체에 건다.
    """
    settings = get_settings()
    raw: dict[str, bytes] = {}
    total = 0
    count = 0
    reader, archive = _open_tar_gz(data, settings)
    try:
        with archive:
            for member in archive:
                count += 1
                _reject_too_many_archive_entries(count, settings)
                if keep is not None and not member.name.startswith(keep):
                    continue  # 서브트리 밖 — 스트림에서 건너뛸 뿐 풀어 담지 않는다
                # 폴더 선택이면 접두사 아래 상대 경로를 본다. 선택 위쪽 폴더(`.claude/skills/`)는
                # 패키지 경로가 아니다 — ZIP과 같다.
                local = member.name if keep is None else member.name[len(keep) :]
                if member.isdir():
                    _check_tar_member_path(local, is_dir=True)
                    continue
                if not member.isreg():
                    raise SkillValidationError(
                        "unsafe_tar_member",
                        f"regular file이 아닌 tar member는 허용되지 않습니다: {member.name}",
                    )
                # 추출 전에 입력 경로를 검사한다. 이 단계의 경로는 아직 root를 벗기기 전이다.
                _check_tar_member_path(local, is_dir=False)
                if member.name in raw:
                    raise SkillValidationError(
                        "path_collision",
                        f"tar.gz에 같은 경로 항목이 둘 이상 있습니다: {member.name}",
                    )
                if len(raw) >= settings.skill_import_max_files:
                    raise SkillValidationError("too_many_files", "tar.gz 파일 수 초과")
                if member.size > settings.skill_import_max_file_bytes:
                    raise SkillValidationError("file_too_large", f"단일 파일 초과: {member.name}")
                total += member.size
                if total > settings.skill_import_max_total_bytes:
                    raise SkillValidationError("package_too_large", "압축 해제 크기 초과")
                raw[member.name] = _read_tar_member(archive, member)
            # `TarFile.next`는 첫 zero 블록에서 끝나고 `close()`는 남은 데이터를 읽지 않는다.
            # drain이 숨김 payload를 예산 밖으로 밀어내고 잘린 트레일러·CRC/ISIZE 오류를
            # 여기서 드러낸다.
            reader.drain()
    except SkillValidationError:
        raise
    except _TAR_READ_ERRORS as e:
        # `zlib.error`는 `tarfile.TarError` 밖에서 나온다 — 잘린 gzip의 CRC32/ISIZE.
        raise SkillValidationError("bad_tar_gz", "tar.gz 항목을 읽을 수 없습니다") from e
    # 스트림이라 선택값이 archive에 있는지는 끝에서야 안다.
    if keep is not None and f"{keep}SKILL.md" not in raw:
        raise _invalid_selection()
    if not raw:
        raise SkillValidationError("empty_zip", "archive에 파일이 없습니다")
    return raw


def unpack_tar_gz_detailed(
    data: bytes, *, selected_skill_path: str | None = None
) -> UnpackedPackage:
    """tar.gz → 정규화된 스킬 package. ZIP과 같은 선택 가져오기를 지원한다."""
    raw = _read_tar_gz_entries(data, keep=_subtree_prefix(selected_skill_path))
    return unpack_entries_detailed(raw, selected_skill_path=selected_skill_path)


_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_GZIP_MAGIC = b"\x1f\x8b"


def unpack_import_archive_detailed(
    data: bytes, *, selected_skill_path: str | None = None
) -> UnpackedPackage:
    """원격 가져오기 archive를 형식 감지해 언팩한다 (ZIP·`.skill` 또는 tar.gz)."""
    if data.startswith(_ZIP_MAGIC):
        return unpack_zip_detailed(data, selected_skill_path=selected_skill_path)
    if data.startswith(_GZIP_MAGIC):
        return unpack_tar_gz_detailed(data, selected_skill_path=selected_skill_path)
    raise SkillValidationError(
        "unsupported_archive", "ZIP, .skill 또는 tar.gz archive만 가져올 수 있습니다"
    )


def _is_skill_candidate(name: str) -> bool:
    """선택할 수 있는 SKILL.md 경로(#402) — 정규형이고 `__MACOSX/` 밖이며 깊이는 무관하다."""
    if name != "SKILL.md" and not name.endswith("/SKILL.md"):
        return False
    return (
        posixpath.normpath(name) == name
        and not name.startswith(("/", "__MACOSX/"))
        and "\\" not in name
        and ".." not in name.split("/")
        and name.isprintable()
        and len(name.encode("utf-8")) <= MAX_PATH_BYTES
    )


def _archive_skill_candidates(names: Iterable[str]) -> list[str]:
    """archive 전체 경로 기준의 SKILL.md 후보 — 선택값은 이 목록의 값과 정확히 같아야 한다."""
    return sorted({name for name in names if _is_skill_candidate(name)})


def _subtree_prefix(selected_skill_path: str | None) -> str | None:
    """폴더 선택(`P/SKILL.md`)의 서브트리 접두사 `P/`. 선택 없음·루트 선택은 None(기존 경로).

    끝 `/`까지 비교하므로 `skills/humanize/`가 `skills/humanize-korean/`을 잡지 않는다.
    """
    if selected_skill_path is None or selected_skill_path == "SKILL.md":
        return None
    if not _is_skill_candidate(selected_skill_path):
        raise _invalid_selection()
    return posixpath.dirname(selected_skill_path) + "/"


def _skill_candidate_paths(raw: Iterable[str]) -> list[str]:
    """루트 또는 최상위 폴더의 SKILL.md — 여러 스킬 거부와 선택 없는 기존 경로가 쓴다."""
    return sorted(
        name
        for name in raw
        if name == "SKILL.md" or (name.count("/") == 1 and name.endswith("/SKILL.md"))
    )


def _reject_multiple_skills(raw: dict[str, bytes]) -> None:
    """선택값 없는 여러 스킬 ZIP은 기존 계약대로 거부한다.

    루트의 `SKILL.md`와 최상위 폴더의 `<dir>/SKILL.md`를 함께 센다 — 평면 패키지에 다른
    스킬 폴더가 딸려 온 경우(둘 다 존재)도 거부해야 두 번째 스킬이 데이터 파일로 조용히
    섞여 들어가지 않는다.
    """
    candidates = _skill_candidate_paths(raw)
    if len(candidates) > 1:
        raise SkillValidationError(
            "multiple_skills",
            "SKILL.md가 여러 개입니다. 스킬을 하나씩 가져오세요: " + ", ".join(candidates),
        )


def _select_skill_package(
    raw: dict[str, bytes], selected_skill_path: str | None
) -> tuple[dict[str, bytes], str | None]:
    """선택된 스킬만 떼어 상대 경로 패키지로 만든다.

    선택하지 않았거나 루트 `SKILL.md`를 고르면 기존 래퍼/평면 처리와 완전히 같은 결과를
    낸다. 폴더(`P/SKILL.md`, 깊이 무관)를 고르면 `P/` 아래만 쓴다(#402).
    """
    if selected_skill_path is not None and selected_skill_path != "SKILL.md":
        return _select_subtree(raw, selected_skill_path)
    bundle_prefix, inner = _unwrap_bundle(raw)
    if bundle_prefix and len(_skill_candidate_paths(inner)) > 1:
        # 감싼 다중 스킬 ZIP — 래퍼 안의 스킬은 폴더 선택(위)으로만 고른다.
        if selected_skill_path is not None:
            raise _invalid_selection()
        raw = inner
    candidates = _skill_candidate_paths(raw)
    if len(candidates) <= 1:
        if selected_skill_path is not None and selected_skill_path not in candidates:
            raise _invalid_selection()
        return _strip_common_root(raw)
    if selected_skill_path is None:
        _reject_multiple_skills(raw)
    if selected_skill_path not in candidates:
        raise _invalid_selection()

    # 루트 `SKILL.md` 선택 — 다른 후보 폴더의 파일은 섞지 않는다.
    child_roots = {candidate.split("/", 1)[0] for candidate in candidates if "/" in candidate}
    selected = {
        name: content
        for name, content in raw.items()
        if name.split("/", 1)[0] not in child_roots
    }
    _reject_multiple_skills(selected)
    return selected, None


def _select_subtree(
    raw: dict[str, bytes], selected_skill_path: str
) -> tuple[dict[str, bytes], str | None]:
    """폴더 선택 — `P/` 아래만 떼어 내고 root 이름은 `P`의 마지막 폴더다(#402)."""
    if not _is_skill_candidate(selected_skill_path) or selected_skill_path not in raw:
        raise _invalid_selection()
    root = posixpath.dirname(selected_skill_path)
    prefix = f"{root}/"
    selected = {
        name[len(prefix):]: content
        for name, content in raw.items()
        if name.startswith(prefix) and len(name) > len(prefix)
    }
    # 고른 폴더 안에 또 다른 스킬 폴더가 있으면(`beta/gamma/SKILL.md`) 선택 없이 가져올 때와
    # 같이 거부한다 — 두 번째 스킬이 데이터 파일로 섞여 들어가지 않게 한다.
    _reject_multiple_skills(selected)
    return selected, posixpath.basename(root)


# ---------- 후보 조회 사전 점검 (#402) ----------

# 후보 조회가 요약하는 후보 수 상한 — 넘는 수는 응답의 `truncated`로 알린다.
MAX_IMPORT_CANDIDATES = 100


class ArchiveEntry(NamedTuple):
    """후보 사전 점검용 항목 메타데이터(내용 없음)."""

    name: str
    size: int
    kind: Literal["file", "dir", "link"]  # link: 심링크(ZIP)·regular가 아닌 member(tar)


class ArchiveScan(NamedTuple):
    """후보 조회용 archive 메타데이터와 요약할 후보의 SKILL.md 내용."""

    archive_format: Literal["zip", "tar.gz"]
    entries: list[ArchiveEntry]
    candidates: list[str]  # 전체 후보(정렬). 요약은 앞의 MAX_IMPORT_CANDIDATES개만 한다
    skill_md: dict[str, bytes]  # 크기 상한 안에서 읽은 후보 SKILL.md


class CandidateLayout(NamedTuple):
    file_count: int
    errors: list[str]
    root_name: str | None


def scan_zip_archive(data: bytes) -> ArchiveScan:
    """업로드 ZIP·`.skill`의 후보 사전 점검 — 중앙 디렉터리와 후보 SKILL.md만 읽는다.

    archive 전체 관문(항목 수·압축 방식·신고 압축률)은 폴더 선택 가져오기와 같다. 요약할
    후보의 SKILL.md만 크기 상한 안에서 풀고, 그 합계도 압축 해제 합계 상한 안이다.
    """
    settings = get_settings()
    entries: list[ArchiveEntry] = []
    skill_md: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            _reject_too_many_archive_entries(len(infos), settings)
            readable: dict[str, zipfile.ZipInfo] = {}
            for info in infos:
                if info.is_dir():
                    continue
                _reject_unreadable_method(info)
                _reject_bomb_ratio(info, settings)
                name = info.filename
                # 가져오기 reader와 같은 순서 — 심링크를 먼저 보고 macOS 부산물은 건너뛴다
                if _is_zip_symlink(info):
                    entries.append(ArchiveEntry(name, info.file_size, "link"))
                elif not (name.startswith("__MACOSX/") or name.endswith("/.DS_Store")):
                    entries.append(ArchiveEntry(name, info.file_size, "file"))
                    readable.setdefault(name, info)
            if not entries:
                raise SkillValidationError("empty_zip", "ZIP에 파일이 없습니다")
            candidates = _archive_skill_candidates(entry.name for entry in entries)
            budget = settings.skill_import_max_total_bytes
            for path in candidates[:MAX_IMPORT_CANDIDATES]:
                target = readable.get(path)
                if target is None or target.file_size > settings.skill_import_max_file_bytes:
                    continue  # 심링크·크기 초과 — 요약이 사유를 단다
                budget -= target.file_size
                if budget < 0:
                    raise SkillValidationError("package_too_large", "압축 해제 크기 초과")
                skill_md[path] = _read_zip_member(zf, target)
    except SkillValidationError:
        raise
    except _ZIP_READ_ERRORS as e:
        raise SkillValidationError("bad_zip", "유효한 ZIP을 읽을 수 없습니다") from e
    return ArchiveScan("zip", entries, candidates, skill_md)


def _scan_tar_gz(data: bytes) -> ArchiveScan:
    """tar.gz 후보 사전 점검 — 같은 해제 예산으로 스트림을 한 번 흘린다."""
    settings = get_settings()
    entries: list[ArchiveEntry] = []
    skill_md: dict[str, bytes] = {}
    read_total = 0
    reader, archive = _open_tar_gz(data, settings)
    try:
        with archive:
            for member in archive:
                _reject_too_many_archive_entries(len(entries) + 1, settings)
                if member.isdir():
                    entries.append(ArchiveEntry(member.name, 0, "dir"))
                    continue
                if not member.isreg():
                    entries.append(ArchiveEntry(member.name, 0, "link"))
                    continue
                entries.append(ArchiveEntry(member.name, member.size, "file"))
                if (
                    not _is_skill_candidate(member.name)
                    or member.name in skill_md
                    or member.size > settings.skill_import_max_file_bytes
                ):
                    continue
                # 정렬 순서는 스트림 끝에서야 알므로 후보 SKILL.md는 모두 읽되 합계를 묶는다
                read_total += member.size
                if read_total > settings.skill_import_max_total_bytes:
                    raise SkillValidationError("package_too_large", "압축 해제 크기 초과")
                skill_md[member.name] = _read_tar_member(archive, member)
            reader.drain()
    except SkillValidationError:
        raise
    except _TAR_READ_ERRORS as e:
        raise SkillValidationError("bad_tar_gz", "tar.gz 항목을 읽을 수 없습니다") from e
    files = [entry.name for entry in entries if entry.kind != "dir"]
    if not files:
        raise SkillValidationError("empty_zip", "archive에 파일이 없습니다")
    return ArchiveScan("tar.gz", entries, _archive_skill_candidates(files), skill_md)


def scan_import_archive(data: bytes) -> ArchiveScan:
    """원격 archive 후보 사전 점검 — 형식 감지는 `unpack_import_archive_detailed`와 같다."""
    if data.startswith(_ZIP_MAGIC):
        return scan_zip_archive(data)
    if data.startswith(_GZIP_MAGIC):
        return _scan_tar_gz(data)
    raise SkillValidationError(
        "unsupported_archive", "ZIP, .skill 또는 tar.gz archive만 가져올 수 있습니다"
    )


def inspect_candidate(scan: ArchiveScan, path: str) -> CandidateLayout:
    """후보 `path`를 고르면 패키지가 될 항목을 메타데이터로만 점검한다.

    선택 규칙(`_select_skill_package`)과 reader 관문을 본뜬다. 루트 `SKILL.md`는 기존 경로처럼
    archive 전체를 읽은 뒤 다른 후보 폴더를 뺀다. 사유 코드는 가져오기와 같다 — 가져오기는
    첫 사유에서 멈추지만 요약은 모두 모은다.
    """
    settings = get_settings()
    if path == "SKILL.md":
        scope = [(entry, entry.name) for entry in scan.entries]
        names = [entry.name for entry in scan.entries if entry.kind != "dir"]
        child_roots = {
            name.split("/", 1)[0] for name in _skill_candidate_paths(names) if "/" in name
        }
        root_name = None
    else:
        root = posixpath.dirname(path)
        prefix = f"{root}/"
        scope = [
            (entry, entry.name[len(prefix) :])
            for entry in scan.entries
            if entry.name.startswith(prefix)
        ]
        child_roots = set()
        root_name = posixpath.basename(root)

    errors: set[str] = set()
    seen: set[str] = set()
    selected: list[str] = []
    total = 0
    for entry, local in scope:  # reader가 이 범위에 거는 관문
        if entry.kind == "link":
            errors.add("symlink" if scan.archive_format == "zip" else "unsafe_tar_member")
            continue
        if scan.archive_format == "tar.gz":
            try:
                _check_tar_member_path(local, is_dir=entry.kind == "dir")
            except SkillValidationError as e:
                errors.add(e.reason)
        if entry.kind == "dir":
            continue
        if entry.name in seen:
            errors.add("path_collision")
        seen.add(entry.name)
        total += entry.size
        if entry.size > settings.skill_import_max_file_bytes:
            errors.add("file_too_large")
        if local and entry.name.split("/", 1)[0] not in child_roots:
            selected.append(local)
    if len(seen) > settings.skill_import_max_files:
        errors.add("too_many_files")
    if total > settings.skill_import_max_total_bytes:
        errors.add("package_too_large")

    normalized: set[str] = set()
    for local in selected:  # 선택 뒤의 경로 정규화·충돌·중첩 스킬
        try:
            norm = validate_path(local)
        except SkillValidationError as e:
            errors.add(e.reason)
            continue
        if norm in normalized:
            errors.add("path_collision")
        normalized.add(norm)
    if len(_skill_candidate_paths(selected)) > 1:
        errors.add("multiple_skills")
    return CandidateLayout(len(selected), sorted(errors), root_name)


def _strip_common_root(raw: dict[str, bytes]) -> tuple[dict[str, bytes], str | None]:
    """SKILL.md가 루트에 없고 모든 항목이 같은 최상위 폴더면 그 폴더를 벗긴다.

    벗겨낸 root 이름을 함께 돌려준다 — 표준 strict 검증의
    directory-name/skill-name 일치 검사가 이 값을 쓴다.
    """
    _reject_multiple_skills(raw)
    if "SKILL.md" in raw:
        return raw, None
    tops = {name.split("/", 1)[0] for name in raw}
    if len(tops) == 1:
        root = next(iter(tops))
        prefix = f"{root}/"
        if any(name == f"{prefix}SKILL.md" for name in raw):
            # root와 **같은 이름의 최상위 파일**(항목 이름이 정확히 `root`)은 prefix에
            # 걸리지 않아 예전에는 조용히 사라졌다(리뷰 `2-10`). 사용자가 넣은 파일이
            # 증거 없이 없어지지 않도록 거부한다.
            dropped = [
                name for name in raw if not (name.startswith(prefix) and len(name) > len(prefix))
            ]
            if dropped:
                raise SkillValidationError(
                    "root_shadow_entry",
                    "패키지 root 디렉토리와 이름이 같은 항목이 있어 가져올 수 없습니다: "
                    + ", ".join(sorted(dropped)),
                )
            return (
                {
                    name[len(prefix):]: content
                    for name, content in raw.items()
                    if name.startswith(prefix) and len(name) > len(prefix)
                },
                root,
            )
    return raw, None


# ---------- ZIP export (byte-preserving) ----------


def export_zip(
    files: dict[str, bytes],
    root_name: str,
    *,
    layout: ExportLayout = "wrapped",
    exclude: Collection[str] = (),
) -> bytes:
    """패키지 파일 dict → 표준 디렉터리 구조의 ZIP (D10).

    - **byte-preserving**: 파일 내용을 재작성하지 않는다(hash 불변).
    - `wrapped`는 root 디렉터리(D3: 표준 `name`, 없으면 slug)를 붙이고, `flat`은
      파일을 최상위에 둔다. root는 export 시에만 붙는다 — 저장된 object는 그대로다.
    - `exclude`는 내보내지 않을 경로다(`portable=true`의 제품 매니페스트).
    - 결정적 출력: 경로 정렬 + 고정 타임스탬프·모드로 같은 입력이면 같은 byte.
    - `scripts/` 아래는 0755, 나머지는 0644다.
    """
    if "SKILL.md" not in files:
        raise SkillValidationError("missing_skill_md", "SKILL.md 없이 export할 수 없습니다")
    prefix = ""
    if layout == "wrapped":
        safe_root = validate_path(root_name)
        if "/" in safe_root:
            raise SkillValidationError(
                "bad_export_root",
                f"export root는 단일 디렉토리 이름이어야 합니다: {root_name!r}",
            )
        prefix = f"{safe_root}/"
    elif layout != "flat":
        raise SkillValidationError("bad_export_layout", f"알 수 없는 layout: {layout!r}")

    skipped = set(exclude)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(files):
            if path in skipped:
                continue
            # 저장 경로는 이미 validate_path를 통과한 값이지만, export 입력이
            # 저장소에서 직접 온 경우를 대비해 한 번 더 검증한다.
            rel = validate_path(path)
            info = zipfile.ZipInfo(f"{prefix}{rel}", date_time=EXPORT_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = _ZIP_CREATE_SYSTEM_UNIX
            mode = EXPORT_SCRIPT_MODE if rel.startswith("scripts/") else EXPORT_FILE_MODE
            info.external_attr = (0o100000 | mode) << 16  # 일반 파일 + 권한 비트
            zf.writestr(info, files[path])
    return buf.getvalue()


# ---------- 스토리지 저장 (동기) ----------


def media_type_for_path(path: str) -> str:
    """저장·응답에 공통으로 쓰는 파일의 보수적 media type."""
    mt, _ = mimetypes.guess_type(path)
    return mt or "application/octet-stream"


def save_draft(skill_id: uuid.UUID, files: dict[str, bytes]) -> None:
    """draft 작업본 전체 교체 저장 (기존 prefix 삭제 후 기록)."""
    bucket = _bucket()
    prefix = draft_prefix(skill_id)
    storage.delete_prefix(bucket, prefix)
    for path, data in files.items():
        storage.put_object(bucket, prefix + path, data, media_type_for_path(path))


def load_draft_files(skill_id: uuid.UUID) -> dict[str, bytes]:
    bucket = _bucket()
    prefix = draft_prefix(skill_id)
    files: dict[str, bytes] = {}
    for key in storage.list_objects(bucket, prefix):
        rel = key[len(prefix):]
        if rel:
            files[rel] = storage.get_object(bucket, key)
    return files


def get_draft_file(skill_id: uuid.UUID, path: str) -> bytes:
    rel = validate_path(path)
    return storage.get_object(_bucket(), draft_prefix(skill_id) + rel)


def put_draft_file(skill_id: uuid.UUID, path: str, data: bytes) -> None:
    rel = validate_path(path)
    if len(data) > get_settings().skill_import_max_file_bytes:
        raise SkillValidationError("file_too_large", f"단일 파일 초과: {rel}")
    storage.put_object(
        _bucket(), draft_prefix(skill_id) + rel, data, media_type_for_path(rel)
    )


def delete_draft_file(skill_id: uuid.UUID, path: str) -> None:
    rel = validate_path(path)
    storage.delete_object(_bucket(), draft_prefix(skill_id) + rel)


def delete_draft(skill_id: uuid.UUID) -> None:
    storage.delete_prefix(_bucket(), draft_prefix(skill_id))


def delete_skill_objects(skill_id: uuid.UUID, version_ids: Collection[uuid.UUID]) -> bool:
    """삭제가 커밋된 스킬의 draft와 모든 version prefix를 지운다(#151).

    DB 삭제를 커밋한 **뒤에** 부른다 — 실패해도 남는 것은 회수 가능한 고아 오브젝트다
    (`skills.orphans`). 예외를 올리지 않고 실패한 prefix마다 경고를 남긴다. 전부 지웠으면 True.
    """
    bucket = _bucket()
    prefixes = [draft_prefix(skill_id), *(version_prefix(v) for v in version_ids)]
    ok = True
    for prefix in prefixes:
        try:
            failed = storage.delete_prefix(bucket, prefix)
        except Exception as e:
            ok = False
            logger.warning(
                "skill.delete.objects_failed",
                skill_id=str(skill_id),
                prefix=prefix,
                error=type(e).__name__,
            )
            continue
        if failed:
            ok = False
            logger.warning(
                "skill.delete.objects_failed",
                skill_id=str(skill_id),
                prefix=prefix,
                failed_keys=failed[:20],
                failed_count=len(failed),
            )
    return ok


def freeze_draft_to_version(skill_id: uuid.UUID, version_id: uuid.UUID) -> None:
    """draft prefix 트리를 version prefix로 서버사이드 복사(불변 동결)."""
    bucket = _bucket()
    src = draft_prefix(skill_id)
    dst = version_prefix(version_id)
    for key in storage.list_objects(bucket, src):
        rel = key[len(src):]
        if rel:
            storage.copy_object(bucket, key, dst + rel)


def load_version_files(version_id: uuid.UUID) -> dict[str, bytes]:
    bucket = _bucket()
    prefix = version_prefix(version_id)
    files: dict[str, bytes] = {}
    for key in storage.list_objects(bucket, prefix):
        rel = key[len(prefix):]
        if rel:
            files[rel] = storage.get_object(bucket, key)
    return files


def get_version_file(version_id: uuid.UUID, path: str) -> bytes:
    rel = validate_path(path)
    return storage.get_object(_bucket(), version_prefix(version_id) + posixpath.normpath(rel))
