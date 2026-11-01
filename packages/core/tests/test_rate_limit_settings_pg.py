# 신규(#386). 필요: PostgreSQL·Valkey(PG 테스트).
"""분당 레이트 리밋의 관리자 재정의 — 실제 라우터·인증·DB·감사 로그·Redis 리미터.

`GET`·`PUT /admin/rate-limits`. 제품처럼 `/admin` 집계 라우터에 `require_admin`을 걸어 조립하고,
가드 없이 조립한 경우도 경로 자체가 관리자를 확인하는지 본다.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import select
from starlette.requests import Request

from biznotes_core.api.deps import require_admin
from biznotes_core.api.v1 import auth
from biznotes_core.api.v1.admin import limits_router
from biznotes_core.config import get_settings
from biznotes_core.core import rate_limit
from biznotes_core.core.db import get_session_factory
from biznotes_core.core.rate_limit import RateLimiter
from biznotes_core.models import AuditLog, SystemSetting, User
from biznotes_core.schemas.limits import RATE_LIMIT_OVERRIDE_MAX
from biznotes_core.services import rate_limit_settings

PASSWORD = "pw-test-1234"  # tests/conftest.py 공용 해시의 평문


def _app(*, guarded: bool = True) -> FastAPI:
    app = FastAPI()
    admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)] if guarded else [])
    admin.include_router(limits_router)
    for router in (auth.router, admin):
        app.include_router(router, prefix="/api/v1")
    return app


class _Api:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.tokens: dict[uuid.UUID, str] = {}

    async def call(self, user: User, method: str, path: str, **kw: Any) -> httpx.Response:
        if user.id not in self.tokens:
            self.client.cookies.clear()
            res = await self.client.post(
                "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
            )
            assert res.status_code == 200, res.text
            self.tokens[user.id] = res.json()["access_token"]
        self.client.cookies.clear()
        return await self.client.request(
            method,
            f"/api/v1{path}",
            headers={"Authorization": f"Bearer {self.tokens[user.id]}"},
            **kw,
        )


async def _client(app: FastAPI) -> AsyncIterator[_Api]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield _Api(client)


@pytest.fixture
async def api() -> AsyncIterator[_Api]:
    async for a in _client(_app()):
        yield a


@pytest.fixture
async def unguarded_api() -> AsyncIterator[_Api]:
    async for a in _client(_app(guarded=False)):
        yield a


@pytest.fixture(autouse=True)
def _provider() -> Any:
    """DB 공급자를 쓰고, 테스트 사이로 캐시가 새지 않게 비운다."""
    rate_limit_settings.install_override_provider()
    yield
    rate_limit.invalidate_overrides()


async def _stored() -> dict[str, Any] | None:
    async with get_session_factory()() as s:
        row = await s.get(SystemSetting, "rate_limits")
        return None if row is None else dict(row.value)


async def _audits() -> list[AuditLog]:
    async with get_session_factory()() as s:
        rows = await s.execute(select(AuditLog).where(AuditLog.action == "rate_limits.update"))
        return list(rows.scalars().all())


def _items(res: httpx.Response) -> dict[str, dict[str, Any]]:
    return {i["key"]: i for i in res.json()["items"]}


# ── 수락 기준 2: 설정이 없으면 env 값 ───────────────────────────────────────


async def test_get_without_overrides_shows_env_values(api, admin_user) -> None:
    res = await api.call(admin_user, "GET", "/admin/rate-limits")
    assert res.status_code == 200, res.text
    body = res.json()
    settings = get_settings()
    assert body["enabled"] == settings.rate_limit_enabled
    assert body["cache_ttl_seconds"] == rate_limit.RATE_LIMIT_OVERRIDE_TTL_SECONDS
    assert (body["min_value"], body["max_value"]) == (1, RATE_LIMIT_OVERRIDE_MAX)
    items = _items(res)
    assert set(items) == set(rate_limit_settings.RATE_LIMIT_KEYS)
    for key, item in items.items():
        env_value = getattr(settings, key)
        assert item == {
            "key": key,
            "env_value": env_value,
            "override": None,
            "effective": env_value,
        }
    assert await rate_limit_settings.load_rate_limit_overrides() == {}


# ── 수락 기준 3: 일반 사용자 403 ────────────────────────────────────────────


@pytest.mark.parametrize("guarded", [True, False], ids=["admin-router", "route-guard"])
async def test_regular_user_gets_403(guarded, api, unguarded_api, user_a) -> None:
    client = api if guarded else unguarded_api
    assert (await client.call(user_a, "GET", "/admin/rate-limits")).status_code == 403
    res = await client.call(user_a, "PUT", "/admin/rate-limits", json={"rate_limit_chat": 5})
    assert res.status_code == 403
    assert await _stored() is None
    assert await _audits() == []


# ── 수락 기준 4: 범위 밖 422, 변경은 감사 로그 ──────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        {"rate_limit_chat": 0},
        {"rate_limit_chat": -1},
        {"rate_limit_chat": RATE_LIMIT_OVERRIDE_MAX + 1},
        {"rate_limit_chat": "many"},
        {"rate_limit_enabled": False},  # 전체 끄기는 env 전용
        {"rate_limit_login": 5},
    ],
    ids=["zero", "negative", "over-max", "not-int", "global-switch", "unknown-key"],
)
async def test_out_of_range_is_422_and_writes_nothing(api, admin_user, body) -> None:
    res = await api.call(admin_user, "PUT", "/admin/rate-limits", json=body)
    assert res.status_code == 422, res.text
    assert await _stored() is None
    assert await _audits() == []


async def test_put_stores_override_and_audits(api, admin_user) -> None:
    res = await api.call(
        admin_user,
        "PUT",
        "/admin/rate-limits",
        json={"rate_limit_chat": 50, "rate_limit_search": RATE_LIMIT_OVERRIDE_MAX},
    )
    assert res.status_code == 200, res.text
    items = _items(res)
    assert items["rate_limit_chat"]["override"] == 50
    assert items["rate_limit_chat"]["effective"] == 50
    assert items["rate_limit_search"]["effective"] == RATE_LIMIT_OVERRIDE_MAX
    assert items["rate_limit_agent_run"]["override"] is None
    assert await _stored() == {"rate_limit_chat": 50, "rate_limit_search": RATE_LIMIT_OVERRIDE_MAX}

    [log] = await _audits()
    assert log.user_id == admin_user.id
    assert log.resource == "rate_limits"
    assert log.detail == {
        "changes": {"rate_limit_chat": 50, "rate_limit_search": RATE_LIMIT_OVERRIDE_MAX},
        "before": {},
        "after": {"rate_limit_chat": 50, "rate_limit_search": RATE_LIMIT_OVERRIDE_MAX},
    }

    # 부분 갱신 — 없는 키는 그대로, null은 재정의 삭제(env로 복귀)
    res = await api.call(admin_user, "PUT", "/admin/rate-limits", json={"rate_limit_search": None})
    assert res.status_code == 200, res.text
    items = _items(res)
    assert items["rate_limit_chat"]["effective"] == 50
    assert items["rate_limit_search"]["override"] is None
    assert items["rate_limit_search"]["effective"] == get_settings().rate_limit_search
    assert await _stored() == {"rate_limit_chat": 50}
    assert len(await _audits()) == 2

    get = await api.call(admin_user, "GET", "/admin/rate-limits")
    assert _items(get) == items


async def test_invalid_stored_values_fall_back_to_env(api, admin_user, db) -> None:
    """DB를 직접 고친 범위 밖 값은 판정에 쓰지 않는다."""
    db.add(SystemSetting(key="rate_limits", value={"rate_limit_chat": 0, "rate_limit_search": 9}))
    await db.commit()
    assert await rate_limit_settings.load_rate_limit_overrides() == {"rate_limit_search": 9}
    items = _items(await api.call(admin_user, "GET", "/admin/rate-limits"))
    assert items["rate_limit_chat"]["override"] is None
    assert items["rate_limit_search"]["effective"] == 9


# ── 수락 기준 1: 재기동 없이 적용 — 실제 Redis 리미터 ─────────────────────


async def test_put_value_is_enforced_by_limiter_without_restart(
    api, admin_user, monkeypatch
) -> None:
    settings = get_settings().model_copy(update={"app_env": "dev", "rate_limit_enabled": True})
    monkeypatch.setattr(rate_limit, "get_settings", lambda: settings)
    limiter = RateLimiter(f"pg-{uuid.uuid4().hex}", scope="user", limit_setting="rate_limit_search")
    assert await limiter._resolve_limit() == settings.rate_limit_search  # 재정의 전 = env

    res = await api.call(admin_user, "PUT", "/admin/rate-limits", json={"rate_limit_search": 2})
    assert res.status_code == 200, res.text

    request = Request({"type": "http", "headers": [], "client": ("198.51.100.20", 1)})
    await limiter(request)
    await limiter(request)
    with pytest.raises(Exception) as exc:
        await limiter(request)
    assert getattr(exc.value, "status_code", None) == 429
