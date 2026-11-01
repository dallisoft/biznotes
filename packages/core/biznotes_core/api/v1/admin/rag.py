"""어드민 — RAG 설정/인덱스 현황/재인덱싱."""

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.api.deps import get_current_user
from biznotes_core.core.db import get_db
from biznotes_core.models import Notebook, ReindexRun, ReindexRunItem, Source, User
from biznotes_core.services.audit import record
from biznotes_core.services.ingestion.enqueue import ingest_uow
from biznotes_core.services.ingestion.reindex import (
    MAX_REINDEX_ATTEMPTS,
    OPEN_REINDEX_ITEM,
    ReindexProgress,
    pending_by_notebook,
    reindex_progress,
    start_reindex,
)
from biznotes_core.services.settings_store import get_setting, set_setting

router = APIRouter(prefix="/rag")


class RagSettings(BaseModel):
    chunk_size: int = Field(ge=100, le=4000)
    chunk_overlap: int = Field(ge=0, le=1000)
    top_k: int = Field(ge=1, le=30)
    rerank_enabled: bool = False
    rerank_candidates: int = Field(default=20, ge=5, le=50)
    rerank_model: str = Field(default="BAAI/bge-reranker-base", max_length=255)
    # [Phase 18a] 인용-청크 검증 2단계 임계값 (어휘 겹침 → 임베딩 코사인 폴백)
    verify_lexical_threshold: float = Field(default=0.25, ge=0.0, le=1.0)
    verify_embedding_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    # [Phase 18b] top-1 검색 점수 임계값(모드별 분리 — CR-10)
    insufficient_context_threshold_base: float = Field(default=0.5, ge=0.0, le=1.0)
    insufficient_context_threshold_rerank: float = Field(default=0.5, ge=0.0, le=1.0)
    # [#194] 게이트웨이 임베딩 요청 1건의 입력 수·타임아웃(초) — 인제스트에만 쓴다
    embedding_batch_size: int = Field(default=64, ge=1, le=2048)
    embedding_timeout_seconds: int = Field(default=60, ge=5, le=180)
    # [#209] 리랭크 1회(재시도 포함)의 벽시계 상한(초) — 채팅 첫 토큰 경로라 짧게 둔다
    rerank_timeout_seconds: int = Field(default=30, ge=5, le=180)

    @field_validator("rerank_model")
    @classmethod
    def _rerank_model_format(cls, v: str) -> str:
        """`gateway:<model_id>:<name>`·`fastembed:<model>`·접두사 없는 모델명·`""`(자동).

        저장은 입력 그대로다 — 접두사 없는 기존 값을 바꾸지 않아야 구 코어로 롤백해도 읽힌다.
        """
        from biznotes_core.services.rerank import is_valid_rerank_key

        if not is_valid_rerank_key(v):
            raise ValueError(
                "rerank_model 형식: gateway:<model_id>:<model_name> 또는 fastembed:<model>"
            )
        return v


_EXPLICIT_ONLY_FIELDS = frozenset(
    {"embedding_batch_size", "embedding_timeout_seconds", "rerank_timeout_seconds"}
)


class NotebookIndexStatus(BaseModel):
    notebook_id: uuid.UUID
    title: str
    source_count: int
    ready: int
    failed: int
    processing: int
    chunk_total: int
    # 재인덱싱 요청 후 아직 끝나지 않은 소스 수(처리 중 포함). 재인덱싱은 소스
    # status를 바꾸지 않으므로 진행률은 이 값으로 그린다 — (1 - pending/요청 수).
    reindex_pending: int = 0
    # 현재 임베딩 모델·청킹 설정과 다르게 색인된 ready 소스 수(#7) — stale_only 대상
    stale: int = 0


class ReindexRequest(BaseModel):
    notebook_id: uuid.UUID | None = None  # None = 전체
    failed_only: bool = False
    # False(기본): 저장된 파싱 결과로 재청킹·재임베딩만 — 원본 재수집 없음.
    # True: 원본을 다시 가져와 파싱부터 (원본 갱신·파서 개선을 반영할 때).
    refetch: bool = False
    # True: 현재 임베딩 모델·청킹 설정과 다르게 색인된 ready 소스만 (#7)
    stale_only: bool = False


class EmbeddingSpaceUsage(BaseModel):
    key: str  # sources.embedding_model 값 (NULL은 로컬 기본 모델 키로 합산)
    display_name: str
    active: bool  # 지금 인제스천이 쓰는 공간
    # 이 공간의 모델로 질의 벡터를 만들 수 있는가 — false면 해당 소스는 검색되지 않는다
    available: bool
    source_count: int  # 이 공간의 벡터를 가진 소스 수


class EmbeddingStatusOut(BaseModel):
    active_key: str
    active_display_name: str
    chunk_config: dict[str, int]  # 현재 RAG 청킹 설정
    spaces: list[EmbeddingSpaceUsage]
    stale_sources: int  # 현재 모델·청킹 설정과 다르게 색인된 ready 소스 수


class RerankerOption(BaseModel):
    key: str  # `rag.rerank_model`에 저장할 값
    display_name: str
    # 지금 해석할 수 있는가 — false면 이 값으로는 리랭크가 실패한다(삭제·비활성·이름 변경)
    available: bool


class RerankerStatusOut(BaseModel):
    saved_key: str  # 저장값 그대로(`""`는 자동)
    # 해석 결과 — 해석에 실패하면 None이고 `error`에 이유가 있다
    active_key: str | None
    active_display_name: str | None
    error: str | None = None
    options: list[RerankerOption]


class ReindexStartOut(BaseModel):
    enqueued: int
    skipped: int  # 이미 대기·처리 중이라 건너뛴 소스 수
    run_id: uuid.UUID | None  # 발행할 소스가 없으면 None


class ReindexRunOut(BaseModel):
    id: uuid.UUID
    notebook_id: uuid.UUID | None
    failed_only: bool
    refetch: bool
    requested: int
    queued: int  # 아직 끝나지 않음(처리 중 포함)
    done: int
    failed: int
    created_at: datetime
    # queued가 0이 된 뒤의 마지막 항목 갱신 시각. 저장하지 않고 계산한다 — 항목이 없으면
    # created_at이다(그때 last_update는 None). 남은 항목이 소스 삭제로 빠져 끝난 run은 삭제가
    # 항목 갱신 시각을 바꾸지 않으므로 실제 종료 시각보다 이를 수 있다
    finished_at: datetime | None
    # 아래는 #179에서 더했다. requested = queued + done + failed + removed
    removed: int = 0  # 처리 전에 소스가 지워져 집계에서 빠진 항목
    retrying: int = 0  # 대기 중이면서 reaper가 다시 발행한 적이 있는 항목
    max_attempts: int = MAX_REINDEX_ATTEMPTS  # 재발행 상한 — 넘으면 failed로 닫힌다
    last_update: datetime | None = None  # 항목의 마지막 갱신(항목이 없으면 None)
    stale_only: bool = False
    skipped: int | None = None  # 이미 대기·처리 중이라 건너뛴 후보(이전 run은 None)
    candidates: int | None = None  # requested + skipped
    scope: Literal["all", "notebook"] | None = None  # 이전 run은 None
    notebook_title: str | None = None  # 시작 시점 노트북 제목(노트북이 지워져도 남는다)


@router.get("/settings", response_model=RagSettings)
async def get_rag_settings(db: AsyncSession = Depends(get_db)) -> RagSettings:
    return RagSettings(**await get_setting(db, "rag"))


@router.put("/settings", response_model=RagSettings)
async def update_rag_settings(
    body: RagSettings,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> RagSettings:
    value = body.model_dump()
    # 새 필드(#194)는 본문에 있을 때만 저장한다 — 이 필드를 모르는 클라이언트의 PUT이
    # 운영자가 정한 값을 기본값으로 되돌리지 않게 한다
    for key in _EXPLICIT_ONLY_FIELDS - body.model_fields_set:
        value.pop(key)
    await record(db, admin.id, "rag.settings_update", detail=value)
    result = await set_setting(db, "rag", value)
    await db.commit()
    return RagSettings(**result)


@router.get("/index-status", response_model=list[NotebookIndexStatus])
async def index_status(db: AsyncSession = Depends(get_db)) -> list[NotebookIndexStatus]:
    # 지연 import — 임베딩 스택(fastembed)을 API 프로세스 기동에 끌어들이지 않는다
    from biznotes_core.services.embedding_spaces import (
        active_space,
        chunk_config_of,
        stale_condition,
    )

    active = await active_space(db)
    chunk_config = chunk_config_of(await get_setting(db, "rag"))
    rows = await db.execute(
        select(
            Notebook.id,
            Notebook.title,
            func.count(Source.id),
            func.count(Source.id).filter(Source.status == "ready"),
            func.count(Source.id).filter(Source.status == "failed"),
            func.count(Source.id).filter(
                Source.status.in_(["pending", "parsing", "chunking", "embedding"])
            ),
            func.coalesce(func.sum(Source.chunk_count), 0),
            func.count(Source.id).filter(
                Source.status == "ready", stale_condition(active.key, chunk_config)
            ),
        )
        .outerjoin(Source, Source.notebook_id == Notebook.id)
        .group_by(Notebook.id)
        .order_by(Notebook.updated_at.desc())
    )
    pending = await pending_by_notebook(db)
    return [
        NotebookIndexStatus(
            notebook_id=nb_id,
            title=title,
            source_count=total,
            ready=ready,
            failed=failed,
            processing=processing,
            chunk_total=chunks,
            reindex_pending=pending.get(nb_id, 0),
            stale=stale,
        )
        for nb_id, title, total, ready, failed, processing, chunks, stale in rows.all()
    ]


@router.get("/embedding", response_model=EmbeddingStatusOut)
async def embedding_status(db: AsyncSession = Depends(get_db)) -> EmbeddingStatusOut:
    """활성 임베딩 모델과, 기존 벡터가 어떤 모델로 만들어졌는지 (#7).

    모델을 바꾼 뒤에는 `stale_sources`가 0이 될 때까지 `stale_only` 재인덱싱을
    돌린다. `available=false`인 공간의 소스는 검색되지 않는다.
    """
    from biznotes_core.services.embedding_spaces import (
        LOCAL_SPACE_KEY,
        active_space,
        chunk_config_of,
        resolve_space,
        stale_condition,
    )

    active = await active_space(db)
    chunk_config = chunk_config_of(await get_setting(db, "rag"))
    key_col = func.coalesce(Source.embedding_model, LOCAL_SPACE_KEY)
    counts = dict(
        (
            await db.execute(
                select(key_col, func.count(Source.id))
                .where(Source.chunk_count > 0)
                .group_by(key_col)
            )
        )
        .tuples()
        .all()
    )
    counts.setdefault(active.key, 0)
    spaces = []
    for key, count in sorted(counts.items(), key=lambda kv: (kv[0] != active.key, kv[0])):
        space = active if key == active.key else await resolve_space(db, key)
        spaces.append(
            EmbeddingSpaceUsage(
                key=key,
                display_name=space.display_name if space else key,
                active=key == active.key,
                available=space is not None,
                source_count=count,
            )
        )
    stale = (
        await db.execute(
            select(func.count(Source.id)).where(
                Source.status == "ready", stale_condition(active.key, chunk_config)
            )
        )
    ).scalar_one()
    return EmbeddingStatusOut(
        active_key=active.key,
        active_display_name=active.display_name,
        chunk_config=chunk_config,
        spaces=spaces,
        stale_sources=stale,
    )


@router.get("/reranker", response_model=RerankerStatusOut)
async def reranker_status(db: AsyncSession = Depends(get_db)) -> RerankerStatusOut:
    """리랭커 선택지와 지금 쓰이는 리랭커(#209) — `GET /embedding`과 짝이다.

    선택지는 활성 리랭커 모델과 로컬 기본 모델이다. 저장값이 선택지에 없으면
    `available=false`로 함께 보여 준다 — 그 값으로는 리랭크가 실패한다.
    """
    from biznotes_core.services.gateway.llm import list_models
    from biznotes_core.services.rerank import (
        DEFAULT_RERANK_MODEL,
        DEFAULT_RERANK_TIMEOUT_SECONDS,
        FASTEMBED_RERANK_PREFIX,
        RerankUnavailableError,
        gateway_rerank_key,
        gateway_target,
        normalize_rerank_key,
        resolve_reranker,
    )
    from biznotes_core.services.settings_store import DEFAULTS

    rag_cfg = await get_setting(db, "rag")
    saved = str(rag_cfg.get("rerank_model", DEFAULTS["rag"]["rerank_model"]))
    def _callable(resolved: Any) -> bool:
        # 미지원 프로바이더 타입·base_url 없음은 고르는 순간 채팅이 실패한다 — 미리 드러낸다
        try:
            gateway_target(resolved, DEFAULT_RERANK_TIMEOUT_SECONDS)
        except RerankUnavailableError:
            return False
        return True

    options = [
        RerankerOption(
            key=gateway_rerank_key(m.model.id, m.model.model_name),
            display_name=m.model.display_name or m.model.model_name,
            available=_callable(m),
        )
        for m in await list_models(db, "reranker")
    ]
    options.append(
        RerankerOption(
            key=f"{FASTEMBED_RERANK_PREFIX}{DEFAULT_RERANK_MODEL}",
            display_name=f"{DEFAULT_RERANK_MODEL} (로컬)",
            available=True,
        )
    )
    active_key: str | None = None
    active_name: str | None = None
    error: str | None = None
    try:
        target = await resolve_reranker(db, saved)
        active_key, active_name = target.key, target.display_name
    except RerankUnavailableError as exc:
        error = str(exc)
    normalized = normalize_rerank_key(saved)
    if normalized and all(o.key != normalized for o in options):
        # 로컬 모델은 미리 받을 수 없어 해석만으로 판단한다(실제 로드는 첫 리랭크에서)
        options.append(
            RerankerOption(
                key=normalized,
                display_name=active_name or normalized,
                available=error is None,
            )
        )
    return RerankerStatusOut(
        saved_key=saved,
        active_key=active_key,
        active_display_name=active_name,
        error=error,
        options=options,
    )


@router.post("/reindex", response_model=ReindexStartOut)
async def trigger_reindex(
    body: ReindexRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> ReindexStartOut:
    """소스 재인덱싱 잡 일괄 발행 (임베딩 모델 교체/청킹 변경 후 사용).

    기본은 저장된 파싱 결과에서 시작한다 — 원격이 막혀도 멀쩡한 소스가 실패로
    떨어지지 않는다. 원본 재수집이 필요하면 `refetch=true`.

    소스 status는 그대로 둔다 — 진행률은 `GET /reindex/{run_id}` 또는 인덱스 현황의
    `reindex_pending`으로 본다. 이미 대기·처리 중인 소스는 다시 발행하지 않는다.
    """
    async with ingest_uow(db) as uow:
        started = await start_reindex(
            db,
            uow,
            requested_by=admin.id,
            notebook_id=body.notebook_id,
            failed_only=body.failed_only,
            refetch=body.refetch,
            stale_only=body.stale_only,
        )
        await record(
            db,
            admin.id,
            "rag.reindex",
            detail={
                "count": started.enqueued,
                "skipped": started.skipped,
                "failed_only": body.failed_only,
                "refetch": body.refetch,
                "stale_only": body.stale_only,
                "run_id": str(started.run.id) if started.run else None,
            },
        )
    return ReindexStartOut(
        enqueued=started.enqueued,
        skipped=started.skipped,
        run_id=started.run.id if started.run else None,
    )


async def _runs_out(db: AsyncSession, runs: list[ReindexRun]) -> list[ReindexRunOut]:
    progress = await reindex_progress(db, [r.id for r in runs])
    out = []
    for run in runs:
        p = progress.get(run.id) or ReindexProgress()
        out.append(
            ReindexRunOut(
                id=run.id,
                notebook_id=run.notebook_id,
                failed_only=bool(run.options.get("failed_only", False)),
                refetch=bool(run.options.get("refetch", False)),
                requested=run.requested,
                queued=p.queued,
                done=p.done,
                failed=p.failed,
                created_at=run.created_at,
                finished_at=(p.last_update or run.created_at) if p.queued == 0 else None,
                removed=p.removed,
                retrying=p.retrying,
                last_update=p.last_update,
                stale_only=bool(run.options.get("stale_only", False)),
                skipped=run.skipped,
                candidates=run.requested + run.skipped if run.skipped is not None else None,
                scope=run.scope if run.scope in ("all", "notebook") else None,  # type: ignore[arg-type]
                notebook_title=run.notebook_title,
            )
        )
    return out


@router.get("/reindex", response_model=list[ReindexRunOut])
async def list_reindex_runs(
    limit: int = 20, active_only: bool = False, db: AsyncSession = Depends(get_db)
) -> list[ReindexRunOut]:
    """최근 재인덱싱 요청과 진행률 (최신순) — 화면 재진입 시 진행 중인 run을 찾는 데 쓴다.

    `active_only=true`면 아직 처리할 항목(queued, 소스 남음)이 있는 run만 준다(#179) — 최근
    `limit`건 밖으로 밀려난 진행 중 run도 찾는다.
    """
    q = select(ReindexRun)
    if active_only:
        q = q.where(
            select(ReindexRunItem.id)
            .where(ReindexRunItem.run_id == ReindexRun.id, OPEN_REINDEX_ITEM)
            .exists()
        )
    runs = list(
        (
            await db.execute(
                q.order_by(ReindexRun.created_at.desc()).limit(max(1, min(limit, 100)))
            )
        )
        .scalars()
        .all()
    )
    return await _runs_out(db, runs)


@router.get("/reindex/{run_id}", response_model=ReindexRunOut)
async def get_reindex_run(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> ReindexRunOut:
    run = await db.get(ReindexRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reindex run not found")
    return (await _runs_out(db, [run]))[0]
