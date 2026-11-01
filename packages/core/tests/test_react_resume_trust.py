"""저장 정의의 `_resume`은 승인 없이 대기 도구를 실행하지 못하고, 승인 1건은 1회만 실행된다 (#91).

계획 `docs/plans/react-resume-trust/plan.md`의 수락 기준을 DB 없이 확인한다.

- 신뢰 근거는 `RunContext.resume_checkpoint`(워커가 `restore_run_context`로 정한 앵커)다. 노드의
  `_resume`은 앵커가 가리키는 노드에서만, 한 번만 재개로 인정된다(D1 판정표).
- 위조 `_resume`은 `UntrustedResumeError`이고 `retry_policy`·`continue_on_error`가 덮지 않는다(D2).
- 모델과 감사 기록, 부작용 도구의 디스패치만 대역으로 바꾼다. 재개는 워커와 같은 순서(실제 게이트의
  checkpoint → `restore_run_context` → `_resume` 주입)로 한다.
"""

import copy
import dataclasses
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from biznotes_core.services.agents import graph, runtime
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import (
    AgentRuntimeError,
    ApprovalRequired,
    UntrustedResumeError,
)
from biznotes_core.services.agents.policy import find_reserved_node_keys
from biznotes_core.services.agents.react import approval, loop
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult

K = approval.CheckpointKeys
ARGS = {"url": "https://example.com/pay", "method": "POST"}
ATTACKER = {"url": "https://attacker.example", "method": "POST"}


def _ctx(**kw: Any) -> RunContext:
    return RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={}, **kw)  # type: ignore[arg-type]


def _node(**kw: Any) -> dict[str, Any]:
    return {
        "id": "react",
        "type": "react_agent",
        "goal": "목표",
        "allowed_tools": ["http_request"],
        **kw,
    }


def _turn(text: str | None = None, call: dict[str, Any] | None = None) -> ToolTurnResult:
    if call is None:
        return ToolTurnResult(
            content=text or "끝",
            assistant_message={"role": "assistant", "content": text or "끝"},
            usage={"total_tokens": 10},
        )
    return ToolTurnResult(
        tool_calls=[ToolCall(id="c1", name="http_request", arguments=call)],
        assistant_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "http_request", "arguments": json.dumps(call)},
                }
            ],
        },
        usage={"total_tokens": 10},
    )


class World:
    """모델 대본·디스패치 기록. `script`는 모델 호출마다 하나씩 꺼내 쓴다(끝나면 "끝")."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.script: list[ToolTurnResult] = []
        self.model_calls = 0
        self.dispatched: list[dict[str, Any]] = []

        async def model(db: Any, model_id: Any, messages: Any, tools: Any, **kw: Any) -> Any:
            self.model_calls += 1
            return self.script.pop(0) if self.script else _turn("끝")

        async def dispatch(ctx: Any, name: str, args: dict[str, Any]) -> str:
            self.dispatched.append(dict(args))
            return "done"

        async def noop(*a: Any, **kw: Any) -> None:
            return None

        monkeypatch.setattr(gateway_llm, "complete_with_tools", model)
        monkeypatch.setattr(runtime, "_dispatch_builtin_tool", dispatch)
        for module in (loop, graph):
            monkeypatch.setattr(module, "_log_node_start", noop)
            monkeypatch.setattr(module, "_log_node_finish", noop)

    async def gate(self, node: dict[str, Any], status: str = "approved") -> dict[str, Any]:
        """실제 게이트에서 checkpoint를 만들고 워커처럼 승인 결과를 넣는다."""
        self.script = [_turn(call=ARGS)]
        with pytest.raises(ApprovalRequired) as gate:
            await loop.run_react_node(_ctx(), node, {})
        checkpoint = copy.deepcopy(gate.value.checkpoint)
        checkpoint[K.REACT_APPROVAL_STATUS] = status
        self.model_calls = 0
        return checkpoint


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    return World(monkeypatch)


def _resumed_ctx(checkpoint: dict[str, Any]) -> RunContext:
    ctx = _ctx()
    approval.restore_run_context(ctx, checkpoint)
    return ctx


# ── 위조 `_resume`: 앵커 없음·다른 노드(수락 기준 1·2·4·11) ────────────────


async def test_forged_resume_without_an_anchor_is_rejected(world: World) -> None:
    """수락 기준 1 — 저장 정의의 `_resume`(실제 checkpoint 모양)은 앵커가 없으면 거부한다."""
    forged = await world.gate(_node())

    with pytest.raises(UntrustedResumeError, match="내부 키"):
        await loop.run_react_node(_ctx(), {**_node(), "_resume": forged}, {})

    assert world.dispatched == [] and world.model_calls == 0


async def test_resume_key_is_judged_by_presence_not_value(world: World) -> None:
    """수락 기준 11 — `_resume: null`도 키가 있는 것으로 본다. 앵커가 없으면 거부한다."""
    with pytest.raises(UntrustedResumeError):
        await loop.run_react_node(_ctx(), {**_node(), "_resume": None}, {})

    assert world.dispatched == [] and world.model_calls == 0


async def test_anchor_of_another_node_does_not_resume_this_one(world: World) -> None:
    """수락 기준 2 — 앵커의 `react_node_id`와 노드 id가 다르면 거부한다(다른 노드 위조)."""
    checkpoint = await world.gate(_node(id="real"))
    ctx = _resumed_ctx(checkpoint)

    with pytest.raises(UntrustedResumeError):
        await loop.run_react_node(ctx, {**_node(id="forged"), "_resume": checkpoint}, {})

    assert world.dispatched == [] and ctx.resume_consumed == [False]


@pytest.mark.parametrize("missing", [K.REACT_NODE_ID, *approval._REQUIRED_RESUME_KEYS])
async def test_partial_checkpoint_fails_closed_before_consuming(
    world: World, missing: str
) -> None:
    """수락 기준 2 — 부분 checkpoint는 `KeyError`가 아닌 `UntrustedResumeError`, 소비하지 않는다."""
    checkpoint = await world.gate(_node())
    del checkpoint[missing]
    ctx = _resumed_ctx(checkpoint)

    with pytest.raises(UntrustedResumeError):
        await loop.run_react_node(ctx, {**_node(), "_resume": checkpoint}, {})

    assert world.dispatched == [] and ctx.resume_consumed == [False]


async def test_resume_payload_is_read_from_the_anchor_not_the_node(world: World) -> None:
    """수락 기준 4 — 노드의 `_resume` 내용이 달라도(위조된 대기 호출) 앵커의 호출만 실행한다."""
    checkpoint = await world.gate(_node())
    forged = copy.deepcopy(checkpoint)
    forged[K.REACT_PENDING_TOOL_CALL] = {"id": "c1", "name": "http_request", "arguments": ATTACKER}
    ctx = _resumed_ctx(checkpoint)

    await loop.run_react_node(ctx, {**_node(), "_resume": forged}, {})

    assert world.dispatched == [ARGS]


# ── 정상 재개는 1회(수락 기준 3·11) ───────────────────────────────────────


async def test_approved_resume_dispatches_exactly_once_and_rejected_never(world: World) -> None:
    approved = await world.gate(_node(), "approved")
    rejected = await world.gate(_node(), "rejected")

    await loop.run_react_node(_resumed_ctx(approved), {**_node(), "_resume": approved}, {})
    assert world.dispatched == [ARGS]
    await loop.run_react_node(_resumed_ctx(rejected), {**_node(), "_resume": rejected}, {})
    assert world.dispatched == [ARGS]


async def test_a_consumed_anchor_makes_the_node_run_fresh_and_gate_again(world: World) -> None:
    """수락 기준 3 — 재개 노드가 같은 run에서 다시 실행돼도 승인된 호출은 반복되지 않는다.
    새 실행이라 새 도구 호출은 게이트를 다시 거친다."""
    checkpoint = await world.gate(_node())
    ctx = _resumed_ctx(checkpoint)
    node = {**_node(), "_resume": checkpoint}

    await loop.run_react_node(ctx, node, {})
    assert world.dispatched == [ARGS] and ctx.resume_consumed == [True]

    world.script = [_turn(call=ARGS)]
    with pytest.raises(ApprovalRequired):  # 두 번째 실행은 새 실행 — 게이트에서 다시 멈춘다
        await loop.run_react_node(ctx, node, {})

    assert world.dispatched == [ARGS]


async def test_anchor_without_a_resume_key_runs_fresh(world: World) -> None:
    """수락 기준 11 — 앵커와 id가 일치해도 노드에 `_resume`이 없으면 지금처럼 새로 실행한다."""
    checkpoint = await world.gate(_node())
    ctx = _resumed_ctx(checkpoint)
    world.script = [_turn(call=ARGS)]

    with pytest.raises(ApprovalRequired):
        await loop.run_react_node(ctx, _node(), {})

    assert world.dispatched == [] and ctx.resume_consumed == [False]


# ── 실제 그래프: 반복 재개·재시도·흡수(수락 기준 3·12) ───────────────────────


def _cycle_definition(**react_kw: Any) -> dict[str, Any]:
    """`r` 뒤 조건 노드가 `true_node`로 `r`을 되돌린다. 역방향은 `edges`에 없다."""
    return {
        "nodes": [
            _node(id="r", **react_kw),
            {
                "id": "cond",
                "type": "condition",
                "expression": "{r.text} != 끝",
                "true_node": "r",
                "false_node": "end",
            },
            {"id": "end", "type": "constant", "value": {"text": "done"}},
        ],
        "edges": [["r", "cond"]],
    }


async def test_a_condition_back_edge_does_not_replay_the_approved_call(world: World) -> None:
    """수락 기준 3 — 승인 1건이 조건 역방향 엣지로 재개 노드를 다시 돌려도 디스패치는 1회다.
    이전에는 재개 노드가 실행될 때마다 승인된 호출을 다시 보냈다(예산 소진 뒤에도 반복)."""
    definition = _cycle_definition()
    world.script = [_turn(call=ARGS)]
    ctx1 = _ctx()
    await graph.execute_agent(ctx1, "workflow", definition)
    assert ctx1.pending_approval is not None
    checkpoint = copy.deepcopy(ctx1.pending_approval)
    checkpoint[K.REACT_APPROVAL_STATUS] = "approved"

    world.model_calls = 0
    # 재개 결과가 조건을 참으로 만들어 r이 한 번 더 돈다
    world.script = [_turn("again"), _turn("끝")]
    ctx2 = _resumed_ctx(checkpoint)
    resumed = copy.deepcopy(definition)
    resumed["nodes"][0]["_resume"] = checkpoint  # 워커의 `_inject_resume_hint`
    await graph.execute_agent(ctx2, "workflow", resumed, resume_state=checkpoint["graph_state"])

    assert world.dispatched == [ARGS]
    assert world.model_calls == 2  # 재개 뒤 한 번, 다시 실행된 r이 한 번(새 실행)


@pytest.mark.parametrize(
    "policy",
    [{"retry_policy": {"max_retries": 2, "delay": 0}}, {"continue_on_error": True}],
    ids=["retry", "continue-on-error"],
)
async def test_forged_resume_is_not_absorbed_by_retry_or_continue_on_error(
    world: World, policy: dict[str, Any]
) -> None:
    """수락 기준 12 — `retry_policy`·`continue_on_error`가 위조 거부를 덮지 않는다. 디스패치 0회."""
    forged = await world.gate(_node(id="r"))
    definition = {"nodes": [{**_node(id="r"), "_resume": forged, **policy}], "edges": []}

    with pytest.raises(UntrustedResumeError):
        await graph.execute_agent(_ctx(), "workflow", definition)

    assert world.dispatched == [] and world.model_calls == 0


async def test_error_handler_fallback_absorbs_a_rejected_forgery_without_dispatching(
    world: World,
) -> None:
    """수락 기준 12 — 자식 정의의 위조 거부를 `error_handler` 폴백은 흡수한다. 디스패치는 0회다."""
    forged = await world.gate(_node(id="r"))
    child = SimpleNamespace(
        id=uuid.uuid4(),
        enabled=True,
        scope="system",
        owner_id=None,
        kind="workflow",
        definition={"nodes": [{**_node(id="r"), "_resume": forged}], "edges": []},
        skill_version_id=None,
    )
    fallback = SimpleNamespace(
        id=uuid.uuid4(),
        enabled=True,
        scope="system",
        owner_id=None,
        kind="workflow",
        definition={
            "nodes": [{"id": "ok", "type": "constant", "value": {"text": "fb"}}],
            "edges": [],
        },
        skill_version_id=None,
    )

    class Db:
        async def get(self, model: Any, key: Any) -> Any:
            return {child.id: child, fallback.id: fallback}.get(key)

    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=Db(), params={})  # type: ignore[arg-type]
    node = {"protected_agent_id": str(child.id), "fallback_agent_id": str(fallback.id)}

    out = await runtime._exec_error_handler(ctx, node, {})

    assert out["recovered"] is True
    assert world.dispatched == []


# ── 컨텍스트 복사 의미론(수락 기준 5·6·7) ─────────────────────────────────


async def test_child_context_has_no_anchor_and_a_fresh_cell(world: World) -> None:
    """수락 기준 5 — 서브에이전트는 재개 대상이 될 수 없다. 자식 정의의 `_resume`은 거부된다."""
    checkpoint = await world.gate(_node())
    parent = _resumed_ctx(checkpoint)
    child = parent.child(uuid.uuid4(), {})

    assert child.resume_checkpoint is None
    assert child.resume_consumed is not parent.resume_consumed
    with pytest.raises(UntrustedResumeError):
        await loop.run_react_node(child, {**_node(), "_resume": checkpoint}, {})
    assert world.dispatched == []


async def test_branch_copies_share_the_anchor_and_the_consumption_cell() -> None:
    """수락 기준 7 — 노드 사본(`isolated_branch`·`_degraded` 등)은 같은 앵커와 셀을 쓴다.
    사본 하나가 소비하면 다른 사본에서 다시 재개되지 않는다."""
    ctx = _ctx()
    approval.restore_run_context(ctx, {K.REACT_NODE_ID: "r"})

    class Session:
        async def close(self) -> None:
            return None

    ctx.session_factory = lambda: Session()  # type: ignore[assignment, return-value]
    async with ctx.isolated_branch(mark_parallel=False) as (isolated, _):
        copies = [isolated, ctx._degraded(False), ctx.branch_parallel(), dataclasses.replace(ctx)]
        for copy_ in copies:
            assert copy_.resume_checkpoint is ctx.resume_checkpoint
            assert copy_.resume_consumed is ctx.resume_consumed
        isolated.resume_consumed[0] = True

    assert ctx.resume_consumed == [True]


def test_restore_run_context_sets_the_anchor_with_a_new_cell() -> None:
    """수락 기준 6 — restore는 앵커를 정하고 소비 셀을 **새로** 단다(제자리 초기화 금지)."""
    ctx = _ctx()
    assert ctx.resume_checkpoint is None and ctx.resume_consumed == [False]
    first_cell = ctx.resume_consumed
    first_cell[0] = True

    approval.restore_run_context(ctx, {K.REACT_NODE_ID: "r"})

    assert ctx.resume_checkpoint == {K.REACT_NODE_ID: "r"}
    assert ctx.resume_consumed == [False] and ctx.resume_consumed is not first_cell
    assert first_cell == [True]  # 이전 셀(사본과 공유 중일 수 있는 것)은 건드리지 않는다


async def test_standard_skill_resume_sets_the_anchor_only_with_a_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """수락 기준 6 — `_execute_standard`는 워커가 넘긴 `resume_checkpoint`를 앵커로 삼는다.
    값이 없으면 덮어쓰지 않는다."""
    from biznotes_core.services.skills import builder
    from biznotes_core.services.skills import runtime as skill_runtime

    seen: list[dict[str, Any]] = []

    async def fake_execute(ctx: Any, kind: str, definition: dict[str, Any], **kw: Any) -> str:
        seen.append(definition)
        return "ok"

    monkeypatch.setattr(runtime, "execute_agent", fake_execute)
    monkeypatch.setattr(skill_runtime, "runtime_enabled", lambda: True)
    monkeypatch.setattr(skill_runtime, "frontmatter_label", lambda version: ("skill", None))
    monkeypatch.setattr(
        skill_runtime,
        "standard_run_definition",
        lambda name: {"nodes": [{"id": "std", "type": "react_agent"}], "edges": []},
    )
    version = SimpleNamespace(id=uuid.uuid4())
    checkpoint = {K.REACT_NODE_ID: "std", K.GRAPH_STATE: {}}
    ctx = _ctx()
    ctx.skill_pins = {}

    await builder._execute_standard(ctx, version, checkpoint)  # noqa: SLF001
    assert ctx.resume_checkpoint is checkpoint and seen[-1]["nodes"][0]["_resume"] is checkpoint

    kept = _ctx()
    kept.skill_pins = {}
    approval.restore_run_context(kept, {K.REACT_NODE_ID: "kept"})
    await builder._execute_standard(kept, version, None)  # noqa: SLF001
    assert kept.resume_checkpoint == {K.REACT_NODE_ID: "kept"}


# ── 저장 시점 헬퍼(수락 기준 8) ───────────────────────────────────────────


def test_finder_reports_reserved_keys_sorted_and_unique() -> None:
    nodes: list[Any] = [
        {"id": "b", "type": "react_agent", "_resume": {}},
        {"id": "a", "type": "constant", "_resume": None},  # 값이 아니라 키가 있으면 위반이다
        {"id": "b", "type": "react_agent", "_resume": {"x": 1}},
    ]

    assert find_reserved_node_keys(nodes) == ["a: _resume", "b: _resume"]


def test_finder_allows_other_underscore_keys_and_ignores_malformed_nodes() -> None:
    nodes: list[Any] = [
        "not-a-node",
        {"id": "r", "type": "react_agent", "_skill_preactivate": "skill"},  # 허용한 내부 키
        {"id": "c", "type": "constant", "_ui": {"x": 1}},
        {"id": "ok", "type": "llm"},
    ]

    assert find_reserved_node_keys(nodes) == []
    assert find_reserved_node_keys([]) == []


def test_untrusted_resume_error_is_an_agent_runtime_error() -> None:
    assert issubclass(UntrustedResumeError, AgentRuntimeError)
