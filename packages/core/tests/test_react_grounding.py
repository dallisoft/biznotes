"""react 근거 — run 단위 인용 번호(#9)와 필수 초기 도구(#10).

모델(`complete_with_tools`)·검색(`retrieve_context`)·실행 로그만 가짜로 두고 react
루프와 도구 디스패처는 실제 코드를 탄다.
"""

import json
import uuid
from typing import Any

import pytest

from biznotes_core.services import rag_chat, settings_store
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError, ApprovalRequired
from biznotes_core.services.agents.nodes.llm import _dispatch_builtin_tool
from biznotes_core.services.agents.react import approval, loop
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import ToolTurnResult
from biznotes_core.services.rag_chat import Citation, RetrievalResult


def _ctx(**kw: Any) -> RunContext:
    return RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={}, **kw)  # type: ignore[arg-type]


def _hits(*chunks: str) -> RetrievalResult:
    """retrieve_context처럼 호출마다 [1]부터 번호를 매긴 결과."""
    return RetrievalResult(
        citations=[
            Citation(n=i, source_id="s", chunk_id=c, quote=f"본문 {c}", source_title="소스")
            for i, c in enumerate(chunks, start=1)
        ],
        insufficient_context=False,
    )


def _src(n: int, chunk: str) -> str:
    """모델이 보는 발췌 하나 — 비신뢰 데이터 블록 안에 `[n] (제목)` 머리가 있다(HWP-05)."""
    return f'<untrusted_data kind="source">\n[{n}] (소스)\n본문 {chunk}\n</untrusted_data>'


class _Queue(list):
    calls: list[dict[str, Any]]
    rag_settings: dict[str, Any]


@pytest.fixture
def search(monkeypatch):
    """rag_search가 부를 retrieve_context — 차례로 줄 결과를 넣고 받은 인자를 기록한다.

    `top_k` 기본값을 읽는 RAG 설정도 DB 대신 `queue.rag_settings`에서 준다.
    """
    queue = _Queue()
    calls: list[dict[str, Any]] = []
    queue.rag_settings = dict(settings_store.DEFAULTS["rag"])

    async def fake_setting(db, key):
        assert key == "rag"
        return queue.rag_settings

    async def fake(db, notebook_id, query, source_ids, top_k=6):
        calls.append({"query": query, "source_ids": source_ids, "top_k": top_k})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(rag_chat, "retrieve_context", fake)
    monkeypatch.setattr(settings_store, "get_setting", fake_setting)
    queue.calls = calls
    return queue


def _numbers(ctx: RunContext) -> dict[str, int]:
    return {e["citation"]["chunk_id"]: e["citation"]["n"] for e in ctx.tool_evidence}


# ── #9 run 단위 인용 번호 ──────────────────────────────────────────────


async def test_second_search_continues_numbering_and_reuses_seen_chunks(search):
    search.extend([_hits("a", "b"), _hits("b", "c")])
    ctx = _ctx()

    first = await _dispatch_builtin_tool(ctx, "rag_search", {"query": "q1"})
    second = await _dispatch_builtin_tool(ctx, "rag_search", {"query": "q2"})

    assert "[1] (소스)\n본문 a" in first and "[2] (소스)\n본문 b" in first
    # 이미 본 b는 [2] 그대로, 새 c는 [3] — 두 번째 검색에 [1]이 다시 나오지 않는다
    assert "[2] (소스)\n본문 b" in second and "[3] (소스)\n본문 c" in second
    assert "[1]" not in second
    # evidence의 citation.n이 run 전체에서 유일하고 모델이 본 번호와 같다
    assert _numbers(ctx) == {"a": 1, "b": 2, "c": 3}
    assert len(ctx.tool_evidence) == 3


async def test_duplicate_chunk_within_one_result_is_shown_once(search):
    search.append(_hits("a", "a", "b"))
    ctx = _ctx()

    out = await _dispatch_builtin_tool(ctx, "rag_search", {"query": "q"})

    assert out.count("본문 a") == 1
    assert _numbers(ctx) == {"a": 1, "b": 2}


async def test_subagent_shares_the_run_numbering(search):
    search.extend([_hits("a"), _hits("x", "a")])
    parent = _ctx()
    await _dispatch_builtin_tool(parent, "rag_search", {"query": "q"})

    child = parent.child(uuid.uuid4(), {})
    out = await _dispatch_builtin_tool(child, "rag_search", {"query": "q"})

    # 스킬 서브에이전트의 [n]도 부모가 모은 evidence와 같은 번호 체계다
    assert "[2] (소스)\n본문 x" in out and "[1] (소스)\n본문 a" in out
    assert _numbers(parent) == {"a": 1, "x": 2}


async def test_published_skill_rag_node_shares_the_run_numbering(search):
    from biznotes_core.services.agents.graph import NODE_EXECUTORS
    from biznotes_core.services.skills.builder import LoadedSkill, build_nodes

    search.extend([_hits("a"), _hits("x", "a")])
    parent = _ctx()
    await _dispatch_builtin_tool(parent, "rag_search", {"query": "q"})

    # run_skill → execute_skill_package는 react 도구가 아니라 워크플로우 rag_search 노드로 찾는다
    skill = LoadedSkill(metadata_name="s", metadata_description="d", instructions="i", manifest={})
    (search_node,) = [n for n in build_nodes(skill)[0] if n["type"] == "rag_search"]
    child = parent.child(uuid.uuid4(), {"topic": "t"})
    out = await NODE_EXECUTORS["rag_search"](child, search_node, {"params": {"topic": "t"}})

    # 스킬 LLM이 보는 컨텍스트와 부모가 모은 evidence가 같은 번호를 쓴다
    assert out["context"] == _src(2, "x") + "\n\n" + _src(1, "a")
    assert [c["n"] for c in out["citations"]] == [2, 1]
    assert _numbers(parent) == {"a": 1, "x": 2}


async def test_numbering_survives_hitl_resume(search):
    search.extend([_hits("a", "b"), _hits("c")])
    before = _ctx()
    await _dispatch_builtin_tool(before, "rag_search", {"query": "q"})

    # 승인 대기 → 새 워커가 체크포인트로 컨텍스트를 복원하는 경로
    checkpoint = {approval.CheckpointKeys.CTX_TOOL_EVIDENCE: before.tool_evidence}
    after = _ctx()
    approval.restore_run_context(after, json.loads(json.dumps(checkpoint)))
    out = await _dispatch_builtin_tool(after, "rag_search", {"query": "q"})

    assert "[3] (소스)\n본문 c" in out
    assert _numbers(after) == {"a": 1, "b": 2, "c": 3}


async def test_other_evidence_kinds_do_not_affect_numbering(search):
    search.append(_hits("a"))
    ctx = _ctx()
    ctx.tool_evidence.extend(
        [
            {"kind": "source", "source_id": "s", "title": "t", "mode": "full"},  # source_load
            {"kind": "mcp", "server": "m", "tool": "t", "status": "succeeded"},
        ]
    )

    out = await _dispatch_builtin_tool(ctx, "rag_search", {"query": "q"})

    assert out.startswith(_src(1, "a"))


async def test_empty_source_selection_skips_search(search):
    ctx = _ctx(source_ids=[])

    out = await _dispatch_builtin_tool(ctx, "rag_search", {"query": "q"})

    assert out == "선택된 소스가 없어 검색하지 않았습니다."
    assert search.calls == [] and ctx.tool_evidence == []


async def test_empty_source_selection_skips_the_workflow_rag_node():
    from biznotes_core.services.agents.nodes.retrieval import _exec_rag_search

    ctx = _ctx(source_ids=[])  # db=None — 검색 계층이 DB를 건드리면 실패한다

    out = await _exec_rag_search(ctx, {"query": "q"}, {})

    assert out == {"context": "", "citations": [], "insufficient_context": True}


# ── #10 required_initial_tools ─────────────────────────────────────────


@pytest.fixture
def model(monkeypatch):
    """도구를 부르지 않고 바로 답하는 모델. 매 턴 받은 messages를 기록한다."""
    turns: list[list[dict[str, Any]]] = []

    async def fake(db, model_id, messages, tools, **kw):
        turns.append([dict(m) for m in messages])
        return ToolTurnResult(
            content="답변 [1]",
            assistant_message={"role": "assistant", "content": "답변 [1]"},
            usage={"total_tokens": 10},
        )

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake)
    return turns


@pytest.fixture
def audit(monkeypatch):
    """react_tool_call 보조 로그 행 — 실제로는 격리 세션에 쓰는 best-effort 기록."""
    rows: list[dict[str, Any]] = []

    async def start(run_id, node_id, node_type, input_data, started_at):
        rows.append({"node_id": node_id, "node_type": node_type, **input_data})
        return None

    async def finish(log_id, **kw):
        return None

    monkeypatch.setattr(loop, "_log_node_start", start)
    monkeypatch.setattr(loop, "_log_node_finish", finish)
    return rows


def _node(**kw: Any) -> dict[str, Any]:
    return {
        "id": "react",
        "type": "react_agent",
        "goal": "다음 질문에 소스를 근거로 답하라: {chat_message}",
        "allowed_tools": ["rag_search", "source_load"],
        "required_initial_tools": ["rag_search"],
        **kw,
    }


async def test_required_rag_search_runs_before_the_first_model_turn(search, model, audit):
    search.append(_hits("a"))
    ctx = _ctx(trigger="chat", chat_message="agent의 어원은?", source_ids=[uuid.uuid4()])

    result = await loop.run_react_node(ctx, _node(), {"chat_message": "agent의 어원은?"})

    assert result["text"] == "답변 [1]"
    assert len(model) == 1  # 강제 호출은 모델 턴이 아니다
    system, user, assistant, tool = model[0]
    assert system["role"] == "system" and user["role"] == "user"
    # 발췌는 user 턴이 아니라 assistant tool_calls에 짝지은 tool 역할 메시지로 들어간다
    (call,) = assistant["tool_calls"]
    assert assistant["role"] == "assistant" and call["function"]["name"] == "rag_search"
    assert json.loads(call["function"]["arguments"]) == {"query": "agent의 어원은?"}
    # name은 없다 — OpenAI tool 메시지의 허용 필드가 아니다(#29)
    assert tool == {
        "role": "tool",
        "tool_call_id": call["id"],
        "content": _src(1, "a"),
    }
    assert "본문 a" not in user["content"]
    # 검색어는 goal 템플릿이 아니라 발화 원문이고, 소스 스코프는 run의 것이다
    assert search.calls[0]["query"] == "agent의 어원은?"
    assert search.calls[0]["source_ids"] == ctx.source_ids
    # 모델이 부른 도구와 같은 관측 경로: SSE 타임라인·tool_log·evidence·감사 행
    assert [(c["tool"], c["status"]) for c in ctx.tool_calls] == [("rag_search", "succeeded")]
    assert [t["tool"] for t in result["tool_calls"]] == ["rag_search"]
    assert _numbers(ctx) == {"a": 1}
    assert audit == [
        {
            "node_id": "react:tool:0",
            "node_type": "react_tool_call",
            "tool_call_id": call["id"],
            "tool": "rag_search",
            "arguments": {"query": "agent의 어원은?"},
            "call_status": "succeeded",
            "side_effect": False,
            "required_initial": True,
        }
    ]
    assert ctx.tool_calls[0]["required_initial"] is True


async def test_required_search_uses_the_admin_top_k(search, model, audit):
    """#24 — 강제 호출은 query만 넘기므로 발췌 수는 어드민 RAG 설정(rag.top_k)을 따른다."""
    search.rag_settings["top_k"] = 10
    search.append(_hits("a"))
    ctx = _ctx(trigger="chat", chat_message="q", source_ids=[uuid.uuid4()])

    await loop.run_react_node(ctx, _node(), {"chat_message": "q"})

    assert search.calls[0]["top_k"] == 10


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"top_k": 3}, 3),  # 모델이 명시하면 설정보다 우선한다
        ({}, 10),  # 모델이 생략해도 설정을 따른다
        ({"top_k": None}, 10),
        ({"top_k": 0}, 1),  # 어드민 설정 범위(1~30)로 제한한다
        ({"top_k": -2}, 1),
        ({"top_k": 500}, 30),
    ],
)
async def test_model_top_k_falls_back_to_the_setting_and_is_clamped(search, args, expected):
    search.rag_settings["top_k"] = 10
    search.append(_hits("a"))

    await _dispatch_builtin_tool(_ctx(), "rag_search", {"query": "q", **args})

    assert search.calls[0]["top_k"] == expected


async def test_model_search_after_forced_search_continues_numbering(search, monkeypatch, audit):
    search.extend([_hits("a", "b"), _hits("c", "a")])
    turns: list[list[dict[str, Any]]] = []

    async def model(db, model_id, messages, tools, **kw):
        turns.append(list(messages))
        if len(turns) == 1:
            call = gateway_llm.ToolCall(id="m1", name="rag_search", arguments={"query": "더"})
            return ToolTurnResult(
                tool_calls=[call],
                assistant_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "m1",
                            "type": "function",
                            "function": {"name": "rag_search", "arguments": '{"query": "더"}'},
                        }
                    ],
                },
            )
        done = {"role": "assistant", "content": "끝"}
        return ToolTurnResult(content="끝", assistant_message=done)

    monkeypatch.setattr(gateway_llm, "complete_with_tools", model)
    ctx = _ctx(trigger="chat", chat_message="질문")

    await loop.run_react_node(ctx, _node(), {})

    assert turns[1][-1]["content"] == _src(3, "c") + "\n\n" + _src(1, "a")
    assert _numbers(ctx) == {"a": 1, "b": 2, "c": 3}


async def test_non_chat_run_searches_with_the_rendered_goal(search, model, audit):
    search.append(_hits("a"))
    ctx = _ctx()

    await loop.run_react_node(ctx, _node(goal="{params.topic} 요약"), {"params": {"topic": "RAG"}})

    assert search.calls[0]["query"] == "RAG 요약"


async def test_failed_required_tool_fails_the_node_without_answering(search, model, audit):
    search.append(RuntimeError("vector search down"))
    ctx = _ctx(trigger="chat", chat_message="질문")

    with pytest.raises(AgentRuntimeError, match="필수 초기 도구 'rag_search'"):
        await loop.run_react_node(ctx, _node(), {})

    assert model == []
    assert [(c["tool"], c["status"]) for c in ctx.tool_calls] == [("rag_search", "failed")]
    assert audit[0]["call_status"] == "failed"


async def test_empty_selection_is_not_a_failure(search, model, audit):
    ctx = _ctx(trigger="chat", chat_message="질문", source_ids=[])

    await loop.run_react_node(ctx, _node(), {})

    assert model[0][-1]["content"] == "선택된 소스가 없어 검색하지 않았습니다."
    assert search.calls == []


@pytest.mark.parametrize(
    ("node", "match"),
    [
        (_node(allowed_tools=["source_load"]), "노출되지 않았습니다"),
        (_node(required_initial_tools=["web_search"]), "쓸 수 없는 도구"),
        (_node(required_initial_tools=["call_agent"]), "쓸 수 없는 도구"),
        (_node(required_initial_tools="rag_search"), "문자열 리스트"),
    ],
)
async def test_invalid_required_tools_fail_before_any_work(search, model, audit, node, match):
    ctx = _ctx(trigger="chat", chat_message="질문")

    with pytest.raises(AgentRuntimeError, match=match):
        await loop.run_react_node(ctx, node, {})

    assert model == [] and search.calls == [] and ctx.tool_calls == []


async def test_without_the_option_the_model_decides(search, model, audit):
    ctx = _ctx(trigger="chat", chat_message="질문")
    node = _node()
    del node["required_initial_tools"]

    await loop.run_react_node(ctx, node, {})

    assert [m["role"] for m in model[0]] == ["system", "user"]
    assert search.calls == []


async def test_nested_react_in_a_chat_run_searches_with_its_own_goal(search, model, audit):
    search.append(_hits("a"))
    parent = _ctx(trigger="chat", chat_message="사용자 질문")
    child = parent.child(uuid.uuid4(), {})

    await loop.run_react_node(child, _node(goal="하위 목표"), {})

    assert search.calls[0]["query"] == "하위 목표"


async def test_hitl_resume_after_forced_search_does_not_search_again(search, monkeypatch, audit):
    search.append(_hits("a"))
    turns: list[list[dict[str, Any]]] = []

    async def model(db, model_id, messages, tools, **kw):
        turns.append(list(messages))
        if len(turns) == 1:  # 강제 검색 뒤 첫 턴: 승인이 필요한 도구를 요청한다
            args = {"url": "https://example.com"}
            call = gateway_llm.ToolCall(id="m1", name="http_request", arguments=args)
            return ToolTurnResult(
                tool_calls=[call],
                assistant_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "m1",
                            "type": "function",
                            "function": {"name": "http_request", "arguments": json.dumps(args)},
                        }
                    ],
                },
            )
        done = {"role": "assistant", "content": "끝 [1]"}
        return ToolTurnResult(content="끝 [1]", assistant_message=done)

    monkeypatch.setattr(gateway_llm, "complete_with_tools", model)
    node = _node(allowed_tools=["rag_search", "http_request"])
    ctx = _ctx(trigger="chat", chat_message="질문")

    with pytest.raises(ApprovalRequired) as gate:
        await loop.run_react_node(ctx, node, {})

    # 체크포인트는 JSONB로 저장됐다가 새 워커에서 복원된다
    checkpoint = json.loads(json.dumps(gate.value.checkpoint))
    checkpoint[approval.CheckpointKeys.REACT_APPROVAL_STATUS] = "rejected"
    resumed = _ctx(trigger="chat", chat_message="질문")
    approval.restore_run_context(resumed, checkpoint)
    result = await loop.run_react_node(resumed, {**node, "_resume": checkpoint}, {})

    assert result["text"] == "끝 [1]"
    assert len(search.calls) == 1  # 재개가 강제 검색을 다시 돌리지 않는다
    roles = [m["role"] for m in turns[-1]]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool"]
    assert turns[-1][3]["content"] == _src(1, "a")
    assert _numbers(resumed) == {"a": 1}
