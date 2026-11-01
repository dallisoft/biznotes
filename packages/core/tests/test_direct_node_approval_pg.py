# 신규(#153 PR-A). 필요: PostgreSQL·Valkey(PG 테스트).
"""직접 부작용 노드(`http`·`mcp_tool`) 승인 — 실제 그래프 실행·승인 행·소비 CAS로 본다.

- http 호출 수는 mock이 아니라 **로컬 HTTP 서버**가 센다(외부 네트워크 경계만 로컬로 바꾼다).
- MCP는 `mcp_client.call_tool`(외부 프로세스 경계)을 기록형 대역으로 바꾼다.
- 승인 저장·소비·재개는 제품 워커(sl-agents `workers/tasks/agent.py`)의 흐름을 같은 SQL로
  재현한다 — `AgentRunApproval` 삽입 → 소비 CAS(`consumed=false` → true) →
  `react_approval_status` 주입 → `restore_run_context` → `execute_agent(resume_state=…)`.
`create_all` 기반이라 Alembic 마이그레이션 검증이 아니다.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update

from biznotes_core.config import get_settings
from biznotes_core.core.db import get_session_factory
from biznotes_core.models import (
    Agent,
    AgentRun,
    AgentRunApproval,
    MCPServer,
    MCPServerTool,
    Notebook,
    User,
)
from biznotes_core.services import agent_runs
from biznotes_core.services.agents import mcp_client
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import (
    AgentRuntimeError,
    DirectNodeRejectedError,
    DirectNodeRequestChangedError,
    GatedNodeInFanOutError,
)
from biznotes_core.services.agents.graph import execute_agent
from biznotes_core.services.agents.react import approval

K = approval.CheckpointKeys


# ---------- 외부 경계 ----------


class Counter:
    """로컬 HTTP 서버 — 요청 수와 경로를 센다."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bytes]] = []
        self.status = 200
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            head = await reader.readuntil(b"\r\n\r\n")
            line, *headers = head.decode().split("\r\n")
            method, path, _ = line.split(" ", 2)
            length = next(
                (
                    int(h.split(":", 1)[1])
                    for h in headers
                    if h.lower().startswith("content-length")
                ),
                0,
            )
            body = await reader.readexactly(length) if length else b""
            self.requests.append((method, path, body))
            payload = b'{"ok":true}'
            writer.write(
                f"HTTP/1.1 {self.status} OK\r\nContent-Length: {len(payload)}\r\n"
                "Content-Type: application/json\r\nConnection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
            writer.close()

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    def url(self, path: str = "/hook") -> str:
        return f"http://127.0.0.1:{self.port}{path}"


@pytest.fixture
async def http_server(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "ssrf_allow_private", True)  # 로컬 서버만 허용
    counter = Counter()
    await counter.start()
    yield counter
    assert counter.server is not None
    counter.server.close()
    await counter.server.wait_closed()


@pytest.fixture
def mcp_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    recorded: list[tuple[str, dict[str, Any]]] = []

    async def fake(server: Any, tool: str, arguments: dict[str, Any], **_kw: Any) -> str:
        recorded.append((tool, arguments))
        return "mcp-ok"

    monkeypatch.setattr(mcp_client, "call_tool", fake)
    return recorded


@pytest.fixture
def artifacts(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    saved: dict[str, bytes] = {}
    monkeypatch.setattr("biznotes_core.services.storage.ensure_buckets", lambda: None)
    monkeypatch.setattr(
        "biznotes_core.services.storage.put_object",
        lambda bucket, key, data, content_type="": saved.__setitem__(key, data),
    )
    return saved


@pytest.fixture(autouse=True)
def _no_node_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def start(*a: Any, **k: Any) -> None:
        return None

    async def finish(*a: Any, **k: Any) -> bool:
        return True

    monkeypatch.setattr("biznotes_core.services.agents.graph._log_node_start", start)
    monkeypatch.setattr("biznotes_core.services.agents.graph._log_node_finish", finish)


# ---------- 행·흐름 헬퍼 ----------


async def _agent(db: Any, owner: User, scope: str, definition: dict, kind: str = "workflow"):
    agent = Agent(
        name=f"direct-{uuid.uuid4().hex[:6]}",
        kind=kind,
        definition=definition,
        params_schema={},
        scope=scope,
        owner_id=None if scope == "system" else owner.id,
        enabled=True,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


async def _run(db: Any, agent: Agent, user: User) -> AgentRun:
    nb = Notebook(owner_id=user.id, title="t")
    db.add(nb)
    await db.flush()
    run = AgentRun(
        agent_id=agent.id, notebook_id=nb.id, user_id=user.id, status="running", trigger="webhook"
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


def _ctx(db: Any, agent: Agent, run: AgentRun, user: User, params: dict | None = None):
    return RunContext(
        run_id=run.id,
        notebook_id=run.notebook_id,
        db=db,
        params=params or {},
        user_id=user.id,
        agent_id=agent.id,
        trigger="webhook",
    )


async def _persist(db: Any, run: AgentRun, checkpoint: dict[str, Any]) -> AgentRunApproval:
    """제품 `_persist_pending_approval`과 같은 승인 행 저장."""
    now = datetime.now(UTC)
    row = AgentRunApproval(
        run_id=run.id,
        tool_name=checkpoint[K.TOOL_NAME],
        arguments_summary=checkpoint[K.ARGUMENTS_SUMMARY],
        requested_at=now,
        expires_at=now + timedelta(minutes=10),
        checkpoint=json.loads(json.dumps(checkpoint)),
    )
    db.add(row)
    await db.execute(
        update(AgentRun).where(AgentRun.id == run.id).values(status="awaiting_approval")
    )
    await db.commit()
    return row


async def _resolve_and_consume(approval_id: uuid.UUID, outcome: str) -> dict | None:
    """라우터 해결 CAS + 워커 소비 CAS(제품 SQL과 같은 조건). 이미 소비됐으면 None."""
    async with get_session_factory()() as s:
        await s.execute(
            update(AgentRunApproval)
            .where(AgentRunApproval.id == approval_id, AgentRunApproval.status == "pending")
            .values(status=outcome, resolved_at=datetime.now(UTC))
        )
        row = (
            await s.execute(
                update(AgentRunApproval)
                .where(
                    AgentRunApproval.id == approval_id,
                    AgentRunApproval.status.in_(("approved", "rejected")),
                    AgentRunApproval.consumed.is_(False),
                )
                .values(consumed=True)
                .returning(AgentRunApproval.status, AgentRunApproval.checkpoint)
            )
        ).first()
        await s.commit()
    if row is None:
        return None
    status, checkpoint = row
    return {**checkpoint, K.REACT_APPROVAL_STATUS: status}


async def _gate(db, agent, run, user, kind, definition, params=None) -> AgentRunApproval:
    ctx = _ctx(db, agent, run, user, params)
    assert await execute_agent(ctx, kind, definition) == ""
    assert ctx.pending_approval is not None
    return await _persist(db, run, ctx.pending_approval)


async def _resume(db, agent, run, user, kind, definition, checkpoint, params=None) -> RunContext:
    ctx = _ctx(db, agent, run, user, params)
    approval.restore_run_context(ctx, checkpoint)
    await execute_agent(ctx, kind, definition, resume_state=checkpoint[K.GRAPH_STATE])
    return ctx


def _wf(*nodes: dict, edges: list[list[str]] | None = None) -> dict[str, Any]:
    ids = [n["id"] for n in nodes]
    chain = [[src, dst] for src, dst in zip(ids, ids[1:], strict=False)]
    return {"nodes": list(nodes), "edges": edges if edges is not None else chain}


# ---------- 수락 기준 1·2: 승인 전 0회, 승인 1회, 거부 0회, 이중 재개 1회, 컨텍스트 보존 ----------


async def test_approve_once_reject_zero_and_double_resume_once(
    db, user_a: User, http_server: Counter, artifacts: dict
) -> None:
    definition = _wf(
        {"id": "save", "type": "save_artifact", "name": "note.md", "content": "hi"},
        {
            "id": "call",
            "type": "http",
            "url": http_server.url("/hook/{params.id}"),
            "headers": {"Authorization": "Bearer {params.token}"},
            "body": {"id": "{params.id}"},
        },
    )
    params = {"id": "42", "token": "t-secret"}
    agent = await _agent(db, user_a, "user", definition)
    run = await _run(db, agent, user_a)

    ctx = _ctx(db, agent, run, user_a, params)
    ctx.cost_total[0] = 0.75  # 게이트 전 사용량
    ctx.register_source_calls[0] = 2
    assert await execute_agent(ctx, "workflow", definition) == ""
    assert http_server.requests == []  # 승인 전 0회
    checkpoint = ctx.pending_approval
    assert checkpoint[K.TOOL_NAME] == "http:POST"
    assert "t-secret" not in json.dumps(checkpoint[K.ARGUMENTS_SUMMARY])  # 카드에 비밀 없음
    row = await _persist(db, run, checkpoint)

    resumed_checkpoint = await _resolve_and_consume(row.id, "approved")
    resumed = await _resume(
        db, agent, run, user_a, "workflow", definition, resumed_checkpoint, params
    )
    assert [(m, p) for m, p, _ in http_server.requests] == [("POST", "/hook/42")]
    # 게이트 전 RunContext가 그대로 돌아왔다(리뷰 1)
    assert resumed.cost_total[0] == 0.75
    assert resumed.register_source_calls[0] == 2
    assert [a["name"] for a in resumed.artifact_keys] == ["note.md"]
    assert len(artifacts) == 1  # save_artifact는 재개 때 다시 돌지 않았다

    # 같은 승인으로 두 번째 재개 — 소비 CAS가 막는다
    assert await _resolve_and_consume(row.id, "approved") is None
    assert len(http_server.requests) == 1


async def test_reject_runs_zero_and_is_not_absorbed(db, user_a: User, http_server: Counter) -> None:
    """수락 기준 5 — 거부는 retry_policy·continue_on_error로 덮이지 않는다."""
    node = {
        "id": "call",
        "type": "http",
        "url": http_server.url(),
        "retry_policy": {"max_retries": 2, "delay": 0},
        "continue_on_error": True,
    }
    definition = _wf(node, {"id": "after", "type": "constant", "value": {"x": 1}})
    agent = await _agent(db, user_a, "user", definition)
    run = await _run(db, agent, user_a)
    row = await _gate(db, agent, run, user_a, "workflow", definition)

    checkpoint = await _resolve_and_consume(row.id, "rejected")
    with pytest.raises(DirectNodeRejectedError):
        await _resume(db, agent, run, user_a, "workflow", definition, checkpoint)
    assert http_server.requests == []


async def test_changed_request_runs_zero(db, user_a: User, http_server: Counter) -> None:
    """수락 기준 5 — 대기 중 렌더링 결과가 바뀌면(여기서는 사용자 정보) 실행하지 않는다."""
    definition = _wf({"id": "call", "type": "http", "url": http_server.url("/u/{user.name}")})
    agent = await _agent(db, user_a, "user", definition)
    run = await _run(db, agent, user_a)
    ctx = _ctx(db, agent, run, user_a)
    ctx.user_name = "before"
    await execute_agent(ctx, "workflow", definition)
    row = await _persist(db, run, ctx.pending_approval)

    checkpoint = await _resolve_and_consume(row.id, "approved")
    resumed = _ctx(db, agent, run, user_a)
    resumed.user_name = "after"
    approval.restore_run_context(resumed, checkpoint)
    with pytest.raises(DirectNodeRequestChangedError):
        await execute_agent(resumed, "workflow", definition, resume_state=checkpoint[K.GRAPH_STATE])
    assert http_server.requests == []


async def test_approved_failure_is_not_retried(
    db, user_a: User, http_server: Counter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """수락 기준 5 — 승인된 요청의 전송 실패는 재시도하지 않는다(이미 전달됐을 수 있다)."""
    definition = _wf(
        {
            "id": "call",
            "type": "http",
            "url": http_server.url(),
            "retry_policy": {"max_retries": 3, "delay": 0},
        }
    )
    agent = await _agent(db, user_a, "user", definition)
    run = await _run(db, agent, user_a)
    row = await _gate(db, agent, run, user_a, "workflow", definition)
    attempts: list[str] = []

    async def failing(method: str, url: str, **kw: Any) -> Any:
        attempts.append(url)
        raise httpx.ConnectError("boom")

    monkeypatch.setattr("biznotes_core.core.ssrf.ssrf_safe_request", failing)
    checkpoint = await _resolve_and_consume(row.id, "approved")
    with pytest.raises(AgentRuntimeError, match="승인된 HTTP 요청 실패"):
        await _resume(db, agent, run, user_a, "workflow", definition, checkpoint)
    assert len(attempts) == 1


# ---------- 수락 기준 4: kind ----------


async def test_tool_kind_resumes_by_anchor_without_resume_key(
    db, user_a: User, http_server: Counter, artifacts: dict
) -> None:
    """tool kind는 definition에 nodes가 없어 워커가 `_resume`을 넣지 못한다 — 앵커로 재개한다."""
    definition = {"http": {"url": http_server.url("/tool"), "method": "GET"}}
    agent = await _agent(db, user_a, "group", definition, kind="tool")
    run = await _run(db, agent, user_a)
    row = await _gate(db, agent, run, user_a, "tool", definition)
    checkpoint = await _resolve_and_consume(row.id, "approved")
    await _resume(db, agent, run, user_a, "tool", definition, checkpoint)
    assert [(m, p) for m, p, _ in http_server.requests] == [("GET", "/tool")]
    assert len(artifacts) == 1


async def _mcp_server(db: Any, *tools: tuple[str, bool]) -> MCPServer:
    server = MCPServer(
        name=f"m-{uuid.uuid4().hex[:6]}", transport="stdio", endpoint_or_command="x", enabled=True
    )
    db.add(server)
    await db.flush()
    for tool, read_only in tools:
        db.add(
            MCPServerTool(
                server_id=server.id,
                tool_name=tool,
                read_only=read_only,
                approved_at=datetime.now(UTC),
            )
        )
    await db.commit()
    await db.refresh(server)
    return server


async def test_mcp_kind_rules(db, user_a: User, mcp_calls: list, artifacts: dict) -> None:
    """수락 기준 4·8 — mcp kind의 false는 read-only 승인 행일 때만 즉시 실행한다."""
    server = await _mcp_server(db, ("read", True), ("write", False))

    def kind_def(tool: str, **kw: Any) -> dict[str, Any]:
        return {"server_id": str(server.id), "tool_name": tool, "arguments": {}, **kw}

    read = await _agent(db, user_a, "system", kind_def("read", requires_approval=False), "mcp")
    run = await _run(db, read, user_a)
    await execute_agent(_ctx(db, read, run, user_a), "mcp", read.definition)
    assert [c[0] for c in mcp_calls] == ["read"]

    # read_only=False 행은 false여도 게이트 → 승인 후 1회
    write = await _agent(db, user_a, "system", kind_def("write", requires_approval=False), "mcp")
    run = await _run(db, write, user_a)
    row = await _gate(db, write, run, user_a, "mcp", write.definition)
    assert [c[0] for c in mcp_calls] == ["read"]
    checkpoint = await _resolve_and_consume(row.id, "approved")
    await _resume(db, write, run, user_a, "mcp", write.definition, checkpoint)
    assert [c[0] for c in mcp_calls] == ["read", "write"]

    # 승인 행이 없으면 거부(fail closed)
    missing = await _agent(
        db, user_a, "system", kind_def("unknown", requires_approval=False), "mcp"
    )
    run = await _run(db, missing, user_a)
    with pytest.raises(AgentRuntimeError, match="승인한 도구"):
        await execute_agent(_ctx(db, missing, run, user_a), "mcp", missing.definition)
    assert len(mcp_calls) == 2


async def test_precheck_runs_again_after_approval(db, user_a: User, mcp_calls: list) -> None:
    """수락 기준 8 — 대기 중 서버를 끄면 승인해도 0회다."""
    server = await _mcp_server(db, ("write", False))
    definition = {"server_id": str(server.id), "tool_name": "write", "arguments": {}}
    agent = await _agent(db, user_a, "system", definition, "mcp")
    run = await _run(db, agent, user_a)
    row = await _gate(db, agent, run, user_a, "mcp", definition)
    async with get_session_factory()() as s:
        await s.execute(update(MCPServer).where(MCPServer.id == server.id).values(enabled=False))
        await s.commit()
    checkpoint = await _resolve_and_consume(row.id, "approved")
    with pytest.raises(AgentRuntimeError, match="비활성화"):
        await _resume(db, agent, run, user_a, "mcp", definition, checkpoint)
    assert mcp_calls == []


async def test_resumed_mcp_call_carries_the_run_owner_meta(
    db, user_a: User, monkeypatch: pytest.MonkeyPatch, artifacts: dict
) -> None:
    """#162 — 승인 뒤 재개된 호출의 `_meta` 사용자는 재개 ctx의 run 소유자다.

    승인자 신원은 checkpoint·ctx에 들어가지 않으므로, 누가 승인해도 `_meta`는 재개 ctx에서
    만든다."""
    from biznotes_core.services.agents.mcp_client import MCP_META_RUN_ID, MCP_META_USER_ID

    metas: list[dict[str, Any] | None] = []

    async def fake(server: Any, tool: str, arguments: dict[str, Any], **kw: Any) -> str:
        metas.append(kw.get("meta"))
        return "mcp-ok"

    monkeypatch.setattr(mcp_client, "call_tool", fake)
    server = await _mcp_server(db, ("write", False))
    definition = {"server_id": str(server.id), "tool_name": "write", "arguments": {}}
    agent = await _agent(db, user_a, "system", definition, "mcp")
    run = await _run(db, agent, user_a)
    row = await _gate(db, agent, run, user_a, "mcp", definition)
    assert metas == []

    checkpoint = await _resolve_and_consume(row.id, "approved")
    await _resume(db, agent, run, user_a, "mcp", definition, checkpoint)

    assert metas == [{MCP_META_RUN_ID: str(run.id), MCP_META_USER_ID: str(user_a.id)}]


# ---------- 수락 기준 7: false 인정 ----------


async def test_system_false_runs_without_gate_and_user_false_is_gated(
    db, user_a: User, http_server: Counter
) -> None:
    node = {"id": "call", "type": "http", "url": http_server.url(), "requires_approval": False}
    system = await _agent(db, user_a, "system", _wf(node))
    run = await _run(db, system, user_a)
    ctx = _ctx(db, system, run, user_a)
    await execute_agent(ctx, "workflow", system.definition)
    assert ctx.pending_approval is None and len(http_server.requests) == 1

    user_def = await _agent(db, user_a, "user", _wf(node))
    run = await _run(db, user_def, user_a)
    ctx = _ctx(db, user_def, run, user_a)
    await execute_agent(ctx, "workflow", user_def.definition)
    assert ctx.pending_approval is not None and len(http_server.requests) == 1


# ---------- 수락 기준 3: fan-out ----------


async def test_gated_nodes_in_fan_out_fail_before_anything_runs(
    db, user_a: User, http_server: Counter, mcp_calls: list
) -> None:
    server = await _mcp_server(db, ("write", False))
    start = {
        "id": "start",
        "type": "http",
        "url": http_server.url("/first"),
        "requires_approval": False,
    }
    a = {"id": "a", "type": "http", "url": http_server.url("/a")}
    # system false + read_only=False 승인 행 — 정의만 보면 통과하지만 게이트 대상이다(재리뷰 R1)
    b = {
        "id": "b",
        "type": "mcp_tool",
        "server_id": str(server.id),
        "tool_name": "write",
        "arguments": {},
        "requires_approval": False,
    }
    definition = _wf(start, a, b, edges=[["start", "a"], ["start", "b"]])
    agent = await _agent(db, user_a, "system", definition)
    run = await _run(db, agent, user_a)
    with pytest.raises(GatedNodeInFanOutError, match=r"\['a', 'b'\]"):
        await execute_agent(_ctx(db, agent, run, user_a), "workflow", definition)
    assert http_server.requests == [] and mcp_calls == []


async def test_fan_out_gate_is_not_absorbed_by_error_handler(
    db, user_a: User, http_server: Counter
) -> None:
    """error_handler의 보호 대상 안에서도 폴백으로 흡수되지 않는다(재리뷰 R1)."""
    a = {"id": "a", "type": "http", "url": http_server.url("/a")}
    b = {"id": "b", "type": "http", "url": http_server.url("/b")}
    start = {"id": "start", "type": "constant", "value": {}}
    protected = await _agent(
        db, user_a, "user", _wf(start, a, b, edges=[["start", "a"], ["start", "b"]])
    )
    fallback = await _agent(db, user_a, "user", _wf({"id": "c", "type": "constant", "value": {}}))
    parent = await _agent(
        db,
        user_a,
        "user",
        _wf(
            {
                "id": "guard",
                "type": "error_handler",
                "protected_agent_id": str(protected.id),
                "fallback_agent_id": str(fallback.id),
            }
        ),
    )
    run = await _run(db, parent, user_a)
    with pytest.raises(GatedNodeInFanOutError):
        await execute_agent(_ctx(db, parent, run, user_a), "workflow", parent.definition)
    assert http_server.requests == []


# ---------- 수락 기준 9: 승인 만료 sweep ----------


async def test_expire_stale_approvals(db, user_a: User, http_server: Counter) -> None:
    definition = _wf({"id": "call", "type": "http", "url": http_server.url()})
    agent = await _agent(db, user_a, "user", definition)
    stale_run = await _run(db, agent, user_a)
    stale = await _gate(db, agent, stale_run, user_a, "workflow", definition)
    fresh_run = await _run(db, agent, user_a)
    fresh = await _gate(db, agent, fresh_run, user_a, "workflow", definition)
    past = datetime.now(UTC) - timedelta(minutes=1)
    await db.execute(
        update(AgentRunApproval).where(AgentRunApproval.id == stale.id).values(expires_at=past)
    )
    await db.commit()
    assert await agent_runs.has_active_run(db, agent.id, stale_run.notebook_id)

    async with get_session_factory()() as s:
        expired = await agent_runs.expire_stale_approvals(s)
        await s.commit()

    assert [(e.approval_id, e.run_id, e.run_failed) for e in expired] == [
        (stale.id, stale_run.id, True)
    ]
    async with get_session_factory()() as s:
        statuses = dict(
            (await s.execute(select(AgentRunApproval.id, AgentRunApproval.status))).all()
        )
        run_row = await s.get(AgentRun, stale_run.id)
        assert run_row is not None
        assert (run_row.status, run_row.error) == ("failed", agent_runs.APPROVAL_EXPIRED_ERROR)
        assert not await agent_runs.has_active_run(s, agent.id, stale_run.notebook_id)
    assert statuses == {stale.id: "expired", fresh.id: "pending"}

    # 라우터 지연 만료와 경합해도 한쪽만 이긴다 — 이미 만료된 승인은 다시 잡히지 않는다
    async with get_session_factory()() as s:
        assert await agent_runs.expire_stale_approvals(s) == []
        lazy = await s.execute(
            update(AgentRunApproval)
            .where(AgentRunApproval.id == stale.id, AgentRunApproval.status == "pending")
            .values(status="expired")
            .returning(AgentRunApproval.id)
        )
        assert lazy.first() is None
    assert http_server.requests == []


# ---------- save_sources (#403): 관리자 정의가 아니면 게이트, 승인 1회 ----------


@pytest.fixture
def source_writes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """소스 원본 저장·인제스트 발행 대역(외부 경계)."""
    env: dict[str, Any] = {"objects": {}, "published": []}
    monkeypatch.setattr("biznotes_core.services.storage.ensure_buckets", lambda: None)
    monkeypatch.setattr(
        "biznotes_core.services.storage.put_object",
        lambda bucket, key, data, content_type="": env["objects"].__setitem__(key, data),
    )
    monkeypatch.setattr(
        "biznotes_core.services.storage.delete_object",
        lambda bucket, key: env["objects"].pop(key, None),
    )
    monkeypatch.setattr(
        "biznotes_core.services.ingestion.enqueue._publish",
        lambda source_id, task_id, queue=None: env["published"].append(source_id),
    )
    return env


def _save_sources_wf(items: list[dict[str, Any]], namespace: str = "ns") -> dict[str, Any]:
    return _wf(
        {"id": "list", "type": "constant", "value": {"text": json.dumps(items)}},
        {
            "id": "save",
            "type": "save_sources",
            "items": "{list.text}",
            "namespace": namespace,
            "requires_approval": False,  # 사용자 정의라 무시되고 게이트한다
        },
    )


async def _source_refs() -> list[str | None]:
    from biznotes_core.models import Source

    async with get_session_factory()() as s:
        return list((await s.scalars(select(Source.external_ref).order_by(Source.title))).all())


async def test_save_sources_outside_system_is_gated_then_runs_once(
    db, user_a: User, source_writes: dict
) -> None:
    items = [{"title": f"t{i}", "external_id": f"g-{i}", "content": f"본문{i}"} for i in range(7)]
    definition = _save_sources_wf(items)
    agent = await _agent(db, user_a, "user", definition)
    run = await _run(db, agent, user_a)
    row = await _gate(db, agent, run, user_a, "workflow", definition)

    assert await _source_refs() == [] and source_writes["objects"] == {}  # 승인 전 0건
    assert row.tool_name == "save_sources:ns"
    card = row.arguments_summary
    # 카드는 항목 수·앞 5개 제목·대상만 — 본문은 싣지 않는다(카드 정화가 스칼라를 글자로 바꾼다)
    assert (card["count"], card["total"]) == ("7", "7")
    assert card["titles"] == ["t0", "t1", "t2", "t3", "t4"]
    assert card["target"] == str(run.notebook_id)
    assert "본문" not in json.dumps(card, ensure_ascii=False)

    checkpoint = await _resolve_and_consume(row.id, "approved")
    await _resume(db, agent, run, user_a, "workflow", definition, checkpoint)
    assert await _source_refs() == [f"mcp:ns:g-{i}" for i in range(7)]
    assert len(source_writes["published"]) == 7
    # 같은 승인으로 두 번째 재개 — 소비 CAS가 막는다
    assert await _resolve_and_consume(row.id, "approved") is None


async def test_save_sources_changed_definition_after_approval_writes_nothing(
    db, user_a: User, source_writes: dict
) -> None:
    items = [{"title": "t", "external_id": "c-1", "content": "본문"}]
    definition = _save_sources_wf(items)
    agent = await _agent(db, user_a, "user", definition)
    run = await _run(db, agent, user_a)
    row = await _gate(db, agent, run, user_a, "workflow", definition)
    checkpoint = await _resolve_and_consume(row.id, "approved")

    # 대기 중에 정의의 namespace(중복 키)를 바꿨다 — 승인한 요청과 다르다
    changed = _save_sources_wf(items, namespace="ns-2")
    with pytest.raises(DirectNodeRequestChangedError):
        await _resume(db, agent, run, user_a, "workflow", changed, checkpoint)
    assert await _source_refs() == [] and source_writes["objects"] == {}


async def test_save_sources_rule_resumes_after_month_change(
    db, user_a: User, source_writes: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """요청 해시는 규칙 원문이다 — 승인 대기 중에 달이 바뀌어도 같은 요청이다.

    재개한 실행은 새 달의 노트북에 쓴다.
    """
    from biznotes_core.models import Notebook
    from biznotes_core.services.agents.nodes import save_sources

    now = [datetime(2026, 10, 31, 14, 0, tzinfo=UTC)]  # KST 10월 31일 23시
    monkeypatch.setattr(save_sources, "_utcnow", lambda: now[0])
    rule = {"title_template": "모음 {month}", "timezone": "Asia/Seoul"}
    definition = _save_sources_wf([{"title": "t", "external_id": "m-1", "content": "본문"}])
    definition["nodes"][1]["notebook_rule"] = rule
    agent = await _agent(db, user_a, "user", definition)
    run = await _run(db, agent, user_a)
    row = await _gate(db, agent, run, user_a, "workflow", definition)
    assert row.arguments_summary["target"] == rule  # 카드의 대상은 규칙 원문

    now[0] = datetime(2026, 10, 31, 15, 30, tzinfo=UTC)  # KST 11월 1일 0시 30분
    checkpoint = await _resolve_and_consume(row.id, "approved")
    await _resume(db, agent, run, user_a, "workflow", definition, checkpoint)
    async with get_session_factory()() as s:
        titles = set((await s.scalars(select(Notebook.title))).all())
    assert "모음 2026-11" in titles and "모음 2026-10" not in titles
    assert await _source_refs() == ["mcp:ns:m-1"]
