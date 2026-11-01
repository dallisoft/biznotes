# 신규(#371). 필요: PostgreSQL·Valkey(PG 테스트).
"""에이전트 run 숨김 — 목록 조건·집계 유지·사용자 삭제 시 SET NULL·감사 로그.

`create_all` 기반이라 Alembic 마이그레이션 검증이 아니다(열 정의가 제품 리비전과 같은 모양인지만
본다).
"""

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text

from biznotes_core.core.db import get_session_factory
from biznotes_core.models import Agent, AgentRun, AuditLog, ExecutionLog, Notebook, User
from biznotes_core.services.agent_runs import RunNotFinished, run_is_visible, set_run_hidden


async def _runs(db: Any, owner: User, *statuses: str) -> list[AgentRun]:
    nb = Notebook(owner_id=owner.id, title="숨김")
    agent = Agent(
        name=f"a-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        definition={},
        params_schema={},
        scope="system",
        enabled=True,
    )
    db.add_all([nb, agent])
    await db.flush()
    runs = [
        AgentRun(
            agent_id=agent.id,
            notebook_id=nb.id,
            user_id=owner.id,
            params={},
            status=status,
            token_usage={"total_tokens": 100},
        )
        for status in statuses
    ]
    db.add_all(runs)
    await db.flush()
    db.add_all(
        ExecutionLog(run_id=r.id, node_id="gen", node_type="llm", cost_usd=Decimal("0.5"))
        for r in runs
    )
    await db.commit()
    return runs


async def _visible_ids(notebook_id: uuid.UUID) -> set[uuid.UUID]:
    async with get_session_factory()() as s:
        rows = await s.scalars(
            select(AgentRun.id).where(AgentRun.notebook_id == notebook_id, run_is_visible())
        )
        return set(rows)


async def test_hidden_run_leaves_the_list_and_comes_back(db, user_a, admin_user):
    shown, hidden = await _runs(db, user_a, "succeeded", "succeeded")
    nb_id = shown.notebook_id

    assert await set_run_hidden(db, hidden, hidden=True, actor_id=admin_user.id)
    await db.commit()

    assert await _visible_ids(nb_id) == {shown.id}

    # 해제하면 다시 보인다
    assert await set_run_hidden(db, hidden, hidden=False, actor_id=user_a.id)
    await db.commit()
    assert await _visible_ids(nb_id) == {shown.id, hidden.id}
    actions = await db.scalars(
        select(AuditLog.action).where(AuditLog.resource == str(hidden.id))
    )
    assert sorted(actions) == ["agent_run.hide", "agent_run.unhide"]


async def test_deleting_the_hider_keeps_the_run_hidden(db, user_a, user_b):
    [run] = await _runs(db, user_a, "failed")
    await set_run_hidden(db, run, hidden=True, actor_id=user_b.id)
    await db.commit()

    await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_b.id})
    await db.commit()

    async with get_session_factory()() as s:
        stored = await s.get(AgentRun, run.id)
        assert stored is not None
        assert stored.hidden_by is None and stored.hidden_at is not None


@pytest.mark.parametrize("status", ["queued", "running", "awaiting_approval"])
async def test_running_run_is_refused(db, user_a, status):
    [run] = await _runs(db, user_a, status)
    with pytest.raises(RunNotFinished):
        await set_run_hidden(db, run, hidden=True, actor_id=user_a.id)
    await db.rollback()
    assert await _visible_ids(run.notebook_id) == {run.id}


async def test_columns_match_the_revision_shape(db):
    rows = (
        await db.execute(
            text(
                "SELECT column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns WHERE table_name = 'agent_runs' "
                "AND column_name IN ('hidden_at', 'hidden_by') ORDER BY column_name"
            )
        )
    ).all()
    assert [tuple(r) for r in rows] == [
        ("hidden_at", "timestamp with time zone", "YES", None),
        ("hidden_by", "uuid", "YES", None),
    ]
