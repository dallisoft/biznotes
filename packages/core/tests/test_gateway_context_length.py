"""컨텍스트 초과 400의 게이트웨이 처리(#358).

OpenAI 호환 서버를 로컬 HTTP 서버로 띄우고 실제 LiteLLM으로 호출한다 — 판별이 LiteLLM 예외
모양에 기대므로 가짜 예외로는 고정할 수 없다(vLLM `max_num_tokens`는 `ContextWindowExceededError`가
아니라 일반 `BadRequestError`로 온다). 서킷 브레이커의 Redis와 모델 조회 DB만 가짜다. 5xx·429의
재시도·서킷 경로는 `complete_stream` 대역으로 시도 횟수만 본다(LiteLLM·OpenAI SDK 내부 재시도의
백오프를 기다리지 않으려고). 대역 예외에는 실제 LiteLLM 예외처럼 HTTP 응답을 사슬에 단다 — 재시도·
서킷은 그 상태로 나눈다(#380). 영구 요청 오류(400·404)와 401은 실제 서버 응답으로 두 경로를 본다.
"""

import json
import threading
import uuid
from collections.abc import AsyncGenerator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from biznotes_core.core.crypto import encrypt
from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.services.agents import graph
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError
from biznotes_core.services.agents.node_errors import describe_node_failure
from biznotes_core.services.gateway import circuit
from biznotes_core.services.gateway import llm as gateway
from biznotes_core.services.gateway.llm import (
    GatewayCallError,
    GatewayContextLengthError,
    ResolvedModel,
    StreamResult,
    complete_stream,
    complete_stream_with_fallback,
    complete_with_tools,
    is_context_length_error,
)

MESSAGES = [{"role": "user", "content": "긴 원문"}]
CONTEXT_FIXED = "입력이 모델 컨텍스트 한도를 넘었습니다"
VLLM_MAX_NUM_TOKENS = {
    "object": "error",
    "message": "The sum of prompt length (80428.0), query length (0) should not exceed "
    "max_num_tokens (32768)",
    "type": "BadRequestError",
    "param": None,
    "code": 400,
}
# 서버 경로(첫 마디)마다 응답 — 상태 코드와 본문
RESPONSES: dict[str, tuple[int, dict[str, Any]]] = {
    "vllm": (400, VLLM_MAX_NUM_TOKENS),
    "vllm-long": (400, {
        "object": "error",
        "message": "This model's maximum context length is 32768 tokens. However, you "
        "requested 40000 tokens (31808 in the messages, 8192 in the completion).",
        "type": "BadRequestError", "param": None, "code": 400}),
    "openai": (400, {"error": {
        "message": "This model's maximum context length is 8192 tokens. However, your "
        "messages resulted in 9000 tokens.",
        "type": "invalid_request_error", "param": "messages",
        "code": "context_length_exceeded"}}),
    "bad-param": (400, {"error": {
        "message": "Unsupported value: 'temperature' does not support 0.2 with this model.",
        "type": "invalid_request_error", "param": "temperature", "code": "unsupported_value"}}),
    # 영구 요청 오류(#380) — 도구를 받지 않는 모델, 잘못된 모델 이름. 그리고 키 거부
    "no-tools": (400, {"error": {
        "message": "This model does not support tools.",
        "type": "invalid_request_error", "param": "tools", "code": None}}),
    "missing": (404, {"error": {
        "message": "The model `gpt-oss-120b` does not exist.",
        "type": "invalid_request_error", "param": None, "code": "model_not_found"}}),
    "bad-key": (401, {"error": {
        "message": "Incorrect API key provided.",
        "type": "invalid_request_error", "param": None, "code": "invalid_api_key"}}),
}
# 첫 조각 전에 SSE로 오는 오류(#380) — HTTP 200 안에 실려 사슬에 httpx 응답이 없다
SSE_ERRORS = {
    "anthropic-overloaded": 'event: error\ndata: {"type": "error", "error": '
    '{"type": "overloaded_error", "message": "Overloaded"}}\n\n',
    "openai-overloaded": 'data: {"error": {"message": "server overloaded", '
    '"type": "server_error", "code": 503}}\n\n',
}


def _chunks(model: str) -> str:
    base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": model}
    chunks = [
        {**base, "choices": [{"index": 0, "finish_reason": None,
                              "delta": {"role": "assistant", "content": "pong"}}]},
        {**base, "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]},
        {**base, "choices": [],
         "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}},
    ]
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


class _Server:
    """`/<경로>/v1/chat/completions` — RESPONSES의 응답, `ok`는 스트리밍 성공."""

    def __init__(self) -> None:
        self.hits: dict[str, int] = {}
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                key = self.path.strip("/").split("/", 1)[0]
                server.hits[key] = server.hits.get(key, 0) + 1
                if key == "ok":
                    self._send(200, "text/event-stream", _chunks(body["model"]).encode())
                    return
                if key in SSE_ERRORS:
                    self._send(200, "text/event-stream", SSE_ERRORS[key].encode())
                    return
                code, payload = RESPONSES[key]
                self._send(code, "application/json", json.dumps(payload).encode())

            def _send(self, code: int, content_type: str, raw: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args: Any) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.root = f"http://127.0.0.1:{self._httpd.server_port}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def server() -> Iterator[_Server]:
    srv = _Server()
    yield srv
    srv.close()


def _resolved(server: _Server, key: str, model_name: str = "openai/gpt-oss-120b") -> ResolvedModel:
    provider = LLMProvider(
        id=uuid.uuid4(),
        name=f"p-{key}",
        type="vllm",
        base_url=f"{server.root}/{key}/v1",
        api_key_encrypted=encrypt("test-secret"),
    )
    model = LLMModel(
        id=uuid.uuid4(),
        provider_id=provider.id,
        model_name=model_name,
        display_name=f"모델-{key}",
        capabilities=["llm"],
    )
    return ResolvedModel(model=model, provider=provider)


class _Circuit:
    """서킷 브레이커의 메모리 대역 — 임계·차단 규칙은 실제 모듈 상수를 쓴다."""

    def __init__(self) -> None:
        self.fails: dict[uuid.UUID, int] = {}
        self.opened: set[uuid.UUID] = set()
        self.failures: list[uuid.UUID] = []

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
def breaker(monkeypatch: pytest.MonkeyPatch) -> _Circuit:
    fake = _Circuit()
    monkeypatch.setattr(circuit, "is_open", fake.is_open)
    monkeypatch.setattr(circuit, "record_failure", fake.record_failure)
    monkeypatch.setattr(circuit, "record_success", fake.record_success)
    return fake


def _use_chain(monkeypatch: pytest.MonkeyPatch, *chain: ResolvedModel) -> None:
    async def fake_chain(db: Any, model_id: Any, modality: str = "llm") -> list[ResolvedModel]:
        return list(chain)

    monkeypatch.setattr(gateway, "resolve_chain", fake_chain)


async def _stream(result: StreamResult | None = None) -> str:
    gen = await complete_stream_with_fallback(
        None, None, MESSAGES, result or StreamResult()  # type: ignore[arg-type]
    )
    return "".join([d async for d in gen])


# ── 판별: 실제 LiteLLM 예외 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "model_name"),
    [
        ("vllm", "openai/gpt-oss-120b"),  # 이슈 재현 — 일반 BadRequestError로 온다
        ("vllm", "hosted_vllm/gpt-oss-120b"),
        ("vllm-long", "openai/gpt-oss-120b"),
        ("openai", "openai/gpt-oss-120b"),  # LiteLLM이 ContextWindowExceededError로 매핑
    ],
)
async def test_context_rejections_are_classified(server, key, model_name):
    gen = await complete_stream(_resolved(server, key, model_name), MESSAGES, StreamResult())
    with pytest.raises(GatewayContextLengthError) as raised:
        await anext(gen.__aiter__())

    assert isinstance(raised.value, GatewayCallError)  # 기존 처리도 그대로 잡는다
    failure = describe_node_failure(raised.value)
    assert (failure.code, failure.message) == ("context_length_exceeded", CONTEXT_FIXED)
    assert failure.message != GatewayCallError.node_log_message


async def test_other_bad_requests_stay_model_call_failed(server):
    gen = await complete_stream(_resolved(server, "bad-param"), MESSAGES, StreamResult())
    with pytest.raises(GatewayCallError) as raised:
        await anext(gen.__aiter__())

    assert not isinstance(raised.value, GatewayContextLengthError)
    assert describe_node_failure(raised.value).code == "model_call_failed"


async def test_tool_turn_classifies_context_rejection(server, monkeypatch, breaker):
    _use_chain(monkeypatch, _resolved(server, "vllm"))
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    with pytest.raises(GatewayContextLengthError):
        await complete_with_tools(None, None, MESSAGES, tools)  # type: ignore[arg-type]

    assert server.hits == {"vllm": 1}  # 같은 후보에 다시 보내지 않는다
    assert breaker.failures == []


class _Status(Exception):
    def __init__(self, status_code: int | None, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_Status(400, "prompt is too long: 210000 tokens > 200000 maximum"), True),
        (_Status(413, "Input is too long for requested model."), True),
        (_Status(400, "The input token count (9) exceeds the maximum number of tokens"), True),
        # 문구가 같아도 거부(400·413)가 아니면 일시 오류 경로다
        (_Status(429, "max_num_tokens budget exhausted"), False),
        (_Status(500, "maximum context length"), False),
        (_Status(None, "maximum context length"), False),
        (_Status(400, "messages must not be empty"), False),
    ],
)
def test_message_patterns_need_a_rejection_status(exc, expected):
    assert is_context_length_error(exc) is expected


def test_status_is_read_through_the_cause_chain():
    try:
        try:
            raise _Status(400, "max_num_tokens (32768)")
        except _Status as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert is_context_length_error(outer)


# ── 폴백 체인 ─────────────────────────────────────────────────────────────


async def test_context_rejection_is_tried_once_and_falls_back(server, monkeypatch, breaker):
    small, big = _resolved(server, "vllm"), _resolved(server, "ok")
    _use_chain(monkeypatch, small, big)
    result = StreamResult()

    assert await _stream(result) == "pong"

    assert server.hits == {"vllm": 1, "ok": 1}  # 같은 후보에 다시 보내지 않는다
    assert (result.model, result.fallback_used) == (big, True)
    assert breaker.failures == []  # 제공자 실패로 세지 않는다


async def test_repeated_context_rejections_never_open_the_circuit(server, monkeypatch, breaker):
    small = _resolved(server, "vllm")
    _use_chain(monkeypatch, small)

    for _ in range(circuit.FAIL_THRESHOLD + 1):
        with pytest.raises(GatewayContextLengthError) as raised:
            await _stream()
        assert describe_node_failure(raised.value).code == "context_length_exceeded"

    assert server.hits == {"vllm": circuit.FAIL_THRESHOLD + 1}
    assert breaker.failures == []
    assert not await circuit.is_open(small.provider.id)


def _scripted(monkeypatch: pytest.MonkeyPatch, plan: dict[str, list[Exception | None]]) -> dict:
    """후보(표시 이름)마다 시도별 결과를 정한 `complete_stream` 대역. None은 성공 스트림."""
    calls: dict[str, int] = {}

    async def fake(candidate: ResolvedModel, *a: Any, **k: Any) -> AsyncGenerator[str, None]:
        name = candidate.model.display_name
        attempt = calls.get(name, 0)
        calls[name] = attempt + 1
        outcome = plan[name][min(attempt, len(plan[name]) - 1)]
        if outcome is not None:
            raise outcome

        async def ok() -> AsyncGenerator[str, None]:
            yield "pong"

        return ok()

    monkeypatch.setattr(gateway, "complete_stream", fake)
    return calls


def _named(name: str) -> ResolvedModel:
    provider = LLMProvider(id=uuid.uuid4(), name=f"p-{name}", type="vllm")
    model = LLMModel(id=uuid.uuid4(), provider_id=provider.id, model_name=name,
                     display_name=name, capabilities=["llm"])
    return ResolvedModel(model=model, provider=provider)


def _call_error(status: int) -> GatewayCallError:
    """`complete_stream`이 내는 모양 — 실제 LiteLLM 예외처럼 HTTP 응답을 사슬에 단다(#380)."""
    request = httpx.Request("POST", "https://provider.test/v1/chat/completions")
    error = GatewayCallError(f"LLM 호출 실패: {status}")
    error.__cause__ = httpx.HTTPStatusError(
        str(status), request=request, response=httpx.Response(status, request=request)
    )
    return error


def _timeout_error() -> GatewayCallError:
    error = GatewayCallError("LLM 호출 실패: timed out")
    error.__cause__ = httpx.ReadTimeout("timed out")
    return error


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(_call_error(500), id="500"),
        pytest.param(_call_error(429), id="429"),
        pytest.param(_call_error(503), id="503"),
        pytest.param(_timeout_error(), id="timeout"),
    ],
)
async def test_transient_failures_keep_retry_and_circuit(monkeypatch, breaker, error):
    a = _named("a")
    _use_chain(monkeypatch, a)
    calls = _scripted(monkeypatch, {"a": [error]})

    for _ in range(circuit.FAIL_THRESHOLD):
        with pytest.raises(GatewayCallError) as raised:
            await _stream()
        assert not isinstance(raised.value, GatewayContextLengthError)

    assert calls == {"a": 2 * circuit.FAIL_THRESHOLD}  # 후보마다 백오프 1회 재시도
    assert breaker.failures == [a.provider.id] * circuit.FAIL_THRESHOLD
    assert await circuit.is_open(a.provider.id)


async def test_mixed_failures_are_not_reported_as_context(monkeypatch, breaker):
    a, b = _named("a"), _named("b")
    _use_chain(monkeypatch, a, b)
    calls = _scripted(
        monkeypatch, {"a": [GatewayContextLengthError("ctx")], "b": [_call_error(500)]}
    )

    with pytest.raises(GatewayCallError) as raised:
        await _stream()

    assert not isinstance(raised.value, GatewayContextLengthError)
    assert describe_node_failure(raised.value).code == "model_call_failed"
    assert calls == {"a": 1, "b": 2}
    assert breaker.failures == [b.provider.id]


async def test_open_circuit_candidate_does_not_hide_the_context_cause(monkeypatch, breaker):
    a, b = _named("a"), _named("b")
    breaker.opened.add(b.provider.id)
    _use_chain(monkeypatch, a, b)
    _scripted(monkeypatch, {"a": [GatewayContextLengthError("ctx")], "b": [None]})

    with pytest.raises(GatewayContextLengthError):
        await _stream()


# ── 영구 요청 오류·키 거부: 실제 서버 응답, 스트리밍·tool 두 경로(#380) ──────────

TOOLS = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]


def _mock_model(name: str = "mock") -> ResolvedModel:
    provider = LLMProvider(id=uuid.uuid4(), name=f"p-{name}", type="mock")
    model = LLMModel(id=uuid.uuid4(), provider_id=provider.id, model_name=name,
                     display_name=name, capabilities=["llm"])
    return ResolvedModel(model=model, provider=provider)


async def _call(path: str) -> ResolvedModel | None:
    """경로 하나로 1회 호출하고 응답한 모델을 돌려준다."""
    if path == "tool":
        turn = await complete_with_tools(None, None, MESSAGES, TOOLS)  # type: ignore[arg-type]
        return turn.model
    result = StreamResult()
    assert await _stream(result)
    return result.model


@pytest.mark.parametrize("path", ["stream", "tool"])
@pytest.mark.parametrize("key", ["no-tools", "missing"])
async def test_permanent_rejections_never_open_the_circuit(
    server, monkeypatch, breaker, path, key
):
    """도구를 받지 않는 모델의 400·잘못된 모델 이름의 404가 반복돼도 서킷이 열리지 않고 다음
    후보가 받는다 — 같은 후보에 다시 보내지 않는다."""
    rejecting, fallback = _resolved(server, key), _mock_model()
    _use_chain(monkeypatch, rejecting, fallback)

    for _ in range(circuit.FAIL_THRESHOLD + 1):
        assert await _call(path) == fallback

    assert server.hits == {key: circuit.FAIL_THRESHOLD + 1}
    assert breaker.failures == []
    assert not await circuit.is_open(rejecting.provider.id)


@pytest.mark.parametrize("path", ["stream", "tool"])
async def test_permanent_only_failure_reports_model_call_failed(server, monkeypatch, breaker, path):
    _use_chain(monkeypatch, _resolved(server, "no-tools"))

    with pytest.raises(GatewayCallError) as raised:
        await _call(path)

    assert not isinstance(raised.value, GatewayContextLengthError)
    assert describe_node_failure(raised.value).code == "model_call_failed"
    assert breaker.failures == []


@pytest.mark.parametrize("path", ["stream", "tool"])
async def test_auth_rejection_is_counted_but_not_retried(server, monkeypatch, breaker, path):
    """401·403은 키·권한 문제 — 다시 보내지 않지만, 고칠 때까지 막도록 서킷에는 센다(이슈 권고)."""
    rejecting, fallback = _resolved(server, "bad-key"), _mock_model()
    _use_chain(monkeypatch, rejecting, fallback)

    assert await _call(path) == fallback

    assert server.hits == {"bad-key": 1}
    assert breaker.failures == [rejecting.provider.id]


@pytest.mark.parametrize(
    ("key", "model_name"),
    [
        ("anthropic-overloaded", "anthropic/claude-x"),
        ("openai-overloaded", "openai/gpt-oss-120b"),
    ],
)
async def test_sse_error_before_first_chunk_keeps_retry_and_circuit(
    server, monkeypatch, breaker, key, model_name
):
    """HTTP 응답이 없는 첫 조각 전 오류는 LiteLLM `status_code`(500)로 보조 분류해 일시 오류다 —
    지금처럼 재시도하고 서킷에 센다(수락 기준 2)."""
    overloaded, fallback = _resolved(server, key, model_name), _mock_model()
    _use_chain(monkeypatch, overloaded, fallback)
    result = StreamResult()

    assert await _stream(result)

    assert result.model == fallback
    assert server.hits == {key: 2}  # 백오프 1회 재시도
    assert breaker.failures == [overloaded.provider.id]


# ── 노드 실행 로그 ─────────────────────────────────────────────────────────


async def test_llm_node_logs_context_length_exceeded(server, monkeypatch, breaker):
    _use_chain(monkeypatch, _resolved(server, "vllm"))
    finished: dict[str, Any] = {}

    async def log_start(run_id: Any, node_id: str, *a: Any, **k: Any) -> str:
        return node_id

    async def log_finish(log_id: str, **kw: Any) -> bool:
        finished[log_id] = kw
        return True

    monkeypatch.setattr(graph, "_log_node_start", log_start)
    monkeypatch.setattr(graph, "_log_node_finish", log_finish)
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={})  # type: ignore[arg-type]
    definition = {"nodes": [{"id": "gen", "type": "llm", "prompt": "요약"}], "edges": []}

    with pytest.raises(AgentRuntimeError) as raised:
        await graph.execute_agent(ctx, "workflow", definition)

    log = finished["gen"]
    assert (log["status"], log["error_code"], log["error_message"]) == (
        "failed", "context_length_exceeded", CONTEXT_FIXED
    )
    assert str(raised.value) == f"노드 [gen] 실행 실패: {CONTEXT_FIXED}"
    assert server.hits == {"vllm": 1}
