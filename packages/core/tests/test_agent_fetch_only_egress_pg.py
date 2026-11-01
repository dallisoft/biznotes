# 신규(#395). 필요: PostgreSQL·Valkey(PG 테스트).
"""받아오기 전용 모드(#395) — 실제 그래프 실행(`execute_agent`)에서 http 노드와 tool·plugin kind.

- 요청 수는 mock이 아니라 **로컬 HTTP 서버**가 센다(외부 네트워크 경계만 로컬로 바꾼다).
- 저장 검증을 거치지 않은 정의(DB에 이미 있는 정의)도 실행 시 거부되고, 승인 행이 생기지 않는다.
`create_all` 기반이라 Alembic 마이그레이션 검증이 아니다.
"""

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from biznotes_core.config import get_settings
from biznotes_core.models import Agent, AgentRun, AgentRunApproval, Notebook, User
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError
from biznotes_core.services.agents.graph import execute_agent


class Origin:
    """로컬 HTTP 서버 — 요청(method·path·User-Agent)을 센다. `routes`: path → 302 Location."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str]] = []
        self.routes: dict[str, str] = {}
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            head = await reader.readuntil(b"\r\n\r\n")
            line, *lines = head.decode("latin-1").split("\r\n")
            method, path, _ = line.split(" ", 2)
            headers = {
                k.strip().lower(): v.strip()
                for k, v in (h.split(":", 1) for h in lines if ":" in h)
            }
            length = int(headers.get("content-length") or 0)
            if length:
                await reader.readexactly(length)
            self.requests.append((method, path, headers.get("user-agent", "")))
            if path in self.routes:
                status, extra, payload = "302 Found", f"Location: {self.routes[path]}\r\n", b""
            else:
                status, extra, payload = "200 OK", "Content-Type: text/plain\r\n", b"fetched"
            writer.write(
                f"HTTP/1.1 {status}\r\n{extra}Content-Length: {len(payload)}\r\n"
                "Connection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
            writer.close()

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"


@pytest.fixture
async def origin(monkeypatch: pytest.MonkeyPatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ssrf_allow_private", True)  # 로컬 서버만 허용
    monkeypatch.setattr(settings, "agent_http_egress_mode", "fetch_only")
    server = Origin()
    await server.start()
    yield server
    assert server.server is not None
    server.server.close()
    await server.server.wait_closed()


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


async def _run(db: Any, owner: User, scope: str, kind: str, definition: dict) -> RunContext:
    agent = Agent(
        name=f"egress-{uuid.uuid4().hex[:6]}",
        kind=kind,
        definition=definition,
        params_schema={},
        scope=scope,
        owner_id=None if scope == "system" else owner.id,
        enabled=True,
    )
    nb = Notebook(owner_id=owner.id, title="t")
    db.add_all([agent, nb])
    await db.flush()
    run = AgentRun(
        agent_id=agent.id, notebook_id=nb.id, user_id=owner.id, status="running", trigger="webhook"
    )
    db.add(run)
    await db.commit()
    return RunContext(
        run_id=run.id,
        notebook_id=nb.id,
        db=db,
        params={"q": "x"},
        user_id=owner.id,
        agent_id=agent.id,
        trigger="webhook",
    )


@pytest.mark.parametrize("kind", ["tool", "plugin"])
@pytest.mark.parametrize(
    ("http", "reason"),
    [
        ({}, "method"),  # 기본 POST — 웹훅으로 쓰던 정의
        ({"method": "GET", "body": {"note": "{params.q}"}}, "body"),
        ({"method": "GET", "headers": {"X-Api-Key": "k"}}, "header"),
    ],
)
async def test_stored_kind_definition_is_rejected_at_run_time(
    db, user_a: User, origin: Origin, artifacts: dict, kind: str, http: dict, reason: str
) -> None:
    """저장 검증을 거치지 않은 정의도 실행 시 거부된다 — 요청 0건, 승인 행 0건, 산출물 0건."""
    definition = {"http": {"url": origin.url("/hook"), **http}}
    ctx = await _run(db, user_a, "user", kind, definition)
    with pytest.raises(AgentRuntimeError, match=rf"\({reason}\)"):
        await execute_agent(ctx, kind, definition)
    assert origin.requests == []
    assert ctx.pending_approval is None
    count = await db.scalar(select(func.count()).select_from(AgentRunApproval))
    assert count == 0
    assert artifacts == {}


@pytest.mark.parametrize("kind", ["tool", "plugin"])
async def test_get_kind_fetches_through_redirect(
    db, user_a: User, origin: Origin, artifacts: dict, kind: str
) -> None:
    origin.routes["/start"] = "/final?q=x"
    definition = {
        "http": {"url": origin.url("/start"), "method": "GET", "requires_approval": False}
    }
    ctx = await _run(db, user_a, "system", kind, definition)
    await execute_agent(ctx, kind, definition)
    assert [(m, p) for m, p, _ in origin.requests] == [("GET", "/start"), ("GET", "/final?q=x")]
    assert {ua for _, _, ua in origin.requests} == {"biznotes-core-fetch/1"}
    assert list(artifacts.values()) == [b"fetched"]


async def test_workflow_query_template_is_bounded_after_render(
    db, user_a: User, origin: Origin, artifacts: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """경로·쿼리 템플릿은 허용하되 렌더링한 쿼리가 상한을 넘으면 보내지 않는다."""
    monkeypatch.setattr(get_settings(), "agent_http_max_query_chars", 16)
    definition = {
        "nodes": [
            {
                "id": "call",
                "type": "http",
                "url": origin.url("/search?q={params.q}"),
                "method": "GET",
                "requires_approval": False,
            }
        ],
        "edges": [],
    }
    ctx = await _run(db, user_a, "system", "workflow", definition)
    ctx.params = {"q": "한글한글"}  # 인코딩하면 36자
    with pytest.raises(AgentRuntimeError, match="query_too_long"):
        await execute_agent(ctx, "workflow", definition)
    assert origin.requests == []
    ctx = await _run(db, user_a, "system", "workflow", definition)
    ctx.params = {"q": "ok"}
    await execute_agent(ctx, "workflow", definition)
    assert [(m, p) for m, p, _ in origin.requests] == [("GET", "/search?q=ok")]
