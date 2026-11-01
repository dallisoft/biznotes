"""agent-run 이벤트 구독 준비의 상한 (#154 C).

Pub/Sub 풀은 read timeout이 없어(`socket_timeout=None`) TCP는 받지만 응답하지 않는
서버(half-open) 앞에서 무기한 기다렸다. 실제 Redis 없이 두 단계를 따로 재현한다.

- 핸드셰이크 단계: 아무 응답도 하지 않는 서버(연결 직후 `CLIENT SETINFO` 응답 대기에서 멈춘다)
- ACK 단계: 핸드셰이크 명령에는 `+OK`, `SUBSCRIBE`는 삼키는 서버

redis-py는 코어 락 8.1.0(블로킹 read에 무한을 넘긴다)이고, 두 제품 락 8.0.0은 풀의
`socket_timeout=None`으로 같은 무한 대기가 된다. `asyncio.timeout`은 버전과 무관하다.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace

import pytest
import structlog.testing
from redis.asyncio import Redis as AsyncRedis

from biznotes_core.services import events

LIMIT = 0.3


def _commands(buffer: bytearray) -> list[str]:
    """버퍼에서 완결된 RESP 배열 명령의 첫 단어를 꺼낸다(나머지는 버퍼에 남긴다)."""
    found: list[str] = []
    while True:
        data = bytes(buffer)
        if not data.startswith(b"*") or b"\r\n" not in data:
            return found
        head, rest = data.split(b"\r\n", 1)
        parts: list[bytes] = []
        for _ in range(int(head[1:])):
            if b"\r\n" not in rest:
                return found
            length_line, rest = rest.split(b"\r\n", 1)
            size = int(length_line[1:])
            if len(rest) < size + 2:
                return found
            parts.append(rest[:size])
            rest = rest[size + 2 :]
        del buffer[: len(data) - len(rest)]
        found.append(parts[0].decode().upper())


async def _serve(reply: Callable[[str], bytes | None]) -> tuple[asyncio.Server, str, list[str]]:
    seen: list[str] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        buffer = bytearray()
        try:
            while data := await reader.read(1024):
                buffer.extend(data)
                for command in _commands(buffer):
                    seen.append(command)
                    answer = reply(command)
                    if answer is not None:
                        writer.write(answer)
                        await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"redis://127.0.0.1:{port}/0", seen


@pytest.fixture
async def silent_server() -> AsyncIterator[tuple[str, list[str]]]:
    server, url, seen = await _serve(lambda command: None)
    yield url, seen
    server.close()
    await server.wait_closed()


@pytest.fixture
async def ack_swallowing_server() -> AsyncIterator[tuple[str, list[str]]]:
    server, url, seen = await _serve(lambda command: None if command == "SUBSCRIBE" else b"+OK\r\n")
    yield url, seen
    server.close()
    await server.wait_closed()


@pytest.fixture
def use_server(monkeypatch: pytest.MonkeyPatch):
    def apply(url: str, limit: float) -> None:
        settings = SimpleNamespace(redis_url=url, run_event_subscribe_timeout_sec=limit)
        monkeypatch.setattr(events, "get_settings", lambda: settings)
        events._async_redis_pool.cache_clear()
        events._async_redis_pubsub_pool.cache_clear()

    yield apply
    events._async_redis_pool.cache_clear()
    events._async_redis_pubsub_pool.cache_clear()


async def _assert_bounded_failure(channel: str) -> None:
    started = time.monotonic()
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(events.RunEventSubscriptionError, match="timeout"):
            await asyncio.wait_for(events.subscribe_channel(channel), 5)
    assert LIMIT <= time.monotonic() - started < LIMIT + 1.5
    warnings = [e for e in logs if e["event"] == "run_event.subscribe_timeout"]
    assert [(w["channel"], w["timeout_sec"]) for w in warnings] == [(channel, LIMIT)]
    # 응답을 덜 읽은 연결이 풀에 살아 남으면 다음 구독이 늦은 응답을 ACK로 읽는다.
    # redis-py 8.0·8.1은 취소된 연결을 끊어 둔다(실측) — 이 동작에 기대므로 회귀를 잠근다
    idle = events._async_redis_pubsub_pool()._available_connections
    assert not [c for c in idle if c.is_connected]


async def test_handshake_stage_timeout(silent_server, use_server) -> None:
    """수락 기준 3 — 연결 핸드셰이크에서 멈춰도 상한 안에 끝나고 연결을 남기지 않는다."""
    url, seen = silent_server
    use_server(url, LIMIT)
    await _assert_bounded_failure("events:agent_run:handshake")
    assert seen and "SUBSCRIBE" not in seen  # 핸드셰이크 단계에서 멈췄다


async def test_ack_stage_timeout(ack_swallowing_server, use_server) -> None:
    """수락 기준 3 — SUBSCRIBE ACK가 오지 않아도 상한 안에 끝난다."""
    url, seen = ack_swallowing_server
    use_server(url, LIMIT)
    await _assert_bounded_failure("events:agent_run:ack")
    assert "SUBSCRIBE" in seen  # ACK 단계까지 갔다


async def _assert_unbounded_hangs(url: str) -> None:
    client = AsyncRedis.from_url(url, decode_responses=True, socket_timeout=None)
    pubsub = client.pubsub(ignore_subscribe_messages=False)

    async def unbounded() -> None:
        await pubsub.subscribe("events:agent_run:raw")
        await pubsub.get_message(ignore_subscribe_messages=False, timeout=None)

    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(unbounded(), 2 * LIMIT)
    finally:
        await pubsub.aclose()
        await client.aclose()


async def test_without_a_bound_handshake_waits_forever(silent_server) -> None:
    """음성 대조 — 같은 서버 앞에서 상한 없는 redis-py 호출은 2×LIMIT 안에 끝나지 않는다."""
    await _assert_unbounded_hangs(silent_server[0])


async def test_without_a_bound_ack_waits_forever(ack_swallowing_server) -> None:
    await _assert_unbounded_hangs(ack_swallowing_server[0])
