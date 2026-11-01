# 신규(#437). 필요: PostgreSQL·Valkey(PG 테스트).
"""관리자 LLM 모델의 Integer 열 범위 — 실제 라우터·인증·DB.

`context_window`·`fallback_priority`는 int4 열이라 -2147483648~2147483647이 저장·조회된다.
그 밖의 값은 요청 검증에서 422이고 DB 행은 그대로다(수정 전에는 저장 때 범위 오류로 500이었다).
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from biznotes_core.api.deps import require_admin
from biznotes_core.api.v1 import auth
from biznotes_core.api.v1.admin import llm
from biznotes_core.core.db import get_session_factory
from biznotes_core.models import LLMModel, LLMProvider, User

PASSWORD = "pw-test-1234"  # tests/conftest.py 공용 해시의 평문
INT4_MAX = 2_147_483_647
INT4_MIN = -2_147_483_648
TOO_BIG = INT4_MAX + 1
TOO_SMALL = INT4_MIN - 1


def _app() -> FastAPI:
    app = FastAPI()
    admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
    admin.include_router(llm.router)
    for router in (auth.router, admin):
        app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _send(
    client: httpx.AsyncClient, user: User, method: str, path: str, body: Any
) -> httpx.Response:
    client.cookies.clear()
    res = await client.post("/api/v1/auth/login", json={"email": user.email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    client.cookies.clear()
    headers = {"Authorization": f"Bearer {res.json()['access_token']}"}
    return await client.request(method, f"/api/v1{path}", json=body, headers=headers)


async def _seed() -> tuple[LLMProvider, LLMModel]:
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
        )
        s.add(model)
        await s.commit()
    return provider, model


async def _provider_models(provider_id: uuid.UUID) -> dict[str, LLMModel]:
    async with get_session_factory()() as s:
        rows = await s.execute(select(LLMModel).where(LLMModel.provider_id == provider_id))
        return {m.model_name: m for m in rows.scalars()}


@pytest.mark.parametrize("bad", [TOO_BIG, TOO_SMALL])
async def test_int4_column_rejects_out_of_range(bad: int) -> None:
    """스키마 상·하한의 근거 — 열이 int4라 범위 밖 값은 저장 단계에서 실패한다."""
    provider, _model = await _seed()

    async with get_session_factory()() as s:
        s.add(
            LLMModel(
                provider_id=provider.id,
                model_name="out-of-range",
                display_name="범위 밖",
                context_window=bad,
            )
        )
        with pytest.raises(DBAPIError):
            await s.commit()

    assert "out-of-range" not in await _provider_models(provider.id)


@pytest.mark.parametrize("value", [INT4_MAX, INT4_MIN])
async def test_int4_bounds_round_trip_through_create_update_import(
    client: httpx.AsyncClient, admin_user: User, value: int
) -> None:
    provider, model = await _seed()

    created = await _send(
        client,
        admin_user,
        "POST",
        "/admin/llm/models",
        {
            "provider_id": str(provider.id),
            "model_name": "created",
            "display_name": "생성",
            "context_window": value,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["context_window"] == value

    updated = await _send(
        client,
        admin_user,
        "PATCH",
        f"/admin/llm/models/{model.id}",
        {"context_window": value, "fallback_priority": value},
    )
    assert updated.status_code == 200, updated.text
    assert (updated.json()["context_window"], updated.json()["fallback_priority"]) == (
        value,
        value,
    )

    imported = await _send(
        client,
        admin_user,
        "POST",
        f"/admin/llm/providers/{provider.id}/models/import",
        {"items": [{"model_name": "imported", "display_name": "가져옴", "context_window": value}]},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json() == {"created": 1, "skipped": []}

    rows = await _provider_models(provider.id)
    assert rows["created"].context_window == value
    assert (rows["m"].context_window, rows["m"].fallback_priority) == (value, value)
    assert rows["imported"].context_window == value


@pytest.mark.parametrize(("ok", "bad"), [(INT4_MAX, TOO_BIG), (INT4_MIN, TOO_SMALL)])
async def test_beyond_int4_is_422_and_rows_are_unchanged(
    client: httpx.AsyncClient, admin_user: User, ok: int, bad: int
) -> None:
    provider, model = await _seed()
    path = f"/admin/llm/models/{model.id}"

    responses = [
        await _send(
            client,
            admin_user,
            "POST",
            "/admin/llm/models",
            {
                "provider_id": str(provider.id),
                "model_name": "created",
                "display_name": "생성",
                "context_window": bad,
            },
        ),
        await _send(
            client,
            admin_user,
            "PATCH",
            path,
            {"display_name": "바뀐 이름", "context_window": bad},
        ),
        await _send(
            client,
            admin_user,
            "PATCH",
            path,
            {"display_name": "바뀐 이름", "fallback_priority": bad},
        ),
        # 범위 안 항목과 섞여도 배치 전체를 거부한다
        await _send(
            client,
            admin_user,
            "POST",
            f"/admin/llm/providers/{provider.id}/models/import",
            {
                "items": [
                    {"model_name": "ok", "display_name": "범위 안", "context_window": ok},
                    {"model_name": "big", "display_name": "범위 밖", "context_window": bad},
                ]
            },
        ),
    ]

    for res in responses:
        assert res.status_code == 422, res.text
        assert str(bad) not in res.text
    rows = await _provider_models(provider.id)
    assert set(rows) == {"m"}
    assert (rows["m"].display_name, rows["m"].context_window, rows["m"].fallback_priority) == (
        "모델",
        8192,
        1,
    )
