"""이미지 생성 모달리티 (#338) — 실제 PostgreSQL(부분 유니크 인덱스 포함).

필요: PostgreSQL·Valkey(PG 테스트). `create_all` 기반이라 제품 Alembic 리비전
(`uq_llm_models_default_image`) 검증이 아니다.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.services.gateway.llm import GatewayError, resolve_model
from biznotes_core.services.llm_admin import default_conflict


@pytest.fixture
async def provider(db) -> LLMProvider:
    p = LLMProvider(name=f"img-{uuid.uuid4().hex[:6]}", type="mock", enabled=True)
    db.add(p)
    await db.commit()
    return p


def _model(provider: LLMProvider, name: str, caps: list[str], *, default: bool) -> LLMModel:
    return LLMModel(
        provider_id=provider.id,
        model_name=name,
        display_name=name,
        capabilities=caps,
        is_default=default,
        enabled=True,
        cost_per_1k={"per_image": 0.04} if caps == ["image"] else {},
    )


async def test_one_default_image_model_and_it_is_independent_of_llm(db, provider) -> None:
    db.add_all(
        [
            _model(provider, "chat", ["llm"], default=True),
            _model(provider, "img-a", ["image"], default=True),
            _model(provider, "img-b", ["image"], default=False),
        ]
    )
    await db.commit()

    resolved = await resolve_model(db, None, modality="image")
    assert resolved.model.model_name == "img-a"
    assert resolved.model.cost_per_1k == {"per_image": 0.04}
    assert (await resolve_model(db, None, modality="llm")).model.model_name == "chat"

    db.add(_model(provider, "img-c", ["image"], default=True))
    with pytest.raises(IntegrityError) as exc:
        await db.commit()
    await db.rollback()
    assert "uq_llm_models_default_image" in str(exc.value)
    assert default_conflict(exc.value)


async def test_without_an_image_model_resolution_fails_clearly(db, provider) -> None:
    db.add(_model(provider, "chat", ["llm"], default=True))
    await db.commit()

    with pytest.raises(GatewayError, match="등록된 image 모델이 없습니다"):
        await resolve_model(db, None, modality="image")


async def test_import_registers_discovered_image_models_outside_the_chat_list(db, provider) -> None:
    """모델 가져오기(#347): 디스커버리 분류 그대로 등록하면 이미지 목록에만 나오고, 단건 등록처럼
    기본값을 자동으로 정하지 않는다 — 기본값이 없어도 첫 활성 이미지 모델로 해석된다."""
    from biznotes_core.schemas.llm import ModelImportItem
    from biznotes_core.services.gateway.discovery import _capabilities
    from biznotes_core.services.gateway.llm import list_models
    from biznotes_core.services.llm_admin import plan_model_import

    names = ["gpt-image-1", "dall-e-3", "gpt-4o"]
    items = [
        ModelImportItem(model_name=n, display_name=n, capabilities=_capabilities(n)) for n in names
    ]

    result = await plan_model_import(db, provider.id, items)
    await db.commit()

    caps = {m.model_name: m.capabilities for m in result.created}
    assert caps == {"gpt-image-1": ["image"], "dall-e-3": ["image"], "gpt-4o": ["llm"]}
    assert not any(m.is_default for m in result.created)
    image_names = {r.model.model_name for r in await list_models(db, modality="image")}
    chat_names = {r.model.model_name for r in await list_models(db, modality="llm")}
    assert {"gpt-image-1", "dall-e-3"} <= image_names
    assert not {"gpt-image-1", "dall-e-3"} & chat_names
    assert (await resolve_model(db, None, modality="image")).model.model_name in image_names


async def test_import_keeps_the_existing_default_image_model(db, provider) -> None:
    from biznotes_core.schemas.llm import ModelImportItem
    from biznotes_core.services.llm_admin import plan_model_import

    db.add(_model(provider, "img-old", ["image"], default=True))
    await db.commit()

    result = await plan_model_import(
        db,
        provider.id,
        [
            ModelImportItem(model_name=f"img-{i}", display_name=f"i{i}", capabilities=["image"])
            for i in range(3)
        ],
    )
    await db.commit()  # 부분 유니크 인덱스와 충돌하지 않는다

    assert [m.is_default for m in result.created] == [False, False, False]
    assert (await resolve_model(db, None, modality="image")).model.model_name == "img-old"
