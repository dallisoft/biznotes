"""MCP `tools/call`의 호출자 컨텍스트 `_meta` (#162).

계획 `docs/plans/mcp-caller-meta/plan.md`의 수락 기준을 DB 없이 확인한다.

`call_tool`·`open_session`·stdio 서버는 실제다. 서버는 `fixtures/mcp_meta/echo_meta_server.py`이고,
받은 `params._meta`와 `arguments`를 그대로 돌려준다. 대역으로 두는 것은 바깥 계층뿐이다.
- 정책 조회: 가짜 DB(`get`·`execute`)와 `approved_tools`
- LLM: `complete_with_tools`
- 감사 행 쓰기: `audit_user_mcp_*`
"""

import dataclasses
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import stub_mcp_approvals

from biznotes_core.services.agents import mcp_client, mcp_exposure, tool_registry
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.mcp_client import MCP_META_RUN_ID, MCP_META_USER_ID, caller_meta
from biznotes_core.services.agents.nodes import llm as llm_node
from biznotes_core.services.agents.nodes.composite import _exec_mcp_tool
from biznotes_core.services.agents.react import loop
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult

FIXTURES = Path(__file__).parent / "fixtures"
ECHO = FIXTURES / "mcp_meta" / "echo_meta_server.py"
PROTOCOL_SERVER = FIXTURES / "mcp" / "protocol_server.py"
SERVER_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
ADMIN_ID = uuid.uuid4()
# LLM·정의가 인자로 넣을 수 있는 위조 시도 — `_meta`에 섞이면 안 된다
SPOOF = {"user_id": "someone-else", MCP_META_USER_ID: "spoofed", "_meta": {MCP_META_USER_ID: "x"}}


def _server(transport: str = "stdio", *, exposure: str = "admin_only") -> Any:
    return SimpleNamespace(
        id=SERVER_ID,
        name="echo",
        enabled=True,
        exposure=exposure,
        transport=transport,
        endpoint_or_command=f"{sys.executable} {ECHO}",
        env_encrypted=None,
    )


class FakeDb:
    """정책 조회 대역 — 서버 1개, 승인된 read-only 도구, 시스템 정의 에이전트."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self.agent = SimpleNamespace(
            id=uuid.uuid4(), enabled=True, scope="system", owner_id=None, kind="workflow",
            definition={"nodes": [], "edges": []}, skill_version_id=None,
        )

    async def get(self, model: Any, key: Any, **kw: Any) -> Any:
        if model.__name__ == "Agent":
            return self.agent if key == self.agent.id else None
        return self.server if key == self.server.id else None

    async def execute(self, stmt: Any) -> Any:
        row = SimpleNamespace(read_only=True)
        return SimpleNamespace(
            scalar_one_or_none=lambda: row,
            scalars=lambda: SimpleNamespace(all=lambda: [self.server]),
        )


def _ctx(db: FakeDb, *, user_id: uuid.UUID | None, role: str) -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,  # type: ignore[arg-type]
        params={},
        user_id=user_id,
        user_role=role,
        agent_id=db.agent.id,
    )


@pytest.fixture(autouse=True)
def _policy(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    async def approved(db: Any, server_ids: Any) -> dict[Any, frozenset[str]]:
        return {SERVER_ID: frozenset({"whoami"})}

    async def available(db: Any) -> bool:
        return True

    audits: dict[str, list[Any]] = {"call": [], "result": []}

    async def audit_call(ctx: Any, server: Any, tool: str, args: Any) -> uuid.UUID:
        audits["call"].append(tool)
        return uuid.uuid4()

    async def audit_result(ctx: Any, server: Any, tool: str, status: str, call_id: Any) -> None:
        audits["result"].append(status)

    stub_mcp_approvals(monkeypatch, approved)
    monkeypatch.setattr(mcp_exposure, "user_mcp_tools_available", available)
    monkeypatch.setattr(mcp_exposure, "audit_user_mcp_call", audit_call)
    monkeypatch.setattr(mcp_exposure, "audit_user_mcp_result", audit_result)
    monkeypatch.setattr(tool_registry, "_safe_discovered_mcp_tools", set())
    return audits


def _expected(ctx: RunContext) -> dict[str, str]:
    meta = {MCP_META_RUN_ID: str(ctx.run_id)}
    if ctx.user_id is not None:
        meta[MCP_META_USER_ID] = str(ctx.user_id)
    return meta


# ── caller_meta 규칙 ─────────────────────────────────────────────────────


def test_keys_are_reverse_dns_namespaced() -> None:
    assert MCP_META_RUN_ID == "io.github.dalli.biznotes/run_id"
    assert MCP_META_USER_ID == "io.github.dalli.biznotes/user_id"


@pytest.mark.parametrize("transport", ["stdio", "sse", "streamable_http"])
@pytest.mark.parametrize("user_id", [USER_ID, None], ids=["user-run", "system-run"])
def test_user_id_goes_only_to_stdio_and_never_as_placeholder(
    transport: str, user_id: uuid.UUID | None
) -> None:
    ctx = _ctx(FakeDb(_server(transport)), user_id=user_id, role="user")

    meta = caller_meta(ctx, _server(transport))

    assert meta[MCP_META_RUN_ID] == str(ctx.run_id)
    if transport == "stdio" and user_id is not None:
        assert meta[MCP_META_USER_ID] == str(user_id)
    else:
        assert MCP_META_USER_ID not in meta
    assert "None" not in meta.values()


def test_missing_run_id_is_omitted_not_stringified() -> None:
    ctx = dataclasses.replace(_ctx(FakeDb(_server()), user_id=USER_ID, role="user"), run_id=None)  # type: ignore[arg-type]

    assert caller_meta(ctx, _server()) == {MCP_META_USER_ID: str(USER_ID)}


def test_child_and_branch_contexts_keep_the_run_user() -> None:
    db = FakeDb(_server())
    ctx = _ctx(db, user_id=USER_ID, role="user")
    child = ctx.child(uuid.uuid4(), {})
    branch = dataclasses.replace(ctx, agent_id=uuid.uuid4())

    assert caller_meta(child, _server()) == caller_meta(ctx, _server()) == _expected(ctx)
    assert caller_meta(branch, _server()) == _expected(ctx)


# ── 실행 경로 4곳 — 실제 stdio 서버 ─────────────────────────────────────


@pytest.mark.parametrize(
    ("user_id", "role"), [(USER_ID, "user"), (None, "user")], ids=["user-run", "system-run"]
)
async def test_mcp_tool_direct_node(user_id: uuid.UUID | None, role: str) -> None:
    ctx = _ctx(FakeDb(_server()), user_id=user_id, role=role)
    node = {
        "id": "call",
        "type": "mcp_tool",
        "server_id": str(SERVER_ID),
        "tool_name": "whoami",
        "arguments": SPOOF,
        "requires_approval": False,
    }

    out = json.loads((await _exec_mcp_tool(ctx, node, {}))["text"])

    assert out["meta"] == _expected(ctx)
    assert out["arguments"] == SPOOF  # 인자는 그대로 간다 — `_meta`와 섞이지 않는다


@pytest.mark.parametrize(
    ("user_id", "role", "audits"),
    [(ADMIN_ID, "admin", 0), (None, "user", 0), (USER_ID, "user", 1)],
    ids=["admin", "system-run", "user-audience"],
)
async def test_react_mcp_call_tool(
    _policy: dict[str, list[Any]], user_id: uuid.UUID | None, role: str, audits: int
) -> None:
    ctx = _ctx(FakeDb(_server(exposure="all_users")), user_id=user_id, role=role)

    raw = await llm_node._dispatch_builtin_tool(
        ctx, "mcp_call", {"server_id": str(SERVER_ID), "tool_name": "whoami", "arguments": SPOOF}
    )
    out = json.loads(raw)

    assert out["meta"] == _expected(ctx)
    assert out["arguments"] == SPOOF
    assert len(_policy["call"]) == audits


async def _discovered(ctx: RunContext) -> tuple[str, Any]:
    _, dispatchers = await tool_registry.build_tool_specs(ctx, ["mcp"])
    [name] = [n for n in dispatchers if n.endswith("_whoami")]
    return name, dispatchers[name]


@pytest.mark.parametrize(
    ("user_id", "role"), [(ADMIN_ID, "admin"), (USER_ID, "user")], ids=["admin", "user"]
)
async def test_react_discovered_mcp_tool(user_id: uuid.UUID, role: str) -> None:
    ctx = _ctx(FakeDb(_server(exposure="all_users")), user_id=user_id, role=role)
    name, dispatch = await _discovered(ctx)

    out = json.loads(await dispatch(ctx, name, SPOOF))

    assert out["meta"] == _expected(ctx)
    assert out["arguments"] == SPOOF
    # 같은 run 캐시의 디스패처를 서브에이전트 ctx로 불러도 호출 시점 ctx의 사용자다
    child = ctx.child(uuid.uuid4(), {})
    assert json.loads(await dispatch(child, name, {}))["meta"] == _expected(ctx)


async def test_llm_node_tools_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(FakeDb(_server()), user_id=ADMIN_ID, role="admin")
    tool_results: list[str] = []

    async def model(db: Any, model_id: Any, messages: Any, tools: Any, **kw: Any) -> Any:
        tool_results.extend(m["content"] for m in messages if m.get("role") == "tool")
        if tool_results:
            return ToolTurnResult(
                content="끝", assistant_message={"role": "assistant", "content": "끝"}
            )
        [name] = [t["function"]["name"] for t in tools if t["function"]["name"].endswith("_whoami")]
        call = {
            "id": "c1",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(SPOOF)},
        }
        return ToolTurnResult(
            tool_calls=[ToolCall(id="c1", name=name, arguments=SPOOF)],
            assistant_message={"role": "assistant", "content": None, "tool_calls": [call]},
        )

    async def quiet(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(gateway_llm, "complete_with_tools", model)
    monkeypatch.setattr(loop, "_log_node_start", quiet)
    monkeypatch.setattr(loop, "_log_node_finish", quiet)

    await llm_node._exec_llm(ctx, {"id": "gen", "type": "llm", "prompt": "q", "tools": ["mcp"]}, {})

    [result] = tool_results
    expected = json.dumps({"meta": _expected(ctx), "arguments": SPOOF})
    assert expected in result


# ── wire: SDK와 독립적인 원시 JSON-RPC 서버 ─────────────────────────────


def _wire_tool_calls(trace: Path) -> list[dict[str, Any]]:
    calls = []
    for line in trace.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event["direction"] != "client-to-server":
            continue
        message = json.loads(event["wire"])
        if message.get("method") == "tools/call":
            calls.append(message["params"])
    return calls


async def test_wire_carries_meta_only_when_given(tmp_path: Path) -> None:
    from biznotes_core.core.crypto import encrypt

    trace = tmp_path / "trace.jsonl"
    server = SimpleNamespace(
        transport="stdio",
        endpoint_or_command=f"{sys.executable} {PROTOCOL_SERVER} --revision 2025-11-25",
        env_encrypted=encrypt(json.dumps({"MCP_TRACE_FILE": str(trace)})),
    )
    ctx = _ctx(FakeDb(_server()), user_id=USER_ID, role="user")
    arguments = {"text": "wire-byte"}

    # `_meta`를 읽지 않는 서버의 결과는 그대로다
    assert await mcp_client.call_tool(server, "echo", arguments) == "wire-byte"
    meta = caller_meta(ctx, server)  # type: ignore[arg-type]
    assert await mcp_client.call_tool(server, "echo", arguments, meta=meta) == "wire-byte"
    ctx_system = dataclasses.replace(ctx, user_id=None)
    meta_system = caller_meta(ctx_system, server)  # type: ignore[arg-type]
    await mcp_client.call_tool(server, "echo", arguments, meta=meta_system)

    plain, with_meta, system = _wire_tool_calls(trace)
    assert "_meta" not in plain
    assert with_meta["_meta"] == _expected(ctx)  # progressToken 등 다른 키가 없다
    assert system["_meta"] == {MCP_META_RUN_ID: str(ctx.run_id)}
    assert plain["arguments"] == with_meta["arguments"] == system["arguments"] == arguments


@pytest.mark.parametrize("transport", ["sse", "streamable_http"])
async def test_call_sites_drop_user_id_for_http_servers(
    monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    """호출 지점이 HTTP 계열 서버에는 사용자 키를 빼고 넘긴다(재리뷰 m2).

    HTTP 서버를 띄우지 않으려고 `call_tool`만 기록형으로 바꾼다. wire 형식은 위 stdio
    테스트가 본다."""
    metas: list[Any] = []

    async def record(server: Any, tool: str, arguments: Any, **kw: Any) -> str:
        metas.append(kw["meta"])
        return "{}"

    monkeypatch.setattr(mcp_client, "call_tool", record)
    ctx = _ctx(FakeDb(_server(transport, exposure="all_users")), user_id=ADMIN_ID, role="admin")
    node = {
        "id": "call",
        "type": "mcp_tool",
        "server_id": str(SERVER_ID),
        "tool_name": "whoami",
        "arguments": {},
        "requires_approval": False,
    }

    await _exec_mcp_tool(ctx, node, {})
    await llm_node._dispatch_builtin_tool(
        ctx, "mcp_call", {"server_id": str(SERVER_ID), "tool_name": "whoami", "arguments": {}}
    )
    await tool_registry._make_mcp_dispatcher(SERVER_ID, "whoami")(ctx, "mcp_x_whoami", {})

    assert metas == [{MCP_META_RUN_ID: str(ctx.run_id)}] * 3
