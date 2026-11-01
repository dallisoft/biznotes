"""MCP 차단 신호와 역할별 노출 판정 (#139 단계 C, docs/plans/mcp-exposure/).

허용한 MCP 도구가 이 실행에서 막히면 LLM system 조항이 붙고 `ctx.unavailable_tools`에 사유가 남는다.
도구 타임라인(이벤트·`tool_calls`)은 바뀌지 않는다. 막히지 않은 실행의 프롬프트도 그대로다.
모델·디스커버리·실행 로그만 대역이다.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import structlog.testing
from conftest import stub_mcp_approvals

from biznotes_core.services.agents import mcp_exposure, tool_registry
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError
from biznotes_core.services.agents.mcp_exposure import (
    MCP_UNAVAILABLE_CLAUSES,
    MCPExposure,
    mcp_audience,
    mcp_exposure_for,
    unavailable_mcp_tools,
)
from biznotes_core.services.agents.react import loop
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import ToolTurnResult

USER_ID = uuid.uuid4()
ADMIN_ID = uuid.uuid4()
ROLE_DENIED = MCP_UNAVAILABLE_CLAUSES["role_denied"]
NO_APPROVED = MCP_UNAVAILABLE_CLAUSES["no_approved_tools"]
DISCOVERED = "mcp_0a1b2c3d_find_region"


def _ctx(*, user_id: uuid.UUID | None, role: str = "user") -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=None,  # type: ignore[arg-type]
        params={},
        user_id=user_id,
        user_role=role,
    )


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """도구를 부르지 않고 바로 답하는 모델. 턴마다 받은 messages·도구 이름을 기록한다."""
    turns: list[dict[str, Any]] = []

    async def fake(db: Any, model_id: Any, messages: Any, tools: Any, **kw: Any) -> Any:
        turns.append(
            {
                "messages": [dict(m) for m in messages],
                "tools": [t["function"]["name"] for t in tools],
            }
        )
        return ToolTurnResult(
            content="답변",
            assistant_message={"role": "assistant", "content": "답변"},
            usage={"total_tokens": 1},
        )

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake)
    return turns


@pytest.fixture(autouse=True)
def _quiet_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def noop(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(loop, "_log_node_start", noop)
    monkeypatch.setattr(loop, "_log_node_finish", noop)


class Discovery:
    """디스커버리 대역 — `names`에 넣은 이름만 노출하고 호출 횟수를 센다."""

    def __init__(self) -> None:
        self.names: list[str] = []
        self.calls = 0

    async def __call__(self, ctx: Any) -> Any:
        self.calls += 1
        specs = [
            {"type": "function", "function": {"name": n, "description": "", "parameters": {}}}
            for n in self.names
        ]
        return specs, {n: (lambda *a, **kw: None) for n in self.names}


@pytest.fixture
def discovered(monkeypatch: pytest.MonkeyPatch) -> Discovery:
    fake = Discovery()
    monkeypatch.setattr(tool_registry, "_discover_mcp_tools", fake)
    return fake


ARCHHUB_TOOLS = frozenset({"find_region", "building_data"})


@pytest.fixture(autouse=True)
def _no_public_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """공개(`all_users`) 서버 없음 — 일반 사용자 `mcp_call` 노출 조회 대역(#139 A)."""

    async def none(db: Any) -> bool:
        return False

    monkeypatch.setattr(mcp_exposure, "user_mcp_tools_available", none)


@pytest.fixture
def approvals(monkeypatch: pytest.MonkeyPatch) -> dict[str, frozenset[str]]:
    """DB 승인 대역 — 서버 **이름** → 승인 도구. 판정 함수가 넘기는 서버 id를 이름으로 되짚는다.

    승인 조회 SQL은 `tests/test_mcp_server_tools_pg.py`가 실제 PostgreSQL로 검증한다.
    """
    by_name: dict[str, frozenset[str]] = {"archhub": ARCHHUB_TOOLS}

    async def fake(db: Any, server_ids: Any) -> dict[Any, frozenset[str]]:
        names = {s.id: s.name for s in _SERVERS}
        return {
            sid: by_name[names[sid]] for sid in server_ids if by_name.get(names.get(sid, ""))
        }

    stub_mcp_approvals(monkeypatch, fake)
    return by_name


_SERVERS: list[Any] = []


def _react(allowed: list[str]) -> dict[str, Any]:
    return {"id": "react", "type": "react_agent", "goal": "목표", "allowed_tools": allowed}


def _system(turn: dict[str, Any]) -> str:
    first = turn["messages"][0]
    assert first["role"] == "system"
    return str(first["content"])


# ---------- react 노드 ----------


async def test_regular_user_gets_a_clause_and_no_timeline_event(model, discovered) -> None:
    events: list[tuple[str, str, str, dict[str, Any]]] = []

    async def on_tool_call(tool_call_id: str, name: str, status: str, args: dict) -> None:
        events.append((tool_call_id, name, status, args))

    ctx = _ctx(user_id=USER_ID)
    ctx.on_tool_call = on_tool_call
    result = await loop.run_react_node(ctx, _react(["rag_search", "mcp"]), {})

    assert result["text"] == "답변"
    assert model[0]["tools"] == ["rag_search"]
    system = _system(model[0])
    assert system.count(ROLE_DENIED) == 1 and NO_APPROVED not in system
    assert ctx.unavailable_tools == {"mcp": "role_denied"}
    assert events == [] and ctx.tool_calls == []  # 채팅마다 가짜 호출이 생기지 않는다
    # 일반 사용자는 공개 서버만 디스커버리한다(#139 A) — 공개 서버가 없으면 사유는 권한이다
    assert discovered.calls == 1


async def test_mcp_call_is_signalled_separately(model, discovered) -> None:
    ctx = _ctx(user_id=USER_ID)
    await loop.run_react_node(ctx, _react(["rag_search", "mcp", "mcp_call"]), {})

    assert ctx.unavailable_tools == {"mcp": "role_denied", "mcp_call": "role_denied"}
    assert _system(model[0]).count(ROLE_DENIED) == 1  # 같은 사유의 조항은 한 번


@pytest.mark.parametrize(
    ("user_id", "role"), [(ADMIN_ID, "admin"), (None, "user")], ids=["admin", "system-run"]
)
async def test_admin_and_system_runs_are_unchanged_when_tools_are_exposed(
    model, discovered, user_id, role
) -> None:
    discovered.names.append(DISCOVERED)
    plain = _ctx(user_id=user_id, role=role)
    await loop.run_react_node(plain, _react(["rag_search"]), {})
    ctx = _ctx(user_id=user_id, role=role)
    await loop.run_react_node(ctx, _react(["rag_search", "mcp", "mcp_call"]), {})

    assert model[1]["tools"] == ["rag_search", "mcp_call", DISCOVERED]
    assert _system(model[1]) == _system(model[0])  # 조항 없음
    assert ctx.unavailable_tools == {}


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (None, "no_approved_tools"),
        ({"servers": 0, "approved_servers": 0, "failed_approved_servers": 0}, "no_servers"),
        ({"servers": 2, "approved_servers": 0, "failed_approved_servers": 0}, "no_approved_tools"),
        ({"servers": 2, "approved_servers": 1, "failed_approved_servers": 1}, "discovery_failed"),
        ({"servers": 2, "approved_servers": 1, "failed_approved_servers": 0}, "no_approved_tools"),
    ],
)
async def test_admin_with_no_exposed_tools_gets_the_outage_or_approval_reason(
    model, discovered, monkeypatch, outcome, reason
) -> None:
    """노출 0개를 장애·미등록·미승인으로 나눈다 — 장애를 권한 문제로 안내하지 않는다(리뷰 C-3)."""

    async def fake(ctx: Any) -> Any:
        if outcome is not None:
            ctx._mcp_discovery_outcome = outcome
        return [], {}

    monkeypatch.setattr(tool_registry, "_discover_mcp_tools", fake)
    ctx = _ctx(user_id=ADMIN_ID, role="admin")
    await loop.run_react_node(ctx, _react(["rag_search", "mcp"]), {})

    system = _system(model[0])
    assert MCP_UNAVAILABLE_CLAUSES[reason] in system and ROLE_DENIED not in system
    assert ctx.unavailable_tools == {"mcp": reason}


async def test_only_mcp_fails_with_the_reason(model, discovered) -> None:
    ctx = _ctx(user_id=USER_ID)
    with structlog.testing.capture_logs():
        with pytest.raises(AgentRuntimeError, match="사용 가능한 도구가 없습니다.*role_denied"):
            await loop.run_react_node(ctx, _react(["mcp"]), {})
    assert model == []


async def test_resume_records_the_reason_without_repeating_the_clause(
    model, discovered, monkeypatch: pytest.MonkeyPatch
) -> None:
    """실제 게이트 checkpoint로 재개한다 — 조항은 messages에 1회뿐이고 사유는 다시 남는다."""
    from biznotes_core.services.agents import runtime
    from biznotes_core.services.agents.errors import ApprovalRequired
    from biznotes_core.services.agents.react import approval
    from biznotes_core.services.gateway.llm import ToolCall

    args = {"url": "https://example.com/pay", "method": "POST"}
    script = [
        ToolTurnResult(
            tool_calls=[ToolCall(id="c1", name="http_request", arguments=args)],
            assistant_message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "http_request", "arguments": "{}"},
                    }
                ],
            },
            usage={"total_tokens": 1},
        )
    ]
    real_model = gateway_llm.complete_with_tools

    async def scripted(db: Any, model_id: Any, messages: Any, tools: Any, **kw: Any) -> Any:
        if script:
            model.append({"messages": [dict(m) for m in messages], "tools": []})
            return script.pop(0)
        return await real_model(db, model_id, messages, tools, **kw)

    async def dispatch(ctx: Any, name: str, a: dict[str, Any]) -> str:
        return "done"

    monkeypatch.setattr(gateway_llm, "complete_with_tools", scripted)
    monkeypatch.setattr(runtime, "_dispatch_builtin_tool", dispatch)
    node = _react(["http_request", "mcp"])
    with pytest.raises(ApprovalRequired) as gate:
        await loop.run_react_node(_ctx(user_id=USER_ID), node, {})
    checkpoint = gate.value.checkpoint
    checkpoint[approval.CheckpointKeys.REACT_APPROVAL_STATUS] = "rejected"

    resumed = _ctx(user_id=USER_ID)
    approval.restore_run_context(resumed, checkpoint)
    await loop.run_react_node(resumed, {**node, "_resume": checkpoint}, {})

    after = model[-1]["messages"]
    assert _system(model[-1]).count(ROLE_DENIED) == 1
    assert after[0] == model[0]["messages"][0]  # 재개는 system 메시지를 바꾸지 않는다
    assert resumed.unavailable_tools == {"mcp": "role_denied"}


async def test_cached_discovery_keeps_the_same_reason(
    monkeypatch: pytest.MonkeyPatch, approvals
) -> None:
    """같은 ctx에서 디스커버리 캐시가 맞아도 사유가 같다."""
    from biznotes_core.services.agents import mcp_client

    monkeypatch.setattr(tool_registry, "_safe_discovered_mcp_tools", set())

    class Db:
        async def execute(self, stmt: Any) -> Any:
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [_server("x")]))

    async def list_tools(server: Any) -> list[dict[str, Any]]:
        raise TimeoutError

    monkeypatch.setattr(mcp_client, "list_tools", list_tools)
    ctx = _ctx(user_id=ADMIN_ID, role="admin")
    ctx.db = Db()  # type: ignore[assignment]
    with structlog.testing.capture_logs():
        for _ in range(2):
            _, dispatchers = await tool_registry.build_tool_specs(ctx, ["mcp"])
            reasons = await unavailable_mcp_tools(ctx, ["mcp"], dispatchers)
            assert reasons == {"mcp": "no_approved_tools"}  # 응답 없는 서버가 승인 서버가 아니다


# ---------- llm 노드 경로(guards=None) ----------


async def test_llm_node_path_records_but_keeps_the_authored_prompt(model, discovered) -> None:
    """llm 노드의 프롬프트는 작성자가 정한다 — 조항을 넣지 않고 사유만 남긴다(리뷰 C-5)."""
    ctx = _ctx(user_id=USER_ID)
    for messages in (
        [{"role": "user", "content": "질문"}],
        [{"role": "system", "content": "작성자 지침"}, {"role": "user", "content": "질문"}],
    ):
        original = [dict(m) for m in messages]
        await loop._exec_llm_with_tools(
            ctx, {"id": "l", "type": "llm"}, messages, None, ["rag_search", "mcp"]
        )
        assert model[-1]["messages"] == original
    assert ctx.unavailable_tools == {"mcp": "role_denied"}
    assert ctx.tool_calls == []


async def test_subagent_block_is_visible_on_the_parent(model, discovered) -> None:
    parent = _ctx(user_id=USER_ID)
    child = parent.child(uuid.uuid4(), {})
    await loop.run_react_node(child, _react(["rag_search", "mcp"]), {})
    assert parent.unavailable_tools == {"mcp": "role_denied"}


# ---------- 판정 함수 ----------


async def test_unavailable_mcp_tools_reasons() -> None:
    user, admin = _ctx(user_id=USER_ID), _ctx(user_id=ADMIN_ID, role="admin")
    assert await unavailable_mcp_tools(user, ["rag_search"], []) == {}
    assert await unavailable_mcp_tools(user, None, []) == {}
    assert await unavailable_mcp_tools(user, ["mcp", "mcp_call"], []) == {
        "mcp": "role_denied",
        "mcp_call": "role_denied",
    }
    assert await unavailable_mcp_tools(admin, ["mcp", "mcp_call"], ["mcp_call"]) == {
        "mcp": "no_approved_tools"  # 내장 mcp_call은 디스커버리 도구가 아니다
    }
    assert await unavailable_mcp_tools(admin, ["mcp"], [DISCOVERED]) == {}


def _server(name: str, *, enabled: bool = True, exposure: str = "admin_only") -> Any:
    server = SimpleNamespace(id=uuid.uuid4(), name=name, enabled=enabled, exposure=exposure)
    _SERVERS.append(server)
    return server


async def test_exposure_for_each_audience(approvals) -> None:
    archhub = _server("archhub")
    approved = ARCHHUB_TOOLS

    assert mcp_audience("admin", ADMIN_ID) == "admin"
    assert mcp_audience("user", None) == "admin"  # 사용자 없는 시스템 실행
    assert mcp_audience("user", USER_ID) == "user"
    exposure = await mcp_exposure_for(None, archhub, audience="admin")  # type: ignore[arg-type]
    assert exposure == MCPExposure(exposed=True, reason="ok", tools=approved)
    exposure = await mcp_exposure_for(None, archhub, audience="user")  # type: ignore[arg-type]
    assert exposure == MCPExposure(exposed=False, reason="role_denied")
    disabled = _server("archhub", enabled=False)
    exposure = await mcp_exposure_for(None, disabled, audience="admin")  # type: ignore[arg-type]
    assert exposure == MCPExposure(exposed=False, reason="server_disabled")
    exposure = await mcp_exposure_for(None, _server("kosis"), audience="admin")  # type: ignore[arg-type]
    assert exposure == MCPExposure(exposed=False, reason="no_approved_tools")
    public = _server("archhub", exposure="all_users")
    exposure = await mcp_exposure_for(None, public, audience="user")  # type: ignore[arg-type]
    assert exposure == MCPExposure(exposed=True, reason="ok", tools=approved)


async def test_exposure_matches_discovery(monkeypatch: pytest.MonkeyPatch, approvals) -> None:
    """`mcp_exposure_for`가 노출한다고 한 서버·도구만 디스커버리가 노출한다."""
    from biznotes_core.services.agents import mcp_client

    # 실제 디스커버리는 프로세스 전역 안전 집합에 이름을 넣는다 — 다른 테스트로 새지 않게 격리한다
    monkeypatch.setattr(tool_registry, "_safe_discovered_mcp_tools", set())

    servers = [
        _server("archhub"),
        _server("archhub", exposure="all_users"),
        _server("kosis", exposure="all_users"),
        _server("off", enabled=False, exposure="all_users"),
    ]
    listed = {
        "archhub": [{"name": "find_region"}, {"name": "delete_building"}],
        "kosis": [{"name": "search"}],
        "off": [{"name": "x"}],
    }

    class Db:
        async def execute(self, stmt: Any) -> Any:
            # 디스커버리 SQL의 조건(켜짐·일반 사용자는 공개 서버만)을 흉내 낸다
            public_only = "exposure" in str(stmt.whereclause)
            rows = [
                s for s in servers if s.enabled and (not public_only or s.exposure == "all_users")
            ]
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def list_tools(server: Any) -> list[dict[str, Any]]:
        return listed[server.name]

    monkeypatch.setattr(mcp_client, "list_tools", list_tools)
    for role, user_id in (("admin", ADMIN_ID), ("user", USER_ID)):
        ctx = _ctx(user_id=user_id, role=role)
        ctx.db = Db()  # type: ignore[assignment]
        _, dispatchers = await tool_registry.build_tool_specs(ctx, ["mcp"])
        exposed_tools = sorted(name.split("_", 2)[2] for name in dispatchers)
        expected: list[str] = []
        for server in servers:
            audience = mcp_audience(role, user_id)
            exposure = await mcp_exposure_for(ctx.db, server, audience=audience)
            expected += [t["name"] for t in listed[server.name] if t["name"] in exposure.tools]
        assert exposed_tools == sorted(expected), role
        # 관리자는 두 archhub, 일반 사용자는 공개 archhub 하나에서만 find_region을 본다
        assert exposed_tools == (
            ["find_region", "find_region"] if role == "admin" else ["find_region"]
        )


async def test_real_discovery_reports_an_unresponsive_approved_server(
    monkeypatch: pytest.MonkeyPatch, approvals
) -> None:
    from biznotes_core.services.agents import mcp_client

    # 실제 디스커버리는 프로세스 전역 안전 집합에 이름을 넣는다 — 다른 테스트로 새지 않게 격리한다
    monkeypatch.setattr(tool_registry, "_safe_discovered_mcp_tools", set())

    servers = [_server("archhub"), _server("kosis")]

    class Db:
        async def execute(self, stmt: Any) -> Any:
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: servers))

    async def list_tools(server: Any) -> list[dict[str, Any]]:
        if server.name == "archhub":
            raise TimeoutError("응답 없음")
        return [{"name": "search"}]

    monkeypatch.setattr(mcp_client, "list_tools", list_tools)
    ctx = _ctx(user_id=ADMIN_ID, role="admin")
    ctx.db = Db()  # type: ignore[assignment]
    with structlog.testing.capture_logs():
        _, dispatchers = await tool_registry.build_tool_specs(ctx, ["mcp"])

    assert dispatchers == {}
    assert await unavailable_mcp_tools(ctx, ["mcp"], dispatchers) == {"mcp": "discovery_failed"}
    servers.clear()
    fresh = _ctx(user_id=ADMIN_ID, role="admin")
    fresh.db = Db()  # type: ignore[assignment]
    _, dispatchers = await tool_registry.build_tool_specs(fresh, ["mcp"])
    assert await unavailable_mcp_tools(fresh, ["mcp"], dispatchers) == {"mcp": "no_servers"}
