"""관리자 「사용 제한」 코어 경로 조립(core-v0.26.0-upgrade AC-3).

코어 `limits_router`(#386·#387·#388)는 제품 `admin_router`가 조립한다. 관리자는 읽고 쓰고, 일반
사용자는 세 경로 모두 403이다(비인가). 재정의는 다음 요청부터 판정 값에 들어가고, `null`로 지우면
env(기본값)로 돌아간다.
"""

import httpx
import pytest

from tests.conftest import bearer, login

LIMIT_PATHS = (
    "/api/v1/admin/rate-limits",
    "/api/v1/admin/usage-defaults",
    "/api/v1/admin/source-upload",
)


@pytest.mark.parametrize("path", LIMIT_PATHS)
async def test_core_limit_routes_forbid_non_admin(client: httpx.AsyncClient, user_a, path):
    token = await login(client, user_a.email)

    got = await client.get(path, headers=bearer(token))
    put = await client.put(path, json={}, headers=bearer(token))

    assert got.status_code == 403, got.text
    assert put.status_code == 403, put.text


@pytest.mark.parametrize("path", LIMIT_PATHS)
async def test_core_limit_routes_open_to_admin(client: httpx.AsyncClient, admin_user, path):
    token = await login(client, admin_user.email)

    res = await client.get(path, headers=bearer(token))

    assert res.status_code == 200, res.text


@pytest.mark.parametrize("path", LIMIT_PATHS)
async def test_core_limit_routes_require_login(client: httpx.AsyncClient, path):
    res = await client.get(path)

    assert res.status_code == 401, res.text


async def test_admin_source_upload_override_reaches_public_limit(
    client: httpx.AsyncClient, admin_user, user_a
):
    """관리자가 정한 업로드 상한이 공개 조회(`/sources/upload-limits`)에 그대로 보이고, 지우면
    env로 돌아간다."""
    admin_token = await login(client, admin_user.email)
    user_token = await login(client, user_a.email)

    before = await client.get("/api/v1/sources/upload-limits", headers=bearer(user_token))
    assert before.status_code == 200, before.text
    env_value = before.json()["max_upload_bytes"]

    put = await client.put(
        "/api/v1/admin/source-upload",
        json={"max_upload_bytes": 10 * 1024 * 1024},
        headers=bearer(admin_token),
    )
    assert put.status_code == 200, put.text
    after = await client.get("/api/v1/sources/upload-limits", headers=bearer(user_token))
    assert after.json()["max_upload_bytes"] == 10 * 1024 * 1024

    cleared = await client.put(
        "/api/v1/admin/source-upload",
        json={"max_upload_bytes": None},
        headers=bearer(admin_token),
    )
    assert cleared.status_code == 200, cleared.text
    restored = await client.get("/api/v1/sources/upload-limits", headers=bearer(user_token))
    assert restored.json()["max_upload_bytes"] == env_value


async def test_admin_rate_limit_override_rejects_out_of_range(
    client: httpx.AsyncClient, admin_user
):
    """0 이하(비활성)는 env 전용이다 — 화면에서 남용 방지를 끄지 못한다(422)."""
    token = await login(client, admin_user.email)

    res = await client.put(
        "/api/v1/admin/rate-limits", json={"rate_limit_chat": 0}, headers=bearer(token)
    )

    assert res.status_code == 422, res.text


async def test_admin_rate_limit_override_round_trip(client: httpx.AsyncClient, admin_user):
    """재정의는 GET의 `override`·`effective`에 보이고, `null`로 지우면 env 값으로 돌아간다(7종 모두
    응답에 있다)."""
    token = await login(client, admin_user.email)

    def chat_item(body: dict) -> dict:
        return next(i for i in body["items"] if i["key"] == "rate_limit_chat")

    before = await client.get("/api/v1/admin/rate-limits", headers=bearer(token))
    assert before.status_code == 200, before.text
    assert len(before.json()["items"]) == 7
    env_value = chat_item(before.json())["env_value"]

    put = await client.put(
        "/api/v1/admin/rate-limits", json={"rate_limit_chat": 7}, headers=bearer(token)
    )
    assert put.status_code == 200, put.text
    after = chat_item(
        (await client.get("/api/v1/admin/rate-limits", headers=bearer(token))).json()
    )
    assert (after["override"], after["effective"]) == (7, 7)

    cleared = await client.put(
        "/api/v1/admin/rate-limits", json={"rate_limit_chat": None}, headers=bearer(token)
    )
    assert cleared.status_code == 200, cleared.text
    restored = chat_item(cleared.json())
    assert (restored["override"], restored["effective"]) == (None, env_value)


async def test_admin_usage_defaults_round_trip(client: httpx.AsyncClient, admin_user):
    """부분 갱신 — 보낸 필드만 바뀌고, `null`은 기본값을 지운다(무제한)."""
    token = await login(client, admin_user.email)

    put = await client.put(
        "/api/v1/admin/usage-defaults",
        json={"daily_tokens": 50_000, "daily_agent_runs": 3},
        headers=bearer(token),
    )
    assert put.status_code == 200, put.text
    got = (await client.get("/api/v1/admin/usage-defaults", headers=bearer(token))).json()
    assert (got["daily_tokens"], got["daily_agent_runs"]) == (50_000, 3)

    partial = await client.put(
        "/api/v1/admin/usage-defaults", json={"daily_tokens": None}, headers=bearer(token)
    )
    assert partial.status_code == 200, partial.text
    assert (partial.json()["daily_tokens"], partial.json()["daily_agent_runs"]) == (None, 3)
