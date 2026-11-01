"""로컬 스토리지 서명 URL 엔드포인트 — 스킬 샌드박스 워커가 파일을 받고 올리는 유일한 길.

샌드박스 워커는 데이터 볼륨을 마운트하지 않고 자격 증명도 없다. 에이전트 워커가 작업마다
`storage.presigned_url_for`로 토큰 URL을 만들어 보내면, 샌드박스는 그 URL로만 GET·PUT한다.
토큰은 객체 하나·메서드 하나·만료 시각(PUT은 본문 상한까지)을 HMAC으로 묶는다.

- 로컬 백엔드(`storage_backend=local`)일 때만 응답한다. s3면 샌드박스가 서버 presigned URL을
  쓰므로 404다.
- 버킷은 메서드마다 정해 둔다: GET은 스킬 패키지·산출물, PUT은 산출물뿐이다.
- 실패 이유(서명·만료·메서드)는 응답에 싣지 않는다 — 403 하나다.
- OpenAPI에 싣지 않는다(내부 경로). 제품은 이 라우터를 접두사 없이 붙인다.
"""

import anyio
import structlog
from fastapi import APIRouter, HTTPException, Request, Response, status

from biznotes_core.api.object_stream import ObjectStreamResponse
from biznotes_core.config import get_settings
from biznotes_core.services import storage

logger = structlog.get_logger()

router = APIRouter(include_in_schema=False)

_PATH = storage.INTERNAL_STORAGE_PATH + "{token}"


def _verify(token: str, method: str) -> storage.SignedObject:
    settings = get_settings()
    if settings.storage_backend != "local":
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    try:
        signed = storage.verify_object_token(token, method)
    except storage.InvalidObjectToken as e:
        logger.warning("internal_storage.token_rejected", method=method, reason=str(e))
        raise HTTPException(status.HTTP_403_FORBIDDEN) from None
    allowed = (
        {settings.storage_bucket_skill_packages, settings.storage_bucket_artifacts}
        if method == "GET"
        else {settings.storage_bucket_artifacts}
    )
    if signed.bucket not in allowed:
        logger.warning("internal_storage.bucket_rejected", method=method, bucket=signed.bucket)
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    return signed


@router.get(_PATH)
async def get_signed_object(token: str) -> Response:
    signed = _verify(token, "GET")
    try:
        stream = await anyio.to_thread.run_sync(
            lambda: storage.open_object_stream(signed.bucket, signed.key)
        )
    except storage.ObjectNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND) from None
    return ObjectStreamResponse(stream, media_type="application/octet-stream")


@router.put(_PATH)
async def put_signed_object(token: str, request: Request) -> Response:
    signed = _verify(token, "PUT")
    limit = signed.max_bytes or 0
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE)
    data = bytes(body)
    await anyio.to_thread.run_sync(
        lambda: storage.put_object(signed.bucket, signed.key, data, "application/octet-stream")
    )
    return Response(status_code=status.HTTP_200_OK)
