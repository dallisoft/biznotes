"""Phase 19a — HITL 승인 API 통합 테스트 (approve/reject/list).

phase-19-design.md §3.3(권한), §4.1(원자성·멱등성), §2.7(만료)의 API 계약을
실제 HTTP 클라이언트 경유로 검증한다. runtime.py 게이트 로직 자체는
test_hitl_approval.py에서 다룬다.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from tests.conftest import bearer, login, share_with


@pytest.fixture
async def seeded_workflow_agent(db, user_a):
    from biznotes_core.models import Agent

    agent = Agent(
        name=f"hitl-workflow-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        icon="🤖",
        definition={"nodes": [{"id": "r", "type": "react_agent", "goal": "x"}], "edges": []},
        params_schema={},
        scope="private",
        owner_id=user_a.id,
        enabled=True,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


@pytest.fixture
async def seeded_run_with_pending_approval(db, user_a, seeded_workflow_agent):
    """awaiting_approval 상태의 AgentRun + pending AgentRunApproval을 직접 시딩."""
    from biznotes_core.models import AgentRun, AgentRunApproval, Notebook

    notebook = Notebook(title="hitl-nb", owner_id=user_a.id)
    db.add(notebook)
    await db.flush()

    run = AgentRun(
        agent_id=seeded_workflow_agent.id,
        notebook_id=notebook.id,
        user_id=user_a.id,
        params={},
        status="awaiting_approval",
    )
    db.add(run)
    await db.flush()

    now = datetime.now(UTC)
    approval = AgentRunApproval(
        run_id=run.id,
        tool_name="http_request",
        arguments_summary={"url": "https://api.example.com/x", "method": "GET"},
        status="pending",
        requested_at=now,
        expires_at=now + timedelta(minutes=10),
        checkpoint={
            "graph_state": {},
            "react_node_id": "r",
            "react_messages": [],
            "react_pending_tool_call": {"id": "c", "name": "http_request", "arguments": {}},
            "react_remaining_calls_in_turn": [],
            "react_iters_done": 0,
            "react_subagent_calls_in_node": 0,
            "react_guards_aux_idx": 0,
            "react_tool_log": [],
            "guards_token_base": 0,
            "guards_cost_base": 0.0,
            "ctx_usage": {"total_tokens": 0},
            "ctx_cost_total": 0.0,
            "ctx_artifact_keys": [],
            "ctx_react_subagent_calls": 0,
            "tool_name": "http_request",
            "arguments_summary": {"url": "https://api.example.com/x", "method": "GET"},
        },
    )
    db.add(approval)
    await db.commit()
    await db.refresh(run)
    await db.refresh(approval)
    return run, approval


@pytest.fixture(autouse=True)
def _mock_celery(monkeypatch):
    calls: list[tuple] = []

    class FakeTask:
        id = "fake-resume-task"

    def fake_apply_async(args=None, queue=None, **kwargs):
        calls.append((args, queue))
        return FakeTask()

    monkeypatch.setattr(
        "app.workers.tasks.agent.run_agent.apply_async", fake_apply_async
    )
    return calls


@pytest.fixture(autouse=True)
def _mock_publish(monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(
        "app.workers.tasks.agent._publish",
        lambda run_id, event: events.append({"run_id": run_id, **event}),
    )
    return events


async def test_approve_transitions_run_and_reenqueues(
    client: httpx.AsyncClient, user_a, seeded_run_with_pending_approval, _mock_celery, _mock_publish
):
    run, approval = seeded_run_with_pending_approval
    token = await login(client, user_a.email)

    res = await client.post(
        f"/api/v1/agent-runs/{run.id}/approvals/{approval.id}/approve",
        headers=bearer(token),
    )
    assert res.status_code == 200
    body = res.json()
    # [CDX-D1] run.status를 running으로 전이시키는 주체는 API가 아니라 재개
    # 태스크(체크포인트를 실제로 소비한 worker)다 — API는 awaiting_approval을
    # 유지한 채 재개 태스크를 enqueue만 한다(mock celery라 실제 전이는 없음).
    assert body["status"] == "awaiting_approval"
    # 승인자는 run 소유자 — 응답도 목록·상세와 같은 취소 인가를 싣는다(#173)
    assert body["can_cancel"] is True
    assert len(_mock_celery) == 1
    assert _mock_celery[0][0] == [str(run.id)]
    assert any(
        e["type"] == "approval_resolved" and e["status"] == "approved" for e in _mock_publish
    )


async def test_reject_transitions_run_and_reenqueues(
    client: httpx.AsyncClient, user_a, seeded_run_with_pending_approval, _mock_celery
):
    run, approval = seeded_run_with_pending_approval
    token = await login(client, user_a.email)

    res = await client.post(
        f"/api/v1/agent-runs/{run.id}/approvals/{approval.id}/reject",
        headers=bearer(token),
    )
    assert res.status_code == 200
    assert len(_mock_celery) == 1


async def test_non_owner_non_admin_cannot_approve(
    db, client: httpx.AsyncClient, user_b, seeded_run_with_pending_approval
):
    """CR-01 — run 트리거 사용자 본인/admin만 승인 가능. 노트북 공유(read) 사용자는
    조회 권한이 있어 run을 볼 수는 있지만 승인은 거부된다(§3.3, §7 Q5)."""

    run, approval = seeded_run_with_pending_approval
    await share_with(db, run.notebook_id, user_b, "read")
    await db.commit()

    token = await login(client, user_b.email)
    res = await client.post(
        f"/api/v1/agent-runs/{run.id}/approvals/{approval.id}/approve",
        headers=bearer(token),
    )
    assert res.status_code == 403


async def test_double_approve_is_idempotent(
    client: httpx.AsyncClient, user_a, seeded_run_with_pending_approval, _mock_celery
):
    run, approval = seeded_run_with_pending_approval
    token = await login(client, user_a.email)

    res1 = await client.post(
        f"/api/v1/agent-runs/{run.id}/approvals/{approval.id}/approve",
        headers=bearer(token),
    )
    assert res1.status_code == 200
    res2 = await client.post(
        f"/api/v1/agent-runs/{run.id}/approvals/{approval.id}/approve",
        headers=bearer(token),
    )
    assert res2.status_code == 200  # 멱등 — 동일 결과 재요청
    assert len(_mock_celery) == 1, "재개 태스크는 최초 1회만 enqueue되어야 한다"


async def test_approve_after_reject_conflicts(
    client: httpx.AsyncClient, user_a, seeded_run_with_pending_approval
):
    run, approval = seeded_run_with_pending_approval
    token = await login(client, user_a.email)

    res1 = await client.post(
        f"/api/v1/agent-runs/{run.id}/approvals/{approval.id}/reject",
        headers=bearer(token),
    )
    assert res1.status_code == 200
    res2 = await client.post(
        f"/api/v1/agent-runs/{run.id}/approvals/{approval.id}/approve",
        headers=bearer(token),
    )
    assert res2.status_code == 409  # 이미 다른 결과로 처리됨


async def test_approve_expired_approval_returns_409_and_fails_run(
    db, client: httpx.AsyncClient, user_a, seeded_run_with_pending_approval
):
    """[SEC-D2] 만료 평가는 approve 엔드포인트 내부(소유자 검증 후)에서만 수행."""
    run, approval = seeded_run_with_pending_approval
    approval.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db.commit()

    token = await login(client, user_a.email)
    res = await client.post(
        f"/api/v1/agent-runs/{run.id}/approvals/{approval.id}/approve",
        headers=bearer(token),
    )
    assert res.status_code == 409

    await db.refresh(run)
    assert run.status == "failed"
    await db.refresh(approval)
    assert approval.status == "expired"


async def test_approve_cancelled_run_returns_409(
    db, client: httpx.AsyncClient, user_a, seeded_run_with_pending_approval
):
    run, approval = seeded_run_with_pending_approval
    run.status = "cancelled"
    await db.commit()

    token = await login(client, user_a.email)
    res = await client.post(
        f"/api/v1/agent-runs/{run.id}/approvals/{approval.id}/approve",
        headers=bearer(token),
    )
    assert res.status_code == 409


async def test_list_approvals_shows_history(
    client: httpx.AsyncClient, user_a, seeded_run_with_pending_approval
):
    run, approval = seeded_run_with_pending_approval
    token = await login(client, user_a.email)

    await client.post(
        f"/api/v1/agent-runs/{run.id}/approvals/{approval.id}/approve",
        headers=bearer(token),
    )
    res = await client.get(f"/api/v1/agent-runs/{run.id}/approvals", headers=bearer(token))
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    assert body[0]["status"] == "approved"
    assert body[0]["resolved_by"] == str(user_a.id)
    assert "checkpoint" not in body[0]  # SEC-D1 — checkpoint는 절대 API로 노출하지 않음


# ── 엔드투엔드 재개: worker._run 최초 실행 → 승인 대기 → approve → 재개 태스크 실행 ──


async def test_full_cycle_worker_run_to_awaiting_approval_to_resume(
    db, user_a, mock_llm, monkeypatch
):
    """[phase-19-design.md 전체] worker._run을 직접 호출해 최초 실행(승인 대기 전환)
    → approve API(§4.1) → 재개 태스크(§2.4~2.5) 소비 → succeeded까지 전체 사이클을
    검증한다. 선행 노드(constant)가 재개 시 재실행되지 않음(no-op, §2.2)도 확인."""
    import json

    from biznotes_core.models import Agent, AgentRun, AgentRunApproval, Notebook
    from biznotes_core.services.agents import runtime
    from biznotes_core.services.gateway import llm as gateway_llm
    from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult

    from app.workers.tasks import agent as agent_task

    prior_calls = {"n": 0}

    async def counted_constant(ctx, node, state):
        prior_calls["n"] += 1
        return {"value": "seed"}

    monkeypatch.setitem(runtime.NODE_EXECUTORS, "constant", counted_constant)

    target = Agent(
        name=f"plain-{uuid.uuid4().hex[:6]}", kind="workflow", icon="⚙️",
        definition={"nodes": [{"id": "g", "type": "llm", "prompt": "x"}], "edges": []},
        params_schema={}, scope="system", owner_id=user_a.id, enabled=True,
    )
    db.add(target)

    workflow_agent = Agent(
        name=f"e2e-workflow-{uuid.uuid4().hex[:6]}", kind="workflow", icon="🤖",
        definition={
            "nodes": [
                {"id": "c", "type": "constant", "value": "seed"},
                {"id": "r", "type": "react_agent", "goal": "부작용 도구"},
            ],
            "edges": [["c", "r"]],
        },
        params_schema={}, scope="private", owner_id=user_a.id, enabled=True,
    )
    db.add(workflow_agent)
    notebook = Notebook(title="e2e-nb", owner_id=user_a.id)
    db.add(notebook)
    await db.flush()

    run = AgentRun(
        agent_id=workflow_agent.id, notebook_id=notebook.id, user_id=user_a.id,
        params={}, status="queued",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    await db.refresh(target)

    call_idx = {"n": 0}
    dispatched = {"n": 0}
    real_dispatch = runtime.dispatch_agent

    async def spy_dispatch(ctx, agent):
        dispatched["n"] += 1
        return await real_dispatch(ctx, agent)

    monkeypatch.setattr(runtime, "dispatch_agent", spy_dispatch)
    # _run_subagent(nodes/composite.py, R2-5 이동)가 자기 모듈 네임스페이스에서
    # dispatch_agent를 직접 호출하므로, runtime 재수출만 패치하면 이 경로가
    # 패치를 우회한다 — nodes.composite도 함께 패치해야 seam이 보존된다.
    from biznotes_core.services.agents.nodes import composite as nodes_composite

    monkeypatch.setattr(nodes_composite, "dispatch_agent", spy_dispatch)

    def script(i):
        idx = call_idx["n"]
        call_idx["n"] += 1
        if idx == 0:
            call = ToolCall(id="c1", name="call_agent", arguments={"agent_id": str(target.id)})
            assistant = {
                "role": "assistant", "content": "",
                "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "call_agent",
                                  "arguments": json.dumps({"agent_id": str(target.id)})}}
                ],
            }
            return ToolTurnResult(
                content="", tool_calls=[call], assistant_message=assistant,
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, model=None,
            )
        return ToolTurnResult(
            content="완료", tool_calls=[],
            assistant_message={"role": "assistant", "content": "완료"},
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, model=None,
        )

    async def fake_complete(db_, model_id, messages, tools, **kwargs):
        return script(0)

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake_complete)
    monkeypatch.setattr(agent_task, "_publish", lambda run_id, event: None)

    # 1) 최초 실행 — HITL 게이트에 걸려 awaiting_approval로 종료.
    result1 = await agent_task._run(str(run.id))
    assert result1 == {"ok": True, "awaiting_approval": True}
    assert prior_calls["n"] == 1
    assert dispatched["n"] == 0

    await db.refresh(run)
    assert run.status == "awaiting_approval"

    approvals = await db.execute(
        select(AgentRunApproval).where(AgentRunApproval.run_id == run.id)
    )
    approval = approvals.scalar_one()
    assert approval.status == "pending"
    assert approval.checkpoint is not None
    assert approval.tool_name == "call_agent"

    # 2) approve — 상태 전이 + 재개 태스크 인자 구성(직접 재호출로 시뮬레이션).
    approval.status = "approved"
    approval.consumed = False
    await db.commit()

    # 3) 재개 태스크 — worker._run을 다시 호출(Celery apply_async 대신 직접 실행).
    call_idx["n"] = 1  # 다음 스크립트 턴은 최종 텍스트 반환

    result2 = await agent_task._run(str(run.id))
    assert result2 == {"ok": True}
    assert prior_calls["n"] == 1, "재개 시 constant 노드가 재실행되지 않아야 한다(no-op)"
    assert dispatched["n"] == 1, "승인된 call_agent가 정확히 1회 실행되어야 한다"

    await db.refresh(run)
    assert run.status == "succeeded"
    assert run.result_summary == "완료"

    await db.refresh(approval)
    assert approval.consumed is True
    assert approval.checkpoint is None, "재개 완료 시 checkpoint는 NULL화되어야 한다(SEC-D1)"
