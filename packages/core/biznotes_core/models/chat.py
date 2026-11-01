import uuid
from typing import Any

from sqlalchemy import BigInteger, Boolean, ForeignKey, Identity, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from biznotes_core.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

MESSAGE_ROLES = ("user", "assistant")


class ChatSession(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "chat_sessions"

    notebook_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notebooks.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), default="새 대화", nullable=False)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ChatMessage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "chat_messages"

    __table_args__ = (
        Index("ix_chat_messages_run_id", "run_id"),
        Index(
            "uq_chat_messages_run_assistant",
            "run_id",
            unique=True,
            postgresql_where=text("run_id IS NOT NULL AND role = 'assistant'"),
        ),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user | assistant
    # 삽입 순번 — **대화 순서의 정본**. created_at(`now()`)은 트랜잭션 시작 시각이라
    # 한 트랜잭션에 넣은 질문·답변이 같은 값을 갖고, 트랜잭션이 겹치면(워커가 먼저
    # 연 트랜잭션에서 답변을 나중에 커밋) 순서가 뒤집히기까지 한다(#4).
    # 조회는 `.order_by(ChatMessage.seq)`로 한다 — created_at을 앞에 두면 위의
    # 겹치는 트랜잭션에서 답변이 질문보다 먼저 나온다(실측 확인).
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # [{n, source_id, chunk_id, quote, source_title}]
    citations: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    model_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    token_usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    # biznotes 제품 기능(출처 추적). sl-agents는 기록하지 않는다 — 컬럼 합집합을
    # 코어가 소유하기로 한 결정에 따라 스키마에는 양쪽 모두 존재한다.
    provenance: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "agent_runs.id",
            name="fk_chat_messages_run_id",
            ondelete="SET NULL",
            use_alter=True,
        ),
        nullable=True,
    )
