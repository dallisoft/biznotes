import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from biznotes_core.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class NotebookOverview(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """노트북 소스 요약 + 추천 질문 (소스 셋 변경 시 백그라운드 재생성)."""

    __tablename__ = "notebook_overviews"

    notebook_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notebooks.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    suggested_questions: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    # 생성 시점의 ready 소스 수 — 현재와 다르면 stale (소스 삭제 감지)
    source_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 노트북 제목 자동 제안 후보(phase-22-design.md §3.3) — 개요 생성과 동일 LLM 호출에서 추출
    title_suggestions: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
