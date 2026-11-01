"""도구 호출 관측 계약 (#23) — 감사 행 먼저, 감사 실패 신호, `required_initial` 표시.

계획 `docs/plans/tool-call-observation/plan.md`(PR #79)의 수락 기준 1~8을 DB 없이 확인한다.
감사 행 기록(`loop._log_node_start`·`_log_node_finish`)과 모델만 대역으로 바꾼다.
"""

import json
import uuid
from typing import Any

import pytest
import structlog.testing

from biznotes_core.schemas.run_events import (
    RunEventAdapter,
    ToolCallEvent,
    ToolCallEventInput,
    ToolCallSnapshot,
)
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import ApprovalRequired
from biznotes_core.services.agents.react import approval, loop
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult

_DONE = ToolTurnResult(
    content="끝",
    assistant_message={"role": "assistant", "content": "끝"},
    usage={"total_tokens": 10},
)


def _ctx(**kw: Any) -> RunContext:
    return RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={}, **kw)  # type: ignore[arg-type]


def _node(**kw: Any) -> dict[str, Any]:
    return {
        "id": "react",
        "type": "react_agent",
        "goal": "목표",
        "allowed_tools": ["rag_search"],
        **kw,
    }


def _assistant_call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


def _script(monkeypatch: pytest.MonkeyPatch, calls: list[ToolCall]) -> None:
    """첫 턴에 `calls`를 요청하고, 다음 턴에 끝내는 모델."""
    turns: list[int] = []

    async def fake(db: Any, model_id: Any, messages: Any, tools: Any, **kw: Any) -> Any:
        turns.append(1)
        if len(turns) == 1 and calls:
            call = calls[0]
            return ToolTurnResult(
                tool_calls=calls,
                assistant_message=_assistant_call(call.id, call.name, call.arguments),
            )
        return _DONE

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake)


class Timeline:
    """감사 기록·이벤트 발행 순서를 한 줄로 기록한다."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[str] = []
        self.rows: list[dict[str, Any]] = []
        self.start_result: Any = "log-id"
        self.finish_result: Any = True

        async def start(run_id: Any, node_id: str, node_type: str, input_data: Any, at: Any) -> Any:
            self.events.append("audit.start")
            self.rows.append(dict(input_data))
            return self.start_result

        async def finish(log_id: Any, **kw: Any) -> Any:
            self.events.append("audit.finish")
            return self.finish_result

        monkeypatch.setattr(loop, "_log_node_start", start)
        monkeypatch.setattr(loop, "_log_node_finish", finish)

    async def on_tool_call(self, tool_call_id: str, name: str, status: str, args: Any) -> None:
        self.events.append(f"emit:{name}:{status}")


@pytest.fixture
def tl(monkeypatch: pytest.MonkeyPatch) -> Timeline:
    return Timeline(monkeypatch)


# ── 수락 기준 1·2: 감사 → 발행 순서 ─────────────────────────────────────


async def test_dispatch_emits_after_the_audit_row(
    monkeypatch: pytest.MonkeyPatch, tl: Timeline
) -> None:
    _script(monkeypatch, [ToolCall(id="c1", name="rag_search", arguments={"query": "q"})])
    ctx = _ctx(source_ids=[], on_tool_call=tl.on_tool_call)

    await loop.run_react_node(ctx, _node(), {})

    assert tl.events == ["audit.start", "audit.finish", "emit:rag_search:succeeded"]


async def test_subagent_cap_block_emits_after_the_audit_row(
    monkeypatch: pytest.MonkeyPatch, tl: Timeline
) -> None:
    _script(monkeypatch, [ToolCall(id="c2", name="call_agent", arguments={"agent": "x"})])
    ctx = _ctx(on_tool_call=tl.on_tool_call)
    ctx.react_subagent_calls[0] = loop._REACT_GLOBAL_SUBAGENT_CAP

    await loop.run_react_node(ctx, _node(allowed_tools=["call_agent"]), {})

    assert tl.events == ["audit.start", "audit.finish", "emit:call_agent:blocked"]


async def test_approval_gate_then_resume(monkeypatch: pytest.MonkeyPatch, tl: Timeline) -> None:
    """게이트 대기는 감사 없이 한 번 발행한다(수락 기준 2). 재개 결과는 감사 뒤에 발행한다."""
    args = {"url": "https://example.com"}
    _script(monkeypatch, [ToolCall(id="m1", name="http_request", arguments=args)])
    node = _node(allowed_tools=["http_request"])

    with pytest.raises(ApprovalRequired) as gate:
        await loop.run_react_node(_ctx(on_tool_call=tl.on_tool_call), node, {})
    assert tl.events == ["emit:http_request:awaiting_approval"]

    checkpoint = json.loads(json.dumps(gate.value.checkpoint))
    checkpoint[approval.CheckpointKeys.REACT_APPROVAL_STATUS] = "rejected"
    resumed = _ctx(on_tool_call=tl.on_tool_call)
    approval.restore_run_context(resumed, checkpoint)
    tl.events.clear()
    _script(monkeypatch, [])

    await loop.run_react_node(resumed, {**node, "_resume": checkpoint}, {})

    assert tl.events == ["audit.start", "audit.finish", "emit:http_request:blocked"]


# ── 수락 기준 3: 감사 실패 신호 ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("start", "finish", "stage"),
    [(None, True, "start"), ("log-id", False, "finish"), (None, False, "start")],
)
async def test_audit_failure_warns_once_and_still_emits(
    monkeypatch: pytest.MonkeyPatch, tl: Timeline, start: Any, finish: Any, stage: str
) -> None:
    tl.start_result, tl.finish_result = start, finish
    _script(monkeypatch, [ToolCall(id="c1", name="rag_search", arguments={"query": "q"})])
    ctx = _ctx(source_ids=[], on_tool_call=tl.on_tool_call)

    with structlog.testing.capture_logs() as logs:
        await loop.run_react_node(ctx, _node(), {})

    failed = [e for e in logs if e["event"] == "agent.tool_call.audit_failed"]
    assert [(e["stage"], e["tool_call_id"], e["tool"]) for e in failed] == [
        (stage, "c1", "rag_search")
    ]
    assert tl.events[-1] == "emit:rag_search:succeeded"  # 타임라인은 막지 않는다


async def test_finish_returning_none_is_unknown_not_failure(
    monkeypatch: pytest.MonkeyPatch, tl: Timeline
) -> None:
    """반환값 없는 finish 대역(기존 테스트들)은 실패로 보지 않는다."""
    tl.finish_result = None
    _script(monkeypatch, [ToolCall(id="c1", name="rag_search", arguments={"query": "q"})])

    with structlog.testing.capture_logs() as logs:
        await loop.run_react_node(_ctx(source_ids=[], on_tool_call=tl.on_tool_call), _node(), {})

    assert not [e for e in logs if e["event"] == "agent.tool_call.audit_failed"]


async def test_llm_node_path_is_not_an_audit_failure(
    monkeypatch: pytest.MonkeyPatch, tl: Timeline
) -> None:
    """guards 없는 llm 노드 경로는 감사 행을 쓰지 않는다 — 실패가 아니다."""
    from biznotes_core.services.agents.nodes import llm as llm_node

    _script(monkeypatch, [ToolCall(id="c1", name="rag_search", arguments={"query": "q"})])
    ctx = _ctx(source_ids=[], on_tool_call=tl.on_tool_call)

    with structlog.testing.capture_logs() as logs:
        await llm_node._exec_llm(
            ctx, {"id": "g", "type": "llm", "prompt": "p", "tools": ["rag_search"]}, {}
        )

    assert not [e for e in logs if e["event"] == "agent.tool_call.audit_failed"]
    assert tl.events == ["emit:rag_search:succeeded"]


# ── 수락 기준 4·6: 새 콜백과 required_initial ────────────────────────────


async def test_event_callback_marks_required_initial_calls(
    monkeypatch: pytest.MonkeyPatch, tl: Timeline
) -> None:
    seen: list[ToolCallEventInput] = []

    async def on_event(event: ToolCallEventInput) -> None:
        tl.events.append(f"event:{event.name}")
        seen.append(event)

    _script(monkeypatch, [ToolCall(id="m1", name="rag_search", arguments={"query": "모델"})])
    ctx = _ctx(source_ids=[], on_tool_call_event=on_event, chat_message="질문", trigger="chat")

    await loop.run_react_node(ctx, _node(required_initial_tools=["rag_search"]), {})

    assert [(e.required_initial, e.status) for e in seen] == [
        (True, "succeeded"),
        (None, "succeeded"),
    ]
    assert [bool(r.get("required_initial")) for r in tl.rows] == [True, False]
    # 감사 → 발행 순서는 새 콜백에서도 같다
    assert tl.events == ["audit.start", "audit.finish", "event:rag_search"] * 2
    # 모델이 부른 호출의 JSON은 예전 형식과 같다(키 없음)
    assert "required_initial" not in json.loads(seen[1].model_dump_json(exclude_none=True))


async def test_event_callback_takes_precedence_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, tl: Timeline
) -> None:
    seen: list[str] = []

    async def on_event(event: ToolCallEventInput) -> None:
        seen.append(event.tool_call_id)

    calls = [
        ToolCall(id="c1", name="rag_search", arguments={"query": "a"}),
        ToolCall(id="c2", name="rag_search", arguments={"query": "b"}),
    ]
    _script(monkeypatch, calls)
    ctx = _ctx(source_ids=[], on_tool_call=tl.on_tool_call, on_tool_call_event=on_event)

    with structlog.testing.capture_logs() as logs:
        await loop.run_react_node(ctx, _node(), {})

    assert seen == ["c1", "c2"]
    assert not [e for e in tl.events if e.startswith("emit:")]  # 이중 발행 없음
    assert len([e for e in logs if e["event"] == "agent.tool_call.dual_callbacks"]) == 1


async def test_legacy_callback_keeps_four_positional_arguments(
    monkeypatch: pytest.MonkeyPatch, tl: Timeline
) -> None:
    """수락 기준 5 — 새 콜백이 없으면 기존 4인자 콜백을 지금처럼 부른다(강제 호출도 같은 형태)."""
    received: list[tuple[Any, ...]] = []

    async def legacy(*args: Any, **kwargs: Any) -> None:
        assert not kwargs
        received.append(args)

    _script(monkeypatch, [])
    ctx = _ctx(source_ids=[], on_tool_call=legacy, chat_message="질문", trigger="chat")

    await loop.run_react_node(ctx, _node(required_initial_tools=["rag_search"]), {})

    assert len(received) == 1 and len(received[0]) == 4
    assert received[0][1:3] == ("rag_search", "succeeded")


# ── 수락 기준 7: 전파 ────────────────────────────────────────────────────


@pytest.mark.parametrize("scope", ["child", "branch"])
async def test_tool_calls_from_child_and_branch_contexts_reach_the_event_callback(
    monkeypatch: pytest.MonkeyPatch, tl: Timeline, scope: str
) -> None:
    """수락 기준 7 — 서브에이전트(`child()`)·병렬 분기 사본(`replace()`)에서 난 도구 호출도
    새 콜백으로 전달된다. 병렬 분기에서는 react_agent가 거부되므로 llm 노드 도구로 본다."""
    from biznotes_core.services.agents.nodes import llm as llm_node

    seen: list[str] = []

    async def on_event(event: ToolCallEventInput) -> None:
        seen.append(event.tool_call_id)

    _script(monkeypatch, [ToolCall(id="c1", name="rag_search", arguments={"query": "q"})])
    root = _ctx(source_ids=[], on_tool_call_event=on_event)
    ctx = root.child(uuid.uuid4(), {}) if scope == "child" else root.branch_parallel()
    node = {"id": "g", "type": "llm", "prompt": "p", "tools": ["rag_search"]}

    await llm_node._exec_llm(ctx, node, {})

    assert seen == ["c1"]


async def test_event_callback_propagates_to_children_and_branch_copies() -> None:
    async def on_event(event: ToolCallEventInput) -> None:
        return None

    ctx = _ctx(on_tool_call_event=on_event)

    assert ctx.child(uuid.uuid4(), {}).on_tool_call_event is on_event
    assert ctx.branch_parallel().on_tool_call_event is on_event


async def test_branch_warning_flag_is_shared_across_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """이중 설정 경고는 run당 한 번 — 자식 컨텍스트가 다시 경고하지 않는다."""

    async def cb(*a: Any) -> None:
        return None

    ctx = _ctx(on_tool_call=cb, on_tool_call_event=cb)
    child = ctx.child(uuid.uuid4(), {})

    with structlog.testing.capture_logs() as logs:
        await ctx.emit_tool_call("a", "rag_search", "succeeded", {})
        await child.emit_tool_call("b", "rag_search", "succeeded", {})

    assert len([e for e in logs if e["event"] == "agent.tool_call.dual_callbacks"]) == 1


# ── 수락 기준 8: 스키마 호환 ─────────────────────────────────────────────


def test_old_format_json_round_trips() -> None:
    old = {
        "type": "tool_call",
        "event_id": 3,
        "tool_call_id": "c1",
        "name": "rag_search",
        "status": "succeeded",
        "arguments": {},
    }

    event = RunEventAdapter.validate_json(json.dumps(old))

    assert isinstance(event, ToolCallEvent) and event.required_initial is None
    assert json.loads(event.model_dump_json(exclude_none=True)) == old


def test_required_initial_is_optional_on_input_and_snapshot() -> None:
    base = {"tool_call_id": "c1", "name": "rag_search", "status": "succeeded", "arguments": {}}

    assert ToolCallEventInput(type="tool_call", **base).required_initial is None
    assert ToolCallSnapshot(**base).required_initial is None
    assert ToolCallSnapshot(**base, required_initial=True).required_initial is True
