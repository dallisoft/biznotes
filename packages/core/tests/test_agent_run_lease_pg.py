# 신규(#153 PR-B). 필요: PostgreSQL·Valkey(PG 테스트).
"""agent run lease·fencing·회수 — 실제 행·유일성 인덱스·트랜잭션으로 검증한다.

워커 강제 종료는 "lease를 받은 뒤 갱신하지 않는다"로 흉내 낸다(lease TTL을 1초로 줄인다).
`create_all` 기반이라 Alembic 마이그레이션 검증이 아니다.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from biznotes_core.config import get_settings
from biznotes_core.core.db import get_session_factory
from biznotes_core.models import (
    Agent,
    AgentRun,
    AgentRunApproval,
    ChatMessage,
    ChatSession,
    Notebook,
    User,
)
from biznotes_core.services import agent_run_lease as leases


@pytest.fixture
def short_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_run_lease_ttl_sec", 1)
    monkeypatch.setattr(settings, "agent_run_heartbeat_interval_sec", 0.2)
    monkeypatch.setattr(settings, "agent_run_heartbeat_tick_timeout_sec", 0.5)
    monkeypatch.setattr(settings, "agent_run_heartbeat_max_failed_ticks", 0)


async def _session_run(db: Any, user: User, *, status: str = "queued") -> AgentRun:
    agent = Agent(name=f"a-{uuid.uuid4().hex[:6]}", kind="workflow", definition={},
                  params_schema={}, scope="user", owner_id=user.id, enabled=True)
    nb = Notebook(owner_id=user.id, title="t")
    db.add_all([agent, nb])
    await db.flush()
    chat = ChatSession(notebook_id=nb.id, user_id=user.id)
    db.add(chat)
    await db.flush()
    run = AgentRun(agent_id=agent.id, notebook_id=nb.id, user_id=user.id,
                   chat_session_id=chat.id, status=status, trigger="chat")
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def _row(run_id: uuid.UUID) -> AgentRun:
    async with get_session_factory()() as s:
        row = await s.get(AgentRun, run_id)
        assert row is not None
        return row


async def _claim(run_id: uuid.UUID, owner: str = "w1") -> leases.Lease:
    async with get_session_factory()() as s:
        lease = await leases.claim_run(s, run_id, owner)
        await s.commit()
    assert lease is not None
    return lease


async def _reclaim() -> leases.ReclaimResult:
    async with get_session_factory()() as s:
        result = await leases.reclaim_expired_runs(s)
        await s.commit()
    return result


# ---------- claim ----------


async def test_claim_only_takes_queued_runs(db, user_a: User) -> None:
    run = await _session_run(db, user_a)
    lease = await _claim(run.id)
    assert lease.attempt == 1
    row = await _row(run.id)
    assert (row.status, row.attempt, row.lease_owner) == ("running", 1, "w1")
    assert row.lease_expires_at is not None and row.started_at is not None
    async with get_session_factory()() as s:  # Celery 재전달은 살아 있는 run을 넘겨받지 않는다
        assert await leases.claim_run(s, run.id, "w2") is None


# ---------- 수락 기준 10: 회수 후 같은 세션에서 새 run ----------


async def test_dead_worker_is_reclaimed_and_the_chat_session_is_freed(
    db, user_a: User, short_lease: None
) -> None:
    run = await _session_run(db, user_a)
    await _claim(run.id)  # 워커가 lease를 받은 뒤 죽는다(갱신 없음)

    def second_run() -> AgentRun:
        return AgentRun(agent_id=run.agent_id, notebook_id=run.notebook_id, user_id=user_a.id,
                        chat_session_id=run.chat_session_id, status="queued", trigger="chat")

    async with get_session_factory()() as s:  # 회수 전에는 활성 run 유일성 때문에 막힌다
        s.add(second_run())
        with pytest.raises(IntegrityError):
            await s.commit()

    assert (await _reclaim()).lost_runs == []  # 아직 만료 전
    await asyncio.sleep(1.2)
    result = await _reclaim()
    assert result.lost_runs == [run.id]
    row = await _row(run.id)
    assert (row.status, row.error, row.attempt) == ("failed", leases.WORKER_LOST_ERROR, 2)
    assert row.lease_owner is None and row.lease_expires_at is None

    async with get_session_factory()() as s:
        s.add(second_run())
        await s.commit()


# ---------- 수락 기준 11: fencing ----------


async def test_late_worker_write_is_fenced_and_rolled_back(
    db, user_a: User, short_lease: None
) -> None:
    run = await _session_run(db, user_a)
    lease = await _claim(run.id)
    await asyncio.sleep(1.2)
    await _reclaim()

    async with get_session_factory()() as s:  # 원래 워커가 늦게 완료를 쓴다
        s.add(ChatMessage(session_id=run.chat_session_id, role="assistant", content="늦은 답"))
        ok = await leases.fenced_transition(
            s, lease, from_statuses=("running",), to_status="succeeded",
            values={"result_summary": "late"},
        )
        assert ok is False
        await s.rollback()  # 계약: False면 같은 트랜잭션의 다른 쓰기를 롤백한다

    row = await _row(run.id)
    assert (row.status, row.result_summary) == ("failed", None)
    async with get_session_factory()() as s:
        messages = (await s.execute(select(ChatMessage).where(
            ChatMessage.session_id == run.chat_session_id))).scalars().all()
    assert messages == []


async def test_cancel_is_not_overwritten_by_worker_completion(db, user_a: User) -> None:
    """기존 결함(취소 뒤 워커의 마지막 쓰기가 cancelled를 덮음)의 회귀 테스트."""
    run = await _session_run(db, user_a)
    lease = await _claim(run.id)
    async with get_session_factory()() as s:  # 취소 라우터(제품 이관 후 모양) — lease도 지운다
        await s.execute(update(AgentRun).where(AgentRun.id == run.id).values(
            status="cancelled", lease_owner=None, lease_expires_at=None))
        await s.commit()
    async with get_session_factory()() as s:
        assert not await leases.fenced_transition(
            s, lease, from_statuses=("running",), to_status="succeeded")
        await s.commit()
    assert (await _row(run.id)).status == "cancelled"


async def test_successful_transition_clears_the_lease(db, user_a: User) -> None:
    run = await _session_run(db, user_a)
    lease = await _claim(run.id)
    async with get_session_factory()() as s:
        assert await leases.fenced_transition(
            s, lease, from_statuses=("running",), to_status="succeeded",
            values={"result_summary": "done", "finished_at": datetime.now(UTC)})
        await s.commit()
    row = await _row(run.id)
    assert (row.status, row.result_summary, row.lease_owner) == ("succeeded", "done", None)


async def test_pending_approval_is_stored_only_when_the_transition_wins(
    db, user_a: User, short_lease: None
) -> None:
    run = await _session_run(db, user_a)
    lease = await _claim(run.id)
    await asyncio.sleep(1.2)
    await _reclaim()

    async with get_session_factory()() as s:
        now = datetime.now(UTC)
        approval = AgentRunApproval(run_id=run.id, tool_name="http:POST", arguments_summary={},
                                    requested_at=now, expires_at=now + timedelta(minutes=10))
        assert not await leases.persist_pending_approval(s, lease, approval)
        await s.rollback()
    async with get_session_factory()() as s:
        count = (await s.execute(select(AgentRunApproval).where(
            AgentRunApproval.run_id == run.id))).scalars().all()
    assert count == []  # 회수된 run에 승인 카드가 남지 않는다


# ---------- 수락 기준 12: 혼합 배포 ----------


async def test_old_worker_resume_after_new_claim_is_not_reclaimed(
    db, user_a: User, short_lease: None
) -> None:
    """새 claim → 승인 대기(lease NULL) → 구 워커 재개(lease 컬럼 미갱신) → 회수되지 않는다."""
    run = await _session_run(db, user_a)
    lease = await _claim(run.id)
    async with get_session_factory()() as s:
        now = datetime.now(UTC)
        approval = AgentRunApproval(run_id=run.id, tool_name="t", arguments_summary={},
                                    requested_at=now, expires_at=now + timedelta(minutes=10))
        assert await leases.persist_pending_approval(s, lease, approval)
        await s.commit()
    row = await _row(run.id)
    assert (row.status, row.lease_owner, row.lease_expires_at) == ("awaiting_approval", None, None)

    async with get_session_factory()() as s:  # 구 워커: ORM으로 status만 running으로 바꾼다
        await s.execute(update(AgentRun).where(AgentRun.id == run.id).values(status="running"))
        await s.commit()
    await asyncio.sleep(1.2)
    assert (await _reclaim()).lost_runs == []
    assert (await _row(run.id)).status == "running"


# ---------- 수락 기준 13: 재개 claim ----------


async def _awaiting_with_approval(db: Any, user: User, status: str) -> tuple[AgentRun, Any]:
    run = await _session_run(db, user, status="awaiting_approval")
    now = datetime.now(UTC)
    approval = AgentRunApproval(run_id=run.id, tool_name="t", arguments_summary={},
                                requested_at=now, expires_at=now + timedelta(minutes=10),
                                status=status, checkpoint={"graph_state": {"a": 1}})
    db.add(approval)
    await db.commit()
    return run, approval.id


async def test_claim_resume_is_atomic_and_single_use(db, user_a: User) -> None:
    run, approval_id = await _awaiting_with_approval(db, user_a, "approved")
    async with get_session_factory()() as s:
        claim = await leases.claim_resume(s, run.id, "w2")
        await s.commit()
    assert claim is not None
    assert (claim.approval_id, claim.approval_status) == (approval_id, "approved")
    assert claim.checkpoint == {"graph_state": {"a": 1}}
    row = await _row(run.id)
    assert (row.status, row.attempt, row.lease_owner) == ("running", 1, "w2")
    async with get_session_factory()() as s:
        approval = await s.get(AgentRunApproval, approval_id)
        assert approval is not None and approval.consumed and approval.checkpoint is None
        assert await leases.claim_resume(s, run.id, "w3") is None  # 두 번째 재개는 없다


async def test_claim_resume_rolls_back_when_the_run_moved_on(db, user_a: User) -> None:
    run, approval_id = await _awaiting_with_approval(db, user_a, "approved")
    await db.execute(update(AgentRun).where(AgentRun.id == run.id).values(status="cancelled"))
    await db.commit()
    async with get_session_factory()() as s:
        assert await leases.claim_resume(s, run.id, "w2") is None
        await s.rollback()
    async with get_session_factory()() as s:
        approval = await s.get(AgentRunApproval, approval_id)
        assert approval is not None and approval.consumed is False  # 소비가 남지 않는다


async def test_stale_awaiting_run_is_reclaimed(db, user_a: User) -> None:
    """소비된 승인만 남은 채 정체된 awaiting_approval은 임계 시간 뒤 정리된다."""
    stuck, _ = await _awaiting_with_approval(db, user_a, "approved")
    waiting, _ = await _awaiting_with_approval(db, user_a, "pending")
    await db.execute(update(AgentRunApproval).where(AgentRunApproval.run_id == stuck.id)
                     .values(consumed=True))
    old = datetime.now(UTC) - timedelta(hours=2)
    await db.execute(update(AgentRun).where(AgentRun.id.in_([stuck.id, waiting.id]))
                     .values(updated_at=old))
    await db.commit()

    result = await _reclaim()
    assert result.stale_awaiting_runs == [stuck.id]
    assert (await _row(stuck.id)).status == "failed"
    assert (await _row(waiting.id)).status == "awaiting_approval"  # pending 승인이 있다


# ---------- 수락 기준 14: heartbeat ----------


async def test_heartbeat_renews_and_cancels_the_body_when_ownership_is_lost(
    db, user_a: User, short_lease: None
) -> None:
    run = await _session_run(db, user_a)
    lease = await _claim(run.id)
    first = (await _row(run.id)).lease_expires_at

    reached_end = False
    with pytest.raises(leases.LeaseLost):
        async with leases.lease_heartbeat(lease) as handle:
            await asyncio.sleep(0.5)  # 몇 틱 갱신된다
            assert handle.valid()
            renewed = (await _row(run.id)).lease_expires_at
            assert renewed is not None and first is not None and renewed > first
            async with get_session_factory()() as s:  # 다른 시도가 소유권을 가져갔다
                await s.execute(update(AgentRun).where(AgentRun.id == run.id)
                                .values(attempt=AgentRun.attempt + 1))
                await s.commit()
            started = asyncio.get_running_loop().time()
            try:
                await asyncio.sleep(5)
                reached_end = True
            finally:
                # 한 interval(+틱) 안에 본문이 취소된다
                assert asyncio.get_running_loop().time() - started < 1.0
    assert not reached_end
    assert not handle.valid()


async def test_heartbeat_does_not_start_without_ownership(db, user_a: User) -> None:
    run = await _session_run(db, user_a)
    lease = await _claim(run.id)
    stale = leases.Lease(run.id, lease.attempt + 1, lease.owner)
    entered = False
    with pytest.raises(leases.LeaseLost):
        async with leases.lease_heartbeat(stale):
            entered = True
    assert not entered


async def test_normal_exit_then_transition_is_not_a_false_loss(
    db, user_a: User, short_lease: None
) -> None:
    """블록을 나온 뒤 전이하면 소유 상실로 오탐하지 않는다(재리뷰 R3)."""
    run = await _session_run(db, user_a)
    lease = await _claim(run.id)
    async with leases.lease_heartbeat(lease):
        await asyncio.sleep(0.3)
    async with get_session_factory()() as s:
        assert await leases.fenced_transition(
            s, lease, from_statuses=("running",), to_status="awaiting_approval")
        await s.commit()
    await asyncio.sleep(0.5)  # 남은 틱이 없다 — 상실 경고·취소 없음
    assert (await _row(run.id)).status == "awaiting_approval"
