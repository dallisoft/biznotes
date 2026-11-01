"""fan-out 그래프의 노드마다 세션·컨텍스트 사본을 준다 (#28 Phase 2).

계획 `docs/plans/agent-branch-sessions/plan.md`의 수락 기준 9(fan-out 부분)·12~16과 D9를 DB 없이
실제 `execute_agent`(LangGraph)로 확인한다. 실행 로그 기록은 가로채 노드별 토큰 델타를 본다. 병렬성
회복(수락 기준 11)은 계획 문서의 PG 스모크에 있다.
"""

import asyncio
import uuid
from typing import Any

import pytest

from biznotes_core.services.agents import graph
from biznotes_core.services.agents.context import BRANCH_BUDGET, RunContext


class Sessions:
    def __init__(self) -> None:
        self.opened: list[FakeSession] = []
        self.open_now = 0
        self.max_open = 0

    def __call__(self) -> "FakeSession":
        session = FakeSession(self)
        self.opened.append(session)
        self.open_now += 1
        self.max_open = max(self.max_open, self.open_now)
        return session


class FakeSession:
    def __init__(self, owner: Sessions | None) -> None:
        self.owner = owner
        self.closed = False
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def close(self) -> None:
        self.closed = True
        if self.owner is not None:
            self.owner.open_now -= 1


def _ctx(sessions: Sessions, **kw: Any) -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=FakeSession(None),  # type: ignore[arg-type]
        params={},
        session_factory=sessions,  # type: ignore[arg-type]
        **kw,
    )


def _node(nid: str, **kw: Any) -> dict[str, Any]:
    return {"id": nid, "type": "probe", **kw}


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """probe 노드 — node의 `tokens`만큼 토큰을 더하고 `sleep`만큼 쉰다. `fail`이면 예외.
    실행 로그는 가로채 노드별 token_usage를 모은다."""
    out: dict[str, Any] = {"calls": {}, "usage": {}, "cost": {}, "fails": {}, "running": 0,
                           "max_running": 0, "finished": {}}

    async def log_start(run_id: Any, node_id: str, *a: Any, **k: Any) -> str:
        return node_id

    async def log_finish(log_id: str, **kw: Any) -> None:
        out["usage"][log_id] = (kw.get("token_usage") or {}).get("total_tokens", 0)
        out["cost"][log_id] = round(kw.get("cost_usd") or 0.0, 6)
        out["finished"][log_id] = kw.get("status")

    async def probe(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
        out["calls"][node["id"]] = {
            "db": ctx.db,
            "parallel": ctx.in_parallel_branch,
            "ctx": ctx,
        }
        out["running"] += 1
        out["max_running"] = max(out["max_running"], out["running"])
        try:
            await asyncio.sleep(node.get("sleep", 0.0))
            if node.get("fail") and out["fails"].get(node["id"], 0) < node["fail"]:
                out["fails"][node["id"]] = out["fails"].get(node["id"], 0) + 1
                raise RuntimeError(f"{node['id']} 실패")
            # 형제와 번갈아 가며 토큰·비용을 더한다 — 공유 셀이면 서로의 델타에 섞인다
            for _ in range(2):
                ctx.add_tokens({"total_tokens": node.get("tokens", 0) // 2})
                ctx.add_cost(node.get("cost", 0.0) / 2)
                await asyncio.sleep(0.005)
            return {"text": node["id"]}
        finally:
            out["running"] -= 1

    monkeypatch.setattr(graph, "_log_node_start", log_start)
    monkeypatch.setattr(graph, "_log_node_finish", log_finish)
    monkeypatch.setitem(graph.NODE_EXECUTORS, "probe", probe)
    return out


FANOUT = {
    "nodes": [
        _node("a"),
        _node("b", tokens=10, cost=0.4, sleep=0.01),
        _node("c", tokens=6, cost=0.2),
    ],
    "edges": [["a", "b"], ["a", "c"]],
}


async def test_fanout_siblings_get_their_own_sessions(run: dict[str, Any]) -> None:
    """수락 기준 9·12의 전제 — fan-out 그래프의 노드는 각자 분기 세션, in_parallel_branch는
    그대로다."""
    sessions = Sessions()
    ctx = _ctx(sessions)

    await graph.execute_agent(ctx, "workflow", FANOUT)

    dbs = {nid: c["db"] for nid, c in run["calls"].items()}
    assert len({id(d) for d in dbs.values()}) == 3 and ctx.db not in dbs.values()
    assert not any(c["parallel"] for c in run["calls"].values())  # D5 — react 거부 대상이 아니다
    assert all(s.closed for s in sessions.opened)


async def test_sequential_graph_keeps_the_run_session(run: dict[str, Any]) -> None:
    """비목표 — fan-out이 없는 그래프는 지금처럼 run 세션 하나를 쓴다."""
    sessions = Sessions()
    ctx = _ctx(sessions)

    await graph.execute_agent(
        ctx, "workflow", {"nodes": [_node("a"), _node("b")], "edges": [["a", "b"]]}
    )

    assert all(c["db"] is ctx.db for c in run["calls"].values())
    assert sessions.opened == []


async def test_per_node_token_deltas_do_not_mix(run: dict[str, Any]) -> None:
    """수락 기준 12 — 형제가 번갈아 토큰·비용을 더해도 노드별 델타는 자기 것만."""
    await graph.execute_agent(_ctx(Sessions()), "workflow", FANOUT)

    assert run["usage"]["b"] == 10 and run["usage"]["c"] == 6
    assert run["cost"]["b"] == 0.4 and run["cost"]["c"] == 0.2


async def test_resume_baseline_goes_only_to_the_gated_node(run: dict[str, Any]) -> None:
    """수락 기준 13 — 재개 기준선은 `_resume` 노드만 받는다. 이름이 앞서는 미완료 형제(beta)가
    먼저 시작해도 가져가지 않는다. `_resume`은 워커가 소비한 승인의 앵커가 가리키는 노드에서만
    인정한다(#91)."""
    ctx = _ctx(Sessions())
    ctx.resume_checkpoint = {"react_node_id": "zeta"}
    ctx.resume_node_token_base = {"total_tokens": -5}  # 승인 전 소비 5토큰(T1)
    ctx.resume_node_cost_base = -0.3  # 승인 전 비용
    definition = {
        "nodes": [
            _node("a"),
            _node("beta", tokens=2, cost=0.2),
            _node("zeta", tokens=4, cost=0.4, _resume={}),
        ],
        "edges": [["a", "beta"], ["a", "zeta"]],
    }

    await graph.execute_agent(ctx, "workflow", definition, resume_state={"a": {"text": "a"}})

    assert run["usage"]["beta"] == 2 and run["cost"]["beta"] == 0.2
    assert run["usage"]["zeta"] == 4 + 5  # 승인 전 소비가 게이트 노드에 계상된다
    assert run["cost"]["zeta"] == 0.7
    assert ctx.resume_node_token_base is None and ctx.resume_node_cost_base is None  # 1회 소비


@pytest.mark.parametrize(
    ("edges", "expected"),
    [
        ([["a", "b"], ["b", "c"]], False),
        ([["a", "b"], ["a", "b"]], False),  # 같은 엣지 — LangGraph가 합친다
        ([["a", "b"], ["a", "c"]], True),
        ([["cond", "b"], ["cond", "c"]], False),  # 조건 노드의 일반 엣지는 그래프에 들어가지 않는다
        ([["a", "b"], ["a", "ghost"]], False),  # 미지 노드
    ],
)
def test_fanout_detection_uses_the_graph_filters(edges: list[list[str]], expected: bool) -> None:
    """수락 기준 14 — `add_edge`와 같은 필터로 센다."""
    node_map = {
        "a": {"id": "a", "type": "probe"},
        "b": {"id": "b", "type": "probe"},
        "c": {"id": "c", "type": "probe"},
        "cond": {"id": "cond", "type": "condition"},
    }

    assert graph._has_fan_out(node_map, edges) is expected


async def test_langgraph_sibling_cancellation_leaks_no_permits(run: dict[str, Any]) -> None:
    """수락 기준 15 — 형제 하나가 실패하면 LangGraph가 다른 형제를 (두 번) 취소한다. 예산과 세션이
    모두 돌아온다."""
    sessions = Sessions()
    ctx = _ctx(sessions)
    definition = {
        "nodes": [_node("a"), _node("b", fail=1), _node("c", sleep=1.0)],
        "edges": [["a", "b"], ["a", "c"]],
    }

    with pytest.raises(graph.AgentRuntimeError):
        await graph.execute_agent(ctx, "workflow", definition)
    await asyncio.sleep(0.05)  # 취소로 백그라운드로 넘어간 닫기가 끝날 시간

    assert ctx.branch_state.budget._value == BRANCH_BUDGET  # noqa: SLF001
    assert all(s.closed for s in sessions.opened)


async def test_top_level_fanout_wider_than_budget_isolates_every_node(run: dict[str, Any]) -> None:
    """수락 기준 16 — 최상위 fan-out은 폭이 예산보다 커도 강등하지 않는다(모든 노드가 분기 세션)."""
    sessions = Sessions()
    ctx = _ctx(sessions)
    kids = [f"k{i}" for i in range(BRANCH_BUDGET + 2)]
    definition = {
        "nodes": [_node("a")] + [_node(k, sleep=0.02) for k in kids],
        "edges": [["a", k] for k in kids],
    }

    await asyncio.wait_for(graph.execute_agent(ctx, "workflow", definition), 2)

    assert all(run["calls"][k]["db"] is not ctx.db for k in kids)
    assert sessions.max_open <= BRANCH_BUDGET


@pytest.mark.parametrize("fanout", [True, False])
async def test_retry_rolls_back_only_an_owned_branch_session(
    run: dict[str, Any], fanout: bool
) -> None:
    """D9 — 재시도 전에 노드 소유 분기 세션만 되돌린다. run 세션(순차 그래프)은 건드리지 않는다."""
    ctx = _ctx(Sessions())
    flaky = _node("b", fail=1, retry_policy={"max_retries": 1, "delay": 0})
    if fanout:
        definition = {
            "nodes": [_node("a"), flaky, _node("c")],
            "edges": [["a", "b"], ["a", "c"]],
        }
    else:
        definition = {"nodes": [_node("a"), flaky], "edges": [["a", "b"]]}

    await graph.execute_agent(ctx, "workflow", definition)

    b_db = run["calls"]["b"]["db"]
    if fanout:
        assert b_db is not ctx.db and b_db.rollbacks == 1
    else:
        assert b_db is ctx.db and ctx.db.rollbacks == 0


# ── 강등 경로 (phase 2 리뷰 F1) ──────────────────────────────────────────


async def test_degraded_nodes_share_the_caller_session_one_at_a_time(run: dict[str, Any]) -> None:
    """분기 안(permit 보유)에서 예산이 떨어지면 노드는 호출자 세션을 쓰되, 그래프 잠금으로 한 번에
    하나씩만 돈다 — 호출자 세션을 동시에 쓰지 않는다."""
    from dataclasses import replace

    sessions = Sessions()
    ctx = replace(_ctx(sessions), holds_branch_permit=True)
    for _ in range(BRANCH_BUDGET):
        await ctx.branch_state.budget.acquire()  # 형제 분기들이 예산을 모두 쥐고 있다
    kids = ["k0", "k1", "k2"]
    definition = {
        "nodes": [_node("a")] + [_node(k, sleep=0.02, tokens=4) for k in kids],
        "edges": [["a", k] for k in kids],
    }

    await asyncio.wait_for(graph.execute_agent(ctx, "workflow", definition), 2)

    assert all(run["calls"][k]["db"] is ctx.db for k in kids)
    assert run["max_running"] == 1
    assert all(run["usage"][k] == 4 for k in kids)  # 강등 노드도 per-node 사본을 받는다
    assert sessions.opened == []


async def test_unisolatable_fanout_runs_nodes_one_at_a_time(run: dict[str, Any]) -> None:
    """db=None(격리 불가)이면 fan-out 형제를 차례로 돌린다."""
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={})  # type: ignore[arg-type]

    await graph.execute_agent(ctx, "workflow", FANOUT)

    assert run["max_running"] == 1
    assert run["usage"]["b"] == 10 and run["usage"]["c"] == 6


async def test_resume_noop_progress_waits_for_degraded_sibling(run: dict[str, Any]) -> None:
    """리뷰 F6 — 모든 노드가 강등된 그래프를 재개할 때, 완료 노드(no-op)의 진행 콜백이 run 세션을
    쓰는 강등 형제와 겹치지 않는다."""
    overlaps: list[bool] = []

    async def on_progress(node_id: str, done: int, total: int) -> None:
        overlaps.append(run["running"] > 0)

    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={}, on_progress=on_progress
    )  # type: ignore[arg-type]
    definition = {
        "nodes": [_node("a"), _node("c", sleep=0.05), _node("z")],  # z는 c보다 늦게 시작한다
        "edges": [["a", "c"], ["a", "z"]],
    }

    await graph.execute_agent(
        ctx, "workflow", definition, resume_state={"a": {"text": "a"}, "z": {"text": "z"}}
    )

    assert overlaps and not any(overlaps)


async def test_rollback_failure_stops_retrying_and_finishes_the_node(
    run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """리뷰 F2 — 재시도 전 롤백이 실패하면 재시도를 멈추고 원래 오류로 노드를 실패 처리한다
    (로그 행을 닫고 continue_on_error를 따른다)."""

    async def broken_rollback(self: FakeSession) -> None:
        raise ConnectionError("연결 끊김")

    monkeypatch.setattr(FakeSession, "rollback", broken_rollback)
    flaky = _node("b", fail=5, retry_policy={"max_retries": 3, "delay": 0}, continue_on_error=True)
    definition = {"nodes": [_node("a"), flaky, _node("c")], "edges": [["a", "b"], ["a", "c"]]}

    await graph.execute_agent(_ctx(Sessions()), "workflow", definition)

    assert run["fails"]["b"] == 1  # 재시도하지 않았다
    assert run["finished"]["b"] == "failed" and run["finished"]["c"] == "succeeded"


async def test_baseline_is_not_taken_by_a_node_that_only_claims_resume(
    run: dict[str, Any],
) -> None:
    """#91 — 정의에 `_resume`만 둔 형제(beta)는 앵커가 가리키지 않아 기준선을 가로채지 못한다."""
    ctx = _ctx(Sessions())
    ctx.resume_checkpoint = {"react_node_id": "zeta"}
    ctx.resume_node_token_base = {"total_tokens": -5}
    ctx.resume_node_cost_base = -0.3
    definition = {
        "nodes": [
            _node("a"),
            _node("beta", tokens=2, cost=0.2, _resume={}),  # 위조: 이름이 앞서 먼저 시작한다
            _node("zeta", tokens=4, cost=0.4, _resume={}),
        ],
        "edges": [["a", "beta"], ["a", "zeta"]],
    }

    await graph.execute_agent(ctx, "workflow", definition, resume_state={"a": {"text": "a"}})

    assert run["usage"]["beta"] == 2 and run["cost"]["beta"] == 0.2
    assert run["usage"]["zeta"] == 4 + 5 and run["cost"]["zeta"] == 0.7


async def test_missing_resume_hint_is_reported(run: dict[str, Any]) -> None:
    """리뷰 F5 — fan-out 그래프에서 `_resume` 노드가 없어 재개 기준선이 남으면 경고한다."""
    import structlog.testing

    ctx = _ctx(Sessions())
    ctx.resume_node_token_base = {"total_tokens": -5}
    with structlog.testing.capture_logs() as logs:
        await graph.execute_agent(ctx, "workflow", FANOUT, resume_state={"a": {"text": "a"}})

    assert any("_resume" in e["event"] for e in logs if e["log_level"] == "warning")

