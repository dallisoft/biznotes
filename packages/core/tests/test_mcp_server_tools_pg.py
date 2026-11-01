# 신규(#139 B, docs/plans/mcp-exposure/). 필요: PostgreSQL·Valkey(PG 테스트).
"""MCP 승인 도구의 정본은 DB다 — 실제 `mcp_server_tools` 행으로 노출·호출을 본다 (#139 B).

`list_tools`·`call_tool`(외부 MCP 서버)만 대역이다. 승인 조회 SQL, 승인 0개 서버의 `list_tools`
생략, 같은 run 안의 승인 철회 즉시 반영, 관리자 승인 교체(감사·이름 검증·CASCADE)를 확인한다.
`create_all` 기반이라 Alembic 마이그레이션(부록 A 시드) 검증이 아니다.
"""

import uuid
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
import structlog.testing
from sqlalchemy import event, select

from biznotes_core.models import AuditLog, MCPServer, MCPServerTool, User
from biznotes_core.services.agents import mcp_client, tool_registry
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import ToolUnavailableError
from biznotes_core.services.agents.mcp_exposure import (
    MCPExposure,
    approved_tools,
    mcp_exposure_for,
    set_approved_tools,
    unavailable_mcp_tools,
)
from biznotes_core.services.agents.mcp_tool_spec import mcp_tool_spec_hash
from biznotes_core.services.agents.nodes.llm import _dispatch_builtin_tool

LISTED = {
    "archhub": ["find_region", "building_data", "delete_building"],
    "kosis": ["search"],
}


@pytest.fixture(autouse=True)
def _isolated_safe_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_registry, "_safe_discovered_mcp_tools", set())


@pytest.fixture
def listed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`list_tools` 대역 — 부른 서버 이름을 기록한다."""
    seen: list[str] = []

    async def list_tools(server: Any) -> list[dict[str, Any]]:
        seen.append(server.name.split("-")[0])
        return [{"name": n} for n in LISTED[server.name.split("-")[0]]]

    monkeypatch.setattr(mcp_client, "list_tools", list_tools)
    return seen


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    recorded: list[tuple[str, str]] = []

    async def call_tool(server: Any, tool: str, arguments: dict[str, Any], **_kw: Any) -> str:
        recorded.append((server.name.split("-")[0], tool))
        return "ok"

    monkeypatch.setattr(mcp_client, "call_tool", call_tool)
    return recorded


async def _server(db: Any, kind: str, *, enabled: bool = True) -> MCPServer:
    row = MCPServer(
        name=f"{kind}-{uuid.uuid4().hex[:6]}",
        transport="stdio",
        endpoint_or_command="x",
        enabled=enabled,
    )
    db.add(row)
    await db.flush()
    return row


def _approve(server: MCPServer, *names: str, read_only: bool = True) -> list[MCPServerTool]:
    """승인 행 — `list_tools` 대역이 주는 스펙(`{"name": n}`)으로 고정한다(#293)."""
    now = datetime.now(UTC)
    return [
        MCPServerTool(
            server_id=server.id,
            tool_name=n,
            read_only=read_only,
            approved_at=now,
            approved_spec_hash=mcp_tool_spec_hash({"name": n}),
        )
        for n in names
    ]


@pytest.fixture
async def world(db: Any) -> dict[str, MCPServer]:
    """archhub: 두 도구 승인(+쓰기 도구 read_only=False). kosis: 승인 없음. off: 꺼진 서버."""
    w = {
        "archhub": await _server(db, "archhub"),
        "kosis": await _server(db, "kosis"),
        "off": await _server(db, "archhub", enabled=False),
    }
    db.add_all(_approve(w["archhub"], "find_region", "building_data"))
    db.add_all(_approve(w["archhub"], "delete_building", read_only=False))
    db.add_all(_approve(w["off"], "find_region"))
    await db.commit()
    return w


def _ctx(db: Any, user: User, role: str = "admin") -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=user.id,
        user_role=role,
    )


def _tool_of(exposed: str) -> str:
    return exposed.split("_", 2)[2]


@contextmanager
def _count_statements(db: Any) -> Iterator[list[str]]:
    engine = db.bind.sync_engine
    statements: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before)


# ---------- 승인 조회 ----------


async def test_approved_tools_reads_only_read_only_rows_in_one_query(db, world) -> None:
    ids = [s.id for s in world.values()]
    with _count_statements(db) as statements:
        approved = await approved_tools(db, ids)
    assert len(statements) == 1
    assert approved == {
        world["archhub"].id: frozenset({"find_region", "building_data"}),
        world["off"].id: frozenset({"find_region"}),
    }  # kosis는 승인 행이 없어 결과에 없다
    with _count_statements(db) as statements:
        assert await approved_tools(db, []) == {}
    assert statements == []


# ---------- 디스커버리·호출 ----------


async def test_discovery_exposes_only_approved_read_only_tools(
    db, world, admin_user, listed
) -> None:
    ctx = _ctx(db, admin_user)
    with warnings.catch_warnings():
        # 코어 판정 경로는 deprecated 심볼을 쓰지 않는다(재리뷰 Minor 2)
        warnings.simplefilter("error", DeprecationWarning)
        _, dispatchers = await tool_registry.build_tool_specs(ctx, ["mcp"])

    assert sorted(_tool_of(n) for n in dispatchers) == ["building_data", "find_region"]
    assert listed == ["archhub"]  # 승인 0개(kosis)·꺼진 서버는 list_tools를 부르지 않는다
    assert ctx._mcp_discovery_outcome == {  # type: ignore[attr-defined]
        "servers": 2,
        "approved_servers": 1,
        "failed_approved_servers": 0,
    }


async def test_new_approval_shows_up_in_the_next_run(db, world, admin_user, listed) -> None:
    await set_approved_tools(
        db,
        world["kosis"],
        {"search": True},
        admin_id=admin_user.id,
        spec_hashes={"search": mcp_tool_spec_hash({"name": "search"})},  # 검토한 스펙(#293)
    )
    await db.commit()
    _, dispatchers = await tool_registry.build_tool_specs(_ctx(db, admin_user), ["mcp"])
    assert sorted(_tool_of(n) for n in dispatchers) == ["building_data", "find_region", "search"]


async def test_revoked_approval_is_rejected_within_the_same_run(
    db, world, admin_user, listed, calls
) -> None:
    """디스패처는 호출 시점에 승인을 다시 본다 — run 캐시로 철회를 우회하지 못한다."""
    ctx = _ctx(db, admin_user)
    _, dispatchers = await tool_registry.build_tool_specs(ctx, ["mcp"])
    exposed = next(n for n in dispatchers if _tool_of(n) == "find_region")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert await dispatchers[exposed](ctx, exposed, {}) == "ok"

    await set_approved_tools(db, world["archhub"], {"building_data": True}, admin_id=admin_user.id)
    await db.commit()
    with pytest.raises(ToolUnavailableError, match="승인 목록"):
        await dispatchers[exposed](ctx, exposed, {})
    assert calls == [("archhub", "find_region")]
    assert [e["status"] for e in ctx.tool_evidence] == ["succeeded", "failed"]


async def test_mcp_call_checks_db_approval(db, world, admin_user, calls) -> None:
    ctx = _ctx(db, admin_user)
    server_id = str(world["archhub"].id)
    out = await _dispatch_builtin_tool(
        ctx, "mcp_call", {"server_id": server_id, "tool_name": "find_region", "arguments": {}}
    )
    assert out == "ok"
    for tool in ("delete_building", "search"):  # read_only=False 승인·목록에 없는 도구
        with pytest.raises(ToolUnavailableError, match="승인 목록"):
            await _dispatch_builtin_tool(
                ctx, "mcp_call", {"server_id": server_id, "tool_name": tool}
            )
    with pytest.raises(ToolUnavailableError, match="승인 목록"):  # 다른 서버의 승인은 통하지 않는다
        await _dispatch_builtin_tool(
            ctx,
            "mcp_call",
            {"server_id": str(world["kosis"].id), "tool_name": "find_region"},
        )
    assert calls == [("archhub", "find_region")]


async def test_exposure_for_matches_discovery(db, world, admin_user, listed) -> None:
    assert await mcp_exposure_for(db, world["archhub"], audience="admin") == MCPExposure(
        exposed=True, reason="ok", tools=frozenset({"find_region", "building_data"})
    )
    assert await mcp_exposure_for(db, world["kosis"], audience="admin") == MCPExposure(
        exposed=False, reason="no_approved_tools"
    )
    assert await mcp_exposure_for(db, world["off"], audience="admin") == MCPExposure(
        exposed=False, reason="server_disabled"
    )


async def test_unapproved_servers_only_give_no_approved_tools(db, admin_user, listed) -> None:
    await _server(db, "kosis")
    await db.commit()
    ctx = _ctx(db, admin_user)
    _, dispatchers = await tool_registry.build_tool_specs(ctx, ["rag_search", "mcp"])
    assert await unavailable_mcp_tools(ctx, ["mcp"], dispatchers) == {"mcp": "no_approved_tools"}
    assert listed == []


async def test_approval_lookup_errors_are_not_swallowed(db, admin_user, monkeypatch) -> None:
    """승인 조회 실패를 삼키면 run 트랜잭션이 aborted로 남는다 — 오류를 올린다(리뷰 B-2)."""
    from biznotes_core.services.agents import mcp_exposure

    async def broken(db_: Any, ids: Any) -> Any:
        raise RuntimeError("db down")

    await _server(db, "archhub")
    await db.commit()
    # 디스커버리는 승인을 고정 해시와 함께 읽는다(#293)
    monkeypatch.setattr(mcp_exposure, "approved_tool_pins", broken)
    with structlog.testing.capture_logs(), pytest.raises(RuntimeError, match="db down"):
        await tool_registry.build_tool_specs(_ctx(db, admin_user), ["mcp"])


# ---------- 관리자 승인 교체 ----------


async def test_set_approved_tools_replaces_and_audits(db, world, admin_user) -> None:
    server = world["archhub"]
    await set_approved_tools(
        db,
        server,
        {"find_region": True, "delete_building": True, "permits_pipeline": True},
        admin_id=admin_user.id,
    )
    await db.commit()

    rows = (
        await db.execute(select(MCPServerTool).where(MCPServerTool.server_id == server.id))
    ).scalars()
    by_name = {r.tool_name: r for r in rows}
    assert {n: r.read_only for n, r in by_name.items()} == {
        "find_region": True,
        "delete_building": True,
        "permits_pipeline": True,
    }
    assert by_name["find_region"].approved_by is None  # 바뀌지 않은 행은 그대로
    assert by_name["permits_pipeline"].approved_by == admin_user.id
    assert by_name["delete_building"].approved_by == admin_user.id
    audit = (
        await db.execute(select(AuditLog).where(AuditLog.action == "mcp.tools_approve"))
    ).scalar_one()
    assert audit.user_id == admin_user.id and audit.resource == server.name
    assert audit.detail == {
        "server_id": str(server.id),
        "added": ["permits_pipeline"],
        "removed": ["building_data"],
        "read_only_changed": ["delete_building"],
    }


async def test_unchanged_approval_writes_no_audit(db, world, admin_user) -> None:
    await set_approved_tools(
        db,
        world["off"],
        {"find_region": True},
        admin_id=admin_user.id,
    )
    await db.commit()
    count = (await db.execute(select(AuditLog))).scalars().all()
    assert count == []


@pytest.mark.parametrize("name", ["", "a/b", "공백 있음", "x" * 129, "drop table;"])
async def test_invalid_tool_names_are_rejected(db, world, admin_user, name) -> None:
    with pytest.raises(ValueError, match="형식"):
        await set_approved_tools(db, world["kosis"], {name: True}, admin_id=admin_user.id)


async def test_deleting_a_server_cascades_its_approvals(db, world) -> None:
    server_id = world["archhub"].id
    await db.delete(world["archhub"])
    await db.commit()
    rows = await db.execute(select(MCPServerTool).where(MCPServerTool.server_id == server_id))
    assert rows.scalars().all() == []
