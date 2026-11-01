"""JWT 무효화(denylist) — 로그아웃/refresh 회전·재사용 탐지용 (OPS_CATCHUP_PLAN §3d).

Redis 기반 근사·저지연 denylist. 토큰 잔여 만료시간을 TTL로 사용해 자동 정리된다.

가용성 우선(§6 결정): Redis 장애 시 보안 강화는 best-effort로 degrade하고 인증 자체는
기존 JWT 검증으로 유지한다 — 등록 실패는 무시, 조회 실패는 '무효화 안 됨'으로 통과.
"""

from datetime import UTC, datetime
from typing import Any

import structlog
from redis.asyncio import Redis

from biznotes_core.config import get_settings

logger = structlog.get_logger()

_PREFIX = "denylist:jti:"


def remaining_seconds(payload: dict[str, Any]) -> int:
    """토큰 payload의 exp 기준 잔여 초 (만료/누락 시 0)."""
    exp = payload.get("exp")
    if exp is None:
        return 0
    return max(0, int(exp - datetime.now(UTC).timestamp()))


async def revoke(jti: str | None, ttl_seconds: int) -> None:
    """jti를 denylist에 등록 (TTL=잔여 만료시간). 잔여 0/누락이면 무시."""
    if not jti or ttl_seconds <= 0:
        return
    settings = get_settings()
    try:
        redis = Redis.from_url(settings.redis_url)
        try:
            await redis.set(f"{_PREFIX}{jti}", "1", ex=ttl_seconds)
        finally:
            await redis.aclose()
    except Exception as e:  # noqa: BLE001 — Redis 장애는 best-effort degrade
        logger.warning("denylist 등록 실패 — best-effort degrade", jti=jti, error=str(e))


async def is_revoked(jti: str | None) -> bool:
    """jti가 denylist에 있으면 True. Redis 장애 시 False(통과)로 degrade."""
    if not jti:
        return False
    settings = get_settings()
    try:
        redis = Redis.from_url(settings.redis_url)
        try:
            return bool(await redis.exists(f"{_PREFIX}{jti}"))
        finally:
            await redis.aclose()
    except Exception as e:  # noqa: BLE001 — 조회 실패 시 가용성 우선으로 통과
        logger.warning("denylist 조회 실패 — 통과 처리", jti=jti, error=str(e))
        return False
