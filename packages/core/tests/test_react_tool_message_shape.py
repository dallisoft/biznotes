"""react가 쌓는 tool 역할 메시지의 필드 계약 (issue #29).

OpenAI `ChatCompletionToolMessageParam`은 `role`·`tool_call_id`·`content`만 받는다.
`name`을 넣으면 엄격히 검증하는 OpenAI 호환 게이트웨이가 메시지 하나가 아니라
**요청 전체**를 400으로 거절해서, 자율 채팅이 첫 도구 호출 직후 failed로 끝난다.

loop.py에는 tool 메시지를 만드는 자리가 세 군데 있으므로(일반 디스패치 ·
서브에이전트 상한 · 승인 복귀) 세 경로를 모두 태워서 확인한다.
"""

import json
import uuid
from typing import Any

import pytest

from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import ApprovalRequired
from biznotes_core.services.agents.react import approval, loop
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import ToolTurnResult

TOOL_MESSAGE_KEYS = {"role", "tool_call_id", "content"}


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


def _tool_messages(turns: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [m for turn in turns for m in turn if m.get("role") == "tool"]


def _assert_tool_message_shape(messages: list[dict[str, Any]]) -> None:
    assert messages, "검사할 tool 메시지가 없다 — 경로를 타지 못했다"
    for m in messages:
        extra = sorted(set(m) - TOOL_MESSAGE_KEYS)
        assert not extra, f"허용되지 않는 필드: {extra}"
        assert set(m) == TOOL_MESSAGE_KEYS


@pytest.fixture
def audit(monkeypatch):
    """보조 로그 행 — 실제로는 격리 세션에 쓰는 best-effort 기록."""

    async def start(run_id, node_id, node_type, input_data, started_at):
        return None

    async def finish(log_id, **kw):
        return None

    monkeypatch.setattr(loop, "_log_node_start", start)
    monkeypatch.setattr(loop, "_log_node_finish", finish)


def _scripted_model(monkeypatch, scripts: list[Any]) -> list[list[dict[str, Any]]]:
    """턴마다 scripts를 하나씩 소비하는 모델. 매 턴 받은 messages를 기록한다."""
    turns: list[list[dict[str, Any]]] = []

    async def fake(db, model_id, messages, tools, **kw):
        turns.append([dict(m) for m in messages])
        item = scripts[min(len(turns) - 1, len(scripts) - 1)]
        return item() if callable(item) else item

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake)
    return turns


_DONE = ToolTurnResult(
    content="끝",
    assistant_message={"role": "assistant", "content": "끝"},
    usage={"total_tokens": 10},
)


async def test_dispatch_path_tool_message_has_no_name(monkeypatch, audit):
    """일반 디스패치 — 모델이 부른 도구의 결과 메시지."""

    call = gateway_llm.ToolCall(id="c1", name="rag_search", arguments={"query": "q"})
    turns = _scripted_model(
        monkeypatch,
        [
            ToolTurnResult(
                tool_calls=[call],
                assistant_message=_assistant_call("c1", "rag_search", {"query": "q"}),
            ),
            _DONE,
        ],
    )
    ctx = _ctx(source_ids=[])  # 선택된 소스가 없으면 검색 없이 빈 결과를 돌려준다

    await loop.run_react_node(ctx, _node(), {})

    _assert_tool_message_shape(_tool_messages(turns))


async def test_subagent_cap_path_tool_message_has_no_name(monkeypatch, audit):
    """서브에이전트 전역 상한에 걸려 차단된 call_agent의 거부 메시지."""
    call = gateway_llm.ToolCall(id="c2", name="call_agent", arguments={"agent": "x"})
    turns = _scripted_model(
        monkeypatch,
        [
            ToolTurnResult(
                tool_calls=[call],
                assistant_message=_assistant_call("c2", "call_agent", {"agent": "x"}),
            ),
            _DONE,
        ],
    )
    ctx = _ctx()
    ctx.react_subagent_calls[0] = loop._REACT_GLOBAL_SUBAGENT_CAP  # 이미 상한 도달

    await loop.run_react_node(ctx, _node(allowed_tools=["call_agent"]), {})

    blocked = _tool_messages(turns)
    _assert_tool_message_shape(blocked)
    assert blocked[0]["content"] == loop._SUBAGENT_CAP_NOTE


async def test_approval_resume_path_tool_message_has_no_name(monkeypatch, audit):
    """승인 대기 후 재개 — 새 워커가 체크포인트에서 이어 붙이는 결과 메시지."""
    args = {"url": "https://example.com"}
    call = gateway_llm.ToolCall(id="m1", name="http_request", arguments=args)
    turns = _scripted_model(
        monkeypatch,
        [
            ToolTurnResult(
                tool_calls=[call],
                assistant_message=_assistant_call("m1", "http_request", args),
            ),
            _DONE,
        ],
    )
    node = _node(allowed_tools=["http_request"])

    with pytest.raises(ApprovalRequired) as gate:
        await loop.run_react_node(_ctx(), node, {})

    # 체크포인트는 JSONB로 저장됐다가 새 워커에서 복원된다
    checkpoint = json.loads(json.dumps(gate.value.checkpoint))
    checkpoint[approval.CheckpointKeys.REACT_APPROVAL_STATUS] = "rejected"
    resumed = _ctx()
    approval.restore_run_context(resumed, checkpoint)

    await loop.run_react_node(resumed, {**node, "_resume": checkpoint}, {})

    _assert_tool_message_shape(_tool_messages(turns[1:]))
