# 신규(#387). 필요: PostgreSQL·Valkey(PG 테스트).
"""일일 사용 한도 기본값 — 실제 한도 검사(DB·Redis 카운터)·관리자 API·감사 로그.

우선순위는 사용자별 값 > 시스템 기본값(`system_settings["usage_defaults"]`) > 무제한이다. 사용자별
값이 null이면 기본값을 따른다(이슈 권고).
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import select

from biznotes_core.api.deps import require_admin
from biznotes_core.api.v1 import auth
from biznotes_core.api.v1.admin import limits_router, users_router
from biznotes_core.core.db import get_session_factory
from biznotes_core.models import AuditLog, SystemSetting, UsageLimit, User
from biznotes_core.schemas.limits import USAGE_LIMIT_MAX
from biznotes_core.services import usage_guard
from biznotes_core.services.usage_guard import UsageLimitExceeded, check_usage, record_usage

PASSWORD = "pw-test-1234"  # tests/conftest.py 공용 해시의 평문


def _app(*, guarded: bool = True) -> FastAPI:
    app = FastAPI()
    admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)] if guarded else [])
    admin.include_router(users_router)
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


async def _set_defaults(db, **values: int) -> None:
    db.add(SystemSetting(key=usage_guard.USAGE_DEFAULTS_KEY, value=values))
    await db.commit()


async def _set_user_limits(db, user: User, **values: int | None) -> None:
    db.add(UsageLimit(user_id=user.id, **values))
    await db.commit()


async def _passes(db, user: User, amount: int, kind: str = "agent_runs") -> bool:
    """요청마다 새 세션처럼 — 다른 세션(API)이 바꾼 값을 identity map이 가리지 않게."""
    async with get_session_factory()() as session:
        try:
            await check_usage(session, user.id, user.role, kind, amount)
        except UsageLimitExceeded:
            return False
    return True


async def _stored() -> dict[str, Any] | None:
    async with get_session_factory()() as s:
        row = await s.get(SystemSetting, usage_guard.USAGE_DEFAULTS_KEY)
        return None if row is None else dict(row.value)


async def _audits() -> list[AuditLog]:
    async with get_session_factory()() as s:
        rows = await s.execute(select(AuditLog).where(AuditLog.action == "usage_defaults.update"))
        return list(rows.scalars().all())


# ── 수락 기준: 한도 검사 ────────────────────────────────────────────────────


async def test_no_row_and_no_default_is_unlimited(db, user_a) -> None:
    """기본값 미설정이면 지금과 같다(무제한)."""
    assert await _passes(db, user_a, 10**9)


async def test_default_applies_to_user_without_row(db, user_a) -> None:
    await _set_defaults(db, daily_agent_runs=3)
    assert await _passes(db, user_a, 3)
    assert not await _passes(db, user_a, 4)
    # 카운터 누적도 기본값으로 판정한다
    await record_usage(user_a.id, "agent_runs", 3)
    async with get_session_factory()() as session:
        with pytest.raises(UsageLimitExceeded) as exc:
            await check_usage(session, user_a.id, "user", "agent_runs")
    assert (exc.value.used, exc.value.limit) == (3, 3)


async def test_user_value_takes_precedence_over_default(db, user_a) -> None:
    await _set_defaults(db, daily_agent_runs=3, daily_tokens=100)
    await _set_user_limits(db, user_a, daily_agent_runs=10)
    assert await _passes(db, user_a, 10)  # 사용자 값이 기본값보다 크다
    assert not await _passes(db, user_a, 11)
    # 같은 행이라도 null인 필드는 기본값을 따른다(이슈 권고)
    assert await _passes(db, user_a, 100, kind="tokens")
    assert not await _passes(db, user_a, 101, kind="tokens")


async def test_user_value_smaller_than_default_also_wins(db, user_a) -> None:
    await _set_defaults(db, daily_tokens=1000)
    await _set_user_limits(db, user_a, daily_tokens=0)
    assert not await _passes(db, user_a, 1, kind="tokens")


async def test_admin_still_passes(db, admin_user) -> None:
    await _set_defaults(db, daily_agent_runs=0, daily_tokens=0)
    assert await _passes(db, admin_user, 10**9)
    assert await _passes(db, admin_user, 10**9, kind="tokens")


async def test_out_of_range_stored_default_is_ignored(db, user_a) -> None:
    db.add(
        SystemSetting(
            key=usage_guard.USAGE_DEFAULTS_KEY,
            value={"daily_agent_runs": -1, "daily_tokens": "5", "daily_stt_minutes": 2},
        )
    )
    await db.commit()
    # 범위 밖·형식 밖 값과 지금 없는 필드(옛 판의 STT 한도)는 무시한다
    assert await usage_guard.get_usage_defaults(db) == {}
    assert await _passes(db, user_a, 10**6)


# ── 관리자 API ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("guarded", [True, False], ids=["admin-router", "route-guard"])
async def test_regular_user_gets_403(guarded, api, unguarded_api, user_a) -> None:
    client = api if guarded else unguarded_api
    assert (await client.call(user_a, "GET", "/admin/usage-defaults")).status_code == 403
    res = await client.call(user_a, "PUT", "/admin/usage-defaults", json={"daily_tokens": 1})
    assert res.status_code == 403
    assert await _stored() is None


@pytest.mark.parametrize(
    "body",
    [
        {"daily_tokens": -1},
        {"daily_tokens": USAGE_LIMIT_MAX + 1},
        {"daily_tokens": "lots"},
        {"monthly_tokens": 5},
    ],
    ids=["negative", "over-max", "not-int", "unknown-key"],
)
async def test_out_of_range_is_422_and_writes_nothing(api, admin_user, body) -> None:
    res = await api.call(admin_user, "PUT", "/admin/usage-defaults", json=body)
    assert res.status_code == 422, res.text
    assert await _stored() is None
    assert await _audits() == []


async def test_put_get_and_audit(api, admin_user, db, user_a) -> None:
    empty = await api.call(admin_user, "GET", "/admin/usage-defaults")
    assert empty.status_code == 200
    assert empty.json() == {
        "daily_tokens": None,
        "daily_agent_runs": None,
    }

    res = await api.call(
        admin_user,
        "PUT",
        "/admin/usage-defaults",
        json={"daily_tokens": 5000, "daily_agent_runs": 0},
    )
    assert res.status_code == 200, res.text
    assert res.json()["daily_tokens"] == 5000
    assert res.json()["daily_agent_runs"] == 0
    assert await _stored() == {"daily_tokens": 5000, "daily_agent_runs": 0}
    [log] = await _audits()
    assert log.user_id == admin_user.id
    assert log.resource == "usage_defaults"
    assert log.detail["after"] == {"daily_tokens": 5000, "daily_agent_runs": 0}
    assert not await _passes(db, user_a, 1)  # 재기동 없이 다음 검사부터

    # 부분 갱신 — null은 기본값 삭제
    res = await api.call(
        admin_user, "PUT", "/admin/usage-defaults", json={"daily_agent_runs": None}
    )
    assert res.json()["daily_tokens"] == 5000
    assert res.json()["daily_agent_runs"] is None
    assert await _stored() == {"daily_tokens": 5000}
    assert len(await _audits()) == 2
    assert await _passes(db, user_a, 10**6)


# ── GET /admin/users/{id}/limits — 적용값·출처(하위 호환 필드 추가) ──────────


async def test_user_limits_report_effective_value_and_source(api, admin_user, db, user_a) -> None:
    res = await api.call(admin_user, "GET", f"/admin/users/{user_a.id}/limits")
    assert res.status_code == 200, res.text
    body = res.json()
    # 기존 필드는 그대로(사용자별 값, 행이 없으면 null)
    assert body["daily_tokens"] is None and body["daily_agent_runs"] is None
    assert "today" in body
    assert body["effective"]["daily_tokens"] == {"value": None, "source": "unlimited"}

    await _set_defaults(db, daily_tokens=100, daily_agent_runs=3)
    await _set_user_limits(db, user_a, daily_agent_runs=7)
    body = (await api.call(admin_user, "GET", f"/admin/users/{user_a.id}/limits")).json()
    assert body["daily_agent_runs"] == 7
    assert body["daily_tokens"] is None
    assert body["effective"] == {
        "daily_tokens": {"value": 100, "source": "default"},
        "daily_agent_runs": {"value": 7, "source": "user"},
    }

    # PUT 응답도 같은 필드를 싣는다 — null로 바꾸면 기본값을 따른다
    put = await api.call(
        admin_user,
        "PUT",
        f"/admin/users/{user_a.id}/limits",
        json={"daily_agent_runs": None, "daily_tokens": 9},
    )
    assert put.status_code == 200, put.text
    assert put.json()["effective"]["daily_agent_runs"] == {"value": 3, "source": "default"}
    assert put.json()["effective"]["daily_tokens"] == {"value": 9, "source": "user"}
