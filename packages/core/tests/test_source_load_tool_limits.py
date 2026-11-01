"""채팅 source_load 도구의 1회 상한 설정과 남은 토큰 예산 맞춤(#389).

source_load 출력은 실제 `_exec_source_load`로 만든다(DB는 소스 목록·청크 재조립 두 쿼리만
흉내 낸다). 모델(`complete_with_tools`)과 실행 로그만 가짜로 두고 react 루프·도구 디스패처·
토큰 추정(`estimate_prompt_tokens`, cl100k)은 실제 코드를 탄다.
"""

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs

from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.nodes import llm as llm_node
from biznotes_core.services.agents.nodes import retrieval
from biznotes_core.services.agents.react import loop
from biznotes_core.services.agents.react.loop import BUDGET_HIT_KEY, BUDGET_INFO_KEY
from biznotes_core.services.agents.tool_registry import _BUILTIN_SPECS
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult, estimate_prompt_tokens

# 이슈 재현 — 원문 6만 자(한국어). cl100k로 약 6만 토큰이다
BODY = ("한국어 원문은 약 0.67 토큰/자였다. 소스 원문을 한 번에 넣는다. " * 2000)[:20_000]


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_Rows":
        return self

    def all(self) -> list[Any]:
        return self._rows


class _SourceDB:
    """`_exec_source_load`의 두 쿼리(소스 목록 → 소스마다 청크 재조립)만 흉내 낸다."""

    def __init__(self, n: int, body: str = BODY) -> None:
        self._sources = [
            SimpleNamespace(id=uuid.uuid4(), title=f"자료 {i + 1}", parsed_object_key=None)
            for i in range(n)
        ]
        self._body = body

    async def execute(self, q: Any) -> _Rows:
        # 같은 run에서 여러 번 불러도 되게 쿼리 대상으로 구분한다
        if "source_chunks" in str(q):
            return _Rows([(self._body,)])
        return _Rows(self._sources)


def _ctx(n_sources: int = 3, **kw: Any) -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=_SourceDB(n_sources), params={}, **kw
    )  # type: ignore[arg-type]


@pytest.fixture
def loads(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """실제 `_exec_source_load`가 받은 상한을 기록한다."""
    seen: list[dict[str, Any]] = []
    real = retrieval._exec_source_load

    async def spy(ctx: RunContext, node: dict[str, Any], state: dict[str, Any]) -> Any:
        seen.append(dict(node))
        return await real(ctx, node, state)

    monkeypatch.setattr(retrieval, "_exec_source_load", spy)
    return seen


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """react_tool_call 보조 로그 행의 종료 기록(출력)을 모은다."""
    rows: list[dict[str, Any]] = []

    async def start(run_id: Any, node_id: str, *a: Any, **k: Any) -> str:
        return node_id

    async def finish(log_id: str, **kw: Any) -> bool:
        rows.append({"log_id": log_id, **kw})
        return True

    monkeypatch.setattr(loop, "_log_node_start", start)
    monkeypatch.setattr(loop, "_log_node_finish", finish)
    return rows


def _messages_tokens(messages: list[dict[str, Any]]) -> int:
    return estimate_prompt_tokens(
        [{"role": str(m["role"]), "content": str(m.get("content") or "")} for m in messages]
    )


class _Model:
    """차례대로 도구를 부르고 답하는 모델 — 매 턴 다시 보낸 메시지 전체를 입력 토큰으로 센다."""

    def __init__(self, *calls: tuple[str, dict[str, Any]], answer: str = "최종 답변") -> None:
        self.calls = list(calls)
        self.answer = answer
        self.tools_seen: list[list[dict[str, Any]]] = []
        self.tool_results: list[str] = []

    async def __call__(
        self, db: Any, model_id: Any, messages: list[dict[str, Any]], tools: Any, **kw: Any
    ) -> ToolTurnResult:
        self.tools_seen.append(list(tools))
        self.tool_results = [m["content"] for m in messages if m["role"] == "tool"]
        prompt = _messages_tokens(messages)
        usage = {"prompt_tokens": prompt, "completion_tokens": 50, "total_tokens": prompt + 50}
        if not self.calls:
            return ToolTurnResult(
                content=self.answer,
                assistant_message={"role": "assistant", "content": self.answer},
                usage=usage,
            )
        name, args = self.calls.pop(0)
        call_id = uuid.uuid4().hex[:9]
        return ToolTurnResult(
            tool_calls=[ToolCall(id=call_id, name=name, arguments=args)],
            assistant_message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            },
            usage=usage,
        )


def _install(monkeypatch: pytest.MonkeyPatch, model: _Model) -> _Model:
    monkeypatch.setattr(gateway_llm, "complete_with_tools", model)
    return model


def _node(**kw: Any) -> dict[str, Any]:
    return {
        "id": "react",
        "type": "react_agent",
        "goal": "소스를 읽고 답하라",
        "allowed_tools": ["source_load"],
        **kw,
    }


def _source_load_spec(tools: list[dict[str, Any]]) -> dict[str, Any]:
    (spec,) = [t for t in tools if t["function"]["name"] == "source_load"]
    return spec


# ── 1. 상한 설정 ─────────────────────────────────────────────────────────


async def test_unset_limits_keep_the_current_clamp(
    monkeypatch: pytest.MonkeyPatch, loads: list[dict[str, Any]], audit: list[Any]
) -> None:
    model = _install(monkeypatch, _Model(("source_load", {"max_sources": 99, "max_chars": 10**9})))

    out = await loop.run_react_node(_ctx(), _node())

    assert loads == [{"max_sources": 5, "max_chars": 60_000}]
    # 모델이 보는 스펙도 공유 스펙 그대로다
    assert _source_load_spec(model.tools_seen[0]) is _BUILTIN_SPECS["source_load"]
    assert "budget_fit" not in json.loads(model.tool_results[0])
    assert BUDGET_HIT_KEY not in out


async def test_unset_limits_keep_the_builtin_dispatch_seam(
    monkeypatch: pytest.MonkeyPatch, audit: list[Any]
) -> None:
    """설정이 없으면 디스패처를 바꾸지 않는다 — `runtime._dispatch_builtin_tool` seam이 그대로다."""
    from biznotes_core.services.agents import runtime

    seen: list[str] = []

    async def dispatch(ctx: RunContext, name: str, args: dict[str, Any]) -> str:
        seen.append(name)
        return "{}"

    monkeypatch.setattr(runtime, "_dispatch_builtin_tool", dispatch)
    _install(monkeypatch, _Model(("source_load", {})))

    await loop.run_react_node(_ctx(), _node())

    assert seen == ["source_load"]


async def test_direct_dispatch_keeps_the_current_clamp(loads: list[dict[str, Any]]) -> None:
    await llm_node._dispatch_builtin_tool(_ctx(), "source_load", {"max_sources": 0})
    await llm_node._dispatch_builtin_tool(_ctx(), "source_load", {"max_chars": 10**9})

    assert loads == [
        {"max_sources": 1, "max_chars": 60_000},
        {"max_sources": 5, "max_chars": 60_000},
    ]


@pytest.mark.parametrize(
    ("node_keys", "model_args", "caps", "expected"),
    [
        # 모델이 생략하면 설정한 상한이다
        (
            {"source_load_max_sources": 10, "source_load_max_chars": 100_000},
            {},
            (10, 100_000),
            (10, 100_000),
        ),
        # 모델 인자는 설정한 상한으로 자른다
        (
            {"source_load_max_sources": 10, "source_load_max_chars": 100_000},
            {"max_sources": 99, "max_chars": 10**9},
            (10, 100_000),
            (10, 100_000),
        ),
        # 상한 밑의 모델 인자는 그대로다
        ({"source_load_max_chars": 20_000}, {"max_chars": 5_000}, (5, 20_000), (5, 5_000)),
        # 하드 상한
        (
            {"source_load_max_sources": 10**6, "source_load_max_chars": 10**9},
            {},
            (20, 200_000),
            (20, 200_000),
        ),
    ],
)
async def test_node_limits_clamp_the_tool(
    monkeypatch: pytest.MonkeyPatch,
    loads: list[dict[str, Any]],
    audit: list[Any],
    node_keys: dict[str, Any],
    model_args: dict[str, Any],
    caps: tuple[int, int],
    expected: tuple[int, int],
) -> None:
    model = _install(monkeypatch, _Model(("source_load", model_args)))

    await loop.run_react_node(_ctx(25), _node(**node_keys))

    assert loads == [{"max_sources": expected[0], "max_chars": expected[1]}]
    # 모델이 보는 스펙이 설정한 상한을 알린다
    spec = _source_load_spec(model.tools_seen[0])["function"]
    assert spec["parameters"]["properties"]["max_sources"]["maximum"] == caps[0]
    assert spec["parameters"]["properties"]["max_chars"]["maximum"] == caps[1]
    assert f"최대 {caps[0]}개/{caps[1]}자" in spec["description"]
    # 공유 스펙은 고치지 않는다
    shared = _BUILTIN_SPECS["source_load"]["function"]["parameters"]["properties"]
    assert (shared["max_sources"]["maximum"], shared["max_chars"]["maximum"]) == (5, 60_000)


async def test_invalid_node_limits_fall_back_to_the_defaults(
    monkeypatch: pytest.MonkeyPatch, loads: list[dict[str, Any]], audit: list[Any]
) -> None:
    model = _install(monkeypatch, _Model(("source_load", {})))

    await loop.run_react_node(
        _ctx(), _node(source_load_max_sources=0, source_load_max_chars="많이")
    )

    assert loads == [{"max_sources": 5, "max_chars": 60_000}]
    assert _source_load_spec(model.tools_seen[0]) is _BUILTIN_SPECS["source_load"]


async def test_llm_node_tool_loop_reads_the_limits(
    monkeypatch: pytest.MonkeyPatch, loads: list[dict[str, Any]]
) -> None:
    _install(monkeypatch, _Model(("source_load", {})))
    node = {
        "id": "gen",
        "type": "llm",
        "prompt": "읽어라",
        "tools": ["source_load"],
        "source_load_max_chars": 1_000,
        "source_load_budget_ratio": 0.1,  # llm 노드에는 예산이 없어 쓰지 않는다
    }

    out = await llm_node._exec_llm(_ctx(), node, {})

    assert loads == [{"max_sources": 5, "max_chars": 1_000}]
    assert out["text"] == "최종 답변"


def test_unknown_react_keys_are_still_rejected() -> None:
    assert {
        "source_load_max_sources",
        "source_load_max_chars",
        "source_load_budget_ratio",
    } <= loop._REACT_CONFIG_KEYS


# ── 2. 남은 토큰 예산 맞춤(옵션) ──────────────────────────────────────────


async def test_budget_ratio_fits_the_result_and_records_the_cut(
    monkeypatch: pytest.MonkeyPatch, audit: list[dict[str, Any]]
) -> None:
    model = _install(monkeypatch, _Model(("source_load", {})))
    ctx = _ctx()

    with capture_logs() as logs:
        await loop.run_react_node(ctx, _node(source_load_budget_ratio=0.2))

    # 첫 턴만 쓴 상태에서 부른다 — 남은 예산 × 0.2
    (fit_log,) = [e for e in logs if e["event"] == "source_load 도구 결과를 남은 토큰 한도에 맞춤"]
    allowance = fit_log["allowance"]
    assert 0 < allowance <= int(100_000 * 0.2)
    (result,) = model.tool_results
    assert estimate_prompt_tokens([{"role": "tool", "content": result}]) <= allowance
    assert fit_log["fitted_tokens"] <= allowance < fit_log["prompt_tokens"]
    # 잘린 사실이 결과 머리에 있다
    shown = json.loads(result)
    assert list(shown)[0] == "budget_fit"
    assert shown["budget_fit"]["reason"] == "token_budget"
    assert shown["budget_fit"]["max_tokens"] == allowance
    # 60,000자 상한으로 이미 잘린 소스(max_chars*) 위에 예산 맞춤 사유가 더해진다
    reasons = {d["reason"] for d in shown["dropped"]}
    assert reasons & {"token_budget", "token_budget_partial"}
    assert reasons <= {"token_budget", "token_budget_partial", "max_chars", "max_chars_partial"}
    # 실행 로그(react_tool_call 보조 행)의 출력에도 남는다
    (row,) = [r for r in audit if r["log_id"] == "react:tool:0"]
    assert row["output_data"]["output"].startswith('{"budget_fit"')
    # 근거(evidence)는 실제로 담은 소스만이다
    assert [e["title"] for e in ctx.tool_evidence] == [s["title"] for s in shown["sources"]]


async def test_budget_ratio_leaves_small_results_alone(
    monkeypatch: pytest.MonkeyPatch, audit: list[Any]
) -> None:
    model = _install(monkeypatch, _Model(("source_load", {"max_chars": 2_000})))

    await loop.run_react_node(_ctx(), _node(source_load_budget_ratio=0.5))

    shown = json.loads(model.tool_results[0])
    assert "budget_fit" not in shown
    assert {d["reason"] for d in shown["dropped"]} <= {"max_chars", "max_chars_partial"}


@pytest.mark.parametrize("ratio", [0, None, -1, "끔"])
async def test_budget_ratio_off_values(
    monkeypatch: pytest.MonkeyPatch, audit: list[Any], ratio: Any
) -> None:
    model = _install(monkeypatch, _Model(("source_load", {})))

    await loop.run_react_node(_ctx(), _node(source_load_budget_ratio=ratio))

    assert "budget_fit" not in json.loads(model.tool_results[0])


async def test_no_room_left_returns_only_the_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()

    out = await llm_node._dispatch_source_load(ctx, {}, token_allowance=0)

    shown = json.loads(out)
    assert list(shown) == ["budget_fit", "context", "sources", "dropped"]
    assert shown["context"] == "" and ctx.tool_evidence == []


# ── 3. 이슈 시나리오 — 원문 6만 자, 3턴, 기본 token_budget ─────────────────


@pytest.fixture
def search(monkeypatch: pytest.MonkeyPatch) -> None:
    """rag_search가 부를 검색·RAG 설정 — 짧은 발췌 하나를 준다."""
    from biznotes_core.services import rag_chat, settings_store
    from biznotes_core.services.rag_chat import Citation, RetrievalResult

    async def fake_setting(db: Any, key: str) -> dict[str, Any]:
        return dict(settings_store.DEFAULTS["rag"])

    async def fake(db: Any, notebook_id: Any, query: str, source_ids: Any, top_k: int = 6) -> Any:
        hit = Citation(n=1, source_id="s", chunk_id="c", quote="발췌", source_title="자료 1")
        return RetrievalResult(citations=[hit], insufficient_context=False)

    monkeypatch.setattr(rag_chat, "retrieve_context", fake)
    monkeypatch.setattr(settings_store, "get_setting", fake_setting)


def _three_turns() -> _Model:
    # 실제 운영에서 본 순서 — source_load → rag_search → 3번째 턴
    return _Model(("source_load", {}), ("rag_search", {"query": "핵심"}))


def _scenario_node(**kw: Any) -> dict[str, Any]:
    return _node(allowed_tools=["source_load", "rag_search"], **kw)


async def test_scenario_exhausts_without_the_option(
    monkeypatch: pytest.MonkeyPatch, audit: list[Any], search: None
) -> None:
    _install(monkeypatch, _three_turns())

    out = await loop.run_react_node(_ctx(), _scenario_node())

    # 원문을 매 턴 다시 보내 3턴째에 기본 한도(100,000)를 넘긴다
    assert out[BUDGET_HIT_KEY] is True
    assert out[BUDGET_INFO_KEY]["reason"] == "token"


async def test_scenario_finishes_with_the_option(
    monkeypatch: pytest.MonkeyPatch, audit: list[Any], search: None
) -> None:
    model = _install(monkeypatch, _three_turns())
    ctx = _ctx()

    out = await loop.run_react_node(ctx, _scenario_node(source_load_budget_ratio=0.3))

    assert BUDGET_HIT_KEY not in out and BUDGET_INFO_KEY not in out
    assert out["text"] == "최종 답변"
    assert ctx.usage["total_tokens"] < loop.REACT_DEFAULTS["token_budget"]
    assert json.loads(model.tool_results[0])["budget_fit"]["reason"] == "token_budget"
