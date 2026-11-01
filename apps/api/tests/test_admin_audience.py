"""관리자 전용 system 정의(`audience: "admin"`)와 v0.29.0 저장 검증.

core-v0.30.0-upgrade AC-3~AC-5.

코어 v0.29.0 #436: system scope 정의의 최상위 `audience`가 있으면 비관리자에게는 없는 정의와 같다.
소유자 예외가 없다(강등된 소유자도 못 본다). 제품 목록·상세·callable이 같은 규칙을 따르는지,
관리자가 사용자 편집기로 저장해도 키가 남는지(#436 ②), 저장 검증이 잘못된 `audience`·
`reasoning_effort`·`min_output_tokens`를 422로 막는지(#436 ③·#429·#428) 본다.
"""

import uuid

import httpx
import pytest
from biznotes_core.models import Agent

from tests.conftest import bearer, login

_DEF = {"nodes": [{"id": "gen", "type": "llm", "prompt": "{params.topic}"}], "edges": []}


async def _system_workflow(db, *, owner=None, audience: object = "admin") -> Agent:
    definition: dict = {**_DEF}
    if audience is not None:
        definition["audience"] = audience
    agent = Agent(
        name=f"sys-{uuid.uuid4().hex[:8]}",
        kind="workflow",
        definition=definition,
        params_schema={"type": "object", "properties": {}},
        scope="system",
        owner_id=owner.id if owner else None,
        enabled=True,
        version=1,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


async def _ids(client: httpx.AsyncClient, token: str, path: str) -> set[str]:
    res = await client.get(path, headers=bearer(token))
    assert res.status_code == 200, res.text
    return {row["id"] for row in res.json()}


@pytest.mark.parametrize(
    "path", ["/api/v1/agents", "/api/v1/workflows", "/api/v1/workflows/callable"]
)
async def test_admin_only_system_definition_hidden_from_non_admin_lists(
    client: httpx.AsyncClient, db, user_a, admin_user, path: str
):
    hidden = await _system_workflow(db)
    public = await _system_workflow(db, audience=None)

    user_ids = await _ids(client, await login(client, user_a.email), path)
    assert str(hidden.id) not in user_ids
    assert str(public.id) in user_ids

    admin_ids = await _ids(client, await login(client, admin_user.email), path)
    assert {str(hidden.id), str(public.id)} <= admin_ids


async def test_admin_only_definition_has_no_owner_exception(client: httpx.AsyncClient, db, user_a):
    """owner_id가 남은 관리자 전용 정의 — 강등된 소유자도 목록·상세·복제에서 404다(비인가)."""
    agent = await _system_workflow(db, owner=user_a)
    token = await login(client, user_a.email)

    assert str(agent.id) not in await _ids(client, token, "/api/v1/workflows")
    assert str(agent.id) not in await _ids(client, token, "/api/v1/agents")
    res = await client.get(f"/api/v1/workflows/{agent.id}", headers=bearer(token))
    assert res.status_code == 404
    res = await client.post(f"/api/v1/workflows/{agent.id}/duplicate", headers=bearer(token))
    assert res.status_code == 404


async def test_admin_detail_sees_admin_only_definition(client: httpx.AsyncClient, db, admin_user):
    agent = await _system_workflow(db)
    token = await login(client, admin_user.email)
    res = await client.get(f"/api/v1/workflows/{agent.id}", headers=bearer(token))
    assert res.status_code == 200
    assert res.json()["definition"]["audience"] == "admin"


async def test_admin_editor_save_keeps_top_level_keys(client: httpx.AsyncClient, db, admin_user):
    """#436 ② — 사용자 편집기 PATCH가 `audience`·`output`을 버리면 모두에게 열린다(fail-open)."""
    agent = await _system_workflow(db)
    token = await login(client, admin_user.email)
    definition = {**_DEF, "audience": "admin", "output": "{gen.text}"}
    res = await client.patch(
        f"/api/v1/workflows/{agent.id}",
        json={"definition": definition},
        headers=bearer(token),
    )
    assert res.status_code == 200, res.text
    await db.refresh(agent)
    assert agent.definition["audience"] == "admin"
    assert agent.definition["output"] == "{gen.text}"


_BAD_DEFINITIONS = [
    pytest.param({**_DEF, "audience": "everyone"}, "audience", id="audience"),
    pytest.param(
        {"nodes": [{"id": "gen", "type": "llm", "prompt": "p", "reasoning_effort": "max"}]},
        "reasoning_effort",
        id="reasoning_effort",
    ),
    pytest.param(
        {
            "nodes": [
                {
                    "id": "gen",
                    "type": "llm",
                    "prompt": "p",
                    "max_tokens": 512,
                    "min_output_tokens": 1024,
                }
            ]
        },
        "min_output_tokens",
        id="min_output_tokens",
    ),
]


@pytest.mark.parametrize(("definition", "needle"), _BAD_DEFINITIONS)
async def test_user_workflow_save_rejects_invalid_core_keys(
    client: httpx.AsyncClient, user_a, definition: dict, needle: str
):
    token = await login(client, user_a.email)
    res = await client.post(
        "/api/v1/workflows",
        json={"name": "bad", "definition": {"edges": [], **definition}},
        headers=bearer(token),
    )
    assert res.status_code == 422, res.text
    assert needle in res.text


@pytest.mark.parametrize(("definition", "needle"), _BAD_DEFINITIONS)
async def test_admin_agent_save_rejects_invalid_core_keys(
    client: httpx.AsyncClient, admin_user, definition: dict, needle: str
):
    token = await login(client, admin_user.email)
    res = await client.post(
        "/api/v1/admin/agents",
        json={
            "name": f"adm-{uuid.uuid4().hex[:6]}",
            "kind": "workflow",
            "scope": "system",
            "definition": {"edges": [], **definition},
        },
        headers=bearer(token),
    )
    assert res.status_code == 422, res.text
    assert needle in res.text


async def test_admin_agent_save_accepts_admin_audience_and_valid_keys(
    client: httpx.AsyncClient, admin_user
):
    token = await login(client, admin_user.email)
    definition = {
        "audience": "admin",
        "nodes": [
            {
                "id": "gen",
                "type": "llm",
                "prompt": "p",
                "max_tokens": 4096,
                "reasoning_effort": "low",
                "min_output_tokens": 1024,
            }
        ],
        "edges": [],
    }
    res = await client.post(
        "/api/v1/admin/agents",
        json={
            "name": f"adm-{uuid.uuid4().hex[:6]}",
            "kind": "workflow",
            "scope": "system",
            "definition": definition,
        },
        headers=bearer(token),
    )
    assert res.status_code in (200, 201), res.text


async def test_non_admin_owner_cannot_edit_system_definition(client: httpx.AsyncClient, db, user_a):
    """공개 system 정의에 비관리자 owner_id가 남아 있어도 PATCH·게시 취소는 403이고 정의는 그대로다.

    독립 리뷰 High 1 — 편집을 허용하면 `audience: "admin"`을 붙여 다른 사용자에게서 숨길 수 있다.
    """
    agent = await _system_workflow(db, owner=user_a, audience=None)
    token = await login(client, user_a.email)

    res = await client.get(f"/api/v1/workflows/{agent.id}", headers=bearer(token))
    assert res.status_code == 200
    assert res.json()["can_edit"] is False

    res = await client.patch(
        f"/api/v1/workflows/{agent.id}",
        json={"definition": {**_DEF, "audience": "admin"}},
        headers=bearer(token),
    )
    assert res.status_code == 403, res.text
    res = await client.post(f"/api/v1/workflows/{agent.id}/unpublish", headers=bearer(token))
    assert res.status_code == 403, res.text
    await db.refresh(agent)
    assert "audience" not in agent.definition
    assert agent.enabled is True
