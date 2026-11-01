"""채팅 답변 중지(ui-ux-improvements B3) — 클라이언트가 연결을 끊으면 스트림이 취소된다.

Starlette(ASGI spec 2.3)는 연결이 끊기면 응답 스트림 태스크를 취소한다. 취소된 시점까지 쓴 토큰은
일일 한도에 들어가야 하고(중지로 한도를 우회하지 않는다), 끝나지 않은 답변은 저장하지 않는다.
여기서는 실제 엔드포인트 함수가 돌려준 스트림을 태스크로 소비하다가 취소해 같은 상황을 만든다.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from biznotes_core.models import ChatMessage
from sqlalchemy import func, select

from app.api.v1.chat import send_message
from app.schemas.chat import SendMessageRequest
from tests.conftest import bearer, login


async def _session(client: httpx.AsyncClient, email: str) -> uuid.UUID:
    token = await login(client, email)
    nb = await client.post("/api/v1/notebooks", json={"title": "stop-nb"}, headers=bearer(token))
    sess = await client.post(
        f"/api/v1/notebooks/{nb.json()['id']}/chat/sessions", headers=bearer(token)
    )
    return uuid.UUID(sess.json()["id"])


async def _consume(it: AsyncIterator[str]) -> None:
    async for _ in it:
        pass


@pytest.fixture
def recorded(monkeypatch) -> list[tuple[uuid.UUID, str, int]]:
    calls: list[tuple[uuid.UUID, str, int]] = []

    async def fake_record(user_id, kind, amount):
        calls.append((user_id, kind, amount))

    monkeypatch.setattr("biznotes_core.services.usage_guard.record_usage", fake_record)
    return calls


async def _settle() -> None:
    # 취소 뒤 보호된(shield) 기록 태스크가 끝날 시간을 준다
    for _ in range(5):
        await asyncio.sleep(0)


async def _message_count(db, session_id: uuid.UUID) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(ChatMessage)
            .where(ChatMessage.session_id == session_id)
        )
    ).scalar_one()


async def test_autonomous_stop_records_spent_tokens_and_saves_nothing(
    client, db, user_a, mock_llm, recorded, monkeypatch
):
    session_id = await _session(client, user_a.email)
    started = asyncio.Event()

    async def hanging_react(ctx, node, state):
        ctx.add_tokens({"prompt_tokens": 100, "completion_tokens": 23, "total_tokens": 123})
        started.set()
        await asyncio.Event().wait()  # 사용자가 중지할 때까지 끝나지 않는 도구 루프

    monkeypatch.setattr("biznotes_core.services.agents.runtime.run_react_node", hanging_react)

    res = await send_message(
        session_id, SendMessageRequest(content="연차 규정 알려줘", autonomous=True), user_a, db
    )
    task = asyncio.create_task(_consume(res.body_iterator))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _settle()

    assert recorded == [(user_a.id, "tokens", 123)]
    assert await _message_count(db, session_id) == 0


async def test_stop_while_recording_budget_exhausted_usage_still_records(
    client, db, user_a, mock_llm, monkeypatch
):
    """1회 한도 소진 오류를 내기 전 사용량 기록 중에 연결이 끊겨도 쓴 토큰은 일일 한도에 남는다
    (독립 리뷰 #229 Medium — 기록이 shield되지 않으면 취소 처리도 `usage_recorded`로 건너뛴다)."""
    from biznotes_core.services.agents.react.loop import BUDGET_HIT_KEY, BUDGET_INFO_KEY

    session_id = await _session(client, user_a.email)
    recording = asyncio.Event()
    release = asyncio.Event()
    calls: list[tuple[uuid.UUID, str, int]] = []

    async def slow_record(user_id, kind, amount):
        recording.set()
        await release.wait()
        calls.append((user_id, kind, amount))

    async def exhausted_react(ctx, node, state):
        ctx.add_tokens(
            {"prompt_tokens": 108_000, "completion_tokens": 629, "total_tokens": 108_629}
        )
        budget = {
            "reason": "token",
            "used_tokens": 108_629,
            "token_budget": 100_000,
            "used_cost_usd": 0.1,
            "cost_budget_usd": 0.5,
            "has_content": False,
        }
        return {"text": "(안내)", BUDGET_HIT_KEY: True, BUDGET_INFO_KEY: budget}

    monkeypatch.setattr("biznotes_core.services.usage_guard.record_usage", slow_record)
    monkeypatch.setattr("biznotes_core.services.agents.runtime.run_react_node", exhausted_react)

    res = await send_message(
        session_id, SendMessageRequest(content="리포트 정리해줘", autonomous=True), user_a, db
    )
    task = asyncio.create_task(_consume(res.body_iterator))
    await asyncio.wait_for(recording.wait(), timeout=5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _settle()

    assert calls == [(user_a.id, "tokens", 108_629)]
    assert await _message_count(db, session_id) == 0


async def test_autonomous_stop_before_any_llm_call_records_nothing(
    client, db, user_a, mock_llm, recorded, monkeypatch
):
    session_id = await _session(client, user_a.email)
    started = asyncio.Event()

    async def hanging_react(ctx, node, state):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("biznotes_core.services.agents.runtime.run_react_node", hanging_react)

    body = SendMessageRequest(content="q", autonomous=True)
    res = await send_message(session_id, body, user_a, db)
    task = asyncio.create_task(_consume(res.body_iterator))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _settle()

    assert recorded == []


async def test_static_stop_records_estimated_tokens_and_saves_nothing(
    client, db, user_a, mock_llm, recorded, monkeypatch
):
    """정적 경로는 제공자 usage가 스트림 끝에서야 채워진다.

    받은 델타와 프롬프트로 추정해 기록한다."""
    from biznotes_core.services.rag_chat import RetrievalResult

    session_id = await _session(client, user_a.email)
    delivered = asyncio.Event()

    async def fake_retrieve(db_, notebook_id, query, source_ids, top_k=6):
        return RetrievalResult(citations=[], insufficient_context=False)

    async def fake_stream(db_, model_id, messages, result, *a, **kw):
        async def gen():
            yield "연차는 "
            yield "입사 1년 후 15일입니다"
            delivered.set()
            await asyncio.Event().wait()

        return gen()

    monkeypatch.setattr("app.api.v1.chat.retrieve_context", fake_retrieve)
    monkeypatch.setattr("app.api.v1.chat.complete_stream_with_fallback", fake_stream)

    res = await send_message(
        session_id, SendMessageRequest(content="연차 규정 알려줘", autonomous=False), user_a, db
    )
    task = asyncio.create_task(_consume(res.body_iterator))
    await asyncio.wait_for(delivered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _settle()

    assert len(recorded) == 1
    user_id, kind, amount = recorded[0]
    assert (user_id, kind) == (user_a.id, "tokens")
    assert amount > 0
    assert await _message_count(db, session_id) == 0
