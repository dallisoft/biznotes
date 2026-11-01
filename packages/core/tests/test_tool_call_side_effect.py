"""`tool_call`·`approval_required` 이벤트의 `side_effect` (#154 A).

판정은 승인 게이트와 같은 `tool_registry.is_side_effect_tool`이다. 이벤트와 재연결 snapshot의
정본(감사 행 `input_data`)이 같은 값을 싣는지, 필드가 없는 구 페이로드가 통과하는지 확인한다.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from biznotes_core.schemas.run_events import (
    ApprovalRequiredEventInput,
    PendingApprovalSnapshot,
    RunEventAdapter,
    ToolCallEvent,
    ToolCallEventInput,
    ToolCallSnapshot,
)
from biznotes_core.services.agents import tool_registry
from biznotes_core.services.agents.errors import ApprovalRequired
from biznotes_core.services.agents.react import approval, loop
from biznotes_core.services.gateway.llm import ToolCall
from tests.test_tool_call_observation import Timeline, _ctx, _node, _script


@pytest.fixture
def tl(monkeypatch: pytest.MonkeyPatch) -> Timeline:
    return Timeline(monkeypatch)


class Events:
    def __init__(self) -> None:
        self.seen: list[ToolCallEventInput] = []

    async def __call__(self, event: ToolCallEventInput) -> None:
        self.seen.append(event)


async def test_read_tool_is_marked_false(monkeypatch: pytest.MonkeyPatch, tl: Timeline) -> None:
    """수락 기준 1 — 읽기 도구는 `side_effect=false`, 이벤트와 감사 행이 같다."""
    events = Events()
    _script(monkeypatch, [ToolCall(id="c1", name="rag_search", arguments={"query": "q"})])

    await loop.run_react_node(_ctx(source_ids=[], on_tool_call_event=events), _node(), {})

    assert [(e.name, e.side_effect) for e in events.seen] == [("rag_search", False)]
    assert [r["side_effect"] for r in tl.rows] == [False]
    assert json.loads(events.seen[0].model_dump_json(exclude_none=True))["side_effect"] is False


async def test_side_effect_tool_is_marked_true_while_waiting_and_after_resume(
    monkeypatch: pytest.MonkeyPatch, tl: Timeline
) -> None:
    """수락 기준 1 — 부작용 도구는 승인 대기 이벤트와 재개 결과 모두 `side_effect=true`."""
    events = Events()
    _script(monkeypatch, [ToolCall(id="m1", name="http_request", arguments={"url": "https://x"})])
    node = _node(allowed_tools=["http_request"])

    with pytest.raises(ApprovalRequired) as gate:
        await loop.run_react_node(_ctx(on_tool_call_event=events), node, {})
    checkpoint = json.loads(json.dumps(gate.value.checkpoint))
    # 승인 대기는 게이트를 통과한 도구다 — 제품이 approval_required에 싣는 값과 같다
    assert tool_registry.is_side_effect_tool(checkpoint[approval.CheckpointKeys.TOOL_NAME])

    checkpoint[approval.CheckpointKeys.REACT_APPROVAL_STATUS] = "rejected"
    resumed = _ctx(on_tool_call_event=events)
    approval.restore_run_context(resumed, checkpoint)
    _script(monkeypatch, [])
    await loop.run_react_node(resumed, {**node, "_resume": checkpoint}, {})

    assert [(e.status, e.side_effect) for e in events.seen] == [
        ("awaiting_approval", True),
        ("blocked", True),
    ]
    assert [r["side_effect"] for r in tl.rows] == [True]


async def test_approved_resume_and_blocked_and_unknown_calls(
    monkeypatch: pytest.MonkeyPatch, tl: Timeline
) -> None:
    """승인 후 재개 결과는 게이트를 통과한 호출이라 True로 고정한다.

    상한 초과 `call_agent`(blocked)와 노출하지 않은 이름(알 수 없는 도구, failed)도 True다."""
    events = Events()
    _script(monkeypatch, [ToolCall(id="m1", name="http_request", arguments={"url": "https://x"})])
    node = _node(allowed_tools=["http_request"])
    with pytest.raises(ApprovalRequired) as gate:
        await loop.run_react_node(_ctx(on_tool_call_event=events), node, {})
    checkpoint = json.loads(json.dumps(gate.value.checkpoint))
    assert checkpoint[approval.CheckpointKeys.SIDE_EFFECT] is True
    checkpoint[approval.CheckpointKeys.REACT_APPROVAL_STATUS] = "approved"

    async def fake_dispatch(*_a: Any, **_k: Any) -> str:
        return "ok"

    from biznotes_core.services.agents import tool_registry as registry

    original_build = registry.build_tool_specs

    async def build(ctx: Any, allowed: Any, **kw: Any) -> Any:
        specs, dispatchers = await original_build(ctx, allowed, **kw)
        return specs, {**dispatchers, "http_request": fake_dispatch}

    monkeypatch.setattr(registry, "build_tool_specs", build)
    # 재개 시점에 판정이 바뀌어도(프로세스 상태 차이) 기록은 True다
    monkeypatch.setattr(registry, "is_side_effect_tool", lambda name: False)
    resumed = _ctx(on_tool_call_event=events)
    approval.restore_run_context(resumed, checkpoint)
    _script(monkeypatch, [])
    await loop.run_react_node(resumed, {**node, "_resume": checkpoint}, {})
    assert [(e.status, e.side_effect) for e in events.seen][-1] == ("succeeded", True)
    assert tl.rows[-1]["side_effect"] is True
    monkeypatch.undo()

    # call_agent 상한 초과(blocked)
    events.seen.clear()
    tl2 = Timeline(monkeypatch)
    _script(monkeypatch, [ToolCall(id="c2", name="call_agent", arguments={"agent": "x"})])
    ctx = _ctx(on_tool_call_event=events)
    ctx.react_subagent_calls[0] = loop._REACT_GLOBAL_SUBAGENT_CAP
    await loop.run_react_node(ctx, _node(allowed_tools=["call_agent"]), {})
    assert [(e.status, e.side_effect) for e in events.seen] == [("blocked", True)]
    assert tl2.rows[-1]["side_effect"] is True

    # 노출하지 않은 이름 — 알 수 없는 도구로 실패하고 부작용으로 표시한다(보수적 기본값)
    events.seen.clear()
    _script(monkeypatch, [ToolCall(id="c3", name="not_exposed", arguments={})])
    await loop.run_react_node(_ctx(source_ids=[], on_tool_call_event=events), _node(), {})
    assert [(e.status, e.side_effect) for e in events.seen] == [("failed", True)]


async def test_llm_node_read_tool_is_false(monkeypatch: pytest.MonkeyPatch, tl: Timeline) -> None:
    """guards 없는 llm 노드 경로도 `tool_call`을 낸다 — 읽기 도구는 False다(감사 행은 없다)."""
    from biznotes_core.services.agents.nodes import llm as llm_node

    events = Events()
    _script(monkeypatch, [ToolCall(id="c1", name="rag_search", arguments={"query": "q"})])
    await llm_node._exec_llm(
        _ctx(source_ids=[], on_tool_call_event=events),
        {"id": "g", "type": "llm", "prompt": "p", "tools": ["rag_search"]},
        {},
    )
    assert [(e.name, e.side_effect) for e in events.seen] == [("rag_search", False)]
    assert tl.rows == []


async def test_emit_tool_call_default_argument_is_compatible() -> None:
    """`emit_tool_call`의 새 인자는 키워드 전용·기본 None — 기존 호출은 게이트 판정으로 채운다."""
    events = Events()
    ctx = _ctx(on_tool_call_event=events)
    await ctx.emit_tool_call("c", "http_request", "succeeded", {})
    await ctx.emit_tool_call("c", "http_request", "succeeded", {}, side_effect=False)
    assert [e.side_effect for e in events.seen] == [True, False]


async def test_event_carries_the_gate_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """이벤트 값은 게이트 판정(`is_side_effect_tool`) 그대로다 — 동적 MCP·스킬 도구도 같은 경로다.

    MCP 승인 도구 규칙 자체는 `test_tool_registry_public_helpers.py`(#152)가 잠근다.
    """
    verdict = {"mcp_1234abcd_search": False, "run_skill_script": True, "run_skill": False}
    monkeypatch.setattr(tool_registry, "is_side_effect_tool", lambda name: verdict[name])
    events = Events()
    ctx = _ctx(on_tool_call_event=events)
    for name in verdict:
        await ctx.emit_tool_call("c", name, "succeeded", {})
    assert [e.side_effect for e in events.seen] == [False, True, False]
    # 실제 규칙으로도 내장 도구 분류와 같다
    monkeypatch.undo()
    assert tool_registry.is_side_effect_tool("run_skill_script") is True
    assert tool_registry.is_side_effect_tool("run_skill") is False
    assert tool_registry.is_side_effect_tool("mcp_1234abcd_unapproved") is True


def test_payloads_without_the_field_still_validate() -> None:
    """수락 기준 1 — 필드가 없는 구 페이로드는 새 스키마를 통과하고 다시 직렬화해도 같다."""
    old_call = {
        "type": "tool_call",
        "event_id": 3,
        "tool_call_id": "c1",
        "name": "rag_search",
        "status": "succeeded",
        "arguments": {},
    }
    event = RunEventAdapter.validate_json(json.dumps(old_call))
    assert isinstance(event, ToolCallEvent) and event.side_effect is None
    assert json.loads(event.model_dump_json(exclude_none=True)) == old_call

    approval_id = uuid.uuid4()
    expires = datetime(2026, 10, 8, tzinfo=UTC)
    old_approval = {
        "type": "approval_required",
        "event_id": 4,
        "approval_id": str(approval_id),
        "tool": "http_request",
        "arguments_summary": {},
        "expires_at": expires.isoformat(),
    }
    assert RunEventAdapter.validate_json(json.dumps(old_approval)).side_effect is None
    assert ApprovalRequiredEventInput(
        type="approval_required",
        approval_id=approval_id,
        tool="http_request",
        arguments_summary={},
        expires_at=expires,
        side_effect=True,
    ).side_effect


def test_snapshots_accept_the_field() -> None:
    base: dict[str, Any] = {
        "tool_call_id": "c1",
        "name": "rag_search",
        "status": "succeeded",
        "arguments": {},
    }
    assert ToolCallSnapshot(**base).side_effect is None
    assert ToolCallSnapshot(**base, side_effect=False).side_effect is False
    pending = PendingApprovalSnapshot(
        approval_id=uuid.uuid4(),
        tool="http_request",
        arguments_summary={},
        status="pending",
        expires_at=datetime(2026, 10, 8, tzinfo=UTC),
        side_effect=True,
    )
    assert pending.side_effect is True
