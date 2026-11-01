"""Phase 2 — `react_agent` 노드 타입 + 루프 가드 단위 테스트.

REACT_AGENT_PLAN.md §3 Phase 2 수락기준에 1:1 대응한다.
- 워크플로우 노드로 정상 편입 + mock 도구 1회 호출→마무리(결정적)
- fail-fast(F6: 필수키 누락·미지 설정 키)
- 기본 안전 도구(T4·Q2: rag_search·call_agent)
- max_iters 하드상한 클램프(§5.1)
- 예산 가드(T2: 미설정 시 기본상한, 초과 시 부분 결과로 중단)
- call_agent 가로 fan-out 상한(T3: max_subagent_calls)
- react→call_agent 세로 재귀 깊이 가드(MAX_AGENT_DEPTH)
- 병렬 supervisor/loop 워커 실행 금지 가드(T5/F1)
- tool-call 보조 로깅 + 토큰/비용 이중계상 없음(Q5)

mock 의존(`_mock_runtime_deps`)은 test_workflows.py 패턴을 재사용한다.
"""

import json
import socket
import uuid
from pathlib import Path

import httpx
import pytest
from biznotes_core.config import get_settings
from biznotes_core.models import Agent
from biznotes_core.services.agents import runtime
from biznotes_core.services.agents.runtime import (
    _REACT_TOOL_NODE_TYPE,
    AgentRuntimeError,
    RunContext,
    execute_agent,
)
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult


def _ctx(db, user_id=None, *, depth=0, agent_id=None):
    return RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=user_id,
        agent_id=agent_id,
        depth=depth,
    )


async def _make_react_worker(db, owner_id, *, goal="조사", scope="system"):
    """react_agent 노드 1개로 구성된 워크플로우 에이전트(서브에이전트 호출 대상)."""
    agent = Agent(
        name=f"react-sub-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        icon="🤖",
        definition={
            "nodes": [{"id": "r", "type": "react_agent", "goal": goal}],
            "edges": [],
        },
        params_schema={},
        scope=scope,
        owner_id=owner_id,
        enabled=True,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


async def _make_plain_worker(db, owner_id, *, scope="system"):
    """llm 노드 1개 워크플로우 — call_agent 대상(깊이 가드 양성 제어용)."""
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


# ── 스크립트 가능한 가짜 complete_with_tools ──────────────────────────────────


def _tool_turn(name: str, args: dict, usage: dict | None = None) -> ToolTurnResult:
    call = ToolCall(id="c", name=name, arguments=args)
    assistant = {
        "role": "assistant",
        "content": "",  # 루프 소진 후 finalize(mock) 경로가 None content에서 깨지지 않도록
        "tool_calls": [
            {"id": "c", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}
        ],
    }
    return ToolTurnResult(
        content="",
        tool_calls=[call],
        assistant_message=assistant,
        usage=usage or {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        model=None,  # model=None → 비용 미가산(예산 토큰 테스트 결정성)
    )


def _final_turn(text: str = "끝") -> ToolTurnResult:
    return ToolTurnResult(
        content=text,
        tool_calls=[],
        assistant_message={"role": "assistant", "content": text},
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        model=None,
    )


def _patch_completer(monkeypatch, script):
    """gateway_llm.complete_with_tools를 script(turn_index)->ToolTurnResult로 교체."""
    calls = {"n": 0}

    async def fake(db, model_id, messages, tools, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        return script(i)

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake)
    return calls


# ── 통합/결정성 (수락기준: 워크플로우 편입 + mock 1회 호출→마무리) ─────────────


async def test_react_node_runs_in_workflow_tool_then_finish(
    db, user_a, _mock_runtime_deps, mock_llm
):
    """kind=workflow의 react_agent 노드가 위상정렬·실행에 편입되고,
    mock 프로바이더로 도구 1회 호출→마무리가 결정적으로 통과한다."""
    definition = {
        "nodes": [{"id": "r", "type": "react_agent", "goal": "커피 추출 조사"}],
        "edges": [],
    }
    ctx = _ctx(db, user_a.id)
    summary = await execute_agent(ctx, "workflow", definition)
    # mock 루프: rag_search 1회(결과 없음) → 도구 결과 종합 마무리.
    assert "도구 결과를 종합" in summary
    assert "관련 발췌를 찾지 못했습니다" in summary
    assert ctx.usage["total_tokens"] > 0


async def test_default_tools_are_rag_and_call_agent(
    db, user_a, _mock_runtime_deps, mock_llm, monkeypatch
):
    """도구 미지정 시 기본 안전값(rag_search·call_agent만) 노출(T4·Q2)."""
    from biznotes_core.services.agents import tool_registry

    captured: dict[str, object] = {}
    real = tool_registry.build_tool_specs

    async def spy(ctx, allowed=None):
        captured["allowed"] = allowed
        specs, dispatchers = await real(ctx, allowed)
        captured["names"] = [s["function"]["name"] for s in specs]
        return specs, dispatchers

    monkeypatch.setattr(tool_registry, "build_tool_specs", spy)

    await execute_agent(
        _ctx(db, user_a.id),
        "workflow",
        {"nodes": [{"id": "r", "type": "react_agent", "goal": "조사"}], "edges": []},
    )
    assert captured["allowed"] is None  # 미지정 → 기본 안전값 경로
    assert captured["names"] == ["rag_search", "call_agent"]


# ── fail-fast (F6) ───────────────────────────────────────────────────────────


async def test_missing_goal_fails_fast(db, user_a, mock_llm):
    """필수 키(goal/prompt) 누락 시 진입부에서 fail-fast."""
    with pytest.raises(AgentRuntimeError, match="goal"):
        await execute_agent(
            _ctx(db, user_a.id),
            "workflow",
            {"nodes": [{"id": "r", "type": "react_agent"}], "edges": []},
        )


async def test_unknown_config_key_fails_fast(db, user_a, mock_llm):
    """미지 설정 키는 명확한 메시지로 즉시 거부."""
    with pytest.raises(AgentRuntimeError, match="알 수 없는 설정"):
        await execute_agent(
            _ctx(db, user_a.id),
            "workflow",
            {
                "nodes": [
                    {"id": "r", "type": "react_agent", "goal": "x", "bogus_key": 1}
                ],
                "edges": [],
            },
        )


# ── 클램프 / 무제한 금지 (§5.1) ──────────────────────────────────────────────


def test_clamp_defaults_and_hard_caps():
    """미설정=기본값, 하드상한 초과=상한, 비정상값=기본값(무제한 불가)."""
    from biznotes_core.services.agents.runtime import _clamp_react_float, _clamp_react_int

    assert _clamp_react_int(None, "max_iters") == 6
    assert _clamp_react_int(999, "max_iters") == 12  # 하드상한
    assert _clamp_react_int(0, "max_iters") == 6  # 비정상→기본
    assert _clamp_react_int("bad", "max_iters") == 6
    assert _clamp_react_int(None, "token_budget") == 100_000
    assert _clamp_react_int(10_000_000, "token_budget") == 200_000
    assert _clamp_react_int(None, "max_subagent_calls") == 5
    assert _clamp_react_int(100, "max_subagent_calls") == 10
    assert _clamp_react_float(None, "cost_budget_usd") == 0.50
    assert _clamp_react_float(99.0, "cost_budget_usd") == 2.00


async def test_max_iters_clamped_to_hard_cap(
    db, user_a, _mock_runtime_deps, mock_llm, monkeypatch
):
    """max_iters를 상한 초과로 설정해도 12회로 클램프된다(이후 도구 없이 마무리)."""
    calls = _patch_completer(monkeypatch, lambda i: _tool_turn("rag_search", {"query": "q"}))

    await execute_agent(
        _ctx(db, user_a.id),
        "workflow",
        {
            "nodes": [
                {"id": "r", "type": "react_agent", "goal": "조사", "max_iters": 999}
            ],
            "edges": [],
        },
    )
    # 루프는 하드상한 12턴까지만 complete_with_tools를 호출한다(이후 finalize는 별 함수).
    assert calls["n"] == 12


# ── 예산 가드 (T2) ───────────────────────────────────────────────────────────


async def test_budget_exceeded_stops_with_partial(db, monkeypatch):
    """누적 토큰이 token_budget을 넘으면 즉시 중단하고 부분 결과로 마무리(전체 실패 아님)."""
    retrieve_calls = {"n": 0}

    async def fake_retrieve(db_, nb, q, source_ids, top_k=6):
        from biznotes_core.services.rag_chat import RetrievalResult

        retrieve_calls["n"] += 1
        return RetrievalResult(citations=[], insufficient_context=True)

    monkeypatch.setattr("biznotes_core.services.rag_chat.retrieve_context", fake_retrieve)

    # 첫 턴부터 거대한 토큰 사용 → 기본 token_budget(100k) 초과.
    calls = _patch_completer(
        monkeypatch,
        lambda i: _tool_turn(
            "rag_search", {"query": "q"},
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 999_999},
        ),
    )
    # 시스템 실행(user_id=None) — check_usage Redis 경로 우회로 결정성 확보.
    ctx = _ctx(db, user_id=None)
    summary = await execute_agent(
        ctx,
        "workflow",
        {"nodes": [{"id": "r", "type": "react_agent", "goal": "조사"}], "edges": []},
    )
    assert calls["n"] == 1, "예산 초과 후 추가 턴을 호출하지 않아야 한다"
    assert retrieve_calls["n"] == 0, "예산 초과 시 보류 중인 도구를 실행하지 않아야 한다"
    # 코어 v0.26.0(#385): 본문 없이 소진하면 걸린 한도를 밝힌다(옛 「부분 결과」 문구는 없다)
    assert "1회 토큰 한도 100,000" in summary
    assert ctx.usage["total_tokens"] >= 100_000


# ── fan-out 상한 (T3) ────────────────────────────────────────────────────────


async def test_subagent_fanout_capped(db, user_a, mock_llm, monkeypatch):
    """단일 react 노드의 누적 call_agent 호출이 max_subagent_calls를 넘지 못한다."""
    sub_calls = {"n": 0}

    async def fake_run_subagent(ctx, raw_id, params, **_kwargs):
        sub_calls["n"] += 1
        return "sub-ok"

    monkeypatch.setattr(runtime, "_run_subagent", fake_run_subagent)

    target = uuid.uuid4()
    # 모델이 매 턴 call_agent를 요청하다가 5턴째 최종 텍스트로 종료.
    def script(i):
        if i < 4:
            return _tool_turn("call_agent", {"agent_id": str(target)})
        return _final_turn("완료")

    _patch_completer(monkeypatch, script)

    ctx = _ctx(db, user_a.id)
    await execute_agent(
        ctx,
        "workflow",
        {
            "nodes": [
                {
                    "id": "r", "type": "react_agent", "goal": "여러 서브 호출",
                    "max_subagent_calls": 2,
                    # call_agent는 Phase 19a부터 HITL 게이트 대상(side_effect) —
                    # fan-out 가드 검증 의도를 유지하려면 자동 승인 필요.
                    "auto_approve_tools": {"call_agent": {"agent_ids": [str(target)]}},
                }
            ],
            "edges": [],
        },
    )
    # 상한 2 → 실제 디스패치는 2회까지만. 이후 요청은 거부 메시지로 대체.
    assert sub_calls["n"] == 2
    # 전역 카운터 셀도 동일하게 2 증가(가로 fan-out 전역 방어선).
    assert ctx.react_subagent_calls[0] == 2


# ── codex 리뷰 수정 (C1 빈 화이트리스트 / C2 도구후 예산 / C3 서브 비용) ──────


async def test_empty_allowed_tools_fails_fast(db, user_a, mock_llm):
    """[C1] allowed_tools=[]는 기본값으로 둔갑하지 않고 fail-fast(빈 화이트리스트 존중)."""
    with pytest.raises(AgentRuntimeError, match="비어 있습니다"):
        await execute_agent(
            _ctx(db, user_a.id),
            "workflow",
            {
                "nodes": [
                    {"id": "r", "type": "react_agent", "goal": "x", "allowed_tools": []}
                ],
                "edges": [],
            },
        )


def test_budget_measured_from_node_baseline():
    """[C4] 예산은 노드 진입 기준선 이후의 델타로 판정한다(선행 노드 소비 무관)."""
    from biznotes_core.services.agents.runtime import _react_budget_exceeded, _ReactGuards

    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=None,  # type: ignore[arg-type]  # 순수 가드 계산 — DB 미사용
        params={},
    )
    ctx.usage["total_tokens"] = 90_000  # 선행 노드가 이미 소비
    ctx.cost_total[0] = 0.40
    guards = _ReactGuards(
        token_budget=100_000, cost_budget_usd=0.50, max_subagent_calls=5,
        aux_node_id="r", token_base=90_000, cost_base=0.40,
    )
    # 진입 직후(델타 0) — 선행 소비분이 본 노드 예산을 깎지 않는다.
    assert _react_budget_exceeded(ctx, guards) is False
    # 본 노드가 token_budget만큼 더 쓰면 초과.
    ctx.usage["total_tokens"] = 190_000
    assert _react_budget_exceeded(ctx, guards) is True
    # 비용 델타 초과도 독립적으로 감지.
    ctx.usage["total_tokens"] = 90_000
    ctx.cost_total[0] = 0.91
    assert _react_budget_exceeded(ctx, guards) is True


async def test_budget_recheck_after_subagent_cost(db, user_a, mock_llm, monkeypatch):
    """[C2/C3] call_agent가 공유 비용 셀을 예산 위로 밀면 다음 모델 턴 전에 중단된다.

    서브에이전트 비용(cost_total 셀)이 부모 react 예산 가드에 보이고(C3),
    도구 실행 직후 재검사로 추가 LLM 턴을 막는다(C2).
    """
    sub_calls = {"n": 0}

    async def fake_run_subagent(ctx, raw_id, params, **_kwargs):
        sub_calls["n"] += 1
        ctx.cost_total[0] += 1.0  # 기본 cost_budget 0.50를 초과하는 서브에이전트 비용
        return "비싼-서브-결과"

    monkeypatch.setattr(runtime, "_run_subagent", fake_run_subagent)

    target = uuid.uuid4()
    # 모델은 매 턴 call_agent를 요청 — 가드가 없으면 무한 호출.
    calls = _patch_completer(
        monkeypatch, lambda i: _tool_turn("call_agent", {"agent_id": str(target)})
    )

    ctx = _ctx(db, user_a.id)
    summary = await execute_agent(
        ctx,
        "workflow",
        {
            "nodes": [
                {
                    "id": "r", "type": "react_agent", "goal": "비싼 호출",
                    "auto_approve_tools": {"call_agent": {"agent_ids": [str(target)]}},
                }
            ],
            "edges": [],
        },
    )
    assert sub_calls["n"] == 1, "서브에이전트는 한 번만 실행되어야 한다"
    assert calls["n"] == 1, "예산 초과 후 추가 모델 턴을 호출하지 않아야 한다"
    assert "1회 비용 한도 $0.50" in summary  # 코어 v0.26.0(#385) 소진 문구
    assert ctx.cost_total[0] >= 0.50


async def test_budget_breaks_mid_turn_on_multiple_tool_calls(
    db, user_a, mock_llm, monkeypatch
):
    """[C6] 한 턴에 여러 call_agent가 와도 예산을 넘긴 즉시 남은 호출을 실행하지 않는다."""
    sub_calls = {"n": 0}

    async def fake_run_subagent(ctx, raw_id, params, **_kwargs):
        sub_calls["n"] += 1
        ctx.cost_total[0] += 1.0  # 첫 호출에서 곧바로 예산(0.50) 초과
        return "ok"

    monkeypatch.setattr(runtime, "_run_subagent", fake_run_subagent)

    target = uuid.uuid4()
    # 한 어시스턴트 턴이 call_agent 두 개를 동시에 요청.
    def two_calls_turn():
        c1 = ToolCall(id="a", name="call_agent", arguments={"agent_id": str(target)})
        c2 = ToolCall(id="b", name="call_agent", arguments={"agent_id": str(target)})
        assistant = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "a", "type": "function",
                 "function": {"name": "call_agent",
                              "arguments": json.dumps({"agent_id": str(target)})}},
                {"id": "b", "type": "function",
                 "function": {"name": "call_agent",
                              "arguments": json.dumps({"agent_id": str(target)})}},
            ],
        }
        return ToolTurnResult(
            content="", tool_calls=[c1, c2], assistant_message=assistant,
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            model=None,
        )

    calls = _patch_completer(monkeypatch, lambda i: two_calls_turn())

    await execute_agent(
        _ctx(db, user_a.id),
        "workflow",
        {
            "nodes": [
                {
                    "id": "r", "type": "react_agent", "goal": "동시 호출",
                    "auto_approve_tools": {"call_agent": {"agent_ids": [str(target)]}},
                }
            ],
            "edges": [],
        },
    )
    # 첫 call_agent가 예산을 넘기면 같은 턴의 두 번째 call_agent는 실행되지 않는다.
    assert sub_calls["n"] == 1
    assert calls["n"] == 1


async def test_finalizer_skips_llm_when_budget_exhausted(db, monkeypatch):
    """[C8] 루프 소진 마무리 경로도 예산 가드를 따른다 — 초과 시 추가 LLM 호출 없음."""
    called = {"n": 0}

    async def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("예산 초과 시 마무리 LLM을 호출하면 안 된다")

    monkeypatch.setattr(gateway_llm, "complete_stream_with_fallback", boom)

    ctx = _ctx(db, user_id=None)  # 시스템 실행 — 기본 도구(rag_search·call_agent) 노출
    ctx.usage["total_tokens"] = 999_999  # 이미 예산 초과 상태
    guards = runtime._ReactGuards(
        token_budget=100, cost_budget_usd=0.5, max_subagent_calls=5,
        aux_node_id="r", token_base=0, cost_base=0.0,
    )
    out = await runtime._exec_llm_with_tools(
        ctx, {"id": "r"}, [{"role": "user", "content": "x"}], None, None,
        guards=guards, max_iters=0,  # 즉시 루프 소진 → 마무리 경로 진입
    )
    # 코어 v0.26.0(#385): 본문 없이 소진하면 어느 한도에 걸렸는지 알리는 문구를 내고,
    # 사유·사용량을 `BUDGET_INFO_KEY`에 싣는다(옛 `BUDGET_PARTIAL_NOTE`는 더 이상 출력하지 않는다)
    from biznotes_core.services.agents.react.loop import BUDGET_INFO_KEY

    assert out["text"] == "(1회 토큰 한도 100에 도달해 답변을 만들지 못했습니다.)"
    budget = out[BUDGET_INFO_KEY]
    assert budget["reason"] == "token"
    assert budget["has_content"] is False
    assert budget["token_budget"] == 100
    assert budget["used_tokens"] == 999_999
    assert called["n"] == 0, "예산 초과 마무리 경로는 LLM을 호출하지 않아야 한다"


async def test_finalizer_over_budget_converts_to_partial(
    db, _mock_runtime_deps, monkeypatch
):
    """[C9] 마무리 LLM 호출이 예산을 넘기면 전체 텍스트가 아닌 부분 결과로 변환된다."""

    async def fake_stream(db_, model_id, messages, result, *a, **k):
        result.usage = {"total_tokens": 999_999}  # 마무리 호출이 예산을 초과시킴
        result.model = None

        async def gen():
            yield "최종 답변(예산 초과)"

        return gen()

    monkeypatch.setattr(gateway_llm, "complete_stream_with_fallback", fake_stream)
    # 단일 반복 동안은 도구 턴 → 루프 소진 → 마무리 경로 진입.
    _patch_completer(monkeypatch, lambda i: _tool_turn("rag_search", {"query": "q"}))

    ctx = _ctx(db, user_id=None)
    guards = runtime._ReactGuards(
        token_budget=100, cost_budget_usd=0.5, max_subagent_calls=5,
        aux_node_id="r", token_base=0, cost_base=0.0,
    )
    out = await runtime._exec_llm_with_tools(
        ctx, {"id": "r"}, [{"role": "user", "content": "x"}], None, None,
        guards=guards, max_iters=1,
    )
    # 마무리 출력은 버리고 걸린 한도를 밝힌다(코어 v0.26.0 #385)
    assert out["text"] == "(1회 토큰 한도 100에 도달해 답변을 만들지 못했습니다.)"
    assert "최종 답변" not in out["text"]


# ── 세로 재귀 깊이 가드 (MAX_AGENT_DEPTH) ────────────────────────────────────


async def test_react_call_agent_blocked_by_depth(
    db, user_a, _mock_runtime_deps, mock_llm, monkeypatch
):
    """react→call_agent 재귀가 MAX_AGENT_DEPTH에서 차단된다(서브에이전트 미실행)."""
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
        return _final_turn("끝")

    _patch_completer(monkeypatch, script)

    # 이미 최대 깊이 — call_agent는 _run_subagent 깊이 가드에 막힌다.
    ctx = _ctx(db, user_a.id, depth=runtime.MAX_AGENT_DEPTH)
    summary = await execute_agent(
        ctx,
        "workflow",
        {
            "nodes": [
                {
                    "id": "r", "type": "react_agent", "goal": "재귀",
                    "auto_approve_tools": {"call_agent": {"agent_ids": [str(target.id)]}},
                }
            ],
            "edges": [],
        },
    )
    assert dispatched["n"] == 0, "깊이 초과로 서브에이전트가 실행되지 않아야 한다"
    assert summary == "끝"  # 루프는 실패를 흡수하고 정상 마무리


# ── 병렬 워커 금지 가드 (T5/F1) ──────────────────────────────────────────────


async def test_react_blocked_in_parallel_supervisor(
    db, user_a, _mock_runtime_deps, mock_llm
):
    """react_agent가 parallel supervisor 워커로 실행되면 차단된다(세션 레이스 방지)."""
    worker = await _make_react_worker(db, user_a.id, scope="system")
    definition = {
        "nodes": [
            {
                "id": "sup", "type": "supervisor",
                "workers": [{"agent_id": str(worker.id)}],
                "parallel": True,
            }
        ],
        "edges": [],
    }
    with pytest.raises(AgentRuntimeError, match="병렬"):
        await execute_agent(_ctx(db, user_a.id), "workflow", definition)


async def test_react_allowed_in_sequential_supervisor(
    db, user_a, _mock_runtime_deps, mock_llm
):
    """순차(parallel=false) supervisor 워커로는 react_agent가 정상 실행된다."""
    worker = await _make_react_worker(db, user_a.id, scope="system")
    definition = {
        "nodes": [
            {
                "id": "sup", "type": "supervisor",
                "workers": [{"agent_id": str(worker.id)}],
                "parallel": False,
            }
        ],
        "edges": [],
    }
    summary = await execute_agent(_ctx(db, user_a.id), "workflow", definition)
    assert "도구 결과를 종합" in summary


async def test_branch_parallel_propagates_flag_to_children(db):
    """branch_parallel/child가 in_parallel_branch 플래그를 자식에 전파한다."""
    ctx = _ctx(db, uuid.uuid4())
    assert ctx.in_parallel_branch is False
    branch = ctx.branch_parallel()
    assert branch.in_parallel_branch is True
    child = branch.child(uuid.uuid4(), {})
    assert child.in_parallel_branch is True
    # 전역 카운터 셀은 동일 객체 공유.
    assert child.react_subagent_calls is ctx.react_subagent_calls


# ── Phase 3: run_react_node 전체 텍스트 보존 + 이력 주입 ─────────────────────


async def test_run_react_node_returns_full_text(db, user_a, mock_llm, monkeypatch):
    """[Phase 3·codex D1] run_react_node는 execute_agent의 500자 요약 절단 없이
    react 노드의 전체 텍스트를 반환한다(채팅 자율 모드 진입점)."""
    from biznotes_core.services.agents.runtime import run_react_node

    long_text = "가" * 1200  # 500자 요약 절단을 초과하는 긴 응답
    _patch_completer(monkeypatch, lambda i: _final_turn(long_text))

    result = await run_react_node(
        _ctx(db, user_a.id), {"id": "r", "type": "react_agent", "goal": "조사"}
    )
    assert result["text"] == long_text
    assert len(result["text"]) > 500


async def test_react_history_injected_into_messages(db, user_a, mock_llm, monkeypatch):
    """[Phase 3·codex D2] node['history']가 system과 현재 goal 사이에 role 보존되어
    삽입된다(후속 턴 맥락 유지). 미지원 role/빈 콘텐츠는 제외된다."""
    from biznotes_core.services.agents.runtime import run_react_node

    captured: dict[str, list] = {}

    async def capturing(db_, model_id, messages, tools, **kwargs):
        captured["messages"] = list(messages)
        return _final_turn("끝")

    monkeypatch.setattr(gateway_llm, "complete_with_tools", capturing)

    await run_react_node(
        _ctx(db, user_a.id),
        {
            "id": "r",
            "type": "react_agent",
            "goal": "두 번째 것 설명해줘",
            "history": [
                {"role": "user", "content": "커피 추출 방법은?"},
                {"role": "assistant", "content": "에스프레소와 드립이 있습니다."},
                {"role": "system", "content": "무시될 role"},  # user/assistant 외 제외
                {"role": "user", "content": ""},  # 빈 콘텐츠 제외
            ],
        },
    )

    msgs = captured["messages"]
    assert msgs[0]["role"] == "system"  # 가드 프롬프트
    assert msgs[1] == {"role": "user", "content": "커피 추출 방법은?"}
    assert msgs[2] == {"role": "assistant", "content": "에스프레소와 드립이 있습니다."}
    assert msgs[3] == {"role": "user", "content": "두 번째 것 설명해줘"}  # 현재 goal 말미
    assert len(msgs) == 4  # system + 유효 이력 2 + goal (무시 role/빈 콘텐츠 제외)


async def test_react_goal_via_params_preserves_braces(db, user_a, mock_llm, monkeypatch):
    """[Phase 3·codex D5] goal을 `{params.x}`로 두고 본문을 params로 주입하면
    내용 속 중괄호(`{name}` 등)가 단일 패스 렌더에서 그대로 보존된다(채팅 자율 모드)."""
    from biznotes_core.services.agents.runtime import run_react_node

    raw = "이 코드 설명해줘: def f(x): return {x: x} 그리고 {params.secret}는?"
    captured: dict[str, list] = {}

    async def capturing(db_, model_id, messages, tools, **kwargs):
        captured["messages"] = list(messages)
        return _final_turn("끝")

    monkeypatch.setattr(gateway_llm, "complete_with_tools", capturing)

    await run_react_node(
        _ctx(db, user_a.id),
        {"id": "r", "type": "react_agent", "goal": "{params.chat_message}"},
        {"params": {"chat_message": raw}},
    )
    # 마지막 user 메시지가 원문과 동일(중괄호 절단 없음).
    assert captured["messages"][-1] == {"role": "user", "content": raw}


async def test_react_surfaces_finalize_model_and_fallback(db, _mock_runtime_deps, monkeypatch):
    """[Phase 3·codex D6] 루프 소진 마무리가 폴백 모델로 넘어가면 react 반환 dict의
    model/fallback_used가 실제 마무리 모델을 반영한다(guards 있는 react 경로만)."""

    class _FakeLLMModel:
        id = uuid.uuid4()
        display_name = "Fallback Model"
        cost_per_1k: dict = {}

    class _FakeResolved:
        model = _FakeLLMModel()

    async def fake_stream(db_, model_id, messages, result, *a, **k):
        result.usage = {"total_tokens": 2}
        result.model = _FakeResolved()
        result.fallback_used = True

        async def gen():
            yield "최종 답변"

        return gen()

    monkeypatch.setattr(gateway_llm, "complete_stream_with_fallback", fake_stream)
    # 1턴 도구 호출 → 루프(max_iters=1) 소진 → 마무리(폴백) 경로 진입.
    _patch_completer(monkeypatch, lambda i: _tool_turn("rag_search", {"query": "q"}))

    guards = runtime._ReactGuards(
        token_budget=100_000, cost_budget_usd=2.0, max_subagent_calls=5, aux_node_id="r",
    )
    out = await runtime._exec_llm_with_tools(
        _ctx(db, user_id=None), {"id": "r"}, [{"role": "user", "content": "x"}], None, None,
        guards=guards, max_iters=1,
    )
    assert out["fallback_used"] is True
    # model은 JSON 직렬화 가능한 dict({id, display_name})이어야 한다(codex D8).
    assert out["model"] == {"id": str(_FakeLLMModel.id), "display_name": "Fallback Model"}
    import json

    json.dumps(out["model"])  # JSONB 직렬화 가능 확인


async def test_react_workflow_output_json_serializable(
    db, user_a, _mock_runtime_deps, mock_llm, monkeypatch
):
    """[Phase 3·codex D8] execute_agent 경로의 react 노드 output_data가 JSON 직렬화
    가능해야 한다 — ORM 모델 객체가 들어가면 ExecutionLog(JSONB) 종료가 깨진다."""
    import json

    captured: list = []

    async def fake_start(run_id, node_id, node_type, input_data, started_at):
        return 1

    async def fake_finish(log_id, *, status, finished_at, output_data=None, **k):
        if output_data is not None:
            captured.append(output_data)

    # execute_agent(그래프 노드 루프)는 graph.py로 이동(R2-6)하며 _log_node_*를 자기
    # 모듈 네임스페이스에서 해석하므로, 노드 종료 output_data 포착은 graph 모듈을 패치한다.
    from biznotes_core.services.agents import graph as graph_mod

    monkeypatch.setattr(graph_mod, "_log_node_start", fake_start)
    monkeypatch.setattr(graph_mod, "_log_node_finish", fake_finish)

    await execute_agent(
        _ctx(db, user_a.id),
        "workflow",
        {"nodes": [{"id": "r", "type": "react_agent", "goal": "조사"}], "edges": []},
    )
    assert captured, "react 노드 종료 output_data가 기록되어야 한다"
    for out in captured:
        json.dumps(out)  # ORM 객체가 섞이면 TypeError로 실패한다(D8 회귀 방어)


# ── tool-call 보조 로깅 + 이중계상 없음 (Q5) ─────────────────────────────────


async def test_aux_tool_logging_no_double_count(
    db, user_a, _mock_runtime_deps, mock_llm, monkeypatch
):
    """tool-call이 보조행(node_type=react_tool_call)으로 기록되되 토큰/비용은
    부모 react 노드 행에서만 집계된다(이중계상 금지)."""
    starts: dict[int, str] = {}
    finishes: list[tuple] = []

    async def fake_start(run_id, node_id, node_type, input_data, started_at):
        lid = len(starts) + 1
        starts[lid] = node_type
        return lid

    async def fake_finish(
        log_id, *, status, finished_at, output_data=None, error_message=None,
        token_usage=None, cost_usd=None, duration_ms=None,
    ):
        finishes.append((starts.get(log_id), token_usage, cost_usd))

    # 부모 react_agent 노드 행은 execute_agent 노드 루프(graph.py, R2-6 이동)가,
    # tool-call 보조행은 _aux_log_tool_call(react/loop.py, R2-4 이동)이 각자 모듈
    # 네임스페이스에서 로깅 함수를 해석하므로 두 모듈 모두 패치해야 양쪽 경로가
    # 포착된다(seam 보존).
    from biznotes_core.services.agents import graph as graph_mod
    from biznotes_core.services.agents.react import loop as react_loop

    monkeypatch.setattr(graph_mod, "_log_node_start", fake_start)
    monkeypatch.setattr(graph_mod, "_log_node_finish", fake_finish)
    monkeypatch.setattr(react_loop, "_log_node_start", fake_start)
    monkeypatch.setattr(react_loop, "_log_node_finish", fake_finish)

    await execute_agent(
        _ctx(db, user_a.id),
        "workflow",
        {"nodes": [{"id": "r", "type": "react_agent", "goal": "조사"}], "edges": []},
    )

    node_types = set(starts.values())
    assert "react_agent" in node_types  # 부모 노드 행
    assert _REACT_TOOL_NODE_TYPE in node_types  # 보조 tool-call 행

    aux = [f for f in finishes if f[0] == _REACT_TOOL_NODE_TYPE]
    assert aux, "tool-call 보조행 종료 기록이 있어야 한다"
    assert all(tu is None and cu is None for _, tu, cu in aux), "보조행은 토큰/비용 미집계"

    react_rows = [f for f in finishes if f[0] == "react_agent"]
    assert react_rows and any(tu is not None for _, tu, cu in react_rows), (
        "토큰/비용은 부모 react 노드 행에서 1회 집계되어야 한다"
    )


async def test_resume_approved_call_with_missing_dispatcher_marks_failed(db):
    """승인 재개 시 dispatcher가 사라졌다면 일반 경로와 같이 failed로 기록한다."""
    from biznotes_core.services.agents.react import loop as react_loop

    ctx = _ctx(db)
    messages: list[dict] = []
    tool_log: list[dict] = []
    await react_loop._apply_resolved_pending_call(
        ctx,
        None,
        pending_call={"id": "call-1", "name": "missing_tool", "arguments": {}},
        approval_status="approved",
        messages=messages,
        tool_log=tool_log,
        aux_idx=0,
        dispatchers={},
    )

    assert ctx.tool_calls[-1]["status"] == "failed"
    assert messages[-1]["content"] == "알 수 없는 도구: missing_tool"


async def test_tool_unavailable_error_passes_message_and_marks_failed(db):
    """안전한 ToolUnavailableError 문구는 그대로 LLM tool 메시지에 전달한다."""
    from biznotes_core.services.agents.errors import ToolUnavailableError
    from biznotes_core.services.agents.react import loop as react_loop

    async def unavailable(ctx, name, arguments):
        raise ToolUnavailableError("MCP 서버를 찾을 수 없거나 비활성화됨")

    ctx = _ctx(db)
    messages: list[dict] = []
    await react_loop._process_new_tool_call(
        ctx,
        None,
        {"id": "r"},
        call={"id": "call-1", "name": "mcp_test_find_region", "arguments": {}},
        messages=messages,
        tool_log=[],
        aux_idx=0,
        iters_done=0,
        subagent_calls=0,
        remaining_after=[],
        dispatchers={"mcp_test_find_region": unavailable},
    )

    assert ctx.tool_calls[-1]["status"] == "failed"
    assert messages[-1]["content"] == "MCP 서버를 찾을 수 없거나 비활성화됨"


async def test_resume_tool_unavailable_error_passes_message_and_marks_failed(db):
    """승인 재개 경로도 안전한 가용성 오류 문구를 그대로 전달한다."""
    from biznotes_core.services.agents.errors import ToolUnavailableError
    from biznotes_core.services.agents.react import loop as react_loop

    async def unavailable(ctx, name, arguments):
        raise ToolUnavailableError("MCP 서버를 찾을 수 없거나 비활성화됨")

    ctx = _ctx(db)
    messages: list[dict] = []
    await react_loop._apply_resolved_pending_call(
        ctx,
        None,
        pending_call={"id": "call-1", "name": "mcp_test_find_region", "arguments": {}},
        approval_status="approved",
        messages=messages,
        tool_log=[],
        aux_idx=0,
        dispatchers={"mcp_test_find_region": unavailable},
    )

    assert ctx.tool_calls[-1]["status"] == "failed"
    assert messages[-1]["content"] == "MCP 서버를 찾을 수 없거나 비활성화됨"


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_message"),
    [
        (
            "http_request",
            {"url": "ftp://example.com"},
            "요청 차단됨: 안전하지 않은 URL입니다.",
        ),
        (
            "mcp_call",
            {"server_id": "00000000-0000-0000-0000-000000000001", "tool_name": "read"},
            "MCP 서버를 찾을 수 없거나 비활성화됨",
        ),
        ("unknown_builtin", {}, "알 수 없는 도구: unknown_builtin"),
    ],
)
async def test_builtin_unavailable_results_mark_tool_call_failed(
    db, tool_name, arguments, expected_message
):
    from biznotes_core.services.agents.react import loop as react_loop
    from biznotes_core.services.agents.tool_registry import _builtin_dispatcher

    ctx = _ctx(db)
    messages: list[dict] = []
    await react_loop._process_new_tool_call(
        ctx,
        None,
        {"id": "r"},
        call={"id": "call-1", "name": tool_name, "arguments": arguments},
        messages=messages,
        tool_log=[],
        aux_idx=0,
        iters_done=0,
        subagent_calls=0,
        remaining_after=[],
        dispatchers={tool_name: _builtin_dispatcher()},
    )

    assert ctx.tool_calls[-1]["status"] == "failed"
    assert messages[-1]["content"] == expected_message


async def _assert_aux_tool_log_status(db, monkeypatch, expected_status: str) -> None:
    from datetime import UTC, datetime

    from biznotes_core.services.agents.react import loop as react_loop

    starts: list[dict] = []
    finishes: list[str] = []

    async def fake_start(run_id, node_id, node_type, input_data, started_at):
        starts.append(input_data)
        return uuid.uuid4()

    async def fake_finish(log_id, *, status, **kwargs):
        finishes.append(status)

    monkeypatch.setattr(react_loop, "_log_node_start", fake_start)
    monkeypatch.setattr(react_loop, "_log_node_finish", fake_finish)
    now = datetime.now(UTC)
    await react_loop._aux_log_tool_call(
        _ctx(db),
        runtime._ReactGuards(
            token_budget=100,
            cost_budget_usd=1.0,
            max_subagent_calls=1,
            aux_node_id="r",
        ),
        0,
        "rag_search",
        {"query": "q"},
        "result",
        now,
        now,
        tool_call_id="call-1",
        tool_status=expected_status,
    )

    assert starts[0]["tool"] == "rag_search"
    assert starts[0]["arguments"] == {"query": "q"}
    assert starts[0]["call_status"] == expected_status
    assert starts[0]["tool_call_id"] == "call-1"
    assert finishes == [expected_status]


async def test_aux_tool_log_records_call_status_succeeded(db, monkeypatch):
    await _assert_aux_tool_log_status(db, monkeypatch, "succeeded")


async def test_aux_tool_log_records_call_status_failed(db, monkeypatch):
    await _assert_aux_tool_log_status(db, monkeypatch, "failed")


async def test_aux_tool_log_records_call_status_blocked(db, monkeypatch):
    await _assert_aux_tool_log_status(db, monkeypatch, "blocked")


# ── backlog-p2 Phase 3 (AC-10 · PR-18): http_request 내장 도구의 SSRF 관문 경유 ──
#
# 네트워크 경계(DNS `getaddrinfo`·transport)만 가짜로 두고 관문 본문(core/ssrf.py)은
# 실경로로 실행한다(§3.3-3). transport는 실제 httpx.AsyncClient에 MockTransport를
# 주입해 요청이 만들어지는 과정(URL·Host 헤더·extensions)도 실제 httpx를 거친다.

_REAL_ASYNC_CLIENT = httpx.AsyncClient
_REAL_GETADDRINFO = socket.getaddrinfo


def _install_network_boundary(monkeypatch, dns: dict[str, str], handler) -> list[httpx.Request]:
    from biznotes_core.core import ssrf

    # 코어 모듈은 더 이상 설정을 모듈 레벨에 스냅샷하지 않는다 —
    # 캐시된 설정 인스턴스를 직접 패치한다.
    monkeypatch.setattr(get_settings(), "ssrf_allow_private", False)

    def fake_getaddrinfo(host, *args, **kwargs):
        ip = dns.get(host)
        if ip is None:
            # 테스트 호스트 외(예: DB localhost)는 실제 해석으로 위임.
            return _REAL_GETADDRINFO(host, *args, **kwargs)
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    class _BoundaryClient(_REAL_ASYNC_CLIENT):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(record)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(ssrf.httpx, "AsyncClient", _BoundaryClient)
    return requests


def test_http_request_builtin_has_no_direct_httpx_client():
    """AC-10: `grep -n "httpx.AsyncClient" nodes/llm.py` → 0건(관문 우회 경로 제거)."""
    from biznotes_core.services.agents.nodes import llm as llm_nodes

    assert "httpx.AsyncClient" not in Path(llm_nodes.__file__).read_text(encoding="utf-8")


async def test_http_request_goes_through_ssrf_gateway_pinned_and_truncated(db, monkeypatch):
    """정상 요청: 검증 IP로 핀 연결(Host·SNI는 원 호스트), 메서드·body 전달, 응답 8,000자 절단."""
    from biznotes_core.services.agents.tool_registry import _builtin_dispatcher

    requests = _install_network_boundary(
        monkeypatch,
        {"public.test": "93.184.216.34"},
        # 관문은 원본 전송 바이트를 스트림으로 읽는다(코어 v0.28.0 #413) — `text=`로 만든 응답은
        # 이미 읽힌 본문이라 `StreamConsumed`가 난다
        lambda request: httpx.Response(200, stream=httpx.ByteStream(b"a" * 9000)),
    )

    out = await _builtin_dispatcher()(
        _ctx(db),
        "http_request",
        {"url": "https://public.test/data?q=1", "method": "post", "body": {"k": "v"}},
    )

    assert out == "a" * 8000
    assert len(requests) == 1
    sent = requests[0]
    assert sent.method == "POST"
    assert str(sent.url) == "https://93.184.216.34/data?q=1"
    assert sent.headers["host"] == "public.test"
    assert sent.extensions["sni_hostname"] == "public.test"
    assert json.loads(sent.content) == {"k": "v"}


@pytest.mark.parametrize(
    "private_ip", ["10.0.0.7", "127.0.0.1", "169.254.169.254", "::ffff:127.0.0.1"]
)
async def test_http_request_blocks_host_resolving_to_private_ip(db, monkeypatch, private_ip):
    from biznotes_core.services.agents.errors import ToolUnavailableError
    from biznotes_core.services.agents.tool_registry import _builtin_dispatcher

    requests = _install_network_boundary(
        monkeypatch, {"looks-public.test": private_ip}, lambda request: httpx.Response(200)
    )

    with pytest.raises(ToolUnavailableError, match="요청 차단됨: 안전하지 않은 URL입니다."):
        await _builtin_dispatcher()(
            _ctx(db), "http_request", {"url": "http://looks-public.test/admin"}
        )
    assert requests == []  # 연결 시도 자체가 없다


async def test_http_request_blocks_redirect_to_private(db, monkeypatch):
    """리다이렉트 추적은 관문으로 새로 적용된 계약(HCI-P2-4) — 사설 홉은 연결 전 차단."""
    from biznotes_core.services.agents.errors import ToolUnavailableError
    from biznotes_core.services.agents.tool_registry import _builtin_dispatcher

    requests = _install_network_boundary(
        monkeypatch,
        {"public.test": "93.184.216.34", "internal.test": "10.0.0.9"},
        lambda request: httpx.Response(302, headers={"Location": "http://internal.test/secret"}),
    )

    with pytest.raises(ToolUnavailableError, match="요청 차단됨: 안전하지 않은 URL입니다."):
        await _builtin_dispatcher()(_ctx(db), "http_request", {"url": "https://public.test/r"})
    assert [str(r.url) for r in requests] == ["https://93.184.216.34/r"]


async def test_auto_approved_http_request_still_blocked_by_ssrf_gateway(
    db, user_a, mock_llm, monkeypatch
):
    """PR-18: allowlist 자동 승인(gate 없음) 경로도 같은 디스패처 → 관문을 지난다.

    allowlist 도메인이 사설 IP로 해석되면 승인 대기 없이 도구가 실패(ToolUnavailableError
    문구 그대로 LLM에 전달)하고 transport 연결은 한 번도 일어나지 않는다.
    """
    from biznotes_core.services.agents.tool_registry import auto_approve_match

    requests = _install_network_boundary(
        monkeypatch, {"allowed.test": "10.0.0.5"}, lambda request: httpx.Response(200, text="x")
    )
    args = {"url": "https://allowed.test/admin", "method": "GET"}
    policy = {"http_request": {"domains": ["allowed.test"], "methods": ["GET"]}}
    # 전제: 이 호출은 allowlist에 일치해 HITL gate를 거치지 않는다.
    assert auto_approve_match("http_request", args, policy) is True

    seen_messages: list[list[dict]] = []

    async def fake(db, model_id, messages, tools, **kwargs):
        seen_messages.append([dict(m) for m in messages])
        if len(seen_messages) == 1:
            return _tool_turn("http_request", args)
        return _final_turn("마무리")

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake)

    ctx = _ctx(db, user_a.id)
    summary = await execute_agent(
        ctx,
        "workflow",
        {
            "nodes": [
                {
                    "id": "r",
                    "type": "react_agent",
                    "goal": "자동 승인 http",
                    "allowed_tools": ["http_request"],
                    "auto_approve_tools": policy,
                }
            ],
            "edges": [],
        },
    )

    assert summary == "마무리"
    assert ctx.pending_approval is None  # gate 없이 진행됨
    assert requests == []  # 사설 IP로의 연결은 한 번도 없음
    assert [c["tool"] for c in ctx.tool_calls] == ["http_request"]
    assert ctx.tool_calls[0]["status"] == "failed"
    tool_messages = [m for m in seen_messages[1] if m.get("role") == "tool"]
    assert tool_messages[-1]["content"] == "요청 차단됨: 안전하지 않은 URL입니다."
