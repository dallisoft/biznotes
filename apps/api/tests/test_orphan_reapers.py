"""발행 누락으로 queued에 정체된 agent run / export를 재발행하는 안전망 (R3-1c/d).

`enqueue_agent_run`·`_resolve_approval`·`create_export`는 행을 커밋한 뒤 브로커에
발행한다. 그 사이 실패하면 행은 영원히 `queued`로 남고 회수 장치가 없었다
(R3-0 설계 §2, HCI-R3-3(a) 인간 승인).

`running` 정체(stuck-running)는 별개 문제이며 범위 밖이다(HCI-R3-6) — 이 reaper가
건드리지 않는지도 함께 검증한다.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from biznotes_core.models import Agent, AgentRun, Notebook, User
from sqlalchemy import func, select, update

from app.workers.tasks.agent import ORPHANED_RUN_THRESHOLD_MINUTES, _reap_orphaned_runs


@pytest.fixture
def _mock_run_publish(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        "app.workers.tasks.agent.run_agent.apply_async",
        lambda args, task_id, queue: calls.append((args[0], task_id, queue)),
    )
    return calls


async def _db_now(db) -> datetime:
    """DB 시계의 현재 시각(`clock_timestamp()`).

    `updated_at`(onupdate `func.now()`)·만료 승인 sweep(`expires_at < now()`)은 DB 시계로
    찍히거나 비교된다. 호스트 `datetime.now`와 섞으면 시계 차이만큼 간헐 실패한다(BL-86).
    `now()`는 트랜잭션 시작 시각이라 열린 트랜잭션에서 과거로 고정되므로 `clock_timestamp()`를 쓴다.
    """
    return await db.scalar(select(func.clock_timestamp()))


async def _age(db, model, row_id: uuid.UUID, minutes: int) -> None:
    """updated_at을 과거로 강제 (onupdate 우회)."""
    moment = datetime.now(UTC) - timedelta(minutes=minutes)
    await db.execute(update(model).where(model.id == row_id).values(updated_at=moment))
    await db.commit()


async def _make_run(db, user: User, status: str, age_minutes: int) -> uuid.UUID:
    nb = Notebook(owner_id=user.id, title="nb")
    db.add(nb)
    await db.flush()
    agent = Agent(
        owner_id=user.id, name="a", kind="workflow", definition={"nodes": [], "edges": []}
    )
    db.add(agent)
    await db.flush()
    run = AgentRun(agent_id=agent.id, notebook_id=nb.id, user_id=user.id, status=status)
    db.add(run)
    await db.flush()
    run_id = run.id
    await _age(db, AgentRun, run_id, age_minutes)
    return run_id


# --- agent run reaper (R3-1c) ---


async def test_오래된_queued_run만_재발행한다(db, user_a, _mock_run_publish):
    old = await _make_run(db, user_a, "queued", ORPHANED_RUN_THRESHOLD_MINUTES + 5)
    fresh = await _make_run(db, user_a, "queued", 1)  # 정상 경로 — 아직 소비 대기 중일 뿐

    result = await _reap_orphaned_runs()

    assert result["requeued"] == 1
    published = {c[0] for c in _mock_run_publish}
    assert published == {str(old)}
    assert str(fresh) not in published, "갓 등록된 run을 고아로 오인하면 안 된다"


@pytest.mark.parametrize("status", ["running", "succeeded", "failed"])
async def test_queued가_아니면_건드리지_않는다(db, user_a, _mock_run_publish, status):
    """특히 running 정체(stuck-running)는 범위 밖이다 — HCI-R3-6.

    `awaiting_approval`은 approval 행의 상태에 따라 갈리므로 별도 테스트에서 다룬다.
    """
    await _make_run(db, user_a, status, ORPHANED_RUN_THRESHOLD_MINUTES + 5)

    result = await _reap_orphaned_runs()

    assert result["requeued"] == 0
    assert _mock_run_publish == []


async def test_해결됐으나_미소비된_approval의_run은_재발행한다(db, user_a, _mock_run_publish):
    """_resolve_approval은 run.status를 바꾸지 않고 발행한다 — 실패 시 awaiting_approval로 정체.

    그냥 오래된 awaiting_approval을 잡으면 사람의 승인을 기다리는 정상 run까지
    재발행한다. 구분 신호는 approval이 이미 해결됐는데(consumable) 미소비인지다.
    """
    from biznotes_core.models import AgentRunApproval

    run_id = await _make_run(db, user_a, "awaiting_approval", ORPHANED_RUN_THRESHOLD_MINUTES + 5)
    db.add(
        AgentRunApproval(
            run_id=run_id,
            tool_name="t",
            arguments_summary="{}",
            status="approved",  # 해결됨
            consumed=False,  # 그러나 아무도 소비하지 않음 → 고아
            requested_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await db.commit()

    result = await _reap_orphaned_runs()

    assert result["requeued"] == 1
    assert _mock_run_publish[0][0] == str(run_id)


async def test_사람의_승인을_기다리는_run은_재발행하지_않는다(db, user_a, _mock_run_publish):
    """approval이 pending이면 사람이 아직 결정하지 않은 것 — 고아가 아니다."""
    from biznotes_core.models import AgentRunApproval

    run_id = await _make_run(db, user_a, "awaiting_approval", ORPHANED_RUN_THRESHOLD_MINUTES + 60)
    db.add(
        AgentRunApproval(
            run_id=run_id,
            tool_name="t",
            arguments_summary="{}",
            status="pending",  # 사람 대기 중
            consumed=False,
            requested_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await db.commit()

    result = await _reap_orphaned_runs()

    assert result["requeued"] == 0, "승인 대기 중인 run을 재발행하면 안 된다"
    assert _mock_run_publish == []


async def test_이미_소비된_approval의_run은_재발행하지_않는다(db, user_a, _mock_run_publish):
    """워커가 이미 집어간 approval — 발행은 성공했다."""
    from biznotes_core.models import AgentRunApproval

    run_id = await _make_run(db, user_a, "awaiting_approval", ORPHANED_RUN_THRESHOLD_MINUTES + 5)
    db.add(
        AgentRunApproval(
            run_id=run_id,
            tool_name="t",
            arguments_summary="{}",
            status="approved",
            consumed=True,  # 소비됨
            requested_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await db.commit()

    assert (await _reap_orphaned_runs())["requeued"] == 0
    assert _mock_run_publish == []


async def test_한_건의_발행_실패가_나머지_회수를_막지_않는다(db, user_a, monkeypatch):
    """루프 안에서 발행 예외가 전파되면 뒤의 고아가 영원히 회수되지 않는다."""
    ids = [
        await _make_run(db, user_a, "queued", ORPHANED_RUN_THRESHOLD_MINUTES + 5) for _ in range(3)
    ]
    published: list[str] = []
    calls = {"n": 0}

    def flaky(args, task_id, queue):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("broker hiccup")
        published.append(args[0])

    monkeypatch.setattr("app.workers.tasks.agent.run_agent.apply_async", flaky)

    result = await _reap_orphaned_runs()

    assert calls["n"] == 3, "첫 건 실패 후에도 나머지를 시도해야 한다"
    assert result["requeued"] == 2
    assert len(published) == 2
    assert set(published) < {str(i) for i in ids}


async def test_재발행시_task_id를_커밋하고_updated_at을_갱신한다(db, user_a, _mock_run_publish):
    """updated_at이 갱신되지 않으면 다음 tick이 같은 run을 무한 재발행한다."""
    from biznotes_core.core.db import async_session_factory

    run_id = await _make_run(db, user_a, "queued", ORPHANED_RUN_THRESHOLD_MINUTES + 5)
    async with async_session_factory() as s:
        aged_updated_at = (await s.get(AgentRun, run_id)).updated_at
        # 비교 기준은 DB 시계로 잡는다 — updated_at은 reaper 트랜잭션의 DB `now()`로 찍힌다(BL-86)
        db_before = await _db_now(s)

    await _reap_orphaned_runs()

    async with async_session_factory() as s:
        run = await s.get(AgentRun, run_id)
        assert run.celery_task_id is not None
        # 발행에 쓰인 id와 커밋된 id가 같아야 revoke 경로가 동작한다
        assert _mock_run_publish[0][1] == run.celery_task_id
        assert run.updated_at > aged_updated_at
        assert run.updated_at >= db_before

    # 두 번째 tick에서는 cutoff 밖이므로 재발행되지 않는다
    _mock_run_publish.clear()
    result = await _reap_orphaned_runs()
    assert result["requeued"] == 0, "updated_at 갱신 실패 시 무한 재발행"


# --- 만료 승인 sweep (코어 #153 A-7, core-v0.11.0-upgrade AC-2.2·HCI-7 ③) ---
#
# reaper가 코어 `expire_stale_approvals`를 실제로 부르고(대체 없음), 커밋 뒤 이벤트를 발행한다.
# 이벤트는 **실제 Redis pub/sub**을 구독해 받는다 — 발행 함수를 대체하지 않는다. pub/sub 채널은
# Redis DB 번호와 무관하게 전역이므로 이 테스트가 만든 run id 채널만 구독해 다른 xdist 워커와
# 섞이지 않는다.


class _RunEvents:
    """run 채널 구독자 — 구독 확인을 받은 뒤에 반환해 발행보다 늦게 붙는 경합이 없다."""

    def __init__(self, run_ids):
        import redis
        from biznotes_core.services.events import channel_for_agent_run

        from app.core.config import get_settings

        self._client = redis.Redis.from_url(get_settings().redis_url)
        self._pubsub = self._client.pubsub()
        channels = [channel_for_agent_run(str(r)) for r in run_ids]
        self._pubsub.subscribe(*channels)
        confirmed = 0
        while confirmed < len(channels):
            msg = self._pubsub.get_message(timeout=5)
            assert msg is not None, "Redis 구독 확인을 받지 못했다"
            if msg["type"] == "subscribe":
                confirmed += 1

    def drain(self) -> list[tuple[str, dict]]:
        import json

        out: list[tuple[str, dict]] = []
        while (msg := self._pubsub.get_message(timeout=0.5)) is not None:
            if msg["type"] == "message":
                run_id = msg["channel"].decode().rsplit(":", 1)[1]
                out.append((run_id, json.loads(msg["data"])))
        return out

    def close(self) -> None:
        self._pubsub.close()
        self._client.close()


@pytest.fixture
def run_events():
    opened: list[_RunEvents] = []

    def _open(*run_ids):
        sub = _RunEvents(run_ids)
        opened.append(sub)
        return sub

    yield _open
    for sub in opened:
        sub.close()


_FAILED_EVENT = {"type": "status", "status": "failed", "error": "승인 대기 시간이 만료되었습니다"}


def _resolved_event(approval_id: uuid.UUID) -> dict:
    return {"type": "approval_resolved", "approval_id": str(approval_id), "status": "expired"}


async def _add_approval(db, run_id: uuid.UUID, *, expires_in_minutes: int, status="pending"):
    from biznotes_core.models import AgentRunApproval

    # sweep은 `expires_at < DB now()`로 비교하므로 기준 시각도 DB 시계에서 읽는다(BL-86)
    now = await _db_now(db)
    approval = AgentRunApproval(
        run_id=run_id,
        tool_name="http:GET",
        arguments_summary="{}",
        status=status,
        consumed=False,
        requested_at=now - timedelta(minutes=30),
        expires_at=now + timedelta(minutes=expires_in_minutes),
    )
    db.add(approval)
    await db.commit()
    return approval.id


async def _state(run_id: uuid.UUID, approval_ids: list[uuid.UUID]):
    """별도 세션으로 커밋된 상태를 읽는다(reaper 세션과 독립)."""
    from biznotes_core.core.db import async_session_factory
    from biznotes_core.models import AgentRunApproval

    async with async_session_factory() as s:
        run = await s.get(AgentRun, run_id)
        approvals = [await s.get(AgentRunApproval, a) for a in approval_ids]
        return run, approvals


async def test_reaper_expires_stale_approvals_and_publishes(
    db, user_a, _mock_run_publish, run_events
):
    """① 만료된 pending 승인 → 승인 expired, run failed·코어 오류 상수, 이벤트 2개(승인 → 상태)."""
    from biznotes_core.services.agent_runs import APPROVAL_EXPIRED_ERROR

    run_id = await _make_run(db, user_a, "awaiting_approval", 1)
    approval_id = await _add_approval(db, run_id, expires_in_minutes=-1)
    events = run_events(run_id)

    result = await _reap_orphaned_runs()

    assert APPROVAL_EXPIRED_ERROR == "승인 대기 시간이 만료되었습니다"
    run, [approval] = await _state(run_id, [approval_id])
    assert approval.status == "expired"
    assert run.status == "failed"
    assert run.error == APPROVAL_EXPIRED_ERROR
    assert run.finished_at is not None
    assert events.drain() == [
        (str(run_id), _resolved_event(approval_id)),
        (str(run_id), _FAILED_EVENT),
    ]
    assert result == {
        "requeued": 0,
        "expired_approvals": 1,
        "failed_runs": 1,
        "expired_seq_keys": 0,
    }
    assert _mock_run_publish == []  # 만료된 run은 재발행 후보가 아니다


async def test_reaper_mixed_cancelled_and_normal_runs(db, user_a, _mock_run_publish, run_events):
    """①″ 같은 tick의 정상 run 1건 + cancelled run 1건 → status 이벤트는 정상 run 1개뿐."""
    normal = await _make_run(db, user_a, "awaiting_approval", 1)
    cancelled = await _make_run(db, user_a, "cancelled", 1)
    normal_approval = await _add_approval(db, normal, expires_in_minutes=-1)
    cancelled_approval = await _add_approval(db, cancelled, expires_in_minutes=-1)
    events = run_events(normal, cancelled)

    result = await _reap_orphaned_runs()

    got = events.drain()
    statuses = [(rid, e) for rid, e in got if e["type"] == "status"]
    assert statuses == [(str(normal), _FAILED_EVENT)]
    resolved = {(rid, e["approval_id"]) for rid, e in got if e["type"] == "approval_resolved"}
    assert resolved == {
        (str(normal), str(normal_approval)),
        (str(cancelled), str(cancelled_approval)),
    }
    assert result["expired_approvals"] == 2 and result["failed_runs"] == 1
    run, _ = await _state(cancelled, [])
    assert run.status == "cancelled"


async def test_reaper_cancelled_run_gets_only_approval_event(
    db, user_a, _mock_run_publish, run_events
):
    """② cancelled run + pending 승인(취소가 승인을 남기는 경로) → 승인만 expired, run 그대로."""
    run_id = await _make_run(db, user_a, "cancelled", 1)
    approval_id = await _add_approval(db, run_id, expires_in_minutes=-1)
    events = run_events(run_id)

    result = await _reap_orphaned_runs()

    run, [approval] = await _state(run_id, [approval_id])
    assert approval.status == "expired"
    assert run.status == "cancelled" and run.error is None
    assert events.drain() == [(str(run_id), _resolved_event(approval_id))]
    assert result == {
        "requeued": 0,
        "expired_approvals": 1,
        "failed_runs": 0,
        "expired_seq_keys": 0,
    }


async def test_reaper_keeps_unexpired_approval(db, user_a, _mock_run_publish, run_events):
    """③ 만료 전 승인과 이미 해결된 승인은 건드리지 않는다 — 이벤트 0."""
    run_id = await _make_run(db, user_a, "awaiting_approval", 1)
    pending = await _add_approval(db, run_id, expires_in_minutes=30)
    other = await _make_run(db, user_a, "awaiting_approval", 1)
    rejected = await _add_approval(db, other, expires_in_minutes=-1, status="rejected")
    events = run_events(run_id, other)

    result = await _reap_orphaned_runs()

    run, [approval] = await _state(run_id, [pending])
    assert approval.status == "pending" and run.status == "awaiting_approval"
    _, [resolved] = await _state(other, [rejected])
    assert resolved.status == "rejected"  # pending이 아니면 만료 대상이 아니다
    assert events.drain() == []
    assert result["expired_approvals"] == 0 and result["failed_runs"] == 0


async def test_reaper_sweep_rerun_noop(db, user_a, _mock_run_publish, run_events):
    """⑤ 같은 tick을 다시 돌려도 0건 — 이미 expired인 승인은 CAS를 다시 통과하지 않는다."""
    run_id = await _make_run(db, user_a, "awaiting_approval", 1)
    await _add_approval(db, run_id, expires_in_minutes=-1)
    events = run_events(run_id)

    first = await _reap_orphaned_runs()
    assert first["expired_approvals"] == 1
    assert len(events.drain()) == 2

    second = await _reap_orphaned_runs()
    assert second == {
        "requeued": 0,
        "expired_approvals": 0,
        "failed_runs": 0,
        "expired_seq_keys": 0,
    }
    assert events.drain() == []


async def test_reaper_publish_failure_does_not_block_sweep_or_requeue(
    db, user_a, _mock_run_publish, monkeypatch
):
    """발행(Redis 경계) 실패는 best-effort — 만료는 커밋돼 있고 뒤의 고아 회수도 진행된다."""
    def broken_publish(channel, event):
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.workers.tasks.agent.publish", broken_publish)
    stale = await _make_run(db, user_a, "awaiting_approval", 1)
    approval_id = await _add_approval(db, stale, expires_in_minutes=-1)
    orphan = await _make_run(db, user_a, "queued", ORPHANED_RUN_THRESHOLD_MINUTES + 5)

    result = await _reap_orphaned_runs()

    run, [approval] = await _state(stale, [approval_id])
    assert approval.status == "expired" and run.status == "failed"
    assert result == {
        "requeued": 1,
        "expired_approvals": 1,
        "failed_runs": 1,
        "expired_seq_keys": 0,
    }
    assert [c[0] for c in _mock_run_publish] == [str(orphan)]


# --- export reaper (R3-1d) ---


# --- R32CR-01: 중복 전달 시 원자적 소유권 주장 (read-then-write 레이스) ---


async def test_동시_중복_전달시_한_워커만_run을_집어간다(db, user_a):
    """`status == "queued"` 확인 후 `running` 대입은 read-then-write라 원자적이지 않다.

    reaper는 의도적으로 중복 전달을 만들므로, 두 워커가 같은 run을 동시에
    실행해 도구 부수효과를 중복 수행할 수 있다. 단일 UPDATE CAS로 막는다.
    """
    import asyncio

    from biznotes_core.core.db import async_session_factory

    from app.workers.tasks.agent import _claim_queued_run

    run_id = await _make_run(db, user_a, "queued", 0)

    async def claim() -> bool:
        async with async_session_factory() as s:
            return await _claim_queued_run(s, run_id)

    results = await asyncio.gather(claim(), claim(), claim())

    assert sum(results) == 1, f"정확히 한 워커만 소유권을 얻어야 한다 (실제: {results})"

    async with async_session_factory() as s:
        assert (await s.get(AgentRun, run_id)).status == "running"


# --- R32CR-04: enqueue_agent_run은 발행 실패에도 예외를 올리지 않는다 ---


async def test_enqueue_agent_run은_커밋된_task_id로_발행한다(db, user_a, monkeypatch):
    """발행이 실제로 일어나야 한다. 스텁 시그니처가 어긋나면 TypeError가
    publishing_unit_of_work에 삼켜져 '통과하지만 발행 안 되는' 거짓 green이 된다."""
    from biznotes_core.core.db import async_session_factory
    from biznotes_core.services.agent_runs import enqueue_agent_run

    published: list[tuple] = []
    # 코어는 태스크를 import하지 않고 이름으로 발행한다(send_task).
    # 태스크 모듈을 patch하면 발행 경로를 못 잡아 거짓 green이 된다.
    from biznotes_core.workers.celery_app import celery_app
    from biznotes_core.workers.task_names import RUN_AGENT

    def _send_task(name, args, task_id, queue):
        assert name == RUN_AGENT, f"예상과 다른 태스크 이름: {name}"
        published.append((args[0], task_id, queue))

    monkeypatch.setattr(celery_app, "send_task", _send_task)

    nb = Notebook(owner_id=user_a.id, title="nb")
    db.add(nb)
    await db.flush()
    agent = Agent(
        owner_id=user_a.id, name="a", kind="workflow", definition={"nodes": [], "edges": []}
    )
    db.add(agent)
    await db.commit()

    run = await enqueue_agent_run(
        db, agent=agent, notebook_id=nb.id, user_id=user_a.id, user_role="user", params={}
    )

    assert len(published) == 1, "발행이 실제로 일어나야 한다"
    run_id, task_id, queue = published[0]
    assert run_id == str(run.id)
    assert queue == "agent"

    async with async_session_factory() as s:
        persisted = await s.get(AgentRun, run.id)
        assert persisted.celery_task_id == task_id, "취소(revoke)가 읽는 id와 발행 id가 같아야 한다"
        assert persisted.status == "queued"


async def test_enqueue_agent_run은_발행_실패시_예외를_올리지_않는다(db, user_a, monkeypatch):
    """HCI-R3B-1(a): 500을 내면 사용자 재시도가 중복 run을 만든다. reaper가 회수한다."""
    from biznotes_core.core.db import async_session_factory
    from biznotes_core.services.agent_runs import enqueue_agent_run

    def boom(*a, **k):
        raise RuntimeError("broker down")

    monkeypatch.setattr("app.workers.tasks.agent.run_agent.apply_async", boom)

    nb = Notebook(owner_id=user_a.id, title="nb")
    db.add(nb)
    await db.flush()
    agent = Agent(
        owner_id=user_a.id, name="a", kind="workflow", definition={"nodes": [], "edges": []}
    )
    db.add(agent)
    await db.commit()

    run = await enqueue_agent_run(
        db, agent=agent, notebook_id=nb.id, user_id=user_a.id, user_role="user", params={}
    )

    async with async_session_factory() as s:
        persisted = await s.get(AgentRun, run.id)
        assert persisted is not None, "run은 커밋된다 — reaper가 회수한다"
        assert persisted.status == "queued"


async def test_승인_재개도_커밋된_task_id로_발행한다(db, user_a, monkeypatch):
    """_resolve_approval도 커밋 후 발행한다(R32CR-04 일관성).

    스텁을 엄격한 시그니처로 두어, task_id 누락 시 TypeError가
    publishing_unit_of_work에 삼켜져 거짓 green이 되는 것을 막는다.
    """
    from biznotes_core.core.db import async_session_factory
    from biznotes_core.models import AgentRunApproval

    published: list[tuple] = []
    monkeypatch.setattr(
        "app.workers.tasks.agent.run_agent.apply_async",
        lambda args, task_id, queue: published.append((args[0], task_id)),
    )
    monkeypatch.setattr("app.api.v1.agents._publish", lambda *a, **k: None)

    run_id = await _make_run(db, user_a, "awaiting_approval", 0)
    approval_row = AgentRunApproval(
        run_id=run_id,
        tool_name="t",
        arguments_summary="{}",
        status="pending",
        consumed=False,
        requested_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add(approval_row)
    await db.commit()
    approval_id = approval_row.id

    from app.api.v1.agents import _resolve_approval

    async with async_session_factory() as s:
        await _resolve_approval(run_id, approval_id, "approved", user_a, s)

    assert len(published) == 1, "재개 발행이 실제로 일어나야 한다"
    pub_run_id, task_id = published[0]
    assert pub_run_id == str(run_id)

    async with async_session_factory() as s:
        persisted = await s.get(AgentRun, run_id)
        assert persisted.celery_task_id == task_id, "revoke가 읽는 id와 발행 id가 같아야 한다"


async def test_reaper_sweep_failure_does_not_block_requeue(
    db, user_a, _mock_run_publish, monkeypatch, capsys
):
    """P2-02: sweep(코어 `expire_stale_approvals`의 갱신 뒤 커밋 전)이 예외를 내도 같은 tick의 고아
    재발행은 진행되고, 실패는 로그로 남는다. 다음 tick에 sweep이 회복되면 만료를 처리한다.

    실패 주입은 제품 코드가 부르는 **코어 의존성** 경계(DB 오류를 흉내 내는 예외)에서만 한다 —
    제품 함수(`_reap_orphaned_runs`·`_expire_stale_approvals`)는 대체하지 않는다.
    """
    import biznotes_core.services.agent_runs as agent_runs

    real = agent_runs.expire_stale_approvals

    seen: dict[str, int] = {}

    async def broken(db, **kwargs):
        # 실제 CAS로 승인·run을 갱신한 **뒤** 실패 — 아래 「무변경」 단언이 롤백을 증명(P2R-01).
        # 건수는 여기서 단언하지 않는다 — 제품의 except가 AssertionError도 삼킨다(P2R3-01)
        seen["updated"] = len(await real(db, **kwargs))
        raise RuntimeError("db unavailable after update")

    monkeypatch.setattr(agent_runs, "expire_stale_approvals", broken)
    stale = await _make_run(db, user_a, "awaiting_approval", 1)
    approval_id = await _add_approval(db, stale, expires_in_minutes=-1)
    orphan = await _make_run(db, user_a, "queued", ORPHANED_RUN_THRESHOLD_MINUTES + 5)

    result = await _reap_orphaned_runs()

    assert seen == {"updated": 1}  # 실패 전에 CAS가 실제로 1건을 갱신했다(롤백 단언의 전제)
    assert result == {
        "requeued": 1,
        "expired_approvals": 0,
        "failed_runs": 0,
        "expired_seq_keys": 0,
    }
    assert [c[0] for c in _mock_run_publish] == [str(orphan)]
    run, [approval] = await _state(stale, [approval_id])
    # sweep 세션의 갱신(expired·failed)은 커밋 전 예외로 롤백됐다 — 커밋된 상태는 그대로다
    assert approval.status == "pending" and run.status == "awaiting_approval"
    logged = capsys.readouterr()
    assert "승인 만료 sweep 실패" in logged.out + logged.err  # structlog → 표준 출력

    # 회복된 다음 tick은 같은 승인을 만료시킨다
    monkeypatch.setattr(agent_runs, "expire_stale_approvals", real)
    again = await _reap_orphaned_runs()
    assert again["expired_approvals"] == 1 and again["failed_runs"] == 1
    run, [approval] = await _state(stale, [approval_id])
    assert approval.status == "expired" and run.status == "failed"


# --- 종료 run의 이벤트 seq 키 만료 sweep (#97, BL-174) ---
#
# 코어 `initialize_run_event_sequence`가 run마다 seq 키를 만들고, 만료는 코어
# `publish_run_event`의 terminal status 경로에서만 건다. 제품 워커는 `publish(channel, dict)`를
# 써서 그 경로를 타지 않으므로 reaper가 terminal run의 TTL 없는 키에 보존 기간을 건다.
# 키는 실제 테스트 Redis(워커별 DB)에 만든다.


@pytest.fixture
def seq_keys():
    """실제 코어 초기화 경로로 seq 키를 만들고, 테스트 뒤 지운다."""
    import redis
    from biznotes_core.services.events import (
        initialize_run_event_sequence,
        sequence_key_for_agent_run,
    )

    from app.core.config import get_settings

    client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    created: list[str] = []

    class _SeqKeys:
        redis = client

        @staticmethod
        def create(run_id) -> str:
            assert initialize_run_event_sequence(run_id)
            key = sequence_key_for_agent_run(run_id)
            created.append(key)
            return key

        @staticmethod
        def ttl(key: str) -> int:
            return client.ttl(key)

    yield _SeqKeys
    if created:
        client.delete(*created)
    client.close()


def _retention() -> int:
    from app.core.config import get_settings

    return get_settings().run_event_sequence_retention_sec


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
async def test_reaper가_종료_run의_seq_키에_보존_기간을_건다(
    db, user_a, _mock_run_publish, seq_keys, status
):
    run_id = await _make_run(db, user_a, status, 1)
    key = seq_keys.create(run_id)
    assert seq_keys.ttl(key) == -1  # 전제: 코어 초기화는 만료를 걸지 않는다

    result = await _reap_orphaned_runs()

    assert result["expired_seq_keys"] == 1
    assert 0 < seq_keys.ttl(key) <= _retention()


@pytest.mark.parametrize("status", ["queued", "running", "awaiting_approval"])
async def test_reaper는_진행_중_run의_seq_키를_건드리지_않는다(
    db, user_a, _mock_run_publish, seq_keys, status
):
    """진행 중 run의 키가 만료되면 이벤트 순서(cursor)가 깨진다."""
    run_id = await _make_run(db, user_a, status, 1)
    key = seq_keys.create(run_id)

    result = await _reap_orphaned_runs()

    assert result["expired_seq_keys"] == 0
    assert seq_keys.ttl(key) == -1


async def test_reaper는_이미_만료가_걸린_seq_키를_바꾸지_않는다(
    db, user_a, _mock_run_publish, seq_keys
):
    """코어 terminal 발행이 건 TTL 등 기존 만료를 늘리거나 줄이지 않는다."""
    run_id = await _make_run(db, user_a, "succeeded", 1)
    key = seq_keys.create(run_id)
    seq_keys.redis.expire(key, 300)

    result = await _reap_orphaned_runs()

    assert result["expired_seq_keys"] == 0
    assert 0 < seq_keys.ttl(key) <= 300


async def test_reaper는_DB에_없는_run의_seq_키를_건드리지_않는다(
    db, user_a, _mock_run_publish, seq_keys
):
    """run 행을 확인할 수 없는 키(다른 DB의 run, 갓 만든 run 등)는 terminal로 판정하지 않는다."""
    key = seq_keys.create(uuid.uuid4())

    result = await _reap_orphaned_runs()

    assert result["expired_seq_keys"] == 0
    assert seq_keys.ttl(key) == -1


async def test_seq_키_sweep은_tick당_상한을_지키고_다음_tick에_이어간다(
    db, user_a, _mock_run_publish, seq_keys, monkeypatch
):
    monkeypatch.setattr("app.workers.tasks.agent.SEQ_KEY_SWEEP_LIMIT", 1)
    keys = [seq_keys.create(await _make_run(db, user_a, "succeeded", 1)) for _ in range(2)]

    first = await _reap_orphaned_runs()
    assert first["expired_seq_keys"] == 1
    assert sorted(seq_keys.ttl(k) == -1 for k in keys) == [False, True]

    second = await _reap_orphaned_runs()
    assert second["expired_seq_keys"] == 1
    assert all(0 < seq_keys.ttl(k) <= _retention() for k in keys)


async def test_seq_키_sweep_실패는_고아_재발행을_막지_않는다(
    db, user_a, _mock_run_publish, seq_keys, monkeypatch, capsys
):
    """Redis 경계(연결 불가)에서만 실패를 주입한다 — 제품 함수는 대체하지 않는다."""
    from redis.asyncio import Redis as AsyncRedis

    real_from_url = AsyncRedis.from_url

    def unreachable(url, **kwargs):
        return real_from_url("redis://127.0.0.1:1/0", **kwargs)

    monkeypatch.setattr("app.workers.tasks.agent.AsyncRedis.from_url", unreachable)
    done = await _make_run(db, user_a, "succeeded", 1)
    key = seq_keys.create(done)
    orphan = await _make_run(db, user_a, "queued", ORPHANED_RUN_THRESHOLD_MINUTES + 5)

    result = await _reap_orphaned_runs()

    assert result == {
        "requeued": 1,
        "expired_approvals": 0,
        "failed_runs": 0,
        "expired_seq_keys": 0,
    }
    assert [c[0] for c in _mock_run_publish] == [str(orphan)]
    assert seq_keys.ttl(key) == -1
    logged = capsys.readouterr()
    assert "seq 키 만료 sweep 실패" in logged.out + logged.err
