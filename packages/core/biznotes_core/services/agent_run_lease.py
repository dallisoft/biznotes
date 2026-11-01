"""agent run lease·fencing과 stuck run 회수 (#153 B).

계획은 `docs/plans/direct-node-hitl-and-run-lease/plan.md`다. 워커가 OOM·SIGKILL로 죽으면 run은
영원히 `running`으로 남아 그 채팅 세션(활성 run 유일성)과 스케줄·웹훅(`has_active_run`)을 막는다.
TTL만으로 강제 실패시키면 살아 있는 장기 run을 죽이고, 늦게 끝난 원래 워커가 결과를 덮어쓴다.
그래서 다음 세 가지를 둔다.

- **lease**: running 동안 `lease_owner`·`lease_expires_at`을 두고 heartbeat로 연장한다.
  running을 벗어나는 모든 전이는 lease를 NULL로 만든다. lease가 NULL인 running(구 워커가
  실행 중)은 회수하지 않는다.
- **fencing**: claim·재개·회수마다 `attempt`가 1씩 오른다. 상태·결과 쓰기는
  `attempt = :mine` CAS다. 회수된 뒤 늦게 온 워커의 쓰기는 0행이고, 호출자는 같은 트랜잭션의
  다른 쓰기를 롤백한다.
- **회수**: 만료된 lease의 running과 정체된 awaiting_approval을 `failed`로 끝낸다(재개하지
  않는다).

규칙
- 이 모듈의 DB 함수는 **커밋하지 않는다**(heartbeat 틱은 자기 세션에서 커밋한다). 시각은
  DB `now()`다.
- 제품 이관 조건: run 상태·결과 필드는 `fenced_transition(values=...)`로만 쓴다 — run ORM
  객체에 속성을 대입하면 autoflush가 fencing 없는 UPDATE를 낸다. False로 롤백한 뒤에는 run
  속성에 접근하지 않는다. 메인 세션은 실행 중에 `agent_runs` 행을 잠그지 않는다(heartbeat
  갱신과 경합한다).
- running을 벗어나는 전이(승인 대기·완료)는 `lease_heartbeat` 블록을 **나온 뒤에** 한다.
  블록 안에서 전이하면 다음 틱이 0행을 보고 소유 상실로 오탐한다.
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import AsyncIterator, Callable, Collection, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import anyio
import structlog
from sqlalchemy import and_, exists, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.config import get_settings
from biznotes_core.models import AgentRun, AgentRunApproval
from biznotes_core.services.agent_runs import ExpiredApproval, expire_stale_approvals

logger = structlog.get_logger()

#: 회수된 run의 오류 문구
WORKER_LOST_ERROR = "실행 워커와 연결이 끊겨 실행을 중단했습니다(lease 만료)"
STALE_AWAITING_ERROR = "승인 상태를 잃은 실행이라 중단했습니다"

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True)
class Lease:
    """이 워커가 가진 run 소유권. `attempt`가 fencing 토큰이다."""

    run_id: uuid.UUID
    attempt: int
    owner: str


@dataclass(frozen=True)
class ResumeClaim:
    lease: Lease
    approval_id: uuid.UUID
    #: 소비한 승인의 결정(`approved`·`rejected`)
    approval_status: str
    #: 소비 전 checkpoint(소비하면서 행에서는 NULL로 지운다)
    checkpoint: dict[str, Any] | None


@dataclass(frozen=True)
class ReclaimResult:
    """`reclaim_expired_runs` 결과 — 제품이 커밋 뒤 이벤트를 발행할 때 쓴다."""

    #: lease가 만료돼 `failed`로 끝낸 run
    lost_runs: list[uuid.UUID]
    #: 기한이 지나 만료시킨 승인(`expire_stale_approvals`)
    expired_approvals: list[ExpiredApproval]
    #: 승인 상태를 잃고 정체돼 `failed`로 끝낸 awaiting_approval run
    stale_awaiting_runs: list[uuid.UUID]


def new_lease_owner(task_id: str | None = None) -> str:
    """`host:pid:task_id` — 진단용 식별자다(비밀정보를 넣지 않는다)."""
    return f"{socket.gethostname()}:{os.getpid()}:{task_id or '-'}"[:255]


def _lease_expiry() -> Any:
    return func.now() + timedelta(seconds=get_settings().agent_run_lease_ttl_sec)


def _cleared_lease() -> dict[str, Any]:
    return {"lease_owner": None, "lease_expires_at": None}


async def claim_run(db: AsyncSession, run_id: uuid.UUID, owner: str) -> Lease | None:
    """`queued` run을 `running`으로 가져온다(attempt+1, lease 설정). 못 가져오면 None.

    running을 다시 가져오지 않는다 — Celery 재전달(acks_late)이 살아 있는 run을 넘겨받지 않게 한다.
    승인 재개는 `claim_resume`이다.
    """
    attempt = await db.scalar(
        update(AgentRun)
        .where(AgentRun.id == run_id, AgentRun.status == "queued")
        .values(
            status="running",
            attempt=AgentRun.attempt + 1,
            lease_owner=owner,
            lease_expires_at=_lease_expiry(),
            started_at=func.coalesce(AgentRun.started_at, func.now()),
        )
        .returning(AgentRun.attempt)
        .execution_options(synchronize_session=False)
    )
    return None if attempt is None else Lease(run_id, int(attempt), owner)


async def claim_resume(db: AsyncSession, run_id: uuid.UUID, owner: str) -> ResumeClaim | None:
    """해결된 승인을 소비하고 run을 `awaiting_approval → running`으로 가져온다 — 한 트랜잭션.

    소비할 승인은 run_id로 CAS 안에서 고른다(`approved|rejected AND consumed=false`, 가장 최근 것).
    승인 소비(consumed=true, checkpoint NULL)와 run 전이(attempt+1, lease 설정)가 함께
    커밋되므로, 그 사이 워커가 죽어 "소비됐지만 승인 대기인" 정체가 생기지 않는다. 어느 하나라도
    실패하면 None이고 호출자는 롤백한다.
    """
    target = (
        await db.execute(
            select(AgentRunApproval.id, AgentRunApproval.status, AgentRunApproval.checkpoint)
            .where(
                AgentRunApproval.run_id == run_id,
                AgentRunApproval.status.in_(("approved", "rejected")),
                AgentRunApproval.consumed.is_(False),
            )
            .order_by(AgentRunApproval.requested_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).first()
    if target is None:
        return None
    approval_id, approval_status, checkpoint = target
    consumed = await db.scalar(
        update(AgentRunApproval)
        .where(AgentRunApproval.id == approval_id, AgentRunApproval.consumed.is_(False))
        .values(consumed=True, checkpoint=None)
        .returning(AgentRunApproval.id)
        .execution_options(synchronize_session=False)
    )
    if consumed is None:
        return None
    attempt = await db.scalar(
        update(AgentRun)
        .where(AgentRun.id == run_id, AgentRun.status == "awaiting_approval")
        .values(
            status="running",
            attempt=AgentRun.attempt + 1,
            lease_owner=owner,
            lease_expires_at=_lease_expiry(),
        )
        .returning(AgentRun.attempt)
        .execution_options(synchronize_session=False)
    )
    if attempt is None:
        return None
    return ResumeClaim(
        lease=Lease(run_id, int(attempt), owner),
        approval_id=approval_id,
        approval_status=str(approval_status),
        checkpoint=checkpoint,
    )


async def fenced_transition(
    db: AsyncSession,
    lease: Lease,
    *,
    from_statuses: Collection[str],
    to_status: str,
    values: Mapping[str, Any] | None = None,
) -> bool:
    """`attempt = :mine AND status IN (:from)` CAS로 run 상태·결과를 쓴다. 커밋하지 않는다.

    False이면 이 워커는 더 이상 소유자가 아니다(회수·취소·다른 시도). **호출자는 같은
    트랜잭션의 다른 쓰기(assistant ChatMessage·승인 행 등)를 롤백한다.** running을 벗어나는
    전이는 lease를 지운다.
    """
    row_values: dict[str, Any] = {**(values or {}), "status": to_status}
    if to_status != "running":
        row_values.update(_cleared_lease())
    updated = await db.scalar(
        update(AgentRun)
        .where(
            AgentRun.id == lease.run_id,
            AgentRun.attempt == lease.attempt,
            AgentRun.status.in_(tuple(from_statuses)),
        )
        .values(**row_values)
        .returning(AgentRun.id)
        .execution_options(synchronize_session=False)
    )
    return updated is not None


async def persist_pending_approval(
    db: AsyncSession, lease: Lease, approval: AgentRunApproval
) -> bool:
    """run을 `running → awaiting_approval`로 fencing 전이하고, 성공했을 때만 승인 행을 넣는다.

    회수·취소된 run에 승인 카드가 남지 않는다. 커밋하지 않는다 — False이면 호출자가 롤백한다.
    """
    if not await fenced_transition(
        db, lease, from_statuses=("running",), to_status="awaiting_approval"
    ):
        return False
    db.add(approval)
    await db.flush()
    return True


async def renew_lease(db: AsyncSession, lease: Lease) -> bool:
    """lease를 연장한다. 소유자가 아니면(회수·전이·다른 시도) False. 커밋하지 않는다."""
    updated = await db.scalar(
        update(AgentRun)
        .where(
            AgentRun.id == lease.run_id,
            AgentRun.attempt == lease.attempt,
            AgentRun.status == "running",
            AgentRun.lease_owner == lease.owner,
        )
        .values(lease_expires_at=_lease_expiry())
        .returning(AgentRun.id)
        .execution_options(synchronize_session=False)
    )
    return updated is not None


async def reclaim_expired_runs(db: AsyncSession) -> ReclaimResult:
    """stuck run을 `failed`로 끝낸다. 커밋하지 않는다 — 호출자가 커밋한 뒤 이벤트를 발행한다.

    1. lease가 만료된 running(lease NULL은 제외 — 구 워커) → `failed`, attempt+1, lease NULL.
    2. 기한이 지난 pending 승인 → `expire_stale_approvals`.
    3. pending 승인도 소비되지 않은 해결 승인도 없이 `updated_at`이
       `agent_run_awaiting_stale_sec`보다 오래된 awaiting_approval → `failed`.
    재개하지 않는다 — 부작용 멱등성이 없어 다시 실행하면 외부 호출이 반복된다.
    """
    lost = list(
        (
            await db.execute(
                update(AgentRun)
                .where(
                    AgentRun.status == "running",
                    AgentRun.lease_expires_at.is_not(None),
                    AgentRun.lease_expires_at < func.now(),
                )
                .values(
                    status="failed",
                    error=WORKER_LOST_ERROR,
                    finished_at=func.now(),
                    attempt=AgentRun.attempt + 1,
                    **_cleared_lease(),
                )
                .returning(AgentRun.id)
                .execution_options(synchronize_session=False)
            )
        ).scalars()
    )
    expired = await expire_stale_approvals(db)
    stale_after = timedelta(seconds=get_settings().agent_run_awaiting_stale_sec)
    open_approval = exists().where(
        AgentRunApproval.run_id == AgentRun.id,
        (AgentRunApproval.status == "pending")
        | and_(
            AgentRunApproval.status.in_(("approved", "rejected")),
            AgentRunApproval.consumed.is_(False),
        ),
    )
    stale = list(
        (
            await db.execute(
                update(AgentRun)
                .where(
                    AgentRun.status == "awaiting_approval",
                    AgentRun.updated_at < func.now() - stale_after,
                    ~open_approval,
                )
                .values(
                    status="failed",
                    error=STALE_AWAITING_ERROR,
                    finished_at=func.now(),
                    attempt=AgentRun.attempt + 1,
                )
                .returning(AgentRun.id)
                .execution_options(synchronize_session=False)
            )
        ).scalars()
    )
    return ReclaimResult(lost_runs=lost, expired_approvals=expired, stale_awaiting_runs=stale)


# ---------- heartbeat ----------


class LeaseLost(Exception):  # noqa: N818 — 공개 이름(계획)
    """이 워커는 더 이상 run의 소유자가 아니다 — 상태를 쓰지 말고 반환해야 한다."""

    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(f"agent run lease lost: {run_id}")
        self.run_id = run_id


class LeaseStartFailed(Exception):
    """진입 틱이 실패했다 — 본문을 실행하지 않았다(fail closed)."""

    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(f"agent run lease heartbeat could not start: {run_id}")
        self.run_id = run_id


@dataclass
class LeaseHandle:
    lease: Lease
    lost: bool = False
    #: 마지막으로 갱신에 성공한 시각(monotonic)
    last_ok: float = 0.0
    fails: int = 0
    _ttl: float = field(default=0.0, repr=False)
    _margin: float = field(default=0.0, repr=False)

    def valid(self) -> bool:
        """외부 호출 직전 확인용 — 잃었거나 lease가 만료됐을 수 있으면 False."""
        return not self.lost and anyio.current_time() - self.last_ok < self._ttl - self._margin


async def _tick(factory: SessionFactory, lease: Lease, tick_timeout_sec: float) -> bool:
    """틱 하나 — 새 세션에서 갱신·커밋. 행 잠금을 오래 기다리지 않는다."""
    with anyio.fail_after(tick_timeout_sec):
        async with factory() as session:
            await session.execute(
                text(f"SET LOCAL lock_timeout = '{int(tick_timeout_sec * 1000)}ms'")
            )
            ok = await renew_lease(session, lease)
            await session.commit()
    return ok


@asynccontextmanager
async def lease_heartbeat(
    lease: Lease, *, session_factory: SessionFactory | None = None
) -> AsyncIterator[LeaseHandle]:
    """본문을 lease heartbeat로 감싼다. 소유권을 잃으면 본문을 취소하고 `LeaseLost`.

    - 진입 틱이 실패하면 본문에 들어가지 않는다(`LeaseStartFailed`, 소유 상실이면 `LeaseLost`).
    - fixed-rate로 연장한다. 0행(회수·다른 시도)이면 즉시 상실이다. 결과 미상(예외·timeout)이
      `agent_run_heartbeat_max_failed_ticks`를 넘거나, 마지막 성공에서 ttl − interval이 지나면
      상실로 보고 본문을 취소한다 — lease가 만료돼 회수되기 전에 스스로 멈춘다.
    - running을 벗어나는 전이는 이 블록을 나온 뒤에 한다(모듈 docstring).
    """
    from biznotes_core.core.db import get_session_factory

    settings = get_settings()
    factory = session_factory or get_session_factory()
    interval = float(settings.agent_run_heartbeat_interval_sec)
    tick_timeout = float(settings.agent_run_heartbeat_tick_timeout_sec)
    max_fails = settings.agent_run_heartbeat_max_failed_ticks
    handle = LeaseHandle(
        lease, _ttl=float(settings.agent_run_lease_ttl_sec), _margin=interval
    )
    try:
        ok = await _tick(factory, lease, tick_timeout)
    except Exception as e:
        raise LeaseStartFailed(lease.run_id) from e
    if not ok:
        raise LeaseLost(lease.run_id)
    handle.last_ok = anyio.current_time()

    body_exc: BaseException | None = None
    async with anyio.create_task_group() as tg:
        body_scope = anyio.CancelScope()

        def lose(reason: str) -> None:
            handle.lost = True
            logger.warning(
                "agent_run.lease_lost",
                run_id=str(lease.run_id),
                attempt=lease.attempt,
                reason=reason,
                fails=handle.fails,
            )
            body_scope.cancel()

        async def loop() -> None:
            t0 = anyio.current_time()
            k = 0
            while True:
                k += 1
                await anyio.sleep_until(t0 + k * interval)
                try:
                    ok = await _tick(factory, lease, tick_timeout)
                except Exception as e:  # noqa: BLE001 — 결과 미상 틱은 다음 틱에서 다시 시도한다
                    handle.fails += 1
                    logger.warning(
                        "agent_run.lease_renew_failed",
                        run_id=str(lease.run_id),
                        error_type=type(e).__name__,
                        fails=handle.fails,
                    )
                    if handle.fails > max_fails or not handle.valid():
                        lose("renew_failed")
                        return
                    continue
                if not ok:
                    lose("not_owner")
                    return
                handle.fails = 0
                handle.last_ok = anyio.current_time()

        tg.start_soon(loop)
        try:
            with body_scope:
                yield handle
        except BaseException as e:
            body_exc = e
        finally:
            tg.cancel_scope.cancel()
    if handle.lost:
        raise LeaseLost(lease.run_id) from body_exc
    if body_exc is not None:
        raise body_exc
