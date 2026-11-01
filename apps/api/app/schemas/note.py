import uuid
from datetime import datetime
from typing import Literal

from biznotes_core.schemas.enums import NoteOrigin
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.chat import CitationOut
from app.schemas.provenance import ProvenanceOut


class NoteCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(
        default="",
        max_length=200_000,
        description=(
            "origin='manual'에서만 사용. chat/agent는 서버가 결합한 본문으로 대체하며 무시된다."
        ),
    )
    origin: Literal["manual", "chat", "agent"] = "manual"
    origin_ref: uuid.UUID | None = Field(
        default=None,
        description=(
            "chat: ChatMessage.id(assistant) / agent: AgentRun.id(succeeded). manual은 None."
        ),
    )
    artifact_name: str | None = Field(
        default=None,
        # StoredProvenance.artifact_name(ShortString)과 상한을 맞춘다(B-02) — 비대칭이면
        # 200자 넘는 실제 아티팩트 이름이 존재-검사는 통과한 뒤 내부 검증에서 터진다.
        max_length=200,
        description=(
            "origin='agent'에서만 사용 — AgentRun.artifact_keys[].name. 다른 origin에서는 무시된다."
        ),
    )
    # 요청도 응답과 **같은 형태로** 검증한다(R4CR-01, blocking).
    #
    # 응답만 `list[CitationOut]`으로 좁히고 요청은 `list[dict[str, Any]]`로 열어두면,
    # `filter_citations_to_notebook()`이 source_id/chunk_id 소속만 보기 때문에
    # `citations=[{}]`(둘 다 없음)가 두 가드를 모두 건너뛰고 **저장된다**. 그 뒤
    # `NoteOut.model_validate()`가 필수 5필드를 찾지 못해 HTTP 500 — 게다가 행이 남아
    # 해당 노트북의 `GET /notes`가 **영구적으로 500**이 된다(poison row).
    # 여기서 막으면 422로 거절되고 아무것도 저장되지 않는다.
    citations: list[CitationOut] = Field(
        default_factory=list,
        description=(
            "origin='manual'에서만 사용. chat은 서버가 원본 메시지의 citations로 재필터링한다."
        ),
    )


class NoteUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = Field(default=None, max_length=200_000)


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    notebook_id: uuid.UUID
    author_id: uuid.UUID
    title: str
    content: str
    origin: NoteOrigin
    origin_ref: uuid.UUID | None
    citations: list[CitationOut]
    provenance: ProvenanceOut | None = None
    content_intact: bool | None = None
    created_at: datetime
    updated_at: datetime
