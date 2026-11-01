"""채팅 세션 고정(pin) API 테스트(phase-22-design.md §3.4)."""

import httpx

from tests.conftest import bearer, login


async def _make_notebook(client, token) -> str:
    res = await client.post(
        "/api/v1/notebooks", json={"title": "pin-nb"}, headers=bearer(token)
    )
    return res.json()["id"]


async def test_pin_and_unpin_session(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    session = (
        await client.post(f"/api/v1/notebooks/{nb_id}/chat/sessions", headers=bearer(token))
    ).json()
    assert session["pinned"] is False

    res = await client.patch(
        f"/api/v1/chat/sessions/{session['id']}", json={"pinned": True}, headers=bearer(token)
    )
    assert res.status_code == 200
    assert res.json()["pinned"] is True

    res = await client.patch(
        f"/api/v1/chat/sessions/{session['id']}", json={"pinned": False}, headers=bearer(token)
    )
    assert res.status_code == 200
    assert res.json()["pinned"] is False


async def test_pinned_sessions_sort_first(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    s1 = (
        await client.post(f"/api/v1/notebooks/{nb_id}/chat/sessions", headers=bearer(token))
    ).json()
    s2 = (
        await client.post(f"/api/v1/notebooks/{nb_id}/chat/sessions", headers=bearer(token))
    ).json()

    # s2가 더 최근에 만들어졌으므로 기본 정렬은 s2, s1 순 — s1을 고정하면 s1이 앞으로 온다.
    await client.patch(
        f"/api/v1/chat/sessions/{s1['id']}", json={"pinned": True}, headers=bearer(token)
    )

    res = await client.get(f"/api/v1/notebooks/{nb_id}/chat/sessions", headers=bearer(token))
    ids = [s["id"] for s in res.json()]
    assert ids.index(s1["id"]) < ids.index(s2["id"])


async def test_other_user_cannot_pin_session(client: httpx.AsyncClient, user_a, user_b):
    token_a = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token_a)
    session = (
        await client.post(f"/api/v1/notebooks/{nb_id}/chat/sessions", headers=bearer(token_a))
    ).json()

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    res = await client.patch(
        f"/api/v1/chat/sessions/{session['id']}", json={"pinned": True}, headers=bearer(token_b)
    )
    assert res.status_code == 404
