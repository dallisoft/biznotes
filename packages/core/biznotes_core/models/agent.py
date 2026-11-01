import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from biznotes_core.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

AGENT_KINDS = ("workflow", "skill", "plugin", "tool", "mcp")  # 우측 패널 탭과 1:1
AGENT_SCOPES = ("user", "system")  # workflows.py:237,264 · admin/skills.py:35
RUN_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled", "awaiting_approval")
# AgentRun.trigger 값 — 사용자가 직접 시작한 실행(수동·채팅)뿐이다
TRIGGER_SOURCES = ("manual", "chat")
APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired")
# MCP 서버 공개 범위(#139 A) — `admin_only`는 관리자·시스템 실행만, `all_users`는 일반 사용자
# 실행에도 승인된 read-only 도구를 노출한다.
MCP_SERVER_EXPOSURES = ("admin_only", "all_users")


class Agent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "agents"

    # 슬래시 호출 키(handle)의 네임스페이스 분리 — owner별/system별 partial unique.
    # 고아 행(owner_id IS NULL AND scope = 'user')은 두 predicate 어디에도 걸리지 않는다:
    # owner_id의 ON DELETE SET NULL이 unique 위반으로 실패하지 않게 하려는 의도적 설계다.
    # sl-agents 리비전 f7a9c1b3d5e2와 선언이 일치해야 한다.
    __table_args__ = (
        Index(
            "uq_agents_handle_owner",
            "owner_id",
            "handle",
            unique=True,
            postgresql_where=text("owner_id IS NOT NULL AND handle IS NOT NULL"),
        ),
        Index(
            "uq_agents_handle_system",
            "handle",
            unique=True,
            postgresql_where=text(
                "scope = 'system' AND owner_id IS NULL AND handle IS NOT NULL"
            ),
        ),
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 표시 이름(name, 한글 허용)과 분리된 영문 슬래시 호출 키. NULL이면 슬래시 호출 불가.
    handle: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    icon: Mapped[str] = mapped_column(String(16), default="🤖", nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # workflow: {nodes, edges} / skill: {prompt_template,...}
    # tool: {http,...} / mcp: {server_id, tool_name}
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    params_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    scope: Mapped[str] = mapped_column(String(20), default="system", nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # 스킬 패키지 실행 wrapper (skill_plan.md §6.2) — source_kind="skill_package"면 wrapper
    skill_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("skill_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_kind: Mapped[str] = mapped_column(
        String(20), default="legacy", nullable=False
    )  # legacy | skill_package


class MCPServer(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "mcp_servers"
    __table_args__ = (
        CheckConstraint(
            "exposure IN ('admin_only', 'all_users')", name="ck_mcp_servers_exposure"
        ),
    )

    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    transport: Mapped[str] = mapped_column(String(30), nullable=False)  # stdio|sse|streamable_http
    endpoint_or_command: Mapped[str] = mapped_column(Text, nullable=False)
    env_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON 암호화
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_health_check: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_health_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # 공개 범위(#139 A). 두 제품 리비전이 server_default로 기존 행을 `admin_only`로 채운다(부록 B).
    exposure: Mapped[str] = mapped_column(
        String(20), default="admin_only", server_default="admin_only", nullable=False
    )


class MCPServerTool(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """관리자가 승인한 MCP 서버 도구(#139 B) — 노출 allowlist의 정본.

    `read_only=True`이고 승인 시점 스펙이 고정된(`approved_spec_hash`) 승인만 에이전트에 노출한다
    (#293). `read_only=False`는 향후 HITL 경로용으로 예약한다. 두 제품의 Alembic 리비전이 이 정의를
    따라가고 기존 archhub 상수를 시드한다(docs/plans/mcp-exposure/plan.md 부록 A).
    """

    __tablename__ = "mcp_server_tools"
    __table_args__ = (
        UniqueConstraint("server_id", "tool_name", name="uq_mcp_server_tools_server_tool"),
    )

    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    read_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 관리자가 승인할 때 본 설명·입력 스키마(정화 결과)의 sha256 hex(#293,
    # `mcp_tool_spec.mcp_tool_spec_hash`). NULL은 고정되지 않은 승인이라 노출되지 않는다 — 다시
    # 승인해야 한다. 디스커버리에서 현재 스펙의 해시가 다르면 그 도구를 노출하지 않는다(rug-pull).
    approved_spec_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AgentRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "agent_runs"

    __table_args__ = (
        Index("ix_agent_runs_chat_session_id", "chat_session_id"),
        Index(
            "uq_agent_runs_chat_active",
            "chat_session_id",
            unique=True,
            postgresql_where=text(
                "chat_session_id IS NOT NULL AND status IN ('queued','running','awaiting_approval')"
            ),
        ),
        Index(
            "uq_agent_runs_idempotency",
            "user_id",
            "chat_session_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        # 만료된 lease 회수 스캔(#153 B) — running이고 lease를 가진 행만 담는다
        Index(
            "ix_agent_runs_lease_expires_at",
            "lease_expires_at",
            postgresql_where=text("status = 'running' AND lease_expires_at IS NOT NULL"),
        ),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    notebook_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notebooks.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    chat_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "chat_sessions.id",
            name="fk_agent_runs_chat_session_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    prompt_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "chat_messages.id",
            name="fk_agent_runs_prompt_message_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    # run 단위 요청 모델(#359) — model_id를 고정하지 않은 llm·react 노드의 1차 모델. 모델을 지우면
    # NULL(run은 남는다). 제품 워커가 `RunContext.run_model_id`로 넘긴다
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("llm_models.id", name="fk_agent_runs_model_id", ondelete="SET NULL"),
        nullable=True,
    )
    # 입력 스코프 핀 — scope:selected로 고른 source_id 목록(문자열). None = 노트 전체(scope:all).
    # 관측·재현성·스케줄 고정 실행 보존 (agent-input-source-plan.md §Phase4·6).
    input_source_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    # TRIGGER_SOURCES — 실행 출처 (이력 표시·동시 가드용)
    trigger: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 0~100
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_keys: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    token_usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(155), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 목록 숨김(#371) — 지우지 않고 노트북 목록에서만 뺀다. 집계·단건 조회·산출물·실행 로그는
    # 그대로다. 숨기기·해제는 `services.agent_runs.set_run_hidden`, 목록 조건은 `run_is_visible()`
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hidden_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_agent_runs_hidden_by", ondelete="SET NULL"),
        nullable=True,
    )
    # 실행 lease와 fencing(#153 B, `services/agent_run_lease.py`). attempt는 claim·재개·회수마다
    # 1씩 오른다 — 상태·결과 쓰기는 `attempt = :mine` CAS라 회수된 뒤 늦게 온 워커의 쓰기가
    # 반영되지 않는다. lease는 running 동안만 있고, running을 벗어나는 모든 전이에서 NULL이 된다.
    # lease가 NULL인 running(구 워커)은 회수하지 않는다.
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 스킬 실행 재현성 스냅샷 (skill_plan.md §6.2, §9.4)
    skill_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("skill_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    skill_package_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    skill_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class AgentRunApproval(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """자율 에이전트 부작용 도구 HITL 승인 요청 (phase-19-design.md §1.1).

    checkpoint는 재개용 임시 상태이며 감사 기록이 아니다 — 재개 소비 완료 시
    NULL화한다(phase-19-design.md §2.6, SEC-D1/CDX-D7 반영). 감사 목적 보존은
    arguments_summary(마스킹됨)로만 수행한다.
    """

    __tablename__ = "agent_run_approvals"

    # 마이그레이션(20260703_a1b2c3d4e5f6)과의 정합: 만료 스캔용 복합 인덱스 +
    # run당 pending 1건 보장 부분 유니크 인덱스. alembic check 게이트(R0-3)가
    # 모델↔마이그레이션 드리프트를 검증하므로 양쪽 선언이 일치해야 한다.
    __table_args__ = (
        Index("ix_agent_run_approvals_status_expires_at", "status", "expires_at"),
        Index(
            "uq_agent_run_approvals_run_pending",
            "run_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True,
        nullable=False,
    )
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    arguments_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
