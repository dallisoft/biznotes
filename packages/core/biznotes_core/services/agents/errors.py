"""에이전트 런타임 예외 계약 — 모듈 경계를 넘는 leaf 모듈(R2 분해 기반).

runtime.py가 재수출 셸로 축소되는 과정에서 여러 노드 실행기·react 루프가
공유하는 예외를 순환 없이 참조할 수 있도록 leaf로 분리한다. 기존 공개 경로
`from biznotes_core.services.agents.runtime import AgentRuntimeError ...`는 runtime.py의
재수출로 하위 호환된다(R0-6 특성화 계약).
"""

from typing import Any


class AgentRuntimeError(Exception):
    pass


class ToolUnavailableError(AgentRuntimeError):
    """사용자와 LLM에 그대로 노출해도 안전한 도구 가용성 오류."""

    node_error_code = "tool_unavailable"  # 노드 실패 코드(#216)


class UntrustedResumeError(AgentRuntimeError):
    """승인 재개 checkpoint로 신뢰할 수 없는 `_resume`(#91).

    `_resume`은 워커가 소비한 승인의 checkpoint를 react 노드에 싣는 내부 키다. 저장된 워크플로우
    정의에 든 `_resume`은 대기 도구 호출을 승인 없이 실행하게 하므로 노드를 실패시킨다. 정책적
    차단이라 `retry_policy`·`continue_on_error`가 흡수하지 않는다
    (`SubagentApprovalNotSupportedError`와 같은 통로).
    """


class SubagentApprovalNotSupportedError(AgentRuntimeError):
    """서브에이전트 내부 HITL 승인 요청을 명시적으로 거부(§9 DA-02, HCI-D4).

    AgentRuntimeError를 상속해 기존 `except AgentRuntimeError`/`except Exception`
    경로와 하위 호환되지만, 별도 타입이므로 error_handler처럼 "실패를 흡수해
    재시도/폴백하는" 로직이 이를 일반 도구 실패와 구분해 즉시 재전파할 수 있다
    (일반 실패로 흡수하면 승인 대기를 재시도로 낭비하고 fallback으로 조용히
    대체 실행하게 되어 HITL 정책이 무력화된다).
    """


class DirectNodeRejectedError(AgentRuntimeError):
    """사용자가 직접 부작용 노드(`http`·`mcp_tool`) 요청을 거부했다(#153).

    정책적 차단이다 — `retry_policy`가 다시 승인을 요청하거나 `continue_on_error`가 하류를 계속
    실행하지 않게 `UntrustedResumeError`와 같은 통로로 즉시 전파한다.
    """


class DirectNodeRequestChangedError(AgentRuntimeError):
    """재개 때 다시 렌더링한 요청이 승인된 요청과 다르다(#153 `direct_request_hash`).

    승인한 요청만 실행한다 — 대기 중 정의·사용자 정보가 바뀌었으면 실행하지 않고 실패한다.
    거부와 같은 정책적 차단이다.
    """


class NonRetryableNodeError(AgentRuntimeError):
    """재시도하면 안 되는 노드 실패(#153) — 승인된 직접 노드 요청이 실패한 경우.

    요청이 이미 전달됐을 수 있어 `retry_policy`로 다시 보내지 않는다(다시 보내려면 다시 승인해야
    한다). 재시도만 멈추고 실패 처리(`continue_on_error` 포함)는 일반 실패와 같다. 하위 클래스도
    같은 규칙을 탄다(`LLMOutputTruncatedError`).
    """


class LLMOutputTruncatedError(NonRetryableNodeError):
    """LLM 출력이 출력 상한(`max_tokens`)에서 잘려 결과를 쓸 수 없다(#422).

    같은 상한으로 다시 불러도 또 잘리므로 json_validate도, 노드 `retry_policy`도 다시 부르지 않는다
    (`NonRetryableNodeError`). 메시지는 상한·토큰 수만 담는다 — 작성자가 상한을 올리거나 입력을
    줄이도록 그대로 실행 로그에 남긴다.
    """

    node_error_code = "llm_output_truncated"  # 노드 실패 코드(#216)


class GatedNodeInFanOutError(SubagentApprovalNotSupportedError):
    """승인이 필요한 직접 노드가 fan-out 그래프에서 실행되려 한다(#153 A-4).

    형제가 함께 돌면 승인 대기가 형제를 취소해 승인이 반복되고 승인된 요청이 취소·중복된다.
    `SubagentApprovalNotSupportedError` 계열이라 재시도·`continue_on_error`·`error_handler` 폴백이
    흡수하지 않는다.
    """


class RunLeaseLostError(AgentRuntimeError):
    """이 워커가 run lease를 잃었다(#153 B) — 외부 호출을 하지 않고 멈춘다.

    회수된 run에서 부작용이 나가지 않게 한다. 정책적 차단이라 재시도·`continue_on_error`로 덮지
    않는다.
    """


class ApprovalRequired(Exception):
    """react_agent 노드가 HITL 승인 대기 상태로 전환됨(phase-19-design.md §2.1).

    AgentRuntimeError를 상속하지 않는 별도 타입 — make_node._run과
    _exec_llm_with_tools 양쪽의 일반 예외 처리(재시도/실패/도구 실패 로깅)를
    거치지 않고 execute_agent까지 그대로 전파되어야 한다. 게이트 판정은 항상
    도구 dispatch의 try 블록 진입 **전**에 완료해 raise하므로(CDX-D2), 기존
    `except Exception`이 이 예외를 삼킬 수 없다.
    """

    def __init__(self, node_id: str, checkpoint: dict[str, Any]) -> None:
        super().__init__(f"react_agent 노드 [{node_id}] 승인 대기")
        self.node_id = node_id
        self.checkpoint = checkpoint
