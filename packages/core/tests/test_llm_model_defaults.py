"""모델 기본값을 호출에 잇는다(#429·#428 ③) — 모델 열 `reasoning_effort`·`max_output_tokens`.

- 사고 양: 호출자(노드) 값이 없으면 후보 모델의 `reasoning_effort`를 쓴다. 노드 밖 호출(채팅
  등 `complete_stream_with_fallback`를 직접 부르는 경로)도 같다. 같은 보정(`_reasoning_request`)을
  탄다.
- 최대 출력: 후보 모델에 `max_output_tokens`가 있으면 그 후보 호출의 `max_tokens`를 그 값으로
  줄인다. 스트림·tool turn·react 마무리 스트림·노드 출력 계획(`_ContextFit`) 모두다. 줄인 값이
  사고 예산 판정과 `max_tokens_used`·`output_truncated.max_tokens`에 들어간다.
- 두 열이 NULL이면 지금과 같다(회귀).

모델 호출은 실제 LiteLLM으로 로컬 대역 서버에 보낸다(①·②·#431 테스트의 대역을 쓴다). 외부 경계(원격
LLM 서버, 서킷 브레이커의 Redis, 모델 조회 DB, 원장 쓰기)만 가짜다. 실제 DB의 모델 해석은
`tests/test_llm_model_reasoning_columns_pg.py`가 본다.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from structlog.testing import capture_logs

from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.services.agents import runtime
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.nodes import validation
from biznotes_core.services.agents.nodes.llm import _exec_llm
from biznotes_core.services.agents.react import loop
from biznotes_core.services.gateway import circuit
from biznotes_core.services.gateway import llm as gateway
from biznotes_core.services.gateway.llm import (
    OUTPUT_FLOOR_UNMET,
    ResolvedModel,
    StreamResult,
    ToolCall,
    ToolTurnResult,
    complete_stream,
    complete_stream_with_fallback,
    complete_with_tools,
    estimate_prompt_tokens,
)
from tests.test_gateway_reasoning_effort import (  # noqa: F401 — 번들 모델 맵 픽스처(autouse)
    _REASONING_KEYS,
    HAIKU,
    MESSAGES,
    OPENAI_COMPAT,
    TOOLS,
    _assert_no_leak,
    _bundled_model_map,
    _resolved,
    _Server,
)
from tests.test_llm_context_fit import PROMPT, SYSTEM, _loaded
from tests.test_llm_output_budget import _budget, _Compatible, _model, _reserve
from tests.test_llm_output_truncation import _resolved as _cut_resolved
from tests.test_llm_output_truncation import _Server as _CutServer


@pytest.fixture
def server() -> Iterator[_Server]:
    """OpenAI 호환·Anthropic 대역(①) — 사고 예산 ≥ 상한, 사고 중 temperature≠1을 400으로
    거부한다."""
    srv = _Server()
    yield srv
    srv.close()


@pytest.fixture
def compat() -> Iterator[_Compatible]:
    """OpenAI 호환 스트림 대역(②) — `replies`로 종료 사유를 정한다."""
    srv = _Compatible()
    yield srv
    srv.close()


@pytest.fixture
def cut() -> Iterator[_CutServer]:
    """출력 상한에서 잘린 응답을 내는 대역(#431) — 경로 `cut`은 스트림·비스트림 모두 `length`다."""
    srv = _CutServer()
    yield srv
    srv.close()


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch) -> list[ResolvedModel]:
    """모델 조회 DB·서킷·추적 대역 — 테스트가 후보 목록을 채운다."""
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


def _with(resolved: ResolvedModel, **columns: Any) -> ResolvedModel:
    """관리자가 모델에 둔 값(`reasoning_effort`·`max_output_tokens`)."""
    for key, value in columns.items():
        setattr(resolved.model, key, value)
    return resolved


def _ctx() -> RunContext:
    return RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={})  # type: ignore[arg-type]


async def _call(path: str, **kw: Any) -> Any:
    """채팅처럼 게이트웨이를 직접 부른다 — 스트림은 `StreamResult`, tool은 `ToolTurnResult`."""
    if path == "stream":
        result = StreamResult()
        gen = await complete_stream_with_fallback(None, None, MESSAGES, result, **kw)  # type: ignore[arg-type]
        assert "".join([d async for d in gen]) == "pong"
        return result
    turn = await complete_with_tools(None, None, MESSAGES, TOOLS, **kw)  # type: ignore[arg-type]
    assert [c.name for c in turn.tool_calls] == ["search"]
    return turn


PATHS = pytest.mark.parametrize("path", ["stream", "tool"])


# ── 사고 양: 모델 기본값 ─────────────────────────────────────────────────


@PATHS
async def test_model_default_reaches_an_openai_compatible_request(
    server: _Server, chain: list[ResolvedModel], path: str
) -> None:
    chain.append(_with(_resolved(server, *OPENAI_COMPAT), reasoning_effort="low"))

    await _call(path, max_tokens=8_192)

    [body] = server.bodies
    assert body["reasoning_effort"] == "low"
    _assert_no_leak(body)


@PATHS
async def test_model_default_reaches_an_anthropic_budget_request(
    server: _Server, chain: list[ResolvedModel], path: str
) -> None:
    """같은 보정을 탄다 — 대역은 사고 중 temperature≠1을 400으로 거부한다. 한 번에 성공했다."""
    chain.append(_with(_resolved(server, *HAIKU), reasoning_effort="medium"))

    await _call(path, max_tokens=8_192)

    [body] = server.bodies
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 2_048}
    assert "temperature" not in body


@PATHS
async def test_caller_value_beats_the_model_default(
    server: _Server, chain: list[ResolvedModel], path: str
) -> None:
    chain.append(_with(_resolved(server, *OPENAI_COMPAT), reasoning_effort="low"))

    await _call(path, max_tokens=8_192, reasoning_effort="high")

    [body] = server.bodies
    assert body["reasoning_effort"] == "high"


@PATHS
@pytest.mark.parametrize("model", [OPENAI_COMPAT, HAIKU], ids=["openai-compatible", "anthropic"])
async def test_empty_columns_keep_todays_request(
    server: _Server, chain: list[ResolvedModel], path: str, model: tuple[str, str]
) -> None:
    chain.append(_with(_resolved(server, *model), reasoning_effort=None, max_output_tokens=None))

    await _call(path, max_tokens=8_192)

    [body] = server.bodies
    assert not _REASONING_KEYS & body.keys()
    assert body["max_tokens"] == 8_192 and body["temperature"] == 0.2


@pytest.mark.parametrize(
    ("first_default", "second_default", "expected"),
    [
        pytest.param("high", None, None, id="fallback-without-a-default"),
        pytest.param(None, "low", "low", id="fallback-with-its-own-default"),
    ],
)
async def test_each_candidate_uses_its_own_default(
    server: _Server,
    chain: list[ResolvedModel],
    first_default: str | None,
    second_default: str | None,
    expected: str | None,
) -> None:
    first = _with(_resolved(server, *HAIKU), reasoning_effort=first_default)
    second = _with(_resolved(server, *OPENAI_COMPAT), reasoning_effort=second_default)
    server.down_models.add("claude-haiku-4-5")
    chain.extend([first, second])

    result = await _call("stream", max_tokens=8_192)

    assert result.model is second
    body = server.bodies[-1]
    assert body["model"] == "gpt-oss-120b"
    assert body.get("reasoning_effort") == expected


@PATHS
async def test_model_default_is_lowered_below_the_limit(
    server: _Server, chain: list[ResolvedModel], path: str
) -> None:
    chain.append(_with(_resolved(server, *HAIKU), reasoning_effort="high"))

    with capture_logs() as logs:
        await _call(path, max_tokens=4_096)

    [body] = server.bodies
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 2_048}
    adjusted = [e for e in logs if e["event"] == "reasoning_effort_adjusted"]
    assert [(e["requested"], e["effective"], e["max_tokens"]) for e in adjusted] == [
        ("high", "medium", 4_096)
    ]


@PATHS
async def test_a_stored_value_outside_the_literals_is_not_sent(
    server: _Server, chain: list[ResolvedModel], path: str
) -> None:
    """열에 CHECK가 없다(D1) — 관리자 API를 거치지 않은 값은 보내지 않고 경고한다."""
    chain.append(_with(_resolved(server, *OPENAI_COMPAT), reasoning_effort="xhigh"))

    with capture_logs() as logs:
        await _call(path, max_tokens=8_192)

    [body] = server.bodies
    assert not _REASONING_KEYS & body.keys()
    assert [e["model"] for e in logs if e["event"] == "llm_model_reasoning_effort_ignored"] == [
        "openai/gpt-oss-120b"
    ]


async def test_mock_provider_ignores_the_model_default(chain: list[ResolvedModel]) -> None:
    provider = LLMProvider(id=uuid.uuid4(), name="p-mock", type="mock")
    model = LLMModel(
        id=uuid.uuid4(), provider_id=provider.id, model_name="mock", display_name="mock",
        capabilities=["llm"], reasoning_effort="high",
    )  # fmt: skip
    chain.append(ResolvedModel(model=model, provider=provider))

    result = StreamResult()
    gen = await complete_stream_with_fallback(None, None, MESSAGES, result)  # type: ignore[arg-type]

    assert "".join([d async for d in gen])
    assert result.model is chain[0]


async def test_llm_node_uses_the_model_default_but_keeps_it_out_of_llm_settings(
    server: _Server, chain: list[ResolvedModel]
) -> None:
    """D4 — `llm_settings`에는 노드가 정한 값만 싣는다. 모델 기본값으로 해석한 값은 싣지 않는다."""
    chain.append(_with(_resolved(server, *OPENAI_COMPAT), reasoning_effort="low"))
    node = {"id": "gen", "type": "llm", "prompt": "요약", "max_tokens": 8_192}

    plain = await _exec_llm(_ctx(), node, {})
    overridden = await _exec_llm(_ctx(), {**node, "reasoning_effort": "high"}, {})

    assert [b["reasoning_effort"] for b in server.bodies] == ["low", "high"]
    assert "llm_settings" not in plain
    assert overridden["llm_settings"] == {"reasoning_effort": "high"}


async def test_json_validate_retry_uses_the_model_default(
    server: _Server, chain: list[ResolvedModel]
) -> None:
    chain.append(_with(_resolved(server, *OPENAI_COMPAT), reasoning_effort="medium"))
    check = {"id": "check", "type": "json_validate", "schema": "slide_outline",
             "source": "{outline.text}", "prompt": "개요 JSON"}  # fmt: skip

    with pytest.raises(Exception, match="slide_outline"):  # 대역 답("pong")도 JSON이 아니다
        await validation._exec_json_validate(_ctx(), check, {"outline": {"text": "JSON 아님"}})

    [body] = server.bodies
    assert body["reasoning_effort"] == "medium"


# ── 최대 출력: 모델 `max_output_tokens` ────────────────────────────────────


@PATHS
@pytest.mark.parametrize(
    ("cap", "sent"),
    [
        pytest.param(1_000, 1_000, id="cap-below-max-tokens"),
        pytest.param(4_096, 4_096, id="cap-equal"),
        pytest.param(100_000, 4_096, id="cap-above-max-tokens"),
        pytest.param(None, 4_096, id="no-cap"),
    ],
)
async def test_model_cap_lowers_the_request_limit(
    server: _Server, chain: list[ResolvedModel], path: str, cap: int | None, sent: int
) -> None:
    chain.append(_with(_resolved(server, *OPENAI_COMPAT), max_output_tokens=cap))

    answered = await _call(path, max_tokens=4_096)

    [body] = server.bodies
    assert body["max_tokens"] == sent
    assert answered.max_tokens_used == sent


@pytest.mark.parametrize(("cap", "sent"), [(1_000, 1_000), (None, 4_096)], ids=["cap", "no-cap"])
async def test_direct_stream_call_is_capped_and_reports_the_sent_limit(
    server: _Server, cap: int | None, sent: int
) -> None:
    """`complete_stream`은 공개 표면이다 — 폴백 루프를 거치지 않아도 모델 상한을 지키고, 보낸
    상한을 `max_tokens_used`로 돌려준다(폴백 경로와 같은 값)."""
    resolved = _with(_resolved(server, *OPENAI_COMPAT), max_output_tokens=cap)
    result = StreamResult()

    gen = await complete_stream(resolved, MESSAGES, result, max_tokens=4_096)
    assert "".join([d async for d in gen]) == "pong"

    [body] = server.bodies
    assert body["max_tokens"] == result.max_tokens_used == sent


@pytest.mark.parametrize(("cap", "sent"), [(2, 2), (None, 8)], ids=["cap", "no-cap"])
async def test_direct_mock_stream_reports_the_sent_limit(cap: int | None, sent: int) -> None:
    """리뷰 재현: cap=2, 호출 상한 8 — 잘리고(`length`) 보낸 상한은 2다. mock 출력은 상한 × 4
    글자다."""
    provider = LLMProvider(id=uuid.uuid4(), name="p-mock", type="mock")
    model = LLMModel(
        id=uuid.uuid4(), provider_id=provider.id, model_name="mock", display_name="mock",
        capabilities=["llm"], max_output_tokens=cap,
    )  # fmt: skip
    result = StreamResult()

    gen = await complete_stream(ResolvedModel(model=model, provider=provider), MESSAGES, result,
                                max_tokens=8)  # fmt: skip
    text = "".join([d async for d in gen])

    assert (result.finish_reason, result.max_tokens_used) == ("length", sent)
    assert len(text) == sent * 4
    assert StreamResult().max_tokens_used is None  # 스트림 전에는 모른다


def test_tool_turn_result_gains_a_keyword_only_limit_with_a_safe_default() -> None:
    assert ToolTurnResult().max_tokens_used is None
    with pytest.raises(TypeError):
        ToolTurnResult("", [], {}, {}, None, False, None, 1)  # type: ignore[misc]


@PATHS
async def test_capped_limit_drives_the_effort_adjustment(
    server: _Server, chain: list[ResolvedModel], path: str
) -> None:
    """줄인 상한으로 사고 예산을 판정한다 — high(4096) ≥ 4096이라 medium(2048)으로 낮춘다."""
    chain.append(_with(_resolved(server, *HAIKU), max_output_tokens=4_096))

    with capture_logs() as logs:
        await _call(path, max_tokens=8_192, reasoning_effort="high")

    [body] = server.bodies
    assert body["max_tokens"] == 4_096
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 2_048}
    adjusted = [e for e in logs if e["event"] == "reasoning_effort_adjusted"]
    assert [(e["effective"], e["max_tokens"]) for e in adjusted] == [("medium", 4_096)]


async def test_each_candidate_uses_its_own_cap(server: _Server, chain: list[ResolvedModel]) -> None:
    first = _with(_resolved(server, *HAIKU), max_output_tokens=1_000)
    second = _with(_resolved(server, *OPENAI_COMPAT), max_output_tokens=None)
    server.down_models.add("claude-haiku-4-5")
    chain.extend([first, second])

    result = await _call("stream", max_tokens=4_096)

    assert [b["max_tokens"] for b in server.bodies] == [1_000, 4_096]
    assert (result.model, result.max_tokens_used) == (second, 4_096)


# ── 노드: 줄인 상한이 계획·잘림 표시에 들어간다 ────────────────────────────


def _capped(srv: _Compatible, name: str, window: int | None, cap: int | None) -> ResolvedModel:
    return _with(_model(srv, name, window), max_output_tokens=cap)


async def test_llm_node_reports_the_capped_limit_when_cut(
    compat: _Compatible, chain: list[ResolvedModel]
) -> None:
    chain.append(_capped(compat, "m", None, 6_000))
    compat.replies = [('{"title": "개', "length")]
    node = {"id": "gen", "type": "llm", "prompt": "요약", "max_tokens": 16_384}

    out = await _exec_llm(_ctx(), node, {})

    [body] = compat.bodies
    assert body["max_tokens"] == 6_000
    assert out["output_truncated"] == {"max_tokens": 6_000, "completion_tokens": 7}


async def test_source_fit_without_a_floor_budgets_for_the_capped_output(
    compat: _Compatible, chain: list[ResolvedModel]
) -> None:
    """하한이 없어도(#357 맞춤) 실제로 보낼 상한만큼만 출력 몫을 뺀다 — 입력을 더 남긴다."""
    load = await _loaded()
    chain.append(_capped(compat, "gpt-oss-32k", 32_768, 4_096))
    node = {"id": "gen", "type": "llm", "system": SYSTEM, "prompt": PROMPT, "max_tokens": 16_384}

    out = await _exec_llm(_ctx(), node, {"load": load})

    [body] = compat.bodies
    assert body["max_tokens"] == 4_096
    prompt = estimate_prompt_tokens(body["messages"])
    reserve = _reserve(32_768)
    assert 32_768 - 16_384 - reserve < prompt <= 32_768 - 4_096 - reserve
    assert out["dropped"]


@pytest.mark.parametrize(
    ("window", "cap"),
    [
        # 큰 후보: 노드 max_tokens(16384)보다 모델 상한(6000)이 작다
        pytest.param(1_000_000, 6_000, id="model-cap-wins"),
        # 32k 후보: 컨텍스트의 남는 몫이 모델 상한(30000)보다 작다
        pytest.param(32_768, 30_000, id="context-wins"),
    ],
)
async def test_floor_plan_takes_the_smallest_of_the_three(
    compat: _Compatible, chain: list[ResolvedModel], window: int, cap: int
) -> None:
    load = await _loaded()
    candidate = _capped(compat, "m", window, cap)
    chain.append(candidate)
    node = {"id": "gen", "type": "llm", "system": SYSTEM, "prompt": PROMPT, "max_tokens": 16_384,
            "min_output_tokens": 4_096}  # fmt: skip

    out = await _exec_llm(_ctx(), node, {"load": load})

    [body] = compat.bodies
    reserve = _reserve(window)
    room = window - estimate_prompt_tokens(body["messages"]) - reserve
    assert body["max_tokens"] == min(16_384, cap, room)
    assert out["output_budget"] == [
        _budget(candidate, window - 4_096 - reserve, body["max_tokens"], False)
    ]


async def test_candidate_whose_cap_is_below_the_floor_is_skipped(
    compat: _Compatible, chain: list[ResolvedModel]
) -> None:
    small, big = _capped(compat, "small", 1_000_000, 2_048), _capped(compat, "big", None, None)
    chain.extend([small, big])
    node = {"id": "gen", "type": "llm", "prompt": "요약", "max_tokens": 8_192,
            "min_output_tokens": 4_096}  # fmt: skip

    out = await _exec_llm(_ctx(), node, {})

    assert [b["model"] for b in compat.bodies] == ["big"]  # 건너뛴 후보에는 요청이 가지 않았다
    assert out["model"]["display_name"] == "big"
    [skipped, used] = out["output_budget"]
    assert (skipped["display_name"], skipped["max_tokens"], skipped["skipped"]) == (
        "small", 2_048, True
    )
    assert (used["max_tokens"], used["skipped"]) == (8_192, False)


async def test_candidate_cap_alone_fails_as_context_length_when_nothing_is_left(
    compat: _Compatible, chain: list[ResolvedModel]
) -> None:
    chain.append(_capped(compat, "only", None, 1_024))
    node = {"id": "gen", "type": "llm", "prompt": "요약", "max_tokens": 8_192,
            "min_output_tokens": 2_048}  # fmt: skip

    with pytest.raises(gateway.GatewayContextLengthError) as caught:
        await _exec_llm(_ctx(), node, {})

    assert [a["error_code"] for a in caught.value.model_attempts] == [OUTPUT_FLOOR_UNMET]
    assert compat.bodies == []


# ── react 루프: tool turn·마무리 스트림 ───────────────────────────────────


@pytest.fixture
def quiet_react(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """react 보조 로그와 내장 도구 실행 대역 — 실행한 도구 이름을 모은다."""
    seen: list[str] = []

    async def none(*a: Any, **k: Any) -> None:
        return None

    async def dispatch(ctx: RunContext, name: str, args: dict[str, Any]) -> str:
        seen.append(name)
        return "발췌 없음"

    monkeypatch.setattr(loop, "_log_node_start", none)
    monkeypatch.setattr(loop, "_log_node_finish", none)
    monkeypatch.setattr(runtime, "_dispatch_builtin_tool", dispatch)
    return seen


def _react(**kw: Any) -> dict[str, Any]:
    return {"id": "r", "type": "react_agent", "goal": "조사", "allowed_tools": ["rag_search"],
            "max_tokens": 4_096, **kw}  # fmt: skip


async def test_react_tool_turn_reports_the_capped_limit_when_cut(
    cut: _CutServer, chain: list[ResolvedModel], quiet_react: list[str]
) -> None:
    """tool turn이 잘린 최종 답을 내면 그 턴에 보낸(줄인) 상한으로 표시한다."""
    chain.append(_with(_cut_resolved(cut, "cut"), max_output_tokens=1_000, cost_per_1k={}))

    out = await loop.run_react_node(_ctx(), _react())

    assert quiet_react == []  # 도구 없이 바로 답했다
    assert out["finish_reason"] == "length"
    assert out["output_truncated"]["max_tokens"] == 1_000


async def test_react_closing_stream_reports_the_capped_limit_when_cut(
    cut: _CutServer,
    chain: list[ResolvedModel],
    quiet_react: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """루프 소진 마무리 스트림(#398)도 모델 상한을 지키고 그 값으로 잘림을 표시한다."""
    chain.append(_with(_cut_resolved(cut, "cut"), max_output_tokens=1_000, cost_per_1k={}))
    call = ToolCall(id="c1", name="rag_search", arguments={"query": "q"})

    async def tool_turn(db: Any, model_id: Any, messages: Any, tools: Any, **kw: Any) -> Any:
        return ToolTurnResult(
            content="",
            tool_calls=[call],
            assistant_message={"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "rag_search", "arguments": '{"query": "q"}'}}]},
            usage={"total_tokens": 10},
            model=chain[0],
            finish_reason="tool_calls",
        )  # fmt: skip

    monkeypatch.setattr(gateway, "complete_with_tools", tool_turn)

    out = await loop.run_react_node(_ctx(), _react(max_iters=1))

    assert quiet_react == ["rag_search"]
    assert out["finish_reason"] == "length"
    assert out["output_truncated"]["max_tokens"] == 1_000
