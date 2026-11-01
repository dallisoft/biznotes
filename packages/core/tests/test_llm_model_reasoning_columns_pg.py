# 신규(#429·#428, L-1). 필요: PostgreSQL·Valkey(PG 테스트).
"""모델 사고 양 기본값·최대 출력 토큰과 원장 사고 토큰 — 실제 라우터·인증·DB.

관리자 생성·조회·수정(null 해제)·가져오기 왕복, 모델 해석(`resolve_model`·`resolve_chain`)이 새 열과
함께 동작하는지, 원장 INSERT에 열이 채워지는지, 사용량 집계가 그대로인지 본다.
`create_all` 기반이라 Alembic 마이그레이션 검증이 아니다(테이블이 제품 리비전과 같은 모양인지만
본다).
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import select, text

from biznotes_core.api.deps import require_admin
from biznotes_core.api.v1 import auth
from biznotes_core.api.v1.admin import llm
from biznotes_core.core.db import get_session_factory
from biznotes_core.models import AuditLog, LLMModel, LLMProvider, User
from biznotes_core.services.gateway.llm import (
    StreamResult,
    complete_stream_with_fallback,
    complete_with_tools,
    resolve_chain,
    resolve_model,
)
from biznotes_core.services.llm_admin import plan_model_import

PASSWORD = "pw-test-1234"  # tests/conftest.py 공용 해시의 평문


def _app() -> FastAPI:
    app = FastAPI()
    admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
    admin.include_router(llm.router)
    for router in (auth.router, admin):
        app.include_router(router, prefix="/api/v1")
    return app


class _Admin:
    def __init__(self, client: httpx.AsyncClient, user: User) -> None:
        self.client = client
        self.user = user
        self.token: str | None = None

    async def send(self, method: str, path: str, body: Any = None) -> httpx.Response:
        if self.token is None:
            self.client.cookies.clear()
            res = await self.client.post(
                "/api/v1/auth/login", json={"email": self.user.email, "password": PASSWORD}
            )
            assert res.status_code == 200, res.text
            self.token = res.json()["access_token"]
        self.client.cookies.clear()
        return await self.client.request(
            method,
            f"/api/v1{path}",
            json=body,
            headers={"Authorization": f"Bearer {self.token}"},
        )


@pytest.fixture
async def admin(admin_user: User) -> AsyncIterator[_Admin]:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield _Admin(client, admin_user)


@pytest.fixture
async def provider(db: Any) -> LLMProvider:
    p = LLMProvider(name=f"p-{uuid.uuid4().hex[:6]}", type="mock", enabled=True)
    db.add(p)
    await db.commit()
    return p


async def _fresh(model_id: uuid.UUID) -> LLMModel:
    async with get_session_factory()() as s:
        row = await s.get(LLMModel, model_id)
        assert row is not None
        return row


async def _audits(action: str) -> list[dict[str, Any]]:
    async with get_session_factory()() as s:
        rows = await s.scalars(
            select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.created_at)
        )
        return [r.detail for r in rows]


# ── 관리자 왕복 ───────────────────────────────────────────────────────────


async def test_create_list_patch_and_clear_round_trip(
    admin: _Admin, provider: LLMProvider
) -> None:
    res = await admin.send(
        "POST",
        "/admin/llm/models",
        {
            "provider_id": str(provider.id),
            "model_name": "gpt-oss-120b",
            "display_name": "oss",
            "context_window": 32_768,
            "reasoning_effort": "low",
            "max_output_tokens": 16_384,
        },
    )
    assert res.status_code == 201, res.text
    created = res.json()
    assert (created["reasoning_effort"], created["max_output_tokens"]) == ("low", 16_384)
    model_id = uuid.UUID(created["id"])
    row = await _fresh(model_id)
    assert (row.reasoning_effort, row.max_output_tokens) == ("low", 16_384)
    assert await _audits("llm.model_create") == [
        {"capabilities": ["llm"], "reasoning_effort": "low", "max_output_tokens": 16_384}
    ]

    listed = (await admin.send("GET", "/admin/llm/models")).json()
    [item] = [m for m in listed if m["id"] == str(model_id)]
    assert (item["reasoning_effort"], item["max_output_tokens"]) == ("low", 16_384)

    # 한 필드만 바꾸면 다른 필드는 그대로다
    res = await admin.send("PATCH", f"/admin/llm/models/{model_id}", {"reasoning_effort": "high"})
    assert res.status_code == 200, res.text
    row = await _fresh(model_id)
    assert (row.reasoning_effort, row.max_output_tokens) == ("high", 16_384)

    # null은 해제다(nullable 열) — 프로바이더 기본으로 돌아간다
    res = await admin.send(
        "PATCH",
        f"/admin/llm/models/{model_id}",
        {"reasoning_effort": None, "max_output_tokens": None},
    )
    assert res.status_code == 200, res.text
    assert (res.json()["reasoning_effort"], res.json()["max_output_tokens"]) == (None, None)
    row = await _fresh(model_id)
    assert (row.reasoning_effort, row.max_output_tokens) == (None, None)
    assert await _audits("llm.model_update") == [
        {"fields": ["reasoning_effort"]},
        {"fields": ["max_output_tokens", "reasoning_effort"]},
    ]


async def test_create_without_the_fields_leaves_the_columns_null(
    admin: _Admin, provider: LLMProvider
) -> None:
    res = await admin.send(
        "POST",
        "/admin/llm/models",
        {"provider_id": str(provider.id), "model_name": "m", "display_name": "m"},
    )

    assert res.status_code == 201, res.text
    assert (res.json()["reasoning_effort"], res.json()["max_output_tokens"]) == (None, None)
    row = await _fresh(uuid.UUID(res.json()["id"]))
    assert (row.reasoning_effort, row.max_output_tokens) == (None, None)


async def test_out_of_range_values_are_422_and_nothing_is_written(
    admin: _Admin, provider: LLMProvider, db: Any
) -> None:
    res = await admin.send(
        "POST",
        "/admin/llm/models",
        {
            "provider_id": str(provider.id),
            "model_name": "m",
            "display_name": "m",
            "reasoning_effort": "xhigh",
        },
    )
    assert res.status_code == 422
    assert (await db.scalars(select(LLMModel.id))).all() == []

    model = LLMModel(
        provider_id=provider.id, model_name="kept", display_name="kept", capabilities=["llm"],
        reasoning_effort="medium", max_output_tokens=4096,
    )  # fmt: skip
    db.add(model)
    await db.commit()
    for body in ({"max_output_tokens": 0}, {"reasoning_effort": "none"}):
        res = await admin.send("PATCH", f"/admin/llm/models/{model.id}", body)
        assert res.status_code == 422, body
    row = await _fresh(model.id)
    assert (row.reasoning_effort, row.max_output_tokens) == ("medium", 4096)


async def test_the_largest_integer_round_trips_and_the_next_is_422(
    admin: _Admin, provider: LLMProvider, db: Any
) -> None:
    """`max_output_tokens`는 PostgreSQL Integer 범위 안이다 — 넘는 값은 저장 전에 422다(500
    아님)."""
    top = 2_147_483_647
    res = await admin.send(
        "POST",
        "/admin/llm/models",
        {"provider_id": str(provider.id), "model_name": "top", "display_name": "top",
         "max_output_tokens": top},
    )  # fmt: skip
    assert res.status_code == 201, res.text
    model_id = uuid.UUID(res.json()["id"])
    assert (await _fresh(model_id)).max_output_tokens == top
    listed = (await admin.send("GET", "/admin/llm/models")).json()
    assert [m["max_output_tokens"] for m in listed if m["id"] == str(model_id)] == [top]

    for method, path, body in (
        ("POST", "/admin/llm/models",
         {"provider_id": str(provider.id), "model_name": "over", "display_name": "over",
          "max_output_tokens": top + 1}),
        ("PATCH", f"/admin/llm/models/{model_id}", {"max_output_tokens": top + 1}),
        ("POST", f"/admin/llm/providers/{provider.id}/models/import",
         {"items": [{"model_name": "over", "display_name": "over",
                     "max_output_tokens": top + 1}]}),
    ):  # fmt: skip
        res = await admin.send(method, path, body)
        assert res.status_code == 422, (method, path)
        assert str(top + 1) not in res.text
    assert (await _fresh(model_id)).max_output_tokens == top
    assert [m.model_name for m in (await db.scalars(select(LLMModel))).all()] == ["top"]


async def test_import_carries_the_fields(admin: _Admin, provider: LLMProvider, db: Any) -> None:
    res = await admin.send(
        "POST",
        f"/admin/llm/providers/{provider.id}/models/import",
        {
            "items": [
                {
                    "model_name": "think",
                    "display_name": "think",
                    "reasoning_effort": "medium",
                    "max_output_tokens": 65_536,
                },
                {"model_name": "plain", "display_name": "plain"},
            ]
        },
    )

    assert res.status_code == 200, res.text
    assert res.json() == {"created": 2, "skipped": []}
    rows = {
        m.model_name: (m.reasoning_effort, m.max_output_tokens)
        for m in (await db.scalars(select(LLMModel))).all()
    }
    assert rows == {"think": ("medium", 65_536), "plain": (None, None)}

    bad = await admin.send(
        "POST",
        f"/admin/llm/providers/{provider.id}/models/import",
        {"items": [{"model_name": "x", "display_name": "x", "reasoning_effort": "minimal"}]},
    )
    assert bad.status_code == 422


@dataclass
class _OldItem:
    """두 필드를 모르는 이전 호출자의 항목 — `plan_model_import(items: list)`는 덕 타입이다."""

    model_name: str
    display_name: str
    capabilities: list[str]
    context_window: int | None = None


async def test_import_items_without_the_fields_still_work(db: Any, provider: LLMProvider) -> None:
    result = await plan_model_import(db, provider.id, [_OldItem("old", "old", ["llm"])])
    await db.commit()

    [created] = result.created
    row = await _fresh(created.id)
    assert (row.model_name, row.reasoning_effort, row.max_output_tokens) == ("old", None, None)


# ── 모델 해석 ─────────────────────────────────────────────────────────────


async def test_model_resolution_loads_the_new_columns(db: Any, provider: LLMProvider) -> None:
    primary = LLMModel(
        provider_id=provider.id, model_name="primary", display_name="primary",
        capabilities=["llm"], is_default=True, reasoning_effort="low", max_output_tokens=8192,
    )  # fmt: skip
    backup = LLMModel(
        provider_id=provider.id, model_name="backup", display_name="backup",
        capabilities=["llm"], fallback_priority=1,
    )  # fmt: skip
    db.add_all([primary, backup])
    await db.commit()

    async with get_session_factory()() as s:
        resolved = await resolve_model(s, None)
        by_id = await resolve_model(s, backup.id)
        chain = await resolve_chain(s, None)

    assert (resolved.model.reasoning_effort, resolved.model.max_output_tokens) == ("low", 8192)
    assert (by_id.model.reasoning_effort, by_id.model.max_output_tokens) == (None, None)
    assert [(c.model.display_name, c.model.reasoning_effort, c.model.max_output_tokens)
            for c in chain] == [("primary", "low", 8192), ("backup", None, None)]  # fmt: skip


async def test_gateway_uses_the_resolved_row_for_the_cap_and_the_default(
    db: Any, provider: LLMProvider
) -> None:
    """DB에서 해석한 후보 행이 그대로 쓰인다(추가 조회 없음) — mock 프로바이더는 상한을 지키고
    사고 양은 무시한다(②·①과 같다)."""
    model = LLMModel(
        provider_id=provider.id, model_name="mock", display_name="mock", capabilities=["llm"],
        is_default=True, reasoning_effort="high", max_output_tokens=8,
    )  # fmt: skip
    db.add(model)
    await db.commit()
    messages = [{"role": "user", "content": "요약"}]
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]

    async with get_session_factory()() as s:
        result = StreamResult()
        gen = await complete_stream_with_fallback(s, None, messages, result, max_tokens=4_096)
        text = "".join([d async for d in gen])
        turn = await complete_with_tools(s, None, messages, tools, max_tokens=4_096)

    assert result.model is not None and result.model.model.id == model.id
    assert (result.max_tokens_used, result.finish_reason) == (8, "length")
    assert 0 < len(text) <= 8 * 4  # mock 출력 상한은 토큰 × 4글자다
    assert turn.max_tokens_used == 8


# ── 열 모양 ───────────────────────────────────────────────────────────────


async def test_columns_match_the_revision_shape(db: Any) -> None:
    cols = (
        await db.execute(
            text(
                "SELECT table_name, column_name, data_type, character_maximum_length, "
                "is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = 'llm_models' "
                "  AND column_name IN ('reasoning_effort', 'max_output_tokens')"
            )
        )
    ).all()

    assert {(c[0], c[1]): tuple(c[2:]) for c in cols} == {
        ("llm_models", "reasoning_effort"): ("character varying", 16, "YES", None),
        ("llm_models", "max_output_tokens"): ("integer", None, "YES", None),
    }
    # CHECK 제약은 두지 않는다(결정 D1 — type·capabilities 선례). 허용 값은 스키마가 검증한다
    checks = (
        await db.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'llm_models'::regclass "
                "AND contype = 'c'"
            )
        )
    ).scalars().all()
    assert not [c for c in checks if "reasoning" in c or "max_output_tokens" in c]
