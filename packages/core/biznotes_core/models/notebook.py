import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from biznotes_core.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# `group`은 #278 3단계에서 없앴다 — 소비 제품 리비전이 남은 행을 `private`로 바꾼다
NOTEBOOK_VISIBILITIES = ("private", "public")
NOTEBOOK_PERMISSIONS = ("owner", "write", "read")
SHARE_PERMISSIONS = ("read", "write")


class Notebook(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "notebooks"
    __table_args__ = (
        CheckConstraint("visibility IN ('private', 'public')", name="ck_notebook_visibility"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    emoji: Mapped[str] = mapped_column(String(16), default="📓", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    visibility: Mapped[str] = mapped_column(
        String(20), default="private", nullable=False
    )  # private | public
