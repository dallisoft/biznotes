"""오브젝트 스트림을 HTTP 응답으로 흘려보내는 응답 클래스 — 소스 본문 조회·내부 스토리지."""

from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from biznotes_core.services.storage import ObjectStream


class ObjectStreamResponse(StreamingResponse):
    """오브젝트 스트림을 흘려보내고, 전송이 어떻게 끝나든(완료·끊김·전송 오류) 닫는다.

    Starlette는 클라이언트가 끊겨도 동기 이터레이터를 닫지 않는다 — 그대로 두면 파일 핸들·
    S3 연결 반납이 GC 시점까지 미뤄진다. 닫기는 핸들 close·풀 반납뿐이라 루프에서 바로
    부른다(취소 중에도 건너뛰지 않도록 await하지 않는다).
    """

    def __init__(self, stream: ObjectStream, media_type: str) -> None:
        headers = {"Content-Length": str(stream.length)} if stream.length is not None else None
        super().__init__(stream.chunks(), media_type=media_type, headers=headers)
        self._stream = stream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._stream.close()
