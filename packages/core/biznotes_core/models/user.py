from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column

from biznotes_core.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

USER_ROLES = ("admin", "user")


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="user", nullable=False)  # admin | user
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 다음 로그인 때 비밀번호를 바꿔야 한다 — 켜져 있으면 허용 목록 밖 API가 403(#165)
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    # 마지막 비밀번호 변경 시각 — 토큰 `pwv` 클레임과 비교해 그 전에 발급된 토큰을 거부한다
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
