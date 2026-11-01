"""분당 레이트 리밋의 관리자 재정의 — 공급자·캐시·적용(#386).

`RateLimiter.__call__`은 실경로다. Redis와 설정만 대역이다. DB에서 읽는 공급자와 관리자 API는
`tests/test_rate_limit_settings_pg.py`가 본다.
"""

from types import SimpleNamespace
from typing import Any, get_args

import pytest
from conftest import make_settings
from fastapi import HTTPException
from starlette.requests import Request

from biznotes_core.config import CoreSettings
from biznotes_core.core import rate_limit
from biznotes_core.core.rate_limit import RateLimiter
from biznotes_core.schemas.limits import (
    RATE_LIMIT_KEYS,
    RATE_LIMIT_OVERRIDE_MAX,
    RATE_LIMIT_OVERRIDE_MIN,
    RateLimitKey,
    RateLimitsUpdate,
)
from biznotes_core.services import rate_limit_settings


class _FakeRedis:
    store: dict[str, int] = {}

    @classmethod
    def from_url(cls, url: str) -> "_FakeRedis":
        return cls()

    async def incr(self, key: str) -> int:
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key: str, seconds: int) -> None:
        return None

    async def ttl(self, key: str) -> int:
        return 42

    async def aclose(self) -> None:
        return None


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    # 모듈의 `time`만 바꾼다 — 전역 time.monotonic은 이벤트 루프도 쓴다
    monkeypatch.setattr(rate_limit, "time", SimpleNamespace(monotonic=c.monotonic))
    return c


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """레이트 리밋이 켜진 설정 + 대역 Redis. 공급자는 테스트가 정한다."""
    state: dict[str, Any] = {"settings": make_settings(app_env="dev", rate_limit_search=3)}
    monkeypatch.setattr(rate_limit, "get_settings", lambda: state["settings"])
    monkeypatch.setattr(rate_limit, "Redis", _FakeRedis)
    _FakeRedis.store = {}
    previous = rate_limit._override_provider
    rate_limit.set_override_provider(None)
    yield state
    rate_limit.set_override_provider(previous)


class _Provider:
    def __init__(self, values: dict[str, int]) -> None:
        self.values = values
        self.calls = 0
        self.fail = False

    async def __call__(self) -> dict[str, int]:
        self.calls += 1
        if self.fail:
            raise ConnectionError("db down")
        return dict(self.values)


def _request() -> Request:
    return Request({"type": "http", "headers": [], "client": ("203.0.113.7", 1)})


async def _hits_until_429(limiter: RateLimiter, attempts: int = 20) -> int:
    """429가 나기 전까지 통과한 요청 수."""
    for n in range(attempts):
        try:
            await limiter(_request())
        except HTTPException as e:
            assert e.status_code == 429
            return n
    return attempts


# ── 수락 기준 2: 설정이 없으면 지금과 같다(env 값) ─────────────────────────


async def test_without_provider_env_value_applies(env) -> None:
    limiter = RateLimiter("search", scope="user", limit_setting="rate_limit_search")
    assert await _hits_until_429(limiter) == 3


async def test_provider_without_value_for_key_uses_env(env, clock) -> None:
    rate_limit.set_override_provider(_Provider({"rate_limit_chat": 1}))
    limiter = RateLimiter("search", scope="user", limit_setting="rate_limit_search")
    assert await _hits_until_429(limiter) == 3


async def test_fixed_limit_without_setting_ignores_overrides(env, clock) -> None:
    provider = _Provider({"rate_limit_search": 1})
    rate_limit.set_override_provider(provider)
    limiter = RateLimiter("login", limit=2)
    assert await _hits_until_429(limiter) == 2
    assert provider.calls == 0


async def test_env_zero_still_disables_without_override(env, clock) -> None:
    env["settings"] = make_settings(app_env="dev", rate_limit_search=0)
    rate_limit.set_override_provider(_Provider({}))
    limiter = RateLimiter("search", scope="user", limit_setting="rate_limit_search")
    assert await _hits_until_429(limiter) == 20


async def test_global_switch_stays_env_only(env, clock) -> None:
    """`rate_limit_enabled=false`면 재정의가 있어도 제한하지 않는다(env 전용 비상 경로)."""
    env["settings"] = make_settings(app_env="dev", rate_limit_enabled=False)
    provider = _Provider({"rate_limit_search": 1})
    rate_limit.set_override_provider(provider)
    limiter = RateLimiter("search", scope="user", limit_setting="rate_limit_search")
    assert await _hits_until_429(limiter) == 20
    assert provider.calls == 0


# ── 수락 기준 1: 재기동 없이 캐시 주기 안에 적용 ────────────────────────────


async def test_override_takes_precedence_over_env(env, clock) -> None:
    rate_limit.set_override_provider(_Provider({"rate_limit_search": 5}))
    limiter = RateLimiter("search", scope="user", limit_setting="rate_limit_search")
    assert await _hits_until_429(limiter) == 5


async def test_changed_value_applies_after_cache_period(env, clock) -> None:
    provider = _Provider({"rate_limit_search": 5})
    rate_limit.set_override_provider(provider)
    limiter = RateLimiter("search", scope="user", limit_setting="rate_limit_search")
    assert await limiter._resolve_limit() == 5

    provider.values = {"rate_limit_search": 7}  # 다른 프로세스가 바꿨다
    clock.now += rate_limit.RATE_LIMIT_OVERRIDE_TTL_SECONDS - 1
    assert await limiter._resolve_limit() == 5  # 주기 안 — 요청마다 DB를 읽지 않는다
    assert provider.calls == 1

    clock.now += 1
    assert await limiter._resolve_limit() == 7  # 주기가 지나면 재기동 없이 반영
    assert provider.calls == 2


async def test_invalidate_applies_immediately(env, clock) -> None:
    provider = _Provider({"rate_limit_search": 5})
    rate_limit.set_override_provider(provider)
    limiter = RateLimiter("search", scope="user", limit_setting="rate_limit_search")
    assert await limiter._resolve_limit() == 5
    provider.values = {}
    rate_limit.invalidate_overrides()  # 이 프로세스에서 PUT한 직후
    assert await limiter._resolve_limit() == 3


async def test_provider_failure_keeps_last_values_and_does_not_block(env, clock) -> None:
    provider = _Provider({"rate_limit_search": 5})
    rate_limit.set_override_provider(provider)
    limiter = RateLimiter("search", scope="user", limit_setting="rate_limit_search")
    assert await limiter._resolve_limit() == 5
    provider.fail = True
    clock.now += rate_limit.RATE_LIMIT_OVERRIDE_TTL_SECONDS
    assert await limiter._resolve_limit() == 5  # 직전 값


async def test_provider_failure_on_first_load_falls_back_to_env(env, clock) -> None:
    provider = _Provider({"rate_limit_search": 5})
    provider.fail = True
    rate_limit.set_override_provider(provider)
    limiter = RateLimiter("search", scope="user", limit_setting="rate_limit_search")
    assert await _hits_until_429(limiter) == 3
    assert provider.calls == 1  # 실패해도 주기 동안 다시 부르지 않는다


# ── 저장값 검증·계약 ────────────────────────────────────────────────────────


def test_stored_values_outside_range_are_ignored() -> None:
    raw = {
        "rate_limit_chat": 0,
        "rate_limit_search": RATE_LIMIT_OVERRIDE_MAX + 1,
        "rate_limit_web_search": True,
        "rate_limit_source_create": "10",
        "rate_limit_enabled": 1,  # 재정의 대상 아님
        "rate_limit_agent_run": 4,
    }
    assert rate_limit_settings.valid_overrides(raw) == {"rate_limit_agent_run": 4}


def test_keys_are_core_setting_fields_and_update_fields() -> None:
    assert set(RATE_LIMIT_KEYS) == set(get_args(RateLimitKey))
    assert set(RATE_LIMIT_KEYS) == set(RateLimitsUpdate.model_fields)
    for key in RATE_LIMIT_KEYS:
        assert isinstance(CoreSettings.model_fields[key].default, int)
    assert "rate_limit_enabled" not in RATE_LIMIT_KEYS
    assert RATE_LIMIT_OVERRIDE_MIN == 1


@pytest.mark.parametrize("value", [0, -1, RATE_LIMIT_OVERRIDE_MAX + 1])
def test_update_schema_rejects_out_of_range(value: int) -> None:
    with pytest.raises(ValueError):
        RateLimitsUpdate(rate_limit_chat=value)


def test_update_schema_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError):
        RateLimitsUpdate.model_validate({"rate_limit_enabled": 1})


def test_describe_marks_effective_value() -> None:
    settings = make_settings(rate_limit_chat=30)
    items = {
        i["key"]: i
        for i in rate_limit_settings.describe_rate_limits(settings, {"rate_limit_chat": 50})
    }
    assert items["rate_limit_chat"] == {
        "key": "rate_limit_chat",
        "env_value": 30,
        "override": 50,
        "effective": 50,
    }
    assert items["rate_limit_search"]["override"] is None
    assert items["rate_limit_search"]["effective"] == items["rate_limit_search"]["env_value"]


def test_deps_import_installs_db_provider() -> None:
    import biznotes_core.api.deps  # noqa: F401

    rate_limit_settings.install_override_provider()
    assert rate_limit._override_provider is rate_limit_settings.load_rate_limit_overrides
