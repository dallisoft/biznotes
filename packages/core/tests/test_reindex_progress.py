"""재인덱싱 진행률 — run·항목 기록과 API 조립 (#5).

SQL 동작(중복 제외·집계·reaper)은 PostgreSQL이 필요해 소비 제품 통합 검증 몫이다.
여기서는 DB 없이 스키마 계약, 진행률 조립, 워커 기록의 실패 격리를 확인한다.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog.testing
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from biznotes_core.api.v1.admin import rag as rag_api
from biznotes_core.models import ReindexRun, ReindexRunItem
from biznotes_core.services.ingestion.reindex import (
    HARD_STALLED_MULTIPLIER,
    IN_FLIGHT_STATUSES,
    MAX_REINDEX_ATTEMPTS,
    ReindexProgress,
    stalled_reindex_items,
)
from biznotes_core.workers.tasks import ingestion


def test_reindex_tables_schema():
    items = ReindexRunItem.__table__
    fks = {fk.parent.name: (fk.column.table.name, fk.ondelete) for fk in items.foreign_keys}

    # 소스가 지워져도 항목은 남는다(#179) — run 집계가 줄지 않는다
    assert fks == {"run_id": ("reindex_runs", "CASCADE"), "source_id": ("sources", "SET NULL")}
    assert items.c.source_id.nullable and not items.c.run_id.nullable
    assert {c.name for c in items.constraints if c.name} >= {"uq_reindex_run_items_run_source"}
    assert items.c.run_id.index and items.c.source_id.index
    ddl = str(CreateTable(ReindexRun.__table__).compile(dialect=postgresql.dialect()))
    assert "options JSONB NOT NULL" in ddl


def _run(**kw: Any) -> ReindexRun:
    now = datetime.now(UTC)
    return ReindexRun(
        id=uuid.uuid4(),
        notebook_id=None,
        options=kw.get("options", {"failed_only": True}),
        requested=kw.get("requested", 3),
        created_at=now - timedelta(minutes=5),
    )


async def test_runs_out_derives_finished_at_from_last_item(monkeypatch):
    running, finished, empty = _run(), _run(), _run(requested=0)
    last = datetime.now(UTC)

    async def fake_progress(db: Any, run_ids: list[uuid.UUID]) -> dict:
        return {
            running.id: ReindexProgress(queued=1, done=1, failed=1, last_update=last),
            finished.id: ReindexProgress(queued=0, done=2, failed=1, last_update=last),
        }

    monkeypatch.setattr(rag_api, "reindex_progress", fake_progress)

    out = await rag_api._runs_out(None, [running, finished, empty])  # type: ignore[arg-type]

    assert (out[0].queued, out[0].done, out[0].failed, out[0].finished_at) == (1, 1, 1, None)
    assert out[0].failed_only is True
    assert (out[1].queued, out[1].finished_at) == (0, last)
    # 항목이 없는 run은 생성 시각에 끝난 것으로 본다 — last_update가 None이라 합성값임을 안다
    assert (out[2].queued, out[2].finished_at, out[2].last_update) == (0, empty.created_at, None)


async def test_runs_out_exposes_snapshot_and_removed_counts(monkeypatch):
    """#179 — 후보 수·범위 스냅샷·제외·재발행 집계. 컬럼이 생기기 전 run은 None(알 수 없음)."""
    new = _run(requested=4, options={"stale_only": True})
    new.skipped, new.scope, new.notebook_title = 2, "notebook", "지워진 노트북"
    legacy = _run(requested=3)
    last = datetime.now(UTC)

    async def fake_progress(db: Any, run_ids: list[uuid.UUID]) -> dict:
        return {
            new.id: ReindexProgress(
                queued=0, done=1, failed=1, removed=2, retrying=0, last_update=last
            ),
            legacy.id: ReindexProgress(queued=2, done=1, retrying=1, last_update=last),
        }

    monkeypatch.setattr(rag_api, "reindex_progress", fake_progress)

    new_out, legacy_out = await rag_api._runs_out(None, [new, legacy])  # type: ignore[arg-type]

    assert new_out.requested == new_out.queued + new_out.done + new_out.failed + new_out.removed
    assert new_out.finished_at == last  # 제외된 항목만 남으면 끝난 run이다
    assert (new_out.skipped, new_out.candidates, new_out.scope, new_out.notebook_title) == (
        2,
        6,
        "notebook",
        "지워진 노트북",
    )
    assert (new_out.stale_only, new_out.max_attempts, new_out.last_update) == (
        True,
        MAX_REINDEX_ATTEMPTS,
        last,
    )
    assert (legacy_out.skipped, legacy_out.candidates, legacy_out.scope) == (None, None, None)
    assert (legacy_out.retrying, legacy_out.finished_at) == (1, None)


async def test_close_reindex_failure_does_not_break_ingestion(monkeypatch):
    """진행률 기록이 실패해도 인제스천 결과(성공/실패)는 그대로여야 한다."""

    def broken_factory():
        raise RuntimeError("db down")

    monkeypatch.setattr(ingestion, "get_session_factory", broken_factory)

    await ingestion._close_reindex(uuid.uuid4(), ok=True, error=None)  # 예외가 새지 않는다


async def test_close_reindex_uses_its_own_session(monkeypatch):
    calls: list[tuple[uuid.UUID, bool, str | None]] = []

    class _Session:
        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    async def fake_close(db: Any, source_id: uuid.UUID, *, ok: bool, error: str | None) -> None:
        assert isinstance(db, _Session)
        calls.append((source_id, ok, error))

    monkeypatch.setattr(ingestion, "get_session_factory", lambda: _Session)
    monkeypatch.setattr(ingestion, "close_reindex_items", fake_close)
    sid = uuid.uuid4()

    await ingestion._close_reindex(sid, ok=False, error="boom")

    assert calls == [(sid, False, "boom")]


class _StallDB:
    """대기열 흐름 판정(scalar)과 후보 조회(첫 execute)에 답하고 이후 UPDATE 문을 기록하는 대역.

    scalar는 호출 순서로 답한다 — 1) 대기열 흐름 2) 흐를 때만 묻는 "미룬 항목 유무"(#43).
    """

    def __init__(
        self,
        pairs: list[tuple[ReindexRunItem, ReindexRun]],
        *,
        flowing: bool = False,
        deferred: bool = True,
    ) -> None:
        self.pairs = pairs
        self.flowing = flowing
        self.deferred = deferred
        self.flow_checks: list[Any] = []
        self.statements: list[Any] = []
        self.plan_modes: list[str] = []  # 스윕 첫 문장의 계획 모드 고정(#44 D4)

    async def scalar(self, stmt: Any) -> bool:
        self.flow_checks.append(stmt)
        return self.flowing if len(self.flow_checks) == 1 else self.deferred

    async def execute(self, stmt: Any) -> Any:
        if str(stmt).startswith("SET LOCAL"):
            if self.flow_checks or self.statements:
                raise AssertionError("계획 모드는 스윕의 첫 문장이어야 한다")
            self.plan_modes.append(str(stmt))
            return None
        self.statements.append(stmt)
        if len(self.statements) > 1:
            return None
        pairs = self.pairs

        class _Result:
            def tuples(self) -> Any:
                return self

            def all(self) -> list[tuple[ReindexRunItem, ReindexRun]]:
                return pairs

        return _Result()


def _sql(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


async def test_stalled_items_cap_republish_by_attempts():
    """#25 — 상한에 닿은 항목은 발행 대상에서 빠지고 실패로 닫히며, 나머지는 attempts가 는다."""
    run = _run()
    fresh = ReindexRunItem(id=uuid.uuid4(), run_id=run.id, source_id=uuid.uuid4(), attempts=0)
    last = ReindexRunItem(
        id=uuid.uuid4(), run_id=run.id, source_id=uuid.uuid4(), attempts=MAX_REINDEX_ATTEMPTS - 1
    )
    spent = ReindexRunItem(
        id=uuid.uuid4(), run_id=run.id, source_id=uuid.uuid4(), attempts=MAX_REINDEX_ATTEMPTS
    )
    db = _StallDB([(fresh, run), (last, run), (spent, run)])

    out = await stalled_reindex_items(db, datetime.now(UTC))  # type: ignore[arg-type]

    assert [item for item, _ in out] == [fresh, last]
    (flow_sql,) = (_sql(s) for s in db.flow_checks)
    select_sql, fail_sql, retry_sql = (_sql(s) for s in db.statements)
    # 대기열 흐름은 워커 처리 상태(pending 제외)와 닫힌 항목으로 판정한다
    assert "sources.updated_at >=" in flow_sql and "reindex_run_items.status !=" in flow_sql
    assert db.flow_checks[0].compile().params["status_1"] == ["parsing", "chunking", "embedding"]
    # 처리 중인 소스의 항목은 재발행하지 않는다(소스 reaper 몫)
    assert "sources.status NOT IN" in select_sql
    assert db.statements[1].compile().params["status"] == "failed"
    # 조회 뒤 워커가 닫은 항목을 덮어쓰지 않도록 두 UPDATE 모두 queued 조건을 다시 건다
    assert "reindex_run_items.status = " in fail_sql
    assert "reindex_run_items.status = " in retry_sql
    assert "attempts=(reindex_run_items.attempts + " in retry_sql


async def test_flowing_queue_republishes_nothing():
    """#25 — 워커가 최근에 무언가를 처리했으면 queued 항목은 차례를 기다리는 것이다."""
    run = _run()
    item = ReindexRunItem(id=uuid.uuid4(), run_id=run.id, source_id=uuid.uuid4(), attempts=0)
    db = _StallDB([(item, run)], flowing=True)

    assert await stalled_reindex_items(db, datetime.now(UTC)) == []  # type: ignore[arg-type]
    assert db.statements == []


async def test_deferred_recovery_is_logged_with_stalled_items():
    """#43 — 흐름 때문에 미뤘는데 cutoff보다 오래 멈춘 queued 항목이 있으면 경고를 남긴다."""
    run = _run()
    item = ReindexRunItem(id=uuid.uuid4(), run_id=run.id, source_id=uuid.uuid4(), attempts=0)
    db = _StallDB([(item, run)], flowing=True, deferred=True)
    cutoff = datetime.now(UTC)

    with structlog.testing.capture_logs() as logs:
        assert await stalled_reindex_items(db, cutoff) == []  # type: ignore[arg-type]

    # 미룬 항목 판정은 부분 인덱스를 타는 queued 조건이어야 한다
    deferred_sql = _sql(db.flow_checks[1])
    assert "reindex_run_items.status = " in deferred_sql
    assert "reindex_run_items.updated_at < " in deferred_sql
    warnings = [e for e in logs if e["log_level"] == "warning"]
    assert [e["event"] for e in warnings] == ["재인덱싱 유실 회수 보류 — 대기열이 흐르는 중"]
    assert warnings[0]["hard_cutoff"] is None
    assert db.statements == []


async def test_flowing_queue_without_stalled_items_stays_quiet():
    """#43 — 미룬 항목이 없으면(정상 진행) 경고를 남기지 않는다 — 5분마다 도는 스윕이다."""
    db = _StallDB([], flowing=True, deferred=False)

    with structlog.testing.capture_logs() as logs:
        assert await stalled_reindex_items(db, datetime.now(UTC)) == []  # type: ignore[arg-type]

    assert [e for e in logs if e["log_level"] == "warning"] == []
    assert db.statements == []


async def test_hard_cutoff_republishes_despite_flowing_queue():
    """#43 — hard_cutoff보다 오래 멈춘 run은 대기열 흐름과 무관하게 재발행한다."""
    run = _run()
    item = ReindexRunItem(id=uuid.uuid4(), run_id=run.id, source_id=uuid.uuid4(), attempts=0)
    db = _StallDB([(item, run)], flowing=True, deferred=True)
    cutoff = datetime.now(UTC)
    hard_cutoff = cutoff - timedelta(minutes=45)

    with structlog.testing.capture_logs() as logs:
        out = await stalled_reindex_items(db, cutoff, hard_cutoff)  # type: ignore[arg-type]

    assert [item for item, _ in out] == [item]
    # 후보 조회의 정체 판정은 hard_cutoff로 한다 — cutoff로 재발행하면 #25 회귀다
    params = db.statements[0].compile().params
    assert hard_cutoff in params.values()
    assert cutoff not in params.values()
    # 미룬 사실은 상한 회수와 별개로 드러난다(다른 run은 여전히 미뤄진다)
    warnings = [e for e in logs if e["log_level"] == "warning"]
    assert warnings[0]["hard_cutoff"] == hard_cutoff.isoformat()
    retry_sql = _sql(db.statements[1])
    assert "attempts=(reindex_run_items.attempts + " in retry_sql


async def test_hard_cutoff_is_a_multiple_of_the_stall_window():
    """#43 — 상한은 cutoff의 배수다: 15분 창 × 4 = 60분."""
    assert HARD_STALLED_MULTIPLIER > 1
    assert ingestion.STALLED_THRESHOLD_MINUTES * HARD_STALLED_MULTIPLIER == 60


async def test_reaper_passes_soft_and_hard_cutoff(monkeypatch):
    """#43 — 워커는 회수 cutoff와 절대 상한을 함께 넘긴다."""

    class _Empty:
        def scalars(self) -> Any:
            return self

        def tuples(self) -> Any:
            return self

        def all(self) -> list[Any]:
            return []

    class _Session:
        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def execute(self, stmt: Any) -> Any:
            return _Empty()

        async def commit(self) -> None:
            return None

    calls: list[tuple[datetime, datetime | None]] = []

    async def fake_stalled(db: Any, cutoff: datetime, hard_cutoff: datetime | None = None) -> list:
        calls.append((cutoff, hard_cutoff))
        return []

    monkeypatch.setattr(ingestion, "get_session_factory", lambda: _Session)
    monkeypatch.setattr(ingestion, "stalled_reindex_items", fake_stalled)

    before = datetime.now(UTC)
    assert await ingestion._reap_stalled() == {"requeued": 0, "reindex_requeued": 0}
    after = datetime.now(UTC)

    (cutoff, hard_cutoff) = calls[0]
    window = timedelta(minutes=ingestion.STALLED_THRESHOLD_MINUTES)
    assert before - window <= cutoff <= after - window
    assert hard_cutoff is not None
    assert before - window * HARD_STALLED_MULTIPLIER <= hard_cutoff
    assert hard_cutoff <= after - window * HARD_STALLED_MULTIPLIER


async def test_stalled_items_without_candidates_writes_nothing():
    db = _StallDB([])

    assert await stalled_reindex_items(db, datetime.now(UTC)) == []  # type: ignore[arg-type]
    assert len(db.statements) == 1


@pytest.mark.parametrize("path", ["/rag/reindex", "/rag/reindex/{run_id}", "/rag/index-status"])
def test_admin_rag_routes_registered(path: str):
    assert path in {r.path for r in rag_api.router.routes}  # type: ignore[attr-defined]


def test_in_flight_statuses_is_a_public_contract():
    """#56 — 제품 write-site 가드가 import하는 공개 상수. 소스 reaper도 같은 집합을 쓴다."""
    import inspect

    assert IN_FLIGHT_STATUSES == ("pending", "parsing", "chunking", "embedding")
    reaper = inspect.getsource(ingestion._reap_stalled)
    assert "IN_FLIGHT_STATUSES" in reaper and '"parsing"' not in reaper  # 사본을 두지 않는다


# ── #44: 스윕 질의 ─────────────────────────────────────────────────────────


async def test_sweep_pins_custom_plans_first():
    """#44 D4 — generic plan은 인덱스가 있어도 순차 스캔을 고를 수 있다.

    그래서 스윕의 첫 문장으로 계획 모드를 고정한다."""
    from biznotes_core.services.ingestion.reindex import FORCE_CUSTOM_PLAN

    db = _StallDB([])

    await stalled_reindex_items(db, datetime.now(UTC))  # type: ignore[arg-type]

    assert db.plan_modes == [FORCE_CUSTOM_PLAN]


async def test_reaper_pins_custom_plans_before_its_first_query(monkeypatch):
    from biznotes_core.services.ingestion.reindex import FORCE_CUSTOM_PLAN

    seen: list[str] = []

    class _Empty:
        def scalars(self) -> Any:
            return self

        def tuples(self) -> Any:
            return self

        def all(self) -> list[Any]:
            return []

    class _Session:
        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def execute(self, stmt: Any) -> Any:
            seen.append(str(stmt))
            return _Empty()

        async def commit(self) -> None:
            return None

    async def fake_stalled(db: Any, cutoff: datetime, hard_cutoff: datetime | None = None) -> list:
        return []

    monkeypatch.setattr(ingestion, "get_session_factory", lambda: _Session)
    monkeypatch.setattr(ingestion, "stalled_reindex_items", fake_stalled)

    await ingestion._reap_stalled()

    assert seen[0] == FORCE_CUSTOM_PLAN
    assert "FROM sources" in seen[1]


async def test_stalled_runs_aggregate_over_all_items_of_open_runs():
    """#44 D3 — queued 항목이 있는 run으로 좁히되, 마지막 갱신은 그 run의 모든 항목으로 본다.

    바깥 HAVING을 queued로 거르면 "오래된 queued + 방금 닫힌 항목" run을 정체로 오판한다.
    """
    db = _StallDB([])

    await stalled_reindex_items(db, datetime.now(UTC))  # type: ignore[arg-type]

    sql = _sql(db.statements[0])
    # stalled_runs: 안쪽 IN(queued run)이 끝난 바로 뒤가 GROUP BY다 — 바깥 WHERE에 status
    # 조건이 끼어들면(queued 항목만 집계) 이 패턴이 깨진다
    assert re.search(
        r"WHERE reindex_run_items\.run_id IN \(SELECT reindex_run_items\.run_id\s+"
        r"FROM reindex_run_items\s+WHERE reindex_run_items\.status = %\(status_\d+\)s "
        # 지워진 소스의 항목은 영영 닫히지 않으므로 열린 run 판정에서 뺀다(#179)
        r"AND reindex_run_items\.source_id IS NOT NULL\) "
        r"GROUP BY reindex_run_items\.run_id\s+HAVING ",
        sql,
    ), sql
    having = re.search(r"HAVING (max\([^)]*\)[^)]*)\)", sql)
    assert having and "status" not in having.group(1)
    assert "max(reindex_run_items.updated_at) <" in having.group(1)
    assert "count(" not in sql


async def test_deferred_check_ignores_items_of_deleted_sources():
    """#179 — 지워진 소스의 queued 항목은 영영 닫히지 않는다.

    미룸 판정에 넣으면 스윕마다 "회수 보류" 경고가 난다.
    """
    db = _StallDB([], flowing=True, deferred=False)

    await stalled_reindex_items(db, datetime.now(UTC))  # type: ignore[arg-type]

    deferred_sql = _sql(db.flow_checks[1])
    assert "reindex_run_items.status = %(status_1)s" in deferred_sql
    assert "reindex_run_items.source_id IS NOT NULL" in deferred_sql


async def test_active_only_filter_counts_only_open_items():
    """#179 — active_only는 소스가 남은 queued 항목이 있는 run만 고른다(엔드포인트가 낸 SQL)."""

    class _Capture:
        def __init__(self) -> None:
            self.statements: list[Any] = []

        async def execute(self, stmt: Any) -> Any:
            self.statements.append(stmt)

            class _R:
                def scalars(self) -> Any:
                    return self

                def all(self) -> list[Any]:
                    return []

            return _R()

    active, everything = _Capture(), _Capture()
    await rag_api.list_reindex_runs(limit=5, active_only=True, db=active)  # type: ignore[arg-type]
    await rag_api.list_reindex_runs(limit=5, db=everything)  # type: ignore[arg-type]

    sql = _sql(active.statements[0])
    assert "EXISTS (SELECT reindex_run_items.id" in sql
    assert "reindex_run_items.run_id = reindex_runs.id" in sql
    assert "reindex_run_items.source_id IS NOT NULL" in sql
    assert "EXISTS" not in _sql(everything.statements[0])
