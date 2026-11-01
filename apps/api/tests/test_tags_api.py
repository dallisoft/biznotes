import httpx

from tests.conftest import bearer, login, share_group


async def _create_notebook(client, token, title="Tag Test NB") -> dict:
    res = await client.post(
        "/api/v1/notebooks", json={"title": title, "emoji": "🏷️"}, headers=bearer(token)
    )
    assert res.status_code == 201, res.text
    return res.json()


async def test_create_list_delete_tag(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)

    res = await client.post("/api/v1/tags", json={"name": "Research"}, headers=bearer(token))
    assert res.status_code == 201, res.text
    tag = res.json()
    assert tag["name"] == "research"  # 소문자 정규화

    res = await client.get("/api/v1/tags", headers=bearer(token))
    assert res.status_code == 200
    assert any(t["id"] == tag["id"] for t in res.json())

    res = await client.delete(f"/api/v1/tags/{tag['id']}", headers=bearer(token))
    assert res.status_code == 204

    res = await client.get("/api/v1/tags", headers=bearer(token))
    assert all(t["id"] != tag["id"] for t in res.json())


async def test_duplicate_tag_name_rejected(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)
    await client.post("/api/v1/tags", json={"name": "dup"}, headers=bearer(token))
    res = await client.post("/api/v1/tags", json={"name": "dup"}, headers=bearer(token))
    assert res.status_code == 409


async def test_same_name_different_owners_allowed(client: httpx.AsyncClient, user_a, user_b):
    token_a = await login(client, user_a.email)
    res = await client.post("/api/v1/tags", json={"name": "shared-name"}, headers=bearer(token_a))
    assert res.status_code == 201

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    res = await client.post("/api/v1/tags", json={"name": "shared-name"}, headers=bearer(token_b))
    assert res.status_code == 201


async def test_attach_detach_tag_owner_only(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)
    nb = await _create_notebook(client, token)
    tag_res = await client.post("/api/v1/tags", json={"name": "attach-me"}, headers=bearer(token))
    tag = tag_res.json()

    res = await client.post(
        f"/api/v1/notebooks/{nb['id']}/tags/{tag['id']}", headers=bearer(token)
    )
    assert res.status_code == 204

    res = await client.get(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token))
    assert res.json()["tags"] == ["attach-me"]

    res = await client.delete(
        f"/api/v1/notebooks/{nb['id']}/tags/{tag['id']}", headers=bearer(token)
    )
    assert res.status_code == 204

    res = await client.get(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token))
    assert res.json()["tags"] == []


async def test_write_share_user_cannot_attach_tag(client: httpx.AsyncClient, db, user_a, user_b):
    """A안 확정: owner가 아닌 write 공유 사용자는 태그 부착이 거부된다(D-19)."""
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "Shared Write NB")
    group = await share_group(db, user_a, user_b)
    await client.post(
        f"/api/v1/notebooks/{nb['id']}/share",
        json={"group_id": str(group.id), "permission": "write"},
        headers=bearer(token_a),
    )
    tag_res = await client.post(
        "/api/v1/tags", json={"name": "owner-tag"}, headers=bearer(token_a)
    )
    tag = tag_res.json()

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    res = await client.post(
        f"/api/v1/notebooks/{nb['id']}/tags/{tag['id']}", headers=bearer(token_b)
    )
    assert res.status_code == 403


async def test_cannot_attach_other_users_tag(client: httpx.AsyncClient, user_a, user_b):
    """타 사용자 소유 태그 id로 부착 시도 시 404(F13 IDOR 방지)."""
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a)

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    tag_res = await client.post(
        "/api/v1/tags", json={"name": "bs-tag"}, headers=bearer(token_b)
    )
    tag_b = tag_res.json()

    client.cookies.clear()
    token_a = await login(client, user_a.email)
    res = await client.post(
        f"/api/v1/notebooks/{nb['id']}/tags/{tag_b['id']}", headers=bearer(token_a)
    )
    assert res.status_code == 404


async def test_favorite_toggle_is_per_user(client: httpx.AsyncClient, db, user_a, user_b):
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "Fav NB")
    group = await share_group(db, user_a, user_b)
    await client.post(
        f"/api/v1/notebooks/{nb['id']}/share",
        json={"group_id": str(group.id), "permission": "read"},
        headers=bearer(token_a),
    )

    res = await client.post(f"/api/v1/notebooks/{nb['id']}/favorite", headers=bearer(token_a))
    assert res.status_code == 204
    res = await client.get(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_a))
    assert res.json()["is_favorite"] is True

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    res = await client.get(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_b))
    assert res.json()["is_favorite"] is False

    client.cookies.clear()
    token_a = await login(client, user_a.email)
    res = await client.delete(f"/api/v1/notebooks/{nb['id']}/favorite", headers=bearer(token_a))
    assert res.status_code == 204
    res = await client.get(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_a))
    assert res.json()["is_favorite"] is False


async def test_admin_cannot_attach_own_tag_to_others_notebook(
    client: httpx.AsyncClient, user_a, admin_user
):
    """codex 리뷰 지적: get_permission()은 admin을 owner로 간주하므로, owner_id 직접
    비교 없이는 admin이 자신의 태그를 남의 노트북에 붙일 수 있었다(A안 불변식 위반)."""
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "User A's NB")

    client.cookies.clear()
    token_admin = await login(client, admin_user.email)
    tag_res = await client.post(
        "/api/v1/tags", json={"name": "admin-tag"}, headers=bearer(token_admin)
    )
    tag = tag_res.json()

    res = await client.post(
        f"/api/v1/notebooks/{nb['id']}/tags/{tag['id']}", headers=bearer(token_admin)
    )
    assert res.status_code == 403


async def test_read_share_user_can_favorite(client: httpx.AsyncClient, db, user_a, user_b):
    """즐겨찾기는 owner 제약 없음 — read 권한만 있어도 가능(§3.2)."""
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "Read Share Fav")
    group = await share_group(db, user_a, user_b)
    await client.post(
        f"/api/v1/notebooks/{nb['id']}/share",
        json={"group_id": str(group.id), "permission": "read"},
        headers=bearer(token_a),
    )

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    res = await client.post(f"/api/v1/notebooks/{nb['id']}/favorite", headers=bearer(token_b))
    assert res.status_code == 204


async def test_unrelated_user_attach_detach_tag_is_404(client: httpx.AsyncClient, user_a, user_b):
    """무관 사용자의 태그 부착·해제는 단건 GET과 같은 404 `Notebook not found`다 — 노트북 존재를
    드러내지 않는다(코어 v0.19.0 #269, core-v0.20.0-upgrade AC-1.8 ④). 권한자·admin의 403은
    그대로다(`test_admin_cannot_attach_own_tag_to_others_notebook`). 소유자 노트북의 태그는
    그대로다."""
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "Hidden From Unrelated")

    token_b = await login(client, user_b.email)
    res = await client.post("/api/v1/tags", json={"name": "b-tag"}, headers=bearer(token_b))
    tag = res.json()
    path = f"/api/v1/notebooks/{nb['id']}/tags/{tag['id']}"
    for res in (
        await client.post(path, headers=bearer(token_b)),
        await client.delete(path, headers=bearer(token_b)),
    ):
        assert (res.status_code, res.json()["detail"]) == (404, "Notebook not found")

    token_a = await login(client, user_a.email)
    res = await client.get(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_a))
    assert res.json()["tags"] == []
