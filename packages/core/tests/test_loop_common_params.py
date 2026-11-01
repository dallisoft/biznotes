"""loop 노드 공통 `params`(#390) — 본문 워크플로우가 항목과 함께 덱 수준 값을 받는다.

loop 정의의 선택 키 `params`(dict)를 부모 상태로 한 번 렌더해 본문 params에 `item_var`와 함께
넘긴다. 키가 겹치면 항목이 이긴다. 생략하면 지금과 같다(본문 params는 항목 하나).

이미지 프롬프트 출처(#338)는 키마다 판정한다 — 렌더 템플릿의 뿌리가 안전한 공통 키만 본문에서
안전하다. 실제 `loop → dispatch_agent → execute_agent(본문 그래프)` 경로를 탄다. 대상 조회(세션)와
실행 로그·이미지 프로바이더 호출만 대역이다.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from biznotes_core.services.agents import graph, prompt_provenance
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError
from biznotes_core.services.agents.nodes import retrieval
from biznotes_core.services.agents.templating import render

COMPOSE = {
    "id": "b",
    "type": "json_compose",
    "template": {
        "format": "{params.format}",
        "language": "{params.language}",
        "slide": "{params.slide}",
    },
}


class FakeSession:
    """`_exec_loop`가 쓰는 `get`과 분기 세션의 `close`만 있는 세션."""

    def __init__(self, agents: dict[uuid.UUID, Any]) -> None:
        self.agents = agents

    async def get(self, model: Any, key: Any) -> Any:
        return self.agents.get(key)

    async def close(self) -> None:
        return None


class Harness:
    def __init__(self) -> None:
        self.agents: dict[uuid.UUID, Any] = {}
        self.prompts: list[str] = []  # 본문 image_generate가 렌더한 프롬프트

    def body(self, *nodes: dict[str, Any], output: str | None = None) -> str:
        definition: dict[str, Any] = {"nodes": list(nodes), "edges": []}
        if output is not None:
            definition["output"] = output
        agent = SimpleNamespace(
            id=uuid.uuid4(),
            enabled=True,
            scope="system",
            owner_id=None,
            kind="workflow",
            definition=definition,
            skill_version_id=None,
        )
        self.agents[agent.id] = agent
        return str(agent.id)

    def ctx(self, params: dict[str, Any]) -> RunContext:
        return RunContext(
            run_id=uuid.uuid4(),
            notebook_id=uuid.uuid4(),
            db=FakeSession(self.agents),  # type: ignore[arg-type]
            params=params,
            session_factory=lambda: FakeSession(self.agents),  # type: ignore[arg-type,return-value]
        )


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    h = Harness()

    async def no_log(*_a: Any, **_k: Any) -> None:
        return None

    async def image_spy(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
        h.prompts.append(render(node["prompt"], state))
        return {"key": "k"}

    monkeypatch.setattr(graph, "_log_node_start", no_log)
    monkeypatch.setattr(graph, "_log_node_finish", no_log)
    monkeypatch.setitem(graph.NODE_EXECUTORS, "image_generate", image_spy)
    return h


SLIDES = [{"heading": "a"}, {"heading": "b"}]


def _deck(body_id: str, *, parallel: bool, **extra: Any) -> dict[str, Any]:
    loop = {
        "id": "lp",
        "type": "loop",
        "loop_body_agent_id": body_id,
        "items": "{params.slides}",
        "item_var": "slide",
        "parallel": parallel,
        **extra,
    }
    return {"nodes": [loop], "edges": []}


async def _run_deck(h: Harness, definition: dict[str, Any], params: dict[str, Any]) -> Any:
    """덱 워크플로우를 실제 그래프로 돌려 loop 노드 반환값을 돌려준다."""
    seen: dict[str, Any] = {}
    original = graph.NODE_EXECUTORS["loop"]

    async def capture(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
        seen["out"] = await original(ctx, node, state)
        return seen["out"]

    graph.NODE_EXECUTORS["loop"] = capture
    try:
        await graph.execute_agent(h.ctx(params), "workflow", definition)
    finally:
        graph.NODE_EXECUTORS["loop"] = original
    return seen["out"]


# ── 수락 기준 1: 본문이 `{params.format}`으로 덱 수준 값을 읽는다(직렬·병렬) ──────────


@pytest.mark.parametrize("parallel", [False, True])
async def test_body_reads_deck_level_params_alongside_the_item(
    harness: Harness, parallel: bool
) -> None:
    body = harness.body(COMPOSE, output="{b.json}")
    definition = _deck(
        body,
        parallel=parallel,
        params={"format": "{params.format}", "language": "{params.language}"},
    )

    out = await _run_deck(
        harness, definition, {"slides": SLIDES, "format": "html", "language": "ko"}
    )

    assert [r["output"] for r in out["results"]] == [
        {"format": "html", "language": "ko", "slide": {"heading": "a"}},
        {"format": "html", "language": "ko", "slide": {"heading": "b"}},
    ]


@pytest.mark.parametrize("parallel", [False, True])
async def test_common_params_render_with_the_template_rules(
    harness: Harness, parallel: bool
) -> None:
    """값은 기존 템플릿 렌더(`render_value`) — 문자열 안 치환·중첩 dict·목록·리터럴."""
    body = harness.body(
        {"id": "b", "type": "json_compose", "template": {"style": "{params.style}"}},
        output="{b.json}",
    )
    definition = _deck(
        body,
        parallel=parallel,
        params={"style": {"label": "형식: {params.format}", "tags": ["{params.language}", 3]}},
    )

    out = await _run_deck(
        harness, definition, {"slides": SLIDES, "format": "html", "language": "ko"}
    )

    assert {str(r["output"]) for r in out["results"]} == {
        str({"style": {"label": "형식: html", "tags": ["ko", 3]}})
    }


# ── 수락 기준 2: 키 충돌 시 항목이 이긴다 / 생략하면 같다 ─────────────────────────────


@pytest.mark.parametrize("parallel", [False, True])
async def test_item_wins_on_key_collision(harness: Harness, parallel: bool) -> None:
    body = harness.body(COMPOSE, output="{b.json}")
    definition = _deck(
        body, parallel=parallel, params={"slide": "덮어쓰면 안 된다", "format": "pdf"}
    )

    out = await _run_deck(harness, definition, {"slides": SLIDES})

    assert [r["output"]["slide"] for r in out["results"]] == SLIDES
    assert {r["output"]["format"] for r in out["results"]} == {"pdf"}


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("extra", [{}, {"params": None}, {"params": {}}])
async def test_omitted_params_pass_only_the_item(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, parallel: bool, extra: dict[str, Any]
) -> None:
    seen: list[dict[str, Any]] = []

    async def body_node(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
        seen.append(dict(ctx.params))
        return {"text": "ok"}

    monkeypatch.setitem(graph.NODE_EXECUTORS, "body", body_node)
    body = harness.body({"id": "b", "type": "body"})

    out = await _run_deck(
        harness, _deck(body, parallel=parallel, **extra), {"slides": SLIDES, "format": "html"}
    )

    assert sorted(seen, key=str) == [{"slide": s} for s in SLIDES]
    assert set(out) == {"results"}
    assert [r["item"] for r in out["results"]] == SLIDES


@pytest.mark.parametrize("bad", ["{params.format}", ["format"], 3, True])
async def test_non_object_params_fail_before_the_body_runs(harness: Harness, bad: Any) -> None:
    body = harness.body(COMPOSE, output="{b.json}")
    with pytest.raises(AgentRuntimeError, match="loop params"):
        await _run_deck(harness, _deck(body, parallel=False, params=bad), {"slides": SLIDES})


# ── 수락 기준 3: 공통 params의 이미지 프롬프트 안전 판정이 출처를 따른다 ────────────────


SEARCH = {"id": "search", "type": "rag_search", "query": "q"}
LLM = {"id": "gen", "type": "llm", "prompt": "p"}
STATE = {
    "params": {"format": "html", "slides": SLIDES},
    "search": {"citations": [{"quote": "원문"}], "context": "원문"},
    "gen": {"text": "LLM 묘사"},
}


def _parent_ctx(h: Harness, *, params_safe: bool = True) -> RunContext:
    ctx = h.ctx(STATE["params"])
    ctx.params_image_safe = params_safe
    ctx.image_safe_roots = prompt_provenance.image_safe_roots(
        {n["id"]: n for n in (SEARCH, LLM)}, params_safe=params_safe
    )
    return ctx


def _loop(body_id: str, common: dict[str, Any], *, items: str = "{params.slides}") -> dict:
    return {
        "id": "lp",
        "type": "loop",
        "loop_body_agent_id": body_id,
        "items": items,
        "item_var": "slide",
        "params": common,
    }


def _image_body(h: Harness, prompt: str) -> str:
    return h.body({"id": "img", "type": "image_generate", "prompt": prompt})


@pytest.mark.parametrize(
    ("common", "prompt"),
    [
        ({"format": "{params.format}"}, "{params.format} 삽화: {params.slide.heading}"),
        ({"look": "{gen.text}"}, "{params.look}"),
        ({"look": "고정 문구"}, "{params.look}"),
        ({"style": {"a": "{params.format}", "b": ["{gen.text}"]}}, "{params.style.a}"),
        # 전부 안전하면 params 전체 참조도 지금처럼 안전하다
        ({"format": "{params.format}"}, "{params}"),
    ],
)
async def test_safe_common_params_reach_the_image_prompt(
    harness: Harness, common: dict[str, Any], prompt: str
) -> None:
    node = _loop(_image_body(harness, prompt), common)

    await retrieval._exec_loop(_parent_ctx(harness), node, STATE)

    assert len(harness.prompts) == 2


@pytest.mark.parametrize(
    ("common", "prompt"),
    [
        ({"quote": "{search.citations}"}, "{params.quote}"),
        ({"quote": "근거: {search.context}"}, "{params.quote}"),
        ({"look": "{gen.text|untrusted}"}, "{params.look}"),
        ({"style": {"a": "{params.format}", "b": "{search.context}"}}, "{params.style.a}"),
        # 안전하지 않은 키가 하나라도 있으면 params 전체 참조는 안전하지 않다
        ({"format": "{params.format}", "quote": "{search.citations}"}, "{params}"),
    ],
)
async def test_untrusted_common_params_are_rejected_before_the_body_runs(
    harness: Harness, common: dict[str, Any], prompt: str
) -> None:
    node = _loop(_image_body(harness, prompt), common)

    with pytest.raises(AgentRuntimeError, match="params의 값을 직접 쓸 수 없습니다"):
        await retrieval._exec_loop(_parent_ctx(harness), node, STATE)

    assert harness.prompts == []


async def test_an_untrusted_key_does_not_taint_the_safe_ones(harness: Harness) -> None:
    """키별 판정 — 안전하지 않은 공통 키가 있어도 항목·안전한 키는 그대로 쓸 수 있다."""
    common = {"format": "{params.format}", "quote": "{search.citations}"}
    node = _loop(_image_body(harness, "{params.format}: {params.slide.heading}"), common)

    await retrieval._exec_loop(_parent_ctx(harness), node, STATE)

    assert harness.prompts == ["html: a", "html: b"]


async def test_unsafe_items_stay_unsafe_next_to_safe_common_params(harness: Harness) -> None:
    common = {"format": "{params.format}"}
    ok = _loop(_image_body(harness, "{params.format}"), common, items="{search.citations}")
    await retrieval._exec_loop(_parent_ctx(harness), ok, STATE)
    assert harness.prompts == ["html"]

    bad = _loop(_image_body(harness, "{params.slide.quote}"), common, items="{search.citations}")
    with pytest.raises(AgentRuntimeError, match="params의 값을 직접 쓸 수 없습니다"):
        await retrieval._exec_loop(_parent_ctx(harness), bad, STATE)


async def test_parent_params_unsafe_makes_common_params_unsafe(harness: Harness) -> None:
    """부모 params가 안전하지 않은 하위 실행이면 `{params.x}`를 옮긴 공통 키도 안전하지 않다."""
    node = _loop(_image_body(harness, "{params.format}"), {"format": "{params.format}"})

    with pytest.raises(AgentRuntimeError, match="params의 값을 직접 쓸 수 없습니다"):
        await retrieval._exec_loop(_parent_ctx(harness, params_safe=False), node, STATE)


async def test_item_key_collision_follows_the_item_provenance(harness: Harness) -> None:
    """항목이 이기므로 겹친 키의 출처는 항목 출처다 — 공통 값의 출처가 아니다."""
    safe_items = _loop(
        _image_body(harness, "{params.slide.heading}"), {"slide": "{search.context}"}
    )
    await retrieval._exec_loop(_parent_ctx(harness), safe_items, STATE)
    assert harness.prompts == ["a", "b"]

    unsafe_items = _loop(
        _image_body(harness, "{params.slide}"), {"slide": "고정"}, items="{search.citations}"
    )
    with pytest.raises(AgentRuntimeError, match="params의 값을 직접 쓸 수 없습니다"):
        await retrieval._exec_loop(_parent_ctx(harness), unsafe_items, STATE)


# ── 키별 출처의 전파(중첩 하위 실행)와 판정 단위 ─────────────────────────────────────


def test_per_key_params_roots() -> None:
    roots = prompt_provenance.image_safe_roots({}, params_safe=False, params_safe_keys={"format"})

    assert prompt_provenance.is_image_safe("{params.format}", roots)
    assert prompt_provenance.is_image_safe("{params.format.label}", roots)
    assert not prompt_provenance.is_image_safe("{params.quote}", roots)
    assert not prompt_provenance.is_image_safe("{params}", roots)
    assert not prompt_provenance.is_image_safe("{params.format|untrusted}", roots)
    assert prompt_provenance.loop_items_image_safe("{x}", roots, resolved_path="params.format")
    assert not prompt_provenance.loop_items_image_safe("{x}", roots, resolved_path="params.quote")
    # json_validate의 source도 키별로 따른다
    v = {"id": "v", "type": "json_validate", "source": "{params.format}", "schema": "quiz"}
    w = {"id": "w", "type": "json_validate", "source": "{params.quote}", "schema": "quiz"}
    roots = prompt_provenance.image_safe_roots(
        {"v": v, "w": w}, params_safe=False, params_safe_keys={"format"}
    )
    assert "v" in roots and "w" not in roots


def test_params_safe_ignores_the_key_list() -> None:
    roots = prompt_provenance.image_safe_roots({}, params_safe=True, params_safe_keys={"format"})

    assert prompt_provenance.is_image_safe("{params.quote}", roots)
    assert prompt_provenance.is_image_safe("{params}", roots)


def test_a_dotted_node_id_cannot_pose_as_a_params_key() -> None:
    """노드 id는 임의 문자열이다 — `params.quote`라는 constant 노드가 키 판정을 바꾸지 못한다."""
    node = {"id": "params.quote", "type": "constant", "value": "x"}
    roots = prompt_provenance.image_safe_roots({node["id"]: node}, params_safe=False)

    assert not prompt_provenance.is_image_safe("{params.quote}", roots)


def test_child_carries_the_safe_key_list() -> None:
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={})  # type: ignore[arg-type]
    assert ctx.params_image_safe_keys == frozenset()
    assert ctx.child(uuid.uuid4(), {}).params_image_safe_keys == frozenset()
    child = ctx.child(uuid.uuid4(), {}, params_image_safe_keys=frozenset({"format"}))
    assert (child.params_image_safe, child.params_image_safe_keys) == (False, {"format"})


async def test_safe_keys_propagate_into_a_nested_loop(harness: Harness) -> None:
    """본문이 다시 loop로 `{params.format}`을 넘기면 그 키의 출처를 그대로 이어 간다."""
    inner_ok = _image_body(harness, "{params.format}")
    inner_bad = _image_body(harness, "{params.quote}")
    for inner, ok in ((inner_ok, True), (inner_bad, False)):
        middle = harness.body(
            {
                "id": "lp2",
                "type": "loop",
                "loop_body_agent_id": inner,
                "items": "[1]",
                "params": {"format": "{params.format}", "quote": "{params.quote}"},
            }
        )
        node = _loop(middle, {"format": "{params.format}", "quote": "{search.citations}"})
        if ok:
            await retrieval._exec_loop(_parent_ctx(harness), node, STATE)
        else:
            with pytest.raises(AgentRuntimeError, match="params의 값을 직접 쓸 수 없습니다"):
                await retrieval._exec_loop(_parent_ctx(harness), node, STATE)
    assert harness.prompts == ["html", "html"]
