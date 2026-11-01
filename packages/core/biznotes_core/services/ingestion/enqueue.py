"""소스 인제스트 큐잉·상한 — 라우터와 runtime 도구가 공유하는 단일 관문 (R1-3).

원래 `_enqueue_ingest`·노트북당 소스 상한은 라우터(`api/v1/sources.py`)의
프라이빗 헬퍼였고, runtime.py의 register_source 도구가 이를 역임포트해
service→API 레이어 역전을 만들었다(§1.2-3). 큐잉·상한 로직을 ingestion 서비스로
내려 라우터·runtime 진입점이 모두 이 모듈을 호출하도록 한다.
"""

import hashlib
import unicodedata
import uuid
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.config import get_settings
from biznotes_core.core.uow import UnitOfWork, publishing_unit_of_work
from biznotes_core.models import Source
from biznotes_core.services.ingestion.source_limits import effective_max_sources_per_notebook


@dataclass(frozen=True)
class SourceRoute:
    """ORM 객체 없이 발행할 때 쓰는 행 값 — 큐 라우팅에 드는 컬럼만 담는다(§5.3).

    재인덱싱은 대상이 수만 건일 수 있어 Source 객체를 적재하지 않는다. 그렇다고 id만
    넘기면 HWP를 전용 큐로 보낼 수 없으므로(PLAN12-09), 호출자가 컬럼을 함께 뽑아
    이 값으로 넘긴다.
    """

    id: uuid.UUID
    type: str
    mime_type: str | None
    title: str | None
    # 메일 첨부는 이 값으로 알아본다(HWP-01) — 위치 인자 호출과 호환되게 끝에 기본값으로 둔다
    external_ref: str | None = None


def canonicalize_file_name(filename: str | None) -> str:
    """Return the notebook-scoped canonical name used for file identity.

    Paths are not part of a file's identity.  Replacing both path separators
    keeps uploads from POSIX and Windows clients equivalent before applying
    Unicode NFC normalization and Unicode whitespace trimming.
    """
    basename = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    canonical = unicodedata.normalize("NFC", basename).strip()
    if not canonical:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "파일명이 비어 있습니다"
        )
    return canonical


def _notebook_source_lock_key(notebook_id: uuid.UUID) -> int:
    """Map a notebook id to a stable PostgreSQL advisory-lock bigint."""
    namespace = get_settings().service_name
    digest = hashlib.sha256(f"{namespace}:sources:{notebook_id}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def lock_notebook_sources(db: AsyncSession, notebook_id: uuid.UUID) -> None:
    """Serialize all source admissions for one notebook until this transaction ends."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _notebook_source_lock_key(notebook_id)},
    )


async def notebook_source_limit(db: AsyncSession) -> int:
    """지금 적용되는 노트북당 소스 상한 — 관리자 재정의 > env(`max_sources_per_notebook`).

    소스를 더하는 모든 관문(`ensure_source_capacity`·`ensure_file_source_capacity_and_uniqueness`·
    `source_create.admit_source`)과 공개 조회가 이 값 하나를 쓴다. runtime의 register_source
    run-scoped 카운터와는 별개다. 폐기 별칭 `MAX_SOURCES_PER_NOTEBOOK`(#445)은 v0.31.0에서
    지웠다(#453) — 상한은 설정과 관리자 재정의로만 바꾼다.
    """
    return await effective_max_sources_per_notebook(db, get_settings())


def _notebook_full(limit: int) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT, f"노트북당 소스는 최대 {limit}개입니다"
    )


async def ensure_source_capacity(
    db: AsyncSession, notebook_id: uuid.UUID, additional: int = 1
) -> None:
    """Lock a notebook and enforce capacity in the caller's transaction.

    The caller must add Source rows and commit using this same session before
    the transaction (and therefore the advisory lock) is released.
    """
    if additional < 1:
        return
    await lock_notebook_sources(db, notebook_id)
    count = await source_count(db, notebook_id)
    limit = await notebook_source_limit(db)
    if count + additional > limit:
        raise _notebook_full(limit)


async def ensure_file_source_capacity_and_uniqueness(
    db: AsyncSession, notebook_id: uuid.UUID, filename: str | None, size_bytes: int
) -> str:
    """Lock, check capacity, and reject an existing notebook-scoped file identity.

    This function intentionally performs all reads after acquiring the xact
    lock.  The Source insert must happen in the same enclosing transaction.
    """
    canonical_name = canonicalize_file_name(filename)
    await lock_notebook_sources(db, notebook_id)

    count = await source_count(db, notebook_id)
    limit = await notebook_source_limit(db)
    if count >= limit:
        raise _notebook_full(limit)

    rows = await db.execute(
        select(Source).where(
            Source.notebook_id == notebook_id,
            Source.type == "file",
            Source.size_bytes == size_bytes,
            Source.status != "deleted",
        )
    )
    for existing in rows.scalars():
        try:
            existing_name = canonicalize_file_name(existing.title)
        except HTTPException:
            continue
        if existing_name == canonical_name:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "detail": "이미 등록된 파일입니다",
                    "existing_source_id": str(existing.id),
                },
            )

    return canonical_name


def stage_ingest_job(db: AsyncSession, source: Source | SourceRoute, uow: UnitOfWork) -> None:
    """커밋 후 인제스트 발행을 예약한다 — **호출자의 UoW에 참여**한다 (R3-1e).

    트랜잭션 경계를 열지 않으므로, source 생성과 발행 예약을 **한 커밋에** 묶고 싶은
    호출자가 쓴다. `enqueue_ingest`는 이 함수를 자체 UoW로 감싼 얇은 래퍼다
    (중첩 금지 때문에 둘 중 하나만 쓸 수 있다).

    발행은 `uow.after_commit`으로 예약되어 **커밋이 성공한 뒤에만** 일어난다
    (결함 β, R3-0 설계 §1.4). 커밋이 실패하면 발행도 없고, 발행이 실패하면 source가
    `pending`으로 남아 reaper가 회수한다. `ingestion_jobs` 행은 더 이상 쓰지 않고(#163)
    모델도 지웠다(#168) — 읽는 곳이 없었고 상태가 실제와 무관했다.
    `db`는 호출 규약(시그니처) 호환으로 남는다.
    task id는 계속 미리 만들어 `send_task(task_id=...)`로 넘긴다 — 발행 계약을 그대로 둔다.

    flush하지 않은 source(`id is None`)는 거부한다 — 예전에는 job 행의 NOT NULL FK가
    커밋을 깨뜨려 드러났지만, 이제는 그대로 두면 `"None"`을 발행한다.

    `source`는 Source 인스턴스 또는 `SourceRoute`다 — 일괄 발행(재인덱싱)에서 수만 건의
    ORM 객체를 적재하지 않아도 되게 컬럼만 담은 값을 받는다. **id만으로는 발행하지
    않는다** — 큐를 발행 시점의 행 값으로 정하기 때문이다(§5.3).
    """
    if source.id is None:
        raise ValueError("stage_ingest_job: flush하지 않은 source(id 없음)는 발행할 수 없습니다")
    task_id = str(uuid.uuid4())
    source_id = str(source.id)
    uow.after_commit(lambda: _publish(source_id, task_id))


def ingest_uow(db: AsyncSession) -> AbstractAsyncContextManager[UnitOfWork]:
    """`stage_ingest_job`을 쓰는 트랜잭션 경계 — 발행 실패를 삼킨다.

    발행이 누락돼도 source가 `pending`으로 남아 `reap_stalled_sources`(최대 15분)가
    재발행하고, 재발행이 기존 실행과 겹쳐도 색인 교체는 소스 단위 잠금으로 직렬화되므로(#27),
    사용자에게 500을 돌려줄 이유가 없다 — `publishing_unit_of_work` 참조.
    """
    return publishing_unit_of_work(db)


async def enqueue_ingest(db: AsyncSession, source: Source) -> None:
    """이미 커밋된 source에 대해 커밋 후 인제스트 태스크를 발행한다.

    `stage_ingest_job`을 자체 경계로 감싼 얇은 래퍼. 호출자가 별도의 트랜잭션
    경계를 갖지 않을 때(runtime 도구·notes) 쓴다. **호출자의 UoW 안에서
    부르면 `NestedUnitOfWorkError`** — 그 경우 `ingest_uow` + `stage_ingest_job`을
    직접 써라.
    """
    async with ingest_uow(db) as uow:
        stage_ingest_job(db, source, uow)


def _publish(source_id: str, task_id: str) -> None:
    """미리 정한 task id로 인제스트 태스크를 발행한다 (커밋 후 실행).

    태스크를 import하지 않고 **이름으로** 발행한다 — 태스크 모듈은 프로젝트에
    있으므로 import하면 코어가 프로젝트에 의존하게 된다. Celery는 이름으로
    라우팅하므로 동작은 동일하다. 큐는 `task_routes`가 정한다.
    """
    from biznotes_core.workers.celery_app import celery_app
    from biznotes_core.workers.task_names import INGEST_SOURCE

    celery_app.send_task(INGEST_SOURCE, args=[source_id], task_id=task_id)


async def source_count(db: AsyncSession, notebook_id: uuid.UUID) -> int:
    """노트북의 현재 소스 개수."""
    return (
        await db.execute(
            select(func.count()).select_from(Source).where(Source.notebook_id == notebook_id)
        )
    ).scalar_one()


async def check_source_limit(db: AsyncSession, notebook_id: uuid.UUID) -> None:
    """Backward-compatible single-source capacity guard."""
    await ensure_source_capacity(db, notebook_id)
