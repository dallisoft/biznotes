"""스킬 API 요청·응답 계약 (표준 계획 D5·D8·D10·D12).

두 제품의 `app.schemas.skill`을 코어로 옮겼다. 응답 모델은 sl-agents 기준(`SkillFileOut`,
`SkillValidateOut` RootModel)으로 맞췄다. 제품 스키마는 이 클래스를 재노출한다.

**API 호환(AGENTS.md §5.1)**: 기존 응답 키는 모두 유지하고 필드를 추가만 한다.
의미가 바뀐 필드(D8, R3-12):
- `SkillOut.current_version_id` — 비소유자에게는 노출 version id의 별칭이다(한 릴리스 병행).
- `SkillOut.params_schema`·`input` — 비소유자에게는 노출 version 기준이다.
- `SkillOut.status` — 공개 포인터에서 파생한 값이다.

호출자 기준 파생 필드(#135): `SkillOut.chat_entry_reason` — #85 채팅 진입 판정 사유. 비소유자
(관리자 포함)는 노출 version 기준이고, wrapper가 없으면(freeze 전) None이다.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

from biznotes_core.schemas.agent import AgentInput, ParamsSchema
from biznotes_core.schemas.enums import (
    ChatEntryReasonValue,
    SkillFileRole,
    SkillOrigin,
    SkillReviewKind,
    SkillReviewStatus,
    SkillRuntimeProfile,
    SkillStatus,
    SkillValidationProfile,
    SkillVersionStatus,
    SkillVisibility,
)


class SkillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID | None
    name: str
    description: str
    slug: str
    tags: list[str]
    visibility: SkillVisibility
    status: SkillStatus
    current_version_id: uuid.UUID | None
    forked_from_version_id: uuid.UUID | None
    clone_count: int
    run_count: int
    enabled: bool
    created_at: datetime
    updated_at: datetime
    # 다른 사용자에게 드러나는 version(D8). 비소유자에게는 노출 조건을 통과할 때만 채운다.
    published_version_id: uuid.UUID | None = None
    origin: SkillOrigin = "authored"
    # 조인/파생 필드
    wrapper_agent_id: uuid.UUID | None = None
    params_schema: ParamsSchema = Field(default_factory=ParamsSchema)  # 보는 version에서 파생
    # 입력 정책(mode/scope) — 보는 version의 manifest에서 파생, 소스 피커 노출용
    input: AgentInput = Field(default_factory=AgentInput)
    can_edit: bool = False
    can_run: bool = False
    # 호출자 기준 채팅 팔레트(`/`) 진입 판정 사유(#85·#135). wrapper가 없으면(freeze 전) None
    chat_entry_reason: ChatEntryReasonValue | None = None


class AdminSkillOut(SkillOut):
    """관리자 목록 — 철회 보류와 대기 중인 검토 요청 수를 함께 보여 준다(R4-17)."""

    exposure_hold: bool = False
    pending_review_count: int = 0


class SkillImportOut(SkillOut):
    """가져오기 결과 — 네 층 보고서와 루트 폴더 정보(D4·D5)."""

    import_report: dict[str, Any] = Field(default_factory=dict)


class SkillImportUrlRequest(BaseModel):
    """원격 ZIP·`.skill`·tar.gz 가져오기 요청(Phase 5)."""

    url: str = Field(min_length=1, max_length=4096)
    # 선택한 archive와 정확히 일치해야 한다. 형식·대조는 서비스 경계에서 고정 reason으로 한다.
    sha256: str | None = Field(default=None, max_length=128)
    selected_skill_path: str | None = Field(default=None, max_length=512)
    # 소유자의 가져온 스킬 draft를 바꾼다(#402). 없으면 새 스킬을 만든다
    target_skill_id: uuid.UUID | None = None
    # 출처 키(예: `github:owner/repo@sha:path`). 형식은 서비스가 `bad_source_ref`로 검사한다 —
    # 스키마 제약의 422 본문이 입력값(URL·토큰일 수 있다)을 되돌려주지 않게 여기 두지 않는다.
    source_ref: str | None = None


class SkillImportUrlCandidatesRequest(BaseModel):
    """원격 archive의 스킬 후보 조회 요청(#402). 저장하지 않는다."""

    url: str = Field(min_length=1, max_length=4096)


class SkillImportCandidateOut(BaseModel):
    """여러 스킬 archive에서 사용자가 고를 수 있는 SKILL.md 경로와 사전 점검 요약(#402).

    요약은 archive 메타데이터와 그 후보의 SKILL.md frontmatter만 본다. `ok`여도 비밀정보·
    매니페스트·stages 검사는 가져올 때 확정된다.
    """

    path: str
    name: str | None = None
    description: str | None = None
    file_count: int = 0
    ok: bool = False
    errors: list[str] = Field(default_factory=list)  # 가져오기 거부 사유 코드
    warnings: list[str] = Field(default_factory=list)  # 이식성·루트 불일치 경고 코드
    validation_profile: str | None = None


class SkillImportCandidatesOut(BaseModel):
    candidates: list[SkillImportCandidateOut] = Field(default_factory=list)
    # 조회한 archive의 SHA-256 — `import-url`의 `sha256`으로 넘겨 같은 archive임을 고정한다
    archive_sha256: str | None = None
    truncated: int = 0  # 후보 상한을 넘어 빠진 후보 수


class SkillCatalogEntryOut(BaseModel):
    """채팅·에이전트의 표준 스킬 카탈로그 항목(표준 계획 D6-1). `shadowed_by`가 있으면 가려졌다."""

    name: str
    description: str
    skill_id: uuid.UUID
    version_id: uuid.UUID
    source: Literal["own", "system", "org"]
    shadowed_by: uuid.UUID | None = None


class SkillCatalogOut(BaseModel):
    enabled: bool
    # 번들 스크립트 실행 백엔드가 켜져 있는가(D7) — 제품 UI의 "스크립트 실행 허가 요청" 표시용
    scripts_enabled: bool = False
    entries: list[SkillCatalogEntryOut] = Field(default_factory=list)
    truncated: int = 0


class SkillFileOut(BaseModel):
    path: str
    role: SkillFileRole
    size: int
    media_type: str
    # 기존 패키지는 hash 없이도 읽을 수 있게 두되, 신규 package builder는 항상 채운다.
    hash: str = ""


class SkillVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    skill_id: uuid.UUID
    version: int
    package_hash: str
    manifest: dict[str, Any]
    file_tree: list[SkillFileOut]
    status: SkillVersionStatus
    changelog: str | None
    created_at: datetime
    validation_profile: SkillValidationProfile = "legacy_v1"
    runtime_profile: SkillRuntimeProfile = "legacy_instruction_v1"


class SkillVersionMetadataOut(BaseModel):
    """version 메타데이터 — 저장소를 읽지 않고 DB 값만 준다."""

    version_id: uuid.UUID
    skill_id: uuid.UUID
    version: int
    name: str | None
    description: str | None
    frontmatter: dict[str, Any]
    validation_profile: SkillValidationProfile
    runtime_profile: SkillRuntimeProfile
    report: dict[str, Any]
    package_hash: str


class SkillValidateSuccessOut(BaseModel):
    ok: Literal[True]
    name: str
    description: str
    file_count: int
    manifest: dict[str, Any]
    references: list[str]
    # 네 층 보고서(D5) — 추가 필드
    report: dict[str, Any] = Field(default_factory=dict)


class SkillValidateFailureOut(BaseModel):
    ok: Literal[False]
    reason: str
    message: str
    report: dict[str, Any] = Field(default_factory=dict)


class SkillValidateOut(RootModel[SkillValidateSuccessOut | SkillValidateFailureOut]):
    pass


class SkillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    template: str = Field(default="instruction")


class SkillUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = None
    # private→org는 공개 관문을 거친다. 검토가 필요하면 409 review_pending이고 같은 요청의
    # 다른 필드도 적용하지 않는다(D8 경로 2).
    visibility: str | None = Field(default=None, pattern="^(private|org)$")


class SkillFileWrite(BaseModel):
    content: str


class SkillFrontmatterOut(BaseModel):
    """폼 편집 가능 여부와 현재 SKILL.md frontmatter (Phase 2)."""

    editable: bool
    fields: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None
    source_hash: str


class SkillFrontmatterWrite(BaseModel):
    """폼이 보내는 전체 frontmatter. 알 수 없는 키는 서비스 경계에서 거부한다."""

    fields: dict[str, Any] = Field(default_factory=dict)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SkillFreezeRequest(BaseModel):
    changelog: str | None = Field(default=None, max_length=2000)


class SkillPublishRequest(BaseModel):
    changelog: str | None = Field(default=None, max_length=2000)


class SkillFromArtifactRequest(BaseModel):
    run_id: uuid.UUID
    artifact_name: str


class SkillCloneOut(BaseModel):
    skill_id: uuid.UUID
    status: SkillStatus


class SkillFreezeOut(BaseModel):
    version_id: uuid.UUID
    version: int
    wrapper_agent_id: uuid.UUID
    status: SkillVersionStatus


class SkillVersionPublishOut(BaseModel):
    """version publish 결과 — 200 `published` 또는 202 `review_pending`(D8 경로 1)."""

    status: Literal["published", "review_pending"]
    version_id: uuid.UUID
    request_id: uuid.UUID | None = None


class SkillReviewRequestCreate(BaseModel):
    kind: SkillReviewKind
    note: str | None = Field(default=None, max_length=2000)


class SkillReviewRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version_id: uuid.UUID
    kind: SkillReviewKind
    target_visibility: SkillVisibility | None
    status: SkillReviewStatus
    requested_by: uuid.UUID | None
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    note: str | None
    created_at: datetime
    # 검토 큐 표시용
    skill_id: uuid.UUID | None = None
    skill_name: str | None = None
    version: int | None = None


class SkillReviewDecision(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class AdminSkillUpdate(BaseModel):
    enabled: bool | None = None
    # 승격(→system, private→org)은 version_id가 필요하다. 관리자 승격은 그 version의
    # expose_* 승인으로 기록된다(D8 경로 3).
    visibility: str | None = Field(default=None, pattern="^(private|org|system)$")
    version_id: uuid.UUID | None = None


class AdminPublishedVersionSet(BaseModel):
    """공개 version 지정. `null`은 철회이고 보류를 세운다(D8 경로 4)."""

    version_id: uuid.UUID | None
