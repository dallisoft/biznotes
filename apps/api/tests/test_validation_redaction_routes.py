"""비밀 값이 실릴 수 있는 제품 라우터의 422 입력값 마스킹 (core-v0.12.0-upgrade AC-2.0, 코어 #173).

필수 필드가 빠지면 FastAPI 기본 422는 요청 본문 전체를 `input`에 싣는다 — 그 경로로 sentinel이
되돌아오지 않는지 본다. dict 필드의 키는 `loc`에 남는다(코어 문서화 한계 — env 변수명).
"""

import httpx

from tests.conftest import bearer, login

SENTINEL = "sk-redaction-sentinel-7f3a"


def _assert_redacted(res: httpx.Response) -> None:
    assert res.status_code == 422, res.text
    assert SENTINEL not in res.text
    detail = res.json()["detail"]
    assert isinstance(detail, list) and detail
    for error in detail:
        assert "input" not in error and "ctx" not in error


async def test_mcp_server_create_missing_field_hides_env(client: httpx.AsyncClient, admin_user):
    token = await login(client, admin_user.email)
    res = await client.post(
        "/api/v1/admin/mcp-servers",
        json={"transport": "stdio", "endpoint_or_command": "echo", "env": {"API_KEY": SENTINEL}},
        headers=bearer(token),
    )
    _assert_redacted(res)


async def test_mcp_server_update_bad_type_hides_env(client: httpx.AsyncClient, admin_user):
    token = await login(client, admin_user.email)
    created = await client.post(
        "/api/v1/admin/mcp-servers",
        json={"name": "redaction-mcp", "transport": "stdio", "endpoint_or_command": "echo"},
        headers=bearer(token),
    )
    assert created.status_code == 201, created.text

    res = await client.patch(
        f"/api/v1/admin/mcp-servers/{created.json()['id']}",
        json={"env": {"API_KEY": SENTINEL}, "enabled": "notbool"},
        headers=bearer(token),
    )
    _assert_redacted(res)


async def test_mcp_server_write_requires_admin(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)
    res = await client.post(
        "/api/v1/admin/mcp-servers",
        json={"name": "x", "transport": "stdio", "endpoint_or_command": "echo"},
        headers=bearer(token),
    )
    assert res.status_code == 403


async def test_mcp_server_update_requires_admin(
    client: httpx.AsyncClient, admin_user, user_a
):
    admin_token = await login(client, admin_user.email)
    created = await client.post(
        "/api/v1/admin/mcp-servers",
        json={"name": "redaction-mcp-403", "transport": "stdio", "endpoint_or_command": "echo"},
        headers=bearer(admin_token),
    )
    assert created.status_code == 201, created.text
    token = await login(client, user_a.email)
    res = await client.patch(
        f"/api/v1/admin/mcp-servers/{created.json()['id']}",
        json={"enabled": False},
        headers=bearer(token),
    )
    assert res.status_code == 403


async def test_admin_agent_create_missing_field_hides_definition(
    client: httpx.AsyncClient, admin_user
):
    token = await login(client, admin_user.email)
    definition = {
        "nodes": [{"id": "h", "type": "http", "headers": {"Authorization": SENTINEL}}],
        "edges": [],
    }
    res = await client.post(
        "/api/v1/admin/agents",
        json={"name": "redaction-agent", "definition": definition},
        headers=bearer(token),
    )
    _assert_redacted(res)


async def test_admin_agent_create_requires_admin(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)
    res = await client.post(
        "/api/v1/admin/agents",
        json={"name": "x", "kind": "workflow", "definition": {}},
        headers=bearer(token),
    )
    assert res.status_code == 403


async def test_workflow_create_invalid_node_hides_definition(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)
    res = await client.post(
        "/api/v1/workflows",
        json={"definition": {"nodes": [{"id": "h", "headers": {"Authorization": SENTINEL}}]}},
        headers=bearer(token),
    )
    _assert_redacted(res)


async def test_workflow_create_requires_login(client: httpx.AsyncClient):
    res = await client.post("/api/v1/workflows", json={})
    assert res.status_code == 401


async def test_reindex_active_only_requires_admin(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)
    res = await client.get("/api/v1/admin/rag/reindex?active_only=true", headers=bearer(token))
    assert res.status_code == 403
