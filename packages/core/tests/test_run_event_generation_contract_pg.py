# 신규(#317). 필요: Valkey(tests/conftest.py의 CORE_TEST_VALKEY_URL). DB는 쓰지 않는다.
"""run event sequence의 세대 판정 계약 — 서버가 `INFO server`의 `run_id`를 주지 않을 때 명시적으로
실패한다(ADR `docs/plans/redis-alternative-readiness/adr.md` 결정 2).

`run_id` 없는 서버는 스크립트의 `INFO` 호출 자리에 고정 문자열을 넣어 흉내 낸다. 세대 파싱·판정·
본문은 운영 스크립트와 같은 코드다(`events._sequence_script`). Redis 7.2+·Valkey 8+ 어디서나 같다.
"""

import uuid

import pytest
import redis as redis_sync
from redis.exceptions import ResponseError

from biznotes_core.config import get_settings
from biznotes_core.schemas.run_events import ProgressEventInput, StatusEventInput
from biznotes_core.services import events

NO_GENERATION = "RUN_EVENT_SEQUENCE_NO_GENERATION"
# Redis 호환이지만 `run_id`를 주지 않는 서버의 `INFO server` 응답
_INFO_WITHOUT_RUN_ID = "'# Server\\r\\nredis_version:7.2.4\\r\\nprocess_id:1\\r\\n'"


@pytest.fixture
def redis_client():
    events._async_redis_pool.cache_clear()
    client = redis_sync.Redis.from_url(get_settings().redis_url, decode_responses=True)
    yield client
    client.close()
    events._async_redis_pool.cache_clear()


@pytest.fixture
def server_without_run_id(monkeypatch):
    """세 스크립트를 `run_id` 없는 `INFO` 응답으로 바꾼다."""
    for name, body in (
        ("_INIT_SEQUENCE_LUA", events._INIT_SEQUENCE_BODY),
        ("_ALLOCATE_AND_PUBLISH_LUA", events._ALLOCATE_AND_PUBLISH_BODY),
        ("_READ_CURSOR_LUA", events._READ_CURSOR_BODY),
    ):
        monkeypatch.setattr(events, name, events._sequence_script(body, _INFO_WITHOUT_RUN_ID))


def _key(run_id: str) -> str:
    return events.sequence_key_for_agent_run(run_id)


def _progress() -> ProgressEventInput:
    return ProgressEventInput(type="progress", node="n1", progress=10)


async def test_generation_is_the_server_run_id(redis_client) -> None:
    """정상 서버 — 초기화가 서버의 `run_id`를 세대로 기록하고, 같은 세대에서 발행·cursor가 된다."""
    run_id = str(uuid.uuid4())
    assert events.initialize_run_event_sequence(run_id)
    server_run_id = redis_client.info("server")["run_id"]
    assert redis_client.hget(_key(run_id), "generation") == server_run_id

    published = events.publish_run_event(run_id, _progress())
    assert published is not None and published.event_id == 1
    client = events._async_redis_client()
    try:
        assert await events.read_run_event_cursor_async(client, run_id) == 1
    finally:
        await client.aclose()


async def test_initialize_raises_no_generation(redis_client, server_without_run_id) -> None:
    """초기화는 명시적 표식을 호출자에게 올리고 키를 만들지 않는다(동기·비동기 모두)."""
    run_id = str(uuid.uuid4())
    with pytest.raises(ResponseError, match=NO_GENERATION):
        events.initialize_run_event_sequence(run_id)
    with pytest.raises(ResponseError, match=NO_GENERATION):
        await events.initialize_run_event_sequence_async(run_id)
    assert redis_client.exists(_key(run_id)) == 0


async def test_publish_fails_with_no_generation_not_stale(
    redis_client, monkeypatch, server_without_run_id
) -> None:
    """발행은 예외를 삼키지만(관측 경로) 원인은 NO_GENERATION이고, 키·TTL을 건드리지 않는다."""
    run_id = str(uuid.uuid4())
    redis_client.hset(_key(run_id), mapping={"generation": "g", "value": 0})
    logged: list[str] = []

    class _Logger:
        def exception(self, _msg: str, **kw: object) -> None:
            logged.append(str(kw.get("error")))

    import structlog

    monkeypatch.setattr(structlog, "get_logger", lambda: _Logger())

    terminal = StatusEventInput(type="status", status="failed")
    assert events.publish_run_event(run_id, terminal) is None
    assert await events.publish_run_event_async(run_id, _progress()) is None
    assert len(logged) == 2 and all(NO_GENERATION in e for e in logged)
    assert redis_client.hget(_key(run_id), "value") == "0"
    assert redis_client.ttl(_key(run_id)) == -1  # terminal이어도 보존 기간을 걸지 않는다


async def test_snapshot_cursor_does_not_treat_no_generation_as_gone(
    redis_client, server_without_run_id
) -> None:
    """NO_GENERATION은 보존 만료·재시작이 아니다 — terminal run이어도 None이 아니라 오류다."""
    run_id = str(uuid.uuid4())
    redis_client.hset(_key(run_id), mapping={"generation": "g", "value": 3})
    client = events._async_redis_client()
    try:
        for terminal in (True, False):
            with pytest.raises(events.RunEventSequenceUnavailable, match=NO_GENERATION):
                await events.read_run_event_cursor_for_snapshot(client, run_id, terminal=terminal)
    finally:
        await client.aclose()
