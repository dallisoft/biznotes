"""tool-calling 경로의 폴백 체인·재시도·서킷과 턴 사이 모델 유지(#374).

게이트웨이는 실경로다 — 모델 조회(`resolve_chain`)와 서킷의 Redis만 대역이다. 응답하는 후보는 mock
프로바이더 실경로다. 공급자 실패는 두 가지로 낸다: `openai` 유형 후보는 `litellm.acompletion`이
LiteLLM 예외(429·5xx·컨텍스트 초과)를 내고(SDK 내부 재시도를 기다리지 않는다), 시도별 시나리오는
후보 호출(`_tool_turn`) 앞에서 게이트웨이 예외를 낸다. 재시도·서킷은 예외 사슬의 HTTP 상태로
나누므로(#380) 실패에는 실제 LiteLLM 예외처럼 HTTP 응답을 사슬에 단다(`_caused`).
"""

import copy
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import litellm
import pytest
from litellm.exceptions import MidStreamFallbackError

from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.services.agents import graph, runtime
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import ApprovalRequired
from biznotes_core.services.agents.react import approval, loop
from biznotes_core.services.gateway import circuit
from biznotes_core.services.gateway import llm as gateway
from biznotes_core.services.gateway.llm import (
    GatewayCallError,
    GatewayContextLengthError,
    GatewayError,
    ResolvedModel,
    StreamResult,
    ToolCall,
    ToolTurnResult,
    complete_stream_with_fallback,
    complete_with_tools,
)
from biznotes_core.services.gateway.retry import classify_call_failure

MESSAGES = [{"role": "user", "content": "노트를 검색해 주세요"}]
TOOLS = [{"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}]
TOOL_NODE = {"id": "gen", "type": "llm", "prompt": "검색해서 답해 주세요", "tools": ["web_search"]}


def _model(name: str, provider_type: str = "mock") -> ResolvedModel:
    provider = LLMProvider(id=uuid.uuid4(), name=f"p-{name}", type=provider_type)
    model = LLMModel(
        id=uuid.uuid4(),
        provider_id=provider.id,
        model_name=f"m-{name}",
        display_name=name,
        capabilities=["llm"],
    )
    return ResolvedModel(model=model, provider=provider)


def _meta(m: ResolvedModel) -> dict[str, str]:
    return {"id": str(m.model.id), "display_name": m.model.display_name}


def _http_error(status: int) -> httpx.HTTPStatusError:
    """실제 LiteLLM 예외 사슬 끝의 HTTP 응답 — 재시도·서킷 분류(#380)가 이것으로 상태를 읽는다."""
    request = httpx.Request("POST", "https://provider.test/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def _caused[E: BaseException](error: E, status: int) -> E:
    error.__cause__ = _http_error(status)
    return error


def _call_error(status: int) -> GatewayCallError:
    """`_tool_turn`이 내는 모양 — 공급자 예외를 사슬에 단 게이트웨이 예외."""
    return _caused(GatewayCallError(f"LLM tool 호출 실패: {status}"), status)


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", "https://provider.test"))


def _context_rejection(model: str) -> Exception:
    return litellm.ContextWindowExceededError(
        message="maximum context length exceeded", model=model, llm_provider="openai"
    )


def _rate_limited(m: str) -> Exception:
    return _caused(litellm.RateLimitError(message="429", llm_provider="openai", model=m), 429)


def _timed_out(m: str) -> Exception:
    return litellm.Timeout(message="timed out", model=m, llm_provider="openai")


def _tools_unsupported(m: str) -> Exception:
    """도구를 받지 않는 모델의 400(이슈 재현)."""
    return _caused(
        litellm.BadRequestError(message="tools is not supported", model=m, llm_provider="openai"),
        400,
    )


def _model_not_found(m: str) -> Exception:
    return _caused(litellm.NotFoundError(message="no model", model=m, llm_provider="openai"), 404)


def _bad_key(m: str) -> Exception:
    return _caused(litellm.AuthenticationError(message="401", llm_provider="openai", model=m), 401)


TRANSIENT = [
    pytest.param(_rate_limited, id="429"),
    pytest.param(
        lambda m: _caused(
            litellm.InternalServerError(message="boom", llm_provider="openai", model=m), 500
        ),
        id="500",
    ),
    pytest.param(
        lambda m: _caused(
            litellm.ServiceUnavailableError(message="unavailable", llm_provider="openai", model=m),
            503,
        ),
        id="503",
    ),
    pytest.param(_timed_out, id="timeout"),
]
# 다시 보내도 결과가 같은 요청 거부(#380)
PERMANENT = [
    pytest.param(_tools_unsupported, id="400"),
    pytest.param(_model_not_found, id="404"),
    pytest.param(
        lambda m: _caused(
            litellm.UnprocessableEntityError(
                message="invalid schema", model=m, llm_provider="openai", response=_response(422)
            ),
            422,
        ),
        id="422",
    ),
]
# 키·권한 거부 — 재시도하지 않지만 서킷에는 센다(이슈 권고)
AUTH = [
    pytest.param(_bad_key, id="401"),
    pytest.param(
        lambda m: _caused(
            litellm.PermissionDeniedError(
                message="forbidden", llm_provider="openai", model=m, response=_response(403)
            ),
            403,
        ),
        id="403",
    ),
]


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """체인·열린 서킷·실패를 정하고 시도한 후보(`tried`)와 서킷 실패(`failures`)를 모은다.

    - `plan[표시 이름]`: 시도마다 하나씩 쓰는 결과 목록(마지막 값은 계속 쓴다). 예외면 그 시도가
      실패하고, None이면 실경로로 보낸다.
    - `provider_errors[model_name]`: `openai` 유형 후보의 `litellm.acompletion`이 낼 예외.
    """
    state: dict[str, Any] = {
        "chain": [], "open": set(), "plan": {}, "provider_errors": {}, "tried": [], "failures": [],
    }
    real_turn = gateway._tool_turn

    async def chain(db: Any, model_id: Any, modality: str = "llm") -> list[ResolvedModel]:
        return state["chain"]

    async def turn(candidate: ResolvedModel, *a: Any, **k: Any) -> ToolTurnResult:
        name = candidate.model.display_name
        state["tried"].append(name)
        plan = state["plan"].get(name)
        if plan:
            outcome = plan.pop(0) if len(plan) > 1 else plan[0]
            if outcome is not None:
                raise outcome
        return await real_turn(candidate, *a, **k)

    async def acompletion(**kw: Any) -> Any:
        raise state["provider_errors"][kw["model"]]

    async def is_open(provider_id: Any) -> bool:
        return provider_id in state["open"]

    async def record_failure(provider_id: Any) -> bool:
        state["failures"].append(provider_id)
        return False

    async def record_success(provider_id: Any) -> None:
        return None

    monkeypatch.setattr(gateway, "resolve_chain", chain)
    monkeypatch.setattr(gateway, "_tool_turn", turn)
    monkeypatch.setattr(litellm, "acompletion", acompletion)
    monkeypatch.setattr(circuit, "is_open", is_open)
    monkeypatch.setattr(circuit, "record_failure", record_failure)
    monkeypatch.setattr(circuit, "record_success", record_success)
    return state


class _Breaker:
    """서킷 브레이커의 메모리 대역 — 임계·차단 규칙은 실제 모듈 상수를 쓴다."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.fails: dict[uuid.UUID, int] = {}
        self.opened: set[uuid.UUID] = set()
        self.failures: list[uuid.UUID] = []
        monkeypatch.setattr(circuit, "is_open", self.is_open)
        monkeypatch.setattr(circuit, "record_failure", self.record_failure)
        monkeypatch.setattr(circuit, "record_success", self.record_success)

    async def is_open(self, provider_id: uuid.UUID) -> bool:
        return provider_id in self.opened

    async def record_failure(self, provider_id: uuid.UUID) -> bool:
        self.failures.append(provider_id)
        self.fails[provider_id] = self.fails.get(provider_id, 0) + 1
        if self.fails[provider_id] >= circuit.FAIL_THRESHOLD:
            self.opened.add(provider_id)
            self.fails[provider_id] = 0
            return True
        return False

    async def record_success(self, provider_id: uuid.UUID) -> None:
        self.fails.pop(provider_id, None)
        self.opened.discard(provider_id)


@pytest.fixture
def node_logs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """노드 로그 기록을 엿보고, 내장 web_search는 고정 결과를 돌려준다."""
    logs: dict[str, Any] = {}

    async def log_start(run_id: Any, node_id: str, *a: Any, **k: Any) -> str:
        return node_id

    async def log_finish(log_id: str, **kw: Any) -> bool:
        logs[log_id] = kw
        return True

    async def no_search(*_a: Any, **_k: Any) -> str:
        return "검색 결과 없음"

    monkeypatch.setattr(graph, "_log_node_start", log_start)
    monkeypatch.setattr(graph, "_log_node_finish", log_finish)
    monkeypatch.setattr(runtime, "_dispatch_builtin_tool", no_search)
    return logs


async def _execute(node: dict[str, Any]) -> RunContext:
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={})  # type: ignore[arg-type]
    await graph.execute_agent(ctx, "workflow", {"nodes": [node], "edges": []})
    return ctx


async def _turn(**kw: Any) -> ToolTurnResult:
    return await complete_with_tools(None, None, MESSAGES, TOOLS, **kw)  # type: ignore[arg-type]


# ── 수락 기준: 5xx·429는 폴백, 열린 서킷은 건너뜀, 컨텍스트 초과는 재시도·서킷 밖 ──


@pytest.mark.parametrize("error", TRANSIENT)
async def test_transient_failure_falls_back_to_the_next_candidate(gw, error):
    a, b = _model("a", "openai"), _model("b")
    gw["chain"] = [a, b]
    gw["provider_errors"] = {"m-a": error("m-a")}

    res = await _turn()

    assert (res.model, res.fallback_used) == (b, True)
    assert gw["tried"] == ["a", "a", "b"]  # 후보마다 백오프 1회 재시도
    assert gw["failures"] == [a.provider.id]  # 실패한 후보는 서킷에 1회 센다


async def test_open_circuit_provider_is_skipped(gw):
    a, b = _model("a"), _model("b")
    gw["chain"] = [a, b]
    gw["open"] = {a.provider.id}

    res = await _turn()

    assert (res.model, res.fallback_used) == (b, True)
    assert gw["tried"] == ["b"]


async def test_context_rejection_is_neither_retried_nor_counted(gw):
    a, b = _model("a", "openai"), _model("b")
    gw["chain"] = [a, b]
    gw["provider_errors"] = {"m-a": _context_rejection("m-a")}

    res = await _turn()

    assert (res.model, res.fallback_used) == (b, True)
    assert gw["tried"] == ["a", "b"]  # 같은 후보에 다시 보내지 않는다
    assert gw["failures"] == []


async def test_all_failed_candidates_are_listed(gw):
    a, b, c = _model("a", "openai"), _model("b"), _model("c", "openai")
    gw["chain"] = [a, b, c]
    gw["open"] = {b.provider.id}
    gw["provider_errors"] = {
        "m-a": _context_rejection("m-a"),
        "m-c": _caused(
            litellm.InternalServerError(message="boom", llm_provider="openai", model="m-c"), 500
        ),
    }

    with pytest.raises(GatewayCallError) as raised:
        await _turn()

    # 컨텍스트 초과가 아닌 실패가 섞이면 원인을 컨텍스트로 돌리지 않는다(#358)
    assert not isinstance(raised.value, GatewayContextLengthError)
    assert raised.value.model_attempts == [
        {**_meta(a), "error_code": "context_length_exceeded"},
        {**_meta(b), "error_code": "circuit_open"},
        {**_meta(c), "error_code": "model_call_failed"},
    ]
    assert gw["failures"] == [c.provider.id]


async def test_context_only_failures_report_context_length(gw):
    a = _model("a", "openai")
    gw["chain"] = [a]
    gw["provider_errors"] = {"m-a": _context_rejection("m-a")}

    with pytest.raises(GatewayContextLengthError) as raised:
        await _turn()

    assert raised.value.model_attempts == [{**_meta(a), "error_code": "context_length_exceeded"}]


# ── 영구 요청 오류는 재시도·서킷 밖, 401·403은 재시도 없이 서킷에 센다(#380) ──────


def _refused(m: str) -> Exception:
    error = litellm.InternalServerError(message="refused", llm_provider="openai", model=m)
    error.__cause__ = httpx.ConnectError("refused")
    return error


def _mid_stream_overloaded(m: str) -> Exception:
    """첫 조각 전 SSE 오류의 실제 사슬(litellm 1.105 실측) — `MidStreamFallbackError`(500) →
    `AnthropicError`(500). HTTP 200 안에 실려 httpx 응답이 없다."""
    from litellm.llms.anthropic.common_utils import AnthropicError

    inner = AnthropicError(status_code=500, message="Overloaded")
    error = MidStreamFallbackError(
        message="Overloaded", model=m, llm_provider="anthropic", original_exception=inner,
        is_pre_first_chunk=True,
    )
    error.__context__ = inner
    return error


class _NoResponse(Exception):
    """HTTP 응답 없이 `status_code`만 단 예외 — LiteLLM 공급자 예외 모양."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


# HTTP 근거가 없는 실패(#380) — LiteLLM `status_code`로 보조 분류한다
UNGROUNDED = {
    "transient": [pytest.param(_mid_stream_overloaded, id="sse-overloaded-500"),
                  pytest.param(lambda m: _NoResponse(429), id="no-response-429")],
    "auth": [pytest.param(lambda m: _NoResponse(401), id="no-response-401")],
    "permanent": [pytest.param(lambda m: _NoResponse(400), id="no-response-400"),
                  pytest.param(lambda m: GatewayCallError(m), id="status-less")],
}


@pytest.mark.parametrize(
    ("errors", "kind"),
    [
        (TRANSIENT + [pytest.param(_refused, id="connect")] + UNGROUNDED["transient"],
         "transient"),
        (AUTH + UNGROUNDED["auth"], "auth"),
        (PERMANENT + UNGROUNDED["permanent"], "permanent"),
    ],
    ids=["transient", "auth", "permanent"],
)
def test_call_failure_classes(errors, kind):
    """공유 분류(`retry.classify_call_failure`) — 임베딩·리랭크와 같은 `_classify` 위의 세 갈래."""
    for param in errors:
        error = param.values[0]("m")
        assert classify_call_failure(error) == kind, param.id
        # 게이트웨이 예외로 감싸도(`raise ... from e`) 사슬로 같은 판정이다
        wrapped = GatewayCallError("LLM 호출 실패")
        wrapped.__cause__ = error
        assert classify_call_failure(wrapped) == kind, param.id


@pytest.mark.parametrize("error", PERMANENT)
async def test_permanent_rejection_falls_back_without_retry_or_circuit(gw, error):
    a, b = _model("a", "openai"), _model("b")
    gw["chain"] = [a, b]
    gw["provider_errors"] = {"m-a": error("m-a")}

    res = await _turn()

    assert (res.model, res.fallback_used) == (b, True)
    assert gw["tried"] == ["a", "b"]  # 같은 후보에 다시 보내지 않는다
    assert gw["failures"] == []  # 프로바이더 실패로 세지 않는다


async def test_repeated_tools_unsupported_400_never_opens_the_circuit(gw, monkeypatch):
    """이슈 재현 — 도구를 받지 않는 모델의 400이 임계 넘게 반복돼도 서킷이 열리지 않는다."""
    a, b = _model("a", "openai"), _model("b")
    breaker = _Breaker(monkeypatch)
    gw["chain"] = [a, b]
    gw["provider_errors"] = {"m-a": _tools_unsupported("m-a")}

    for _ in range(circuit.FAIL_THRESHOLD + 1):
        res = await _turn()
        assert (res.model, res.fallback_used) == (b, True)

    assert gw["tried"] == ["a", "b"] * (circuit.FAIL_THRESHOLD + 1)  # 매번 a부터, 재시도 없이
    assert breaker.failures == []
    assert not await circuit.is_open(a.provider.id)


@pytest.mark.parametrize("error", TRANSIENT)
async def test_repeated_transient_failures_still_open_the_circuit(gw, monkeypatch, error):
    a, b = _model("a", "openai"), _model("b")
    breaker = _Breaker(monkeypatch)
    gw["chain"] = [a, b]
    gw["provider_errors"] = {"m-a": error("m-a")}

    for _ in range(circuit.FAIL_THRESHOLD):
        assert (await _turn()).model == b

    assert gw["tried"] == ["a", "a", "b"] * circuit.FAIL_THRESHOLD  # 후보마다 백오프 1회 재시도
    assert breaker.failures == [a.provider.id] * circuit.FAIL_THRESHOLD
    assert await circuit.is_open(a.provider.id)


@pytest.mark.parametrize("error", AUTH)
async def test_auth_rejection_is_counted_but_not_retried(gw, error):
    a, b = _model("a", "openai"), _model("b")
    gw["chain"] = [a, b]
    gw["provider_errors"] = {"m-a": error("m-a")}

    res = await _turn()

    assert (res.model, res.fallback_used) == (b, True)
    assert gw["tried"] == ["a", "b"]  # 키·권한은 다시 보내도 같다
    assert gw["failures"] == [a.provider.id]  # 고칠 때까지 막도록 서킷에는 센다


async def test_status_less_gateway_error_is_permanent(gw):
    """상태를 알 수 없는 예외는 임베딩·리랭크(`retry._classify`)와 같이 영구 오류로 본다."""
    a, b = _model("a"), _model("b")
    gw["chain"] = [a, b]
    gw["plan"] = {"a": [GatewayCallError("LLM tool 호출 실패: 원인 모름")]}

    res = await _turn()

    assert res.model == b
    assert gw["tried"] == ["a", "b"]
    assert gw["failures"] == []


async def test_permanent_only_failures_are_not_reported_as_context(gw):
    a = _model("a", "openai")
    gw["chain"] = [a]
    gw["provider_errors"] = {"m-a": _tools_unsupported("m-a")}

    with pytest.raises(GatewayCallError) as raised:
        await _turn()

    assert not isinstance(raised.value, GatewayContextLengthError)
    assert raised.value.model_attempts == [{**_meta(a), "error_code": "model_call_failed"}]
    assert gw["failures"] == []


@pytest.mark.parametrize(
    ("error", "retried", "counted"),
    [
        pytest.param(_rate_limited, True, True, id="429"),
        pytest.param(_timed_out, True, True, id="timeout"),
        pytest.param(_bad_key, False, True, id="401"),
        pytest.param(_tools_unsupported, False, False, id="400"),
        pytest.param(_model_not_found, False, False, id="404"),
        # HTTP 응답이 없는 실패 — LiteLLM `status_code`로 보조 분류(#380)
        pytest.param(_mid_stream_overloaded, True, True, id="sse-overloaded-500"),
        pytest.param(lambda m: _NoResponse(401), False, True, id="no-response-401"),
        pytest.param(lambda m: _NoResponse(400), False, False, id="no-response-400"),
        pytest.param(lambda m: GatewayError("원인 모름"), False, False, id="bare-gateway-error"),
    ],
)
async def test_stream_and_tool_paths_share_the_rule(gw, monkeypatch, error, retried, counted):
    """같은 공급자 실패에 스트리밍·tool 경로가 같은 재시도·서킷 결정을 내린다(#380)."""
    a, b = _model("a", "openai"), _model("b")
    gw["chain"] = [a, b]
    gw["provider_errors"] = {"m-a": error("m-a")}
    sent: list[str] = []
    provider = litellm.acompletion

    async def counting(**kw: Any) -> Any:
        sent.append(kw["model"])
        return await provider(**kw)

    monkeypatch.setattr(litellm, "acompletion", counting)

    tool_res = await _turn()
    tool_sent, tool_failures = sent[:], gw["failures"][:]
    sent.clear()
    gw["failures"].clear()
    result = StreamResult()
    gen = await complete_stream_with_fallback(None, None, MESSAGES, result)  # type: ignore[arg-type]
    assert "".join([d async for d in gen])

    assert (tool_res.model, result.model) == (b, b)
    assert tool_sent == sent == (["m-a", "m-a"] if retried else ["m-a"])
    assert tool_failures == gw["failures"] == ([a.provider.id] if counted else [])


# ── 먼저 시도할 후보(prefer_model_id) ─────────────────────────────────────


async def test_preferred_candidate_goes_first_and_the_chain_wraps(gw):
    a, b, c = _model("a"), _model("b"), _model("c")
    gw["chain"] = [a, b, c]
    gw["plan"] = {"b": [_call_error(500)], "c": [_call_error(503)]}

    res = await _turn(prefer_model_id=b.model.id)

    # b부터 체인 순서로 한 바퀴(b → c → a). 폴백 여부는 체인의 1차 모델과 견준다
    assert gw["tried"] == ["b", "b", "c", "c", "a"]
    assert (res.model, res.fallback_used) == (a, False)


@pytest.mark.parametrize("prefer", ["primary", "unknown", None])
async def test_preferring_the_primary_or_an_unknown_model_keeps_the_chain(gw, prefer):
    a, b = _model("a"), _model("b")
    gw["chain"] = [a, b]
    ids = {"primary": a.model.id, "unknown": uuid.uuid4(), None: None}

    res = await _turn(prefer_model_id=ids[prefer])

    assert gw["tried"] == ["a"]
    assert (res.model, res.fallback_used) == (a, False)


async def test_stream_starts_from_the_preferred_candidate(gw):
    a, b = _model("a"), _model("b")
    gw["chain"] = [a, b]
    result = StreamResult()

    gen = await complete_stream_with_fallback(
        None, None, MESSAGES, result, prefer_model_id=b.model.id  # type: ignore[arg-type]
    )
    text = "".join([d async for d in gen])

    assert text
    assert (result.model, result.fallback_used) == (b, True)


# ── 턴 사이 모델 유지(owner 결정: 첫 턴 모델 유지) ─────────────────────────


async def test_tool_node_keeps_the_fallback_model_after_the_primary_recovers(gw, node_logs):
    a, b = _model("a"), _model("b")
    gw["chain"] = [a, b]
    # a는 첫 턴(재시도 포함)에만 실패하고 그 뒤로는 응답할 수 있다
    gw["plan"] = {"a": [_call_error(500), _call_error(500), None]}

    await _execute(TOOL_NODE)

    # 첫 턴: a 실패 2회 → b(도구 호출). 둘째 턴: 회복한 a로 돌아가지 않고 b가 최종 답을 낸다
    assert gw["tried"] == ["a", "a", "b", "b"]
    out = node_logs["gen"]["output_data"]
    assert (out["model"], out["fallback_used"]) == (_meta(b), True)


async def test_failing_kept_model_moves_on_in_chain_order(gw, node_logs):
    a, b, c = _model("a"), _model("b"), _model("c")
    gw["chain"] = [a, b, c]
    # a는 계속 실패하고, b는 첫 응답(도구 호출) 뒤로 실패한다
    gw["plan"] = {"a": [_call_error(500)], "b": [None, _call_error(500)]}

    await _execute(TOOL_NODE)

    # 둘째 턴은 b부터 — b 실패 2회 뒤 체인의 다음 후보 c(a보다 먼저)
    assert gw["tried"] == ["a", "a", "b", "b", "b", "c"]
    out = node_logs["gen"]["output_data"]
    assert (out["model"], out["fallback_used"]) == (_meta(c), True)


def _tool_call_turn(model: ResolvedModel, *, fallback_used: bool) -> ToolTurnResult:
    call = ToolCall(id=f"c-{uuid.uuid4().hex[:6]}", name="web_search", arguments={"query": "q"})
    raw = {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": '{"query": "q"}'},
    }
    return ToolTurnResult(
        tool_calls=[call],
        assistant_message={"role": "assistant", "content": None, "tool_calls": [raw]},
        model=model,
        fallback_used=fallback_used,
    )


def _final_turn(model: ResolvedModel) -> ToolTurnResult:
    return ToolTurnResult(
        content="끝", assistant_message={"role": "assistant", "content": "끝"}, model=model
    )


async def test_turns_without_fallback_keep_the_old_call_arguments(monkeypatch, node_logs):
    """폴백이 없으면 예전과 같은 인자로 부른다 — 고정 시그니처 대역(제품 테스트)을 지킨다."""
    a = _model("a")
    turns = [_tool_call_turn(a, fallback_used=False), _final_turn(a)]
    seen: list[Any] = []

    async def old_signature(
        db: Any, model_id: Any, messages: Any, tools: Any, max_tokens: int = 2048
    ) -> ToolTurnResult:
        seen.append(model_id)
        return turns[len(seen) - 1]

    monkeypatch.setattr(gateway, "complete_with_tools", old_signature)

    await _execute(TOOL_NODE)

    assert seen == [None, None]
    assert node_logs["gen"]["output_data"]["fallback_used"] is False


async def test_only_turns_after_a_fallback_name_the_preferred_model(monkeypatch, node_logs):
    a, b = _model("a"), _model("b")
    # b(폴백)가 도구를 부르고 → 1차 a가 도구를 부르고 → a가 답한다
    turns = [_tool_call_turn(b, fallback_used=True), _tool_call_turn(a, fallback_used=False),
             _final_turn(a)]
    seen: list[dict[str, Any]] = []

    async def fake(db: Any, model_id: Any, messages: Any, tools: Any, **kw: Any) -> ToolTurnResult:
        seen.append({k: v for k, v in kw.items() if k != "max_tokens"})
        return turns[len(seen) - 1]

    monkeypatch.setattr(gateway, "complete_with_tools", fake)

    await _execute(TOOL_NODE)

    assert seen == [{}, {"prefer_model_id": b.model.id}, {}]
    assert node_logs["gen"]["output_data"]["fallback_used"] is False


async def test_loop_exhaustion_wrap_up_prefers_the_kept_model(monkeypatch, node_logs):
    b = _model("b")
    asked: list[dict[str, Any]] = []

    async def always_tools(db: Any, model_id: Any, messages: Any, tools: Any, **kw: Any) -> Any:
        return _tool_call_turn(b, fallback_used=True)

    async def wrap_up(
        db: Any, model_id: Any, messages: Any, result: StreamResult, **kw: Any
    ) -> AsyncGenerator[str, None]:
        asked.append(kw)
        result.model, result.fallback_used = b, True

        async def gen() -> AsyncGenerator[str, None]:
            yield "마무리"

        return gen()

    monkeypatch.setattr(gateway, "complete_with_tools", always_tools)
    monkeypatch.setattr(gateway, "complete_stream_with_fallback", wrap_up)

    await _execute(TOOL_NODE)

    assert asked == [{"prefer_model_id": b.model.id, "max_tokens": 4096}]
    out = node_logs["gen"]["output_data"]
    assert (out["text"], out["model"], out["fallback_used"]) == ("마무리", _meta(b), True)


REACT_TOOL_NODE = {
    "id": "gen", "type": "react_agent", "goal": "검색해서 답해 주세요",
    "allowed_tools": ["web_search"], "max_iters": 2,
}


@pytest.mark.parametrize("base", [TOOL_NODE, REACT_TOOL_NODE], ids=["llm", "react_agent"])
@pytest.mark.parametrize("node_max_tokens", [None, 8192])
async def test_loop_exhaustion_wrap_up_keeps_the_node_max_tokens(
    monkeypatch, node_logs, base, node_max_tokens
):
    """루프를 다 쓴 뒤 마무리 호출도 도구 턴과 같은 `max_tokens`를 받는다(#398).

    빠뜨리면 게이트웨이 기본값(2048)으로 나가 노드에 4096 이상을 둔 답변이 잘린다.
    """
    a = _model("a")
    tool_turns: list[int] = []
    wrap_ups: list[int] = []

    async def always_tools(
        db: Any, model_id: Any, messages: Any, tools: Any, max_tokens: int = 2048, **kw: Any
    ) -> Any:
        tool_turns.append(max_tokens)
        return _tool_call_turn(a, fallback_used=False)

    async def wrap_up(
        db: Any, model_id: Any, messages: Any, result: StreamResult, max_tokens: int = 2048,
        **kw: Any
    ) -> AsyncGenerator[str, None]:
        wrap_ups.append(max_tokens)
        result.model = a

        async def gen() -> AsyncGenerator[str, None]:
            yield "마무리"

        return gen()

    monkeypatch.setattr(gateway, "complete_with_tools", always_tools)
    monkeypatch.setattr(gateway, "complete_stream_with_fallback", wrap_up)
    node = dict(base)
    if node_max_tokens is not None:
        node["max_tokens"] = node_max_tokens

    await _execute(node)

    expected = node_max_tokens or 4096
    assert tool_turns and set(tool_turns) == {expected}
    assert wrap_ups == [expected]
    assert node_logs["gen"]["output_data"]["text"] == "마무리"


# ── HITL 재개도 유지 모델부터(독립 리뷰 M1) ───────────────────────────────

K = approval.CheckpointKeys
PAY = {"url": "https://example.com/pay", "method": "POST"}
REACT_NODE = {
    "id": "react", "type": "react_agent", "goal": "목표", "allowed_tools": ["http_request"]
}


def _pay_turn(model: ResolvedModel, *, fallback_used: bool) -> ToolTurnResult:
    raw = {"id": "c1", "type": "function",
           "function": {"name": "http_request", "arguments": json.dumps(PAY)}}
    return ToolTurnResult(
        tool_calls=[ToolCall(id="c1", name="http_request", arguments=PAY)],
        assistant_message={"role": "assistant", "content": None, "tool_calls": [raw]},
        usage={"total_tokens": 10},
        model=model,
        fallback_used=fallback_used,
    )


@pytest.fixture
def hitl(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """react 노드의 실제 승인 게이트·재개. 모델 턴은 대본이고 받은 `prefer_model_id`를 모은다."""
    state: dict[str, Any] = {"script": [], "asked": [], "dispatched": 0}

    async def model(db: Any, model_id: Any, messages: Any, tools: Any, **kw: Any) -> Any:
        state["asked"].append(kw.get("prefer_model_id"))
        return state["script"].pop(0)

    async def dispatch(ctx: Any, name: str, args: dict[str, Any]) -> str:
        state["dispatched"] += 1
        return "done"

    async def noop(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(gateway, "complete_with_tools", model)
    monkeypatch.setattr(runtime, "_dispatch_builtin_tool", dispatch)
    for module in (loop, graph):
        monkeypatch.setattr(module, "_log_node_start", noop)
        monkeypatch.setattr(module, "_log_node_finish", noop)
    return state


def _react_ctx() -> RunContext:
    return RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={})  # type: ignore[arg-type]


async def _gate_then_resume(hitl: dict[str, Any], first: ToolTurnResult,
                            then: ToolTurnResult) -> tuple[dict[str, Any], dict[str, Any]]:
    """`first` 턴이 승인 대기를 만들고, 승인 뒤 워커처럼 재개해 `then` 턴으로 끝낸다."""
    hitl["script"] = [first]
    with pytest.raises(ApprovalRequired) as gate:
        await loop.run_react_node(_react_ctx(), REACT_NODE, {})
    checkpoint = copy.deepcopy(gate.value.checkpoint)
    checkpoint[K.REACT_APPROVAL_STATUS] = "approved"

    hitl["script"], hitl["asked"] = [then], []
    ctx = _react_ctx()
    approval.restore_run_context(ctx, checkpoint)
    out = await loop.run_react_node(ctx, {**REACT_NODE, "_resume": checkpoint}, {})
    return checkpoint, out


async def test_approval_after_a_fallback_turn_resumes_with_the_kept_model(hitl):
    b = _model("b")

    checkpoint, out = await _gate_then_resume(
        hitl, _pay_turn(b, fallback_used=True), _final_turn(b)
    )

    assert checkpoint[K.REACT_PREFER_MODEL_ID] == str(b.model.id)
    assert hitl["dispatched"] == 1  # 승인된 호출을 실행한 뒤
    assert hitl["asked"] == [b.model.id]  # 재개한 첫 턴도 b부터 시도한다
    assert out["model"] == _meta(b)


async def test_approval_without_a_fallback_keeps_the_checkpoint_shape(hitl):
    a = _model("a")

    checkpoint, _ = await _gate_then_resume(
        hitl, _pay_turn(a, fallback_used=False), _final_turn(a)
    )

    assert K.REACT_PREFER_MODEL_ID not in checkpoint
    assert hitl["asked"] == [None]  # 예전과 같은 인자로 부른다
