"""관리자 LLM 모델의 정수 필드가 PostgreSQL Integer 범위를 벗어나면 422다 (#437).

`llm_models.context_window`·`fallback_priority`는 int4 열이다. 범위 밖 값이 요청 검증을
통과하면 저장 때 범위 오류로 500이 된다. 생성·수정·가져오기 모두 -2147483648~2147483647을
받고 그 밖은 422로 거부한다. 422 본문에는 입력값이 실리지 않는다(`RedactedValidationRoute`).
하한은 열 범위만 둔다 — null(해제)·0·음수는 지금처럼 받는다.
인증(`get_current_user`)과 DB 세션(`get_db`)만 대역이고, 요청 검증·핸들러·서비스·응답
직렬화는 실경로다. 실제 열의 왕복은 `tests/test_llm_integer_range_pg.py`가 확인한다.
"""

import uuid
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from pydantic import BaseModel, ValidationError

from biznotes_core.api import deps
from biznotes_core.api.v1.admin import llm
from biznotes_core.core.db import get_db
from biznotes_core.models import LLMModel, LLMProvider, User
from biznotes_core.schemas.llm import ModelCreate, ModelImportItem, ModelUpdate

INT4_MAX = 2_147_483_647  # PostgreSQL Integer 상한
INT4_MIN = -2_147_483_648
TOO_BIG = INT4_MAX + 1
TOO_SMALL = INT4_MIN - 1

# (범위 안 경계값, 범위 밖 값, pydantic 오류 type)
BOUNDS = [
    pytest.param(INT4_MAX, TOO_BIG, "less_than_equal", id="max"),
    pytest.param(INT4_MIN, TOO_SMALL, "greater_than_equal", id="min"),
]


class _NoRows:
    def all(self) -> list[Any]:
        return []

    def first(self) -> None:
        return None


class _FakeDb:
    """행을 돌려주고 DB 호출을 기록한다. 조회 결과는 비어 있다."""

    def __init__(self, *rows: Any) -> None:
        self.rows = {(type(r), r.id): r for r in rows}
        self.calls: list[str] = []
        self.added: list[Any] = []

    async def get(self, model: type, key: uuid.UUID, **_kw: Any) -> Any:
        self.calls.append(f"get:{model.__name__}")
        return self.rows.get((model, key))

    async def execute(self, _stmt: Any) -> _NoRows:
        self.calls.append("execute")
        return _NoRows()

    def add(self, obj: Any) -> None:
        self.calls.append(f"add:{type(obj).__name__}")
        self.added.append(obj)

    async def commit(self) -> None:
        self.calls.append("commit")

    async def refresh(self, obj: Any) -> None:
        # 기본키는 flush 때 채워진다 — 커밋된 행처럼 채운다
        self.calls.append("refresh")
        if obj.id is None:
            obj.id = uuid.uuid4()


def _rows() -> tuple[LLMModel, LLMProvider]:
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
        cost_per_1k={},
        fallback_priority=1,
    )
    return model, provider


async def _send(db: _FakeDb, method: str, path: str, body: Any) -> httpx.Response:
    admin = APIRouter(prefix="/admin", dependencies=[Depends(deps.require_admin)])
    admin.include_router(llm.router)
    app = FastAPI()
    app.include_router(admin)
    me = User(id=uuid.uuid4(), email="admin@test.dev", role="admin", is_active=True)
    app.dependency_overrides[deps.get_current_user] = lambda: me
    app.dependency_overrides[get_db] = lambda: db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.request(method, path, json=body)


def _create(provider: LLMProvider, value: int) -> dict[str, Any]:
    return {
        "provider_id": str(provider.id),
        "model_name": "new",
        "display_name": "새 모델",
        "context_window": value,
    }


def _import_item(value: int) -> dict[str, Any]:
    return {"model_name": "new", "display_name": "새 모델", "context_window": value}


# ---------- 스키마 ----------

SCHEMA_FIELDS = [
    pytest.param(
        ModelCreate,
        "context_window",
        {"provider_id": uuid.uuid4(), "model_name": "m", "display_name": "모델"},
        id="create-context_window",
    ),
    pytest.param(ModelUpdate, "context_window", {}, id="update-context_window"),
    pytest.param(ModelUpdate, "fallback_priority", {}, id="update-fallback_priority"),
    pytest.param(
        ModelImportItem,
        "context_window",
        {"model_name": "m", "display_name": "모델"},
        id="import-context_window",
    ),
]


@pytest.mark.parametrize(("ok", "bad", "error_type"), BOUNDS)
@pytest.mark.parametrize(("schema", "field", "base"), SCHEMA_FIELDS)
def test_schema_accepts_int4_bounds_and_rejects_beyond(
    schema: type[BaseModel],
    field: str,
    base: dict[str, Any],
    ok: int,
    bad: int,
    error_type: str,
) -> None:
    assert getattr(schema.model_validate({**base, field: ok}), field) == ok

    with pytest.raises(ValidationError) as exc:
        schema.model_validate({**base, field: bad})

    [error] = exc.value.errors()
    assert (error["loc"], error["type"]) == ((field,), error_type)


@pytest.mark.parametrize(("schema", "field", "base"), SCHEMA_FIELDS)
@pytest.mark.parametrize("value", [None, 0, -1])
def test_schema_keeps_accepting_lower_values(
    schema: type[BaseModel], field: str, base: dict[str, Any], value: int | None
) -> None:
    """하한은 열 범위만 둔다 — null(해제)·0·음수는 지금처럼 받는다."""
    assert getattr(schema.model_validate({**base, field: value}), field) == value


# ---------- 422: 생성·수정·가져오기 ----------


@pytest.mark.parametrize(("ok", "bad", "error_type"), BOUNDS)
@pytest.mark.parametrize(
    ("kind", "loc"),
    [
        ("create", ["body", "context_window"]),
        ("update-context_window", ["body", "context_window"]),
        ("update-fallback_priority", ["body", "fallback_priority"]),
        ("import", ["body", "items", 1, "context_window"]),
    ],
)
async def test_beyond_int4_is_422_without_input_and_nothing_is_written(
    kind: str, loc: list[Any], ok: int, bad: int, error_type: str
) -> None:
    model, provider = _rows()
    db = _FakeDb(model, provider)
    requests: dict[str, tuple[str, str, dict[str, Any]]] = {
        "create": ("POST", "/admin/llm/models", _create(provider, bad)),
        "update-context_window": (
            "PATCH",
            f"/admin/llm/models/{model.id}",
            {"display_name": "바뀐 이름", "context_window": bad},
        ),
        "update-fallback_priority": (
            "PATCH",
            f"/admin/llm/models/{model.id}",
            {"display_name": "바뀐 이름", "fallback_priority": bad},
        ),
        # 범위 안 항목과 섞여도 배치 전체를 거부한다
        "import": (
            "POST",
            f"/admin/llm/providers/{provider.id}/models/import",
            {"items": [_import_item(ok), _import_item(bad)]},
        ),
    }
    method, path, body = requests[kind]

    resp = await _send(db, method, path, body)

    assert resp.status_code == 422
    [error] = resp.json()["detail"]
    assert (error["loc"], error["type"]) == (loc, error_type)
    # 입력값을 되돌려주지 않는다
    assert "input" not in error
    assert str(bad) not in resp.text
    # 핸들러에 닿지 않는다 — 조회·기록·커밋이 없다
    assert db.calls == []
    assert (model.display_name, model.context_window, model.fallback_priority) == (
        "모델",
        8192,
        1,
    )


# ---------- 경계값 통과 ----------


@pytest.mark.parametrize("value", [INT4_MAX, INT4_MIN])
async def test_create_accepts_int4_bounds(value: int) -> None:
    _model, provider = _rows()
    db = _FakeDb(provider)

    resp = await _send(db, "POST", "/admin/llm/models", _create(provider, value))

    assert resp.status_code == 201, resp.text
    assert resp.json()["context_window"] == value
    [created] = [o for o in db.added if isinstance(o, LLMModel)]
    assert created.context_window == value
    assert "commit" in db.calls


@pytest.mark.parametrize("value", [INT4_MAX, INT4_MIN])
async def test_update_accepts_int4_bounds(value: int) -> None:
    model, provider = _rows()
    db = _FakeDb(model, provider)

    resp = await _send(
        db,
        "PATCH",
        f"/admin/llm/models/{model.id}",
        {"context_window": value, "fallback_priority": value},
    )

    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert (out["context_window"], out["fallback_priority"]) == (value, value)
    assert (model.context_window, model.fallback_priority) == (value, value)
    assert "commit" in db.calls


@pytest.mark.parametrize("value", [INT4_MAX, INT4_MIN])
async def test_import_accepts_int4_bounds(value: int) -> None:
    _model, provider = _rows()
    db = _FakeDb(provider)

    resp = await _send(
        db,
        "POST",
        f"/admin/llm/providers/{provider.id}/models/import",
        {"items": [_import_item(value)]},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"created": 1, "skipped": []}
    [created] = [o for o in db.added if isinstance(o, LLMModel)]
    assert created.context_window == value
    assert "commit" in db.calls
