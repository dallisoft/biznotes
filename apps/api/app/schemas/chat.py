import uuid
from datetime import datetime
from typing import Literal, Self

from biznotes_core.schemas.enums import MessageRole, ProviderType, RunStatus
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.agent import AgentRunOut
from app.schemas.provenance import ProvenanceOut


class CitationOut(BaseModel):
    """RAG 답변의 인용 출처 — `services.rag_chat.Citation.to_dict()`의 응답 표현.

    `verified`는 나중에 추가된 필드라 **과거 행에는 키가 없다**(실측: 126건 중 24건).
    기본값이 없으면 옛 메시지 조회가 ValidationError → HTTP 500이 된다.
    """

    n: int
    source_id: str
    chunk_id: str
    quote: str
    source_title: str
    verified: bool = True


class ChatSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    notebook_id: uuid.UUID
    title: str
    pinned: bool
    created_at: datetime
    updated_at: datetime


class RecentChatOut(BaseModel):
    """홈 「최근 대화 이어가기」 항목 — 내 대화 중 지금 읽을 수 있는 노트북의 것만."""

    session_id: uuid.UUID
    notebook_id: uuid.UUID
    notebook_title: str
    notebook_emoji: str
    # 대화 제목 = 첫 질문 앞 60자(전송 시 갱신)
    title: str
    updated_at: datetime


class ChatSessionUpdate(BaseModel):
    pinned: bool


class ChatEntryRunRefOut(BaseModel):
    """진입 질문 행에 붙는 run 요약(#66) — 웹 run 카드가 새로고침 뒤 다시 붙는 데 쓴다.

    표시 이름은 실행한 스킬 version의 박제 이름(스킬) 또는 정의 이름(workflow)이다.
    """

    run_id: uuid.UUID
    agent_id: uuid.UUID
    display_name: str
    slash_name: str | None
    status: RunStatus


class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    role: MessageRole
    content: str
    citations: list[CitationOut]
    model_id: uuid.UUID | None
    # 토큰 카운터(prompt/completion/total) + insufficient_context 플래그(0|1).
    # 진입 run의 실패·중지 결과 행은 `chat_entry_no_result: 1`(#66). 실측상 값은 전부 정수다.
    token_usage: dict[str, int]
    provenance: ProvenanceOut | None = None
    created_at: datetime
    # 진입 run(#66, 추가형 필드) — assistant 결과 행에는 그 run id, user 질문 행에는 run 요약.
    # 그 밖의 행은 둘 다 null이다
    run_id: uuid.UUID | None = None
    entry_run: ChatEntryRunRefOut | None = None


class ChatEntryOut(BaseModel):
    """`/` 팔레트 항목(#66) — 코어 `list_chat_entry_agents` 판정을 직렬화만 한다.

    스킬은 wrapper의 이름·설명이 아니라 실행 version의 표시값(`ChatEntry.display_*`)이다.
    `slash_name`은 스킬 slug·workflow handle이고, handle이 없는 workflow는 null이다(고르기만 된다).
    """

    agent_id: uuid.UUID
    kind: Literal["skill", "workflow"]
    slash_name: str | None
    source: Literal["own", "system", "org"]
    display_name: str
    description: str


class ChatEntryAmbiguousOut(BaseModel):
    """`/<이름>`이 여러 항목에 맞을 때의 409 본문 `detail`(#66) — 실행하지 않고 후보를 돌려준다."""

    code: Literal["chat_entry_ambiguous"]
    candidates: list[ChatEntryOut]


class ChatEntryConflictOut(BaseModel):
    """진입 실행 409 본문(#66) — 이름이 여럿에 맞으면 후보(`ChatEntryAmbiguousOut`), 세션에 활성
    run이 있으면 문구다(코어 `ActiveChatRunExists`).
    """

    detail: ChatEntryAmbiguousOut | str


# `/<이름>` 길이 상한 — 코어 `Skill.slug` 길이(이보다 긴 이름은 어떤 slug·handle과도 같을 수 없다)
CHAT_ENTRY_NAME_MAX_CHARS = 160


class ChatEntryRunCreate(BaseModel):
    """채팅 진입 실행(#66) — `agent_id`(팔레트에서 고름)나 `name`(`/<이름>` 입력) 중 정확히 하나."""

    agent_id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=CHAT_ENTRY_NAME_MAX_CHARS)
    # 질문만 담는다(`/<이름>` 접두는 웹이 떼어 낸다). 일반 메시지와 같은 길이 상한이다
    content: str = Field(min_length=1, max_length=8000)
    source_ids: list[uuid.UUID] | None = None
    model_id: uuid.UUID | None = None
    # 같은 키로 다시 보내면 같은 run을 돌려준다(코어 `uq_agent_runs_idempotency`)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _exactly_one_target(self) -> Self:
        if (self.agent_id is None) == (self.name is None):
            raise ValueError("agent_id와 name 중 정확히 하나를 보내야 합니다")
        return self


class ChatEntryRunOut(BaseModel):
    """진입 실행 응답(202) — 저장한 질문 행과 큐에 넣은 run.

    진행은 `GET /agent-runs/{id}`로 본다.
    """

    user_message: ChatMessageOut
    run: AgentRunOut


# 마인드맵 노드 질문의 서버 상한 — 웹 문구 생성기(`lib/mindmap/discuss-prompt.ts`)와 같은 값
MINDMAP_PROMPT_MAX_CHARS = 400


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    source_ids: list[uuid.UUID] | None = None
    model_id: uuid.UUID | None = None
    # 자율 모드 토글(Phase 3, Q1=옵션 B). True면 정적 RAG 대신 react_agent로 라우팅한다.
    # 기본 False — 생략 시 기존 정적 rag_chat 경로(회귀 없음).
    # 자율 모드 기본 도구는 rag_search·call_agent만(Q2).
    autonomous: bool = False
    # 다시 생성(ui-ux-improvements ③): 마지막 질문·답변 한 쌍을 새 답변으로 바꾼다.
    # content는 그 마지막 질문과 같아야 한다(409). 실패·중지하면 이전 쌍을 그대로 둔다.
    # 기본 False — 기존 동작과 같다.
    regenerate: bool = False
    # 질문 출처(mindmap-agent-skill D-17 — 추가형 필드, 기본값이 기존 동작).
    # "mindmap"이면 서버가 `autonomous`와 무관하게 정적 RAG 경로로 보내고 이번 턴에 이력을
    # 쓰지 않으며, 저장한 user 메시지에 표식을 달아 이후 턴의 LLM 이력에서 고정 문구로 바꾼다
    # (LLM 산출 라벨의 재주입 차단 — T2).
    origin: Literal["chat", "mindmap"] = "chat"

    @model_validator(mode="after")
    def _mindmap_prompt_length(self) -> Self:
        if self.origin == "mindmap" and len(self.content) > MINDMAP_PROMPT_MAX_CHARS:
            raise ValueError(f"마인드맵 질문은 {MINDMAP_PROMPT_MAX_CHARS}자 이하여야 합니다")
        return self


class LLMModelOut(BaseModel):
    id: uuid.UUID
    display_name: str
    provider_name: str
    provider_type: ProviderType
    is_default: bool
