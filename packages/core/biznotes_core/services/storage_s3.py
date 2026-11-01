"""S3 호환 스토리지 백엔드(`storage_backend=s3`) — minio 클라이언트를 쓴다(`biznotes-core[s3]`).

RustFS·SeaweedFS·MinIO처럼 S3 API를 말하는 서버면 된다. 호출부는 이 모듈을 직접 쓰지 않고
`services/storage.py`의 함수를 쓴다.
"""

import io
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

import structlog
from minio import Minio
from minio.error import S3Error

from biznotes_core.config import get_settings
from biznotes_core.services.storage import ObjectNotFound, ObjectStream

logger = structlog.get_logger()

_MISSING_CODES = frozenset({"NoSuchKey", "NoSuchBucket"})


@lru_cache
def get_minio() -> Minio:
    s = get_settings()
    return Minio(
        s.s3_endpoint, access_key=s.s3_access_key, secret_key=s.s3_secret_key, secure=s.s3_secure
    )


@lru_cache
def _signer(endpoint: str, region: str) -> Minio:
    """다른 호스트명으로 서명하는 클라이언트 — 서명만 하고 요청은 보내지 않는다.

    region을 주면 minio가 버킷 위치를 묻는 요청을 보내지 않는다. 받는 쪽(샌드박스 워커)이
    이 호스트명으로 접속해야 SigV4 host가 맞는다(표준 계획 D7, R2-18).
    """
    s = get_settings()
    return Minio(
        endpoint,
        access_key=s.s3_access_key,
        secret_key=s.s3_secret_key,
        secure=s.s3_secure,
        region=region,
    )


def _found[T](key: str, call: Callable[[], T]) -> T:
    """없는 오브젝트·버킷의 `S3Error`를 백엔드 공통 `ObjectNotFound`로 바꾼다."""
    try:
        return call()
    except S3Error as e:
        if e.code in _MISSING_CODES:
            raise ObjectNotFound(key, e.code) from e
        raise


def _close(res: Any) -> None:
    res.close()
    res.release_conn()


class S3Storage:
    def ensure_buckets(self, buckets: tuple[str, ...]) -> None:
        client = get_minio()
        for bucket in buckets:
            if not client.bucket_exists(bucket):
                client.make_bucket(bucket)

    def put(self, bucket: str, key: str, data: bytes, content_type: str) -> None:
        """업로드 — 일시 오류(네트워크/락) 대비 0.5초 백오프 1회 재시도."""
        for attempt in range(2):
            try:
                get_minio().put_object(
                    bucket, key, io.BytesIO(data), length=len(data), content_type=content_type
                )
                return
            except Exception:
                if attempt == 1:
                    raise
                logger.warning("s3 put_object 실패 — 재시도", bucket=bucket, key=key)
                time.sleep(0.5)

    def get(self, bucket: str, key: str) -> bytes:
        res = _found(key, lambda: get_minio().get_object(bucket, key))
        try:
            return bytes(res.read())
        finally:
            _close(res)

    def open(self, bucket: str, key: str, chunk_size: int) -> ObjectStream:
        res = _found(key, lambda: get_minio().get_object(bucket, key))
        length = res.headers.get("Content-Length")
        return ObjectStream(
            res.stream(chunk_size),
            lambda: _close(res),
            int(length) if length is not None else None,
        )

    def size(self, bucket: str, key: str) -> int:
        return _found(key, lambda: get_minio().stat_object(bucket, key)).size or 0

    def delete(self, bucket: str, key: str) -> None:
        get_minio().remove_object(bucket, key)

    def scan(self, bucket: str, prefix: str) -> list[tuple[str, datetime | None]]:
        client = get_minio()
        if not client.bucket_exists(bucket):
            return []
        return [
            (obj.object_name, obj.last_modified)
            for obj in client.list_objects(bucket, prefix=prefix, recursive=True)
            if obj.object_name is not None
        ]

    def copy(self, bucket: str, src_key: str, dst_key: str) -> None:
        """서버사이드 복사."""
        from minio.commonconfig import CopySource

        source = CopySource(bucket, src_key)
        _found(src_key, lambda: get_minio().copy_object(bucket, dst_key, source))

    def delete_many(self, bucket: str, keys: list[str]) -> list[str]:
        """한 번의 일괄 요청으로 삭제. 서버가 거부한 key 목록을 반환한다."""
        client = get_minio()
        if not client.bucket_exists(bucket):
            return []
        from minio.deleteobjects import DeleteObject

        objects = [DeleteObject(key) for key in dict.fromkeys(keys)]
        # remove_objects는 lazy generator — 결과를 소비해야 실제 삭제 수행
        return [error.name or error.code for error in client.remove_objects(bucket, objects)]

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
        """`endpoint` 호스트명으로 서명한 GET·PUT URL. 본문 상한은 서명에 담지 않는다."""
        return _signer(endpoint, region).get_presigned_url(method, bucket, key, expires=expires)
