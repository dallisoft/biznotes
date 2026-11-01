# 신규(#403 PR-B). 필요: PostgreSQL·Valkey(PG 테스트).
"""`save_sources`의 URL(PDF) 항목 — 실제 관문(`ssrf_safe_request`)·DB·인제스트 워커로 본다.

외부 경계만 바꾼다: DNS(`socket.getaddrinfo` — 모르는 이름은 실제 해석으로 넘긴다), HTTP 전송
(`httpx.AsyncClient`에 `MockTransport`를 끼운다 — 관문의 핀 연결 요청을 Host 헤더로 고른다),
스토리지(사전 대역), 브로커 발행, 원격 임베딩 공간. 계획 `docs/plans/save-sources-node.md` §6,
수락 기준 3·4. `create_all` 기반이라 Alembic 마이그레이션 검증이 아니다.
"""

import io
import json
import socket
import types
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import httpx
import pytest
import structlog.testing
from fastapi import UploadFile
from sqlalchemy import delete, select, text

from biznotes_core.api.v1 import sources as sources_api
from biznotes_core.config import get_settings
from biznotes_core.core import ssrf
from biznotes_core.core.db import get_session_factory
from biznotes_core.models import Agent, AgentRun, Notebook, Source, SourceChunk, User
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import RunLeaseLostError
from biznotes_core.services.agents.graph import execute_agent
from biznotes_core.services.agents.nodes import save_sources
from biznotes_core.services.ingestion import enqueue
from biznotes_core.services.ingestion.embedder import SparseVector
from biznotes_core.workers.celery_app import celery_app
from biznotes_core.workers.tasks import ingestion, overview

NAMESPACE = "naver-research"
CDN = "cdn.example.test"  # 허용 호스트(공인 IP)
INTERNAL = "internal.example.test"  # 허용 패턴 안이지만 사설 IP로 해석된다
EVIL = "evil.test"  # 허용 밖(공인 IP)
SECRET = "q-secret-7f3a"  # URL 쿼리 — 출력·로그에 나오면 안 된다
DNS = {CDN: "93.184.216.34", INTERNAL: "10.0.0.7", EVIL: "93.184.216.99"}
LIMIT = 4096  # 유효 업로드 상한(시험값)
_PDF_CTYPE = "application/pdf"


def _pdf(text: str) -> bytes:
    """pypdf가 읽는 한 쪽짜리 텍스트 PDF — 빈 쪽 판정(OCR 폴백)에 걸리지 않을 만큼 길다."""
    content = f"BT /F1 10 Tf 40 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


PDF = _pdf("Research note on semiconductor demand and memory pricing outlook " * 2)


# ---------- 외부 경계 ----------


Handler = Callable[[httpx.Request], Awaitable[httpx.Response] | httpx.Response]


def _stream(status: int, body: bytes = b"", headers: dict[str, str] | None = None):
    """전송처럼 스트림으로 주는 응답 — 관문은 원본 바이트(`aiter_raw`)로 읽는다.

    `httpx.Response(content=…)`는 본문을 미리 읽어 두어 원본 읽기가 가려진다(#395 보강).
    """
    return httpx.Response(status, headers=headers or {}, stream=httpx.ByteStream(body))


class Web:
    """가짜 인터넷 — `(호스트, 경로)` → 응답. 받은 요청을 `(호스트, 경로, 쿼리)`로 남긴다."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Handler] = {}
        self.requests: list[tuple[str, str, str]] = []

    def pdf(self, host: str, path: str, body: bytes = PDF, ctype: str = "application/pdf"):
        self.routes[(host, path)] = lambda r: _stream(200, body, {"content-type": ctype})

    def redirect(self, host: str, path: str, location: str) -> None:
        self.routes[(host, path)] = lambda r: _stream(302, headers={"location": location})

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.headers["host"]
        self.requests.append((host, request.url.path, request.url.query.decode()))
        handler = self.routes.get((host, request.url.path))
        if handler is None:
            return _stream(404, b"not found")
        response = handler(request)
        return await response if not isinstance(response, httpx.Response) else response

    def hosts(self) -> set[str]:
        return {h for h, _, _ in self.requests}


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> Web:
    fake = Web()
    real_resolve = socket.getaddrinfo
    real_client = httpx.AsyncClient

    def resolve(host: Any, *args: Any, **kwargs: Any) -> Any:
        ip = DNS.get(str(host))
        if ip is None:  # DB·Redis 등 — 실제 해석
            return real_resolve(host, *args, **kwargs)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    class Routed(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *a: Any, **k: Any) -> None:
            k["transport"] = httpx.MockTransport(fake)
            super().__init__(*a, **k)

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(ssrf.httpx, "AsyncClient", Routed)
    settings = get_settings()
    monkeypatch.setattr(settings, "ssrf_allow_private", False)
    monkeypatch.setattr(settings, "source_max_upload_bytes", LIMIT)
    monkeypatch.setattr(settings, "agent_http_allowed_hosts", "")
    return fake


class Store:
    """스토리지 대역 — 노드·업로드 라우터·인제스트 워커가 같은 저장소를 본다."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.deleted: list[str] = []

    def put(self, bucket: str, key: str, data: bytes, content_type: str = "") -> None:
        self.objects[(bucket, key)] = data

    def get(self, bucket: str, key: str) -> bytes:
        return self.objects[(bucket, key)]

    def stat(self, bucket: str, key: str) -> int:
        return len(self.objects[(bucket, key)])

    def delete(self, bucket: str, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop((bucket, key), None)

    def source_keys(self) -> set[str]:
        bucket = get_settings().storage_bucket_sources
        return {k for b, k in self.objects if b == bucket}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    fake = Store()
    for module in ("biznotes_core.services.storage", "biznotes_core.api.v1.sources"):
        monkeypatch.setattr(f"{module}.ensure_buckets", lambda: None)
        monkeypatch.setattr(f"{module}.put_object", fake.put)
    monkeypatch.setattr("biznotes_core.services.storage.delete_object", fake.delete)
    monkeypatch.setattr(enqueue, "_publish", lambda source_id, task_id, queue=None: None)
    return fake


@pytest.fixture(autouse=True)
def _no_node_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def start(*a: Any, **k: Any) -> None:
        return None

    async def finish(*a: Any, **k: Any) -> bool:
        return True

    monkeypatch.setattr("biznotes_core.services.agents.graph._log_node_start", start)
    monkeypatch.setattr("biznotes_core.services.agents.graph._log_node_finish", finish)


_captured: dict[uuid.UUID, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def _capture(monkeypatch: pytest.MonkeyPatch) -> None:
    from biznotes_core.services.agents import graph

    real = save_sources._exec_save_sources

    async def wrapped(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
        out = await real(ctx, node, state)
        _captured[ctx.run_id] = out
        return out

    monkeypatch.setitem(graph.NODE_EXECUTORS, "save_sources", wrapped)
    _captured.clear()


# ---------- 헬퍼 ----------


def _url(host: str, path: str) -> str:
    return f"https://{host}{path}?token={SECRET}"


def _url_item(external_id: str, url: str) -> dict[str, Any]:
    return {"title": f"리포트 {external_id}", "external_id": external_id, "url": url}


def _definition(items: list[Any], **save_kw: Any) -> dict[str, Any]:
    save = {
        "id": "save",
        "type": "save_sources",
        "items": "{list.text}",
        "namespace": NAMESPACE,
        "requires_approval": False,
        "allowed_hosts": ["*.example.test"],
        **save_kw,
    }
    if save.get("allowed_hosts") is None:
        save.pop("allowed_hosts")
    return {
        "nodes": [{"id": "list", "type": "constant", "value": {"text": json.dumps(items)}}, save],
        "edges": [["list", "save"]],
    }


async def _setup(db: Any, user: User, definition: dict[str, Any]) -> tuple[Agent, Notebook]:
    agent = Agent(
        name=f"pdf-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        definition=definition,
        params_schema={},
        scope="system",
        enabled=True,
    )
    nb = Notebook(owner_id=user.id, title="리서치")
    db.add_all([agent, nb])
    await db.commit()
    await db.refresh(agent)
    await db.refresh(nb)
    return agent, nb


def _ctx(db: Any, run: AgentRun, user: User, **kw: Any) -> RunContext:
    kw.setdefault("lease_valid", lambda: True)
    return RunContext(
        run_id=run.id,
        notebook_id=run.notebook_id,
        db=db,
        params={},
        trigger="schedule",
        user_id=user.id,
        user_role=user.role,
        agent_id=run.agent_id,
        **kw,
    )


async def _execute(db: Any, agent: Agent, nb: Notebook, user: User, **kw: Any) -> dict[str, Any]:
    run = AgentRun(
        agent_id=agent.id, notebook_id=nb.id, user_id=user.id, status="running", trigger="schedule"
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    await execute_agent(_ctx(db, run, user, **kw), agent.kind, agent.definition)
    return _captured.pop(run.id)


async def _rows(notebook_id: uuid.UUID) -> list[Source]:
    async with get_session_factory()() as s:
        rows = await s.scalars(
            select(Source).where(Source.notebook_id == notebook_id).order_by(Source.created_at)
        )
        return list(rows.all())


def _codes(out: dict[str, Any]) -> list[tuple[str, str]]:
    return [(f["external_id"], f["code"]) for f in out["failed"]]


# ---------- 수락 기준 3: 업로드와 같은 파싱 결과로 ready ----------


class FakeSpace:
    """원격 임베딩 공간 대역(`test_ingest_stage_tasks_pg.py`와 같은 모양)."""

    is_local = False
    key = "gateway:fake-403"
    display_name = "fake"

    def embed(self, texts: list[str], **kw: Any) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


@pytest.fixture
def worker(monkeypatch: pytest.MonkeyPatch, store: Store) -> None:
    """인제스트 워커 실경로(파싱·청킹·색인) — 스토리지는 같은 `store`, 임베딩만 대역이다."""
    monkeypatch.setattr(ingestion, "get_object", store.get)
    monkeypatch.setattr(ingestion, "put_object", store.put)
    monkeypatch.setattr(ingestion, "stat_object_size", store.stat)
    monkeypatch.setattr(ingestion, "ensure_buckets", lambda: None)

    async def active_space(db: Any) -> Any:
        return FakeSpace()

    monkeypatch.setattr(ingestion, "active_space", active_space)
    monkeypatch.setattr(
        ingestion, "embed_sparse", lambda texts: [SparseVector([0], [1.0]) for _ in texts]
    )
    monkeypatch.setattr(overview, "schedule_overview_refresh", lambda nid, **kw: None)
    monkeypatch.setattr(ingestion, "publish_source_event", lambda sid, event: None)
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: None)


async def _ingest(source_id: uuid.UUID) -> Source:
    result = await ingestion._run(str(source_id))
    handoff = result.pop("handoff")
    await ingestion._run_index(str(source_id), **ingestion._index_kwargs_of(handoff))
    async with get_session_factory()() as s:
        row = await s.get(Source, source_id)
        assert row is not None
        return row


async def _chunks(source_id: uuid.UUID) -> list[str]:
    async with get_session_factory()() as s:
        rows = await s.scalars(
            select(SourceChunk.text).where(SourceChunk.source_id == source_id).order_by(
                SourceChunk.seq
            )
        )
        return list(rows.all())


async def test_pdf_url_item_becomes_a_file_source_ready_like_an_upload(
    db, user_a: User, web: Web, store: Store, worker: None
) -> None:
    web.pdf(CDN, "/r/1.pdf", ctype="application/octet-stream")  # content-type은 판정이 아니다
    agent, nb = await _setup(db, user_a, _definition([_url_item("r-1", _url(CDN, "/r/1.pdf"))]))
    out = await _execute(db, agent, nb, user_a)
    assert out["failed"] == [] and [c["external_id"] for c in out["created"]] == ["r-1"]

    (row,) = await _rows(nb.id)
    assert (row.type, row.mime_type, row.status, row.size_bytes) == (
        "file",
        "application/pdf",
        "pending",
        len(PDF),
    )
    assert row.origin_url == _url(CDN, "/r/1.pdf")
    assert row.external_ref == f"mcp:{NAMESPACE}:r-1"
    assert row.original_object_key == f"{row.id}/raw.pdf"
    assert store.objects[(get_settings().storage_bucket_sources, row.original_object_key)] == PDF
    assert web.requests == [(CDN, "/r/1.pdf", f"token={SECRET}")]

    # 같은 바이트를 업로드 라우터로 올린다
    uploaded = await sources_api.upload_file_source(
        nb.id, UploadFile(io.BytesIO(PDF), filename="리포트.pdf"), user=user_a, db=db
    )
    # 워커가 고르는 파서 입력(행 모양·원본)이 같다
    up = await db.get(Source, uploaded.id, populate_existing=True)
    assert (up.type, up.mime_type, up.size_bytes) == (row.type, row.mime_type, row.size_bytes)
    assert ingestion._parse(row, PDF).markdown == ingestion._parse(up, PDF).markdown

    by_url, by_upload = await _ingest(row.id), await _ingest(up.id)
    assert (by_url.status, by_upload.status) == ("ready", "ready")
    assert by_url.error_message is None
    url_chunks = await _chunks(row.id)
    assert url_chunks and url_chunks == await _chunks(up.id)
    assert "semiconductor demand" in url_chunks[0]


# ---------- 수락 기준 4: 그 항목만 failed, 나머지는 계속 ----------


async def test_each_url_failure_fails_only_its_item(
    db, user_a: User, web: Web, store: Store
) -> None:
    web.pdf(CDN, "/ok.pdf")
    web.pdf(EVIL, "/x.pdf")
    web.pdf(INTERNAL, "/x.pdf")
    web.redirect(CDN, "/to-internal", f"https://{INTERNAL}/x.pdf")  # 공개 → 사설 IP
    web.redirect(CDN, "/to-evil", f"https://{EVIL}/x.pdf")  # 허용 → 밖
    web.pdf(CDN, "/edge.pdf", body=b"%PDF" + b"0" * (LIMIT - 3))  # 상한 + 1바이트(관문 통과)
    web.pdf(CDN, "/big.pdf", body=b"%PDF" + b"0" * (LIMIT * 2))  # 관문 상한 초과
    web.pdf(CDN, "/page.html", body=b"<html><body>not a pdf</body></html>", ctype="text/html")
    items = [
        _url_item("ok", _url(CDN, "/ok.pdf")),
        _url_item("outside", _url(EVIL, "/x.pdf")),
        _url_item("to-internal", _url(CDN, "/to-internal")),
        _url_item("to-evil", _url(CDN, "/to-evil")),
        _url_item("edge", _url(CDN, "/edge.pdf")),
        _url_item("big", _url(CDN, "/big.pdf")),
        _url_item("html", _url(CDN, "/page.html")),
        _url_item("missing", _url(CDN, "/missing.pdf")),
        {"title": "본문", "external_id": "text", "content": "텍스트 항목"},
    ]
    agent, nb = await _setup(db, user_a, _definition(items))
    with structlog.testing.capture_logs() as logs:
        out = await _execute(db, agent, nb, user_a)

    assert [c["external_id"] for c in out["created"]] == ["ok", "text"]
    assert _codes(out) == [
        ("outside", "host_not_allowed"),
        ("to-internal", "ssrf_blocked"),
        ("to-evil", "host_not_allowed"),
        ("edge", "too_large"),
        ("big", "too_large"),
        ("html", "not_pdf"),
        ("missing", "fetch_failed"),
    ]
    assert {f["message"] for f in out["failed"]} == {
        save_sources.SAVE_SOURCE_FAILURE_MESSAGES[c] for _, c in _codes(out)
    }
    # 허용 밖·사설 호스트에는 요청이 가지 않았다(DNS·정책 검사가 연결 전에 막았다)
    assert web.hosts() == {CDN}
    rows = await _rows(nb.id)
    assert sorted(r.external_ref or "" for r in rows) == [
        f"mcp:{NAMESPACE}:ok",
        f"mcp:{NAMESPACE}:text",
    ]
    assert store.source_keys() == {r.original_object_key for r in rows}  # 실패 항목은 원본이 없다
    # URL 쿼리 원문은 출력·로그 어디에도 없다
    assert SECRET not in json.dumps(out, ensure_ascii=False)
    assert SECRET not in json.dumps(logs, ensure_ascii=False, default=str)
    assert any(e.get("event") == "agent_http_blocked" for e in logs)


async def test_url_items_need_an_allowed_host_in_node_and_global_lists(
    db, user_a: User, web: Web, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    web.pdf(CDN, "/a.pdf")
    item = [_url_item("a", _url(CDN, "/a.pdf"))]

    agent, nb = await _setup(db, user_a, _definition(item, allowed_hosts=None))
    assert _codes(await _execute(db, agent, nb, user_a)) == [("a", "host_not_allowed")]

    # 전역 목록과 겹치지 않으면 노드 목록만으로는 받지 않는다
    monkeypatch.setattr(get_settings(), "agent_http_allowed_hosts", "other.test,*.elsewhere.test")
    agent, nb = await _setup(db, user_a, _definition(item))
    assert _codes(await _execute(db, agent, nb, user_a)) == [("a", "host_not_allowed")]
    assert web.requests == []  # 어느 경우도 연결하지 않았다

    # 교집합(`*.example.test` ∩ 전역 `cdn.example.test`)이면 받는다
    monkeypatch.setattr(get_settings(), "agent_http_allowed_hosts", f"{CDN},other.test")
    agent, nb = await _setup(db, user_a, _definition(item))
    out = await _execute(db, agent, nb, user_a)
    assert [c["external_id"] for c in out["created"]] == ["a"]


async def test_existing_url_item_is_not_fetched_again(
    db, user_a: User, web: Web, store: Store
) -> None:
    web.pdf(CDN, "/a.pdf")
    agent, nb = await _setup(db, user_a, _definition([_url_item("a", _url(CDN, "/a.pdf"))]))
    await _execute(db, agent, nb, user_a)
    again = await _execute(db, agent, nb, user_a)

    assert [s["external_id"] for s in again["skipped_duplicate"]] == ["a"]
    assert len(web.requests) == 1


async def test_fetch_time_limit_is_per_item(
    db, user_a: User, web: Web, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(save_sources, "_URL_FETCH_TOTAL_SEC", 0.3)

    async def slow(request: httpx.Request) -> httpx.Response:
        await anyio.sleep(5)
        return _stream(200, PDF)

    web.routes[(CDN, "/slow.pdf")] = slow
    web.pdf(CDN, "/fast.pdf")
    items = [_url_item("slow", _url(CDN, "/slow.pdf")), _url_item("fast", _url(CDN, "/fast.pdf"))]
    agent, nb = await _setup(db, user_a, _definition(items))
    with anyio.fail_after(4):
        out = await _execute(db, agent, nb, user_a)

    assert _codes(out) == [("slow", "fetch_failed")]
    assert [c["external_id"] for c in out["created"]] == ["fast"]


async def test_db_failure_after_fetch_discards_the_object(
    db, user_a: User, web: Web, store: Store
) -> None:
    """받는 동안 대상 노트북이 지워지면 INSERT가 FK로 실패한다 — 그 항목만 `save_failed`이고
    올린 원본을 보상 삭제한다."""
    agent, nb = await _setup(db, user_a, _definition([_url_item("a", _url(CDN, "/a.pdf"))]))

    async def delete_notebook_then_serve(request: httpx.Request) -> httpx.Response:
        async with get_session_factory()() as s:
            await s.execute(delete(Notebook).where(Notebook.id == nb.id))
            await s.commit()
        return _stream(200, PDF)

    web.routes[(CDN, "/a.pdf")] = delete_notebook_then_serve
    out = await _execute(db, agent, nb, user_a)
    assert _codes(out) == [("a", "save_failed")]
    assert store.source_keys() == set()
    assert len(store.deleted) == 1
    async with get_session_factory()() as s:
        assert (await s.scalars(select(Source))).all() == []


async def test_lease_lost_while_fetching_writes_nothing(
    db, user_a: User, web: Web, store: Store
) -> None:
    lease = {"valid": True}

    def serve(request: httpx.Request) -> httpx.Response:
        lease["valid"] = False  # 받는 동안 run을 회수당했다
        return _stream(200, PDF)

    web.routes[(CDN, "/a.pdf")] = serve
    agent, nb = await _setup(db, user_a, _definition([_url_item("a", _url(CDN, "/a.pdf"))]))
    with pytest.raises(RunLeaseLostError):
        await _execute(db, agent, nb, user_a, lease_valid=lambda: lease["valid"])
    assert await _rows(nb.id) == [] and store.source_keys() == set()


async def test_url_item_follows_the_notebook_rule(
    db, user_a: User, web: Web, store: Store
) -> None:
    web.pdf(CDN, "/a.pdf")
    rule = {"title_template": "리서치 {month}", "timezone": "Asia/Seoul"}
    agent, anchor = await _setup(
        db, user_a, _definition([_url_item("a", _url(CDN, "/a.pdf"))], notebook_rule=rule)
    )
    out = await _execute(db, agent, anchor, user_a)

    (created,) = out["created"]
    async with get_session_factory()() as s:
        target = await s.get(Notebook, uuid.UUID(created["notebook_id"]))
    assert target is not None and target.owner_id == user_a.id
    assert target.title.startswith("리서치 ") and target.id != anchor.id
    assert [r.type for r in await _rows(target.id)] == ["file"]


# ---------- PR-B 리뷰 반영 ----------


async def test_url_item_db_failure_leaks_no_query_secret(
    db, user_a: User, web: Web, store: Store, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """URL 항목의 생성 실패(실제 INSERT 실패)가 쿼리의 비밀값을 남기지 않는다(High).

    노드 실행 로그(`execution_logs`)를 켠다. 서버 로그는 예외 추적까지 렌더해 본다 — DB 예외
    문자열의 바인드 값에 `origin_url`이 실린다. 실패는 새 소스 id를 이미 있는 행의 id로 바꿔
    PK 위반으로 만든다(노트북을 지우면 run과 실행 로그까지 함께 지워진다).
    """
    from biznotes_core.models import ExecutionLog
    from biznotes_core.services.agents import graph
    from biznotes_core.services.agents import logging as node_logging
    from biznotes_core.services.ingestion import source_create

    monkeypatch.setattr(graph, "_log_node_start", node_logging._log_node_start)
    monkeypatch.setattr(graph, "_log_node_finish", node_logging._log_node_finish)
    secret_url = f"https://{CDN}/a.pdf?token=test-secret"
    web.pdf(CDN, "/a.pdf")
    agent, nb = await _setup(db, user_a, _definition([_url_item("a", secret_url)]))
    other = Notebook(owner_id=user_a.id, title="다른 노트북")
    db.add(other)
    await db.flush()
    taken = Source(notebook_id=other.id, type="text", title="이미 쓰는 id", status="ready")
    db.add(taken)
    await db.commit()
    monkeypatch.setattr(
        source_create, "uuid", types.SimpleNamespace(UUID=uuid.UUID, uuid4=lambda: taken.id)
    )

    caplog.set_level("DEBUG")
    with structlog.testing.capture_logs(
        processors=[structlog.processors.format_exc_info]
    ) as logs:
        out = await _execute(db, agent, nb, user_a)

    assert _codes(out) == [("a", "save_failed")]
    assert "test-secret" not in json.dumps(out, ensure_ascii=False)
    assert "test-secret" not in json.dumps(logs, ensure_ascii=False, default=str)
    assert any(e.get("error") == "IntegrityError" for e in logs)  # 유형만 남겼다
    # httpx 자체의 요청 INFO 로그(`HTTP Request: GET …`)는 관문을 쓰는 모든 경로(http 노드 포함)의
    # 기존 동작이라 이 노드의 범위 밖이다 — 후속으로 보고한다
    app_records = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    assert not any("test-secret" in r.getMessage() for r in app_records)
    async with get_session_factory()() as s:
        rows = (
            await s.scalars(select(ExecutionLog).where(ExecutionLog.node_id == "save"))
        ).all()
    assert [r.status for r in rows] == ["succeeded"]
    for row in rows:
        dumped = json.dumps([row.input_data, row.output_data, row.error_message], default=str)
        assert "test-secret" not in dumped
    assert store.source_keys() == set()


async def test_no_transaction_is_held_while_fetching(
    db, user_a: User, web: Web, store: Store
) -> None:
    """받는 동안 이 DB에 `idle in transaction` 연결이 없다(Medium).

    사전 조회 트랜잭션을 닫고 받는다.
    """
    seen: list[int] = []

    async def count_then_serve(request: httpx.Request) -> httpx.Response:
        async with get_session_factory()() as s:
            seen.append(
                int(
                    await s.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname = current_database() "
                            "AND state = 'idle in transaction'"
                        )
                    )
                    or 0
                )
            )
        return _stream(200, PDF if request.url.path != "/b.pdf" else _pdf("second " * 20))

    web.routes[(CDN, "/a.pdf")] = count_then_serve
    web.routes[(CDN, "/b.pdf")] = count_then_serve
    items = [
        {"title": "본문", "external_id": "t", "content": "먼저 만드는 텍스트 항목"},
        _url_item("a", _url(CDN, "/a.pdf")),
        _url_item("b", _url(CDN, "/b.pdf")),
    ]
    agent, nb = await _setup(db, user_a, _definition(items))
    out = await _execute(db, agent, nb, user_a)

    assert [c["external_id"] for c in out["created"]] == ["t", "a", "b"]
    assert seen == [0, 0]


class _WatchedStream(httpx.AsyncByteStream):
    """본문을 읽었는지 기록하는 응답 스트림."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.read = False

    async def __aiter__(self):  # type: ignore[override]
        self.read = True
        yield self.body


async def test_compressed_response_fails_its_item_without_decoding(
    db, user_a: User, web: Web, store: Store
) -> None:
    """허용 호스트의 gzip 응답은 풀기 전에 거부된다(#409) — 그 항목만 `fetch_failed`(H2 회귀)."""
    import gzip

    bomb = gzip.compress(b"\0" * (8 * 1024 * 1024))  # 수 KiB → 8 MiB
    stream = _WatchedStream(bomb)
    asked: list[str | None] = []

    def serve_gzip(request: httpx.Request) -> httpx.Response:
        asked.append(request.headers.get("accept-encoding"))
        return httpx.Response(
            200, headers={"content-encoding": "gzip", "content-type": _PDF_CTYPE}, stream=stream
        )

    web.routes[(CDN, "/bomb.pdf")] = serve_gzip
    web.pdf(CDN, "/ok.pdf")
    items = [_url_item("bomb", _url(CDN, "/bomb.pdf")), _url_item("ok", _url(CDN, "/ok.pdf"))]
    agent, nb = await _setup(db, user_a, _definition(items))
    out = await _execute(db, agent, nb, user_a)

    assert _codes(out) == [("bomb", "fetch_failed")]
    assert [c["external_id"] for c in out["created"]] == ["ok"]
    assert stream.read is False  # 본문을 읽지(풀지) 않았다
    assert asked == ["identity"]


async def test_slow_dns_is_cut_by_the_item_time_limit(
    db, user_a: User, web: Web, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """느린 DNS(스레드 대기)도 항목 시간 한도로 끊고 다음 항목으로 간다(M1 회귀, #409)."""
    import time

    monkeypatch.setattr(save_sources, "_URL_FETCH_TOTAL_SEC", 0.3)
    resolve = ssrf.socket.getaddrinfo  # `web`이 바꿔 둔 가짜 DNS

    def slow_resolve(host: Any, *args: Any, **kwargs: Any) -> Any:
        if str(host) == "slow-dns.example.test":
            time.sleep(2)
        return resolve(host, *args, **kwargs)

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", slow_resolve)
    web.pdf(CDN, "/ok.pdf")
    items = [
        _url_item("slow", "https://slow-dns.example.test/a.pdf"),
        _url_item("ok", _url(CDN, "/ok.pdf")),
    ]
    agent, nb = await _setup(db, user_a, _definition(items))
    started = time.monotonic()
    out = await _execute(db, agent, nb, user_a)

    assert time.monotonic() - started < 1.5
    assert _codes(out) == [("slow", "fetch_failed")]
    assert [c["external_id"] for c in out["created"]] == ["ok"]
