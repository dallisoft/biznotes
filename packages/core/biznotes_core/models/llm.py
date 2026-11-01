import uuid as _uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from biznotes_core.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

PROVIDER_TYPES = (
    "openai",
    "anthropic",
    "google",
    "ollama",
    "vllm",
    "lmstudio",
    "azure",
    "bedrock",
    "mock",
    "custom",
)
MODALITIES = ("llm", "vlm", "embedding", "reranker", "image")
# 사고 양(#429) — 모델 기본값·노드 재정의의 허용 값 정본. 값이 없으면 보내지 않는다
REASONING_EFFORTS = ("low", "medium", "high")


class LLMProvider(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "llm_providers"

    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 이 프로바이더의 임베딩 서버가 동시에 처리할 수 있는 요청 수(#196). NULL = 제한 없음.
    # 워커 프로세스 전체가 Redis 리스로 공유하므로 프로세스 수와 무관하다.
    # LLM 호출의 동시 처리 한도와는 별개다 — 같은 서버가 다른 한도를 가질 수 있다
    embedding_max_concurrency: Mapped[int | None] = mapped_column(Integer, nullable=True)


class LLMModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "llm_models"

    __table_args__ = tuple(
        Index(
            f"uq_llm_models_default_{modality}",
            "is_default",
            unique=True,
            postgresql_where=text(f"is_default AND '{modality}' = ANY (capabilities)"),
        )
        for modality in MODALITIES
    )

    provider_id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("llm_providers.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)  # litellm 모델 식별자
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 모델이 제공하는 능력(다중) — 라우팅 키. 최소 1개 (MODALITIES 중)
    capabilities: Mapped[list[str]] = mapped_column(
        ARRAY(String(20)), default=lambda: ["llm"], nullable=False
    )
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_per_1k: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    # 폴백 체인 순서 (null=폴백 후보 아님, 낮을수록 먼저 시도)
    fallback_priority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 사고 양 기본값(#429, REASONING_EFFORTS 중). NULL = 보내지 않는다(프로바이더 기본).
    # CHECK 제약은 두지 않는다 — 허용 값은 관리자 스키마가 검증한다(type·capabilities와 같다)
    reasoning_effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 모델 최대 출력 토큰(#428). NULL = 모른다 — 호출 상한을 줄이지 않는다
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
