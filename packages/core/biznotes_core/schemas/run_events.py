"""에이전트 run SSE 이벤트의 단일 Pydantic 계약."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from biznotes_core.schemas.enums import ApprovalStatus, MessageRole, RunStatus


class StrictEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class StatusEventInput(StrictEvent):
    type: Literal["status"]
    status: RunStatus
    progress: int | None = Field(default=None, ge=0, le=100)
    error: str | None = None
    artifact_keys: list[dict[str, str]] | None = None
    result_summary: str | None = None


class ProgressEventInput(StrictEvent):
    type: Literal["progress"]
    node: str
    progress: int = Field(ge=0, le=100)


class ApprovalRequiredEventInput(StrictEvent):
    type: Literal["approval_required"]
    approval_id: uuid.UUID
    tool: str
    arguments_summary: dict[str, Any]
    expires_at: datetime
    # 부작용 도구 여부(#154 A). 승인 대기는 게이트를 통과한 부작용 도구라 항상 True다.
    # None이면 직렬화(`exclude_none`)에서 빠진다 — 필드가 없는 구 페이로드도 통과한다.
    side_effect: bool | None = None


class ApprovalResolvedEventInput(StrictEvent):
    type: Literal["approval_resolved"]
    approval_id: uuid.UUID
    status: Literal["approved", "rejected", "expired"]


ToolCallStatus = Literal["awaiting_approval", "succeeded", "failed", "blocked"]


class ToolCallEventInput(StrictEvent):
    type: Literal["tool_call"]
    tool_call_id: str
    name: str
    status: ToolCallStatus
    arguments: dict[str, Any]
    # 정의(`required_initial_tools`)가 부른 호출이면 True(#10·#23). 모델이 부른 호출은 None으로
    # 두어 직렬화(`exclude_none`)에서 빠진다 — 구 버전 리더(`extra="forbid"`)와 호환된다.
    required_initial: bool | None = None
    # 부작용 도구 여부(#154 A) — 승인 게이트와 같은 판정(`tool_registry.is_side_effect_tool`).
    # 코어는 항상 채운다. 구 버전 리더(`extra="forbid"`)는 이 필드를 거부하므로 API를 워커와
    # 같이 또는 먼저 올린다. 필드가 없는 구 페이로드는 None으로 통과한다.
    side_effect: bool | None = None


RunEventInput = Annotated[
    StatusEventInput
    | ProgressEventInput
    | ApprovalRequiredEventInput
    | ApprovalResolvedEventInput
    | ToolCallEventInput,
    Field(discriminator="type"),
]


class StatusEvent(StatusEventInput):
    event_id: int = Field(ge=1)


class ProgressEvent(ProgressEventInput):
    event_id: int = Field(ge=1)


class ApprovalRequiredEvent(ApprovalRequiredEventInput):
    event_id: int = Field(ge=1)


class ApprovalResolvedEvent(ApprovalResolvedEventInput):
    event_id: int = Field(ge=1)


class ToolCallEvent(ToolCallEventInput):
    event_id: int = Field(ge=1)


class AgentRunSnapshot(StrictEvent):
    id: uuid.UUID
    agent_id: uuid.UUID
    notebook_id: uuid.UUID
    chat_session_id: uuid.UUID | None = None
    prompt_message_id: uuid.UUID | None = None
    status: RunStatus
    progress: int = Field(ge=0, le=100)
    result_summary: str | None = None
    artifact_keys: list[dict[str, Any]] = Field(default_factory=list)
    token_usage: dict[str, int] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class PendingApprovalSnapshot(StrictEvent):
    approval_id: uuid.UUID
    tool: str
    arguments_summary: dict[str, Any]
    status: ApprovalStatus
    expires_at: datetime
    side_effect: bool | None = None  # ApprovalRequiredEventInput과 같은 규칙(#154)


class ToolCallSnapshot(StrictEvent):
    tool_call_id: str
    name: str
    status: ToolCallStatus
    arguments: dict[str, Any]
    required_initial: bool | None = None  # ToolCallEventInput과 같은 규칙(#23)
    side_effect: bool | None = None  # ToolCallEventInput과 같은 규칙(#154)


class RunMessageSnapshot(StrictEvent):
    id: uuid.UUID
    role: MessageRole
    content: str
    run_id: uuid.UUID | None = None
    created_at: datetime


class SnapshotEvent(StrictEvent):
    type: Literal["snapshot"]
    event_id: int = Field(ge=0)
    run: AgentRunSnapshot
    pending_approval: PendingApprovalSnapshot | None = None
    tool_calls: list[ToolCallSnapshot] = Field(default_factory=list)
    messages: list[RunMessageSnapshot] = Field(default_factory=list)


RunEvent = Annotated[
    StatusEvent
    | ProgressEvent
    | ApprovalRequiredEvent
    | ApprovalResolvedEvent
    | ToolCallEvent
    | SnapshotEvent,
    Field(discriminator="type"),
]

RunEventInputAdapter: TypeAdapter[RunEventInput] = TypeAdapter(RunEventInput)
RunEventAdapter: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)


__all__ = [
    "AgentRunSnapshot",
    "ApprovalRequiredEventInput",
    "ApprovalResolvedEventInput",
    "PendingApprovalSnapshot",
    "ProgressEventInput",
    "RunEvent",
    "RunEventAdapter",
    "RunEventInput",
    "RunEventInputAdapter",
    "RunMessageSnapshot",
    "SnapshotEvent",
    "StatusEventInput",
    "ToolCallEventInput",
    "ToolCallSnapshot",
]
