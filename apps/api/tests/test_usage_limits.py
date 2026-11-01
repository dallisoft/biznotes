"""16b 사용자별 일일 사용량 한도 — 가드 단위 + API 통합 테스트.

Redis db 15(테스트 전용) 카운터 사용 — user_id가 테스트마다 랜덤이라 충돌 없음.
"""

import uuid

import httpx
import pytest
from biznotes_core.models import ChatSession, LLMModel, LLMProvider, Notebook, UsageLimit
from biznotes_core.services.usage_guard import (
    UsageLimitExceeded,
    check_usage,
    get_today_usage,
    record_usage,
)

from tests.conftest import bearer, login


async def _set_limits(db, user_id, **kwargs):
    db.add(UsageLimit(user_id=user_id, **kwargs))
    await db.commit()


# ---------- 가드 단위 ----------


async def test_no_limit_row_is_unlimited(db, user_a):
    await record_usage(user_a.id, "tokens", 10_000)
    await check_usage(db, user_a.id, "user", "tokens")  # 예외 없음


async def test_check_raises_when_over_limit(db, user_a):
    await _set_limits(db, user_a.id, daily_tokens=100)
    await record_usage(user_a.id, "tokens", 100)
    with pytest.raises(UsageLimitExceeded) as exc:
        await check_usage(db, user_a.id, "user", "tokens")
    assert "한도를 초과" in str(exc.value)


async def test_check_counts_pending_amount(db, user_a):
    """amount 포함 검사 — 사전 크기를 아는 경우."""
    await _set_limits(db, user_a.id, daily_tokens=1000)
    await record_usage(user_a.id, "tokens", 900)
    await check_usage(db, user_a.id, "user", "tokens", amount=100)  # 정확히 한도 — 허용
    with pytest.raises(UsageLimitExceeded):
        await check_usage(db, user_a.id, "user", "tokens", amount=101)


async def test_admin_role_is_exempt(db, user_a):
    await _set_limits(db, user_a.id, daily_agent_runs=0)
    await check_usage(db, user_a.id, "admin", "agent_runs")  # 예외 없음


async def test_today_usage_counters(db, user_a):
    await record_usage(user_a.id, "tokens", 42)
    await record_usage(user_a.id, "agent_runs", 1)
    today = await get_today_usage(user_a.id)
    assert today == {"tokens": 42, "agent_runs": 1}


# ---------- API 통합 ----------


@pytest.fixture
async def chat_setup(db, user_a):
    provider = LLMProvider(name=f"mock-{uuid.uuid4().hex[:6]}", type="mock", enabled=True)
    db.add(provider)
    await db.flush()
    db.add(
        LLMModel(
            provider_id=provider.id,
            model_name="mock-chat",
            display_name="Mock Chat",
            capabilities=["llm"],
            is_default=True,
            enabled=True,
        )
    )
    nb = Notebook(owner_id=user_a.id, title="limit-nb")
    db.add(nb)
    await db.flush()
    session = ChatSession(notebook_id=nb.id, user_id=user_a.id)
    db.add(session)
    await db.commit()
    return session


async def test_chat_blocked_when_token_limit_exceeded(
    client: httpx.AsyncClient, db, user_a, chat_setup
):
    await _set_limits(db, user_a.id, daily_tokens=10)
    await record_usage(user_a.id, "tokens", 10)

    token = await login(client, user_a.email)
    client.cookies.clear()
    res = await client.post(
        f"/api/v1/chat/sessions/{chat_setup.id}/messages",
        json={"content": "차단 확인", "source_ids": None, "model_id": None},
        headers=bearer(token),
    )
    assert res.status_code == 429
    assert "한도를 초과" in res.json()["detail"]


async def test_chat_allowed_and_records_usage(
    client: httpx.AsyncClient, db, user_a, chat_setup
):
    await _set_limits(db, user_a.id, daily_tokens=100_000)
    token = await login(client, user_a.email)
    client.cookies.clear()
    res = await client.post(
        f"/api/v1/chat/sessions/{chat_setup.id}/messages",
        json={"content": "사용량 기록 확인", "source_ids": None, "model_id": None},
        headers=bearer(token),
    )
    assert res.status_code == 200
    body = res.text
    assert '"type": "done"' in body
    today = await get_today_usage(user_a.id)
    assert today["tokens"] > 0  # mock 응답 토큰이 기록됨


async def test_admin_limits_api(client: httpx.AsyncClient, db, user_a):
    admin = await login(client, (await _make_admin(db)).email)
    client.cookies.clear()

    # 기본: 전부 null(무제한)
    res = await client.get(f"/api/v1/admin/users/{user_a.id}/limits", headers=bearer(admin))
    assert res.status_code == 200
    assert res.json()["daily_tokens"] is None

    # 설정 → 반영 + 오늘 사용량 동봉
    await record_usage(user_a.id, "tokens", 7)
    await record_usage(user_a.id, "agent_runs", 2)
    res = await client.put(
        f"/api/v1/admin/users/{user_a.id}/limits",
        json={"daily_tokens": 5000, "daily_agent_runs": 3},
        headers=bearer(admin),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["daily_tokens"] == 5000 and body["daily_agent_runs"] == 3
    assert body["today"] == {"tokens": 7, "agent_runs": 2}

    res = await client.get(f"/api/v1/admin/users/{user_a.id}/limits", headers=bearer(admin))
    assert res.json()["daily_tokens"] == 5000
    assert res.json()["daily_agent_runs"] == 3


async def test_usage_defaults_reach_users_without_own_limit(
    client: httpx.AsyncClient, db, user_a, user_b
):
    """일일 한도 기본값(코어 v0.26.0 #387) — 사용자별 한도 행이 없는 사용자도 기본값을 받는다.

    **동작 변화**: 전에는 행이 없으면 무제한이었다. `/admin/users/{id}/limits`의 `effective`가
    적용값과 출처(`user`·`default`·`unlimited`)를 보이고(「사용자·그룹」 한도 다이얼로그, #228
    AC-14), 사용자별 값이 있으면 그 값이 우선한다. 기본값을 지우면 행 없는 사용자는 다시 무제한이다.
    """
    admin = await login(client, (await _make_admin(db)).email)
    client.cookies.clear()

    put = await client.put(
        "/api/v1/admin/usage-defaults", json={"daily_tokens": 1000}, headers=bearer(admin)
    )
    assert put.status_code == 200, put.text

    # 행이 없는 사용자 — 기존 필드는 사용자별 값(null) 그대로, 적용값은 기본값
    res = await client.get(f"/api/v1/admin/users/{user_a.id}/limits", headers=bearer(admin))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["daily_tokens"] is None
    assert body["effective"] == {
        "daily_tokens": {"value": 1000, "source": "default"},
        "daily_agent_runs": {"value": None, "source": "unlimited"},
    }
    # 가드도 같은 값으로 막는다
    await record_usage(user_a.id, "tokens", 1000)
    with pytest.raises(UsageLimitExceeded):
        await check_usage(db, user_a.id, "user", "tokens")

    # 사용자별 값이 있으면 그 값이 우선한다(PUT 응답에도 effective가 붙는다)
    own = await client.put(
        f"/api/v1/admin/users/{user_b.id}/limits",
        json={"daily_tokens": 5000, "daily_agent_runs": None},
        headers=bearer(admin),
    )
    assert own.status_code == 200, own.text
    assert own.json()["effective"]["daily_tokens"] == {"value": 5000, "source": "user"}

    # 기본값을 지우면(null) 행 없는 사용자는 다시 무제한이다
    cleared = await client.put(
        "/api/v1/admin/usage-defaults", json={"daily_tokens": None}, headers=bearer(admin)
    )
    assert cleared.status_code == 200, cleared.text
    res = await client.get(f"/api/v1/admin/users/{user_a.id}/limits", headers=bearer(admin))
    assert res.json()["effective"]["daily_tokens"] == {"value": None, "source": "unlimited"}
    await check_usage(db, user_a.id, "user", "tokens")  # 예외 없음


async def test_limits_api_requires_admin(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)
    client.cookies.clear()
    res = await client.get(f"/api/v1/admin/users/{user_a.id}/limits", headers=bearer(token))
    assert res.status_code == 403


async def _make_admin(db):
    from tests.conftest import _create_user

    return await _create_user(db, "admin-limits", role="admin")
