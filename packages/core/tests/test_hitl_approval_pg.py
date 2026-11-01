# 출처: sl-agents `apps/api/tests/test_hitl_approval.py` @ a7f245f
#       (원본 제품 저장소 1079bbf 사본의 추가 4건은 제품 `app.workers.tasks.agent`를
#       시험하므로 제품에 남는다)
# #1 이관(docs/plans/opt-in-pg-suite/). 필요: PostgreSQL·Valkey(PG 테스트).
# 본문은 원본 그대로 — 바꾼 것: import 정렬(ruff I001), 재개 테스트 3건(#91)이 두 제품 워커와
#   같은 순서로 재개한다: `approval.restore_run_context(ctx, checkpoint)` 뒤 `execute_agent`.
"""Phase 19a — HITL(Human-in-the-loop) 승인 게이트 단위 테스트.

phase-19-design.md §2(체크포인트/재개), §3(자동 승인 정책)의 핵심 동작을
runtime.py 레벨에서 검증한다. API 엔드포인트·worker 재개 분기는 별도 테스트
파일(test_agent_approvals_api.py 등)에서 다룬다.
"""

import uuid

import pytest

from biznotes_core.models import Agent
from biznotes_core.services.agents import runtime
from biznotes_core.services.agents.react import approval
from biznotes_core.services.agents.runtime import ApprovalRequired, RunContext, execute_agent
from biznotes_core.services.agents.tool_registry import (
    TOOL_SIDE_EFFECT,
    _domain_allowed,
    auto_approve_match,
    is_side_effect_tool,
    sanitize_for_approval_card,
)
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult


def _ctx(db, user_id=None, *, depth=0, agent_id=None):
    return RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db,
        params={}, user_id=user_id, agent_id=agent_id, depth=depth,
    )


async def _make_plain_worker(db, owner_id, *, scope="system"):
    agent = Agent(
        name=f"plain-sub-{uuid.uuid4().hex[:6]}",
        kind="workflow", icon="⚙️",
        definition={"nodes": [{"id": "g", "type": "llm", "prompt": "x"}], "edges": []},
        params_schema={}, scope=scope, owner_id=owner_id, enabled=True,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


def _tool_turn(name: str, args: dict) -> ToolTurnResult:
    call = ToolCall(id="c", name=name, arguments=args)
    assistant = {
        "role": "assistant", "content": "",
        "tool_calls": [
            {"id": "c", "type": "function",
             "function": {"name": name, "arguments": __import__("json").dumps(args)}}
        ],
    }
    return ToolTurnResult(
        content="", tool_calls=[call], assistant_message=assistant,
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, model=None,
    )


def _final_turn(text: str = "끝") -> ToolTurnResult:
    return ToolTurnResult(
        content=text, tool_calls=[], assistant_message={"role": "assistant", "content": text},
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, model=None,
    )


def _patch_completer(monkeypatch, script):
    calls = {"n": 0}

    async def fake(db, model_id, messages, tools, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        return script(i)

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake)
    return calls


# ── tool_registry 도구 정책 단위 테스트 ──────────────────────────────────────


def test_tool_side_effect_classification():
    assert TOOL_SIDE_EFFECT["rag_search"] is False
    assert TOOL_SIDE_EFFECT["web_search"] is False
    assert TOOL_SIDE_EFFECT["call_agent"] is True
    assert TOOL_SIDE_EFFECT["http_request"] is True
    assert TOOL_SIDE_EFFECT["mcp_call"] is True
    assert is_side_effect_tool("rag_search") is False
    # 레지스트리에 없는(예: MCP 자율 디스커버리) 도구는 보수적으로 side_effect=True.
    assert is_side_effect_tool("mcp_abcd1234_unknown_tool") is True


@pytest.mark.parametrize(
    ("host", "allowlist", "expected"),
    [
        ("api.internal.corp", ["*.internal.corp"], True),
        ("a.b.internal.corp", ["*.internal.corp"], True),
        ("evil-internal.corp", ["*.internal.corp"], False),
        ("internal.corp.attacker.com", ["*.internal.corp"], False),
        ("internal.corp", ["*.internal.corp"], False),
        ("api.example.com", ["api.example.com"], True),
        ("api.example.com.evil.com", ["api.example.com"], False),
    ],
)
def test_domain_allowed_wildcard_matching(host, allowlist, expected):
    """SEC-D3 반영 — 선행 점 포함 접미사 매칭만 허용(우회 방지)."""
    assert _domain_allowed(host, allowlist) is expected


def test_auto_approve_match_http_request_requires_ssrf_layer_separately():
    """auto_approve_match는 allowlist 판정만 — SSRF 검증은 호출자 책임(SEC-D3)."""
    policy = {"http_request": {"domains": ["api.example.com"], "methods": ["GET"]}}
    assert auto_approve_match(
        "http_request", {"url": "https://api.example.com/x", "method": "GET"}, policy
    ) is True
    assert auto_approve_match(
        "http_request", {"url": "https://evil.com/x", "method": "GET"}, policy
    ) is False
    assert auto_approve_match(
        "http_request", {"url": "https://api.example.com/x", "method": "POST"}, policy
    ) is False


def test_auto_approve_match_call_agent():
    target = str(uuid.uuid4())
    policy = {"call_agent": {"agent_ids": [target]}}
    assert auto_approve_match("call_agent", {"agent_id": target}, policy) is True
    assert auto_approve_match("call_agent", {"agent_id": str(uuid.uuid4())}, policy) is False


def test_auto_approve_match_no_policy_falls_back_to_gate():
    assert auto_approve_match("call_agent", {"agent_id": "x"}, None) is False
    assert auto_approve_match("call_agent", {"agent_id": "x"}, {}) is False


def test_sanitize_for_approval_card_masks_url_and_body_secrets():
    out = sanitize_for_approval_card(
        "http_request",
        {
            "url": "https://user:pass@api.example.com/x?api_key=secret123&q=hello",
            "method": "POST",
            "body": {"password": "hunter2", "note": "keep"},
            "headers": {"Authorization": "Bearer xyz", "X-Trace": "abc"},
        },
    )
    assert "user:pass@" not in out["url"]
    assert "secret123" not in out["url"]
    assert "q=hello" in out["url"]
    assert out["body"]["password"] != "hunter2"
    assert out["body"]["note"] == "keep"
    assert out["headers"]["Authorization"] == "***"
    assert out["headers"]["X-Trace"] == "abc"


# ── runtime.py HITL 게이트 동작 ──────────────────────────────────────────────


async def test_side_effect_tool_without_policy_raises_approval_required(
    db, user_a, mock_llm, monkeypatch
):
    """부작용 도구(call_agent) 호출이 자동 승인 정책 없이 시도되면 ApprovalRequired.

    _exec_react_agent를 직접 호출해 make_node._run/execute_agent의 캐치 없이
    예외가 실제로 발생하는지 확인한다(execute_agent 경유 시의 캐치 동작은
    별도 테스트 test_execute_agent_catches_approval_required_and_sets_ctx에서 검증).
    """
    from biznotes_core.services.agents.runtime import _exec_react_agent

    target = await _make_plain_worker(db, user_a.id, scope="system")
    _patch_completer(monkeypatch, lambda i: _tool_turn("call_agent", {"agent_id": str(target.id)}))

    ctx = _ctx(db, user_a.id)
    with pytest.raises(ApprovalRequired) as exc_info:
        await _exec_react_agent(
            ctx, {"id": "r", "type": "react_agent", "goal": "부작용 도구"}, {}
        )
    checkpoint = exc_info.value.checkpoint
    assert checkpoint["tool_name"] == "call_agent"
    assert checkpoint["react_pending_tool_call"]["name"] == "call_agent"


async def test_execute_agent_catches_approval_required_and_sets_ctx(
    db, user_a, mock_llm, monkeypatch
):
    """execute_agent는 ApprovalRequired를 캐치해 ctx.pending_approval에 담고
    빈 문자열을 반환한다(-> str 시그니처 무변경, CDX-D3)."""
    target = await _make_plain_worker(db, user_a.id, scope="system")
    _patch_completer(monkeypatch, lambda i: _tool_turn("call_agent", {"agent_id": str(target.id)}))

    ctx = _ctx(db, user_a.id)
    summary = await execute_agent(
        ctx, "workflow",
        {"nodes": [{"id": "r", "type": "react_agent", "goal": "부작용 도구"}], "edges": []},
    )
    assert summary == ""
    assert ctx.pending_approval is not None
    assert ctx.pending_approval["tool_name"] == "call_agent"
    assert ctx.pending_approval["graph_state"] is not None
    assert "r" not in ctx.pending_approval["graph_state"]  # react 노드 자신은 미완료


async def test_auto_approved_call_agent_executes_without_gate(
    db, user_a, mock_llm, monkeypatch
):
    """auto_approve_tools 정책이 매칭되면 게이트 없이 즉시 실행된다."""
    target = await _make_plain_worker(db, user_a.id, scope="system")
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
        if i == 0:
            return _tool_turn("call_agent", {"agent_id": str(target.id)})
        return _final_turn("완료")

    _patch_completer(monkeypatch, script)

    ctx = _ctx(db, user_a.id)
    summary = await execute_agent(
        ctx, "workflow",
        {
            "nodes": [
                {
                    "id": "r", "type": "react_agent", "goal": "자동승인",
                    "auto_approve_tools": {"call_agent": {"agent_ids": [str(target.id)]}},
                }
            ],
            "edges": [],
        },
    )
    assert dispatched["n"] == 1
    assert summary == "완료"
    assert ctx.pending_approval is None


async def test_resume_after_approval_completes_tool_call_without_rerunning_prior_nodes(
    db, user_a, mock_llm, monkeypatch
):
    """[phase-19-design.md §2.2] 재개(resume_state) 시 선행 노드(constant)는 재실행되지
    않고(no-op), react 노드만 승인된 도구를 실행해 마무리한다."""
    target = await _make_plain_worker(db, user_a.id, scope="system")
    prior_calls = {"n": 0}

    async def counted_constant(ctx, node, state):
        prior_calls["n"] += 1
        return {"value": node.get("value")}

    monkeypatch.setitem(runtime.NODE_EXECUTORS, "constant", counted_constant)

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
        if i == 0:
            return _tool_turn("call_agent", {"agent_id": str(target.id)})
        return _final_turn("재개완료")

    _patch_completer(monkeypatch, script)

    definition = {
        "nodes": [
            {"id": "c", "type": "constant", "value": "seed"},
            {"id": "r", "type": "react_agent", "goal": "부작용 도구"},
        ],
        "edges": [["c", "r"]],
    }

    ctx1 = _ctx(db, user_a.id)
    summary1 = await execute_agent(ctx1, "workflow", definition)
    assert summary1 == ""
    assert prior_calls["n"] == 1  # 최초 실행에서 constant 1회
    checkpoint = ctx1.pending_approval
    assert checkpoint is not None

    # 승인 처리를 시뮬레이션 — worker가 하는 것처럼 approval_status를 주입.
    checkpoint = dict(checkpoint)
    checkpoint["react_approval_status"] = "approved"

    ctx2 = _ctx(db, user_a.id)
    approval.restore_run_context(ctx2, checkpoint)  # 워커와 같은 순서(#91)
    # 재개 노드 config에 _resume 힌트 주입은 execute_agent가 아니라 worker 책임이므로,
    # 여기서는 execute_agent(resume_state=...) 경로만 검증 — react 노드는 completed가
    # 아니므로 다시 executor가 호출되고, _exec_react_agent가 node["_resume"]을 본다.
    definition_resume = {
        "nodes": [
            {"id": "c", "type": "constant", "value": "seed"},
            {"id": "r", "type": "react_agent", "goal": "부작용 도구", "_resume": checkpoint},
        ],
        "edges": [["c", "r"]],
    }
    summary2 = await execute_agent(
        ctx2, "workflow", definition_resume, resume_state=checkpoint["graph_state"]
    )
    assert summary2 == "재개완료"
    assert prior_calls["n"] == 1, "재개 시 선행 constant 노드가 재실행되지 않아야 한다"
    assert dispatched["n"] == 1, "승인된 call_agent가 정확히 1회 실행되어야 한다"


async def test_resume_with_rejected_status_skips_tool_and_continues(
    db, user_a, mock_llm, monkeypatch
):
    """거부된 도구 호출은 실행되지 않고 거부 메시지가 도구 결과로 주입된다."""
    target = await _make_plain_worker(db, user_a.id, scope="system")
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
        if i == 0:
            return _tool_turn("call_agent", {"agent_id": str(target.id)})
        return _final_turn("거부후마무리")

    _patch_completer(monkeypatch, script)

    definition = {
        "nodes": [{"id": "r", "type": "react_agent", "goal": "부작용 도구"}], "edges": [],
    }
    ctx1 = _ctx(db, user_a.id)
    await execute_agent(ctx1, "workflow", definition)
    checkpoint = dict(ctx1.pending_approval)
    checkpoint["react_approval_status"] = "rejected"

    ctx2 = _ctx(db, user_a.id)
    approval.restore_run_context(ctx2, checkpoint)  # 워커와 같은 순서(#91)
    definition_resume = {
        "nodes": [
            {"id": "r", "type": "react_agent", "goal": "부작용 도구", "_resume": checkpoint},
        ],
        "edges": [],
    }
    summary = await execute_agent(
        ctx2, "workflow", definition_resume, resume_state=checkpoint["graph_state"]
    )
    assert summary == "거부후마무리"
    assert dispatched["n"] == 0, "거부된 도구는 실행되지 않아야 한다"


async def test_run_subagent_raises_when_inner_approval_pending(db, user_a, mock_llm, monkeypatch):
    """[HCI-D4, §9 DA-02] 서브에이전트 내부에서 승인이 필요한 호출이 발생하면
    _run_subagent가 명확한 AgentRuntimeError로 거부한다(재개 불가능한 checkpoint 방지,
    스코프 축소)."""
    inner_target = await _make_plain_worker(db, user_a.id, scope="system")
    sub_agent = Agent(
        name=f"nested-react-{uuid.uuid4().hex[:6]}",
        kind="workflow", icon="🤖",
        definition={
            "nodes": [{"id": "inner", "type": "react_agent", "goal": "내부 부작용"}],
            "edges": [],
        },
        params_schema={}, scope="system", owner_id=user_a.id, enabled=True,
    )
    db.add(sub_agent)
    await db.commit()
    await db.refresh(sub_agent)

    _patch_completer(
        monkeypatch, lambda i: _tool_turn("call_agent", {"agent_id": str(inner_target.id)})
    )

    ctx = _ctx(db, user_a.id)
    with pytest.raises(runtime.AgentRuntimeError, match="서브에이전트.*내부"):
        await runtime._run_subagent(ctx, sub_agent.id, {})


async def test_nested_subagent_approval_absorbed_as_tool_failure(
    db, user_a, mock_llm, monkeypatch
):
    """서브에이전트 내부 승인 거부(AgentRuntimeError)는 call_agent 도구 dispatch의
    기존 예외 처리(SEC-08 일반화)에 흡수되어 부작용 도구는 실행되지 않은 채
    그래프가 실패 메시지로 계속 진행된다 — 부작용이 승인 없이 실행되지 않는다는
    핵심 안전 속성은 유지된다."""
    inner_target = await _make_plain_worker(db, user_a.id, scope="system")
    sub_agent = Agent(
        name=f"nested-react-{uuid.uuid4().hex[:6]}",
        kind="workflow", icon="🤖",
        definition={
            "nodes": [{"id": "inner", "type": "react_agent", "goal": "내부 부작용"}],
            "edges": [],
        },
        params_schema={}, scope="system", owner_id=user_a.id, enabled=True,
    )
    db.add(sub_agent)
    await db.commit()
    await db.refresh(sub_agent)

    inner_dispatched = {"n": 0}
    real_dispatch = runtime.dispatch_agent

    async def spy_dispatch(ctx, agent):
        if agent.id == inner_target.id:
            inner_dispatched["n"] += 1
        return await real_dispatch(ctx, agent)

    monkeypatch.setattr(runtime, "dispatch_agent", spy_dispatch)
    # _run_subagent(nodes/composite.py, R2-5 이동)가 자기 모듈 네임스페이스에서
    # dispatch_agent를 직접 호출하므로, runtime 재수출만 패치하면 이 경로가
    # 패치를 우회한다 — nodes.composite도 함께 패치해야 seam이 보존된다.
    from biznotes_core.services.agents.nodes import composite as nodes_composite

    monkeypatch.setattr(nodes_composite, "dispatch_agent", spy_dispatch)

    call_count = {"n": 0}

    def script(i):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _tool_turn("call_agent", {"agent_id": str(sub_agent.id)})
        if call_count["n"] == 2:
            # 서브에이전트 내부 react 노드가 승인 없이 부작용 도구 호출 시도.
            return _tool_turn("call_agent", {"agent_id": str(inner_target.id)})
        return _final_turn("마무리")

    _patch_completer(monkeypatch, script)

    ctx = _ctx(db, user_a.id)
    summary = await execute_agent(
        ctx, "workflow",
        {
            "nodes": [
                {
                    "id": "r", "type": "react_agent", "goal": "중첩",
                    "auto_approve_tools": {"call_agent": {"agent_ids": [str(sub_agent.id)]}},
                }
            ],
            "edges": [],
        },
    )
    assert inner_dispatched["n"] == 0, "내부 부작용 도구는 승인 없이 실행되지 않아야 한다"
    assert summary == "마무리"


# ── codex 리뷰 수정 검증 (Angle A/C 발견 버그) ────────────────────────────────


async def test_resume_multi_call_turn_does_not_rerun_already_executed_call(
    db, user_a, mock_llm, monkeypatch
):
    """[Angle A 리뷰] 한 턴에 tool_calls [C, D]가 있고 C 실행 후 D가 승인 대기에
    걸리면, 재개 시 이미 실행된 C가 checkpoint에 재포함되어 중복 실행되면 안 된다.
    (identity 기반 필터 버그 — 위치 슬라이싱으로 수정됨)"""
    import json

    target1 = await _make_plain_worker(db, user_a.id, scope="system")
    target2 = await _make_plain_worker(db, user_a.id, scope="system")

    dispatched: list[str] = []
    real_dispatch = runtime.dispatch_agent

    async def spy_dispatch(ctx, agent):
        dispatched.append(str(agent.id))
        return await real_dispatch(ctx, agent)

    monkeypatch.setattr(runtime, "dispatch_agent", spy_dispatch)
    # _run_subagent(nodes/composite.py, R2-5 이동)가 자기 모듈 네임스페이스에서
    # dispatch_agent를 직접 호출하므로, runtime 재수출만 패치하면 이 경로가
    # 패치를 우회한다 — nodes.composite도 함께 패치해야 seam이 보존된다.
    from biznotes_core.services.agents.nodes import composite as nodes_composite

    monkeypatch.setattr(nodes_composite, "dispatch_agent", spy_dispatch)

    def two_calls_turn():
        c1 = ToolCall(id="a", name="call_agent", arguments={"agent_id": str(target1.id)})
        c2 = ToolCall(id="b", name="call_agent", arguments={"agent_id": str(target2.id)})
        assistant = {
            "role": "assistant", "content": "",
            "tool_calls": [
                {"id": "a", "type": "function",
                 "function": {"name": "call_agent",
                              "arguments": json.dumps({"agent_id": str(target1.id)})}},
                {"id": "b", "type": "function",
                 "function": {"name": "call_agent",
                              "arguments": json.dumps({"agent_id": str(target2.id)})}},
            ],
        }
        return ToolTurnResult(
            content="", tool_calls=[c1, c2], assistant_message=assistant,
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, model=None,
        )

    call_idx = {"n": 0}

    def script(i):
        if call_idx["n"] == 0:
            call_idx["n"] += 1
            return two_calls_turn()
        return _final_turn("재개완료")

    _patch_completer(monkeypatch, script)

    # target1(C)만 자동 승인 — target2(D)는 게이트에 걸림.
    ctx1 = _ctx(db, user_a.id)
    summary1 = await execute_agent(
        ctx1, "workflow",
        {
            "nodes": [
                {
                    "id": "r", "type": "react_agent", "goal": "다중 호출",
                    "auto_approve_tools": {"call_agent": {"agent_ids": [str(target1.id)]}},
                }
            ],
            "edges": [],
        },
    )
    assert summary1 == ""
    assert dispatched == [str(target1.id)], "C(target1)만 자동승인으로 먼저 실행돼야 한다"

    checkpoint = dict(ctx1.pending_approval)
    assert checkpoint["tool_name"] == "call_agent"
    assert checkpoint["react_pending_tool_call"]["name"] == "call_agent"
    # 버그 수정 검증: remaining_calls에 이미 실행된 target1 호출이 남아있지 않아야 한다.
    remaining_ids = [
        json.loads(c["arguments"]) if isinstance(c["arguments"], str) else c["arguments"]
        for c in checkpoint.get("react_remaining_calls_in_turn", [])
    ]
    assert all(r.get("agent_id") != str(target1.id) for r in remaining_ids), (
        "이미 실행된 target1 호출이 remaining_calls에 재포함되면 안 된다"
    )

    checkpoint["react_approval_status"] = "approved"
    ctx2 = _ctx(db, user_a.id)
    approval.restore_run_context(ctx2, checkpoint)  # 워커와 같은 순서(#91)
    definition_resume = {
        "nodes": [
            {
                "id": "r", "type": "react_agent", "goal": "다중 호출", "_resume": checkpoint,
                "auto_approve_tools": {"call_agent": {"agent_ids": [str(target1.id)]}},
            }
        ],
        "edges": [],
    }
    summary2 = await execute_agent(
        ctx2, "workflow", definition_resume, resume_state=checkpoint["graph_state"]
    )
    assert summary2 == "재개완료"
    # target1은 재개 후에도 다시 나타나면 안 된다(중복 실행 방지).
    assert dispatched == [str(target1.id), str(target2.id)], (
        "재개 시 target1(C)이 중복 실행되지 않고 target2(D)만 1회 실행되어야 한다"
    )


async def test_reserved_node_id_rejected_fail_fast(db, user_a):
    """[Angle A 리뷰] 노드 id가 state 예약 메타키("params"/"user")와 충돌하면
    재개 시 completed_node_ids 오판을 방지하기 위해 그래프 구성 시점에 즉시 거부."""
    with pytest.raises(runtime.AgentRuntimeError, match="예약어"):
        await execute_agent(
            _ctx(db, user_a.id), "workflow",
            {
                "nodes": [
                    {"id": "params", "type": "constant", "value": "x"},
                    {"id": "r", "type": "react_agent", "goal": "y"},
                ],
                "edges": [["params", "r"]],
            },
        )


async def test_error_handler_does_not_absorb_subagent_approval_required(
    db, user_a, mock_llm, monkeypatch
):
    """[Angle C 리뷰] error_handler가 보호하는 서브에이전트 내부에서 승인이 필요한
    호출이 발생하면, 일반 실패로 흡수해 재시도·폴백하지 않고 즉시 재전파해야 한다."""
    inner_target = await _make_plain_worker(db, user_a.id, scope="system")
    sub_agent = Agent(
        name=f"nested-react-{uuid.uuid4().hex[:6]}", kind="workflow", icon="🤖",
        definition={
            "nodes": [{"id": "inner", "type": "react_agent", "goal": "내부 부작용"}],
            "edges": [],
        },
        params_schema={}, scope="system", owner_id=user_a.id, enabled=True,
    )
    db.add(sub_agent)
    await db.commit()
    await db.refresh(sub_agent)

    fallback_dispatched = {"n": 0}
    real_dispatch = runtime.dispatch_agent

    async def spy_dispatch(ctx, agent):
        if agent.id == inner_target.id:
            fallback_dispatched["n"] += 1
        return await real_dispatch(ctx, agent)

    monkeypatch.setattr(runtime, "dispatch_agent", spy_dispatch)
    # _run_subagent(nodes/composite.py, R2-5 이동)가 자기 모듈 네임스페이스에서
    # dispatch_agent를 직접 호출하므로, runtime 재수출만 패치하면 이 경로가
    # 패치를 우회한다 — nodes.composite도 함께 패치해야 seam이 보존된다.
    from biznotes_core.services.agents.nodes import composite as nodes_composite

    monkeypatch.setattr(nodes_composite, "dispatch_agent", spy_dispatch)
    _patch_completer(
        monkeypatch, lambda i: _tool_turn("call_agent", {"agent_id": str(inner_target.id)})
    )

    ctx = _ctx(db, user_a.id)
    with pytest.raises(runtime.SubagentApprovalNotSupportedError):
        await runtime._exec_error_handler(
            ctx,
            {
                "protected_agent_id": str(sub_agent.id),
                "max_retries": 2,
                "fallback_agent_id": str(inner_target.id),
            },
            {},
        )
    # 재시도·폴백 모두 실행되지 않아야 한다(정책적 차단은 복구 대상이 아님).
    assert fallback_dispatched["n"] == 0, "폴백 에이전트가 실행되면 안 된다"
