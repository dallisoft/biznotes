"""loop 노드 `max_items` 상한(#342).

루프 안 `image_generate`의 장수는 LLM이 쓴 목록 길이가 정한다. `max_items`는 루프가 도는 항목
수를 앞에서부터 묶는다 — 정수 또는 템플릿 문자열(렌더 후 10진 정수), 0 이상. 잘못된 값은 본문을
부르기 전에 실패한다. 생략하면 지금과 같다.
실제 `execute_agent → loop → dispatch_agent → 그래프` 경로로 본문 노드가 돈 항목을 센다.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from biznotes_core.services.agents import graph
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError


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
        self.ran: list[Any] = []  # 본문 노드가 실제로 돈 항목
        self.body = SimpleNamespace(
            id=uuid.uuid4(),
            enabled=True,
            scope="system",
            owner_id=None,
            kind="workflow",
            definition={"nodes": [{"id": "b", "type": "body"}], "edges": []},
            skill_version_id=None,
        )
        self.agents = {self.body.id: self.body}

    def definition(self, items: Any, *, parallel: bool = False, **extra: Any) -> dict[str, Any]:
        node = {
            "id": "lp",
            "type": "loop",
            "loop_body_agent_id": str(self.body.id),
            "items": items,
            "parallel": parallel,
            **extra,
        }
        return {"nodes": [node], "edges": []}

    def ctx(self, params: dict[str, Any] | None = None) -> RunContext:
        return RunContext(
            run_id=uuid.uuid4(),
            notebook_id=uuid.uuid4(),
            db=FakeSession(self.agents),  # type: ignore[arg-type]
            params=params or {},
            session_factory=lambda: FakeSession(self.agents),  # type: ignore[arg-type,return-value]
        )

    async def run(self, definition: dict[str, Any], params: dict[str, Any] | None = None) -> Any:
        ctx = self.ctx(params)
        await graph.execute_agent(ctx, "workflow", definition)
        return ctx


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    h = Harness()

    async def no_log(*_a: Any, **_k: Any) -> None:
        return None

    async def body_node(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
        h.ran.append(ctx.params["item"])
        return {"text": f"ok-{ctx.params['item']}"}

    monkeypatch.setattr(graph, "_log_node_start", no_log)
    monkeypatch.setattr(graph, "_log_node_finish", no_log)
    monkeypatch.setitem(graph.NODE_EXECUTORS, "body", body_node)
    return h


async def _loop_output(h: Harness, definition: dict[str, Any], params: Any = None) -> Any:
    """loop 노드의 반환값 — 상위 그래프가 다음 노드에 넘기는 `state["lp"]`."""
    from biznotes_core.services.agents.nodes import retrieval

    node = definition["nodes"][0]
    return await retrieval._exec_loop(h.ctx(params), node, {"params": params or {}})


@pytest.mark.parametrize("parallel", [False, True])
async def test_omitted_max_items_runs_every_item_and_keeps_the_result_shape(
    harness: Harness, parallel: bool
) -> None:
    out = await _loop_output(harness, harness.definition([1, 2, 3], parallel=parallel))

    assert sorted(harness.ran) == [1, 2, 3]
    assert set(out) == {"results"}


@pytest.mark.parametrize("parallel", [False, True])
async def test_integer_max_items_runs_only_the_leading_items(
    harness: Harness, parallel: bool
) -> None:
    out = await _loop_output(
        harness, harness.definition([1, 2, 3, 4, 5], parallel=parallel, max_items=2)
    )

    assert sorted(harness.ran) == [1, 2]
    assert [r["item"] for r in out["results"]] == [1, 2]
    assert out["truncated"] is True
    assert out["total"] == 5


@pytest.mark.parametrize("parallel", [False, True])
async def test_max_items_not_exceeded_reports_untruncated(harness: Harness, parallel: bool) -> None:
    out = await _loop_output(harness, harness.definition([1, 2], parallel=parallel, max_items=4))

    assert sorted(harness.ran) == [1, 2]
    assert out["truncated"] is False
    assert out["total"] == 2


@pytest.mark.parametrize("parallel", [False, True])
async def test_template_max_items_zero_never_calls_the_body(
    harness: Harness, parallel: bool
) -> None:
    out = await _loop_output(
        harness,
        harness.definition([1, 2, 3], parallel=parallel, max_items="{params.n}"),
        {"n": 0},
    )

    assert harness.ran == []
    assert out["results"] == []
    assert out["truncated"] is True
    assert out["total"] == 3


@pytest.mark.parametrize("value", ["4", 4, "{params.n}", "{params.s}", " 4 "])
async def test_max_items_string_and_integer_forms_mean_the_same(
    harness: Harness, value: Any
) -> None:
    await harness.run(harness.definition(list(range(6)), max_items=value), {"n": 4, "s": "4"})

    assert harness.ran == [0, 1, 2, 3]


@pytest.mark.parametrize(("count", "expected"), [(4.0, 4), (0.0, 0), ("4.0", 4), ("2.00", 2)])
@pytest.mark.parametrize("parallel", [False, True])
async def test_integral_float_params_count_as_integers(
    harness: Harness, count: Any, expected: int, parallel: bool
) -> None:
    """params JSON Schema `integer`는 수 4.0을 받는다 — 렌더 결과 `"4.0"`도 4로 읽는다(#346)."""
    out = await _loop_output(
        harness,
        harness.definition(list(range(6)), parallel=parallel, max_items="{params.image_count}"),
        {"image_count": count},
    )

    assert sorted(harness.ran) == list(range(expected))
    assert (out["truncated"], out["total"]) == (True, 6)


@pytest.mark.parametrize(
    "value",
    [
        -1,
        True,
        False,
        1.5,
        2.0,
        "abc",
        "-1",
        "1.5",
        "",
        "+2",
        "{params.missing}",
        "{params.flag}",
        "{params.neg}",
        "{params.frac}",
        "{params.neg_float}",
        "{params.word}",
        "{params.blank}",
        "4.",
        ".0",
        [2],
        {"n": 2},
    ],
)
@pytest.mark.parametrize("parallel", [False, True])
async def test_invalid_max_items_fails_before_the_body_runs(
    harness: Harness, value: Any, parallel: bool
) -> None:
    definition = harness.definition([1, 2, 3], parallel=parallel, max_items=value)

    with pytest.raises(AgentRuntimeError, match="max_items"):
        await harness.run(
            definition,
            {"flag": True, "neg": -1, "frac": 1.5, "neg_float": -1.0, "word": "abc", "blank": ""},
        )

    assert harness.ran == []


async def test_explicit_null_max_items_is_the_same_as_omitted(harness: Harness) -> None:
    out = await _loop_output(harness, harness.definition([1, 2, 3], max_items=None))

    assert harness.ran == [1, 2, 3]
    assert set(out) == {"results"}


async def test_max_items_bounds_the_items_read_from_a_template_path(harness: Harness) -> None:
    """제품 흐름 — `items: "{validate.json.images}"`, `max_items: "{params.image_count}"`."""
    from biznotes_core.services.agents.nodes import retrieval

    images = [{"slide": i, "prompt": f"p{i}"} for i in range(1, 6)]
    definition = harness.definition("{validate.json.images}", max_items="{params.image_count}")
    node = definition["nodes"][0]
    state = {"params": {"image_count": 3}, "validate": {"json": {"images": images}}}

    out = await retrieval._exec_loop(harness.ctx({"image_count": 3}), node, state)

    assert harness.ran == images[:3]
    assert (out["truncated"], out["total"]) == (True, 5)
