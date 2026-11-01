"""후보별 출력 상한 — 노드 출력 하한(`min_output_tokens`)과 게이트웨이 호출 계획(#428).

llm·json_validate 실행기, `_ContextFit`, 게이트웨이 폴백 루프는 실제 코드를 탄다. 모델 호출은 실제
LiteLLM으로 로컬 OpenAI 호환 서버(사고 양은 로컬 Anthropic 대역)에 보낸다. 외부 경계(원격 LLM
서버, 서킷 브레이커의 Redis, 모델 조회 DB, 원장 쓰기)만 가짜다. source_load 출력은 실제
`_exec_source_load`로 만든다(`test_llm_context_fit`의 소스 DB 대역).
"""

import json
import threading
import uuid
from collections.abc import AsyncGenerator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from structlog.testing import capture_logs

from biznotes_core.core.crypto import encrypt
from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.services.agents import graph, policy
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError, LLMOutputTruncatedError
from biznotes_core.services.agents.nodes import llm as llm_node
from biznotes_core.services.agents.nodes import validation
from biznotes_core.services.agents.nodes.llm import _build_messages, _exec_llm
from biznotes_core.services.gateway import circuit
from biznotes_core.services.gateway import llm as gateway
from biznotes_core.services.gateway.llm import (
    OUTPUT_FLOOR_UNMET,
    CallPlan,
    GatewayCallError,
    GatewayContextLengthError,
    ResolvedModel,
    StreamResult,
    complete_stream_with_fallback,
    estimate_prompt_tokens,
)
from tests.test_gateway_reasoning_effort import (  # noqa: F401 — 번들 모델 맵 픽스처(autouse)
    HAIKU,
    _bundled_model_map,
    _resolved,
    _Server,
)
from tests.test_llm_context_fit import PROMPT, SYSTEM, _loaded

SLIDE_OUTLINE = {"title": "개요", "slides": [{"title": "도입", "points": ["a"]}]}
# 저장 헬퍼·실행 전 검사의 사유
POSITIVE = "min_output_tokens는 양의 정수여야 합니다"


def _ctx() -> RunContext:
    return RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={})  # type: ignore[arg-type]


def _reserve(window: int) -> int:
    return max(256, int(window * 0.05))


class _Compatible:
    """OpenAI 호환 `/v1/chat/completions` 스트림 대역 — 요청 본문을 남기고 `replies`를 차례로 낸다.

    응답은 `(본문, 종료 사유)`다. 다 쓰면 `("답", "stop")`이다.
    """

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.replies: list[tuple[str, str]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                server.bodies.append(body)
                text, finish = server.replies.pop(0) if server.replies else ("답", "stop")
                base = {"id": "c1", "object": "chat.completion.chunk", "created": 0,
                        "model": body["model"]}  # fmt: skip
                chunks = [
                    {**base, "choices": [{"index": 0, "finish_reason": None,
                                          "delta": {"role": "assistant", "content": text}}]},
                    {**base, "choices": [{"index": 0, "finish_reason": finish, "delta": {}}]},
                    {**base, "choices": [], "usage": {
                        "prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10}},
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
def server() -> Iterator[_Compatible]:
    srv = _Compatible()
    yield srv
    srv.close()


def _model(server: _Compatible, name: str, window: int | None) -> ResolvedModel:
    provider = LLMProvider(
        id=uuid.uuid4(),
        name=f"p-{name}",
        type="custom",
        base_url=f"{server.root}/v1",
        api_key_encrypted=encrypt("test-secret"),
    )
    model = LLMModel(
        id=uuid.uuid4(),
        provider_id=provider.id,
        model_name=f"openai/{name}",
        display_name=name,
        capabilities=["llm"],
        context_window=window,
    )
    return ResolvedModel(model=model, provider=provider)


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """모델 조회·서킷 대역 — `models`가 후보 체인이고, 서킷에 센 실패는 `failures`에 남는다."""
    state: dict[str, Any] = {"models": [], "failures": []}

    async def fake_chain(db: Any, model_id: Any, modality: str = "llm") -> list[ResolvedModel]:
        return list(state["models"])

    async def closed(provider_id: uuid.UUID) -> bool:
        return False

    async def failure(provider_id: uuid.UUID) -> bool:
        state["failures"].append(provider_id)
        return False

    monkeypatch.setattr(gateway, "resolve_chain", fake_chain)
    monkeypatch.setattr(circuit, "is_open", closed)
    monkeypatch.setattr(circuit, "record_success", closed)
    monkeypatch.setattr(circuit, "record_failure", failure)
    return state


@pytest.fixture
def gateway_kwargs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """실제 게이트웨이를 그대로 부르고 노드가 넘긴 키워드 인자만 남긴다."""
    calls: list[dict[str, Any]] = []
    real = gateway.complete_stream_with_fallback

    async def spy(*a: Any, **k: Any) -> Any:
        calls.append(k)
        return await real(*a, **k)

    monkeypatch.setattr(gateway, "complete_stream_with_fallback", spy)
    return calls


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """노드 실행 로그 — 노드 id → {input_data, output_data, status, error_code, ...}."""
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


def _node(**kw: Any) -> dict[str, Any]:
    base = {"id": "gen", "type": "llm", "system": SYSTEM, "prompt": PROMPT, "max_tokens": 16_384}
    return {**base, **kw}


def _budget(m: ResolvedModel, input_budget: int | None, max_tokens: int, skipped: bool) -> dict:
    return {
        "model_id": str(m.model.id),
        "display_name": m.model.display_name,
        "context_window": m.model.context_window,
        "input_budget": input_budget,
        "max_tokens": max_tokens,
        "skipped": skipped,
    }


# ── 하한이 없으면 지금 동작 그대로다(회귀) ────────────────────────────────


async def test_without_a_floor_source_fit_keeps_the_old_gateway_call(
    server, chain, gateway_kwargs
):
    load = await _loaded()
    small = _model(server, "gpt-oss-32k", 32_768)
    chain["models"] = [small]

    out = await _exec_llm(_ctx(), _node(), {"load": load})

    [kw] = gateway_kwargs
    assert "fit_messages" in kw and "plan_call" not in kw
    [body] = server.bodies
    # 출력 몫은 max_tokens 그대로이고 입력이 그만큼 줄어든다(#357)
    assert body["max_tokens"] == 16_384
    assert estimate_prompt_tokens(body["messages"]) <= 32_768 - 16_384 - _reserve(32_768)
    assert "output_budget" not in out and "llm_settings" not in out
    assert out["dropped"]


async def test_without_a_floor_or_source_refs_the_gateway_call_is_unchanged(
    server, chain, gateway_kwargs
):
    chain["models"] = [_model(server, "small", 4_096)]
    node = {"id": "gen", "type": "llm", "prompt": "요약", "max_tokens": 8_192}

    out = await _exec_llm(_ctx(), node, {})

    [kw] = gateway_kwargs
    assert "fit_messages" not in kw and "plan_call" not in kw
    [body] = server.bodies
    assert body["max_tokens"] == 8_192  # 컨텍스트보다 커도 지금처럼 그대로 보낸다
    assert "output_budget" not in out


# ── 하한이 있으면 후보마다 입력 몫을 먼저 지킨다 ─────────────────────────


async def test_32k_candidate_keeps_the_input_and_gets_the_rest_as_output(
    server, chain, gateway_kwargs
):
    load = await _loaded()
    small = _model(server, "gpt-oss-32k", 32_768)
    chain["models"] = [small]

    out = await _exec_llm(_ctx(), _node(min_output_tokens=4_096), {"load": load})

    [kw] = gateway_kwargs
    assert "plan_call" in kw and "fit_messages" not in kw
    [body] = server.bodies
    reserve = _reserve(32_768)
    input_budget = 32_768 - 4_096 - reserve
    prompt = estimate_prompt_tokens(body["messages"])
    assert prompt <= input_budget
    # 하한이 없을 때(입력 예산 32k − 16k − 여유)보다 입력을 훨씬 많이 넣는다
    assert prompt > 32_768 - 16_384 - reserve
    # 출력 상한은 남은 몫이다 — 하한 이상, max_tokens 미만
    assert body["max_tokens"] == 32_768 - prompt - reserve
    assert 4_096 <= body["max_tokens"] < 16_384
    assert out["output_budget"] == [_budget(small, input_budget, body["max_tokens"], False)]
    assert out["dropped"]  # 그래도 넘치는 원문은 뺀다


async def test_large_window_candidate_uses_max_tokens(server, chain):
    load = await _loaded()
    big = _model(server, "gemini-1m", 1_000_000)
    chain["models"] = [big]

    out = await _exec_llm(_ctx(), _node(min_output_tokens=4_096), {"load": load})

    [body] = server.bodies
    assert body["max_tokens"] == 16_384
    sent = [{"role": m["role"], "content": m["content"]} for m in body["messages"]]
    assert sent == _build_messages(SYSTEM, PROMPT, {"load": load}, None)  # 원문을 다 넣는다
    input_budget = 1_000_000 - 4_096 - _reserve(1_000_000)
    assert out["output_budget"] == [_budget(big, input_budget, 16_384, False)]
    assert "dropped" not in out


@pytest.mark.parametrize("window", [None, 0])
async def test_unknown_window_sends_max_tokens_without_fitting(
    server, chain, monkeypatch, window
):
    load = await _loaded()
    unknown = _model(server, "custom", window)
    chain["models"] = [unknown]

    def boom(_m: Any) -> int:
        raise AssertionError("한도를 모르면 토큰을 세지 않는다")

    monkeypatch.setattr(gateway, "estimate_prompt_tokens", boom)

    out = await _exec_llm(_ctx(), _node(min_output_tokens=4_096), {"load": load})

    [body] = server.bodies
    assert body["max_tokens"] == 16_384
    assert out["output_budget"] == [_budget(unknown, None, 16_384, False)]


async def test_floor_plans_the_output_even_without_source_refs(server, chain, gateway_kwargs):
    small = _model(server, "small", 4_096)
    chain["models"] = [small]
    node = {"id": "gen", "type": "llm", "prompt": "요약", "max_tokens": 8_192,
            "min_output_tokens": 1_024}  # fmt: skip

    out = await _exec_llm(_ctx(), node, {})

    [kw] = gateway_kwargs
    assert "plan_call" in kw
    [body] = server.bodies
    prompt = estimate_prompt_tokens([{"role": "user", "content": "요약"}])
    expected = 4_096 - prompt - _reserve(4_096)
    assert body["max_tokens"] == expected
    assert body["messages"][-1]["content"] == "요약"  # 줄일 원문이 없으면 프롬프트는 그대로다
    assert out["output_budget"] == [
        _budget(small, 4_096 - 1_024 - _reserve(4_096), expected, False)
    ]


# ── 하한 미달 후보는 건너뛴다 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("window", "prompt"),
    [
        # 컨텍스트가 하한 + 여유보다 작다
        pytest.param(4_096, "요약", id="window-below-floor"),
        # 줄일 원문이 없는 고정 프롬프트만으로 하한 몫이 남지 않는다
        pytest.param(32_768, "요약 " * 30_000, id="fixed-prompt-overflow"),
    ],
)
async def test_candidate_below_the_floor_is_skipped_for_the_next(
    server, chain, window, prompt
):
    small, big = _model(server, "small", window), _model(server, "big", 1_000_000)
    chain["models"] = [small, big]
    node = {"id": "gen", "type": "llm", "prompt": prompt, "max_tokens": 8_192,
            "min_output_tokens": 4_096}  # fmt: skip

    with capture_logs() as captured:
        out = await _exec_llm(_ctx(), node, {})

    # 건너뛴 후보에는 요청이 가지 않았다
    assert [b["model"] for b in server.bodies] == ["big"]
    assert out["model"]["display_name"] == "big" and out["fallback_used"] is True
    [skipped, used] = out["output_budget"]
    assert (skipped["display_name"], skipped["skipped"]) == ("small", True)
    assert skipped["max_tokens"] < 4_096
    assert used == _budget(big, 1_000_000 - 4_096 - _reserve(1_000_000), 8_192, False)
    # 서킷에 넣지 않는다
    assert chain["failures"] == []
    [event] = [e for e in captured if e["event"] == "llm candidate skipped by call plan"]
    assert (event["code"], event["model"]) == (OUTPUT_FLOOR_UNMET, "openai/small")


async def test_every_candidate_below_the_floor_fails_as_context_length(
    server, chain, logs
):
    a, b = _model(server, "a", 2_048), _model(server, "b", 4_096)
    chain["models"] = [a, b]
    definition = {"nodes": [{"id": "gen", "type": "llm", "prompt": "요약", "max_tokens": 4_096,
                             "min_output_tokens": 4_096}], "edges": []}  # fmt: skip

    with pytest.raises(AgentRuntimeError):
        await graph.execute_agent(_ctx(), "workflow", definition)

    log = logs["gen"]
    assert (log["status"], log["error_code"]) == ("failed", "context_length_exceeded")
    assert log["output_data"] == {
        "model_attempts": [
            {"id": str(m.model.id), "display_name": m.model.display_name,
             "error_code": OUTPUT_FLOOR_UNMET}
            for m in (a, b)
        ]
    }  # fmt: skip
    assert server.bodies == []
    assert chain["failures"] == []


# ── 게이트웨이 훅 ─────────────────────────────────────────────────────────


async def test_plan_call_comes_before_fit_messages(server, chain):
    small = _model(server, "small", 8_192)
    chain["models"] = [small]
    planned = [{"role": "user", "content": "계획한 메시지"}]

    def fit(_candidate: ResolvedModel) -> list[dict[str, str]]:
        raise AssertionError("plan_call이 있으면 fit_messages를 부르지 않는다")

    def plan(candidate: ResolvedModel) -> CallPlan:
        assert candidate is small
        return CallPlan(messages=planned, max_tokens=1_500)

    result = StreamResult()
    gen = await complete_stream_with_fallback(
        None, None, [{"role": "user", "content": "원래 메시지"}], result,  # type: ignore[arg-type]
        max_tokens=4_096, fit_messages=fit, plan_call=plan,
    )  # fmt: skip
    assert "".join([d async for d in gen]) == "답"

    [body] = server.bodies
    assert body["max_tokens"] == 1_500
    assert body["messages"][-1]["content"] == "계획한 메시지"
    assert (result.model, result.max_tokens_used) == (small, 1_500)


async def test_without_a_plan_the_used_limit_is_max_tokens(server, chain):
    chain["models"] = [_model(server, "m", 8_192)]
    result = StreamResult()

    gen = await complete_stream_with_fallback(
        None, None, [{"role": "user", "content": "x"}], result, max_tokens=2_000  # type: ignore[arg-type]
    )
    [_ async for _ in gen]

    assert result.max_tokens_used == 2_000
    assert StreamResult().max_tokens_used is None  # 스트림 전에는 모른다


async def test_other_skip_codes_are_not_context_failures(server, chain):
    """출력 하한 미달이 아닌 건너뛰기는 컨텍스트 초과로 세지 않는다(서킷에도 넣지 않는다)."""
    only = _model(server, "m", 8_192)
    chain["models"] = [only]

    def plan(_candidate: ResolvedModel) -> CallPlan:
        return CallPlan(messages=[], max_tokens=1, skip_code="caller_skip")

    with pytest.raises(GatewayCallError) as caught:
        await complete_stream_with_fallback(
            None, None, [{"role": "user", "content": "x"}], StreamResult(), plan_call=plan  # type: ignore[arg-type]
        )

    assert not isinstance(caught.value, GatewayContextLengthError)
    assert caught.value.model_attempts == [
        {"id": str(only.model.id), "display_name": "m", "error_code": "caller_skip"}
    ]
    assert server.bodies == [] and chain["failures"] == []


# ── 잘림 표시는 실효 상한이다 ─────────────────────────────────────────────


async def test_output_truncated_reports_the_effective_limit(server, chain):
    chain["models"] = [_model(server, "small", 4_096)]
    server.replies = [('{"title": "개', "length")]
    node = {"id": "gen", "type": "llm", "prompt": "요약", "max_tokens": 8_192,
            "min_output_tokens": 1_024}  # fmt: skip

    out = await _exec_llm(_ctx(), node, {})

    [body] = server.bodies
    assert body["max_tokens"] < 8_192
    assert out["output_truncated"] == {"max_tokens": body["max_tokens"], "completion_tokens": 7}

    # json_validate는 원 노드 출력의 실효 상한으로 끝낸다 — 재시도하지 않는다
    check = {"id": "check", "type": "json_validate", "schema": "slide_outline",
             "source": "{gen.text}", "prompt": "개요 JSON"}  # fmt: skip
    with pytest.raises(LLMOutputTruncatedError, match=rf"max_tokens={body['max_tokens']}\)"):
        await validation._exec_json_validate(_ctx(), check, {"gen": out})
    assert len(server.bodies) == 1


async def test_json_validate_retry_truncation_reports_the_effective_limit(server, chain):
    chain["models"] = [_model(server, "small", 4_096)]
    server.replies = [('{"title": "개', "length")]
    check = _validate(prompt="개요 JSON", min_output_tokens=1_024)

    with pytest.raises(LLMOutputTruncatedError) as caught:
        await validation._exec_json_validate(_ctx(), check, {"outline": {"text": "JSON 아님"}})

    [body] = server.bodies
    assert body["max_tokens"] < 4_096
    assert f"max_tokens={body['max_tokens']})" in str(caught.value)


# ── 결과·로그 키 ──────────────────────────────────────────────────────────


async def test_floor_is_logged_and_carried_in_llm_settings(server, chain, logs):
    chain["models"] = [_model(server, "big", 1_000_000)]
    definition = {"nodes": [{"id": "gen", "type": "llm", "prompt": "요약", "max_tokens": 8_192,
                             "min_output_tokens": 2_048}], "edges": []}  # fmt: skip

    await graph.execute_agent(_ctx(), "workflow", definition)

    assert logs["gen"]["input_data"]["min_output_tokens"] == 2_048
    out = logs["gen"]["output_data"]
    assert out["llm_settings"] == {"min_output_tokens": 2_048}
    assert [b["max_tokens"] for b in out["output_budget"]] == [8_192]
    assert "min_output_tokens" in graph._LOG_INPUT_KEYS


# ── json_validate 재시도 상속(D4) ─────────────────────────────────────────


def _validate(**kw: Any) -> dict[str, Any]:
    return {"id": "check", "type": "json_validate", "schema": "slide_outline",
            "source": "{outline.text}", "prompt": "슬라이드 개요 JSON", **kw}  # fmt: skip


def _outline(**settings: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"text": "JSON 아님", "finish_reason": "stop"}
    if settings:
        out["llm_settings"] = settings
    return {"outline": out}


# 4k 후보에서 남는 출력 몫(약 3,780)보다 큰 하한 — 이 하한이 쓰이면 후보를 건너뛴다
HIGH_FLOOR = 3_900


@pytest.mark.parametrize(
    ("node_kw", "state", "outcome"),
    [
        pytest.param({}, _outline(min_output_tokens=HIGH_FLOOR), "skipped", id="inherits"),
        pytest.param({"min_output_tokens": 1_024}, _outline(min_output_tokens=HIGH_FLOOR),
                     "planned", id="own-key-first"),
        pytest.param({"min_output_tokens": HIGH_FLOOR}, _outline(), "skipped", id="own-key-only"),
        pytest.param({}, _outline(min_output_tokens=1_024), "planned", id="inherits-and-fits"),
        pytest.param({}, _outline(), "as-today", id="neither"),
        # 명시적 null은 자기 값이다(하한 없음) — 물려받지 않는다
        pytest.param({"min_output_tokens": None}, _outline(min_output_tokens=HIGH_FLOOR),
                     "as-today", id="own-null"),
        # 물려받을 수 없는 값은 버린다 — 재시도는 지금처럼 간다
        pytest.param({}, _outline(min_output_tokens=0), "as-today", id="invalid-inherited"),
        # 재시도 상한(이 노드의 max_tokens, 기본 4096)보다 큰 하한은 물려받지 않는다
        pytest.param({}, _outline(min_output_tokens=8_192), "as-today", id="above-retry-max"),
    ],
)  # fmt: skip
async def test_json_validate_retry_uses_own_then_source_floor(
    server, chain, node_kw: dict[str, Any], state: dict[str, Any], outcome: str
):
    small = _model(server, "small", 4_096)
    chain["models"] = [small]
    server.replies = [(json.dumps(SLIDE_OUTLINE), "stop")]

    if outcome == "skipped":
        with pytest.raises(GatewayContextLengthError) as caught:
            await validation._exec_json_validate(_ctx(), _validate(**node_kw), state)
        assert server.bodies == []
        assert [a["error_code"] for a in caught.value.model_attempts or []] == [OUTPUT_FLOOR_UNMET]
        return

    out = await validation._exec_json_validate(_ctx(), _validate(**node_kw), state)

    assert out["json"]["title"] == "개요"
    [body] = server.bodies
    if outcome == "as-today":
        assert body["max_tokens"] == 4_096
    else:
        # 하한이 있으면 llm 노드와 같은 규칙으로 남은 몫을 상한으로 준다
        prompt = estimate_prompt_tokens(body["messages"])
        assert body["max_tokens"] == 4_096 - prompt - _reserve(4_096) < 4_096


async def test_json_validate_does_not_inherit_the_floor_from_run_input(server, chain):
    chain["models"] = [_model(server, "small", 4_096)]
    server.replies = [(json.dumps(SLIDE_OUTLINE), "stop")]
    state = {"params": {"text": "JSON 아님", "llm_settings": {"min_output_tokens": HIGH_FLOOR}}}

    await validation._exec_json_validate(_ctx(), _validate(source="{params.text}"), state)

    [body] = server.bodies
    assert body["max_tokens"] == 4_096


# ── json_validate 재시도의 출력 예산이 실행 로그에 남는다(#428 리뷰) ───────


def _retry_workflow(outline_kw: dict[str, Any], check_kw: dict[str, Any]) -> dict[str, Any]:
    outline = {"id": "outline", "type": "llm", "prompt": "개요", "max_tokens": 4_096, **outline_kw}
    return {"nodes": [outline, _validate(**check_kw)], "edges": [["outline", "check"]]}


@pytest.mark.parametrize(
    ("outline_kw", "check_kw", "requested"),
    [
        # 원 노드에는 하한이 없다 — 원 호출은 1k 후보가 받고, 재시도만 1k 후보를 건너뛴다
        pytest.param({}, {"min_output_tokens": 1_024}, ["tiny", "big"], id="own-floor"),
        # 원 노드의 하한을 물려받는다 — 원 호출과 재시도 모두 1k 후보를 건너뛴다
        pytest.param({"min_output_tokens": 1_024}, {}, ["big", "big"], id="inherited-floor"),
    ],
)
async def test_json_validate_retry_budget_reaches_the_execution_log(
    server, chain, logs, outline_kw, check_kw, requested
):
    tiny, big = _model(server, "tiny", 1_024), _model(server, "big", 32_768)
    chain["models"] = [tiny, big]
    server.replies = [("JSON 아님", "stop"), (json.dumps(SLIDE_OUTLINE), "stop")]

    await graph.execute_agent(_ctx(), "workflow", _retry_workflow(outline_kw, check_kw))

    assert [b["model"].removeprefix("openai/") for b in server.bodies] == requested
    out = logs["check"]["output_data"]
    assert out["json"]["title"] == "개요"
    [skipped, used] = out["output_budget"]
    assert (skipped["display_name"], skipped["skipped"]) == ("tiny", True)
    assert used == _budget(big, 32_768 - 1_024 - _reserve(32_768), 4_096, False)
    # 결과 키는 출력 예산 하나만 늘고, 검증한 JSON·마인드맵 절단 표시와 섞이지 않는다
    assert out.keys() == {"json", "text", "truncated", "output_budget"}
    assert out["truncated"] is False and "output_budget" not in out["json"]


async def test_json_validate_retry_budget_stays_apart_from_mindmap_truncation(
    server, chain, logs
):
    chain["models"] = [_model(server, "tiny", 1_024), _model(server, "big", 32_768)]
    mindmap = {"root": {"id": "root", "label": "주제", "children": []}}
    server.replies = [("JSON 아님", "stop"), (json.dumps(mindmap), "stop")]
    definition = _retry_workflow({}, {"schema": "mindmap", "min_output_tokens": 1_024})

    await graph.execute_agent(_ctx(), "workflow", definition)

    out = logs["check"]["output_data"]
    assert out["truncated"] is False and out["json"]["truncated"] is False
    assert [b["skipped"] for b in out["output_budget"]] == [True, False]
    assert "output_budget" not in out["json"]


@pytest.mark.parametrize(
    ("outline_kw", "check_kw", "replies"),
    [
        # 하한이 없는 재시도 — 결과 키는 지금 그대로다
        pytest.param({}, {}, ["JSON 아님", json.dumps(SLIDE_OUTLINE)], id="retry-without-floor"),
        # 하한이 있어도 재시도가 없으면 결과 키는 지금 그대로다
        pytest.param({"min_output_tokens": 1_024}, {"min_output_tokens": 1_024},
                     [json.dumps(SLIDE_OUTLINE)], id="floor-without-retry"),
    ],
)  # fmt: skip
async def test_json_validate_result_keys_are_unchanged_without_a_retry_budget(
    server, chain, logs, outline_kw, check_kw, replies
):
    chain["models"] = [_model(server, "big", 32_768)]
    server.replies = [(text, "stop") for text in replies]

    await graph.execute_agent(_ctx(), "workflow", _retry_workflow(outline_kw, check_kw))

    assert len(server.bodies) == len(replies)
    assert logs["check"]["output_data"].keys() == {"json", "text", "truncated"}


# ── 정의 오류 ─────────────────────────────────────────────────────────────


@pytest.fixture
def no_model_call(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """모델 호출 경계 — 불리면 기록한다(정의 오류는 호출 전에 나야 한다)."""
    calls: list[str] = []

    async def stream(*a: Any, **k: Any) -> AsyncGenerator[str, None]:
        calls.append("stream")
        raise AssertionError("모델을 부르지 않는다")

    async def tools(*a: Any, **k: Any) -> Any:
        calls.append("tools")
        raise AssertionError("모델을 부르지 않는다")

    monkeypatch.setattr(gateway, "complete_stream_with_fallback", stream)
    monkeypatch.setattr(gateway, "complete_with_tools", tools)
    return calls


INVALID = [0, -1, True, False, 1.5, "1024", "{params.n}", [1024]]


@pytest.mark.parametrize("value", INVALID)
async def test_llm_node_rejects_a_non_positive_or_non_integer_floor(no_model_call, value):
    node = {"id": "gen", "type": "llm", "prompt": "요약", "min_output_tokens": value}

    with pytest.raises(AgentRuntimeError, match=POSITIVE):
        await _exec_llm(_ctx(), node, {})

    assert no_model_call == []


@pytest.mark.parametrize(
    ("node_kw", "reason"),
    [
        pytest.param({"max_tokens": 2_048, "min_output_tokens": 2_049},
                     r"max_tokens\(2048\) 이하", id="above-max-tokens"),
        # max_tokens가 없으면 실행기 기본값(4096)과 견준다
        pytest.param({"min_output_tokens": 4_097}, r"max_tokens\(4096\) 이하", id="above-default"),
        pytest.param({"max_tokens": "8192", "min_output_tokens": 1_024},
                     "max_tokens가 정수", id="non-integer-max-tokens"),
        # 하한은 스트림 경로만 지킨다 — 도구 경로(D7 범위 밖)에는 둘 수 없다
        pytest.param({"tools": ["web_search"], "min_output_tokens": 1_024},
                     r"도구\(tools\)를 쓰지 않는", id="tool-path"),
    ],
)  # fmt: skip
async def test_llm_node_rejects_a_floor_it_cannot_keep(no_model_call, node_kw, reason):
    node = {"id": "gen", "type": "llm", "prompt": "요약", **node_kw}

    with pytest.raises(AgentRuntimeError, match=reason):
        await _exec_llm(_ctx(), node, {})

    assert no_model_call == []


async def test_llm_node_accepts_a_floor_equal_to_max_tokens(server, chain):
    chain["models"] = [_model(server, "big", 1_000_000)]
    node = {"id": "gen", "type": "llm", "prompt": "요약", "max_tokens": 2_048,
            "min_output_tokens": 2_048}  # fmt: skip

    out = await _exec_llm(_ctx(), node, {})

    assert out["output_budget"][0]["max_tokens"] == 2_048


@pytest.mark.parametrize("value", [0, "2048", 5_000])
async def test_json_validate_rejects_its_own_floor_before_calling_the_model(no_model_call, value):
    with pytest.raises(AgentRuntimeError, match="min_output_tokens"):
        await validation._exec_json_validate(_ctx(), _validate(min_output_tokens=value), {
            "outline": {"text": json.dumps(SLIDE_OUTLINE)}})  # fmt: skip

    assert no_model_call == []


# ── 저장 헬퍼 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("node_type", ["llm", "json_validate"])
@pytest.mark.parametrize("value", INVALID)
def test_save_helper_rejects_invalid_floors(node_type: str, value: Any):
    nodes = [{"id": "n", "type": node_type, "min_output_tokens": value}]

    assert policy.find_min_output_tokens_violations(nodes) == [f"n: {POSITIVE}"]


def test_save_helper_accepts_valid_floors_and_ignores_other_types():
    nodes = [
        {"id": "a", "type": "llm", "max_tokens": 8_192, "min_output_tokens": 8_192},
        {"id": "b", "type": "json_validate", "min_output_tokens": 1},
        {"id": "c", "type": "llm", "min_output_tokens": None},
        {"id": "d", "type": "llm"},
        # 이 키를 읽지 않는 노드는 보지 않는다(react_agent는 모르는 키를 실행 시 거부한다)
        {"id": "e", "type": "react_agent", "min_output_tokens": -1},
        "not-a-node",
    ]

    assert policy.find_min_output_tokens_violations(nodes) == []  # type: ignore[arg-type]


def test_save_helper_lists_violations_sorted():
    nodes = [
        {"id": "z", "type": "llm", "max_tokens": 1_000, "min_output_tokens": 2_000},
        {"id": "a", "type": "llm", "tools": ["web_search"], "min_output_tokens": 10},
    ]

    assert policy.find_min_output_tokens_violations(nodes) == [
        "a: min_output_tokens는 도구(tools)를 쓰지 않는 llm 노드에만 둘 수 있습니다",
        "z: min_output_tokens는 max_tokens(1000) 이하여야 합니다",
    ]
    assert {"find_min_output_tokens_violations", "min_output_tokens_errors"} <= set(policy.__all__)


# ── 사고 양 맞춤은 후보별 실효 상한으로 한다(#429 D3 ①) ──────────────────


@pytest.fixture
def anthropic() -> Iterator[_Server]:
    srv = _Server()
    yield srv
    srv.close()


@pytest.mark.parametrize(
    ("window", "budget"),
    [
        # 4k 후보: 실효 상한이 high 예산(4096)보다 작아 medium(2048)으로 낮춘다
        pytest.param(4_096, 2_048, id="small-window-lowers-effort"),
        # 큰 후보: max_tokens(8192) 그대로라 high를 그대로 보낸다
        pytest.param(200_000, 4_096, id="large-window-keeps-effort"),
    ],
)
async def test_reasoning_effort_is_fitted_to_the_candidate_limit(
    anthropic, chain, window, budget
):
    haiku = _resolved(anthropic, *HAIKU)
    haiku.model.context_window = window
    chain["models"] = [haiku]
    node = {"id": "gen", "type": "llm", "prompt": "요약", "max_tokens": 8_192,
            "min_output_tokens": 1_024, "reasoning_effort": "high"}  # fmt: skip

    with capture_logs() as captured:
        out = await _exec_llm(_ctx(), node, {})

    # 대역은 예산 ≥ 상한과 사고 중 temperature≠1을 400으로 거부한다 — 한 번에 성공했다
    [body] = anthropic.bodies
    assert out["text"] == "pong"
    assert body["thinking"] == {"type": "enabled", "budget_tokens": budget}
    assert body["max_tokens"] == out["output_budget"][0]["max_tokens"]
    adjusted = [e for e in captured if e["event"] == "reasoning_effort_adjusted"]
    if window == 4_096:
        assert body["max_tokens"] < 4_096
        assert [(e["requested"], e["effective"], e["max_tokens"]) for e in adjusted] == [
            ("high", "medium", body["max_tokens"])
        ]
    else:
        assert body["max_tokens"] == 8_192 and adjusted == []


def test_reserve_matches_the_context_fit_constants():
    assert _reserve(32_768) == llm_node._fit_reserve(32_768) == 1_638
    assert _reserve(4_096) == llm_node._fit_reserve(4_096) == 256
