"""분당 레이트 리밋의 관리자 재정의(#386) — `system_settings["rate_limits"]`.

키는 `RateLimiter(limit_setting=...)`가 쓰는 설정 필드 이름이고 값은 분당 한도다. 재정의가 있는
키는 그 값을, 없는 키는 설정(env) 값을 쓴다. env는 기본값이자 비상 복구 경로다 — 재정의를 지우면
env 값으로 돌아간다.

- 재정의는 `RATE_LIMIT_OVERRIDE_MIN`~`RATE_LIMIT_OVERRIDE_MAX`다. 0 이하(비활성)는 env에서만 정한다.
- 전체 끄기(`rate_limit_enabled`)는 env 전용이다 — 화면에서 남용 방지를 끄지 못하게 한다.
- `RateLimiter`는 이 값을 프로세스마다 `core.rate_limit.RATE_LIMIT_OVERRIDE_TTL_SECONDS`(30초) 동안
  캐시한다. 바꾼 프로세스는 곧바로, 다른 프로세스는 그 주기 안에 반영한다(재기동 없음).
"""

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.config import CoreSettings
from biznotes_core.core import rate_limit
from biznotes_core.core.db import get_session_factory
from biznotes_core.schemas.limits import (
    RATE_LIMIT_KEYS,
    RATE_LIMIT_OVERRIDE_MAX,
    RATE_LIMIT_OVERRIDE_MIN,
)
from biznotes_core.services.settings_store import get_overrides, valid_int

logger = structlog.get_logger()

SETTING_KEY = "rate_limits"

__all__ = [
    "RATE_LIMIT_KEYS",
    "RATE_LIMIT_OVERRIDE_MAX",
    "RATE_LIMIT_OVERRIDE_MIN",
    "SETTING_KEY",
    "describe_rate_limits",
    "get_rate_limit_overrides",
    "install_override_provider",
    "load_rate_limit_overrides",
    "valid_overrides",
]


def valid_overrides(raw: dict[str, Any]) -> dict[str, int]:
    """저장값에서 쓸 수 있는 재정의만 — 모르는 키·범위 밖 값은 버린다(env 값을 쓴다)."""
    result: dict[str, int] = {}
    for key, value in raw.items():
        if key not in RATE_LIMIT_KEYS:
            continue
        checked = valid_int(value, RATE_LIMIT_OVERRIDE_MIN, RATE_LIMIT_OVERRIDE_MAX)
        if checked is None:
            logger.warning("rate_limit_override_ignored", key=key)
            continue
        result[key] = checked
    return result


async def get_rate_limit_overrides(db: AsyncSession) -> dict[str, int]:
    return valid_overrides(await get_overrides(db, SETTING_KEY))


def describe_rate_limits(settings: CoreSettings, overrides: dict[str, int]) -> list[dict[str, Any]]:
    """키별 `{key, env_value, override, effective}` — 관리자 화면용."""
    items = []
    for key in RATE_LIMIT_KEYS:
        env_value = int(getattr(settings, key))
        override = overrides.get(key)
        items.append(
            {
                "key": key,
                "env_value": env_value,
                "override": override,
                "effective": override if override is not None else env_value,
            }
        )
    return items


async def load_rate_limit_overrides() -> dict[str, int]:
    """`RateLimiter` 재정의 공급자 — 요청 세션과 별도인 짧은 세션으로 읽는다."""
    async with get_session_factory()() as session:
        return await get_rate_limit_overrides(session)


def install_override_provider() -> None:
    """이 프로세스의 `RateLimiter`가 관리자 재정의를 읽게 한다(다시 부르면 캐시만 비운다).

    `biznotes_core.api.deps`가 import될 때 부른다 — 인증 라우터를 쓰는 API 프로세스는 모두 해당한다.
    """
    rate_limit.set_override_provider(load_rate_limit_overrides)
