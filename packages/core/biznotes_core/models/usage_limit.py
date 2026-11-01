import uuid

from sqlalchemy import ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from biznotes_core.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class UsageLimit(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """사용자별 일일 사용량 한도 — 행 없음/필드 null = 무제한.

    카운터는 Redis(일일 키, UTC)로 가드하고 정산은 DB 집계가 기준이다.
    어드민 역할은 한도 검사에서 제외된다.
    """

    __tablename__ = "usage_limits"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    daily_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_agent_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)
