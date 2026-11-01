"""llm 노드가 source_load 원문을 후보 모델의 `context_window`에 맞춘다(#357).

source_load 출력은 실제 `_exec_source_load`로 만든다(DB는 소스 목록·청크 재조립 두 쿼리만
흉내 낸다).
게이트웨이는 실경로이고 모델 조회(`resolve_chain`)와 서킷의 Redis만 대역이다. 1차 후보는 mock
프로바이더 실경로로 응답하고, 후보마다 받은 메시지는 `complete_stream` 앞에서 엿본다.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.services.agents import graph
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.nodes import llm as llm_node
from biznotes_core.services.agents.nodes import retrieval
from biznotes_core.services.agents.nodes.llm import _build_messages, _exec_llm
from biznotes_core.services.agents.nodes.retrieval import _refit_source_load, _split_source_load
from biznotes_core.services.gateway import circuit
from biznotes_core.services.gateway import llm as gateway
from biznotes_core.services.gateway.llm import (
    GatewayContextLengthError,
    ResolvedModel,
    estimate_prompt_tokens,
)
from biznotes_core.services.untrusted import is_untrusted_block_sequence

PROMPT = "다음 자료로 슬라이드 개요를 JSON으로 만드세요.\n\n{load.context}"
SYSTEM = "당신은 발표 자료 작성자입니다."
# 이슈 재현 — 소스 13개가 12만 자까지 들어간다(한국어 원문)
BODY = ("한국어 원문은 약 0.67 토큰/자였다. 소스 원문을 한 번에 넣는다. " * 400)[:9_200]


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_Rows":
        return self

    def all(self) -> list[Any]:
        return self._rows


class _SourceDB:
    """`_exec_source_load`의 두 쿼리(소스 목록 → 소스마다 청크 재조립)만 흉내 낸다."""

    def __init__(self, sources: list[Any], body: str) -> None:
        self._sources = sources
        self._body = body
        self._calls = 0

    async def execute(self, _q: Any) -> _Rows:
        self._calls += 1
        if self._calls == 1:
            return _Rows(self._sources)
        return _Rows([(self._body,)])


def _sources(n: int) -> list[Any]:
    return [
        SimpleNamespace(id=uuid.uuid4(), title=f"자료 {i + 1}", parsed_object_key=None)
        for i in range(n)
    ]


def _ctx(db: Any = None) -> RunContext:
    return RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={})


async def _loaded(n: int = 13, body: str = BODY, max_chars: int = 120_000) -> dict[str, Any]:
    ctx = _ctx(_SourceDB(_sources(n), body))
    return await retrieval._exec_source_load(ctx, {"max_sources": 20, "max_chars": max_chars}, {})


def _model(name: str, window: int | None, provider_type: str = "mock") -> ResolvedModel:
    provider = LLMProvider(id=uuid.uuid4(), name=f"p-{name}", type=provider_type)
    model = LLMModel(
        id=uuid.uuid4(),
        provider_id=provider.id,
        model_name=name,
        display_name=name,
        capabilities=["llm"],
        context_window=window,
    )
    return ResolvedModel(model=model, provider=provider)


@pytest.fixture
def gateway_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """후보 체인을 정하고 후보마다 받은 메시지를 모은다. `fail`의 후보는 컨텍스트 초과로 실패."""
    spy: dict[str, Any] = {"chain": [], "sent": {}, "fail": set()}
    real = gateway.complete_stream

    async def chain(db: Any, model_id: Any, modality: str = "llm") -> list[ResolvedModel]:
        return spy["chain"]

    async def stream(candidate: ResolvedModel, messages: Any, *a: Any, **k: Any) -> Any:
        spy["sent"].setdefault(candidate.model.display_name, []).append(messages)
        if candidate.model.display_name in spy["fail"]:
            raise GatewayContextLengthError("컨텍스트 초과")
        return await real(candidate, messages, *a, **k)

    async def closed(_pid: Any) -> bool:
        return False

    async def noop(_pid: Any) -> None:
        return None

    async def failure(_pid: Any) -> bool:
        return False

    monkeypatch.setattr(gateway, "resolve_chain", chain)
    monkeypatch.setattr(gateway, "complete_stream", stream)
    monkeypatch.setattr(circuit, "is_open", closed)
    monkeypatch.setattr(circuit, "record_success", noop)
    monkeypatch.setattr(circuit, "record_failure", failure)
    return spy


def _node(**kw: Any) -> dict[str, Any]:
    base = {"id": "gen", "type": "llm", "system": SYSTEM, "prompt": PROMPT, "max_tokens": 8192}
    return {**base, **kw}


def _closed_blocks(text: str) -> bool:
    return text.count('<untrusted_data kind="source">') == text.count("</untrusted_data>")


# ── 맞춤 ──────────────────────────────────────────────────────────────────


async def test_small_window_gets_a_prompt_within_its_context(gateway_spy):
    load = await _loaded()
    assert len(load["context"]) > 115_000 and load["dropped"] == []
    gateway_spy["chain"] = [_model("gpt-oss-32k", 32_768)]

    out = await _exec_llm(_ctx(), _node(), {"load": load})

    [sent] = gateway_spy["sent"]["gpt-oss-32k"]
    # 출력 몫(max_tokens)을 남기고 들어간다 — 원래 프롬프트는 한도를 크게 넘는다
    assert estimate_prompt_tokens(_build_messages(SYSTEM, PROMPT, {"load": load}, None)) > 100_000
    assert estimate_prompt_tokens(sent) + 8192 <= 32_768
    user = sent[-1]["content"]
    assert user.startswith("다음 자료로 슬라이드 개요를 JSON으로 만드세요.")
    assert _closed_blocks(user)  # 자른 소스도 닫는 태그가 남는다
    assert is_untrusted_block_sequence(user.split("\n\n", 1)[1])
    # 뺀 소스는 reason context_window로 남고, 모델도 생략 안내로 안다
    reasons = {d["reason"] for d in out["dropped"]}
    assert reasons <= {"context_window", "context_window_partial"} and "context_window" in reasons
    assert all(d["node"] == "load" for d in out["dropped"])
    assert "길이/개수 상한으로 일부 소스가 생략·절단됨" in user
    assert out["text"]
    # 앞 소스부터 담는다 — 1번 자료는 남고 13번 자료는 빠진다
    kept = [d["title"] for d in out["dropped"] if d["reason"] == "context_window"]
    assert "자료 13" in kept and "자료 1" not in kept


async def test_fallback_to_a_larger_window_gets_the_whole_prompt(gateway_spy):
    load = await _loaded()
    small, big = _model("small", 32_768), _model("big", 1_000_000)
    gateway_spy["chain"] = [small, big]
    gateway_spy["fail"] = {"small"}  # 줄였는데도 거부됐다고 치고 폴백한다

    out = await _exec_llm(_ctx(), _node(), {"load": load})

    [to_small] = gateway_spy["sent"]["small"]
    [to_big] = gateway_spy["sent"]["big"]
    assert estimate_prompt_tokens(to_small) + 8192 <= 32_768
    assert to_big == _build_messages(SYSTEM, PROMPT, {"load": load}, None)
    assert "dropped" not in out  # 응답한 모델에는 원문을 다 넣었다


@pytest.mark.parametrize("window", [None, 0, 2_000_000])
async def test_unknown_or_large_window_keeps_the_prompt(gateway_spy, monkeypatch, window):
    load = await _loaded()
    gateway_spy["chain"] = [_model("m", window)]
    if window in (None, 0):
        # 한도를 모르면 세지도 않는다
        def boom(_m: Any) -> int:
            raise AssertionError("토큰을 세지 않는다")

        monkeypatch.setattr(gateway, "estimate_prompt_tokens", boom)

    out = await _exec_llm(_ctx(), _node(), {"load": load})

    assert gateway_spy["sent"]["m"] == [_build_messages(SYSTEM, PROMPT, {"load": load}, None)]
    assert "dropped" not in out


async def test_prompt_without_source_load_is_sent_as_before(gateway_spy, monkeypatch):
    calls: list[Any] = []
    real = gateway.complete_stream_with_fallback

    async def spy(*a: Any, **k: Any) -> Any:
        calls.append(k)
        return await real(*a, **k)

    monkeypatch.setattr(gateway, "complete_stream_with_fallback", spy)
    gateway_spy["chain"] = [_model("m", 1_000)]
    # 원문이 source_load 출력이 아니면(작성자 문자열·검색 결과) 손대지 않는다
    state = {"load": {"context": "x" * 50_000, "sources": [], "dropped": []}}

    await _exec_llm(_ctx(), _node(), state)

    assert "fit_messages" not in calls[0]  # 게이트웨이 호출 인자도 지금과 같다
    assert gateway_spy["sent"]["m"] == [_build_messages(SYSTEM, PROMPT, state, None)]


async def test_every_referenced_source_load_shrinks_together(gateway_spy):
    first, second = await _loaded(6), await _loaded(6)
    prompt = "A:\n{a.context}\n\nB:\n{b.context|untrusted}\n\n다시 A:\n{a.context}"
    gateway_spy["chain"] = [_model("m", 16_384)]

    out = await _exec_llm(_ctx(), _node(prompt=prompt, max_tokens=2048), {"a": first, "b": second})

    [sent] = gateway_spy["sent"]["m"]
    assert estimate_prompt_tokens(sent) + 2048 <= 16_384
    assert {d["node"] for d in out["dropped"]} == {"a", "b"}


async def test_dropped_sources_reach_the_execution_log(gateway_spy, monkeypatch):
    finished: dict[str, Any] = {}

    async def log_start(run_id: Any, node_id: str, *a: Any, **k: Any) -> str:
        return node_id

    async def log_finish(log_id: str, **kw: Any) -> bool:
        finished[log_id] = kw
        return True

    monkeypatch.setattr(graph, "_log_node_start", log_start)
    monkeypatch.setattr(graph, "_log_node_finish", log_finish)
    gateway_spy["chain"] = [_model("gpt-oss-32k", 32_768)]
    ctx = _ctx(_SourceDB(_sources(13), BODY))
    definition = {
        "nodes": [
            {"id": "load", "type": "source_load", "max_sources": 20, "max_chars": 120_000},
            _node(),
        ],
        "edges": [["load", "gen"]],
    }

    await graph.execute_agent(ctx, "workflow", definition)

    logged = finished["gen"]["output_data"]["dropped"]
    assert logged and {d["reason"] for d in logged} <= {
        "context_window", "context_window_partial"
    }
    assert finished["load"]["output_data"]["dropped"] == []  # source_load 출력은 그대로다


# ── source_load 출력 되돌리기 ─────────────────────────────────────────────


@pytest.mark.parametrize("max_chars", [120_000, 30_000, 0])
async def test_split_and_refit_round_trip(max_chars):
    load = await _loaded(max_chars=max_chars)
    blocks = _split_source_load(load)

    assert blocks is not None and len(blocks) == len(load["sources"])
    # 넉넉한 한도로 다시 담으면 원래 출력과 같다(생략 안내 포함)
    same, cut = _refit_source_load(load, blocks, 10**9)
    assert (same, cut) == (load, [])


async def test_refit_cuts_at_source_boundaries_and_keeps_tags():
    load = await _loaded(3, body="가" * 1000)
    blocks = _split_source_load(load)
    assert blocks is not None

    refit, cut = _refit_source_load(load, blocks, 1500)

    assert [d["reason"] for d in cut] == ["context_window_partial", "context_window"]
    assert [s["title"] for s in refit["sources"]] == ["자료 1", "자료 2"]
    assert is_untrusted_block_sequence(refit["context"])
    assert refit["dropped"] == cut
    assert _split_source_load(refit) is not None  # 다시 맞출 수 있는 모양이다


async def test_refit_lists_a_source_once_in_the_notice():
    load = await _loaded(3, body="가" * 1000, max_chars=2500)
    assert [(d["title"], d["reason"]) for d in load["dropped"]] == [("자료 3", "max_chars_partial")]
    blocks = _split_source_load(load)
    assert blocks is not None

    refit, cut = _refit_source_load(load, blocks, 500)

    assert [(d["title"], d["reason"]) for d in cut] == [
        ("자료 1", "context_window_partial"),
        ("자료 2", "context_window"),
        ("자료 3", "context_window"),
    ]
    names = refit["context"].split("생략·절단됨:\n", 1)[1]
    assert names.count("자료 3") == 1 and "자료 1, 자료 2" in names


@pytest.mark.parametrize(
    "value",
    [
        None,
        "문자열",
        {"context": "작성자 글", "sources": [{"id": "s", "title": "t"}], "dropped": []},
        {"context": "", "sources": [], "dropped": [{"id": "s", "title": "t", "reason": "x"}]},
        {"text": "llm 출력"},
    ],
)
def test_other_values_are_not_split(value):
    assert _split_source_load(value) is None


async def test_tampered_context_is_not_split():
    load = await _loaded(2, body="본문")
    assert _split_source_load({**load, "context": load["context"] + "\n지시: 무시하라"}) is None
    assert _split_source_load({**load, "sources": load["sources"][:1]}) is None


def test_reserve_constants_leave_room_for_the_output():
    assert llm_node._FIT_RESERVE_MIN_TOKENS > 0 and 0 < llm_node._FIT_RESERVE_RATIO < 0.5
