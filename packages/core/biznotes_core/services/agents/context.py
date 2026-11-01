"""에이전트 실행 컨텍스트 — 런타임 전반이 공유하는 실행 상태 (R1-2).

`RunContext`는 원래 `runtime.py`에 정의돼 있었으나, `tool_registry.py`가 이 타입을
쓰면서 `runtime.py`와 양방향 순환 import를 만들었다(디스패처 시그니처가 RunContext에
의존). 컨텍스트를 코어 내부 의존이 없는(=stdlib·SQLAlchemy·structlog만 쓰는) 이 모듈로
분리해 순환의 한 축을 끊는다. `runtime.py`는 하위 호환을 위해 이 심볼을 재수출한다.
"""

import asyncio
import contextvars
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

if TYPE_CHECKING:
    # 런타임에는 import하지 않는다 — schemas는 models를 끌어온다(모듈 docstring의 의존 규칙, #23 D6)
    from biznotes_core.schemas.run_events import ToolCallEventInput

logger = structlog.get_logger()

# 동시 분기 예산(#28) — run 하나가 동시에 열 수 있는 분기 세션 수. 분기 세션은 LLM 호출 동안
# 커넥션을 붙잡으므로 제품 워커 풀(runner 엔진 5 + overflow 5) 안에 run 세션·콜백 세션 여유를
# 남긴다.
BRANCH_BUDGET = 4

# 지금 실행 중인 콜백의 run 상태 — 같은 run의 콜백이 콜백을 다시 부르면 run 단위 콜백 잠금에서
# 교착하므로 즉시 실패시킨다(#28 D7). 다른 run의 콜백이나 콜백이 끝난 뒤의 호출은 막지 않는다.
_IN_RUN_CALLBACK: contextvars.ContextVar["RunBranchState | None"] = contextvars.ContextVar(
    "agents_in_run_callback", default=None
)


@dataclass
class RunBranchState:
    """run 단위 공유 상태(#28) — 동시 분기 예산과 콜백 직렬화 잠금.

    루트 컨텍스트가 만들고 `child()`·`replace()`가 **같은 객체**를 넘긴다. 서브에이전트가 새로
    만들면 예산이 서브에이전트마다 따로 생겨 상한이 무력해진다.
    """

    budget: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(BRANCH_BUDGET))
    callback_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    degraded_warned: bool = False
    # 두 도구 이벤트 콜백이 함께 설정됐다는 경고를 run당 한 번만 남긴다(#23 D4)
    tool_event_dual_warned: bool = False
    # 취소 중 백그라운드로 넘어간 분기 세션 닫기 — 강한 참조를 두어 GC로 사라지지 않게 한다
    pending_closes: set["asyncio.Task[None]"] = field(default_factory=set)


@dataclass
class RunContext:
    run_id: uuid.UUID
    notebook_id: uuid.UUID
    db: AsyncSession
    params: dict[str, Any]
    # 실행 진입점. chat은 정의의 auto_approve_tools를 무시하는 보안 경계다.
    trigger: str = "manual"
    # 검증된 chat prompt와 직전 이력은 사용자 params와 다른 예약 namespace다.
    chat_message: str = ""
    chat_history: list[dict[str, Any]] = field(default_factory=list)
    # autonomous chat이 요청한 모델과 실제 응답 모델 메타데이터.
    # requested id는 노드 model_id가 없을 때 런타임 폴백으로 쓰고,
    # used 필드는 영속 메시지와 레거시 done 프레임의 정본이다.
    chat_model_id: uuid.UUID | None = None
    chat_used_model_id: uuid.UUID | None = None
    chat_used_model_name: str | None = None
    chat_fallback_used: bool = False
    # 워크플로우의 대화 본문 정본. result_summary(목록용 500자)와 분리한다.
    chat_output_text: str = ""
    # 실행 사용자 — 사용량 한도 가드용 (None = 시스템 실행, 한도 미적용)
    user_id: uuid.UUID | None = None
    user_email: str | None = None
    user_name: str | None = None
    user_role: str = "user"
    # 입력 소스 스코프 — None이면 노트 전체(scope:all), 목록이면 그 소스만(scope:selected),
    # []이면 선택한 소스 없음(검색·로드하지 않음, #8·#13).
    # rag_search 필터·source_load 대상 양쪽에서 사용.
    source_ids: list[uuid.UUID] | None = None
    # run 전체 누적 토큰 셀 — child가 참조 공유(서브에이전트 토큰 포함). react 예산
    # 가드(loop.py)와 run-level record_usage(chat.py)가 이 값을 읽는다.
    usage: dict[str, int] = field(default_factory=lambda: {"total_tokens": 0})
    # per-node 토큰 델타용 셀 — child와 **미공유**(cost_usd와 대칭, R5-2).
    # make_node가 노드 전후 델타를 ExecutionLog.token_usage에 영속화한다. usage(공유)
    # 로 델타를 재면 서브에이전트 토큰이 부모 노드 행 + 자식 행에 이중 계상되므로
    # (monitoring.py SUM), 영속화 소스는 이 미공유 셀을 쓴다(codex C3와 동형).
    node_tokens: dict[str, int] = field(default_factory=lambda: {"total_tokens": 0})
    # 누적 비용(USD) — _exec_llm이 노드 응답 모델의 cost_per_1k로 가산.
    # make_node가 노드 전후 델타를 ExecutionLog.cost_usd에 영속화한다(per-node, 미공유).
    cost_usd: float = 0.0
    # run 전체 누적 비용 셀 — child가 참조 공유한다. per-node 델타(cost_usd)와 분리해
    # react 예산 가드가 서브에이전트 비용까지 보도록 하면서 ExecutionLog 합산의
    # 이중계상은 피한다(codex C3). _react_budget_exceeded만 이 값을 읽는다.
    cost_total: list[float] = field(default_factory=lambda: [0.0])
    artifact_keys: list[dict[str, str]] = field(default_factory=list)
    # 진행률 콜백 (node_id, done, total)
    on_progress: Callable[[str, int, int], Awaitable[None]] | None = None
    # 실시간 도구 타임라인 콜백(tool_call_id, name, status, sanitized arguments).
    on_tool_call: (
        Callable[
            [
                str,
                str,
                Literal["awaiting_approval", "succeeded", "failed", "blocked"],
                dict[str, Any],
            ],
            Awaitable[None],
        ]
        | None
    ) = None
    # 도구 이벤트 콜백(#23) — 완성된 이벤트 모델을 받는다. 설정되면 on_tool_call 대신 이것을 부른다.
    # 필드가 늘어도 콜백 시그니처가 바뀌지 않는다(required_initial 등).
    on_tool_call_event: "Callable[[ToolCallEventInput], Awaitable[None]] | None" = None
    # 서브에이전트 가드 — 현재 실행 중인 에이전트 id, 조상 체인, 중첩 깊이.
    agent_id: uuid.UUID | None = None
    call_stack: tuple[str, ...] = ()
    depth: int = 0
    # 이미지 프롬프트 출처(#338 D1, `prompt_provenance`) — params가 안전한 출처인가(최상위 run은 참,
    # 하위 실행은 부모가 판정해 넘긴다)와, execute_agent가 그래프마다 정하는 안전한 뿌리 집합
    params_image_safe: bool = True
    image_safe_roots: frozenset[str] = frozenset()
    # 병렬 워커 분기 표시 — supervisor/loop(parallel) 워커로 진입하면 True.
    # react_agent는 부모 단일 AsyncSession을 형제와 동시에 공유하면 asyncpg 레이스가
    # 나므로(T5/F1), 이 플래그가 True인 컨텍스트에서는 실행을 거부한다.
    in_parallel_branch: bool = False
    # 지금 실행 중인 그래프가 fan-out(형제 동시 실행)인가(#153 A-4). execute_agent가
    # 그래프마다 정한다 — 분기 사본(`replace`)은 물려받고, `child()`(다른 그래프)는 자기
    # 그래프 기준으로 다시 정한다.
    in_fan_out_graph: bool = False
    # run lease가 유효한가(#153 B, `agent_run_lease.LeaseHandle.valid`) — 직접 부작용 노드가
    # 외부 호출 직전에 확인한다. None이면 lease를 쓰지 않는 실행(구 워커·테스트)이다.
    # 서브에이전트로 전파한다.
    lease_valid: Callable[[], bool] | None = None
    # 전역 서브에이전트(call_agent) 호출 카운터 — 모든 child가 공유하는 가변 셀.
    # 단일 react 노드 누적 상한(max_subagent_calls)과 별개로, 중첩 react→call_agent
    # 재귀 전반의 가로 fan-out 폭주를 막는 전역 방어선(T3).
    react_subagent_calls: list[int] = field(default_factory=lambda: [0])
    # run 단위 register_source 호출 카운터(phase-22-design.md §2.3, D-15) — child와
    # 공유되는 가변 셀. 상한 초과 시 도구 호출만 거부하고 run은 계속 진행한다.
    register_source_calls: list[int] = field(default_factory=lambda: [0])
    # 채팅/자율 실행 관측 — 현재 SSE 응답에 실제 호출 도구와 근거를 노출한다.
    # child 컨텍스트도 같은 셀을 공유해 skill 내부의 rag/source 근거를 부모가 수집한다.
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_evidence: list[dict[str, Any]] = field(default_factory=list)
    # 허용했지만 이 실행에서 막힌 MCP 도구 이름 → 사유(#139). react·llm 노드가 채운다. child와
    # 공유해 서브에이전트의 차단도 최상위에서 보인다. 제품이 채팅 응답 등에 표시할 때 쓴다 —
    # 도구 타임라인(`tool_calls`)에는 넣지 않는다(일반 사용자 채팅마다 가짜 호출이 생긴다).
    unavailable_tools: dict[str, str] = field(default_factory=dict)
    # 표준 스킬 런타임(표준 계획 D6-5) — run 단위 공유 카운터 셀. child·분기와 공유하고
    # 승인 재개 때 checkpoint로 복원한다. 활성화 횟수와 스킬 파일로 읽은 누적 바이트.
    skill_activation_count: list[int] = field(default_factory=lambda: [0])
    skill_read_bytes: list[int] = field(default_factory=lambda: [0])
    # 이 컨텍스트가 활성화한 스킬 name → version id(str). 컨텍스트마다 따로 둔다 — 자식은
    # 부모의 활성화를 물려받지 않고, 분기는 사본을 쓴다(P2 skill_scope 비상속 결정 승계).
    skill_activations: dict[str, str] = field(default_factory=dict)
    # 직접 실행이 고정한 스킬 name → version id(str). run staging이 D8 규칙으로 정한 version을
    # 카탈로그를 거치지 않고 활성화한다(S-HCI-5: 카탈로그에 없는 org 스킬도 직접 실행은 된다).
    skill_pins: dict[str, str] = field(default_factory=dict)
    # HITL 승인 대기 checkpoint — execute_agent가 ApprovalRequired를 캐치하면 이 필드에
    # 채워 넣고 빈 문자열을 반환한다(execute_agent의 -> str 시그니처는 무변경, CDX-D3).
    # 서브에이전트 호출 경로(_run_subagent)는 자식 ctx의 이 필드를 부모로 명시 재전파해야
    # 중첩된 react_agent의 승인 요청이 최상위 worker까지 도달한다(phase-19-design.md §2.1).
    pending_approval: dict[str, Any] | None = None
    # [R5-2/R5-2-01] HITL 재개 시 gated 노드의 per-node 토큰 델타 기준선. 승인 전
    # 소비한 토큰(T1)이 재개 노드의 최종 ExecutionLog 델타에 포함되도록, make_node가
    # 재캡처하는 tokens_before를 (현 node_tokens가 아니라) **노드 최초 진입 기준선**으로
    # 되돌린다. restore_run_context가 채우고 make_node가 1회 소비 후 None으로 비운다.
    resume_node_token_base: dict[str, int] | None = None
    # make_node가 현재 노드 진입 시 캡처한 per-node 토큰 기준선 — build_checkpoint가
    # gate 시점 승인 전 델타(node_tokens - 이 값)를 체크포인트에 싣는 데 쓴다.
    node_token_base_snapshot: dict[str, int] = field(default_factory=dict)
    # HITL 재개 비용은 토큰과 같은 2셀 패턴을 쓴다. resume 값은 make_node가 1회
    # 소비하고, snapshot은 gate 시점에 노드 최초 진입 비용 기준선을 보존한다.
    resume_node_cost_base: float | None = None
    node_cost_base_snapshot: float = 0.0
    # 동시 분기 세션(#28) — 동시에 실행되는 단위(병렬 supervisor 워커·loop 항목)는 각자 세션을 쓴다.
    # session_factory가 없으면 부모 세션(`db`)의 엔진으로 새 세션을 연다. 연결 상태(`SET`·RLS 등)에
    # 기대는 제품은 그 상태를 거는 팩토리를 넘겨야 한다.
    session_factory: Callable[[], AsyncSession] | None = None
    branch_state: RunBranchState = field(default_factory=RunBranchState)
    # 이 컨텍스트가 분기 permit을 쥔 실행 단위 안인지 — 쥔 단위는 permit을 기다리지 않는다(D6)
    holds_branch_permit: bool = False
    # HITL 재개의 신뢰 앵커(#91) — 워커가 소비한 승인의 checkpoint. `restore_run_context`와
    # `_execute_standard`가 채운다. react 노드의 `_resume`은 이 값이 있고 노드 id가 checkpoint의
    # `react_node_id`와 같을 때만 재개로 인정하며, 재개 값도 노드가 아니라 이 값에서 읽는다(저장
    # 정의가 위조한 `_resume`은 실행되지 않는다). `child()`는 넘기지 않는다 — 서브에이전트는 재개
    # 대상이 될 수 없다.
    resume_checkpoint: dict[str, Any] | None = None
    # 앵커를 이미 썼는지 — 승인 1건은 1회만 실행한다(같은 run에서 재개 노드가 다시 실행돼도 승인된
    # 호출을 반복하지 않는다). `replace` 사본(fan-out 노드·분기)이 같은 셀을 공유하도록 리스트다.
    # `child()`는 새 셀을 만든다.
    resume_consumed: list[bool] = field(default_factory=lambda: [False])
    # 새 필드는 여기(맨 끝)에 키워드 전용으로 더한다 — 위치 인자로 만든 기존 호출의 순서를 지킨다
    # run 생성 때 고른 모델(#359, `AgentRun.model_id`) — 노드 model_id가 없는 llm·react 노드의
    # 1차 모델이다. 없으면 `chat_model_id`, 그다음 기본 모델이다. `child()`가 넘긴다
    run_model_id: uuid.UUID | None = field(default=None, kw_only=True)
    # 정의의 `output`(#361)이 가리킨 값 — execute_agent가 끝날 때 채운다. loop가 본문의
    # 구조화 결과를 자르지 않고 받는다. `child()`는 넘기지 않는다(실행마다 자기 값이다)
    structured_output: Any = field(default=None, kw_only=True)
    # params 전체가 이미지 프롬프트에 안전하지 않을 때도 안전한 최상위 params 키(#390, 키별 출처).
    # loop 공통 `params`처럼 키마다 출처가 다른 하위 실행에 부모가 넘긴다. `params_image_safe`가
    # 참이면 쓰지 않는다
    params_image_safe_keys: frozenset[str] = field(default=frozenset(), kw_only=True)
    # 채팅 본문을 만든 노드의 출력이 출력 상한에서 잘렸다(#422, 그 노드 결과의 `output_truncated`).
    # 본문 없이 잘린 출력 노드(그래서 본문이 비었거나 앞 노드 본문으로 넘어간 경우)도 포함한다.
    # `chat_fallback_used`처럼 execute_agent가 채운다. 상한·토큰 수는 그 노드의 실행 로그에 있다
    chat_output_truncated: bool = field(default=False, kw_only=True)

    async def emit_progress(self, node_id: str, done: int, total: int) -> None:
        """진행률 콜백 — run 단위로 직렬화한다(#28 D7)."""
        if self.on_progress is not None:
            await self._run_callback(self.on_progress, node_id, done, total)

    async def emit_tool_call(
        self,
        tool_call_id: str,
        name: str,
        status: Literal["awaiting_approval", "succeeded", "failed", "blocked"],
        arguments: dict[str, Any],
        *,
        required_initial: bool = False,
        side_effect: bool | None = None,
    ) -> None:
        """도구 타임라인 콜백 — run 단위로 직렬화한다(#28 D7).

        `on_tool_call_event`가 있으면 이벤트 모델로 그것만 부르고, 없으면 기존 4인자
        `on_tool_call`을 부른다(#23 D4). `required_initial`은 True일 때만 싣는다 — 발행이
        `exclude_none`이라 모델이 부른 호출의 이벤트 JSON은 예전과 같다(D3).
        `side_effect`는 호출처가 계산한 값을 싣는다(#154 A). None이면 승인 게이트와 같은 판정
        (`is_side_effect_tool`)으로 채운다. 구 4인자 `on_tool_call`에는 전달되지 않는다.
        """
        if self.on_tool_call_event is not None:
            if self.on_tool_call is not None and not self.branch_state.tool_event_dual_warned:
                self.branch_state.tool_event_dual_warned = True
                logger.warning(
                    "agent.tool_call.dual_callbacks",
                    run_id=str(self.run_id),
                    detail="on_tool_call과 on_tool_call_event가 함께 설정됨 — 새 콜백만 부른다",
                )
            from biznotes_core.schemas.run_events import ToolCallEventInput
            from biznotes_core.services.agents.tool_registry import is_side_effect_tool

            event = ToolCallEventInput(
                type="tool_call",
                tool_call_id=tool_call_id,
                name=name,
                status=status,
                arguments=arguments,
                required_initial=True if required_initial else None,
                side_effect=is_side_effect_tool(name) if side_effect is None else side_effect,
            )
            await self._run_callback(self.on_tool_call_event, event)
        elif self.on_tool_call is not None:
            await self._run_callback(self.on_tool_call, tool_call_id, name, status, arguments)

    async def _run_callback(self, fn: Callable[..., Awaitable[None]], *args: Any) -> None:
        """제품 콜백은 run 세션을 커밋하기도 한다 — 동시 분기의 콜백이 겹치지 않게 한 번에 하나만.

        같은 run의 콜백 안에서 다시 콜백을 부르면 이 잠금에서 영원히 기다리므로 즉시 실패시킨다.
        한계: 콜백이 시작되기 **전에** 만든 태스크가 콜백을 부르고 콜백이 그 태스크를 기다리면
        감지하지 못한다(컨텍스트가 복사되기 전이다) — 제품 콜백은 그런 태스크를 기다리지 않는다.
        """
        state = self.branch_state
        if _IN_RUN_CALLBACK.get() is state and state.callback_lock.locked():
            raise RuntimeError("실행 콜백 안에서 다른 실행 콜백을 부를 수 없다(run 단위 콜백 잠금)")
        async with state.callback_lock:
            token = _IN_RUN_CALLBACK.set(state)
            try:
                await fn(*args)
            finally:
                _IN_RUN_CALLBACK.reset(token)

    def _branch_session_opener(self) -> Callable[[], AsyncSession] | None:
        """분기 세션을 여는 방법. None이면 격리할 수 없다 — 호출자는 순차로 돈다(D1)."""
        if self.session_factory is not None:
            return self.session_factory
        if self.db is None:  # 단위 테스트
            return None
        bind = getattr(self.db, "bind", None)
        if isinstance(bind, AsyncEngine):
            engine = bind
            return lambda: AsyncSession(bind=engine, expire_on_commit=False)
        if isinstance(bind, AsyncConnection):
            # 연결에 묶인 세션(트랜잭션 테스트 픽스처 등) — 다른 연결은 그 데이터를 못 본다
            return None
        raise RuntimeError(
            "병렬 분기 세션을 열 수 없다 — 엔진에 묶이지 않은 세션이면 "
            "RunContext.session_factory를 넘겨야 한다"
        )

    def _degraded(self, mark_parallel: bool) -> "RunContext":
        return replace(
            self,
            in_parallel_branch=self.in_parallel_branch or mark_parallel,
            node_tokens=dict(self.node_tokens),
            resume_node_token_base=None,
            resume_node_cost_base=None,
            skill_activations=dict(self.skill_activations),
            skill_pins=dict(self.skill_pins),
        )

    @asynccontextmanager
    async def isolated_branch(
        self, *, mark_parallel: bool = True
    ) -> AsyncIterator[tuple["RunContext", bool]]:
        """동시 실행 단위 하나의 컨텍스트 — (컨텍스트, 독립 세션인지)를 준다(#28).

        permit을 얻으면 부모와 **다른 세션**을 연 분기를 주고, 나갈 때 닫는다(암묵 커밋 없음 —
        쓰기는 각자 UoW로 커밋한다). 규칙은 하나: permit을 쥔 실행 단위는 permit을 기다리지 않는다.
        permit이 없는 최상위는 기다리고, 분기 안에서는 비차단으로만 얻는다. 얻지 못하거나
        격리할 수 없으면 호출자 세션을 그대로 쓰는 컨텍스트와 False를 준다 — 호출자가 순차로 돌린다.

        permit은 세션을 닫은 **뒤** 안쪽 `finally`에서 돌려준다. LangGraph는 형제를 두 번
        취소하므로 닫기를 기다리는 도중 취소가 와도 반환이 실행돼야 한다 — 같은 블록의 await
        뒤에 두면 건너뛴다. 닫기 전에 돌려주면 다음 분기가 세션을 먼저 열어 예산을 순간 넘는다.
        닫기 실패는 로그로만 남긴다 — 본문의 원래 예외(예: `SubagentApprovalNotSupportedError`)를
        덮으면 호출자의 예외 처리가 어긋난다.

        대기에는 상한을 두지 않는다. 기다리는 쪽은 permit을 쥐지 않으므로 순환 대기가 없고,
        permit은 어떤 종료 경로에서도 돌아온다. 상한을 두면 넓은 parallel loop의 꼬리 항목이 정상
        대기 중에 실패한다(대기 시간 = 앞선 항목 수 × 항목 시간 / 예산).
        """
        opener = self._branch_session_opener()
        if opener is None:
            if not self.branch_state.degraded_warned:
                self.branch_state.degraded_warned = True
                logger.warning(
                    "병렬 분기를 격리할 수 없어 순차로 실행한다(#28)", run_id=str(self.run_id)
                )
            yield self._degraded(mark_parallel), False
            return
        budget = self.branch_state.budget
        if self.holds_branch_permit:
            if budget.locked():
                yield self._degraded(mark_parallel), False
                return
            await budget.acquire()  # locked()가 False면 기다리지 않고 바로 얻는다
        else:
            await budget.acquire()
        session: AsyncSession | None = None
        try:
            session = opener()
            yield (
                replace(
                    self,
                    db=session,
                    holds_branch_permit=True,
                    in_parallel_branch=self.in_parallel_branch or mark_parallel,
                    node_tokens=dict(self.node_tokens),
                    resume_node_token_base=None,
                    resume_node_cost_base=None,
                    skill_activations=dict(self.skill_activations),
                    skill_pins=dict(self.skill_pins),
                ),
                True,
            )
        finally:
            try:
                if session is not None:
                    await self._close_branch_session(session)
            finally:
                budget.release()

    async def _close_branch_session(self, session: AsyncSession) -> None:
        """분기 세션을 닫는다 — 취소가 와도 닫기는 끝까지 돌고, 닫기 실패는 로그로만 남긴다."""
        closes = self.branch_state.pending_closes
        task = asyncio.ensure_future(session.close())
        closes.add(task)
        task.add_done_callback(closes.discard)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("병렬 분기 세션 닫기 실패", run_id=str(self.run_id), exc_info=True)

    def add_cost(self, amount: float) -> None:
        """비용을 per-node 델타용 float와 run 전체 공유 셀 양쪽에 가산.

        cost_usd는 make_node 노드 델타(ExecutionLog)용, cost_total은 child와 공유되어
        react 예산 가드가 서브에이전트 비용까지 합산해 보도록 한다(codex C3).
        """
        self.cost_usd += amount
        self.cost_total[0] += amount

    def add_tokens(self, usage: dict[str, int]) -> None:
        """토큰 사용량을 공유 셀(usage)과 per-node 셀(node_tokens) 양쪽에 가산(R5-2).

        usage는 child 공유(예산 가드·run-level 기록·표시)이고, node_tokens는 미공유라
        make_node의 per-node ExecutionLog 델타가 서브에이전트 토큰을 제외한다 —
        cost의 cost_total(공유)/cost_usd(per-node) 분리와 동형. 이중계상 해소.
        """
        for k, v in usage.items():
            self.usage[k] = self.usage.get(k, 0) + v
            self.node_tokens[k] = self.node_tokens.get(k, 0) + v

    def child(
        self,
        target_id: uuid.UUID,
        params: dict[str, Any],
        *,
        params_image_safe: bool = False,
        params_image_safe_keys: frozenset[str] = frozenset(),
    ) -> "RunContext":
        """서브에이전트 실행용 자식 컨텍스트 — 토큰/아티팩트는 부모와 공유(누적), 깊이+1.

        on_progress는 부모 진행률을 오염시키지 않도록 전파하지 않는다.
        in_parallel_branch와 전역 호출 카운터 셀은 부모에서 그대로 전파(공유)한다.
        `params_image_safe`·`params_image_safe_keys`는 부모가 판정한 자식 params의 이미지 프롬프트
        출처다(#338·#390) — 생략하면 안전하지 않다.
        """
        stack = self.call_stack
        if self.agent_id is not None:
            stack = (*stack, str(self.agent_id))
        return RunContext(
            run_id=self.run_id,
            notebook_id=self.notebook_id,
            db=self.db,
            params=params,
            trigger=self.trigger,
            chat_message=self.chat_message,
            chat_history=self.chat_history,
            chat_model_id=self.chat_model_id,
            run_model_id=self.run_model_id,
            chat_used_model_id=self.chat_used_model_id,
            chat_used_model_name=self.chat_used_model_name,
            chat_fallback_used=self.chat_fallback_used,
            chat_output_text=self.chat_output_text,
            user_id=self.user_id,
            user_email=self.user_email,
            user_name=self.user_name,
            user_role=self.user_role,
            source_ids=self.source_ids,
            usage=self.usage,  # 토큰 누적 공유
            cost_total=self.cost_total,  # run 전체 비용 셀 공유(react 예산 가드 가시성)
            artifact_keys=self.artifact_keys,  # 아티팩트 누적 공유
            on_progress=None,
            on_tool_call=self.on_tool_call,
            on_tool_call_event=self.on_tool_call_event,
            agent_id=target_id,
            call_stack=stack,
            depth=self.depth + 1,
            params_image_safe=params_image_safe,
            in_parallel_branch=self.in_parallel_branch,  # 병렬 분기 표시 전파
            react_subagent_calls=self.react_subagent_calls,  # 전역 카운터 셀 공유
            register_source_calls=self.register_source_calls,  # run 단위 카운터 셀 공유
            tool_calls=self.tool_calls,
            tool_evidence=self.tool_evidence,
            unavailable_tools=self.unavailable_tools,
            # 스킬 카운터는 공유하고, 활성화·고정은 물려주지 않는다(D6-5) — 자식이 부모 스킬의
            # 파일을 읽으려면 스스로 활성화해야 한다.
            skill_activation_count=self.skill_activation_count,
            skill_read_bytes=self.skill_read_bytes,
            # 분기 세션 출처·예산·보유 표식은 서브에이전트로 그대로 넘긴다(#28) — 빠지면
            # 서브에이전트가 새 예산을 만들고, 분기 안인데 최상위로 오판해 permit을 기다린다
            session_factory=self.session_factory,
            branch_state=self.branch_state,
            holds_branch_permit=self.holds_branch_permit,
            lease_valid=self.lease_valid,
            params_image_safe_keys=params_image_safe_keys,
        )

    def branch_parallel(self) -> "RunContext":
        """병렬 워커 진입용 분기 컨텍스트 — 동일 깊이/세션/누적을 공유하되
        in_parallel_branch=True로 표시한다(T5/F1 react_agent 병렬 실행 금지 가드용).

        **세션을 공유한다** — 이 컨텍스트로 동시에 실행하면 같은 AsyncSession을 나눠 쓴다(#28).
        코어의 병렬 supervisor·loop는 `run_parallel`(`isolated_branch`)을 쓴다(fan-out은 #28
        Phase 2). 이 메서드는 하위 호환으로 남아 있다.

        dataclasses.replace는 얕은 복사라 db/usage/artifact_keys/카운터 셀 등
        가변 참조를 그대로 공유한다(누적 일관성 유지). 단 **node_tokens는 per-node
        델타용 미공유 셀**이므로 분기마다 독립 사본을 준다(R5-2/CR-R5-02) — 공유하면
        병렬 형제 노드의 토큰이 서로의 per-node 델타에 섞인다. 스킬 활성화 맵·고정도 사본이다.
        """
        return replace(
            self,
            in_parallel_branch=True,
            node_tokens=dict(self.node_tokens),
            # 스킬 활성화 맵은 분기마다 사본이다 — 공유하면 형제의 활성화가 섞인다(D6-5)
            skill_activations=dict(self.skill_activations),
            skill_pins=dict(self.skill_pins),
        )


async def run_parallel[T, R](
    ctx: RunContext,
    items: Sequence[T],
    run_one: Callable[[RunContext, T], Awaitable[R]],
    *,
    mark_parallel: bool = True,
) -> list[R]:
    """병렬 호출 지점 공용(#28) — 항목마다 분기 세션으로 동시에 돌리고 입력 순서대로 결과를 준다.

    분기를 얻지 못한 항목(분기 안에서 예산 소진, 격리 불가)은 호출자 세션을 쓰므로 이 호출 지점의
    잠금으로 **한 번에 하나씩** 돈다. 한 항목이 실패하면 나머지를 취소하고 기다린 뒤 원래 예외를
    올린다 — 취소하지 않으면 실패한 run 뒤에도 분기가 세션을 쥔 채 돌며 커밋까지 한다.
    `TaskGroup`은 예외를 `ExceptionGroup`으로 감싸 `except ApprovalRequired` 같은 처리를 깨므로
    쓰지 않는다.
    """
    site_lock = asyncio.Lock()

    async def one(item: T) -> R:
        async with ctx.isolated_branch(mark_parallel=mark_parallel) as (branch, isolated):
            if isolated:
                return await run_one(branch, item)
            async with site_lock:
                return await run_one(branch, item)

    tasks = [asyncio.ensure_future(one(item)) for item in items]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
