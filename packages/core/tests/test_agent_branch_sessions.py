"""동시 실행 단위마다 DB 세션을 따로 준다 (#28 Phase 1 — supervisor·loop).

계획 `docs/plans/agent-branch-sessions/plan.md`의 수락 기준 3~10(과 16의 Phase 1 선행분)을 DB 없이
확인한다. 수락 기준 17(제품 특성화 스위트)은 계획 문서의 검증 기록에 있다. 가짜
`session_factory`로 세션 할당·닫힘·동시 개수를 세고, 중첩 교착은 스텁이 아니라 실제
`execute_agent → supervisor → _run_subagent → child() → loop` 경로로 돌린다. 병렬성 회복(수락
기준 1·2)은 PostgreSQL이 필요해 계획 문서의 검증 기록에 남긴다.
"""

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import structlog.testing

from biznotes_core.services.agents import graph
from biznotes_core.services.agents.context import BRANCH_BUDGET, RunContext, run_parallel


class Sessions:
    """가짜 세션 공장 — 연 세션·닫은 세션·동시에 열린 최대 수를 센다."""

    def __init__(self, agents: dict[uuid.UUID, Any] | None = None) -> None:
        self.agents = agents or {}
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
    def __init__(self, owner: Sessions) -> None:
        self.owner = owner
        self.closed = False

    async def get(self, model: Any, key: Any) -> Any:
        return self.owner.agents.get(key)

    async def close(self) -> None:
        self.closed = True
        self.owner.open_now -= 1


def _ctx(sessions: Sessions, **kw: Any) -> RunContext:
    root = FakeSession(sessions)  # 제품이 넘긴 run 세션 — 공장 밖에서 만든다
    return RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=root,  # type: ignore[arg-type]
        params={},
        session_factory=sessions,  # type: ignore[arg-type]
        **kw,
    )


# ── isolated_branch · run_parallel ──────────────────────────────────────


async def test_each_parallel_unit_gets_its_own_session_and_it_is_closed() -> None:
    """수락 기준 3 — 동시 실행 단위마다 서로 다른 세션, 끝나면 모두 닫힌다."""
    sessions = Sessions()
    ctx = _ctx(sessions)
    seen: list[Any] = []

    async def work(branch: RunContext, item: int) -> int:
        seen.append((branch.db, branch.in_parallel_branch, branch.holds_branch_permit))
        await asyncio.sleep(0.01)
        return item * 10

    out = await run_parallel(ctx, [1, 2, 3], work)

    assert out == [10, 20, 30]  # 입력 순서대로
    dbs = [db for db, _, _ in seen]
    assert len(set(map(id, dbs))) == 3 and ctx.db not in dbs
    assert all(s.closed for s in sessions.opened) and sessions.open_now == 0
    # 수락 기준 9 — 워커는 in_parallel_branch=True(T5/F1 가드), 분기 안이라는 표식을 쥔다
    assert all(flag and holds for _, flag, holds in seen)


async def test_top_level_waits_for_permits_instead_of_degrading() -> None:
    """수락 기준 16(Phase 1 선행분) — 최상위 호출 지점은 폭이 예산보다 커도 모든 항목이 분기
    세션을 받는다(강등하지 않는다). 동시에 열린 세션은 예산을 넘지 않는다."""
    sessions = Sessions()
    ctx = _ctx(sessions)
    width = BRANCH_BUDGET + 3

    async def work(branch: RunContext, item: int) -> bool:
        await asyncio.sleep(0.02)
        return branch.db is not ctx.db

    out = await run_parallel(ctx, list(range(width)), work)

    assert all(out) and len(sessions.opened) == width
    assert sessions.max_open == BRANCH_BUDGET


async def test_permit_holder_degrades_to_sequential_instead_of_waiting() -> None:
    """D6 — permit을 쥔 분기 안의 호출 지점은 기다리지 않고, 얻지 못한 항목은 호출자 세션에서
    한 번에 하나씩 돈다."""
    sessions = Sessions()
    ctx = _ctx(sessions)
    running_on_caller = 0
    max_on_caller = 0

    async def inner(branch: RunContext, item: int) -> str:
        nonlocal running_on_caller, max_on_caller
        on_caller = branch.db is outer_db[0]
        if on_caller:
            running_on_caller += 1
            max_on_caller = max(max_on_caller, running_on_caller)
        await asyncio.sleep(0.01)
        if on_caller:
            running_on_caller -= 1
        return "caller" if on_caller else "own"

    outer_db: list[Any] = []

    async def outer(branch: RunContext, item: int) -> list[str]:
        outer_db.append(branch.db)
        return await run_parallel(branch, [1, 2, 3, 4, 5, 6], inner)

    # 바깥 하나가 permit 1개, 안쪽은 남은 3개를 비차단으로 얻고 나머지 3개는 호출자 세션에서 순차로
    out = await asyncio.wait_for(run_parallel(ctx, [0], outer), 2)

    assert sorted(out[0]).count("own") == BRANCH_BUDGET - 1
    assert out[0].count("caller") == 6 - (BRANCH_BUDGET - 1)
    assert max_on_caller == 1  # 호출자 세션은 동시에 쓰지 않는다
    assert sessions.max_open <= BRANCH_BUDGET


async def test_failure_cancels_siblings_closes_sessions_and_keeps_the_original_error() -> None:
    """수락 기준 5 — 한 항목이 실패하면 나머지를 취소·정리하고 원래 예외를 올린다."""
    sessions = Sessions()
    ctx = _ctx(sessions)
    finished: list[int] = []

    class Boom(Exception):
        pass

    async def work(branch: RunContext, item: int) -> None:
        if item == 0:
            await asyncio.sleep(0.01)
            raise Boom("첫 워커 실패")
        await asyncio.sleep(1)
        finished.append(item)

    with pytest.raises(Boom, match="첫 워커 실패"):
        await run_parallel(ctx, [0, 1, 2], work)

    assert finished == []  # 형제는 끝까지 돌지 않았다
    assert all(s.closed for s in sessions.opened) and sessions.open_now == 0
    # permit이 모두 돌아왔다 — 다음 호출 지점이 예산만큼 다시 연다
    assert ctx.branch_state.budget._value == BRANCH_BUDGET  # noqa: SLF001


async def test_double_cancellation_does_not_leak_permits() -> None:
    """R2-1 — 취소가 두 번 와도(LangGraph의 형제 취소) permit은 샌 적 없이 돌아온다."""
    sessions = Sessions()
    ctx = _ctx(sessions)

    async def hold() -> None:
        async with ctx.isolated_branch() as (_branch, isolated):
            assert isolated
            await asyncio.sleep(10)

    task = asyncio.ensure_future(hold())
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ctx.branch_state.budget._value == BRANCH_BUDGET  # noqa: SLF001


# ── 세션 출처 ───────────────────────────────────────────────────────────


async def test_default_opener_uses_the_parent_session_engine() -> None:
    """D1-② — session_factory가 없으면 부모 세션의 엔진으로 연다(제품 워커는 runner 엔진)."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    engine = create_async_engine("postgresql+asyncpg://u:p@localhost/db")
    parent = AsyncSession(bind=engine)
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=parent, params={})

    async with ctx.isolated_branch() as (branch, isolated):
        assert isolated and branch.db is not parent and branch.db.bind is engine
    await engine.dispose()


@pytest.mark.parametrize("kind", ["connection", "none"])
async def test_unisolatable_session_degrades_sequentially_and_warns_once_per_run(
    kind: str,
) -> None:
    """D1-③⑤ — 연결에 묶인 세션·db=None이면 공유 세션을 동시에 쓰지 않고 순차로 강등한다."""
    from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

    engine = create_async_engine("postgresql+asyncpg://u:p@localhost/db")
    if kind == "connection":
        # 트랜잭션 테스트 픽스처처럼 연결에 묶인 세션(연결을 열 필요는 없다 — bind만 본다)
        db: Any = SimpleNamespace(bind=AsyncConnection(engine))
    else:
        db = None
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={})
    running = 0
    max_running = 0

    async def work(branch: RunContext, item: int) -> Any:
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.01)
        running -= 1
        return branch.db

    with structlog.testing.capture_logs() as logs:
        out = await run_parallel(ctx, [1, 2, 3], work)
        await run_parallel(ctx, [1, 2], work)

    assert all(d is db for d in out) and max_running == 1
    assert len([e for e in logs if e["log_level"] == "warning"]) == 1
    await engine.dispose()


async def test_bindless_session_without_factory_is_an_explicit_error() -> None:
    """D1-④ — 엔진에 묶이지 않은 세션은 session_factory 없이 격리할 수 없다."""
    from sqlalchemy.ext.asyncio import AsyncSession

    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=AsyncSession(), params={})

    with pytest.raises(RuntimeError, match="session_factory"):
        async with ctx.isolated_branch():
            pass


def test_child_and_replace_keep_the_shared_branch_state() -> None:
    """수락 기준 3 · R2-3 — 서브에이전트로 내려가도 세션 출처·예산·보유 표식이 같은 객체다."""
    from dataclasses import replace

    sessions = Sessions()
    ctx = replace(_ctx(sessions), holds_branch_permit=True)
    child = ctx.child(uuid.uuid4(), {})
    copied = replace(ctx)

    for other in (child, copied):
        assert other.session_factory is ctx.session_factory
        assert other.branch_state is ctx.branch_state
        assert other.holds_branch_permit is True


# ── 콜백 직렬화 ─────────────────────────────────────────────────────────


async def test_callbacks_never_overlap() -> None:
    """수락 기준 6 — 제품 콜백(run 세션 커밋)은 동시 분기에서도 한 번에 하나만 돈다."""
    sessions = Sessions()
    inside = 0
    max_inside = 0

    async def on_progress(node_id: str, done: int, total: int) -> None:
        nonlocal inside, max_inside
        inside += 1
        max_inside = max(max_inside, inside)
        await asyncio.sleep(0.01)
        inside -= 1

    ctx = _ctx(sessions, on_progress=on_progress)

    async def work(branch: RunContext, item: int) -> None:
        await branch.emit_progress(f"n{item}", item, 4)

    await run_parallel(ctx, [1, 2, 3, 4], work)

    assert max_inside == 1


async def test_reentrant_callback_fails_fast_instead_of_deadlocking() -> None:
    """수락 기준 10 — 콜백 안에서 콜백을 부르면 잠금에서 영원히 기다리지 않고 바로 실패한다."""
    sessions = Sessions()
    holder: list[RunContext] = []

    async def on_tool_call(*args: Any) -> None:
        await holder[0].emit_progress("n", 1, 1)

    async def on_progress(*args: Any) -> None:
        return None

    ctx = _ctx(sessions, on_tool_call=on_tool_call, on_progress=on_progress)
    holder.append(ctx)

    with pytest.raises(RuntimeError, match="콜백 안에서"):
        await asyncio.wait_for(ctx.emit_tool_call("t", "rag_search", "succeeded", {}), 1)


# ── 실제 경로: supervisor → _run_subagent → child() → loop ────────────────


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


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """실행 로그(격리 세션 DB 쓰기)를 끄고, 받은 컨텍스트를 기록하는 노드 타입을 끼운다."""
    calls: list[dict[str, Any]] = []

    async def log_start(*a: Any, **k: Any) -> None:
        return None

    async def log_finish(*a: Any, **k: Any) -> None:
        return None

    async def probe_node(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
        calls.append(
            {
                "db": ctx.db,
                "parallel": ctx.in_parallel_branch,
                "holds": ctx.holds_branch_permit,
            }
        )
        await asyncio.sleep(0.02)
        return {"text": "ok"}

    monkeypatch.setattr(graph, "_log_node_start", log_start)
    monkeypatch.setattr(graph, "_log_node_finish", log_finish)
    monkeypatch.setitem(graph.NODE_EXECUTORS, "probe", probe_node)
    return calls


@pytest.mark.parametrize("workers", [BRANCH_BUDGET, 2])
async def test_nested_supervisor_loop_on_the_real_path_never_deadlocks(
    probe: list[dict[str, Any]], workers: int
) -> None:
    """수락 기준 4·8 — supervisor 워커(permit 보유) 안의 parallel loop가 permit을 기다리지
    않는다. 실제 `_run_subagent → child() → dispatch_agent` 경로라 보유 표식이 child()로
    전파돼야 통과한다."""
    body = _agent({"nodes": [{"id": "p", "type": "probe"}], "edges": []})
    worker = _agent(
        {
            "nodes": [
                {
                    "id": "lp",
                    "type": "loop",
                    "loop_body_agent_id": str(body.id),
                    "items": [1, 2, 3, 4],
                    "parallel": True,
                }
            ],
            "edges": [],
        }
    )
    sessions = Sessions({body.id: body, worker.id: worker})
    ctx = _ctx(sessions)
    supervisor = {
        "nodes": [
            {
                "id": "sup",
                "type": "supervisor",
                "workers": [{"agent_id": str(worker.id)} for _ in range(workers)],
            }
        ],
        "edges": [],
    }

    await asyncio.wait_for(graph.execute_agent(ctx, "workflow", supervisor), 2)

    assert len(probe) == workers * 4
    assert sessions.max_open <= BRANCH_BUDGET
    assert all(c["parallel"] and c["holds"] for c in probe)
    assert all(s.closed for s in sessions.opened)


async def test_close_failure_does_not_mask_the_original_error() -> None:
    """리뷰 F3 — 분기 세션 닫기가 실패해도 본문의 원래 예외(HITL 차단 등)가 그대로 올라간다."""
    sessions = Sessions()

    async def broken_close(self: FakeSession) -> None:
        self.owner.open_now -= 1
        raise ConnectionError("close 중 연결 끊김")

    class Original(Exception):
        pass

    ctx = _ctx(sessions)

    async def work(branch: RunContext, item: int) -> None:
        branch.db.close = broken_close.__get__(branch.db)  # type: ignore[method-assign]
        raise Original("원래 예외")

    with structlog.testing.capture_logs() as logs, pytest.raises(Original):
        await run_parallel(ctx, [1], work)

    assert any(e["event"] == "병렬 분기 세션 닫기 실패" for e in logs)
    assert ctx.branch_state.budget._value == BRANCH_BUDGET  # noqa: SLF001


async def test_callback_reentrancy_check_is_per_run_and_only_while_inside() -> None:
    """리뷰 F4 — 다른 run의 콜백, 콜백이 끝난 뒤 도는 태스크의 호출은 막지 않는다."""
    sessions = Sessions()
    later: list[asyncio.Task[None]] = []
    calls: list[str] = []

    async def other_progress(*args: Any) -> None:
        calls.append("other")

    other = _ctx(sessions, on_progress=other_progress)

    async def own_progress(*args: Any) -> None:
        calls.append("own")

    async def on_tool_call(*args: Any) -> None:
        await other.emit_progress("n", 1, 1)  # 다른 run — 잠금이 달라 교착하지 않는다
        later.append(asyncio.ensure_future(ctx.emit_progress("n", 1, 1)))  # 끝난 뒤 실행

    ctx = _ctx(sessions, on_tool_call=on_tool_call, on_progress=own_progress)

    await asyncio.wait_for(ctx.emit_tool_call("t", "rag_search", "succeeded", {}), 1)
    await asyncio.wait_for(later[0], 1)

    assert calls == ["other", "own"]
