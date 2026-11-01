"""parsing 구간 heartbeat — 정상 실행 중인 소스가 reaper에 재발행되지 않게 한다.

`_run`은 `_set_status("parsing")`부터 `_set_status("chunking")`까지 상태를 갱신하지 않는데,
reaper는 `updated_at`이 15분 넘게 멈춘 in-flight 소스를 재발행한다. 이 컨텍스트 매니저가
그 구간 동안 `updated_at`을 주기적으로 갱신하고, 갱신이 실패하면(다른 소유자가 행을 썼다)
본문을 취소한다. 설계와 상한 유도는 `docs/HWP_INGEST_PLAN.md` §5-7.

- 진입 틱: `status='parsing'`만 보고 갱신해 `last_seen`을 얻는다. 결과를 알 수 없는 실패는
  최대 `_ENTRY_TICK_ATTEMPTS`회 재시도하고, 전부 실패하면 본문에 들어가지 않는다(fail-closed).
- 주기 틱: fixed-rate(`t0 + k·interval`)로 `updated_at = :last_seen`까지 확인한다. 이 워커가
  마지막으로 쓴 뒤 누군가 행을 썼다면 0행이 매칭된다.
- 결과 미상(예외·timeout) 틱 뒤에는 자기 커밋을 회수하도록 조건을 복구 창으로 넓힌다.
  연속 결과 미상이 `_MAX_CONSECUTIVE_UNKNOWN_TICKS`를 넘으면 즉시 소유권 상실로 본다.
"""

import math
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import anyio
import structlog
from sqlalchemy import Update, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.models import Source
from biznotes_core.services.ingestion.events import (
    INGEST_PARSE_HEARTBEAT_FAILED,
    INGEST_PARSE_HEARTBEAT_LOST,
)

logger = structlog.get_logger()

_ENTRY_TICK_ATTEMPTS = 3
_ENTRY_RETRY_DELAY_SEC = 1.0
_MAX_CONSECUTIVE_UNKNOWN_TICKS = 4  # F — 복구 창 식과 카운터가 같은 값을 쓴다
_RECOVERY_WINDOW_SLACK_SEC = 5  # J — fixed-rate 슬롯 지터와 DB 왕복 여유

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class ParsingOwnershipLost(Exception):  # noqa: N818 — 계획 문서의 공개 이름
    """이 워커는 더 이상 소스의 소유자가 아니다 — 상태를 쓰지 말고 반환해야 한다."""

    def __init__(self, source_id: uuid.UUID):
        super().__init__(f"parsing ownership lost: {source_id}")
        self.source_id = source_id


class HeartbeatStartFailed(Exception):
    """진입 틱이 재시도 끝에 실패했다 — 본문을 실행하지 않았다."""

    def __init__(self, source_id: uuid.UUID):
        super().__init__(f"parsing heartbeat could not start: {source_id}")
        self.source_id = source_id


@dataclass
class HeartbeatHandle:
    source_id: uuid.UUID
    lost: bool = False
    abort: threading.Event = field(default_factory=threading.Event)
    last_seen: datetime | None = None
    fails: int = 0  # 연속 결과 미상 틱 수 — 결과가 확정된 틱에서 0으로 돌아간다


def recovery_window_sec(interval_sec: float, tick_timeout_sec: float) -> float:
    """W = F·interval + T + J (§5-7)."""
    f = _MAX_CONSECUTIVE_UNKNOWN_TICKS
    return f * interval_sec + tick_timeout_sec + _RECOVERY_WINDOW_SLACK_SEC


def _now() -> float:
    return anyio.current_time()


async def _sleep_until(deadline: float) -> None:
    await anyio.sleep_until(deadline)


async def _retry_backoff() -> None:
    await _sleep_until(_now() + _ENTRY_RETRY_DELAY_SEC)


def _entry_stmt(source_id: uuid.UUID) -> Update:
    return (
        update(Source)
        .where(Source.id == source_id, Source.status == "parsing")
        .values(updated_at=func.now())
        .returning(Source.updated_at)
        .execution_options(synchronize_session=False)
    )


def _tick_stmt(source_id: uuid.UUID, last_seen: datetime, window: timedelta | None) -> Update:
    if window is None:
        seen = Source.updated_at == last_seen
    else:
        seen = (Source.updated_at >= last_seen) & (Source.updated_at <= last_seen + window)
    return (
        update(Source)
        .where(Source.id == source_id, Source.status == "parsing", seen)
        .values(updated_at=func.now())
        .returning(Source.updated_at)
        .execution_options(synchronize_session=False)
    )


async def _execute(factory: SessionFactory, stmt: Update) -> tuple[int, datetime | None]:
    """틱 하나 — 새 세션에서 실행·커밋·종료. (매칭 행 수, RETURNING 값)."""
    async with factory() as session:
        result = await session.execute(stmt)
        rows = result.fetchall()
        await session.commit()
    return len(rows), (rows[0][0] if rows else None)


@asynccontextmanager
async def parsing_heartbeat(
    session_factory: SessionFactory,
    source_id: uuid.UUID,
    *,
    interval_sec: float,
    tick_timeout_sec: float,
) -> AsyncIterator[HeartbeatHandle]:
    """본문을 heartbeat로 감싼다. 소유권을 잃으면 본문을 취소하고 `ParsingOwnershipLost`.

    본문의 진행 중 `anyio.to_thread.run_sync`는 스레드가 끝날 때까지 기다린 뒤 취소된다
    (`abandon_on_cancel`을 쓰지 않는다). HWP 파서는 `handle.abort`를 보고 스스로 멈춘다.
    """
    handle = HeartbeatHandle(source_id)
    t0 = await _entry_tick(session_factory, handle, tick_timeout_sec)

    # 본문 예외는 태스크 그룹 밖에서 다시 던진다 — 그룹 __aexit__은 호스트 예외를
    # ExceptionGroup으로 감싸므로 호출자의 `except ParseError`가 깨진다.
    body_exc: BaseException | None = None
    async with anyio.create_task_group() as tg:
        body_scope = anyio.CancelScope()
        tg.start_soon(
            _tick_loop, session_factory, handle, body_scope, t0, interval_sec, tick_timeout_sec
        )
        try:
            with body_scope:
                yield handle
        except BaseException as e:
            body_exc = e
        finally:
            tg.cancel_scope.cancel()
    if handle.lost:
        # 들어온 예외의 종류(취소·ParseAborted·파서 오류)와 무관하게 상실로 바꾼다
        raise ParsingOwnershipLost(source_id) from body_exc
    if body_exc is not None:
        raise body_exc


async def _entry_tick(
    factory: SessionFactory, handle: HeartbeatHandle, tick_timeout_sec: float
) -> float:
    """진입 틱은 `updated_at` 가드가 없어 재실행해도 소유권 판정이 깨지지 않는다."""
    for attempt in range(1, _ENTRY_TICK_ATTEMPTS + 1):
        try:
            with anyio.fail_after(tick_timeout_sec):
                matched, returned = await _execute(factory, _entry_stmt(handle.source_id))
        except Exception as e:
            gave_up = attempt == _ENTRY_TICK_ATTEMPTS
            (logger.error if gave_up else logger.warning)(
                INGEST_PARSE_HEARTBEAT_FAILED,
                source_id=str(handle.source_id),
                error_type=type(e).__name__,
                phase="entry",
                attempt=attempt,
                gave_up=gave_up,
                fails=0,
                sched_lag_ms=None,
            )
            if gave_up:
                raise HeartbeatStartFailed(handle.source_id) from e
            await _retry_backoff()
            continue
        if matched != 1:
            _mark_lost(handle, reason="entry_rowcount_zero", rowcount=matched, after_failed=False)
            raise ParsingOwnershipLost(handle.source_id)
        handle.last_seen = returned
        return _now()
    raise AssertionError("unreachable")


def _mark_lost(
    handle: HeartbeatHandle,
    *,
    reason: str,
    rowcount: int | None,
    after_failed: bool,
    sched_lag_ms: int | None = None,
) -> None:
    handle.lost = True
    handle.abort.set()
    logger.warning(
        INGEST_PARSE_HEARTBEAT_LOST,
        source_id=str(handle.source_id),
        rowcount=rowcount,
        after_failed_tick=after_failed,
        fails=handle.fails,
        reason=reason,
        sched_lag_ms=sched_lag_ms,
    )


async def _tick_loop(
    factory: SessionFactory,
    handle: HeartbeatHandle,
    body_scope: anyio.CancelScope,
    t0: float,
    interval_sec: float,
    tick_timeout_sec: float,
) -> None:
    window = timedelta(seconds=recovery_window_sec(interval_sec, tick_timeout_sec))
    k = 1
    while True:
        slot = t0 + k * interval_sec
        await _sleep_until(slot)
        lag_ms = int(max(0.0, _now() - slot) * 1000)
        assert handle.last_seen is not None
        recovering = handle.fails > 0
        stmt = _tick_stmt(handle.source_id, handle.last_seen, window if recovering else None)
        outcome: Any
        try:
            with anyio.fail_after(tick_timeout_sec):
                outcome = await _execute(factory, stmt)
        except Exception as e:
            handle.fails += 1
            if handle.fails > _MAX_CONSECUTIVE_UNKNOWN_TICKS:
                # 더 넓힌 창으로 재시도하지 않는다 — 창 상한 유도가 F개까지만 덮는다
                _mark_lost(
                    handle,
                    reason="max_consecutive_unknown",
                    rowcount=None,
                    after_failed=True,
                    sched_lag_ms=lag_ms,
                )
                body_scope.cancel()
                return
            logger.warning(
                INGEST_PARSE_HEARTBEAT_FAILED,
                source_id=str(handle.source_id),
                error_type=type(e).__name__,
                phase="periodic",
                attempt=handle.fails,
                gave_up=False,
                fails=handle.fails,
                sched_lag_ms=lag_ms,
            )
        else:
            matched, returned = outcome
            if matched != 1:
                _mark_lost(
                    handle,
                    reason="rowcount_zero",
                    rowcount=matched,
                    after_failed=recovering,
                    sched_lag_ms=lag_ms,
                )
                body_scope.cancel()
                return
            handle.last_seen = returned
            handle.fails = 0
        # 슬롯을 넘긴 틱 뒤에는 지난 슬롯을 몰아 보내지 않는다
        k = max(k + 1, math.ceil((_now() - t0) / interval_sec))
