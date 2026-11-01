"""노트북 개요 재생성 태스크 — Redis 토큰 디바운스.

예약 헬퍼(`schedule_overview_refresh`)는 라우터도 쓰므로 `biznotes_core.services.overview_refresh`에
있다(#321). 여기서는 같은 객체를 재노출한다 — 기존 import 경로와 테스트 패치 지점을 유지한다.
countdown 후 실행되는 태스크는 토큰이 최신일 때만 생성한다(연속 인제스천 시 마지막 1회만 LLM 호출).
"""

import uuid
from typing import Any

import redis as redis_sync
import structlog
from sqlalchemy.exc import IntegrityError

from biznotes_core.config import get_settings
from biznotes_core.models import Notebook
from biznotes_core.services.overview_refresh import DEBOUNCE_SECONDS as DEBOUNCE_SECONDS
from biznotes_core.services.overview_refresh import TOKEN_KEY as _TOKEN_KEY
from biznotes_core.services.overview_refresh import (
    schedule_overview_refresh as schedule_overview_refresh,
)
from biznotes_core.workers.celery_app import celery_app
from biznotes_core.workers.runner import get_session_factory, run_async

logger = structlog.get_logger()
# 개요 행의 노트북 FK — 생성 중 노트북이 지워진 경합에서 위반된다(#220)
_NOTEBOOK_FK = "notebook_overviews_notebook_id_fkey"


def _is_notebook_fk_violation(error: IntegrityError) -> bool:
    """개요 행의 노트북 FK 위반(23503)인지 — 다른 위반은 추측하지 않는다."""
    if getattr(error.orig, "sqlstate", None) != "23503":
        return False
    cause = getattr(error.orig, "__cause__", None)
    return getattr(cause, "constraint_name", None) == _NOTEBOOK_FK


def _notebook_deleted(notebook_id: str) -> dict[str, Any]:
    # 예약 뒤 노트북 삭제는 정상 사용자 동작이다 — ERROR가 아니다
    logger.info("overview skipped: notebook deleted", notebook_id=notebook_id)
    return {"ok": True, "skipped": "notebook_deleted"}


async def _run(notebook_id: str) -> dict[str, Any]:
    from biznotes_core.services.overview import generate_overview

    factory = get_session_factory()
    async with factory() as db:
        notebook = await db.get(Notebook, uuid.UUID(notebook_id))
        if notebook is None:
            return _notebook_deleted(notebook_id)
        try:
            overview = await generate_overview(db, uuid.UUID(notebook_id))
        except IntegrityError as e:
            # 확인 뒤 생성 중 삭제된 경합 — 그 FK 위반만 정상 종료로 본다
            if not _is_notebook_fk_violation(e):
                raise
            await db.rollback()
            return _notebook_deleted(notebook_id)
        return {"ok": overview is not None}


@celery_app.task(name="app.workers.tasks.overview.generate_notebook_overview", max_retries=0)
def generate_notebook_overview(notebook_id: str, token: str) -> dict[str, Any]:
    key = _TOKEN_KEY.format(notebook_id=notebook_id)
    client = redis_sync.Redis.from_url(get_settings().redis_url)
    try:
        current = client.get(key)
        current_token = current.decode() if isinstance(current, bytes) else current
        if current_token is not None and current_token != token:
            # 더 최신 예약이 존재 — 이번 실행은 건너뜀
            return {"ok": True, "skipped": "superseded"}
        result = run_async(_run(notebook_id))
        # 완료 — 토큰 + 수동 재생성 락 정리(조회 경로의 SET NX가 다시 예약 가능해짐)
        client.delete(key, f"overview:regen:{notebook_id}")
        return result
    finally:
        client.close()
