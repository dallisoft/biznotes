# 신규(#154 B). 필요: Valkey(tests/conftest.py의 CORE_TEST_VALKEY_URL). DB는 쓰지 않는다.
"""terminal status 발행 때 run event sequence 키에 보존 기간을 걸고, 만료 뒤 재접속 cursor 계약을
실제 Redis(Lua `EXPIRE`·`INFO run_id` 세대)로 검증한다."""

import asyncio
import uuid

import pytest
import redis as redis_sync

from biznotes_core.config import get_settings
from biznotes_core.schemas.enums import TERMINAL_RUN_STATUSES
from biznotes_core.schemas.run_events import (
    ProgressEventInput,
    StatusEventInput,
    ToolCallEventInput,
)
from biznotes_core.services import events


@pytest.fixture
def redis_client():
    events._async_redis_pool.cache_clear()
    client = redis_sync.Redis.from_url(get_settings().redis_url, decode_responses=True)
    yield client
    client.close()
    events._async_redis_pool.cache_clear()


def _run() -> str:
    run_id = str(uuid.uuid4())
    assert events.initialize_run_event_sequence(run_id)
    return run_id


def _ttl(client, run_id: str) -> int:
    return int(client.ttl(events.sequence_key_for_agent_run(run_id)))


def _node(progress: int = 10) -> ProgressEventInput:
    return ProgressEventInput(type="progress", node="n1", progress=progress)


@pytest.mark.parametrize("status", sorted(TERMINAL_RUN_STATUSES))
async def test_terminal_status_sets_retention(redis_client, status: str) -> None:
    """수락 기준 2 — terminal status 발행이 TTL(설정값)을 걸고, 그 전 발행은 걸지 않는다."""
    run_id = _run()
    assert events.publish_run_event(run_id, StatusEventInput(type="status", status="running"))
    assert await events.publish_run_event_async(run_id, _node())
    assert _ttl(redis_client, run_id) == -1  # 만료 없음

    published = events.publish_run_event(run_id, StatusEventInput(type="status", status=status))
    assert published is not None and published.event_id == 3

    retention = get_settings().run_event_sequence_retention_sec
    assert retention - 5 <= _ttl(redis_client, run_id) <= retention


async def test_async_publisher_sets_retention_too(redis_client) -> None:
    run_id = _run()
    await events.publish_run_event_async(run_id, StatusEventInput(type="status", status="failed"))
    assert _ttl(redis_client, run_id) > 0


async def test_expired_terminal_run_snapshot_has_no_cursor(redis_client, monkeypatch) -> None:
    """만료 뒤 terminal run은 cursor 없이(None) snapshot만, 비terminal은 지금처럼 오류다."""
    monkeypatch.setattr(get_settings(), "run_event_sequence_retention_sec", 1)
    run_id = _run()
    events.publish_run_event(run_id, StatusEventInput(type="status", status="succeeded"))

    client = events._async_redis_client()
    try:
        assert await events.read_run_event_cursor_for_snapshot(client, run_id, terminal=True) == 1
        await asyncio.sleep(2.1)  # 실제 EXPIRE로 키가 사라질 때까지
        assert redis_client.exists(events.sequence_key_for_agent_run(run_id)) == 0

        cursor = await events.read_run_event_cursor_for_snapshot(client, run_id, terminal=True)
        assert cursor is None
        with pytest.raises(events.RunEventSequenceUnavailable, match="MISSING"):
            await events.read_run_event_cursor_for_snapshot(client, run_id, terminal=False)
        # 만료된 sequence에는 다시 발행하지 않는다(0으로 되돌아가 cursor가 역행하지 않는다)
        assert events.publish_run_event(run_id, _node(100)) is None
    finally:
        await client.aclose()


async def test_stale_generation_terminal_run_snapshot_has_no_cursor(redis_client) -> None:
    """Redis 재시작(세대 변경)도 terminal run은 snapshot만 받는다 — 지금까지는 503이었다."""
    run_id = _run()
    redis_client.hset(events.sequence_key_for_agent_run(run_id), "generation", "old-generation")
    client = events._async_redis_client()
    try:
        cursor = await events.read_run_event_cursor_for_snapshot(client, run_id, terminal=True)
        assert cursor is None
        with pytest.raises(events.RunEventSequenceUnavailable, match="STALE"):
            await events.read_run_event_cursor_for_snapshot(client, run_id, terminal=False)
    finally:
        await client.aclose()


async def test_live_run_cursor_is_unchanged(redis_client) -> None:
    run_id = _run()
    events.publish_run_event(run_id, _node())
    client = events._async_redis_client()
    try:
        for terminal in (False, True):
            got = await events.read_run_event_cursor_for_snapshot(client, run_id, terminal=terminal)
            assert got == 1
    finally:
        await client.aclose()


def _status(status: str) -> StatusEventInput:
    return StatusEventInput(type="status", status=status)


async def test_late_terminal_after_cancel_rearms_the_retention(redis_client) -> None:
    """취소 → 워커가 계속 낸 tool_call → 늦은 succeeded.

    이벤트가 버려지지 않고 TTL은 다시 걸린다."""
    run_id = _run()
    events.publish_run_event(run_id, _status("cancelled"))
    redis_client.expire(events.sequence_key_for_agent_run(run_id), 50)  # 줄어든 남은 시간 흉내
    tool = ToolCallEventInput(
        type="tool_call", tool_call_id="c", name="rag_search", status="succeeded", arguments={}
    )
    assert events.publish_run_event(run_id, tool) is not None
    assert 0 < _ttl(redis_client, run_id) <= 50  # 비status 이벤트는 TTL을 건드리지 않는다
    late = events.publish_run_event(run_id, _status("succeeded"))
    assert late is not None and late.event_id == 3
    retention = get_settings().run_event_sequence_retention_sec
    assert _ttl(redis_client, run_id) > retention - 5  # 다시 걸었다


async def test_revived_run_persists_the_sequence(redis_client) -> None:
    """취소 → 승인 대기(되살아남) → running. 비terminal status가 보존 기간을 푼다."""
    run_id = _run()
    events.publish_run_event(run_id, _status("cancelled"))
    assert _ttl(redis_client, run_id) > 0
    events.publish_run_event(run_id, _status("awaiting_approval"))
    assert _ttl(redis_client, run_id) == -1
    events.publish_run_event(run_id, _status("running"))
    assert _ttl(redis_client, run_id) == -1


async def test_stale_generation_terminal_publish_still_sets_retention(redis_client) -> None:
    """재시작 전 세대의 키는 발행이 실패해도 terminal이면 보존 기간이 걸린다(영구히 남지 않는다)."""
    run_id = _run()
    redis_client.hset(events.sequence_key_for_agent_run(run_id), "generation", "old-generation")
    assert events.publish_run_event(run_id, _status("failed")) is None
    assert _ttl(redis_client, run_id) > 0
    other = _run()
    redis_client.hset(events.sequence_key_for_agent_run(other), "generation", "old-generation")
    events.publish_run_event(other, _status("running"))
    assert _ttl(redis_client, other) == -1
