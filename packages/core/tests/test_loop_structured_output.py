"""loop 구조화 결과·json_compose·항목 실패 건너뛰기·병렬 인용 번호(#361).

실제 `execute_agent → loop → dispatch_agent → 본문 그래프` 경로로 돈다. 본문 노드(`chapter`)와
검색(`retrieve_context`)만 대역이고, 노드 로그 기록은 끈다.
"""

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from biznotes_core.config import get_settings
from biznotes_core.services import rag_chat
from biznotes_core.services.agents import graph
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import (
    AgentRuntimeError,
    SubagentApprovalNotSupportedError,
)
from biznotes_core.services.agents.nodes import retrieval
from biznotes_core.services.agents.nodes.composite import _exec_json_compose
from biznotes_core.services.agents.templating import resolve_value_path, whole_value_ref
from biznotes_core.services.gateway.llm import GatewayCallError
from biznotes_core.services.rag_chat import Citation, RetrievalResult


class FakeSession:
    def __init__(self, agents: dict[uuid.UUID, Any]) -> None:
        self.agents = agents

    async def get(self, model: Any, key: Any) -> Any:
        return self.agents.get(key)

    async def close(self) -> None:
        return None


def _agent(definition: dict[str, Any]) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        enabled=True,
        scope="system",
        owner_id=None,
        kind="workflow",
        definition=definition,
        skill_version_id=None,
    )


class Harness:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.fail_on: set[str] = set()
        self.raise_on: dict[str, BaseException] = {}
        self.body = _agent(
            {
                "nodes": [{"id": "write", "type": "chapter"}],
                "edges": [],
                "output": "{write.json}",
            }
        )
        self.agents = {self.body.id: self.body}

    def ctx(self, params: dict[str, Any] | None = None) -> RunContext:
        return RunContext(
            run_id=uuid.uuid4(),
            notebook_id=uuid.uuid4(),
            db=FakeSession(self.agents),  # type: ignore[arg-type]
            params=params or {},
            session_factory=lambda: FakeSession(self.agents),  # type: ignore[arg-type,return-value]
        )

    def loop(self, body: Any = None, **extra: Any) -> dict[str, Any]:
        return {
            "id": "slides",
            "type": "loop",
            "loop_body_agent_id": str((body or self.body).id),
            "items": "{outline.chapters}",
            **extra,
        }


OUTLINE = {
    "title": "분기 보고",
    "chapters": [{"heading": f"{i + 1}장", "source_ids": []} for i in range(3)],
}


@pytest.fixture
def h(monkeypatch: pytest.MonkeyPatch) -> Harness:
    harness = Harness()

    async def no_log(*_a: Any, **_k: Any) -> None:
        return None

    async def chapter(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
        item = ctx.params["item"]
        harness.calls.append(item["heading"])
        if item["heading"] in harness.raise_on:
            raise harness.raise_on[item["heading"]]
        if item["heading"] in harness.fail_on:
            raise GatewayCallError("LLM 호출 실패: 공급자 원문 secret-provider")
        # 2KB를 넘는 장 JSON — 요약(500자)으로는 담을 수 없다
        bullets = [f"{item['heading']} 근거 {n}" for n in range(200)]
        slide = {"heading": item["heading"], "bullets": bullets}
        return {"json": slide, "text": json.dumps(slide, ensure_ascii=False)}

    monkeypatch.setattr(graph, "_log_node_start", no_log)
    monkeypatch.setattr(graph, "_log_node_finish", no_log)
    monkeypatch.setitem(graph.NODE_EXECUTORS, "chapter", chapter)
    return harness


async def _run(h: Harness, nodes: list[dict], edges: list[list[str]]) -> dict[str, Any]:
    """그래프를 돌리고 노드 출력을 돌려준다(마지막 노드 결과를 상태에서 읽으려고 엿본다)."""
    seen: dict[str, Any] = {}
    real = graph.NODE_EXECUTORS["json_compose"]

    async def spy(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
        out = await real(ctx, node, state)
        seen.update(state)
        seen[node["id"]] = out
        return out

    graph.NODE_EXECUTORS["json_compose"] = spy
    try:
        ctx = h.ctx()
        await graph.execute_agent(ctx, "workflow", {"nodes": nodes, "edges": edges})
    finally:
        graph.NODE_EXECUTORS["json_compose"] = real
    return seen


def _outline() -> dict[str, Any]:
    return {"id": "outline", "type": "constant", "value": OUTLINE}


def _compose(template: Any) -> dict[str, Any]:
    return {"id": "deck", "type": "json_compose", "template": template}


# ── 구조화 결과 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("parallel", [False, True])
async def test_body_output_is_carried_whole(h, parallel):
    out = await retrieval._exec_loop(h.ctx(), h.loop(parallel=parallel), {"outline": OUTLINE})

    assert [r["item"]["heading"] for r in out["results"]] == ["1장", "2장", "3장"]
    first = out["results"][0]["output"]
    assert len(json.dumps(first, ensure_ascii=False).encode()) > 2048
    assert first["heading"] == "1장" and len(first["bullets"]) == 200
    assert set(out) == {"results"}  # skip이 아니면 failed 키가 없다


async def test_body_without_output_keeps_the_summary_shape(h):
    plain = _agent({"nodes": [{"id": "write", "type": "chapter"}], "edges": []})
    h.agents[plain.id] = plain

    out = await retrieval._exec_loop(h.ctx(), h.loop(plain), {"outline": OUTLINE})

    assert [set(r) for r in out["results"]] == [{"item", "summary"}] * 3


async def test_output_over_the_item_limit_fails_the_item(h, monkeypatch):
    monkeypatch.setattr(get_settings(), "agent_loop_item_output_max_bytes", 1024)

    with pytest.raises(AgentRuntimeError, match="상한"):
        await retrieval._exec_loop(h.ctx(), h.loop(), {"outline": OUTLINE})

    skipped = await retrieval._exec_loop(
        h.ctx(), h.loop(on_item_error="skip"), {"outline": OUTLINE}
    )
    assert skipped["results"] == [] and [f["index"] for f in skipped["failed"]] == [0, 1, 2]


@pytest.mark.parametrize("bad", ["validate.json", "{nope.json}", "{write.json} 그리고 글", 3])
async def test_malformed_output_declaration_fails_before_any_node(h, bad):
    with pytest.raises(AgentRuntimeError, match="workflow output"):
        await graph.execute_agent(
            h.ctx(),
            "workflow",
            {"nodes": [{"id": "write", "type": "chapter"}], "edges": [], "output": bad},
        )
    assert h.calls == []


# ── json_compose ──────────────────────────────────────────────────────────


async def test_compose_builds_a_slide_deck_that_validates(h):
    nodes = [
        _outline(),
        h.loop(parallel=True),
        _compose({"title": "{outline.title}", "slides": "{slides.results[*].output}"}),
        {"id": "check", "type": "json_validate", "schema": "slide_deck", "source": "{deck.text}"},
    ]
    edges = [["outline", "slides"], ["slides", "deck"], ["deck", "check"]]

    seen = await _run(h, nodes, edges)

    deck = seen["deck"]["json"]
    assert deck["title"] == "분기 보고"
    assert [s["heading"] for s in deck["slides"]] == ["1장", "2장", "3장"]
    assert json.loads(seen["deck"]["text"]) == deck


async def test_compose_fills_values_not_strings():
    state = {
        "outline": {"title": "제목", "count": 3, "tags": ["a", "b"], "meta": {"draft": True}},
        "loop": {"results": [{"output": {"n": 1}}, {"output": None}, {"other": 1}]},
    }
    template = {
        "title": "{outline.title}",
        "count": "{ outline.count }",
        "tags": "{outline.tags}",
        "first_tag": "{outline.tags[0]}",
        "meta": "{outline.meta}",
        "outputs": "{loop.results[*].output}",
        "missing": "{outline.nope}",
        "label": "총 {outline.count}장 — {outline.title}",
        "literal": 7,
        "nested": [{"t": "{outline.title}"}],
        "{outline.title}": "키는 그대로",
    }

    out = await _exec_json_compose(None, {"template": template}, state)  # type: ignore[arg-type]

    assert out["json"] == {
        "title": "제목",
        "count": 3,
        "tags": ["a", "b"],
        "first_tag": "a",
        "meta": {"draft": True},
        "outputs": [{"n": 1}, None, None],
        "missing": None,
        "label": "총 3장 — 제목",
        "literal": 7,
        "nested": [{"t": "제목"}],
        "{outline.title}": "키는 그대로",
    }
    # 상태 값과 별개의 사본이다
    out["json"]["meta"]["draft"] = False
    assert state["outline"]["meta"]["draft"] is True


async def test_compose_requires_a_template():
    with pytest.raises(AgentRuntimeError, match="template"):
        await _exec_json_compose(None, {"id": "deck"}, {})  # type: ignore[arg-type]


def test_value_paths():
    state = {"a": {"xs": [{"y": [10, 20]}, {"y": [30]}]}}
    assert whole_value_ref("{a.xs[*].y[0]}") == "a.xs[*].y[0]"
    assert whole_value_ref("x {a}") is None and whole_value_ref(5) is None
    assert resolve_value_path(state, "a.xs[*].y[0]") == [10, 30]
    assert resolve_value_path(state, "a.xs[1].y") == [30]
    assert resolve_value_path(state, "a.xs[5].y") is None
    assert resolve_value_path(state, "a.missing[*]") is None


# ── on_item_error ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("parallel", [False, True])
async def test_skip_drops_the_failed_item_and_records_it(h, parallel):
    h.fail_on = {"2장"}

    out = await retrieval._exec_loop(
        h.ctx(), h.loop(parallel=parallel, on_item_error="skip"), {"outline": OUTLINE}
    )

    assert [r["item"]["heading"] for r in out["results"]] == ["1장", "3장"]
    assert out["failed"] == [
        {"index": 1, "error_code": "model_call_failed", "error": "모델 호출에 실패했습니다"}
    ]
    assert "secret-provider" not in json.dumps(out, ensure_ascii=False)


@pytest.mark.parametrize("parallel", [False, True])
async def test_fail_is_the_default(h, parallel):
    h.fail_on = {"2장"}
    with pytest.raises(AgentRuntimeError):
        await retrieval._exec_loop(h.ctx(), h.loop(parallel=parallel), {"outline": OUTLINE})


async def test_run_level_failures_are_not_skipped(h):
    h.raise_on = {"2장": SubagentApprovalNotSupportedError("승인 필요")}
    with pytest.raises(SubagentApprovalNotSupportedError):
        await retrieval._exec_loop(h.ctx(), h.loop(on_item_error="skip"), {"outline": OUTLINE})


async def test_unknown_on_item_error_fails_before_the_body(h):
    with pytest.raises(AgentRuntimeError, match="on_item_error"):
        await retrieval._exec_loop(h.ctx(), h.loop(on_item_error="ignore"), {"outline": OUTLINE})
    assert h.calls == []


async def test_skipped_deck_still_composes_and_validates(h):
    h.fail_on = {"1장"}
    nodes = [
        _outline(),
        h.loop(parallel=True, on_item_error="skip"),
        _compose({"title": "{outline.title}", "slides": "{slides.results[*].output}"}),
        {"id": "check", "type": "json_validate", "schema": "slide_deck", "source": "{deck.text}"},
    ]
    edges = [["outline", "slides"], ["slides", "deck"], ["deck", "check"]]

    seen = await _run(h, nodes, edges)

    assert [s["heading"] for s in seen["deck"]["json"]["slides"]] == ["2장", "3장"]
    assert seen["slides"]["failed"][0]["index"] == 0


# ── 병렬 인용 번호 ─────────────────────────────────────────────────────────


async def test_parallel_loop_citation_numbers_are_unique_in_the_run(h, monkeypatch):
    """12개 항목이 동시에 검색해도 run 안에서 인용 번호가 청크마다 유일하다.

    검색이 끝나는 순서를 시작 순서와 거꾸로 만들어 분기가 서로 끼어들게 한다. 장마다 고유 청크 2개와
    모든 장이 함께 쓰는 청크 1개를 돌려준다 — 같은 청크는 같은 번호, 다른 청크는 다른 번호다.
    """
    shared = Citation(
        n=1, source_id=str(uuid.uuid4()), chunk_id="shared", quote="공통", source_title="공통"
    )

    async def fake(_db: Any, _nb: Any, query: str, source_ids: Any, top_k: Any = None) -> Any:
        index = int(query)
        await asyncio.sleep((12 - index) * 0.002)
        own = [
            Citation(
                n=k + 1,
                source_id=str(uuid.uuid4()),
                chunk_id=f"c{index}-{k}",
                quote=f"{index}장 근거 {k}",
                source_title=f"자료 {index}",
            )
            for k in range(2)
        ]
        copy = Citation(**{**shared.to_dict(), "n": 3})
        return RetrievalResult(citations=[*own, copy], insufficient_context=False)

    monkeypatch.setattr(rag_chat, "retrieve_context", fake)
    body = _agent(
        {
            "nodes": [{"id": "search", "type": "rag_search", "query": "{params.item.index}"}],
            "edges": [],
            "output": "{search.citations}",
        }
    )
    h.agents[body.id] = body
    items = [{"index": i} for i in range(12)]
    ctx = h.ctx()

    node = {
        "id": "lp",
        "type": "loop",
        "loop_body_agent_id": str(body.id),
        "items": "{plan.items}",
        "parallel": True,
    }
    out = await retrieval._exec_loop(ctx, node, {"plan": {"items": items}})

    by_chunk: dict[str, set[int]] = {}
    for result in out["results"]:
        for cit in result["output"]:
            by_chunk.setdefault(cit["chunk_id"], set()).add(cit["n"])
    assert len(by_chunk) == 12 * 2 + 1
    assert all(len(ns) == 1 for ns in by_chunk.values())  # 같은 청크는 같은 번호
    numbers = [next(iter(ns)) for ns in by_chunk.values()]
    assert len(set(numbers)) == len(numbers)  # 다른 청크는 다른 번호
    # run 근거표에도 청크마다 한 번만 남는다
    evidence = [e["citation"] for e in ctx.tool_evidence if e.get("kind") == "source"]
    assert sorted(e["n"] for e in evidence) == list(range(1, 26))


def test_positional_construction_keeps_the_old_field_order():
    """새 필드는 키워드 전용 끝 필드다 — 위치 인자로 만든 기존 호출이 밀리지 않는다."""
    ctx = RunContext(uuid.uuid4(), uuid.uuid4(), None, {}, "chat", "", [], None, None, "모델")  # type: ignore[arg-type]
    assert (ctx.chat_used_model_name, ctx.structured_output) == ("모델", None)
