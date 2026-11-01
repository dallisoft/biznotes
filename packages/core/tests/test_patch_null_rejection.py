"""노트북·스킬·관리자 LLM 모델 PATCH가 명시적 null을 422로 거부한다 (#267).

#258·#242와 같은 규칙이다. null이 뜻을 갖는 필드(스킬 `visibility`의 생략 취급, LLM 모델의
`context_window`·`fallback_priority` 해제, `cost_per_1k` 정규화)는 지금 동작을 유지한다.
인증(`get_current_user`)과 DB 세션(`get_db`)만 대역이고, 요청 검증·핸들러·응답 직렬화는 실경로다.
실제 DB가 그대로인지는 `tests/test_patch_null_rejection_pg.py`가 확인한다.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel

from biznotes_core.api import deps
from biznotes_core.api.patch import non_null_changes
from biznotes_core.api.v1 import notebooks, skills
from biznotes_core.api.v1.admin import llm
from biznotes_core.core.db import get_db
from biznotes_core.models import LLMModel, LLMProvider, Notebook, Skill, User

NOW = datetime(2026, 10, 17, tzinfo=UTC)


class _RecordingDb:
    """행을 돌려주고 DB 호출 순서를 기록한다."""

    def __init__(self, *rows: Any) -> None:
        self.rows = {(type(r), r.id): r for r in rows}
        self.calls: list[str] = []

    async def get(self, model: type, key: uuid.UUID, **_kw: Any) -> Any:
        self.calls.append(f"get:{model.__name__}")
        return self.rows.get((model, key))

    def add(self, obj: Any) -> None:
        self.calls.append(f"add:{type(obj).__name__}")

    async def commit(self) -> None:
        self.calls.append("commit")

    async def refresh(self, obj: Any) -> None:
        self.calls.append("refresh")


class _FakeShareDb(_RecordingDb):
    """공유·그룹 조회가 빈 결과를 돌려주는 세션 — 무관 사용자 판정용."""

    async def execute(self, _stmt: Any) -> Any:
        self.calls.append("execute")
        return _Empty()


class _Empty:
    def all(self) -> list[Any]:
        return []

    def scalars(self) -> "_Empty":
        return self

    def __iter__(self) -> Any:
        return iter(())


def _user(role: str = "user") -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{role}@test.dev",
        name=role,
        role=role,
        is_active=True,
        password_hash="x",
    )


def _notebook(owner: User) -> Notebook:
    return Notebook(
        id=uuid.uuid4(),
        owner_id=owner.id,
        title="제목",
        emoji="📓",
        description="설명",
        visibility="private",
        created_at=NOW,
        updated_at=NOW,
    )


def _skill(owner: User) -> Skill:
    return Skill(
        id=uuid.uuid4(),
        owner_id=owner.id,
        name="스킬",
        description="설명",
        slug="skill",
        tags=["a"],
        visibility="private",
        status="draft",
    )


def _model() -> tuple[LLMModel, LLMProvider]:
    provider = LLMProvider(id=uuid.uuid4(), name="local", type="openai")
    model = LLMModel(
        id=uuid.uuid4(),
        provider_id=provider.id,
        model_name="m",
        display_name="모델",
        capabilities=["llm"],
        is_default=False,
        enabled=True,
        context_window=8192,
        cost_per_1k={"input": 0.1},
        fallback_priority=1,
    )
    return model, provider


async def _patch(me: User, db: _RecordingDb, path: str, body: Any) -> httpx.Response:
    admin = APIRouter(prefix="/admin", dependencies=[Depends(deps.require_admin)])
    admin.include_router(llm.router)
    app = FastAPI()
    for router in (notebooks.router, skills.router, admin):
        app.include_router(router)
    app.dependency_overrides[deps.get_current_user] = lambda: me
    app.dependency_overrides[get_db] = lambda: db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.patch(path, json=body)


# ---------- 노트북 ----------


@pytest.mark.parametrize(
    ("body", "fields"),
    [
        ({"title": None}, "title"),
        ({"emoji": None}, "emoji"),
        ({"description": None}, "description"),
        ({"visibility": None}, "visibility"),
        # 함께 보낸 정상 필드도 적용하지 않는다
        ({"title": "새 제목", "visibility": None, "description": None}, "description, visibility"),
    ],
)
async def test_notebook_null_is_422_and_nothing_is_written(
    body: dict[str, Any], fields: str
) -> None:
    me = _user()
    nb = _notebook(me)
    db = _RecordingDb(nb)

    resp = await _patch(me, db, f"/notebooks/{nb.id}", body)

    assert resp.status_code == 422
    assert resp.json() == {"detail": f"null로 바꿀 수 없는 필드: {fields}"}
    assert db.calls == ["get:Notebook"]
    assert (nb.title, nb.emoji, nb.description, nb.visibility) == (
        "제목",
        "📓",
        "설명",
        "private",
    )


async def test_notebook_permission_is_judged_before_null() -> None:
    """권한이 없는 사용자에게는 null 거부보다 존재 은닉(404)이 먼저다."""
    owner, other = _user(), _user()
    nb = _notebook(owner)

    resp = await _patch(other, _FakeShareDb(nb), f"/notebooks/{nb.id}", {"title": None})

    assert resp.status_code == 404


async def test_notebook_non_null_fields_are_applied() -> None:
    me = _user()
    nb = _notebook(me)
    db = _RecordingDb(nb)

    resp = await _patch(me, db, f"/notebooks/{nb.id}", {"title": "새 제목", "visibility": "public"})

    assert resp.status_code == 200
    assert (resp.json()["title"], resp.json()["visibility"]) == ("새 제목", "public")
    assert db.calls == ["get:Notebook", "commit", "refresh"]


# ---------- 스킬 ----------


@pytest.mark.parametrize(
    ("body", "fields"),
    [
        ({"name": None}, "name"),
        ({"description": None}, "description"),
        ({"tags": None}, "tags"),
        # visibility null은 생략과 같아 문구에 싣지 않는다
        ({"visibility": None, "tags": None, "name": None}, "name, tags"),
        # 공개 관문(검토 요청 커밋)보다 먼저 거부한다 — 같은 요청의 부분 적용이 없다
        ({"visibility": "org", "description": None}, "description"),
    ],
)
async def test_skill_null_is_422_before_exposure_gate(body: dict[str, Any], fields: str) -> None:
    me = _user()
    skill = _skill(me)
    db = _RecordingDb(skill)

    resp = await _patch(me, db, f"/skills/{skill.id}", body)

    assert resp.status_code == 422
    assert resp.json() == {"detail": f"null로 바꿀 수 없는 필드: {fields}"}
    assert db.calls == ["get:Skill"]
    assert (skill.name, skill.description, skill.tags, skill.visibility) == (
        "스킬",
        "설명",
        ["a"],
        "private",
    )


async def test_skill_edit_permission_is_judged_before_null() -> None:
    owner, other = _user(), _user()
    skill = _skill(owner)

    resp = await _patch(other, _RecordingDb(skill), f"/skills/{skill.id}", {"name": None})

    assert resp.status_code == 404


# ---------- 관리자 LLM 모델 ----------


@pytest.mark.parametrize(
    ("body", "fields"),
    [
        ({"display_name": None}, "display_name"),
        ({"enabled": None}, "enabled"),
        ({"capabilities": None}, "capabilities"),
        # null이 정상인 필드와 섞여도 NOT NULL 필드만 문구에 싣는다
        (
            {"context_window": None, "enabled": None, "cost_per_1k": None, "display_name": None},
            "display_name, enabled",
        ),
    ],
)
async def test_llm_model_null_is_422_and_nothing_is_written(
    body: dict[str, Any], fields: str
) -> None:
    me = _user("admin")
    model, provider = _model()
    db = _RecordingDb(model, provider)

    resp = await _patch(me, db, f"/admin/llm/models/{model.id}", body)

    assert resp.status_code == 422
    assert resp.json() == {"detail": f"null로 바꿀 수 없는 필드: {fields}"}
    assert db.calls == ["get:LLMModel"]
    assert (model.display_name, model.enabled, model.capabilities, model.context_window) == (
        "모델",
        True,
        ["llm"],
        8192,
    )


async def test_llm_model_missing_is_still_404() -> None:
    model, _provider = _model()

    resp = await _patch(
        _user("admin"), _RecordingDb(), f"/admin/llm/models/{model.id}", {"enabled": None}
    )

    assert resp.status_code == 404


async def test_llm_model_nullable_fields_keep_meaning() -> None:
    """`context_window`·`fallback_priority`의 null은 해제, `cost_per_1k`의 null은 `{}`다."""
    me = _user("admin")
    model, provider = _model()
    db = _RecordingDb(model, provider)

    resp = await _patch(
        me,
        db,
        f"/admin/llm/models/{model.id}",
        {"context_window": None, "fallback_priority": None, "cost_per_1k": None},
    )

    assert resp.status_code == 200
    out = resp.json()
    assert (out["context_window"], out["fallback_priority"], out["cost_per_1k"]) == (None, None, {})
    assert (model.context_window, model.fallback_priority, model.cost_per_1k) == (None, None, {})
    assert db.calls == ["get:LLMModel", "add:AuditLog", "commit", "get:LLMProvider"]


# ---------- 헬퍼 ----------


class _Body(BaseModel):
    name: str | None = None
    limit: int | None = None


def test_helper_passes_nullable_fields_through() -> None:
    body = _Body.model_validate({"name": "x", "limit": None})

    assert non_null_changes(body, nullable=("limit",)) == {"name": "x", "limit": None}
    # 기본값(nullable 없음)은 #258과 같다
    with pytest.raises(HTTPException) as exc:
        non_null_changes(body)
    assert exc.value.detail == "null로 바꿀 수 없는 필드: limit"


def test_helper_reports_only_non_nullable_nulls() -> None:
    with pytest.raises(HTTPException) as exc:
        non_null_changes(_Body.model_validate({"name": None, "limit": None}), nullable=("limit",))

    assert exc.value.status_code == 422
    assert exc.value.detail == "null로 바꿀 수 없는 필드: name"
