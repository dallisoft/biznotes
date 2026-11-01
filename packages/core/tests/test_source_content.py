"""`GET /sources/{id}/content` — 파싱 markdown 전문 조회 (#2).

상세 조회의 `parsed_preview`는 앞 20,000자로 잘리므로 긴 문서의 뒷부분을 볼
경로가 이 엔드포인트뿐이다. DB와 S3 백엔드의 minio 클라이언트는 가짜로 대체하고 라우터와
스토리지 스트리밍(`open_object_stream`)은 실제 코드를 탄다.
"""

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import ClientDisconnect

from biznotes_core.api.deps import get_current_user
from biznotes_core.api.v1 import sources as sources_api
from biznotes_core.core.db import get_db
from biznotes_core.models import Notebook, Source, User
from biznotes_core.services import storage


class _FakeObject:
    """minio `get_object` 응답(urllib3 HTTPResponse) 대역 — 읽기 방식과 연결 반납을 기록한다."""

    def __init__(self, data: bytes, state: dict[str, Any]) -> None:
        self._data = data
        self._state = state
        self.headers = {"Content-Length": str(len(data))}

    def stream(self, amt: int) -> Any:
        self._state["db_closed_before_stream"] = self._state["db"].closed
        for i in range(0, len(self._data), amt):
            chunk = self._data[i : i + amt]
            self._state["chunks"].append(len(chunk))
            yield chunk

    def read(self) -> bytes:
        self._state["full_reads"] += 1
        return self._data

    def close(self) -> None:
        self._state["closed"] = True

    def release_conn(self) -> None:
        self._state["released"] = True


class _FakeDB:
    def __init__(self, *objs: Any) -> None:
        self._objs = {(type(o), o.id): o for o in objs}
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def get(self, model: type, ident: uuid.UUID) -> Any:
        return self._objs.get((model, ident))


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, use_s3_client: Any) -> dict[str, Any]:
    user = User(id=uuid.uuid4(), email="u@test.dev", name="u", role="user")
    nb = Notebook(id=uuid.uuid4(), owner_id=user.id, title="nb")
    source = Source(
        id=uuid.uuid4(),
        notebook_id=nb.id,
        type="file",
        title="big.pdf",
        status="ready",
        parsed_object_key="k/parsed.md",
    )
    state: dict[str, Any] = {
        "user": user,
        "source": source,
        "db": _FakeDB(nb, source),
        "perm": "read",
        "objects": {},
        "chunks": [],
        "full_reads": 0,
        "closed": False,
        "released": False,
    }

    async def fake_permission(db: Any, notebook: Notebook, u: User) -> str | None:
        return state["perm"]

    class _FakeMinio:
        def get_object(self, bucket: str, key: str) -> _FakeObject:
            reader = state.get("get_object")
            data = reader(bucket, key) if reader is not None else state["objects"][key]
            return _FakeObject(data, state)

    monkeypatch.setattr(sources_api, "get_permission", fake_permission)
    use_s3_client(_FakeMinio())
    return state


@pytest.fixture
async def client(world: dict[str, Any]):
    app = FastAPI()
    app.include_router(sources_api.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: world["user"]
    app.dependency_overrides[get_db] = lambda: world["db"]
    # 한도는 Redis가 필요하다 — 코어 테스트는 Redis를 띄우지 않는다(연결 여부는 따로 검증)
    app.dependency_overrides[sources_api._source_content_limiter] = lambda: None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_content_returns_full_markdown_beyond_preview_limit(client, world):
    body = "# 제목\n\n" + "가" * 30_000 + "\n\n## 마지막 절\n\n끝부분"
    world["objects"]["k/parsed.md"] = body.encode("utf-8")

    res = await client.get(f"/api/v1/sources/{world['source'].id}/content")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/markdown")
    assert res.text == body  # 20,000자 상한 없이 전문
    assert "끝부분" in res.text


async def test_content_404_when_not_parsed_yet(client, world):
    world["source"].parsed_object_key = None

    res = await client.get(f"/api/v1/sources/{world['source'].id}/content")

    assert res.status_code == 404
    assert res.json()["detail"] == "파싱된 본문이 없습니다"


async def test_content_404_without_notebook_permission(client, world):
    world["perm"] = None
    world["objects"]["k/parsed.md"] = b"secret"

    res = await client.get(f"/api/v1/sources/{world['source'].id}/content")

    assert res.status_code == 404
    assert "secret" not in res.text


async def test_content_404_for_unknown_source(client):
    res = await client.get(f"/api/v1/sources/{uuid.uuid4()}/content")

    assert res.status_code == 404


async def test_content_503_when_storage_fails(client, world):
    # objects가 비어 있으면 fake_get_object가 KeyError — 스토리지 장애를 흉내낸다
    res = await client.get(f"/api/v1/sources/{world['source'].id}/content")

    assert res.status_code == 503


async def test_content_404_when_object_is_gone(client, world):
    """오브젝트가 지워진 것은 기다려도 생기지 않는다 — 재시도 가능한 503이 아니라 404."""
    from minio.error import S3Error

    def gone(bucket: str, key: str) -> bytes:
        raise S3Error(
            response=None,
            code="NoSuchKey",
            message="not found",
            resource=key,
            request_id="rid",
            host_id="hid",
        )

    world["get_object"] = gone

    res = await client.get(f"/api/v1/sources/{world['source'].id}/content")

    assert res.status_code == 404


async def test_content_read_does_not_block_the_event_loop(client, world):
    """동기 스토리지 읽기는 스레드로 보낸다 — 루프에서 직접 부르면 다른 요청이 멈춘다."""
    import threading

    world["objects"]["k/parsed.md"] = b"body"
    seen: list[str] = []
    loop_thread = threading.current_thread().name

    def record(bucket: str, key: str) -> bytes:
        seen.append(threading.current_thread().name)
        return world["objects"][key]

    world["get_object"] = record

    res = await client.get(f"/api/v1/sources/{world['source'].id}/content")

    assert res.status_code == 200
    assert seen and seen[0] != loop_thread


async def test_content_is_streamed_in_chunks_not_loaded_whole(client, world):
    """#26 — 전문을 한 번에 읽지 않고 청크로 흘려보내며, 끝나면 연결을 반납한다."""
    body = ("# 큰 문서\n\n" + "나" * 100_000).encode("utf-8")
    world["objects"]["k/parsed.md"] = body

    res = await client.get(f"/api/v1/sources/{world['source'].id}/content")

    assert res.status_code == 200
    assert res.content == body
    assert res.headers["content-length"] == str(len(body))
    assert res.headers["content-type"].startswith("text/markdown")
    assert world["full_reads"] == 0
    assert len(world["chunks"]) > 1
    assert world["closed"] is True and world["released"] is True


async def test_content_releases_the_db_session_before_streaming(client, world):
    """리뷰 지적 — 요청 스코프 세션은 전송이 끝나야 정리되므로 스트리밍 전에 반납한다."""
    world["objects"]["k/parsed.md"] = b"body"

    res = await client.get(f"/api/v1/sources/{world['source'].id}/content")

    assert res.status_code == 200
    assert world["db_closed_before_stream"] is True


@pytest.mark.parametrize("failure", [OSError("client gone"), asyncio.CancelledError()])
async def test_stream_is_closed_when_the_transfer_breaks(world, failure):
    """리뷰 지적 — 클라이언트가 끊겨도(전송 오류·취소) S3 연결을 GC에 맡기지 않는다."""
    world["objects"]["k"] = b"x" * 200_000
    stream = storage.open_object_stream("bucket", "k", chunk_size=1024)
    response = sources_api._ObjectStreamResponse(stream, media_type="text/markdown")
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        await asyncio.sleep(3600)  # 연결 끊김 신호는 send 실패로만 흉내낸다
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)
        if message["type"] == "http.response.body":
            raise failure

    scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
    # Starlette는 전송 중 OSError를 ClientDisconnect로 바꿔 올린다
    with pytest.raises((ClientDisconnect, asyncio.CancelledError)):
        await response(scope, receive, send)

    assert world["closed"] is True and world["released"] is True
    assert len(world["chunks"]) < 200  # 끝까지 읽지 않았다


async def test_stream_is_closed_on_disconnect_message(world):
    """uvicorn이 보고하는 ASGI 2.3 경로 — `http.disconnect`를 받으면 전송 태스크가 취소된다."""
    world["objects"]["k"] = b"x" * 200_000
    stream = storage.open_object_stream("bucket", "k", chunk_size=1024)
    response = sources_api._ObjectStreamResponse(stream, media_type="text/markdown")

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        await asyncio.sleep(0.01)  # 느린 클라이언트 — 끊김 신호가 먼저 처리되게 한다

    await response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)

    assert world["closed"] is True and world["released"] is True
    assert len(world["chunks"]) < 200


def test_content_route_has_its_own_user_rate_limit():
    """#26 — 인증 사용자 단위 전용 한도(`rate_limit_source_content`, 0 이하면 비활성)."""
    from biznotes_core.config import CoreSettings
    from biznotes_core.core.rate_limit import RateLimiter

    (route,) = [
        r
        for r in sources_api.router.routes
        if getattr(r, "path", None) == "/sources/{source_id}/content"
    ]
    limiters = [
        d.call
        for d in route.dependant.dependencies  # type: ignore[attr-defined]
        if isinstance(d.call, RateLimiter)
    ]
    assert [(lim.key, lim.scope, lim.limit_setting) for lim in limiters] == [
        ("source-content", "user", "rate_limit_source_content")
    ]
    assert CoreSettings.model_fields["rate_limit_source_content"].default == 60
