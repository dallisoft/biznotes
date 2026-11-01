"""관리자 LLM 모델 capabilities를 `list[Modality]`로 선언한다 (#218).

OpenAPI `capabilities.items`에 능력 enum이 나오고, 받아들이는 값·정규화는 그대로다. 미지 값의 422는
Pydantic `literal_error`이고, 관리자 LLM 라우터(`RedactedValidationRoute`)가 입력값을 지운다.
스키마 검증 422는 핸들러 전에 나므로 DB 세션은 쓰지 않는다(`get_db` → None).
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
from biznotes_core.models import LLMModel, User
from biznotes_core.schemas.enums import Modality
from biznotes_core.schemas.llm import (
    DiscoveredModel,
    ModelCreate,
    ModelImportItem,
    ModelUpdate,
    model_to_out,
)

MODALITY_VALUES = list(get_args(Modality))


def _app() -> FastAPI:
    admin = APIRouter(prefix="/admin", dependencies=[Depends(deps.require_admin)])
    admin.include_router(llm.router)
    app = FastAPI()
    app.include_router(admin)
    me = User(id=uuid.uuid4(), email="admin@test.dev", role="admin", is_active=True)
    app.dependency_overrides[deps.get_current_user] = lambda: me
    app.dependency_overrides[get_db] = lambda: None
    return app


async def _send(method: str, path: str, body: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.request(method, path, json=body)


def _create(caps: Any) -> dict[str, Any]:
    return {
        "provider_id": str(uuid.uuid4()),
        "model_name": "m",
        "display_name": "모델",
        "capabilities": caps,
    }


def _routes(caps: Any) -> list[tuple[str, str, Any, list[Any]]]:
    """(method, path, body, capabilities 위치) — 생성·수정·가져오기."""
    return [
        ("POST", "/admin/llm/models", _create(caps), ["body", "capabilities"]),
        (
            "PATCH",
            f"/admin/llm/models/{uuid.uuid4()}",
            {"capabilities": caps},
            ["body", "capabilities"],
        ),
        (
            "POST",
            f"/admin/llm/providers/{uuid.uuid4()}/models/import",
            {"items": [{"model_name": "m", "display_name": "모델", "capabilities": caps}]},
            ["body", "items", 0, "capabilities"],
        ),
    ]


def test_openapi_exposes_modality_enum() -> None:
    schemas = _app().openapi()["components"]["schemas"]
    item = {"type": "string", "enum": MODALITY_VALUES}

    for name in ("ModelCreate", "ModelImportItem", "ModelOut", "DiscoveredModel"):
        assert schemas[name]["properties"]["capabilities"]["items"] == item, name
    update = schemas["ModelUpdate"]["properties"]["capabilities"]["anyOf"]
    assert update == [{"items": item, "type": "array"}, {"type": "null"}]


@pytest.mark.parametrize("route", range(3), ids=["create", "update", "import"])
@pytest.mark.parametrize("bad", ["chat", "LLM"])
async def test_unknown_capability_is_literal_error_without_input(route: int, bad: str) -> None:
    method, path, body, loc = _routes(["llm", bad])[route]

    resp = await _send(method, path, body)

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    # 잘못된 항목 하나만, 위치까지 짚는다. 입력값·기대값(ctx)은 싣지 않는다
    assert [(e["type"], e["loc"]) for e in errors] == [("literal_error", [*loc, 1])]
    assert all(set(e) == {"type", "loc", "msg"} for e in errors)
    assert bad not in resp.text


@pytest.mark.parametrize("route", range(3), ids=["create", "update", "import"])
@pytest.mark.parametrize(
    ("caps", "message"),
    [
        ([], "최소 1개의 능력(capability)이 필요합니다"),
        (["reranker", "llm"], "reranker 능력은 다른 능력과 함께 둘 수 없습니다"),
        (["llm", "image"], "image 능력은 다른 능력과 함께 둘 수 없습니다"),
    ],
    ids=["empty", "reranker-mixed", "image-mixed"],
)
async def test_other_rules_keep_their_messages(route: int, caps: list[str], message: str) -> None:
    method, path, body, loc = _routes(caps)[route]

    resp = await _send(method, path, body)

    assert resp.status_code == 422
    assert resp.json()["detail"] == [
        {"type": "value_error", "loc": loc, "msg": f"Value error, {message}"}
    ]


@pytest.mark.parametrize(
    "schema",
    [
        lambda caps: ModelCreate.model_validate(_create(caps)),
        lambda caps: ModelUpdate.model_validate({"capabilities": caps}),
        lambda caps: ModelImportItem.model_validate(
            {"model_name": "m", "display_name": "d", "capabilities": caps}
        ),
    ],
    ids=["create", "update", "import"],
)
def test_valid_values_are_deduplicated_in_order(schema: Any) -> None:
    assert schema(["vlm", "llm", "vlm"]).capabilities == ["vlm", "llm"]
    shared = [m for m in MODALITY_VALUES if m not in ("reranker", "image")]  # 단독 능력 제외
    assert schema(shared).capabilities == shared
    assert schema(["reranker"]).capabilities == ["reranker"]
    assert schema(["image"]).capabilities == ["image"]


def test_create_default_is_llm() -> None:
    body = _create(["llm"])
    del body["capabilities"]

    assert ModelCreate.model_validate(body).capabilities == ["llm"]


def _row(caps: list[str]) -> LLMModel:
    return LLMModel(
        id=uuid.uuid4(),
        provider_id=uuid.uuid4(),
        model_name="m",
        display_name="모델",
        capabilities=caps,
        is_default=False,
        enabled=True,
        context_window=None,
        cost_per_1k={},
        fallback_priority=None,
    )


def test_response_keeps_the_enum_contract() -> None:
    """저장 값이 `Modality` 밖이면 응답을 만들지 않는다 — OpenAPI enum 밖의 값을 내보내지 않는다."""
    assert model_to_out(_row(["llm", "vlm"]), "p").capabilities == ["llm", "vlm"]

    with pytest.raises(ValidationError) as exc:
        model_to_out(_row(["llm", "chat"]), "p")
    assert [e["type"] for e in exc.value.errors()] == ["literal_error"]


def test_discovered_model_keeps_the_enum_contract() -> None:
    """디스커버리 응답 항목도 같다 — 매핑 값은 `test_gateway_discovery.py`가 정본에 묶는다."""
    base = {"model_name": "m", "raw_id": "m", "display_name": "m"}
    assert DiscoveredModel(**base, capabilities=["llm", "vlm"]).capabilities == ["llm", "vlm"]

    with pytest.raises(ValidationError) as exc:
        DiscoveredModel(**base, capabilities=["video"])
    assert [e["type"] for e in exc.value.errors()] == ["literal_error"]
