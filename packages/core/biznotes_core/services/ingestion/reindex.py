"""어드민 재인덱싱 — 발행·진행률·완료 기록의 단일 관문 (#5).

재인덱싱은 소스 `status`를 건드리지 않는다. `pending`으로 바꾸면 대기열에 있는
동안 소스가 `ready` 필터(개요·전문 로딩·소스 선택 UI)에서 빠지기 때문이다.
대신 요청 1건을 `ReindexRun`, 대상 소스를 `ReindexRunItem`으로 기록하고, 워커가
그 소스 처리를 마칠 때 항목을 닫는다. 진행률은 항목 상태를 세서 얻는다.

- 중복 발행 방지: 이미 queued 항목이 있거나 처리 중(non-terminal)인 소스는 건너뛴다.
- 유실 회수: `STALLED_THRESHOLD` 동안 대기열이 흐르지 않았으면(워커 처리 흔적 없음)
  reaper가 멈춘 run의 남은 항목을 재발행한다(`stalled_reindex_items`). 항목당
  `MAX_REINDEX_ATTEMPTS`번까지 재발행하고, 그 뒤에는 실패로 닫는다.
- 회수 보류의 가시성·상한(#43): 대기열이 흐르는 중이라 회수를 미뤘고 그런데도 cutoff보다
  오래 멈춘 queued 항목이 있으면 경고를 남긴다. `hard_cutoff`(cutoff × `HARD_STALLED_MULTIPLIER`)
  보다 오래 멈춘 run은 흐름과 무관하게 재발행한다 — 끊이지 않는 처리나 heartbeat만 살아 있는
  parsing이 회수를 무한히 미루지 못하게 한다.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.config import get_settings
from biznotes_core.core.uow import UnitOfWork
from biznotes_core.models import Notebook, ReindexRun, ReindexRunItem, Source
from biznotes_core.services.ingestion.enqueue import SourceRoute, stage_ingest_job

logger = structlog.get_logger()

# 워커가 처리 중인 상태 — 여기 있는 소스는 새로 발행하지 않는다. **공개 상수**(#56): 소스
# reaper와 제품의 write-site 가드(`parsing` 중인 행을 쓰지 않는 조건)가 같은 집합을 쓰도록
# import한다. 이름·값을 바꾸는 것은 비호환 변경이다(AGENTS.md §5.1).
IN_FLIGHT_STATUSES = ("pending", "parsing", "chunking", "embedding")

# 워커가 실제로 처리하고 있는 상태 — 대기열이 흐르는지 판정할 때 쓴다(`pending`은 대기)
_WORKING_STATUSES = ("parsing", "chunking", "embedding")

# reaper가 한 재인덱싱 항목을 다시 발행하는 최대 횟수 — 넘으면 실패로 닫는다(#25)
MAX_REINDEX_ATTEMPTS = 3

# 스윕 질의의 계획 모드(#44 D4) — generic plan은 `updated_at < $n`·`status IN ($…)`를 기본
# 선택도로 추정해 인덱스가 있어도 순차 스캔을 고를 수 있다. custom plan은 실제 값과 통계로
# 추정한다. `SET LOCAL`이라 트랜잭션이 끝나면 풀리고, 캐시된 prepared statement에도 실행
# 시점에 적용된다. 스윕 함수의 첫 문장으로 낸다.
FORCE_CUSTOM_PLAN = "SET LOCAL plan_cache_mode = force_custom_plan"

# 대기열 흐름과 무관하게 회수하는 절대 상한의 cutoff 배수(#43). 대기열이 흐르는 동안에는
# 회수를 미루는데(#25), 그 미룸에 상한이 없으면 처리가 끊이지 않는 인스턴스나 heartbeat만
# 살아 있는 parsing이 유실 회수를 영구히 막는다. 재발행 중복은 `MAX_REINDEX_ATTEMPTS`가
# 제한하고, 처리 중(in-flight)인 소스의 항목은 어느 경우에도 건드리지 않는다.
HARD_STALLED_MULTIPLIER = 4


@dataclass
class ReindexStart:
    run: ReindexRun | None  # 발행할 소스가 없으면 None
    enqueued: int
    skipped: int  # 이미 대기·처리 중이라 건너뛴 소스 수


@dataclass
class ReindexProgress:
    queued: int = 0  # 요청 후 아직 끝나지 않은 소스(처리 중 포함) — 지워진 소스는 빼고 센다
    done: int = 0
    failed: int = 0
    last_update: datetime | None = None
    removed: int = 0  # 처리 전에 소스가 지워진 항목(#179)
    retrying: int = 0  # 대기 중이면서 reaper가 한 번 이상 다시 발행한 항목(#179)


# 아직 처리할 항목 — queued이고 소스가 남아 있다(#179). 지워진 소스의 항목은 영영 닫히지 않으므로
# 대기·재발행·활성 판정에서 뺀다
OPEN_REINDEX_ITEM = (ReindexRunItem.status == "queued") & ReindexRunItem.source_id.is_not(None)


def _reindex_lock_key() -> int:
    namespace = get_settings().service_name
    digest = hashlib.sha256(f"{namespace}:reindex".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def start_reindex(
    db: AsyncSession,
    uow: UnitOfWork,
    *,
    requested_by: uuid.UUID | None,
    notebook_id: uuid.UUID | None,
    failed_only: bool,
    refetch: bool = False,
    stale_only: bool = False,
) -> ReindexStart:
    """대상 소스를 골라 run·항목을 만들고 인제스트 발행을 예약한다 (호출자 UoW 참여).

    발행은 `stage_ingest_job`이 커밋 후로 미룬다. 동시에 두 요청이 같은 소스를
    중복으로 담지 않도록 트랜잭션 advisory lock으로 직렬화한다.

    기본은 저장된 파싱 결과로 재청킹·재임베딩만 한다(#6). `refetch=True`면 원본을
    다시 가져와 파싱부터 한다 — 원본 문서가 갱신됐거나 파서가 개선됐을 때 쓴다.
    `stale_only=True`면 현재 임베딩 공간·청킹 설정과 다르게 색인된 ready 소스만
    고른다(#7) — 설정이 그대로인 소스는 다시 처리하지 않는다.
    """
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _reindex_lock_key()})

    # 대상이 많을 수 있어 ORM 객체를 적재하지 않는다. 큐 라우팅에 드는 컬럼은 함께 읽는다(§5.3).
    q = select(
        Source.id, Source.status, Source.type, Source.mime_type, Source.title, Source.external_ref
    )
    if notebook_id:
        q = q.where(Source.notebook_id == notebook_id)
    if failed_only:
        q = q.where(Source.status == "failed")
    if stale_only:
        from biznotes_core.services.embedding_spaces import (
            active_space,
            chunk_config_of,
            stale_condition,
        )
        from biznotes_core.services.settings_store import get_setting

        active = await active_space(db)
        chunk_config = chunk_config_of(await get_setting(db, "rag"))
        q = q.where(Source.status == "ready", stale_condition(active.key, chunk_config))
    targets = list((await db.execute(q)).tuples().all())

    busy = set(
        (
            await db.execute(
                select(ReindexRunItem.source_id).where(OPEN_REINDEX_ITEM)
            )
        )
        .scalars()
        .all()
    )
    to_enqueue = [
        SourceRoute(sid, source_type, mime_type, title, external_ref)
        for sid, status, source_type, mime_type, title, external_ref in targets
        if status not in IN_FLIGHT_STATUSES and sid not in busy
    ]
    skipped = len(targets) - len(to_enqueue)
    if not to_enqueue:
        return ReindexStart(run=None, enqueued=0, skipped=skipped)

    run = ReindexRun(
        requested_by=requested_by,
        notebook_id=notebook_id,
        options={"failed_only": failed_only, "refetch": refetch, "stale_only": stale_only},
        requested=len(to_enqueue),
        skipped=skipped,
        scope="notebook" if notebook_id else "all",
        notebook_title=(
            await db.scalar(select(Notebook.title).where(Notebook.id == notebook_id))
            if notebook_id
            else None
        ),
    )
    db.add(run)
    await db.flush()
    for route in to_enqueue:
        db.add(ReindexRunItem(run_id=run.id, source_id=route.id, status="queued"))
        # 발행 메시지에는 모드를 싣지 않는다 — 워커가 이 항목을 보고 판단한다(아래 참조)
        stage_ingest_job(db, route, uow)
    return ReindexStart(run=run, enqueued=len(to_enqueue), skipped=skipped)


async def reindex_mode(db: AsyncSession, source_id: uuid.UUID) -> tuple[bool, bool]:
    """이 소스에 열린 재인덱싱 요청이 있는가, 있다면 원본을 다시 가져와야 하는가.

    모드를 **브로커 메시지에 싣지 않고 DB에서 읽는** 이유: 메시지가 예전과 완전히
    같은 모양이라, 배포 중 구 워커가 새 메시지를 받아도 깨지지 않고 예전처럼
    전체 인제스트를 한다(원본 재수집이라 느릴 뿐 결과는 정상). 태스크 계약을
    바꾸지 않으므로 워커·API 배포 순서 제약도 없다.

    요청이 여러 건이면 하나라도 `refetch`면 원본부터 처리한다 — 요청자 중 누군가는
    갱신된 원본을 원한 것이다.
    """
    rows = (
        await db.execute(
            select(ReindexRun.options)
            .join(ReindexRunItem, ReindexRunItem.run_id == ReindexRun.id)
            .where(ReindexRunItem.source_id == source_id, ReindexRunItem.status == "queued")
        )
    ).scalars().all()
    if not rows:
        return False, False
    return True, any(bool(opts.get("refetch")) for opts in rows)


async def close_reindex_items(
    db: AsyncSession, source_id: uuid.UUID, *, ok: bool, error: str | None = None
) -> None:
    """소스 처리가 끝났다 — 그 소스의 queued 항목을 모두 닫고 커밋한다.

    어떤 경로로 처리됐든(재인덱싱 태스크·사용자 재시도·reaper 재발행) 요청 이후
    소스가 다시 처리됐으면 요청은 충족된 것이다.
    """
    await db.execute(
        update(ReindexRunItem)
        .where(ReindexRunItem.source_id == source_id, ReindexRunItem.status == "queued")
        .values(status="done" if ok else "failed", error=None if ok else error)
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def touch_reindex_items(db: AsyncSession, source_id: uuid.UUID) -> None:
    """소스의 queued 항목을 열어 둔 채 `updated_at`만 갱신한다 — 커밋은 호출자가 한다.

    일시 오류로 재시도를 미룬 동안(#194) run이 정체로 보여 `stalled_reindex_items`가 같은
    항목을 재발행하지 않게 한다.
    """
    await db.execute(
        update(ReindexRunItem)
        .where(ReindexRunItem.source_id == source_id, ReindexRunItem.status == "queued")
        .values(updated_at=func.now())
        .execution_options(synchronize_session=False)
    )


async def reindex_progress(
    db: AsyncSession, run_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ReindexProgress]:
    if not run_ids:
        return {}
    rows = await db.execute(
        select(
            ReindexRunItem.run_id,
            func.count().filter(OPEN_REINDEX_ITEM),
            func.count().filter(ReindexRunItem.status == "done"),
            func.count().filter(ReindexRunItem.status == "failed"),
            func.max(ReindexRunItem.updated_at),
            func.count().filter(
                ReindexRunItem.status == "queued", ReindexRunItem.source_id.is_(None)
            ),
            func.count().filter(OPEN_REINDEX_ITEM, ReindexRunItem.attempts > 0),
        )
        .where(ReindexRunItem.run_id.in_(run_ids))
        .group_by(ReindexRunItem.run_id)
    )
    return {
        run_id: ReindexProgress(
            queued=q, done=d, failed=f, last_update=last, removed=removed, retrying=retrying
        )
        for run_id, q, d, f, last, removed, retrying in rows.tuples().all()
    }


async def pending_by_notebook(db: AsyncSession) -> dict[uuid.UUID, int]:
    """노트북별 재인덱싱 대기 소스 수(처리 중 포함) — 인덱스 현황용."""
    rows = await db.execute(
        select(Source.notebook_id, func.count(func.distinct(ReindexRunItem.source_id)))
        .join(Source, Source.id == ReindexRunItem.source_id)
        .where(ReindexRunItem.status == "queued")
        .group_by(Source.notebook_id)
    )
    return dict(rows.tuples().all())


async def stalled_reindex_items(
    db: AsyncSession, cutoff: datetime, hard_cutoff: datetime | None = None
) -> list[tuple[ReindexRunItem, ReindexRun]]:
    """대기열이 멈췄을 때 재발행할 queued 항목 — 멈춘 run의 항목 중 처리 중이 아닌 것.

    정체는 대기열 전체로 판정한다(#25). cutoff 이후 워커가 어떤 소스든 처리했거나
    (`parsing`·`chunking`·`embedding` 전이와 parsing heartbeat가 `updated_at`을 갱신한다)
    재인덱싱 항목을 닫았다면 대기열은 흐르는 중이고, queued 항목은 메시지를 잃은 게
    아니라 차례를 기다리는 것이다 — 아무것도 재발행하지 않는다. run 단위로만 보면 한
    소스의 처리가 cutoff보다 오래 걸리거나 다른 run 뒤에 줄 서 있을 때 멀쩡한 항목을
    중복 발행한다. `pending`은 대기 중이거나 소스 reaper가 방금 되돌린 상태라 진척으로
    치지 않는다.

    대기열이 멈췄으면 cutoff 이후 갱신이 없는 run의 queued 항목을 고른다. 소스가
    in-flight인 항목은 빼는데, 그 소스는 소스 단위 reaper가 회수한다. 고른 항목은
    `attempts`를 올리고 `updated_at`을 갱신해 다음 스윕이 곧바로 다시 발행하지 않게
    한다. 이미 `MAX_REINDEX_ATTEMPTS`번 재발행한 항목은 발행하지 않고 `failed`로 닫아
    run 진행률의 실패 수로 드러낸다(호출자가 커밋).

    첫 문장으로 `SET LOCAL plan_cache_mode = force_custom_plan`을 낸다(#44 D4) — 호출자의
    현재 트랜잭션이 끝날 때까지 적용된다.

    대기열이 흐르는데도 cutoff보다 오래 멈춘 queued 항목이 있으면 **미뤘다는 사실을 경고로
    남긴다**(#43) — 미룸이 조용히 누적되면 run이 "진행 중"으로만 보인다. `hard_cutoff`를 주면
    그보다 오래 갱신이 없는 run은 흐름과 무관하게 재발행한다(호출자가 cutoff ×
    `HARD_STALLED_MULTIPLIER`로 계산). 판정은 run의 마지막 갱신으로 하므로 항목이 차례로
    닫히며 빠지는 긴 줄은 상한에 걸리지 않는다.

    한계: 어떤 큐의 워커가 멈추고 다른 처리도 없는 동안에는 대기 중인 메시지와 잃은
    메시지를 구분할 수 없다. 그 사이 상한에 닿아 닫힌 항목의 메시지가 나중에 처리되면
    재인덱싱이 아니라 일반 인제스트(원본부터)로 처리된다. reaper가 상한으로 닫은 항목도
    `updated_at`이 갱신되므로 다음 한 창(cutoff) 동안은 "흐름" 신호로 보인다 — 한 번만
    일어나고 회수는 한 창 뒤 이어진다.
    """
    await db.execute(text(FORCE_CUSTOM_PLAN))
    queue_flowing = await db.scalar(
        select(
            select(Source.id)
            .where(Source.status.in_(_WORKING_STATUSES), Source.updated_at >= cutoff)
            .exists()
            | select(ReindexRunItem.id)
            .where(ReindexRunItem.status != "queued", ReindexRunItem.updated_at >= cutoff)
            .exists()
        )
    )
    effective_cutoff = cutoff
    if queue_flowing:
        # 미룸을 드러낸다(#43). 조건이 `status = 'queued'`라 부분 인덱스
        # `ix_reindex_run_items_queued`를 타므로 스윕마다 붙는 비용이 작다.
        deferred = await db.scalar(
            select(
                select(ReindexRunItem.id)
                .where(OPEN_REINDEX_ITEM, ReindexRunItem.updated_at < cutoff)
                .exists()
            )
        )
        if not deferred:
            return []
        logger.warning(
            "재인덱싱 유실 회수 보류 — 대기열이 흐르는 중",
            cutoff=cutoff.isoformat(),
            hard_cutoff=hard_cutoff.isoformat() if hard_cutoff is not None else None,
        )
        if hard_cutoff is None:
            return []
        effective_cutoff = hard_cutoff

    # queued 항목이 있는 run으로 먼저 좁힌다(#44 D3) — 안쪽은 queued 부분 인덱스를 탄다.
    # 바깥 집계는 그 run의 **모든** 항목(done·failed 포함)의 마지막 갱신을 본다: queued만
    # 보면 "오래된 queued + 방금 닫힌 항목"인 흐르는 run을 정체로 오판한다.
    open_runs = select(ReindexRunItem.run_id).where(OPEN_REINDEX_ITEM)
    stalled_runs = (
        select(ReindexRunItem.run_id)
        .where(ReindexRunItem.run_id.in_(open_runs))
        .group_by(ReindexRunItem.run_id)
        .having(func.max(ReindexRunItem.updated_at) < effective_cutoff)
    )
    rows = await db.execute(
        select(ReindexRunItem, ReindexRun)
        .join(ReindexRun, ReindexRun.id == ReindexRunItem.run_id)
        .join(Source, Source.id == ReindexRunItem.source_id)
        .where(
            ReindexRunItem.run_id.in_(stalled_runs),
            ReindexRunItem.status == "queued",
            Source.status.not_in(IN_FLIGHT_STATUSES),
        )
    )
    pairs = list(rows.tuples().all())
    exhausted = [item for item, _ in pairs if item.attempts >= MAX_REINDEX_ATTEMPTS]
    retry = [(item, run) for item, run in pairs if item.attempts < MAX_REINDEX_ATTEMPTS]
    # 두 UPDATE 모두 queued 조건을 다시 건다 — 조회 뒤 워커가 닫은 항목을 덮어쓰지 않는다
    if exhausted:
        await db.execute(
            update(ReindexRunItem)
            .where(
                ReindexRunItem.id.in_([item.id for item in exhausted]),
                ReindexRunItem.status == "queued",
            )
            .values(
                status="failed",
                error=f"정체로 {MAX_REINDEX_ATTEMPTS}회 재발행했지만 처리되지 않았습니다",
            )
            .execution_options(synchronize_session=False)
        )
        logger.warning(
            "재인덱싱 항목 재발행 상한 초과",
            count=len(exhausted),
            source_ids=[str(item.source_id) for item in exhausted],
        )
    if retry:
        await db.execute(
            update(ReindexRunItem)
            .where(
                ReindexRunItem.id.in_([item.id for item, _ in retry]),
                ReindexRunItem.status == "queued",
            )
            .values(attempts=ReindexRunItem.attempts + 1, updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
    return retry
