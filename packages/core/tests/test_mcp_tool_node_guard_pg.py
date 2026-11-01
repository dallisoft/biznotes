# 신규(#77). 필요: PostgreSQL·Valkey(PG 테스트).
"""사용자 정의는 관리자 자격증명의 MCP 도구를 부르지 못한다 — 실제 행·분기 세션 (#77).

판정 규칙과 `mcp_call`은 `tests/test_mcp_tool_node_guard.py`가 DB 없이 고정한다. 여기서는 실제
`Agent`·`MCPServer` 행과 `execute_agent`로, 분기 세션(병렬 loop·fan-out)이 새로 SELECT해도 판정이
같은지, 삭제된 정의는 관리자 실행이어도 거부되는지 본다.

`create_all` 기반이라 Alembic 마이그레이션 검증이 아니다.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from biznotes_core.models import Agent, MCPServer, MCPServerTool
from biznotes_core.services.agents import mcp_client
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError
from biznotes_core.services.agents.runtime import execute_agent


@pytest.fixture
async def server(db: Any) -> MCPServer:
    row = MCPServer(
        name=f"vacation-{uuid.uuid4().hex[:6]}",
        transport="stdio",
        endpoint_or_command="x",
        enabled=True,
    )
    db.add(row)
    await db.flush()
    # #153부터 mcp_tool은 승인 행을 요구한다 — 이 파일은 작성 주체 규칙(#77)을 본다
    for tool in ("vacation_balance", "word_count"):
        db.add(
            MCPServerTool(
                server_id=row.id, tool_name=tool, read_only=True, approved_at=datetime.now(UTC)
            )
        )
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    recorded: list[tuple[str, str]] = []

    async def fake(srv: Any, tool: str, arguments: dict[str, Any], **_kw: Any) -> str:
        recorded.append((srv.name, tool))
        return "ok"

    monkeypatch.setattr(mcp_client, "call_tool", fake)
    return recorded


@pytest.fixture
def artifacts(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """`kind="mcp"` 그래프의 `save` 노드(save_artifact)가 스토리지에 닿지 않게 한다(#323)."""
    saved: dict[str, bytes] = {}
    monkeypatch.setattr("biznotes_core.services.storage.ensure_buckets", lambda: None)
    monkeypatch.setattr(
        "biznotes_core.services.storage.put_object",
        lambda bucket, key, data, content_type="": saved.__setitem__(key, data),
    )
    return saved


def _mcp_nodes(server: MCPServer, *ids: str) -> list[dict[str, Any]]:
    return [
        {
            "id": node_id,
            "type": "mcp_tool",
            "server_id": str(server.id),
            "tool_name": "vacation_balance",
            "arguments": {},
            "requires_approval": False,  # 게이트는 test_direct_node_approval이 본다(#153)
        }
        for node_id in ids
    ]


async def _agent(
    db: Any, owner: Any, scope: str, definition: dict[str, Any], kind: str = "workflow"
) -> Agent:
    agent = Agent(
        name=f"mcp-guard-{uuid.uuid4().hex[:6]}",
        kind=kind,
        icon="🔒",
        definition=definition,
        params_schema={},
        scope=scope,
        owner_id=owner.id if scope != "system" else None,
        enabled=True,
    )
    db.add(agent)
    await db.commit()  # 분기 세션은 커밋된 행만 본다
    await db.refresh(agent)
    return agent


def _ctx(db: Any, user: Any, agent_id: uuid.UUID | None, role: str = "user") -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=user.id,
        user_role=role,
        agent_id=agent_id,
    )


def _loop_parent(body: Agent, *, parallel: bool) -> dict[str, Any]:
    return {
        "nodes": [
            {
                "id": "lp",
                "type": "loop",
                "loop_body_agent_id": str(body.id),
                "items": [1, 2, 3],
                "parallel": parallel,
            }
        ],
        "edges": [],
    }


async def test_user_scope_workflow_fails_and_system_workflow_runs(
    db: Any, user_a: Any, server: MCPServer, calls: list[Any]
) -> None:
    """수락 기준 1·2 — 같은 정의가 scope에 따라 갈린다(시드와 그 사용자 복제본)."""
    definition = {"nodes": _mcp_nodes(server, "call"), "edges": []}
    system = await _agent(db, user_a, "system", definition)
    copy = await _agent(db, user_a, "user", definition)

    await execute_agent(_ctx(db, user_a, system.id), "workflow", definition)
    assert len(calls) == 1

    with pytest.raises(AgentRuntimeError, match="system"):
        await execute_agent(_ctx(db, user_a, copy.id), "workflow", definition)
    assert len(calls) == 1


async def test_admin_running_a_users_workflow_is_still_rejected(
    db: Any, user_a: Any, admin_user: Any, server: MCPServer, calls: list[Any]
) -> None:
    """수락 기준 4 — 관리자가 사용자 소유 정의를 실행해도 거부한다(혼동된 대리인)."""
    definition = {"nodes": _mcp_nodes(server, "call"), "edges": []}
    users = await _agent(db, user_a, "user", definition)

    with pytest.raises(AgentRuntimeError):
        await execute_agent(_ctx(db, admin_user, users.id, role="admin"), "workflow", definition)

    assert calls == []


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential-loop", "parallel-loop"])
async def test_loop_body_is_judged_by_the_called_definition_on_branch_sessions(
    db: Any, user_a: Any, server: MCPServer, calls: list[Any], parallel: bool
) -> None:
    """수락 기준 3 — 병렬 loop의 분기 세션은 `Agent`를 새로 SELECT한다. 그래도 callee 기준이다."""
    body = {"nodes": _mcp_nodes(server, "call"), "edges": []}
    system_body = await _agent(db, user_a, "system", body)
    user_body = await _agent(db, user_a, "user", body)
    user_parent = await _agent(db, user_a, "user", _loop_parent(system_body, parallel=parallel))
    system_parent = await _agent(db, user_a, "system", _loop_parent(user_body, parallel=parallel))

    await execute_agent(
        _ctx(db, user_a, user_parent.id), "workflow", user_parent.definition
    )
    assert len(calls) == 3

    with pytest.raises(AgentRuntimeError, match="system"):
        await execute_agent(
            _ctx(db, user_a, system_parent.id), "workflow", system_parent.definition
        )
    assert len(calls) == 3


async def test_fan_out_siblings_on_branch_sessions_run_for_a_system_definition(
    db: Any, user_a: Any, server: MCPServer, calls: list[Any]
) -> None:
    """수락 기준 3 — fan-out 형제는 노드별 분기 세션을 쓴다. 커밋된 system 행은 보인다."""
    definition = {
        "nodes": [{"id": "start", "type": "constant", "value": {"text": "x"}}]
        + _mcp_nodes(server, "a", "b"),
        "edges": [["start", "a"], ["start", "b"]],
    }
    system = await _agent(db, user_a, "system", definition)

    await execute_agent(_ctx(db, user_a, system.id), "workflow", definition)

    assert len(calls) == 2


async def test_deleted_definition_is_rejected_even_for_an_admin_run(
    db: Any, user_a: Any, admin_user: Any, server: MCPServer, calls: list[Any]
) -> None:
    """수락 기준 5(b) — 실행 중에 소유자가 정의를 지우면(삭제 경합) 관리자 실행이어도 거부한다.
    `execute_agent`가 도는 사이의 삭제를 다른 세션에서 재현한다."""
    definition = {
        "nodes": [{"id": "start", "type": "constant", "value": {"text": "x"}}]
        + _mcp_nodes(server, "a", "b"),
        "edges": [["start", "a"], ["start", "b"]],
    }
    doomed = await _agent(db, user_a, "user", definition)
    doomed_id, admin = doomed.id, SimpleNamespace(id=admin_user.id)  # expire 뒤 지연 로딩을 피한다
    await db.delete(doomed)
    await db.commit()
    db.expire_all()

    with pytest.raises(AgentRuntimeError):
        await execute_agent(_ctx(db, admin, doomed_id, role="admin"), "workflow", definition)

    assert calls == []


async def test_kind_mcp_agents_follow_the_same_rule(
    db: Any, user_a: Any, server: MCPServer, calls: list[Any], artifacts: dict[str, bytes]
) -> None:
    """수락 기준 6 — `kind="mcp"` 에이전트도 scope에 따라 갈린다."""
    definition = {
        "server_id": str(server.id),
        "tool_name": "word_count",
        "arguments": {},
        "requires_approval": False,
    }
    system = await _agent(db, user_a, "system", definition, kind="mcp")
    users = await _agent(db, user_a, "user", definition, kind="mcp")

    await execute_agent(_ctx(db, user_a, system.id), "mcp", definition)
    assert len(calls) == 1
    assert len(artifacts) == 1  # 도구 결과가 save 노드까지 흘렀다

    with pytest.raises(AgentRuntimeError):
        await execute_agent(_ctx(db, user_a, users.id), "mcp", definition)
    assert len(calls) == 1
    assert len(artifacts) == 1
