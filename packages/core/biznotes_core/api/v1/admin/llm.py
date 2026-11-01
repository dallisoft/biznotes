"""어드민 — LLM 게이트웨이 관리 (프로바이더/모델/default/임베딩 정책)."""

import uuid
from typing import Any

import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import any_, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.api.deps import get_current_user
from biznotes_core.api.patch import non_null_changes
from biznotes_core.api.routing import RedactedValidationRoute
from biznotes_core.core.crypto import encrypt
from biznotes_core.core.db import get_db
from biznotes_core.models import LLMModel, LLMProvider, User
from biznotes_core.schemas.llm import (
    DiscoveredModel,
    EmbeddingGateOut,
    ModelCreate,
    ModelImport,
    ModelOut,
    ModelSyncOut,
    ModelUpdate,
    ProviderCircuitOut,
    ProviderCreate,
    ProviderOut,
    ProviderUpdate,
    model_to_out,
    provider_to_out,
)
from biznotes_core.services.audit import record
from biznotes_core.services.llm_admin import (
    default_conflict,
    embedding_default_exists,
    embedding_model_exists,
    plan_model_import,
    reranker_default_exists,
)

router = APIRouter(prefix="/llm", route_class=RedactedValidationRoute)


async def _commit_or_409(db: AsyncSession) -> None:
    """Commit하고 능력별 default 경합만 결정적인 409로 변환한다."""
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        conflict = default_conflict(exc)
        if conflict is None:
            raise
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "default 지정이 동시에 변경되었습니다 — 다시 시도하세요",
        ) from conflict


@router.get("/providers", response_model=list[ProviderOut])
async def list_providers(db: AsyncSession = Depends(get_db)) -> list[ProviderOut]:
    from sqlalchemy import func

    rows = await db.execute(
        select(LLMProvider, func.count(LLMModel.id))
        .outerjoin(LLMModel, LLMModel.provider_id == LLMProvider.id)
        .group_by(LLMProvider.id)
        .order_by(LLMProvider.name)
    )
    return [provider_to_out(p, cnt) for p, cnt in rows.all()]


@router.post("/providers", response_model=ProviderOut, status_code=status.HTTP_201_CREATED)
async def create_provider(
    body: ProviderCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> ProviderOut:
    existing = (
        await db.execute(select(LLMProvider).where(LLMProvider.name == body.name))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 존재하는 프로바이더명입니다")
    provider = LLMProvider(
        name=body.name,
        type=body.type,
        base_url=body.base_url,
        api_key_encrypted=encrypt(body.api_key) if body.api_key else None,
        enabled=body.enabled,
        embedding_max_concurrency=body.embedding_max_concurrency,
    )
    db.add(provider)
    await record(
        db,
        admin.id,
        "llm.provider_create",
        body.name,
        {"type": body.type, "embedding_max_concurrency": body.embedding_max_concurrency},
    )
    await db.commit()
    await db.refresh(provider)
    return provider_to_out(provider)


@router.patch("/providers/{provider_id}", response_model=ProviderOut)
async def update_provider(
    provider_id: uuid.UUID,
    body: ProviderUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> ProviderOut:
    provider = await db.get(LLMProvider, provider_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Provider not found")
    if body.base_url is not None:
        provider.base_url = body.base_url
    if body.api_key is not None:
        provider.api_key_encrypted = encrypt(body.api_key) if body.api_key else None
    if body.enabled is not None:
        provider.enabled = body.enabled
    # [#196] 임베딩 동시성 상한. 본문에 키가 있으면 None이라도 반영한다 — NULL이 "제한 해제"다.
    # 키가 없으면 "바꾸지 않는다"로 둔다(다른 필드와 같은 규칙)
    detail: dict[str, Any] = {}
    if "embedding_max_concurrency" in body.model_fields_set:
        provider.embedding_max_concurrency = body.embedding_max_concurrency
        detail = {"embedding_max_concurrency": body.embedding_max_concurrency}
    await record(db, admin.id, "llm.provider_update", provider.name, detail)
    await db.commit()
    await db.refresh(provider)
    return provider_to_out(provider)


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    provider_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> None:
    provider = await db.get(LLMProvider, provider_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Provider not found")
    await record(db, admin.id, "llm.provider_delete", provider.name)
    await db.delete(provider)
    await db.commit()


_NO_TESTABLE_MODEL = (
    "테스트할 LLM·임베딩·리랭커 모델이 등록되어 있지 않습니다. 이미지 생성·STT·TTS 모델은 호출마다 "
    "비용이 들어 연결 테스트를 하지 않습니다 — 연결은 모델 가져오기 목록 조회로 확인하세요"
)


@router.post("/providers/{provider_id}/test")
async def test_provider(
    provider_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """프로바이더 연결 테스트 — LLM 모델이 있으면 1토큰 완성, 없으면 임베딩 1건, 그것도 없으면
    리랭크 1건(#209)을 호출한다. 이미지·STT·TTS 모델만 있으면 호출하지 않고 안내한다."""
    provider = await db.get(LLMProvider, provider_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Provider not found")
    if provider.type == "mock":
        return {"ok": True, "detail": "mock 프로바이더는 항상 사용 가능합니다"}

    from biznotes_core.services.gateway.llm import ResolvedModel, probe_connection

    # 이미지 생성·STT·TTS는 호출 비용이 커서 시험하지 않는다(#347)
    for capability in ("llm", "embedding", "reranker"):
        model = (
            (
                await db.execute(
                    select(LLMModel).where(
                        LLMModel.provider_id == provider_id,
                        literal(capability) == any_(LLMModel.capabilities),
                    )
                )
            )
            .scalars()
            .first()
        )
        if model is not None:
            resolved = ResolvedModel(model=model, provider=provider)
            if capability == "reranker":
                return await _probe_reranker(resolved)
            return await probe_connection(resolved, capability)
    return {"ok": False, "detail": _NO_TESTABLE_MODEL}


async def _probe_reranker(resolved: Any) -> dict[str, Any]:
    """리랭크 1건 — 게이트웨이 `/rerank` 경로와 응답 계약을 함께 확인한다."""
    from biznotes_core.services.rerank import (
        RerankUnavailableError,
        gateway_target,
        rerank_relevance,
    )

    try:
        target = gateway_target(resolved, timeout=15)
        scores = await anyio.to_thread.run_sync(rerank_relevance, target, "ping", ["pong"])
    except RerankUnavailableError as exc:
        return {"ok": False, "detail": str(exc)[:500]}
    return {"ok": True, "detail": f"리랭크 점수 수신: {scores[0]:.3f}"}


@router.get(
    "/providers/{provider_id}/available-models", response_model=list[DiscoveredModel]
)
async def discover_provider_models(
    provider_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> list[DiscoveredModel]:
    """프로바이더가 제공하는 모델 목록을 조회 (등록 전 선택용)."""
    provider = await db.get(LLMProvider, provider_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Provider not found")

    from biznotes_core.services.gateway.discovery import discover_provider_models

    # DiscoveryError(→502)는 전역 예외 핸들러가 매핑한다(R1-1).
    found = await discover_provider_models(provider)

    existing = {
        name
        for (name,) in (
            await db.execute(
                select(LLMModel.model_name).where(LLMModel.provider_id == provider_id)
            )
        ).all()
    }
    return [
        DiscoveredModel(**m, already_registered=m["model_name"] in existing) for m in found
    ]


@router.post("/providers/{provider_id}/models/import")
async def import_models(
    provider_id: uuid.UUID,
    body: ModelImport,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> dict[str, Any]:
    """디스커버리에서 선택한 모델들을 일괄 등록 (이미 등록된 model_name은 건너뜀)."""
    # 등록 판별 규칙은 services/llm_admin.plan_model_import가 소유한다 — 라우터는
    # 404 매핑과 감사 로그·커밋 경계만 다룬다. (docstring은 OpenAPI description으로
    # 노출되므로 내부 구조 설명을 넣지 않는다.)
    provider = await db.get(LLMProvider, provider_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Provider not found")

    result = await plan_model_import(db, provider_id, body.items)

    await record(
        db, admin.id, "llm.model_import", provider.name, {"created": len(result.created)}
    )
    await _commit_or_409(db)
    return {"created": len(result.created), "skipped": result.skipped}


@router.get("/providers/{provider_id}/models/sync-check", response_model=list[ModelSyncOut])
async def sync_check_provider_models(
    provider_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> list[ModelSyncOut]:
    """등록된 모델의 model_name이 프로바이더 목록에 여전히 존재하는지 검증(자동 삭제 없음).

    실행 이력 참조 무결성 보존을 위해 사라진 모델도 삭제하지 않고 배지로만 표시한다
    (phase-20-design.md §2.3).
    """
    provider = await db.get(LLMProvider, provider_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Provider not found")

    from biznotes_core.services.gateway.discovery import discover_provider_models

    # DiscoveryError(→502)는 전역 예외 핸들러가 매핑한다(R1-1).
    found = await discover_provider_models(provider)

    available_names = {m["model_name"] for m in found}
    rows = (
        await db.execute(
            select(LLMModel)
            .where(LLMModel.provider_id == provider_id)
            .order_by(LLMModel.display_name)
        )
    ).scalars().all()
    return [
        ModelSyncOut(
            id=m.id,
            model_name=m.model_name,
            display_name=m.display_name,
            still_available=m.model_name in available_names,
        )
        for m in rows
    ]


@router.get("/providers/status", response_model=list[ProviderCircuitOut])
async def providers_circuit_status(db: AsyncSession = Depends(get_db)) -> list[ProviderCircuitOut]:
    """프로바이더별 서킷 브레이커 상태 (정상/차단, 최근 실패 수)."""
    from biznotes_core.services.gateway import circuit

    rows = await db.execute(select(LLMProvider).order_by(LLMProvider.name))
    out: list[ProviderCircuitOut] = []
    for provider in rows.scalars().all():
        state = await circuit.get_status(provider.id)
        out.append(ProviderCircuitOut(id=provider.id, name=provider.name, **state))
    return out


@router.post("/providers/{provider_id}/circuit-reset")
async def reset_provider_circuit(
    provider_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> dict[str, bool]:
    """수동 차단 해제."""
    from biznotes_core.services.gateway import circuit

    provider = await db.get(LLMProvider, provider_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Provider not found")
    await circuit.reset(provider_id)
    await record(db, admin.id, "llm.circuit_reset", provider.name)
    await db.commit()
    return {"ok": True}


@router.get("/providers/{provider_id}/embedding-gate", response_model=EmbeddingGateOut)
async def provider_embedding_gate(
    provider_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> EmbeddingGateOut:
    """프로바이더의 임베딩 동시성 게이트 상태(#196).

    `in_use`이 `limit`에 붙어 있으면 상한이 걸리고 있다는 뜻이다. 상한이 너무 작게 잡힌 경우와
    실제로 정체된 경우를 구분하는 단서가 된다. 상한이 없으면(`limit=null`) 게이트는 저한 계층이라
    Redis를 만지지 않는다.
    """
    from biznotes_core.core import concurrency_gate

    provider = await db.get(LLMProvider, provider_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Provider not found")
    limit = provider.embedding_max_concurrency
    if limit is None:
        return EmbeddingGateOut(
            id=provider.id,
            name=provider.name,
            limit=None,
            in_use=0,
            earliest_expiry_in=None,
            last_error=None,
        )
    try:
        # `status()`는 동기 Redis로 3회 왕복한다. 스레드로 넘기지 않으면 Redis가 느릴 때
        # API 이벤트 루프 전체가 멈춘다(#207)
        state = await anyio.to_thread.run_sync(concurrency_gate.status, provider_id)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return EmbeddingGateOut(
        id=provider.id,
        name=provider.name,
        limit=limit,
        in_use=state.in_use,
        earliest_expiry_in=state.earliest_expiry_in,
        last_error=state.last_error,
    )


# ---------- models ----------


@router.get("/models", response_model=list[ModelOut])
async def list_models_admin(db: AsyncSession = Depends(get_db)) -> list[ModelOut]:
    rows = await db.execute(
        select(LLMModel, LLMProvider.name)
        .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
        .order_by(LLMModel.display_name)
    )
    return [model_to_out(m, pname) for m, pname in rows.all()]


@router.post("/models", response_model=ModelOut, status_code=status.HTTP_201_CREATED)
async def create_model(
    body: ModelCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> ModelOut:
    provider = await db.get(LLMProvider, body.provider_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Provider not found")

    is_embedding = "embedding" in body.capabilities
    # 동시에 쓰는 임베딩 모델은 1개다. 단 **비활성 모델은 세지 않는다** — 교체 중에는
    # 이전 모델 행이 남아 있어야 그 모델로 만든 벡터를 계속 질의할 수 있다(#7).
    # 교체: 기존 모델 비활성화 → 새 모델 등록 → set-default → stale_only 재인덱싱 →
    # 기존 모델 삭제.
    if is_embedding and await embedding_model_exists(db):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "임베딩 모델은 전역 1개입니다. 교체하려면 기존 모델을 비활성화(enabled=false)한 뒤 "
            "새 모델을 등록하고, 기본값으로 지정한 다음 재인덱싱하세요 "
            "(/admin/rag/reindex, stale_only). 기존 모델은 재인덱싱이 끝난 뒤 삭제합니다 — "
            "먼저 삭제하면 그 모델로 만든 벡터를 재인덱싱 전까지 검색할 수 없습니다.",
        )
    # 기본값은 능력별 1개라 기존 기본값 행이 남아 있으면 비기본으로 만든다(set-default로 전환).
    # 리랭커는 여러 개 활성일 수 있고, 기본 리랭커가 없을 때 첫 리랭커만 기본값이 된다(#209)
    is_default = is_embedding and not await embedding_default_exists(db)
    if (
        not is_embedding
        and "reranker" in body.capabilities
        and not await reranker_default_exists(db)
    ):
        is_default = True

    model = LLMModel(
        provider_id=body.provider_id,
        model_name=body.model_name,
        display_name=body.display_name,
        capabilities=body.capabilities,
        context_window=body.context_window,
        enabled=body.enabled,
        is_default=is_default,  # 첫 임베딩·첫 리랭커 모델만 즉시 default
        cost_per_1k=body.cost_per_1k or {},
        reasoning_effort=body.reasoning_effort,
        max_output_tokens=body.max_output_tokens,
    )
    db.add(model)
    await record(
        db,
        admin.id,
        "llm.model_create",
        body.model_name,
        {
            "capabilities": body.capabilities,
            "reasoning_effort": body.reasoning_effort,
            "max_output_tokens": body.max_output_tokens,
        },
    )
    await _commit_or_409(db)
    await db.refresh(model)
    return model_to_out(model, provider.name)


@router.patch("/models/{model_id}", response_model=ModelOut)
async def update_model(
    model_id: uuid.UUID,
    body: ModelUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> ModelOut:
    model = await db.get(LLMModel, model_id)
    if model is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Model not found")
    # context_window·fallback_priority·reasoning_effort·max_output_tokens의 null은 해제다(nullable
    # 열). 나머지 NOT NULL 필드의 null은 커밋 때 무결성 오류(500)가 되므로 먼저 거부한다(#267).
    fields = non_null_changes(
        body,
        nullable=(
            "context_window",
            "fallback_priority",
            "cost_per_1k",
            "reasoning_effort",
            "max_output_tokens",
        ),
    )
    # cost_per_1k는 JSONB NOT NULL 컬럼 — None(명시적 null 전송)은 "단가 미설정"으로
    # 정규화해 {}로 저장한다(CDX20B-02, None을 그대로 setattr하면 저장 실패).
    if "cost_per_1k" in fields and fields["cost_per_1k"] is None:
        fields["cost_per_1k"] = {}
    new_caps = fields.get("capabilities")
    # 임베딩 능력 추가도 같은 규칙 — 활성 임베딩 모델이 이미 있으면 막는다
    if (
        new_caps is not None
        and "embedding" in new_caps
        and fields.get("enabled", model.enabled)
        and await embedding_model_exists(db, exclude_id=model.id)
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "임베딩 모델은 전역 1개입니다. 교체하려면 기존 모델을 비활성화(enabled=false)한 뒤 "
            "지정하세요.",
        )
    # default 행의 능력 변경만 다른 default와 겹치는지 사전 검증한다.
    if (
        model.is_default is True
        and new_caps is not None
        and set(new_caps) != set(model.capabilities)
    ):
        overlapping_default = (
            await db.execute(
                select(LLMModel.id).where(
                    LLMModel.id != model.id,
                    LLMModel.is_default.is_(True),
                    LLMModel.capabilities.overlap(new_caps),
                )
            )
        ).first()
        if overlapping_default is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "변경할 능력에 이미 default 모델이 있습니다.",
            )
    for field, value in fields.items():
        setattr(model, field, value)
    await record(db, admin.id, "llm.model_update", model.model_name, {"fields": sorted(fields)})
    await _commit_or_409(db)
    provider = await db.get(LLMProvider, model.provider_id)
    return model_to_out(model, provider.name if provider else "")


@router.post("/models/{model_id}/set-default", response_model=ModelOut)
async def set_default_model(
    model_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> ModelOut:
    """능력별 default 모델 지정 — 능력이 겹치는 기존 default를 해제해 능력별 유일성 보장."""
    model = await db.get(LLMModel, model_id)
    if model is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Model not found")
    target_caps = set(model.capabilities)
    current_defaults = (
        (await db.execute(select(LLMModel).where(LLMModel.is_default.is_(True))))
        .scalars()
        .all()
    )
    for other in current_defaults:
        if other.id != model.id and target_caps & set(other.capabilities):
            other.is_default = False
    await db.flush()
    model.is_default = True
    await record(db, admin.id, "llm.model_set_default", model.model_name)
    await _commit_or_409(db)
    provider = await db.get(LLMProvider, model.provider_id)
    return model_to_out(model, provider.name if provider else "")


@router.delete("/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model(
    model_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> None:
    model = await db.get(LLMModel, model_id)
    if model is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Model not found")
    await record(db, admin.id, "llm.model_delete", model.model_name)
    await db.delete(model)
    await db.commit()
