"""`source_digest` 노드 — run 범위의 모든 소스를 요약으로 담는다(#360).

`source_load`는 원문을 담고 개수·길이 상한으로 소스를 뺀다. 이 노드는 소스마다 문서 전체를 덮는
요약(`services.source_digest`)을 담고 **소스를 빼지 않는다**. 캐시가 없거나 낡은 소스만 노드 안에서
만든다(동시성 상한, 분기 세션). 만들지 못한 소스는 소스 가이드 요약이나 앞부분 발췌를 넣고
`degraded`에 남긴다.

생성 모델(#396)은 llm 노드와 같은 순서로 정한다 — 노드 `model_id` → run 모델(#359) → 채팅이 고른
모델 → 기본 모델(+ 폴백 체인). 캐시는 모델 간에 공유한다 — 다른 모델이 만든 캐시도 그대로 쓰고
모델을 부르지 않는다. `refresh_other_models`를 켜면 1차 모델이 아닌 모델(삭제돼 NULL인 행 포함)이
만든 캐시만 다시 만들어 덮는다.

출력 `context`는 source_load와 같은 모양(소스마다 `## 제목` + 본문 블록, `---`로 이음)이라 템플릿
`{노드.context}`로 넣는다. `fit: "shrink_each"` 표식이 있어 llm 노드가 모델 예산에 맞출 때
(#357) 소스를 빼지 않고 소스마다 같은 비율로 줄인다.

소스가 많아 담을 예산(노드 `budget_tokens` 또는 1차 모델 창의 일부)으로는 소스마다 남는 몫이 너무
작으면 글자 자르기 대신 소스를 배치로 묶은 배치 요약을 담는다(계층 요약, #448). 한 run이 만드는
소스 요약 수는 설정으로 제한할 수 있다(`source_digest_run_max_generate`). 넘은 소스가 있으면 그
노트북 백필을 예약한다(#454).
"""

import asyncio
import uuid
from typing import Any

import structlog
from sqlalchemy import select

from biznotes_core.config import get_settings
from biznotes_core.services.agents.context import RunContext, run_parallel
from biznotes_core.services.agents.errors import AgentRuntimeError
from biznotes_core.services.agents.node_errors import describe_node_failure
from biznotes_core.services.agents.templating import render, resolve_value_path, whole_value_ref
from biznotes_core.services.untrusted import clean_label, neutralize_citation_heads, wrap_untrusted

logger = structlog.get_logger()


def _positive_int(node: dict, key: str) -> int | None:
    raw = node.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise AgentRuntimeError(f"source_digest {key}는 1 이상의 정수여야 합니다: {raw!r}")
    return raw


def _primary_model_id(ctx: RunContext, node: dict) -> uuid.UUID | None:
    """1차 모델 — 노드 `model_id` → run 모델(#359) → 채팅이 고른 모델. 없으면 None(기본 모델)."""
    raw = node.get("model_id") or ctx.run_model_id or ctx.chat_model_id
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError as e:
        raise AgentRuntimeError(f"source_digest model_id가 올바르지 않습니다: {raw}") from e


def _refresh_flag(node: dict, state: dict) -> bool:
    """`refresh_other_models` — bool 또는 템플릿(`"{params.refresh}"`). 비거나 없으면 False."""
    raw = node.get("refresh_other_models", False)
    if isinstance(raw, str):
        path = whole_value_ref(raw)
        raw = resolve_value_path(state, path) if path is not None else render(raw, state)
    if raw is None or isinstance(raw, bool):
        return bool(raw)
    if isinstance(raw, str) and raw.strip().lower() in ("", "true", "false"):
        return raw.strip().lower() == "true"
    raise AgentRuntimeError(f"source_digest refresh_other_models는 true·false여야 합니다: {raw!r}")


async def _refresh_target(ctx: RunContext, model_id: uuid.UUID | None) -> uuid.UUID | None:
    """다시 만들기에서 견줄 1차 모델 id — 지정이 없으면 기본 모델이다. 기본 모델이 없으면 None."""
    if model_id is not None:
        return model_id
    from biznotes_core.services.source_digest import resolve_model

    try:
        return (await resolve_model(ctx.db, None)).model.id
    except Exception:  # 등록 모델이 없다 — 만들어도 실패하므로 캐시를 그대로 쓴다
        return None


async def _schedule_deferred_backfill(ctx: RunContext) -> bool:
    """`generation_deferred` 소스가 생긴 run 뒤 그 노트북 백필을 예약한다(#454). 예약했으면 True.

    같은 노트북에 대기·진행 중인 백필이 있으면 다시 발행하지 않는다. Redis·브로커 오류는 경고만
    남긴다 — run은 대체 본문으로 이미 끝났다.
    """
    from biznotes_core.services import source_digest_backfill as backfill

    try:
        return await backfill.schedule_backfill(
            ctx.notebook_id, countdown=backfill.RUN_SCHEDULE_COUNTDOWN_SECONDS
        )
    except Exception as e:  # 예약 실패 — 다음 run이 다시 예약한다
        logger.warning(
            "source_digest 백필 예약 실패", run_id=str(ctx.run_id), error=type(e).__name__
        )
        return False


def _cap_tokens(text: str, cap: int | None) -> str:
    if cap is None or not text:
        return text
    from biznotes_core.services.source_digest import count_tokens

    tokens = count_tokens(text)
    return text if tokens <= cap else text[: max(int(len(text) * cap / tokens), 1)]


async def _digest_budget(
    ctx: RunContext, explicit: int | None, model_id: uuid.UUID | None, window_ratio: float
) -> int | None:
    """담을 예산(토큰) — 노드 `budget_tokens`, 없으면 1차 모델 `context_window` × 비율(#448).

    창을 모르는 모델(없음·0)이나 모델 해석 실패는 None이다 — 지금처럼 소스마다 담는다.
    """
    if explicit is not None:
        return explicit
    from biznotes_core.services.source_digest import resolve_model

    try:
        window = (await resolve_model(ctx.db, model_id)).model.context_window
    except Exception:  # 등록 모델이 없다 — 예산 없이 지금처럼 담는다
        return None
    if not window or window <= 0:
        return None
    return max(int(window * window_ratio), 1)


def _needs_batches(
    total: int, count: int, budget: int, min_per_source: int, shrink_ratio: float
) -> bool:
    """배치 요약을 담을지(#448) — 요약 합이 예산을 넘고, 소스당 몫이 하한보다 작거나 줄일 비율이
    임계보다 작을 때(글자 자르기로는 뒷부분이 너무 많이 사라진다)."""
    if total <= budget:
        return False
    return budget < count * min_per_source or budget < total * shrink_ratio


async def _exec_source_digest(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
    """`{model_id?, max_tokens_per_source?, concurrency?, refresh_other_models?, budget_tokens?}` →
    `{context, sources, degraded, dropped, fit}` (배치 요약이면 `blocks`가 더 있다).

    대상: `ctx.source_ids`(없으면 노트북 전체)의 ready 소스. `[]`면 읽지 않는다(#13).
    순서(`context` 블록·`sources`·`degraded`): `(created_at, id)` 오름차순 — 한 트랜잭션에서
    넣어 `created_at`이 같은 소스도 행을 고쳐도 순서가 그대로다(#391).
    `max_tokens_per_source`는 담을 때 소스마다 자르는 상한이다(캐시는 그대로). `concurrency`는
    요약을 만드는 동시 소스 수(설정 `source_digest_max_concurrency`가 상한·기본값)다.
    `sources[]`의 `model_id`·`model_name`은 그 요약을 만든 모델(캐시 행 또는 이번 생성)이고 대체
    본문인 소스는 null이다. 다시 만들기가 실패한 소스는 기존 캐시를 그대로 쓴다.

    run 생성 상한(설정 `source_digest_run_max_generate`, 0=무제한)을 넘는 캐시 없는 소스는 대체
    본문을 넣고 `degraded`(`generation_deferred`)에 남긴다. 상한 안에서는 캐시 없는 소스를 다시
    만들기보다 먼저 만든다(#448). 그런 소스가 있으면 그 노트북 백필을 예약한다(#454, 중복 발행
    없음).

    계층 요약(#448): 예산(`_digest_budget`)이 있고 `_needs_batches`면 소스를 `(created_at, id)` 순
    배치로 묶어 배치 요약(`services.source_digest.generate_batch_digest`)을 담는다. 블록은 배치
    단위(`## 소스 묶음 i/N`)이고 `blocks[]`(`{id, title, source_ids, cached, fallback}`)가 블록과
    소스를 잇는다. `sources`는 여전히 모든 소스이고(`batch`: 1부터 센 배치 번호) `degraded`·
    `dropped`의 뜻은 그대로다. 배치 요약을 만들지 못하면 `- 제목: 앞부분` 목록을 넣는다
    (`fallback: "excerpt"`).
    """
    from biznotes_core.models import Source
    from biznotes_core.services.agents.nodes.retrieval import _join_source_context
    from biznotes_core.services.source_digest import (
        BatchItem,
        DigestResult,
        batch_cache_key,
        batch_fallback_text,
        chunk_texts,
        content_hash_of,
        content_hashes,
        count_tokens,
        fallback_body,
        generate_batch_digest,
        generate_digest,
        load_batch_cached,
        load_cached,
        plan_batches,
        store_batch_digest,
        store_digest,
    )

    settings = get_settings()
    cap = _positive_int(node, "max_tokens_per_source")
    explicit_budget = _positive_int(node, "budget_tokens")
    concurrency = min(
        _positive_int(node, "concurrency") or settings.source_digest_max_concurrency,
        settings.source_digest_max_concurrency,
    )
    model_id = _primary_model_id(ctx, node)
    refresh = _refresh_flag(node, state)

    sources: list[Any] = []
    if ctx.source_ids is None or ctx.source_ids:
        q = select(Source).where(Source.notebook_id == ctx.notebook_id, Source.status == "ready")
        if ctx.source_ids is not None:
            q = q.where(Source.id.in_(ctx.source_ids))
        q = q.order_by(Source.created_at, Source.id)
        sources = list((await ctx.db.execute(q)).scalars().all())

    hashes = await content_hashes(ctx.db, [s.id for s in sources])
    cached = await load_cached(ctx.db, hashes)
    stale: set[uuid.UUID] = set()  # 다른 모델이 만든 캐시 — 다시 만들고, 실패하면 그대로 쓴다
    if refresh and cached:
        target = await _refresh_target(ctx, model_id)
        if target is not None:
            stale = {sid for sid, row in cached.items() if row.model_id != target}
    missing = [s.id for s in sources if s.id in hashes and s.id not in cached]
    wanted = missing + [s.id for s in sources if s.id in stale]
    limit = settings.source_digest_run_max_generate
    chosen = set(wanted[:limit] if limit else wanted)
    deferred = [sid for sid in missing if sid not in chosen]
    todo = [s for s in sources if s.id in chosen]
    gate = asyncio.Semaphore(concurrency)

    async def make(branch: RunContext, src: Any) -> tuple[uuid.UUID, Any, list[str], Any, Any]:
        """(소스 id, 성공한 결과, 청크, 실패 판정, 쓴 사용량). 사용량은 실패해도 남는다."""
        async with gate:
            texts = await chunk_texts(branch.db, src.id)
            spent = DigestResult(text="", token_count=0)
            try:
                result = await generate_digest(
                    branch.db,
                    src.title,
                    texts,
                    target_tokens=settings.source_digest_target_tokens,
                    group_tokens=settings.source_digest_group_tokens,
                    into=spent,
                    model_id=model_id,
                )
            except Exception as e:  # 소스 하나의 실패는 대체 본문으로 넣는다(소스를 빼지 않는다)
                return src.id, None, texts, describe_node_failure(e), spent
            return src.id, result, texts, None, spent

    made = await run_parallel(ctx, todo, make, mark_parallel=False) if todo else []

    bodies: dict[uuid.UUID, str] = {sid: row.digest for sid, row in cached.items()}
    # 소스마다 그 요약을 만든 모델 — 대체 본문이면 빠진다(null)
    makers: dict[uuid.UUID, tuple[str | None, str | None]] = {
        sid: (str(row.model_id) if row.model_id is not None else None, row.model_name)
        for sid, row in cached.items()
    }
    refreshed: set[uuid.UUID] = set()
    degraded: list[dict[str, Any]] = []
    usage: dict[str, int] = {}
    cost = 0.0

    def spend(spent: Any) -> None:
        # 분기 컨텍스트의 노드 토큰은 사본이라 여기서(부모) 더한다 — 실패한 생성이 이미 쓴 것도
        nonlocal cost
        for key, value in spent.usage.items():
            usage[key] = usage.get(key, 0) + value
        cost += spent.cost_usd

    by_id = {s.id: s for s in sources}
    for sid, result, texts, failure, spent in made:
        spend(spent)
        if result is not None:
            bodies[sid] = result.text
            maker = result.model.model if result.model is not None else None
            makers[sid] = (str(maker.id), maker.display_name) if maker is not None else (None, None)
            if sid in cached:
                refreshed.add(sid)
            # 지문은 생성에 실제로 쓴 청크로 낸다 — 지문 조회 뒤 재인제스트돼도 요약과 지문이
            # 어긋나지 않는다(독립 리뷰 M2)
            await store_digest(sid, content_hash_of(texts), result)
            continue
        if sid in cached:  # 다시 만들기 실패 — 기존 캐시 요약을 그대로 쓴다
            logger.warning(
                "source_digest 다시 만들기 실패, 캐시 사용",
                run_id=str(ctx.run_id),
                source_id=str(sid),
                error_code=failure.code,
            )
            continue
        src = by_id[sid]
        body, kind = fallback_body(src, texts)
        bodies[sid] = body
        degraded.append(
            {
                "id": str(sid),
                "title": src.title,
                "reason": "generation_failed",
                "error_code": failure.code,
                "fallback": kind,
            }
        )
    for sid in deferred:  # run 생성 상한을 넘었다 — 다음 run·미리 만들기가 만든다
        src = by_id[sid]
        texts = [] if src.summary else await chunk_texts(ctx.db, sid)
        body, kind = fallback_body(src, texts)
        bodies[sid] = body
        degraded.append(
            {
                "id": str(sid),
                "title": src.title,
                "reason": "generation_deferred",
                "error_code": None,
                "fallback": kind,
            }
        )
    for src in sources:
        if src.id not in hashes:  # 청크가 없다
            body, kind = fallback_body(src, [])
            bodies[src.id] = body
            degraded.append(
                {
                    "id": str(src.id),
                    "title": src.title,
                    "reason": "no_content",
                    "error_code": None,
                    "fallback": kind,
                }
            )

    capped = {src.id: _cap_tokens(bodies.get(src.id, ""), cap) for src in sources}
    budget = (
        await _digest_budget(
            ctx, explicit_budget, model_id, settings.source_digest_budget_window_ratio
        )
        if sources
        else None
    )
    if budget is not None and not _needs_batches(
        sum(count_tokens(body) for body in capped.values() if body),
        len(sources),
        budget,
        settings.source_digest_min_tokens_per_source,
        settings.source_digest_shrink_summary_ratio,
    ):
        budget = None  # 예산 안이거나 같은 비율로 줄여도 된다 — 지금처럼 소스마다 담는다
    batched = budget is not None

    parts: list[str] = []
    blocks: list[dict[str, Any]] = []
    batch_of: dict[str, int] = {}
    if budget is not None:
        batches = await plan_batches(
            ctx.db,
            [BatchItem(key=str(s.id), title=s.title, body=capped[s.id]) for s in sources],
            budget_tokens=budget,
            group_tokens=settings.source_digest_group_tokens,
            model_id=model_id,
        )
        keys = [batch_cache_key(batch, model_id) for batch in batches]
        hits = await load_batch_cached(keys)
        texts_of: dict[int, str] = {i: hits[key].text for i, key in enumerate(keys) if key in hits}
        failed: set[int] = set()

        async def make_batch(branch: RunContext, index: int) -> tuple[int, Any, Any, Any]:
            """(배치 번호, 성공한 결과, 실패 판정, 쓴 사용량)."""
            async with gate:
                spent = DigestResult(text="", token_count=0)
                try:
                    result = await generate_batch_digest(
                        branch.db, batches[index], model_id=model_id, into=spent
                    )
                except Exception as e:  # 배치 하나의 실패는 제목 목록으로 넣는다
                    return index, None, describe_node_failure(e), spent
                return index, result, None, spent

        pending = [i for i in range(len(batches)) if i not in texts_of]
        made_batches = (
            await run_parallel(ctx, pending, make_batch, mark_parallel=False) if pending else []
        )
        for index, result, failure, spent in made_batches:
            spend(spent)
            if result is not None:
                texts_of[index] = result.text
                await store_batch_digest(
                    keys[index], result, ttl_seconds=settings.source_digest_batch_cache_ttl_sec
                )
                continue
            logger.warning(
                "source_digest 배치 요약 실패, 제목 목록 사용",
                run_id=str(ctx.run_id),
                batch=index + 1,
                error_code=failure.code,
            )
            texts_of[index] = batch_fallback_text(batches[index])
            failed.add(index)
        for index, batch in enumerate(batches):
            title = f"소스 묶음 {index + 1}/{len(batches)}"
            text = neutralize_citation_heads(texts_of[index])
            parts.append(wrap_untrusted(f"## {title}\n\n{text}".strip(), "source"))
            blocks.append(
                {
                    "id": f"batch-{index + 1}",
                    "title": title,
                    "source_ids": [item.key for item in batch.items],
                    "cached": keys[index] in hits,
                    "fallback": "excerpt" if index in failed else None,
                }
            )
            batch_of.update((item.key, index + 1) for item in batch.items)
    if usage:
        ctx.add_tokens(usage)
    if cost:
        ctx.add_cost(cost)
    backfill_scheduled = await _schedule_deferred_backfill(ctx) if deferred else False

    listed: list[dict[str, Any]] = []
    for src in sources:
        if not batched:
            block = f"## {clean_label(src.title)}\n\n{neutralize_citation_heads(capped[src.id])}"
            parts.append(wrap_untrusted(block.strip(), "source"))
        maker_id, maker_name = makers.get(src.id, (None, None))
        entry: dict[str, Any] = {
            "id": str(src.id),
            "title": src.title,
            "cached": src.id in cached and src.id not in refreshed,
            "model_id": maker_id,
            "model_name": maker_name,
        }
        if batched:
            entry["batch"] = batch_of[str(src.id)]
        listed.append(entry)
    logger.info(
        "source_digest",
        run_id=str(ctx.run_id),
        sources=len(sources),
        cached=len(cached) - len(refreshed),
        refreshed=len(refreshed),
        generated=sum(1 for _, r, _, _, _ in made if r is not None),
        deferred=len(deferred),
        backfill_scheduled=backfill_scheduled,
        degraded=len(degraded),
        batches=len(blocks),
    )
    out: dict[str, Any] = {
        "context": _join_source_context(parts, []),
        "sources": listed,
        "degraded": degraded,
        "dropped": [],
        "fit": "shrink_each",
    }
    if batched:
        out["blocks"] = blocks
    return out
