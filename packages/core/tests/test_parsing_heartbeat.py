"""parsing heartbeat(§5-7) — 진입 틱·fixed-rate 주기 틱·복구 창·`fails` 카운터·소유권 상실.

DB 세션만 가짜다. 가짜 DB는 컴파일된 UPDATE의 WHERE 조건(status·updated_at 등치/범위)을
행 하나에 실제로 평가하고, 매칭되면 `updated_at`을 DB 시각으로 갱신해 RETURNING으로 돌려준다.
가상 시계 케이스는 `_now`·`_sleep_until`만 바꾼다.
"""

import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import anyio.lowlevel
import pytest
import structlog
from conftest import FakeResult, pg, pg_params

from biznotes_core.services.ingestion.events import (
    INGEST_PARSE_HEARTBEAT_FAILED,
    INGEST_PARSE_HEARTBEAT_LOST,
)
from biznotes_core.workers import parsing_heartbeat as hb
from biznotes_core.workers.parsing_heartbeat import (
    HeartbeatStartFailed,
    ParsingOwnershipLost,
    parsing_heartbeat,
)


class ParseAborted(Exception):
    """중단 신호를 보고 스스로 멈춘 파서가 내는 예외(대역) — 하트비트는 예외 종류와 무관하다."""

BASE = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
SID = uuid.UUID("00000000-0000-0000-0000-00000000abcd")


class VClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.oversleep = 0.0

    def now(self) -> float:
        return self.t

    async def sleep_until(self, deadline: float) -> None:
        self.t = max(self.t, deadline + self.oversleep)
        await anyio.lowlevel.checkpoint()


Action = Callable[["FakeDB", Any], Any]


class FakeDB:
    """행 하나짜리 sources. actions[i]가 i번째 execute(0 = 진입 틱 첫 시도)의 동작이다."""

    def __init__(self, clock: VClock | None = None, actions: dict[int, Any] | None = None):
        self.clock = clock
        self.actions = actions or {}
        self.status = "parsing"
        self.updated_at = BASE
        self.statements: list[Any] = []
        self.starts: list[float] = []
        self.opened = 0
        self.closed = 0
        self.commits = 0
        self.reached = anyio.Event()
        self.stop_after: int | None = None
        self.real_start = time.monotonic()

    # DB 시각 — 가상 시계가 있으면 그 경과를, 없으면 실제 경과를 쓴다
    def db_now(self) -> datetime:
        if self.clock is not None:
            return BASE + timedelta(seconds=self.clock.t - 1000.0)
        return BASE + timedelta(seconds=time.monotonic() - self.real_start)

    def apply(self, stmt: Any) -> list[tuple[Any, ...]]:
        p = pg_params(stmt)
        ok = p["id_1"] == SID and self.status == p["status_1"]
        if "updated_at_2" in p:
            ok = ok and p["updated_at_1"] <= self.updated_at <= p["updated_at_2"]
        elif "updated_at_1" in p:
            ok = ok and self.updated_at == p["updated_at_1"]
        if not ok:
            return []
        self.updated_at = self.db_now()
        return [(self.updated_at,)]

    def factory(self) -> "FakeSession":
        return FakeSession(self)

    def kinds(self) -> list[str]:
        out = []
        for s in self.statements:
            p = pg_params(s)
            kind = "strict" if "updated_at_1" in p else "entry"
            out.append("recovery" if "updated_at_2" in p else kind)
        return out


class FakeSession:
    def __init__(self, db: FakeDB):
        self.db = db

    async def __aenter__(self) -> "FakeSession":
        self.db.opened += 1
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.db.closed += 1

    async def execute(self, stmt: Any) -> FakeResult:
        db = self.db
        index = len(db.statements)
        db.statements.append(stmt)
        db.starts.append(db.clock.t if db.clock else time.monotonic())
        if db.stop_after is not None and len(db.statements) >= db.stop_after:
            db.reached.set()
        action = db.actions.get(index, "apply")
        if callable(action):
            action = action(db, stmt)
        if action == "apply":
            return FakeResult(db.apply(stmt))
        if action == "zero":
            return FakeResult([])
        kind, arg = action
        if kind == "advance":  # 틱이 arg초 걸려 끝남(가상)
            db.clock.t += arg
            return FakeResult(db.apply(stmt))
        if kind == "commit_then_timeout":  # 커밋은 됐는데 응답만 유실(결과 미상)
            db.clock.t += arg
            db.apply(stmt)
            raise TimeoutError()
        if kind == "raise":
            raise arg
        if kind == "sleep":
            await anyio.sleep(arg)
            return FakeResult(db.apply(stmt))
        raise AssertionError(action)

    async def commit(self) -> None:
        self.db.commits += 1


@pytest.fixture
def vclock(monkeypatch: pytest.MonkeyPatch) -> VClock:
    clock = VClock()
    monkeypatch.setattr(hb, "_now", clock.now)
    monkeypatch.setattr(hb, "_sleep_until", clock.sleep_until)
    return clock


def _events(logs: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == name]


async def _run_until(db: FakeDB, statements: int, *, interval: float, tick: float = 10) -> Any:
    """statements개의 execute가 일어날 때까지 본문을 유지한다."""
    db.stop_after = statements
    async with parsing_heartbeat(
        db.factory, SID, interval_sec=interval, tick_timeout_sec=tick
    ) as handle:
        await db.reached.wait()
    return handle


# ---------------------------------------------------------------------------
# 진입 틱
# ---------------------------------------------------------------------------


async def test_entry_tick_returns_last_seen(vclock) -> None:
    db = FakeDB(vclock)
    async with parsing_heartbeat(db.factory, SID, interval_sec=30, tick_timeout_sec=10) as h:
        assert h.last_seen == BASE + timedelta(0)
        assert h.fails == 0 and not h.lost and not h.abort.is_set()
    sql = pg(db.statements[0])
    assert "UPDATE sources SET updated_at=now()" in sql
    assert "sources.status = %(status_1)s" in sql and "sources.updated_at =" not in sql
    assert sql.endswith("RETURNING sources.updated_at")


async def test_entry_tick_gives_up_after_three_failures(vclock) -> None:
    db = FakeDB(vclock, {i: ("raise", ConnectionError("db down")) for i in range(3)})
    body_ran = False
    with structlog.testing.capture_logs() as logs, pytest.raises(HeartbeatStartFailed):
        async with parsing_heartbeat(db.factory, SID, interval_sec=30, tick_timeout_sec=10):
            body_ran = True
    assert body_ran is False
    assert len(db.statements) == 3  # 주기 틱 태스크는 시작하지 않았다
    failed = _events(logs, INGEST_PARSE_HEARTBEAT_FAILED)
    assert [(e["phase"], e["attempt"], e["gave_up"]) for e in failed] == [
        ("entry", 1, False),
        ("entry", 2, False),
        ("entry", 3, True),
    ]
    assert [e["log_level"] for e in failed] == ["warning", "warning", "error"]


async def test_entry_tick_succeeds_on_third_attempt(vclock) -> None:
    db = FakeDB(vclock, {0: ("raise", TimeoutError()), 1: ("raise", OSError("reset"))})
    async with parsing_heartbeat(db.factory, SID, interval_sec=30, tick_timeout_sec=10) as h:
        assert h.last_seen == BASE + timedelta(seconds=2)
    assert db.starts[:3] == [1000.0, 1001.0, 1002.0]  # 시도 간 1s 백오프 2회


async def test_entry_rowcount_zero_is_immediate_loss(vclock) -> None:
    db = FakeDB(vclock)
    db.status = "pending"  # reaper가 이미 되돌렸다
    body_ran = False
    with structlog.testing.capture_logs() as logs, pytest.raises(ParsingOwnershipLost):
        async with parsing_heartbeat(db.factory, SID, interval_sec=30, tick_timeout_sec=10):
            body_ran = True
    assert body_ran is False and len(db.statements) == 1
    [lost] = _events(logs, INGEST_PARSE_HEARTBEAT_LOST)
    assert lost["reason"] == "entry_rowcount_zero" and lost["rowcount"] == 0


async def test_cancel_during_entry_retry_propagates(monkeypatch) -> None:
    monkeypatch.setattr(hb, "_ENTRY_RETRY_DELAY_SEC", 0.01)
    db = FakeDB(None, {0: ("raise", ConnectionError()), 1: ("sleep", 5)})
    db.stop_after = 2
    body_ran = False
    with anyio.CancelScope() as scope:
        async with anyio.create_task_group() as tg:

            async def cancel_soon() -> None:
                await db.reached.wait()  # 두 번째 시도가 진행 중
                scope.cancel()

            tg.start_soon(cancel_soon)
            async with parsing_heartbeat(db.factory, SID, interval_sec=30, tick_timeout_sec=10):
                body_ran = True
    assert scope.cancelled_caught
    assert body_ran is False
    assert len(db.statements) == 2
    assert db.opened == db.closed == 2


# ---------------------------------------------------------------------------
# fixed-rate 스케줄
# ---------------------------------------------------------------------------


async def test_ticks_are_fixed_rate(vclock) -> None:
    interval = 10.0
    db = FakeDB(vclock, {i: ("advance", 0.7 * interval) for i in range(1, 6)})
    await _run_until(db, 6, interval=interval)
    t0 = db.starts[0]
    offsets = [round(s - t0, 6) for s in db.starts[1:]]
    assert offsets == [10.0, 20.0, 30.0, 40.0, 50.0]
    # completion-relative였다면 10, 27, 44 …
    assert offsets != [10.0 + k * 17.0 for k in range(5)]


async def test_overrun_tick_skips_missed_slots(vclock) -> None:
    interval = 10.0
    db = FakeDB(vclock, {1: ("advance", 2.5 * interval)})
    await _run_until(db, 3, interval=interval)
    t0 = db.starts[0]
    assert [s - t0 for s in db.starts[1:]] == [10.0, 40.0]


async def test_real_time_ticks_and_stop_after_exit() -> None:
    db = FakeDB()
    async with parsing_heartbeat(db.factory, SID, interval_sec=0.01, tick_timeout_sec=1) as h:
        await anyio.sleep(0.05)
    ticks = len(db.statements) - 1
    assert ticks >= 2
    await anyio.sleep(0.05)
    assert len(db.statements) == ticks + 1  # 종료 뒤 틱 0회
    assert h.lost is False


async def test_periodic_statement_guards_last_seen(vclock) -> None:
    db = FakeDB(vclock, {i: ("advance", 1.0) for i in range(1, 4)})
    await _run_until(db, 4, interval=5.0)
    sql = pg(db.statements[1])
    assert "sources.status = %(status_1)s AND sources.updated_at = %(updated_at_1)s" in sql
    assert sql.startswith("UPDATE sources SET updated_at=now()")
    assert sql.endswith("RETURNING sources.updated_at")
    # 틱마다 직전 RETURNING 값이 다음 틱의 last_seen이다
    seen = [pg_params(s)["updated_at_1"] for s in db.statements[1:]]
    assert seen == [BASE, BASE + timedelta(seconds=6), BASE + timedelta(seconds=11)]


async def test_each_tick_uses_fresh_session(vclock) -> None:
    db = FakeDB(vclock)
    await _run_until(db, 4, interval=5.0)
    assert db.opened == db.closed == db.commits == len(db.statements)


# ---------------------------------------------------------------------------
# 결과 미상 틱·복구 창·fails 카운터(PLAN6-02·PLAN7-05)
# ---------------------------------------------------------------------------


async def test_failed_tick_continues_with_recovery_condition(vclock) -> None:
    db = FakeDB(vclock, {1: ("raise", ConnectionError("blip"))})
    with structlog.testing.capture_logs() as logs:
        handle = await _run_until(db, 4, interval=30.0, tick=10)
    assert db.kinds() == ["entry", "strict", "recovery", "strict"]
    params = pg_params(db.statements[2])
    assert params["updated_at_2"] - params["updated_at_1"] == timedelta(seconds=4 * 30 + 10 + 5)
    [failed] = _events(logs, INGEST_PARSE_HEARTBEAT_FAILED)
    assert (failed["phase"], failed["error_type"], failed["fails"]) == (
        "periodic",
        "ConnectionError",
        1,
    )
    assert handle.lost is False and handle.fails == 0


async def test_f_unknown_ticks_are_recovered_by_next_tick(vclock) -> None:
    """F=4 연속 결과 미상 → 5번째(복구) 틱이 쓰기 전 값 L+490 ≤ L+495를 만나 성공한다."""
    interval, timeout = 120.0, 10.0
    actions: dict[int, Any] = {i: ("commit_then_timeout", timeout) for i in range(1, 5)}
    actions[5] = ("advance", 1.0)
    db = FakeDB(vclock, actions)
    written_before: list[datetime] = []

    def record(db: FakeDB, stmt: Any) -> Any:
        written_before.append(db.updated_at)
        return ("advance", 1.0)

    actions[5] = record
    handle = await _run_until(db, 7, interval=interval, tick=timeout)

    assert written_before == [BASE + timedelta(seconds=490)]
    params = pg_params(db.statements[5])
    assert params["updated_at_2"] == BASE + timedelta(seconds=495)
    assert db.kinds() == ["entry", "strict", "recovery", "recovery", "recovery", "recovery",
                          "strict"]
    assert handle.lost is False
    assert pg_params(db.statements[6])["updated_at_1"] == BASE + timedelta(seconds=601)


async def test_f_plus_one_unknown_ticks_fail_closed(vclock) -> None:
    interval, timeout = 120.0, 10.0
    db = FakeDB(vclock, {i: ("commit_then_timeout", timeout) for i in range(1, 6)})
    with structlog.testing.capture_logs() as logs, pytest.raises(ParsingOwnershipLost):
        async with parsing_heartbeat(
            db.factory, SID, interval_sec=interval, tick_timeout_sec=timeout
        ) as handle:
            await anyio.sleep_forever()
    assert len(db.statements) == 6  # 6번째 틱은 발행되지 않는다
    assert handle.abort.is_set()
    [lost] = _events(logs, INGEST_PARSE_HEARTBEAT_LOST)
    assert (lost["reason"], lost["rowcount"], lost["fails"], lost["after_failed_tick"]) == (
        "max_consecutive_unknown",
        None,
        5,
        True,
    )
    assert len(_events(logs, INGEST_PARSE_HEARTBEAT_FAILED)) == 4


async def test_fails_resets_on_confirmed_result(vclock) -> None:
    db = FakeDB(
        vclock,
        {1: ("raise", TimeoutError()), 2: ("raise", TimeoutError()), 4: ("raise", TimeoutError())},
    )
    handle = await _run_until(db, 6, interval=30.0)
    assert db.kinds() == ["entry", "strict", "recovery", "recovery", "strict", "recovery"]
    assert handle.fails in (0, 1)


async def test_rowcount_zero_after_failed_tick_is_marked(vclock) -> None:
    db = FakeDB(vclock, {1: ("raise", TimeoutError()), 2: "zero"})
    with structlog.testing.capture_logs() as logs, pytest.raises(ParsingOwnershipLost):
        async with parsing_heartbeat(db.factory, SID, interval_sec=30, tick_timeout_sec=10):
            await anyio.sleep_forever()
    [lost] = _events(logs, INGEST_PARSE_HEARTBEAT_LOST)
    assert (lost["reason"], lost["rowcount"], lost["after_failed_tick"]) == (
        "rowcount_zero",
        0,
        True,
    )
    assert len(db.statements) == 3


async def test_sched_lag_is_reported(vclock) -> None:
    vclock.oversleep = hb._RECOVERY_WINDOW_SLACK_SEC + 1
    db = FakeDB(vclock, {1: ("raise", ConnectionError()), 2: "zero"})
    with structlog.testing.capture_logs() as logs, pytest.raises(ParsingOwnershipLost):
        async with parsing_heartbeat(db.factory, SID, interval_sec=30, tick_timeout_sec=10):
            await anyio.sleep_forever()
    [failed] = _events(logs, INGEST_PARSE_HEARTBEAT_FAILED)
    [lost] = _events(logs, INGEST_PARSE_HEARTBEAT_LOST)
    assert failed["sched_lag_ms"] == 6000
    assert lost["sched_lag_ms"] >= 6000


async def test_slow_tick_times_out_and_continues() -> None:
    db = FakeDB(None, {1: ("sleep", 1.0)})
    with structlog.testing.capture_logs() as logs:
        await _run_until(db, 3, interval=0.02, tick=0.05)
    [failed] = _events(logs, INGEST_PARSE_HEARTBEAT_FAILED)
    assert failed["error_type"] == "TimeoutError"
    assert db.kinds()[:3] == ["entry", "strict", "recovery"]


# ---------------------------------------------------------------------------
# 소유권 상실(PLAN4-02b·PLAN5-01·PLAN5-02)
# ---------------------------------------------------------------------------


async def test_rowcount_zero_cancels_body() -> None:
    db = FakeDB(None, {1: "zero"})
    started = time.monotonic()
    with structlog.testing.capture_logs() as logs, pytest.raises(ParsingOwnershipLost) as exc:
        async with parsing_heartbeat(db.factory, SID, interval_sec=0.02, tick_timeout_sec=1) as h:
            await anyio.sleep(1)
    assert time.monotonic() - started < 0.5
    assert h.lost and h.abort.is_set()
    assert exc.value.source_id == SID
    count = len(db.statements)
    await anyio.sleep(0.1)
    assert len(db.statements) == count == 2  # 이후 틱 0회
    [lost] = _events(logs, INGEST_PARSE_HEARTBEAT_LOST)
    assert (lost["rowcount"], lost["after_failed_tick"], lost["reason"]) == (
        0,
        False,
        "rowcount_zero",
    )


async def test_reverted_status_is_detected_by_last_seen_guard() -> None:
    """pending으로 바꿨다가 곧바로 parsing으로 되돌린 행 — status는 같지만 updated_at이 다르다."""

    def revert(db: FakeDB, stmt: Any) -> str:
        db.status = "pending"
        db.updated_at = db.db_now()  # 되돌린 UPDATE가 쓴 값
        db.status = "parsing"
        return "apply"

    db = FakeDB(None, {2: revert})
    with pytest.raises(ParsingOwnershipLost):
        async with parsing_heartbeat(db.factory, SID, interval_sec=0.02, tick_timeout_sec=1):
            await anyio.sleep(2)
    assert db.kinds() == ["entry", "strict", "strict"]


async def test_body_exception_becomes_ownership_lost_with_cause() -> None:
    """스레드가 abort를 보고 ParseAborted로 끝나도 결과는 ParsingOwnershipLost다."""
    db = FakeDB(None, {1: "zero"})

    def parser(abort: threading.Event) -> None:
        assert abort.wait(5)
        raise ParseAborted()

    with pytest.raises(ParsingOwnershipLost) as exc:
        async with parsing_heartbeat(db.factory, SID, interval_sec=0.02, tick_timeout_sec=1) as h:
            await anyio.to_thread.run_sync(parser, h.abort)
    assert isinstance(exc.value.__cause__, ParseAborted)


async def test_body_exception_without_loss_propagates_as_is() -> None:
    db = FakeDB()
    with pytest.raises(ValueError, match="parser bug"):
        async with parsing_heartbeat(db.factory, SID, interval_sec=0.01, tick_timeout_sec=1):
            await anyio.sleep(0.03)
            raise ValueError("parser bug")
    count = len(db.statements)
    await anyio.sleep(0.05)
    assert len(db.statements) == count


def test_recovery_window_formula() -> None:
    assert hb.recovery_window_sec(120, 10) == 495
    assert hb.recovery_window_sec(30, 10) == 135
    assert hb._MAX_CONSECUTIVE_UNKNOWN_TICKS == 4
