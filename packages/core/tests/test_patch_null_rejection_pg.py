# 신규(#267). 필요: PostgreSQL·Valkey(PG 테스트).
"""노트북·스킬·관리자 LLM 모델 PATCH의 명시적 null — 실제 라우터·인증·DB.

NOT NULL 열 필드의 null은 422이고 DB 행이 그대로다(수정 전에는 커밋 때 무결성 오류였다).
null이 뜻을 갖는 필드(스킬 `visibility`의 생략 취급, LLM 모델 해제·정규화)는 실제로 커밋된다.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI

from biznotes_core.api.deps import require_admin
from biznotes_core.api.v1 import auth, notebooks, skills
from biznotes_core.api.v1.admin import llm
from biznotes_core.core.db import get_session_factory
from biznotes_core.models import LLMModel, LLMProvider, Notebook, Skill, User

PASSWORD = "pw-test-1234"  # tests/conftest.py 공용 해시의 평문


def _app() -> FastAPI:
    app = FastAPI()
    admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
    admin.include_router(llm.router)
    for router in (auth.router, notebooks.router, skills.router, admin):
        app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _patch(client: httpx.AsyncClient, user: User, path: str, body: Any) -> httpx.Response:
    client.cookies.clear()
    res = await client.post("/api/v1/auth/login", json={"email": user.email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    client.cookies.clear()
    headers = {"Authorization": f"Bearer {res.json()['access_token']}"}
    return await client.patch(f"/api/v1{path}", json=body, headers=headers)


async def _fresh(model: type, key: uuid.UUID) -> Any:
    async with get_session_factory()() as s:
        return await s.get(model, key)


async def test_notebook_null_is_422_and_row_is_unchanged(
    client: httpx.AsyncClient, user_a: User
) -> None:
    async with get_session_factory()() as s:
        nb = Notebook(owner_id=user_a.id, title="제목", description="설명")
        s.add(nb)
        await s.commit()

    res = await _patch(
        client, user_a, f"/notebooks/{nb.id}", {"title": None, "description": "바뀐 설명"}
    )

    assert res.status_code == 422
    assert res.json() == {"detail": "null로 바꿀 수 없는 필드: title"}
    row = await _fresh(Notebook, nb.id)
    assert (row.title, row.description) == ("제목", "설명")


async def test_skill_null_is_422_and_visibility_null_is_omitted(
    client: httpx.AsyncClient, user_a: User
) -> None:
    async with get_session_factory()() as s:
        skill = Skill(owner_id=user_a.id, name="스킬", description="설명", slug="s", tags=["a"])
        s.add(skill)
        await s.commit()

    rejected = await _patch(
        client, user_a, f"/skills/{skill.id}", {"visibility": "org", "tags": None}
    )
    assert rejected.status_code == 422
    assert rejected.json() == {"detail": "null로 바꿀 수 없는 필드: tags"}
    row = await _fresh(Skill, skill.id)
    assert (row.tags, row.visibility) == (["a"], "private")

    # visibility null은 지금처럼 생략과 같다 — 함께 보낸 필드만 적용한다
    applied = await _patch(
        client, user_a, f"/skills/{skill.id}", {"visibility": None, "name": "새 이름"}
    )
    assert applied.status_code == 200, applied.text
    row = await _fresh(Skill, skill.id)
    assert (row.name, row.visibility) == ("새 이름", "private")


async def test_llm_model_null_is_422_and_nullable_fields_are_cleared(
    client: httpx.AsyncClient, admin_user: User
) -> None:
    async with get_session_factory()() as s:
        provider = LLMProvider(name=f"p-{uuid.uuid4().hex[:6]}", type="openai")
        s.add(provider)
        await s.flush()
        model = LLMModel(
            provider_id=provider.id,
            model_name="m",
            display_name="모델",
            capabilities=["llm"],
            context_window=8192,
            fallback_priority=1,
            cost_per_1k={"input": 0.1},
        )
        s.add(model)
        await s.commit()
    path = f"/admin/llm/models/{model.id}"

    rejected = await _patch(client, admin_user, path, {"enabled": None, "context_window": None})
    assert rejected.status_code == 422
    assert rejected.json() == {"detail": "null로 바꿀 수 없는 필드: enabled"}
    row = await _fresh(LLMModel, model.id)
    assert (row.enabled, row.context_window) == (True, 8192)

    cleared = await _patch(
        client,
        admin_user,
        path,
        {"context_window": None, "fallback_priority": None, "cost_per_1k": None},
    )
    assert cleared.status_code == 200, cleared.text
    row = await _fresh(LLMModel, model.id)
    assert (row.context_window, row.fallback_priority, row.cost_per_1k) == (None, None, {})
