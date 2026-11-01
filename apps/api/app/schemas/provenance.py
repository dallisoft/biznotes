"""생성 근거의 내부 저장 계약과 외부 공개 계약(v1)."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

ProvenanceMode = Literal["autonomous", "static", "agent_run", "unrecorded"]
ProvenanceKind = Literal["skill", "mcp", "tool", "source", "agent"]
ProvenanceStatus = Literal["succeeded", "failed", "blocked", "unknown"]
ShortString = Annotated[str, Field(max_length=200)]


class StoredProvenanceItem(BaseModel):
    """JSONB에 저장하는 항목. 내부 식별자는 공개 응답 모델에 포함하지 않는다."""

    model_config = ConfigDict(extra="ignore")

    kind: ProvenanceKind
    status: ProvenanceStatus
    name: ShortString | None = None
    version: int | None = None
    server: ShortString | None = None
    tool: ShortString | None = None
    source_id: ShortString | None = None
    title: ShortString | None = None
    count: int | None = Field(default=None, ge=0)
    skill_version_id: ShortString | None = None
    package_hash: ShortString | None = None
    agent_id: ShortString | None = None


class StoredProvenance(BaseModel):
    """서버 내부 JSONB 저장 형태."""

    model_config = ConfigDict(extra="ignore")

    v: Literal[1] = 1
    mode: ProvenanceMode
    recorded_at: datetime
    run_id: ShortString | None = None
    artifact_name: ShortString | None = None
    content_sha256: ShortString | None = None
    items: list[StoredProvenanceItem] = Field(default_factory=list, max_length=50)


class ProvenanceItemOut(BaseModel):
    """API에 공개하는 근거 항목. 저장 전용 ID와 해시는 의도적으로 없다."""

    model_config = ConfigDict(extra="ignore")

    kind: ProvenanceKind
    status: ProvenanceStatus
    name: ShortString | None = None
    version: int | None = None
    server: ShortString | None = None
    tool: ShortString | None = None
    source_id: ShortString | None = None
    title: ShortString | None = None
    count: int | None = Field(default=None, ge=0)


class ProvenanceOut(BaseModel):
    """API 공개 형태. content_sha256는 NoteOut.content_intact로만 노출한다."""

    model_config = ConfigDict(extra="ignore")

    v: Literal[1] = 1
    mode: ProvenanceMode
    recorded_at: datetime
    run_id: ShortString | None = None
    artifact_name: ShortString | None = None
    items: list[ProvenanceItemOut] = Field(default_factory=list, max_length=50)
