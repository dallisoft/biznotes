"""소스 요약 백필(#454) 관리 계약 — 실행 요청·진행률.

진행률은 DB가 아니라 Redis에 둔다(`services.source_digest_backfill`). 재개의 정본은 `source_digests`
캐시 행이다 — 진행률이 사라져도 다시 실행하면 이미 최신 요약이 있는 소스를 건너뛴다.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

#: 백필 상태 — 대기(예약됨)·진행·끝·상한으로 멈춤·오류로 멈춤
BackfillStatus = Literal["queued", "running", "done", "stopped", "failed"]
#: 상한으로 멈춘 이유 — 하루 비용 상한, 하루 생성 소스 수 상한
BackfillStopReason = Literal["daily_cost_cap", "daily_source_cap"]


class SourceDigestBackfillRequest(BaseModel):
    """`notebook_id`가 없으면 모든 노트북이다."""

    notebook_id: uuid.UUID | None = None


class SourceDigestBackfillProgress(BaseModel):
    """백필 한 범위(노트북 하나 또는 `all`)의 마지막 진행률.

    `total`은 시작할 때 센 ready 소스 수다. `skipped`는 이미 최신 요약이 있거나 청크가 없는 소스,
    `failed`는 생성·저장에 실패한 소스다(다음 실행이 다시 만든다). 상한으로 멈추면
    `total - generated - skipped - failed`개가 남는다.
    """

    scope: str
    notebook_id: uuid.UUID | None = None
    status: BackfillStatus
    total: int = 0
    generated: int = 0
    skipped: int = 0
    failed: int = 0
    stop_reason: BackfillStopReason | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None


class SourceDigestBackfillStartOut(BaseModel):
    """`scheduled`가 False면 같은 범위에 대기·진행 중인 백필이 있어 새로 발행하지 않았다."""

    scope: str
    scheduled: bool
    progress: SourceDigestBackfillProgress | None = None


class SourceDigestBackfillStatusOut(BaseModel):
    """`progress`가 없으면 그 범위의 백필 기록이 없다(실행한 적 없음 또는 만료)."""

    scope: str
    progress: SourceDigestBackfillProgress | None = None
