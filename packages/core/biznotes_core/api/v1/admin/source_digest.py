"""어드민 — 기존 소스 요약 백필(#454) 실행·진행률.

프로젝트가 `/admin` 집계 라우터에 `require_admin`을 걸어 조립한다. 이 모듈도 각 경로에서 관리자를
다시 확인한다. 실행은 감사 로그에 남는다(`source_digest.backfill`). 백필은 태스크 이름으로 발행한다
(`services.source_digest_backfill`) — 같은 범위에 대기·진행 중인 백필이 있으면 새로 발행하지 않는다.
"""

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.api.deps import require_admin
from biznotes_core.core.db import get_db
from biznotes_core.models import Notebook, User
from biznotes_core.schemas.source_digest import (
    SourceDigestBackfillRequest,
    SourceDigestBackfillStartOut,
    SourceDigestBackfillStatusOut,
)
from biznotes_core.services import source_digest_backfill as backfill
from biznotes_core.services.audit import record

logger = structlog.get_logger()

router = APIRouter(prefix="/source-digests")


@router.post(
    "/backfill",
    response_model=SourceDigestBackfillStartOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_source_digest_backfill(
    body: SourceDigestBackfillRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SourceDigestBackfillStartOut:
    """노트북 하나(`notebook_id`) 또는 모든 노트북의 최신 요약이 없는 ready 소스를 만든다.

    이미 최신 요약이 있는 소스는 건너뛰므로 멈춘 백필을 다시 실행하면 이어서 진행한다. 동시성·하루
    비용·개수 상한은 설정(`source_digest_backfill_*`)을 따른다. 없는 노트북은 404, 백필 상태 저장소
    (Redis)나 브로커에 닿지 못하면 503이다.
    """
    if body.notebook_id is not None and await db.get(Notebook, body.notebook_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    try:
        scheduled = await backfill.schedule_backfill(body.notebook_id)
        progress = await backfill.read_progress(body.notebook_id)
    except Exception as e:
        logger.warning("source_digest 백필 예약 실패", error=type(e).__name__)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "백필을 예약하지 못했습니다"
        ) from e
    scope = backfill.backfill_scope(body.notebook_id)
    await record(
        db,
        admin.id,
        "source_digest.backfill",
        resource=scope,
        detail={
            "notebook_id": str(body.notebook_id) if body.notebook_id is not None else None,
            "scheduled": scheduled,
        },
    )
    await db.commit()
    return SourceDigestBackfillStartOut(scope=scope, scheduled=scheduled, progress=progress)


@router.get("/backfill", response_model=SourceDigestBackfillStatusOut)
async def get_source_digest_backfill(
    notebook_id: uuid.UUID | None = None,
    _admin: User = Depends(require_admin),
) -> SourceDigestBackfillStatusOut:
    """그 범위(`notebook_id`, 없으면 모든 노트북)의 마지막 백필 진행률. 기록이 없으면 `progress`가
    null이다."""
    try:
        progress = await backfill.read_progress(notebook_id)
    except Exception as e:
        logger.warning("source_digest 백필 진행률 조회 실패", error=type(e).__name__)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "백필 진행률을 읽지 못했습니다"
        ) from e
    return SourceDigestBackfillStatusOut(
        scope=backfill.backfill_scope(notebook_id), progress=progress
    )
