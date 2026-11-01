"""사용자용 워크플로우 CRUD — admin /agents 미러, 소유권 강제 (workflow-visual-editor-plan §3-B).

라이프사이클: in-place + version++ (별도 버전 테이블 없음).
신규 생성은 enabled=false(draft), publish가 enabled=true. 저장(PATCH)마다 version++.
스코프: scope="user"+owner_id 개인, scope="system" admin 전역(사용자는 duplicate로 복제).
"""

import uuid
from typing import Any

from biznotes_core.api.deps import get_current_user
from biznotes_core.api.routing import RedactedValidationRoute
from biznotes_core.core.db import get_db
from biznotes_core.models import Agent, User
from biznotes_core.services.agents.policy import (
    find_direct_node_approval_violations,
    find_forbidden_mcp_tool_nodes,
    find_reserved_node_keys,
)
from biznotes_core.services.agents.runtime import skill_agent_visible_to
from biznotes_core.services.agents.tool_registry import find_llm_node_side_effect_tools
from biznotes_core.services.audit import record
from biznotes_core.services.authz import agent_base_policy, system_agent_audience_condition
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.agent import (
    CallableAgentOut,
    WorkflowCreate,
    WorkflowOut,
    WorkflowUpdate,
)
from app.services.definition_policy import definition_policy_problems
from app.services.image_prompts import find_unsafe_image_prompts

router = APIRouter(
    prefix="/workflows", tags=["workflows"], route_class=RedactedValidationRoute
)


def _can_edit(agent: Agent, user: User) -> bool:
    """편집 권한 — 소유자 또는 admin. system 워크플로우는 admin만(사용자는 duplicate).

    system 정의에 비관리자 `owner_id`가 남아 있어도(강등된 관리자·옛 데이터) 소유자로 편집하지
    못한다. 허용하면 비관리자가 공개 system 정의에 `audience: "admin"`(코어 v0.29.0 #436)을 붙여
    다른 사용자에게서 숨길 수 있다(독립 리뷰 High 1).
    """
    if user.role == "admin":
        return True
    if agent.scope == "system":
        return False
    return agent.owner_id == user.id


def _to_out(agent: Agent, user: User) -> WorkflowOut:
    out = WorkflowOut.model_validate(agent)
    out.can_edit = _can_edit(agent, user)
    return out


def _validate_structure(
    definition: dict[str, Any],
    params_schema: dict[str, Any],
    *,
    agent_scope: str = "user",
) -> None:
    """definition 토폴로지 + 저장 시점 정책 + params_schema JSON Schema 정적 검증 (admin 검증 미러).

    `agent_scope`는 정의가 저장될(또는 저장된) scope다 — 생성·복제는 `"user"`(이 라우터가
    리터럴로 저장하는 값), 수정·발행은 `agent.scope`. 생략하면 신뢰되지 않는 `"user"`로
    판정한다(fail-closed — 생략이 신뢰를 만들지 않는다).
    """
    from biznotes_core.services.agents.runtime import (
        AgentRuntimeError,
        _topological_order,
        kind_to_nodes,
    )
    from biznotes_core.services.agents.tool_registry import (
        find_always_gated_auto_approve_violations,
    )

    try:
        nodes, edges = kind_to_nodes("workflow", definition)
        _topological_order(nodes, edges)
    except (KeyError, AgentRuntimeError) as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"definition 검증 실패: {e}"
        ) from e

    # 코어 공개 헬퍼 다섯 개를 모두 돌려 위반을 한 번의 422에 담는다 — 작성자가 정의를 한 번에
    # 고치게 한다(코어 후속 R10). 코어는 이 헬퍼들을 스스로 호출하지 않으므로 저장 시 거부는
    # 이 배선의 몫이고, 실행 시점에는 코어 런타임이 같은 규칙으로 노드를 실패시킨다.
    # admin/agents.py::_validate_definition과 같은 검사다.
    problems: list[str] = []
    # [phase-22-design.md §2.3, D-14] react_agent auto_approve_tools의 항상-승인 도구
    blocked = find_always_gated_auto_approve_violations(nodes)
    if blocked:
        problems.append(
            f"auto_approve_tools에 다음 도구를 포함할 수 없습니다(항상 승인 필요): {blocked}"
        )
    # 코어 v0.10.3 #70 — llm 노드 도구 루프에는 승인 게이트가 없다
    side_effect = find_llm_node_side_effect_tools(nodes)
    if side_effect:
        problems.append(
            "llm 노드 tools에는 부작용 도구를 둘 수 없습니다(승인 게이트가 있는 react_agent로"
            f" 옮기세요): {side_effect}"
        )
    # 코어 v0.10.6 #77·#95 — `find_forbidden_mcp_tool_nodes(nodes, agent_scope) ->
    # list[tuple[str, str]]`, 신뢰 집합 `_MCP_TRUSTED_AGENT_SCOPES = frozenset({"system"})`
    # (policy.py:19, 158-185). 실행자 role이 아니라 정의의 scope로 판정한다 — admin이 이 라우터로
    # 저장해도 scope는 "user"다.
    forbidden_mcp = find_forbidden_mcp_tool_nodes(nodes, agent_scope)
    if forbidden_mcp:
        listed = ", ".join(f"{node_id} ({kind})" for node_id, kind in forbidden_mcp)
        problems.append(
            "mcp_tool 노드와 react_agent의 mcp_call은 관리자가 관리하는(system) 정의에서만 쓸 수"
            f" 있습니다(scope {agent_scope}): {listed}"
        )
    # 코어 v0.10.6 #91·#96 — `_resume`은 워커가 소비한 승인 checkpoint를 사본에만 싣는 내부 키
    # (policy.py:191, 194-207). 저장된 `_resume`은 승인 재개 위조 경로다.
    reserved = find_reserved_node_keys(nodes)
    if reserved:
        problems.append(
            f"노드 정의에 승인 재개 전용 내부 키를 저장할 수 없습니다: {', '.join(reserved)}"
        )
    # 코어 v0.10.15 #153 A — 직접 부작용 노드(http·mcp_tool)의 승인 설정. `requires_approval:
    # false`는 system 정의 + (http면) 리터럴 url 대상일 때만, 승인이 필요한 노드는 병렬(fan-out)
    # 그래프에 둘 수 없다. 런타임 게이트(`direct_node_needs_gate`)와 같은 규칙을 저장 시점에 걸어
    # "저장은 되는데 실행 전에 멈추는" 정의를 막는다(core-v0.11.0-upgrade HCI-7 ②, D-26).
    direct = find_direct_node_approval_violations(nodes, edges, agent_scope)
    if direct:
        problems.append(f"직접 노드 승인 설정을 저장할 수 없습니다: {', '.join(direct)}")
    # 코어 v0.23.0 #338 D1 — image_generate 프롬프트는 출처가 안전한 값만 참조한다. 런타임은
    # 그래프 구성 때 같은 규칙으로 run을 실패시킨다(core-v0.23.0-upgrade D2)
    problems.extend(find_unsafe_image_prompts(nodes))
    # 코어 v0.29.0 #436·#429·#428 — 최상위 `audience`, 노드 `reasoning_effort`·`min_output_tokens`
    problems.extend(definition_policy_problems(definition, nodes))
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, " / ".join(problems))

    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    try:
        Draft202012Validator.check_schema(params_schema)
    except SchemaError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"params_schema가 유효한 JSON Schema가 아닙니다: {e.message}",
        ) from e


async def _validate_agent_calls(
    db: AsyncSession,
    definition: dict[str, Any],
    user: User,
    self_id: uuid.UUID | None,
) -> None:
    """서브에이전트 참조 노드의 대상 가시성 + 직접 자기참조 정적 검사 (전이 순환은 런타임 가드).

    대상: agent_call(agent_id), sub_workflow(workflow_id),
    error_handler(protected_agent_id·fallback_agent_id).
    """
    # 노드 타입 → 검사할 에이전트-id 필드 (필수 여부)
    ref_fields: dict[str, list[tuple[str, bool]]] = {
        "agent_call": [("agent_id", True)],
        "sub_workflow": [("workflow_id", True)],
        "error_handler": [("protected_agent_id", True), ("fallback_agent_id", False)],
    }
    async def _check_target(node_type: str, raw_id: Any) -> None:
        try:
            target_id = uuid.UUID(str(raw_id))
        except ValueError as e:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{node_type} 대상 id가 올바르지 않습니다: {raw_id}",
            ) from e
        if self_id is not None and target_id == self_id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "워크플로우가 자기 자신을 호출할 수 없습니다",
            )
        target = await db.get(Agent, target_id)
        # 스킬 wrapper는 노출 모델로 판정한다(biznotes-core D8, R5-03) — 저장 검사와
        # 실행 검사가 같은 규칙을 써야 저장은 되는데 실행이 막히는 상태가 생기지 않는다.
        if (
            target is None
            or target.kind not in ("workflow", "skill")
            or not await skill_agent_visible_to(db, target, user.id, user.role)
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{node_type} 대상 에이전트를 호출할 수 없습니다: {target_id}",
            )

    nodes = definition.get("nodes", [])
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("type"))
        # supervisor: workers[].agent_id 검사
        if node_type == "supervisor":
            for worker in node.get("workers", []) or []:
                if isinstance(worker, dict) and worker.get("agent_id"):
                    await _check_target("supervisor", worker["agent_id"])
            continue
        fields = ref_fields.get(node_type)
        if not fields:
            continue
        for field_key, required in fields:
            raw_id = node.get(field_key)
            if not raw_id:
                if required:
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_CONTENT,
                        f"{node_type} 노드 '{node.get('id')}'에 대상 에이전트({field_key})가"
                        " 지정되지 않았습니다",
                    )
                continue
            await _check_target(node_type, raw_id)


async def _get_for_view(db: AsyncSession, workflow_id: uuid.UUID, user: User) -> Agent:
    agent = await db.get(Agent, workflow_id)
    if agent is None or agent.kind != "workflow":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")
    # 코어 `agent_base_policy` — system ∨ 소유자 ∨ admin, 단 관리자 전용 system 정의(`audience`,
    # 코어 v0.29.0 #436)는 소유자여도 비관리자에게 닫힌다. 존재를 숨기려고 404다
    if not agent_base_policy(user, agent):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")
    return agent


async def _get_for_edit(db: AsyncSession, workflow_id: uuid.UUID, user: User) -> Agent:
    agent = await _get_for_view(db, workflow_id, user)
    if not _can_edit(agent, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "편집 권한이 없습니다")
    return agent


# ---------- 목록 / 상세 ----------


@router.get("", response_model=list[WorkflowOut])
async def list_workflows(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[WorkflowOut]:
    """내 워크플로우 + system 빌트인(enabled). draft(enabled=false)는 본인 것만 노출."""
    stmt = (
        select(Agent)
        .where(
            Agent.kind == "workflow",
            or_(
                Agent.owner_id == user.id,
                (Agent.scope == "system") & Agent.enabled.is_(True),
            ),
            # 관리자 전용 system 정의(#436) — 소유자 분기 바깥에 AND로 건다(소유자 예외 없음)
            system_agent_audience_condition(user),
        )
        .order_by(Agent.updated_at.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [_to_out(a, user) for a in rows]


@router.get("/callable", response_model=list[CallableAgentOut])
async def list_callable_agents(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CallableAgentOut]:
    """agent_call 노드가 호출 가능한 서브에이전트 목록 — workflow/skill, 가시성 필터.

    스킬 wrapper는 저장 검사(`_validate_agent_calls`)와 같은 노출 모델(`skill_agent_visible_to`)로
    한 번 더 거른다. wrapper의 `enabled`·`scope`는 freeze 때만 갱신되어 스킬의 공개 철회·보류·
    비활성을 보지 못하므로(코어 `nodes/composite.py`의 deprecated 표시), 그것만으로 거르면 목록이
    제시한 대상이 저장 시 422가 된다(계획 P0-09).
    """
    stmt = (
        select(Agent)
        .where(
            Agent.kind.in_(("workflow", "skill")),
            Agent.enabled.is_(True),
            or_(Agent.owner_id == user.id, Agent.scope == "system"),
            system_agent_audience_condition(user),  # #436 — 바깥 AND
        )
        .order_by(Agent.kind, Agent.name)
    )
    rows = (await db.execute(stmt)).scalars().all()
    visible = [
        a
        for a in rows
        if a.source_kind != "skill_package"
        or await skill_agent_visible_to(db, a, user.id, user.role)
    ]
    return [CallableAgentOut.model_validate(a) for a in visible]


@router.get("/{workflow_id}", response_model=WorkflowOut)
async def get_workflow(
    workflow_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkflowOut:
    agent = await _get_for_view(db, workflow_id, user)
    return _to_out(agent, user)


# ---------- 생성 / 복제 ----------


@router.post("", response_model=WorkflowOut, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    body: WorkflowCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkflowOut:
    definition = body.definition.model_dump()
    params_schema = body.params_schema.model_dump()
    _validate_structure(definition, params_schema, agent_scope="user")
    await _validate_agent_calls(db, definition, user, None)
    agent = Agent(
        name=body.name,
        description=body.description,
        icon=body.icon,
        kind="workflow",
        definition=definition,
        params_schema=params_schema,
        scope="user",
        owner_id=user.id,
        enabled=False,  # draft
        version=1,
    )
    db.add(agent)
    await record(db, user.id, "workflow.create", body.name)
    await db.commit()
    await db.refresh(agent)
    return _to_out(agent, user)


@router.post("/{workflow_id}/duplicate", response_model=WorkflowOut)
async def duplicate_workflow(
    workflow_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkflowOut:
    """가시 워크플로우를 내 소유 draft로 복제 (스킬 fork와 동일 UX)."""
    src = await _get_for_view(db, workflow_id, user)
    definition = dict(src.definition)
    params_schema = dict(src.params_schema)
    # 사본은 scope "user"로 저장된다 — system 원본의 mcp_tool 노드처럼 사본 scope에서 쓸 수 없는
    # 정의는 저장 시점에 422로 드러낸다(코어 수락 기준 "복제 사본 거부", 계획 D-26·P0-14).
    _validate_structure(definition, params_schema, agent_scope="user")
    clone = Agent(
        name=f"{src.name} (복사본)",
        description=src.description,
        icon=src.icon,
        kind="workflow",
        definition=definition,
        params_schema=params_schema,
        scope="user",
        owner_id=user.id,
        enabled=False,
        version=1,
    )
    db.add(clone)
    await record(db, user.id, "workflow.duplicate", src.name, {"from": str(src.id)})
    await db.commit()
    await db.refresh(clone)
    return _to_out(clone, user)


# ---------- 편집 / 게시 / 삭제 ----------


@router.patch("/{workflow_id}", response_model=WorkflowOut)
async def update_workflow(
    workflow_id: uuid.UUID,
    body: WorkflowUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkflowOut:
    agent = await _get_for_edit(db, workflow_id, user)
    changes = body.model_dump(exclude_unset=True)
    if "definition" in changes:
        params_schema = changes.get("params_schema", agent.params_schema)
        _validate_structure(changes["definition"], params_schema, agent_scope=agent.scope)
        await _validate_agent_calls(db, changes["definition"], user, agent.id)
    elif "params_schema" in changes:
        _validate_structure(agent.definition, changes["params_schema"], agent_scope=agent.scope)
    for field, value in changes.items():
        setattr(agent, field, value)
    agent.version += 1
    await record(db, user.id, "workflow.update", agent.name)
    await db.commit()
    await db.refresh(agent)
    return _to_out(agent, user)


@router.post("/{workflow_id}/publish", response_model=WorkflowOut)
async def publish_workflow(
    workflow_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkflowOut:
    """게시 — enabled=true. 저장 즉시 실행 정의가 되므로 구조 검증 통과 필수."""
    agent = await _get_for_edit(db, workflow_id, user)
    _validate_structure(agent.definition, agent.params_schema, agent_scope=agent.scope)
    await _validate_agent_calls(db, agent.definition, user, agent.id)
    if not agent.definition.get("nodes"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "노드가 없는 워크플로우는 게시할 수 없습니다"
        )
    agent.enabled = True
    agent.version += 1
    await record(db, user.id, "workflow.publish", agent.name)
    await db.commit()
    await db.refresh(agent)
    return _to_out(agent, user)


@router.post("/{workflow_id}/unpublish", response_model=WorkflowOut)
async def unpublish_workflow(
    workflow_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkflowOut:
    """게시 취소 — enabled=false(draft)로 되돌림."""
    agent = await _get_for_edit(db, workflow_id, user)
    agent.enabled = False
    await record(db, user.id, "workflow.unpublish", agent.name)
    await db.commit()
    await db.refresh(agent)
    return _to_out(agent, user)


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(
    workflow_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    agent = await _get_for_edit(db, workflow_id, user)
    await record(db, user.id, "workflow.delete", agent.name)
    await db.delete(agent)
    await db.commit()
