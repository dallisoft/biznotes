"""관리 › 사용 제한 — 채팅 1회 한도·source_load 상한·HITL 승인 대기 설정 API(#228)."""

import asyncio
import inspect
import uuid
from datetime import timedelta

import httpx
import pytest
from biznotes_core.models import AuditLog, SystemSetting
from biznotes_core.services.agents.nodes.llm import (
    SOURCE_LOAD_TOOL_DEFAULTS,
    SOURCE_LOAD_TOOL_HARD_CAPS,
)
from biznotes_core.services.agents.react.loop import REACT_DEFAULTS, REACT_HARD_CAPS
from biznotes_core.services.rag_chat import build_messages
from biznotes_core.services.settings_store import DEFAULTS as SETTING_DEFAULTS
from biznotes_core.services.settings_store import get_setting, set_setting
from sqlalchemy import select, text

from app.services.limits import (
    LIMIT_BOUNDS,
    approval_timeout_minutes,
    chat_token_budget,
    get_limit_settings,
)
from tests.conftest import bearer, login

LIMITS_PATH = "/api/v1/admin/limits"

# 저장값이 없을 때 적용되는 값
DEFAULTS = {
    "chat_token_budget": 100_000,
    "chat_cost_budget_usd": 0.5,
    "chat_max_iters": 6,
    "chat_max_tokens": 4096,
    "chat_history_messages": 6,
    "source_load_max_sources": 5,
    "source_load_max_chars": 60_000,
    "source_load_budget_ratio": 0.3,
    "approval_timeout_minutes": 10,
}


def _out_of_range() -> list[tuple[str, float]]:
    """항목마다 최솟값 바로 아래·최댓값 바로 위 값."""
    cases: list[tuple[str, float]] = []
    for name, (default, minimum, maximum) in LIMIT_BOUNDS.items():
        step = 0.01 if isinstance(default, float) else 1
        cases += [(name, round(minimum - step, 2)), (name, round(maximum + step, 2))]
    return cases


async def _audit_rows(db) -> list[AuditLog]:
    rows = await db.execute(select(AuditLog).where(AuditLog.action == "limits.settings_update"))
    return list(rows.scalars().all())


def test_limit_bounds_follow_core_constants():
    """기본값·상한은 코어 공개 상수를 따른다.

    상한을 넘기면 코어가 조용히 깎아 화면 값과 어긋난다.
    """
    for name, key in (
        ("chat_token_budget", "token_budget"),
        ("chat_cost_budget_usd", "cost_budget_usd"),
        ("chat_max_iters", "max_iters"),
    ):
        default, _, maximum = LIMIT_BOUNDS[name]
        assert (default, maximum) == (REACT_DEFAULTS[key], REACT_HARD_CAPS[key]), name
    for name, key in (
        ("source_load_max_sources", "max_sources"),
        ("source_load_max_chars", "max_chars"),
    ):
        default, _, maximum = LIMIT_BOUNDS[name]
        assert (default, maximum) == (
            SOURCE_LOAD_TOOL_DEFAULTS[key],
            SOURCE_LOAD_TOOL_HARD_CAPS[key],
        ), name
    assert (
        LIMIT_BOUNDS["approval_timeout_minutes"][0]
        == SETTING_DEFAULTS["agents"]["approval_timeout_minutes"]
    )
    # 이력 기본값은 코어 정적 경로 `build_messages`의 기본값과 같다
    max_history = inspect.signature(build_messages).parameters["max_history"].default
    assert LIMIT_BOUNDS["chat_history_messages"][0] == max_history
    for name, (default, minimum, maximum) in LIMIT_BOUNDS.items():
        assert minimum <= default <= maximum, name


def test_limit_bounds_match_web_form():
    """웹 「사용 제한」(`admin/limits/request-limit-fields.tsx`)이 보이는 기본값·범위와 같다.

    코어 값이 바뀌면 이 숫자와 웹 상수를 함께 고친다.
    """
    assert LIMIT_BOUNDS == {
        "chat_token_budget": (100_000, 10_000, 200_000),
        "chat_cost_budget_usd": (0.5, 0.05, 2.0),
        "chat_max_iters": (6, 1, 12),
        "chat_max_tokens": (4096, 512, 16_384),
        "chat_history_messages": (6, 0, 20),
        "source_load_max_sources": (5, 1, 20),
        "source_load_max_chars": (60_000, 5_000, 200_000),
        "source_load_budget_ratio": (0.3, 0.0, 1.0),
        "approval_timeout_minutes": (10, 1, 1_440),
    }
    assert {name: bounds[0] for name, bounds in LIMIT_BOUNDS.items()} == DEFAULTS


async def test_get_returns_default_when_unset(client: httpx.AsyncClient, admin_user):
    token = await login(client, admin_user.email)
    res = await client.get(LIMITS_PATH, headers=bearer(token))
    assert res.status_code == 200
    assert res.json() == DEFAULTS


async def test_put_saves_values_and_records_audit(client: httpx.AsyncClient, db, admin_user):
    token = await login(client, admin_user.email)
    changes = {
        "chat_token_budget": 180_000,
        "chat_cost_budget_usd": 1.25,
        "chat_max_iters": 10,
        "chat_max_tokens": 8192,
        "chat_history_messages": 0,
        "source_load_max_sources": 12,
        "source_load_max_chars": 150_000,
        "source_load_budget_ratio": 0.5,
        "approval_timeout_minutes": 30,
    }
    res = await client.put(LIMITS_PATH, json=changes, headers=bearer(token))
    assert res.status_code == 200, res.text
    assert res.json() == changes

    again = await client.get(LIMITS_PATH, headers=bearer(token))
    assert again.json() == changes
    assert await chat_token_budget(db) == 180_000
    # 승인 대기 시간은 워커가 읽는 코어 `agents` 키에 둔다 — `limits` 키에는 없다
    assert (await get_setting(db, "agents"))["approval_timeout_minutes"] == 30
    assert "approval_timeout_minutes" not in await get_setting(db, "limits")
    [audit] = await _audit_rows(db)
    assert audit.user_id == admin_user.id
    assert audit.resource == "limits"
    assert audit.detail == {"changes": changes, "before": DEFAULTS, "after": changes}


async def test_put_without_field_keeps_saved_value(client: httpx.AsyncClient, db, admin_user):
    """필드를 모르는 클라이언트의 PUT이 저장값을 기본값으로 되돌리지 않는다."""
    token = await login(client, admin_user.email)
    await client.put(LIMITS_PATH, json={"chat_max_iters": 3}, headers=bearer(token))
    await client.put(LIMITS_PATH, json={"approval_timeout_minutes": 60}, headers=bearer(token))
    res = await client.put(
        LIMITS_PATH, json={"source_load_max_chars": 80_000}, headers=bearer(token)
    )
    assert res.json() == {
        **DEFAULTS,
        "chat_max_iters": 3,
        "approval_timeout_minutes": 60,
        "source_load_max_chars": 80_000,
    }
    # 감사 행은 보낸 항목만 `changes`에 싣는다 — 조회 순서는 정해져 있지 않아 내용으로 찾는다
    audits = await _audit_rows(db)
    assert sorted(list(a.detail["changes"]) for a in audits) == [
        ["approval_timeout_minutes"],
        ["chat_max_iters"],
        ["source_load_max_chars"],
    ]
    [last] = [a for a in audits if "source_load_max_chars" in a.detail["changes"]]
    assert last.detail["before"]["chat_max_iters"] == 3
    assert last.detail["after"]["source_load_max_chars"] == 80_000

    # 빈 PUT·null 값은 아무것도 바꾸지 않고 감사도 남기지 않는다
    for body in ({}, {"chat_max_iters": None}):
        res = await client.put(LIMITS_PATH, json=body, headers=bearer(token))
        assert res.status_code == 200
        assert res.json()["chat_max_iters"] == 3
    assert len(await _audit_rows(db)) == 3


@pytest.mark.parametrize(("name", "value"), _out_of_range())
async def test_put_rejects_out_of_range(
    client: httpx.AsyncClient, db, admin_user, name: str, value: float
):
    token = await login(client, admin_user.email)
    res = await client.put(LIMITS_PATH, json={name: value}, headers=bearer(token))
    assert res.status_code == 422, (name, value)
    assert (await client.get(LIMITS_PATH, headers=bearer(token))).json() == DEFAULTS
    assert await _audit_rows(db) == []


@pytest.mark.parametrize("value", [{"chat_max_iters": "many"}, {"chat_max_tokens": 1024.5}])
async def test_put_rejects_non_integer_values(client: httpx.AsyncClient, admin_user, value):
    token = await login(client, admin_user.email)
    res = await client.put(LIMITS_PATH, json=value, headers=bearer(token))
    assert res.status_code == 422, value


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
async def test_put_rejects_non_finite_number_with_422(
    client: httpx.AsyncClient, db, admin_user, literal: str
):
    """[#244] 본문의 NaN·Infinity는 500이 아니라 422다 — 오류의 `input`은 JSON 문자열로 실린다."""
    token = await login(client, admin_user.email)
    res = await client.put(
        LIMITS_PATH,
        content=f'{{"chat_cost_budget_usd": {literal}}}',
        headers={**bearer(token), "Content-Type": "application/json"},
    )
    assert res.status_code == 422, res.text
    [error] = res.json()["detail"]
    assert error["loc"] == ["body", "chat_cost_budget_usd"]
    assert error["input"] == literal
    assert (await client.get(LIMITS_PATH, headers=bearer(token))).json() == DEFAULTS
    assert await _audit_rows(db) == []


async def _wait_for_setting_lock_waiters(count: int) -> None:
    """`system_settings` 행을 기다리는 세션이 `count`개가 될 때까지 기다린다(실제 PG 잠금 대기)."""
    from biznotes_core.core.db import engine

    query = text(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
        "AND wait_event_type = 'Lock' AND query ILIKE '%system_settings%'"
    )
    for _ in range(200):
        async with engine.connect() as conn:
            if (await conn.execute(query)).scalar_one() >= count:
                return
        await asyncio.sleep(0.05)
    raise AssertionError(f"설정 행을 기다리는 세션이 {count}개가 되지 않았다")


async def _put_concurrently(
    client: httpx.AsyncClient, token: str, bodies: list[dict], release
) -> list[httpx.Response]:
    """두 PUT이 같은 설정 행에서 기다리게 한 뒤 `release`로 막고 있던 세션을 푼다."""
    tasks = [
        asyncio.create_task(client.put(LIMITS_PATH, json=body, headers=bearer(token)))
        for body in bodies
    ]
    await _wait_for_setting_lock_waiters(len(bodies))
    await release()
    return list(await asyncio.wait_for(asyncio.gather(*tasks), timeout=15))


async def test_concurrent_puts_of_different_fields_keep_both(
    client: httpx.AsyncClient, db, admin_user
):
    """두 관리자가 다른 항목을 동시에 저장해도 둘 다 남는다.

    다른 세션이 행을 잠근 동안 두 PUT을 보내 같은 행에서 기다리게 한다. 잠그지 않고 읽어 합치면
    잠금이 풀린 뒤 나중 쓰기가 먼저 저장한 항목을 지운다.
    """
    token = await login(client, admin_user.email)
    await set_setting(db, "limits", {"chat_token_budget": 150_000})
    await db.commit()
    await db.get(SystemSetting, "limits", with_for_update=True, populate_existing=True)

    results = await _put_concurrently(
        client,
        token,
        [{"chat_max_iters": 3}, {"source_load_max_chars": 80_000}],
        db.rollback,
    )

    assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
    res = await client.get(LIMITS_PATH, headers=bearer(token))
    assert res.json() == {
        **DEFAULTS,
        "chat_token_budget": 150_000,
        "chat_max_iters": 3,
        "source_load_max_chars": 80_000,
    }
    audits = await _audit_rows(db)
    assert sorted(list(a.detail["changes"]) for a in audits) == [
        ["chat_max_iters"],
        ["source_load_max_chars"],
    ]


@pytest.mark.parametrize(
    ("key", "bodies"),
    [
        ("limits", [{"chat_max_iters": 3}, {"source_load_max_chars": 80_000}]),
        ("agents", [{"approval_timeout_minutes": 30}, {"approval_timeout_minutes": 45}]),
    ],
)
async def test_concurrent_first_save_does_not_conflict(
    client: httpx.AsyncClient, db, admin_user, key: str, bodies: list[dict]
):
    """행이 없을 때 동시 첫 저장이 PK 충돌로 실패하지 않고 둘 다 반영된다.

    다른 세션이 같은 키를 넣고 커밋하지 않은 동안 두 PUT을 보낸 뒤 그 세션을 되돌린다 — 두 PUT이
    첫 행을 두고 경합한다.
    """
    token = await login(client, admin_user.email)
    db.add(SystemSetting(key=key, value={}))
    await db.flush()

    results = await _put_concurrently(client, token, bodies, db.rollback)

    assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
    res = (await client.get(LIMITS_PATH, headers=bearer(token))).json()
    if key == "limits":
        assert res == {**DEFAULTS, "chat_max_iters": 3, "source_load_max_chars": 80_000}
    else:
        # 같은 항목이라 나중에 잠금을 얻은 쪽 값이 남는다
        assert res["approval_timeout_minutes"] in (30, 45)
        assert (await get_setting(db, "agents"))["approval_timeout_minutes"] in (30, 45)
    assert len(await _audit_rows(db)) == 2


async def test_non_admin_cannot_read_or_change(client: httpx.AsyncClient, db, user_a):
    """§3.3 비인가 — 일반 사용자는 조회·변경 모두 403이고 값이 바뀌지 않는다."""
    token = await login(client, user_a.email)
    assert (await client.get(LIMITS_PATH, headers=bearer(token))).status_code == 403
    res = await client.put(
        LIMITS_PATH,
        json={
            "chat_token_budget": 200_000,
            "chat_cost_budget_usd": 2.0,
            "chat_max_iters": 12,
            "source_load_budget_ratio": 1.0,
            "approval_timeout_minutes": 1_440,
        },
        headers=bearer(token),
    )
    assert res.status_code == 403
    assert await get_limit_settings(db) == DEFAULTS
    assert await _audit_rows(db) == []


async def test_stored_value_outside_bounds_is_clamped(db):
    """DB에 직접 들어간 범위 밖 값도 채팅·워커에는 범위 안으로 쓰인다."""
    above = {name: maximum * 10 + 1 for name, (_, _, maximum) in LIMIT_BOUNDS.items()}
    await set_setting(db, "limits", above)
    await set_setting(db, "agents", {"approval_timeout_minutes": 99_999})
    assert await get_limit_settings(db) == {
        name: maximum for name, (_, _, maximum) in LIMIT_BOUNDS.items()
    }

    below = {name: minimum - 5 for name, (_, minimum, _) in LIMIT_BOUNDS.items()}
    await set_setting(db, "limits", below)
    await set_setting(db, "agents", {"approval_timeout_minutes": 0})
    assert await get_limit_settings(db) == {
        name: minimum for name, (_, minimum, _) in LIMIT_BOUNDS.items()
    }

    # 읽을 수 없는 값(문자열·bool)은 기본값이다 — 예전 워커는 `int("x")`에서 실패했다
    await set_setting(db, "limits", {name: "x" for name in LIMIT_BOUNDS})
    await set_setting(db, "agents", {"approval_timeout_minutes": "x"})
    assert await get_limit_settings(db) == DEFAULTS
    await set_setting(db, "limits", {"chat_max_iters": True})
    assert (await get_limit_settings(db))["chat_max_iters"] == DEFAULTS["chat_max_iters"]
    assert await approval_timeout_minutes(db) == DEFAULTS["approval_timeout_minutes"]


async def test_worker_approval_wait_uses_admin_value(
    client: httpx.AsyncClient, db, admin_user, user_a, monkeypatch
):
    """「사용 제한」에서 저장한 승인 대기 시간이 워커가 만드는 승인의 만료 시각이 된다."""
    from biznotes_core.models import Agent, AgentRun, AgentRunApproval, Notebook

    from app.workers.tasks import agent as agent_tasks

    published: list[dict] = []
    monkeypatch.setattr(agent_tasks, "_publish", lambda _run_id, event: published.append(event))

    token = await login(client, admin_user.email)
    res = await client.put(
        LIMITS_PATH, json={"approval_timeout_minutes": 45}, headers=bearer(token)
    )
    assert res.status_code == 200, res.text

    agent = Agent(
        name=f"limits-hitl-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        icon="🤖",
        definition={"nodes": [{"id": "r", "type": "react_agent", "goal": "x"}], "edges": []},
        params_schema={},
        scope="private",
        owner_id=user_a.id,
        enabled=True,
    )
    notebook = Notebook(title="limits-hitl-nb", owner_id=user_a.id)
    db.add_all([agent, notebook])
    await db.flush()
    run = AgentRun(
        agent_id=agent.id, notebook_id=notebook.id, user_id=user_a.id, params={}, status="running"
    )
    db.add(run)
    await db.commit()

    await agent_tasks._persist_pending_approval(
        db,
        run,
        {"tool_name": "http_request", "arguments_summary": {"url": "https://example.com"}},
    )

    approval = (
        await db.execute(select(AgentRunApproval).where(AgentRunApproval.run_id == run.id))
    ).scalar_one()
    assert approval.expires_at - approval.requested_at == timedelta(minutes=45)
    assert run.status == "awaiting_approval"
    assert published[-1]["expires_at"] == approval.expires_at.isoformat()
