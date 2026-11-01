import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from biznotes_core.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

NOTE_ORIGINS = ("manual", "chat", "agent")


class Note(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "notes"

    notebook_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notebooks.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)  # markdown
    origin: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    # chat: chat_message_id / agent: agent_run_id
    origin_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # 채팅 답변 저장 시 인용 스냅샷 [{n, source_id, chunk_id, quote, source_title}]
    citations: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    provenance: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
