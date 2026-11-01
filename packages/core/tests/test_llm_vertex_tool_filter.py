"""Vertex(Gemini) 변환이 예외를 내는 도구를 Gemini 호출에서만 뺀다 (#314).

계획 `docs/plans/mcp-vertex-schema-filter.md`. 판정은 실제 LiteLLM Vertex 변환이다. 끝단 확인은
Gemini API를 흉내 낸 로컬 HTTP 서버에 실제 LiteLLM으로 보낸다 — 원격 서버·모델 조회 DB만 가짜다.
"""

import copy
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from litellm.llms.vertex_ai.gemini.vertex_and_google_ai_studio_gemini import VertexGeminiConfig
from structlog.testing import capture_logs

from biznotes_core.core.crypto import encrypt
from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.services.gateway import circuit
from biznotes_core.services.gateway import llm as gateway
from biznotes_core.services.gateway.llm import (
    ResolvedModel,
    _vertex_compatible_tools,
    complete_with_tools,
)


def _tool(name: str, parameters: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": "d", "parameters": parameters},
    }


def _obj(prop: Any) -> dict[str, Any]:
    return {"type": "object", "properties": {"a": prop}}


# 이슈 #314 표의 형태 — Vertex 후처리만 예외를 낸다
BAD = {
    "tuple_items": _obj({"type": "array", "items": [{"type": "string"}, {"type": "integer"}]}),
    "empty_any_of": _obj({"anyOf": []}),
    "null_only_any_of": _obj({"anyOf": [{"type": "null"}]}),
    "bool_property": _obj(True),
    "bool_items": _obj({"type": "array", "items": True}),
    "properties_list": {"type": "object", "properties": []},
    "any_of_int": _obj({"anyOf": [1]}),
}
GOOD = _tool(
    "search",
    {
        "type": "object",
        "properties": {
            "q": {"type": "string"},
            "n": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        },
        "required": ["q"],
    },
)
VERTEX_MODELS = [
    "gemini/gemini-2.5-flash",
    "vertex_ai/gemini-2.5-pro",
    "vertex_ai_beta/gemini-2.0-flash",
]
OTHER_MODELS = [
    "gpt-4o",
    "openai/local-model",
    "anthropic/claude-sonnet-4-6",
    "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0",
    "vertex_ai/claude-3-5-sonnet",  # Vertex 파트너 모델은 Anthropic 형식이다
    # Vertex Model Garden 경로는 OpenAI 형식이다 — Vertex 스키마 변환을 타지 않는다(#328 리뷰)
    "vertex_ai/xai/grok-4.1-fast-non-reasoning",
    "vertex_ai/gemma/gemma-3-12b-it",
    "vertex_ai/1234567890",
    "mock-model",
]


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway, "_VERTEX_TOOL_VERDICTS", gateway.OrderedDict())


def _all_tools() -> list[dict[str, Any]]:
    return [GOOD, *(_tool(name, schema) for name, schema in BAD.items())]


# ── 판정 ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", list(BAD))
def test_negative_control_raw_vertex_conversion_raises(name: str) -> None:
    """필터 없이 넣으면 Vertex 변환이 예외를 낸다 — 거른 결과가 의미 있다는 대조."""
    with pytest.raises(Exception):  # noqa: B017 — 형태마다 예외 종류가 다르다(이슈 표)
        VertexGeminiConfig()._map_function([_tool(name, copy.deepcopy(BAD[name]))], {})


@pytest.mark.parametrize("model", VERTEX_MODELS)
def test_vertex_models_drop_only_failing_tools(model: str) -> None:
    tools = _all_tools()
    original = copy.deepcopy(tools)

    kept, dropped = _vertex_compatible_tools(model, tools)

    assert kept == [GOOD]
    assert dropped == list(BAD)
    assert tools == original  # 입력 도구를 바꾸지 않는다
    VertexGeminiConfig()._map_function(copy.deepcopy(kept), {})  # 남은 도구는 예외 없이 지난다


@pytest.mark.parametrize("model", OTHER_MODELS)
def test_other_providers_receive_the_same_tools(model: str) -> None:
    tools = _all_tools()
    kept, dropped = _vertex_compatible_tools(model, tools)
    assert kept is tools and dropped == []


def test_verdicts_are_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = VertexGeminiConfig._map_function

    def counting(self: Any, value: Any, optional_params: Any) -> Any:
        calls.append(1)
        return real(self, value, optional_params)

    monkeypatch.setattr(VertexGeminiConfig, "_map_function", counting)
    _vertex_compatible_tools("gemini/gemini-2.5-flash", _all_tools())
    first = len(calls)
    _vertex_compatible_tools("gemini/gemini-2.5-flash", _all_tools())
    assert first == len(_all_tools()) and len(calls) == first


def test_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway, "_VERTEX_TOOL_VERDICT_LIMIT", 3)
    tools = [_tool(f"t{i}", _obj({"type": "string"})) for i in range(5)]
    _vertex_compatible_tools("gemini/gemini-2.5-flash", tools)
    assert len(gateway._VERTEX_TOOL_VERDICTS) == 3


def test_missing_litellm_internals_keep_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """LiteLLM 내부 구조가 바뀌어 변환을 부르지 못하면 거르지 않는다(지금 동작)."""
    monkeypatch.delattr(VertexGeminiConfig, "_map_function")
    monkeypatch.setattr(gateway, "_vertex_probe_unavailable_warned", False)
    tools = _all_tools()
    with capture_logs() as logs:
        kept, dropped = _vertex_compatible_tools("gemini/gemini-2.5-flash", tools)
    assert kept is tools and dropped == []
    assert [e["event"] for e in logs] == ["llm_vertex_tool_probe_unavailable"]


# ── 끝단: 실제 LiteLLM → 가짜 Gemini 서버 ───────────────────────────────


class _GeminiHandler(BaseHTTPRequestHandler):
    bodies: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802 — http.server 규약
        length = int(self.headers.get("Content-Length") or 0)
        type(self).bodies.append(json.loads(self.rfile.read(length) or b"{}"))
        payload = {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "pong"}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {
                "promptTokenCount": 3,
                "candidatesTokenCount": 1,
                "totalTokenCount": 4,
            },
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — 상위 시그니처
        return


@pytest.fixture
def gemini_server() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    _GeminiHandler.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GeminiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", _GeminiHandler.bodies
    finally:
        server.shutdown()
        server.server_close()


def _resolved(base_url: str) -> ResolvedModel:
    provider = LLMProvider(
        name="gemini", type="gemini", base_url=base_url, api_key_encrypted=encrypt("test-secret")
    )
    model = LLMModel(
        model_name="gemini/gemini-2.5-flash",
        display_name="gemini-2.5-flash",
        capabilities=["llm"],
    )
    return ResolvedModel(model=model, provider=provider)


async def test_gemini_turn_succeeds_without_the_failing_tools(
    monkeypatch: pytest.MonkeyPatch, gemini_server: tuple[str, list[dict[str, Any]]]
) -> None:
    base_url, bodies = gemini_server
    resolved = _resolved(base_url)

    async def fake_chain(db: Any, model_id: Any, modality: str = "llm") -> list[ResolvedModel]:
        return [resolved]

    async def closed(provider_id: Any) -> bool:
        return False

    async def noop(provider_id: Any) -> bool:
        return False

    # tool 경로도 폴백 체인·서킷을 탄다(#374) — 서킷은 닫힌 대역
    monkeypatch.setattr(gateway, "resolve_chain", fake_chain)
    monkeypatch.setattr(circuit, "is_open", closed)
    monkeypatch.setattr(circuit, "record_failure", noop)
    monkeypatch.setattr(circuit, "record_success", noop)
    with capture_logs() as logs:
        result = await complete_with_tools(
            None,  # type: ignore[arg-type]
            None,
            [{"role": "user", "content": "ping"}],
            _all_tools(),
        )

    assert result.content == "pong"
    [body] = bodies
    declared = [f["name"] for t in body["tools"] for f in t["function_declarations"]]
    assert declared == ["search"]
    [warning] = [e for e in logs if e["event"] == "llm_tools_dropped_for_provider"]
    assert warning["tools"] == list(BAD) and warning["log_level"] == "warning"
    # 스키마 원문·예외 메시지는 로그에 없다
    assert "anyOf" not in str(warning) and "object has no attribute" not in str(warning)


@pytest.mark.parametrize("model", VERTEX_MODELS + OTHER_MODELS)
def test_detection_matches_litellm_param_mapping(model: str) -> None:
    """판정이 LiteLLM 자신의 분기와 같다 — 대상이면 실제 매핑이 예외를 내고, 아니면 내지 않는다."""
    import litellm
    from litellm.utils import get_optional_params

    try:
        base, provider, _, _ = litellm.get_llm_provider(model=model)
    except Exception:
        pytest.skip("LiteLLM이 제공자를 판정하지 못하는 모델")
    tool = _tool("tuple_items", copy.deepcopy(BAD["tuple_items"]))
    raised = False
    try:
        get_optional_params(
            model=base, custom_llm_provider=provider, tools=[tool], tool_choice="auto"
        )
    except Exception:
        raised = True
    assert gateway._uses_vertex_schema(model) is raised
