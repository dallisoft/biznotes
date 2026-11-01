import httpx

from tests.conftest import bearer, login


async def test_login_success_sets_cookies(client: httpx.AsyncClient, user_a, test_password):
    res = await client.post(
        "/api/v1/auth/login", json={"email": user_a.email, "password": test_password}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["user"]["email"] == user_a.email
    assert body["access_token"]
    set_cookies = res.headers.get_list("set-cookie")
    assert any("access_token=" in c and "HttpOnly" in c for c in set_cookies)
    assert any("refresh_token=" in c and "HttpOnly" in c for c in set_cookies)


async def test_login_wrong_password(client: httpx.AsyncClient, user_a):
    res = await client.post("/api/v1/auth/login", json={"email": user_a.email, "password": "wrong"})
    assert res.status_code == 401


async def test_login_unknown_email(client: httpx.AsyncClient):
    res = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@test.dev", "password": "x"}
    )
    assert res.status_code == 401


async def test_me_with_bearer(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)
    res = await client.get("/api/v1/auth/me", headers=bearer(token))
    assert res.status_code == 200
    assert res.json()["email"] == user_a.email


async def test_me_with_cookie(client: httpx.AsyncClient, user_a, test_password):
    res = await client.post(
        "/api/v1/auth/login", json={"email": user_a.email, "password": test_password}
    )
    assert res.status_code == 200
    # httpx 클라이언트가 쿠키를 유지 → 헤더 없이 me 호출
    res = await client.get("/api/v1/auth/me")
    assert res.status_code == 200
    assert res.json()["email"] == user_a.email


async def test_me_unauthenticated(client: httpx.AsyncClient):
    res = await client.get("/api/v1/auth/me")
    assert res.status_code == 401


async def test_refresh_rotates_tokens(client: httpx.AsyncClient, user_a, test_password):
    res = await client.post(
        "/api/v1/auth/login", json={"email": user_a.email, "password": test_password}
    )
    refresh_token = res.json()["refresh_token"]
    res = await client.post("/api/v1/auth/refresh", headers=bearer(refresh_token))
    assert res.status_code == 200
    assert res.json()["access_token"]


async def test_refresh_with_access_token_rejected(client: httpx.AsyncClient, user_a):
    access = await login(client, user_a.email)
    client.cookies.clear()  # 쿠키의 refresh_token 제거 후 access로 시도
    res = await client.post("/api/v1/auth/refresh", headers=bearer(access))
    assert res.status_code == 401


async def test_logout_clears_cookies(client: httpx.AsyncClient, user_a, test_password):
    await client.post("/api/v1/auth/login", json={"email": user_a.email, "password": test_password})
    res = await client.post("/api/v1/auth/logout")
    assert res.status_code == 204
    res = await client.get("/api/v1/auth/me")
    assert res.status_code == 401
