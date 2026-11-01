"""Redis Pub/Sub 기반 진행 이벤트 — 워커 발행 헬퍼 + SSE 응답 헬퍼 (R1-4 단일화).

발행(publish)·채널명·SSE 응답 헬퍼를 이 모듈로 모은다. 이전에는 워커 3곳
(source·agent_run)이 각자 `redis_sync.Redis.from_url().publish()`를
복붙했고, 라우터 4곳이 `StreamingResponse(..., media_type="text/event-stream")`와
`data: {json}\n\n` 포맷을 복붙했다.

[R5-3] source의 과거 `/events` 엔드포인트는 제거된 상태를 유지한다.
Phase 1은 agent-run 전용 typed publisher와 ACK 기반 `subscribe_channel` lease를 복원해
`/agent-runs/{id}/stream` 및 worker-backed chat 어댑터가 공유하게 한다. source/export의
기존 raw publisher는 해당 도메인 호환 경로로만 존치한다.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import redis as redis_sync
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from redis import RedisError
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio.client import PubSub
from redis.asyncio.connection import ConnectionPool
from redis.exceptions import TimeoutError as RedisTimeoutError

from biznotes_core.config import get_settings
from biznotes_core.schemas.enums import TERMINAL_RUN_STATUSES
from biznotes_core.schemas.run_events import (
    RunEvent,
    RunEventAdapter,
    RunEventInput,
    RunEventInputAdapter,
)

# SSE 응답 표준 헤더 — 프록시 버퍼링 무효화(X-Accel-Buffering) 포함.
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


# 풀은 **지연 생성**한다. import 시점에 만들면 프로젝트가 설정 클래스를 등록하기
# 전에 코어 기본 redis_url로 굳는다. 그러면 발행은 프로젝트 DB로, 구독은 코어
# 기본 DB로 가서 메시지가 영영 도착하지 않는다 — 오류가 아니라 **무한 대기**로
# 나타나므로 원인을 찾기 어렵다.
@lru_cache
def _async_redis_pool() -> ConnectionPool:
    """명령용 풀 — 프로세스 전체가 공유한다. client wrapper를 닫아도 풀은 살아 있고
    연결만 반납된다."""
    return ConnectionPool.from_url(get_settings().redis_url, decode_responses=True)


@lru_cache
def _async_redis_pubsub_pool() -> ConnectionPool:
    """Pub/Sub 전용 풀.

    redis-py 8의 async Connection은 socket_timeout 기본값이 5초다. Pub/Sub의
    get_message(timeout=None)도 이 기본값을 사용하므로 유휴 채널이 오류로 끝난다.
    무한 대기가 의도인 이 풀에만 None을 명시하고, 명령용 풀의 timeout은 실패
    감지를 위해 그대로 둔다.
    """
    return ConnectionPool.from_url(
        get_settings().redis_url, decode_responses=True, socket_timeout=None
    )

# 세대(Redis 재시작마다 바뀌는 `INFO server`의 `run_id`)를 읽는 Lua 앞부분. 세 스크립트가 공유한다.
# 서버가 `run_id`를 주지 않으면(Redis 호환 대안 서버 등) 세대를 비교할 수 없다. 이때 `HSET`의
# nil 인자 오류나 `STALE_GENERATION`으로 새지 않고 명시적 표식으로 실패한다(#317 ADR 결정 2).
# 이 표식은 보존 만료·재시작을 뜻하는 `_SEQUENCE_GONE_MARKERS`에 넣지 않는다 — 호환되지 않는
# 서버라는 뜻이다. 판정은 키 존재 확인보다 먼저 한다(호환 서버에서는 결과가 같다).
_SERVER_INFO_LUA = "redis.call('INFO', 'server')"
_GENERATION_LUA = r"""
local generation = string.match({info}, 'run_id:([^\r\n]+)')
if not generation then
    return redis.error_reply('RUN_EVENT_SEQUENCE_NO_GENERATION')
end
"""

_INIT_SEQUENCE_BODY = r"""
if redis.call('EXISTS', KEYS[1]) == 0 then
    redis.call('HSET', KEYS[1], 'generation', generation, 'value', 0)
    return 1
end
return 0
"""

# ARGV[2](있으면): terminal status의 보존 기간(초) — 매번 다시 건다(취소 뒤 늦은 terminal 포함).
# 비terminal status(`running`·`awaiting_approval`)는 PERSIST로 보존 기간을 푼다 — 취소 뒤 되살아난
# run의 키가 만료되지 않게 한다. 그 밖의 이벤트는 TTL을 건드리지 않는다(HINCRBY는 TTL을 유지한다).
_ALLOCATE_AND_PUBLISH_BODY = r"""
if redis.call('EXISTS', KEYS[1]) == 0 then
    return redis.error_reply('RUN_EVENT_SEQUENCE_MISSING')
end
local ttl = tonumber(ARGV[2])
local stored = redis.call('HGET', KEYS[1], 'generation')
if stored ~= generation then
    -- 재시작 전 세대의 키도 terminal이면 보존 기간을 건다(영구히 남지 않게)
    if ttl and ttl > 0 then
        redis.call('EXPIRE', KEYS[1], ttl)
    end
    return redis.error_reply('RUN_EVENT_SEQUENCE_STALE_GENERATION')
end
local next_id = redis.call('HINCRBY', KEYS[1], 'value', 1)
local original = ARGV[1]
if string.sub(original, -1) ~= '}' then
    return redis.error_reply('RUN_EVENT_PAYLOAD_NOT_OBJECT')
end
local payload = string.sub(original, 1, -2) .. ',"event_id":' .. tostring(next_id) .. '}'
redis.call('PUBLISH', KEYS[2], payload)
if ttl and ttl > 0 then
    redis.call('EXPIRE', KEYS[1], ttl)
else
    local ok, decoded = pcall(cjson.decode, original)
    if ok and type(decoded) == 'table' and decoded['type'] == 'status' then
        redis.call('PERSIST', KEYS[1])
    end
end
return {next_id, payload}
"""

_READ_CURSOR_BODY = r"""
if redis.call('EXISTS', KEYS[1]) == 0 then
    return redis.error_reply('RUN_EVENT_SEQUENCE_MISSING')
end
local stored = redis.call('HGET', KEYS[1], 'generation')
if stored ~= generation then
    return redis.error_reply('RUN_EVENT_SEQUENCE_STALE_GENERATION')
end
return redis.call('HGET', KEYS[1], 'value')
"""


def _sequence_script(body: str, info_expr: str = _SERVER_INFO_LUA) -> str:
    """세대 판정 앞부분 + 본문. `info_expr`은 테스트가 `run_id` 없는 응답을 넣을 때만 바꾼다."""
    return _GENERATION_LUA.replace("{info}", info_expr) + body


_INIT_SEQUENCE_LUA = _sequence_script(_INIT_SEQUENCE_BODY)
_ALLOCATE_AND_PUBLISH_LUA = _sequence_script(_ALLOCATE_AND_PUBLISH_BODY)
_READ_CURSOR_LUA = _sequence_script(_READ_CURSOR_BODY)


# sequence가 사라졌거나(보존 만료) Redis가 재시작돼 세대가 바뀐 경우의 Lua 오류 표식
_SEQUENCE_GONE_MARKERS = ("RUN_EVENT_SEQUENCE_MISSING", "RUN_EVENT_SEQUENCE_STALE_GENERATION")


class RunEventSequenceUnavailable(RuntimeError):
    pass


class RunEventSubscriptionError(RuntimeError):
    pass


@dataclass
class ChannelSubscription:
    client: AsyncRedis
    pubsub_client: AsyncRedis
    pubsub: PubSub
    channel: str
    queue: asyncio.Queue[RunEvent | BaseException]
    reader: asyncio.Task[None]


def _async_redis_client() -> AsyncRedis:
    """프로세스 공유 풀을 쓰는 비동기 client wrapper를 만든다."""
    return AsyncRedis(connection_pool=_async_redis_pool())


def _async_redis_pubsub_client() -> AsyncRedis:
    """read timeout 없이 대기하는 Pub/Sub 전용 client wrapper를 만든다."""
    return AsyncRedis(connection_pool=_async_redis_pubsub_pool())


# ── 채널명 ────────────────────────────────────────────────────────────────
def channel_for_source(source_id: str) -> str:
    return f"events:source:{source_id}"


def channel_for_agent_run(run_id: str) -> str:
    return f"events:agent_run:{run_id}"


def sequence_key_for_agent_run(run_id: str | uuid.UUID) -> str:
    return f"{channel_for_agent_run(str(run_id))}:seq"


def _publish_args(event: Any) -> list[Any]:
    """발행 Lua의 ARGV — 페이로드, terminal status면 보존 기간(초)을 덧붙인다(#154 B).

    terminal이 아니면 인자 모양을 예전과 같게 둔다(만료를 걸지 않는다).
    """
    args: list[Any] = [event.model_dump_json(exclude_none=True)]
    if getattr(event, "type", None) == "status" and event.status in TERMINAL_RUN_STATUSES:
        args.append(get_settings().run_event_sequence_retention_sec)
    return args


# ── 발행(워커·동기 컨텍스트) ────────────────────────────────────────────────
def publish(channel: str, event: dict[str, Any]) -> None:
    """이벤트 dict를 채널에 발행 — 워커(동기 컨텍스트)에서 호출."""
    client = redis_sync.Redis.from_url(get_settings().redis_url)
    try:
        client.publish(channel, json.dumps(event, ensure_ascii=False))
    finally:
        client.close()


def publish_source_event(source_id: str, event: dict[str, Any]) -> None:
    """소스 진행 이벤트 발행 (하위 호환 래퍼)."""
    publish(channel_for_source(source_id), event)


def initialize_run_event_sequence(run_id: str | uuid.UUID) -> bool:
    """새 run의 event sequence를 0으로 초기화한다. 기존 sequence는 보존한다.

    서버가 세대(`INFO server`의 `run_id`)를 주지 않으면 `RUN_EVENT_SEQUENCE_NO_GENERATION`
    `ResponseError`를 그대로 올린다. 발행 경로는 예외를 삼키므로 원인은 여기서 드러난다(#317).
    """
    client = redis_sync.Redis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        return bool(client.eval(_INIT_SEQUENCE_LUA, 1, sequence_key_for_agent_run(run_id)))
    finally:
        client.close()


async def initialize_run_event_sequence_async(run_id: str | uuid.UUID) -> bool:
    """API 이벤트 루프를 막지 않고 run event sequence를 초기화한다.

    실패 방식은 `initialize_run_event_sequence`와 같다.
    """
    client = _async_redis_client()
    try:
        return bool(
            await client.eval(_INIT_SEQUENCE_LUA, 1, sequence_key_for_agent_run(run_id))
        )
    finally:
        await client.aclose()


def publish_run_event(run_id: str | uuid.UUID, event: RunEventInput) -> RunEvent | None:
    """검증된 agent-run 이벤트에 ID를 원자 할당하고 발행한다.

    Redis 관측 경로 실패는 DB 상태머신을 되돌릴 수 없으므로 기록 후 삼킨다.
    """
    import structlog

    client: redis_sync.Redis | None = None
    try:
        validated = RunEventInputAdapter.validate_python(event)
        client = redis_sync.Redis.from_url(get_settings().redis_url, decode_responses=True)
        result = client.eval(
            _ALLOCATE_AND_PUBLISH_LUA,
            2,
            sequence_key_for_agent_run(run_id),
            channel_for_agent_run(str(run_id)),
            *_publish_args(validated),
        )
        return RunEventAdapter.validate_json(result[1])
    except Exception as exc:  # noqa: BLE001 - 관측 실패는 DB 상태를 되돌리지 않는다.
        structlog.get_logger().exception(
            "run event publish failed", run_id=str(run_id), error=str(exc)
        )
        return None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001 - 종료 실패도 관측 경로에 격리한다.
                structlog.get_logger().warning(
                    "run event redis close failed", run_id=str(run_id), error=str(exc)
                )


async def publish_run_event_async(
    run_id: str | uuid.UUID, event: RunEventInput
) -> RunEvent | None:
    import structlog

    client: AsyncRedis | None = None
    try:
        validated = RunEventInputAdapter.validate_python(event)
        client = _async_redis_client()
        result = await client.eval(
            _ALLOCATE_AND_PUBLISH_LUA,
            2,
            sequence_key_for_agent_run(run_id),
            channel_for_agent_run(str(run_id)),
            *_publish_args(validated),
        )
        return RunEventAdapter.validate_json(result[1])
    except Exception as exc:  # noqa: BLE001 - 관측 실패는 DB 상태를 되돌리지 않는다.
        structlog.get_logger().exception(
            "run event publish failed", run_id=str(run_id), error=str(exc)
        )
        return None
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001 - 종료 실패도 관측 경로에 격리한다.
                structlog.get_logger().warning(
                    "run event redis close failed", run_id=str(run_id), error=str(exc)
                )


async def read_run_event_cursor_async(
    client: AsyncRedis, run_id: str | uuid.UUID
) -> int:
    try:
        value = await client.eval(_READ_CURSOR_LUA, 1, sequence_key_for_agent_run(run_id))
    except RedisError as exc:
        raise RunEventSequenceUnavailable(str(exc)) from exc
    return int(value)


async def read_run_event_cursor_for_snapshot(
    client: AsyncRedis, run_id: str | uuid.UUID, *, terminal: bool
) -> int | None:
    """재접속 snapshot에 실을 cursor. terminal run의 sequence가 없으면 None이다(#154 B).

    terminal run은 보존 기간이 지나면 sequence 키가 사라진다. Redis가 재시작돼 세대가 바뀐 경우도
    같다. 이때는 cursor 없이 snapshot만 보내고 스트림을 닫는다(호출자는 `event_id=0`을 쓴다).
    None은 Lua 오류 `RUN_EVENT_SEQUENCE_MISSING`·`…_STALE_GENERATION`일 때만이다. 연결 오류 등
    다른 Redis 오류와 terminal이 아닌 run은 지금처럼 `RunEventSequenceUnavailable`이다.

    `terminal`은 cursor를 읽은 **뒤에** DB에서 다시 읽은 run 상태로 정한다(`TERMINAL_RUN_STATUSES`).
    취소 뒤 워커가 승인 대기로 되돌리는 등 상태가 다시 바뀔 수 있어서다.
    """
    try:
        return await read_run_event_cursor_async(client, run_id)
    except RunEventSequenceUnavailable as exc:
        if terminal and any(marker in str(exc) for marker in _SEQUENCE_GONE_MARKERS):
            return None
        raise


async def subscribe_channel(channel: str) -> ChannelSubscription:
    """SUBSCRIBE ACK 뒤 단일 reader가 소켓을 소유하는 구독 lease를 만든다."""
    client = _async_redis_client()
    pubsub_client = _async_redis_pubsub_client()
    pubsub = pubsub_client.pubsub(ignore_subscribe_messages=False)
    queue: asyncio.Queue[RunEvent | BaseException] = asyncio.Queue()
    try:
        # Pub/Sub 풀은 read timeout이 없다(유휴 채널 유지). 연결 핸드셰이크·SUBSCRIBE·ACK까지만
        # 상한을 둔다 — TCP는 살아 있고 서버가 응답하지 않는 half-open에서 SSE 요청이
        # 무기한 매달리지 않게 한다(#154 C).
        limit = get_settings().run_event_subscribe_timeout_sec
        try:
            async with asyncio.timeout(limit):
                await pubsub.subscribe(channel)
                ack = await pubsub.get_message(ignore_subscribe_messages=False, timeout=None)
        except TimeoutError as exc:
            import structlog

            structlog.get_logger().warning(
                "run_event.subscribe_timeout", channel=channel, timeout_sec=limit
            )
            raise RunEventSubscriptionError("SUBSCRIBE ACK timeout") from exc
        if ack is None or ack.get("type") != "subscribe" or ack.get("channel") != channel:
            raise RunEventSubscriptionError("SUBSCRIBE ACK mismatch")

        async def pump() -> None:
            try:
                while True:
                    try:
                        message = await pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=None
                        )
                    except (RedisTimeoutError, TimeoutError):
                        # 유휴 Pub/Sub read timeout은 메시지 미도착을 뜻할 뿐이다.
                        # 연결 실패·페이로드 오류 등 다른 예외와 분리해 계속 읽는다.
                        continue
                    if message is not None and message.get("type") == "message":
                        await queue.put(RunEventAdapter.validate_json(message["data"]))
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                await queue.put(exc)

        reader = asyncio.create_task(pump())
        return ChannelSubscription(client, pubsub_client, pubsub, channel, queue, reader)
    except BaseException:
        await pubsub.aclose()
        await pubsub_client.aclose()
        await client.aclose()
        raise


async def close_subscription(subscription: ChannelSubscription) -> None:
    subscription.reader.cancel()
    try:
        await subscription.reader
    except asyncio.CancelledError:
        pass
    try:
        await subscription.pubsub.unsubscribe(subscription.channel)
        await subscription.pubsub.get_message(
            ignore_subscribe_messages=False, timeout=1.0
        )
    except RedisError:
        pass
    finally:
        await subscription.pubsub.aclose()
        await subscription.pubsub_client.aclose()
        await subscription.client.aclose()


# ── SSE 응답 헬퍼(라우터) ───────────────────────────────────────────────────
def format_sse(event: dict[str, Any] | BaseModel) -> str:
    """이벤트 dict를 SSE 데이터 프레임 문자열로 직렬화."""
    if isinstance(event, BaseModel):
        event = event.model_dump(mode="json", exclude_none=True)
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def sse_response(generator: AsyncIterator[str]) -> StreamingResponse:
    """SSE 제너레이터를 표준 헤더의 StreamingResponse로 감싼다."""
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
