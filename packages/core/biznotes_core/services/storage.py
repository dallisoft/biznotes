"""오브젝트 스토리지 — 버킷·키로 바이트를 넣고 꺼낸다.

백엔드는 둘이다(설정 `storage_backend`).

- `local`(기본): `data_dir/<버킷>/<키>` 파일(`BIZNOTES_DATA_DIR`). api·worker가 같은 경로(볼륨)를
  본다. 셀프호스트 단일 노드용이다.
- `s3`(선택): S3 호환 서버(RustFS·SeaweedFS·MinIO 등). minio 클라이언트가 필요하다
  (`biznotes-core[s3]`, `services/storage_s3.py`).

호출부는 아래 모듈 함수만 쓴다 — 백엔드를 바꿔도 호출부는 그대로다. 키는 `/`로 나눈 상대
경로다. 로컬 백엔드는 절대 경로·`..`·`.`·빈 성분·역슬래시·NUL을 거부한다(`InvalidObjectKey`).
없는 오브젝트는 두 백엔드 모두 `ObjectNotFound`(`code`는 S3 오류 코드)다.

샌드박스 워커(스킬 스크립트)는 볼륨을 보지 않고 서명 URL로만 파일을 주고받는다
(`presigned_url_for`). s3는 서버의 presigned URL이고, local은 api 내부 엔드포인트
(`/internal/storage/<토큰>`, `api/internal_storage.py`)를 가리키는 HMAC 토큰 URL이다 —
토큰 하나가 객체 하나·메서드 하나·만료 시각을 묶는다.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import structlog

from biznotes_core.config import get_settings

logger = structlog.get_logger()

#: 서명 URL이 api에서 쓰는 경로 접두사(`api/internal_storage.py`)
INTERNAL_STORAGE_PATH = "/internal/storage/"


class ObjectNotFound(FileNotFoundError):
    """오브젝트(또는 버킷)가 없다. `code`는 S3 오류 코드다 — 호출부는 `code`로 가린다."""

    def __init__(self, key: str, code: str = "NoSuchKey") -> None:
        super().__init__(key)
        self.code = code


class InvalidObjectKey(ValueError):
    """로컬 파일 경로로 옮길 수 없는 버킷·키 — 경로 탈출을 막는다."""


class ObjectStream:
    """열린 오브젝트 본문 — `chunks()`로 읽고, 어떻게 끝나든 `close()`로 자원을 반납한다.

    `chunks()`가 끝까지 돌면 스스로 닫는다. 하지만 HTTP 응답으로 흘려보내다 클라이언트가
    끊으면 Starlette는 동기 이터레이터를 닫지 않아 `finally`가 GC 때까지 미뤄진다 — 그래서
    호출자가 전송이 끝난 뒤 `close()`를 직접 부른다. 두 경로가 겹칠 수 있어 멱등이다.
    """

    def __init__(
        self, chunks: Iterator[bytes], release: Callable[[], None], length: int | None
    ) -> None:
        self._chunks = chunks
        self._release = release
        self._lock = threading.Lock()
        self._closed = False
        self.length = length

    def chunks(self) -> Iterator[bytes]:
        try:
            yield from self._chunks
        finally:
            self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._release()


class StorageBackend(Protocol):
    def ensure_buckets(self, buckets: tuple[str, ...]) -> None: ...

    def put(self, bucket: str, key: str, data: bytes, content_type: str) -> None: ...

    def get(self, bucket: str, key: str) -> bytes: ...

    def open(self, bucket: str, key: str, chunk_size: int) -> ObjectStream: ...

    def size(self, bucket: str, key: str) -> int: ...

    def delete(self, bucket: str, key: str) -> None: ...

    def scan(self, bucket: str, prefix: str) -> list[tuple[str, datetime | None]]: ...

    def copy(self, bucket: str, src_key: str, dst_key: str) -> None: ...

    def delete_many(self, bucket: str, keys: list[str]) -> list[str]: ...

    def presigned_url(
        self,
        method: str,
        bucket: str,
        key: str,
        *,
        expires: timedelta,
        endpoint: str,
        region: str,
        max_bytes: int | None,
    ) -> str: ...


# ---------- 로컬 파일 백엔드 ----------

_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}$")
# 임시 파일 자리 — 버킷 이름은 `.`으로 시작할 수 없어 버킷과 겹치지 않는다
_TMP_DIR = ".tmp"


def _check_bucket(bucket: str) -> str:
    if not isinstance(bucket, str) or not _BUCKET_NAME.match(bucket) or ".." in bucket:
        raise InvalidObjectKey(f"버킷 이름이 아닙니다: {bucket!r}")
    return bucket


def _check_text(value: str) -> None:
    if not isinstance(value, str) or "\x00" in value or "\\" in value or value.startswith("/"):
        raise InvalidObjectKey(f"쓸 수 없는 키입니다: {value!r}")


def key_parts(key: str) -> list[str]:
    """키를 경로 성분으로 — 절대 경로·`..`·`.`·빈 성분·역슬래시·NUL이면 `InvalidObjectKey`."""
    _check_text(key)
    parts = key.split("/")
    if not key or any(p in ("", ".", "..") for p in parts):
        raise InvalidObjectKey(f"쓸 수 없는 키입니다: {key!r}")
    return parts


def _prefix_dir_parts(prefix: str) -> list[str]:
    """접두사가 확정하는 디렉터리 성분 — S3 접두사는 문자열이라 마지막 성분은 미완일 수 있다."""
    if prefix == "":
        return []
    _check_text(prefix)
    head = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
    if not head:
        return []
    parts = head.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise InvalidObjectKey(f"쓸 수 없는 접두사입니다: {prefix!r}")
    return parts


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


class LocalStorage:
    """`root/<버킷>/<키>` 파일. 쓰기는 임시 파일에 쓴 뒤 `os.replace`로 바꿔 끼운다(원자적)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _bucket_dir(self, bucket: str) -> Path:
        return self.root / _check_bucket(bucket)

    def _path(self, bucket: str, key: str) -> Path:
        base = self._bucket_dir(bucket)
        path = base.joinpath(*key_parts(key))
        # 성분 검사로 충분하지만, 볼륨 안에 누가 심볼릭 링크를 두었을 때를 막는다
        if not os.path.realpath(path).startswith(os.path.realpath(base) + os.sep):
            raise InvalidObjectKey(f"버킷 밖을 가리키는 키입니다: {key!r}")
        return path

    def _write(self, path: Path, write: Callable[[int], None]) -> None:
        tmp_dir = self.root / _TMP_DIR
        tmp_dir.mkdir(parents=True, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=tmp_dir)
        try:
            write(fd)
            os.close(fd)
            fd = -1
            os.replace(tmp, path)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            Path(tmp).unlink(missing_ok=True)
            raise

    def ensure_buckets(self, buckets: tuple[str, ...]) -> None:
        for bucket in buckets:
            self._bucket_dir(bucket).mkdir(parents=True, exist_ok=True)

    def put(self, bucket: str, key: str, data: bytes, content_type: str) -> None:
        self._write(self._path(bucket, key), lambda fd: _write_all(fd, data))

    def get(self, bucket: str, key: str) -> bytes:
        try:
            return self._path(bucket, key).read_bytes()
        except (FileNotFoundError, NotADirectoryError) as e:
            raise ObjectNotFound(key) from e

    def open(self, bucket: str, key: str, chunk_size: int) -> ObjectStream:
        try:
            f = self._path(bucket, key).open("rb")
        except (FileNotFoundError, NotADirectoryError) as e:
            raise ObjectNotFound(key) from e
        length = os.fstat(f.fileno()).st_size
        return ObjectStream(iter(lambda: f.read(chunk_size), b""), f.close, length)

    def size(self, bucket: str, key: str) -> int:
        try:
            return self._path(bucket, key).stat().st_size
        except (FileNotFoundError, NotADirectoryError) as e:
            raise ObjectNotFound(key) from e

    def delete(self, bucket: str, key: str) -> None:
        path = self._path(bucket, key)
        try:
            path.unlink()
        except (FileNotFoundError, NotADirectoryError):
            return  # 없는 키 삭제는 오류가 아니다(S3 의미)
        base = self._bucket_dir(bucket)
        parent = path.parent
        # 빈 디렉터리를 버킷 루트까지 걷어낸다 — 동시에 다른 키가 생기면 rmdir이 실패하고 멈춘다
        while parent != base:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def scan(self, bucket: str, prefix: str) -> list[tuple[str, datetime | None]]:
        base = self._bucket_dir(bucket)
        start = base.joinpath(*_prefix_dir_parts(prefix))
        if not start.is_dir():
            return []
        out: list[tuple[str, datetime | None]] = []
        for dirpath, _dirs, files in os.walk(start):
            rel = Path(dirpath).relative_to(base).as_posix()
            for name in files:
                key = name if rel == "." else f"{rel}/{name}"
                if not key.startswith(prefix):
                    continue
                try:
                    mtime = os.stat(os.path.join(dirpath, name)).st_mtime
                except FileNotFoundError:
                    continue  # 순회 도중 지워졌다
                out.append((key, datetime.fromtimestamp(mtime, UTC)))
        out.sort(key=lambda item: item[0])
        return out

    def copy(self, bucket: str, src_key: str, dst_key: str) -> None:
        src = self._path(bucket, src_key)
        dst = self._path(bucket, dst_key)
        try:
            f = src.open("rb")
        except (FileNotFoundError, NotADirectoryError) as e:
            raise ObjectNotFound(src_key) from e

        def copy_into(fd: int) -> None:
            while chunk := f.read(1 << 20):
                _write_all(fd, chunk)

        with f:
            self._write(dst, copy_into)

    def delete_many(self, bucket: str, keys: list[str]) -> list[str]:
        failed: list[str] = []
        for key in dict.fromkeys(keys):
            try:
                self.delete(bucket, key)
            except (OSError, InvalidObjectKey):
                failed.append(key)
        return failed

    def presigned_url(
        self,
        method: str,
        bucket: str,
        key: str,
        *,
        expires: timedelta,
        endpoint: str,
        region: str,
        max_bytes: int | None,
    ) -> str:
        key_parts(key)
        token = sign_object_token(method, _check_bucket(bucket), key, expires, max_bytes=max_bytes)
        return endpoint.rstrip("/") + INTERNAL_STORAGE_PATH + token


# ---------- 서명 URL 토큰(local) ----------


@dataclass(frozen=True)
class SignedObject:
    method: str
    bucket: str
    key: str
    max_bytes: int | None


class InvalidObjectToken(Exception):
    """서명·만료·메서드·모양이 맞지 않는 토큰 — 이유는 응답에 싣지 않는다(로그에만)."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _token_key() -> bytes:
    # JWT 서명 키에서 용도별 키를 파생한다 — 같은 키로 서로 다른 형식에 서명하지 않는다
    secret = get_settings().jwt_secret.encode()
    return hmac.new(secret, b"biznotes-core/storage-url/v1", hashlib.sha256).digest()


def sign_object_token(
    method: str,
    bucket: str,
    key: str,
    expires: timedelta,
    *,
    max_bytes: int | None = None,
    now: float | None = None,
) -> str:
    """객체 하나·메서드 하나·만료 시각을 묶은 토큰. PUT은 본문 상한(`max_bytes`)이 필수다."""
    if method not in ("GET", "PUT"):
        raise ValueError(f"서명할 수 없는 메서드입니다: {method}")
    if method == "PUT" and (max_bytes is None or max_bytes < 0):
        raise ValueError("PUT 토큰에는 본문 상한이 필요합니다")
    exp = int((time.time() if now is None else now) + expires.total_seconds())
    claims: dict[str, object] = {"m": method, "b": bucket, "k": key, "e": exp}
    if method == "PUT":
        claims["n"] = max_bytes
    body = _b64(json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode())
    sig = _b64(hmac.new(_token_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_object_token(token: str, method: str, *, now: float | None = None) -> SignedObject:
    """토큰 검증 — 서명·메서드·만료·버킷·키 모양. 하나라도 어긋나면 `InvalidObjectToken`."""
    body, dot, sig = token.partition(".")
    if not dot or not body or not sig or len(token) > 8192:
        raise InvalidObjectToken("shape")
    expected = _b64(hmac.new(_token_key(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise InvalidObjectToken("signature")
    try:
        claims = json.loads(_unb64(body))
    except ValueError as e:
        raise InvalidObjectToken("payload") from e
    if not isinstance(claims, dict):
        raise InvalidObjectToken("payload")
    m, bucket, key, exp, n = (claims.get(k) for k in ("m", "b", "k", "e", "n"))
    if m != method:
        raise InvalidObjectToken("method")
    if isinstance(exp, bool) or not isinstance(exp, int):
        raise InvalidObjectToken("payload")
    if (time.time() if now is None else now) > exp:
        raise InvalidObjectToken("expired")
    if not isinstance(bucket, str) or not isinstance(key, str):
        raise InvalidObjectToken("payload")
    if method == "PUT" and (isinstance(n, bool) or not isinstance(n, int) or n < 0):
        raise InvalidObjectToken("payload")
    try:
        _check_bucket(bucket)
        key_parts(key)
    except InvalidObjectKey as e:
        raise InvalidObjectToken("key") from e
    return SignedObject(method, bucket, key, n if method == "PUT" else None)


# ---------- 백엔드 선택 ----------


@lru_cache
def get_backend() -> StorageBackend:
    settings = get_settings()
    if settings.storage_backend == "s3":
        from biznotes_core.services.storage_s3 import S3Storage

        return S3Storage()
    return LocalStorage(settings.data_dir)


def _buckets() -> tuple[str, ...]:
    s = get_settings()
    return (
        s.storage_bucket_sources,
        s.storage_bucket_parsed,
        s.storage_bucket_artifacts,
        s.storage_bucket_skill_packages,
    )


# ---------- 호출부가 쓰는 함수 ----------


def ensure_buckets() -> None:
    get_backend().ensure_buckets(_buckets())


def put_object(
    bucket: str, key: str, data: bytes, content_type: str = "application/octet-stream"
) -> None:
    get_backend().put(bucket, key, data, content_type)


def get_object(bucket: str, key: str) -> bytes:
    return get_backend().get(bucket, key)


def open_object_stream(bucket: str, key: str, chunk_size: int = 64 * 1024) -> ObjectStream:
    """오브젝트를 열어 스트림을 돌려준다 — 본문 전체를 메모리에 올리지 않는다.

    여는 단계의 오류(없음 → `ObjectNotFound`)는 이 호출에서 바로 올라와, 호출자가 응답을
    시작하기 전에 처리할 수 있다.
    """
    return get_backend().open(bucket, key, chunk_size)


def stat_object_size(bucket: str, key: str) -> int:
    """오브젝트 바이트 크기만 조회 — 본문 전체를 내려받지 않고 상한 검사에 사용."""
    return get_backend().size(bucket, key)


def delete_object(bucket: str, key: str) -> None:
    get_backend().delete(bucket, key)


def presigned_url_for(
    method: str,
    bucket: str,
    key: str,
    *,
    expires: timedelta,
    endpoint: str,
    region: str,
    max_bytes: int | None = None,
) -> str:
    """샌드박스가 `endpoint`로 닿아 쓰는 GET·PUT URL. PUT의 `max_bytes`는 local이 강제한다."""
    return get_backend().presigned_url(
        method,
        bucket,
        key,
        expires=expires,
        endpoint=endpoint,
        region=region,
        max_bytes=max_bytes,
    )


def list_objects(bucket: str, prefix: str) -> list[str]:
    """prefix 하위 오브젝트 key 목록 (재귀). prefix는 S3처럼 문자열 접두사다."""
    return [key for key, _ in get_backend().scan(bucket, prefix)]


def copy_object(bucket: str, src_key: str, dst_key: str) -> None:
    """버킷 내 오브젝트 복사 (draft → version 동결용)."""
    get_backend().copy(bucket, src_key, dst_key)


def list_object_times(bucket: str, prefix: str) -> list[tuple[str, datetime | None]]:
    """prefix 하위 (key, 마지막 수정 시각) 목록 (재귀). 고아 정리의 유예 판정용."""
    return get_backend().scan(bucket, prefix)


def delete_prefix(bucket: str, prefix: str) -> list[str]:
    """prefix 하위 모든 오브젝트 삭제. 지우지 못한 key 목록을 반환한다.

    개별 삭제 실패는 예외가 아니라 결과로 온다 — 지금까지처럼 무시하려면 반환값을 버린다.
    """
    keys = list_objects(bucket, prefix)
    if not keys:
        return []
    return get_backend().delete_many(bucket, keys)


def delete_objects(bucket: str, keys: list[str]) -> list[str]:
    """여러 오브젝트를 한 번에 삭제. 지우지 못한 key 목록을 반환한다.

    없는 key는 오류가 아니다(S3 의미). 버킷이 없으면 지울 것도 없다.
    """
    if not keys:
        return []
    return get_backend().delete_many(bucket, keys)
