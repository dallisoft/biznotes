"""답변 다시 생성(ui-ux-improvements ③ PR-6) — POST /chat/sessions/{id}/messages 의 `regenerate`.

마지막 질문을 다시 보내 새 답변을 받고, 성공하면 마지막 질문·답변 한 쌍을 새 쌍으로 바꾼다.
실패하면 이전 답변을 그대로 둔다. LLM에는 바꿀 쌍을 뺀 이력만 보낸다.
"""

import json
import uuid
from typing import Any

import httpx
import pytest
from biznotes_core.models import ChatMessage, ChatSession
from sqlalchemy import select

from tests.conftest import bearer, login


def _events(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


async def _session(client: httpx.AsyncClient, token: str) -> uuid.UUID:
    nb = await client.post("/api/v1/notebooks", json={"title": "regen"}, headers=bearer(token))
    sess = await client.post(
        f"/api/v1/notebooks/{nb.json()['id']}/chat/sessions", headers=bearer(token)
    )
    return uuid.UUID(sess.json()["id"])


async def _seed_turns(db, session_id: uuid.UUID, turns: list[tuple[str, str]]) -> None:
    for q, a in turns:
        db.add(ChatMessage(session_id=session_id, role="user", content=q))
        await db.flush()
        db.add(ChatMessage(session_id=session_id, role="assistant", content=a))
        await db.flush()
    await db.commit()


async def _contents(db, session_id: uuid.UUID) -> list[tuple[str, str]]:
    db.expire_all()
    rows = await db.execute(
        select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.seq)
    )
    return [(m.role, m.content) for m in rows.scalars().all()]


@pytest.fixture
def react(monkeypatch) -> dict[str, Any]:
    """자율 경로의 react 노드 — 받은 노드(이력 포함)를 기록하고 정해진 답을 돌려준다."""
    seen: dict[str, Any] = {"answer": "새 답변", "fail": False}

    async def fake_react(ctx, node, state):
        seen["node"] = node
        if seen["fail"]:
            raise RuntimeError("모델 호출 실패")
        ctx.add_tokens({"total_tokens": 10})
        return {"text": seen["answer"]}

    monkeypatch.setattr("biznotes_core.services.agents.runtime.run_react_node", fake_react)
    return seen


async def test_regenerate_replaces_last_turn_and_hides_it_from_history(
    client, db, user_a, mock_llm, react
):
    token = await login(client, user_a.email)
    sid = await _session(client, token)
    await _seed_turns(db, sid, [("첫 질문", "첫 답변"), ("두 번째 질문", "옛 답변")])

    res = await client.post(
        f"/api/v1/chat/sessions/{sid}/messages",
        json={"content": "두 번째 질문", "autonomous": True, "regenerate": True},
        headers=bearer(token),
    )
    assert res.status_code == 200
    assert _events(res.text)[-1]["type"] == "done"
    assert await _contents(db, sid) == [
        ("user", "첫 질문"),
        ("assistant", "첫 답변"),
        ("user", "두 번째 질문"),
        ("assistant", "새 답변"),
    ]
    # LLM에는 바꿀 쌍을 뺀 이력만 간다
    assert react["node"]["history"] == [
        {"role": "user", "content": "첫 질문"},
        {"role": "assistant", "content": "첫 답변"},
    ]


async def test_regenerate_failure_keeps_previous_answer(client, db, user_a, mock_llm, react):
    token = await login(client, user_a.email)
    sid = await _session(client, token)
    await _seed_turns(db, sid, [("질문", "옛 답변")])
    react["fail"] = True

    res = await client.post(
        f"/api/v1/chat/sessions/{sid}/messages",
        json={"content": "질문", "autonomous": True, "regenerate": True},
        headers=bearer(token),
    )
    assert _events(res.text)[-1]["type"] == "error"
    assert await _contents(db, sid) == [("user", "질문"), ("assistant", "옛 답변")]


async def test_regenerate_static_path_replaces_last_turn(client, db, user_a, mock_llm, monkeypatch):
    from biznotes_core.services.rag_chat import RetrievalResult

    async def fake_retrieve(db_, notebook_id, query, source_ids, top_k=6):
        return RetrievalResult(citations=[], insufficient_context=False)

    monkeypatch.setattr("app.api.v1.chat.retrieve_context", fake_retrieve)
    token = await login(client, user_a.email)
    sid = await _session(client, token)
    await _seed_turns(db, sid, [("질문", "옛 답변")])

    res = await client.post(
        f"/api/v1/chat/sessions/{sid}/messages",
        json={"content": "질문", "autonomous": False, "regenerate": True},
        headers=bearer(token),
    )
    assert _events(res.text)[-1]["type"] == "done"
    contents = await _contents(db, sid)
    assert [r for r, _ in contents] == ["user", "assistant"]
    assert contents[0] == ("user", "질문")
    assert contents[1][1] != "옛 답변"


async def test_regenerate_without_answer_is_conflict(client, db, user_a, mock_llm, react):
    token = await login(client, user_a.email)
    sid = await _session(client, token)
    res = await client.post(
        f"/api/v1/chat/sessions/{sid}/messages",
        json={"content": "질문", "autonomous": True, "regenerate": True},
        headers=bearer(token),
    )
    assert res.status_code == 409
    assert "node" not in react  # 모델을 부르지 않는다


async def test_regenerate_with_different_question_is_conflict(
    client, db, user_a, mock_llm, react
):
    token = await login(client, user_a.email)
    sid = await _session(client, token)
    await _seed_turns(db, sid, [("질문", "옛 답변")])
    res = await client.post(
        f"/api/v1/chat/sessions/{sid}/messages",
        json={"content": "다른 질문", "autonomous": True, "regenerate": True},
        headers=bearer(token),
    )
    assert res.status_code == 409
    assert await _contents(db, sid) == [("user", "질문"), ("assistant", "옛 답변")]


async def test_regenerate_other_users_session_is_not_found(
    client, db, user_a, user_b, mock_llm, react
):
    """비인가: 다른 사용자의 대화는 다시 생성할 수 없고 메시지도 바뀌지 않는다."""
    token_a = await login(client, user_a.email)
    sid = await _session(client, token_a)
    await _seed_turns(db, sid, [("질문", "A의 답변")])

    token_b = await login(client, user_b.email)
    res = await client.post(
        f"/api/v1/chat/sessions/{sid}/messages",
        json={"content": "질문", "autonomous": True, "regenerate": True},
        headers=bearer(token_b),
    )
    assert res.status_code == 404
    assert await _contents(db, sid) == [("user", "질문"), ("assistant", "A의 답변")]
    assert (await db.get(ChatSession, sid)) is not None
