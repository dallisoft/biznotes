"""응답 스키마용 `Literal` 별칭 — 정본은 `app/models/*.py`의 상수 튜플이다.

R4-3a(HCI-R4-2 승인). 응답 스키마의 bare `str`을 좁히되, **기준은 프론트 유니온이 아니라
모델의 정본 튜플**이다. 프론트 유니온을 기준으로 삼으면 코드가 실제로 쓰는 값이 잘려 응답
직렬화가 실패한다(HTTP 500).

`Literal[TUPLE]`(튜플 변수 첨자)은 mypy가 거부하므로 별칭을 손으로 적고,
아래 `_assert_matches_canonical()`로 정본 튜플과의 일치를 **임포트 시점에 강제**한다.
정본에 값을 추가하고 별칭을 잊으면 즉시 터진다.

예외: `ChatEntryReasonValue`의 정본은 모델 튜플이 아니라 서비스 enum(`ChatEntryReason`)이다.
대조는 그 enum이 있는 `services/agents/chat_entry.py`가 import 시점에 한다(#135).
"""

from __future__ import annotations

from typing import Final, Literal, get_args

from biznotes_core.models.agent import (
    AGENT_KINDS,
    AGENT_SCOPES,
    APPROVAL_STATUSES,
    MCP_SERVER_EXPOSURES,
    RUN_STATUSES,
    TRIGGER_SOURCES,
)
from biznotes_core.models.chat import MESSAGE_ROLES
from biznotes_core.models.group import GROUP_MEMBER_ROLES
from biznotes_core.models.llm import MODALITIES, PROVIDER_TYPES, REASONING_EFFORTS
from biznotes_core.models.note import NOTE_ORIGINS
from biznotes_core.models.notebook import (
    NOTEBOOK_PERMISSIONS,
    NOTEBOOK_VISIBILITIES,
    SHARE_PERMISSIONS,
)
from biznotes_core.models.skill import (
    SKILL_FILE_ROLES,
    SKILL_ORIGINS,
    SKILL_REVIEW_KINDS,
    SKILL_REVIEW_STATUSES,
    SKILL_RUNTIME_PROFILES,
    SKILL_STATUSES,
    SKILL_VALIDATION_PROFILES,
    SKILL_VERSION_STATUSES,
    SKILL_VISIBILITIES,
)
from biznotes_core.models.source import SOURCE_STATUSES, SOURCE_TYPES
from biznotes_core.models.user import USER_ROLES

# --- agent ---
AgentKind = Literal["workflow", "skill", "plugin", "tool", "mcp"]
AgentScope = Literal["user", "system"]
RunStatus = Literal[
    "queued", "running", "succeeded", "failed", "cancelled", "awaiting_approval"
]
#: 다시 바뀌지 않는 run 상태 — 이 상태의 `status` 이벤트가 event sequence 보존 기간을 건다(#154 B).
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})
assert TERMINAL_RUN_STATUSES <= set(RUN_STATUSES)
ApprovalStatus = Literal["pending", "approved", "rejected", "expired"]
MCPServerExposure = Literal["admin_only", "all_users"]
# `chat`은 chat↔agent_run 연결 기능이 쓰는 값이다(아래 _assert_matches_canonical이
# 정본 튜플과의 어긋남을 기동 시 거부한다).
TriggerSource = Literal["manual", "chat"]

# --- user / group / chat ---
UserRole = Literal["admin", "user"]
GroupMemberRole = Literal["owner", "member"]
MessageRole = Literal["user", "assistant"]

# --- notebook ---
NotebookVisibility = Literal["private", "public"]
NotebookPermission = Literal["owner", "write", "read"]
SharePermission = Literal["read", "write"]

# --- source ---
SourceType = Literal["file", "web", "text"]
SourceStatus = Literal["pending", "parsing", "chunking", "embedding", "ready", "failed"]

# --- note / skill ---
NoteOrigin = Literal["manual", "chat", "agent"]
SkillVisibility = Literal["private", "org", "system"]
SkillStatus = Literal["draft", "published", "archived"]
SkillVersionStatus = Literal["draft_frozen", "published", "archived"]
SkillFileRole = Literal["skill_md", "manifest", "reference", "asset", "script", "other"]
SkillValidationProfile = Literal["legacy_v1", "standard_v1"]
SkillRuntimeProfile = Literal["legacy_instruction_v1", "standard_agent_v1"]
SkillOrigin = Literal["authored", "imported", "system_seed"]
SkillReviewKind = Literal["expose_org", "expose_system", "scripts"]
SkillReviewStatus = Literal["pending", "approved", "rejected", "withdrawn", "cancelled"]
# 채팅 진입 판정 사유(#135). 정본은 모델 튜플이 아니라 `services.agents.chat_entry.ChatEntryReason`
# 이다. `schemas`는 `services`를 import하지 않으므로(AGENTS.md §3.2) 아래 `_BINDINGS` 대신 그 모듈이
# import 시점에 1:1을 대조하고, `tests/test_skill_chat_entry_reason.py`도 고정한다.
ChatEntryReasonValue = Literal[
    "ok",
    "not_visible",
    "not_enabled",
    "workflow_shape",
    "skill_disabled",
    "skill_not_system",
    "skill_not_published",
    "skill_runtime_disabled",
    "skill_params_incompatible",
]

# --- llm ---
ProviderType = Literal[
    "openai", "anthropic", "google", "ollama", "vllm",
    "lmstudio", "azure", "bedrock", "mock", "custom",
]
# 관리자 LLM 모델 요청·응답 스키마가 이 타입을 쓴다(#218). 값 추가는 응답 계약의 확장이다 —
# 새 판이 저장한 값을 옛 판이 읽으면 관리자 모델 응답이 500이 된다. 혼합 배포·롤백 중에는
# 새 능력을 저장하지 않는다
Modality = Literal["llm", "vlm", "embedding", "reranker", "image"]
# 사고 양(#429) — 관리자 모델 기본값과 노드 재정의가 같은 값을 받는다
ReasoningEffort = Literal["low", "medium", "high"]


_BINDINGS: Final[tuple[tuple[str, object, tuple[str, ...]], ...]] = (
    ("AgentKind", AgentKind, AGENT_KINDS),
    ("AgentScope", AgentScope, AGENT_SCOPES),
    ("RunStatus", RunStatus, RUN_STATUSES),
    ("ApprovalStatus", ApprovalStatus, APPROVAL_STATUSES),
    ("MCPServerExposure", MCPServerExposure, MCP_SERVER_EXPOSURES),
    ("TriggerSource", TriggerSource, TRIGGER_SOURCES),
    ("UserRole", UserRole, USER_ROLES),
    ("GroupMemberRole", GroupMemberRole, GROUP_MEMBER_ROLES),
    ("MessageRole", MessageRole, MESSAGE_ROLES),
    ("NotebookVisibility", NotebookVisibility, NOTEBOOK_VISIBILITIES),
    ("NotebookPermission", NotebookPermission, NOTEBOOK_PERMISSIONS),
    ("SharePermission", SharePermission, SHARE_PERMISSIONS),
    ("SourceType", SourceType, SOURCE_TYPES),
    ("SourceStatus", SourceStatus, SOURCE_STATUSES),
    ("NoteOrigin", NoteOrigin, NOTE_ORIGINS),
    ("SkillVisibility", SkillVisibility, SKILL_VISIBILITIES),
    ("SkillStatus", SkillStatus, SKILL_STATUSES),
    ("SkillVersionStatus", SkillVersionStatus, SKILL_VERSION_STATUSES),
    ("SkillFileRole", SkillFileRole, SKILL_FILE_ROLES),
    ("SkillValidationProfile", SkillValidationProfile, SKILL_VALIDATION_PROFILES),
    ("SkillRuntimeProfile", SkillRuntimeProfile, SKILL_RUNTIME_PROFILES),
    ("SkillOrigin", SkillOrigin, SKILL_ORIGINS),
    ("SkillReviewKind", SkillReviewKind, SKILL_REVIEW_KINDS),
    ("SkillReviewStatus", SkillReviewStatus, SKILL_REVIEW_STATUSES),
    ("ProviderType", ProviderType, PROVIDER_TYPES),
    ("Modality", Modality, MODALITIES),
    ("ReasoningEffort", ReasoningEffort, REASONING_EFFORTS),
)


def _assert_matches_canonical() -> None:
    """별칭 ↔ 정본 튜플 불일치를 임포트 시점에 잡는다.

    정본에 값을 추가하고 별칭을 갱신하지 않으면 응답 직렬화가 런타임에 500으로 터진다.
    그 전에 여기서 죽는 편이 낫다.
    """
    for name, alias, canonical in _BINDINGS:
        alias_values = set(get_args(alias))
        if alias_values != set(canonical):
            raise RuntimeError(
                f"{name} 별칭이 정본 튜플과 어긋납니다: "
                f"별칭에만 {sorted(alias_values - set(canonical))}, "
                f"정본에만 {sorted(set(canonical) - alias_values)}"
            )


_assert_matches_canonical()
