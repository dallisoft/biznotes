"""리랭커 해석·어드민 규칙 (#209) — 실제 PostgreSQL(부분 유니크 인덱스 포함).

필요: PostgreSQL·Valkey(PG 테스트). `create_all` 기반이라 제품 Alembic 리비전
(`uq_llm_models_default_reranker`) 검증이 아니다.
"""

import uuid

import pytest
from sqlalchemy import select

from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.services.rerank import (
    DEFAULT_RERANK_MODEL,
    RerankUnavailableError,
    gateway_rerank_key,
    resolve_reranker,
)


@pytest.fixture
async def provider(db) -> LLMProvider:
    p = LLMProvider(
        name=f"gw-{uuid.uuid4().hex[:6]}",
        type="custom",
        base_url="http://gw.test/v1",
        enabled=True,
    )
    db.add(p)
    await db.commit()
    return p


async def _reranker(
    db, provider: LLMProvider, name: str, *, default: bool = False, enabled: bool = True
) -> LLMModel:
    m = LLMModel(
        provider_id=provider.id,
        model_name=name,
        display_name=name,
        capabilities=["reranker"],
        is_default=default,
        enabled=enabled,
    )
    db.add(m)
    await db.commit()
    return m


async def _defaults(db, provider_id: uuid.UUID) -> list[str]:
    q = select(LLMModel.model_name).where(
        LLMModel.provider_id == provider_id, LLMModel.is_default.is_(True)
    )
    return list((await db.execute(q)).scalars().all())


# ── 어드민 규칙 (C3·M3) ─────────────────────────────────────────────────


async def test_batch_import_of_two_rerankers_makes_one_default(db, provider, admin_user) -> None:
    """둘 다 default면 `uq_llm_models_default_reranker` 위반으로 배치 전체가 409였다(리뷰 M3)."""
    from biznotes_core.api.v1.admin.llm import import_models
    from biznotes_core.schemas.llm import ModelImport

    body = ModelImport.model_validate(
        {
            "items": [
                {"model_name": "openai/r1", "display_name": "r1", "capabilities": ["reranker"]},
                {"model_name": "openai/r2", "display_name": "r2", "capabilities": ["reranker"]},
            ]
        }
    )
    assert await import_models(provider.id, body, db, admin_user) == {"created": 2, "skipped": []}
    assert await _defaults(db, provider.id) == ["openai/r1"]


async def test_rerankers_are_not_limited_to_one_active(db, provider, admin_user) -> None:
    from biznotes_core.api.v1.admin.llm import create_model
    from biznotes_core.schemas.llm import ModelCreate

    async def create(name: str) -> None:
        await create_model(
            ModelCreate(
                provider_id=provider.id,
                model_name=name,
                display_name=name,
                capabilities=["reranker"],
            ),
            db,
            admin_user,
        )

    await create("openai/a")
    await create("openai/b")  # 임베딩과 달리 409가 아니다
    assert await _defaults(db, provider.id) == ["openai/a"]


async def test_disabled_default_reranker_keeps_new_ones_non_default(db, provider) -> None:
    from biznotes_core.schemas.llm import ModelImportItem
    from biznotes_core.services.llm_admin import plan_model_import

    await _reranker(db, provider, "openai/old", default=True, enabled=False)
    result = await plan_model_import(
        db,
        provider.id,
        [ModelImportItem(model_name="openai/new", display_name="n", capabilities=["reranker"])],
    )
    assert result.created[0].is_default is False
    await db.commit()  # 부분 유니크 인덱스와 충돌하지 않는다


async def test_reranker_default_does_not_touch_the_llm_default(db, provider, admin_user) -> None:
    from biznotes_core.api.v1.admin.llm import set_default_model

    llm = LLMModel(
        provider_id=provider.id,
        model_name="openai/chat",
        display_name="chat",
        capabilities=["llm"],
        is_default=True,
    )
    db.add(llm)
    await db.commit()
    rr = await _reranker(db, provider, "openai/r")

    await set_default_model(rr.id, db, admin_user)

    assert sorted(await _defaults(db, provider.id)) == ["openai/chat", "openai/r"]


# ── 해석 (C5) ───────────────────────────────────────────────────────────


async def test_auto_without_registered_reranker_is_local(db) -> None:
    target = await resolve_reranker(db, "")
    assert target.local_model == DEFAULT_RERANK_MODEL
    assert target.gateway is None


async def test_auto_uses_the_enabled_default_reranker(db, provider) -> None:
    await _reranker(db, provider, "openai/bge-reranker-v2-m3", default=True)

    target = await resolve_reranker(db, "", rerank_timeout=12)

    assert target.gateway is not None
    assert target.gateway.model == "bge-reranker-v2-m3"
    assert target.gateway.api_base == "http://gw.test/v1"
    assert target.gateway.timeout == 12


async def test_auto_ignores_a_disabled_default(db, provider) -> None:
    await _reranker(db, provider, "openai/r", default=True, enabled=False)
    assert (await resolve_reranker(db, "")).local_model == DEFAULT_RERANK_MODEL


async def test_registered_reranker_does_not_override_an_explicit_value(db, provider) -> None:
    """명시값이 정본이다 — 업그레이드·등록만으로 리랭커가 바뀌지 않는다."""
    await _reranker(db, provider, "openai/r", default=True)
    target = await resolve_reranker(db, DEFAULT_RERANK_MODEL)
    assert target.local_model == DEFAULT_RERANK_MODEL


async def test_explicit_gateway_key_resolves(db, provider) -> None:
    m = await _reranker(db, provider, "openai/r")
    target = await resolve_reranker(db, gateway_rerank_key(m.id, m.model_name))
    assert target.gateway is not None and target.gateway.model == "r"


@pytest.mark.parametrize("broken", ["renamed", "deleted", "model_off", "provider_off", "llm"])
async def test_explicit_gateway_key_fails_loudly(db, provider, broken: str) -> None:
    m = await _reranker(db, provider, "openai/r")
    key = gateway_rerank_key(m.id, m.model_name)
    if broken == "renamed":
        m.model_name = "openai/r2"
    elif broken == "deleted":
        await db.delete(m)
    elif broken == "model_off":
        m.enabled = False
    elif broken == "provider_off":
        provider.enabled = False
    else:
        m.capabilities = ["llm"]
    await db.commit()

    with pytest.raises(RerankUnavailableError):
        await resolve_reranker(db, key)


# ── 어드민 API (C7) ─────────────────────────────────────────────────────


async def test_reranker_status_lists_options_and_active(db, provider) -> None:
    from biznotes_core.api.v1.admin.rag import reranker_status

    m = await _reranker(db, provider, "openai/r")

    out = await reranker_status(db)

    assert out.saved_key == DEFAULT_RERANK_MODEL
    assert out.active_key == f"fastembed:{DEFAULT_RERANK_MODEL}"
    assert out.error is None
    keys = {o.key: o.available for o in out.options}
    assert keys == {
        gateway_rerank_key(m.id, m.model_name): True,
        f"fastembed:{DEFAULT_RERANK_MODEL}": True,
    }


async def test_reranker_status_flags_a_broken_saved_value(db, provider, admin_user) -> None:
    from biznotes_core.api.v1.admin.rag import RagSettings, reranker_status, update_rag_settings

    m = await _reranker(db, provider, "openai/r")
    key = gateway_rerank_key(m.id, m.model_name)
    await update_rag_settings(
        RagSettings(chunk_size=800, chunk_overlap=100, top_k=6, rerank_model=key), db, admin_user
    )
    await db.delete(m)
    await db.commit()

    out = await reranker_status(db)

    assert out.saved_key == key
    assert out.active_key is None and out.error
    assert {o.key: o.available for o in out.options}[key] is False


async def test_put_keeps_a_bare_legacy_name(db, admin_user) -> None:
    """접두사 없는 값을 바꾸지 않아야 구 코어로 롤백해도 그대로 읽힌다(r4)."""
    from biznotes_core.api.v1.admin.rag import RagSettings, update_rag_settings
    from biznotes_core.services.settings_store import get_setting

    await update_rag_settings(
        RagSettings(chunk_size=800, chunk_overlap=100, top_k=6, rerank_model="BAAI/x"),
        db,
        admin_user,
    )
    assert (await get_setting(db, "rag"))["rerank_model"] == "BAAI/x"


async def test_reranker_status_marks_uncallable_models(db, admin_user) -> None:
    """미지원 타입·base_url 없는 리랭커는 고르면 바로 실패한다 — 선택지에 미리 드러낸다."""
    from biznotes_core.api.v1.admin.rag import reranker_status

    ollama = LLMProvider(name=f"o-{uuid.uuid4().hex[:6]}", type="ollama", enabled=True)
    bare = LLMProvider(name=f"c-{uuid.uuid4().hex[:6]}", type="custom", enabled=True)
    db.add_all([ollama, bare])
    await db.commit()
    a = await _reranker(db, ollama, "x-rerank")
    b = await _reranker(db, bare, "openai/r")

    out = await reranker_status(db)

    avail = {o.key: o.available for o in out.options}
    assert avail[gateway_rerank_key(a.id, a.model_name)] is False
    assert avail[gateway_rerank_key(b.id, b.model_name)] is False
