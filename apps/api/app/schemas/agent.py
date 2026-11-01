import uuid
from datetime import datetime
from typing import Any

from biznotes_core.schemas.agent import AgentInput as AgentInput
from biznotes_core.schemas.agent import JsonSchemaProperty as JsonSchemaProperty
from biznotes_core.schemas.agent import ParamsSchema as ParamsSchema
from biznotes_core.schemas.enums import (
    AgentKind,
    AgentScope,
    ApprovalStatus,
    RunStatus,
    TriggerSource,
)
from pydantic import BaseModel, ConfigDict, Field

# 입력 계약(JsonSchemaProperty·ParamsSchema·AgentInput)의 정본은 코어다(표준 계획 D12).
# 스킬 라우터가 코어로 옮겨가며 함께 이관됐고, 여기서는 재노출만 한다. 재노출은 `X as X`
# 형식으로 표시한다 — `__all__`로 표시하면 이 모듈의 공개 이름이 셋으로 좁아져 `AgentOut` 등
# 본 모듈 스키마가 `import *`·문서 도구에서 사라진다.


class ArtifactRefOut(BaseModel):
    """실행이 남긴 아티팩트 참조 — 스토리지 오브젝트 키 + 표시용 메타.

    실측(2026-07-10): `agent_runs.artifact_keys`의 원소 64건이 전부 이 3키 객체이고,
    유일한 생산자(`nodes/composite.py:60`)도 3키를 항상 함께 쓴다.

    그럼에도 `name`·`content_type`에 기본값을 둔다(R4CR-02). 이 값은 **JSONB에서 그대로
    읽어오므로 스키마가 데이터를 강제하지 못한다** — 체크포인트 재개(`react/approval.py`의
    `restore_run_context`)나 과거/부분 행에 필드가 빠져 있으면, 필수로 두었을 때
    `AgentRunOut` 직렬화가 실패해 **실행 목록 전체가 500**이 된다. 표시용 메타 하나 때문에
    목록을 잃는 것보다 빈 문자열로 성능 저하(degrade)하는 편이 낫다.

    `key`는 기본값을 두지 않는다 — 없으면 아티팩트를 가리키지 못해 어차피 무의미하다.
    """

    key: str
    name: str = ""
    content_type: str = ""


class WorkflowNode(BaseModel):
    """워크플로우 노드의 공통 외곽 구조; 노드별 속성은 런타임이 해석한다."""

    model_config = ConfigDict(extra="allow")

    id: str
    type: str


class WorkflowDefinition(BaseModel):
    """워크플로우 DAG의 외곽 구조.

    최상위의 그 밖의 키(`audience`·`output` 등)는 그대로 보존한다. 버리면 관리자가 사용자 편집기로
    관리자 전용 system 정의(`audience: "admin"`, 코어 v0.29.0 #436)를 저장할 때 키가 사라져 모두에게
    열린다(fail-open). 값의 검증은 저장 경로(`definition_policy_problems`)와 코어 런타임이 한다.
    """

    model_config = ConfigDict(extra="allow")

    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[list[str]] = Field(default_factory=list)


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str
    icon: str
    kind: AgentKind
    params_schema: ParamsSchema
    scope: AgentScope
    enabled: bool
    version: int
    # 입력 정책(mode/scope) — 소스 피커 노출용. 기본 rag/all.
    input: AgentInput = Field(default_factory=AgentInput)
    # react_agent 노드의 cost_budget_usd(있으면) — 실행 전 비용 안내용(phase-22-design.md
    # §2.4). definition은 노출하지 않으므로 list_agents가 추출해 채운다.
    cost_budget_usd: float | None = None


class WorkflowOut(BaseModel):
    """사용자용 워크플로우 — definition 포함(에디터 캔버스 구동)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str
    icon: str
    kind: AgentKind
    definition: WorkflowDefinition
    params_schema: ParamsSchema
    scope: AgentScope
    owner_id: uuid.UUID | None = None
    enabled: bool
    version: int
    created_at: datetime
    updated_at: datetime
    # 권한 플래그 — FE가 편집/삭제 버튼 노출 결정
    can_edit: bool = False


class WorkflowCreate(BaseModel):
    name: str = Field(default="제목 없는 워크플로우", min_length=1, max_length=100)
    description: str = ""
    icon: str = "⚙️"
    definition: WorkflowDefinition = Field(default_factory=WorkflowDefinition)
    params_schema: ParamsSchema = Field(default_factory=ParamsSchema)


class WorkflowUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    definition: WorkflowDefinition | None = None
    params_schema: ParamsSchema | None = None


class CallableAgentOut(BaseModel):
    """agent-picker(서브에이전트 선택)용 요약 — params_schema로 파라미터 매핑 폼 생성."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    icon: str
    kind: AgentKind
    scope: AgentScope
    params_schema: ParamsSchema


class AgentRunCreate(BaseModel):
    agent_id: uuid.UUID
    params: dict[str, Any] = Field(default_factory=dict)
    # scope:selected 실행 시 사용자가 고른 소스. None/빈 = 노트 전체(scope:all).
    # skill param과 혼동 방지를 위해 params_schema가 아닌 run의 1급 필드.
    source_ids: list[uuid.UUID] | None = None
    # run 단위 요청 모델(#216, 코어 #359) — `model_id`를 고정하지 않은 llm·react 노드의
    # 1차 모델이다. None = 지정 안 함(기본 모델 + 폴백 체인). 켜진 llm 모델만 받는다
    # (아니면 422 `ModelUnavailable`).
    model_id: uuid.UUID | None = None


class NodeModelOut(BaseModel):
    """노드에 실제로 응답한 모델 — 코어 llm·react 노드 출력의 `model`(#359).

    실행 로그 행(`ExecutionLogOut.model`)과 run 목록(`AgentRunOut.used_models`, #234)이 함께 쓴다.
    """

    id: uuid.UUID
    display_name: str


class AgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    notebook_id: uuid.UUID
    params: dict[str, Any]
    # 실행 스코프 핀 — scope:selected로 고른 소스(관측·재현성). None = 노트 전체.
    input_source_ids: list[uuid.UUID] | None = None
    trigger: TriggerSource = "manual"
    status: RunStatus
    progress: int
    result_summary: str | None
    artifact_keys: list[ArtifactRefOut]
    # 토큰 카운터 — 실측상 값은 전부 정수다.
    token_usage: dict[str, int]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    # 스킬 실행 스냅샷 (skill_package wrapper 실행 시 박제)
    skill_version_id: uuid.UUID | None = None
    # 요청한 run 모델(#216). None = 기본 모델. 실제로 응답한 모델은 노드 실행 로그의
    # `model`이다(폴백 때문에 다를 수 있다)
    model_id: uuid.UUID | None = None
    # 조인 필드
    agent_name: str | None = None
    agent_icon: str | None = None
    # 호출자 기준 취소 인가(실행 소유자 ∨ 노트북 write 이상, #173). 상태와 무관하다 — 웹이
    # 진행 중 상태와 함께 보고 「취소」 버튼을 그린다.
    can_cancel: bool = False
    # 호출자 기준 숨김 인가(#220) — 실패·취소 run ∧ 취소 규칙. 웹이 항목 메뉴 「숨기기」를 그린다.
    # 숨긴 run에서는 되돌리기 인가다 — 웹이 「숨긴 결과」의 「다시 보이기」를 그린다(#221)
    can_hide: bool = False
    # 노트북 목록에서 숨긴 run인지(#220·#221) — 숨긴 사람·시각은 싣지 않는다
    hidden: bool = False
    # 실제로 응답한 모델(처음 응답한 순서, 중복 없음)과 폴백 여부(#234) — 상세 「사용 모델」과 같은
    # 규칙이다(`app.services.run_models`). 목록 응답만 실행 로그를 조회해 채우고 생성·단건·취소
    # 응답은 빈 값이다. `default_factory`가 아니라 리터럴 기본값을 둔다 — 생성 타입이 필수 필드가
    # 된다
    used_models: list[NodeModelOut] = []
    fallback_used: bool = False


class HiddenRunsOut(BaseModel):
    """「실패한 결과 모두 숨기기」 결과(#220) — 이번에 숨긴 run. 웹 「되돌리기」가 id로 되돌린다."""

    hidden: int
    run_ids: list[uuid.UUID]


class AgentRunApprovalOut(BaseModel):
    """HITL 승인 요청 — 프론트 승인 카드/이력 렌더용(phase-19-design.md §1.1).

    checkpoint는 절대 노출하지 않는다(재개용 임시 상태, 시크릿 포함 가능 —
    SEC-D1). arguments_summary만 마스킹된 형태로 노출한다.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    tool_name: str
    arguments_summary: dict[str, Any]
    status: ApprovalStatus
    resolved_by: uuid.UUID | None = None
    requested_at: datetime
    resolved_at: datetime | None = None
    expires_at: datetime
