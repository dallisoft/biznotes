# 신규(#147). 필요: PostgreSQL·Valkey(PG 테스트).
"""MCP `list_tools` 프로세스 캐시를 실제 세션·행으로 본다 (#147).

`list_tools`(외부 MCP 서버)만 대역이다. 같은 세션에서 행을 고쳐 flush하면 서버가 채우는
`updated_at`이 만료된다 — 디스커버리가 그 값을 읽어도 지연 로딩(MissingGreenlet) 없이 다시
조회하는지, 다른 세션이 커밋한 설정 변경이 다음 run의 캐시를 무효로 만드는지 확인한다.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import structlog.testing
from sqlalchemy import inspect

from biznotes_core.core.db import get_session_factory
from biznotes_core.models import MCPServer, MCPServerTool, User
from biznotes_core.services.agents import mcp_client, tool_registry
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.mcp_tool_spec import mcp_tool_spec_hash


@pytest.fixture(autouse=True)
def _isolated_safe_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_registry, "_safe_discovered_mcp_tools", set())


SEARCH = {"name": "search", "description": "", "input_schema": {}}


@pytest.fixture
def listed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`list_tools` 대역 — 부를 때의 endpoint를 기록한다."""
    seen: list[str] = []

    async def list_tools(server: Any) -> list[dict[str, Any]]:
        seen.append(server.endpoint_or_command)
        return [SEARCH]

    monkeypatch.setattr(mcp_client, "list_tools", list_tools)
    return seen


async def _discover(db: Any, user: User) -> list[str]:
    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=user.id,
        user_role="user",
    )
    with structlog.testing.capture_logs():
        _, dispatchers = await tool_registry.build_tool_specs(ctx, ["mcp"])
    return sorted(dispatchers)


async def test_cache_follows_server_rows_across_sessions(db, user_a, listed) -> None:
    server = MCPServer(
        name=f"kosis-{uuid.uuid4().hex[:6]}",
        transport="streamable_http",
        endpoint_or_command="https://mcp.example.test/v1",
        exposure="all_users",
    )
    db.add(server)
    await db.flush()
    db.add(
        MCPServerTool(
            server_id=server.id,
            tool_name="search",
            read_only=True,
            approved_at=datetime.now(UTC),
            approved_spec_hash=mcp_tool_spec_hash(SEARCH),  # #293
        )
    )
    await db.commit()
    server_id = server.id

    exposed = await _discover(db, user_a)
    assert len(exposed) == 1
    async with get_session_factory()() as other:  # 다음 턴 = 새 세션
        assert await _discover(other, user_a) == exposed
    assert listed == ["https://mcp.example.test/v1"]

    # 헬스체크 기록처럼 행을 고쳐 flush하면 서버가 채우는 updated_at이 만료된다
    server.last_health_check = datetime.now(UTC)
    await db.flush()
    assert "updated_at" in inspect(server).expired_attributes
    assert await _discover(db, user_a) == exposed
    assert len(listed) == 2
    await db.commit()

    async with get_session_factory()() as admin:  # 관리자가 다른 세션에서 설정을 바꾼다
        row = await admin.get(MCPServer, server_id)
        assert row is not None
        row.endpoint_or_command = "https://mcp.example.test/v2"
        await admin.commit()
    async with get_session_factory()() as fresh:
        assert await _discover(fresh, user_a) == exposed
        assert await _discover(fresh, user_a) == exposed
    assert listed[2:] == ["https://mcp.example.test/v2"]
