# 신규(#139 A, docs/plans/mcp-exposure/). 필요: PostgreSQL·Valkey(PG 테스트).
"""일반 사용자 MCP 공개(Q6) — 실제 행과 별도 세션 커밋으로 본다 (#139 A).

`list_tools`·`call_tool`(외부 MCP 서버)만 대역이다. 확인하는 것:

- 일반 사용자는 켜진 `all_users` 서버의 승인 read-only 도구만 본다. 관리자 동작은 그대로다.
- 디스패처·`mcp_call`은 호출 시점에 다시 판정한다 — 다른 세션의 `admin_only` 전환·비활성·승인 철회를
  같은 run과 승인 재개에서 바로 거부한다(identity map의 옛 값을 쓰지 않는다).
- 일반 사용자에게는 서버 존재를 가르지 않는 단일 메시지를 준다.
- 일반 사용자 호출은 호출 전에 격리 세션으로 감사 행을 남기고, 기록이 실패하면 호출하지 않는다.

`create_all` 기반이라 Alembic 마이그레이션(부록 B) 검증이 아니다.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import structlog.testing
from sqlalchemy import select, update

from biznotes_core.core.db import get_session_factory
from biznotes_core.models import AuditLog, MCPServer, MCPServerTool, User
from biznotes_core.services.agents import mcp_client, mcp_exposure, tool_registry
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import ToolUnavailableError
from biznotes_core.services.agents.mcp_exposure import (
    USER_MCP_UNAVAILABLE,
    MCPExposure,
    mcp_exposure_for,
    set_server_exposure,
    unavailable_mcp_tools,
)
from biznotes_core.services.agents.mcp_tool_spec import mcp_tool_spec_hash
from biznotes_core.services.agents.nodes.llm import _dispatch_builtin_tool
from biznotes_core.services.agents.react import loop

LISTED = ["find_region", "delete_building"]


@pytest.fixture(autouse=True)
def _isolated_safe_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_registry, "_safe_discovered_mcp_tools", set())


@pytest.fixture
def listed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    async def list_tools(server: Any) -> list[dict[str, Any]]:
        seen.append(server.name)
        return [{"name": n} for n in LISTED]

    monkeypatch.setattr(mcp_client, "list_tools", list_tools)
    return seen


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    recorded: list[tuple[str, str, dict[str, Any]]] = []

    async def call_tool(server: Any, tool: str, arguments: dict[str, Any], **_kw: Any) -> str:
        recorded.append((server.name, tool, arguments))
        return "ok"

    monkeypatch.setattr(mcp_client, "call_tool", call_tool)
    return recorded


async def _server(
    db: Any, name: str, *, exposure: str, enabled: bool = True, approve: bool = True
) -> MCPServer:
    row = MCPServer(
        name=name, transport="stdio", endpoint_or_command="x", enabled=enabled, exposure=exposure
    )
    db.add(row)
    await db.flush()
    if approve:
        db.add(
            MCPServerTool(
                server_id=row.id,
                tool_name="find_region",
                read_only=True,
                approved_at=datetime.now(UTC),
                approved_spec_hash=mcp_tool_spec_hash({"name": "find_region"}),  # #293
            )
        )
    return row


@pytest.fixture
async def world(db: Any) -> dict[str, MCPServer]:
    w = {
        "public": await _server(db, "public-srv", exposure="all_users"),
        "private": await _server(db, "private-srv", exposure="admin_only"),
        "unapproved": await _server(db, "unapproved-srv", exposure="all_users", approve=False),
        "off": await _server(db, "off-srv", exposure="all_users", enabled=False),
    }
    await db.commit()
    return w


def _ctx(db: Any, user: User) -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=user.id,
        user_role=user.role,
    )


async def _commit_elsewhere(server: MCPServer, **values: Any) -> None:
    """관리자가 다른 트랜잭션에서 서버를 바꾼다 — run 세션 identity map과 무관한 커밋."""
    async with get_session_factory()() as other:
        await other.execute(update(MCPServer).where(MCPServer.id == server.id).values(**values))
        await other.commit()


async def _audits(action: str) -> list[AuditLog]:
    async with get_session_factory()() as fresh:
        rows = await fresh.execute(
            select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.created_at)
        )
        return list(rows.scalars().all())


def _servers_of(dispatchers: dict[str, Any], world: dict[str, MCPServer]) -> list[str]:
    tags = {str(s.id).replace("-", "")[:8]: name for name, s in world.items()}
    return sorted(
        tags[n.split("_", 2)[1]] for n in dispatchers if n.startswith("mcp_") and n != "mcp_call"
    )


# ---------- 노출 ----------


async def test_regular_user_sees_only_public_approved_tools(
    db, world, user_a, admin_user, listed
) -> None:
    user_ctx = _ctx(db, user_a)
    _, user_dispatchers = await tool_registry.build_tool_specs(user_ctx, ["mcp", "mcp_call"])
    assert _servers_of(user_dispatchers, world) == ["public"]
    assert "mcp_call" in user_dispatchers  # 켜진 공개 서버에 승인 도구가 있다
    assert listed == ["public-srv"]  # admin_only·미승인·꺼진 서버는 list_tools도 하지 않는다

    _, admin_dispatchers = await tool_registry.build_tool_specs(_ctx(db, admin_user), ["mcp"])
    assert _servers_of(admin_dispatchers, world) == ["private", "public"]  # 관리자 동작 불변


async def test_no_public_server_means_role_denied_without_names(
    db, world, user_a, listed
) -> None:
    ctx = _ctx(db, user_a)
    allowed = ["rag_search", "mcp", "mcp_call"]
    # 공개 서버 중 켜진 것은 미승인 서버 하나 — 사유는 승인 없음(공개 서버만의 정보)
    await _commit_elsewhere(world["public"], exposure="admin_only")
    with structlog.testing.capture_logs():
        _, dispatchers = await tool_registry.build_tool_specs(ctx, allowed)
    assert await unavailable_mcp_tools(ctx, allowed, dispatchers) == {
        "mcp": "no_approved_tools",
        "mcp_call": "role_denied",
    }
    # 켜진 공개 서버가 없으면 admin_only 서버의 승인·상태와 무관하게 권한 사유다
    await _commit_elsewhere(world["unapproved"], exposure="admin_only")
    fresh = _ctx(db, user_a)
    with structlog.testing.capture_logs():
        _, dispatchers = await tool_registry.build_tool_specs(fresh, allowed)
    assert await unavailable_mcp_tools(fresh, allowed, dispatchers) == {
        "mcp": "role_denied",
        "mcp_call": "role_denied",
    }
    assert fresh._mcp_discovery_outcome["servers"] == 0  # type: ignore[attr-defined]
    assert listed == []


async def test_exposure_for_user_audience(db, world) -> None:
    assert await mcp_exposure_for(db, world["public"], audience="user") == MCPExposure(
        exposed=True, reason="ok", tools=frozenset({"find_region"})
    )
    for name, reason in (
        ("private", "role_denied"),
        ("unapproved", "no_approved_tools"),
        ("off", "server_disabled"),
    ):
        exposure = await mcp_exposure_for(db, world[name], audience="user")
        assert exposure == MCPExposure(exposed=False, reason=reason), name


# ---------- mcp_call ----------


async def test_mcp_call_for_a_regular_user(db, world, user_a, calls) -> None:
    ctx = _ctx(db, user_a)
    out = await _dispatch_builtin_tool(
        ctx,
        "mcp_call",
        {
            "server_id": str(world["public"].id),
            "tool_name": "find_region",
            "arguments": {"q": "서울 강남구", "api_key": "test-secret"},
        },
    )
    assert out == "ok"

    denied = []
    for name, tool in (
        ("private", "find_region"),
        ("unapproved", "find_region"),
        ("off", "find_region"),
        ("public", "delete_building"),
    ):
        with pytest.raises(ToolUnavailableError) as error:
            await _dispatch_builtin_tool(
                ctx, "mcp_call", {"server_id": str(world[name].id), "tool_name": tool}
            )
        denied.append(str(error.value))
    with pytest.raises(ToolUnavailableError) as missing:
        await _dispatch_builtin_tool(
            ctx, "mcp_call", {"server_id": str(uuid.uuid4()), "tool_name": "find_region"}
        )
    denied.append(str(missing.value))
    assert set(denied) == {USER_MCP_UNAVAILABLE}  # 존재·공개 범위·승인을 가르지 않는다
    assert calls == [("public-srv", "find_region", {"q": "서울 강남구", "api_key": "test-secret"})]


async def test_user_calls_are_audited_without_argument_values(db, world, user_a, calls) -> None:
    ctx = _ctx(db, user_a)
    user_id = user_a.id
    await _dispatch_builtin_tool(
        ctx,
        "mcp_call",
        {
            "server_id": str(world["public"].id),
            "tool_name": "find_region",
            "arguments": {"q": "서울 강남구", "api_key": "test-secret"},
        },
    )
    await db.rollback()  # run이 실패해 롤백돼도 감사 행은 격리 세션에 남는다

    (started,) = await _audits("mcp.call")
    (result,) = await _audits("mcp.call_result")
    assert started.user_id == user_id and started.resource == "public-srv"
    assert started.detail["status"] == "started"
    assert started.detail["arg_keys"] == ["api_key", "q"]
    assert "test-secret" not in str(started.detail) and "강남구" not in str(started.detail)
    assert result.detail["status"] == "succeeded"
    assert result.detail["call_id"] == started.detail["call_id"]
    assert started.detail["run_id"] == str(ctx.run_id)


async def test_admin_calls_are_not_audited(db, world, admin_user, calls) -> None:
    await _dispatch_builtin_tool(
        _ctx(db, admin_user),
        "mcp_call",
        {"server_id": str(world["private"].id), "tool_name": "find_region"},
    )
    assert calls and await _audits("mcp.call") == []


async def test_audit_failure_blocks_the_call(db, world, user_a, calls, monkeypatch) -> None:
    async def broken(*a: Any, **kw: Any) -> None:
        raise RuntimeError("audit db down")

    monkeypatch.setattr(mcp_exposure, "_audit_isolated", broken)
    with structlog.testing.capture_logs(), pytest.raises(ToolUnavailableError) as error:
        await _dispatch_builtin_tool(
            _ctx(db, user_a),
            "mcp_call",
            {"server_id": str(world["public"].id), "tool_name": "find_region"},
        )
    assert str(error.value) == USER_MCP_UNAVAILABLE
    assert calls == []


# ---------- 호출 시점 재확인 ----------


@pytest.mark.parametrize(
    "change",
    [{"exposure": "admin_only"}, {"enabled": False}],
    ids=["admin-only", "disabled"],
)
async def test_cached_discovered_tool_is_rechecked_at_call_time(
    db, world, user_a, listed, calls, change
) -> None:
    ctx = _ctx(db, user_a)
    _, dispatchers = await tool_registry.build_tool_specs(ctx, ["mcp"])
    (exposed,) = [n for n in dispatchers if n.startswith("mcp_")]
    assert await dispatchers[exposed](ctx, exposed, {}) == "ok"

    await _commit_elsewhere(world["public"], **change)
    with pytest.raises(ToolUnavailableError) as error:
        await dispatchers[exposed](ctx, exposed, {})
    assert str(error.value) == USER_MCP_UNAVAILABLE
    assert len(calls) == 1


async def test_revoked_approval_is_rejected_within_the_run(
    db, world, user_a, listed, calls
) -> None:
    ctx = _ctx(db, user_a)
    _, dispatchers = await tool_registry.build_tool_specs(ctx, ["mcp"])
    (exposed,) = [n for n in dispatchers if n.startswith("mcp_")]
    async with get_session_factory()() as other:
        server = await other.get(MCPServer, world["public"].id)
        assert server is not None
        await mcp_exposure.set_approved_tools(other, server, {}, admin_id=None)
        await other.commit()
    with pytest.raises(ToolUnavailableError):
        await dispatchers[exposed](ctx, exposed, {})
    assert calls == []


async def test_resumed_mcp_call_is_rechecked(db, world, user_a, calls) -> None:
    """승인 대기 중 서버가 admin_only가 되면 재개 시점에 거부한다(재개는 게이트를 거치지 않는다)."""
    ctx = _ctx(db, user_a)
    _, dispatchers = await tool_registry.build_tool_specs(ctx, ["mcp_call"])
    assert "mcp_call" in dispatchers
    await _commit_elsewhere(world["public"], exposure="admin_only")
    messages: list[dict[str, Any]] = []

    await loop._apply_resolved_pending_call(
        ctx,
        None,
        pending_call={
            "id": "c1",
            "name": "mcp_call",
            "arguments": {"server_id": str(world["public"].id), "tool_name": "find_region"},
        },
        approval_status="approved",
        messages=messages,
        tool_log=[],
        aux_idx=0,
        dispatchers=dispatchers,
    )
    assert calls == []
    assert USER_MCP_UNAVAILABLE in messages[0]["content"]
    assert "public-srv" not in messages[0]["content"]


# ---------- 공개 범위 변경 ----------


async def test_set_server_exposure_audits_and_validates(db, world, admin_user) -> None:
    server = world["private"]
    await set_server_exposure(db, server, "all_users", admin_id=admin_user.id)
    await set_server_exposure(db, server, "all_users", admin_id=admin_user.id)  # 변화 없음
    await db.commit()
    (row,) = await _audits("mcp.exposure_change")
    assert row.detail == {"server_id": str(server.id), "from": "admin_only", "to": "all_users"}
    with pytest.raises(ValueError, match="공개 범위"):
        await set_server_exposure(db, server, "everyone", admin_id=admin_user.id)


async def test_exposure_check_constraint(db) -> None:
    from sqlalchemy.exc import IntegrityError

    db.add(MCPServer(name="bad", transport="stdio", endpoint_or_command="x", exposure="public"))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()
    row = MCPServer(name="default", transport="stdio", endpoint_or_command="x")
    db.add(row)
    await db.commit()
    assert row.exposure == "admin_only"


async def test_disabled_server_failure_keeps_the_server_name_in_evidence(
    db, world, admin_user, listed, calls
) -> None:
    """꺼진 서버의 실패 evidence에도 서버 이름이 남는다(기존 동작, biznotes 테스트가 고정)."""
    ctx = _ctx(db, admin_user)
    _, dispatchers = await tool_registry.build_tool_specs(ctx, ["mcp"])
    exposed = next(n for n in dispatchers if n.startswith("mcp_") and "find_region" in n)
    server_name = next(
        s.name for s in world.values() if str(s.id).replace("-", "")[:8] == exposed.split("_")[1]
    )
    await _commit_elsewhere(next(s for s in world.values() if s.name == server_name), enabled=False)
    with pytest.raises(ToolUnavailableError, match="비활성화"):
        await dispatchers[exposed](ctx, exposed, {})
    assert ctx.tool_evidence[-1] == {
        "kind": "mcp", "server": server_name, "tool": "find_region", "status": "failed"
    }


async def test_discovered_tool_calls_are_audited_and_blocked_on_audit_failure(
    db, world, user_a, listed, calls, monkeypatch
) -> None:
    """디스커버리 도구 경로도 `mcp_call`과 같다 — 감사 행을 남기고, 기록 실패면 호출하지 않는다."""
    ctx = _ctx(db, user_a)
    _, dispatchers = await tool_registry.build_tool_specs(ctx, ["mcp"])
    (exposed,) = [n for n in dispatchers if n.startswith("mcp_")]
    assert await dispatchers[exposed](ctx, exposed, {"q": "부산"}) == "ok"
    (started,) = await _audits("mcp.call")
    (result,) = await _audits("mcp.call_result")
    assert started.detail["arg_keys"] == ["q"] and "부산" not in str(started.detail)
    assert result.detail["call_id"] == started.detail["call_id"]

    async def broken(*a: Any, **kw: Any) -> None:
        raise RuntimeError("audit db down")

    monkeypatch.setattr(mcp_exposure, "_audit_isolated", broken)
    with structlog.testing.capture_logs(), pytest.raises(ToolUnavailableError) as error:
        await dispatchers[exposed](ctx, exposed, {"q": "대구"})
    assert str(error.value) == USER_MCP_UNAVAILABLE
    assert len(calls) == 1  # 감사 실패한 두 번째 호출은 나가지 않았다
    assert ctx.tool_evidence[-1]["status"] == "failed"


async def test_subagent_keeps_the_user_audience(db, world, user_a, listed) -> None:
    """서브에이전트 ctx도 일반 사용자 기준이다 — 관리자 전용 서버가 보이지 않는다."""
    child = _ctx(db, user_a).child(uuid.uuid4(), {})
    _, dispatchers = await tool_registry.build_tool_specs(child, ["mcp"])
    assert _servers_of(dispatchers, world) == ["public"]
