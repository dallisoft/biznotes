"""HITL 승인/재개 순수 로직 단일 소유 모듈 (R2b).

phase-19-design.md의 HITL 상태머신이 loop.py(게이트 판정·checkpoint 조립)·
worker(RunContext 복원·소비 판정)·tool_registry(도구 분류)에 분산돼 있던 것을,
**순수 로직**만 여기로 모은다(R2b 설계 §1, 인간 선택 "순수 상태머신만 통합").

**경계(중요)**: 이 모듈은 **DB·모델·SQLAlchemy·FastAPI·Celery 무의존**이다.
- 소비 CAS의 *SQL 조립·실행·commit*은 worker(`workers/tasks/agent.py`)에 잔류한다.
  여기서는 "무엇이 소비 가능한가"라는 순수 판정 규칙(`is_consumable`)만 제공한다.
- 도구 분류(`is_side_effect_tool` 등)는 `tool_registry.py`가 소유하고, `evaluate_gate`는
  이를 **소비만** 한다(함수 내 지연 임포트 — tool_registry는 순환 회피용으로 이미
  런타임 심볼을 지연 임포트하는 구조).

`restore_run_context`는 전달받은 ctx만 명시적으로 변이하며 그 외 외부 부수효과는 없다.
runtime.py는 `_react_approval_gate` old-name wrapper를 재수출해 기존 임포트/monkeypatch
계약(R0-6)을 하위 호환한다(CR-R2B-05).
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from biznotes_core.services.agents.context import RunContext
    from biznotes_core.services.agents.react.loop import _ReactGuards


class CheckpointKeys:
    """재개 checkpoint dict의 키 계약 중앙 정의(F5 드리프트 방지, CR-R2B-02).

    **값은 R2b 이전 문자열과 byte-identical**이어야 한다 — 배포 시점에 이미 저장된
    (awaiting_approval) checkpoint를 새 코드가 그대로 읽어야 하기 때문이다. 새 키를
    추가하거나 이름을 바꾸면 in-flight run의 재개가 조용히 깨진다.
    """

    # execute_agent가 캐치 시점에 채우는 그래프 state 전체 스냅샷.
    GRAPH_STATE = "graph_state"
    # react_agent 노드 재개 상태.
    REACT_NODE_ID = "react_node_id"
    REACT_MESSAGES = "react_messages"
    REACT_PENDING_TOOL_CALL = "react_pending_tool_call"
    REACT_REMAINING_CALLS_IN_TURN = "react_remaining_calls_in_turn"
    REACT_ITERS_DONE = "react_iters_done"
    REACT_SUBAGENT_CALLS_IN_NODE = "react_subagent_calls_in_node"
    REACT_GUARDS_AUX_IDX = "react_guards_aux_idx"
    REACT_TOOL_LOG = "react_tool_log"
    # tool 루프가 이어 쓰는 폴백 모델 id(#374) — 폴백 모델이 응답한 뒤 승인 대기에 들어갔을 때만
    # 싣는다. 없으면(옛 체크포인트 포함) 재개한 턴은 1차 모델부터 고른다
    REACT_PREFER_MODEL_ID = "react_prefer_model_id"
    # worker가 재개 시 주입하는 승인 결과("approved"|"rejected").
    REACT_APPROVAL_STATUS = "react_approval_status"
    # 예산 델타 기준선.
    GUARDS_TOKEN_BASE = "guards_token_base"
    GUARDS_COST_BASE = "guards_cost_base"
    # RunContext 가변 상태 스냅샷(CDX-D4) — 재개 시 복원 대상.
    CTX_USAGE = "ctx_usage"
    # [R5-2-01] gate 시점 per-node 토큰 누적(node_tokens) + 노드 최초 진입 기준선.
    # 재개 시 node_tokens=CTX_NODE_TOKENS, tokens_before=CTX_NODE_TOKEN_BASE로 복원해
    # 재개 노드 델타가 승인 전 소비(T1)까지 포함하게 한다.
    CTX_NODE_TOKENS = "ctx_node_tokens"
    CTX_NODE_TOKEN_BASE = "ctx_node_token_base"
    CTX_NODE_COST = "ctx_node_cost"
    CTX_NODE_COST_BASE = "ctx_node_cost_base"
    CTX_COST_TOTAL = "ctx_cost_total"
    CTX_ARTIFACT_KEYS = "ctx_artifact_keys"
    CTX_REACT_SUBAGENT_CALLS = "ctx_react_subagent_calls"
    CTX_REGISTER_SOURCE_CALLS = "ctx_register_source_calls"
    CTX_TRIGGER = "ctx_trigger"
    CTX_TOOL_CALLS = "ctx_tool_calls"
    CTX_TOOL_EVIDENCE = "ctx_tool_evidence"
    # 표준 스킬 런타임(D6-5) — 활성화 맵·고정과 run 단위 카운터. 빠지면 재개 뒤 한도가 초기화되고
    # 활성화한 스킬의 파일을 읽지 못한다.
    CTX_SKILL_ACTIVATIONS = "ctx_skill_activations"
    CTX_SKILL_PINS = "ctx_skill_pins"
    CTX_SKILL_ACTIVATION_COUNT = "ctx_skill_activation_count"
    CTX_SKILL_READ_BYTES = "ctx_skill_read_bytes"
    # 직접 부작용 노드(`http`·`mcp_tool`) 승인(#153). 재개는 노드의 `_resume`이 아니라
    # 앵커로만 판정한다 — `tool`·`plugin`·`mcp` kind는 definition에 nodes가 없어
    # 워커가 `_resume`을 넣지 못한다.
    DIRECT_NODE = "direct_node"
    # 렌더링한 요청 정본의 sha256 — 재개 때 다시 렌더링해 같을 때만 실행한다.
    # 카드·이벤트에는 싣지 않는다.
    DIRECT_REQUEST_HASH = "direct_request_hash"
    # 승인 카드/감사용.
    TOOL_NAME = "tool_name"
    # 승인 대기 도구의 부작용 여부(#154 A). 게이트를 통과한 도구라 항상 True다. 제품은
    # `approval_required`·`PendingApprovalSnapshot`에 그대로 싣는다.
    # 이 키가 없는 옛 체크포인트는 True로 읽는다.
    SIDE_EFFECT = "side_effect"
    ARGUMENTS_SUMMARY = "arguments_summary"


# ── 소비 판정 순수 predicate (SQL 아님 — worker가 이 규칙으로 CAS 조건 구성) ──────
# 소비 대상 approval status 집합. worker의 `_resolve_pending_approval`이
# `.where(AgentRunApproval.status.in_(CONSUMABLE_STATUSES))` 조건을 이 상수로 구성한다.
CONSUMABLE_STATUSES: tuple[str, ...] = ("approved", "rejected")


def is_consumable(status: str, consumed: bool) -> bool:
    """해결(approved/rejected)됐고 아직 미소비인 approval만 재개 소비 대상이다.

    worker의 원자적 CAS(`UPDATE ... WHERE status IN (...) AND consumed=false`)와 동일한
    판정을 순수 함수로 표현 — DB 없이 단위 테스트·문서화가 가능하다. 실제 원자성은
    worker의 단일 SQL 문이 보장한다(이 함수는 규칙의 단일 출처).
    """
    return status in CONSUMABLE_STATUSES and not consumed


def resume_status_of(status: str) -> str:
    """checkpoint에 실을 재개 승인 상태 문자열("approved"|"rejected")을 정규화."""
    return status if status in CONSUMABLE_STATUSES else "rejected"


def build_checkpoint(
    ctx: "RunContext",
    guards: "_ReactGuards",
    tool_name: str,
    arguments: dict[str, Any],
    node: dict[str, Any],
    *,
    messages: list[dict[str, Any]],
    tool_log: list[dict[str, Any]],
    aux_idx: int,
    iters_done: int,
    subagent_calls_in_node: int,
    remaining_calls: list[dict[str, Any]],
    pending_call: dict[str, Any] | None,
) -> dict[str, Any]:
    """승인 대기 재개용 checkpoint dict를 조립(키 계약은 CheckpointKeys가 소유).

    `graph_state`는 여기서 None으로 두고 execute_agent가 캐치 시점에 채운다
    (phase-19-design.md §2.1). 나머지 필드는 react 루프 재개와 RunContext 복원에
    필요한 상태 스냅샷이다(CDX-D4).
    """
    from biznotes_core.services.agents.tool_registry import sanitize_for_approval_card

    call = pending_call or {"id": "", "name": tool_name, "arguments": arguments}
    K = CheckpointKeys
    return {
        K.GRAPH_STATE: None,
        K.REACT_NODE_ID: str(node.get("id", "react_agent")),
        K.REACT_MESSAGES: list(messages),
        K.REACT_PENDING_TOOL_CALL: call,
        K.REACT_REMAINING_CALLS_IN_TURN: remaining_calls,
        K.REACT_ITERS_DONE: iters_done,
        K.REACT_SUBAGENT_CALLS_IN_NODE: subagent_calls_in_node,
        K.REACT_GUARDS_AUX_IDX: aux_idx,
        K.REACT_TOOL_LOG: list(tool_log),
        K.GUARDS_TOKEN_BASE: guards.token_base,
        K.GUARDS_COST_BASE: guards.cost_base,
        **snapshot_run_context(ctx),
        K.TOOL_NAME: tool_name,
        K.ARGUMENTS_SUMMARY: sanitize_for_approval_card(tool_name, arguments),
        K.SIDE_EFFECT: True,
    }


def snapshot_run_context(ctx: "RunContext") -> dict[str, Any]:
    """재개 때 `restore_run_context`가 되살릴 RunContext 가변 상태(#153 리뷰 1).

    react·직접 노드 체크포인트가 같은 키 계약으로 싣는다. 빠지면 `restore_run_context`가 기본값으로
    덮어 비용 예산·아티팩트·run 단위 상한이 초기화된다.
    """
    K = CheckpointKeys
    return {
        K.CTX_USAGE: dict(ctx.usage),
        # [R5-2-01] gate 시점 per-node 누적(진입 기준선 + 승인 전 소비) + 진입 기준선.
        # 재개 시 node_tokens는 gate 값으로, tokens_before는 진입 기준선으로 복원해
        # 최종 노드 델타가 승인 전+후 소비를 모두 포함하되 자식 행과는 분리된다.
        K.CTX_NODE_TOKENS: dict(ctx.node_tokens),
        K.CTX_NODE_TOKEN_BASE: dict(ctx.node_token_base_snapshot),
        K.CTX_NODE_COST: ctx.cost_usd,
        K.CTX_NODE_COST_BASE: ctx.node_cost_base_snapshot,
        K.CTX_COST_TOTAL: ctx.cost_total[0],
        K.CTX_ARTIFACT_KEYS: list(ctx.artifact_keys),
        K.CTX_REACT_SUBAGENT_CALLS: ctx.react_subagent_calls[0],
        # [R22B-03] register_source 카운터도 실어야 재개 시 run당 상한이 유지된다.
        K.CTX_REGISTER_SOURCE_CALLS: ctx.register_source_calls[0],
        # 구버전/순수 테스트용 최소 컨텍스트도 checkpoint를 만들 수 있어야 한다.
        # 실제 RunContext에는 세 필드가 항상 존재하며, 누락 시 수동 run의 기존값을 쓴다.
        K.CTX_TRIGGER: getattr(ctx, "trigger", "manual"),
        K.CTX_TOOL_CALLS: list(getattr(ctx, "tool_calls", [])),
        K.CTX_TOOL_EVIDENCE: list(getattr(ctx, "tool_evidence", [])),
        K.CTX_SKILL_ACTIVATIONS: dict(getattr(ctx, "skill_activations", {})),
        K.CTX_SKILL_PINS: dict(getattr(ctx, "skill_pins", {})),
        K.CTX_SKILL_ACTIVATION_COUNT: getattr(ctx, "skill_activation_count", [0])[0],
        K.CTX_SKILL_READ_BYTES: getattr(ctx, "skill_read_bytes", [0])[0],
    }


def build_direct_node_checkpoint(
    ctx: "RunContext",
    node_id: str,
    *,
    tool_name: str,
    arguments_summary: dict[str, Any],
    request_hash: str,
) -> dict[str, Any]:
    """직접 부작용 노드의 승인 대기 checkpoint(#153 A-3).

    제품 워커가 읽는 키(`react_node_id`·`tool_name`·`arguments_summary`·`graph_state`)를
    react와 같게 둬 워커 변경 없이 저장·재개된다. `graph_state`는 execute_agent가 채운다.
    """
    K = CheckpointKeys
    return {
        K.GRAPH_STATE: None,
        K.REACT_NODE_ID: node_id,
        **snapshot_run_context(ctx),
        K.DIRECT_NODE: True,
        K.DIRECT_REQUEST_HASH: request_hash,
        K.TOOL_NAME: tool_name,
        K.ARGUMENTS_SUMMARY: arguments_summary,
        K.SIDE_EFFECT: True,  # 게이트를 통과한 직접 노드도 부작용이다(#154)
    }


def evaluate_gate(
    ctx: "RunContext",
    guards: "_ReactGuards | None",
    tool_name: str,
    arguments: dict[str, Any],
    node: dict[str, Any],
    *,
    messages: list[dict[str, Any]],
    tool_log: list[dict[str, Any]],
    aux_idx: int,
    iters_done: int,
    subagent_calls_in_node: int,
    remaining_calls: list[dict[str, Any]],
    pending_call: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """HITL 승인 게이트 판정(phase-19-design.md §2.1·§3). 승인이 필요하면 재개용
    checkpoint dict를 반환하고, 그렇지 않으면(부작용 없음/자동 승인) None을 반환한다.

    판정 순서(보안 이중 방어 불변, R2b §4): ① guards 없으면(llm 노드 경로) 게이트 미적용
    — 그 경로는 부작용 도구를 노출 전에 거부한다(#70, `_exec_llm_with_tools`)
    → ② 부작용 도구 아님 → 통과 → ③ always-gated 도구(register_source 등)면 정책 매칭과
    무관하게 항상 게이트 → ④ 아니면 auto_approve_tools 정책 매칭 시 자동 승인.
    """
    if guards is None:
        return None

    from biznotes_core.services.agents.tool_registry import (
        auto_approve_match,
        is_always_gated_tool,
        is_side_effect_tool,
    )

    if not is_side_effect_tool(tool_name):
        return None

    # [phase-22-design.md §2.3, D-14] register_source 등 상시 게이트 대상 도구는
    # auto_approve_tools 정책 매칭 결과와 무관하게 항상 승인을 요구한다 — 저장 시
    # 검증(admin/agents.py·workflows.py)이 이 정책 자체를 거부하지만, 우회 경로
    # (예: 검증 이전 데이터, 수동 DB 조작) 대비 런타임 이중 방어.
    if getattr(ctx, "trigger", "manual") != "chat" and not is_always_gated_tool(tool_name):
        policy = node.get("auto_approve_tools")
        if isinstance(policy, dict) and auto_approve_match(tool_name, arguments, policy):
            return None

    return build_checkpoint(
        ctx,
        guards,
        tool_name,
        arguments,
        node,
        messages=messages,
        tool_log=tool_log,
        aux_idx=aux_idx,
        iters_done=iters_done,
        subagent_calls_in_node=subagent_calls_in_node,
        remaining_calls=remaining_calls,
        pending_call=pending_call,
    )


# 재개 진입(`_exec_react_agent` → `_exec_llm_with_tools(resume_from=)`)이 checkpoint에서 반드시
# 읽는 키. 하나라도 없으면 재개를 소비하기 전에 거부한다(#91).
_REQUIRED_RESUME_KEYS: tuple[str, ...] = (
    CheckpointKeys.REACT_MESSAGES,
    CheckpointKeys.REACT_PENDING_TOOL_CALL,
    CheckpointKeys.REACT_APPROVAL_STATUS,
)


# 직접 노드 재개가 앵커에서 반드시 읽는 키(#153 A-2). react의 `_REQUIRED_RESUME_KEYS`와 별개다.
_REQUIRED_DIRECT_RESUME_KEYS: tuple[str, ...] = (
    CheckpointKeys.DIRECT_NODE,
    CheckpointKeys.DIRECT_REQUEST_HASH,
    CheckpointKeys.REACT_APPROVAL_STATUS,
)


def claim_direct_resume(ctx: "RunContext", node_id: str) -> dict[str, Any] | None:
    """직접 노드가 워커가 소비한 승인의 재개 대상이면 앵커를 한 번 돌려준다(#153 A-2).

    노드의 `_resume` 키는 보지 않는다. 앵커(`ctx.resume_checkpoint`)가 이 노드를 가리키고
    아직 소비되지 않았을 때만 소비 표시 후 앵커를 돌려준다. 앵커가 이 노드를 가리키는데 직접
    노드 필수 키가 없으면(react 체크포인트·위조) `UntrustedResumeError`다. 그 밖에는
    None(새 게이트)이다.
    """
    from biznotes_core.services.agents.errors import UntrustedResumeError

    if not _is_resume_target(ctx, node_id):
        return None
    anchor = ctx.resume_checkpoint
    assert anchor is not None
    missing = [k for k in _REQUIRED_DIRECT_RESUME_KEYS if k not in anchor]
    if missing or anchor.get(CheckpointKeys.DIRECT_NODE) is not True:
        raise UntrustedResumeError(
            f"노드 [{node_id}]의 재개 checkpoint가 직접 노드 승인 형식이 아닙니다: {missing}"
        )
    if ctx.resume_consumed[0]:
        return None
    ctx.resume_consumed[0] = True
    return anchor


def _is_resume_target(ctx: "RunContext", node_id: Any) -> bool:
    """이 노드가 워커가 소비한 승인 checkpoint(`ctx.resume_checkpoint`)의 재개 대상인가(#91)."""
    anchor = ctx.resume_checkpoint
    if anchor is None:
        return False
    target = anchor.get(CheckpointKeys.REACT_NODE_ID)
    return isinstance(target, str) and target == node_id


def restore_run_context(ctx: "RunContext", checkpoint: dict[str, Any]) -> None:
    """승인 대기 이전 누적된 RunContext 가변 상태를 checkpoint로 복원(CDX-D4).

    **신뢰를 부여하는 호출이다(#91)**: `ctx.resume_checkpoint`를 이 checkpoint로 정하고 소비 셀을
    새로 단다. 워커가 승인을 CAS로 **소비한** checkpoint에만 불러야 한다. react 노드의 `_resume`은
    이 앵커가 가리키는 노드에서만, 한 번만 재개로 인정된다.

    복원 대상: 토큰/비용/아티팩트/서브에이전트 카운터/register_source 카운터/스킬 활성화·카운터.
    그렇지 않으면 재개 후 예산 우회·사용량 누락·아티팩트 소실·상한 무력화가 발생한다.

    **하위 호환(CR-R2B-02)**: R2b 이전 checkpoint는 동일 키를 이미 갖고 있으므로 그대로
    읽힌다. 누락 가능 키는 R2b 이전 인라인 복원과 동일하게 기본값(빈 dict·0.0·빈 list·0)을
    적용한다. 전달받은 ctx만 명시적으로 변이한다(외부 I/O 없음, CR-R2B3-07).

    per-node 비용 키가 없는 구형 checkpoint는 `CTX_COST_TOTAL`을 `cost_usd`에
    복원하고 기준선은 None으로 둔다. 이 fallback의 관측되는 한계(HCI-P2-9):
    (1) 자식 비용이 포함된 run 총계가 부모 per-node 셀에 복원되어 두 값이 섞이고,
    (2) 재개 노드의 진입 기준선이 없어 `make_node`가 복원된 값을 기준으로 삼으므로
    **승인 전 부모 비용이 그 노드 로그에서 누락**된다(BL-05 이전 동작). 델타 계산에서
    (1)이 상쇄되므로 부모 로그가 과대 계상되지는 않는다. 배포 시점 대기 run의 기존
    동작을 보존하려는 의도적 선택이다.
    """
    K = CheckpointKeys
    # 새 셀을 단다 — 제자리 초기화는 분기 사본과 공유한 셀까지 되돌린다.
    ctx.resume_checkpoint = checkpoint
    ctx.resume_consumed = [False]
    ctx.usage.update(checkpoint.get(K.CTX_USAGE, {}))
    # [R5-2-01] 재개 노드 델타가 승인 전 소비(T1)까지 포함하도록:
    #  - node_tokens = gate 시점 per-node 누적(CTX_NODE_TOKENS)
    #  - resume_node_token_base = 노드 최초 진입 기준선(CTX_NODE_TOKEN_BASE)
    # make_node가 tokens_before를 base로 되돌리고, 최종 델타 = gate누적+재개소비 - base
    # = T1 + T2. 자식 행과는 node_tokens 미공유로 분리(이중계상 없음).
    # 하위 호환(R5-2 이전 checkpoint): 두 키가 없으면 CTX_USAGE로 보수 폴백(과거 동작).
    ctx.node_tokens.update(checkpoint.get(K.CTX_NODE_TOKENS, checkpoint.get(K.CTX_USAGE, {})))
    node_base = checkpoint.get(K.CTX_NODE_TOKEN_BASE)
    ctx.resume_node_token_base = dict(node_base) if node_base is not None else None
    ctx.cost_total[0] = checkpoint.get(K.CTX_COST_TOTAL, 0.0)
    ctx.cost_usd = checkpoint.get(K.CTX_NODE_COST, checkpoint.get(K.CTX_COST_TOTAL, 0.0))
    cost_base = checkpoint.get(K.CTX_NODE_COST_BASE)
    ctx.resume_node_cost_base = float(cost_base) if cost_base is not None else None
    ctx.artifact_keys = list(checkpoint.get(K.CTX_ARTIFACT_KEYS, []))
    ctx.react_subagent_calls[0] = checkpoint.get(K.CTX_REACT_SUBAGENT_CALLS, 0)
    ctx.register_source_calls[0] = checkpoint.get(K.CTX_REGISTER_SOURCE_CALLS, 0)
    # trigger의 정본은 워커가 DB AgentRun에서 새로 구성한 ctx다.
    # 구버전·변조 checkpoint가 chat을 manual로 되돌려 자동 승인을
    # 재활성화할 수 없도록 감사용 CTX_TRIGGER를 복원에는 쓰지 않는다.
    ctx.tool_calls = list(checkpoint.get(K.CTX_TOOL_CALLS, getattr(ctx, "tool_calls", [])))
    ctx.tool_evidence = list(
        checkpoint.get(K.CTX_TOOL_EVIDENCE, getattr(ctx, "tool_evidence", []))
    )
    # 표준 스킬 런타임(D6-5). 구버전 checkpoint에는 키가 없다 — 그때는 빈 값(활성화 없음)이다.
    ctx.skill_activations = dict(checkpoint.get(K.CTX_SKILL_ACTIVATIONS, {}))
    ctx.skill_pins = dict(checkpoint.get(K.CTX_SKILL_PINS, {}))
    _restore_cell(ctx, "skill_activation_count", checkpoint.get(K.CTX_SKILL_ACTIVATION_COUNT, 0))
    _restore_cell(ctx, "skill_read_bytes", checkpoint.get(K.CTX_SKILL_READ_BYTES, 0))


def _restore_cell(ctx: "RunContext", name: str, value: Any) -> None:
    """공유 카운터 셀을 제자리에서 복원한다. 셀이 없는 최소 컨텍스트(순수 테스트)는 새로 단다."""
    cell = getattr(ctx, name, None)
    if cell is None:
        setattr(ctx, name, [int(value)])
    else:
        cell[0] = int(value)
