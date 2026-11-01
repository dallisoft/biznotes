# 신규(#403 PR-A). 필요: PostgreSQL·Valkey(PG 테스트).
"""`save_sources` 노드 — 실제 DB·잠금으로 본다(계획 `docs/plans/save-sources-node.md`).

외부 경계만 대역이다: MCP 도구 호출(`mcp_client.call_tool`), 스토리지(사전 대역), Celery 발행.
인제스트 발행은 커밋 뒤에 다른 세션에서 행이 보이는지 확인하는 대역으로 바꾼다.
`create_all` 기반이라 Alembic 마이그레이션 검증이 아니다.
"""

import asyncio
import contextlib
import json
import types
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import share_to_user
from sqlalchemy import select, text

from biznotes_core.config import get_settings
from biznotes_core.core.db import get_session_factory
from biznotes_core.models import (
    Agent,
    AgentRun,
    MCPServer,
    MCPServerTool,
    Notebook,
    Source,
    User,
)
from biznotes_core.services.agents import mcp_client
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import RunLeaseLostError
from biznotes_core.services.agents.graph import execute_agent
from biznotes_core.services.ingestion import enqueue, source_create

NAMESPACE = "naver-research"


# ---------- 외부 경계 ----------


class Store:
    """스토리지 대역 — `(버킷, 키) → 바이트`."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.puts: list[str] = []
        self.deleted: list[str] = []

    def put(self, bucket: str, key: str, data: bytes, content_type: str = "") -> None:
        self.puts.append(key)
        self.objects[(bucket, key)] = data

    def delete(self, bucket: str, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop((bucket, key), None)

    def keys(self) -> set[str]:
        return {key for _bucket, key in self.objects}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    fake = Store()
    monkeypatch.setattr("biznotes_core.services.storage.ensure_buckets", lambda: None)
    monkeypatch.setattr("biznotes_core.services.storage.put_object", fake.put)
    monkeypatch.setattr("biznotes_core.services.storage.delete_object", fake.delete)
    return fake


@pytest.fixture
def ingest(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    """인제스트 발행 대역 — (source_id, 발행 시점에 다른 세션에서 행이 보였는가)."""
    sent: list[tuple[str, bool]] = []

    async def publish(source_id: str, task_id: str, queue: str | None = None) -> None:
        async with get_session_factory()() as s:
            visible = await s.get(Source, uuid.UUID(source_id)) is not None
        sent.append((source_id, visible))

    monkeypatch.setattr(enqueue, "_publish", publish)
    return sent


@pytest.fixture
def mcp_items(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """MCP 도구가 돌려줄 목록 — 테스트가 채운다. 도구 결과는 JSON 텍스트다."""
    items: list[dict[str, Any]] = []

    async def fake(server: Any, tool: str, arguments: dict[str, Any], **_kw: Any) -> str:
        return json.dumps(items, ensure_ascii=False)

    monkeypatch.setattr(mcp_client, "call_tool", fake)
    return items


@pytest.fixture(autouse=True)
def _no_node_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def start(*a: Any, **k: Any) -> None:
        return None

    async def finish(*a: Any, **k: Any) -> bool:
        return True

    monkeypatch.setattr("biznotes_core.services.agents.graph._log_node_start", start)
    monkeypatch.setattr("biznotes_core.services.agents.graph._log_node_finish", finish)


# ---------- 행·흐름 헬퍼 ----------


def _items(*ids: str) -> list[dict[str, Any]]:
    return [{"title": f"리포트 {i}", "external_id": i, "content": f"본문 {i}"} for i in ids]


def _save(**kw: Any) -> dict[str, Any]:
    return {
        "id": "save",
        "type": "save_sources",
        "items": "{list.text}",
        "namespace": NAMESPACE,
        "requires_approval": False,
        **kw,
    }


def _constant_wf(items: list[Any], **save_kw: Any) -> dict[str, Any]:
    """MCP 없이 항목을 넣는 정의 — 목록은 `constant` 노드의 text다."""
    return {
        "nodes": [
            {"id": "list", "type": "constant", "value": {"text": json.dumps(items)}},
            _save(**save_kw),
        ],
        "edges": [["list", "save"]],
    }


async def _agent(db: Any, definition: dict[str, Any], *, scope: str = "system", owner=None):
    agent = Agent(
        name=f"save-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        definition=definition,
        params_schema={},
        scope=scope,
        owner_id=owner.id if owner is not None else None,
        enabled=True,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


async def _notebook(db: Any, owner: User, title: str = "수집") -> Notebook:
    nb = Notebook(owner_id=owner.id, title=title)
    db.add(nb)
    await db.commit()
    await db.refresh(nb)
    return nb


async def _run_row(db: Any, agent: Agent, nb: Notebook, user: User) -> AgentRun:
    run = AgentRun(
        agent_id=agent.id, notebook_id=nb.id, user_id=user.id, status="running", trigger="manual"
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


def _ctx(db: Any, run: AgentRun, user: User, **kw: Any) -> RunContext:
    return RunContext(
        run_id=run.id,
        notebook_id=run.notebook_id,
        db=db,
        params=dict(run.params or {}),
        trigger=run.trigger,
        user_id=user.id,
        user_role=user.role,
        agent_id=run.agent_id,
        lease_valid=lambda: True,
        **kw,
    )


async def _execute(db: Any, agent: Agent, nb: Notebook, user: User) -> dict[str, Any]:
    """run 행을 만들고 제품 워커처럼 실행한다. `save` 노드 출력을 돌려준다."""
    run = await _run_row(db, agent, nb, user)
    await execute_agent(_ctx(db, run, user), agent.kind, agent.definition)
    return _captured.pop(run.id)


# run id → `save_sources` 노드 출력(아래 `_capture`가 채운다)
_captured: dict[uuid.UUID, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def _capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """`save_sources` 노드 출력을 run id별로 남긴다 — 실행기 자체는 그대로 돈다."""
    from biznotes_core.services.agents import graph
    from biznotes_core.services.agents.nodes import save_sources

    real = save_sources._exec_save_sources

    async def wrapped(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
        out = await real(ctx, node, state)
        _captured[ctx.run_id] = out
        return out

    monkeypatch.setitem(graph.NODE_EXECUTORS, "save_sources", wrapped)
    _captured.clear()


async def _sources(notebook_id: uuid.UUID | None = None) -> list[Source]:
    async with get_session_factory()() as s:
        q = select(Source).order_by(Source.created_at, Source.id)
        if notebook_id is not None:
            q = q.where(Source.notebook_id == notebook_id)
        return list((await s.scalars(q)).all())


def _ref(external_id: str) -> str:
    return f"mcp:{NAMESPACE}:{external_id}"


async def _mcp_server(db: Any) -> MCPServer:
    server = MCPServer(
        name=f"naver-{uuid.uuid4().hex[:6]}",
        transport="stdio",
        endpoint_or_command="x",
        enabled=True,
    )
    db.add(server)
    await db.flush()
    db.add(
        MCPServerTool(
            server_id=server.id,
            tool_name="list_reports",
            read_only=True,
            approved_at=datetime.now(UTC),
        )
    )
    await db.commit()
    await db.refresh(server)
    return server


# ---------- 항목별 실패·중복·상한 ----------


async def test_each_bad_item_fails_alone(
    db, user_a: User, store: Store, ingest: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 처리 상한은 새로 처리할 항목 수다 — 잘못된 항목·배치 안 중복은 세지 않는다(리뷰 M1).
    # url 항목은 받아 오기를 시도하므로 센다(여기서는 allowed_hosts가 없어 바로 실패한다)
    monkeypatch.setattr(get_settings(), "agent_save_sources_max_items", 3)
    items: list[Any] = [
        _items("ok-1")[0],
        {"title": "id 없음", "content": "x"},
        {"title": "pdf", "external_id": "u-1", "url": "https://stock.example.com/a.pdf"},
        "문자열",
        {**_items("ok-1")[0], "title": "같은 배치의 중복"},
        _items("ok-2")[0],
        _items("cut-1")[0],  # 새 항목 4번째 — 상한(3) 밖이라 다음 실행으로 남긴다
    ]
    agent = await _agent(db, _constant_wf(items))
    nb = await _notebook(db, user_a)
    out = await _execute(db, agent, nb, user_a)

    assert [(c["index"], c["external_id"]) for c in out["created"]] == [(0, "ok-1"), (5, "ok-2")]
    assert [(f["index"], f["external_id"], f["code"]) for f in out["failed"]] == [
        (1, None, "invalid_item"),
        (2, "u-1", "host_not_allowed"),  # 노드에 allowed_hosts가 없다 — 받아 오지 않는다
        (3, None, "invalid_item"),
    ]
    assert [f["message"] for f in out["failed"]] == [
        "항목 형식이 올바르지 않습니다",
        "허용되지 않은 호스트의 주소입니다",
        "항목 형식이 올바르지 않습니다",
    ]
    assert out["skipped_duplicate"] == [
        {
            "index": 4,
            "external_id": "ok-1",
            "source_id": out["created"][0]["source_id"],
            "notebook_id": str(nb.id),
        }
    ]
    assert (out["truncated"], out["total"]) == (True, 7)
    assert [r.external_ref for r in await _sources(nb.id)] == [_ref("ok-1"), _ref("ok-2")]
    assert len(store.puts) == 2


async def test_capacity_fails_only_overflow_items(
    db, user_a: User, store: Store, ingest: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    nb = await _notebook(db, user_a)
    db.add(Source(notebook_id=nb.id, type="file", title="기존", status="ready"))
    await db.commit()
    monkeypatch.setattr(get_settings(), "max_sources_per_notebook", 2)
    agent = await _agent(db, _constant_wf(_items("a", "b", "c")))
    out = await _execute(db, agent, nb, user_a)

    assert [c["external_id"] for c in out["created"]] == ["a"]
    assert [(f["external_id"], f["code"]) for f in out["failed"]] == [
        ("b", "notebook_full"),
        ("c", "notebook_full"),
    ]
    assert len(await _sources(nb.id)) == 2
    # 실패한 항목의 원본은 보상 삭제했다 — 남은 원본은 만든 소스의 것뿐이다
    (created,) = [r for r in await _sources(nb.id) if r.external_ref]
    assert store.keys() == {created.original_object_key}
    assert len(ingest) == 1


async def test_existing_rows_in_any_status_are_duplicates_but_not_other_notebooks(
    db, user_a: User, store: Store, ingest: list
) -> None:
    """failed 행도 중복이다(D2). 다른 노트북의 같은 키는 아니다."""
    nb = await _notebook(db, user_a)
    other = await _notebook(db, user_a, "다른 노트북")
    db.add_all(
        [
            Source(
                notebook_id=nb.id, type="text", title="실패", status="failed",
                external_ref=_ref("failed"),
            ),
            Source(
                notebook_id=other.id, type="text", title="다른 곳", status="ready",
                external_ref=_ref("elsewhere"),
            ),
        ]
    )
    await db.commit()
    agent = await _agent(db, _constant_wf(_items("failed", "elsewhere")))
    out = await _execute(db, agent, nb, user_a)

    assert [s["external_id"] for s in out["skipped_duplicate"]] == ["failed"]
    assert [c["external_id"] for c in out["created"]] == ["elsewhere"]


# ---------- 인가 ----------


async def test_read_share_user_cannot_write(
    db, user_a: User, user_b: User, store: Store, ingest: list
) -> None:
    nb = await _notebook(db, user_a)
    db.add_all(share_to_user(nb.id, user_b.id, "read"))
    await db.commit()
    agent = await _agent(db, _constant_wf(_items("a", "b")))
    out = await _execute(db, agent, nb, user_b)

    assert out["created"] == []
    assert [(f["external_id"], f["code"]) for f in out["failed"]] == [
        ("a", "forbidden"),
        ("b", "forbidden"),
    ]
    assert await _sources(nb.id) == []
    assert store.puts == [] and ingest == []


async def test_write_share_and_admin_can_write(
    db, user_a: User, user_b: User, admin_user: User, store: Store, ingest: list
) -> None:
    nb = await _notebook(db, user_a)
    db.add_all(share_to_user(nb.id, user_b.id, "write"))
    await db.commit()
    by_writer = await _execute(db, await _agent(db, _constant_wf(_items("w"))), nb, user_b)
    by_admin = await _execute(db, await _agent(db, _constant_wf(_items("x"))), nb, admin_user)

    assert [c["external_id"] for c in by_writer["created"]] == ["w"]
    assert [c["external_id"] for c in by_admin["created"]] == ["x"]


# ---------- lease ----------


async def test_lost_lease_writes_nothing(db, user_a: User, store: Store, ingest: list) -> None:
    nb = await _notebook(db, user_a)
    agent = await _agent(db, _constant_wf(_items("a")))
    run = await _run_row(db, agent, nb, user_a)
    ctx = _ctx(db, run, user_a)
    ctx.lease_valid = lambda: False
    with pytest.raises(RunLeaseLostError):
        await execute_agent(ctx, agent.kind, agent.definition)
    assert await _sources(nb.id) == [] and store.puts == []


# ---------- 동시 두 run — 중복 0 ----------


async def _wait_until_waiters(holder_pid: int, count: int) -> None:
    """`count`개 연결이 `holder_pid`의 잠금을 기다릴 때까지 기다린다(상한 10초)."""
    async with asyncio.timeout(10), get_session_factory()() as s:
        while (
            await s.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND :holder = ANY(pg_blocking_pids(pid))"
                ),
                {"holder": holder_pid},
            )
            or 0
        ) < count:
            await s.rollback()


async def test_concurrent_runs_create_no_duplicates(
    db, user_a: User, store: Store, ingest: list
) -> None:
    ids = ("c-1", "c-2", "c-3")
    nb = await _notebook(db, user_a)
    agent = await _agent(db, _constant_wf(_items(*ids)))
    runs = [await _run_row(db, agent, nb, user_a) for _ in range(2)]

    async def one(run: AgentRun) -> None:
        async with get_session_factory()() as s:
            await execute_agent(_ctx(s, run, user_a), agent.kind, agent.definition)

    tasks: list[asyncio.Task[None]] = []
    try:
        # 두 run이 사전 조회를 지나 첫 항목의 노트북 잠금에서 함께 기다리게 한다 — 잠금 안의
        # 중복 판정이 둘째 run을 막아야 한다
        async with get_session_factory()() as holder:
            pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            await enqueue.lock_notebook_sources(holder, nb.id)
            tasks = [asyncio.create_task(one(r)) for r in runs]
            await _wait_until_waiters(pid, 2)
            await holder.commit()
        async with asyncio.timeout(20):
            await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t

    rows = await _sources(nb.id)
    assert sorted(r.external_ref for r in rows) == sorted(_ref(i) for i in ids)
    outs = [_captured[r.id] for r in runs]
    assert sum(len(o["created"]) for o in outs) == 3
    assert all(len(o["created"]) + len(o["skipped_duplicate"]) == 3 for o in outs)
    assert all(o["failed"] == [] for o in outs)
    # 잠금 안에서 진 쪽의 원본은 보상 삭제했다
    assert store.keys() == {r.original_object_key for r in rows}
    assert len(store.deleted) >= 1
    assert sorted(sid for sid, _ in ingest) == sorted(str(r.id) for r in rows)



# ---------- 리뷰 반영(M1~M4) ----------


async def test_items_beyond_the_limit_are_processed_by_the_next_run(
    db, user_a: User, store: Store, ingest: list
) -> None:
    """처리 상한(기본 200)은 새 항목 수다 — 같은 201개 목록을 다시 돌리면 201번째가 생긴다(M1)."""
    assert get_settings().agent_save_sources_max_items == 200
    ids = [f"r-{i:03d}" for i in range(201)]
    agent = await _agent(db, _constant_wf(_items(*ids)))
    nb = await _notebook(db, user_a)

    first = await _execute(db, agent, nb, user_a)
    assert len(first["created"]) == 200 and first["skipped_duplicate"] == []
    assert (first["truncated"], first["total"]) == (True, 201)

    second = await _execute(db, agent, nb, user_a)
    assert [c["external_id"] for c in second["created"]] == ["r-200"]
    assert len(second["skipped_duplicate"]) == 200
    assert (second["truncated"], second["total"]) == (False, 201)
    assert len(await _sources(nb.id)) == 201


async def test_a_save_error_fails_only_that_item(
    db, user_a: User, store: Store, ingest: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """원본 업로드 실패·DB 오류는 그 항목만 `save_failed` — 나머지는 계속한다(M3)."""
    nb = await _notebook(db, user_a)
    other = await _notebook(db, user_a, "다른 노트북")
    taken = Source(notebook_id=other.id, type="text", title="이미 쓰는 id", status="ready")
    db.add(taken)
    await db.commit()

    real_put = store.put

    def put(bucket: str, key: str, data: bytes, content_type: str = "") -> None:
        if data == "본문 storage".encode():
            raise OSError("스토리지 업로드 재시도 후 실패")
        real_put(bucket, key, data, content_type)

    monkeypatch.setattr("biznotes_core.services.storage.put_object", put)
    # 둘째 항목의 새 id를 이미 있는 행의 id로 — INSERT가 실제 PK 위반으로 실패한다
    ids = iter([uuid.uuid4(), taken.id])
    monkeypatch.setattr(
        source_create,
        "uuid",
        types.SimpleNamespace(UUID=uuid.UUID, uuid4=lambda: next(ids, None) or uuid.uuid4()),
    )
    agent = await _agent(db, _constant_wf(_items("storage", "db", "ok")))
    out = await _execute(db, agent, nb, user_a)

    assert [(f["external_id"], f["code"]) for f in out["failed"]] == [
        ("storage", "save_failed"),
        ("db", "save_failed"),
    ]
    assert {f["message"] for f in out["failed"]} == {"소스를 저장하지 못했습니다"}
    assert [c["external_id"] for c in out["created"]] == ["ok"]
    rows = await _sources(nb.id)
    assert [r.external_ref for r in rows] == [_ref("ok")]
    assert store.keys() == {rows[0].original_object_key}  # 실패 항목의 원본은 보상 삭제했다


async def test_cancel_while_waiting_for_the_lock_leaves_no_object(
    db, user_a: User, store: Store, ingest: list
) -> None:
    """원본을 올린 뒤 잠금을 기다리다 취소돼도(lease 회수) 원본·행이 남지 않는다(M4)."""
    nb = await _notebook(db, user_a)
    agent = await _agent(db, _constant_wf(_items("a")))
    run = await _run_row(db, agent, nb, user_a)

    async def one() -> None:
        async with get_session_factory()() as s:
            await execute_agent(_ctx(s, run, user_a), agent.kind, agent.definition)

    async with get_session_factory()() as holder:
        pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        await enqueue.lock_notebook_sources(holder, nb.id)
        task = asyncio.create_task(one())
        await _wait_until_waiters(pid, 1)
        assert len(store.puts) == 1  # 원본은 이미 올라갔다
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await holder.commit()

    assert store.keys() == set() and store.deleted == store.puts
    assert await _sources(nb.id) == [] and ingest == []


async def test_duplicate_is_checked_before_capacity_inside_the_lock(db, user_a: User) -> None:
    """꽉 찬 노트북에 이미 있는 키는 `full`이 아니라 `duplicate`다 — 넘김을 일으키지 않는다(M2)."""
    nb = await _notebook(db, user_a)
    db.add(
        Source(
            notebook_id=nb.id, type="text", title="기존", status="ready", external_ref=_ref("k")
        )
    )
    await db.commit()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(get_settings(), "max_sources_per_notebook", 1)
        async with enqueue.ingest_uow(db) as uow:
            dup = await source_create.admit_source(
                db,
                uow,
                Source(notebook_id=nb.id, type="text", title="새", external_ref=_ref("k")),
            )
            full = await source_create.admit_source(
                db,
                uow,
                Source(notebook_id=nb.id, type="text", title="새", external_ref=_ref("new")),
            )
    assert (dup.outcome, dup.notebook_id) == ("duplicate", nb.id)
    assert full.outcome == "full"
    assert len(await _sources(nb.id)) == 1
