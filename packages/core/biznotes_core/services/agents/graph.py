"""에이전트 그래프 오케스트레이션 — kind 정규화·위상정렬·LangGraph 실행(R2-6 추출).

runtime.py god-module 분해의 마지막 단계. runtime.py는 이 모듈의 공개 심볼
(kind_to_nodes/execute_agent/NODE_EXECUTORS/_topological_order 및 상수)을 재수출해
기존 임포트 계약(admin/agents.py·workers·skills/builder·특성화 테스트, R0-6)과
NODE_EXECUTORS monkeypatch(setitem) seam을 하위 호환 유지한다.

NODE_EXECUTORS는 nodes/·react 하위 모듈의 실행기를 모아 구성하며, 이 모듈이 그
실행기들을 임포트한다. 노드 모듈이 execute_agent/MAX_AGENT_DEPTH를 참조하는 경로는
runtime 재수출 경유 함수내 지연 임포트라 임포트 시점 순환이 없다.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import (
    AgentRuntimeError,
    ApprovalRequired,
    DirectNodeRejectedError,
    DirectNodeRequestChangedError,
    GatedNodeInFanOutError,
    NonRetryableNodeError,
    RunLeaseLostError,
    SubagentApprovalNotSupportedError,
    UntrustedResumeError,
)
from biznotes_core.services.agents.logging import (
    _log_node_finish,
    _log_node_start,
    _truncate_output,
)
from biznotes_core.services.agents.node_errors import describe_node_failure
from biznotes_core.services.agents.nodes.composite import (
    _exec_agent_call,
    _exec_constant,
    _exec_error_handler,
    _exec_http,
    _exec_json_compose,
    _exec_mcp_tool,
    _exec_save_artifact,
    _exec_sub_workflow,
    _exec_supervisor,
)
from biznotes_core.services.agents.nodes.digest import _exec_source_digest
from biznotes_core.services.agents.nodes.image import _exec_image_generate
from biznotes_core.services.agents.nodes.llm import _exec_llm
from biznotes_core.services.agents.nodes.retrieval import (
    _exec_condition,
    _exec_loop,
    _exec_rag_search,
    _exec_source_load,
)
from biznotes_core.services.agents.nodes.save_sources import _exec_save_sources
from biznotes_core.services.agents.nodes.validation import (
    _exec_citation_verify,
    _exec_json_validate,
)
from biznotes_core.services.agents.output_truncation import truncation_of
from biznotes_core.services.agents.prompt_provenance import (
    check_image_prompt_sources,
    image_safe_roots,
)
from biznotes_core.services.agents.react import approval
from biznotes_core.services.agents.react.loop import _exec_react_agent
from biznotes_core.services.agents.templating import resolve_value_path, whole_value_ref

logger = structlog.get_logger()

_STATE_RESERVED_KEYS = {"params", "user", "chat_message", "chat_history"}


# 서브에이전트(agent_call) 중첩 한도 — 무한 재귀/예산 폭주 방지.
MAX_AGENT_DEPTH = 5

# 노드 단위 토큰 델타 추적 대상 키. 사고 토큰(#422)은 0이면 기록하지 않는다(공급자가 보고할 때만).
_TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens")

# 노드 input_data로 기록할 정의 키(런타임 내부 키/대용량 본문 제외).
_LOG_INPUT_KEYS = (
    "query",
    "top_k",
    "source_ids",
    # rag_search 소스 커버리지(#446)
    "per_source_limit",
    "coverage",
    "max_sources",
    "max_chars",
    # source_load 줄이는 방식(#447) — 리터럴 설정
    "fit",
    "max_tokens_per_source",
    # source_digest 담을 예산(#448)
    "budget_tokens",
    "concurrency",
    "system",
    "name",
    "content_type",
    "url",
    "method",
    "server_id",
    "tool_name",
    "arguments",
    "agent_id",
    "items",
    "item_var",
    "parallel",
    "max_items",
    "on_item_error",
    "loop_body_agent_id",
    "max_retries",
    "fallback_agent_id",
    "model_id",
    "max_tokens",
    # 사고 양(#429) — llm·react_agent·json_validate의 리터럴 설정
    "reasoning_effort",
    # 출력 하한(#428) — llm(스트림)·json_validate
    "min_output_tokens",
    "protected_agent_id",
    "backoff",
    "workflow_id",
    "output_key",
    "tools",
    "synthesis_prompt",
    "prompt_ref",
    "source",
    "schema",
    "template",
    # image_generate — 장수·크기는 기록, prompt 본문은 제외(llm과 동일).
    "n",
    "size",
    "aspect_ratio",
    # react_agent — goal(짧음)·가드 파라미터는 기록, prompt 본문은 제외(llm과 동일).
    "goal",
    "allowed_tools",
    "max_iters",
    "token_budget",
    "cost_budget_usd",
    "max_subagent_calls",
    "required_initial_tools",
    # source_load 도구 1회 상한·남은 예산 비율(#389) — react_agent·도구를 쓰는 llm 노드
    "source_load_max_sources",
    "source_load_max_chars",
    "source_load_budget_ratio",
    # save_sources(#403) — 리터럴 설정만 기록한다(items는 위의 템플릿 키, 렌더링 값은 제외)
    "namespace",
    "allowed_hosts",
    "notebook_rule",
)


def kind_to_nodes(kind: str, definition: dict[str, Any]) -> tuple[list[dict], list[list[str]]]:
    """kind별 정의를 노드/엣지 목록으로 정규화."""
    if kind == "workflow":
        return definition["nodes"], definition.get("edges", [])

    if kind == "skill":
        nodes: list[dict] = []
        edges: list[list[str]] = []
        prev: str | None = None
        if definition.get("use_rag", True):
            nodes.append(
                {
                    "id": "search",
                    "type": "rag_search",
                    "query": definition.get("rag_query", "{params.topic}"),
                    # 생략은 생략으로 남긴다 — 노드가 어드민 RAG 설정으로 해석한다(#45)
                    "top_k": definition.get("top_k"),
                }
            )
            prev = "search"
        nodes.append(
            {
                "id": "generate",
                "type": "llm",
                "system": definition.get("system", ""),
                "prompt": definition["prompt_template"],
            }
        )
        if prev:
            edges.append([prev, "generate"])
        nodes.append(
            {
                "id": "save",
                "type": "save_artifact",
                "name": definition.get("artifact_name", "result.md"),
                "content": "{generate.text}",
                "content_type": definition.get("artifact_type", "text/markdown"),
            }
        )
        edges.append(["generate", "save"])
        return nodes, edges

    if kind in ("tool", "plugin"):
        http = definition["http"]
        return (
            [
                {
                    "id": "call",
                    "type": "http",
                    **http,
                },
                {
                    "id": "save",
                    "type": "save_artifact",
                    "name": definition.get("artifact_name", "response.json"),
                    "content": "{call.text}",
                    "content_type": definition.get("artifact_type", "application/json"),
                },
            ],
            [["call", "save"]],
        )

    if kind == "mcp":
        return (
            [
                {
                    "id": "call",
                    "type": "mcp_tool",
                    "server_id": definition["server_id"],
                    "tool_name": definition["tool_name"],
                    "arguments": definition.get("arguments", {}),
                    # 직접 노드 승인 설정(#153) — 없으면 기본(승인 필요)이다
                    **(
                        {"requires_approval": definition["requires_approval"]}
                        if "requires_approval" in definition
                        else {}
                    ),
                },
                {
                    "id": "save",
                    "type": "save_artifact",
                    "name": definition.get("artifact_name", "mcp-result.md"),
                    "content": "{call.text}",
                    "content_type": "text/markdown",
                },
            ],
            [["call", "save"]],
        )

    raise AgentRuntimeError(f"알 수 없는 kind: {kind}")


NODE_EXECUTORS = {
    "constant": _exec_constant,
    "condition": _exec_condition,
    "loop": _exec_loop,
    "rag_search": _exec_rag_search,
    "source_load": _exec_source_load,
    "source_digest": _exec_source_digest,
    "llm": _exec_llm,
    "json_validate": _exec_json_validate,
    "json_compose": _exec_json_compose,
    "citation_verify": _exec_citation_verify,
    "save_artifact": _exec_save_artifact,
    "http": _exec_http,
    "image_generate": _exec_image_generate,
    "mcp_tool": _exec_mcp_tool,
    "save_sources": _exec_save_sources,
    "agent_call": _exec_agent_call,
    "sub_workflow": _exec_sub_workflow,
    "error_handler": _exec_error_handler,
    "supervisor": _exec_supervisor,
    "react_agent": _exec_react_agent,
}


def _structured_output_path(
    kind: str, definition: dict[str, Any], node_map: dict[str, dict]
) -> str | None:
    """워크플로우 정의 `output`(#361)의 값 경로. 없으면 None이다.

    값 참조 하나(`"{validate.json}"`, `[n]`·`[*]` 첨자 허용)여야 하고 뿌리는 이 그래프의 노드다.
    아니면 어떤 노드도 돌기 전에 실패한다(작성 오류를 조용히 null로 넘기지 않는다).
    """
    if kind != "workflow" or "output" not in definition:
        return None
    raw = definition["output"]
    path = whole_value_ref(raw)
    if path is None or path.split(".", 1)[0].split("[", 1)[0] not in node_map:
        raise AgentRuntimeError(f"workflow output은 노드를 가리키는 값 참조여야 합니다: {raw!r}")
    return path


def _topological_order(nodes: list[dict], edges: list[list[str]]) -> list[str]:
    ids = [n["id"] for n in nodes]
    incoming: dict[str, set[str]] = {i: set() for i in ids}
    outgoing: dict[str, set[str]] = {i: set() for i in ids}
    for src, dst in edges:
        incoming[dst].add(src)
        outgoing[src].add(dst)
    order: list[str] = []
    ready = [i for i in ids if not incoming[i]]
    while ready:
        cur = ready.pop(0)
        order.append(cur)
        for nxt in list(outgoing[cur]):
            incoming[nxt].discard(cur)
            if not incoming[nxt]:
                ready.append(nxt)
    if len(order) != len(ids):
        raise AgentRuntimeError("워크플로우 그래프에 순환이 있습니다")
    return order


def _plain_edges(node_map: dict[str, dict], edges: list[list[str]]) -> list[tuple[str, str]]:
    """`graph.add_edge`로 넣는 일반 엣지 — 미지 노드의 엣지와 조건 노드에서 나가는 엣지는 뺀다
    (조건 노드는 `add_conditional_edges`로 한 갈래만 탄다). 그래프 구성과 fan-out 판정이 이 목록
    하나를 같이 쓴다(#28 D3) — 필터를 따로 두면 판정과 실제 그래프가 어긋난다."""
    cond = {nid for nid, n in node_map.items() if n.get("type") == "condition"}
    return [
        (src, dst)
        for src, dst in edges
        if src in node_map and dst in node_map and src not in cond
    ]


def _has_fan_out(node_map: dict[str, dict], edges: list[list[str]]) -> bool:
    """LangGraph가 형제 노드를 같은 superstep에서 동시에 돌리는 그래프인가(#28 D3).

    일반 엣지에서 서로 다른 목적지가 둘 이상인 노드가 있으면 그렇다. 같은 엣지는 한 번만 센다
    (LangGraph는 엣지를 집합으로 다룬다). 진입점은 하나라 fan-out 외에는 동시 실행이 없다.
    """
    targets: dict[str, set[str]] = {}
    for src, dst in _plain_edges(node_map, edges):
        targets.setdefault(src, set()).add(dst)
    return any(len(dsts) >= 2 for dsts in targets.values())


async def _reject_gated_direct_nodes_in_fan_out(
    ctx: RunContext, node_map: dict[str, dict], completed: set[str]
) -> None:
    """승인이 필요한 직접 노드가 fan-out 그래프에 있으면 어떤 노드도 돌기 전에 실패한다(#153 A-4).

    LangGraph는 형제 하나가 예외를 내면 나머지를 취소한다. 게이트 노드가 형제와 함께 돌면 승인이
    끝없이 반복되고, 승인된 요청이 전송 중에 취소되거나 중복될 수 있다. 저장 시점 헬퍼
    (`find_direct_node_approval_violations`)가 먼저 막고, 이 검사는 검증 전에 저장된 정의를 막는다.
    """
    from biznotes_core.services.agents.policy import (
        DIRECT_SIDE_EFFECT_NODE_TYPES,
        direct_node_needs_gate,
    )

    gated = [
        node_id
        for node_id, node in node_map.items()
        if node_id not in completed
        and node.get("type") in DIRECT_SIDE_EFFECT_NODE_TYPES
        and await direct_node_needs_gate(ctx, node)  # 실행기와 같은 판정(재리뷰 R1)
    ]
    if gated:
        raise GatedNodeInFanOutError(
            f"승인이 필요한 노드 {sorted(gated)}는 병렬(fan-out) 그래프에서 실행할 수 없습니다 — "
            "형제 노드와 함께 돌면 승인 대기가 형제를 취소합니다. 노드를 순차 경로로 옮기세요"
        )


async def execute_agent(
    ctx: RunContext,
    kind: str,
    definition: dict[str, Any],
    resume_state: dict[str, Any] | None = None,
) -> str:
    """노드 파이프라인을 LangGraph StateGraph로 실행. 반환: 결과 요약.

    resume_state가 주어지면(HITL 승인 재개, phase-19-design.md §2.2) 이미 완료된
    노드의 출력을 그대로 state에 주입하고, 그 노드들의 executor는 재호출하지
    않는다(no-op 치환) — 그래프 구조(add_node/add_edge/entry_point)는 최초 실행과
    완전히 동일하게 유지한다. "완료 노드를 그래프에서 제외"하는 방식은 LangGraph가
    고아 edge를 ValueError로 거부해 채택하지 않는다(CDX-D5).

    react_agent 노드 실행 중 ApprovalRequired가 발생하면(부작용 도구 승인 대기)
    그래프 실행을 중단하고 `ctx.pending_approval`에 재개용 checkpoint를 채운 뒤
    빈 문자열을 반환한다 — 반환 타입은 항상 str로 유지해 기존 호출자
    (execute_skill_package·dispatch_agent 등)를 깨뜨리지 않는다(CDX-D3).
    """
    from typing import Annotated

    from langgraph.graph import END, StateGraph

    def merge_dicts(a: dict, b: dict) -> dict:
        if not a:
            return b
        if not b:
            return a
        return {**a, **b}

    nodes, edges = kind_to_nodes(kind, definition)
    node_map = {n["id"]: n for n in nodes}
    order = _topological_order(nodes, edges)
    total = len(order)

    # [phase-19-design.md §2.2, Angle A 리뷰] state의 예약 메타키("params"/"user")와
    # 노드 id가 충돌하면, 재개 시 completed_node_ids 판정이 그 노드를 항상 완료로
    # 오판해 실제 로직(부작용 포함)을 no-op으로 건너뛴다. 그래프 구성 시점에 즉시
    # 거부해 이 충돌이 재개 단계까지 조용히 흘러가지 않도록 한다.
    reserved_ids = _STATE_RESERVED_KEYS & set(node_map)
    if reserved_ids:
        raise AgentRuntimeError(f"노드 id는 예약어를 사용할 수 없습니다: {sorted(reserved_ids)}")
    output_path = _structured_output_path(kind, definition, node_map)
    # 이미지 프롬프트가 출처가 안전하지 않은 값(검색 citations·HTTP·MCP 등)을 참조하면 어떤 노드도
    # 돌기 전에 실패한다(#338 D1). loop·agent_call·sub_workflow는 이 집합으로 자식 params를 판정한다
    ctx.image_safe_roots = image_safe_roots(
        node_map,
        params_safe=ctx.params_image_safe,
        params_safe_keys=ctx.params_image_safe_keys,
    )
    for n in nodes:
        if n.get("type") == "image_generate":
            check_image_prompt_sources(n, ctx.image_safe_roots)

    graph: StateGraph = StateGraph(Annotated[dict, merge_dicts])  # type: ignore[arg-type]
    done_count = {"n": 0}

    # 상태는 런타임이 직접 관리한다 — LangGraph dict 스키마는 노드가 반환한
    # 키만 채널로 유지하므로 초기 params가 후속 노드에서 소실되는 문제가 있음
    runtime_state = {
        "params": ctx.params,
        "user": {
            "id": str(ctx.user_id) if ctx.user_id else "",
            "email": ctx.user_email or "",
            "name": ctx.user_name or "",
            "role": ctx.user_role,
        },
        "chat_message": ctx.chat_message,
        "chat_history": ctx.chat_history,
    }
    if resume_state is not None:
        state: dict[str, Any] = dict(resume_state)
        state.update(runtime_state)
        # graph_state에는 params/user 메타 키와 완료 노드 id가 함께 들어있다 —
        # 노드 id만 completed로 취급(node_map에 없는 params/user는 자연히 제외).
        completed_node_ids = {nid for nid in order if nid in state}
    else:
        state = runtime_state
        completed_node_ids = set()

    # fan-out 그래프(#28 D3)는 형제가 같은 superstep에서 동시에 돈다 — 노드마다 분기 세션과
    # 컨텍스트 사본을 쓴다. 분기를 얻지 못한 노드(분기 안에서 예산 소진, 격리 불가)는 이 그래프의
    # 잠금으로 호출자 세션을 한 번에 하나씩 쓴다.
    fan_out = _has_fan_out(node_map, edges)
    if fan_out:
        await _reject_gated_direct_nodes_in_fan_out(ctx, node_map, completed_node_ids)
    degrade_lock = asyncio.Lock()

    def make_node(node_id: str):
        node = node_map[node_id]
        executor = NODE_EXECUTORS.get(node["type"])
        if executor is None:
            raise AgentRuntimeError(f"알 수 없는 노드 타입: {node['type']}")

        async def _run(_graph_state: dict) -> dict:
            if node_id in completed_node_ids:
                # [phase-19-design.md §2.2, CDX-D5] 재개 시 이미 완료된 노드는
                # executor를 다시 호출하지 않는다(부작용 재실행 방지). 그래프
                # 구조는 무변경이므로 LangGraph가 정상적으로 이 노드를 거쳐가되,
                # state[node_id]는 이미 resume_state로 채워져 있어 재대입 불필요.
                # 관측 로그(execution_logs)는 최초 실행에서 이미 기록됐으므로 여기서
                # 중복 기록하지 않되, 진행률 콜백은 재개 UX를 위해 호출한다.
                done_count["n"] += 1
                if fan_out:
                    # 격리할 수 없어 모든 노드가 강등된 그래프(연결 bind 세션)에서는 강등 노드가
                    # run 세션을 쓰는 중이다 — run 세션을 커밋하기도 하는 제품 콜백과 겹치지
                    # 않게 한다
                    async with degrade_lock:
                        await ctx.emit_progress(node_id, done_count["n"], total)
                else:
                    await ctx.emit_progress(node_id, done_count["n"], total)
                return {}
            if not fan_out:
                return await _execute(ctx, owned=False)
            # 노드별 사본 — per-node 셀(node_tokens·cost_usd·기준선 스냅샷)이 형제와 섞이지 않고,
            # 세션도 형제와 나눠 쓰지 않는다. in_parallel_branch는 바꾸지 않는다(D5).
            async with ctx.isolated_branch(mark_parallel=False) as (nctx, isolated):
                if "_resume" in node and approval._is_resume_target(ctx, node_id):
                    # 재개 기준선은 승인 게이트 노드만 부모에서 1회 소비한다(D4) — LangGraph는
                    # 형제를 이름순으로 시작하므로 "먼저 도는 노드"가 가져가면 엉뚱한 노드에
                    # 계상된다. 워커가 승인을 소비한 앵커가 가리키는 노드만 가져간다(#91) —
                    # 정의에 `_resume`만 둔 노드가 기준선을 가로채지 못한다
                    nctx.resume_node_token_base = ctx.resume_node_token_base
                    nctx.resume_node_cost_base = ctx.resume_node_cost_base
                    ctx.resume_node_token_base = None
                    ctx.resume_node_cost_base = None
                if isolated:
                    return await _execute(nctx, owned=True)
                async with degrade_lock:
                    return await _execute(nctx, owned=False)

        async def _execute(ctx: RunContext, *, owned: bool) -> dict:
            """노드 1회 실행 — `ctx`는 이 노드가 쓰는 컨텍스트(fan-out이면 노드별 사본).

            owned: `ctx.db`가 이 노드 소유의 분기 세션인지 — 재시도 전에 그 세션만 되돌린다(D9).
            """
            retry_policy = node.get("retry_policy", {})
            max_retries = int(retry_policy.get("max_retries", 0))
            delay = float(retry_policy.get("delay", 1.0))

            input_data = {k: v for k, v in node.items() if k in _LOG_INPUT_KEYS}

            # 관측성: 노드 시작 기록 + 토큰/비용 기준선(델타 산출용).
            # [R5-2] 토큰 델타는 **미공유 node_tokens** 기준으로 산출한다 — 공유 usage로
            # 재면 서브에이전트 토큰이 부모 노드 행 + 자식 행에 이중 계상된다(monitoring
            # SUM). cost_usd(미공유)와 동형.
            started_at = datetime.now(UTC)
            # [R5-2-01] 재개된 gated 노드는 승인 전 소비(T1)를 포함해야 하므로,
            # restore_run_context가 채운 노드 최초 진입 기준선을 우선 사용한다(1회 소비).
            if ctx.resume_node_token_base is not None:
                tokens_before = {k: ctx.resume_node_token_base.get(k, 0) for k in _TOKEN_KEYS}
                ctx.resume_node_token_base = None
            else:
                tokens_before = {k: ctx.node_tokens.get(k, 0) for k in _TOKEN_KEYS}
            # gate 시 승인 전 per-node 델타를 체크포인트에 실을 수 있도록 진입 기준선 보관.
            ctx.node_token_base_snapshot = dict(tokens_before)
            # 비용도 토큰과 같은 2셀 패턴 — resume 값은 1회 소비하고, snapshot은
            # gate 시점 진입 기준선을 보존한다 (BL-05).
            if ctx.resume_node_cost_base is not None:
                cost_before = ctx.resume_node_cost_base
                ctx.resume_node_cost_base = None
            else:
                cost_before = ctx.cost_usd
            ctx.node_cost_base_snapshot = cost_before
            log_id = await _log_node_start(
                ctx.run_id, node_id, node["type"], input_data, started_at
            )

            attempt = 0
            output = None
            last_err: Exception | None = None

            while attempt <= max_retries:
                try:
                    output = await executor(ctx, node, state)
                    if ctx.pending_approval is not None:
                        # [phase-19-design.md §2.1, HCI-D4] 서브에이전트 호출 노드
                        # (agent_call/sub_workflow/error_handler)가 _run_subagent를
                        # 거쳐 정상 반환값을 냈더라도, 내부에서 재전파된 승인 대기가
                        # 있으면 이 노드 자체를 승인 대기로 취급해 그래프를 멈춘다.
                        raise ApprovalRequired(node_id, ctx.pending_approval)
                    last_err = None
                    break
                except ApprovalRequired:
                    # [CDX-D2] HITL 승인 대기는 재시도/실패 처리를 거치지 않고
                    # execute_agent까지 그대로 전파한다.
                    raise
                except SubagentApprovalNotSupportedError:
                    # [Angle C 리뷰] agent_call/sub_workflow 노드의 retry_policy가
                    # 이 정책적 차단을 일시적 실패로 오인해 재시도하지 않도록
                    # ApprovalRequired와 동일하게 즉시 전파한다.
                    raise
                except (
                    UntrustedResumeError,
                    DirectNodeRejectedError,
                    DirectNodeRequestChangedError,
                    RunLeaseLostError,
                ):
                    # 위조 `_resume`(#91)과 직접 노드 승인 거부·요청 변경(#153)은 정책적 차단이다 —
                    # 재시도(다시 승인 요청)·continue_on_error(하류 계속 실행)로 덮지 않는다
                    raise
                except NonRetryableNodeError as e:
                    # 승인된 직접 노드 요청의 실패(#153) — 이미 전달됐을 수 있어 재시도하지 않는다.
                    # 출력 상한 잘림(#422)도 같은 상한으로 다시 불러 봐야 또 잘린다.
                    # 실패 처리(continue_on_error 포함)는 일반 실패와 같다
                    last_err = e
                    break
                except Exception as e:
                    last_err = e
                    attempt += 1
                    if attempt <= max_retries and owned:
                        # 실패한 트랜잭션 위에서 재시도하지 않는다(PendingRollbackError) — 노드
                        # 소유 세션만 되돌린다. 공유·강등 세션은 호출자 것이라 건드리지 않는다.
                        # 롤백마저 실패하면(연결 상실) 재시도를 멈추고 원래 오류로 실패 처리한다
                        try:
                            await ctx.db.rollback()
                        except Exception:
                            logger.warning(
                                "재시도 전 분기 세션 롤백 실패 — 재시도를 멈춘다",
                                node_id=node_id,
                                exc_info=True,
                            )
                            break
                    if attempt <= max_retries:
                        logger.warning(
                            "노드 실행 실패, 재시도 중",
                            node_id=node_id,
                            attempt=attempt,
                            max_retries=max_retries,
                            error=str(e),
                        )
                        await asyncio.sleep(delay)

            finished_at = datetime.now(UTC)
            duration_ms = int((finished_at - started_at).total_seconds() * 1000)
            token_delta = {k: ctx.node_tokens.get(k, 0) - tokens_before[k] for k in _TOKEN_KEYS}
            if not token_delta["reasoning_tokens"]:
                del token_delta["reasoning_tokens"]
            token_usage = token_delta if any(token_delta.values()) else None
            cost_delta = ctx.cost_usd - cost_before

            if last_err is not None:
                # 실행 로그·상태·상위 예외 문구에 같은 판정을 쓴다 — 관리자 설정 영역의 원문은
                # 고정 문구로 바꾸고 서버 로그로만 보낸다(#216)
                failure = describe_node_failure(last_err)
                if failure.raw is not None:
                    logger.warning(
                        "노드 실패 원문 — 실행 로그에는 고정 문구를 남긴다",
                        run_id=str(ctx.run_id),
                        node_id=node_id,
                        error_code=failure.code,
                        error=failure.raw,
                    )
                await _log_node_finish(
                    log_id,
                    status="failed",
                    finished_at=finished_at,
                    # 실패에도 남길 값(#359, 예: 시도한 모델) — 예외가 고른 공개 값뿐이다
                    output_data=(
                        _truncate_output(failure.output) if failure.output is not None else None
                    ),
                    error_message=failure.message,
                    error_code=failure.code,
                    token_usage=token_usage,
                    cost_usd=cost_delta,
                    duration_ms=duration_ms,
                )
                if node.get("continue_on_error", False):
                    logger.error(
                        "노드 최종 실패 (continue_on_error 활성화로 실행 지속)",
                        node_id=node_id,
                        error=str(last_err),
                    )
                    state[node_id] = {"failed": True, "error": failure.message}
                else:
                    raise AgentRuntimeError(
                        f"노드 [{node_id}] 실행 실패: {failure.message}"
                    ) from last_err
            else:
                output_data = output if isinstance(output, dict) else {"output": output}
                await _log_node_finish(
                    log_id,
                    status="succeeded",
                    finished_at=finished_at,
                    output_data=_truncate_output(output_data),
                    token_usage=token_usage,
                    cost_usd=cost_delta,
                    duration_ms=duration_ms,
                )
                state[node_id] = output

            done_count["n"] += 1
            # run 단위로 직렬화한다(#28) — 제품 콜백은 run 세션을 커밋하기도 한다
            await ctx.emit_progress(node_id, done_count["n"], total)
            return {}

        return _run

    for node_id in order:
        graph.add_node(node_id, make_node(node_id))

    # 1. 진입점 설정: 들어오는 엣지가 없는 노드 찾기
    incoming_sources = {dst for _, dst in edges}
    entry_nodes = [node_id for node_id in order if node_id not in incoming_sources]
    entry_point = entry_nodes[0] if entry_nodes else order[0]
    graph.set_entry_point(entry_point)

    # 2. 조건 노드 매핑
    cond_nodes = {n["id"]: n for n in nodes if n["type"] == "condition"}

    # 3. 엣지 연결
    # 나가는 엣지 목록을 파악하여 말단 노드를 감지하기 위함
    has_outgoing = set()

    # 조건 노드에서 나가는 엣지는 add_conditional_edges로 별도 처리한다(_plain_edges가 뺀다)
    for src, dst in _plain_edges(node_map, edges):
        graph.add_edge(src, dst)
        has_outgoing.add(src)

    # 조건 노드 분기 엣지 수동 연결
    for cond_id, cond_node in cond_nodes.items():
        true_node = cond_node.get("true_node")
        false_node = cond_node.get("false_node")

        true_target = str(true_node) if true_node in node_map else END
        false_target = str(false_node) if false_node in node_map else END

        def make_routing_fn(c_id: str, t_target: str, f_target: str):
            def route(_graph_state: dict) -> str:
                out = state.get(c_id, {})
                is_true = out.get("result", False)
                return t_target if is_true else f_target

            return route

        graph.add_conditional_edges(
            cond_id,
            make_routing_fn(cond_id, true_target, false_target),
            {true_target: true_target, false_target: false_target},
        )
        has_outgoing.add(cond_id)

    # 4. 말단 노드에서 END로 연결
    for node_id in order:
        if node_id not in has_outgoing:
            graph.add_edge(node_id, END)

    compiled = graph.compile()
    outer_fan_out = ctx.in_fan_out_graph
    ctx.in_fan_out_graph = fan_out  # 이 그래프 기준 — 끝나면 바깥 그래프 값으로 되돌린다
    try:
        await compiled.ainvoke({})
        if fan_out and (
            ctx.resume_node_token_base is not None or ctx.resume_node_cost_base is not None
        ):
            # fan-out 그래프는 `_resume` 노드만 재개 기준선을 받는다 — 힌트가 없었으면 승인 전
            # 소비가 어느 노드 로그에도 실리지 않는다
            logger.warning(
                "재개 기준선을 받을 `_resume` 노드가 없었다(#28)", run_id=str(ctx.run_id)
            )
    except ApprovalRequired as e:
        # [phase-19-design.md §2.1] 그래프 실행 중단 — state 전체(완료 노드 출력
        # 포함)를 checkpoint에 봉인해 재개 시 no-op 치환으로 재실행을 피한다.
        checkpoint = dict(e.checkpoint)
        checkpoint["graph_state"] = dict(state)
        ctx.pending_approval = checkpoint
        return ""
    finally:
        ctx.in_fan_out_graph = outer_fan_out

    if output_path is not None:
        # 자르지 않은 구조화 결과(#361) — loop가 `results[i].output`으로 받는다
        ctx.structured_output = resolve_value_path(state, output_path)

    # 채팅 본문은 명시 참조를 우선하고, 미지정이면 마지막 llm/react_agent를 쓴다.
    output_ref = definition.get("chat_output") if kind == "workflow" else None
    output_node_id = output_ref.get("node_id") if isinstance(output_ref, dict) else None
    output_ids = [output_node_id] if output_node_id else [
        node_id
        for node_id in reversed(order)
        if node_map[node_id].get("type") in {"llm", "react_agent"}
    ]
    ctx.chat_output_text = ""
    ctx.chat_output_truncated = False
    for node_id in output_ids:
        out = state.get(node_id, {})
        # 잘림 표시는 본문 유무와 따로 본다(#422) — 사고 토큰이 상한을 다 쓰면 본문 없이 잘린다.
        # 본문 없이 잘려 건너뛴 뒤 노드도 채팅 본문이 잘림의 영향을 받았다는 표시다
        if truncation_of(out) is not None:
            ctx.chat_output_truncated = True
        if isinstance(out, dict) and out.get("text"):
            ctx.chat_output_text = str(out["text"])
            model = out.get("model")
            if isinstance(model, dict):
                raw_model_id = model.get("id")
                if isinstance(raw_model_id, str):
                    try:
                        ctx.chat_used_model_id = uuid.UUID(raw_model_id)
                    except ValueError:
                        logger.warning(
                            "react output model id is invalid",
                            run_id=str(ctx.run_id),
                            model_id=raw_model_id,
                        )
                display_name = model.get("display_name")
                if isinstance(display_name, str):
                    ctx.chat_used_model_name = display_name
            ctx.chat_fallback_used = bool(out.get("fallback_used", False))
            break
    summary = ctx.chat_output_text[:500]
    # 비-chat 실행의 기존 목록 요약 계약은 모든 text 출력의 역순 fallback을 유지한다.
    if not summary:
        for node_id in reversed(order):
            out = state.get(node_id, {})
            if isinstance(out, dict) and out.get("text"):
                summary = str(out["text"])[:500]
                break
    if not summary and ctx.artifact_keys:
        summary = f"아티팩트 {len(ctx.artifact_keys)}개 생성: " + ", ".join(
            a["name"] for a in ctx.artifact_keys
        )
    return summary
