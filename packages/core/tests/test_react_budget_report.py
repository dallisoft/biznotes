"""react 예산 소진 출력의 사유·사용량(#385).

모델(`complete_with_tools`·마무리 스트림)·내장 도구 디스패처·실행 로그만 가짜로 두고 react 루프와
클램프는 실제 코드를 탄다.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from biznotes_core.services.agents import runtime
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.nodes.retrieval import _exec_condition
from biznotes_core.services.agents.react import loop
from biznotes_core.services.agents.react.loop import (
    BUDGET_HIT_KEY,
    BUDGET_INFO_KEY,
    BUDGET_PARTIAL_NOTE,
    REACT_DEFAULTS,
    REACT_HARD_CAPS,
)
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult


def _ctx(**kw: Any) -> RunContext:
    return RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={}, **kw)  # type: ignore[arg-type]


def _tool_turn(usage: dict[str, int], model: Any = None, content: str = "") -> ToolTurnResult:
    call = ToolCall(id="c1", name="rag_search", arguments={"query": "q"})
    return ToolTurnResult(
        content=content,
        tool_calls=[call],
        assistant_message={
            "role": "assistant",
            "content": content or None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "rag_search", "arguments": '{"query": "q"}'},
                }
            ],
        },
        usage=usage,
        model=model,
    )


def _answer(text: str, usage: dict[str, int], model: Any = None) -> ToolTurnResult:
    return ToolTurnResult(
        content=text,
        assistant_message={"role": "assistant", "content": text},
        usage=usage,
        model=model,
    )


def _priced_model(input_per_1k: float) -> Any:
    """`compute_cost`·출력 메타데이터가 읽는 필드만 가진 응답 모델."""
    return SimpleNamespace(
        model=SimpleNamespace(
            id=uuid.uuid4(), display_name="m", cost_per_1k={"input": input_per_1k, "output": 0}
        )
    )


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
def tool_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """내장 도구 디스패처 — 받은 인자를 기록하고 짧은 결과를 준다."""
    seen: list[dict[str, Any]] = []

    async def dispatch(ctx: RunContext, name: str, args: dict[str, Any]) -> str:
        seen.append({"name": name, **args})
        return "발췌 없음"

    monkeypatch.setattr(runtime, "_dispatch_builtin_tool", dispatch)
    return seen


def _script(monkeypatch: pytest.MonkeyPatch, *turns: ToolTurnResult) -> list[int]:
    count = [0]

    async def fake(db: Any, model_id: Any, messages: Any, tools: Any, **kw: Any) -> ToolTurnResult:
        turn = turns[min(count[0], len(turns) - 1)]
        count[0] += 1
        return turn

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake)
    return count


def _node(**kw: Any) -> dict[str, Any]:
    return {"id": "r", "type": "react_agent", "goal": "조사", "allowed_tools": ["rag_search"], **kw}


def _assert_no_budget_words(text: str) -> None:
    # 「예산」은 계정 예산(일일 한도)으로 읽히고, 본문이 없는데 「부분 결과」라고 하면 틀린 말이다
    assert "예산" not in text and "부분 결과" not in text


async def test_token_exhaustion_without_content_reports_reason_and_usage(
    monkeypatch: pytest.MonkeyPatch, tool_calls: list[dict[str, Any]]
) -> None:
    _script(monkeypatch, _tool_turn({"prompt_tokens": 150_000, "total_tokens": 150_000}))
    ctx = _ctx()

    out = await loop.run_react_node(ctx, _node())

    assert out[BUDGET_HIT_KEY] is True
    assert out[BUDGET_INFO_KEY] == {
        "reason": "token",
        "used_tokens": 150_000,
        "token_budget": 100_000,
        "used_cost_usd": 0.0,
        "cost_budget_usd": 0.5,
        "has_content": False,
    }
    assert out["text"] == "(1회 토큰 한도 100,000에 도달해 답변을 만들지 못했습니다.)"
    _assert_no_budget_words(out["text"])
    assert tool_calls == []  # 예산을 넘긴 턴의 도구는 실행하지 않는다(기존 동작)


async def test_token_exhaustion_with_content_keeps_the_body(
    monkeypatch: pytest.MonkeyPatch, tool_calls: list[dict[str, Any]]
) -> None:
    _script(
        monkeypatch,
        _answer("모델이 쓴 본문", {"prompt_tokens": 120_000, "total_tokens": 120_000}),
    )

    out = await loop.run_react_node(_ctx(), _node())

    assert out["text"] == "모델이 쓴 본문"  # 본문 우선 — 지금처럼
    assert out[BUDGET_HIT_KEY] is True
    assert out[BUDGET_INFO_KEY]["reason"] == "token"
    assert out[BUDGET_INFO_KEY]["has_content"] is True


async def test_cost_exhaustion_reports_cost_reason(
    monkeypatch: pytest.MonkeyPatch, tool_calls: list[dict[str, Any]]
) -> None:
    # 60,000 입력 토큰 × $0.01/1k = $0.60 — 토큰은 한도(200,000) 밑, 비용은 한도($0.50) 위
    model = _priced_model(0.01)
    _script(monkeypatch, _tool_turn({"prompt_tokens": 60_000, "total_tokens": 60_000}, model))

    out = await loop.run_react_node(_ctx(), _node(token_budget=200_000))

    info = out[BUDGET_INFO_KEY]
    assert info["reason"] == "cost"
    assert info["used_tokens"] == 60_000 and info["token_budget"] == 200_000
    assert info["used_cost_usd"] == pytest.approx(0.6)
    assert info["cost_budget_usd"] == 0.5
    assert info["has_content"] is False
    assert out["text"] == "(1회 비용 한도 $0.50에 도달해 답변을 만들지 못했습니다.)"
    _assert_no_budget_words(out["text"])
    assert out[BUDGET_HIT_KEY] is True


async def test_usage_is_measured_from_the_node_entry(
    monkeypatch: pytest.MonkeyPatch, tool_calls: list[dict[str, Any]]
) -> None:
    """같은 run의 선행 노드가 쓴 양은 이 노드 사용량에 들어가지 않는다(codex C4)."""
    _script(monkeypatch, _tool_turn({"prompt_tokens": 30_000, "total_tokens": 30_000}))
    ctx = _ctx()
    ctx.usage["total_tokens"] = 70_000
    ctx.cost_total[0] = 0.2

    out = await loop.run_react_node(ctx, _node(token_budget=25_000))

    info = out[BUDGET_INFO_KEY]
    assert (info["used_tokens"], info["token_budget"]) == (30_000, 25_000)
    assert info["used_cost_usd"] == 0.0


async def test_exhaustion_after_tool_run_has_no_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """도구 실행(서브에이전트 등)이 한도를 넘기면 다음 모델 턴 없이 끝난다 — 본문이 없다."""
    turns = _script(monkeypatch, _tool_turn({"prompt_tokens": 10, "total_tokens": 10}))

    async def costly(ctx: RunContext, name: str, args: dict[str, Any]) -> str:
        ctx.add_tokens({"total_tokens": 200_000})
        return "결과"

    monkeypatch.setattr(runtime, "_dispatch_builtin_tool", costly)

    out = await loop.run_react_node(_ctx(), _node())

    assert turns[0] == 1
    assert out[BUDGET_INFO_KEY]["reason"] == "token"
    assert out[BUDGET_INFO_KEY]["used_tokens"] == 200_010
    assert out[BUDGET_INFO_KEY]["has_content"] is False
    _assert_no_budget_words(out["text"])


async def test_finalizer_over_budget_reports_without_content(
    monkeypatch: pytest.MonkeyPatch, tool_calls: list[dict[str, Any]]
) -> None:
    """루프 소진 마무리 호출이 한도를 넘기면 본문을 버린다(C9) — 그 사실을 사유와 함께 싣는다."""
    _script(monkeypatch, _tool_turn({"prompt_tokens": 10, "total_tokens": 10}))

    async def fake_stream(db: Any, model_id: Any, messages: Any, result: Any, **k: Any) -> Any:
        result.usage = {"total_tokens": 999_999}

        async def gen() -> Any:
            yield "최종 답변"

        return gen()

    monkeypatch.setattr(gateway_llm, "complete_stream_with_fallback", fake_stream)

    out = await loop.run_react_node(_ctx(), _node(max_iters=1))

    assert "최종 답변" not in out["text"]
    _assert_no_budget_words(out["text"])
    assert out[BUDGET_INFO_KEY]["has_content"] is False
    assert out[BUDGET_HIT_KEY] is True


async def test_normal_answer_has_no_budget_keys(
    monkeypatch: pytest.MonkeyPatch, tool_calls: list[dict[str, Any]]
) -> None:
    _script(monkeypatch, _answer("답", {"total_tokens": 10}))

    out = await loop.run_react_node(_ctx(), _node())

    assert BUDGET_HIT_KEY not in out and BUDGET_INFO_KEY not in out


async def test_stage_gate_still_reads_the_structural_signal(
    monkeypatch: pytest.MonkeyPatch, tool_calls: list[dict[str, Any]]
) -> None:
    """#202 게이트(`{단계._react_budget_hit} == "true"`)는 문구가 바뀌어도 그대로 참이다."""
    _script(monkeypatch, _tool_turn({"total_tokens": 150_000}))
    out = await loop.run_react_node(_ctx(), _node())
    gate = {"left": "{a." + BUDGET_HIT_KEY + "}", "op": "==", "right": "true"}

    stopped = await _exec_condition(_ctx(), gate, {"a": out})
    # 소진하지 않은 단계는 키가 없어 거짓이다
    passed = await _exec_condition(_ctx(), gate, {"a": {"text": "답"}})

    assert stopped["result"] is True and passed["result"] is False


async def test_partial_note_constant_is_kept_for_compatibility() -> None:
    # 소비자가 import하는 공개 상수다 — 지우지 않고 값도 그대로 둔다
    assert BUDGET_PARTIAL_NOTE == "(예산 한도에 도달하여 부분 결과로 마무리했습니다.)"
    assert loop._BUDGET_PARTIAL_NOTE is BUDGET_PARTIAL_NOTE


# ── 공개 기본값·하드 상한 = 루프 클램프 ────────────────────────────────


def test_public_defaults_and_caps_are_the_loop_clamp() -> None:
    assert loop._REACT_DEFAULTS is REACT_DEFAULTS and loop._REACT_HARD_CAPS is REACT_HARD_CAPS
    assert runtime._REACT_DEFAULTS is REACT_DEFAULTS  # 재수출도 같은 객체다
    assert set(REACT_DEFAULTS) == set(REACT_HARD_CAPS)
    for key in ("max_iters", "token_budget", "max_subagent_calls"):
        assert loop._clamp_react_int(None, key) == REACT_DEFAULTS[key]
        assert loop._clamp_react_int(0, key) == REACT_DEFAULTS[key]
        assert loop._clamp_react_int(10**9, key) == REACT_HARD_CAPS[key]
    assert loop._clamp_react_float(None, "cost_budget_usd") == REACT_DEFAULTS["cost_budget_usd"]
    assert loop._clamp_react_float(1e9, "cost_budget_usd") == REACT_HARD_CAPS["cost_budget_usd"]


@pytest.mark.parametrize(
    ("node_value", "expected"),
    [(None, REACT_DEFAULTS["token_budget"]), (10**9, REACT_HARD_CAPS["token_budget"])],
)
async def test_reported_budget_is_the_public_constant(
    monkeypatch: pytest.MonkeyPatch,
    tool_calls: list[dict[str, Any]],
    node_value: Any,
    expected: float,
) -> None:
    _script(monkeypatch, _tool_turn({"total_tokens": 10**7}))
    node = _node() if node_value is None else _node(token_budget=node_value)

    out = await loop.run_react_node(_ctx(), node)

    assert out[BUDGET_INFO_KEY]["token_budget"] == expected
    assert out[BUDGET_INFO_KEY]["cost_budget_usd"] == REACT_DEFAULTS["cost_budget_usd"]
