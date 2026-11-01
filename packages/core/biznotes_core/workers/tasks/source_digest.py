"""소스 요약(digest) 미리 만들기 태스크(#378) — 인제스트가 끝난 소스의 요약을 캐시에 넣는다.

예약은 `biznotes_core.services.source_digest_refresh`다(Redis 토큰 디바운스). 이 태스크는 토큰이
최신일 때만 만든다. 캐시 규칙은 `source_digest` 노드와 같다(`services.source_digest`) — 지문(청크
텍스트 sha256)과 프롬프트 버전이 같은 행이 있으면 만들지 않는다. 모델은 기본 llm 모델 + 폴백
체인이고(#360 D3), 사용량은 노트북 소유자에게 귀속한다(`feature="source_digest"`, #366).

만드는 동안 소스가 다시 인제스트돼 청크가 바뀌면 저장하지 않는다 — 그 재인제스트가 예약한 다음
태스크가 새 내용으로 만든다(옛 내용의 요약이 마지막에 남지 않는다). 생성 실패는 재시도하지 않는다
(다음 `source_digest` run이 만든다).

기존 소스 백필(`backfill_source_digests`, #454)은 노트북 하나 또는 모든 노트북의 ready 소스 중 최신
요약이 없는 것을 같은 규칙·모델·귀속으로 만든다. 예약·진행률·하루 상한은
`biznotes_core.services.source_digest_backfill`이다.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any, NamedTuple

import redis as redis_sync
import structlog
from sqlalchemy import func, literal, select, tuple_

from biznotes_core.config import get_settings
from biznotes_core.models import Notebook, Source
from biznotes_core.schemas.source_digest import SourceDigestBackfillProgress
from biznotes_core.services.gateway.llm import GatewayError
from biznotes_core.services.source_digest_refresh import TOKEN_KEY
from biznotes_core.workers.celery_app import celery_app
from biznotes_core.workers.runner import get_session_factory, run_async

logger = structlog.get_logger()


async def _run(source_id: str) -> dict[str, Any]:
    from biznotes_core.services.source_digest import (
        chunk_texts,
        content_hash_of,
        content_hashes,
        generate_digest,
        load_cached,
        store_digest,
    )

    settings = get_settings()
    if not settings.source_digest_on_ingest:
        # 예약 뒤 설정을 껐다 — 큐에 남은 예약은 만들지 않는다
        return {"ok": True, "skipped": "disabled"}
    sid = uuid.UUID(source_id)
    async with get_session_factory()() as db:
        row = (
            await db.execute(
                select(Source, Notebook.owner_id)
                .join(Notebook, Notebook.id == Source.notebook_id)
                .where(Source.id == sid)
            )
        ).first()
        if row is None:
            return {"ok": True, "skipped": "source_deleted"}
        source, owner_id = row
        if source.status != "ready":
            # 그새 재인제스트가 시작됐다 — 끝나면 다시 예약된다
            return {"ok": True, "skipped": "not_ready"}
        hashes = await content_hashes(db, [sid])
        if sid not in hashes:
            return {"ok": True, "skipped": "no_content"}
        if await load_cached(db, hashes):
            return {"ok": True, "skipped": "cached"}
        texts = await chunk_texts(db, sid)
        try:
            result = await generate_digest(
                db,
                source.title,
                texts,
                target_tokens=settings.source_digest_target_tokens,
                group_tokens=settings.source_digest_group_tokens,
            )
        except GatewayError as e:
            logger.warning(
                "source_digest 미리 만들기 실패", source_id=source_id, error_code=e.node_error_code
            )
            return {"ok": False, "error": e.node_error_code}
        content_hash = content_hash_of(texts)
        # 지문은 생성에 쓴 청크로 낸다. 그 사이 청크가 바뀌었으면 새 내용의 예약이 만든다
        if (await content_hashes(db, [sid])).get(sid) != content_hash:
            return {"ok": True, "skipped": "superseded", "calls": result.calls}
    stored = await store_digest(sid, content_hash, result)
    return {"ok": stored, "calls": result.calls}


@celery_app.task(name="app.workers.tasks.source_digest.generate_source_digest", max_retries=0)
def generate_source_digest(source_id: str, token: str) -> dict[str, Any]:
    client = redis_sync.Redis.from_url(get_settings().redis_url)
    try:
        current = client.get(TOKEN_KEY.format(source_id=source_id))
    finally:
        client.close()
    current_token = current.decode() if isinstance(current, bytes) else current
    if current_token is not None and current_token != token:
        # 더 최신 예약이 있다 — 이번 실행은 건너뛴다
        return {"ok": True, "skipped": "superseded"}
    return run_async(_run(source_id))


# ── 기존 소스 백필(#454) ─────────────────────────────────────────────────

#: 백필이 한 번에 읽는 대상 소스 수 — 지문·캐시 조회를 이 단위로 한다
BACKFILL_PAGE_SIZE = 200


class BackfillTarget(NamedTuple):
    """백필 대상 ready 소스 하나와 사용량을 귀속할 노트북 소유자."""

    id: uuid.UUID
    title: str
    notebook_id: uuid.UUID
    owner_id: uuid.UUID
    created_at: datetime


def _targets_query(notebook_id: uuid.UUID | None) -> Any:
    q = (
        select(Source.id, Source.title, Source.notebook_id, Notebook.owner_id, Source.created_at)
        .join(Notebook, Notebook.id == Source.notebook_id)
        .where(Source.status == "ready")
    )
    if notebook_id is not None:
        q = q.where(Source.notebook_id == notebook_id)
    return q


async def _count_targets(db: Any, notebook_id: uuid.UUID | None) -> int:
    """백필 범위의 ready 소스 수(진행률 `total`)."""
    sub = _targets_query(notebook_id).subquery()
    return int(await db.scalar(select(func.count()).select_from(sub)) or 0)


async def _target_page(
    db: Any, notebook_id: uuid.UUID | None, after: tuple[datetime, uuid.UUID] | None
) -> list[BackfillTarget]:
    """`(created_at, id)` 순으로 `after` 다음의 대상 한 쪽(`BACKFILL_PAGE_SIZE`개)."""
    q = _targets_query(notebook_id)
    if after is not None:
        cursor = tuple_(
            literal(after[0], Source.created_at.type), literal(after[1], Source.id.type)
        )
        q = q.where(tuple_(Source.created_at, Source.id) > cursor)
    q = q.order_by(Source.created_at, Source.id).limit(BACKFILL_PAGE_SIZE)
    return [BackfillTarget(*row) for row in (await db.execute(q)).all()]


async def _backfill(notebook_id: str | None) -> dict[str, Any]:
    """범위(노트북 하나 또는 None=모두)의 최신 요약이 없는 ready 소스를 만든다.

    이미 최신 요약(지문·프롬프트 버전이 같은 행)이 있거나 청크가 없는 소스는 건너뛴다 — 중단 뒤
    다시 실행하면 이어서 진행한다. 소스마다 만들기 직전에 캐시를 다시 확인하고 하루 상한을 잡는다.
    상한에 닿으면 새 생성을 시작하지 않고(진행 중인 것은 끝낸다) `stopped`로 끝난다. 생성 실패
    (`GatewayError`)·저장 실패는 `failed`로 세고 다음 실행이 다시 만든다. 그 밖의 오류는 진행률을
    `failed`로 남기고 올린다. 끝나면(성공·실패 모두) 그 범위의 대기 표식을 지운다.
    """
    from biznotes_core.services import source_digest_backfill as backfill
    from biznotes_core.services.source_digest import (
        DigestResult,
        chunk_texts,
        content_hash_of,
        content_hashes,
        generate_digest,
        load_cached,
        store_digest,
    )

    settings = get_settings()
    nb_id = uuid.UUID(notebook_id) if notebook_id else None
    scope = backfill.backfill_scope(nb_id)
    progress = SourceDigestBackfillProgress(
        scope=scope, notebook_id=nb_id, status="running", started_at=datetime.now(UTC)
    )
    factory = get_session_factory()
    gate = asyncio.Semaphore(settings.source_digest_backfill_concurrency)
    client = backfill.backfill_redis()

    async def make(target: BackfillTarget) -> str | None:
        """소스 하나 — `generated`·`skipped`·`failed`, 상한으로 시작하지 않았으면 None."""
        async with gate:
            if progress.stop_reason is not None:
                return None
            async with factory() as db:
                texts = await chunk_texts(db, target.id)
                if not texts:
                    return "skipped"
                content_hash = content_hash_of(texts)
                if await load_cached(db, {target.id: content_hash}):
                    return "skipped"  # 그새 다른 경로(run·미리 만들기)가 만들었다
                reason = await backfill.reserve_generation(client, settings)
                if reason is not None:
                    progress.stop_reason = reason
                    return None
                spent = DigestResult(text="", token_count=0)
                try:
                    result = await generate_digest(
                        db,
                        target.title,
                        texts,
                        target_tokens=settings.source_digest_target_tokens,
                        group_tokens=settings.source_digest_group_tokens,
                        into=spent,
                    )
                except GatewayError as e:
                    logger.warning(
                        "source_digest 백필 생성 실패",
                        source_id=str(target.id),
                        error_code=e.node_error_code,
                    )
                    return "failed"
                finally:
                    await backfill.record_spend(client, spent.cost_usd)
            # 지문은 생성에 쓴 청크로 낸다 — 그새 재인제스트됐으면 저장하지 않는다(다음 실행이
            # 만든다)
            stored = await store_digest(target.id, content_hash, result)
            return "generated" if stored else "failed"

    async def run_one(target: BackfillTarget) -> None:
        outcome = await make(target)
        if outcome is None:
            return
        setattr(progress, outcome, getattr(progress, outcome) + 1)
        await backfill.save_progress(client, progress)

    try:
        async with factory() as db:
            progress.total = await _count_targets(db, nb_id)
        await backfill.save_progress(client, progress)
        after: tuple[datetime, uuid.UUID] | None = None
        while progress.stop_reason is None:
            async with factory() as db:
                page = await _target_page(db, nb_id, after)
                if not page:
                    break
                hashes = await content_hashes(db, [t.id for t in page])
                cached = await load_cached(db, hashes)
            after = (page[-1].created_at, page[-1].id)
            progress.skipped += sum(1 for t in page if t.id not in hashes or t.id in cached)
            todo = [t for t in page if t.id in hashes and t.id not in cached]
            tasks = [asyncio.ensure_future(run_one(t)) for t in todo]
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                # 하나가 실패하면 나머지를 취소하고 기다린다 — 세션을 쥔 채 돌지 않게
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            await backfill.save_progress(client, progress)
        progress.status = "stopped" if progress.stop_reason is not None else "done"
    except BaseException:
        progress.status = "failed"
        raise
    finally:
        progress.finished_at = datetime.now(UTC)
        try:
            await backfill.save_progress(client, progress)
            await backfill.release_pending(client, scope)
        except Exception as e:  # Redis 장애 — 대기 표식은 TTL로 풀린다
            logger.warning("source_digest 백필 상태 저장 실패", scope=scope, error=type(e).__name__)
        finally:
            await client.aclose()
    logger.info(
        "source_digest 백필",
        scope=scope,
        status=progress.status,
        total=progress.total,
        generated=progress.generated,
        skipped=progress.skipped,
        failed=progress.failed,
        stop_reason=progress.stop_reason,
    )
    return {
        "ok": True,
        "scope": scope,
        "status": progress.status,
        "total": progress.total,
        "generated": progress.generated,
        "skipped": progress.skipped,
        "failed": progress.failed,
        "stop_reason": progress.stop_reason,
    }


@celery_app.task(name="app.workers.tasks.source_digest.backfill_source_digests", max_retries=0)
def backfill_source_digests(notebook_id: str | None = None) -> dict[str, Any]:
    """노트북 하나(`notebook_id`) 또는 모든 노트북(None)의 기존 소스 요약을 백필한다(#454)."""
    return run_async(_backfill(notebook_id))
