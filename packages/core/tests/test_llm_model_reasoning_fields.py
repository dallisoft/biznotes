"""관리자 LLM 모델의 사고 양 기본값·최대 출력 토큰(#429·#428, L-1).

정본 튜플 `REASONING_EFFORTS` ↔ `ReasoningEffort` 별칭 대조, 요청 스키마(생성·수정·가져오기) 검증,
응답 매핑, PATCH null 해제를 본다. 인증과 DB 세션만 대역이고 요청 검증·핸들러·응답 직렬화는
실경로다. 실제 DB 왕복(생성·조회·수정·가져오기·모델 해석)은
`tests/test_llm_model_reasoning_columns_pg.py`가 본다.
"""

import uuid
from typing import Any, get_args

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from pydantic import ValidationError

from biznotes_core.api import deps
from biznotes_core.api.v1.admin import llm
from biznotes_core.core.db import get_db
from biznotes_core.models import LLMModel, LLMProvider, User
from biznotes_core.models.llm import REASONING_EFFORTS
from biznotes_core.schemas import enums
from biznotes_core.schemas.enums import ReasoningEffort
from biznotes_core.schemas.llm import (
    ModelCreate,
    ModelImportItem,
    ModelOut,
    ModelUpdate,
    model_to_out,
)

BAD_EFFORTS = ["none", "minimal", "xhigh", "HIGH", "", "Low"]
# PostgreSQL Integer 상한 — 넘는 값은 저장 때 범위 오류(500)라 스키마가 422로 먼저 막는다
PG_INT_MAX = 2_147_483_647


# ── 허용 값 정본 ──────────────────────────────────────────────────────────


def test_reasoning_efforts_are_low_medium_high_in_order() -> None:
    assert REASONING_EFFORTS == ("low", "medium", "high")
    assert get_args(ReasoningEffort) == REASONING_EFFORTS


def test_alias_is_checked_against_the_canonical_tuple_at_import() -> None:
    """정본에 값을 더하고 별칭을 잊으면 import 시점에 죽는다(`PROVIDER_TYPES`·`MODALITIES` 선례)."""
    assert ("ReasoningEffort", ReasoningEffort, REASONING_EFFORTS) in enums._BINDINGS


def test_alias_drift_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    drifted = (("ReasoningEffort", ReasoningEffort, (*REASONING_EFFORTS, "xhigh")),)
    monkeypatch.setattr(enums, "_BINDINGS", drifted)

    with pytest.raises(RuntimeError, match="ReasoningEffort"):
        enums._assert_matches_canonical()


# ── 요청 스키마 ───────────────────────────────────────────────────────────


def _create(**fields: Any) -> dict[str, Any]:
    return {"provider_id": str(uuid.uuid4()), "model_name": "m", "display_name": "모델", **fields}


def _item(**fields: Any) -> dict[str, Any]:
    return {"model_name": "m", "display_name": "모델", **fields}


SCHEMAS = [
    pytest.param(lambda f: ModelCreate.model_validate(_create(**f)), id="create"),
    pytest.param(lambda f: ModelUpdate.model_validate(f), id="update"),
    pytest.param(lambda f: ModelImportItem.model_validate(_item(**f)), id="import"),
]


@pytest.mark.parametrize("schema", SCHEMAS)
@pytest.mark.parametrize("effort", REASONING_EFFORTS)
def test_allowed_values_are_accepted(schema: Any, effort: str) -> None:
    body = schema({"reasoning_effort": effort, "max_output_tokens": 1})

    assert (body.reasoning_effort, body.max_output_tokens) == (effort, 1)


@pytest.mark.parametrize("schema", SCHEMAS)
def test_both_fields_are_optional_and_default_to_none(schema: Any) -> None:
    body = schema({})

    assert (body.reasoning_effort, body.max_output_tokens) == (None, None)
    explicit = schema({"reasoning_effort": None, "max_output_tokens": None})
    assert (explicit.reasoning_effort, explicit.max_output_tokens) == (None, None)


@pytest.mark.parametrize("schema", SCHEMAS)
@pytest.mark.parametrize("bad", BAD_EFFORTS)
def test_other_efforts_are_refused(schema: Any, bad: str) -> None:
    with pytest.raises(ValidationError) as exc:
        schema({"reasoning_effort": bad})

    assert [(e["type"], e["loc"]) for e in exc.value.errors()] == [
        ("literal_error", ("reasoning_effort",))
    ]


@pytest.mark.parametrize("schema", SCHEMAS)
@pytest.mark.parametrize("bad", [0, -1, "many", 1.5])
def test_max_output_tokens_must_be_a_positive_integer(schema: Any, bad: Any) -> None:
    with pytest.raises(ValidationError) as exc:
        schema({"max_output_tokens": bad})

    assert [e["loc"] for e in exc.value.errors()] == [("max_output_tokens",)]


@pytest.mark.parametrize("schema", SCHEMAS)
def test_max_output_tokens_stays_inside_the_integer_column(schema: Any) -> None:
    assert schema({"max_output_tokens": PG_INT_MAX}).max_output_tokens == PG_INT_MAX
    with pytest.raises(ValidationError) as exc:
        schema({"max_output_tokens": PG_INT_MAX + 1})

    assert [(e["type"], e["loc"]) for e in exc.value.errors()] == [
        ("less_than_equal", ("max_output_tokens",))
    ]


def test_openapi_exposes_the_effort_enum_and_the_lower_bound() -> None:
    schemas = _app(None).openapi()["components"]["schemas"]
    enum = {"type": "string", "enum": list(REASONING_EFFORTS)}

    for name in ("ModelCreate", "ModelUpdate", "ModelImportItem"):
        props = schemas[name]["properties"]
        assert props["reasoning_effort"]["anyOf"] == [enum, {"type": "null"}], name
        assert props["max_output_tokens"]["anyOf"] == [
            {"type": "integer", "minimum": 1, "maximum": PG_INT_MAX},
            {"type": "null"},
        ], name
        assert "reasoning_effort" not in schemas[name].get("required", []), name
    out = schemas["ModelOut"]["properties"]
    assert out["reasoning_effort"]["anyOf"] == [enum, {"type": "null"}]
    assert out["max_output_tokens"]["anyOf"] == [{"type": "integer"}, {"type": "null"}]
    assert {"reasoning_effort", "max_output_tokens"}.isdisjoint(
        schemas["ModelOut"].get("required", [])
    )


# ── 422 경로(입력값을 되돌려주지 않는다) ──────────────────────────────────


def _app(db: Any) -> FastAPI:
    admin = APIRouter(prefix="/admin", dependencies=[Depends(deps.require_admin)])
    admin.include_router(llm.router)
    app = FastAPI()
    app.include_router(admin)
    me = User(id=uuid.uuid4(), email="admin@test.dev", role="admin", is_active=True)
    app.dependency_overrides[deps.get_current_user] = lambda: me
    app.dependency_overrides[get_db] = lambda: db
    return app


async def _send(method: str, path: str, body: Any, db: Any = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app(db))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.request(method, path, json=body)


def _routes(fields: dict[str, Any]) -> list[tuple[str, str, Any, list[Any]]]:
    """(method, path, body, 필드 위치의 앞부분) — 생성·수정·가져오기."""
    return [
        ("POST", "/admin/llm/models", _create(**fields), ["body"]),
        ("PATCH", f"/admin/llm/models/{uuid.uuid4()}", fields, ["body"]),
        (
            "POST",
            f"/admin/llm/providers/{uuid.uuid4()}/models/import",
            {"items": [_item(**fields)]},
            ["body", "items", 0],
        ),
    ]


@pytest.mark.parametrize("route", range(3), ids=["create", "update", "import"])
@pytest.mark.parametrize(
    ("fields", "field", "kind"),
    [
        ({"reasoning_effort": "xhigh"}, "reasoning_effort", "literal_error"),
        ({"reasoning_effort": "none"}, "reasoning_effort", "literal_error"),
        ({"max_output_tokens": 0}, "max_output_tokens", "greater_than_equal"),
        ({"max_output_tokens": PG_INT_MAX + 1}, "max_output_tokens", "less_than_equal"),
    ],
    ids=["xhigh", "none", "zero", "above-int"],
)
async def test_out_of_range_values_are_422_without_the_input(
    route: int, fields: dict[str, Any], field: str, kind: str
) -> None:
    method, path, body, loc = _routes(fields)[route]

    resp = await _send(method, path, body)

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert [(e["type"], e["loc"]) for e in errors] == [(kind, [*loc, field])]
    assert all(set(e) == {"type", "loc", "msg"} for e in errors)
    if isinstance(fields[field], str) or fields[field] > PG_INT_MAX:
        assert str(fields[field]) not in resp.text


# ── 응답 매핑 ─────────────────────────────────────────────────────────────


def _row(**fields: Any) -> LLMModel:
    return LLMModel(
        id=uuid.uuid4(),
        provider_id=uuid.uuid4(),
        model_name="m",
        display_name="모델",
        capabilities=["llm"],
        is_default=False,
        enabled=True,
        context_window=32_768,
        cost_per_1k={},
        fallback_priority=None,
        **fields,
    )


def test_response_carries_both_fields() -> None:
    out = model_to_out(_row(reasoning_effort="low", max_output_tokens=8192), "p")

    assert (out.reasoning_effort, out.max_output_tokens) == ("low", 8192)


def test_unset_columns_are_null_in_the_response() -> None:
    out = model_to_out(_row(), "p")

    assert (out.reasoning_effort, out.max_output_tokens) == (None, None)
    dumped = out.model_dump()
    assert dumped["reasoning_effort"] is None and dumped["max_output_tokens"] is None


def test_response_built_by_older_code_still_validates() -> None:
    """두 필드를 모르는 이전 코드가 `ModelOut(...)`을 직접 만들어도 깨지지 않는다(#196 선례)."""
    out = ModelOut(
        id=uuid.uuid4(),
        provider_id=uuid.uuid4(),
        provider_name="p",
        model_name="m",
        display_name="모델",
        capabilities=["llm"],
        is_default=False,
        enabled=True,
        context_window=None,
        cost_per_1k={},
    )

    assert (out.reasoning_effort, out.max_output_tokens) == (None, None)


def test_response_refuses_a_stored_value_outside_the_enum() -> None:
    """DB에 허용 밖 값이 있으면 응답을 만들지 않는다 — OpenAPI enum 밖의 값을 내보내지 않는다."""
    with pytest.raises(ValidationError) as exc:
        model_to_out(_row(reasoning_effort="xhigh"), "p")

    assert [e["type"] for e in exc.value.errors()] == ["literal_error"]


# ── PATCH: 설정·해제 ──────────────────────────────────────────────────────


class _RecordingDb:
    """행을 돌려주고 DB 호출 순서를 기록한다(`tests/test_patch_null_rejection.py`와 같은 대역)."""

    def __init__(self, *rows: Any) -> None:
        self.rows = {(type(r), r.id): r for r in rows}
        self.calls: list[str] = []
        self.added: list[Any] = []

    async def get(self, model: type, key: uuid.UUID, **_kw: Any) -> Any:
        self.calls.append(f"get:{model.__name__}")
        return self.rows.get((model, key))

    def add(self, obj: Any) -> None:
        self.calls.append(f"add:{type(obj).__name__}")
        self.added.append(obj)

    async def commit(self) -> None:
        self.calls.append("commit")


def _stored(**fields: Any) -> tuple[LLMModel, LLMProvider, _RecordingDb]:
    provider = LLMProvider(id=uuid.uuid4(), name="local", type="openai")
    model = _row(**fields)
    model.provider_id = provider.id
    return model, provider, _RecordingDb(model, provider)


async def test_patch_sets_both_fields_and_audits_their_names() -> None:
    model, _provider, db = _stored()

    resp = await _send(
        "PATCH",
        f"/admin/llm/models/{model.id}",
        {"reasoning_effort": "medium", "max_output_tokens": 16_384},
        db,
    )

    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert (out["reasoning_effort"], out["max_output_tokens"]) == ("medium", 16_384)
    assert (model.reasoning_effort, model.max_output_tokens) == ("medium", 16_384)
    [audit] = db.added
    assert (audit.action, audit.detail) == (
        "llm.model_update",
        {"fields": ["max_output_tokens", "reasoning_effort"]},
    )


async def test_patch_null_clears_both_fields() -> None:
    model, _provider, db = _stored(reasoning_effort="high", max_output_tokens=4096)

    resp = await _send(
        "PATCH",
        f"/admin/llm/models/{model.id}",
        {"reasoning_effort": None, "max_output_tokens": None},
        db,
    )

    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert (out["reasoning_effort"], out["max_output_tokens"]) == (None, None)
    assert (model.reasoning_effort, model.max_output_tokens) == (None, None)
    assert db.calls == ["get:LLMModel", "add:AuditLog", "commit", "get:LLMProvider"]


async def test_patch_without_the_keys_keeps_the_values() -> None:
    model, _provider, db = _stored(reasoning_effort="low", max_output_tokens=2048)

    resp = await _send("PATCH", f"/admin/llm/models/{model.id}", {"display_name": "새 이름"}, db)

    assert resp.status_code == 200, resp.text
    assert (model.reasoning_effort, model.max_output_tokens) == ("low", 2048)
    assert (resp.json()["reasoning_effort"], resp.json()["max_output_tokens"]) == ("low", 2048)


async def test_bad_patch_writes_nothing() -> None:
    model, _provider, db = _stored(reasoning_effort="low", max_output_tokens=2048)

    resp = await _send(
        "PATCH",
        f"/admin/llm/models/{model.id}",
        {"reasoning_effort": "xhigh", "max_output_tokens": None},
        db,
    )

    assert resp.status_code == 422
    assert db.calls == []  # 스키마 검증이 핸들러보다 먼저다
    assert (model.reasoning_effort, model.max_output_tokens) == ("low", 2048)
