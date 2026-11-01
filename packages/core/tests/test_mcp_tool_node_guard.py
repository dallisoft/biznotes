"""사용자 정의는 관리자 자격증명의 MCP 도구를 부르지 못한다 (#77).

계획 `docs/plans/mcp-tool-node-guard/plan.md`의 수락 기준을 DB 없이 확인한다.

- `mcp_tool` 노드(그리고 `kind="mcp"`): 실행 중인 정의의 작성 주체가 기준이다(D1) — system만 허용.
- `mcp_call` 도구: 디스커버리와 같은 경계(D7) — admin·시스템 실행 + read-only allowlist.
- 저장 시점 헬퍼 `find_forbidden_mcp_tool_nodes`.

가짜 세션은 `get(model, key)`를 모델별로 구분해 서버·에이전트 조회 횟수를 센다. #153부터
`mcp_tool`은 `MCPServerTool` 승인 행과 HITL 게이트를 거친다 — 이 파일은 작성 주체 규칙(#77)을 보므로
승인 행(read_only)을 두고 노드를 `requires_approval: false`로 둔다. 게이트 자체는
`test_direct_node_approval.py`가 본다. 분기 세션 안의
중첩과 삭제 경합은 `tests/test_mcp_tool_node_guard_pg.py`가 실제 행으로 확인한다.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import structlog.testing

from biznotes_core.services.agents import graph, mcp_client, tool_registry
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError, ToolUnavailableError
from biznotes_core.services.agents.nodes import llm as llm_node
from biznotes_core.services.agents.nodes.composite import _exec_mcp_tool
from biznotes_core.services.agents.policy import find_forbidden_mcp_tool_nodes
from biznotes_core.services.agents.react import loop

USER_ID = uuid.uuid4()
ADMIN_ID = uuid.uuid4()
SERVER_ID = uuid.uuid4()


class FakeDb:
    """`get(model, key)`를 모델 이름별 저장소로 나누고, 조회한 모델 이름을 기록한다."""

    def __init__(self) -> None:
        self.agents: dict[uuid.UUID, Any] = {}
        self.servers: dict[uuid.UUID, Any] = {}
        self.gets: list[str] = []
        self.approved_read_only = True

    async def execute(self, stmt: Any) -> Any:
        """`MCPServerTool` 승인 행 조회 대역 — 승인된 read-only 도구로 답한다."""
        row = SimpleNamespace(read_only=self.approved_read_only)
        return SimpleNamespace(scalar_one_or_none=lambda: row)

    async def get(self, model: Any, key: Any, **kw: Any) -> Any:
        self.gets.append(model.__name__)
        store = self.agents if model.__name__ == "Agent" else self.servers
        return store.get(key)

    def add_agent(self, scope: str, definition: dict[str, Any] | None = None) -> Any:
        agent = SimpleNamespace(
            id=uuid.uuid4(),
            enabled=True,
            scope=scope,
            owner_id=USER_ID if scope != "system" else None,
            kind="workflow",
            definition=definition or {"nodes": [], "edges": []},
            skill_version_id=None,
        )
        self.agents[agent.id] = agent
        return agent

    def add_server(self, name: str = "vacation", *, enabled: bool = True) -> Any:
        server = SimpleNamespace(
            id=SERVER_ID, name=name, enabled=enabled, exposure="admin_only", transport="stdio"
        )
        self.servers[server.id] = server
        return server


@pytest.fixture(autouse=True)
def _no_public_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """공개(`all_users`) 서버 없음 — 일반 사용자 `mcp_call` 노출 조회의 대역(#139 A).

    공개 서버가 있을 때의 노출·호출은 `tests/test_mcp_user_exposure_pg.py`가 실제 행으로 본다.
    """
    from biznotes_core.services.agents import mcp_exposure

    async def none(db: Any) -> bool:
        return False

    monkeypatch.setattr(mcp_exposure, "user_mcp_tools_available", none)


@pytest.fixture
def db() -> FakeDb:
    d = FakeDb()
    d.add_server()
    return d


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    """`call_tool` 대역 — 서버 이름·도구·인자를 기록한다."""
    recorded: list[tuple[str, str, dict[str, Any]]] = []

    async def fake(server: Any, tool: str, arguments: dict[str, Any], **_kw: Any) -> str:
        recorded.append((server.name, tool, arguments))
        return "ok"

    monkeypatch.setattr(mcp_client, "call_tool", fake)
    return recorded


def _ctx(db: FakeDb, *, agent_id: uuid.UUID | None, user_id: uuid.UUID | None, role: str = "user"):
    return RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,  # type: ignore[arg-type]
        params={},
        user_id=user_id,
        user_role=role,
        agent_id=agent_id,
    )


def _node(**kw: Any) -> dict[str, Any]:
    return {
        "id": "call",
        "type": "mcp_tool",
        "server_id": str(SERVER_ID),
        "tool_name": "vacation_balance",
        "arguments": {"user_id": "{user.id}", "year": "{params.year}"},
        "requires_approval": False,
        **kw,
    }


STATE = {"user": {"id": str(USER_ID)}, "params": {"year": "2026"}}


# ── mcp_tool 노드: 정의의 작성 주체(D1) ──────────────────────────────────


async def test_user_scope_definition_is_rejected_before_any_lookup(
    db: FakeDb, calls: list[Any]
) -> None:
    """수락 기준 1·9 — 서버 조회 전에 거부해 서버 존재 여부가 새지 않는다."""
    agent = db.add_agent("user")

    with pytest.raises(AgentRuntimeError, match="system"):
        await _exec_mcp_tool(_ctx(db, agent_id=agent.id, user_id=USER_ID), _node(), STATE)

    assert calls == []
    assert "MCPServer" not in db.gets


async def test_system_definition_runs_for_a_regular_user(db: FakeDb, calls: list[Any]) -> None:
    """수락 기준 2 — 시드 "내 연차 잔여일 확인" 형태(`{user.id}` 인자)가 그대로 동작한다."""
    agent = db.add_agent("system")

    out = await _exec_mcp_tool(_ctx(db, agent_id=agent.id, user_id=USER_ID), _node(), STATE)

    assert out == {"text": "ok"}
    assert calls == [("vacation", "vacation_balance", {"user_id": str(USER_ID), "year": "2026"})]


@pytest.mark.parametrize(
    ("user_id", "role"),
    [(ADMIN_ID, "admin"), (None, "user")],
    ids=["admin-runs-user-definition", "system-run-of-user-definition"],
)
async def test_executor_privilege_does_not_open_a_user_definition(
    db: FakeDb, calls: list[Any], user_id: uuid.UUID | None, role: str
) -> None:
    """수락 기준 4 — 관리자가 사용자 정의를 실행해도(혼동된 대리인) 거부한다."""
    agent = db.add_agent("user")

    with pytest.raises(AgentRuntimeError):
        ctx = _ctx(db, agent_id=agent.id, user_id=user_id, role=role)
        await _exec_mcp_tool(ctx, _node(), STATE)

    assert calls == []


@pytest.mark.parametrize(
    ("user_id", "role", "allowed"),
    [(ADMIN_ID, "admin", True), (None, "user", True), (USER_ID, "user", False)],
    ids=["admin", "system-run", "regular-user"],
)
async def test_run_without_a_definition_falls_back_to_executor_privilege(
    db: FakeDb, calls: list[Any], user_id: uuid.UUID | None, role: str, allowed: bool
) -> None:
    """수락 기준 5(a) — 귀속할 정의가 없는 ctx만 실행 권한(admin·시스템 실행)으로 판정한다.

    #153: 정의가 없으면 `requires_approval: false`를 인정하지 않으므로 통과한 실행도
    승인 대기로 멈춘다.
    """
    from biznotes_core.services.agents.errors import ApprovalRequired

    ctx = _ctx(db, agent_id=None, user_id=user_id, role=role)

    if allowed:
        with pytest.raises(ApprovalRequired):
            await _exec_mcp_tool(ctx, _node(), STATE)
        assert calls == []
    else:
        with pytest.raises(AgentRuntimeError):
            await _exec_mcp_tool(ctx, _node(), STATE)
        assert calls == []


async def test_missing_definition_row_is_rejected_even_for_admin(
    db: FakeDb, calls: list[Any]
) -> None:
    """수락 기준 5(b) — `agent_id`가 있는데 행이 없으면(삭제 경합) 실행자와 무관하게 거부한다."""
    ctx = _ctx(db, agent_id=uuid.uuid4(), user_id=ADMIN_ID, role="admin")

    with pytest.raises(AgentRuntimeError):
        await _exec_mcp_tool(ctx, _node(), STATE)

    assert calls == []


async def test_invalid_server_id_still_reports_the_policy_error_first(
    db: FakeDb, calls: list[Any]
) -> None:
    """수락 기준 9 — 잘못된 `server_id`여도 정책 오류가 먼저 난다(서버 형식으로 탐지하지 못한다)."""
    agent = db.add_agent("user")

    with pytest.raises(AgentRuntimeError, match="system"):
        await _exec_mcp_tool(
            _ctx(db, agent_id=agent.id, user_id=USER_ID), _node(server_id="not-a-uuid"), STATE
        )


async def test_node_fields_that_pick_the_tool_are_not_rendered(
    db: FakeDb, calls: list[Any]
) -> None:
    """수락 기준 13 — `server_id`·`tool_name`은 렌더링하지 않아 `{params.*}`가 바꿀 수 없다."""
    agent = db.add_agent("system")
    state = {**STATE, "params": {"tool": "delete_everything", "year": "2026"}}

    await _exec_mcp_tool(
        _ctx(db, agent_id=agent.id, user_id=USER_ID), _node(tool_name="{params.tool}"), state
    )

    assert [c[1] for c in calls] == ["{params.tool}"]


async def test_kind_mcp_agents_follow_the_same_rule(db: FakeDb, calls: list[Any]) -> None:
    """수락 기준 6 — `kind="mcp"`도 `mcp_tool` 노드로 정규화되어 같은 규칙을 탄다."""
    nodes, _ = graph.kind_to_nodes(
        "mcp",
        {
            "server_id": str(SERVER_ID),
            "tool_name": "word_count",
            "arguments": {},
            "requires_approval": False,
        },
    )
    node = nodes[0]
    assert node["type"] == "mcp_tool"
    system = db.add_agent("system")
    user = db.add_agent("user")

    await _exec_mcp_tool(_ctx(db, agent_id=system.id, user_id=USER_ID), node, {})
    with pytest.raises(AgentRuntimeError):
        await _exec_mcp_tool(_ctx(db, agent_id=user.id, user_id=USER_ID), node, {})

    assert [c[1] for c in calls] == ["word_count"]


async def test_child_context_judges_the_called_definition(db: FakeDb, calls: list[Any]) -> None:
    """수락 기준 3 — `child()`는 `agent_id`를 호출된 정의로 바꾼다(callee 기준)."""
    user, system = db.add_agent("user"), db.add_agent("system")
    from_user = _ctx(db, agent_id=user.id, user_id=USER_ID)
    from_system = _ctx(db, agent_id=system.id, user_id=USER_ID)

    await _exec_mcp_tool(from_user.child(system.id, {}), _node(), STATE)
    with pytest.raises(AgentRuntimeError):
        await _exec_mcp_tool(from_system.child(user.id, {}), _node(), STATE)

    assert len(calls) == 1


# ── 실제 `execute_agent` 경로: 중첩·재시도·흡수 ─────────────────────────


@pytest.fixture
def no_node_log(monkeypatch: pytest.MonkeyPatch) -> None:
    async def noop(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(graph, "_log_node_start", noop)
    monkeypatch.setattr(graph, "_log_node_finish", noop)


def _mcp_definition(**node_kw: Any) -> dict[str, Any]:
    return {"nodes": [_node(**node_kw)], "edges": []}


async def test_nested_sub_workflow_is_judged_by_the_callee(
    db: FakeDb, calls: list[Any], no_node_log: None
) -> None:
    """수락 기준 3 — 사용자 부모→system 정의는 허용, system 부모→사용자 정의는 거부(callee 기준)."""
    system = db.add_agent("system", _mcp_definition())
    user = db.add_agent("user", _mcp_definition())
    call = lambda target: {  # noqa: E731
        "nodes": [{"id": "sub", "type": "sub_workflow", "workflow_id": str(target.id)}],
        "edges": [],
    }
    user_parent = db.add_agent("user", call(system))
    system_parent = db.add_agent("system", call(user))

    await graph.execute_agent(
        _ctx(db, agent_id=user_parent.id, user_id=USER_ID), "workflow", user_parent.definition
    )
    assert len(calls) == 1

    with pytest.raises(AgentRuntimeError, match="system"):
        await graph.execute_agent(
            _ctx(db, agent_id=system_parent.id, user_id=USER_ID),
            "workflow",
            system_parent.definition,
        )
    assert len(calls) == 1


async def test_retry_and_continue_on_error_never_reach_the_server(
    db: FakeDb, calls: list[Any], no_node_log: None
) -> None:
    """수락 기준 12 — 재시도와 `continue_on_error`가 이 실패를 감싸도 `call_tool`은 0회다."""
    agent = db.add_agent("user")
    ctx = _ctx(db, agent_id=agent.id, user_id=USER_ID)

    with pytest.raises(AgentRuntimeError):
        await graph.execute_agent(
            ctx, "workflow", _mcp_definition(retry_policy={"max_retries": 2, "delay": 0})
        )
    await graph.execute_agent(ctx, "workflow", _mcp_definition(continue_on_error=True))

    assert calls == []


async def test_duplicate_of_a_system_definition_is_rejected(
    db: FakeDb, calls: list[Any], no_node_log: None
) -> None:
    """수락 기준 14 — system 정의를 복제한 사용자 사본(scope=user)은 같은 정의여도 거부된다."""
    original = db.add_agent("system", _mcp_definition())
    copy = db.add_agent("user", original.definition)

    await graph.execute_agent(
        _ctx(db, agent_id=original.id, user_id=USER_ID), "workflow", original.definition
    )
    with pytest.raises(AgentRuntimeError):
        await graph.execute_agent(
            _ctx(db, agent_id=copy.id, user_id=USER_ID), "workflow", copy.definition
        )

    assert len(calls) == 1


# ── 저장 시점 헬퍼(수락 기준 8) ───────────────────────────────────────────


def test_finder_reports_nodes_sorted_and_unique() -> None:
    nodes: list[Any] = [
        {"id": "b", "type": "mcp_tool"},
        {"id": "a", "type": "react_agent", "allowed_tools": ["rag_search", "mcp_call"]},
        {"id": "b", "type": "mcp_tool"},
        {"id": "a", "type": "mcp_tool"},
    ]

    assert find_forbidden_mcp_tool_nodes(nodes, "user") == [
        ("a", "mcp_call"),
        ("a", "mcp_tool"),
        ("b", "mcp_tool"),
    ]


def test_finder_returns_nothing_for_system_scope() -> None:
    nodes = [
        {"id": "a", "type": "mcp_tool"},
        {"id": "b", "type": "react_agent", "allowed_tools": ["mcp_call"]},
    ]

    assert find_forbidden_mcp_tool_nodes(nodes, "system") == []


@pytest.mark.parametrize("scope", [None, "user", "group", "SYSTEM", "", "unknown"])
def test_finder_treats_every_non_system_scope_as_forbidden(scope: str | None) -> None:
    assert find_forbidden_mcp_tool_nodes([{"id": "a", "type": "mcp_tool"}], scope) == [
        ("a", "mcp_tool")
    ]


def test_finder_ignores_malformed_nodes_and_keeps_ambiguous_ids_apart() -> None:
    nodes: list[Any] = [
        "not-a-node",
        {"id": "x", "type": "react_agent", "allowed_tools": "mcp_call"},  # 리스트가 아니다
        {"id": "y", "type": "react_agent", "allowed_tools": ["http_request"]},
        {"id": "z", "type": "llm", "tools": ["mcp_call"]},  # llm 노드는 #70이 다룬다
        {"id": "r: x", "type": "mcp_tool"},  # id에 ':'가 있어도 모호하지 않다
        {"type": "mcp_tool"},  # id가 없다
        {"id": 7, "type": "mcp_tool"},  # 문자열이 아닌 id
    ]

    assert find_forbidden_mcp_tool_nodes(nodes, "user") == [
        ("", "mcp_tool"),
        ("7", "mcp_tool"),
        ("r: x", "mcp_tool"),
    ]


# ── mcp_call 도구(D7, 수락 기준 11) ─────────────────────────────────────


def _dispatch(ctx: RunContext, **args: Any) -> Any:
    return llm_node._dispatch_builtin_tool(ctx, "mcp_call", args)


async def test_regular_user_does_not_get_mcp_call_exposed(db: FakeDb) -> None:
    ctx = _ctx(db, agent_id=None, user_id=USER_ID)

    with structlog.testing.capture_logs() as logs:
        specs, dispatchers = await tool_registry.build_tool_specs(ctx, ["rag_search", "mcp_call"])

    assert [s["function"]["name"] for s in specs] == ["rag_search"]
    assert "mcp_call" not in dispatchers
    assert any("mcp_call" in e["event"] for e in logs if e["log_level"] == "warning")


@pytest.mark.parametrize(
    ("user_id", "role"), [(ADMIN_ID, "admin"), (None, "user")], ids=["admin", "system-run"]
)
async def test_admin_and_system_runs_still_get_mcp_call(
    db: FakeDb, user_id: uuid.UUID | None, role: str
) -> None:
    specs, dispatchers = await tool_registry.build_tool_specs(
        _ctx(db, agent_id=None, user_id=user_id, role=role), ["mcp_call"]
    )

    assert [s["function"]["name"] for s in specs] == ["mcp_call"]
    assert "mcp_call" in dispatchers


async def test_mcp_call_dispatch_gives_a_regular_user_one_message(
    db: FakeDb, calls: list[Any]
) -> None:
    """일반 사용자에게 admin_only 서버와 없는 서버는 같은 메시지다 — 존재를 탐지할 수 없다(#139 A).

    `server_id` 형식 오류는 서버 존재와 무관해 기존 문구 그대로다.
    """
    from biznotes_core.services.agents.mcp_exposure import USER_MCP_UNAVAILABLE

    ctx = _ctx(db, agent_id=None, user_id=USER_ID)
    messages = []
    for server_id in (str(SERVER_ID), str(uuid.uuid4())):  # admin_only 서버, 없는 서버
        with pytest.raises(ToolUnavailableError) as denied:
            await _dispatch(ctx, server_id=server_id, tool_name="find_region", arguments={})
        messages.append(str(denied.value))
    assert messages == [USER_MCP_UNAVAILABLE, USER_MCP_UNAVAILABLE]
    assert "vacation" not in "".join(messages)
    out = await _dispatch(ctx, server_id="not-a-uuid", tool_name="find_region", arguments={})
    assert out == "MCP server_id가 올바르지 않습니다."
    assert calls == []


async def test_mcp_call_dispatch_for_admin_requires_the_allowlist(
    db: FakeDb, calls: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from biznotes_core.services.agents import mcp_exposure

    async def approved(db_: Any, server_ids: Any) -> dict[Any, frozenset[str]]:
        # 승인 조회 SQL은 tests/test_mcp_server_tools_pg.py가 검증한다
        return {SERVER_ID: frozenset({"find_region"})} if SERVER_ID in server_ids else {}

    monkeypatch.setattr(mcp_exposure, "approved_tools", approved)
    db.servers[SERVER_ID] = SimpleNamespace(
        id=SERVER_ID, name="archhub", enabled=True, transport="streamable_http"
    )
    ctx = _ctx(db, agent_id=None, user_id=ADMIN_ID, role="admin")

    with pytest.raises(ToolUnavailableError, match="read-only"):
        await _dispatch(ctx, server_id=str(SERVER_ID), tool_name="delete_building", arguments={})
    out = await _dispatch(
        ctx, server_id=str(SERVER_ID), tool_name="find_region", arguments={"q": "서울"}
    )

    assert out == "ok"
    assert calls == [("archhub", "find_region", {"q": "서울"})]


async def test_mcp_call_dispatch_keeps_the_server_id_message_for_admins(db: FakeDb) -> None:
    """관리자·시스템 run의 기존 `server_id` 오류 메시지는 그대로다."""
    ctx = _ctx(db, agent_id=None, user_id=ADMIN_ID, role="admin")

    out = await _dispatch(ctx, server_id="not-a-uuid", tool_name="find_region", arguments={})

    assert out == "MCP server_id가 올바르지 않습니다."


async def test_saved_approval_for_mcp_call_cannot_be_resumed_by_a_regular_user(
    db: FakeDb, calls: list[Any]
) -> None:
    """배포 전에 저장된 승인 대기의 재개 — 재개는 디스패처를 새로 만들므로 도구가 없다.
    승인자가 admin이어도 실행자는 run 사용자다."""
    ctx = _ctx(db, agent_id=None, user_id=USER_ID)
    _, dispatchers = await tool_registry.build_tool_specs(ctx, ["mcp_call"])
    tool_log: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []

    await loop._apply_resolved_pending_call(
        ctx,
        None,
        pending_call={
            "id": "c1",
            "name": "mcp_call",
            "arguments": {"server_id": str(SERVER_ID), "tool_name": "find_region"},
        },
        approval_status="approved",
        messages=messages,
        tool_log=tool_log,
        aux_idx=0,
        dispatchers=dispatchers,
    )

    assert calls == []
    assert messages[0]["content"] == "알 수 없는 도구: mcp_call"
    assert [c["status"] for c in ctx.tool_calls] == ["failed"]


async def test_react_node_with_only_mcp_call_fails_for_a_regular_user(
    db: FakeDb, calls: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """일반 사용자 run에서 `mcp_call`만 둔 노드는 도구가 없어 실패한다(사유는 경고 로그)."""

    async def noop(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(loop, "_log_node_start", noop)
    monkeypatch.setattr(loop, "_log_node_finish", noop)
    node = {"id": "r", "type": "react_agent", "goal": "목표", "allowed_tools": ["mcp_call"]}

    ctx = _ctx(db, agent_id=None, user_id=USER_ID)
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(AgentRuntimeError, match="도구가 없"):
            await loop.run_react_node(ctx, node, {})

    assert any("mcp_call" in e["event"] for e in logs if e["log_level"] == "warning")
    assert calls == []
