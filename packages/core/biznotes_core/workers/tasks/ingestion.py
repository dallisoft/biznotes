"""소스 인제스천 파이프라인: parse → chunk → embed → index.

두 단계로 나뉜다(#244, `docs/plans/ingest-stage-tasks/plan.md`).

- 진입·파싱(`ingest_source` → `_run`): 원본을 파싱해 parsed.md·위치를 저장하고 핸드오프
  상태를 커밋한다.
- 색인(`_run_index`): parsed.md만 읽어 청킹·임베딩·색인하고 `ready`로 만든다. 같은 태스크에서
  곧바로 한다(D6). 미룬 재시도와 reaper 재개는 `index_source`다.

Celery 태스크(동기)에서 프로세스 공용 루프(runner)로 DB 접근.
각 단계마다 sources.status 갱신 + Redis 이벤트 발행 → 좌측 패널 실시간 반영.
"""

import functools
import hashlib
import random
import time
import uuid
from datetime import datetime
from typing import Any

import anyio
import structlog
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from biznotes_core.config import get_settings
from biznotes_core.models import ReindexRunItem, Source, SourceChunk
from biznotes_core.services.embedding_spaces import (
    GATEWAY_BATCH_SIZE,
    GATEWAY_TIMEOUT_SECONDS,
    INGEST_RETRY_BUDGET_SECONDS,
    EmbeddingSpace,
    RetryBudget,
    TransientEmbeddingError,
    active_space,
    chunk_config_of,
    resolve_space,
)
from biznotes_core.services.events import publish_source_event
from biznotes_core.services.ingestion import source_warnings, stage_artifacts
from biznotes_core.services.ingestion.abort import PARSE_ABORT
from biznotes_core.services.ingestion.chunker import Chunk, chunk_markdown, count_tokens
from biznotes_core.services.ingestion.embedder import embed_dense, embed_sparse
from biznotes_core.services.ingestion.parsers import (
    Location,
    ParseError,
    ParseResult,
    parse_file,
    parse_web,
    replace_nul,
)
from biznotes_core.services.ingestion.reindex import (
    FORCE_CUSTOM_PLAN,
    HARD_STALLED_MULTIPLIER,
    IN_FLIGHT_STATUSES,
    close_reindex_items,
    reindex_mode,
    stalled_reindex_items,
    touch_reindex_items,
)
from biznotes_core.services.storage import (
    ensure_buckets,
    get_object,
    put_object,
    stat_object_size,
)
from biznotes_core.workers.celery_app import celery_app
from biznotes_core.workers.parsing_heartbeat import (
    HeartbeatStartFailed,
    ParsingOwnershipLost,
    parsing_heartbeat,
)
from biznotes_core.workers.runner import get_session_factory, run_async

logger = structlog.get_logger()

# `_transition`의 `warnings` 기본값 — 컬럼을 건드리지 않는다
_KEEP: Any = object()


async def _transition(
    db: AsyncSession,
    source: Source,
    *,
    frm: str | tuple[str, ...],
    to: str,
    error: str | None,
    warnings: Any = _KEEP,
    also: tuple[Any, ...] = (),
) -> datetime | None:
    """조건부 상태 전이(#244) — 행이 `frm` 상태일 때만 `to`로 바꾼다. 커밋은 호출자가 한다.

    반환: 새 `updated_at`, 0행이면 `None`(다른 실행이 행을 가져갔다 — 쓰지 말고 물러난다).
    점유한 실행만 행을 쓴다: 파싱 단계는 `parsing`, 색인 단계는 `embedding`을 쥔 동안만 쓴다.
    `also`는 같은 문장에 더할 조건이다.

    이 문장 전에 autoflush하지 않는다. 대입해 둔 컬럼(파싱 결과 등)은 같은 트랜잭션의 커밋이
    싣는다. 메모리 객체는 문장이 쓴 값으로 맞춘다 — 더티로 남기면 다음 flush가 다시 쓴다.
    """
    frms = (frm,) if isinstance(frm, str) else frm
    values: dict[str, Any] = {"status": to, "error_message": error, "updated_at": func.now()}
    if warnings is not _KEEP:
        values["warnings"] = warnings
    stmt = (
        update(Source)
        .where(Source.id == source.id, Source.status.in_(frms), *also)
        .values(**values)
        .returning(Source.updated_at)
        .execution_options(synchronize_session=False)
    )
    with db.no_autoflush:
        row = (await db.execute(stmt)).first()
    if row is None:
        return None
    set_committed_value(source, "status", to)
    set_committed_value(source, "error_message", error)
    if warnings is not _KEEP:
        set_committed_value(source, "warnings", warnings)
    updated_at: datetime = row[0]
    return updated_at


def _publish_status(source: Source, status: str, error: str | None) -> None:
    """상태 이벤트 — 커밋 **뒤**에 부른다. 실패를 삼킨다.

    이미 커밋한 전이의 처리 경로를 바꾸면 안 된다. 점유 커밋 뒤 Redis 오류가 "점유 전 실패"로
    읽히면 행이 주인 없이 남는다(재리뷰 R-m2).
    """
    try:
        publish_source_event(
            str(source.id),
            {"type": "status", "source_id": str(source.id), "status": status, "error": error},
        )
    except Exception:
        logger.warning("상태 이벤트 발행 실패", status=status, exc_info=True)


# 색인 교체 구간 잠금의 대기 상한(#27) — 넘기면 중복 실행이 상태를 쓰지 않고 물러난다.
# 대용량 행 교체보다 길고 reaper 창(15분)보다 짧다. `set_config`에 넘기는 값이다.
INDEX_LOCK_TIMEOUT = "5min"
# 잠금 대기 초과(lock_not_available)·문장 취소(query_canceled, 제품 statement_timeout)
_LOCK_BUSY_SQLSTATES = frozenset({"55P03", "57014"})


class _IndexLockBusy(Exception):
    """같은 소스의 교체가 진행 중이라 잠금을 얻지 못했다 — 상태를 쓰지 않고 물러난다."""


class _SourceGone(Exception):
    """교체 잠금을 잡고 보니 소스 행이 없다(삭제됨)."""


class _EmbeddingSuperseded(Exception):
    """임베딩 중 행이 `embedding`이 아니게 됐다 — reaper 회수나 사용자 재시도가 가져갔다."""


# 임베딩 일시 오류의 Celery 지연 재시도(#194) — 횟수와 countdown(초). countdown 상한은
# reaper 창(STALLED_THRESHOLD_MINUTES)보다 짧다. 미룬 `pending` 소스를 회수하지 않게 한다
INGEST_TRANSIENT_RETRIES = 3
# 프로바이더 동시성 상한에 밀린 재시도(#196). **Provider 장애와 별개의 예산**이다 —
# `limit=1`에 워커 2개인 배포에서는 대기가 정상 동작이라 상시 발생한다. 이걸 네트워크 오류
# 예산으로 세면 상한이 만든 유예까지 소스의 실패로 닫힌다.
INGEST_GATE_DEFERRALS = 10
_TRANSIENT_RETRY_BASE_SECONDS = 60.0
_TRANSIENT_RETRY_MAX_SECONDS = 600


def _transient_countdown(attempt: int, retry_after: float | None) -> int:
    """60·180·540초 근처(±20% 지터). `Retry-After`보다 이르지 않고, 상한을 넘지 않는다."""
    # `3**attempt`이 커져도 `_TRANSIENT_RETRY_MAX_SECONDS`가 건다 — 10회에서도 오버플로 없다
    backoff = _TRANSIENT_RETRY_BASE_SECONDS * 3**attempt * random.uniform(0.8, 1.2)
    return int(min(_TRANSIENT_RETRY_MAX_SECONDS, max(retry_after or 0.0, backoff)))


def _embedding_tick_stmt(source_id: uuid.UUID) -> Any:
    """임베딩 heartbeat 문장 — `embedding` 행의 `updated_at`만 갱신한다."""
    return (
        update(Source)
        .where(Source.id == source_id, Source.status == "embedding")
        .values(updated_at=func.now())
        .execution_options(synchronize_session=False)
    )


async def _embedding_heartbeat(source_id: uuid.UUID) -> None:
    """임베딩 구간 생존 신호(#194 D4) — 배치마다 `updated_at`을 갱신한다.

    reaper가 긴 임베딩을 정체로 보고 재발행하지 않게 한다.

    소유권 확인이 아니다. 동시 실행의 정합은 교체 잠금과 marker가 지킨다(#27·#50). 짧은 별도
    세션에서 곧바로 커밋한다 — 본 세션은 임베딩 동안 트랜잭션을 열지 않는다. 0행이면 행이
    `embedding`이 아니게 됐다는 뜻이라 물러난다. DB 오류는 처리를 멈추지 않는다.
    """
    try:
        async with get_session_factory()() as hb:
            row = (
                await hb.execute(_embedding_tick_stmt(source_id).returning(Source.id))
            ).first()
            await hb.commit()
    except Exception:
        logger.warning("임베딩 heartbeat 실패 — 계속한다", source_id=str(source_id), exc_info=True)
        return
    if row is None:
        raise _EmbeddingSuperseded(str(source_id))


async def _embed_gateway(
    space: EmbeddingSpace, texts: list[str], source_id: uuid.UUID, rag_cfg: dict[str, Any]
) -> list[list[float]]:
    """게이트웨이 공간 임베딩 — `rag` 설정의 배치 크기로 나눠 부르고 배치마다 heartbeat를 낸다.

    대기 예산(`RetryBudget`)은 이 시도의 모든 배치가 나눠 쓴다(#194).
    """
    batch_size = int(rag_cfg.get("embedding_batch_size", GATEWAY_BATCH_SIZE))
    timeout = float(rag_cfg.get("embedding_timeout_seconds", GATEWAY_TIMEOUT_SECONDS))
    budget = RetryBudget(INGEST_RETRY_BUDGET_SECONDS)
    dense: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        dense.extend(
            space.embed(batch, batch_size=batch_size, timeout=timeout, retry_budget=budget)
        )
        if start + batch_size < len(texts):
            await _embedding_heartbeat(source_id)
    return dense


async def _retry_still_wanted(db: AsyncSession, source: Source, reindex: bool | None) -> bool:
    """미룬 재시도(#194)가 아직 할 일인지 — 그 사이 다른 실행이 처리했으면 아무것도 쓰지 않는다.

    미룸은 신규 소스를 `pending`으로, 재인덱싱 중인 ready 소스를 열린 항목과 함께 `ready`로
    둔다. 그 밖의 상태이거나 항목이 닫혔으면 사용자 재시도·reaper·run 상한이 가져간 것이다.
    ready는 재인덱싱 재시도(`reindex=True`)일 때만 이어 간다 — 신규 소스 재시도가 그 사이
    색인을 얻은 ready 소스를 pending·failed로 덮지 않게 한다.
    """
    if source.status == "pending":
        return True
    if source.status == "ready" and reindex:
        open_reindex, _ = await reindex_mode(db, source.id)
        return open_reindex
    return False


async def _defer_transient(
    db: AsyncSession,
    source: Source,
    error: TransientEmbeddingError,
    counter: int,
    cap: int,
    keep_previous: bool,
) -> tuple[int, str, datetime] | None:
    """일시 오류로 이번 시도를 접고 상태를 되돌린다. 반환: (countdown 초, 미룬 상태, 그 updated_at).

    재인덱싱 중인 ready 소스는 이전 색인을 유지한 채 `ready`로, 그 밖은 `pending`으로 둔다(D3-B).
    `ready` 행의 안내는 `error_message`가 아니라 경고 `reindex_deferred`다(#230). 신규 소스의
    "임베딩 지연" 안내는 in-flight 행이라 `error_message`에 둔다(D5-A). 어느 쪽이든 이전 재인덱싱
    안내 코드는 먼저 뺀다.
    전이는 이 실행이 쥔 `embedding`에서만 한다 — 0행이면 다른 실행이 가져갔다(`None`).
    재인덱싱 항목은 닫지 않고 `updated_at`만 갱신한다 — 미루는 동안 run이 정체로 보이지
    않게 한다. 같은 커밋에 실린다.

    `counter`와 `cap`은 **같은 카운터의 값과 상한**이어야 한다(#196) — 게이트 미럼에는
    `INGEST_GATE_DEFERRALS`, 그 밖에는 `INGEST_TRANSIENT_RETRIES`를 넘긴다. 다른 카운터와
    상한을 섞으면 메시지에 `(12/3)` 같은 분모가 찍힌다.
    """
    countdown = _transient_countdown(counter, error.retry_after)
    label = "동시성 상한 대기" if error.reason == "gate" else "임베딩 서버 일시 오류"
    note = f"{label}, {countdown}초 뒤 다시 시도합니다({counter + 1}/{cap}): {error}"
    warnings = source_warnings.without_codes(source.warnings, source_warnings.REINDEX_CODES)
    message: str | None
    if keep_previous:
        # `ready` 행의 안내는 경고 코드다 — `error_message`는 실패 전용이다(#230)
        to, message = "ready", None
        warnings = [
            *(warnings or []),
            source_warnings.warning(
                source_warnings.REINDEX_DEFERRED,
                reason=_failure_reason(error),
                attempt=counter + 1,
                max_attempts=cap,
                retry_in_seconds=countdown,
            ),
        ]
    else:
        to, message = "pending", f"임베딩 지연 — {note}"
    deferred_at = await _transition(
        db, source, frm="embedding", to=to, error=message, warnings=warnings
    )
    if deferred_at is None:
        await _quiet_rollback(db, str(source.id))
        return None
    await touch_reindex_items(db, source.id)
    await db.commit()
    _publish_status(source, to, message)
    return countdown, to, deferred_at


def _source_index_lock_key(source_id: uuid.UUID) -> int:
    namespace = get_settings().service_name
    digest = hashlib.sha256(f"{namespace}:source-index:{source_id}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def _index_marker(db: AsyncSession, source_id: uuid.UUID) -> uuid.UUID | None:
    """커밋된 청크 행 하나의 id — 색인 교체는 행을 전부 새로 만들므로 교체가 커밋되면 바뀐다.

    잠금 밖에서 실패한 실행이 "시작한 뒤 다른 실행이 교체를 커밋했는지"를 판정한다(#50).
    """
    # 행 집합이 같으면 같은 값이어야 한다 — 정렬 없는 `LIMIT 1`은 계획에 따라 다른 행이 나올 수
    # 있다. PostgreSQL에는 uuid용 min 집계가 없어 정렬로 고른다
    return await db.scalar(
        select(SourceChunk.id)
        .where(SourceChunk.source_id == source_id)
        .order_by(SourceChunk.id)
        .limit(1)
    )


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


async def _lock_source_index(db: AsyncSession, source_id: uuid.UUID) -> None:
    """색인 교체 구간 잠금(#27) — 이 트랜잭션이 끝날 때 풀린다.

    같은 소스의 교체(행·벡터 교체 → 커밋)를 직렬화한다. 대기 상한은 잠금 문장에만 걸고
    획득 뒤에는 기본값으로 되돌린다 — 뒤 문장(행 교체·ready UPDATE)의 대기 초과를 busy로
    오분류하면 실패 처리 없이 물러나게 된다.

    잠금 뒤 `FOR KEY SHARE`로 소스 행을 확인한다. heartbeat·상태·제목 UPDATE와는 충돌하지
    않으면서, 부모 행 DELETE를 이 트랜잭션이 끝날 때까지 기다리게 한다 — 삭제는 교체가 끝난
    뒤 CASCADE로 진행된다(청크 행과 벡터가 함께 사라진다).
    """
    await db.execute(
        text("SELECT set_config('lock_timeout', :t, true)"), {"t": INDEX_LOCK_TIMEOUT}
    )
    try:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": _source_index_lock_key(source_id)}
        )
    except DBAPIError as e:
        if _sqlstate(e) in _LOCK_BUSY_SQLSTATES:
            raise _IndexLockBusy(str(source_id)) from e
        raise
    await db.execute(text("SET LOCAL lock_timeout TO DEFAULT"))
    rows = (
        await db.execute(
            select(Source.id)
            .where(Source.id == source_id)
            .with_for_update(read=True, key_share=True)  # FOR KEY SHARE (read 없이는 NO KEY UPDATE)
        )
    ).all()
    if not rows:
        raise _SourceGone(str(source_id))


def _effective_mode(
    status: str, open_item: tuple[bool, bool], reindex: bool | None, refetch: bool | None
) -> tuple[bool, bool]:
    """열린 재인덱싱 항목과 소스 상태로 처리 모드를 정한다.

    재시도(`/sources/{id}/retry`)와 정체 회수는 status를 `pending`으로 바꾼 뒤
    발행한다 — 그 경우는 요청자가 원본부터 다시 처리하기를 기대한 것이므로,
    재인덱싱 항목이 열려 있어도 재인덱싱으로 보지 않는다. 명시 인자가 우선한다.
    """
    open_reindex, open_refetch = open_item
    if reindex is None:
        reindex = open_reindex and status != "pending"
    if refetch is None:
        refetch = open_refetch
    return reindex, refetch


async def _close_reindex(source_id: uuid.UUID, ok: bool, error: str | None) -> None:
    """재인덱싱 진행률 기록 — 실패해도 인제스천 결과를 바꾸지 않는다.

    별도 세션을 쓴다: 기록이 실패해 롤백하더라도 인제스천 세션의 객체가 만료되지
    않는다. 닫지 못한 항목은 run이 멈춘 것으로 보여 reaper가 재발행하고, 그 처리에서
    닫힌다.
    """
    try:
        async with get_session_factory()() as db:
            await close_reindex_items(db, source_id, ok=ok, error=error)
    except Exception:
        logger.exception("재인덱싱 항목 기록 실패", source_id=str(source_id))


def _parse(source: Source, raw: bytes | None) -> ParseResult:
    if source.type == "file":
        assert raw is not None and source.mime_type
        return parse_file(raw, source.mime_type)
    if source.type == "web":
        assert source.origin_url
        return parse_web(source.origin_url)
    if source.type == "text":
        assert raw is not None
        return ParseResult(markdown=raw.decode("utf-8"))
    raise ParseError(f"알 수 없는 소스 타입: {source.type}")


async def _parse_stage(db: AsyncSession, source: Source) -> tuple[ParseResult, str]:
    """원본을 가져와 markdown으로 파싱하고 parsed 버킷에 저장한다.

    parsing 구간은 상태 갱신이 없어 heartbeat가 updated_at을 대신 갱신한다(계획 A §5-7).
    구간 안에서는 db가 sources 행을 flush하면 안 된다 — 자기 heartbeat가 상실로 오판한다.
    동기 호출은 전부 to_thread로 보낸다(abandon 없음 — 취소는 스레드 종료를 기다린다).

    반환: (파싱 결과, parsed.md 세대 해시). 위치(`locations`)가 있으면 parsed.md보다 **먼저**
    `locations.json`으로 올린다 — parsed.md가 보이는 시점에 같은 세대의 위치가 이미 있다.
    색인 단계는 다른 태스크라서 위치를 이 파일로 넘긴다(#244).

    호출자(`_run`)가 행을 `parsing`으로 점유한 뒤에 부른다.
    """
    settings = get_settings()
    async with parsing_heartbeat(
        get_session_factory(),
        source.id,
        interval_sec=settings.parsing_heartbeat_sec,
        tick_timeout_sec=settings.parsing_heartbeat_tick_timeout_sec,
    ) as hb:
        abort_token = PARSE_ABORT.set(hb.abort)
        try:
            raw = None
            if source.original_object_key:
                raw = await anyio.to_thread.run_sync(
                    get_object, settings.storage_bucket_sources, source.original_object_key
                )
            result = await anyio.to_thread.run_sync(_parse, source, raw)

            # 제목은 외부가 정한 문자열이다 — 정화하지 않으면 NUL 등을 PostgreSQL이 거부한다(HWP-02)
            document_title = source_warnings.sanitize_document_title(result.title)
            # 본문도 그렇다 — 모든 파서(웹·문서) 출력의 U+0000을 공백으로(#462)
            result.markdown = replace_nul(result.markdown)
            if document_title and source.title.startswith("(제목 추출 중)"):
                source.title = document_title

            parsed_key = f"{source.id}/parsed.md"
            parsed_bytes = result.markdown.encode("utf-8")
            digest = stage_artifacts.parsed_digest(parsed_bytes)
            if result.locations:
                # 위치가 없는 형식은 쓰지도 지우지도 않는다 — 남은 옛 파일은 해시가 달라 무시된다
                await anyio.to_thread.run_sync(
                    functools.partial(
                        put_object,
                        settings.storage_bucket_parsed,
                        stage_artifacts.locations_key(source.id),
                        stage_artifacts.encode_locations(digest, result.locations),
                        content_type="application/json",
                    )
                )
            await anyio.to_thread.run_sync(
                functools.partial(
                    put_object,
                    settings.storage_bucket_parsed,
                    parsed_key,
                    parsed_bytes,
                    content_type="text/markdown",
                )
            )
            source.parsed_object_key = parsed_key
            # 문서 자체 제목과 인제스트 경고(HWP-02·03) — 전체 파싱마다 교체한다. parsed.md를 올린
            # 뒤에 대입해 parse 단계 실패는 두 컬럼을 바꾸지 않는다. 다음 상태 전이 커밋에 함께
            # 실린다(새 커밋 지점 없음). 파싱이 성공했으면 뒤 단계가 실패해도 남는다 —
            # parsed.md·자리표시자 제목과 같은 정책이다
            source.document_title = document_title
            source.warnings = list(result.source_warnings)
        finally:
            PARSE_ABORT.reset(abort_token)
    return result, digest


# 파싱 단계가 점유할 수 있는 상태(#244 리뷰 B1·재리뷰 R-M1). 일반 인제스트의 정당한 발행 경로
# (업로드·사용자 재시도·reaper·에이전트 등록)는 모두 `pending`에서 출발한다 — `ready`·`failed` 행에
# 온 일반 메시지는 낡은 중복이다(reaper 재발행·재배달). 원본부터 재파싱하는 재인덱싱만 종결 상태에서
# 출발한다. `chunking`·`embedding`은 다른 세대가 색인 중이라 어느 쪽도 점유하지 않는다
_PARSE_FROM_STATUSES = ("pending", "parsing")
_REINDEX_PARSE_FROM_STATUSES = ("pending", "parsing", "ready", "failed")
# 재인덱싱이 색인 단계에 넘길 때 상태를 쓰지 않는 출발 상태 — 점유 전까지 이전 색인으로 검색된다
_REINDEX_CLAIM_STATUSES = ("ready", "failed")
# 색인 단계 점유의 출발 상태(`claim_from`) — `chunking`은 파싱이 끝난 소스, `ready`·`failed`는
# 재인덱싱 대상, `pending`은 신규 소스의 미룬 재시도다(`deferred_at` 토큰이 있어야 한다)
CLAIM_FROM_STATUSES = ("chunking", *_REINDEX_CLAIM_STATUSES, "pending")


async def _run(
    source_id: str,
    reindex: bool | None = None,
    refetch: bool | None = None,
    transient_attempt: int = 0,
    gate_attempt: int = 0,
) -> dict[str, Any]:
    """진입·파싱 단계 — 소스 1건을 파싱하고 색인 단계(`_run_index`)로 넘긴다(#244).

    **재인덱싱 여부는 기본적으로 DB에서 읽는다**(`reindex_mode`) — 열린 재인덱싱
    항목이 있고 소스가 재시도로 `pending` 전환된 것이 아니면 재인덱싱이다. 모드를
    브로커 메시지에 싣지 않으므로 태스크 계약이 그대로이고, 배포 중 구 워커가 새
    메시지를 받아도 깨지지 않는다. 인자를 명시하면 그 값이 우선한다(운영·테스트용).

    재인덱싱은 저장된 파싱 결과(`parsed_object_key`)에서 시작한다 — 파싱하지 않고 곧바로
    넘긴다. 원본(웹)을 다시 가져오지 않으므로 원격이 막혀도 멀쩡한
    소스가 실패로 떨어지지 않는다(#6). 파싱 결과가 없거나 `refetch`면 원본부터
    처리한다.

    파싱은 행을 `parsing`으로 **조건부 점유**한 뒤 한다. 행이 `chunking`·`embedding`이면 다른
    세대가 색인 중이라 아무것도 쓰지 않고 물러난다 — 점유한 색인 단계가 읽는 parsed.md를 그동안
    바꾸지 못하게 한다(리뷰 B1). 일반 인제스트는 `pending`·`parsing`에서만 점유한다 — `ready`·
    `failed` 행에 온 일반 메시지는 낡은 중복이라 끝난 문서를 다시 파싱하지 않는다(재리뷰 R-M1).
    다시 인제스트하려면 사용자 재시도처럼 먼저 `pending`으로 바꾼다. 파싱이 끝나면 `chunking`을
    `parsing`에서만 커밋하고 결과의 `handoff`에 색인 단계 kwargs를 담는다. 청킹·임베딩·색인은
    여기서 하지 않는다 — HWP는 이 단계만 egress가 막힌 전용 워커(`ingest.hwp`)에서 돈다.

    **이전 색인이 있는 재인덱싱은 색인 단계가 점유할 때까지 `ready`를 유지한다**(`claim_from`,
    리뷰 B2). 저장된 parsed.md로 하는 재인덱싱은 상태를 쓰지 않고, 원본부터 재파싱한 재인덱싱은
    파싱 뒤 `ready`로 되돌린다. 실패하면 이전 색인을 유지할 수 있는지(`keep_previous`, #7)도
    넘긴다. 파싱 단계의 실패도 같은 규칙을 탄다.

    `transient_attempt`·`gate_attempt`는 **구 버전이 미룬 재시도 메시지**에만 있다(#194·#196).
    `_retry_still_wanted`로 확인한 뒤 색인 단계로 그대로 넘긴다 — 미룸은 이제 색인 단계가 한다.
    """
    attempt = transient_attempt or gate_attempt
    sid = uuid.UUID(source_id)
    async with get_session_factory()() as db:
        # 행보다 먼저 읽는다 — 그 사이 커밋된 교체도 "시작한 뒤의 교체"로 잡힌다(#50)
        marker = await _index_marker(db, sid)
        source = await db.get(Source, sid)
        if source is None:
            return {"ok": False, "error": "source not found"}
        if attempt > 0 and not await _retry_still_wanted(db, source, reindex):
            logger.warning(
                "미룬 재시도 — 다른 실행이 이미 처리해 물러난다",
                source_id=source_id,
                status=source.status,
            )
            return {"ok": False, "error": "retry_superseded"}

        if reindex is None or refetch is None:
            reindex, refetch = _effective_mode(
                source.status, await reindex_mode(db, source.id), reindex, refetch
            )

        keep_previous = bool(reindex) and source.status == "ready"
        digest: str | None = None
        # 색인 단계가 점유할 상태 — 그때까지 행이 머무는 상태다
        claim_from = "chunking"
        # 이 실행이 행을 `parsing`으로 점유했는가 — 점유 전 실패는 아무것도 쓰지 않는다
        parsing = False
        started = time.monotonic()
        try:
            if reindex and not refetch and source.parsed_object_key:
                if source.status in _REINDEX_CLAIM_STATUSES:
                    # 저장된 parsed.md로 재인덱싱 — 상태를 쓰지 않고 넘긴다. 위치는 색인 단계가
                    # locations.json에서 읽는다(이 기능 전에 파싱된 소스는 위치 없이 청킹된다).
                    # 열린 항목만 갱신한다 — 넘긴 동안 run이 정체로 보여 재발행되지 않게(R-m1)
                    claim_from = source.status
                    await touch_reindex_items(db, source.id)
                    await db.commit()
                elif source.status == "pending" and attempt > 0:
                    # 구 버전이 미룬 신규 소스(`pending`, 저장된 parsed.md로 재시도) — 행을
                    # `chunking`으로 가져와 넘긴다. 미룸 토큰이 없어 `pending`으로는 점유하지
                    # 못한다. 구 버전의 미룸은 언제나 `error_message`("임베딩 지연 — …")를 썼고
                    # 사용자 재시도·reaper는 그것을 비운다 — 비어 있으면 그쪽의 `pending`이다
                    moved = await _transition(
                        db,
                        source,
                        frm="pending",
                        to="chunking",
                        error=None,
                        also=(Source.error_message.is_not(None),),
                    )
                    if moved is None:
                        await _quiet_rollback(db, source_id)
                        return {"ok": False, "error": "retry_superseded"}
                    await db.commit()
                    _publish_status(source, "chunking", None)
                else:
                    # 다른 실행이 처리 중이다 — 그 실행이 끝날 때 재인덱싱 항목을 닫는다.
                    # 롤백이 행 객체를 만료시키므로 상태는 그 전에 읽는다(AsyncSession)
                    seen = source.status
                    await _quiet_rollback(db, source_id)
                    logger.warning(
                        "재인덱싱 — 다른 실행이 처리 중이라 물러난다",
                        source_id=source_id,
                        status=seen,
                    )
                    return {"ok": False, "error": "in_flight"}
            else:
                parse_from = _REINDEX_PARSE_FROM_STATUSES if reindex else _PARSE_FROM_STATUSES
                if await _transition(db, source, frm=parse_from, to="parsing", error=None) is None:
                    # 다른 세대가 색인 중이거나(`chunking`·`embedding`), 이미 끝난 행에 온 낡은
                    # 중복 메시지다(`ready`·`failed`). 아무것도 쓰지 않는다
                    seen = source.status
                    await _quiet_rollback(db, source_id)
                    reason = "already_parsed" if seen in _INDEX_STAGE_STATUSES else "stale_message"
                    logger.warning(
                        "파싱 — 점유할 수 없는 상태라 물러난다",
                        source_id=source_id,
                        status=seen,
                        reason=reason,
                    )
                    return {"ok": False, "error": reason}
                await db.commit()
                _publish_status(source, "parsing", None)
                parsing = True
                # 원본은 스토리지(파일·텍스트) 또는 URL(웹)
                result, digest = await _parse_stage(db, source)
                if keep_previous:
                    # 점유 전까지 이전 색인으로 검색되게 한다. parsed.md는 이미 새 본문이다
                    claim_from = "ready"
                if await _transition(db, source, frm="parsing", to=claim_from, error=None) is None:
                    # 파싱을 마친 사이 다른 실행이 행을 가져갔다(reaper 등) — 넘기지 않는다
                    await _quiet_rollback(db, source_id)
                    logger.warning("파싱 뒤 행을 잃음 — 넘기지 않는다", source_id=source_id)
                    return {"ok": False, "error": "ownership_lost"}
                if keep_previous:
                    # `ready`로 기다리는 동안 run이 정체로 보여 재발행(재파싱)되지 않게(R-m1)
                    await touch_reindex_items(db, source.id)
                await db.commit()
                _publish_status(source, claim_from, None)
                logger.info(
                    "파싱 완료 — 색인 단계로 넘긴다",
                    source_id=source_id,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    locations=len(result.locations),
                    parsed_sha256=digest[:12],
                )
        except ParsingOwnershipLost:
            # 행은 이제 다른 소유자의 것이다 — 상태·error_message·parsed.md를 쓰지 않는다.
            # 재인덱싱 항목도 닫지 않는다 — 지금 그 행을 가진 쪽이 끝낼 때 닫는다.
            # 기록은 heartbeat가 남긴 ingest.parse.heartbeat_lost 하나다.
            return {"ok": False, "error": "ownership_lost"}
        except HeartbeatStartFailed:
            # 본문에 들어가지 않았다. 행은 parsing으로 남고 reaper가 재발행한다(15~20분 + 큐 지연).
            return {"ok": False, "error": "heartbeat_start_failed"}
        except Exception as e:
            if not parsing:
                # 행을 점유하기 전이다 — 남의 행에 쓰지 않는다. 남은 행은 reaper가 회수한다
                await _quiet_rollback(db, source_id)
                logger.warning("진입 단계 실패 — 점유 전이라 쓰지 않는다", exc_info=True)
                return {"ok": False, "error": str(e)}
            error = _failure_text(e, source_id)
            withdrawn = await _reread_after_prep_failure(
                db, source, source_id, marker, e, owned="parsing"
            )
            if withdrawn is not None:
                return withdrawn
            await _write_failure(
                db, source, error, keep_previous, owned="parsing", reason=_failure_reason(e)
            )
            return {"ok": False, "error": str(e)}
    handoff: dict[str, Any] = {
        "parsed_sha256": digest,
        "keep_previous": keep_previous,
        "claim_from": claim_from,
        "transient_attempt": transient_attempt,
        "gate_attempt": gate_attempt,
    }
    return {"ok": True, "stage": "parsed" if digest else "reindex", "handoff": handoff}


def _failure_text(e: BaseException, source_id: str) -> str:
    """실패 상태에 쓸 문구. 예상하지 못한 예외는 여기서 traceback을 남긴다(except 안에서 부른다)."""
    if isinstance(e, ParseError):
        return str(e)
    if isinstance(e, TransientEmbeddingError):
        # 게이트 소진은 Provider 장애가 아니다 — 운영자가 실패 로그에서 우리 쪽
        # 상한이 좁혀진 걸 서버 장애로 읽지 않도록 사유를 구분해 적는다(#196)
        reason = "임베딩 동시성 상한 소진" if e.reason == "gate" else "임베딩 서버 일시 오류"
        return f"{reason} — 재시도 한도 초과: {e}"
    logger.exception("ingestion failed", source_id=source_id)
    return f"내부 오류: {e}"


def _failure_reason(e: BaseException) -> str:
    """재인덱싱 안내 경고의 사유 코드(#230) — 오류 원문 대신 싣는다."""
    if isinstance(e, TransientEmbeddingError):
        return "embedding_gate" if e.reason == "gate" else "embedding_transient"
    if isinstance(e, ParseError):
        return "parse"
    return "error"


async def _reread_after_prep_failure(
    db: AsyncSession,
    source: Source,
    source_id: str,
    marker: uuid.UUID | None,
    e: BaseException,
    *,
    owned: str,
) -> dict[str, Any] | None:
    """잠금 전 실패 — 커밋되지 않은 변경을 버리고 교체 잠금 아래에서 행을 다시 읽는다.

    반환: 상태를 쓰지 말아야 하면 태스크 결과, 이어서 실패를 써도 되면 `None`.
    색인은 건드리지 않았다. 실패를 쓰기 전에 교체 잠금을 비차단으로 잡는다(#50) — 잠금
    아래에서 다시 읽어야 그 사이 커밋된 다른 실행의 교체를 보고, 이 기록이 진행 중인
    교체와 엇갈리지 않는다. `marker`는 이 태스크가 시작할 때 읽은 값이다. `owned`는 이 실행이
    쥔 상태다 — 다시 읽은 행이 그 상태가 아니면 다른 실행이 가져간 것이라 쓰지 않는다.
    """
    await db.rollback()
    # 롤백은 source의 속성(기본 키 포함)을 만료시킨다 — refresh 전에 읽으면 암묵
    # 로드가 AsyncSession에서 실패한다. 키는 태스크 인자로 만든다
    sid = uuid.UUID(source_id)
    swapped = False
    try:
        locked = await db.scalar(
            text("SELECT pg_try_advisory_xact_lock(:k)"),
            {"k": _source_index_lock_key(sid)},
        )
        if locked:
            await db.refresh(source)
            swapped = await _index_marker(db, sid) != marker
    except InvalidRequestError:
        # 처리 중 소스가 삭제됐다 — 남길 상태가 없다. 재인덱싱 항목은 source_id가
        # NULL이 돼 run 집계에서 제외(removed)로 센다(#179)
        logger.warning("실패 처리 중 소스가 사라짐", source_id=source_id)
        return {"ok": False, "error": str(e)}
    except Exception:
        # 행을 다시 읽지 못했다(연결 오류 등). 상태를 쓰지 않고 reaper에 맡긴다
        logger.warning("실패 처리 중 소스를 다시 읽지 못함", source_id=source_id)
        return {"ok": False, "error": str(e)}
    if not locked:
        # 다른 실행이 지금 교체 중이다 — 그 실행이 결과 상태를 쓴다
        await _quiet_rollback(db, source_id)
        logger.warning(
            "실패 처리 — 다른 실행이 교체 중이라 상태를 쓰지 않는다",
            source_id=source_id,
        )
        return {"ok": False, "error": str(e)}
    if swapped:
        # 이 실행이 시작한 뒤 다른 실행이 교체를 커밋했다 — 그 색인이 정본이다.
        # 이 실행이 쥔 진행 상태만 ready로 되돌리고, 그 밖은 그대로 둔다. 재인덱싱 항목은
        # 교체한 실행이 닫았다
        stripped = source_warnings.without_codes(source.warnings, source_warnings.REINDEX_CODES)
        if (
            await _transition(db, source, frm=owned, to="ready", error=None, warnings=stripped)
            is not None
        ):
            await db.commit()
            _publish_status(source, "ready", None)
        else:
            await db.commit()
        logger.warning(
            "잠금 밖 실패 — 다른 실행이 이미 교체해 실패를 쓰지 않는다",
            source_id=source_id,
            status=source.status,
        )
        return {"ok": False, "error": str(e)}
    return None


async def _write_failure(
    db: AsyncSession,
    source: Source,
    error: str,
    keep_previous: bool,
    *,
    owned: str,
    reason: str,
) -> bool:
    """실패를 쓰고 재인덱싱 항목을 닫는다 — 이전 색인을 쓸 수 있으면 ready로 유지한다(#7).

    이 실행이 쥔 상태(`owned`)에서만 쓴다. 0행이면 다른 실행이 행을 가져갔다 — 쓰지 않고
    재인덱싱 항목도 닫지 않는다(그 실행이 닫는다). 반환: 썼는가.

    이전 색인을 유지한 `ready`는 `error_message`를 비우고 경고 `reindex_kept_previous`(사유 코드
    `reason`)를 남긴다(#230) — `error_message`는 실패 전용이다. 어느 쪽이든 이전 재인덱싱 안내
    코드는 먼저 뺀다.
    """
    warnings = source_warnings.without_codes(source.warnings, source_warnings.REINDEX_CODES)
    message: str | None
    if keep_previous:
        # 이전 벡터는 자기 공간의 모델로 질의되므로(#7) 그 모델을 아직 쓸 수
        # 있으면 유효하다. 모델이 삭제·비활성이면 이전 색인은 검색할 수 없다.
        if await resolve_space(db, source.embedding_model) is not None:
            to, message = "ready", None
            warnings = [
                *(warnings or []),
                source_warnings.warning(source_warnings.REINDEX_KEPT_PREVIOUS, reason=reason),
            ]
        else:
            to, message = (
                "failed",
                f"재인덱싱 실패 — 이전 색인의 임베딩 모델을 쓸 수 없습니다: {error}",
            )
    else:
        to, message = "failed", error
    sid = str(source.id)  # 롤백이 행 객체를 만료시킨다 — 키는 그 전에 읽는다
    if (
        await _transition(db, source, frm=owned, to=to, error=message, warnings=warnings)
        is None
    ):
        await _quiet_rollback(db, sid)
        logger.warning("실패 기록 — 다른 실행이 행을 가져가 쓰지 않는다", source_id=sid)
        return False
    await db.commit()
    _publish_status(source, to, message)
    await _close_reindex(source.id, ok=False, error=error)
    return True


def _claim_stmt(
    source_id: uuid.UUID,
    claim_from: str,
    deferred_at: datetime | None,
    warnings: Any = _KEEP,
) -> Any:
    """색인 단계 점유 문장 — 출발 상태일 때만 `embedding`으로 바꾼다(한 실행만 성공한다).

    - `pending`(미룬 신규 소스)은 미룰 때의 `updated_at`(`deferred_at`)까지 같아야 한다 — reaper가
      되돌린 행(`updated_at` 갱신)이나 사용자 재시도의 `pending`을 낡은 재시도가 가로채지 않는다.
    - `ready`·`failed`(재인덱싱)는 열린 재인덱싱 항목이 **같은 문장 안에서** 있어야 한다 — 그 사이
      다른 실행이 처리를 끝냈으면 항목이 닫혔다.
    - `warnings`를 주면 함께 쓴다 — 재시도가 점유하면 "N초 뒤 다시 시도" 안내를 뺀다(#230).
    """
    where = [Source.id == source_id, Source.status == claim_from]
    if claim_from == "pending":
        where.append(Source.updated_at == deferred_at)
    if claim_from in _REINDEX_CLAIM_STATUSES:
        where.append(
            select(ReindexRunItem.id)
            .where(ReindexRunItem.source_id == Source.id, ReindexRunItem.status == "queued")
            .exists()
        )
    values: dict[str, Any] = {
        "status": "embedding",
        "error_message": None,
        "updated_at": func.now(),
    }
    if warnings is not _KEEP:
        values["warnings"] = warnings
    return (
        update(Source)
        .where(*where)
        .values(**values)
        .returning(Source.id)
        .execution_options(synchronize_session=False)
    )


async def _claim_embedding(
    db: AsyncSession, source: Source, claim_from: str, deferred_at: datetime | None
) -> bool:
    """색인 단계의 첫 쓰기 — 행을 점유하고 커밋한다. 점유 전에는 아무것도 쓰지 않는다(리뷰 M2).

    중복 메시지(재배달·reaper 재발행·미룬 재시도)가 겹쳐도 하나만 성공한다. 0행이면 다른
    실행이 가져갔거나 행이 종결됐다 — 아무것도 쓰지 않고 물러난다. 점유한 동안에는 파싱
    단계가 행을 가져가지 못하므로(`_PARSE_FROM_STATUSES`) 점유 뒤 읽은 parsed.md가 이 세대다.
    """
    if claim_from == "pending" and deferred_at is None:
        return False
    # 점유 전에 읽은 행에서 만든 값이다. 읽기와 점유 사이에 재파싱이 끝나 행이 다시 출발
    # 상태가 되면 그 파싱 경고를 덮을 수 있다(몇 ms의 창, PR-B 리뷰 B-n1).
    # 다음 전체 파싱이 바로잡는다
    warnings: Any = _KEEP
    if source_warnings.has_code(source.warnings, source_warnings.REINDEX_DEFERRED):
        warnings = source_warnings.without_codes(
            source.warnings, frozenset({source_warnings.REINDEX_DEFERRED})
        )
    with db.no_autoflush:
        stmt = _claim_stmt(source.id, claim_from, deferred_at, warnings)
        row = (await db.execute(stmt)).first()
    if row is None:
        await _quiet_rollback(db, str(source.id))
        return False
    # 문장이 이미 썼다 — 메모리 값만 맞춘다. 더티로 남기면 다음 flush가 같은 값을 다시 쓴다
    set_committed_value(source, "status", "embedding")
    set_committed_value(source, "error_message", None)
    if warnings is not _KEEP:
        set_committed_value(source, "warnings", warnings)
    await db.commit()
    _publish_status(source, "embedding", None)
    return True


def _drop_stale_deferral_stmt(
    source_id: uuid.UUID, seen_at: datetime | None, warnings: list[dict[str, Any]] | None
) -> Any:
    """물러나는 미룬 재인덱싱 재시도가 남긴 "N초 뒤 다시 시도" 안내를 지운다(#230, 재리뷰 R-m3).

    이 실행은 행을 점유하지 않았다 — 그래서 **같은 문장 안에서** 확인한다. 행이 `ready`이고, 읽은
    뒤 바뀌지 않았고(`updated_at`), 열린 재인덱싱 항목이 없을 때만이다. 열린 항목이 있으면 더 새
    run이 그 안내를 쓴 것일 수 있다.
    """
    return (
        update(Source)
        .where(
            Source.id == source_id,
            Source.status == "ready",
            Source.updated_at == seen_at,
            ~select(ReindexRunItem.id)
            .where(ReindexRunItem.source_id == Source.id, ReindexRunItem.status == "queued")
            .exists(),
        )
        .values(warnings=warnings)
        .returning(Source.id)
        .execution_options(synchronize_session=False)
    )


async def _drop_stale_deferral(
    source_id: uuid.UUID, seen_at: datetime | None, warnings: list[dict[str, Any]] | None
) -> None:
    """별도 세션에서 지우고 곧바로 커밋한다 — 실패해도 물러나는 결과를 바꾸지 않는다."""
    try:
        async with get_session_factory()() as db:
            await db.execute(_drop_stale_deferral_stmt(source_id, seen_at, warnings))
            await db.commit()
    except Exception:
        logger.warning("미룸 안내 정리 실패", source_id=str(source_id), exc_info=True)


class _ClaimLost(Exception):
    """점유한 `embedding`을 그 사이 다른 실행이 가져갔다 — 상태를 쓰지 않고 물러난다."""


_MISSING_OBJECT_CODES = frozenset({"NoSuchKey", "NoSuchBucket"})


async def _read_capped(bucket: str, key: str, cap: int) -> bytes | None:
    """오브젝트를 상한 안에서 읽는다. 없으면 `None`, 상한을 넘으면 `InvalidStageArtifact`.

    색인 단계는 비신뢰 파서(HWP 전용 워커)가 쓴 산출물을 읽는다 — 크기를 먼저 보고 받는다.
    stat과 get 사이에 바뀐 크기는 받은 뒤 다시 본다(메모리 상한은 stat 기준이다).
    """
    try:
        size = await anyio.to_thread.run_sync(stat_object_size, bucket, key)
    except Exception as e:
        if getattr(e, "code", None) in _MISSING_OBJECT_CODES:
            return None
        raise
    if size > cap:
        raise stage_artifacts.InvalidStageArtifact(f"{key} 크기가 상한을 넘는다: {size}")
    data = await anyio.to_thread.run_sync(get_object, bucket, key)
    if len(data) > cap:
        raise stage_artifacts.InvalidStageArtifact(f"{key} 크기가 상한을 넘는다: {len(data)}")
    return data


async def _load_parsed(source: Source) -> tuple[str, str]:
    """저장된 parsed.md와 그 세대 해시. 없거나 너무 크면 `ParseError`."""
    if not source.parsed_object_key:
        raise ParseError("파싱 결과가 없습니다 — 원본부터 다시 처리해야 합니다")
    try:
        data = await _read_capped(
            get_settings().storage_bucket_parsed,
            source.parsed_object_key,
            stage_artifacts.PARSED_MAX_BYTES,
        )
    except stage_artifacts.InvalidStageArtifact as e:
        raise ParseError("파싱 결과가 너무 커서 처리할 수 없습니다") from e
    if data is None:
        raise ParseError("파싱 결과를 찾을 수 없습니다 — 원본부터 다시 처리해야 합니다")
    # #462 이전 세대의 parsed.md에는 U+0000이 남아 있을 수 있다. 세대 해시는 저장된 바이트로
    # 매기고, 같은 길이의 공백으로 바꿔 그 세대의 위치 오프셋이 그대로 맞는다
    return replace_nul(data.decode("utf-8")), stage_artifacts.parsed_digest(data)


async def _load_locations(source_id: uuid.UUID, markdown: str, digest: str) -> list[Location]:
    """이 parsed.md 세대의 위치. 없거나 다른 세대이거나 형식이 틀리면 빈 목록이다.

    위치는 청크 메타일 뿐이다 — 형식 위반도, 읽기 오류(스토리지 일시 장애)도 인제스트를 실패시키지
    않고 로그만 남긴다(계획 §3.4, 재리뷰 R-n3). 위치는 다음 재인덱싱이 다시 읽는다.
    """
    key = stage_artifacts.locations_key(source_id)
    try:
        payload = await _read_capped(
            get_settings().storage_bucket_parsed, key, stage_artifacts.LOCATIONS_MAX_BYTES
        )
        if payload is None:
            return []
        locations = stage_artifacts.decode_locations(
            payload, markdown=markdown, parsed_sha256=digest
        )
    except stage_artifacts.InvalidStageArtifact as e:
        logger.error("위치 산출물이 형식을 벗어남 — 위치 없이 청킹한다", reason=str(e))
        return []
    except Exception:
        logger.error("위치 산출물을 읽지 못함 — 위치 없이 청킹한다", exc_info=True)
        return []
    if not locations:
        logger.info("위치 산출물이 이 parsed.md 세대가 아니다 — 위치 없이 청킹한다")
    return locations


async def _run_index(
    source_id: str,
    parsed_sha256: str | None = None,
    keep_previous: bool = False,
    claim_from: str = "chunking",
    deferred_at: datetime | str | None = None,
    transient_attempt: int = 0,
    gate_attempt: int = 0,
) -> dict[str, Any]:
    """색인 단계 — 저장된 parsed.md(+ locations.json)를 청킹·임베딩·색인해 `ready`로 만든다(#244).

    파서를 부르지 않는다. HWP 핸드오프·미룬 재시도·reaper 재개는 일반 워커의 `index_source`가,
    일반 워커가 파싱한 소스는 같은 태스크(`ingest_source`)가 곧바로 부른다(D6).

    - **점유가 첫 쓰기다**(`_claim_embedding`). 그 전에는 읽기만 하고, 실패하면 아무것도 쓰지
      않는다. 점유한 뒤의 쓰기(미룸·실패·`ready`)는 모두 `embedding`에서만 한다.
    - `claim_from`: 점유할 출발 상태. 신규 소스는 `chunking`, 재인덱싱은 `ready`·`failed`(열린
      항목 필요), 미룬 신규 소스는 `pending`(+ `deferred_at` 토큰)이다.
    - `parsed_sha256`: 파싱 단계가 만든 세대. 점유한 뒤 읽은 parsed.md가 이 세대다 — 메시지 해시가
      다르면 더 새 파싱이 넘긴 행을 이 실행이 가져간 것이라 지금 본문을 색인한다(진단 로그만).
    - `keep_previous`: 재인덱싱 중인 ready 소스 — 실패하면 이전 색인을 유지한다(#7).

    성공하면 벡터 출처(임베딩 공간·청킹 설정)를 소스에 기록한다(#7).

    `transient_attempt`와 `gate_attempt`는 **사유별 재시도 차수**다(#194·#196). 임베딩이 일시
    오류로 끝나면 **해당 사유의 상한** 전까지 실패를 쓰지 않고 상태를 되돌린 뒤
    `retry_in`·`retry_kwargs`를 돌려준다. 두 카운터는 서로를 벌리지 않는다. 재시도는 색인 단계만
    다시 낸다 — 파싱을 되풀이하지 않고 위치도 locations.json에서 다시 읽는다.
    """
    attempt = transient_attempt or gate_attempt
    sid = uuid.UUID(source_id)
    token = _deferred_at_of(deferred_at)
    async with get_session_factory()() as db:
        # 행보다 먼저 읽는다 — 그 사이 커밋된 교체도 "시작한 뒤의 교체"로 잡힌다(#50)
        marker = await _index_marker(db, sid)
        source = await db.get(Source, sid)
        if source is None:
            return {"ok": False, "error": "source not found"}
        # 물러날 때 미룸 안내를 지우려면 점유 전 값을 둔다 — 롤백이 행 객체를 만료시킨다
        seen_at = source.updated_at
        stale_deferral = source.status == "ready" and source_warnings.has_code(
            source.warnings, source_warnings.REINDEX_DEFERRED
        )
        kept_warnings = (
            source_warnings.without_codes(
                source.warnings, frozenset({source_warnings.REINDEX_DEFERRED})
            )
            if stale_deferral
            else None
        )
        if source.status != claim_from:
            # 다른 실행(중복 메시지·reaper·사용자 재시도)이 행을 가져갔거나 이미 끝났다
            logger.warning(
                "색인 단계 — 출발 상태가 아니라 물러난다",
                source_id=source_id,
                status=source.status,
                claim_from=claim_from,
            )
            await _quiet_rollback(db, source_id)
            return {"ok": False, "error": "claim_lost"}
        try:
            # 점유 전 읽기 — DB만 읽는다. 실패하면 아무것도 쓰지 않는다
            from biznotes_core.services.settings_store import get_setting

            rag_cfg = await get_setting(db, "rag")
            # 활성 임베딩 공간(어드민 기본 임베딩 모델, 없으면 로컬 기본값)
            space = await active_space(db)
            # 점유 커밋이 읽기 트랜잭션도 끝낸다 — 임베딩(동기, 오래 걸린다) 동안 붙잡지 않는다.
            # 색인 교체 잠금은 아래에서 새 트랜잭션으로 잡는다(#27)
            claimed = await _claim_embedding(db, source, claim_from, token)
        except Exception as e:
            await _quiet_rollback(db, source_id)
            logger.warning("색인 단계 — 점유 전 실패라 쓰지 않는다", exc_info=True)
            return {"ok": False, "error": str(e)}
        if not claimed:
            logger.warning(
                "색인 단계 점유 실패 — 다른 실행이 행을 가져가 물러난다",
                source_id=source_id,
                claim_from=claim_from,
            )
            if stale_deferral and claim_from == "ready":
                # 항목이 닫혀 물러나는 미룬 재인덱싱 재시도 — 그 안내가 영구히 남지 않게 한다
                await _drop_stale_deferral(sid, seen_at, kept_warnings)
            return {"ok": False, "error": "claim_lost"}

        # 교체 구간의 어디까지 왔는지 — 실패 처리가 이 값으로 갈린다(#27, 계획의 예외 규칙 표)
        #   prep: 잠금 전. 색인을 건드리지 않았다
        #   locked: 잠금 뒤 트랜잭션이 깨졌다 — 롤백이 잠금을 놓는다
        #   recovered: 잠금을 쥔 채 트랜잭션이 살아 있다(savepoint 되돌림)
        #   committing: 커밋 결과를 모른다
        #   done: 커밋했다 — 뒤따르는 부수 작업(이벤트 발행 등)의 실패는 색인과 무관하다
        stage = "prep"
        digest = parsed_sha256
        started = time.monotonic()
        try:
            markdown, digest = await _load_parsed(source)
            if parsed_sha256 is not None and digest != parsed_sha256:
                logger.info(
                    "색인 단계 — 더 새 세대를 색인한다",
                    source_id=source_id,
                    message_sha256=parsed_sha256[:12],
                    parsed_sha256=digest[:12],
                )
            # 위치가 없는 포맷(HWP 외 전부)은 None — 청크가 이전과 바이트 동일하다
            locations = await _load_locations(source.id, markdown, digest) or None

            # 1. chunk — 어드민 RAG 설정 반영
            chunks: list[Chunk] = chunk_markdown(
                markdown,
                max_tokens=int(rag_cfg.get("chunk_size", 800)),
                overlap=int(rag_cfg.get("chunk_overlap", 100)),
                locations=locations,
            )
            if not chunks:
                raise ParseError("청크를 생성하지 못했습니다 (빈 문서)")
            chunked = time.monotonic()

            # 2. embed
            texts = [c.text for c in chunks]
            if space.is_local:
                dense = embed_dense(texts)
            else:
                dense = await _embed_gateway(space, texts, source.id, rag_cfg)
            sparse = embed_sparse(texts)
            embedded = time.monotonic()

            # 3. index — 같은 소스의 교체를 소스 단위 잠금으로 직렬화한다(#27). 벡터는 청크 행에
            #    있으므로(pgvector) 옛 행 삭제·새 행(벡터 포함) 삽입·출처 컬럼·상태 전이가 한
            #    savepoint 안의 한 트랜잭션이다 — 실패하면 savepoint를 되돌려 이전 색인이 그대로
            #    남고, 커밋하면 옛 벡터가 행과 함께 사라진다. 고아 벡터가 생기지 않는다.
            from sqlalchemy import delete as sa_delete

            stage = "locked"
            await _lock_source_index(db, source.id)
            try:
                async with db.begin_nested():
                    await db.execute(
                        sa_delete(SourceChunk).where(SourceChunk.source_id == source.id)
                    )
                    for c, dv, sv in zip(chunks, dense, sparse, strict=True):
                        db.add(
                            SourceChunk(
                                source_id=source.id,
                                seq=c.seq,
                                text=c.text,
                                meta=c.meta,
                                embedding=dv,
                                sparse=sv,
                            )
                        )
                    # 출처 컬럼도 savepoint 안에서 대입한다 — 밖에서 대입하면 실패 커밋에
                    # 새 출처가 실려 옛 벡터를 새 공간으로 검색하게 된다
                    source.token_count = count_tokens(markdown)
                    source.chunk_count = len(chunks)
                    source.embedding_model = space.key
                    source.chunk_config = chunk_config_of(rag_cfg)
                    await db.flush()
                    # ready는 이 실행이 쥔 embedding에서만 쓴다 — 그 사이 reaper가 되돌린 행을
                    # 덮지 않는다(리뷰 B1). 0행이면 savepoint를 되돌린다.
                    # 재인덱싱 안내(#230)는 새 색인이 커밋되면 의미가 없다 — 함께 뺀다
                    if (
                        await _transition(
                            db,
                            source,
                            frm="embedding",
                            to="ready",
                            error=None,
                            warnings=source_warnings.without_codes(
                                source.warnings, source_warnings.REINDEX_CODES
                            ),
                        )
                        is None
                    ):
                        raise _ClaimLost(source_id)
            except Exception as swap_error:
                try:
                    await db.refresh(source)  # savepoint 되돌림이 만료시킨 속성을 다시 읽는다
                except Exception:
                    # 트랜잭션을 되살리지 못했다(연결 상실 등) — stage는 locked로 남는다.
                    # 상태를 쓰지 않는 이유가 이 오류이므로 원인을 남긴다
                    logger.warning(
                        "교체 실패 뒤 행을 다시 읽지 못함", source_id=source_id, exc_info=True
                    )
                    raise swap_error from None
                stage = "recovered"
                raise

            stage = "committing"
            await db.commit()
            stage = "done"
            # 재인덱싱 기록을 먼저 닫는다(실패를 삼킨다). 이벤트 발행·개요 예약은 그 뒤에,
            # 실패해도 이것을 건너뛰지 않도록 따로 감싼다
            await _close_reindex(source.id, ok=True, error=None)
            logger.info(
                "ingestion done",
                source_id=source_id,
                chunks=len(chunks),
                chunk_ms=int((chunked - started) * 1000),
                embed_ms=int((embedded - chunked) * 1000),
                index_ms=int((time.monotonic() - embedded) * 1000),
            )
            try:
                _publish_status(source, "ready", None)
            except Exception:
                logger.warning("ready 이벤트 발행 실패", source_id=source_id, exc_info=True)

            # 노트북 개요(요약+추천 질문) 재생성 예약 — 디바운스로 마지막 1회만 실행
            from biznotes_core.workers.tasks.overview import schedule_overview_refresh

            try:
                schedule_overview_refresh(str(source.notebook_id))
            except Exception:
                logger.warning("노트북 개요 재생성 예약 실패", source_id=source_id, exc_info=True)
            # 소스 요약 미리 만들기(#378) — 켰을 때만, 디바운스로 마지막 예약 1회만 만든다
            if get_settings().source_digest_on_ingest:
                from biznotes_core.services.source_digest_refresh import schedule_digest_refresh

                try:
                    schedule_digest_refresh(source_id)
                except Exception:
                    logger.warning("소스 요약 예약 실패", source_id=source_id, exc_info=True)
            return {"ok": True, "chunks": len(chunks)}

        except _IndexLockBusy:
            # 같은 소스를 다른 실행이 교체 중이다. 상태·재인덱싱 항목을 쓰지 않는다 —
            # 앞선 실행이 끝내거나, 좀비라면 reaper가 회수한다
            await _quiet_rollback(db, source_id)
            logger.warning("색인 교체 잠금 대기 초과 — 물러난다", source_id=source_id)
            return {"ok": False, "error": "index_lock_busy"}
        except _SourceGone:
            await _quiet_rollback(db, source_id)
            logger.warning("색인 교체 전에 소스가 삭제됨", source_id=source_id)
            return {"ok": False, "error": "source not found"}
        except _EmbeddingSuperseded:
            # 임베딩 중 reaper 회수가 행을 가져갔다 — 그 실행이 결과를 쓴다.
            # 상태·재인덱싱 항목을 쓰지 않고 재시도하지도 않는다
            await _quiet_rollback(db, source_id)
            logger.warning("임베딩 중 다른 실행이 행을 가져감 — 물러난다", source_id=source_id)
            return {"ok": False, "error": "embedding_superseded"}
        except _ClaimLost:
            # savepoint는 되돌렸다. 남은 트랜잭션(잠금)만 놓는다
            await _quiet_rollback(db, source_id)
            logger.warning("교체 직전 다른 실행이 행을 가져감 — 물러난다", source_id=source_id)
            return {"ok": False, "error": "claim_lost"}
        except Exception as e:
            if stage == "done":
                # 안전망 — ready는 이미 커밋됐다. 커밋 뒤 부수 작업은 각자 실패를 삼키지만,
                # 여기까지 오더라도 실패 상태를 쓰면 멀쩡한 색인을 failed로 덮는다(예전에는
                # 색인까지 지웠다)
                logger.exception("색인 교체 뒤 부수 작업 실패", source_id=source_id)
                return {"ok": True, "chunks": source.chunk_count}
            if isinstance(e, TransientEmbeddingError):
                logger.warning(
                    "임베딩 일시 오류", source_id=source_id, attempt=attempt, error=str(e)
                )
            error = _failure_text(e, source_id)
            if stage in ("locked", "committing"):
                # 잠금 뒤 트랜잭션이 깨졌거나 커밋 결과를 모른다 — 아래 롤백이 잠금을 놓는다
                # (연결이 끊겼다면 이미 풀렸다). 상태를 쓰면 그 사이 교체한 다른 실행을 덮을 수
                # 있다. 아무것도 쓰지 않고 reaper에 맡긴다.
                await _quiet_rollback(db, source_id)
                logger.warning(
                    "색인 교체 결과 미상 — 상태를 쓰지 않는다", source_id=source_id, stage=stage
                )
                return {"ok": False, "error": str(e)}
            if stage == "prep":
                # recovered는 잠금을 쥔 채 이미 되돌리고 다시 읽었다
                withdrawn = await _reread_after_prep_failure(
                    db, source, source_id, marker, e, owned="embedding"
                )
                if withdrawn is not None:
                    return withdrawn
            if stage == "prep" and isinstance(e, TransientEmbeddingError):
                # 사유별로 다른 카운터와 상한을 쓴다(#196). 게이트 소진은 Provider 장애가 아니라
                # 우리 쪽 정체이므로 네트워크 재시도 예산을 태우지 않는다
                if e.reason == "gate":
                    counter, cap = gate_attempt, INGEST_GATE_DEFERRALS
                else:
                    counter, cap = transient_attempt, INGEST_TRANSIENT_RETRIES
                if counter < cap:
                    if source.status != "embedding":
                        # 잠금 아래에서 다시 읽은 행이 이 실행의 embedding이 아니다 — 다른 쪽
                        # (reaper 회수)이 가져갔다. 그쪽이 처리하므로 미루지 않는다. 롤백이 행
                        # 객체를 만료시키므로 상태는 그 전에 읽는다(재리뷰 R2-m1)
                        seen = source.status
                        await _quiet_rollback(db, source_id)
                        logger.warning(
                            "일시 오류 — 다른 실행이 행을 가져가 미루지 않는다",
                            source_id=source_id,
                            status=seen,
                        )
                        return {"ok": False, "error": str(e)}
                    # 잠금 아래에서 다시 읽었고 다른 실행의 교체도 없었다 — 실패 대신 미룬다.
                    # 재시도는 색인 단계만 다시 낸다. 출발 상태는 미룬 상태이고, `pending`이면
                    # 미룰 때의 updated_at을 토큰으로 싣는다
                    deferred = await _defer_transient(db, source, e, counter, cap, keep_previous)
                    if deferred is None:
                        logger.warning(
                            "일시 오류 — 미루기 전에 행을 잃어 쓰지 않는다", source_id=source_id
                        )
                        return {"ok": False, "error": str(e)}
                    countdown, deferred_status, deferred_ts = deferred
                    # 두 카운터를 **항상 함께** 실어 나른다. 이 사유의 것만 올리고 다른 쪽을
                    # 비우면 그쪽이 0으로 리셋되어(리뷰 M1) 상한이 다시 풀린다. 사유가 섞인
                    # 체인에서는 어느 카운터도 자기 상한에 닿지 못한 채 Celery의
                    # max_retries만 먼저 소진되고, 실패한 태스크가 써둔 `pending`을 reaper가
                    # 카운터 0으로 15분마다 되살린다.
                    retry_kwargs: dict[str, Any] = {
                        "parsed_sha256": digest,
                        "keep_previous": keep_previous,
                        "claim_from": deferred_status,
                        "deferred_at": (
                            deferred_ts.isoformat() if deferred_status == "pending" else None
                        ),
                        "transient_attempt": transient_attempt,
                        "gate_attempt": gate_attempt,
                    }
                    retry_kwargs["transient_attempt" if e.reason != "gate" else "gate_attempt"] = (
                        counter + 1
                    )
                    return {
                        "ok": False,
                        "error": str(e),
                        "retry_in": countdown,
                        "retry_kwargs": retry_kwargs,
                    }
            if stage == "recovered" and source.status == "ready":
                # 잠금을 쥔 채 다시 읽은 행이 ready다 — 이 실행은 잠금 전에 embedding을
                # 커밋했으므로, 그 뒤 다른 실행이 교체를 커밋했다는 뜻이다. 그 색인이 현재
                # 정본이므로 실패로 덮지 않는다(재인덱싱 항목은 그 실행이 닫았다)
                await db.commit()
                logger.warning(
                    "교체 실패 — 다른 실행이 이미 교체해 상태를 유지한다", source_id=source_id
                )
                return {"ok": False, "error": str(e)}
            await _write_failure(
                db, source, error, keep_previous, owned="embedding", reason=_failure_reason(e)
            )
            return {"ok": False, "error": str(e)}


async def _quiet_rollback(db: AsyncSession, source_id: str) -> None:
    """실패 경로의 롤백 — 연결이 이미 끊겼으면 롤백도 실패한다. 원래 실패를 가리지 않는다."""
    try:
        await db.rollback()
    except Exception:
        logger.warning("롤백 실패", source_id=source_id)


def _delivery_queue(task: Any) -> str | None:
    return (task.request.delivery_info or {}).get("routing_key")


def _deferred_at_of(raw: Any) -> datetime | None:
    """미룸 토큰 — 시간대가 있는 ISO 시각(문자열) 또는 datetime만 받는다."""
    if isinstance(raw, str):
        try:
            raw = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if isinstance(raw, datetime) and raw.tzinfo is not None:
        return raw
    return None


def _index_kwargs_of(message: dict[str, Any]) -> dict[str, Any]:
    """색인 단계 kwargs를 정규화한다 — 핸드오프는 전용 워커(비신뢰 파서가 도는 곳)가 낸다.

    해시는 소문자 hex 64자, `claim_from`은 `CLAIM_FROM_STATUSES`, `deferred_at`은 시간대가 있는
    ISO 시각, 차수는 0 이상 정수만 받는다. 어긋난 값은 기본값으로 둔다 — 진행 상태에서
    점유하면 살아 있는 실행을 덮으므로 `claim_from`은 `chunking`으로 본다.
    """
    digest = message.get("parsed_sha256")
    if digest is not None and not stage_artifacts.is_digest(digest):
        logger.warning("색인 단계 — 세대 해시 형식이 틀려 버린다")
        digest = None
    claim_from = message.get("claim_from", "chunking")
    if claim_from not in CLAIM_FROM_STATUSES:
        logger.warning("색인 단계 — 점유 출발 상태가 틀려 chunking으로 본다")
        claim_from = "chunking"
    deferred_at = _deferred_at_of(message.get("deferred_at"))
    return {
        "parsed_sha256": digest,
        "keep_previous": message.get("keep_previous") is True,
        "claim_from": claim_from,
        "deferred_at": deferred_at,
        "transient_attempt": source_warnings.nat(message.get("transient_attempt")),
        "gate_attempt": source_warnings.nat(message.get("gate_attempt")),
    }


@celery_app.task(name="app.workers.tasks.ingestion.ingest_source", bind=True, max_retries=0)
def ingest_source(
    self,
    source_id: str,
    reindex: bool | None = None,
    refetch: bool | None = None,
    transient_attempt: int = 0,
    gate_attempt: int = 0,
) -> dict[str, Any]:
    """소스 인제스트 진입점 — 파싱하고 색인 단계로 넘긴다(#244).

    발행 메시지는 예전과 같이 `source_id` 하나다. 재인덱싱 여부는 워커가 DB에서 판단한다
    (`_run` 참조). 두 kwarg는 운영·테스트에서 모드를 강제할 때만 쓴다 — 큐에 남은 구 메시지와
    신 메시지가 모두 동작한다.

    `transient_attempt`·`gate_attempt`도 사유별 재시도 차수다(#196). **기본값이 필수다** —
    구 버전이 `self.retry(kwargs=...)`로 미룬 메시지가 큐에 남아 있다. 이제 이 태스크는 미루지
    않는다 — 그 차수를 색인 단계로 넘길 뿐이다.

    색인 단계는 **같은 태스크에서 곧바로** 한다(D6). 미루면 `index_source`를 countdown으로 낸다.
    `run_async` 밖에서 한다 — eager 실행에서도 루프에 재진입하지 않는다.
    """
    ensure_buckets()
    # 계획 B §5 공통 필드 — 파서 스레드의 이벤트도 contextvars 사본으로 같은 값을 싣는다
    queue = _delivery_queue(self)
    # 첫 시도에는 차수를 넘기지 않는다 — `_run`을 (source_id, reindex, refetch) 대역으로 바꾸는
    # 테스트가 그대로 동작한다
    extra: dict[str, Any] = {}
    if transient_attempt:
        extra["transient_attempt"] = transient_attempt
    if gate_attempt:
        extra["gate_attempt"] = gate_attempt
    with structlog.contextvars.bound_contextvars(source_id=source_id, queue=queue, stage="parse"):
        result = run_async(_run(source_id, reindex=reindex, refetch=refetch, **extra))
    handoff = result.pop("handoff", None) if isinstance(result, dict) else None
    if handoff is None:
        return result
    with structlog.contextvars.bound_contextvars(source_id=source_id, queue=queue, stage="index"):
        indexed = run_async(_run_index(source_id, **_index_kwargs_of(handoff)))
        if "retry_in" in indexed:
            try:
                index_source.apply_async(
                    args=[source_id],
                    kwargs=indexed.pop("retry_kwargs"),
                    countdown=indexed["retry_in"],
                )
            except Exception:
                # 되돌린 행(`pending`·재인덱싱 `ready`)은 reaper·재인덱싱 회수가 가져간다
                logger.warning("미룬 색인 단계 발행 실패 — reaper가 회수한다", exc_info=True)
    return indexed


@celery_app.task(name="app.workers.tasks.ingestion.index_source", bind=True, max_retries=0)
def index_source(
    self,
    source_id: str,
    parsed_sha256: str | None = None,
    keep_previous: bool = False,
    claim_from: str = "chunking",
    deferred_at: str | None = None,
    transient_attempt: int = 0,
    gate_attempt: int = 0,
) -> dict[str, Any]:
    """색인 단계 — 청킹·임베딩·색인(#244). `ingestion` 큐의 일반 워커만 소비한다.

    HWP 핸드오프, 미룬 재시도(#194·#196), reaper 재개가 이 태스크로 온다. 인자는 모두 기본값이
    있는 kwargs다 — 이후 인자를 더해도 큐에 남은 메시지가 깨지지 않는다(#196). 값은
    `_index_kwargs_of`로 정규화한다(전용 워커가 낸 메시지다).
    """
    ensure_buckets()
    kwargs = _index_kwargs_of(
        {
            "parsed_sha256": parsed_sha256,
            "keep_previous": keep_previous,
            "claim_from": claim_from,
            "deferred_at": deferred_at,
            "transient_attempt": transient_attempt,
            "gate_attempt": gate_attempt,
        }
    )
    queue = _delivery_queue(self)
    with structlog.contextvars.bound_contextvars(source_id=source_id, queue=queue, stage="index"):
        result = run_async(_run_index(source_id, **kwargs))
    if "retry_in" in result:
        # 임베딩 일시 오류(#194) — args(`[source_id]`)는 원 메시지 그대로, 세대·출발 상태·차수만
        # kwargs로 싣는다. 발행이 실패하면 Celery가 원 메시지를 버리고, 되돌린 상태는 reaper가
        # 회수한다. `max_retries`는 **총 시도의 외곽 천장**이다 — 한 실행은 사유를 정확히 하나
        # 만들므로 최대 미룸은 INGEST_TRANSIENT_RETRIES + INGEST_GATE_DEFERRALS이고, 이 값을
        # 낮추면 마지막 미룸에서 MaxRetriesExceededError가 떠 `pending`이 남는다
        raise self.retry(
            countdown=result["retry_in"],
            max_retries=INGEST_TRANSIENT_RETRIES + INGEST_GATE_DEFERRALS,
            kwargs=result["retry_kwargs"],
        )
    return result


# non-terminal 상태로 이 시간 이상 멈춰 있으면 정체로 간주하고 재발행.
# 정상 처리 중인 소스는 단계마다 status 갱신(updated_at 갱신)되므로 대상이 아니다.
# 가장 느린 단계(대용량 임베딩)보다 충분히 길게 잡아 중복 처리를 피한다.
STALLED_THRESHOLD_MINUTES = 15
# 파싱이 끝난 뒤의 in-flight 상태 — reaper가 색인 단계부터 재개한다(#244)
_INDEX_STAGE_STATUSES = ("chunking", "embedding")


async def _reap_stalled() -> dict[str, Any]:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=STALLED_THRESHOLD_MINUTES)
    # 대기열이 흐르는 동안 미룬 재인덱싱 회수의 절대 상한(#43) — 이보다 오래 멈춘 run은
    # 흐름과 무관하게 재발행한다. 소스 reaper는 자기 cutoff만 쓴다(소스 상태가 곧 신호다).
    hard_cutoff = now - timedelta(minutes=STALLED_THRESHOLD_MINUTES * HARD_STALLED_MULTIPLIER)
    factory = get_session_factory()
    requeued: list[str] = []
    async with factory() as db:
        # 계획 모드 고정(#44 D4) — `stalled_reindex_items`도 자기 첫 문장으로 다시 낸다(루프의
        # 커밋 뒤에는 새 트랜잭션이다)
        await db.execute(text(FORCE_CUSTOM_PLAN))
        rows = await db.execute(
            select(
                Source.id,
                Source.status,
                Source.parsed_object_key,
            ).where(Source.status.in_(IN_FLIGHT_STATUSES), Source.updated_at < cutoff)
        )
        stalled = list(rows.tuples().all())
        for sid, status, parsed_key in stalled:
            # 멈춘 단계부터 재개한다(#244) — 파싱이 끝난 소스(`chunking`·`embedding`)는 다시
            # 파싱하지 않고 색인 단계로 보낸다. `pending`·`parsing`은 처음부터다
            resume_index = status in _INDEX_STAGE_STATUSES and parsed_key is not None
            # 조건부 전이 — 조회 뒤 바뀐 행은 덮지 않는다. `updated_at`을 명시해 같은 값
            # 대입(이미 pending)에서도 갱신된다(다음 스윕이 곧바로 다시 내지 않는다)
            moved = (
                await db.execute(
                    update(Source)
                    .where(Source.id == sid, Source.status == status, Source.updated_at < cutoff)
                    .values(
                        status="chunking" if resume_index else "pending",
                        error_message=None,
                        updated_at=func.now(),
                    )
                    .returning(Source.id)
                    .execution_options(synchronize_session=False)
                )
            ).first()
            await db.commit()
            if moved is None:
                continue
            if resume_index:
                # 색인 단계는 큐 인자 없이 낸다 — `task_routes`가 `ingestion`으로 보낸다
                index_source.apply_async(args=[str(sid)])
            else:
                ingest_source.apply_async(args=[str(sid)])
            requeued.append(str(sid))

        # 재인덱싱 대기 항목은 소스가 ready/failed 그대로라 위 조건에 걸리지 않는다.
        # run 전체가 멈췄을 때만(대기열이 흐르지 않을 때) 남은 항목을 재발행한다.
        # 이 부분이 실패해도(예: 마이그레이션 전이라 테이블이 없음) 위의 정체 소스
        # 회수는 이미 끝났다 — 안전망 전체가 같이 죽지 않도록 여기서 막는다.
        reindex_requeued: list[str] = []
        try:
            reindex_items = await stalled_reindex_items(db, cutoff, hard_cutoff)
            await db.commit()
            # 그 사이 지워진 소스는 행이 없어 빠진다
            item_ids = [item.source_id for item, _ in reindex_items]
            routes = await db.execute(select(Source.id).where(Source.id.in_(item_ids)))
            for (sid,) in routes.tuples().all():
                ingest_source.apply_async(args=[str(sid)])
                reindex_requeued.append(str(sid))
        except Exception:
            logger.exception("정체된 재인덱싱 항목 회수 실패")
            await db.rollback()
    if requeued:
        logger.warning("정체된 소스 재발행", count=len(requeued), source_ids=requeued)
    if reindex_requeued:
        logger.warning(
            "정체된 재인덱싱 항목 재발행", count=len(reindex_requeued), source_ids=reindex_requeued
        )
    return {"requeued": len(requeued), "reindex_requeued": len(reindex_requeued)}


@celery_app.task(name="app.workers.tasks.ingestion.reap_stalled_sources")
def reap_stalled_sources() -> dict[str, Any]:
    """정체된 소스 인제스천을 재발행하는 주기 안전망 (beat가 5분마다 호출)."""
    return run_async(_reap_stalled())
