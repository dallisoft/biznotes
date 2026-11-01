"""노드 실패의 오류 코드·실행 로그 문구 (#216).

판정 함수(`describe_node_failure`)는 직접, 그래프 경로는 실제 `execute_agent`(LangGraph)에 probe
노드를 끼워 확인한다. 실행 로그 기록(`_log_node_finish`)만 가로챈다. 실제 열 저장은
`tests/test_runtime_characterization_pg.py`가 확인한다.
"""

import ast
import enum
import pathlib
import re
import uuid
from typing import Any

import pytest
from structlog.testing import capture_logs

from biznotes_core.core.ssrf import SSRFBlockedError
from biznotes_core.models import ExecutionLog
from biznotes_core.services.agents import graph
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError, ToolUnavailableError
from biznotes_core.services.agents.node_errors import NODE_ERROR_CODES, describe_node_failure
from biznotes_core.services.agents.nodes import composite
from biznotes_core.services.gateway.llm import (
    GatewayCallError,
    GatewayContextLengthError,
    GatewayError,
)
from biznotes_core.services.rerank import RerankUnavailableError
from biznotes_core.services.usage_guard import UsageLimitExceeded

CORE = pathlib.Path(__file__).resolve().parent.parent / "biznotes_core"
SECRET = "리랭커 'acme-secret-reranker' 모델 'bge-secret'을 쓸 수 없습니다"
RERANK_FIXED = "리랭커를 사용할 수 없어 검색을 진행하지 못했습니다"
GATEWAY_FIXED = "모델 호출에 실패했습니다"
CONTEXT_FIXED = "입력이 모델 컨텍스트 한도를 넘었습니다"


# ---------- 판정 함수 ----------


def test_unclassified_failure_keeps_the_message() -> None:
    failure = describe_node_failure(RuntimeError("평범한 실패"))

    assert (failure.code, failure.message, failure.raw) == (None, "평범한 실패", None)


@pytest.mark.parametrize(
    ("exc", "code", "fixed"),
    [
        (RerankUnavailableError(SECRET), "rerank_unavailable", RERANK_FIXED),
        (GatewayCallError(f"LLM 호출 실패: {SECRET}"), "model_call_failed", GATEWAY_FIXED),
        # 컨텍스트 초과(#358)도 원문(모델·공급자 응답)을 숨기고 원인만 알린다
        (
            GatewayContextLengthError(f"LLM 호출 실패(컨텍스트 초과): {SECRET}"),
            "context_length_exceeded",
            CONTEXT_FIXED,
        ),
    ],
)
def test_admin_config_failures_get_a_fixed_message(
    exc: Exception, code: str, fixed: str
) -> None:
    failure = describe_node_failure(exc)

    assert (failure.code, failure.message, failure.raw) == (code, fixed, str(exc))
    # 예외 메시지 자체는 그대로다 — 관리자 진단 경로가 원문을 보여 준다
    assert SECRET in str(exc)


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (SSRFBlockedError("요청 차단됨: 안전하지 않은 URL입니다."), "ssrf_blocked"),
        (ToolUnavailableError("도구를 쓸 수 없습니다"), "tool_unavailable"),
        (UsageLimitExceeded("tokens", 10, 5), "usage_limit_exceeded"),
        # 게이트웨이 입력·해석 오류는 작성자가 고칠 수 있는 안내라 문구를 지킨다
        # (호출 실패와 다르다)
        (GatewayError("TTS 입력이 비어 있습니다"), "gateway_error"),
        (GatewayError("등록된 tts 모델이 없습니다 — 어드민에서 등록하세요"), "gateway_error"),
    ],
)
def test_coded_failures_keep_their_safe_message(exc: Exception, code: str) -> None:
    failure = describe_node_failure(exc)

    assert (failure.code, failure.message, failure.raw) == (code, str(exc), None)


def test_wrapped_failure_is_classified_and_redacted_through_the_cause() -> None:
    try:
        try:
            raise RerankUnavailableError(SECRET)
        except RerankUnavailableError as e:
            raise AgentRuntimeError(f"노드 [x] 실행 실패: {e}") from e
    except AgentRuntimeError as outer:
        failure = describe_node_failure(outer)

    assert (failure.code, failure.message) == ("rerank_unavailable", RERANK_FIXED)


def test_implicit_context_redacts_but_does_not_classify() -> None:
    """문맥(`from` 없음)은 문구를 가리는 데만 쓴다 — 처리 중에 난 무관한 실패를 앞 예외로 분류하지
    않는다."""
    try:
        try:
            raise GatewayCallError(f"LLM 호출 실패: {SECRET}")
        except GatewayCallError:
            raise AgentRuntimeError(f"정리 실패: {SECRET}")  # noqa: B904 — `from` 없음
    except AgentRuntimeError as outer:
        failure = describe_node_failure(outer)

    assert (failure.code, failure.message) == (None, GATEWAY_FIXED)


def test_from_none_stops_classification_but_still_redacts() -> None:
    try:
        try:
            raise RerankUnavailableError(SECRET)
        except RerankUnavailableError:
            raise RuntimeError(f"감춘 원인: {SECRET}") from None
    except RuntimeError as outer:
        failure = describe_node_failure(outer)

    assert (failure.code, failure.message) == (None, RERANK_FIXED)


class _H2Codes(enum.IntEnum):
    PROTOCOL_ERROR = 1


@pytest.mark.parametrize(
    "bad",
    [_H2Codes.PROTOCOL_ERROR, "x" * 41, "Not-Snake", "", 7, None],
    ids=["IntEnum", "41자", "대문자", "빈값", "정수", "None"],
)
def test_malformed_codes_are_ignored(bad: Any) -> None:
    """제3자·제품 예외의 형식에 맞지 않는 값은 없는 것으로 본다 — 열 길이·타입 때문에 기록 전체가
    실패하지 않게 한다."""

    class Weird(Exception):
        node_error_code = bad
        node_log_message = None  # None으로 덮으면 고정 문구도 없다

    outer = ToolUnavailableError("바깥")
    outer.__cause__ = Weird("안쪽")

    assert describe_node_failure(Weird("x")) == describe_node_failure(Weird("x"))
    assert describe_node_failure(Weird("x")).code is None
    assert describe_node_failure(Weird("x")).message == "x"
    # 형식이 틀린 코드는 건너뛰고, 바깥 예외의 코드는 그대로 쓴다
    assert describe_node_failure(outer).code == "tool_unavailable"


def test_third_party_error_code_attribute_is_not_read() -> None:
    class ProtocolError(Exception):  # h2.exceptions.ProtocolError와 같은 모양
        error_code = _H2Codes.PROTOCOL_ERROR

    outer = AgentRuntimeError("http 실패")
    outer.__cause__ = ProtocolError("h2")

    assert describe_node_failure(outer).code is None


def test_first_code_wins_but_any_fixed_message_redacts() -> None:
    outer = ToolUnavailableError(f"도구 실패: {SECRET}")
    outer.__cause__ = GatewayCallError(SECRET)

    failure = describe_node_failure(outer)

    assert (failure.code, failure.message) == ("tool_unavailable", GATEWAY_FIXED)


def test_cyclic_chain_terminates() -> None:
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__, b.__cause__ = b, a

    assert describe_node_failure(a).message == "a"


def test_product_exceptions_can_opt_in_with_class_attributes() -> None:
    class ProductQuotaError(Exception):
        node_error_code = "product_quota"
        node_log_message = "제품 한도 초과"

    class Child(ProductQuotaError):
        pass

    failure = describe_node_failure(Child("내부 계정 acme-123"))

    assert (failure.code, failure.message) == ("product_quota", "제품 한도 초과")


def _class_codes() -> dict[str, Any]:
    """코어 소스 전체(지연 import 모듈 포함)에서 클래스 본문의 `node_error_code` 대입을 모은다 —
    import 없이 AST로 훑는다. `x = "..."`와 `x: ClassVar[str] = "..."` 둘 다 본다. 값이 문자열
    상수가 아니면 그 노드를 그대로 담아 아래 단언이 실패하게 한다."""
    codes: dict[str, Any] = {}
    for path in CORE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    targets, value = stmt.targets, stmt.value
                elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                    targets, value = [stmt.target], stmt.value
                else:
                    continue
                if not any(isinstance(t, ast.Name) and t.id == "node_error_code" for t in targets):
                    continue
                owner = f"{path.relative_to(CORE)}:{node.name}"
                is_str = isinstance(value, ast.Constant) and isinstance(value.value, str)
                codes[owner] = value.value if is_str else ast.dump(value)
    return codes


def test_core_codes_are_registered_snake_case_and_fit_the_column() -> None:
    codes = _class_codes()

    assert set(codes.values()) == NODE_ERROR_CODES, codes
    width = ExecutionLog.__table__.c.error_code.type.length
    for owner, code in codes.items():
        assert re.fullmatch(r"[a-z][a-z0-9_]*", code), owner
        assert len(code) <= width, owner


def test_execution_log_has_a_nullable_error_code_column() -> None:
    col = ExecutionLog.__table__.c.error_code

    assert (str(col.type), col.nullable, col.default, col.server_default) == (
        "VARCHAR(40)", True, None, None
    )
    assert ExecutionLog.__table__.c.error_message.nullable  # 기존 열은 그대로다


# ---------- 그래프 경로 ----------


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """probe 노드 — node의 `raise`가 있으면 그 예외를 던진다. 다음 노드는 받은 상태를 기록한다."""
    out: dict[str, Any] = {"finished": {}, "seen_state": {}}

    async def log_start(run_id: Any, node_id: str, *a: Any, **k: Any) -> str:
        return node_id

    async def log_finish(log_id: str, **kw: Any) -> bool:
        out["finished"][log_id] = kw
        return True

    async def probe(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
        if "raise" in node:
            raise node["raise"]
        out["seen_state"] = {k: v for k, v in state.items() if isinstance(v, dict)}
        return {"text": node["id"]}

    monkeypatch.setattr(graph, "_log_node_start", log_start)
    monkeypatch.setattr(graph, "_log_node_finish", log_finish)
    monkeypatch.setitem(graph.NODE_EXECUTORS, "probe", probe)
    return out


def _ctx() -> RunContext:
    return RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={})  # type: ignore[arg-type]


def _wrapped_in_subgraph() -> AgentRuntimeError:
    """하위 그래프·sub_workflow 실패처럼 원문이 감싼 문구에 섞인 예외."""
    try:
        raise RerankUnavailableError(SECRET)
    except RerankUnavailableError as e:
        err = AgentRuntimeError(f"노드 [inner] 실행 실패: {e}")
        err.__cause__ = e
        return err


@pytest.mark.parametrize(
    ("exc", "code", "message"),
    [
        (RerankUnavailableError(SECRET), "rerank_unavailable", RERANK_FIXED),
        (GatewayCallError(f"LLM 호출 실패: {SECRET}"), "model_call_failed", GATEWAY_FIXED),
        (_wrapped_in_subgraph(), "rerank_unavailable", RERANK_FIXED),
    ],
    ids=["리랭커", "모델-호출", "감싼-실패"],
)
async def test_failed_node_logs_code_and_fixed_message_and_raises_it(
    run: dict[str, Any], exc: Exception, code: str, message: str
) -> None:
    definition = {"nodes": [{"id": "a", "type": "probe", "raise": exc}], "edges": []}

    with capture_logs() as logs, pytest.raises(AgentRuntimeError) as raised:
        await graph.execute_agent(_ctx(), "workflow", definition)

    finish = run["finished"]["a"]
    assert (finish["status"], finish["error_code"], finish["error_message"]) == (
        "failed", code, message
    )
    assert str(raised.value) == f"노드 [a] 실행 실패: {message}"
    assert SECRET not in str(raised.value)
    assert raised.value.__cause__ is exc  # 제품 워커의 유형 탐색은 그대로 동작한다
    # 원문은 서버 로그로만 간다
    [warning] = [e for e in logs if e.get("error_code") == code and e["log_level"] == "warning"]
    assert SECRET in warning["error"] and warning["node_id"] == "a"


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (SSRFBlockedError("요청 차단됨: 안전하지 않은 URL입니다."), "ssrf_blocked"),
        (ToolUnavailableError("도구를 쓸 수 없습니다"), "tool_unavailable"),
        (UsageLimitExceeded("tokens", 10, 5), "usage_limit_exceeded"),
        (GatewayError("TTS 입력이 비어 있습니다"), "gateway_error"),
        (RuntimeError("평범한 실패"), None),
    ],
)
async def test_other_failures_keep_their_message(
    run: dict[str, Any], exc: Exception, code: str | None
) -> None:
    definition = {"nodes": [{"id": "a", "type": "probe", "raise": exc}], "edges": []}

    with pytest.raises(AgentRuntimeError) as raised:
        await graph.execute_agent(_ctx(), "workflow", definition)

    finish = run["finished"]["a"]
    assert (finish["error_code"], finish["error_message"]) == (code, str(exc))
    assert str(raised.value) == f"노드 [a] 실행 실패: {exc}"


async def test_continue_on_error_state_carries_the_fixed_message(run: dict[str, Any]) -> None:
    definition = {
        "nodes": [
            {"id": "a", "type": "probe", "raise": RerankUnavailableError(SECRET),
             "continue_on_error": True},
            {"id": "b", "type": "probe"},
        ],
        "edges": [["a", "b"]],
    }

    await graph.execute_agent(_ctx(), "workflow", definition)

    assert run["seen_state"]["a"] == {"failed": True, "error": RERANK_FIXED}
    assert run["finished"]["a"]["error_code"] == "rerank_unavailable"


async def test_succeeded_node_does_not_pass_an_error_code(run: dict[str, Any]) -> None:
    """성공 경로의 기록 인자는 그대로다 — 키워드를 나열한 제품 대역이 깨지지 않는다."""
    await graph.execute_agent(_ctx(), "workflow", {"nodes": [{"id": "a", "type": "probe"}]})

    assert "error_code" not in run["finished"]["a"]
    assert run["finished"]["a"]["status"] == "succeeded"


async def test_error_handler_recovery_output_uses_the_fixed_message(
    run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def subagent(ctx: Any, agent_id: str, params: Any) -> str:
        if agent_id == "protected":
            raise RerankUnavailableError(SECRET)
        return "폴백 결과"

    monkeypatch.setattr(composite, "_run_subagent", subagent)
    definition = {
        "nodes": [
            {"id": "eh", "type": "error_handler", "protected_agent_id": "protected",
             "fallback_agent_id": "fallback"},
        ],
    }

    await graph.execute_agent(_ctx(), "workflow", definition)

    output = run["finished"]["eh"]["output_data"]
    assert output["recovered"] is True and output["error"] == RERANK_FIXED
    assert SECRET not in str(output)
