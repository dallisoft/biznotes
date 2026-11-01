"""홈 「최근 대화 이어가기」 — GET /api/v1/chat/recent (ui-ux-improvements ⑥).

내 대화만, 메시지가 있는 대화만, 지금 읽을 수 있는 노트북의 대화만 최근 활동순으로 돌려준다.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
from biznotes_core.models import ChatMessage, ChatSession, Notebook
from sqlalchemy import update

from tests.conftest import bearer, login


async def _notebook(client: httpx.AsyncClient, token: str, title: str) -> str:
    res = await client.post(
        "/api/v1/notebooks", json={"title": title, "emoji": "📒"}, headers=bearer(token)
    )
    return res.json()["id"]


async def _set_visibility(db, notebook_id: str, visibility: str) -> None:
    await db.execute(
        update(Notebook).where(Notebook.id == uuid.UUID(notebook_id)).values(visibility=visibility)
    )
    await db.commit()


async def _session_with_message(
    db, notebook_id: str, user_id: uuid.UUID, title: str, minutes_ago: int, *, messages: bool = True
) -> uuid.UUID:
    s = ChatSession(notebook_id=uuid.UUID(notebook_id), user_id=user_id, title=title)
    db.add(s)
    await db.flush()
    if messages:
        db.add(ChatMessage(session_id=s.id, role="user", content=title))
        db.add(ChatMessage(session_id=s.id, role="assistant", content="답변"))
    await db.flush()
    await db.execute(
        update(ChatSession)
        .where(ChatSession.id == s.id)
        .values(updated_at=datetime.now(UTC) - timedelta(minutes=minutes_ago))
    )
    await db.commit()
    return s.id


async def test_recent_returns_my_sessions_with_messages_newest_first(client, db, user_a):
    token = await login(client, user_a.email)
    nb1 = await _notebook(client, token, "커피")
    nb2 = await _notebook(client, token, "녹차")
    old = await _session_with_message(db, nb1, user_a.id, "예전 질문", 60)
    new = await _session_with_message(db, nb2, user_a.id, "최근 질문", 1)
    await _session_with_message(db, nb1, user_a.id, "빈 대화", 0, messages=False)

    res = await client.get("/api/v1/chat/recent", headers=bearer(token))
    assert res.status_code == 200
    body = res.json()
    assert [r["session_id"] for r in body] == [str(new), str(old)]
    assert body[0] == {
        "session_id": str(new),
        "notebook_id": nb2,
        "notebook_title": "녹차",
        "notebook_emoji": "📒",
        "title": "최근 질문",
        "updated_at": body[0]["updated_at"],
    }


async def test_recent_limit(client, db, user_a):
    token = await login(client, user_a.email)
    nb = await _notebook(client, token, "nb")
    for i in range(4):
        await _session_with_message(db, nb, user_a.id, f"q{i}", i)
    res = await client.get("/api/v1/chat/recent?limit=2", headers=bearer(token))
    assert [r["title"] for r in res.json()] == ["q0", "q1"]
    for bad in (0, 11):
        res = await client.get(f"/api/v1/chat/recent?limit={bad}", headers=bearer(token))
        assert res.status_code == 422


async def test_recent_excludes_other_users_sessions(client, db, user_a, user_b):
    """비인가: 같은 공개 노트북이라도 다른 사용자의 대화는 보이지 않는다."""
    token_a = await login(client, user_a.email)
    nb = await _notebook(client, token_a, "공개 노트북")
    await _set_visibility(db, nb, "public")
    await _session_with_message(db, nb, user_a.id, "A의 질문", 1)

    token_b = await login(client, user_b.email)
    res = await client.get("/api/v1/chat/recent", headers=bearer(token_b))
    assert res.status_code == 200
    assert res.json() == []


async def test_recent_excludes_notebooks_no_longer_readable(client, db, user_a, user_b):
    """비인가: 공개가 해제돼 읽을 수 없게 된 노트북의 내 대화는 제목·노트북 이름을
    노출하지 않는다."""
    token_a = await login(client, user_a.email)
    nb = await _notebook(client, token_a, "비공개로 바뀔 노트북")
    await _set_visibility(db, nb, "public")
    token_b = await login(client, user_b.email)
    await _session_with_message(db, nb, user_b.id, "B가 남긴 질문", 1)
    assert len((await client.get("/api/v1/chat/recent", headers=bearer(token_b))).json()) == 1

    await _set_visibility(db, nb, "private")
    res = await client.get("/api/v1/chat/recent", headers=bearer(token_b))
    assert res.json() == []


async def test_recent_requires_auth(client):
    assert (await client.get("/api/v1/chat/recent")).status_code == 401
