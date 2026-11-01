"""LLM 출력이 출력 상한(`max_tokens`)에서 잘렸을 때의 감지·기록·처리(#422).

게이트웨이는 OpenAI 호환·Anthropic 서버를 로컬 HTTP 서버로 띄우고 실제 LiteLLM으로 호출한다 —
종료 사유 정규화(`length`)와 스트림 청크 순서(사용량 청크가 맨 끝, 그 청크의 종료 사유는 None)가
LiteLLM에 기대므로 가짜 응답 객체로는 고정할 수 없다. 서킷 브레이커의 Redis와 모델 조회 DB만
가짜다. 노드 경로는 `execute_agent` 실경로이고 mock 프로바이더가 출력 상한을 지킨다. react 루프는
모델 턴(`complete_with_tools`·마무리 스트림)만 대역이다(`test_react_budget_report.py`와 같다).
"""

import json
import re
import threading
import uuid
from collections.abc import AsyncGenerator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs

from biznotes_core.core.crypto import encrypt
from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.services.agents import graph, runtime
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError, LLMOutputTruncatedError
from biznotes_core.services.agents.node_errors import NODE_ERROR_CODES, describe_node_failure
from biznotes_core.services.agents.nodes import validation
from biznotes_core.services.agents.react import loop
from biznotes_core.services.agents.react.loop import BUDGET_HIT_KEY, BUDGET_INFO_KEY
from biznotes_core.services.gateway import circuit
from biznotes_core.services.gateway import llm as gateway
from biznotes_core.services.gateway.llm import (
    ResolvedModel,
    StreamResult,
    ToolCall,
    ToolTurnResult,
    complete_stream,
    complete_stream_with_fallback,
    complete_with_tools,
)

MESSAGES = [{"role": "user", "content": "개요를 JSON으로"}]
TOOLS = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
PARTIAL = '{"title": "개요", "slides": [{"title": "도'
# 경로 첫 마디 → (종료 사유, 출력 토큰, 사고 토큰). 종료 사유가 None이면 종료 조각을 보내지 않는다.
# 사고 토큰이 None이면 `completion_tokens_details`를 보내지 않는다
CASES: dict[str, tuple[str | None, int, int | None]] = {
    "cut": ("length", 6140, 5800),
    "cut-plain": ("length", 6140, None),
    "done": ("stop", 12, None),
    "done-zero": ("stop", 12, 0),
    "silent": (None, 12, None),
}


def _usage(key: str) -> dict[str, Any]:
    _, completion, reasoning = CASES[key]
    usage: dict[str, Any] = {
        "prompt_tokens": 46_346,
        "completion_tokens": completion,
        "total_tokens": 46_346 + completion,
    }
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return usage


def _chunks(key: str, model: str) -> str:
    finish, _, _ = CASES[key]
    base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": model}
    chunks: list[dict[str, Any]] = [
        {**base, "choices": [{"index": 0, "finish_reason": None,
                              "delta": {"role": "assistant", "content": PARTIAL}}]},
    ]
    if finish is not None:
        chunks.append({**base, "choices": [{"index": 0, "finish_reason": finish, "delta": {}}]})
    # 실측 순서 — 사용량 청크가 맨 끝에 오고 종료 사유는 없다
    chunks.append({**base, "choices": [], "usage": _usage(key)})
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def _completion(key: str, model: str) -> dict[str, Any]:
    finish, _, _ = CASES[key]
    return {
        "id": "c1", "object": "chat.completion", "created": 0, "model": model,
        "choices": [{"index": 0, "finish_reason": finish,
                     "message": {"role": "assistant", "content": PARTIAL}}],
        "usage": _usage(key),
    }  # fmt: skip


def _anthropic_events(model: str) -> str:
    """Anthropic Messages 스트림 — 공급자 값 `max_tokens`를 LiteLLM이 `length`로 정규화한다."""
    message = {
        "id": "m1", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "usage": {"input_tokens": 3, "output_tokens": 0},
    }  # fmt: skip
    events = [
        ("message_start", {"type": "message_start", "message": message}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": PARTIAL}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "max_tokens"},
                           "usage": {"output_tokens": 10}}),
        ("message_stop", {"type": "message_stop"}),
    ]  # fmt: skip
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)


class _Server:
    """`/<경로>/v1/chat/completions`(OpenAI 호환)와 `/anthropic/v1/messages`."""

    def __init__(self) -> None:
        self.hits: dict[str, int] = {}
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                key = self.path.strip("/").split("/", 1)[0]
                server.hits[key] = server.hits.get(key, 0) + 1
                if key == "anthropic":
                    self._send("text/event-stream", _anthropic_events(body["model"]))
                elif body.get("stream"):
                    self._send("text/event-stream", _chunks(key, body["model"]))
                else:
                    self._send("application/json", json.dumps(_completion(key, body["model"])))

            def _send(self, content_type: str, text: str) -> None:
                raw = text.encode()
                self.send_response(200)
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


def _resolved(
    server: _Server, key: str, model_name: str = "openai/gemini-3.8-flash"
) -> ResolvedModel:
    provider = LLMProvider(
        id=uuid.uuid4(),
        name=f"p-{key}",
        type="anthropic" if key == "anthropic" else "vllm",
        base_url=f"{server.root}/{key}" if key == "anthropic" else f"{server.root}/{key}/v1",
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


def _mock_model(name: str = "mock") -> ResolvedModel:
    provider = LLMProvider(id=uuid.uuid4(), name=f"p-{name}", type="mock")
    model = LLMModel(id=uuid.uuid4(), provider_id=provider.id, model_name=name,
                     display_name=name, capabilities=["llm"])  # fmt: skip
    return ResolvedModel(model=model, provider=provider)


class _Circuit:
    """서킷 브레이커의 메모리 대역 — 실패·성공 기록만 모은다."""

    def __init__(self) -> None:
        self.failures: list[uuid.UUID] = []
        self.successes: list[uuid.UUID] = []

    async def is_open(self, provider_id: uuid.UUID) -> bool:
        return False

    async def record_failure(self, provider_id: uuid.UUID) -> bool:
        self.failures.append(provider_id)
        return False

    async def record_success(self, provider_id: uuid.UUID) -> None:
        self.successes.append(provider_id)


@pytest.fixture
def breaker(monkeypatch: pytest.MonkeyPatch) -> _Circuit:
    fake = _Circuit()
    monkeypatch.setattr(circuit, "is_open", fake.is_open)
    monkeypatch.setattr(circuit, "record_failure", fake.record_failure)
    monkeypatch.setattr(circuit, "record_success", fake.record_success)
    return fake


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch, breaker: _Circuit) -> list[ResolvedModel]:
    """모델 조회 DB 대역 — 테스트가 후보 목록을 채운다."""
    models: list[ResolvedModel] = []

    async def fake_chain(db: Any, model_id: Any, modality: str = "llm") -> list[ResolvedModel]:
        return list(models)

    async def fake_resolve(db: Any, model_id: Any, modality: str = "llm") -> ResolvedModel:
        return models[0]

    monkeypatch.setattr(gateway, "resolve_chain", fake_chain)
    monkeypatch.setattr(gateway, "resolve_model", fake_resolve)
    return models


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """노드 실행 로그(`_log_node_finish`) — 노드 id → 기록 인자."""
    finished: dict[str, dict[str, Any]] = {}

    async def log_start(run_id: Any, node_id: str, *a: Any, **k: Any) -> str:
        return node_id

    async def log_finish(log_id: str, **kw: Any) -> bool:
        finished[log_id] = kw
        return True

    monkeypatch.setattr(graph, "_log_node_start", log_start)
    monkeypatch.setattr(graph, "_log_node_finish", log_finish)
    return finished


def _ctx(**kw: Any) -> RunContext:
    return RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={}, **kw)  # type: ignore[arg-type]


async def _stream(result: StreamResult) -> str:
    gen = await complete_stream_with_fallback(None, None, MESSAGES, result, max_tokens=6144)  # type: ignore[arg-type]
    return "".join([d async for d in gen])


# ── 게이트웨이: 실제 LiteLLM ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "finish", "usage_extra"),
    [
        ("cut", "length", {"reasoning_tokens": 5800}),
        ("cut-plain", "length", {}),
        ("done", "stop", {}),
        # LiteLLM 스트림은 보고하지 않은 사고 토큰을 0으로 채운다 — 0은 싣지 않는다
        ("done-zero", "stop", {}),
    ],
)
async def test_stream_carries_the_last_finish_reason(server, chain, key, finish, usage_extra):
    chain.append(_resolved(server, key))
    result = StreamResult()

    assert await _stream(result) == PARTIAL

    # 사용량 청크(맨 끝)의 종료 사유는 None이다 — None이 아닌 마지막 값을 쓴다
    assert result.finish_reason == finish
    _, completion, _ = CASES[key]
    assert result.usage == {
        "prompt_tokens": 46_346,
        "completion_tokens": completion,
        "total_tokens": 46_346 + completion,
        **usage_extra,
    }


async def test_stream_without_a_provider_finish_reason_is_not_truncation(server, chain):
    """종료 조각을 보내지 않는 서버 — LiteLLM이 `stop`을 채운다. 잘림으로 보지 않는다."""
    chain.append(_resolved(server, "silent"))
    result = StreamResult()

    assert await _stream(result) == PARTIAL

    assert result.finish_reason == "stop"


async def test_anthropic_max_tokens_is_normalized_to_length(server, chain):
    chain.append(_resolved(server, "anthropic", "anthropic/claude-3-5-haiku-20241022"))
    result = StreamResult()

    assert await _stream(result) == PARTIAL

    assert result.finish_reason == "length"


def test_results_default_to_no_finish_reason():
    """값을 주지 않는 경로(대역·옛 호출)는 None이다 — 지금 동작과 같다."""
    assert StreamResult().finish_reason is None
    assert ToolTurnResult().finish_reason is None


@pytest.mark.parametrize(
    ("key", "finish", "usage_extra"),
    [
        ("cut", "length", {"reasoning_tokens": 5800}),
        ("done", "stop", {}),
        ("done-zero", "stop", {}),
    ],
)
async def test_tool_turn_carries_the_finish_reason(server, chain, key, finish, usage_extra):
    chain.append(_resolved(server, key))

    turn = await complete_with_tools(None, None, MESSAGES, TOOLS, max_tokens=6144)  # type: ignore[arg-type]

    assert (turn.content, turn.finish_reason) == (PARTIAL, finish)
    _, completion, _ = CASES[key]
    assert turn.usage == {
        "prompt_tokens": 46_346,
        "completion_tokens": completion,
        "total_tokens": 46_346 + completion,
        **usage_extra,
    }


@pytest.mark.parametrize("path", ["stream", "tool"])
async def test_truncation_is_not_a_provider_failure(server, chain, breaker, path):
    """잘린 응답은 응답이다 — 다음 후보로 넘기지 않고 서킷 실패로 세지 않는다."""
    cut, backup = _resolved(server, "cut"), _resolved(server, "done")
    chain.extend([cut, backup])

    if path == "tool":
        turn = await complete_with_tools(None, None, MESSAGES, TOOLS)  # type: ignore[arg-type]
        answered, fallback, finish = turn.model, turn.fallback_used, turn.finish_reason
    else:
        result = StreamResult()
        assert await _stream(result) == PARTIAL
        answered, fallback, finish = result.model, result.fallback_used, result.finish_reason

    assert (answered, fallback, finish) == (cut, False, "length")
    assert server.hits == {"cut": 1}
    assert breaker.failures == []
    assert breaker.successes == [cut.provider.id]


# ── mock 프로바이더: 출력 상한을 지킨다 ───────────────────────────────────


async def test_mock_stream_cuts_at_max_tokens():
    mock = _mock_model()
    full = "".join([d async for d in await complete_stream(mock, MESSAGES, StreamResult())])

    cut = StreamResult()
    text = "".join([d async for d in await complete_stream(mock, MESSAGES, cut, max_tokens=5)])
    whole = StreamResult()
    same = "".join([d async for d in await complete_stream(mock, MESSAGES, whole, max_tokens=500)])

    # 토큰 추정(글자 ÷ 4)과 같은 비율로 자른다
    assert (text, cut.finish_reason, cut.usage["completion_tokens"]) == (full[:20], "length", 5)
    assert (same, whole.finish_reason) == (full, "stop")
    assert len(full) > 20


async def test_mock_tool_turn_cuts_the_final_answer(chain):
    chain.append(_mock_model())
    after_tool = [*MESSAGES, {"role": "tool", "tool_call_id": "c1", "content": "가" * 400}]

    call = await complete_with_tools(None, None, MESSAGES, TOOLS, max_tokens=5)  # type: ignore[arg-type]
    cut = await complete_with_tools(None, None, after_tool, TOOLS, max_tokens=5)  # type: ignore[arg-type]
    whole = await complete_with_tools(None, None, after_tool, TOOLS, max_tokens=500)  # type: ignore[arg-type]

    assert (call.tool_calls[0].name, call.finish_reason) == ("f", "tool_calls")
    assert (len(cut.content), cut.finish_reason, cut.usage["completion_tokens"]) == (
        20, "length", 5
    )
    assert cut.content == whole.content[:20]
    assert whole.finish_reason == "stop"
    assert cut.assistant_message["content"] == cut.content


# ── llm 노드 결과·실행 로그 ────────────────────────────────────────────────


async def test_llm_log_records_truncation(chain, logs):
    chain.append(_mock_model())

    await graph.execute_agent(
        _ctx(), "workflow", {"nodes": [{"id": "gen", "type": "llm", "prompt": "요약",
                                        "max_tokens": 5}], "edges": []}
    )  # fmt: skip

    out = logs["gen"]["output_data"]
    assert out["finish_reason"] == "length"
    assert out["output_truncated"] == {"max_tokens": 5, "completion_tokens": 5}
    assert "truncated" not in out  # json_validate의 마인드맵 절단 키와 섞지 않는다


async def test_llm_log_records_a_normal_finish_without_truncation(chain, logs):
    chain.append(_mock_model())

    await graph.execute_agent(
        _ctx(), "workflow", {"nodes": [{"id": "gen", "type": "llm", "prompt": "요약"}], "edges": []}
    )

    out = logs["gen"]["output_data"]
    assert out["finish_reason"] == "stop"
    assert "output_truncated" not in out


async def test_llm_without_a_finish_reason_keeps_todays_keys(monkeypatch, logs):
    """게이트웨이가 종료 사유를 주지 않으면(대역) 키를 싣지 않는다 — 지금 출력과 같다."""

    async def stream(db: Any, model_id: Any, messages: Any, result: Any, **kw: Any) -> Any:
        async def gen() -> AsyncGenerator[str, None]:
            yield "답"

        return gen()

    monkeypatch.setattr(gateway, "complete_stream_with_fallback", stream)

    await graph.execute_agent(
        _ctx(), "workflow", {"nodes": [{"id": "gen", "type": "llm", "prompt": "요약"}], "edges": []}
    )

    assert logs["gen"]["output_data"] == {"text": "답", "fallback_used": False}


@pytest.mark.parametrize(("key", "reasoning"), [("cut", 5800), ("done-zero", None)])
async def test_reasoning_tokens_reach_the_node_log_only_when_positive(
    server, chain, logs, key, reasoning
):
    chain.append(_resolved(server, key))

    await graph.execute_agent(
        _ctx(), "workflow", {"nodes": [{"id": "gen", "type": "llm", "prompt": "개요",
                                        "max_tokens": 6144}], "edges": []}
    )  # fmt: skip

    log = logs["gen"]
    _, completion, _ = CASES[key]
    expected = {
        "prompt_tokens": 46_346,
        "completion_tokens": completion,
        "total_tokens": 46_346 + completion,
    }
    if reasoning is not None:
        expected["reasoning_tokens"] = reasoning
    assert log["token_usage"] == expected
    if key == "cut":
        assert log["output_data"]["output_truncated"] == {
            "max_tokens": 6144, "completion_tokens": 6140, "reasoning_tokens": 5800
        }
    else:
        assert "output_truncated" not in log["output_data"]


# ── json_validate ──────────────────────────────────────────────────────────

SLIDE_OUTLINE = {"title": "개요", "slides": [{"title": "도입", "points": ["a"]}]}


def _validate_node(**kw: Any) -> dict[str, Any]:
    return {"id": "check", "type": "json_validate", "schema": "slide_outline",
            "source": "{outline.text}", "prompt": "슬라이드 개요 JSON", **kw}  # fmt: skip


@pytest.fixture
def replies(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """재시도 LLM 호출이 차례로 받을 응답(게이트웨이 스트림 경계 대역) — text·finish·usage."""
    queue: list[dict[str, Any]] = []

    async def stream(db: Any, model_id: Any, messages: Any, result: Any, **kw: Any) -> Any:
        reply = queue.pop(0)
        result.finish_reason = reply.get("finish")
        result.usage = reply.get("usage", {})

        async def gen() -> AsyncGenerator[str, None]:
            yield reply["text"]

        return gen()

    monkeypatch.setattr(gateway, "complete_stream_with_fallback", stream)
    return queue


def _truncated_source(**info: int) -> dict[str, Any]:
    return {"outline": {"text": PARTIAL, "finish_reason": "length", "output_truncated": info}}


async def test_truncated_source_fails_at_once_without_a_retry(replies):
    replies.append({"text": json.dumps(SLIDE_OUTLINE), "finish": "stop"})
    state = _truncated_source(max_tokens=6144, completion_tokens=6140, reasoning_tokens=5800)

    with pytest.raises(LLMOutputTruncatedError) as raised:
        await validation._exec_json_validate(_ctx(), _validate_node(), state)

    assert str(raised.value) == (
        "json_validate: 출력이 상한(max_tokens=6144)에서 잘렸습니다(출력 6,140토큰, 그중 사고 "
        "5,800토큰). 상한을 올리거나 입력을 줄이세요."
    )
    assert isinstance(raised.value, AgentRuntimeError)  # 기존 처리도 그대로 잡는다
    assert describe_node_failure(raised.value).code == "llm_output_truncated"
    assert len(replies) == 1  # 재시도하지 않았다


async def test_truncation_message_leaves_out_unknown_reasoning(replies):
    state = _truncated_source(max_tokens=6144, completion_tokens=6140)

    with pytest.raises(LLMOutputTruncatedError, match=re.escape("(출력 6,140토큰).")):
        await validation._exec_json_validate(_ctx(), _validate_node(), state)


async def test_truncated_source_without_a_retry_prompt_names_the_cause(replies):
    node = _validate_node()
    del node["prompt"]

    with pytest.raises(LLMOutputTruncatedError, match=r"^json_validate: 출력이 상한"):
        await validation._exec_json_validate(
            _ctx(), node, _truncated_source(max_tokens=6144, completion_tokens=6140)
        )


async def test_truncated_but_valid_json_passes(replies):
    info = {"max_tokens": 6144, "completion_tokens": 6144}
    state = {"outline": {"text": json.dumps(SLIDE_OUTLINE), "finish_reason": "length",
                         "output_truncated": info}}  # fmt: skip

    out = await validation._exec_json_validate(_ctx(), _validate_node(), state)

    assert out["json"] == SLIDE_OUTLINE


async def test_truncated_retry_fails_with_the_same_code(replies):
    """원 출력은 형식 오류(잘림 아님)라 재시도하고, 재시도가 잘리면 같은 코드로 끝낸다."""
    replies.append({"text": PARTIAL, "finish": "length", "usage": {"completion_tokens": 4096}})
    state = {"outline": {"text": "개요입니다", "finish_reason": "stop"}}

    with pytest.raises(LLMOutputTruncatedError) as raised:
        await validation._exec_json_validate(_ctx(), _validate_node(max_tokens=4096), state)

    assert str(raised.value) == (
        "json_validate: 출력이 상한(max_tokens=4096)에서 잘렸습니다(출력 4,096토큰). "
        "상한을 올리거나 입력을 줄이세요."
    )
    assert replies == []


async def test_untruncated_format_error_still_retries_once(replies):
    replies.append({"text": json.dumps(SLIDE_OUTLINE, ensure_ascii=False), "finish": "stop"})
    state = {"outline": {"text": "```json\n{}\n```", "finish_reason": "stop"}}

    out = await validation._exec_json_validate(_ctx(), _validate_node(), state)

    assert out["json"] == SLIDE_OUTLINE
    assert replies == []  # 재시도 1회를 썼다


async def test_untruncated_retry_failure_keeps_the_schema_message(replies):
    replies.append({"text": "{not json", "finish": "stop"})
    state = {"outline": {"text": "개요입니다", "finish_reason": "stop"}}

    final = r"스키마 검증 최종 실패\(slide_outline\)"
    with pytest.raises(AgentRuntimeError, match=final) as raised:
        await validation._exec_json_validate(_ctx(), _validate_node(), state)

    assert not isinstance(raised.value, LLMOutputTruncatedError)
    assert describe_node_failure(raised.value).code is None


def test_truncation_code_is_registered():
    assert LLMOutputTruncatedError.node_error_code == "llm_output_truncated"
    assert "llm_output_truncated" in NODE_ERROR_CODES


async def test_mock_truncation_fails_json_validate_end_to_end(chain, logs, monkeypatch):
    """수락 기준: mock 잘림 → llm 실행 로그 → json_validate 즉시 실패(`execute_agent` 실경로)."""
    chain.append(_mock_model())
    calls = [0]
    real = gateway.complete_stream

    async def counted(*a: Any, **k: Any) -> Any:
        calls[0] += 1
        return await real(*a, **k)

    monkeypatch.setattr(gateway, "complete_stream", counted)
    definition = {
        "nodes": [
            {"id": "outline", "type": "llm", "prompt": "슬라이드 개요 JSON", "max_tokens": 5},
            _validate_node(),
        ],
        "edges": [["outline", "check"]],
    }

    with pytest.raises(AgentRuntimeError) as raised:
        await graph.execute_agent(_ctx(), "workflow", definition)

    message = (
        "json_validate: 출력이 상한(max_tokens=5)에서 잘렸습니다(출력 5토큰). "
        "상한을 올리거나 입력을 줄이세요."
    )
    assert str(raised.value) == f"노드 [check] 실행 실패: {message}"
    assert logs["outline"]["status"] == "succeeded"
    assert logs["outline"]["output_data"]["output_truncated"] == {
        "max_tokens": 5, "completion_tokens": 5
    }
    check = logs["check"]
    assert (check["status"], check["error_code"], check["error_message"]) == (
        "failed", "llm_output_truncated", message
    )
    assert calls[0] == 1  # 같은 상한으로 다시 부르지 않았다


# ── react 루프 ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _quiet_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """react_tool_call 보조 로그 — 실제로는 격리 세션에 쓰는 best-effort 기록이다."""

    async def start(*a: Any, **k: Any) -> None:
        return None

    async def finish(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(loop, "_log_node_start", start)
    monkeypatch.setattr(loop, "_log_node_finish", finish)


@pytest.fixture
def tool_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    async def dispatch(ctx: RunContext, name: str, args: dict[str, Any]) -> str:
        seen.append(name)
        return "발췌 없음"

    monkeypatch.setattr(runtime, "_dispatch_builtin_tool", dispatch)
    return seen


def _model_meta() -> Any:
    return SimpleNamespace(model=SimpleNamespace(id=uuid.uuid4(), display_name="m", cost_per_1k={}))


def _call_turn(
    usage: dict[str, int], content: str = "", finish: str = "tool_calls"
) -> ToolTurnResult:
    call = ToolCall(id="c1", name="rag_search", arguments={"query": "q"})
    return ToolTurnResult(
        content=content,
        tool_calls=[call],
        assistant_message={
            "role": "assistant",
            "content": content or None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "rag_search", "arguments": '{"query": "q"}'}}],
        },  # fmt: skip
        usage=usage,
        model=_model_meta(),
        finish_reason=finish,
    )


def _answer(text: str, finish: str | None, usage: dict[str, int] | None = None) -> ToolTurnResult:
    return ToolTurnResult(
        content=text,
        assistant_message={"role": "assistant", "content": text},
        usage=usage or {"completion_tokens": 4096, "total_tokens": 5000},
        model=_model_meta(),
        finish_reason=finish,
    )


def _script(monkeypatch: pytest.MonkeyPatch, *turns: ToolTurnResult) -> None:
    count = [0]

    async def fake(db: Any, model_id: Any, messages: Any, tools: Any, **kw: Any) -> ToolTurnResult:
        turn = turns[min(count[0], len(turns) - 1)]
        count[0] += 1
        return turn

    monkeypatch.setattr(gateway, "complete_with_tools", fake)


def _finalizer(
    monkeypatch: pytest.MonkeyPatch, text: str, finish: str, usage: dict[str, int]
) -> None:
    async def fake(db: Any, model_id: Any, messages: Any, result: Any, **k: Any) -> Any:
        result.usage = usage
        result.finish_reason = finish

        async def gen() -> AsyncGenerator[str, None]:
            yield text

        return gen()

    monkeypatch.setattr(gateway, "complete_stream_with_fallback", fake)


def _react(**kw: Any) -> dict[str, Any]:
    return {"id": "r", "type": "react_agent", "goal": "조사", "allowed_tools": ["rag_search"], **kw}


def _truncation_warnings(logged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logged if e["log_level"] == "warning" and "잘림" in e["event"]]


async def test_react_truncated_final_answer_is_flagged_and_succeeds(monkeypatch, tool_calls):
    usage = {"completion_tokens": 4096, "reasoning_tokens": 3000}
    _script(monkeypatch, _answer("잘린 답", "length", usage))

    with capture_logs() as logged:
        out = await loop.run_react_node(_ctx(), _react())

    assert out["text"] == "잘린 답"
    assert out["finish_reason"] == "length"
    assert out["output_truncated"] == {
        "max_tokens": 4096, "completion_tokens": 4096, "reasoning_tokens": 3000
    }
    [warning] = _truncation_warnings(logged)
    assert (warning["node_id"], warning["max_tokens"]) == ("r", 4096)


async def test_react_normal_answer_has_only_the_finish_reason(monkeypatch, tool_calls):
    _script(monkeypatch, _answer("답", "stop"))

    with capture_logs() as logged:
        out = await loop.run_react_node(_ctx(), _react())

    assert out["finish_reason"] == "stop"
    assert "output_truncated" not in out
    assert _truncation_warnings(logged) == []


async def test_react_budget_body_truncation_is_flagged(monkeypatch, tool_calls):
    """예산을 넘긴 턴의 본문을 그대로 내는 경로 — 그 본문이 잘렸으면 표시한다."""
    _script(monkeypatch, _call_turn({"completion_tokens": 2048, "total_tokens": 150_000},
                                    content="중간 본문", finish="length"))  # fmt: skip

    out = await loop.run_react_node(_ctx(), _react(max_tokens=2048))

    assert out[BUDGET_INFO_KEY]["has_content"] is True
    assert out["text"] == "중간 본문"
    assert out["output_truncated"] == {"max_tokens": 2048, "completion_tokens": 2048}


async def test_react_finalizer_truncation_is_flagged(monkeypatch, tool_calls):
    """루프 소진 마무리 호출(#398이 `max_tokens`를 넘기게 바꾼 경로)이 잘린 경우."""
    _script(monkeypatch, _call_turn({"total_tokens": 10}))
    usage = {"completion_tokens": 1024, "total_tokens": 1100}
    _finalizer(monkeypatch, "마무리 답", "length", usage)

    with capture_logs() as logged:
        out = await loop.run_react_node(_ctx(), _react(max_iters=1, max_tokens=1024))

    assert out["text"] == "마무리 답"
    assert out["finish_reason"] == "length"
    assert out["output_truncated"] == {"max_tokens": 1024, "completion_tokens": 1024}
    assert len(_truncation_warnings(logged)) == 1
    assert tool_calls == ["rag_search"]


async def test_react_budget_note_carries_no_truncation(monkeypatch, tool_calls):
    """마무리 호출이 예산을 넘겨 본문을 안내 문구로 바꾸면(C9) 잘림 표시는 무의미하다."""
    _script(monkeypatch, _call_turn({"total_tokens": 10}))
    usage = {"completion_tokens": 1024, "total_tokens": 999_999}
    _finalizer(monkeypatch, "마무리 답", "length", usage)

    with capture_logs() as logged:
        out = await loop.run_react_node(_ctx(), _react(max_iters=1))

    assert out[BUDGET_HIT_KEY] is True and out[BUDGET_INFO_KEY]["has_content"] is False
    assert "finish_reason" not in out and "output_truncated" not in out
    assert _truncation_warnings(logged) == []


async def test_llm_tool_path_flags_truncation(monkeypatch, tool_calls):
    """llm 노드 tool 경로(guards 없음)도 같은 키를 싣는다."""
    _script(monkeypatch, _answer("잘린 답", "length"))
    node = {"id": "gen", "type": "llm", "prompt": "조사", "tools": ["rag_search"],
            "max_tokens": 4096}  # fmt: skip

    out = await runtime._exec_llm(_ctx(), node, {})

    assert out["output_truncated"] == {"max_tokens": 4096, "completion_tokens": 4096}


# ── 채팅 출력 표시 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(("max_tokens", "truncated"), [(5, True), (4096, False)])
async def test_chat_output_marks_truncation_on_the_context(chain, logs, max_tokens, truncated):
    chain.append(_mock_model())
    ctx = _ctx()
    node = {"id": "gen", "type": "llm", "prompt": "요약", "max_tokens": max_tokens}

    await graph.execute_agent(ctx, "workflow", {"nodes": [node], "edges": []})

    assert ctx.chat_output_text
    assert ctx.chat_output_truncated is truncated


async def test_chat_output_marks_a_truncated_react_answer(monkeypatch, logs, tool_calls):
    _script(monkeypatch, _answer("잘린 답", "length"))
    ctx = _ctx()

    await graph.execute_agent(ctx, "workflow", {"nodes": [_react()], "edges": []})

    assert (ctx.chat_output_text, ctx.chat_output_truncated) == ("잘린 답", True)


def test_new_context_field_is_keyword_only_with_a_safe_default():
    """위치 인자로 만든 기존 호출의 순서를 지킨다(`RunContext` 관례)."""
    field = RunContext.__dataclass_fields__["chat_output_truncated"]

    assert (field.kw_only, field.default) == (True, False)
    assert _ctx().chat_output_truncated is False


# ── 리뷰 반영: 노드 재시도·빈 본문 잘림 ─────────────────────────────────────


@pytest.mark.parametrize("continue_on_error", [False, True])
async def test_node_retry_policy_does_not_call_the_model_again(
    chain, logs, monkeypatch, continue_on_error
):
    """교정 재시도 출력이 잘린 json_validate — 노드 `retry_policy`가 같은 상한으로 다시 부르지
    않는다. 실패 처리(`continue_on_error`)는 일반 실패와 같다."""
    chain.append(_mock_model())
    calls = [0]
    real = gateway.complete_stream

    async def counted(*a: Any, **k: Any) -> Any:
        calls[0] += 1
        return await real(*a, **k)

    monkeypatch.setattr(gateway, "complete_stream", counted)
    check = _validate_node(max_tokens=5, retry_policy={"max_retries": 1, "delay": 0},
                           continue_on_error=continue_on_error)  # fmt: skip
    definition = {
        "nodes": [{"id": "outline", "type": "llm", "prompt": "슬라이드 개요 JSON"}, check],
        "edges": [["outline", "check"]],
    }

    if continue_on_error:
        await graph.execute_agent(_ctx(), "workflow", definition)
    else:
        with pytest.raises(AgentRuntimeError, match="잘렸습니다"):
            await graph.execute_agent(_ctx(), "workflow", definition)

    assert calls[0] == 2  # 원 llm 1회 + 교정 재시도 1회 — 노드 재시도는 부르지 않는다
    assert (logs["check"]["status"], logs["check"]["error_code"]) == (
        "failed", "llm_output_truncated"
    )


def _empty_cut_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """사고 토큰이 상한을 다 써 본문 없이 잘린 응답(게이트웨이 스트림 경계 대역)."""

    async def stream(db: Any, model_id: Any, messages: Any, result: Any, **kw: Any) -> Any:
        result.finish_reason = "length"
        result.usage = {"completion_tokens": 64, "reasoning_tokens": 64, "total_tokens": 100}

        async def gen() -> AsyncGenerator[str, None]:
            return
            yield  # pragma: no cover — 빈 비동기 생성기

        return gen()

    monkeypatch.setattr(gateway, "complete_stream_with_fallback", stream)


async def test_chat_output_marks_an_empty_truncated_llm_answer(monkeypatch, logs):
    _empty_cut_stream(monkeypatch)
    ctx = _ctx()
    node = {"id": "gen", "type": "llm", "prompt": "요약", "max_tokens": 64}

    await graph.execute_agent(ctx, "workflow", {"nodes": [node], "edges": []})

    assert logs["gen"]["output_data"]["output_truncated"] == {
        "max_tokens": 64, "completion_tokens": 64, "reasoning_tokens": 64
    }
    assert (ctx.chat_output_text, ctx.chat_output_truncated) == ("", True)


async def test_chat_output_marks_an_empty_truncated_react_answer(monkeypatch, logs, tool_calls):
    _script(monkeypatch, _answer("", "length"))
    ctx = _ctx()
    definition = {"nodes": [_react()], "edges": [], "chat_output": {"node_id": "r"}}

    await graph.execute_agent(ctx, "workflow", definition)

    assert (ctx.chat_output_text, ctx.chat_output_truncated) == ("", True)


async def test_empty_truncated_last_answer_marks_the_earlier_fallback_text(monkeypatch, logs):
    """마지막 llm 노드가 본문 없이 잘려 앞 노드 본문이 채팅에 나가도 잘림을 알린다."""
    replies = iter([("앞 노드 본문", "stop"), ("", "length")])

    async def stream(db: Any, model_id: Any, messages: Any, result: Any, **kw: Any) -> Any:
        text, finish = next(replies)
        result.finish_reason = finish
        result.usage = {"completion_tokens": 64, "total_tokens": 100}

        async def gen() -> AsyncGenerator[str, None]:
            if text:
                yield text

        return gen()

    monkeypatch.setattr(gateway, "complete_stream_with_fallback", stream)
    ctx = _ctx()
    definition = {
        "nodes": [{"id": "a", "type": "llm", "prompt": "초안"},
                  {"id": "b", "type": "llm", "prompt": "{a.text} 다듬기", "max_tokens": 64}],
        "edges": [["a", "b"]],
    }  # fmt: skip

    await graph.execute_agent(ctx, "workflow", definition)

    assert (ctx.chat_output_text, ctx.chat_output_truncated) == ("앞 노드 본문", True)


async def test_untruncated_fallback_text_stays_unmarked(monkeypatch, logs):
    """본문 없는 마지막 노드가 잘리지 않았으면(빈 응답) 표시하지 않는다 — 지금 동작과 같다."""
    replies = iter([("앞 노드 본문", "stop"), ("", "stop")])

    async def stream(db: Any, model_id: Any, messages: Any, result: Any, **kw: Any) -> Any:
        text, finish = next(replies)
        result.finish_reason = finish

        async def gen() -> AsyncGenerator[str, None]:
            if text:
                yield text

        return gen()

    monkeypatch.setattr(gateway, "complete_stream_with_fallback", stream)
    ctx = _ctx()
    definition = {
        "nodes": [{"id": "a", "type": "llm", "prompt": "초안"},
                  {"id": "b", "type": "llm", "prompt": "{a.text} 다듬기"}],
        "edges": [["a", "b"]],
    }  # fmt: skip

    await graph.execute_agent(ctx, "workflow", definition)

    assert (ctx.chat_output_text, ctx.chat_output_truncated) == ("앞 노드 본문", False)
