"""loop 본문의 승인 요청은 조용히 사라지지 않고 명확한 오류로 실패한다 (#80).

`_run_subagent`를 거치는 노드(agent_call·supervisor 등)는 자식 ctx의 `pending_approval`을
검사해 `SubagentApprovalNotSupportedError`로 실패한다. loop는 `dispatch_agent`를 직접 불러
이 검사가 없었고, 본문 react_agent가 승인 게이트에 걸리면 승인 요청이 사라진 채 부모 run이
정상 완료됐다. 본문 노드가 `ApprovalRequired`를 올리는 실제
`execute_agent → loop → dispatch_agent → 그래프` 경로로 확인한다. react_agent와 DB를 쓰는
재현은 `tests/test_hitl_approval_pg.py`에 있다.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from biznotes_core.services.agents import graph
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import ApprovalRequired, SubagentApprovalNotSupportedError


class FakeSession:
    """`_exec_loop`가 쓰는 `get`과 분기 세션의 `close`만 있는 세션."""

    def __init__(self, agents: dict[uuid.UUID, Any], opened: list["FakeSession"]) -> None:
        self.agents = agents
        self.closed = False
        opened.append(self)

    async def get(self, model: Any, key: Any) -> Any:
        return self.agents.get(key)

    async def close(self) -> None:
        self.closed = True


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
    def __init__(self, items: list[int], pause_on: int | None) -> None:
        self.pause_on = pause_on  # 이 항목의 본문 노드가 승인 게이트에 걸린다
        self.ran: list[int] = []  # 본문 노드가 실제로 돈 항목
        self.opened: list[FakeSession] = []
        self.body = _agent({"nodes": [{"id": "g", "type": "gate"}], "edges": []})
        self.agents = {self.body.id: self.body}
        self.parent = {
            "nodes": [
                {
                    "id": "lp",
                    "type": "loop",
                    "loop_body_agent_id": str(self.body.id),
                    "items": items,
                    "parallel": False,
                }
            ],
            "edges": [],
        }

    def ctx(self) -> RunContext:
        return RunContext(
            run_id=uuid.uuid4(),
            notebook_id=uuid.uuid4(),
            db=FakeSession(self.agents, self.opened),  # type: ignore[arg-type]
            params={},
            session_factory=lambda: FakeSession(self.agents, self.opened),  # type: ignore[arg-type,return-value]
        )

    def parallel(self) -> None:
        self.parent["nodes"][0]["parallel"] = True  # type: ignore[index]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    """본문 노드 `gate`를 끼운다. `pause_on` 항목을 만나면 react_agent 승인 게이트처럼
    `ApprovalRequired`를 올리고, 그 밖의 항목은 정상 반환한다."""
    h = Harness([1, 2, 3], pause_on=2)

    async def log_start(*a: Any, **k: Any) -> None:
        return None

    async def log_finish(*a: Any, **k: Any) -> None:
        return None

    async def gate_node(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
        item = ctx.params["item"]
        h.ran.append(item)
        if item == h.pause_on:
            raise ApprovalRequired(node["id"], {"tool_name": "http_request"})
        return {"text": f"ok-{item}"}

    monkeypatch.setattr(graph, "_log_node_start", log_start)
    monkeypatch.setattr(graph, "_log_node_finish", log_finish)
    monkeypatch.setitem(graph.NODE_EXECUTORS, "gate", gate_node)
    return h


async def test_sequential_loop_body_approval_fails_and_stops_the_loop(harness: Harness) -> None:
    ctx = harness.ctx()

    with pytest.raises(SubagentApprovalNotSupportedError, match="서브에이전트.*내부"):
        await graph.execute_agent(ctx, "workflow", harness.parent)

    # 승인 요청이 부모로 새지 않고(재개할 수 없는 checkpoint), 뒤 항목은 돌지 않는다
    assert ctx.pending_approval is None
    assert harness.ran == [1, 2]


async def test_parallel_loop_body_approval_fails_and_releases_branch_sessions(
    harness: Harness,
) -> None:
    harness.parallel()
    ctx = harness.ctx()

    with pytest.raises(SubagentApprovalNotSupportedError, match="서브에이전트.*내부"):
        await graph.execute_agent(ctx, "workflow", harness.parent)

    assert ctx.pending_approval is None
    assert 2 in harness.ran
    branches = [s for s in harness.opened if s is not ctx.db]
    assert branches and all(s.closed for s in branches)


@pytest.mark.parametrize("parallel", [False, True])
async def test_loop_body_without_approval_still_completes(
    harness: Harness, parallel: bool
) -> None:
    """가드는 승인 대기가 있을 때만 걸린다 — 정상 본문은 모든 항목이 돈다."""
    harness.pause_on = None
    if parallel:
        harness.parallel()
    ctx = harness.ctx()

    await graph.execute_agent(ctx, "workflow", harness.parent)

    assert sorted(harness.ran) == [1, 2, 3]
    assert ctx.pending_approval is None
