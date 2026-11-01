"""능력별 LLM default partial unique index 테스트."""

import uuid

import pytest
from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.models.llm import MODALITIES
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


async def _provider(db) -> LLMProvider:
    provider = LLMProvider(name=f"default-{uuid.uuid4().hex[:8]}", type="mock", enabled=True)
    db.add(provider)
    await db.flush()
    return provider


def _model(provider_id: uuid.UUID, name: str, capabilities: list[str]) -> LLMModel:
    return LLMModel(
        provider_id=provider_id,
        model_name=name,
        display_name=name,
        capabilities=capabilities,
        is_default=True,
        enabled=True,
    )


async def test_같은_능력의_default_두_행은_DB가_차단한다(db) -> None:
    provider = await _provider(db)
    db.add_all(
        [
            _model(provider.id, "llm-a", ["llm"]),
            _model(provider.id, "llm-b", ["llm"]),
        ]
    )

    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_서로_다른_능력의_default는_공존한다(db) -> None:
    provider = await _provider(db)
    capabilities = ("llm", "vlm", "embedding", "reranker")
    db.add_all([_model(provider.id, capability, [capability]) for capability in capabilities])

    await db.commit()

    rows = (await db.execute(text("SELECT capabilities FROM llm_models WHERE is_default"))).all()
    assert {row.capabilities[0] for row in rows} == set(capabilities)


async def test_다능력_default와_겹치는_default는_DB가_차단한다(db) -> None:
    provider = await _provider(db)
    db.add_all(
        [
            _model(provider.id, "omni", ["llm", "vlm"]),
            _model(provider.id, "vlm-only", ["vlm"]),
        ]
    )

    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_default_partial_unique_index가_능력마다_하나씩_존재한다(db) -> None:
    rows = await db.execute(
        text(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = 'llm_models'
              AND indexname LIKE 'uq_llm_models_default_%'
            """
        )
    )
    assert {row.indexname for row in rows} == {
        f"uq_llm_models_default_{modality}" for modality in MODALITIES
    }
