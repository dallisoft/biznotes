"""image_generate 노드 저장 — 프롬프트 출처 검사(core-v0.23.0-upgrade AC-5·D2).

`POST/PATCH /api/v1/workflows`의 `_validate_structure`와 `POST/PATCH /api/v1/admin/agents`의
`_validate_definition`이 `find_unsafe_image_prompts`를 거쳐 코어 `prompt_provenance` 규칙을 건다.
판정 함수를 대체하지 않는다 — 라우터 → 검증기 → 코어 규칙을 실제로 지난다.

- 안전: llm·constant 출력, 그 출력을 source로 둔 json_validate 출력, params.
- 불안전: rag_search·source_load·http 출력, `|untrusted`, 없는 노드.
- 노드 타입 허용 목록은 제품에 없다 — 안전한 정의는 그대로 저장된다(201).
"""

import uuid

import httpx
import pytest

from tests.conftest import bearer, login


def _definition(prompt: str, *, extra_nodes: list[dict] | None = None) -> dict:
    nodes = [
        {"id": "search", "type": "rag_search", "query": "{params.topic}"},
        {"id": "desc", "type": "llm", "prompt": "그림 묘사: {search.context}"},
        {
            "id": "check",
            "type": "json_validate",
            "schema": "slide_deck",
            "source": "{desc.text}",
        },
        *(extra_nodes or []),
        {"id": "art", "type": "image_generate", "prompt": prompt, "aspect_ratio": "16:9"},
    ]
    return {
        "nodes": nodes,
        "edges": [["search", "desc"], ["desc", "check"], ["check", "art"]],
    }


def _create_body(prompt: str) -> dict:
    return {"name": f"img-{uuid.uuid4().hex[:6]}", "definition": _definition(prompt)}


SAFE_PROMPTS = [
    "{desc.text}",
    "{check.text}",
    "{params.topic} 표지 삽화",
    "고정 프롬프트",
]
UNSAFE_PROMPTS = [
    ("{search.context}", "search"),
    ("{search.citations}", "search"),
    ("{desc.text|untrusted}", "|untrusted"),
    ("{ghost.text}", "ghost"),
]


@pytest.mark.parametrize("prompt", SAFE_PROMPTS, ids=["llm", "json_validate", "params", "const"])
async def test_user_workflow_with_safe_image_prompt_saves(
    client: httpx.AsyncClient, user_a, prompt
):
    token = await login(client, user_a.email)
    res = await client.post("/api/v1/workflows", json=_create_body(prompt), headers=bearer(token))
    assert res.status_code == 201, res.text
    [art] = [n for n in res.json()["definition"]["nodes"] if n["id"] == "art"]
    assert art["type"] == "image_generate"
    assert art["prompt"] == prompt


@pytest.mark.parametrize(
    ("prompt", "expected"), UNSAFE_PROMPTS, ids=["search", "citations", "untrusted", "missing"]
)
async def test_user_workflow_with_unsafe_image_prompt_rejected(
    client: httpx.AsyncClient, user_a, db, prompt, expected
):
    from biznotes_core.models import Agent
    from sqlalchemy import func, select

    token = await login(client, user_a.email)
    before = await db.scalar(select(func.count()).select_from(Agent))
    res = await client.post("/api/v1/workflows", json=_create_body(prompt), headers=bearer(token))
    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert "image_generate art" in detail
    assert expected in detail
    assert await db.scalar(select(func.count()).select_from(Agent)) == before


async def test_unsafe_source_through_json_validate_rejected(client: httpx.AsyncClient, user_a):
    """json_validate는 source의 뿌리가 안전할 때만 안전하다 — http 원문을 검증한 출력은 거부."""
    token = await login(client, user_a.email)
    definition = {
        "nodes": [
            {"id": "fetch", "type": "http", "url": "https://example.com/a.json"},
            {
                "id": "check",
                "type": "json_validate",
                "schema": "slide_deck",
                "source": "{fetch.text}",
            },
            {"id": "art", "type": "image_generate", "prompt": "{check.text}"},
        ],
        "edges": [["fetch", "check"], ["check", "art"]],
    }
    res = await client.post(
        "/api/v1/workflows",
        json={"name": "img-http", "definition": definition},
        headers=bearer(token),
    )
    assert res.status_code == 422, res.text
    assert "check" in res.json()["detail"]


async def test_user_patch_rejects_unsafe_image_prompt(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)
    created = await client.post(
        "/api/v1/workflows", json=_create_body("{desc.text}"), headers=bearer(token)
    )
    assert created.status_code == 201, created.text
    wf_id = created.json()["id"]

    bad = await client.patch(
        f"/api/v1/workflows/{wf_id}",
        json={"definition": _definition("{search.context}")},
        headers=bearer(token),
    )
    assert bad.status_code == 422, bad.text
    assert "image_generate art" in bad.json()["detail"]
    fetched = await client.get(f"/api/v1/workflows/{wf_id}", headers=bearer(token))
    [art] = [n for n in fetched.json()["definition"]["nodes"] if n["id"] == "art"]
    assert art["prompt"] == "{desc.text}"


async def test_other_user_patch_is_denied_before_validation(
    client: httpx.AsyncClient, user_a, user_b
):
    """비인가 관점: 타인 워크플로우 수정은 검증 이전에 거부되고 위반 문구를 내지 않는다."""
    owner = await login(client, user_a.email)
    created = await client.post(
        "/api/v1/workflows", json=_create_body("{desc.text}"), headers=bearer(owner)
    )
    assert created.status_code == 201, created.text

    other = await login(client, user_b.email)
    res = await client.patch(
        f"/api/v1/workflows/{created.json()['id']}",
        json={"definition": _definition("{search.context}")},
        headers=bearer(other),
    )
    assert res.status_code in (403, 404), res.text
    assert "image_generate" not in res.text


async def test_admin_agent_image_prompt_policy(client: httpx.AsyncClient, admin_user):
    """관리자 경로도 같은 규칙 — system 정의라도 비신뢰 출처는 422, 안전하면 201."""
    token = await login(client, admin_user.email)

    async def create(prompt: str) -> httpx.Response:
        return await client.post(
            "/api/v1/admin/agents",
            json={
                "name": f"adm-img-{uuid.uuid4().hex[:6]}",
                "kind": "workflow",
                "definition": _definition(prompt),
                "scope": "system",
            },
            headers=bearer(token),
        )

    bad = await create("{search.citations}")
    assert bad.status_code == 422, bad.text
    assert "image_generate art" in bad.json()["detail"]

    ok = await create("{desc.text}")
    assert ok.status_code == 201, ok.text
    patched = await client.patch(
        f"/api/v1/admin/agents/{ok.json()['id']}",
        json={"definition": _definition("{search.context|untrusted}")},
        headers=bearer(token),
    )
    assert patched.status_code == 422, patched.text
    assert "|untrusted" in patched.json()["detail"]
