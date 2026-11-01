"""자율 채팅의 1회 토큰·비용 한도 초과를 실제 오류로 알리고, 「사용 제한」 값을 채팅에 쓴다.

stg에서 `source_load`가 원문을 읽어 질문 1건이 108,629토큰을 쓰자, 코어 react 루프가 토큰 한도
(기본 100,000)에 걸려 「(예산 한도에 도달하여 부분 결과로 마무리했습니다.)」만 답변 자리에 넣었다.
사용자는 계정 예산이 남았는데도 그 문구를 답변으로 받았다. 모델 본문 없이 한도에 걸리면 답변을
저장하지 않고 SSE `error`로 어느 한도를 얼마나 넘었는지 알린다.

관리자가 「사용 제한」에서 정한 비용 한도·도구 반복·출력 토큰·이력 길이·source_load 상한(#228)이
다음 질문의 모델 호출과 도구에 실제로 닿는지도 본다.

LLM 경계(`complete_with_tools`·`complete_stream_with_fallback`)만 스텁한다 — 예산 판정·중단·
도구 실행은 코어 루프가 실제로 돈다.
"""

import inspect
import json
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from biznotes_core.models import ChatMessage
from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult
from biznotes_core.services.rag_chat import RetrievalResult
from sqlalchemy import func, select

from tests.conftest import bearer, login


@pytest.fixture
def recorded(monkeypatch) -> list[tuple[uuid.UUID, str, int]]:
    calls: list[tuple[uuid.UUID, str, int]] = []

    async def fake_record(user_id, kind, amount):
        calls.append((user_id, kind, amount))

    monkeypatch.setattr("biznotes_core.services.usage_guard.record_usage", fake_record)
    return calls


@pytest.fixture(autouse=True)
def _empty_retrieval(monkeypatch) -> None:
    async def fake_retrieve(db, notebook_id, query, source_ids=None, top_k=6):
        return RetrievalResult(citations=[], insufficient_context=True)

    monkeypatch.setattr("biznotes_core.services.rag_chat.retrieve_context", fake_retrieve)


def _usage(prompt: int, completion: int = 0) -> dict[str, int]:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _tool_turn(usage: dict[str, int], model: Any = None) -> ToolTurnResult:
    call = ToolCall(id="rag-1", name="rag_search", arguments={"query": "리포트"})
    return ToolTurnResult(
        content="",
        tool_calls=[call],
        assistant_message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "rag-1",
                    "type": "function",
                    "function": {"name": "rag_search", "arguments": json.dumps(call.arguments)},
                }
            ],
        },
        usage=usage,
        model=model,
    )


def _final_turn(text: str, usage: dict[str, int]) -> ToolTurnResult:
    return ToolTurnResult(
        content=text,
        tool_calls=[],
        assistant_message={"role": "assistant", "content": text},
        usage=usage,
        model=None,
    )


def _stub_llm(monkeypatch, turns: list[ToolTurnResult]) -> dict[str, Any]:
    """도구 턴 경계 — 호출마다 받은 메시지·도구·인자를 `requests`에 남긴다."""
    calls: dict[str, Any] = {"n": 0, "requests": []}

    async def fake_complete(db_, model_id, messages, tools, **kwargs):
        calls["requests"].append(
            {"messages": [dict(m) for m in messages], "tools": list(tools), "kwargs": kwargs}
        )
        turn = turns[calls["n"]]
        calls["n"] += 1
        return turn

    monkeypatch.setattr("biznotes_core.services.gateway.llm.complete_with_tools", fake_complete)
    return calls


async def _session(client: httpx.AsyncClient, token: str) -> str:
    nb = await client.post("/api/v1/notebooks", json={"title": "limit-nb"}, headers=bearer(token))
    sess = await client.post(
        f"/api/v1/notebooks/{nb.json()['id']}/chat/sessions", headers=bearer(token)
    )
    return sess.json()["id"]


async def _ask(
    client: httpx.AsyncClient, token: str, session_id: str, *, autonomous: bool = True
) -> list[dict]:
    res = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "리포트를 표로 정리해줘", "autonomous": autonomous},
        headers=bearer(token),
    )
    assert res.status_code == 200
    return [json.loads(line[6:]) for line in res.text.splitlines() if line.startswith("data: ")]


async def _message_count(db, session_id: str) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(ChatMessage)
            .where(ChatMessage.session_id == uuid.UUID(session_id))
        )
    ).scalar_one()


async def test_token_limit_without_answer_is_reported_as_error(
    client, db, user_a, mock_llm, recorded, monkeypatch
):
    """모델 본문 없이 1회 토큰 한도에 걸리면 답변 대신 오류를 보낸다.

    답변은 저장하지 않고, 쓴 토큰은 일일 사용량에 넣는다.
    """
    token = await login(client, user_a.email)
    session_id = await _session(client, token)
    _stub_llm(monkeypatch, [_tool_turn(_usage(108_087, 542))])

    events = await _ask(client, token, session_id)

    types = [e["type"] for e in events]
    assert "delta" not in types and "done" not in types, events
    assert types[-1] == "error"
    detail = events[-1]["detail"]
    assert "1회 토큰 한도(100,000)" in detail
    assert "108,629토큰" in detail
    assert "예산" not in detail
    assert await _message_count(db, session_id) == 0
    assert recorded == [(user_a.id, "tokens", 108_629)]


async def test_cost_limit_without_answer_names_cost_limit(
    client, db, user_a, mock_llm, recorded, monkeypatch
):
    """토큰은 남았는데 비용 한도($0.50)에 걸리면 비용 한도로 알린다."""
    token = await login(client, user_a.email)
    session_id = await _session(client, token)
    priced = SimpleNamespace(
        model=SimpleNamespace(
            id=uuid.uuid4(), display_name="Priced", cost_per_1k={"input": 10.0, "output": 10.0}
        )
    )
    _stub_llm(monkeypatch, [_tool_turn(_usage(100), model=priced)])  # $1.00

    events = await _ask(client, token, session_id)

    assert events[-1]["type"] == "error"
    detail = events[-1]["detail"]
    assert "1회 비용 한도($0.50)" in detail
    assert "$1.00" in detail
    assert await _message_count(db, session_id) == 0


async def test_answer_text_over_limit_is_still_delivered(
    client, db, user_a, mock_llm, recorded, monkeypatch
):
    """한도를 넘긴 턴이 본문을 냈으면 그 본문은 답변이다 — 오류로 바꾸지 않는다."""
    token = await login(client, user_a.email)
    session_id = await _session(client, token)
    _stub_llm(monkeypatch, [_final_turn("요약 본문입니다.", _usage(120_000, 500))])

    events = await _ask(client, token, session_id)

    assert [e["type"] for e in events][-1] == "done"
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "요약 본문입니다."
    assert await _message_count(db, session_id) == 2


async def test_admin_token_limit_applies_to_chat(
    client, db, user_a, admin_user, mock_llm, recorded, monkeypatch
):
    """관리자가 정한 1회 토큰 한도가 채팅에 쓰인다 — 올리면 기본값(100,000)을 넘는 질문도 답한다."""
    admin_token = await login(client, admin_user.email)
    res = await client.put(
        "/api/v1/admin/limits", json={"chat_token_budget": 150_000}, headers=bearer(admin_token)
    )
    assert res.status_code == 200, res.text
    token = await login(client, user_a.email)
    session_id = await _session(client, token)
    calls = _stub_llm(
        monkeypatch,
        [_tool_turn(_usage(110_000)), _final_turn("표입니다.", _usage(30_000, 1_000))],
    )

    events = await _ask(client, token, session_id)

    assert calls["n"] == 2
    assert events[-1]["type"] == "done", events
    assert recorded == [(user_a.id, "tokens", 141_000)]


async def test_lowered_admin_token_limit_is_named_in_error(
    client, db, user_a, admin_user, mock_llm, recorded, monkeypatch
):
    """한도를 낮추면 오류 문구도 그 값을 말한다."""
    admin_token = await login(client, admin_user.email)
    res = await client.put(
        "/api/v1/admin/limits", json={"chat_token_budget": 20_000}, headers=bearer(admin_token)
    )
    assert res.status_code == 200, res.text
    token = await login(client, user_a.email)
    session_id = await _session(client, token)
    calls = _stub_llm(monkeypatch, [_tool_turn(_usage(15_000)), _tool_turn(_usage(15_000))])

    events = await _ask(client, token, session_id)

    assert calls["n"] == 2
    assert events[-1]["type"] == "error"
    assert "1회 토큰 한도(20,000)" in events[-1]["detail"]
    assert "30,000토큰" in events[-1]["detail"]


# ── 「사용 제한」 값이 채팅에 닿는다(#228) ─────────────────────────────────────


async def _put_limits(client: httpx.AsyncClient, admin_user, **values: Any) -> None:
    admin_token = await login(client, admin_user.email)
    res = await client.put("/api/v1/admin/limits", json=values, headers=bearer(admin_token))
    assert res.status_code == 200, res.text


async def _notebook_session(client: httpx.AsyncClient, token: str) -> tuple[str, str]:
    nb = await client.post("/api/v1/notebooks", json={"title": "limit-nb"}, headers=bearer(token))
    nb_id = nb.json()["id"]
    sess = await client.post(f"/api/v1/notebooks/{nb_id}/chat/sessions", headers=bearer(token))
    return nb_id, sess.json()["id"]


async def _add_source(db, notebook_id: str, title: str, text: str) -> None:
    """파싱본 없이 청크만 둔 소스 — 코어 source_load는 청크를 이어 원문으로 읽는다."""
    from biznotes_core.models import Source, SourceChunk

    source = Source(notebook_id=uuid.UUID(notebook_id), type="text", title=title, status="ready")
    db.add(source)
    await db.flush()
    db.add(SourceChunk(source_id=source.id, seq=0, text=text))
    await db.commit()


async def _seed_history(db, session_id: str, turns: int) -> list[str]:
    """`turns`쌍의 이전 대화를 저장하고 내용을 순서대로 돌려준다."""
    contents: list[str] = []
    for i in range(1, turns + 1):
        contents += [f"이전 질문 Q{i}", f"이전 답변 A{i}"]
    sid = uuid.UUID(session_id)
    for n, content in enumerate(contents):
        role = "user" if n % 2 == 0 else "assistant"
        db.add(ChatMessage(session_id=sid, role=role, content=content))
        await db.flush()  # seq(Identity)가 저장 순서를 따르게 한 건씩 넣는다
    await db.commit()
    return contents


def _stub_finalize(monkeypatch, text: str = "마무리 답변") -> dict[str, Any]:
    """도구 반복을 다 쓴 react 루프의 마무리 호출 경계(도구 없는 스트림).

    호출마다 실제로 쓰일 `max_tokens`(넘기지 않으면 게이트웨이 기본값)를 남긴다.
    """
    from biznotes_core.services.gateway import llm as gateway_llm

    signature = inspect.signature(gateway_llm.complete_stream_with_fallback)
    calls: dict[str, Any] = {"n": 0, "max_tokens": []}

    async def fake_stream(db_, model_id, messages, result, *args, **kwargs):
        calls["n"] += 1
        bound = signature.bind(db_, model_id, messages, result, *args, **kwargs)
        bound.apply_defaults()
        calls["max_tokens"].append(bound.arguments["max_tokens"])
        result.usage = _usage(10, 5)

        async def gen():
            yield text

        return gen()

    monkeypatch.setattr(
        "biznotes_core.services.gateway.llm.complete_stream_with_fallback", fake_stream
    )
    return calls


def _stub_static_stream(monkeypatch) -> list[dict[str, Any]]:
    """정적 경로의 모델 경계 — 받은 메시지와 인자를 남긴다."""
    seen: list[dict[str, Any]] = []

    async def fake_stream(db_, model_id, messages, result, *args, **kwargs):
        seen.append({"messages": [dict(m) for m in messages], "kwargs": kwargs})
        result.usage = _usage(10, 5)

        async def gen():
            yield "정적 답변"

        return gen()

    monkeypatch.setattr("app.api.v1.chat.complete_stream_with_fallback", fake_stream)
    return seen


def _source_load_turn(arguments: dict[str, Any], usage: dict[str, int]) -> ToolTurnResult:
    call = ToolCall(id="load-1", name="source_load", arguments=arguments)
    return ToolTurnResult(
        content="",
        tool_calls=[call],
        assistant_message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "load-1",
                    "type": "function",
                    "function": {"name": "source_load", "arguments": json.dumps(arguments)},
                }
            ],
        },
        usage=usage,
        model=None,
    )


def _tool_result(request: dict[str, Any]) -> str:
    """모델 턴이 받은 마지막 도구 결과 메시지 본문."""
    tool_messages = [m for m in request["messages"] if m.get("role") == "tool"]
    assert tool_messages, request["messages"]
    return str(tool_messages[-1]["content"])


def _source_load_spec(request: dict[str, Any]) -> dict[str, Any]:
    [spec] = [t for t in request["tools"] if t["function"]["name"] == "source_load"]
    return spec["function"]


async def test_admin_cost_limit_is_named_in_error(
    client, db, user_a, admin_user, mock_llm, recorded, monkeypatch
):
    """관리자가 낮춘 1회 비용 한도로 판정하고, 오류 문구도 그 값을 말한다."""
    await _put_limits(client, admin_user, chat_cost_budget_usd=0.2)
    token = await login(client, user_a.email)
    session_id = await _session(client, token)
    priced = SimpleNamespace(
        model=SimpleNamespace(
            id=uuid.uuid4(), display_name="Priced", cost_per_1k={"input": 10.0, "output": 10.0}
        )
    )
    _stub_llm(monkeypatch, [_tool_turn(_usage(30), model=priced)])  # $0.30 — 기본 $0.50 아래

    events = await _ask(client, token, session_id)

    assert events[-1]["type"] == "error", events
    detail = events[-1]["detail"]
    assert "1회 비용 한도($0.20)" in detail
    assert "$0.30 사용" in detail
    assert "「관리 › 사용 제한」" in detail
    assert await _message_count(db, session_id) == 0


async def test_admin_max_iters_bounds_tool_turns(
    client, db, user_a, admin_user, mock_llm, recorded, monkeypatch
):
    """최대 도구 반복을 2로 두면 모델 도구 턴은 2번이고, 그 뒤 도구 없이 마무리한다."""
    await _put_limits(client, admin_user, chat_max_iters=2)
    token = await login(client, user_a.email)
    session_id = await _session(client, token)
    calls = _stub_llm(monkeypatch, [_tool_turn(_usage(100)) for _ in range(6)])
    finalize = _stub_finalize(monkeypatch)

    events = await _ask(client, token, session_id)

    assert calls["n"] == 2
    assert finalize["n"] == 1
    assert events[-1]["type"] == "done", events
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "마무리 답변"


async def test_admin_max_tokens_reaches_finalize_after_max_iters(
    client, db, user_a, admin_user, mock_llm, recorded, monkeypatch
):
    """도구 반복을 다 쓴 뒤의 마무리 답변도 관리자가 정한 턴당 출력 토큰을 쓴다(코어 v0.28.0 #398 —
    전에는 게이트웨이 기본값 2048로 나갔다)."""
    await _put_limits(client, admin_user, chat_max_iters=1, chat_max_tokens=8192)
    token = await login(client, user_a.email)
    session_id = await _session(client, token)
    calls = _stub_llm(monkeypatch, [_tool_turn(_usage(100)) for _ in range(3)])
    finalize = _stub_finalize(monkeypatch)

    events = await _ask(client, token, session_id)

    assert events[-1]["type"] == "done", events
    assert [r["kwargs"]["max_tokens"] for r in calls["requests"]] == [8192]
    assert finalize["max_tokens"] == [8192]


@pytest.mark.parametrize("autonomous", [True, False])
@pytest.mark.parametrize(("saved", "expected"), [(None, 4096), (8192, 8192)])
async def test_admin_max_tokens_reaches_model(
    client, db, user_a, admin_user, mock_llm, recorded, monkeypatch, autonomous, saved, expected
):
    """턴당 출력 토큰은 자율·정적 두 경로의 모델 호출에 같이 쓰인다(기본 4096)."""
    if saved is not None:
        await _put_limits(client, admin_user, chat_max_tokens=saved)
    token = await login(client, user_a.email)
    session_id = await _session(client, token)
    calls = _stub_llm(monkeypatch, [_final_turn("답변", _usage(100, 10))])
    static = _stub_static_stream(monkeypatch)

    events = await _ask(client, token, session_id, autonomous=autonomous)

    assert events[-1]["type"] == "done", events
    if autonomous:
        assert [r["kwargs"]["max_tokens"] for r in calls["requests"]] == [expected]
    else:
        assert [r["kwargs"]["max_tokens"] for r in static] == [expected]


@pytest.mark.parametrize("autonomous", [True, False])
@pytest.mark.parametrize(("saved", "expected"), [(None, 6), (2, 2), (0, 0), (20, 10)])
async def test_admin_history_length_reaches_model(
    client, db, user_a, admin_user, mock_llm, recorded, monkeypatch, autonomous, saved, expected
):
    """대화 이력 길이만큼 최근 메시지를 넘긴다 — 0이면 이력 없이 답한다(두 경로 같음)."""
    if saved is not None:
        await _put_limits(client, admin_user, chat_history_messages=saved)
    token = await login(client, user_a.email)
    session_id = await _session(client, token)
    contents = await _seed_history(db, session_id, turns=5)
    calls = _stub_llm(monkeypatch, [_final_turn("답변", _usage(100, 10))])
    static = _stub_static_stream(monkeypatch)

    events = await _ask(client, token, session_id, autonomous=autonomous)

    assert events[-1]["type"] == "done", events
    messages = (calls["requests"] if autonomous else static)[0]["messages"]
    # system 다음부터 현재 질문 앞까지가 이력이다
    assert messages[0]["role"] == "system"
    assert messages[-1] == {"role": "user", "content": "리포트를 표로 정리해줘"}
    history = [m["content"] for m in messages[1:-1]]
    assert history == (contents[-expected:] if expected else [])


async def test_admin_source_load_caps_reach_tool(
    client, db, user_a, admin_user, mock_llm, recorded, monkeypatch
):
    """source_load 상한이 도구 스펙과 실제 읽기에 걸린다.

    모델이 더 달라고 해도 상한까지만 읽는다.
    """
    await _put_limits(
        client,
        admin_user,
        source_load_max_sources=2,
        source_load_max_chars=5_000,
        source_load_budget_ratio=0,
    )
    token = await login(client, user_a.email)
    nb_id, session_id = await _notebook_session(client, token)
    for title in ("소스 가", "소스 나", "소스 다"):
        await _add_source(db, nb_id, title, "가" * 4_000)
    calls = _stub_llm(
        monkeypatch,
        [
            _source_load_turn({"max_sources": 20, "max_chars": 200_000}, _usage(100)),
            _final_turn("정리했습니다.", _usage(100, 10)),
        ],
    )

    events = await _ask(client, token, session_id)

    assert events[-1]["type"] == "done", events
    spec = _source_load_spec(calls["requests"][0])
    assert spec["parameters"]["properties"]["max_sources"]["maximum"] == 2
    assert spec["parameters"]["properties"]["max_chars"]["maximum"] == 5_000
    loaded = [e for e in events[-1]["evidence"] if e.get("kind") == "source"]
    assert len(loaded) == 2
    result = _tool_result(calls["requests"][1])
    assert "소스 다" in result and "max_sources" in result
    assert result.count("가") <= 5_000 + 10  # 제목의 「가」 몇 글자를 빼면 원문은 상한 안이다
    assert "budget_fit" not in result


@pytest.mark.parametrize(("ratio", "fitted"), [(None, True), (0, False)])
async def test_source_load_budget_ratio_default_fits_result(
    client, db, user_a, admin_user, mock_llm, recorded, monkeypatch, ratio, fitted
):
    """남은 토큰 한도 맞춤은 기본(0.3)으로 켜져 있고, 0이면 끈다(D3)."""
    limits: dict[str, Any] = {"chat_token_budget": 10_000}
    if ratio is not None:
        limits["source_load_budget_ratio"] = ratio
    await _put_limits(client, admin_user, **limits)
    token = await login(client, user_a.email)
    nb_id, session_id = await _notebook_session(client, token)
    await _add_source(db, nb_id, "긴 소스", "원문 문장입니다. " * 600)
    calls = _stub_llm(
        monkeypatch,
        [
            # 첫 턴이 9,000토큰을 써서 남은 한도는 1,000 — 비율 0.3이면 원문 몫은 300토큰이다
            _source_load_turn({}, _usage(9_000)),
            _final_turn("정리했습니다.", _usage(100, 10)),
        ],
    )

    events = await _ask(client, token, session_id)

    assert calls["n"] == 2, events
    assert ("budget_fit" in _tool_result(calls["requests"][1])) is fitted


async def test_chat_max_tokens_default_matches_core_react_default(db, user_a, monkeypatch):
    """채팅 기본 출력 토큰(4096)은 코어 react 루프가 값 없이 쓰는 기본값과 같다(공개 상수 없음)."""
    from biznotes_core.services.agents.runtime import RunContext, run_react_node

    from app.services.limits import CHAT_MAX_TOKENS_DEFAULT

    calls = _stub_llm(monkeypatch, [_final_turn("답변", _usage(10, 1))])
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={})
    await run_react_node(
        ctx, {"id": "r", "type": "react_agent", "goal": "x", "allowed_tools": ["rag_search"]}
    )
    assert calls["requests"][0]["kwargs"]["max_tokens"] == CHAT_MAX_TOKENS_DEFAULT
