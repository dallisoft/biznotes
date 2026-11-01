"""게이트웨이 사고 양(`reasoning_effort`, #429) — 실제 LiteLLM + 로컬 대역 서버.

OpenAI 호환·Anthropic Messages·Gemini·Bedrock Converse 서버를 로컬 HTTP 서버로 띄우고 실제
LiteLLM으로 호출해 요청 본문을 고정한다. 외부 경계(원격 LLM 서버, 서킷 브레이커의 Redis, 모델
조회 DB)만 가짜다.
프로바이더 형식 변환은 LiteLLM이 하므로 모델 맵은 잠근 버전의 번들 맵으로 고정한다
(`test_gateway_sampling_params`와 같다).

대역 서버가 거부하는 요청은 실제 API의 규칙을 흉내 낸 것이다.
- Anthropic: 사고 예산이 `max_tokens` 이상이면 400. 사고가 켜진 요청에서 도구 호출을 낸 직전
  assistant 메시지가 사고 블록으로 시작하지 않으면 400(공개 문서의 규칙). 사고가 켜진 요청의
  temperature가 1이 아니면 400(LiteLLM pass-through 주석의 규칙 — adaptive까지 같게 본다). Bedrock
  Converse의 Claude도 같다. 사고가 꺼진 요청의 assistant 메시지에 사고 블록이 있으면 400(LiteLLM
  `utils.any_assistant_message_has_thinking_blocks` 주석의 규칙).
- OpenAI 호환: 메시지의 모르는 키를 거부한다(가정 — 서버마다 다르다).
- Gemini: 자기가 발급하지 않은 사고 서명을 거부한다(가정).
"""

import json
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
from structlog.testing import capture_logs

from biznotes_core.core.crypto import encrypt
from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.services.agents import runtime
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.react import loop
from biznotes_core.services.gateway import circuit
from biznotes_core.services.gateway import llm as gateway
from biznotes_core.services.gateway.llm import (
    GatewayError,
    ResolvedModel,
    StreamResult,
    complete_stream,
    complete_stream_with_fallback,
    complete_with_tools,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "노트 검색",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }
]
MESSAGES = [{"role": "user", "content": "노트를 찾아 줘"}]
# 사고를 켜는 요청 키(프로바이더 형식). 설정이 없으면 하나도 실리지 않아야 한다
_REASONING_KEYS = {"reasoning_effort", "thinking", "output_config"}
# 게이트웨이 내부 값·LiteLLM 옵션이 요청 본문으로 새면 안 된다
_LEAK_MARKERS = ("_thinking_model_id", "allowed_openai_params", "drop_params")
# 대역 Anthropic이 낸 사고 블록의 서명 — 다른 프로바이더 요청에 실리면 안 된다
ANTHROPIC_SIGNATURE = "sig-anthropic-1"
# OpenAI 호환 서버가 받는 메시지 키(대역의 가정)
_OPENAI_MESSAGE_KEYS = {"role", "content", "name", "tool_calls", "tool_call_id"}


def _anthropic_has_tool_result(messages: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_result" for b in m["content"])
        for m in messages
    )


_THINKING_TEMPERATURE_ERROR = "temperature may only be set to 1 when thinking is enabled"


def _anthropic_thinking_error(body: dict[str, Any]) -> str | None:
    thinking = body.get("thinking")
    if not isinstance(thinking, dict):
        has_blocks = any(
            m["role"] == "assistant" and isinstance(m["content"], list)
            and any(b.get("type") in ("thinking", "redacted_thinking") for b in m["content"])
            for m in body["messages"]
        )  # fmt: skip
        if has_blocks:
            return "When thinking is disabled, an assistant message cannot contain thinking"
        return None
    if body.get("temperature", 1) != 1:
        return _THINKING_TEMPERATURE_ERROR
    budget = thinking.get("budget_tokens")
    if isinstance(budget, int) and budget >= body["max_tokens"]:
        return "`max_tokens` must be greater than `thinking.budget_tokens`"
    tool_turns = [
        m for m in body["messages"]
        if m["role"] == "assistant" and isinstance(m["content"], list)
        and any(b.get("type") == "tool_use" for b in m["content"])
    ]  # fmt: skip
    if tool_turns and tool_turns[-1]["content"][0].get("type") not in (
        "thinking",
        "redacted_thinking",
    ):
        return "Expected `thinking` or `redacted_thinking`, but found `tool_use`"
    return None


def _anthropic_stream(model: str) -> str:
    message = {
        "id": "m1", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "usage": {"input_tokens": 3, "output_tokens": 0},
    }  # fmt: skip
    events = [
        ("message_start", {"type": "message_start", "message": message}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "pong"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                           "usage": {"output_tokens": 1}}),
        ("message_stop", {"type": "message_stop"}),
    ]  # fmt: skip
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)


def _openai_stream(model: str) -> str:
    base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": model}
    chunks = [
        {**base, "choices": [{"index": 0, "finish_reason": None,
                              "delta": {"role": "assistant", "content": "pong"}}]},
        {**base, "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]},
        {**base, "choices": [],
         "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}},
    ]  # fmt: skip
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


_GEMINI_USAGE = {"promptTokenCount": 3, "candidatesTokenCount": 1, "totalTokenCount": 4}


def _gemini_reply(parts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}],
        "usageMetadata": _GEMINI_USAGE,
    }


def _converse_error(body: dict[str, Any]) -> str | None:
    thinking = (body.get("additionalModelRequestFields") or {}).get("thinking")
    if isinstance(thinking, dict) and body.get("inferenceConfig", {}).get("temperature", 1) != 1:
        return _THINKING_TEMPERATURE_ERROR
    return None


class _Server:
    """Anthropic `/v1/messages`, OpenAI 호환 `/v1/chat/completions`, Gemini `/models/*`,
    Bedrock `/model/{id}/converse`."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        # 참이면 Anthropic이 요청을 거부한다(404) — 다음 후보로의 폴백을 만든다
        self.anthropic_down = False
        # 이 모델(요청 본문의 `model`)만 거부한다(404)
        self.down_models: set[str] = set()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                path = urlsplit(self.path).path
                server.requests.append((path, body))
                if path == "/v1/messages":
                    self._anthropic(body)
                elif path == "/v1/chat/completions":
                    self._openai(body)
                elif path.startswith("/models/"):
                    self._gemini(path, body)
                elif path.startswith("/model/") and path.endswith("/converse"):
                    self._converse(body)
                else:
                    self._json(404, {"error": {"message": "Not Found"}})

            def _anthropic(self, body: dict[str, Any]) -> None:
                if server.anthropic_down or body["model"] in server.down_models:
                    self._json(404, {"type": "error", "error": {
                        "type": "not_found_error", "message": "model not found"}})
                    return
                error = _anthropic_thinking_error(body)
                if error is not None:
                    self._json(400, {"type": "error", "error": {
                        "type": "invalid_request_error", "message": error}})
                    return
                model = body["model"]
                if body.get("stream"):
                    self._sse(_anthropic_stream(model))
                    return
                if _anthropic_has_tool_result(body["messages"]):
                    content: list[dict[str, Any]] = [{"type": "text", "text": "찾았습니다"}]
                    stop = "end_turn"
                else:
                    content = [{"type": "tool_use", "id": "toolu_1", "name": "search",
                                "input": {"q": "노트"}}]  # fmt: skip
                    stop = "tool_use"
                    if body.get("thinking"):
                        content.insert(0, {"type": "thinking", "thinking": "검색이 필요하다",
                                           "signature": ANTHROPIC_SIGNATURE})  # fmt: skip
                self._json(200, {"id": "m1", "type": "message", "role": "assistant",
                                 "model": model, "content": content, "stop_reason": stop,
                                 "usage": {"input_tokens": 3, "output_tokens": 5}})  # fmt: skip

            def _openai(self, body: dict[str, Any]) -> None:
                unknown = sorted(
                    {k for m in body["messages"] for k in m} - _OPENAI_MESSAGE_KEYS
                )
                if unknown:
                    self._json(400, {"error": {
                        "message": f"Additional properties are not allowed ({unknown})",
                        "type": "invalid_request_error"}})
                    return
                model = body["model"]
                if body.get("stream"):
                    self._sse(_openai_stream(model))
                    return
                if any(m["role"] == "tool" for m in body["messages"]):
                    message: dict[str, Any] = {"role": "assistant", "content": "찾았습니다"}
                    finish = "stop"
                else:
                    message = {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {
                            "name": "search", "arguments": json.dumps({"q": "노트"})}}]}
                    finish = "tool_calls"
                self._json(200, {"id": "c1", "object": "chat.completion", "created": 0,
                                 "model": model, "choices": [{"index": 0, "message": message,
                                                              "finish_reason": finish}],
                                 "usage": {"prompt_tokens": 3, "completion_tokens": 1,
                                           "total_tokens": 4}})  # fmt: skip

            def _gemini(self, path: str, body: dict[str, Any]) -> None:
                if ANTHROPIC_SIGNATURE in json.dumps(body):
                    self._json(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                               "message": "Thought signature is not valid"}})
                    return
                answered = any(
                    "functionResponse" in p or "function_response" in p
                    for c in body.get("contents", []) for p in c.get("parts", [])
                )
                if answered or not body.get("tools"):
                    reply = _gemini_reply([{"text": "pong"}])
                else:
                    reply = _gemini_reply(
                        [{"functionCall": {"name": "search", "args": {"q": "노트"}}}]
                    )
                if path.endswith(":streamGenerateContent"):
                    self._sse(f"data: {json.dumps(reply)}\n\n")
                else:
                    self._json(200, reply)

            def _converse(self, body: dict[str, Any]) -> None:
                error = _converse_error(body)
                if error is not None:
                    self._json(400, {"message": error})
                    return
                self._json(200, {
                    "output": {"message": {"role": "assistant", "content": [{"toolUse": {
                        "toolUseId": "tooluse_1", "name": "search", "input": {"q": "노트"}}}]}},
                    "stopReason": "tool_use",
                    "usage": {"inputTokens": 3, "outputTokens": 1, "totalTokens": 4},
                    "metrics": {"latencyMs": 1}})  # fmt: skip

            def _json(self, code: int, payload: dict[str, Any]) -> None:
                self._send(code, "application/json", json.dumps(payload).encode())

            def _sse(self, text: str) -> None:
                self._send(200, "text/event-stream", text.encode())

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
        threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [body for _, body in self.requests]

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def server() -> Iterator[_Server]:
    srv = _Server()
    yield srv
    srv.close()


@pytest.fixture(autouse=True)
def _bundled_model_map() -> Iterator[None]:
    import litellm
    from litellm import utils as litellm_utils

    bundled = json.loads(
        resources.files("litellm")
        .joinpath("model_prices_and_context_window_backup.json")
        .read_text()
    )
    bundled.pop("fallback_generalizations", None)  # LiteLLM이 적재할 때 빼는 의사 항목
    original = litellm.model_cost
    litellm.model_cost = bundled
    litellm_utils._invalidate_model_cost_lowercase_map()
    try:
        yield
    finally:
        litellm.model_cost = original
        litellm_utils._invalidate_model_cost_lowercase_map()


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch) -> list[ResolvedModel]:
    """모델 조회 DB·서킷 대역 — 테스트가 후보 목록을 채운다."""
    models: list[ResolvedModel] = []

    async def fake_chain(db: Any, model_id: Any, modality: str = "llm") -> list[ResolvedModel]:
        return list(models)

    async def closed(provider_id: uuid.UUID) -> bool:
        return False

    monkeypatch.setattr(gateway, "resolve_chain", fake_chain)
    monkeypatch.setattr(circuit, "is_open", closed)
    monkeypatch.setattr(circuit, "record_failure", closed)
    monkeypatch.setattr(circuit, "record_success", closed)
    return models


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bedrock 서명용 가짜 자격 증명. 요청은 base_url의 로컬 서버로만 간다."""
    for key in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_BEARER_TOKEN_BEDROCK"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setenv("AWS_REGION_NAME", "us-east-1")


def _resolved(server: _Server, provider_type: str, model_name: str) -> ResolvedModel:
    # anthropic·google·bedrock은 api_base 뒤에 경로를 붙이고, OpenAI SDK는 `/chat/completions`만
    # 붙인다
    rooted = provider_type in ("anthropic", "google", "bedrock")
    provider = LLMProvider(
        id=uuid.uuid4(),
        name=f"p-{provider_type}-{model_name}",
        type=provider_type,
        base_url=server.root if rooted else f"{server.root}/v1",
        # bedrock은 키 대신 AWS 자격 증명(aws_env)으로 서명한다
        api_key_encrypted=None if provider_type == "bedrock" else encrypt("test-secret"),
    )
    model = LLMModel(
        id=uuid.uuid4(),
        provider_id=provider.id,
        model_name=model_name,
        display_name=model_name,
        capabilities=["llm"],
    )
    return ResolvedModel(model=model, provider=provider)


async def _stream(
    resolved: ResolvedModel, messages: list[dict[str, Any]] = MESSAGES, **kw: Any
) -> str:
    gen = await complete_stream(resolved, messages, StreamResult(), **kw)
    return "".join([d async for d in gen])


def _assert_no_leak(body: dict[str, Any]) -> None:
    raw = json.dumps(body, ensure_ascii=False)
    assert not [marker for marker in _LEAK_MARKERS if marker in raw]


def _gemini_thinking(body: dict[str, Any]) -> Any:
    return body.get("generationConfig", {}).get("thinkingConfig")


HAIKU = ("anthropic", "anthropic/claude-haiku-4-5")
SONNET_ADAPTIVE = ("anthropic", "anthropic/claude-sonnet-4-6")
GEMINI = ("google", "gemini/gemini-2.5-flash")
OPENAI_COMPAT = ("custom", "openai/gpt-oss-120b")


# ── 회귀: 설정이 없으면 요청이 지금과 같다 ───────────────────────────────


@pytest.mark.parametrize("path", ["stream", "tool"])
@pytest.mark.parametrize(
    ("provider_type", "model_name"),
    [
        pytest.param(*OPENAI_COMPAT, id="openai-compatible"),
        pytest.param(*HAIKU, id="anthropic-budget"),
        pytest.param(*SONNET_ADAPTIVE, id="anthropic-adaptive"),
        pytest.param(*GEMINI, id="gemini"),
    ],
)
async def test_no_effort_sends_no_reasoning_keys(
    server: _Server, chain: list[ResolvedModel], path: str, provider_type: str, model_name: str
) -> None:
    resolved = _resolved(server, provider_type, model_name)
    chain.append(resolved)

    if path == "stream":
        assert await _stream(resolved) == "pong"
    else:
        turn = await complete_with_tools(None, None, MESSAGES, TOOLS)  # type: ignore[arg-type]
        assert [c.name for c in turn.tool_calls] == ["search"]
        assert "thinking_blocks" not in turn.assistant_message

    [body] = server.bodies
    assert not _REASONING_KEYS & body.keys()
    assert _gemini_thinking(body) is None
    _assert_no_leak(body)


@pytest.mark.parametrize("path", ["stream", "tool"])
async def test_no_effort_keeps_the_openai_compatible_request_keys(
    server: _Server, chain: list[ResolvedModel], path: str
) -> None:
    """요청 본문의 키 집합을 고정한다 — 사고 양을 더하기 전과 같다."""
    resolved = _resolved(server, *OPENAI_COMPAT)
    chain.append(resolved)

    if path == "stream":
        await _stream(resolved)
        expected = {"model", "messages", "temperature", "max_tokens", "stream", "stream_options"}
    else:
        await complete_with_tools(None, None, MESSAGES, TOOLS)  # type: ignore[arg-type]
        expected = {"model", "messages", "temperature", "max_tokens", "tools", "tool_choice"}

    [body] = server.bodies
    assert body.keys() == expected


# ── 지원 모델에 실린다 ───────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["stream", "tool"])
async def test_openai_compatible_model_carries_effort(
    server: _Server, chain: list[ResolvedModel], path: str
) -> None:
    resolved = _resolved(server, *OPENAI_COMPAT)
    chain.append(resolved)

    if path == "stream":
        assert await _stream(resolved, reasoning_effort="low") == "pong"
    else:
        await complete_with_tools(None, None, MESSAGES, TOOLS, reasoning_effort="low")  # type: ignore[arg-type]

    [body] = server.bodies
    assert body["reasoning_effort"] == "low"
    _assert_no_leak(body)


@pytest.mark.parametrize("path", ["stream", "tool"])
async def test_anthropic_adaptive_model_carries_effort(
    server: _Server, chain: list[ResolvedModel], path: str
) -> None:
    resolved = _resolved(server, *SONNET_ADAPTIVE)
    chain.append(resolved)

    if path == "stream":
        assert await _stream(resolved, reasoning_effort="medium") == "pong"
    else:
        await complete_with_tools(None, None, MESSAGES, TOOLS, reasoning_effort="medium")  # type: ignore[arg-type]

    [body] = server.bodies
    assert body["thinking"]["type"] == "adaptive"
    assert body["output_config"] == {"effort": "medium"}
    assert "reasoning_effort" not in body
    _assert_no_leak(body)


@pytest.mark.parametrize("path", ["stream", "tool"])
async def test_gemini_model_carries_a_thinking_budget(
    server: _Server, chain: list[ResolvedModel], path: str
) -> None:
    resolved = _resolved(server, *GEMINI)
    chain.append(resolved)

    if path == "stream":
        assert await _stream(resolved, reasoning_effort="low", max_tokens=8192) == "pong"
    else:
        await complete_with_tools(  # type: ignore[arg-type]
            None, None, MESSAGES, TOOLS, max_tokens=8192, reasoning_effort="low"
        )

    [body] = server.bodies
    assert _gemini_thinking(body)["thinkingBudget"] == 1024
    _assert_no_leak(body)


# ── 미지원 모델에서는 빠지고 호출이 성공한다 ─────────────────────────────


@pytest.mark.parametrize("path", ["stream", "tool"])
@pytest.mark.parametrize(
    ("provider_type", "model_name"),
    [
        pytest.param("openai", "gpt-4o", id="gpt-4o"),
        pytest.param("anthropic", "anthropic/claude-3-5-sonnet-20240620", id="claude-3-5"),
        pytest.param("lmstudio", "lm_studio/local-model", id="lm-studio"),
    ],
)
async def test_unsupported_model_drops_effort_and_answers(
    server: _Server, chain: list[ResolvedModel], path: str, provider_type: str, model_name: str
) -> None:
    # tool 경로는 `allowed_openai_params=["tools"]`를 쓴다 — 사고 양을 그 목록에 넣으면 미지원
    # 모델에도 강제로 실린다
    resolved = _resolved(server, provider_type, model_name)
    chain.append(resolved)

    if path == "stream":
        assert await _stream(resolved, reasoning_effort="high") == "pong"
    else:
        turn = await complete_with_tools(  # type: ignore[arg-type]
            None, None, MESSAGES, TOOLS, reasoning_effort="high"
        )
        assert [c.name for c in turn.tool_calls] == ["search"]

    [body] = server.bodies
    assert not _REASONING_KEYS & body.keys()
    _assert_no_leak(body)


# ── 사고 예산이 출력 상한 이상이면 낮추거나 뺀다(D3 ①) ───────────────────


@pytest.mark.parametrize("path", ["stream", "tool"])
@pytest.mark.parametrize(
    ("requested", "max_tokens", "budget", "effective"),
    [
        # 예산이 상한보다 작으면 그대로 — 경고하지 않는다
        pytest.param("high", 8192, 4096, "high", id="fits"),
        pytest.param("high", 4096, 2048, "medium", id="high-to-medium"),
        pytest.param("high", 2048, 1024, "low", id="high-to-low"),
        pytest.param("medium", 2048, 1024, "low", id="medium-to-low"),
        # 가장 낮은 단계도 넘치면 뺀다
        pytest.param("low", 1024, None, None, id="dropped"),
    ],
)
async def test_anthropic_budget_is_lowered_below_max_tokens(
    server: _Server,
    chain: list[ResolvedModel],
    path: str,
    requested: str,
    max_tokens: int,
    budget: int | None,
    effective: str | None,
) -> None:
    resolved = _resolved(server, *HAIKU)
    chain.append(resolved)

    with capture_logs() as logs:
        if path == "stream":
            text = await _stream(resolved, reasoning_effort=requested, max_tokens=max_tokens)
            assert text == "pong"
        else:
            turn = await complete_with_tools(  # type: ignore[arg-type]
                None, None, MESSAGES, TOOLS, max_tokens=max_tokens, reasoning_effort=requested
            )
            assert [c.name for c in turn.tool_calls] == ["search"]

    [body] = server.bodies  # 대역은 예산 ≥ 상한을 400으로 거부한다 — 한 번에 성공했다
    assert body["max_tokens"] == max_tokens
    if budget is None:
        assert "thinking" not in body
    else:
        assert body["thinking"] == {"type": "enabled", "budget_tokens": budget}
    adjusted = [e for e in logs if e["event"] == "reasoning_effort_adjusted"]
    if effective == requested:
        assert adjusted == []
    else:
        assert adjusted == [
            {
                "event": "reasoning_effort_adjusted",
                "log_level": "warning",
                "model": "anthropic/claude-haiku-4-5",
                "requested": requested,
                "effective": effective,
                "max_tokens": max_tokens,
                "reason": "budget_exceeds_max_tokens",
            }
        ]


async def test_gemini_budget_is_lowered_below_max_tokens(
    server: _Server, chain: list[ResolvedModel]
) -> None:
    """Gemini의 사고 예산(`thinkingBudget`)도 출력 상한에 들어간다 — 같은 규칙으로 낮춘다."""
    resolved = _resolved(server, *GEMINI)

    assert await _stream(resolved, reasoning_effort="high", max_tokens=2048) == "pong"

    [body] = server.bodies
    assert _gemini_thinking(body)["thinkingBudget"] == 1024


# ── 사고가 켜진 tool 2턴째(D3 ②) ─────────────────────────────────────────


def _off_logs(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """사고 블록 없는 도구 턴 때문에 사고를 끈 경고."""
    return [
        e for e in logs
        if e["event"] == "reasoning_effort_adjusted" and e["reason"] == "tool_turn_without_thinking"
    ]  # fmt: skip


def _second_turn(first: gateway.ToolTurnResult) -> list[dict[str, Any]]:
    """1턴 결과로 2턴째 대화를 만든다 — HITL checkpoint처럼 JSON으로 한 번 오간다."""
    messages = [*MESSAGES, first.assistant_message]
    messages.append(
        {"role": "tool", "tool_call_id": first.tool_calls[0].id, "content": "노트 3건"}
    )
    return json.loads(json.dumps(messages, ensure_ascii=False))


async def test_tool_second_turn_carries_the_thinking_blocks(
    server: _Server, chain: list[ResolvedModel]
) -> None:
    chain.append(_resolved(server, *HAIKU))

    first = await complete_with_tools(  # type: ignore[arg-type]
        None, None, MESSAGES, TOOLS, max_tokens=4096, reasoning_effort="low"
    )
    assert first.assistant_message["thinking_blocks"] == [
        {"type": "thinking", "thinking": "검색이 필요하다", "signature": ANTHROPIC_SIGNATURE}
    ]
    with capture_logs() as logs:
        second = await complete_with_tools(  # type: ignore[arg-type]
            None, None, _second_turn(first), TOOLS, max_tokens=4096, reasoning_effort="low"
        )

    assert second.content == "찾았습니다"
    body = server.bodies[-1]
    # 블록이 제대로 있는 같은 모델 2턴째는 사고를 그대로 켠다
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert _off_logs(logs) == []
    assistant = next(m for m in body["messages"] if m["role"] == "assistant")
    assert assistant["content"][0] == {
        "type": "thinking", "thinking": "검색이 필요하다", "signature": ANTHROPIC_SIGNATURE
    }
    assert assistant["content"][1]["type"] == "tool_use"
    for body in server.bodies:
        _assert_no_leak(body)


async def test_react_closing_stream_carries_the_thinking_blocks(
    server: _Server, chain: list[ResolvedModel]
) -> None:
    """루프 소진 마무리 스트림 — 같은 대화(사고 블록 포함)를 도구 없이 스트리밍한다."""
    haiku = _resolved(server, *HAIKU)
    chain.append(haiku)
    first = await complete_with_tools(  # type: ignore[arg-type]
        None, None, MESSAGES, TOOLS, max_tokens=4096, reasoning_effort="low"
    )
    result = StreamResult()

    gen = await complete_stream_with_fallback(  # type: ignore[arg-type]
        None, None, _second_turn(first), result, max_tokens=4096, reasoning_effort="low"
    )

    assert "".join([d async for d in gen]) == "pong"
    assert result.model is haiku
    body = server.bodies[-1]
    assert body["stream"] is True
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assistant = next(m for m in body["messages"] if m["role"] == "assistant")
    assert assistant["content"][0]["signature"] == ANTHROPIC_SIGNATURE
    _assert_no_leak(body)


@pytest.mark.parametrize("path", ["tool", "stream"])
@pytest.mark.parametrize(
    ("provider_type", "model_name"),
    [pytest.param(*OPENAI_COMPAT, id="openai-compatible"), pytest.param(*GEMINI, id="gemini")],
)
async def test_fallback_to_another_provider_leaves_the_thinking_blocks_out(
    server: _Server,
    chain: list[ResolvedModel],
    path: str,
    provider_type: str,
    model_name: str,
) -> None:
    haiku, backup = _resolved(server, *HAIKU), _resolved(server, provider_type, model_name)
    chain.extend([haiku, backup])
    first = await complete_with_tools(  # type: ignore[arg-type]
        None, None, MESSAGES, TOOLS, max_tokens=4096, reasoning_effort="low"
    )
    assert first.assistant_message.get("thinking_blocks")
    server.anthropic_down = True
    messages = _second_turn(first)

    if path == "tool":
        second = await complete_with_tools(  # type: ignore[arg-type]
            None, None, messages, TOOLS, max_tokens=4096, reasoning_effort="low"
        )
        answered, fallback = second.model, second.fallback_used
    else:
        result = StreamResult()
        gen = await complete_stream_with_fallback(  # type: ignore[arg-type]
            None, None, messages, result, max_tokens=4096, reasoning_effort="low"
        )
        assert "".join([d async for d in gen])
        answered, fallback = result.model, result.fallback_used

    assert (answered, fallback) == (backup, True)
    path_sent, body = server.requests[-1]
    assert not path_sent.startswith("/v1/messages")
    assert ANTHROPIC_SIGNATURE not in json.dumps(body)
    _assert_no_leak(body)
    # 다음 턴에 원래 모델로 돌아와도 그 모델이 만든 사고 블록은 그대로 실린다
    assert messages[1]["thinking_blocks"][0]["signature"] == ANTHROPIC_SIGNATURE


async def test_another_anthropic_model_does_not_get_foreign_thinking_blocks(
    server: _Server, chain: list[ResolvedModel]
) -> None:
    """서명은 그 블록을 만든 모델만 검증한다 — 같은 프로바이더의 다른 모델에도 보내지 않는다."""
    haiku, sonnet = _resolved(server, *HAIKU), _resolved(server, *SONNET_ADAPTIVE)
    chain.append(haiku)
    first = await complete_with_tools(  # type: ignore[arg-type]
        None, None, MESSAGES, TOOLS, max_tokens=4096, reasoning_effort="low"
    )

    text = await _stream(sonnet, _second_turn(first), max_tokens=4096)

    assert text == "pong"
    body = server.bodies[-1]
    assert body["model"] == "claude-sonnet-4-6"
    assert ANTHROPIC_SIGNATURE not in json.dumps(body)
    _assert_no_leak(body)


async def test_untagged_caller_messages_are_sent_as_given(
    server: _Server, chain: list[ResolvedModel]
) -> None:
    """호출자가 직접 만든 메시지(게이트웨이 표시 없음)는 손대지 않는다 — 지금 동작과 같다."""
    haiku = _resolved(server, *HAIKU)
    messages = [
        *MESSAGES,
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "toolu_1", "type": "function",
                            "function": {"name": "search", "arguments": "{}"}}],
            "thinking_blocks": [{"type": "thinking", "thinking": "직접",
                                 "signature": ANTHROPIC_SIGNATURE}],
        },  # fmt: skip
        {"role": "tool", "tool_call_id": "toolu_1", "content": "결과"},
    ]

    assert await _stream(haiku, messages, max_tokens=4096, reasoning_effort="low") == "pong"

    assert ANTHROPIC_SIGNATURE in json.dumps(server.bodies[-1])


# ── 허용 밖 값은 호출 전에 거부한다 ───────────────────────────────────────


@pytest.mark.parametrize("value", ["xhigh", "minimal", "none", "LOW", ""])
@pytest.mark.parametrize("path", ["stream", "fallback", "tool"])
async def test_unknown_effort_is_rejected_before_any_request(
    server: _Server, chain: list[ResolvedModel], path: str, value: str
) -> None:
    resolved = _resolved(server, *OPENAI_COMPAT)
    chain.append(resolved)

    with pytest.raises(GatewayError, match="reasoning_effort"):
        if path == "stream":
            await _stream(resolved, reasoning_effort=value)
        elif path == "fallback":
            await complete_stream_with_fallback(  # type: ignore[arg-type]
                None, None, MESSAGES, StreamResult(), reasoning_effort=value
            )
        else:
            await complete_with_tools(None, None, MESSAGES, TOOLS, reasoning_effort=value)  # type: ignore[arg-type]

    assert server.requests == []


async def test_mock_provider_ignores_the_effort(chain: list[ResolvedModel]) -> None:
    provider = LLMProvider(id=uuid.uuid4(), name="p-mock", type="mock")
    mock = ResolvedModel(
        model=LLMModel(id=uuid.uuid4(), provider_id=provider.id, model_name="mock",
                       display_name="mock", capabilities=["llm"]),
        provider=provider,
    )  # fmt: skip
    chain.append(mock)

    text = await _stream(mock, reasoning_effort="high")
    turn = await complete_with_tools(None, None, MESSAGES, TOOLS, reasoning_effort="high")  # type: ignore[arg-type]

    assert text
    assert [c.name for c in turn.tool_calls] == ["search"]
    assert "thinking_blocks" not in turn.assistant_message


# ── Anthropic 사고가 켜진 호출의 temperature ──────────────────────────────

BEDROCK_HAIKU = ("bedrock", "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0")
BEDROCK_SONNET_ADAPTIVE = ("bedrock", "bedrock/us.anthropic.claude-sonnet-4-6")


def _temperature_logs(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == "llm_temperature_dropped_for_thinking"]


@pytest.mark.parametrize("path", ["stream", "tool"])
@pytest.mark.parametrize(
    ("provider_type", "model_name", "thinking_type"),
    [
        pytest.param(*HAIKU, "enabled", id="anthropic-budget"),
        pytest.param(*SONNET_ADAPTIVE, "adaptive", id="anthropic-adaptive"),
    ],
)
async def test_anthropic_thinking_call_leaves_the_temperature_out(
    server: _Server,
    chain: list[ResolvedModel],
    path: str,
    provider_type: str,
    model_name: str,
    thinking_type: str,
) -> None:
    resolved = _resolved(server, provider_type, model_name)
    chain.append(resolved)

    with capture_logs() as logs:
        if path == "stream":
            text = await _stream(resolved, reasoning_effort="low", max_tokens=8192)
            assert text == "pong"
        else:
            turn = await complete_with_tools(  # type: ignore[arg-type]
                None, None, MESSAGES, TOOLS, max_tokens=8192, reasoning_effort="low"
            )
            assert [c.name for c in turn.tool_calls] == ["search"]

    [body] = server.bodies  # 대역은 사고 + temperature≠1을 400으로 거부한다 — 한 번에 성공했다
    assert body["thinking"]["type"] == thinking_type
    assert "temperature" not in body
    assert _temperature_logs(logs) == [
        {
            "event": "llm_temperature_dropped_for_thinking",
            "log_level": "info",
            "model": model_name,
            "temperature": 0.2,
            "reasoning_effort": "low",
        }
    ]


@pytest.mark.parametrize(
    ("provider_type", "model_name", "thinking_type"),
    [
        pytest.param(*BEDROCK_HAIKU, "enabled", id="bedrock-budget"),
        pytest.param(*BEDROCK_SONNET_ADAPTIVE, "adaptive", id="bedrock-adaptive"),
    ],
)
async def test_bedrock_claude_thinking_call_leaves_the_temperature_out(
    server: _Server,
    chain: list[ResolvedModel],
    aws_env: None,
    provider_type: str,
    model_name: str,
    thinking_type: str,
) -> None:
    """Bedrock Converse(tool 턴) — 스트림은 AWS 이벤트 스트림 형식이라 대역을 두지 않는다."""
    chain.append(_resolved(server, provider_type, model_name))

    turn = await complete_with_tools(  # type: ignore[arg-type]
        None, None, MESSAGES, TOOLS, max_tokens=8192, reasoning_effort="low"
    )

    assert [c.name for c in turn.tool_calls] == ["search"]
    [(path, body)] = server.requests
    assert unquote(path).endswith(model_name.removeprefix("bedrock/") + "/converse")
    assert body["additionalModelRequestFields"]["thinking"]["type"] == thinking_type
    assert "temperature" not in body["inferenceConfig"]
    assert body["inferenceConfig"]["maxTokens"] == 8192


@pytest.mark.parametrize("path", ["stream", "tool"])
@pytest.mark.parametrize(
    ("provider_type", "model_name"),
    [pytest.param(*HAIKU, id="anthropic-budget"),
     pytest.param(*SONNET_ADAPTIVE, id="anthropic-adaptive")],
)  # fmt: skip
async def test_anthropic_call_without_effort_keeps_the_temperature(
    server: _Server, chain: list[ResolvedModel], path: str, provider_type: str, model_name: str
) -> None:
    resolved = _resolved(server, provider_type, model_name)
    chain.append(resolved)

    with capture_logs() as logs:
        if path == "stream":
            await _stream(resolved)
        else:
            await complete_with_tools(None, None, MESSAGES, TOOLS)  # type: ignore[arg-type]

    [body] = server.bodies
    assert body["temperature"] == 0.2
    assert "thinking" not in body
    assert _temperature_logs(logs) == []


async def test_bedrock_call_without_effort_keeps_the_temperature(
    server: _Server, chain: list[ResolvedModel], aws_env: None
) -> None:
    chain.append(_resolved(server, *BEDROCK_HAIKU))

    await complete_with_tools(None, None, MESSAGES, TOOLS)  # type: ignore[arg-type]

    [body] = server.bodies
    assert body["inferenceConfig"]["temperature"] == 0.2
    assert "additionalModelRequestFields" not in body


@pytest.mark.parametrize("path", ["stream", "tool"])
async def test_temperature_stays_when_the_thinking_was_dropped(
    server: _Server, chain: list[ResolvedModel], path: str
) -> None:
    """출력 상한이 작아 사고 양을 뺀 호출(D3 ①)은 사고가 꺼져 있다 — temperature를 그대로 둔다."""
    resolved = _resolved(server, *HAIKU)
    chain.append(resolved)

    if path == "stream":
        await _stream(resolved, reasoning_effort="low", max_tokens=1024)
    else:
        await complete_with_tools(  # type: ignore[arg-type]
            None, None, MESSAGES, TOOLS, max_tokens=1024, reasoning_effort="low"
        )

    [body] = server.bodies
    assert "thinking" not in body
    assert body["temperature"] == 0.2


@pytest.mark.parametrize("path", ["stream", "tool"])
@pytest.mark.parametrize(
    ("provider_type", "model_name", "temperature"),
    [
        pytest.param(*GEMINI, 0.2, id="gemini"),
        pytest.param(*OPENAI_COMPAT, 0.2, id="openai-compatible"),
        # 같은 `thinking` 키를 다른 형식으로 쓰는 프로바이더 — Claude가 아니라 그대로다
        pytest.param("custom", "deepseek/deepseek-chat", 0.2, id="deepseek"),
        # LiteLLM이 이미 temperature를 빼는 모델 — 지금과 같다
        pytest.param("openai", "gpt-5", None, id="gpt-5"),
        pytest.param("anthropic", "anthropic/claude-sonnet-5-5", None, id="claude-sonnet-5-5"),
    ],
)
async def test_other_models_keep_todays_temperature(
    server: _Server,
    chain: list[ResolvedModel],
    path: str,
    provider_type: str,
    model_name: str,
    temperature: float | None,
) -> None:
    resolved = _resolved(server, provider_type, model_name)
    chain.append(resolved)

    with capture_logs() as logs:
        if path == "stream":
            await _stream(resolved, reasoning_effort="low", max_tokens=8192)
        else:
            await complete_with_tools(  # type: ignore[arg-type]
                None, None, MESSAGES, TOOLS, max_tokens=8192, reasoning_effort="low"
            )

    [body] = server.bodies
    sent = body.get("generationConfig", body).get("temperature")
    assert sent == temperature
    assert _temperature_logs(logs) == []


# ── 사고 블록 없는 도구 턴이 있으면 Anthropic 사고를 켜지 않는다(#429 리뷰) ────

# 필수 초기 도구처럼 게이트웨이 밖에서 만든 도구 호출 턴 — 사고 블록이 없다
UNTHOUGHT_TOOL_TURN = [
    *MESSAGES,
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "toolu_0", "type": "function",
         "function": {"name": "search", "arguments": json.dumps({"q": "노트"})}}]},
    {"role": "tool", "tool_call_id": "toolu_0", "content": "노트 3건"},
]  # fmt: skip


@pytest.mark.parametrize("path", ["stream", "tool"])
@pytest.mark.parametrize(
    ("provider_type", "model_name"),
    [pytest.param(*HAIKU, id="anthropic-budget"),
     pytest.param(*SONNET_ADAPTIVE, id="anthropic-adaptive")],
)  # fmt: skip
async def test_tool_turn_without_thinking_leaves_the_thinking_off(
    server: _Server, chain: list[ResolvedModel], path: str, provider_type: str, model_name: str
) -> None:
    resolved = _resolved(server, provider_type, model_name)
    chain.append(resolved)

    with capture_logs() as logs:
        if path == "stream":
            assert await _stream(
                resolved, UNTHOUGHT_TOOL_TURN, reasoning_effort="low", max_tokens=4096
            ) == "pong"
        else:
            turn = await complete_with_tools(  # type: ignore[arg-type]
                None, None, UNTHOUGHT_TOOL_TURN, TOOLS, max_tokens=4096, reasoning_effort="low"
            )
            assert turn.content == "찾았습니다"

    [body] = server.bodies  # 대역은 사고 + 블록 없는 tool_use를 400으로 거부한다
    assert not {"thinking", "output_config"} & body.keys()
    assert body["temperature"] == 0.2  # 사고가 꺼졌으니 temperature도 그대로다
    assert _off_logs(logs) == [
        {
            "event": "reasoning_effort_adjusted",
            "log_level": "warning",
            "model": model_name,
            "requested": "low",
            "effective": None,
            "max_tokens": 4096,
            "reason": "tool_turn_without_thinking",
        }
    ]


async def test_react_with_required_initial_tools_and_effort_answers(
    server: _Server, chain: list[ResolvedModel], monkeypatch: pytest.MonkeyPatch
) -> None:
    """필수 초기 도구가 사고 블록 없는 도구 호출 턴을 먼저 넣는다 — 실제 react 루프로 돌린다."""
    chain.append(_resolved(server, *HAIKU))

    async def dispatch(ctx: RunContext, name: str, args: dict[str, Any]) -> str:
        return "노트 3건"

    async def quiet(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(runtime, "_dispatch_builtin_tool", dispatch)
    monkeypatch.setattr(loop, "_log_node_start", quiet)
    monkeypatch.setattr(loop, "_log_node_finish", quiet)
    node = {
        "id": "r", "type": "react_agent", "goal": "노트를 찾아 줘", "allowed_tools": ["rag_search"],
        "required_initial_tools": ["rag_search"], "reasoning_effort": "low", "max_tokens": 4096,
    }  # fmt: skip
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={})  # type: ignore[arg-type]

    out = await loop.run_react_node(ctx, node)

    assert out["text"] == "찾았습니다"
    [body] = server.bodies
    assert "thinking" not in body
    assert [b["type"] for b in body["messages"][1]["content"]] == ["tool_use"]


@pytest.mark.parametrize("path", ["tool", "stream"])
async def test_fallback_to_a_budget_thinking_model_leaves_the_thinking_off(
    server: _Server, chain: list[ResolvedModel], path: str
) -> None:
    """Sonnet의 도구 호출 뒤 Haiku로 폴백 — 두 번째 후보에도 사고 양을 준다."""
    sonnet, haiku = _resolved(server, *SONNET_ADAPTIVE), _resolved(server, *HAIKU)
    chain.extend([sonnet, haiku])
    first = await complete_with_tools(  # type: ignore[arg-type]
        None, None, MESSAGES, TOOLS, max_tokens=4096, reasoning_effort="low"
    )
    assert first.model is sonnet and first.assistant_message["thinking_blocks"]
    server.down_models.add("claude-sonnet-4-6")
    messages = _second_turn(first)

    with capture_logs() as logs:
        if path == "tool":
            second = await complete_with_tools(  # type: ignore[arg-type]
                None, None, messages, TOOLS, max_tokens=4096, reasoning_effort="low"
            )
            answered, fallback = second.model, second.fallback_used
        else:
            result = StreamResult()
            gen = await complete_stream_with_fallback(  # type: ignore[arg-type]
                None, None, messages, result, max_tokens=4096, reasoning_effort="low"
            )
            assert "".join([d async for d in gen]) == "pong"
            answered, fallback = result.model, result.fallback_used

    assert (answered, fallback) == (haiku, True)
    body = server.bodies[-1]
    assert body["model"] == "claude-haiku-4-5"
    assert "thinking" not in body
    assert ANTHROPIC_SIGNATURE not in json.dumps(body)
    _assert_no_leak(body)
    assert [e["model"] for e in _off_logs(logs)] == ["anthropic/claude-haiku-4-5"]


async def test_leaving_the_thinking_off_also_drops_the_models_own_blocks(
    server: _Server, chain: list[ResolvedModel]
) -> None:
    """같은 모델의 블록이 있어도 블록 없는 도구 턴이 끼면 사고를 끄고, 그 블록도 뺀다.

    사고가 꺼진 요청에 사고 블록이 남으면 Anthropic이 거부한다(대역도 거부한다).
    """
    haiku = _resolved(server, *HAIKU)
    chain.append(haiku)
    first = await complete_with_tools(  # type: ignore[arg-type]
        None, None, MESSAGES, TOOLS, max_tokens=4096, reasoning_effort="low"
    )
    messages = [*_second_turn(first), *UNTHOUGHT_TOOL_TURN[1:]]

    text = await _stream(haiku, messages, reasoning_effort="low", max_tokens=4096)

    assert text == "pong"
    body = server.bodies[-1]
    assert "thinking" not in body
    assert ANTHROPIC_SIGNATURE not in json.dumps(body)
    _assert_no_leak(body)
    # 호출자의 목록은 바꾸지 않는다 — 다음 호출이 사고를 켤 수 있으면 블록을 다시 쓴다
    assert messages[1]["thinking_blocks"][0]["signature"] == ANTHROPIC_SIGNATURE
