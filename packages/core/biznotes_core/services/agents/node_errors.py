"""노드 실패의 오류 코드·실행 로그 문구 (#216).

노드가 실패하면 실행 로그(`execution_logs.error_code`·`error_message`)와 노드 실패 문구
(`continue_on_error` 상태, 상위 `AgentRuntimeError`, error_handler 복구 출력)에 같은 판정 결과를
쓴다. 실행 로그는 노트북 읽기 권한자가 열람할 수 있다(`services/authz.assert_can_read_run`).

예외 클래스는 두 클래스 속성으로 판정에 참여한다. 하위 클래스는 물려받고, 제품 예외도 같은 속성을
두면 같은 규칙을 탄다. 이름이 고유한 것은 제3자 예외의 같은 이름 속성(h2의 IntEnum
`error_code` 등)을 코드로 잘못 읽지 않기 위해서다.

- `node_error_code: ClassVar[str]` — 안정된 snake_case 코드(`^[a-z][a-z0-9_]{0,39}$`). 제품은 이
  코드로 사용자 문구를 고른다. 코드는 더하기만 하고(`NODE_ERROR_CODES`), 제품은 모르는 코드를 일반
  문구로 보인다. 형식에 맞지 않는 값은 없는 것으로 본다(열 길이를 넘겨 기록이 실패하지 않게).
- `node_log_message: ClassVar[str]` — (선택) 원문에 관리자 설정(모델·공급자 이름, 공급자 응답)이
  담기는 예외다. 원문 대신 이 고정 문구를 쓰고, 원문은 서버 로그로만 보낸다. 비어 있지 않은
  문자열만 인정한다(하위 클래스가 `None`으로 덮으면 없는 것이다).
- `node_log_output` — (선택, 인스턴스 값) 실패한 노드의 실행 로그 `output_data`에 남길 JSON
  객체(#359, 예: 게이트웨이가 시도한 모델). 공개해도 되는 값만 담는다. dict가 아니면 없는 것이다.

예외 메시지 자체는 바꾸지 않는다 — 관리자 진단 경로(probe·RAG 상태)는 원문을 일부러 보여 준다.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final

__all__ = ["NODE_ERROR_CODES", "NodeFailure", "describe_node_failure"]

#: 코어 예외가 쓰는 코드 — 더하기만 한다. 제품 예외의 코드는 여기 없어도 된다.
NODE_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "rerank_unavailable",  # services.rerank.RerankUnavailableError
        "gateway_error",  # services.gateway.llm.GatewayError(해석·입력 검증)
        "model_call_failed",  # services.gateway.llm.GatewayCallError(모델 호출)
        "context_length_exceeded",  # services.gateway.llm.GatewayContextLengthError(#358)
        "ssrf_blocked",  # core.ssrf.SSRFBlockedError
        "tool_unavailable",  # services.agents.errors.ToolUnavailableError
        "usage_limit_exceeded",  # services.usage_guard.UsageLimitExceeded
        "llm_output_truncated",  # services.agents.errors.LLMOutputTruncatedError(#422)
    }
)

_CODE = re.compile(r"[a-z][a-z0-9_]{0,39}")


@dataclass(frozen=True)
class NodeFailure:
    """노드 실패의 판정 결과.

    `code`는 코드가 없으면 None이다. `message`는 실행 로그·노드 실패 문구에 쓸 문구다.
    `raw`는 고정 문구로 바꿨을 때 그 예외의 원문이다(호출자가 서버 로그에 남긴다). 아니면 None이다.
    """

    code: str | None
    message: str
    raw: str | None = None
    # 실패 로그의 output_data(`node_log_output`, #359). 없으면 None이다
    output: dict[str, Any] | None = None


def _causes(exc: BaseException) -> Iterator[BaseException]:
    """명시적 원인(`raise … from …`) 사슬 — 코드 판정용(순환 방지)."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """`__cause__`, 없으면 `__context__`까지 — 고정 문구 판정용. 가리는 쪽으로만 넓다."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


def _code_of(exc: BaseException) -> str | None:
    code = getattr(exc, "node_error_code", None)
    return code if isinstance(code, str) and _CODE.fullmatch(code) else None


def _output_of(exc: BaseException) -> dict[str, Any] | None:
    output = getattr(exc, "node_log_output", None)
    return output if isinstance(output, dict) else None


def _fixed_of(exc: BaseException) -> str | None:
    fixed = getattr(exc, "node_log_message", None)
    return fixed if isinstance(fixed, str) and fixed else None


def describe_node_failure(exc: BaseException) -> NodeFailure:
    """예외 체인에서 오류 코드와 실행 로그 문구를 정한다.

    - 코드: 명시적 원인 사슬(`__cause__`)에서 형식에 맞는 `node_error_code`를 가진 **첫** 예외의
      코드. 코어 래퍼는 모두 `from`으로 잇는다. `from None`으로 끊었거나 처리 중에 난 무관한
      실패는 앞 예외로 분류하지 않는다.
    - 문구: `__context__`까지 넓힌 체인에 `node_log_message`를 가진 예외가 **하나라도** 있으면 그
      고정 문구(첫 번째)다. 감싼 예외의 문구에 원문이 섞여 있어도 새지 않는다. 없으면
      `str(exc)`다.
    - 코드와 문구는 다른 예외에서 올 수 있다 — 코드는 가장 바깥 분류, 문구는 가리는 쪽이 이긴다.
    - 로그 출력: 명시적 원인 사슬에서 `node_log_output`이 dict인 **첫** 예외의 값(#359).
    """
    code = next((c for c in map(_code_of, _causes(exc)) if c is not None), None)
    output = next((o for o in map(_output_of, _causes(exc)) if o is not None), None)
    for cur in _chain(exc):
        fixed = _fixed_of(cur)
        if fixed is not None:
            return NodeFailure(code=code, message=fixed, raw=str(cur), output=output)
    return NodeFailure(code=code, message=str(exc), output=output)
