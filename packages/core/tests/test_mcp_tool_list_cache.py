"""MCP `list_tools` 프로세스 캐시 (#147).

일반 사용자 채팅은 매 턴 디스커버리를 돈다. `list_tools` 결과만 run을 넘어 재사용하고,
서버 목록·승인 판정은 run마다 다시 본다. 외부 MCP 서버(`list_tools`)·DB 조회·승인 조회만 대역이다.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import structlog.testing
from conftest import stub_mcp_approvals

from biznotes_core.models import MCPServer
from biznotes_core.services.agents import mcp_client, tool_registry
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.mcp_exposure import unavailable_mcp_tools

USER_ID = uuid.uuid4()
T0 = datetime(2026, 10, 8, tzinfo=UTC)


def _server(name: str, *, exposure: str = "all_users") -> MCPServer:
    """저장된 행처럼 id·updated_at이 채워진 서버(세션에는 붙이지 않는다)."""
    return MCPServer(
        id=uuid.uuid4(),
        name=name,
        transport="streamable_http",
        endpoint_or_command=f"https://mcp.example.test/{name}",
        enabled=True,
        exposure=exposure,
        updated_at=T0,
    )


class World:
    """DB·승인·외부 서버 대역.

    승인은 서버 id → 도구 이름이고, `list_tools` 호출은 서버 이름으로 센다.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.servers: list[MCPServer] = []
        self.approved: dict[Any, frozenset[str]] = {}
        self.listed: dict[str, list[dict[str, Any]]] = {}
        self.failing: set[str] = set()
        self.calls: list[str] = []
        self.now = 1000.0
        world = self

        async def list_tools(server: Any) -> list[dict[str, Any]]:
            world.calls.append(server.name)
            if server.name in world.failing:
                raise TimeoutError("응답 없음")
            return world.listed[server.name]

        async def approved_tools(db: Any, server_ids: Any) -> dict[Any, frozenset[str]]:
            return {sid: world.approved[sid] for sid in server_ids if world.approved.get(sid)}

        monkeypatch.setattr(mcp_client, "list_tools", list_tools)
        stub_mcp_approvals(monkeypatch, approved_tools)
        monkeypatch.setattr(tool_registry.time, "monotonic", lambda: world.now)
        monkeypatch.setattr(tool_registry, "_safe_discovered_mcp_tools", set())

    def add(self, server: MCPServer, tools: list[str], approved: list[str] | None = None) -> None:
        self.servers.append(server)
        self.listed[server.name] = [
            {"name": t, "description": t, "input_schema": {"type": "object", "properties": {}}}
            for t in tools
        ]
        self.approved[server.id] = frozenset(tools if approved is None else approved)

    def ctx(self, *, role: str = "user") -> RunContext:
        world = self

        class Db:
            async def execute(self, stmt: Any) -> Any:
                # 디스커버리 SQL의 조건(켜짐·일반 사용자는 공개 서버만)을 흉내 낸다
                public_only = "exposure" in str(stmt.whereclause)
                rows = [
                    s
                    for s in world.servers
                    if s.enabled and (not public_only or s.exposure == "all_users")
                ]
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

        return RunContext(
            run_id=uuid.uuid4(),
            notebook_id=uuid.uuid4(),
            db=Db(),  # type: ignore[arg-type]
            params={},
            user_id=USER_ID,
            user_role=role,
        )

    async def discover(self, *, role: str = "user") -> tuple[RunContext, list[str]]:
        ctx = self.ctx(role=role)
        with structlog.testing.capture_logs():
            _, dispatchers = await tool_registry.build_tool_specs(ctx, ["mcp"])
        return ctx, sorted(name.split("_", 2)[2] for name in dispatchers)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    return World(monkeypatch)


async def test_next_run_reuses_the_tool_list(world: World) -> None:
    """다음 턴(새 run)은 서버에 다시 붙지 않고 같은 도구를 노출한다."""
    kosis = _server("kosis")
    world.add(kosis, ["search", "meta"])

    _, first = await world.discover()
    _, second = await world.discover()

    assert first == second == ["meta", "search"]
    assert world.calls == ["kosis"]


async def test_approval_and_exposure_are_read_every_run(world: World) -> None:
    """승인 철회와 `admin_only` 전환은 캐시가 살아 있어도 다음 run에 바로 반영된다."""
    kosis = _server("kosis")
    world.add(kosis, ["search", "meta"])
    assert (await world.discover())[1] == ["meta", "search"]

    world.approved[kosis.id] = frozenset({"search"})
    assert (await world.discover())[1] == ["search"]

    kosis.exposure = "admin_only"  # 행 수정이지만 updated_at은 그대로 두어 캐시가 맞게 한다
    assert (await world.discover())[1] == []
    assert (await world.discover(role="admin"))[1] == ["search"]

    world.approved[kosis.id] = frozenset()
    assert (await world.discover(role="admin"))[1] == []
    assert world.calls == ["kosis"]


async def test_server_change_invalidates_the_entry(world: World) -> None:
    """`updated_at`이 바뀌거나, 그대로여도 연결 설정이 바뀌면 다시 조회한다."""
    kosis = _server("kosis")
    world.add(kosis, ["search"])
    await world.discover()

    kosis.updated_at = T0 + timedelta(seconds=1)
    await world.discover()
    assert world.calls == ["kosis", "kosis"]

    for change in (
        {"endpoint_or_command": "https://mcp.example.test/kosis-v2"},
        {"transport": "sse"},
        {"env_encrypted": "새-암호문"},
    ):
        for attr, value in change.items():
            setattr(kosis, attr, value)
        await world.discover()
    assert world.calls == ["kosis"] * 5
    await world.discover()
    assert world.calls == ["kosis"] * 5


async def test_entry_expires_after_ttl(world: World) -> None:
    kosis = _server("kosis")
    world.add(kosis, ["search"])
    await world.discover()

    world.now += tool_registry._MCP_TOOL_LIST_TTL_SECONDS - 0.001
    await world.discover()
    assert world.calls == ["kosis"]

    world.now += 0.001
    world.listed["kosis"].append({"name": "meta", "description": "", "input_schema": {}})
    world.approved[kosis.id] = frozenset({"search", "meta"})
    assert (await world.discover())[1] == ["meta", "search"]
    assert world.calls == ["kosis", "kosis"]


async def test_failure_is_cached_briefly_and_still_reported(world: World) -> None:
    """응답 없는 서버는 짧은 TTL 동안 다시 기다리지 않고, 그동안도 실패로 집계된다."""
    archhub, kosis = _server("archhub"), _server("kosis")
    world.add(archhub, ["find_region"])
    world.add(kosis, ["search"])
    world.failing.add("archhub")

    for _ in range(2):
        ctx, exposed = await world.discover()
        assert exposed == ["search"]
        assert ctx._mcp_discovery_outcome["failed_approved_servers"] == 1  # type: ignore[attr-defined]
    assert world.calls == ["archhub", "kosis"]

    world.failing.clear()
    world.now += tool_registry._MCP_TOOL_LIST_FAILURE_TTL_SECONDS
    ctx, exposed = await world.discover()
    assert exposed == ["find_region", "search"]
    assert ctx._mcp_discovery_outcome["failed_approved_servers"] == 0  # type: ignore[attr-defined]
    assert world.calls == ["archhub", "kosis", "archhub"]


async def test_cached_failure_keeps_the_unavailable_reason(world: World) -> None:
    archhub = _server("archhub")
    world.add(archhub, ["find_region"])
    world.failing.add("archhub")

    for _ in range(2):
        ctx, exposed = await world.discover()
        reasons = await unavailable_mcp_tools(ctx, ["mcp"], [])
        assert exposed == [] and reasons == {"mcp": "discovery_failed"}
    assert world.calls == ["archhub"]


async def test_cancelled_lookup_is_not_cached(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """취소는 실패가 아니다 — 음성 캐시에 남지 않아 다음 run이 다시 조회한다."""
    kosis = _server("kosis")
    world.add(kosis, ["search"])
    original = mcp_client.list_tools

    async def cancelled(server: Any) -> list[dict[str, Any]]:
        world.calls.append(server.name)
        raise asyncio.CancelledError

    monkeypatch.setattr(mcp_client, "list_tools", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await world.discover()
    monkeypatch.setattr(mcp_client, "list_tools", original)
    assert (await world.discover())[1] == ["search"]
    assert world.calls == ["kosis", "kosis"]


async def test_unsaved_server_is_not_cached(world: World) -> None:
    """`updated_at`이 없는 행(저장 전)은 지문이 없어 매번 조회한다."""
    draft = _server("draft")
    draft.updated_at = None  # type: ignore[assignment]
    world.add(draft, ["search"])

    await world.discover()
    await world.discover()
    assert world.calls == ["draft", "draft"]


async def test_runs_do_not_share_mutable_tool_data(world: World) -> None:
    """한 run이 스펙의 parameters를 바꿔도 캐시와 다음 run은 원래 스키마를 받는다."""
    kosis = _server("kosis")
    world.add(kosis, ["search"])

    for _ in range(3):  # 조회·저장 → 적중 → 적중 사본을 바꾼 뒤 다시 적중
        ctx = world.ctx()
        specs, _ = await tool_registry.build_tool_specs(ctx, ["mcp"])
        params = specs[0]["function"]["parameters"]
        assert params == {"type": "object", "properties": {}}
        params["additionalProperties"] = False  # 프로바이더 어댑터의 strict 변환을 흉내 낸다
    assert world.calls == ["kosis"]
