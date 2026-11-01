"""어드민 LLM 관리 API의 요청·응답 스키마 (R3-3).

`api/v1/admin/llm.py`에 라우터와 뒤섞여 있던 Pydantic 모델·검증기·ORM→DTO
매퍼를 옮겼다. 라우터는 HTTP 관심사만, 스키마는 계약만 갖는다.
"""

import uuid
from typing import cast

from pydantic import BaseModel, Field, field_validator

from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.schemas.enums import Modality, ProviderType, ReasoningEffort


def _default_capabilities() -> list[Modality]:
    return ["llm"]


_EXCLUSIVE_CAPABILITIES: tuple[Modality, ...] = ("reranker", "image")
# PostgreSQL Integer 상한 — 넘는 값은 저장 때 범위 오류(500)가 되므로 요청 검증(422)에서 막는다
_PG_INT_MAX = 2_147_483_647
# 하한 — 하한이 따로 없던 필드도 열 범위 밖은 막는다. 0·음수는 지금처럼 받는다
_PG_INT_MIN = -2_147_483_648


def _validate_capabilities(v: list[Modality]) -> list[Modality]:
    """capabilities: 비어있지 않고, 중복 제거. 값 집합은 `Modality` 타입이 먼저 검증한다(#218)."""
    if not v:
        raise ValueError("최소 1개의 능력(capability)이 필요합니다")
    seen: list[Modality] = []
    for c in v:
        if c not in seen:
            seen.append(c)
    # 리랭커(#209)·이미지 생성(#338)은 단독 능력이다 — `is_default`는 능력 사이에 공유되는 값
    # 하나라, 다른 능력과 섞이면 첫 리랭커 자동 기본값이 채팅 기본 LLM이 되거나 기본 채팅 모델과
    # 기본 이미지 모델이 함께 바뀐다
    for exclusive in _EXCLUSIVE_CAPABILITIES:
        if exclusive in seen and len(seen) > 1:
            raise ValueError(f"{exclusive} 능력은 다른 능력과 함께 둘 수 없습니다")
    return seen


def _validate_cost_per_1k(v: dict[str, float] | None) -> dict[str, float] | None:
    """cost_per_1k: input/output(1k 토큰당, compute_cost가 소비)·per_image(장당, #338) 키만 허용."""
    if v is None:
        return v
    allowed = {"input", "output", "per_image"}
    unknown = set(v) - allowed
    if unknown:
        raise ValueError(
            "cost_per_1k는 input/output/per_image 키만 허용합니다 "
            f"(알 수 없는 키: {sorted(unknown)})"
        )
    for k, val in v.items():
        if val < 0:
            raise ValueError(f"cost_per_1k.{k}는 음수일 수 없습니다")
    return v


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    type: str = Field(
        pattern="^(openai|anthropic|google|ollama|vllm|lmstudio|azure|bedrock|mock|custom)$"
    )
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool = True
    # [#196] 임베딩 서버의 동시 처리 한도. 생략/None = 제한 없음(지금 동작).
    # 0과 음수는 "무제한"으로 읽히면 안 된다 — 무제한은 NULL이다
    embedding_max_concurrency: int | None = Field(default=None, ge=1, le=64)


class ProviderUpdate(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool | None = None
    # [#196] 본문에 키가 있고 값이 None이면 NULL로 되돌려 제한을 해제한다
    # (`model_fields_set`으로 "바꾸지 않는다"와 구분한다)
    embedding_max_concurrency: int | None = Field(default=None, ge=1, le=64)


class ProviderOut(BaseModel):
    id: uuid.UUID
    name: str
    type: ProviderType
    base_url: str | None
    has_api_key: bool
    enabled: bool
    model_count: int = 0
    # 기본값을 둔다 — 이 필드를 모르는 이전 코드가 `ProviderOut(...)`를 직접 만들어도
    # 깨지지 않아야 한다(AGENTS.md §5 공유 계약)
    embedding_max_concurrency: int | None = None


class ModelCreate(BaseModel):
    provider_id: uuid.UUID
    model_name: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    capabilities: list[Modality] = Field(default_factory=_default_capabilities)
    context_window: int | None = Field(default=None, ge=_PG_INT_MIN, le=_PG_INT_MAX)
    enabled: bool = True
    cost_per_1k: dict[str, float] | None = None
    # [#429] 사고 양 기본값. 생략/None = 보내지 않는다(프로바이더 기본)
    reasoning_effort: ReasoningEffort | None = None
    # [#428] 모델 최대 출력 토큰. 생략/None = 모른다. 0과 음수, Integer 열 범위 밖은 받지 않는다
    max_output_tokens: int | None = Field(default=None, ge=1, le=_PG_INT_MAX)

    @field_validator("capabilities")
    @classmethod
    def _check_caps(cls, v: list[Modality]) -> list[Modality]:
        return _validate_capabilities(v)

    @field_validator("cost_per_1k")
    @classmethod
    def _check_cost(cls, v: dict[str, float] | None) -> dict[str, float] | None:
        return _validate_cost_per_1k(v)


class ModelUpdate(BaseModel):
    display_name: str | None = None
    enabled: bool | None = None
    context_window: int | None = Field(default=None, ge=_PG_INT_MIN, le=_PG_INT_MAX)
    fallback_priority: int | None = Field(default=None, ge=_PG_INT_MIN, le=_PG_INT_MAX)
    capabilities: list[Modality] | None = None
    cost_per_1k: dict[str, float] | None = None
    # [#429·#428] 본문에 키를 두고 None을 보내면 해제다(NULL 열, `non_null_changes(nullable=…)`)
    reasoning_effort: ReasoningEffort | None = None
    max_output_tokens: int | None = Field(default=None, ge=1, le=_PG_INT_MAX)

    @field_validator("capabilities")
    @classmethod
    def _check_caps(cls, v: list[Modality] | None) -> list[Modality] | None:
        return _validate_capabilities(v) if v is not None else v

    @field_validator("cost_per_1k")
    @classmethod
    def _check_cost(cls, v: dict[str, float] | None) -> dict[str, float] | None:
        return _validate_cost_per_1k(v)


class ModelOut(BaseModel):
    id: uuid.UUID
    provider_id: uuid.UUID
    provider_name: str
    model_name: str
    display_name: str
    capabilities: list[Modality]
    is_default: bool
    enabled: bool
    context_window: int | None
    fallback_priority: int | None = None
    cost_per_1k: dict[str, float]
    # 기본값을 둔다 — 이 필드를 모르는 이전 코드가 `ModelOut(...)`를 직접 만들어도 깨지지 않아야
    # 한다(`ProviderOut.embedding_max_concurrency`와 같다)
    reasoning_effort: ReasoningEffort | None = None
    max_output_tokens: int | None = None


def model_to_out(m: LLMModel, provider_name: str) -> ModelOut:
    return ModelOut(
        id=m.id,
        provider_id=m.provider_id,
        provider_name=provider_name,
        model_name=m.model_name,
        display_name=m.display_name,
        # DB 열은 str 배열이다. 값은 검증한 요청으로만 기록되고 ModelOut 생성이 다시 검증한다(#218)
        capabilities=cast(list[Modality], list(m.capabilities)),
        is_default=m.is_default,
        enabled=m.enabled,
        context_window=m.context_window,
        fallback_priority=m.fallback_priority,
        cost_per_1k=dict(m.cost_per_1k or {}),
        # DB 열은 str이다. 검증한 요청으로만 기록되고 ModelOut 생성이 다시 검증한다
        reasoning_effort=cast(ReasoningEffort | None, m.reasoning_effort),
        max_output_tokens=m.max_output_tokens,
    )


def provider_to_out(p: LLMProvider, model_count: int = 0) -> ProviderOut:
    return ProviderOut(
        id=p.id,
        name=p.name,
        # DB 컬럼은 Mapped[str] — 값은 PROVIDER_TYPES 정본 내에서만 기록된다.
        type=cast(ProviderType, p.type),
        base_url=p.base_url,
        has_api_key=p.api_key_encrypted is not None,
        enabled=p.enabled,
        model_count=model_count,
        embedding_max_concurrency=p.embedding_max_concurrency,
    )


class DiscoveredModel(BaseModel):
    model_name: str
    raw_id: str
    display_name: str
    capabilities: list[Modality]
    already_registered: bool = False


class ModelImportItem(BaseModel):
    model_name: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    capabilities: list[Modality] = Field(default_factory=_default_capabilities)
    context_window: int | None = Field(default=None, ge=_PG_INT_MIN, le=_PG_INT_MAX)
    # [#429·#428] 단건 등록(`ModelCreate`)과 같다
    reasoning_effort: ReasoningEffort | None = None
    max_output_tokens: int | None = Field(default=None, ge=1, le=_PG_INT_MAX)

    @field_validator("capabilities")
    @classmethod
    def _check_caps(cls, v: list[Modality]) -> list[Modality]:
        return _validate_capabilities(v)


class ModelImport(BaseModel):
    items: list[ModelImportItem] = Field(min_length=1)


class ModelSyncOut(BaseModel):
    id: uuid.UUID
    model_name: str
    display_name: str
    still_available: bool


class ProviderCircuitOut(BaseModel):
    id: uuid.UUID
    name: str
    open: bool
    open_remaining_seconds: int
    recent_failures: int


class EmbeddingGateOut(BaseModel):
    """프로바이더의 임베딩 동시성 게이트 상태(#196).

    `in_use`이 `limit`에 붙어 있으면 상한이 걸리고 있다는 뜻이다. 상한이 너무 작게 잡힌
    경우(처리량 감소)와 실제로 정체된 경우를 구분하는 단서가 된다.

    대기 중인 요청 수는 **보이지 않는다** — 게이트는 대기를 ZSET에 남기지 않는다
    (죽은 대기자 회수 문제를 새로 만들지 않기 위해). 대기 소진은 지표와 로그로 본다.
    """

    id: uuid.UUID
    name: str
    limit: int | None  # None = 제한 없음(NULL 컬럼)
    in_use: int  # 지금 리스를 잡은 요청 수
    earliest_expiry_in: float | None  # 가장 이른 리스 해제까지 남은 초
    # 이 프로바이더 게이트가 마지막으로 만난 Redis 오류("connection"/"timeout"/예외 클래스).
    # 워커가 쓰고 이 API가 읽는다 — 워커 카운터는 API /metrics에 노출되지 않기 때문이다
    last_error: str | None


__all__ = [
    "DiscoveredModel",
    "EmbeddingGateOut",
    "ModelCreate",
    "ModelImport",
    "ModelImportItem",
    "ModelOut",
    "ModelSyncOut",
    "ModelUpdate",
    "ProviderCircuitOut",
    "ProviderCreate",
    "ProviderOut",
    "ProviderUpdate",
    "model_to_out",
    "provider_to_out",
]
