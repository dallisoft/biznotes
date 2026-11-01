"""공유 재설계 3단계 (#278·#277) — 사용자 대상·`visibility="group"` 제거와 공유 제약.

계획 `docs/plans/share-redesign/stage-3.md` 수락 기준 2·3·5·6·8이다. 제품 Alembic 리비전이 여기
고정한 이름·식을 그대로 만든다. 실제 제약 동작은 `tests/test_share_redesign_stage3_pg.py`가 본다.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, UniqueConstraint

from biznotes_core.api import deps
from biznotes_core.api.v1 import groups, notebooks
from biznotes_core.core.db import get_db
from biznotes_core.models import Notebook, NotebookShare, User
from biznotes_core.models.notebook import NOTEBOOK_VISIBILITIES
from biznotes_core.schemas.notebook import NotebookShareOut, NotebookUpdate

NOW = datetime(2026, 10, 28, tzinfo=UTC)


def _constraints(model: type, kind: type) -> dict[str, Any]:
    return {c.name: c for c in model.__table__.constraints if isinstance(c, kind)}


# ---------- 모델 (수락 기준 6) ----------


def test_notebook_shares_has_a_group_target_only() -> None:
    columns = NotebookShare.__table__.columns

    assert "user_id" not in columns
    assert columns["group_id"].nullable is False
    # 목록의 `group_id IN (소속)` 조회용 인덱스는 그대로다
    assert {ix.name for ix in NotebookShare.__table__.indexes} >= {
        "ix_notebook_shares_group_id",
        "ix_notebook_shares_notebook_id",
    }


def test_notebook_shares_constraints() -> None:
    uniques = _constraints(NotebookShare, UniqueConstraint)
    checks = _constraints(NotebookShare, CheckConstraint)

    assert list(uniques) == ["uq_share_notebook_group"]
    assert [c.name for c in uniques["uq_share_notebook_group"].columns] == [
        "notebook_id",
        "group_id",
    ]
    assert list(checks) == ["ck_share_permission"]
    assert str(checks["ck_share_permission"].sqltext) == "permission IN ('read', 'write')"


def test_notebooks_visibility_check() -> None:
    checks = _constraints(Notebook, CheckConstraint)

    assert str(checks["ck_notebook_visibility"].sqltext) == "visibility IN ('private', 'public')"
    assert NOTEBOOK_VISIBILITIES == ("private", "public")


# ---------- 스키마 (수락 기준 2·3) ----------


def test_share_out_has_no_user_target_and_a_share_permission() -> None:
    row = {"id": uuid.uuid4(), "notebook_id": uuid.uuid4(), "group_id": uuid.uuid4()}

    assert "user_id" not in NotebookShareOut.model_fields
    assert NotebookShareOut(**row, permission="write").permission == "write"
    # 노트북 권한 `owner`는 공유 권한이 아니다(#277)
    with pytest.raises(ValidationError):
        NotebookShareOut(**row, permission="owner")
    with pytest.raises(ValidationError):
        NotebookShareOut(**{**row, "group_id": None}, permission="read")


def test_visibility_group_is_rejected() -> None:
    with pytest.raises(ValidationError):
        NotebookUpdate(visibility="group")
    assert NotebookUpdate(visibility="public").visibility == "public"


def _schemas() -> dict[str, Any]:
    app = FastAPI()
    app.include_router(notebooks.router)
    app.include_router(groups.router)
    return app.openapi()["components"]["schemas"]


def test_openapi_contract() -> None:
    """수락 기준 8 — 필드 제거·필수화·enum 축소, deprecated 표기 제거."""
    schemas = _schemas()
    req, out = schemas["NotebookShareRequest"], schemas["NotebookShareOut"]

    assert set(req["properties"]) == {"group_id", "permission", "mode"}
    assert req["required"] == ["group_id"]
    assert req["additionalProperties"] is False
    assert set(out["properties"]) == {"id", "notebook_id", "group_id", "permission"}
    assert set(out["required"]) == {"id", "notebook_id", "group_id", "permission"}
    assert out["properties"]["group_id"] == {
        "type": "string",
        "format": "uuid",
        "title": "Group Id",
    }
    assert out["properties"]["permission"]["enum"] == ["read", "write"]
    for name in ("NotebookUpdate", "NotebookOut"):
        visibility = schemas[name]["properties"]["visibility"]
        enum = visibility["enum"] if "enum" in visibility else visibility["anyOf"][0]["enum"]
        assert enum == ["private", "public"]
        assert "deprecated" not in visibility["description"]
    assert "deprecated" not in str(req) + str(out)


# ---------- 라우터 (수락 기준 3) ----------


class _DB:
    """노트북 하나와 그 소유자 판정만 필요하다 — 소유자는 공유 조회 없이 판정된다."""

    def __init__(self, nb: Notebook) -> None:
        self.nb = nb
        self.calls: list[str] = []

    async def get(self, model: type, key: Any, **_kw: Any) -> Any:
        self.calls.append(f"get:{model.__name__}")
        return self.nb if model is Notebook and key == self.nb.id else None

    async def commit(self) -> None:
        self.calls.append("commit")

    async def refresh(self, _obj: Any) -> None:
        self.calls.append("refresh")


async def _patch(me: User, db: _DB, body: dict[str, Any]) -> httpx.Response:
    app = FastAPI()
    app.include_router(notebooks.router)
    app.dependency_overrides[deps.get_current_user] = lambda: me
    app.dependency_overrides[get_db] = lambda: db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.patch(f"/notebooks/{db.nb.id}", json=body)


async def test_owner_cannot_set_visibility_group() -> None:
    owner = User(id=uuid.uuid4(), email="o@test.dev", role="user", is_active=True)
    nb = Notebook(
        id=uuid.uuid4(),
        owner_id=owner.id,
        title="t",
        emoji="📓",
        description="",
        visibility="private",
        created_at=NOW,
        updated_at=NOW,
    )
    db = _DB(nb)

    resp = await _patch(owner, db, {"visibility": "group", "title": "새 제목"})

    assert resp.status_code == 422
    assert [e["loc"][-1] for e in resp.json()["detail"]] == ["visibility"]
    assert (nb.visibility, nb.title) == ("private", "t")
    assert db.calls == []  # 본문 검증이 노트북 조회보다 먼저다
