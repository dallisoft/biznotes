"""프롬프트 버전 관리 (Phase F) — 템플릿 + 버전 이력 + 롤백/활성 포인터.

PromptTemplate은 이름 있는 프롬프트의 컨테이너이고, PromptVersion은 불변 스냅샷이다.
active_version_id로 현재 사용 버전을 가리키며, 롤백 = 활성 포인터를 과거 버전으로 변경.
llm 노드의 prompt_ref가 템플릿을 가리키면 런타임이 활성 버전을 해석한다.
"""

import uuid

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from biznotes_core.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PromptTemplate(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "prompt_templates"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scope: Mapped[str] = mapped_column(String(20), default="user", nullable=False)  # user|system
    # 현재 활성 버전 포인터 — 순환 FK 회피를 위해 제약 없는 UUID(무결성은 서비스 계층에서 관리)
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class PromptVersion(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "prompt_versions"

    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("prompt_templates.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)  # 1부터 증가
    content: Mapped[str] = mapped_column(Text, nullable=False)  # 프롬프트 본문(템플릿 표현식 포함)
    system: Mapped[str] = mapped_column(Text, default="", nullable=False)  # 시스템 프롬프트(선택)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)  # 변경 메모
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
