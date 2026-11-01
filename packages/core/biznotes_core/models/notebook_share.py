import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from biznotes_core.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class NotebookShare(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """노트북 그룹 공유 — 노트북·그룹마다 한 행이다(#278 3단계, 사용자 대상 공유는 없앴다)."""

    __tablename__ = "notebook_shares"
    __table_args__ = (
        UniqueConstraint("notebook_id", "group_id", name="uq_share_notebook_group"),
        CheckConstraint("permission IN ('read', 'write')", name="ck_share_permission"),
    )

    notebook_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notebooks.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), index=True, nullable=False
    )
    permission: Mapped[str] = mapped_column(
        String(10), default="read", nullable=False
    )  # read | write
