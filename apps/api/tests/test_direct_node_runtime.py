"""직접 노드 HITL 런타임 통합 — 제품 워커 `_run` 경유 (core-v0.11.0-upgrade AC-2.7 · D-4 · D-6).

run 행을 만들고 제품 워커의 `_run(run_id)`를 그대로 부른다 — 클레임 CAS·실행 직전 인가 재검사·
`RunContext(agent_id=…)` 구성·코어 `execute_agent`·게이트(`direct_node_needs_gate`)·
`_persist_pending_approval`까지 실제 경로다. 대체는 외부 경계뿐이다:

- HTTP: `ssrf_safe_request`(관문) + DNS(`getaddrinfo` — 테스트 호스트만 공인 IP)
- MCP: `mcp_client.call_tool`(데모 서버) 또는 `mcp_client.open_session`(휴가 서버 — 메모리 전송으로
  **실제** 서버 스크립트에 연결해 `_meta` 인가까지 지난다)
- 스토리지(`save_artifact`)·벡터 검색·LLM: conftest `_mock_runtime_deps`·`mock_llm`

샘플 정의는 실제 시드 함수가 만든 것을 쓴다(AC-2.4의 최종 상태가 런타임에서 의도대로 도는지).
"""

import json
import socket
import uuid

import httpx
import pytest
from biznotes_core.core.db import async_session_factory
from biznotes_core.models import (
    Agent,
    AgentRun,
    AgentRunApproval,
    MCPServer,
    Notebook,
)
from biznotes_core.services.agents.mcp_client import MCP_META_RUN_ID, MCP_META_USER_ID
from sqlalchemy import select, update

from app.workers.tasks.agent import _run

_PUBLIC_HOSTS = {"api.example.com", "catfact.ninja", "httpbin.org"}


@pytest.fixture
def http_stub(monkeypatch):
    """HTTP 경계 — 요청을 기록하고 고정 응답. 외부 네트워크 호출 0."""
    real = socket.getaddrinfo

    def fake_getaddrinfo(host, *args, **kwargs):
        if host in _PUBLIC_HOSTS:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        return real(host, *args, **kwargs)

    monkeypatch.setattr("biznotes_core.core.ssrf.socket.getaddrinfo", fake_getaddrinfo)
    calls: list[dict] = []

    async def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url})
        return httpx.Response(200, text='{"fact": "cats"}')

    monkeypatch.setattr("biznotes_core.core.ssrf.ssrf_safe_request", fake_request)
    return calls


@pytest.fixture
def mcp_calls(monkeypatch):
    """MCP 경계(데모 서버) — `call_tool(server, tool, args, **kw)` 호출을 기록한다."""
    calls: list[dict] = []

    async def fake_call(server, tool_name, arguments, **kw):
        calls.append({"server": server.name, "tool": tool_name, "arguments": arguments, **kw})
        return "단어 수: 3, 문자 수: 20"

    monkeypatch.setattr("biznotes_core.services.agents.mcp_client.call_tool", fake_call)
    return calls


async def _seed_samples() -> None:
    from scripts.seed import (
        ADMIN_EMAIL,
        upsert_mcp_demo,
        upsert_sample_agents,
        upsert_user,
    )

    await upsert_user(ADMIN_EMAIL, "seed-admin-pass-1", "Admin", "admin")
    await upsert_mcp_demo()
    await upsert_sample_agents()


async def _agent_id(name: str) -> uuid.UUID:
    async with async_session_factory() as s:
        return (
            await s.execute(select(Agent.id).where(Agent.name == name, Agent.scope == "system"))
        ).scalar_one()


async def _agent(scope: str, kind: str, definition: dict, owner_id=None) -> uuid.UUID:
    async with async_session_factory() as s:
        agent = Agent(
            name=f"direct-{uuid.uuid4().hex[:6]}",
            kind=kind,
            definition=definition,
            params_schema={},
            scope=scope,
            owner_id=owner_id,
            enabled=True,
        )
        s.add(agent)
        await s.commit()
        return agent.id


async def _start(user, agent_id: uuid.UUID, params: dict | None = None) -> str:
    async with async_session_factory() as s:
        nb = Notebook(owner_id=user.id, title="nb")
        s.add(nb)
        await s.flush()
        run = AgentRun(
            agent_id=agent_id,
            notebook_id=nb.id,
            user_id=user.id,
            status="queued",
            params=params or {},
        )
        s.add(run)
        await s.commit()
        run_id = str(run.id)
    await _run(run_id)
    return run_id


async def _result(run_id: str) -> tuple[AgentRun, list[AgentRunApproval]]:
    async with async_session_factory() as s:
        run = await s.get(AgentRun, uuid.UUID(run_id))
        approvals = (
            (await s.execute(select(AgentRunApproval).where(AgentRunApproval.run_id == run.id)))
            .scalars()
            .all()
        )
        return run, list(approvals)


def _http_workflow(url: str, requires_approval=None) -> dict:
    node = {"id": "h", "type": "http", "url": url, "method": "GET"}
    if requires_approval is not None:
        node["requires_approval"] = requires_approval
    return {"nodes": [node], "edges": []}


async def test_user_http_node_gated(user_a, http_stub, _mock_runtime_deps):
    """D-6 ① user 정의 http 노드는 `requires_approval: false`가 저장돼 있어도 승인 대기, 실행 0."""
    agent_id = await _agent(
        "user", "workflow", _http_workflow("https://api.example.com/v1", False), user_a.id
    )

    run_id = await _start(user_a, agent_id)

    run, approvals = await _result(run_id)
    assert run.status == "awaiting_approval"
    assert [a.status for a in approvals] == ["pending"]
    assert approvals[0].tool_name.startswith("http:GET")
    assert http_stub == []


async def test_system_template_host_gated(user_a, http_stub, _mock_runtime_deps):
    """D-6 ② system 정의라도 url host가 템플릿이면 false가 인정되지 않는다 → 승인 대기, 실행 0."""
    agent_id = await _agent(
        "system", "workflow", _http_workflow("https://{params.host}/v1", False)
    )

    run_id = await _start(user_a, agent_id, {"host": "api.example.com"})

    run, approvals = await _result(run_id)
    assert run.status == "awaiting_approval" and len(approvals) == 1
    assert http_stub == []


async def test_system_literal_false_executes(user_a, http_stub, _mock_runtime_deps):
    """D-6 ③ 대조 — 시드한 「[샘플] 고양이 상식」(system·리터럴·false) → 즉시 실행, 승인 행 0."""
    await _seed_samples()

    run_id = await _start(user_a, await _agent_id("[샘플] 고양이 상식"))

    run, approvals = await _result(run_id)
    assert run.status == "succeeded", run.error
    assert approvals == []
    assert http_stub == [{"method": "GET", "url": "https://catfact.ninja/fact"}]
    assert "cat-fact.json" in json.dumps(run.artifact_keys)


async def test_mcp_tool_without_approval_fails(user_a, mcp_calls, _mock_runtime_deps):
    """D-4 승인 행 없는 `mcp_tool`(system 정의) → run failed(「관리자가 승인한 도구가 아닙니다」),
    대역 `call_tool` 0회. 승인 정본이 없으면 false여도 실행하지 않는다(fail-closed)."""
    async with async_session_factory() as s:
        server = MCPServer(name="demo", transport="stdio", endpoint_or_command="/bin/true")
        s.add(server)
        await s.commit()
        server_id = str(server.id)
    agent_id = await _agent(
        "system",
        "mcp",
        {"server_id": server_id, "tool_name": "word_count", "requires_approval": False},
    )

    run_id = await _start(user_a, agent_id)

    run, approvals = await _result(run_id)
    assert run.status == "failed"
    assert "관리자가 승인한 도구가 아닙니다" in run.error
    assert approvals == []
    assert mcp_calls == []


async def test_demo_sample_executes_with_meta(user_a, mcp_calls, _mock_runtime_deps):
    """승인된 demo 샘플(시드) → succeeded, 대역 `call_tool(**_kw)`에 `meta`(run_id·user_id)."""
    await _seed_samples()

    run_id = await _start(user_a, await _agent_id("[샘플] MCP 단어 세기"), {"text": "a b c"})

    run, approvals = await _result(run_id)
    assert run.status == "succeeded", run.error
    assert approvals == []
    assert len(mcp_calls) == 1
    call = mcp_calls[0]
    assert (call["server"], call["tool"]) == ("demo", "word_count")
    assert call["arguments"] == {"text": "a b c"}
    assert call["meta"] == {MCP_META_RUN_ID: run_id, MCP_META_USER_ID: str(user_a.id)}


async def test_webhook_sample_awaits_approval(user_a, http_stub, _mock_runtime_deps):
    """「[샘플] Webhook 전송」(url 템플릿) → 승인 대기, 승인 `tool_name`은 `http:POST`로 시작.

    시드는 이 샘플을 꺼 둔다(받아오기 전용 배포 — core-v0.28.0-upgrade D4). 여기서는 정의 모양의
    승인 관문을 보려고 켜고, 테스트 기본 모드(`open`)로 돌린다.
    """
    await _seed_samples()
    agent_id = await _agent_id("[샘플] Webhook 전송")
    async with async_session_factory() as s:
        await s.execute(update(Agent).where(Agent.id == agent_id).values(enabled=True))
        await s.commit()

    run_id = await _start(
        user_a,
        agent_id,
        {"webhook_url": "https://httpbin.org/post", "message": "hi"},
    )

    run, approvals = await _result(run_id)
    assert run.status == "awaiting_approval"
    assert len(approvals) == 1 and approvals[0].tool_name.startswith("http:POST")
    assert http_stub == []
