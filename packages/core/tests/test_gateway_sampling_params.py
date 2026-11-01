"""temperature를 받지 않는 모델의 게이트웨이 호출 테스트 (#224).

Anthropic Messages·OpenAI 호환·Bedrock Converse 서버를 로컬 HTTP 서버로 띄우고 실제
LiteLLM으로 호출한다. 외부 경계(원격 LLM 서버, 서킷 브레이커의 Redis, 모델 조회 DB)만
가짜이고, 게이트웨이 파라미터 조립과 LiteLLM 경로는 실경로다.

모델별 temperature 지원 여부는 LiteLLM 모델 맵이 정한다. LiteLLM은 임포트할 때 원격 맵을
받아 오므로, 이 모듈은 잠근 버전에 든 번들 맵으로 고정한다. 원격 맵을 받지 못하는 배포와
같은 조건이고, 하한(`litellm>=1.105.1`)을 낮추면 실패한다. 한계: o 시리즈 판별은 임포트
시점에 맵에서 만든 OpenAI 모델 목록도 보므로 번들 맵으로 고정되지 않는다(o3는 두 맵에 다 있다).

가짜 서버가 temperature에 400을 내는 분기는 실제 API를 흉내 낸 것이다. LiteLLM 맵이 그
모델을 알면 요청 전에 걸러져 실행되지 않고, 맵이 모르는 경우(예: litellm 1.100.1)에만 실행된다.
"""

import json
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import unquote

import pytest

from biznotes_core.core.crypto import encrypt
from biznotes_core.models import LLMModel, LLMProvider
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

# 실제 API가 temperature(≠1)를 거부하는 모델
_ANTHROPIC_NO_SAMPLING = {"claude-sonnet-5-5"}
_OPENAI_NO_SAMPLING = ("o3", "gpt-5")
# Bedrock: 도구를 못 쓰는 모델과, 도구는 쓰지만 toolChoice를 거부하는 모델
# (LiteLLM이 각각 tools·tool_choice를 지원하지 않는다고 표시한다). 이 모델들이 도구
# 정의를 거부한다는 것은 가정이다 — 실계정으로 확인하지 않았다. 테스트가 고정하는 것은
# 도구 정의가 요청에 실린다는 점이다.
_BEDROCK_NO_TOOLS = {"meta.llama3-8b-instruct-v1:0", "mistral.mistral-7b-instruct-v0:2"}
_BEDROCK_NO_TOOL_CHOICE = {"meta.llama3-1-70b-instruct-v1:0"}
# 게이트웨이가 LiteLLM에 주는 옵션이 요청 본문으로 새면 안 된다
_LITELLM_OPTIONS = {"drop_params", "allowed_openai_params", "extra_body"}

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
MESSAGES = [{"role": "user", "content": "ping"}]


def _anthropic_events(model: str) -> list[tuple[str, dict[str, Any]]]:
    message = {
        "id": "m1", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "usage": {"input_tokens": 3, "output_tokens": 0},
    }
    return [
        ("message_start", {"type": "message_start", "message": message}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "pong"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                           "usage": {"output_tokens": 1}}),
        ("message_stop", {"type": "message_stop"}),
    ]


def _openai_chunks(model: str) -> list[dict[str, Any]]:
    base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": model}
    return [
        {**base, "choices": [{"index": 0, "finish_reason": None,
                              "delta": {"role": "assistant", "content": "pong"}}]},
        {**base, "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]},
        {**base, "choices": [],
         "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}},
    ]


def _converse(content: dict[str, Any], stop_reason: str) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [content]}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 3, "outputTokens": 1, "totalTokens": 4},
        "metrics": {"latencyMs": 1},
    }


class _Server:
    """Anthropic `/v1/messages`, OpenAI 호환 `/v1/chat/completions`,
    Bedrock `/model/{id}/converse`를 흉내 낸다."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                server.bodies.append(body)
                if self.path == "/v1/messages":
                    self._anthropic(body)
                elif self.path == "/v1/chat/completions":
                    self._openai(body)
                elif self.path.startswith("/model/") and self.path.endswith("/converse"):
                    self._bedrock(unquote(self.path[len("/model/"):-len("/converse")]), body)
                elif self.path.startswith("/model/") and self.path.endswith("/invoke"):
                    self._bedrock_invoke(body)
                else:
                    self._json(404, {"error": {"message": "Not Found"}})

            def _anthropic(self, body: dict[str, Any]) -> None:
                model = body["model"]
                if model in _ANTHROPIC_NO_SAMPLING and body.get("temperature", 1) != 1:
                    self._json(400, {"type": "error", "error": {
                        "type": "invalid_request_error",
                        "message": "`temperature` is deprecated for this model"}})
                elif body.get("stream"):
                    self._sse("".join(
                        f"event: {e}\ndata: {json.dumps(d)}\n\n"
                        for e, d in _anthropic_events(model)
                    ))
                elif body.get("tools"):
                    self._json(200, {
                        "id": "m1", "type": "message", "role": "assistant", "model": model,
                        "content": [{"type": "tool_use", "id": "toolu_1", "name": "search",
                                     "input": {"q": "노트"}}],
                        "stop_reason": "tool_use",
                        "usage": {"input_tokens": 3, "output_tokens": 1}})
                else:
                    self._json(404, {"error": {"message": "unexpected request"}})

            def _openai(self, body: dict[str, Any]) -> None:
                model = body["model"]
                if model.startswith(_OPENAI_NO_SAMPLING) and body.get("temperature", 1) != 1:
                    self._json(400, {"error": {
                        "message": "Unsupported value: 'temperature' does not support "
                                   "0.2 with this model. Only the default (1) value is "
                                   "supported.",
                        "type": "invalid_request_error", "param": "temperature",
                        "code": "unsupported_value"}})
                elif body.get("stream"):
                    self._sse("".join(f"data: {json.dumps(c)}\n\n" for c in _openai_chunks(model))
                              + "data: [DONE]\n\n")
                elif body.get("tools"):
                    call = {"id": "call_1", "type": "function",
                            "function": {"name": "search",
                                         "arguments": json.dumps({"q": "노트"})}}
                    self._json(200, {
                        "id": "c1", "object": "chat.completion", "created": 0, "model": model,
                        "choices": [{"index": 0, "finish_reason": "tool_calls",
                                     "message": {"role": "assistant", "content": None,
                                                 "tool_calls": [call]}}],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 1,
                                  "total_tokens": 4}})
                else:
                    self._json(404, {"error": {"message": "unexpected request"}})

            def _bedrock(self, model_id: str, body: dict[str, Any]) -> None:
                tool_config = body.get("toolConfig")
                if tool_config and model_id in _BEDROCK_NO_TOOLS:
                    self._json(400, {"message": "This model doesn't support tool use."})
                elif tool_config and "toolChoice" in tool_config and (
                    model_id in _BEDROCK_NO_TOOL_CHOICE
                ):
                    self._json(400, {"message": "This model doesn't support the "
                                                "toolConfig.toolChoice.auto field."})
                elif tool_config:
                    self._json(200, _converse({"toolUse": {
                        "toolUseId": "tooluse_1", "name": "search", "input": {"q": "노트"}}},
                        "tool_use"))
                else:
                    # 도구 정의가 빠진 요청 — 모델은 도구 없이 답한다
                    self._json(200, _converse({"text": "도구 없이 답함"}, "end_turn"))

            def _bedrock_invoke(self, body: dict[str, Any]) -> None:
                # 모델 고유 본문 경로. LiteLLM은 OpenAI 형식 tools를 본문에 그대로 싣는다
                if "tools" in body:
                    self._json(400, {"message": "Malformed input request: #: extraneous "
                                                "key [tools] is not permitted"})
                else:
                    self._json(200, {"outputs": [{"text": "도구 없이 답함",
                                                  "stop_reason": "stop"}]})

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
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

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
    # 맵에서 파생된 조회 캐시도 비운다. 비공개 API지만 LiteLLM이 맵을 바꿀 때 부르는 함수다
    litellm.model_cost = bundled
    litellm_utils._invalidate_model_cost_lowercase_map()
    try:
        yield
    finally:
        litellm.model_cost = original
        litellm_utils._invalidate_model_cost_lowercase_map()


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bedrock 서명용 가짜 자격 증명. 요청은 base_url의 로컬 서버로만 간다."""
    for key in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_BEARER_TOKEN_BEDROCK"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setenv("AWS_REGION_NAME", "us-east-1")


def _resolved(server: _Server, provider_type: str, model_name: str) -> ResolvedModel:
    # anthropic·bedrock은 api_base 뒤에 경로를 붙이고, OpenAI SDK는 `/chat/completions`만 붙인다
    base_url = server.root if provider_type in ("anthropic", "bedrock") else f"{server.root}/v1"
    provider = LLMProvider(
        id=uuid.uuid4(),
        name=f"p-{provider_type}",
        type=provider_type,
        base_url=base_url,
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


async def _stream_text(resolved: ResolvedModel, result: StreamResult) -> str:
    gen = await complete_stream(resolved, MESSAGES, result)
    return "".join([delta async for delta in gen])


async def _tool_turn(
    monkeypatch: pytest.MonkeyPatch, resolved: ResolvedModel
) -> gateway.ToolTurnResult:
    # tool 경로도 폴백 체인·서킷을 탄다(#374) — 후보 하나짜리 체인, 서킷은 닫힌 대역
    async def fake_chain(db: Any, model_id: Any, modality: str = "llm") -> list[ResolvedModel]:
        return [resolved]

    async def closed(provider_id: uuid.UUID) -> bool:
        return False

    async def noop(provider_id: uuid.UUID) -> bool:
        return False

    monkeypatch.setattr(gateway, "resolve_chain", fake_chain)
    monkeypatch.setattr(circuit, "is_open", closed)
    monkeypatch.setattr(circuit, "record_failure", noop)
    monkeypatch.setattr(circuit, "record_success", noop)
    return await complete_with_tools(None, None, MESSAGES, TOOLS)  # type: ignore[arg-type]


NO_SAMPLING = [
    pytest.param("anthropic", "anthropic/claude-sonnet-5-5", id="claude-sonnet-5-5"),
    # 탐색은 openai 타입을 접두사 없이 등록한다
    pytest.param("openai", "o3", id="o3"),
]
SAMPLING = [
    pytest.param("anthropic", "anthropic/claude-sonnet-4-6", id="claude-sonnet-4-6"),
    pytest.param("custom", "openai/local-model", id="openai-compatible"),
]


# ── 수락 기준: temperature 미지원 모델은 temperature 없이 호출된다 ─────────


@pytest.mark.parametrize(("provider_type", "model_name"), NO_SAMPLING)
async def test_stream_omits_temperature_for_model_without_sampling(
    server: _Server, provider_type: str, model_name: str
) -> None:
    result = StreamResult()

    text = await _stream_text(_resolved(server, provider_type, model_name), result)

    assert text == "pong"
    assert result.usage["total_tokens"] == 4
    [body] = server.bodies
    assert "temperature" not in body
    assert body["stream"] is True
    assert not _LITELLM_OPTIONS & body.keys()


@pytest.mark.parametrize(("provider_type", "model_name"), NO_SAMPLING)
async def test_tool_turn_omits_temperature_for_model_without_sampling(
    server: _Server, monkeypatch: pytest.MonkeyPatch, provider_type: str, model_name: str
) -> None:
    res = await _tool_turn(monkeypatch, _resolved(server, provider_type, model_name))

    assert [(c.name, c.arguments) for c in res.tool_calls] == [("search", {"q": "노트"})]
    [body] = server.bodies
    assert "temperature" not in body
    assert body["tools"]
    assert not _LITELLM_OPTIONS & body.keys()


async def test_chat_fallback_chain_answers_without_failing_provider(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 이슈 재현 경로: 기본 채팅이 폴백 후보 없이 이 모델 하나로 스트리밍한다
    resolved = _resolved(server, "anthropic", "anthropic/claude-sonnet-5-5")
    failures: list[uuid.UUID] = []

    async def fake_chain(db: Any, model_id: Any, modality: str = "llm") -> list[ResolvedModel]:
        return [resolved]

    async def is_open(provider_id: uuid.UUID) -> bool:
        return False

    async def record_failure(provider_id: uuid.UUID) -> bool:
        failures.append(provider_id)
        return False

    async def record_success(provider_id: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(gateway, "resolve_chain", fake_chain)
    monkeypatch.setattr(circuit, "is_open", is_open)
    monkeypatch.setattr(circuit, "record_failure", record_failure)
    monkeypatch.setattr(circuit, "record_success", record_success)
    result = StreamResult()

    stream = await complete_stream_with_fallback(None, None, MESSAGES, result)  # type: ignore[arg-type]
    text = "".join([delta async for delta in stream])

    assert text == "pong"
    assert result.model is resolved
    assert result.fallback_used is False
    assert failures == []
    assert all("temperature" not in body for body in server.bodies)


# ── 회귀: temperature를 받는 모델은 그대로 넘긴다 ─────────────────────────


@pytest.mark.parametrize(("provider_type", "model_name"), SAMPLING)
async def test_stream_keeps_temperature_for_sampling_model(
    server: _Server, provider_type: str, model_name: str
) -> None:
    text = await _stream_text(_resolved(server, provider_type, model_name), StreamResult())

    assert text == "pong"
    [body] = server.bodies
    assert body["temperature"] == 0.2
    assert not _LITELLM_OPTIONS & body.keys()
    if provider_type == "custom":
        # drop_params가 OpenAI 호환 서버의 사용량 요청을 지우지 않는다
        assert body["stream_options"] == {"include_usage": True}


@pytest.mark.parametrize(("provider_type", "model_name"), SAMPLING)
async def test_tool_turn_keeps_temperature_and_tools_for_sampling_model(
    server: _Server, monkeypatch: pytest.MonkeyPatch, provider_type: str, model_name: str
) -> None:
    res = await _tool_turn(monkeypatch, _resolved(server, provider_type, model_name))

    assert [c.name for c in res.tool_calls] == ["search"]
    [body] = server.bodies
    assert body["temperature"] == 0.2
    assert [t.get("name") or t["function"]["name"] for t in body["tools"]] == ["search"]
    assert not _LITELLM_OPTIONS & body.keys()


# ── 회귀(리뷰 M1): drop_params가 도구 정의를 조용히 빼지 않는다 ───────────


@pytest.mark.parametrize(
    "model_name",
    [
        pytest.param("bedrock/meta.llama3-8b-instruct-v1:0", id="converse"),
        pytest.param("bedrock/mistral.mistral-7b-instruct-v0:2", id="invoke"),
    ],
)
async def test_tool_turn_keeps_tools_for_bedrock_model_without_tool_support(
    server: _Server, monkeypatch: pytest.MonkeyPatch, aws_env: None, model_name: str
) -> None:
    # 도구를 빼면 모델이 도구 없이 답하고, 런타임은 그 답을 최종 답으로 내보낸다.
    # 도구 정의를 실으면 거부 여부는 프로바이더가 정한다(가짜 서버는 거부한다)
    with pytest.raises(GatewayError):
        await _tool_turn(monkeypatch, _resolved(server, "bedrock", model_name))

    # 도구 미지원 400은 영구 요청 오류라 같은 후보에 다시 보내지 않는다(#380). 요청은 도구 정의를
    # 싣는다
    assert len(server.bodies) == 1
    for body in server.bodies:
        if "toolConfig" in body:
            names = [t["toolSpec"]["name"] for t in body["toolConfig"]["tools"]]
        else:
            names = [t["function"]["name"] for t in body["tools"]]
        assert names == ["search"]


async def test_tool_turn_drops_only_tool_choice_for_bedrock_model(
    server: _Server, monkeypatch: pytest.MonkeyPatch, aws_env: None
) -> None:
    resolved = _resolved(server, "bedrock", "bedrock/meta.llama3-1-70b-instruct-v1:0")

    res = await _tool_turn(monkeypatch, resolved)

    assert [(c.name, c.arguments) for c in res.tool_calls] == [("search", {"q": "노트"})]
    [body] = server.bodies
    assert [t["toolSpec"]["name"] for t in body["toolConfig"]["tools"]] == ["search"]
    assert "toolChoice" not in body["toolConfig"]
    assert body["inferenceConfig"]["temperature"] == 0.2
