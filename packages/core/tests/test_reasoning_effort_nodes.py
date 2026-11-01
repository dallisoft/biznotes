"""노드 사고 양(`reasoning_effort`, #429) — 정의 키·저장 헬퍼·실행 전 검사·json_validate 상속.

llm·react_agent·json_validate 실행기는 실제 코드를 탄다. 모델 호출은 게이트웨이 경계
(`complete_stream_with_fallback`·`complete_with_tools`)만 대역이다. 끝의 노드 경로 테스트는 실제
LiteLLM으로 로컬 OpenAI 호환 서버에 보내 요청 본문까지 본다.
"""

import json
import threading
import uuid
from collections.abc import AsyncGenerator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, get_args

import pytest

from biznotes_core.core.crypto import encrypt
from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.models.llm import REASONING_EFFORTS
from biznotes_core.schemas import enums
from biznotes_core.services.agents import graph, policy, runtime
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError
from biznotes_core.services.agents.nodes import llm as llm_node
from biznotes_core.services.agents.nodes import validation
from biznotes_core.services.agents.react import loop
from biznotes_core.services.gateway import circuit
from biznotes_core.services.gateway import llm as gateway
from biznotes_core.services.gateway.llm import ResolvedModel, ToolCall, ToolTurnResult

SLIDE_OUTLINE = {"title": "개요", "slides": [{"title": "도입", "points": ["a"]}]}
INVALID = ["xhigh", "minimal", "none", "LOW", "{params.effort}", "", 1, ["low"]]
# 실행 전 검사의 사유 — 저장 헬퍼와 같은 문구다(모르는 키 거부와 구별한다)
EFFORT_REASON = "reasoning_effort는 low·medium·high 중 하나의 리터럴이어야 합니다"


def _ctx(**kw: Any) -> RunContext:
    return RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={}, **kw)  # type: ignore[arg-type]


# ── 허용 값 정본 ──────────────────────────────────────────────────────────


def test_allowed_values_are_the_model_tuple_and_the_schema_literal():
    assert REASONING_EFFORTS == ("low", "medium", "high")  # 낮은 것부터 — 게이트웨이가 낮추는 순서
    assert set(get_args(enums.ReasoningEffort)) == set(REASONING_EFFORTS)
    # import 시점 대조(`_assert_matches_canonical`)에 묶여 있다
    assert ("ReasoningEffort", enums.ReasoningEffort, REASONING_EFFORTS) in enums._BINDINGS


# ── 저장 헬퍼 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("node_type", ["llm", "react_agent", "json_validate"])
@pytest.mark.parametrize("value", INVALID)
def test_save_helper_rejects_values_outside_the_literals(node_type: str, value: Any):
    nodes = [{"id": "n", "type": node_type, "reasoning_effort": value}]

    assert policy.find_reasoning_effort_violations(nodes) == [f"n: {EFFORT_REASON}"]


@pytest.mark.parametrize("value", [*REASONING_EFFORTS, None])
def test_save_helper_accepts_the_literals_and_no_value(value: str | None):
    nodes = [
        {"id": "a", "type": "llm", "reasoning_effort": value},
        {"id": "b", "type": "react_agent", "reasoning_effort": value},
        {"id": "c", "type": "json_validate", "reasoning_effort": value},
        {"id": "d", "type": "llm"},
    ]

    assert policy.find_reasoning_effort_violations(nodes) == []
    assert policy.find_reasoning_effort_violations([]) == []


def test_save_helper_lists_violations_sorted_and_ignores_other_node_types():
    nodes = [
        {"id": "z", "type": "llm", "reasoning_effort": "max"},
        {"id": "a", "type": "react_agent", "reasoning_effort": "HIGH"},
        # 이 키를 읽지 않는 노드는 보지 않는다
        {"id": "m", "type": "http", "reasoning_effort": "max"},
        "not-a-node",
    ]

    assert policy.find_reasoning_effort_violations(nodes) == [  # type: ignore[arg-type]
        f"a: {EFFORT_REASON}",
        f"z: {EFFORT_REASON}",
    ]
    assert "find_reasoning_effort_violations" in policy.__all__
    assert "reasoning_effort_errors" in policy.__all__


# ── llm 노드 ──────────────────────────────────────────────────────────────


@pytest.fixture
def streams(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """스트림 게이트웨이 경계 대역 — 호출마다 키워드 인자를 남기고 `답`을 낸다."""
    calls: list[dict[str, Any]] = []

    async def stream(db: Any, model_id: Any, messages: Any, result: Any, **kw: Any) -> Any:
        calls.append(kw)
        result.finish_reason = "stop"

        async def gen() -> AsyncGenerator[str, None]:
            yield "답"

        return gen()

    monkeypatch.setattr(gateway, "complete_stream_with_fallback", stream)
    return calls


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """노드 실행 로그 — 노드 id → {input_data, output_data, ...}."""
    rows: dict[str, dict[str, Any]] = {}

    async def log_start(run_id: Any, node_id: str, node_type: str, input_data: Any, *a: Any) -> str:
        rows[node_id] = {"input_data": input_data}
        return node_id

    async def log_finish(log_id: str, **kw: Any) -> bool:
        rows[log_id].update(kw)
        return True

    monkeypatch.setattr(graph, "_log_node_start", log_start)
    monkeypatch.setattr(graph, "_log_node_finish", log_finish)
    return rows


async def test_llm_node_passes_the_effort_and_records_it(streams, logs):
    await graph.execute_agent(
        _ctx(), "workflow",
        {"nodes": [{"id": "gen", "type": "llm", "prompt": "요약", "reasoning_effort": "low"}],
         "edges": []},
    )  # fmt: skip

    assert [kw.get("reasoning_effort") for kw in streams] == ["low"]
    assert logs["gen"]["input_data"]["reasoning_effort"] == "low"
    assert logs["gen"]["output_data"]["llm_settings"] == {"reasoning_effort": "low"}


async def test_llm_node_without_the_key_calls_the_gateway_as_today(streams, logs):
    await graph.execute_agent(
        _ctx(), "workflow", {"nodes": [{"id": "gen", "type": "llm", "prompt": "요약"}], "edges": []}
    )

    # 키가 없으면 인자를 넘기지 않는다 — 옛 대역·제품 대역도 그대로 받는다
    assert ["reasoning_effort" in kw for kw in streams] == [False]
    assert "reasoning_effort" not in logs["gen"]["input_data"]
    assert "llm_settings" not in logs["gen"]["output_data"]


@pytest.mark.parametrize("value", INVALID)
async def test_llm_node_rejects_an_invalid_effort_before_calling_the_model(streams, value):
    node = {"id": "gen", "type": "llm", "prompt": "요약", "reasoning_effort": value}

    with pytest.raises(AgentRuntimeError, match=EFFORT_REASON):
        await llm_node._exec_llm(_ctx(), node, {})

    assert streams == []


@pytest.fixture
def tool_turns(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """tool 게이트웨이 경계 대역 — 첫 턴은 도구 호출, 다음 턴부터 답한다."""
    calls: list[dict[str, Any]] = []

    async def fake(db: Any, model_id: Any, messages: Any, tools: Any, **kw: Any) -> ToolTurnResult:
        calls.append(kw)
        if len(calls) == 1:
            call = ToolCall(id="c1", name="rag_search", arguments={"query": "q"})
            return ToolTurnResult(
                tool_calls=[call],
                assistant_message={"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "rag_search", "arguments": '{"query": "q"}'}}]},
            )  # fmt: skip
        answer = {"role": "assistant", "content": "답"}
        return ToolTurnResult(content="답", assistant_message=answer)

    async def dispatch(ctx: RunContext, name: str, args: dict[str, Any]) -> str:
        return "발췌 없음"

    async def quiet(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(gateway, "complete_with_tools", fake)
    monkeypatch.setattr(runtime, "_dispatch_builtin_tool", dispatch)
    monkeypatch.setattr(loop, "_log_node_start", quiet)
    monkeypatch.setattr(loop, "_log_node_finish", quiet)
    return calls


async def test_llm_tool_node_passes_the_effort_to_every_turn(tool_turns):
    node = {"id": "gen", "type": "llm", "prompt": "찾아", "tools": ["rag_search"],
            "reasoning_effort": "medium"}  # fmt: skip

    out = await llm_node._exec_llm(_ctx(), node, {})

    assert out["text"] == "답"
    assert [kw.get("reasoning_effort") for kw in tool_turns] == ["medium", "medium"]
    assert out["llm_settings"] == {"reasoning_effort": "medium"}


async def test_llm_tool_node_without_the_key_calls_the_gateway_as_today(tool_turns):
    node = {"id": "gen", "type": "llm", "prompt": "찾아", "tools": ["rag_search"]}

    out = await llm_node._exec_llm(_ctx(), node, {})

    assert ["reasoning_effort" in kw for kw in tool_turns] == [False, False]
    assert "llm_settings" not in out


# ── react_agent ───────────────────────────────────────────────────────────


def _react(**kw: Any) -> dict[str, Any]:
    return {"id": "r", "type": "react_agent", "goal": "조사", "allowed_tools": ["rag_search"], **kw}


async def test_react_accepts_the_key_and_uses_it_for_turns_and_the_closing_stream(
    tool_turns, streams
):
    # 반복 1회 — 도구 턴 뒤 루프 소진 마무리 스트림까지 같은 값을 쓴다
    out = await loop.run_react_node(_ctx(), _react(reasoning_effort="high", max_iters=1))

    assert out["text"] == "답"
    assert [kw.get("reasoning_effort") for kw in tool_turns] == ["high"]
    assert [kw.get("reasoning_effort") for kw in streams] == ["high"]
    assert "reasoning_effort" in loop._REACT_CONFIG_KEYS


async def test_react_without_the_key_calls_the_gateway_as_today(tool_turns, streams):
    await loop.run_react_node(_ctx(), _react(max_iters=1))

    assert ["reasoning_effort" in kw for kw in tool_turns + streams] == [False, False]


@pytest.mark.parametrize("value", ["xhigh", "{params.effort}"])
async def test_react_rejects_an_invalid_effort_before_tools_and_model(
    monkeypatch, tool_turns, value
):
    ran: list[str] = []

    async def dispatch(ctx: RunContext, name: str, args: dict[str, Any]) -> str:
        ran.append(name)
        return "발췌"

    monkeypatch.setattr(runtime, "_dispatch_builtin_tool", dispatch)
    node = _react(reasoning_effort=value, required_initial_tools=["rag_search"])

    with pytest.raises(AgentRuntimeError, match=EFFORT_REASON):
        await loop.run_react_node(_ctx(), node)

    assert (ran, tool_turns) == ([], [])  # 필수 초기 도구도 실행하지 않았다


# ── json_validate 재시도 상속(D4) ─────────────────────────────────────────


@pytest.fixture
def retries(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """재시도 스트림 대역 — 유효한 JSON을 내고 키워드 인자를 남긴다."""
    calls: list[dict[str, Any]] = []

    async def stream(db: Any, model_id: Any, messages: Any, result: Any, **kw: Any) -> Any:
        calls.append(kw)
        result.finish_reason = "stop"

        async def gen() -> AsyncGenerator[str, None]:
            yield json.dumps(SLIDE_OUTLINE)

        return gen()

    monkeypatch.setattr(gateway, "complete_stream_with_fallback", stream)
    return calls


def _validate(**kw: Any) -> dict[str, Any]:
    return {"id": "check", "type": "json_validate", "schema": "slide_outline",
            "source": "{outline.text}", "prompt": "슬라이드 개요 JSON", **kw}  # fmt: skip


def _outline(**settings: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"text": "JSON 아님", "finish_reason": "stop"}
    if settings:
        out["llm_settings"] = settings
    return {"outline": out}


@pytest.mark.parametrize(
    ("node_kw", "state", "expected"),
    [
        pytest.param({}, _outline(reasoning_effort="low"), "low", id="inherits"),
        pytest.param({"reasoning_effort": "high"}, _outline(reasoning_effort="low"), "high",
                     id="own-key-first"),
        pytest.param({"reasoning_effort": "medium"}, _outline(), "medium", id="own-key-only"),
        # 명시적 null은 자기 값이다(보내지 않는다) — 원 노드 값으로 덮지 않는다
        pytest.param({"reasoning_effort": None}, _outline(reasoning_effort="high"), None,
                     id="explicit-null"),
        pytest.param({}, _outline(), None, id="neither"),
        # 물려받을 수 없는 값은 버린다 — 재시도를 실패시키지 않는다
        pytest.param({}, _outline(reasoning_effort="xhigh"), None, id="invalid-inherited"),
        pytest.param({}, {"outline": {"text": "x", "llm_settings": "low"}}, None,
                     id="malformed-settings"),
    ],
)  # fmt: skip
async def test_json_validate_retry_uses_own_then_source_effort(
    retries, node_kw: dict[str, Any], state: dict[str, Any], expected: str | None
):
    out = await validation._exec_json_validate(_ctx(), _validate(**node_kw), state)

    assert out["json"]["title"] == "개요"
    [kw] = retries
    assert kw.get("reasoning_effort") == expected
    assert ("reasoning_effort" in kw) is (expected is not None)


async def test_json_validate_does_not_inherit_from_run_input(retries):
    """실행 입력(`params`)은 노드 출력이 아니다 — 사용자가 재시도 설정을 고르지 못한다."""
    node = _validate(source="{params.text}")
    state = {"params": {"text": "JSON 아님", "llm_settings": {"reasoning_effort": "high"}}}

    await validation._exec_json_validate(_ctx(), node, state)

    [kw] = retries
    assert "reasoning_effort" not in kw


async def test_json_validate_without_a_retry_does_not_call_the_model(retries):
    state = {"outline": {"text": json.dumps(SLIDE_OUTLINE), "llm_settings": {
        "reasoning_effort": "low"}}}  # fmt: skip

    await validation._exec_json_validate(_ctx(), _validate(), state)

    assert retries == []


@pytest.mark.parametrize("value", ["xhigh", "{params.effort}"])
async def test_json_validate_rejects_its_own_invalid_effort(retries, value):
    with pytest.raises(AgentRuntimeError, match=EFFORT_REASON):
        await validation._exec_json_validate(
            _ctx(), _validate(reasoning_effort=value), _outline(reasoning_effort="low")
        )

    assert retries == []


# ── 노드 경로: 실제 LiteLLM으로 요청 본문까지 ─────────────────────────────


class _OpenAICompatible:
    """OpenAI 호환 `/v1/chat/completions` 스트림 — 차례로 `texts`를 낸다."""

    def __init__(self, texts: list[str]) -> None:
        self.bodies: list[dict[str, Any]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                server.bodies.append(body)
                text = texts.pop(0)
                base = {"id": "c1", "object": "chat.completion.chunk", "created": 0,
                        "model": body["model"]}  # fmt: skip
                chunks = [
                    {**base, "choices": [{"index": 0, "finish_reason": None,
                                          "delta": {"role": "assistant", "content": text}}]},
                    {**base, "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]},
                    {**base, "choices": [], "usage": {
                        "prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}},
                ]  # fmt: skip
                raw = (
                    "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
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

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def compatible() -> Iterator[_OpenAICompatible]:
    srv = _OpenAICompatible(["JSON 아님", json.dumps(SLIDE_OUTLINE)])
    yield srv
    srv.close()


async def test_llm_node_effort_reaches_the_request_and_the_json_validate_retry(
    monkeypatch, compatible, logs
):
    provider = LLMProvider(
        id=uuid.uuid4(),
        name="p-custom",
        type="custom",
        base_url=f"{compatible.root}/v1",
        api_key_encrypted=encrypt("test-secret"),
    )
    model = LLMModel(id=uuid.uuid4(), provider_id=provider.id, model_name="openai/gpt-oss-120b",
                     display_name="gpt-oss", capabilities=["llm"])  # fmt: skip
    resolved = ResolvedModel(model=model, provider=provider)

    async def fake_chain(db: Any, model_id: Any, modality: str = "llm") -> list[ResolvedModel]:
        return [resolved]

    async def closed(provider_id: uuid.UUID) -> bool:
        return False

    monkeypatch.setattr(gateway, "resolve_chain", fake_chain)
    monkeypatch.setattr(circuit, "is_open", closed)
    monkeypatch.setattr(circuit, "record_success", closed)
    definition = {
        "nodes": [
            {"id": "outline", "type": "llm", "prompt": "개요", "reasoning_effort": "low"},
            _validate(),
        ],
        "edges": [["outline", "check"]],
    }

    await graph.execute_agent(_ctx(), "workflow", definition)

    # 원 노드 호출과 json_validate 재시도 호출 모두 같은 사고 양을 싣는다
    assert [body.get("reasoning_effort") for body in compatible.bodies] == ["low", "low"]
    assert logs["outline"]["output_data"]["llm_settings"] == {"reasoning_effort": "low"}
    assert logs["check"]["output_data"]["json"]["title"] == "개요"
