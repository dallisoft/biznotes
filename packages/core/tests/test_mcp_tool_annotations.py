"""MCP 도구 title·annotations 보존 (#146, docs/plans/mcp-tool-annotations/).

실제 wire 경로는 `test_mcp_compatibility.py`가 본다. 여기서는 SDK `Tool`로 표시 이름 규칙을
확인하고, 서버가 자기 신고한 힌트가 디스커버리 노출·스펙에 끼어들지 않는지 본다.
DB·승인 조회·외부 서버(`list_tools`)만 대역이다.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import structlog.testing
from conftest import stub_mcp_approvals
from mcp.types import Tool

from biznotes_core.services.agents import mcp_client, tool_registry
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.mcp_client import _tool_annotations, _tool_title


def _tool(**fields: Any) -> Tool:
    return Tool.model_validate({"name": "drop_all", "inputSchema": {"type": "object"}, **fields})


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"title": "Search", "annotations": {"title": "Legacy"}}, "Search"),
        ({"title": "", "annotations": {"title": "Legacy"}}, "Legacy"),
        ({"title": "   ", "annotations": {"title": "Legacy"}}, "Legacy"),
        ({"annotations": {"title": ""}}, None),
        ({"annotations": None}, None),
        ({}, None),
    ],
)
def test_title_prefers_tool_title_and_skips_blank_values(
    fields: dict[str, Any], expected: str | None
) -> None:
    assert _tool_title(_tool(**fields)) == expected


def test_unknown_annotation_fields_are_dropped() -> None:
    tool = _tool(annotations={"readOnlyHint": False, "vendorHint": True, "title": "x"})
    assert _tool_annotations(tool) == {
        "readOnlyHint": False,
        "destructiveHint": None,
        "idempotentHint": None,
        "openWorldHint": None,
    }


async def test_self_reported_hints_do_not_affect_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """승인되지 않은 도구는 `readOnlyHint=True`를 신고해도 노출되지 않고, 스펙에 힌트가 없다."""
    server = SimpleNamespace(id=uuid.uuid4(), name="kosis", enabled=True, exposure="admin_only")
    hints = {"readOnlyHint": True, "destructiveHint": False}

    async def list_tools(_server: Any) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "title": "Search",
                "description": "d",
                "input_schema": {"type": "object", "properties": {}},
                "annotations": {**dict.fromkeys(mcp_client._TOOL_ANNOTATION_HINTS), **hints},
            }
            for name in ("search", "drop_all")
        ]

    async def approved_tools(db: Any, server_ids: Any) -> dict[Any, frozenset[str]]:
        return {server.id: frozenset({"search"})}

    class Db:
        async def execute(self, stmt: Any) -> Any:
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [server]))

    monkeypatch.setattr(mcp_client, "list_tools", list_tools)
    stub_mcp_approvals(monkeypatch, approved_tools)
    monkeypatch.setattr(tool_registry, "_safe_discovered_mcp_tools", set())
    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=Db(),  # type: ignore[arg-type]
        params={},
        user_id=None,
        user_role="admin",
    )
    with structlog.testing.capture_logs():
        specs, dispatchers = await tool_registry.build_tool_specs(ctx, ["mcp"])

    assert [name.split("_", 2)[2] for name in dispatchers] == ["search"]
    function = specs[0]["function"]
    assert set(function) == {"name", "description", "parameters"}
    assert function["description"] == "d"  # 서버 신고 title을 설명에 섞지 않는다
    assert "Hint" not in repr(specs)
