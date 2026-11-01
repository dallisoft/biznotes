"""프로바이더 단위 서킷 브레이커 — Redis 카운터 기반.

연속 실패 FAIL_THRESHOLD회 → OPEN_SECONDS 동안 차단(open).
차단 중에는 폴백 체인에서 즉시 건너뛰고, TTL 만료로 자동 half-open
(다음 시도 허용 — 성공 시 카운터 리셋, 실패 시 즉시 재차단).
"""

import uuid

from redis.asyncio import Redis as AsyncRedis

from biznotes_core.config import get_settings

FAIL_THRESHOLD = 5
OPEN_SECONDS = 60
FAIL_WINDOW_SECONDS = 300

_FAIL_KEY = "circuit:fail:{provider_id}"
_OPEN_KEY = "circuit:open:{provider_id}"


def _client() -> AsyncRedis:
    return AsyncRedis.from_url(get_settings().redis_url)


async def is_open(provider_id: uuid.UUID) -> bool:
    client = _client()
    try:
        return bool(await client.exists(_OPEN_KEY.format(provider_id=provider_id)))
    finally:
        await client.aclose()


async def record_failure(provider_id: uuid.UUID) -> bool:
    """실패 기록. 임계 도달로 차단이 새로 열리면 True."""
    fail_key = _FAIL_KEY.format(provider_id=provider_id)
    client = _client()
    try:
        count = await client.incr(fail_key)
        await client.expire(fail_key, FAIL_WINDOW_SECONDS)
        if count >= FAIL_THRESHOLD:
            await client.set(_OPEN_KEY.format(provider_id=provider_id), "1", ex=OPEN_SECONDS)
            await client.delete(fail_key)
            return True
        return False
    finally:
        await client.aclose()


async def record_success(provider_id: uuid.UUID) -> None:
    client = _client()
    try:
        await client.delete(
            _FAIL_KEY.format(provider_id=provider_id),
            _OPEN_KEY.format(provider_id=provider_id),
        )
    finally:
        await client.aclose()


async def reset(provider_id: uuid.UUID) -> None:
    """어드민 수동 차단 해제."""
    await record_success(provider_id)


async def get_status(provider_id: uuid.UUID) -> dict:
    client = _client()
    try:
        open_ttl = await client.ttl(_OPEN_KEY.format(provider_id=provider_id))
        fails_raw = await client.get(_FAIL_KEY.format(provider_id=provider_id))
        fails = int(fails_raw) if fails_raw else 0
        return {
            "open": open_ttl > 0,
            "open_remaining_seconds": max(open_ttl, 0),
            "recent_failures": fails,
        }
    finally:
        await client.aclose()
