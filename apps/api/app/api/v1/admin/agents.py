"""어드민 — 에이전트 CRUD (definition은 JSON 에디터, 서버에서 스키마 검증)."""

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
from biznotes_core.services.agents.tool_registry import (
    TOOL_SIDE_EFFECT,
    find_llm_node_side_effect_tools,
    is_side_effect_tool,
)
from biznotes_core.services.audit import record
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.agent import AgentOut
from app.services.definition_policy import definition_policy_problems
from app.services.image_prompts import find_unsafe_image_prompts

router = APIRouter(prefix="/agents", route_class=RedactedValidationRoute)


class AgentAdminOut(AgentOut):
    definition: dict[str, Any]


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    icon: str = "🤖"
    kind: str = Field(pattern="^(workflow|skill|plugin|tool|mcp)$")
    definition: dict[str, Any]
    params_schema: dict[str, Any] = Field(default_factory=dict)
    scope: str = Field(default="system", pattern="^(system|group|user)$")
    enabled: bool = True


class AgentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    definition: dict[str, Any] | None = None
    params_schema: dict[str, Any] | None = None
    enabled: bool | None = None


def _validate_definition(
    kind: str, definition: dict[str, Any], *, agent_scope: str = "user"
) -> None:
    """kind별 definition 필수 구조 검증 + 저장 시점 정책.

    `agent_scope`는 정의가 저장될 scope다 — 생성은 `body.scope`, 수정은 `agent.scope`
    (`AgentUpdate`에는 scope가 없다). 생략하면 신뢰되지 않는 `"user"`로 판정한다(fail-closed).
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
        nodes, edges = kind_to_nodes(kind, definition)
        _topological_order(nodes, edges)
    except (KeyError, AgentRuntimeError) as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"definition 검증 실패: {e}"
        ) from e

    # workflows.py::_validate_structure와 같은 검사 — 위반을 한 번의 422에 담는다.
    # `kind_to_nodes`가 kind="mcp"를 mcp_tool 노드로, tool·plugin을 http 노드로 정규화한 뒤라
    # 그 표현에도 같은 규칙이 걸린다.
    problems: list[str] = []
    # [phase-22-design.md §2.3, D-14] 워크플로우 작성자가 실수로/의도적으로 HITL
    # 게이트를 우회하지 못하게 하는 일반적 안전장치.
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
    # 코어 v0.10.6 #77·#95 — 신뢰 집합 `_MCP_TRUSTED_AGENT_SCOPES = frozenset({"system"})`
    # (policy.py:19, 158-185). 관리자라도 group·user scope 정의에는 둘 수 없다.
    forbidden_mcp = find_forbidden_mcp_tool_nodes(nodes, agent_scope)
    if forbidden_mcp:
        listed = ", ".join(f"{node_id} ({node_kind})" for node_id, node_kind in forbidden_mcp)
        problems.append(
            "mcp_tool 노드와 react_agent의 mcp_call은 관리자가 관리하는(system) 정의에서만 쓸 수"
            f" 있습니다(scope {agent_scope}): {listed}"
        )
    # 코어 v0.10.6 #91·#96 — `_resume`은 워커만 런타임 사본에 주입한다(policy.py:191, 194-207)
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


# 서브에이전트를 가리키는 노드 타입 → 대상 id 필드(필수 여부). 사용자 경로
# workflows.py::_validate_agent_calls와 같은 목록이다.
_AGENT_REF_FIELDS: dict[str, list[tuple[str, bool]]] = {
    "agent_call": [("agent_id", True)],
    "sub_workflow": [("workflow_id", True)],
    "error_handler": [("protected_agent_id", True), ("fallback_agent_id", False)],
}


async def _validate_agent_call_targets(
    db: AsyncSession, kind: str, definition: dict[str, Any], self_id: uuid.UUID | None
) -> None:
    """서브에이전트 참조 대상의 존재·kind·직접 자기참조를 저장 시점에 검사한다(계획 P0-26).

    사용자 경로와 달리 가시성은 보지 않는다 — 관리자는 모든 정의를 볼 수 있다. 실행 시점의
    `_run_subagent`·`resolve_subagent_skill_version`이 같은 대상을 다시 판정하므로 이 검사는
    우회 방어가 아니라 "저장은 되는데 실행이 막히는 정의"를 미리 드러내는 것이다.
    """
    if kind != "workflow":
        return

    async def _check(node_type: str, raw_id: Any) -> None:
        try:
            target_id = uuid.UUID(str(raw_id))
        except ValueError as e:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{node_type} 대상 id가 올바르지 않습니다: {raw_id}",
            ) from e
        if self_id is not None and target_id == self_id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "워크플로우가 자기 자신을 호출할 수 없습니다"
            )
        target = await db.get(Agent, target_id)
        if target is None or target.kind not in ("workflow", "skill"):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{node_type} 대상 에이전트를 호출할 수 없습니다: {target_id}",
            )

    for node in definition.get("nodes", []) or []:
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("type"))
        if node_type == "supervisor":
            for worker in node.get("workers", []) or []:
                if isinstance(worker, dict) and worker.get("agent_id"):
                    await _check("supervisor", worker["agent_id"])
            continue
        for field_key, required in _AGENT_REF_FIELDS.get(node_type, []):
            raw_id = node.get(field_key)
            if not raw_id:
                if required:
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_CONTENT,
                        f"{node_type} 노드 '{node.get('id')}'에 대상 에이전트({field_key})가"
                        " 지정되지 않았습니다",
                    )
                continue
            await _check(node_type, raw_id)


def _validate_params_schema(schema: dict[str, Any]) -> None:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"params_schema가 유효한 JSON Schema가 아닙니다: {e.message}",
        ) from e


def _reject_skill_wrapper(agent: Agent) -> None:
    """스킬 wrapper는 실행 핸들이다 — 스킬 경로로만 바꾼다(biznotes-core 표준 계획 D8, R3-02).

    wrapper의 name·params_schema·enabled를 여기서 바꾸면 공개 관문을 우회해 다른
    사용자에게 보이는 값이 달라진다.
    """
    if agent.source_kind == "skill_package":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "스킬 wrapper는 /admin/skills 경로에서 관리합니다",
        )


@router.get("", response_model=list[AgentAdminOut])
async def list_agents_admin(db: AsyncSession = Depends(get_db)) -> list[AgentAdminOut]:
    rows = await db.execute(select(Agent).order_by(Agent.kind, Agent.name))
    return [AgentAdminOut.model_validate(a) for a in rows.scalars().all()]


class ToolPolicyOut(BaseModel):
    """도구별 side_effect 분류 + 이를 자동 승인하는 workflow agent 목록(19c)."""

    tool_name: str
    side_effect: bool
    auto_approved_by: list["AutoApprovalRef"]


class AutoApprovalRef(BaseModel):
    """특정 react_agent 노드가 이 도구에 부여한 auto_approve_tools 정책(19c)."""

    agent_id: uuid.UUID
    agent_name: str
    node_id: str
    policy: dict[str, Any]


ToolPolicyOut.model_rebuild()


@router.get("/tool-policies", response_model=list[ToolPolicyOut])
async def list_tool_policies(db: AsyncSession = Depends(get_db)) -> list[ToolPolicyOut]:
    """도구별 side_effect 분류 + 각 workflow agent의 react_agent 노드가
    설정한 auto_approve_tools 정책(동적, definition 스캔) 현황(phase-19-design.md §6).

    TOOL_SIDE_EFFECT는 내장 도구의 정적 분류이고, auto_approve_tools는 에이전트
    definition마다 다르므로 workflow kind 전체를 스캔해 집계한다. MCP 자율
    디스커버리 도구(mcp_<8hex>_<tool>)처럼 TOOL_SIDE_EFFECT에 없는 이름도
    실제로 정책이 걸려 있으면 `is_side_effect_tool`의 안전 기본값(True)으로
    분류해 목록에 포함한다 — 그렇지 않으면 감사 대상에서 조용히 누락된다
    (코드 리뷰 지적: 정적 맵 미등록 도구가 화면에서 사라지는 문제).
    """
    refs_by_tool: dict[str, list[AutoApprovalRef]] = {name: [] for name in TOOL_SIDE_EFFECT}

    rows = await db.execute(select(Agent).where(Agent.kind == "workflow"))
    for agent in rows.scalars().all():
        nodes = agent.definition.get("nodes", []) if isinstance(agent.definition, dict) else []
        for node in nodes:
            if not isinstance(node, dict) or node.get("type") != "react_agent":
                continue
            policy = node.get("auto_approve_tools")
            if not isinstance(policy, dict):
                continue
            for tool_name, rule in policy.items():
                refs_by_tool.setdefault(tool_name, [])
                refs_by_tool[tool_name].append(
                    AutoApprovalRef(
                        agent_id=agent.id,
                        agent_name=agent.name,
                        node_id=str(node.get("id", "")),
                        policy=rule if isinstance(rule, dict) else {},
                    )
                )

    return [
        ToolPolicyOut(
            tool_name=name,
            side_effect=is_side_effect_tool(name),
            auto_approved_by=refs,
        )
        for name, refs in refs_by_tool.items()
    ]


@router.post("", response_model=AgentAdminOut, status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: AgentCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> AgentAdminOut:
    _validate_definition(body.kind, body.definition, agent_scope=body.scope)
    await _validate_agent_call_targets(db, body.kind, body.definition, None)
    _validate_params_schema(body.params_schema)
    agent = Agent(**body.model_dump())
    db.add(agent)
    await record(db, admin.id, "agent.create", body.name, {"kind": body.kind})
    await db.commit()
    await db.refresh(agent)
    return AgentAdminOut.model_validate(agent)


@router.patch("/{agent_id}", response_model=AgentAdminOut)
async def update_agent(
    agent_id: uuid.UUID,
    body: AgentUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> AgentAdminOut:
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    _reject_skill_wrapper(agent)
    changes = body.model_dump(exclude_unset=True)
    if "definition" in changes:
        _validate_definition(agent.kind, changes["definition"], agent_scope=agent.scope)
        await _validate_agent_call_targets(db, agent.kind, changes["definition"], agent.id)
    if "params_schema" in changes:
        _validate_params_schema(changes["params_schema"])
    for field, value in changes.items():
        setattr(agent, field, value)
    agent.version += 1
    await record(db, admin.id, "agent.update", agent.name)
    await db.commit()
    await db.refresh(agent)
    return AgentAdminOut.model_validate(agent)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> None:
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    _reject_skill_wrapper(agent)
    await record(db, admin.id, "agent.delete", agent.name)
    await db.delete(agent)
    await db.commit()
