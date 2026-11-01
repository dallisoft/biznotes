"""rag_search 소스 커버리지 옵션(#446) — `per_source_limit`·`coverage: per_source`.

여기서는 옵션 해석과 노드·도구 출력을 본다 — 검색(`retrieve_context`)은 대역이다. 실제 검색
(pgvector)의 커버리지는 `test_rag_source_coverage_search_pg.py`가 본다.

노드 출력의 모델 컨텍스트 맞춤은 llm 노드 실경로(`_exec_llm` → `_ContextFit`)로 본다.
"""

import uuid
from typing import Any

import pytest

from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.services import rag_chat
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError
from biznotes_core.services.agents.graph import _LOG_INPUT_KEYS
from biznotes_core.services.agents.nodes.llm import (
    _build_messages,
    _dispatch_builtin_tool,
    _exec_llm,
)
from biznotes_core.services.agents.nodes.retrieval import _exec_rag_search, _split_source_load
from biznotes_core.services.agents.tool_registry import _BUILTIN_SPECS
from biznotes_core.services.gateway import circuit
from biznotes_core.services.gateway import llm as gateway
from biznotes_core.services.gateway.llm import ResolvedModel, estimate_prompt_tokens
from biznotes_core.services.rag_chat import Citation, RetrievalResult, SourceCoverage
from biznotes_core.services.untrusted import format_source_excerpts, is_untrusted_block_sequence


@pytest.mark.parametrize(
    ("requested", "expected"), [(None, None), (1, 1), ("3", 3), (0, 1), (-4, 1), (99, 5)]
)
def test_per_source_limit_is_clamped(requested: Any, expected: int | None):
    assert rag_chat.per_source_limit_from(requested) == expected


@pytest.mark.parametrize("value", [None, "ranked", "per_source"])
def test_known_coverage_values(value: Any):
    assert rag_chat.coverage_from(value) == value


@pytest.mark.parametrize("value", ["all", "", 1])
def test_unknown_coverage_is_rejected(value: Any):
    with pytest.raises(ValueError):
        rag_chat.coverage_from(value)


# ── rag_search 노드 ───────────────────────────────────────────────────


def _ctx(db: Any = None, source_ids: list[uuid.UUID] | None = None) -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, source_ids=source_ids
    )


def _citations(n_sources: int, per_source: int = 1, quote: str = "본문") -> list[Citation]:
    out: list[Citation] = []
    for s in range(n_sources):
        sid = str(uuid.UUID(int=s + 1))
        for j in range(per_source):
            out.append(
                Citation(
                    n=len(out) + 1,
                    source_id=sid,
                    chunk_id=str(uuid.uuid4()),
                    quote=f"{quote} {s}-{j}",
                    source_title=f"자료 {s + 1}",
                )
            )
    return out


@pytest.fixture
def fake_retrieve(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"citations": _citations(3, per_source=2)}

    async def fake(_db: Any, _nb: Any, _q: str, source_ids: Any, top_k: Any = None, **kw: Any):
        seen["kwargs"] = kw
        cits = seen["citations"]
        coverage = None
        if kw:
            hits = list(dict.fromkeys(c.source_id for c in cits))
            coverage = SourceCoverage(
                scope_count=len(hits) + 1, hit_sources=hits, missing=["missing-id"]
            )
        return RetrievalResult(citations=cits, insufficient_context=False, coverage=coverage)

    monkeypatch.setattr(rag_chat, "retrieve_context", fake)
    return seen


async def test_node_without_options_keeps_its_output(fake_retrieve):
    out = await _exec_rag_search(_ctx(), {"id": "search", "query": "q"}, {})

    assert fake_retrieve["kwargs"] == {}  # 새 인자를 넘기지 않는다
    assert set(out) == {"context", "citations", "insufficient_context"}
    assert out["context"] == format_source_excerpts(fake_retrieve["citations"], 1500)


async def test_node_with_options_emits_source_blocks_and_coverage(fake_retrieve):
    node = {"id": "search", "query": "q", "coverage": "per_source", "per_source_limit": 2}
    out = await _exec_rag_search(_ctx(), node, {})

    assert fake_retrieve["kwargs"] == {"coverage": "per_source", "per_source_limit": 2}
    assert out["fit"] == "shrink_each" and out["dropped"] == []
    assert out["sources"] == [
        {"id": str(uuid.UUID(int=i + 1)), "title": f"자료 {i + 1}"} for i in range(3)
    ]
    assert out["scope_count"] == 4
    assert out["hit_sources"] == [s["id"] for s in out["sources"]]
    assert out["missing"] == ["missing-id"]
    # source_load와 같은 모양이다 — llm 노드의 컨텍스트 맞춤이 소스 단위로 읽는다
    blocks = _split_source_load(out)
    assert blocks is not None and [src for src, _ in blocks] == out["sources"]
    assert is_untrusted_block_sequence(out["context"])
    # 소스 블록 안의 발췌마다 run 인용 번호 머리가 남는다
    first = blocks[0][1]
    assert first.startswith("## 자료 1") and "[1]\n본문 0-0" in first and "[2]\n본문 0-1" in first
    assert [c["n"] for c in out["citations"]] == list(range(1, 7))


async def test_node_rejects_unknown_coverage(fake_retrieve):
    with pytest.raises(AgentRuntimeError, match="coverage"):
        await _exec_rag_search(_ctx(), {"id": "s", "query": "q", "coverage": "all"}, {})


def test_options_are_logged_as_node_input():
    assert {"per_source_limit", "coverage"} <= set(_LOG_INPUT_KEYS)


# ── llm 노드 컨텍스트 맞춤: per_source 결과는 소스를 빼지 않는다 ─────────


def _model(name: str, window: int) -> ResolvedModel:
    provider = LLMProvider(id=uuid.uuid4(), name=f"p-{name}", type="mock")
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
    spy: dict[str, Any] = {"chain": [], "sent": []}
    real = gateway.complete_stream

    async def chain(_db: Any, _model_id: Any, modality: str = "llm") -> list[ResolvedModel]:
        return spy["chain"]

    async def stream(candidate: ResolvedModel, messages: Any, *a: Any, **k: Any) -> Any:
        spy["sent"].append(messages)
        return await real(candidate, messages, *a, **k)

    async def closed(_pid: Any) -> bool:
        return False

    async def noop(_pid: Any) -> None:
        return None

    monkeypatch.setattr(gateway, "resolve_chain", chain)
    monkeypatch.setattr(gateway, "complete_stream", stream)
    monkeypatch.setattr(circuit, "is_open", closed)
    monkeypatch.setattr(circuit, "record_success", noop)
    monkeypatch.setattr(circuit, "record_failure", closed)
    return spy


async def test_per_source_output_shrinks_without_dropping_on_a_32k_model(
    fake_retrieve, gateway_spy
):
    body = "소스마다 질의와 가장 관련 높은 발췌를 담는다. " * 60
    fake_retrieve["citations"] = _citations(500, quote=body)
    search = await _exec_rag_search(
        _ctx(), {"id": "search", "query": "q", "coverage": "per_source"}, {}
    )
    prompt = "다음 자료로 보고서를 쓰세요.\n\n{search.context}"
    full = estimate_prompt_tokens(_build_messages("", prompt, {"search": search}, None))
    assert full > 100_000  # 그대로는 32k 모델에 들어가지 않는다
    gateway_spy["chain"] = [_model("gpt-oss-32k", 32_768)]

    node = {"id": "gen", "type": "llm", "prompt": prompt, "max_tokens": 4096}
    out = await _exec_llm(_ctx(), node, {"search": search})

    [sent] = gateway_spy["sent"]
    assert estimate_prompt_tokens(sent) + 4096 <= 32_768
    # 소스를 빼지 않고 줄였다 — 뺀 소스(`context_window`) 없이 자른 소스(`_partial`)만 남는다
    assert {d["reason"] for d in out["dropped"]} == {"context_window_partial"}
    assert len(out["dropped"]) == 500
    user = sent[-1]["content"]
    assert all(f"## 자료 {i + 1}\n" in user for i in range(500))


# ── rag_search 도구 ───────────────────────────────────────────────────


def test_tool_spec_exposes_the_options():
    props = _BUILTIN_SPECS["rag_search"]["function"]["parameters"]["properties"]
    assert props["per_source_limit"]["type"] == "integer"
    assert props["coverage"]["enum"] == ["ranked", "per_source"]
    assert _BUILTIN_SPECS["rag_search"]["function"]["parameters"]["required"] == ["query"]


async def test_tool_without_options_keeps_its_output(fake_retrieve, monkeypatch):
    async def top_k(_db: Any, requested: Any) -> int:
        return 6

    monkeypatch.setattr(rag_chat, "resolve_top_k", top_k)
    text = await _dispatch_builtin_tool(_ctx(), "rag_search", {"query": "q"})

    assert fake_retrieve["kwargs"] == {}
    assert text == format_source_excerpts(fake_retrieve["citations"], 1000)


async def test_tool_per_source_output_is_bounded_and_keeps_every_source(fake_retrieve, monkeypatch):
    async def top_k(_db: Any, requested: Any) -> int:
        return 6

    monkeypatch.setattr(rag_chat, "resolve_top_k", top_k)
    fake_retrieve["citations"] = _citations(500, quote="긴 발췌 " * 300)
    ctx = _ctx()
    text = await _dispatch_builtin_tool(
        ctx, "rag_search", {"query": "q", "coverage": "per_source", "per_source_limit": "1"}
    )

    assert fake_retrieve["kwargs"] == {"coverage": "per_source", "per_source_limit": 1}
    blocks_text, summary = text.rsplit("\n\n", 1)
    assert is_untrusted_block_sequence(blocks_text)
    assert all(f"## 자료 {i + 1}\n" in blocks_text for i in range(500))
    assert summary == "[범위 소스 501개 중 500개에서 발췌를 찾았습니다]"
    # 발췌는 줄여서 담는다 — ranked 최대 출력(30발췌 × 1000자) 수준을 넘지 않게
    uncapped = format_source_excerpts(fake_retrieve["citations"], 1000)
    assert len(uncapped) > 500_000 and len(blocks_text) < 90_000
    # 근거표에는 모든 발췌가 원문으로 남는다
    evidence = [e for e in ctx.tool_evidence if e["kind"] == "source"]
    assert len(evidence) == 500
    assert evidence[0]["citation"]["quote"] == fake_retrieve["citations"][0].quote
