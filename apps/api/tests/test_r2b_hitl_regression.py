"""R2b HITL 승인 상태머신 리팩토링 — 회귀·행동 불변성 방어 테스트.

코드 리뷰에서 지적된 4가지 위험(하위 호환 체크포인트, 다중 승인 라운드,
CAS 경쟁 조건, 재소비 방지)을 고정한다. `approval.py`(순수 상태머신)와
`workers/tasks/agent.py`(CAS·세션 어댑터)의 계약이 리팩토링 전후로
byte-identical하게 유지되는지 worker `_run`을 직접 구동해 검증한다.

harness는 test_agent_approvals_api.py::test_full_cycle_worker_run_to_awaiting_approval_to_resume
와 test_hitl_approval.py의 재개 테스트들을 그대로 따른다 — mock_llm 스크립팅,
NODE_EXECUTORS/dispatch_agent seam 패치 패턴을 동일하게 사용한다.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from biznotes_core.models import Agent, AgentRun, AgentRunApproval, Notebook
from biznotes_core.services.agents import runtime
from biznotes_core.services.agents.nodes import composite as nodes_composite
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult
from sqlalchemy import select

from app.workers.tasks import agent as agent_task


async def _make_plain_worker(db, owner_id, *, scope="system"):
    """부작용 없는 최소 워크플로우 — call_agent(부작용 도구)의 대상으로 사용."""
    agent = Agent(
        name=f"plain-sub-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        icon="⚙️",
        definition={"nodes": [{"id": "g", "type": "llm", "prompt": "x"}], "edges": []},
        params_schema={},
        scope=scope,
        owner_id=owner_id,
        enabled=True,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


async def _make_workflow_and_run(db, user_a, *, goal="부작용 도구"):
    """단일 react_agent 노드 워크플로우 + queued AgentRun 시딩."""
    workflow_agent = Agent(
        name=f"r2b-workflow-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        icon="🤖",
        definition={
            "nodes": [{"id": "r", "type": "react_agent", "goal": goal}],
            "edges": [],
        },
        params_schema={},
        scope="private",
        owner_id=user_a.id,
        enabled=True,
    )
    db.add(workflow_agent)
    notebook = Notebook(title="r2b-nb", owner_id=user_a.id)
    db.add(notebook)
    await db.flush()

    run = AgentRun(
        agent_id=workflow_agent.id,
        notebook_id=notebook.id,
        user_id=user_a.id,
        params={},
        status="queued",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    await db.refresh(workflow_agent)
    return workflow_agent, run


def _spy_dispatch(monkeypatch, dispatched: list[str]):
    """runtime.dispatch_agent + nodes.composite.dispatch_agent 양쪽 패치(R2-5 seam)."""
    real_dispatch = runtime.dispatch_agent

    async def spy(ctx, agent):
        dispatched.append(str(agent.id))
        return await real_dispatch(ctx, agent)

    monkeypatch.setattr(runtime, "dispatch_agent", spy)
    monkeypatch.setattr(nodes_composite, "dispatch_agent", spy)
    return spy


def _final_turn(text: str) -> ToolTurnResult:
    return ToolTurnResult(
        content=text,
        tool_calls=[],
        assistant_message={"role": "assistant", "content": text},
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        model=None,
    )


@pytest.fixture(autouse=True)
def _mock_publish(monkeypatch):
    """이벤트 발행은 이 회귀 스위트의 관심사가 아니므로 no-op 처리."""
    monkeypatch.setattr(agent_task, "_publish", lambda run_id, event: None)


# ── 1) CR-R2B2-02: R2b 이전(pre-refactor) 문자열 키 checkpoint가 그대로 재개된다 ──


async def test_pre_r2b_checkpoint_json_resumes(db, user_a, mock_llm, monkeypatch):
    """[CR-R2B2-02 하위 호환] R2b 배포 시점에 이미 DB에 저장돼 있던 "구형" checkpoint
    (CheckpointKeys 상수를 거치지 않고 리터럴 문자열 키로 직접 조립된 JSON)를 새 코드
    (`approval.restore_run_context`/`worker._run`)가 그대로 읽어 재개할 수 있어야 한다.
    CheckpointKeys를 여기서 import하지 않고 문자열을 하드코딩하는 것이 테스트의 핵심 —
    상수 값이 몰래 바뀌면 이 테스트가 (상수 자체가 바뀌어도) 여전히 구형 리터럴로
    실패해 드리프트를 드러낸다.

    단순화: graph_state는 단일 react_agent 노드 그래프의 실제 캡처 형태를 그대로
    재현한다(params/user 메타키만 있고 노드 자신은 미완료 — graph.py:277 참고).
    """
    target = await _make_plain_worker(db, user_a.id)
    workflow_agent, run = await _make_workflow_and_run(db, user_a)

    dispatched: list[str] = []
    _spy_dispatch(monkeypatch, dispatched)

    now = datetime.now(UTC)
    pending_call = {"id": "c1", "name": "call_agent", "arguments": {"agent_id": str(target.id)}}
    graph_state = {
        "params": {},
        "user": {"id": str(user_a.id), "email": "", "name": "", "role": "user"},
    }
    old_checkpoint = {
        "graph_state": graph_state,
        "react_node_id": "r",
        "react_messages": [],
        "react_pending_tool_call": pending_call,
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
        "ctx_register_source_calls": 0,
        "tool_name": "call_agent",
        "arguments_summary": {"agent_id": str(target.id)},
    }

    run.status = "awaiting_approval"
    approval_row = AgentRunApproval(
        run_id=run.id,
        tool_name="call_agent",
        arguments_summary={"agent_id": str(target.id)},
        status="approved",
        consumed=False,
        requested_at=now,
        expires_at=now + timedelta(minutes=10),
        checkpoint=old_checkpoint,
    )
    db.add(approval_row)
    await db.commit()

    call_idx = {"n": 0}

    async def fake_complete(db_, model_id, messages, tools, **kwargs):
        i = call_idx["n"]
        call_idx["n"] += 1
        if i == 0:
            return _final_turn("구형 체크포인트 재개 완료")
        raise AssertionError("react 루프가 예상보다 더 많이 호출됨")

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake_complete)

    result = await agent_task._run(str(run.id))
    assert result == {"ok": True}
    assert dispatched == [str(target.id)], "승인된 call_agent가 정확히 1회 실행돼야 한다"

    await db.refresh(run)
    assert run.status == "succeeded"

    await db.refresh(approval_row)
    assert approval_row.consumed is True
    assert approval_row.checkpoint is None, "재개 완료 시 checkpoint는 NULL화되어야 한다"


# ── 2) CR-R2B2-04 / CR-R2B4: 한 턴 내 다중 gated tool_calls — 라운드 A → B ──


async def test_multi_approval_round_a_then_b(db, user_a, mock_llm, monkeypatch):
    """[CR-R2B2-04 / CR-R2B4 다중 승인 라운드] 한 턴에 gated 호출이 2개([A, B])
    있으면, A 승인 → 재개 시 A만 실행되고 B가 다시 게이트에 걸려 두 번째
    awaiting_approval로 전환되며, B 승인 → 재개 시 B만 실행되고 완료된다.
    각 단계에서 A/B가 정확히 1회씩만 dispatch되어야 한다(중복 실행 방지)."""
    target_a = await _make_plain_worker(db, user_a.id)
    target_b = await _make_plain_worker(db, user_a.id)
    _workflow_agent, run = await _make_workflow_and_run(db, user_a, goal="다중 승인 라운드")

    dispatched: list[str] = []
    _spy_dispatch(monkeypatch, dispatched)

    def two_calls_turn():
        c_a = ToolCall(id="a", name="call_agent", arguments={"agent_id": str(target_a.id)})
        c_b = ToolCall(id="b", name="call_agent", arguments={"agent_id": str(target_b.id)})
        assistant = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "a",
                    "type": "function",
                    "function": {
                        "name": "call_agent",
                        "arguments": json.dumps({"agent_id": str(target_a.id)}),
                    },
                },
                {
                    "id": "b",
                    "type": "function",
                    "function": {
                        "name": "call_agent",
                        "arguments": json.dumps({"agent_id": str(target_b.id)}),
                    },
                },
            ],
        }
        return ToolTurnResult(
            content="",
            tool_calls=[c_a, c_b],
            assistant_message=assistant,
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            model=None,
        )

    call_idx = {"n": 0}

    def script(i):
        if call_idx["n"] == 0:
            call_idx["n"] += 1
            return two_calls_turn()
        return _final_turn("완료")

    async def fake_complete(db_, model_id, messages, tools, **kwargs):
        return script(call_idx["n"])

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake_complete)

    # 1) 최초 실행 — 양쪽 다 gated(auto_approve_tools 없음) → A가 먼저 게이트에 걸림.
    result1 = await agent_task._run(str(run.id))
    assert result1 == {"ok": True, "awaiting_approval": True}
    assert dispatched == [], "게이트에 걸린 시점에는 아무 것도 dispatch되지 않아야 한다"

    await db.refresh(run)
    assert run.status == "awaiting_approval"

    approvals = (
        await db.execute(select(AgentRunApproval).where(AgentRunApproval.run_id == run.id))
    ).scalars().all()
    assert len(approvals) == 1
    approval_a = approvals[0]
    assert approval_a.arguments_summary["agent_id"] == str(target_a.id)
    checkpoint_a = approval_a.checkpoint
    assert checkpoint_a is not None
    assert checkpoint_a["react_pending_tool_call"]["name"] == "call_agent"
    remaining_after_a = [
        json.loads(c["arguments"]) if isinstance(c["arguments"], str) else c["arguments"]
        for c in checkpoint_a["react_remaining_calls_in_turn"]
    ]
    assert [r.get("agent_id") for r in remaining_after_a] == [str(target_b.id)], (
        "A의 checkpoint에는 B 하나만 remaining으로 남아야 한다"
    )
    assert checkpoint_a["react_tool_log"] == [], "A 실행 전이므로 tool_log는 비어 있어야 한다"

    # 2) A 승인 — 재개 시 A만 실행되고 B가 다시 게이트에 걸려야 한다.
    approval_a.status = "approved"
    approval_a.consumed = False
    await db.commit()

    result2 = await agent_task._run(str(run.id))
    assert result2 == {"ok": True, "awaiting_approval": True}
    assert dispatched == [str(target_a.id)], "재개 후 A만 정확히 1회 실행돼야 한다"

    await db.refresh(run)
    assert run.status == "awaiting_approval"

    await db.refresh(approval_a)
    assert approval_a.consumed is True
    assert approval_a.checkpoint is None

    approvals_b = (
        await db.execute(
            select(AgentRunApproval).where(
                AgentRunApproval.run_id == run.id, AgentRunApproval.id != approval_a.id
            )
        )
    ).scalars().all()
    assert len(approvals_b) == 1
    approval_b = approvals_b[0]
    assert approval_b.arguments_summary["agent_id"] == str(target_b.id)
    checkpoint_b = approval_b.checkpoint
    assert checkpoint_b is not None
    remaining_after_b = checkpoint_b["react_remaining_calls_in_turn"]
    assert remaining_after_b == [], "B의 checkpoint에는 remaining이 남아있지 않아야 한다"
    assert len(checkpoint_b["react_tool_log"]) == 1, "A의 실행 결과가 tool_log에 1건 기록돼야 한다"
    assert checkpoint_b["react_guards_aux_idx"] >= checkpoint_a["react_guards_aux_idx"]

    # 3) B 승인 — 재개 시 B만 실행되고 완료된다.
    approval_b.status = "approved"
    approval_b.consumed = False
    await db.commit()

    result3 = await agent_task._run(str(run.id))
    assert result3 == {"ok": True}
    assert dispatched == [str(target_a.id), str(target_b.id)], (
        "전체 사이클에서 A/B가 각각 정확히 1회씩만 실행되고 중복 dispatch가 없어야 한다"
    )

    await db.refresh(run)
    assert run.status == "succeeded"

    await db.refresh(approval_b)
    assert approval_b.consumed is True
    assert approval_b.checkpoint is None


# ── 3) CR-R2B4-02/04: CAS 소비 멱등성 — 소비된 행은 재소비되지 않는다(순차 증명) ──


async def test_resolve_pending_approval_is_idempotent_after_consume(
    db, user_a, mock_llm, monkeypatch
):
    """[CR-R2B4-02·R2B-04 순차 멱등성] approved+미소비 approval에 대해 `_resolve_pending_approval`을
    연달아 두 번 호출하면, 첫 호출만 행을 소비(consumed=True로 전이 + 반환)하고 두
    번째 호출은 WHERE 절(status IN CONSUMABLE_STATUSES AND consumed=False)이 더 이상
    매치되지 않아 None을 반환해야 한다 — worker의 원자적 UPDATE...RETURNING이 단일
    SQL 문으로 CAS를 보장하는 계약(CR-R2B-01)을 직접 검증한다.

    범위 명시(R2B-04): 이 테스트는 **순차 멱등성**만 증명한다(진짜 동시 두 세션
    레이스 아님). production SQL이 `UPDATE ... WHERE consumed=false RETURNING`인 유효한
    CAS라 순차 증명으로 "소비된 행은 어떤 재시도에도 다시 잡히지 않는다"는 핵심
    불변식을 결정적으로 보장하기에 충분하다. 두 세션 동시 gather는 conftest가 세션을
    하나만 제공하므로 도입하지 않았다.
    """
    target = await _make_plain_worker(db, user_a.id)
    _workflow_agent, run = await _make_workflow_and_run(db, user_a)

    dispatched: list[str] = []
    _spy_dispatch(monkeypatch, dispatched)

    now = datetime.now(UTC)
    pending_call = {"id": "c1", "name": "call_agent", "arguments": {"agent_id": str(target.id)}}
    graph_state = {
        "params": {},
        "user": {"id": str(user_a.id), "email": "", "name": "", "role": "user"},
    }
    from biznotes_core.services.agents.react.approval import CheckpointKeys as K

    checkpoint = {
        K.GRAPH_STATE: graph_state,
        K.REACT_NODE_ID: "r",
        K.REACT_MESSAGES: [],
        K.REACT_PENDING_TOOL_CALL: pending_call,
        K.REACT_REMAINING_CALLS_IN_TURN: [],
        K.REACT_ITERS_DONE: 0,
        K.REACT_SUBAGENT_CALLS_IN_NODE: 0,
        K.REACT_GUARDS_AUX_IDX: 0,
        K.REACT_TOOL_LOG: [],
        K.GUARDS_TOKEN_BASE: 0,
        K.GUARDS_COST_BASE: 0.0,
        K.CTX_USAGE: {"total_tokens": 0},
        K.CTX_COST_TOTAL: 0.0,
        K.CTX_ARTIFACT_KEYS: [],
        K.CTX_REACT_SUBAGENT_CALLS: 0,
        K.CTX_REGISTER_SOURCE_CALLS: 0,
        K.TOOL_NAME: "call_agent",
        K.ARGUMENTS_SUMMARY: {"agent_id": str(target.id)},
    }

    run.status = "awaiting_approval"
    approval_row = AgentRunApproval(
        run_id=run.id,
        tool_name="call_agent",
        arguments_summary={"agent_id": str(target.id)},
        status="approved",
        consumed=False,
        requested_at=now,
        expires_at=now + timedelta(minutes=10),
        checkpoint=checkpoint,
    )
    db.add(approval_row)
    await db.commit()
    await db.refresh(approval_row)

    first = await agent_task._resolve_pending_approval(db, run.id)
    assert first is not None
    assert first.id == approval_row.id
    assert first.consumed is True

    second = await agent_task._resolve_pending_approval(db, run.id)
    assert second is None, "동일 approval을 두 번째 호출이 재소비하면 안 된다"

    # CAS 자체는 검증했으니, 이어지는 정상 _run 사이클도 중복 dispatch 없이
    # 정확히 완료되는지 함께 확인한다(첫 CAS로 이미 checkpoint가 NULL 전 상태이므로
    # run 상태를 다시 awaiting_approval로 되돌리고 checkpoint는 아직 살아있는 채로
    # 재구성해 _run의 전체 경로를 태운다).
    await db.refresh(approval_row)
    approval_row.consumed = False
    run.status = "awaiting_approval"
    await db.commit()

    async def fake_complete(db_, model_id, messages, tools, **kwargs):
        return _final_turn("CAS 이후 재개 완료")

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake_complete)

    result = await agent_task._run(str(run.id))
    assert result == {"ok": True}
    assert dispatched == [str(target.id)], "정상 재개 경로에서도 정확히 1회만 dispatch돼야 한다"

    await db.refresh(approval_row)
    assert approval_row.consumed is True
    assert approval_row.checkpoint is None


# ── 4) CR-R2B3-05: 소비 완료 후 재개 시도는 재실행(replay)하지 않는다 ──


async def test_second_resume_after_consume_cannot_redispatch(db, user_a, mock_llm, monkeypatch):
    """[CR-R2B3-05 재생 방지 / R2B-01] Celery 중복 enqueue를 시뮬레이션 — approve API가
    재개 태스크를 두 번 큐에 넣어도(네트워크 재시도 등) 동일 approval이 두 번
    소비·재실행되면 안 된다.

    두 방어선을 검증한다:
    ① CAS 멱등성: consumed=True인 행은 `_resolve_pending_approval`이 다시 반환하지 않는다.
    ② 종료 상태 가드(R2B-01): 첫 재개로 run이 `succeeded`가 된 뒤 중복 `_run`이 도착하면,
       worker는 "queued/awaiting_approval이 아닌 상태"로 판단해 **아무 것도 하지 않고
       no-op 반환**한다 — 종료된 run을 다시 running으로 되돌려 그래프를 replay하지 않는다.
       (수정 전에는 이 경로가 succeeded→running으로 떨어져 게이트되지 않은 워크플로우를
       처음부터 재실행할 수 있었다 — R2B-01.)"""
    target = await _make_plain_worker(db, user_a.id)
    _workflow_agent, run = await _make_workflow_and_run(db, user_a)

    dispatched: list[str] = []
    _spy_dispatch(monkeypatch, dispatched)

    now = datetime.now(UTC)
    pending_call = {"id": "c1", "name": "call_agent", "arguments": {"agent_id": str(target.id)}}
    graph_state = {
        "params": {},
        "user": {"id": str(user_a.id), "email": "", "name": "", "role": "user"},
    }
    checkpoint = {
        "graph_state": graph_state,
        "react_node_id": "r",
        "react_messages": [],
        "react_pending_tool_call": pending_call,
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
        "ctx_register_source_calls": 0,
        "tool_name": "call_agent",
        "arguments_summary": {"agent_id": str(target.id)},
    }

    run.status = "awaiting_approval"
    approval_row = AgentRunApproval(
        run_id=run.id,
        tool_name="call_agent",
        arguments_summary={"agent_id": str(target.id)},
        status="approved",
        consumed=False,
        requested_at=now,
        expires_at=now + timedelta(minutes=10),
        checkpoint=checkpoint,
    )
    db.add(approval_row)
    await db.commit()

    async def fake_complete(db_, model_id, messages, tools, **kwargs):
        return _final_turn("최초 재개 완료")

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake_complete)

    result1 = await agent_task._run(str(run.id))
    assert result1 == {"ok": True}
    assert dispatched == [str(target.id)]

    await db.refresh(run)
    assert run.status == "succeeded"
    await db.refresh(approval_row)
    assert approval_row.consumed is True
    assert approval_row.checkpoint is None

    # CAS 자체를 직접 재확인 — consumed=True인 행은 어떤 재시도에도 다시 잡히지 않는다
    # (worker가 run.status를 보기 전에 이 판정부터 이뤄지는 것이 실제 방어선).
    stale = await agent_task._resolve_pending_approval(db, run.id)
    assert stale is None, "이미 소비된 approval은 CAS가 다시 반환하면 안 된다"

    # 두 번째 _run 호출(중복 enqueue 시뮬레이션) — run.status가 이미 "succeeded"라
    # 종료 상태 가드(R2B-01)에 걸려 no-op 반환해야 한다. LLM/도구는 절대 재호출되지
    # 않으며(dispatch 카운트 불변), 새 approval도 생기지 않는다. 만약 이 fake가
    # 호출되면 종료 상태 가드가 뚫린 것이므로 즉시 실패시킨다.
    async def fail_if_called(db_, model_id, messages, tools, **kwargs):
        raise AssertionError("종료된 run에 대해 워크플로우가 재실행됨 — R2B-01 가드 실패")

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fail_if_called)

    result2 = await agent_task._run(str(run.id))
    assert result2 == {"ok": True, "skipped": "non_runnable_status:succeeded"}
    assert dispatched == [str(target.id)], (
        "종료된 run의 중복 enqueue는 도구를 재실행(dispatch)하면 안 된다"
    )

    await db.refresh(run)
    assert run.status == "succeeded", "종료 상태는 중복 enqueue로 바뀌면 안 된다"

    approvals_after = (
        await db.execute(
            select(AgentRunApproval).where(
                AgentRunApproval.run_id == run.id, AgentRunApproval.id != approval_row.id
            )
        )
    ).scalars().all()
    assert approvals_after == [], "종료된 run의 중복 enqueue는 새 approval을 만들면 안 된다"


# ── 5) R2B-03 / CR-R2B2-03: 거부 시 fan-out 카운터 보존 + remaining이 cap에 걸림 ──


async def test_rejected_call_agent_preserves_fanout_counter_and_caps_remaining(
    db, user_a, mock_llm, monkeypatch
):
    """[R2B-03 / CR-R2B2-03 거부 + fan-out 카운터 불변식] fan-out 카운터는 게이트 판정
    **전**에 증가하므로, 거부된 `call_agent`도 카운터를 되돌리지 않고 보존해야 한다.

    시나리오: `max_subagent_calls=1`, 한 턴에 call_agent 2개([A, B]). A가 게이트에 걸려
    승인 대기(이때 카운터는 이미 1). A를 **거부** → 재개: A는 dispatch되지 않고(거부),
    카운터는 1로 보존된 채 remaining의 B로 진행. B는 `subagent_calls(1) >= max(1)`이라
    cap note만 주입되고 dispatch도 새 approval 생성도 없이 완료된다.

    이 테스트가 없으면 향후 리팩터가 카운터 증가를 게이트 뒤로 옮기거나 거부 시
    리셋해도 회귀가 잡히지 않는다(R2B-03)."""
    target_a = await _make_plain_worker(db, user_a.id)
    target_b = await _make_plain_worker(db, user_a.id)
    workflow_agent = Agent(
        name=f"r2b-cap-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        icon="🤖",
        definition={
            "nodes": [
                {"id": "r", "type": "react_agent", "goal": "거부+cap", "max_subagent_calls": 1}
            ],
            "edges": [],
        },
        params_schema={},
        scope="private",
        owner_id=user_a.id,
        enabled=True,
    )
    db.add(workflow_agent)
    notebook = Notebook(title="r2b-cap-nb", owner_id=user_a.id)
    db.add(notebook)
    await db.flush()
    run = AgentRun(
        agent_id=workflow_agent.id,
        notebook_id=notebook.id,
        user_id=user_a.id,
        params={},
        status="awaiting_approval",
    )
    db.add(run)
    await db.flush()

    dispatched: list[str] = []
    _spy_dispatch(monkeypatch, dispatched)

    now = datetime.now(UTC)
    # A는 이미 게이트 전에 fan-out 카운트가 반영됐다 → checkpoint 카운터 = 1.
    pending_a = {"id": "a", "name": "call_agent", "arguments": {"agent_id": str(target_a.id)}}
    remaining_b = [{"id": "b", "name": "call_agent", "arguments": {"agent_id": str(target_b.id)}}]
    graph_state = {
        "params": {},
        "user": {"id": str(user_a.id), "email": "", "name": "", "role": "user"},
    }
    checkpoint = {
        "graph_state": graph_state,
        "react_node_id": "r",
        "react_messages": [],
        "react_pending_tool_call": pending_a,
        "react_remaining_calls_in_turn": remaining_b,
        "react_iters_done": 0,
        "react_subagent_calls_in_node": 1,  # A가 게이트 전 이미 카운트됨
        "react_guards_aux_idx": 0,
        "react_tool_log": [],
        "guards_token_base": 0,
        "guards_cost_base": 0.0,
        "ctx_usage": {"total_tokens": 0},
        "ctx_cost_total": 0.0,
        "ctx_artifact_keys": [],
        "ctx_react_subagent_calls": 1,
        "ctx_register_source_calls": 0,
        "tool_name": "call_agent",
        "arguments_summary": {"agent_id": str(target_a.id)},
    }
    approval_row = AgentRunApproval(
        run_id=run.id,
        tool_name="call_agent",
        arguments_summary={"agent_id": str(target_a.id)},
        status="rejected",  # A 거부
        consumed=False,
        requested_at=now,
        expires_at=now + timedelta(minutes=10),
        checkpoint=checkpoint,
    )
    db.add(approval_row)
    await db.commit()

    async def fake_complete(db_, model_id, messages, tools, **kwargs):
        # A 거부 + B cap 처리 후 도구 없는 마무리 턴.
        return _final_turn("거부·cap 후 완료")

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake_complete)

    result = await agent_task._run(str(run.id))
    assert result == {"ok": True}
    # A는 거부라 dispatch 안 됨, B는 cap에 걸려 dispatch 안 됨 → dispatch 0건.
    assert dispatched == [], "거부된 A와 cap에 걸린 B 모두 dispatch되지 않아야 한다"

    await db.refresh(run)
    assert run.status == "succeeded"

    # 새 approval이 생기지 않아야 한다(B는 승인 요청이 아니라 cap note).
    others = (
        await db.execute(
            select(AgentRunApproval).where(
                AgentRunApproval.run_id == run.id, AgentRunApproval.id != approval_row.id
            )
        )
    ).scalars().all()
    assert others == [], "cap에 걸린 B는 새 승인 요청을 만들면 안 된다"
