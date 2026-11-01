"""사용자별 일일 사용량 가드 — Redis 일일 카운터 기반.

날짜 경계(「오늘」)는 배포 표시 시간대(`display_timezone`, #352) 기준이다. 기본값은 UTC다.

원칙:
- 카운터는 가드용 근사치 — 정산/표시는 DB 집계(token_usage 등)가 기준.
- 한도 우선순위(#387): 사용자별 값 > 시스템 기본값(`system_settings["usage_defaults"]`) > 무제한.
  사용자별 행이 없거나 필드가 null이면 기본값을 따르고, 기본값도 없으면 무제한이다.
  어드민 역할은 검사 제외 (record는 수행).
- kind: "tokens" | "agent_runs"
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.config import get_settings
from biznotes_core.core.timezones import to_display
from biznotes_core.models.usage_limit import UsageLimit
from biznotes_core.schemas.limits import USAGE_LIMIT_FIELDS, USAGE_LIMIT_MAX, UsageLimitSource
from biznotes_core.services.settings_store import get_overrides, valid_int

KINDS = ("tokens", "agent_runs")
_LIMIT_FIELD = {
    "tokens": "daily_tokens",
    "agent_runs": "daily_agent_runs",
}
_LABEL = {
    "tokens": "LLM 토큰",
    "agent_runs": "에이전트 실행",
}
_TTL = 60 * 60 * 48  # 이틀 — 날짜가 바뀐 뒤 자연 만료
#: 시스템 기본 일일 한도의 `system_settings` 키(#387) — 필드 이름 → 한도
USAGE_DEFAULTS_KEY = "usage_defaults"


class UsageLimitExceeded(Exception):
    # 노드 실패 코드(#216). 메시지는 사용자 자신의 한도 안내다
    node_error_code = "usage_limit_exceeded"

    def __init__(self, kind: str, used: int, limit: int):
        self.kind = kind
        self.used = used
        self.limit = limit
        super().__init__(
            f"오늘 {_LABEL[kind]} 한도를 초과했습니다 ({used:,}/{limit:,}). "
            "내일 다시 시도하거나 관리자에게 한도 상향을 요청하세요."
        )


def _key(kind: str, user_id: uuid.UUID, now: datetime | None = None) -> str:
    # 키 형식은 그대로다. 표시 시간대를 바꾸면 그 순간 날짜 문자열이 달라질 수 있어, 그날 누적이
    # 한 번 적게 잡힐 수 있다(가드는 근사치다)
    today = to_display(now or datetime.now(UTC)).strftime("%Y%m%d")
    return f"usage:{kind}:{user_id}:{today}"


async def get_limits(db: AsyncSession, user_id: uuid.UUID) -> UsageLimit | None:
    return (
        await db.execute(select(UsageLimit).where(UsageLimit.user_id == user_id))
    ).scalar_one_or_none()


async def get_usage_defaults(db: AsyncSession) -> dict[str, int]:
    """시스템 기본 일일 한도(#387) — 설정된 필드만. 범위 밖 저장값은 버린다(그 필드는 무제한)."""
    raw = await get_overrides(db, USAGE_DEFAULTS_KEY)
    result: dict[str, int] = {}
    for field in USAGE_LIMIT_FIELDS:
        value = valid_int(raw.get(field), 0, USAGE_LIMIT_MAX)
        if value is not None:
            result[field] = value
    return result


async def effective_limits(
    db: AsyncSession, user_id: uuid.UUID
) -> dict[str, tuple[int | None, UsageLimitSource]]:
    """필드별 (적용 한도, 출처) — 출처는 `user`·`default`·`unlimited`(#387).

    관리자 역할도 여기서는 값대로 보인다 — 검사에서 빠지는 것은 `check_usage`의 몫이다.
    """
    limits = await get_limits(db, user_id)
    defaults = await get_usage_defaults(db)
    result: dict[str, tuple[int | None, UsageLimitSource]] = {}
    for field in USAGE_LIMIT_FIELDS:
        own = getattr(limits, field, None) if limits else None
        if own is not None:
            result[field] = (own, "user")
        elif field in defaults:
            result[field] = (defaults[field], "default")
        else:
            result[field] = (None, "unlimited")
    return result


async def get_today_usage(user_id: uuid.UUID) -> dict[str, int]:
    """오늘 누적 사용량 (가드 카운터 기준 근사치)."""
    redis = Redis.from_url(get_settings().redis_url)
    try:
        values = await redis.mget(*[_key(k, user_id) for k in KINDS])
        return {k: int(v) if v else 0 for k, v in zip(KINDS, values, strict=True)}
    finally:
        await redis.aclose()


async def check_usage(
    db: AsyncSession, user_id: uuid.UUID, role: str, kind: str, amount: int = 1
) -> None:
    """한도 초과 시 UsageLimitExceeded. 어드민/한도 미설정(사용자별·기본값 모두 없음)은 통과."""
    if role == "admin":
        return
    field = _LIMIT_FIELD[kind]
    limits = await get_limits(db, user_id)
    limit = getattr(limits, field, None) if limits else None
    if limit is None:
        limit = (await get_usage_defaults(db)).get(field)
    if limit is None:
        return
    redis = Redis.from_url(get_settings().redis_url)
    try:
        used_raw = await redis.get(_key(kind, user_id))
    finally:
        await redis.aclose()
    used = int(used_raw) if used_raw else 0
    if used + amount > limit:
        raise UsageLimitExceeded(kind, used, limit)


async def record_usage(user_id: uuid.UUID, kind: str, amount: int) -> None:
    """사용량 기록 — 실패해도 본 기능을 막지 않는다 (가드는 근사치)."""
    if amount <= 0:
        return
    try:
        redis = Redis.from_url(get_settings().redis_url)
        try:
            key = _key(kind, user_id)
            await redis.incrby(key, amount)
            await redis.expire(key, _TTL)
        finally:
            await redis.aclose()
    except Exception:
        import structlog

        structlog.get_logger().warning("usage record 실패", kind=kind, user_id=str(user_id))


@dataclass
class _UsageMeter:
    """usage_meter 컨텍스트의 기록량 핸들 — 소비량이 작업 후에 확정되는 경우
    (예: chat 토큰) 호출부가 amount를 갱신한다."""

    amount: int


@asynccontextmanager
async def usage_meter(
    db: AsyncSession,
    user_id: uuid.UUID,
    role: str,
    kind: str,
    amount: int = 1,
) -> AsyncIterator[_UsageMeter]:
    """사용량 한도 가드 컨텍스트 (R1-6) — check_usage → 작업 → record_usage 3종 세트를 통합.

    진입 시 `amount`로 한도를 검사(초과 시 UsageLimitExceeded)하고, 블록이 예외 없이
    끝나면 `meter.amount`(기본 = 진입 amount)만큼 기록한다. 소비량이 작업 후에야
    확정되는 경우(chat 토큰 등) 블록 안에서 `meter.amount = 실제값`으로 갱신한다.
    예외로 빠져나가면 기록하지 않는다(기존 "성공 후 기록" 동작과 동일).
    """
    await check_usage(db, user_id, role, kind, amount)
    meter = _UsageMeter(amount=amount)
    yield meter
    await record_usage(user_id, kind, meter.amount)
