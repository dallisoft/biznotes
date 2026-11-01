# 신규(#403 PR-C). 필요: PostgreSQL·Valkey(PG 테스트).
"""`save_sources`의 `notebook_rule` — 실제 DB·잠금으로 본다(계획 save-sources-node.md §5).

월별·일별(#434) 노트북 탐색·생성, 상한 넘김 `(n)`, 소유자 범위, 계열 잠금, 계열 단위 중복.
외부 경계(스토리지·인제스트 발행)만 대역이고 시각은 노드의 시계 함수를 고정한다. `create_all`
기반이라 Alembic 마이그레이션 검증이 아니다.
"""

import asyncio
import contextlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import share_to_user
from sqlalchemy import select, text

from biznotes_core.config import get_settings
from biznotes_core.core.db import get_session_factory
from biznotes_core.models import Agent, AgentRun, Notebook, Source, User
from biznotes_core.services import notebook_series
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.graph import execute_agent
from biznotes_core.services.agents.nodes import save_sources
from biznotes_core.services.ingestion import enqueue

NAMESPACE = "naver-research"
TEMPLATE = "종목분석 {month}"
# 2026-10-31 14:00 UTC = 23:00 KST(10월), 15:30 UTC = 다음 날 00:30 KST(11월)
OCT = datetime(2026, 10, 31, 14, 0, tzinfo=UTC)
NOV = datetime(2026, 10, 31, 15, 30, tzinfo=UTC)


# ---------- 외부 경계·시계 ----------


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    objects: dict[str, bytes] = {}
    monkeypatch.setattr("biznotes_core.services.storage.ensure_buckets", lambda: None)
    monkeypatch.setattr(
        "biznotes_core.services.storage.put_object",
        lambda bucket, key, data, content_type="": objects.__setitem__(key, data),
    )
    monkeypatch.setattr(
        "biznotes_core.services.storage.delete_object", lambda bucket, key: objects.pop(key, None)
    )
    monkeypatch.setattr(enqueue, "_publish", lambda source_id, task_id, queue=None: None)
    return objects


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    now = [OCT]
    monkeypatch.setattr(save_sources, "_utcnow", lambda: now[0])
    return now


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
    """`save_sources` 출력을 run id별로 남긴다 — 실행기 자체는 그대로 돈다."""
    from biznotes_core.services.agents import graph

    real = save_sources._exec_save_sources

    async def wrapped(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
        out = await real(ctx, node, state)
        _captured[ctx.run_id] = out
        return out

    monkeypatch.setitem(graph.NODE_EXECUTORS, "save_sources", wrapped)
    _captured.clear()


# ---------- 헬퍼 ----------


def _items(*ids: str) -> list[dict[str, Any]]:
    return [{"title": f"리포트 {i}", "external_id": i, "content": f"본문 {i}"} for i in ids]


def _definition(items: list[Any], rule: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "nodes": [
            {"id": "list", "type": "constant", "value": {"text": json.dumps(items)}},
            {
                "id": "save",
                "type": "save_sources",
                "items": "{list.text}",
                "namespace": NAMESPACE,
                "requires_approval": False,
                "notebook_rule": rule
                or {"title_template": TEMPLATE, "timezone": "Asia/Seoul", "emoji": "📈"},
            },
        ],
        "edges": [["list", "save"]],
    }


async def _agent(db: Any, definition: dict[str, Any]) -> Agent:
    agent = Agent(
        name=f"rule-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        definition=definition,
        params_schema={},
        scope="system",
        enabled=True,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


async def _notebook(db: Any, owner: User, title: str, sources: int = 0, ref: str | None = None):
    nb = Notebook(owner_id=owner.id, title=title)
    db.add(nb)
    await db.flush()
    for i in range(sources):
        db.add(
            Source(
                notebook_id=nb.id,
                type="text",
                title=f"기존 {i}",
                status="ready",
                external_ref=ref if i == 0 else None,
            )
        )
    await db.commit()
    await db.refresh(nb)
    return nb


async def _anchor(db: Any, user: User) -> Notebook:
    """스케줄 앵커 노트북 — 규칙이 있으면 대상이 아니다."""
    return await _notebook(db, user, "앵커")


async def _run_row(db: Any, agent: Agent, anchor: Notebook, user: User) -> AgentRun:
    run = AgentRun(
        agent_id=agent.id, notebook_id=anchor.id, user_id=user.id, status="running",
        trigger="schedule",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


def _ctx(db: Any, run: AgentRun, user: User) -> RunContext:
    return RunContext(
        run_id=run.id,
        notebook_id=run.notebook_id,
        db=db,
        params={},
        trigger=run.trigger,
        user_id=user.id,
        user_role=user.role,
        agent_id=run.agent_id,
        lease_valid=lambda: True,
    )


async def _execute(db: Any, agent: Agent, anchor: Notebook, user: User) -> dict[str, Any]:
    run = await _run_row(db, agent, anchor, user)
    await execute_agent(_ctx(db, run, user), agent.kind, agent.definition)
    return _captured.pop(run.id)


async def _notebooks(owner: User) -> dict[str, Notebook]:
    async with get_session_factory()() as s:
        rows = await s.scalars(select(Notebook).where(Notebook.owner_id == owner.id))
        return {nb.title: nb for nb in rows.all()}


async def _refs(notebook_id: uuid.UUID) -> list[str | None]:
    async with get_session_factory()() as s:
        rows = await s.scalars(
            select(Source.external_ref)
            .where(Source.notebook_id == notebook_id, Source.external_ref.is_not(None))
            .order_by(Source.created_at, Source.id)
        )
        return list(rows.all())


def _ref(external_id: str) -> str:
    return f"mcp:{NAMESPACE}:{external_id}"


# ---------- 달이 바뀌면 새 노트북, 계열 전체 중복 ----------


async def test_month_change_creates_a_new_notebook_and_dedupes_across_months(
    db, user_a: User, store: dict, clock: list
) -> None:
    anchor = await _anchor(db, user_a)
    oct_out = await _execute(db, await _agent(db, _definition(_items("a", "b"))), anchor, user_a)

    books = await _notebooks(user_a)
    october = books["종목분석 2026-10"]  # Asia/Seoul 기준 10월 31일 23시
    assert (october.owner_id, october.emoji) == (user_a.id, "📈")
    assert await _refs(october.id) == [_ref("a"), _ref("b")]
    assert oct_out["notebook_ids"] == [str(october.id)]
    assert await _refs(anchor.id) == []  # 앵커에는 쓰지 않는다

    clock[0] = NOV  # KST 자정을 넘겼다
    nov_out = await _execute(
        db, await _agent(db, _definition(_items("b", "c"))), anchor, user_a
    )
    november = (await _notebooks(user_a))["종목분석 2026-11"]
    assert await _refs(november.id) == [_ref("c")]
    # 전월 노트북에 있던 키는 건너뛴다(계열 단위 중복)
    assert [(s["external_id"], s["notebook_id"]) for s in nov_out["skipped_duplicate"]] == [
        ("b", str(october.id))
    ]
    assert nov_out["notebook_ids"] == [str(november.id)]


# ---------- 일별 계열(#434) — 날이 바뀌면 새 노트북, 월별 계열과 섞이지 않는다 ----------


async def test_daily_rule_creates_a_notebook_per_day_apart_from_the_monthly_series(
    db, user_a: User, store: dict, clock: list
) -> None:
    daily = {"title_template": "종목분석 {date}", "timezone": "Asia/Seoul"}
    anchor = await _anchor(db, user_a)
    monthly = await _notebook(db, user_a, "종목분석 2026-10", sources=1, ref=_ref("m"))

    # 10-31 23:00 KST — 월별 노트북의 키는 일별 계열의 중복이 아니다
    first = await _execute(
        db, await _agent(db, _definition(_items("a", "m"), daily)), anchor, user_a
    )
    oct31 = (await _notebooks(user_a))["종목분석 2026-10-31"]
    assert (oct31.owner_id, first["notebook_ids"]) == (user_a.id, [str(oct31.id)])
    assert await _refs(oct31.id) == [_ref("a"), _ref("m")]
    assert first["skipped_duplicate"] == []

    clock[0] = NOV  # 11-01 00:30 KST — 다음 날 노트북, 전날 키는 건너뛴다(계열 단위 중복)
    second = await _execute(
        db, await _agent(db, _definition(_items("a", "b"), daily)), anchor, user_a
    )
    nov1 = (await _notebooks(user_a))["종목분석 2026-11-01"]
    assert await _refs(nov1.id) == [_ref("b")]
    assert [(s["external_id"], s["notebook_id"]) for s in second["skipped_duplicate"]] == [
        ("a", str(oct31.id))
    ]

    # 같은 접두의 월별 규칙은 일별 노트북을 대상·중복 범위로 보지 않는다
    by_month = await _execute(
        db, await _agent(db, _definition(_items("a", "m"))), anchor, user_a
    )
    november = (await _notebooks(user_a))["종목분석 2026-11"]
    assert await _refs(november.id) == [_ref("a")]
    assert [(s["external_id"], s["notebook_id"]) for s in by_month["skipped_duplicate"]] == [
        ("m", str(monthly.id))
    ]
    assert await _refs(monthly.id) == [_ref("m")]
    assert await _refs(oct31.id) == [_ref("a"), _ref("m")]  # 그대로
    assert len(await _notebooks(user_a)) == 5  # 앵커·월별 2·일별 2


# ---------- 상한 넘김 ----------


async def test_rollover_within_one_run(
    db, user_a: User, store: dict, clock: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "max_sources_per_notebook", 2)
    anchor = await _anchor(db, user_a)
    out = await _execute(
        db, await _agent(db, _definition(_items("a", "b", "c", "d", "e"))), anchor, user_a
    )

    books = await _notebooks(user_a)
    titles = ["종목분석 2026-10", "종목분석 2026-10 (2)", "종목분석 2026-10 (3)"]
    assert [await _refs(books[t].id) for t in titles] == [
        [_ref("a"), _ref("b")],
        [_ref("c"), _ref("d")],
        [_ref("e")],
    ]
    assert out["failed"] == [] and len(out["created"]) == 5
    assert out["notebook_ids"] == [str(books[t].id) for t in titles]
    assert {books[t].emoji for t in titles} == {"📈"}


async def test_writes_to_the_largest_existing_suffix(
    db, user_a: User, store: dict, clock: list
) -> None:
    anchor = await _anchor(db, user_a)
    base = await _notebook(db, user_a, "종목분석 2026-10")
    second = await _notebook(db, user_a, "종목분석 2026-10 (2)", sources=1)
    await _execute(db, await _agent(db, _definition(_items("a"))), anchor, user_a)

    assert await _refs(second.id) == [_ref("a")]
    assert await _refs(base.id) == []
    assert len(await _notebooks(user_a)) == 3  # 새로 만들지 않았다


async def test_suffix_limit_fails_as_notebook_full(
    db, user_a: User, store: dict, clock: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "max_sources_per_notebook", 1)
    anchor = await _anchor(db, user_a)
    await _notebook(db, user_a, "종목분석 2026-10 (99)", sources=1)
    out = await _execute(db, await _agent(db, _definition(_items("a"))), anchor, user_a)

    assert [(f["external_id"], f["code"]) for f in out["failed"]] == [("a", "notebook_full")]
    assert "종목분석 2026-10 (100)" not in await _notebooks(user_a)
    assert store == {}  # 원본은 보상 삭제했다


# ---------- 소유자 범위 ----------


async def test_rule_never_picks_another_users_notebook(
    db, user_a: User, user_b: User, admin_user: User, store: dict, clock: list
) -> None:
    """같은 제목이어도 run 사용자가 실제로 소유한 노트북만 고른다.

    쓰기 공유·admin 승격과 무관하다.
    """
    others = await _notebook(db, user_b, "종목분석 2026-10", sources=1, ref=_ref("a"))
    db.add_all(share_to_user(others.id, user_a.id, "write"))
    await db.commit()

    by_a = await _execute(
        db, await _agent(db, _definition(_items("a"))), await _anchor(db, user_a), user_a
    )
    mine = (await _notebooks(user_a))["종목분석 2026-10"]
    assert mine.id != others.id
    # 남의 노트북에 있는 키는 중복으로 보지 않는다(계열은 소유자 범위)
    assert [c["notebook_id"] for c in by_a["created"]] == [str(mine.id)]

    await _execute(
        db, await _agent(db, _definition(_items("b"))), await _anchor(db, admin_user), admin_user
    )
    admins = (await _notebooks(admin_user))["종목분석 2026-10"]
    assert admins.id not in {mine.id, others.id}
    assert await _refs(admins.id) == [_ref("b")]
    assert await _refs(others.id) == [_ref("a")]  # 그대로
    assert await _refs(mine.id) == [_ref("a")]


# ---------- 계열 범위 ----------


async def test_series_scope_covers_suffixes_and_other_months_only(
    db, user_a: User, store: dict, clock: list
) -> None:
    anchor = await _anchor(db, user_a)
    await _notebook(db, user_a, "종목분석 2026-09 (2)", sources=1, ref=_ref("old"))
    outside = await _notebook(db, user_a, "종목분석 2026-10 백업", sources=1, ref=_ref("x"))
    out = await _execute(db, await _agent(db, _definition(_items("old", "x"))), anchor, user_a)

    assert [s["external_id"] for s in out["skipped_duplicate"]] == ["old"]
    assert [c["external_id"] for c in out["created"]] == ["x"]  # 계열 밖 제목은 범위가 아니다
    assert await _refs(outside.id) == [_ref("x")]


async def test_like_wildcards_in_the_template_are_literal(
    db, user_a: User, store: dict, clock: list
) -> None:
    template = "a_b%c\\d {month}"
    anchor = await _anchor(db, user_a)
    existing = await _notebook(db, user_a, "a_b%c\\d 2026-10")
    decoy = await _notebook(db, user_a, "aXbYc\\d 2026-10")  # LIKE 와일드카드면 맞는다
    rule = {"title_template": template, "timezone": "Asia/Seoul"}
    await _execute(db, await _agent(db, _definition(_items("a"), rule)), anchor, user_a)

    assert await _refs(existing.id) == [_ref("a")]
    assert await _refs(decoy.id) == []
    assert len(await _notebooks(user_a)) == 3


# ---------- 동시 두 run — 새 노트북은 하나 ----------


async def _wait_until_waiters(holder_pid: int, count: int) -> None:
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


async def test_concurrent_runs_create_one_notebook_and_no_duplicates(
    db, user_a: User, store: dict, clock: list
) -> None:
    ids = ("c-1", "c-2", "c-3")
    anchor = await _anchor(db, user_a)
    agent = await _agent(db, _definition(_items(*ids)))
    runs = [await _run_row(db, agent, anchor, user_a) for _ in range(2)]

    async def one(run: AgentRun) -> None:
        async with get_session_factory()() as s:
            await execute_agent(_ctx(s, run, user_a), agent.kind, agent.definition)

    tasks: list[asyncio.Task[None]] = []
    try:
        # 두 run이 계열 잠금에서 함께 기다리게 한다 — 노트북이 아직 없다
        async with get_session_factory()() as holder:
            pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            await notebook_series.lock_series(holder, user_a.id, TEMPLATE)
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

    books = await _notebooks(user_a)
    assert sorted(books) == ["앵커", "종목분석 2026-10"]  # 같은 새 노트북을 두 번 만들지 않았다
    assert sorted(await _refs(books["종목분석 2026-10"].id)) == sorted(_ref(i) for i in ids)
    outs = [_captured[r.id] for r in runs]
    assert sum(len(o["created"]) for o in outs) == 3
    assert all(o["failed"] == [] for o in outs)
    async with get_session_factory()() as s:
        keys = set((await s.scalars(select(Source.original_object_key))).all()) - {None}
    assert set(store) == keys  # 진 쪽 원본은 보상 삭제했다


async def test_racing_duplicate_at_capacity_creates_no_empty_notebook(
    db, user_a: User, store: dict, clock: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """꽉 찬 노트북에서 두 run이 같은 항목을 다투면 이긴 쪽만 `(2)`를 만든다.

    진 쪽은 계열 잠금 안에서 중복을 먼저 보고 멈춘다 — 넘김 노트북 `(3)`을 만들지 않는다.
    """
    monkeypatch.setattr(get_settings(), "max_sources_per_notebook", 1)
    anchor = await _anchor(db, user_a)
    await _notebook(db, user_a, "종목분석 2026-10", sources=1)
    agent = await _agent(db, _definition(_items("c-1")))
    runs = [await _run_row(db, agent, anchor, user_a) for _ in range(2)]

    async def one(run: AgentRun) -> None:
        async with get_session_factory()() as s:
            await execute_agent(_ctx(s, run, user_a), agent.kind, agent.definition)

    tasks: list[asyncio.Task[None]] = []
    try:
        async with get_session_factory()() as holder:
            pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            await notebook_series.lock_series(holder, user_a.id, TEMPLATE)
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

    books = await _notebooks(user_a)
    assert sorted(books) == ["앵커", "종목분석 2026-10", "종목분석 2026-10 (2)"]
    assert await _refs(books["종목분석 2026-10 (2)"].id) == [_ref("c-1")]
    outs = [_captured[r.id] for r in runs]
    assert sorted((len(o["created"]), len(o["skipped_duplicate"])) for o in outs) == [
        (0, 1),
        (1, 0),
    ]


# ---------- 리뷰 반영(M1·M2) ----------


async def test_existing_series_items_do_not_use_the_processing_limit(
    db, user_a: User, store: dict, clock: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """처리 상한은 새 항목 수다 — 계열(전월 포함)에 이미 있는 항목은 세지 않는다(M1)."""
    monkeypatch.setattr(get_settings(), "agent_save_sources_max_items", 2)
    anchor = await _anchor(db, user_a)
    september = await _notebook(db, user_a, "종목분석 2026-09")
    db.add_all(
        [
            Source(
                notebook_id=september.id, type="text", title=i, status="ready",
                external_ref=_ref(i),
            )
            for i in ("a", "b")
        ]
    )
    await db.commit()
    agent = await _agent(db, _definition(_items("a", "b", "c", "d", "e")))

    first = await _execute(db, agent, anchor, user_a)
    assert [s["external_id"] for s in first["skipped_duplicate"]] == ["a", "b"]
    assert [c["external_id"] for c in first["created"]] == ["c", "d"]
    assert first["truncated"] is True

    second = await _execute(db, agent, anchor, user_a)
    assert [c["external_id"] for c in second["created"]] == ["e"]
    assert second["truncated"] is False


async def test_direct_run_racing_a_rule_run_creates_no_empty_rollover(
    db, user_a: User, store: dict, clock: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """규칙 없는 직접 실행이 같은 키를 마지막 자리에 넣는 동안 규칙 실행이 노트북 잠금을 기다린다.

    잠금을 얻은 규칙 실행은 중복을 용량보다 먼저 본다 — `full`로 오판해 빈 `(2)`를 만들지 않고
    `skipped_duplicate`다(M2). 순서는 `pg_blocking_pids` 대기로 고정한다.
    """
    monkeypatch.setattr(get_settings(), "max_sources_per_notebook", 2)
    target = await _notebook(db, user_a, "종목분석 2026-10", sources=1)  # 한 자리 남음
    item = _items("k")
    direct_definition = _definition(item)
    del direct_definition["nodes"][1]["notebook_rule"]  # 대상 = run 노트북
    direct = await _agent(db, direct_definition)
    by_rule = await _agent(db, _definition(item))
    runs = [await _run_row(db, a, target, user_a) for a in (direct, by_rule)]

    async def one(agent: Agent, run: AgentRun) -> None:
        async with get_session_factory()() as s:
            await execute_agent(_ctx(s, run, user_a), agent.kind, agent.definition)

    tasks: list[asyncio.Task[None]] = []
    try:
        async with get_session_factory()() as holder:
            pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            await enqueue.lock_notebook_sources(holder, target.id)
            # 직접 실행이 먼저 줄을 서고, 규칙 실행은 계열 잠금·계열 중복 확인을 지나 뒤에 선다
            tasks.append(asyncio.create_task(one(direct, runs[0])))
            await _wait_until_waiters(pid, 1)
            tasks.append(asyncio.create_task(one(by_rule, runs[1])))
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

    assert sorted(await _notebooks(user_a)) == ["종목분석 2026-10"]  # 빈 (2)가 없다
    assert await _refs(target.id) == [_ref("k")]
    direct_out, rule_out = (_captured[r.id] for r in runs)
    assert [c["external_id"] for c in direct_out["created"]] == ["k"]
    assert rule_out["created"] == [] and rule_out["failed"] == []
    assert [(s["external_id"], s["notebook_id"]) for s in rule_out["skipped_duplicate"]] == [
        ("k", str(target.id))
    ]
    async with get_session_factory()() as s:
        keys = set((await s.scalars(select(Source.original_object_key))).all()) - {None}
    assert set(store) == keys  # 진 쪽 원본은 보상 삭제했다
