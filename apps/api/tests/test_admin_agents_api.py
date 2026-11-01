"""어드민 에이전트 저장 경로의 직접 노드 승인 검증 — 코어 v0.10.15 #153 A.

core-v0.11.0-upgrade AC-2.1 · 권한 표 E-B1 · HCI-7 ②.

`POST/PATCH /api/v1/admin/agents`의 `_validate_definition`이 `kind_to_nodes` 결과로
`find_direct_node_approval_violations(nodes, edges, agent_scope)`를 부르고, 위반을 기존 problems
422에 합친다. 판정 함수를 대체하지 않는다 — 라우터 → 검증기 → 코어 규칙을 실제로 지난다.
`agent_scope`는 생성이면 `body.scope`, 수정이면 저장된 `agent.scope`다(`AgentUpdate`에 scope 없음).

kind별 위치: tool·plugin은 `kind_to_nodes`가 `definition["http"]`를 노드로 펼치므로
`requires_approval`이 `http` 안에, mcp는 정의 최상위에 있다(코어 `graph.py`).
"""

import uuid

import httpx
import pytest

from tests.conftest import bearer, login

_LITERAL_URL = "https://api.example.com/v1/rates?base={params.base}"
_TEMPLATE_URL = "{params.webhook_url}"


def _tool_def(url: str = _LITERAL_URL, **http_extra) -> dict:
    return {"http": {"url": url, "method": "GET", **http_extra}, "artifact_name": "r.json"}


def _mcp_def(**extra) -> dict:
    return {"server_id": str(uuid.uuid4()), "tool_name": "word_count", "arguments": {}, **extra}


async def _create(client: httpx.AsyncClient, token: str, kind: str, definition: dict, **body):
    return await client.post(
        "/api/v1/admin/agents",
        json={
            "name": f"adm-{uuid.uuid4().hex[:6]}",
            "kind": kind,
            "definition": definition,
            **body,
        },
        headers=bearer(token),
    )


@pytest.mark.parametrize(
    ("kind", "definition", "scope", "expected"),
    [
        # user scope 정의는 관리자가 만들어도 false를 둘 수 없다
        (
            "workflow",
            {
                "nodes": [
                    {
                        "id": "h",
                        "type": "http",
                        "url": _LITERAL_URL,
                        "requires_approval": False,
                    }
                ],
                "edges": [],
            },
            "user",
            "h: requires_approval=false는 system 정의에서만",
        ),
        # system이어도 url 대상(호스트)이 템플릿이면 false 불가(「[샘플] Webhook 전송」과 같은 모양)
        (
            "plugin",
            _tool_def(_TEMPLATE_URL, requires_approval=False),
            "system",
            "call: 승인 없는 http 노드의 url 대상",
        ),
        # 문자열 "false"는 bool이 아니다
        ("tool", _tool_def(requires_approval="false"), "system", "requires_approval은 true/false"),
        # mcp kind — group scope는 신뢰 집합 밖
        (
            "mcp",
            _mcp_def(requires_approval=False),
            "group",
            "call: requires_approval=false는 system 정의에서만",
        ),
        # fan-out 그래프 안의 게이트 노드(승인 기본값)
        (
            "workflow",
            {
                "nodes": [
                    {"id": "a", "type": "constant", "value": "x"},
                    {"id": "h", "type": "http", "url": _LITERAL_URL},
                    {"id": "b", "type": "constant", "value": "y"},
                ],
                "edges": [["a", "h"], ["a", "b"]],
            },
            "system",
            "h: 승인이 필요한 노드는 병렬(fan-out)",
        ),
    ],
    ids=["user-scope-false", "system-template-host", "string-false", "group-mcp-false", "fan-out"],
)
async def test_admin_direct_node_violation_rejected(
    client: httpx.AsyncClient, admin_user, db, kind, definition, scope, expected
):
    """생성 경로: 위반 정의는 422이고 저장되지 않는다."""
    from biznotes_core.models import Agent
    from sqlalchemy import func, select

    token = await login(client, admin_user.email)
    before = await db.scalar(select(func.count()).select_from(Agent))
    res = await _create(client, token, kind, definition, scope=scope)
    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert "직접 노드 승인 설정을 저장할 수 없습니다" in detail
    assert expected in detail
    assert await db.scalar(select(func.count()).select_from(Agent)) == before


async def test_admin_patch_validates_with_stored_scope(client: httpx.AsyncClient, admin_user):
    """수정 경로: 저장된 `agent.scope`로 판정한다 — system은 리터럴 false 200, 템플릿 host 422."""
    token = await login(client, admin_user.email)
    res = await _create(client, token, "tool", _tool_def(), scope="system")
    assert res.status_code == 201, res.text
    agent_id = res.json()["id"]

    ok = await client.patch(
        f"/api/v1/admin/agents/{agent_id}",
        json={"definition": _tool_def(requires_approval=False)},
        headers=bearer(token),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["definition"]["http"]["requires_approval"] is False

    bad = await client.patch(
        f"/api/v1/admin/agents/{agent_id}",
        json={"definition": _tool_def(_TEMPLATE_URL, requires_approval=False)},
        headers=bearer(token),
    )
    assert bad.status_code == 422, bad.text
    assert "url 대상" in bad.json()["detail"]

    # user scope로 저장된 정의를 false로 고치는 것은 거부(scope는 PATCH로 바꿀 수 없다)
    res = await _create(client, token, "tool", _tool_def(), scope="user")
    assert res.status_code == 201, res.text
    user_scoped = await client.patch(
        f"/api/v1/admin/agents/{res.json()['id']}",
        json={"definition": _tool_def(requires_approval=False)},
        headers=bearer(token),
    )
    assert user_scoped.status_code == 422, user_scoped.text
    assert "system 정의에서만" in user_scoped.json()["detail"]


@pytest.mark.parametrize(
    ("kind", "definition"),
    [
        ("tool", _tool_def(requires_approval=False)),
        ("plugin", _tool_def(_TEMPLATE_URL)),  # 템플릿 host라도 승인 기본값이면 저장 가능
        ("mcp", _mcp_def(requires_approval=False)),
        ("mcp", _mcp_def()),
    ],
    ids=["tool-literal-false", "plugin-template-gated", "mcp-false", "mcp-default"],
)
async def test_admin_direct_node_valid_definitions_pass(
    client: httpx.AsyncClient, admin_user, kind, definition
):
    """대조: system 정의의 리터럴 false·승인 기본값은 201(실행 시점 게이트는 런타임이 정한다)."""
    token = await login(client, admin_user.email)
    res = await _create(client, token, kind, definition, scope="system")
    assert res.status_code == 201, res.text


async def test_admin_agents_write_requires_admin(client: httpx.AsyncClient, user_a, admin_user):
    """비인가 관점(E-B1): 일반 사용자의 생성·수정은 검증 이전에 403 — 위반 문구를 내지 않는다."""
    admin_token = await login(client, admin_user.email)
    created = await _create(client, admin_token, "tool", _tool_def(), scope="system")
    assert created.status_code == 201, created.text

    token = await login(client, user_a.email)
    res = await _create(client, token, "tool", _tool_def(requires_approval=False), scope="system")
    assert res.status_code == 403, res.text
    res = await client.patch(
        f"/api/v1/admin/agents/{created.json()['id']}",
        json={"definition": _tool_def(_TEMPLATE_URL, requires_approval=False)},
        headers=bearer(token),
    )
    assert res.status_code == 403, res.text
    assert "직접 노드" not in res.text
    res = await client.post(
        "/api/v1/admin/agents",
        json={"name": "x", "kind": "tool", "definition": _tool_def()},
    )
    assert res.status_code == 401, res.text
