"""채팅 진입 run의 워커 분기(#66 PR-1) — trigger·질문·이력 전달, 진입 재검사, 결과 메시지.

설계: docs/plans/chat-slash-entry.md AC-5·AC-6 및 §3 워커. 워커 `_run`을 실제로 돌리고 진입 판정
(`assert_chat_entry_allowed`)·승인 게이트·스킬 실행은 대체하지 않는다. 대체하는 것은 경계뿐이다 —
LLM 게이트웨이(`complete_stream`·`complete_with_tools`), 내장 도구 디스패치, 이벤트·태스크 발행,
스토리지(`mem_storage`·`_mock_runtime_deps`).
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from biznotes_core.models import AgentRun, AgentRunApproval, ChatMessage, Skill
from biznotes_core.services.agents import runtime as agents_runtime
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult
from biznotes_core.workers.runner import get_session_factory
from sqlalchemy import select

import app.workers.tasks.agent as agent_task
from app.services.chat_entry import (
    ENTRY_CANCELLED_TEXT,
    ENTRY_NO_RESULT_MARK,
    entry_failed_text,
    write_entry_result,
)
from tests.chat_entry_fakes import (
    DENIED,
    boundaries,  # noqa: F401 — 픽스처
    chat_run,
    chat_workflow,
    legacy_skill,
    notebook,
    post_entry,
    react_definition,
    session,
    token,
)

pytestmark = pytest.mark.usefixtures("boundaries", "mem_storage", "mock_llm")

LONG_ANSWER = "주간 요약: " + "가나다라마바사 " * 150  # 500자를 넘는다


async def _results(db, run_id: uuid.UUID) -> list[ChatMessage]:
    rows = await db.execute(
        select(ChatMessage).where(ChatMessage.run_id == run_id).order_by(ChatMessage.seq)
    )
    return list(rows.scalars().all())


async def _assert_failure_row(db, run: AgentRun, error: str) -> None:
    await db.refresh(run)
    assert run.status == "failed"
    assert run.error == error
    [row] = await _results(db, run.id)
    assert row.role == "assistant"
    assert row.session_id == run.chat_session_id
    assert row.content == entry_failed_text(error)
    assert row.token_usage.get(ENTRY_NO_RESULT_MARK) == 1


def _turn(name: str, args: dict[str, Any]) -> ToolTurnResult:
    call_id = uuid.uuid4().hex[:9]
    return ToolTurnResult(
        content="",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=args)],
        assistant_message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
        },
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        model=None,
    )


def _final(text: str) -> ToolTurnResult:
    return ToolTurnResult(
        content=text,
        tool_calls=[],
        assistant_message={"role": "assistant", "content": text},
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        model=None,
    )


def _script(monkeypatch: pytest.MonkeyPatch, turns: list[ToolTurnResult]) -> list[list[dict]]:
    """react 턴 경계 — 받은 메시지를 기록하고 정해 둔 턴을 차례로 돌려준다."""
    seen: list[list[dict]] = []

    async def fake(_db, _model_id, messages, tools, **_kw):
        seen.append([dict(m) for m in messages])
        return turns[min(len(seen) - 1, len(turns) - 1)]

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake)
    return seen


@pytest.fixture
def http_tool(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """내장 도구 디스패치 경계 — 외부 요청 없이 호출만 기록한다."""
    calls: list[dict[str, Any]] = []

    async def dispatch(_ctx, name, args):
        calls.append({"name": name, **args})
        return "200 OK"

    monkeypatch.setattr(agents_runtime, "_dispatch_builtin_tool", dispatch)
    return calls


@pytest.fixture
def long_stream(monkeypatch: pytest.MonkeyPatch) -> list[list[dict]]:
    """legacy llm 노드의 스트림 경계 — 500자를 넘는 답을 낸다."""
    seen: list[list[dict]] = []

    async def fake(resolved, messages, result, temperature=0.2, max_tokens=2048):
        seen.append([dict(m) for m in messages])

        async def gen():
            yield LONG_ANSWER
            result.usage = {"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10}

        return gen()

    monkeypatch.setattr(gateway_llm, "complete_stream", fake)
    return seen


@pytest.fixture
def no_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    """RAG 검색 경계 — 결과 없음. 산출물 저장은 `mem_storage`가 받는다(드래프트와 같은 저장소)."""

    async def fake_retrieve(db, notebook_id, query, source_ids, top_k=6):
        from biznotes_core.services.rag_chat import RetrievalResult

        return RetrievalResult(citations=[], insufficient_context=True)

    monkeypatch.setattr("biznotes_core.services.rag_chat.retrieve_context", fake_retrieve)


# ---------- AC-6 성공 결과 메시지 ----------


async def test_skill_entry_end_to_end_writes_full_result_message(
    client: httpx.AsyncClient, db, user_a, mock_llm, long_stream, no_retrieval
):
    """진입 API → 워커 → assistant 결과 행 1개(run_id). 본문은 500자로 자르지 않은 전문이다."""
    skill, _, _ = await legacy_skill(db, user_a, "brief-writer")
    nb = await notebook(db, user_a)
    s = await session(db, nb, user_a)
    entry = await post_entry(client, user_a, s.id, name=skill.slug, content="주간 요약해 줘")
    assert entry.status_code == 202, entry.text
    run_id = uuid.UUID(entry.json()["run"]["id"])

    assert await agent_task._run(str(run_id)) == {"ok": True}

    run = await db.get(AgentRun, run_id)
    assert run is not None
    await db.refresh(run)
    assert run.status == "succeeded", run.error
    assert run.result_summary == LONG_ANSWER[:500]
    [row] = await _results(db, run_id)
    assert row.role == "assistant"
    assert row.session_id == s.id
    assert row.content == LONG_ANSWER
    assert row.model_id is not None  # 실제로 응답한 모델(코어 `chat_used_model_id`)
    assert ENTRY_NO_RESULT_MARK not in row.token_usage
    # 질문은 스킬 파라미터(`topic`)로 들어갔다
    assert "주간 요약해 줘" in "\n".join(m["content"] for m in long_stream[-1])

    listed = await client.get(
        f"/api/v1/chat/sessions/{s.id}/messages", headers=await token(client, user_a)
    )
    rows = listed.json()
    assert [(m["role"], m["run_id"]) for m in rows] == [
        ("user", None),
        ("assistant", str(run_id)),
    ]
    assert rows[0]["entry_run"]["status"] == "succeeded"
    assert rows[1]["content"] == LONG_ANSWER


# ---------- AC-5 trigger·질문·이력, 승인 경계 ----------


async def test_chat_run_context_carries_trigger_message_and_history(
    client: httpx.AsyncClient, db, user_a, http_tool, monkeypatch
):
    """chat run의 `RunContext`에 `trigger="chat"`·질문·이력이 실린다. 정의에 `auto_approve_tools`가
    있어도 채팅에서는 승인 대기다. 승인하면 같은 세션에 결과 행이 생긴다.
    """
    policy = {"http_request": {"domains": ["example.com"]}}
    wf = await chat_workflow(db, user_a, definition=react_definition(auto_approve=policy))
    nb = await notebook(db, user_a)
    s = await session(db, nb, user_a)
    # 앞선 대화 — 일반 턴 하나와 실패한 진입 턴 하나(이력에서 빠져야 한다)
    db.add(ChatMessage(session_id=s.id, role="user", content="앞 질문 P1"))
    await db.flush()
    db.add(ChatMessage(session_id=s.id, role="assistant", content="앞 답변 A1"))
    await db.commit()
    failed = await chat_run(db, wf, nb, user_a, status="failed", prompt="실패 질문", chat_session=s)
    await write_entry_result(
        db, run_id=failed.id, session_id=s.id, content=entry_failed_text("x"), no_result=True
    )
    await db.commit()
    seen = _script(
        monkeypatch,
        [_turn("http_request", {"url": "https://example.com/a"}), _final("최종 답변 F")],
    )

    entry = await post_entry(client, user_a, s.id, agent_id=wf.id, content="이번 질문 Q")
    assert entry.status_code == 202, entry.text
    run_id = uuid.UUID(entry.json()["run"]["id"])
    assert entry.json()["run"]["params"] == {}  # 채팅 본문은 params가 아니라 예약 이름공간이다

    assert await agent_task._run(str(run_id)) == {"ok": True, "awaiting_approval": True}
    assert http_tool == []  # 자동 승인되지 않았다
    first = seen[0]
    assert first[-1] == {"role": "user", "content": "이번 질문 Q"}  # goal `{chat_message}`
    history = [m["content"] for m in first[1:-1]]
    assert history == ["앞 질문 P1", "앞 답변 A1"]  # 실패한 진입 턴은 질문까지 빠졌다
    approval = (
        await db.execute(select(AgentRunApproval).where(AgentRunApproval.run_id == run_id))
    ).scalar_one()
    assert approval.tool_name == "http_request"
    assert await _results(db, run_id) == []  # 승인 대기 중에는 결과 행이 없다

    approved = await client.post(
        f"/api/v1/agent-runs/{run_id}/approvals/{approval.id}/approve",
        headers=await token(client, user_a),
    )
    assert approved.status_code == 200, approved.text
    assert await agent_task._run(str(run_id)) == {"ok": True}

    assert [c["url"] for c in http_tool] == ["https://example.com/a"]
    [row] = await _results(db, run_id)
    assert row.content == "최종 답변 F"
    assert row.session_id == s.id


async def test_same_definition_auto_approves_outside_chat(db, user_a, http_tool, monkeypatch):
    """대조군 — 같은 정의를 수동 run으로 돌리면 정책대로 자동 승인된다(채팅 경계는 trigger로
    갈린다).
    """
    policy = {"http_request": {"domains": ["example.com"]}}
    wf = await chat_workflow(db, user_a, definition=react_definition(auto_approve=policy))
    nb = await notebook(db, user_a)
    _script(monkeypatch, [_turn("http_request", {"url": "https://example.com/a"}), _final("끝")])
    run = AgentRun(
        agent_id=wf.id, notebook_id=nb.id, user_id=user_a.id, params={}, status="queued"
    )
    db.add(run)
    await db.commit()

    assert await agent_task._run(str(run.id)) == {"ok": True}

    assert [c["url"] for c in http_tool] == ["https://example.com/a"]
    assert await _results(db, run.id) == []  # 채팅이 아닌 run은 결과 행을 쓰지 않는다


# ---------- AC-5 진입 재검사 ----------


async def test_skill_disabled_after_queue_denies_first_run(
    client: httpx.AsyncClient, db, user_a, long_stream
):
    """큐잉 뒤 스킬을 끄면 최초 실행 전에 거부되고 실패 결과 행이 남는다."""
    skill, _, _ = await legacy_skill(db, user_a)
    s = await session(db, await notebook(db, user_a), user_a)
    entry = await post_entry(client, user_a, s.id, name=skill.slug)
    assert entry.status_code == 202, entry.text
    run = await db.get(AgentRun, uuid.UUID(entry.json()["run"]["id"]))
    assert run is not None
    stored = await db.get(Skill, skill.id)
    assert stored is not None
    stored.enabled = False
    await db.commit()

    assert await agent_task._run(str(run.id)) == {"ok": False, "error": DENIED}

    await _assert_failure_row(db, run, DENIED)
    assert long_stream == []


async def _awaiting_with_resolved_approval(db, agent, nb, user, **fields) -> tuple:
    run = await chat_run(db, agent, nb, user, status="awaiting_approval", **fields)
    now = datetime.now(UTC)
    approval = AgentRunApproval(
        run_id=run.id,
        tool_name="http_request",
        arguments_summary={},
        status="approved",
        requested_at=now,
        expires_at=now + timedelta(minutes=10),
        checkpoint={"tool_name": "http_request"},
    )
    db.add(approval)
    await db.commit()
    return run, approval


async def test_skill_disabled_before_resume_denies_without_consuming_approval(db, user_a):
    """승인 재개 전에도 재검사한다 — 거부하면 승인을 소비하지 않고 실패 결과 행을 남긴다."""
    skill, version, wrapper = await legacy_skill(db, user_a)
    nb = await notebook(db, user_a)
    run, approval = await _awaiting_with_resolved_approval(
        db, wrapper, nb, user_a, skill_version_id=version.id, params={"topic": "q"}
    )
    stored = await db.get(Skill, skill.id)
    assert stored is not None
    stored.enabled = False
    await db.commit()

    assert await agent_task._run(str(run.id)) == {"ok": False, "error": DENIED}

    await _assert_failure_row(db, run, DENIED)
    await db.refresh(approval)
    assert approval.consumed is False


_NOT_CHAT_SHAPED = {
    "nodes": [
        {"id": "a", "type": "constant", "value": "v"},
        {"id": "b", "type": "constant", "value": "w"},
    ],
    "edges": [["a", "b"]],
}


@pytest.mark.parametrize("resume", [False, True])
async def test_entry_recheck_blocks_workflow_that_lost_chat_shape(
    client: httpx.AsyncClient, db, user_a, resume
):
    """진입 판정만 바뀐 경우(실행 인가는 그대로) — 정의가 채팅 진입 형태를 잃으면
    최초 실행·승인 재개 모두 코어 `assert_chat_entry_allowed`가 막는다.
    """
    wf = await chat_workflow(db, user_a)
    nb = await notebook(db, user_a)
    if resume:
        run, approval = await _awaiting_with_resolved_approval(db, wf, nb, user_a)
    else:
        s = await session(db, nb, user_a)
        entry = await post_entry(client, user_a, s.id, agent_id=wf.id)
        assert entry.status_code == 202, entry.text
        found = await db.get(AgentRun, uuid.UUID(entry.json()["run"]["id"]))
        assert found is not None
        run, approval = found, None
    wf.definition = _NOT_CHAT_SHAPED
    await db.commit()

    assert await agent_task._run(str(run.id)) == {"ok": False, "error": DENIED}

    await _assert_failure_row(db, run, DENIED)
    if approval is not None:
        await db.refresh(approval)
        assert approval.consumed is False


async def test_non_chat_run_of_same_workflow_is_not_rechecked_as_entry(db, user_a):
    """대조군 — 같은 정의의 수동 run은 진입 재검사를 받지 않는다(실행 인가만)."""
    wf = await chat_workflow(db, user_a, definition=_NOT_CHAT_SHAPED)
    nb = await notebook(db, user_a)
    run = AgentRun(
        agent_id=wf.id, notebook_id=nb.id, user_id=user_a.id, params={}, status="queued"
    )
    db.add(run)
    await db.commit()

    assert await agent_task._run(str(run.id)) == {"ok": True}


async def test_missing_question_row_denies(db, user_a):
    """질문 행이 사라진 chat run(FK SET NULL)은 실행하지 않는다."""
    wf = await chat_workflow(db, user_a)
    nb = await notebook(db, user_a)
    run = await chat_run(db, wf, nb, user_a, status="queued")
    question = await db.get(ChatMessage, run.prompt_message_id)
    await db.delete(question)
    await db.commit()

    assert await agent_task._run(str(run.id)) == {"ok": False, "error": DENIED}

    await _assert_failure_row(db, run, DENIED)


async def test_recheck_error_closes_chat_run_with_failure_row(db, user_a, monkeypatch):
    """재검사 자체가 예기치 않게 실패하면(#68) 실행하지 않고 실패 결과 행을 남긴다(예외 원문
    없음).
    """
    import biznotes_core.services.agents.chat_entry as chat_entry

    async def broken(*_a, **_k):
        raise RuntimeError("dsn=postgres://secret")

    monkeypatch.setattr(chat_entry, "assert_chat_entry_allowed", broken)
    wf = await chat_workflow(db, user_a)
    run = await chat_run(db, wf, await notebook(db, user_a), user_a, status="queued")

    result = await agent_task._run(str(run.id))

    assert result == {"ok": False, "error": agent_task._RECHECK_FAILED}
    await _assert_failure_row(db, run, agent_task._RECHECK_FAILED)
    [row] = await _results(db, run.id)
    assert "secret" not in row.content


# ---------- 실패·중지·만료 결과 행 ----------


async def test_execution_failure_writes_fixed_failure_row(db, user_a, monkeypatch):
    """실행 실패는 「실행하지 못했습니다 — <고정 문구>」다. 공급자 원문은 싣지 않는다."""

    async def down(*_a, **_k):
        raise gateway_llm.GatewayCallError("provider said: secret-key-123")

    monkeypatch.setattr(gateway_llm, "complete_with_tools", down)
    wf = await chat_workflow(db, user_a)
    run = await chat_run(db, wf, await notebook(db, user_a), user_a, status="queued")

    result = await agent_task._run(str(run.id))

    assert result["ok"] is False
    await db.refresh(run)
    assert run.error is not None
    await _assert_failure_row(db, run, run.error)
    [row] = await _results(db, run.id)
    assert "secret-key-123" not in row.content


async def test_cancel_writes_stopped_row_once(client: httpx.AsyncClient, db, user_a):
    """취소 API가 같은 커밋에서 「중지했습니다」를 쓴다. 그 뒤 워커 전달은 아무것도 쓰지 않는다."""
    wf = await chat_workflow(db, user_a)
    run = await chat_run(db, wf, await notebook(db, user_a), user_a, status="running")
    headers = await token(client, user_a)

    res = await client.post(f"/api/v1/agent-runs/{run.id}/cancel", headers=headers)
    again = await client.post(f"/api/v1/agent-runs/{run.id}/cancel", headers=headers)

    assert res.status_code == 200, res.text
    assert again.status_code == 409
    assert await agent_task._run(str(run.id)) == {"ok": False, "error": "cancelled"}
    [row] = await _results(db, run.id)
    assert row.content == ENTRY_CANCELLED_TEXT
    assert row.token_usage.get(ENTRY_NO_RESULT_MARK) == 1


async def test_cancel_during_execution_keeps_cancelled_and_one_row(
    client: httpx.AsyncClient, db, user_a, monkeypatch
):
    """실행 중 취소되면 워커는 결과로 덮지 않는다 — run은 cancelled, 결과 행은 「중지했습니다」
    하나.
    """
    wf = await chat_workflow(db, user_a)
    nb = await notebook(db, user_a)
    s = await session(db, nb, user_a)
    entry = await post_entry(client, user_a, s.id, agent_id=wf.id)
    run_id = entry.json()["run"]["id"]
    headers = await token(client, user_a)

    async def cancel_then_answer(*_a, **_k):
        res = await client.post(f"/api/v1/agent-runs/{run_id}/cancel", headers=headers)
        assert res.status_code == 200, res.text
        return _final("늦은 답변")

    monkeypatch.setattr(gateway_llm, "complete_with_tools", cancel_then_answer)

    assert await agent_task._run(run_id) == {"ok": False, "error": "cancelled"}

    run = await db.get(AgentRun, uuid.UUID(run_id))
    assert run is not None
    await db.refresh(run)
    assert run.status == "cancelled"
    [row] = await _results(db, run.id)
    assert row.content == ENTRY_CANCELLED_TEXT


async def test_cancel_before_approval_wait_keeps_cancelled(
    client: httpx.AsyncClient, db, user_a, http_tool, monkeypatch
):
    """도구 승인을 기다리기 직전에 취소되면 `awaiting_approval`로 덮지 않는다 — 승인 행도
    만들지 않고 세션도 다시 잠그지 않는다(리뷰 Blocking).
    """
    wf = await chat_workflow(db, user_a, definition=react_definition())
    s = await session(db, await notebook(db, user_a), user_a)
    entry = await post_entry(client, user_a, s.id, agent_id=wf.id)
    run_id = entry.json()["run"]["id"]
    headers = await token(client, user_a)

    async def cancel_then_call_tool(*_a, **_k):
        res = await client.post(f"/api/v1/agent-runs/{run_id}/cancel", headers=headers)
        assert res.status_code == 200, res.text
        return _turn("http_request", {"url": "https://example.com/a"})

    monkeypatch.setattr(gateway_llm, "complete_with_tools", cancel_then_call_tool)

    assert await agent_task._run(run_id) == {"ok": False, "error": "cancelled"}

    run = await db.get(AgentRun, uuid.UUID(run_id))
    assert run is not None
    await db.refresh(run)
    assert run.status == "cancelled"
    approvals = await db.execute(
        select(AgentRunApproval).where(AgentRunApproval.run_id == run.id)
    )
    assert approvals.scalars().all() == []
    [row] = await _results(db, run.id)
    assert row.content == ENTRY_CANCELLED_TEXT
    assert http_tool == []


async def test_lazy_approval_expiry_writes_failure_row(client: httpx.AsyncClient, db, user_a):
    """승인 시도 때 만료가 드러나면(지연 평가) 실패 결과 행을 같은 커밋에 쓴다."""
    wf = await chat_workflow(db, user_a)
    run = await chat_run(db, wf, await notebook(db, user_a), user_a, status="awaiting_approval")
    now = datetime.now(UTC)
    approval = AgentRunApproval(
        run_id=run.id,
        tool_name="http_request",
        arguments_summary={},
        status="pending",
        requested_at=now - timedelta(minutes=20),
        expires_at=now - timedelta(minutes=10),
        checkpoint={"tool_name": "http_request"},
    )
    db.add(approval)
    await db.commit()

    res = await client.post(
        f"/api/v1/agent-runs/{run.id}/approvals/{approval.id}/approve",
        headers=await token(client, user_a),
    )

    assert res.status_code == 409
    await _assert_failure_row(db, run, "승인 대기 시간이 만료되었습니다")


async def test_reaper_approval_expiry_writes_failure_row(db, user_a):
    """주기 정리(reaper)가 만료시킨 채팅 run에도 실패 결과 행을 쓴다."""
    from biznotes_core.services.agent_runs import APPROVAL_EXPIRED_ERROR

    wf = await chat_workflow(db, user_a)
    run = await chat_run(db, wf, await notebook(db, user_a), user_a, status="awaiting_approval")
    now = datetime.now(UTC)
    db.add(
        AgentRunApproval(
            run_id=run.id,
            tool_name="http_request",
            arguments_summary={},
            status="pending",
            requested_at=now - timedelta(minutes=20),
            expires_at=now - timedelta(minutes=10),
            checkpoint={"tool_name": "http_request"},
        )
    )
    await db.commit()

    expired = await agent_task._expire_stale_approvals(get_session_factory())

    assert expired == {"expired_approvals": 1, "failed_runs": 1}
    await _assert_failure_row(db, run, APPROVAL_EXPIRED_ERROR)
