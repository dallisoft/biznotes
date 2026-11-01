"""서브에이전트 호출/오케스트레이션 노드 실행기(R2-5 기계적 추출, 동작 변경 없음).

runtime.py는 이 모듈의 `agent_visible_to`/`dispatch_agent`/`_run_subagent`/
`_exec_agent_call`/`_exec_sub_workflow`/`_exec_error_handler`/`_exec_supervisor`/
`_exec_save_artifact`/`_exec_http`/`_exec_mcp_tool`/`_exec_constant`를 재수출한다 —
NODE_EXECUTORS 등록·기존 공개 임포트 계약(R0-6) 하위 호환 유지.

순환 임포트 방지:
- `dispatch_agent`는 `execute_agent`(runtime.py에 잔류)를 호출해야 하므로, 함수 내부
  지연 임포트(`from biznotes_core.services.agents.runtime import execute_agent`)를 사용한다.
  runtime.py가 이 모듈을 재수출하기 위해 모듈 최상단에서 임포트하므로, 이 모듈이
  runtime을 최상단에서 역참조하면 순환 ImportError가 발생한다.
- `MAX_AGENT_DEPTH`(runtime.py 모듈 상수)도 같은 이유로 `_run_subagent` 내부에서
  지연 임포트한다 — `uv run python -c "import biznotes_core.services.agents.runtime"`으로
  순환 없음을 확인했다.
- `_exec_supervisor`가 호출하는 `_exec_llm`은 순환이 없는 리프 모듈(nodes/llm.py)에서
  최상단 임포트한다.
"""

import copy
import json
import uuid
import warnings
from typing import TYPE_CHECKING, Any

import structlog

from biznotes_core.config import get_settings
from biznotes_core.services.agents.context import RunContext, run_parallel
from biznotes_core.services.agents.errors import (
    AgentRuntimeError,
    SubagentApprovalNotSupportedError,
)
from biznotes_core.services.agents.node_errors import describe_node_failure
from biznotes_core.services.agents.nodes.llm import _exec_llm
from biznotes_core.services.agents.prompt_provenance import is_image_safe
from biznotes_core.services.agents.templating import (
    render,
    render_value,
    resolve_value_path,
    whole_value_ref,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


async def _exec_constant(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
    """상수 값을 상태에 주입 (스킬 지침/참조 본문 등). 템플릿 치환을 거치지 않으므로
    임의의 중괄호를 포함한 사용자 텍스트가 손상 없이 후속 노드 prompt에 값으로 들어간다."""
    value = node.get("value", {})
    return dict(value) if isinstance(value, dict) else {"value": value}


def _compose_value(value: Any, state: dict) -> Any:
    if isinstance(value, dict):
        return {k: _compose_value(v, state) for k, v in value.items()}
    if isinstance(value, list):
        return [_compose_value(v, state) for v in value]
    if isinstance(value, str):
        path = whole_value_ref(value)
        if path is not None:
            # 사본을 넣는다 — 뒤 노드가 결과를 고쳐도 앞 노드의 상태 값은 그대로다
            return copy.deepcopy(resolve_value_path(state, path))
        return render(value, state)
    return value


async def _exec_json_compose(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
    """LLM 없이 JSON을 조립한다(#361) — 템플릿의 값 참조를 문자열로 바꾸지 않고 값 그대로 채운다.

    `template`은 객체·목록·문자열이다. 문자열 전체가 참조 하나(`"{outline.json.title}"`,
    `"{slides.results[*].output}"`)면 그 값(객체·목록·수 포함)이 들어가고 없으면 null이다. 참조와
    글이 섞인 문자열은 다른 노드처럼 글로 렌더한다. 객체 키는 치환하지 않는다. 출력 `json`은 조립한
    값, `text`는 그 JSON 문자열이다 — `json_validate`의 `source`로 `"{노드.text}"`를 바로 쓴다.
    """
    if "template" not in node:
        raise AgentRuntimeError("json_compose 노드에 template이 없습니다")
    composed = _compose_value(node["template"], state)
    return {"json": composed, "text": json.dumps(composed, ensure_ascii=False, default=str)}


async def _exec_save_artifact(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
    import anyio

    from biznotes_core.services.storage import ensure_buckets, put_object

    name = render(node["name"], state)
    content = render(node["content"], state)
    content_type = node.get("content_type", "text/markdown")
    key = f"{ctx.run_id}/{name}"

    def _put():
        ensure_buckets()
        put_object(
            get_settings().storage_bucket_artifacts, key, content.encode("utf-8"), content_type
        )

    await anyio.to_thread.run_sync(_put)
    ctx.artifact_keys.append({"key": key, "name": name, "content_type": content_type})
    return {"key": key}


def _direct_request_hash(node: dict, request: dict[str, Any]) -> str:
    """렌더링한 요청 정본의 sha256(#153 A-3). 직렬화할 수 없으면 실행하지 않는다(fail closed)."""
    import hashlib
    import json

    try:
        canonical = json.dumps(
            {"node_id": str(node.get("id")), "type": node.get("type"), **request},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as e:
        raise AgentRuntimeError(
            f"노드 [{node.get('id')}]의 요청을 승인용으로 직렬화할 수 없습니다: {e}"
        ) from e
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _assert_lease_valid(ctx: RunContext, node: dict) -> None:
    """외부 호출 직전 lease 확인(#153 B) — 회수된 run에서 부작용을 내지 않는다."""
    from biznotes_core.services.agents.errors import RunLeaseLostError

    if ctx.lease_valid is not None and not ctx.lease_valid():
        raise RunLeaseLostError(
            f"run lease를 잃어 노드 [{node.get('id')}]의 외부 호출을 하지 않습니다"
        )


def _direct_node_gate(
    ctx: RunContext,
    node: dict,
    *,
    request: dict[str, Any],
    tool_name: str,
    arguments_summary: dict[str, Any],
) -> None:
    """직접 부작용 노드의 승인 게이트(#153 A-2). 통과하면 돌아오고, 아니면 예외다.

    - 워커가 소비한 승인의 앵커가 이 노드를 가리키면(노드의 `_resume`은 보지 않는다) 한 번만
      재개한다. 거부면 `DirectNodeRejectedError`, 다시 렌더링한 요청이 승인된 요청과 다르면
      `DirectNodeRequestChangedError`, 승인이면 돌아온다(호출자가 1회 실행한다).
    - 그 밖에는 `ApprovalRequired`로 그래프를 멈춘다. 체크포인트에 RunContext 스냅샷을 실어 재개 때
      비용·아티팩트·상한이 초기화되지 않는다.
    """
    from biznotes_core.services.agents.errors import (
        ApprovalRequired,
        DirectNodeRejectedError,
        DirectNodeRequestChangedError,
        GatedNodeInFanOutError,
    )
    from biznotes_core.services.agents.react import approval

    node_id = str(node.get("id"))
    if ctx.in_fan_out_graph:
        # 사전 차단(execute_agent) 뒤에 판정이 바뀐 경우(승인 행·scope 변경)의
        # 실행 중 방어(재리뷰 R1)
        raise GatedNodeInFanOutError(
            f"승인이 필요한 노드 [{node_id}]는 병렬(fan-out) 그래프에서 실행할 수 없습니다"
        )
    request_hash = _direct_request_hash(node, request)
    anchor = approval.claim_direct_resume(ctx, node_id)
    if anchor is None:
        raise ApprovalRequired(
            node_id,
            approval.build_direct_node_checkpoint(
                ctx,
                node_id,
                tool_name=tool_name,
                arguments_summary=arguments_summary,
                request_hash=request_hash,
            ),
        )
    K = approval.CheckpointKeys
    if anchor.get(K.REACT_APPROVAL_STATUS) != "approved":
        raise DirectNodeRejectedError(f"사용자가 노드 [{node_id}]의 요청을 거부했습니다")
    if anchor.get(K.DIRECT_REQUEST_HASH) != request_hash:
        raise DirectNodeRequestChangedError(
            f"노드 [{node_id}]의 요청이 승인한 요청과 달라 실행하지 않습니다"
        )


async def _exec_http(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
    import anyio
    import httpx

    # R5-1: SSRF 단일 관문 — 매 홉 재검증(리다이렉트 우회 차단). 등록 시점 1회
    # 검증 + 자체 httpx 요청은 리다이렉트로 사설 IP 도달이 가능했다.
    #
    # ⚠️ 계약 변경(R5-1-03): 이전에는 httpx 기본값(리다이렉트 미추적)으로 3xx를
    # 그대로 반환했다. 이제 관문이 리다이렉트를 **매 홉 재검증하며 추적**하고,
    # 리다이렉트 시 메서드를 GET으로 강등하고 body를 버린다(SSRF 안전화). 따라서
    # 3xx 상태를 기대하던 워크플로우는 최종 응답을 받고, 307/308 POST 리다이렉트는
    # GET(무body)으로 이어진다. 리다이렉트를 쓰지 않는 통상 요청은 동작 동일.
    # 받아오기 전용 모드(#395)에서는 GET·HEAD만 보내고 리다이렉트 뒤에도 원 메서드를 유지한다.
    from biznotes_core.core.ssrf import (
        EgressBlockedError,
        SSRFBlockedError,
        check_egress,
        ssrf_safe_request,
        validate_external_url,
    )
    from biznotes_core.services.agents.errors import NonRetryableNodeError
    from biznotes_core.services.agents.policy import (
        _http_node_egress_reasons,
        agent_egress_policy,
        direct_node_needs_gate,
    )
    from biznotes_core.services.agents.tool_registry import sanitize_for_approval_card

    url = render(node["url"], state)
    method = node.get("method", "POST").upper()
    headers = render_value(node.get("headers", {}), state)
    body = render_value(node.get("body", None), state)
    timeout = node.get("timeout", 30)
    allow_private = get_settings().ssrf_allow_private
    egress = agent_egress_policy()

    # 외부 연결 정책(#395)은 렌더 직후, DNS 사전 검사·승인 게이트보다 먼저 본다 — 위반이면 DNS
    # 질의도 승인 카드도 없다. 저장 검증을 우회한 정의도 여기서 막힌다. 관문도 다시 본다
    try:
        reasons = _http_node_egress_reasons(node, egress)
        if reasons:
            raise EgressBlockedError(reasons[0])
        check_egress(method, url, headers, body is not None, egress)
    except EgressBlockedError as e:
        e.record(method, url)
        raise AgentRuntimeError(str(e)) from e
    except SSRFBlockedError as e:
        raise AgentRuntimeError(str(e)) from e

    # 사전 검사는 게이트 전과 승인 후 실행 직전에 모두 돈다(#153 A-2) — 승인해도 막힐 요청을
    # 승인 카드로 띄우지 않는다
    try:
        await anyio.to_thread.run_sync(lambda: validate_external_url(url, allow_private))
    except SSRFBlockedError as e:
        raise AgentRuntimeError(str(e)) from e

    approved = False
    if await direct_node_needs_gate(ctx, node):
        _direct_node_gate(
            ctx,
            node,
            request={
                "method": method,
                "url": url,
                "headers": headers,
                "body": body,
                "timeout": timeout,
            },
            tool_name=f"http:{method}",
            arguments_summary=sanitize_for_approval_card(
                "http_request", {"method": method, "url": url, "headers": headers, "body": body}
            ),
        )
        approved = True

    _assert_lease_valid(ctx, node)
    try:
        res = await ssrf_safe_request(
            method,
            url,
            allow_private=allow_private,
            timeout=timeout,
            headers=headers,
            json=body,
            egress=egress,
        )
    except SSRFBlockedError as e:
        raise AgentRuntimeError(str(e)) from e
    except httpx.HTTPError as e:
        if approved:  # 이미 전달됐을 수 있다 — 재시도로 다시 보내지 않는다(#153 A-6)
            raise NonRetryableNodeError(f"승인된 HTTP 요청 실패: {e}") from e
        raise AgentRuntimeError(f"HTTP 요청 실패: {e}") from e
    return {"text": res.text[:100_000], "status": res.status_code}


async def _exec_mcp_tool(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
    from biznotes_core.models import MCPServer
    from biznotes_core.services.agents.errors import NonRetryableNodeError
    from biznotes_core.services.agents.mcp_client import call_tool, caller_meta
    from biznotes_core.services.agents.policy import (
        assert_mcp_tool_approved,
        assert_mcp_tool_node_allowed,
        direct_node_needs_gate,
    )
    from biznotes_core.services.agents.tool_registry import sanitize_for_approval_card

    # 전역 MCP 서버(관리자 자격증명)는 관리자가 관리하는 정의에서만 부른다(#77). 서버 조회 전에
    # 판정해 서버 존재 여부가 새지 않게 한다. `server_id`·`tool_name`은 렌더링하지 않는다 —
    # 노드의 `{params.*}`가 이 둘을 바꿀 수 없어야 한다. 사전 검사(정의 scope·서버·승인 행)는
    # 게이트 전과 승인 후 실행 직전에 모두 돈다(#153 A-2) — 대기 중 서버를 끄거나 승인을 거두면
    # 실행하지 않는다.
    await assert_mcp_tool_node_allowed(ctx)
    server = await ctx.db.get(MCPServer, uuid.UUID(str(node["server_id"])), populate_existing=True)
    if server is None or not server.enabled:
        raise AgentRuntimeError("MCP 서버를 찾을 수 없거나 비활성화됨")
    tool_name = str(node["tool_name"])
    arguments = render_value(node.get("arguments", {}), state)
    await assert_mcp_tool_approved(ctx, server.id, tool_name)

    approved = False
    if await direct_node_needs_gate(ctx, node):
        _direct_node_gate(
            ctx,
            node,
            request={"server_id": str(server.id), "tool_name": tool_name, "arguments": arguments},
            tool_name=f"mcp:{server.name}/{tool_name}",
            arguments_summary=sanitize_for_approval_card(
                "mcp_tool",
                {
                    "server_id": str(server.id),
                    "server": server.name,
                    "tool_name": tool_name,
                    "arguments": arguments,
                },
            ),
        )
        approved = True
    _assert_lease_valid(ctx, node)
    try:
        text = await call_tool(server, tool_name, arguments, meta=caller_meta(ctx, server))
    except Exception as e:
        if approved:  # 이미 전달됐을 수 있다 — 재시도로 다시 보내지 않는다(#153 A-6)
            raise NonRetryableNodeError(f"승인된 MCP 도구 호출 실패: {e}") from e
        raise
    return {"text": text}


def _visible_by_wrapper(agent: "Any", user_id: uuid.UUID | None, user_role: str) -> bool:
    from biznotes_core.services.authz import _agent_audience_allows_run

    if not agent.enabled:
        return False
    # 관리자 전용 system 정의(#436)는 run 사용자가 관리자이거나 사용자 없는 실행일 때만 보인다 —
    # 소유자 분기보다 먼저 본다
    if not _agent_audience_allows_run(agent, user_id, user_role):
        return False
    if agent.scope == "system":
        return True
    if user_role == "admin":
        return True
    return user_id is not None and agent.owner_id == user_id


def agent_visible_to(agent: "Any", user_id: uuid.UUID | None, user_role: str = "user") -> bool:
    """서브에이전트 호출/선택 가시성 — enabled & (system | 본인 소유 | admin).

    agent-picker 목록 필터와 런타임 호출 가드가 동일 규칙을 쓰도록 공용화. 관리자 전용 system
    정의(#436)는 admin과 사용자 없는 실행(`user_id` None)에만 보인다(소유자여도 그렇다).

    **스킬 wrapper에 대해서는 deprecated**(표준 계획 D8, R5-03): wrapper.scope는 freeze
    때만 갱신되고 스킬의 visibility·enabled·공개 version을 보지 못한다. 동작은 바꾸지
    않고 경고만 낸다. 대체는 `skill_agent_visible_to`이며, `0.13.0` 이상에서 두 제품의
    이관을 확인한 뒤 스킬 wrapper 판정을 이 함수에서 제거한다.
    """
    if getattr(agent, "source_kind", None) == "skill_package":
        warnings.warn(
            "agent_visible_to는 스킬 wrapper에 대해 deprecated입니다. "
            "skill_agent_visible_to를 쓰세요(0.13.0 이상에서 제거 예정).",
            DeprecationWarning,
            stacklevel=2,
        )
    return _visible_by_wrapper(agent, user_id, user_role)


async def _skill_of_wrapper(db: "AsyncSession", agent: "Any") -> "Any":
    from biznotes_core.models import Skill, SkillVersion

    if getattr(agent, "skill_version_id", None) is None:
        return None
    wrapper_version = await db.get(SkillVersion, agent.skill_version_id)
    if wrapper_version is None:
        return None
    return await db.get(Skill, wrapper_version.skill_id)


async def resolve_subagent_skill_version(
    db: "AsyncSession", agent: "Any", user_id: uuid.UUID | None, user_role: str = "user"
) -> uuid.UUID | None:
    """스킬 wrapper를 이 호출자가 서브에이전트로 실행할 version으로 푼다. 불가면 None.

    조건(D8 wrapper 행): `resolve_run_version(skill, actor)`가 있고 (system 스킬 또는
    소유자 또는 관리자). org 스킬의 비소유자 서브에이전트 호출은 지금처럼 허용하지 않는다.
    """
    from biznotes_core.services.skills.exposure import is_owner, resolve_run_version_id

    skill = await _skill_of_wrapper(db, agent)
    if skill is None:
        return None
    version_id = resolve_run_version_id(skill, user_id)
    if version_id is None:
        return None
    if skill.visibility == "system" or is_owner(skill, user_id) or user_role == "admin":
        return version_id
    return None


async def skill_agent_visible_to(
    db: "AsyncSession", agent: "Any", user_id: uuid.UUID | None, user_role: str = "user"
) -> bool:
    """서브에이전트 가시성 — 스킬 wrapper는 스킬의 노출 모델로, 그 밖은 기존 규칙으로 판정한다.

    `agent_visible_to`(동기)의 대체다. 워크플로우 저장 검사와 런타임 호출 가드가 같은
    규칙을 써야 저장과 실행의 판정이 갈리지 않는다(R5-03).
    """
    if getattr(agent, "source_kind", None) != "skill_package":
        return _visible_by_wrapper(agent, user_id, user_role)
    return await resolve_subagent_skill_version(db, agent, user_id, user_role) is not None


async def dispatch_agent(
    ctx: RunContext, agent: "Any", *, version_id: uuid.UUID | None = None
) -> str:
    """대상 에이전트를 실행 — skill_package wrapper면 builder 경로, 아니면 kind 경로.

    ctx는 child()로 만든 자식 컨텍스트(깊이/조상 체인 반영)여야 한다. 스킬 wrapper는
    wrapper가 가리키는 version이 아니라 `version_id`(없으면 호출 run 사용자의
    `resolve_run_version`)를 실행한다(D8, R4-03).
    """
    if agent.skill_version_id is not None:
        from biznotes_core.services.skills.builder import execute_skill_package
        from biznotes_core.services.skills.exposure import resolve_run_version_id

        if version_id is None:
            skill = await _skill_of_wrapper(ctx.db, agent)
            version_id = resolve_run_version_id(skill, ctx.user_id) if skill else None
        if version_id is None:
            raise AgentRuntimeError("실행할 수 있는 스킬 version이 없습니다")
        return await execute_skill_package(ctx, version_id)

    from biznotes_core.services.agents.runtime import (
        execute_agent,  # 순환 임포트 회피(모듈 상단 주석 참조)
    )

    return await execute_agent(ctx, agent.kind, agent.definition)


def _reject_subagent_approval(sub_ctx: RunContext, target_id: uuid.UUID) -> None:
    """서브에이전트가 승인 대기로 멈췄다면 명확한 오류로 거부한다.

    `child()`로 만든 컨텍스트로 `dispatch_agent`를 부른 호출부는 반환 직후 이 가드를 거쳐야
    한다. 자식 ctx의 `pending_approval`은 부모로 전달되지 않으므로, 거르지 않으면 승인 요청이
    조용히 사라지고 부모 run은 정상 완료된다(#80).
    """
    if sub_ctx.pending_approval is None:
        return
    # [phase-19-design.md §9 DA-02, HCI-D4] 서브에이전트 내부에서 발생한 승인
    # 요청의 checkpoint는 그 서브에이전트 자신의 그래프 노드 id를 가리키므로,
    # 최상위 execute_agent가 이를 그대로 재개(resume_state)에 사용할 수 없다
    # (재개 시 최상위 그래프에는 해당 노드가 없음 — 스파이크 결과 복잡도 과다로
    # 판단해 스코프를 좁힘). 서브에이전트 내부 HITL 승인은 이번 phase에서
    # 비허용하고 명확한 오류로 거부한다. 최상위 react_agent의 직접 도구 호출
    # 승인(가장 흔한 경로)은 정상 동작하므로 PLAN.md 수락 기준을 위반하지 않는다.
    sub_ctx.pending_approval = None
    raise SubagentApprovalNotSupportedError(
        f"서브에이전트 [{target_id}] 내부에서 승인이 필요한 도구 호출이 발생했습니다 — "
        "현재 서브에이전트 내부 HITL 승인은 지원되지 않습니다. 부작용 도구는 "
        "최상위 react_agent 노드에서 직접 호출하도록 워크플로우를 구성하세요."
    )


async def _run_subagent(
    ctx: RunContext,
    raw_id: Any,
    params: dict[str, Any],
    *,
    version_id: uuid.UUID | None = None,
    params_image_safe: bool = False,
) -> str:
    """서브에이전트 1회 실행 — 순환/깊이/가시성/kind 가드 후 dispatch.

    agent_call·sub_workflow·error_handler가 공유하는 단일 호출 경로. `version_id`는
    호출부가 이미 판정한 스킬 version이다(`run_skill`의 노출 version, R5-04).
    """
    from biznotes_core.models import Agent
    from biznotes_core.services.agents.runtime import (
        MAX_AGENT_DEPTH,  # 순환 임포트 회피(모듈 상단 주석 참조)
    )

    if not raw_id:
        raise AgentRuntimeError("서브에이전트 대상 id가 없습니다")
    try:
        target_id = uuid.UUID(str(raw_id))
    except ValueError as e:
        raise AgentRuntimeError(f"서브에이전트 id가 올바르지 않습니다: {raw_id}") from e

    # 순환 검출: 조상 체인 또는 현재 에이전트 자신.
    if str(target_id) in ctx.call_stack or target_id == ctx.agent_id:
        raise AgentRuntimeError(f"서브에이전트 순환 호출: {target_id}")
    if ctx.depth >= MAX_AGENT_DEPTH:
        raise AgentRuntimeError(f"서브에이전트 중첩 한도({MAX_AGENT_DEPTH}) 초과")

    target = await ctx.db.get(Agent, target_id)
    if target is None:
        raise AgentRuntimeError("서브에이전트를 찾을 수 없거나 호출 권한이 없습니다")
    if getattr(target, "source_kind", None) == "skill_package":
        resolved = await resolve_subagent_skill_version(
            ctx.db, target, ctx.user_id, ctx.user_role
        )
        if resolved is None:
            raise AgentRuntimeError("서브에이전트를 찾을 수 없거나 호출 권한이 없습니다")
        version_id = version_id or resolved
    elif not _visible_by_wrapper(target, ctx.user_id, ctx.user_role):
        raise AgentRuntimeError("서브에이전트를 찾을 수 없거나 호출 권한이 없습니다")
    if target.kind not in ("workflow", "skill"):
        raise AgentRuntimeError(f"호출할 수 없는 kind: {target.kind} (workflow/skill만 가능)")

    sub_ctx = ctx.child(
        target_id, params if isinstance(params, dict) else {}, params_image_safe=params_image_safe
    )
    # 스킬 wrapper일 때만 version 인자를 넘긴다 — 일반 에이전트 경로의 호출 모양을
    # 바꾸지 않아야 제품의 dispatch 스파이·데코레이터가 그대로 동작한다.
    result = (
        await dispatch_agent(sub_ctx, target, version_id=version_id)
        if version_id is not None
        else await dispatch_agent(sub_ctx, target)
    )
    _reject_subagent_approval(sub_ctx, target_id)
    return result


async def _exec_agent_call(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
    """서브에이전트 호출 — kind∈{workflow, skill} 재진입. 순환/깊이/가시성 가드."""
    raw_id = node.get("agent_id")
    if not raw_id:
        raise AgentRuntimeError("agent_call 노드에 agent_id가 없습니다")
    params = render_value(node.get("params", {}), state)
    if not isinstance(params, dict):
        params = {}
    safe = is_image_safe(node.get("params", {}), ctx.image_safe_roots)
    summary = await _run_subagent(ctx, raw_id, params, params_image_safe=safe)
    return {"text": summary}


async def _exec_sub_workflow(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
    """정식 서브워크플로우 호출 — 입력 매핑(input_map) + 출력 키(output_key) 명시.

    agent_call의 특수화: 부모 상태를 input_map으로 대상 params에 매핑하고,
    결과 요약을 {text} 및 (지정 시) {output_key}로 노출. 깊이/순환 가드 공유.
    """
    raw_id = node.get("workflow_id") or node.get("agent_id")
    if not raw_id:
        raise AgentRuntimeError("sub_workflow 노드에 workflow_id가 없습니다")

    input_map = node.get("input_map", node.get("params", {}))
    params = render_value(input_map, state)
    if not isinstance(params, dict):
        params = {}

    summary = await _run_subagent(
        ctx, raw_id, params, params_image_safe=is_image_safe(input_map, ctx.image_safe_roots)
    )
    out: dict[str, Any] = {"text": summary}
    output_key = node.get("output_key")
    if isinstance(output_key, str) and output_key and output_key != "text":
        out[output_key] = summary
    return out


async def _exec_error_handler(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
    """보호 대상 에이전트를 try/catch로 감싸 재시도(백오프) + 폴백 에이전트 실행.

    config: protected_agent_id(필수), params, max_retries, backoff,
            fallback_agent_id, fallback_params.
    반환: {text, recovered: bool, attempts: int, error?: str}.
    보호 대상 실패 + 폴백 없음/실패 시 AgentRuntimeError (노드 continue_on_error 적용 가능).
    """
    import asyncio

    protected = node.get("protected_agent_id") or node.get("agent_id")
    if not protected:
        raise AgentRuntimeError("error_handler 노드에 protected_agent_id가 없습니다")

    params = render_value(node.get("params", {}), state)
    if not isinstance(params, dict):
        params = {}
    max_retries = int(node.get("max_retries", 0))
    backoff = float(node.get("backoff", 1.0))

    last_err: Exception | None = None
    attempts = 0
    for attempt in range(max_retries + 1):
        attempts = attempt + 1
        try:
            text = await _run_subagent(ctx, protected, params)
            return {"text": text, "recovered": False, "attempts": attempts}
        except SubagentApprovalNotSupportedError:
            # [Angle C 리뷰] 승인 대기는 일시적 실패가 아니라 정책적 차단이므로
            # 재시도/백오프·폴백 경로로 흡수하지 않고 그대로 재전파한다 —
            # 그렇지 않으면 HITL 안전 정책이 "실패 복구"로 둔갑해 무력화된다.
            raise
        except Exception as e:  # noqa: BLE001 — 모든 실패를 폴백 경로로 흡수
            last_err = e
            logger.warning(
                "error_handler 보호 대상 실패",
                attempt=attempts,
                max_retries=max_retries,
                error=str(e),
            )
            if attempt < max_retries:
                await asyncio.sleep(backoff)

    fallback = node.get("fallback_agent_id")
    if fallback:
        fb_params = render_value(node.get("fallback_params", {}), state)
        if not isinstance(fb_params, dict):
            fb_params = {}
        try:
            text = await _run_subagent(ctx, fallback, fb_params)
            return {
                "text": text,
                "recovered": True,
                "attempts": attempts,
                # 성공 노드의 출력이라 실행 로그에 남는다 — 노드 실패와 같은 문구 규칙(#216)
                "error": (
                    describe_node_failure(last_err).message if last_err is not None else str(None)
                ),
            }
        except Exception as e:
            raise AgentRuntimeError(f"error_handler: 보호 대상과 폴백 모두 실패: {e}") from e

    raise AgentRuntimeError(f"error_handler: 보호 대상 실행 실패: {last_err}") from last_err


async def _exec_supervisor(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
    """슈퍼바이저/토론 오케스트레이션 — 여러 워커 에이전트에 팬아웃 후 결과를 종합.

    config:
      workers: [{agent_id, params?, label?}, ...]  (필수)
      parallel: bool                                (기본 True)
      synthesis_prompt: str                         (선택; 있으면 LLM으로 종합)
      model_id: str                                 (선택; 종합 LLM 고정)
    반환: {text, results: [{label, agent_id, text}, ...]}
    """
    workers = node.get("workers", [])
    if not isinstance(workers, list) or not workers:
        raise AgentRuntimeError("supervisor 노드에 workers가 없습니다")

    parallel = node.get("parallel", True)

    async def _run_worker(worker_ctx: RunContext, indexed: tuple[int, dict]) -> dict[str, Any]:
        idx, spec = indexed
        agent_id = spec.get("agent_id")
        params = render_value(spec.get("params", {}), state)
        if not isinstance(params, dict):
            params = {}
        label = str(spec.get("label", f"worker-{idx + 1}"))
        text = await _run_subagent(worker_ctx, agent_id, params)
        return {"label": label, "agent_id": str(agent_id), "text": text}

    if parallel:
        # 워커마다 분기 세션을 쓴다(#28) — 공유 세션을 형제와 동시에 쓰면 오류가 나거나 조용히
        # 직렬화된다. 워커 ctx는 in_parallel_branch=True다(T5/F1 react_agent 병렬 거부 가드).
        results = await run_parallel(ctx, list(enumerate(workers)), _run_worker)
    else:
        results = [await _run_worker(ctx, (i, w)) for i, w in enumerate(workers)]

    # 종합 — synthesis_prompt가 있으면 워커 결과를 주입해 LLM 1회 호출.
    synthesis_prompt = node.get("synthesis_prompt")
    if synthesis_prompt:
        joined = "\n\n".join(f"### {r['label']}\n{r['text']}" for r in results)
        synth_state = {**state, "results": joined}
        synth_node = {
            "prompt": synthesis_prompt,
            "system": node.get("system", ""),
            "model_id": node.get("model_id"),
            "max_tokens": node.get("max_tokens", 4096),
        }
        out = await _exec_llm(ctx, synth_node, synth_state)
        return {"text": out.get("text", ""), "results": results}

    # 종합 프롬프트가 없으면 워커 결과를 라벨과 함께 연결.
    combined = "\n\n".join(f"### {r['label']}\n{r['text']}" for r in results)
    return {"text": combined, "results": results}
