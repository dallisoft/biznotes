"""파일 기반 Agent Skill 패키지 모델 (skill_plan.md §6 · 표준 계획 D13).

skills                  — 소유·가시성 단위 (초안의 hub_entry + user_skill 통합)
skill_versions          — 불변 패키지 버전 (실행·공유 단위)
skill_review_requests   — version 단위 관리자 검토 요청(공개·스크립트 허가, D8)

`skills.current_version_id`·`skills.published_version_id`는 순환 FK를 피하려 FK 없이
앱 레벨로 보장한다(§6.1 권장안). 소속·상태 검사는 공개 관문
`services/skills/exposure.py::set_published_version`이 한다.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from biznotes_core.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

SKILL_VISIBILITIES = ("private", "org", "system")
SKILL_STATUSES = ("draft", "published", "archived")
SKILL_VERSION_STATUSES = ("draft_frozen", "published", "archived")
# 패키지 내 파일 역할 — services/skills/package.py::_role_for 가 산출하는 값
SKILL_FILE_ROLES = ("skill_md", "manifest", "reference", "asset", "script", "other")

# 표준 적합성(D1)과 실행 자격(D6) — 둘은 독립이다(S-HCI-11)
SKILL_VALIDATION_PROFILES = ("legacy_v1", "standard_v1")
SKILL_RUNTIME_LEGACY = "legacy_instruction_v1"
SKILL_RUNTIME_STANDARD = "standard_agent_v1"
SKILL_RUNTIME_PROFILES = (SKILL_RUNTIME_LEGACY, SKILL_RUNTIME_STANDARD)
# 출처(D4) — 거버넌스용이며 보안 통제가 아니다
SKILL_ORIGINS = ("authored", "imported", "system_seed")
# 검토 요청(D8) — 종류별 판정은 서로 독립이다
SKILL_REVIEW_KINDS = ("expose_org", "expose_system", "scripts")
SKILL_REVIEW_STATUSES = ("pending", "approved", "rejected", "withdrawn", "cancelled")


def _in_check(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Skill(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "skills"

    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )  # null = system 빌트인
    # 표시용 필드(name·description·tags)는 목록 UI에만 쓴다. LLM에 들어가는 이름·설명은
    # 노출 version의 frontmatter에서 읽는다(D8 적용 범위).
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(160), nullable=False)
    tags: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    visibility: Mapped[str] = mapped_column(String(20), default="private", nullable=False)
    # 파생값 — 공개 관문만 쓴다. 포인터가 있으면 published, 없으면 draft(D8 파생 상태)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    # → skill_versions.id (FK 없는 앱 레벨 포인터, §6.1 — 순환 FK 회피). 가장 최근 freeze.
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # → skill_versions.id. 다른 사용자에게 드러나는 내용의 **유일한 정본**(D8).
    published_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    forked_from_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    clone_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    run_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    origin: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="authored", default="authored"
    )
    # 계보·가져오기 정보 — {"lineage": [version id…], "root_name": …}
    origin_detail: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    # 관리자 철회 뒤의 보류 — 관리자 expose_* 승인 전까지 어떤 공개도 불가(S-HCI-14)
    exposure_hold: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )

    __table_args__ = (
        # private/user 소유: 소유자 내 slug 유일 (NULL owner는 제외)
        Index(
            "uq_skills_owner_slug",
            "owner_id",
            "slug",
            unique=True,
            postgresql_where=text("owner_id IS NOT NULL"),
        ),
        # system 빌트인: 전역 slug 유일
        Index(
            "uq_skills_system_slug",
            "slug",
            unique=True,
            postgresql_where=text("visibility = 'system'"),
        ),
        # org 공유: 1차는 전역 slug 유일 (멀티테넌시 도입 시 (org_id, slug)로 교체 — 백로그)
        Index(
            "uq_skills_org_slug",
            "slug",
            unique=True,
            postgresql_where=text("visibility = 'org'"),
        ),
        CheckConstraint(_in_check("origin", SKILL_ORIGINS), name="ck_skills_origin"),
    )


class SkillVersion(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "skill_versions"

    skill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"), index=True, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    package_uri: Mapped[str] = mapped_column(Text, nullable=False)  # 스토리지 key prefix
    skill_md_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    package_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    file_tree: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # 네 층 검증 보고서(D5) — {standard, portability, policy, capability}
    security_report: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # 이력 표식 — 공개 관문이 붙인다. 노출 판정에는 쓰지 않는다(D8 파생 상태)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    changelog: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 기존 행은 추가 전용 리비전의 DEFAULT로 legacy를 유지한다. 원문·해시·트리는 불변이다.
    validation_profile: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="legacy_v1", default="legacy_v1"
    )
    runtime_profile: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=SKILL_RUNTIME_LEGACY,
        default=SKILL_RUNTIME_LEGACY,
    )
    # SKILL.md frontmatter 원문(모르는 필드 포함, 문자열 스칼라 JSON). 64KiB 상한(D3).
    standard_metadata: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )

    __table_args__ = (
        Index("uq_skill_versions_skill_version", "skill_id", "version", unique=True),
        CheckConstraint(
            _in_check("validation_profile", SKILL_VALIDATION_PROFILES),
            name="ck_skill_versions_validation_profile",
        ),
        CheckConstraint(
            _in_check("runtime_profile", SKILL_RUNTIME_PROFILES),
            name="ck_skill_versions_runtime_profile",
        ),
    )


class SkillReviewRequest(Base, UUIDPrimaryKeyMixin):
    """version 하나에 대한 관리자 검토 요청(D8 검토 요청, R3-04).

    - `expose_org`·`expose_system`: 그 version의 공개 승인. 승인하면 같은 트랜잭션에서
      공개까지 끝낸다. authored org 스킬의 `expose_org`는 채팅 카탈로그 등재를 뜻한다.
    - `scripts`: 그 version의 번들 스크립트 실행 허가(D7). 공개와 독립이다.
    - 같은 (version, kind)에 `pending`은 하나만 둔다(부분 유일 인덱스).
    """

    __tablename__ = "skill_review_requests"

    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("skill_versions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # expose_*만 — 승인 시점의 목표 visibility
    target_visibility: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending", default="pending"
    )
    # seed 경로의 요청은 NULL(시스템 행위자)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            _in_check("kind", SKILL_REVIEW_KINDS), name="ck_skill_review_requests_kind"
        ),
        CheckConstraint(
            _in_check("status", SKILL_REVIEW_STATUSES), name="ck_skill_review_requests_status"
        ),
        Index(
            "uq_skill_review_requests_pending",
            "version_id",
            "kind",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )
