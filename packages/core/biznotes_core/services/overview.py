"""노트북 개요(요약 + 추천 질문) 생성 서비스.

소스 ready 전이 시 디바운스를 걸어 백그라운드에서 재생성한다.
LLM 응답은 JSON을 우선 파싱하고, 실패하면 원문을 요약으로 쓰는 폴백을 둔다
(mock 프로바이더는 JSON을 생성하지 않으므로 폴백 경로가 기본 동작).

소스가 많아 소스마다 한 줄씩 담으면 소스당 몫이 너무 작을 때는 소스를 배치로 묶은 배치 요약
(`source_digest`의 계층 요약 함수, #448)을 한 줄씩 담는다(#450).
"""

import json
import math
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.config import get_settings
from biznotes_core.models import NotebookOverview, Source, SourceChunk, SourceDigest
from biznotes_core.services.gateway.llm import (
    GatewayError,
    StreamResult,
    complete_stream_with_fallback,
)
from biznotes_core.services.source_digest import (
    PROMPT_VERSION,
    BatchItem,
    batch_cache_key,
    batch_fallback_text,
    content_hashes,
    count_tokens,
    generate_batch_digest,
    load_batch_cached,
    plan_batches,
    store_batch_digest,
)
from biznotes_core.services.untrusted import UNTRUSTED_DATA_RULE, clean_label, wrap_untrusted

logger = structlog.get_logger()

# 개요 입력 블록 본문의 글자 예산 — 줄 머리 `[n] `·줄바꿈 포함, 소스별로 균등 배분한다(#449)
MAX_CONTEXT_CHARS = 24_000
MAX_QUESTIONS = 5

# 배치 요약의 목표 합계 ≤ 예산(토큰 추정) × 이 비율 — 배치 요약 출력 상한(목표 × 1.5 + 여유)과
# 빠진 제목 덧붙임이 예산 안에 들게 해, 고르게 자를 때 묶음 뒤쪽 소스 제목이 잘리지 않게 한다
_BATCH_TARGET_RATIO = 0.6

_OVERVIEW_ROLE = "당신은 노트북 소스를 요약하는 어시스턴트입니다.\n"
_OVERVIEW_FORMAT = (
    UNTRUSTED_DATA_RULE
    + "\n\n"
    "다음 JSON 형식으로만 답하세요 (다른 텍스트 금지):\n"
    '{"summary": "<소스 전체를 아우르는 3~5문장 한국어 요약>", '
    '"questions": ["<소스 내용으로 답할 수 있는 한국어 질문>", "..."], '
    '"title_suggestions": ["<소스 내용을 대표하는 간결한 노트북 제목 후보(10자 내외)>", "..."]}\n\n'
    "질문은 3~5개, 서로 다른 주제를 다루도록 작성하세요.\n"
    "제목 후보는 1~3개, 서로 다른 관점으로 작성하세요."
)
OVERVIEW_PROMPT = (
    _OVERVIEW_ROLE
    + "아래 untrusted_data 블록의 [n] 줄은 노트북에 등록된 소스마다 하나씩 있고,\n"
    "각 줄은 그 소스의 요약(없으면 앞부분 발췌)입니다.\n"
    "일부 소스만이 아니라 모든 줄을 고르게 반영하세요.\n" + _OVERVIEW_FORMAT
)
# 배치 reduce 입력(#450) — 줄마다 소스 묶음 하나, 묶음 안에 소스마다 `- 제목: 핵심` 항목
OVERVIEW_BATCH_PROMPT = (
    _OVERVIEW_ROLE
    + "아래 untrusted_data 블록의 [n] 줄은 노트북에 등록된 소스를 등록 순서대로 묶은\n"
    "소스 묶음마다 하나씩 있고, 각 줄은 그 묶음의 소스마다 `- 제목: 핵심` 항목을 담은 요약입니다.\n"
    "일부 묶음이나 소스만이 아니라 모든 줄의 모든 소스를 고르게 반영하세요.\n" + _OVERVIEW_FORMAT
)
MAX_TITLE_SUGGESTIONS = 3


async def _ready_sources(db: AsyncSession, notebook_id: uuid.UUID) -> list[Source]:
    rows = await db.execute(
        select(Source)
        .where(Source.notebook_id == notebook_id, Source.status == "ready")
        .order_by(Source.created_at, Source.id)  # 같은 시각이면 id 순(#391)
    )
    return list(rows.scalars().all())


async def _source_texts(db: AsyncSession, sources: list[Source]) -> list[str]:
    """소스마다 개요 입력 본문 하나(`sources`와 같은 순서) — 어떤 소스도 빼지 않는다(#449).

    소스 가이드 요약(`Source.summary`) → 지금 청크와 지문이 같은 digest(`SourceDigest`) → 첫 청크
    → 제목 순으로 고른다. digest는 재인제스트 뒤 옛 본문의 요약일 수 있어 지문과 프롬프트 버전을
    확인한다(`source_digest.load_cached`와 같은 규칙). 쿼리는 소스 수와 무관하게 최대 3개다(#164).
    """
    texts: dict[uuid.UUID, str] = {
        s.id: s.summary for s in sources if s.summary and s.summary.strip()
    }
    missing = [s.id for s in sources if s.id not in texts]
    if missing:
        rows = await db.execute(select(SourceDigest).where(SourceDigest.source_id.in_(missing)))
        digests = {
            row.source_id: row
            for row in rows.scalars().all()
            if row.prompt_version == PROMPT_VERSION and row.digest.strip()
        }
        if digests:
            hashes = await content_hashes(db, list(digests))
            for source_id, row in digests.items():
                if hashes.get(source_id) == row.content_hash:
                    texts[source_id] = row.digest
            missing = [source_id for source_id in missing if source_id not in texts]
    if missing:
        ranked = (
            select(
                SourceChunk.source_id,
                SourceChunk.text,
                func.row_number()
                .over(partition_by=SourceChunk.source_id, order_by=SourceChunk.seq)
                .label("rn"),
            )
            .where(SourceChunk.source_id.in_(missing))
            .subquery()
        )
        rows = await db.execute(select(ranked.c.source_id, ranked.c.text).where(ranked.c.rn == 1))
        for source_id, text in rows.all():
            if text.strip():
                texts[source_id] = text
    return [texts.get(s.id) or clean_label(s.title) for s in sources]


def _fair_shares(lengths: list[int], budget: int) -> list[int]:
    """`budget` 글자를 max-min 균등 배분 — 짧은 본문이 남긴 몫은 긴 본문들이 고르게 나눈다.

    결과는 `lengths`와 같은 순서이고 합은 `max(budget, 0)` 이하다. 몫이 0인 항목도 남긴다.
    """
    shares = [0] * len(lengths)
    remaining = max(budget, 0)
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    for k, i in enumerate(order):
        shares[i] = min(lengths[i], remaining // (len(order) - k))
        remaining -= shares[i]
    return shares


def _flatten(text: str) -> str:
    """공백·줄바꿈을 접어 한 줄로 — 본문이 `[n]` 줄 머리를 위조하지 못한다."""
    return " ".join(text.split())


def _line_overhead(count: int) -> int:
    """`count`줄 블록 본문의 줄 머리 `[n] `와 줄바꿈 글자 수."""
    return sum(len(f"[{n}] ") for n in range(1, count + 1)) + max(count - 1, 0)


def _overview_context(texts: list[str]) -> str:
    """줄마다 `[n] 본문`을 비신뢰 데이터 블록 하나로 감싼다(HWP-05).

    본문의 공백·줄바꿈을 접어 한 줄로 만든다 — 본문이 `[n]` 줄 머리를 위조하지 못한다. 줄 머리·
    줄바꿈까지 합친 블록 본문이 `MAX_CONTEXT_CHARS`(이스케이프 전) 안에 들도록 남은 예산을 줄별로
    균등 배분한다(`_fair_shares`). 줄 수가 많아도 줄을 빼지 않고 줄마다 덜 담는다.
    """
    flat = [_flatten(text) for text in texts]
    heads = [f"[{n}] " for n in range(1, len(flat) + 1)]
    budget = MAX_CONTEXT_CHARS - _line_overhead(len(flat))
    shares = _fair_shares([len(text) for text in flat], budget)
    lines = [
        f"{head}{text[:share]}".rstrip()
        for head, text, share in zip(heads, flat, shares, strict=True)
    ]
    return wrap_untrusted("\n".join(lines), "source")


def _batch_share_tokens(texts: list[str]) -> int | None:
    """배치 reduce를 쓸 때 배치 요약에서 소스마다 줄 몫(토큰), 소스마다 한 줄씩 담으면 None(#450).

    한 줄씩 담은 합계가 예산을 넘고, 그때 소스마다 남는 몫(토큰 추정)이 설정
    `source_digest_min_tokens_per_source`(#448 `source_digest` 노드와 같은 하한)보다 작을 때만
    줄인다. 0이면 줄이지 않는다. 토큰은 소스 본문의 글자당 토큰 비율로 추정한다.

    몫은 √2배 단위로 내린다 — 소스가 조금 늘어도 몫이 그대로라 앞 배치들의 캐시 키(목표 토큰 =
    몫 × 항목 수)가 같고, 새 소스가 든 마지막 배치만 다시 만든다.
    """
    flat = [_flatten(text) for text in texts]
    total = sum(len(text) for text in flat)
    budget = MAX_CONTEXT_CHARS - _line_overhead(len(flat))
    if total <= budget:
        return None
    per_char = count_tokens("\n".join(flat)) / total
    minimum = get_settings().source_digest_min_tokens_per_source
    if max(budget, 0) * per_char >= len(flat) * minimum:
        return None
    share = MAX_CONTEXT_CHARS * per_char * _BATCH_TARGET_RATIO / len(flat)
    if share < 1:
        return 1
    return int(2 ** (math.floor(math.log2(share) * 2) / 2))


def _cap_tokens(text: str, cap: int) -> str:
    """`cap` 토큰 안으로 뒤를 자른다(글자 비율 추정)."""
    tokens = count_tokens(text) if text else 0
    return text if tokens <= cap else text[: max(int(len(text) * cap / tokens), 1)]


async def _batch_texts(
    db: AsyncSession, sources: list[Source], texts: list[str], share_tokens: int
) -> list[str]:
    """소스를 순서대로 배치로 묶은 배치 요약 목록 — 어떤 소스도 빠지지 않는다(#450).

    #448 함수(`plan_batches`·`generate_batch_digest`)를 쓴다. 배치 요약의 목표는 소스마다
    `share_tokens`다. 배치 입력 본문은 소스 요약 하나 분량(설정 `source_digest_target_tokens`)까지만
    넣어 배치 수(호출 수)를 묶어 둔다. 배치 캐시(Redis)에 있으면 다시 만들지 않고 만든 요약은 캐시에
    쓴다 — Redis 오류는 캐시 미스다. 배치 요약을 만들지 못하면 `- 제목: 앞부분` 목록을 쓴다. 세션
    하나를 쓰므로 배치를 차례로 부른다.
    """
    settings = get_settings()
    items = [
        BatchItem(
            key=str(source.id),
            title=source.title,
            body=_cap_tokens(text, settings.source_digest_target_tokens),
        )
        for source, text in zip(sources, texts, strict=True)
    ]
    batches = await plan_batches(
        db,
        items,
        budget_tokens=share_tokens * len(items),
        group_tokens=settings.source_digest_group_tokens,
    )
    keys = [batch_cache_key(batch, None) for batch in batches]
    hits = await load_batch_cached(keys)
    summaries: list[str] = []
    for index, (batch, key) in enumerate(zip(batches, keys, strict=True)):
        if key in hits:
            summaries.append(hits[key].text)
            continue
        try:
            result = await generate_batch_digest(db, batch)
        except GatewayError as e:
            logger.warning(
                "overview batch digest failed, title list used", batch=index + 1, error=str(e)
            )
            summaries.append(batch_fallback_text(batch))
            continue
        await store_batch_digest(
            key, result, ttl_seconds=settings.source_digest_batch_cache_ttl_sec
        )
        summaries.append(result.text)
    return summaries


def _extract_json_string(key: str, text: str) -> str | None:
    """truncate/말줄임된 JSON에서도 단일 문자열 필드를 추출한다."""
    m = re.search(rf'"{key}"\s*:\s*"((?:\\.|[^"\\])*)"', text, re.S)
    if not m:
        return None
    try:
        return json.loads(f'"{m.group(1)}"').strip()
    except json.JSONDecodeError:
        return m.group(1).strip()


def _extract_json_array(key: str, text: str) -> list[str]:
    """key 배열에서 (배열이 닫히지 않았더라도) 완결된 문자열만 모은다."""
    arr = re.search(rf'"{key}"\s*:\s*\[(.*)', text, re.S)
    if not arr:
        return []
    items: list[str] = []
    for m in re.finditer(r'"((?:\\.|[^"\\])*)"', arr.group(1)):
        try:
            v = json.loads(f'"{m.group(1)}"').strip()
        except json.JSONDecodeError:
            v = m.group(1).strip()
        if v:
            items.append(v)
    return items


def _spread(items: list[str], count: int) -> list[str]:
    """`items`에서 처음·마지막을 포함해 고르게 `count`개(순서 유지) — `count`개 이하면 전부.

    추천 질문 대체 문구가 앞 소스들만 다루지 않게 한다(#450).
    """
    if len(items) <= count:
        return list(items)
    step = (len(items) - 1) / (count - 1)
    return [items[round(i * step)] for i in range(count)]


def _parse_overview_response(
    raw: str, source_titles: list[str]
) -> tuple[str, list[str], list[str]]:
    """JSON 응답 파싱 — (summary, questions, title_suggestions).

    1) 완전한 JSON이면 정상 파싱
    2) truncate된 JSON이면 정규식으로 summary/questions/title_suggestions를 부분 추출
    3) 그래도 실패하면 원문을 요약으로, 소스 제목 기반 질문 폴백(제목 후보는 빈 배열)
    """
    text = raw.strip()
    fallback_questions = [
        f"{title}의 핵심 내용은 무엇인가요?" for title in _spread(source_titles, MAX_QUESTIONS)
    ]

    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            data: dict[str, Any] = json.loads(match.group(0))
            summary = str(data.get("summary", "")).strip()
            questions = [str(q).strip() for q in data.get("questions", []) if str(q).strip()]
            titles = [
                str(t).strip() for t in data.get("title_suggestions", []) if str(t).strip()
            ]
            if summary:
                return summary, questions[:MAX_QUESTIONS], titles[:MAX_TITLE_SUGGESTIONS]
        except (json.JSONDecodeError, AttributeError):
            pass

    # JSON 구조가 깨졌을 때(예: 응답 잘림) 필드만이라도 건져낸다.
    if '"summary"' in text:
        partial = _extract_json_string("summary", text)
        if partial:
            questions = _extract_json_array("questions", text)
            titles = _extract_json_array("title_suggestions", text)
            return (
                partial,
                (questions or fallback_questions)[:MAX_QUESTIONS],
                titles[:MAX_TITLE_SUGGESTIONS],
            )

    return text, fallback_questions, []


async def generate_overview(db: AsyncSession, notebook_id: uuid.UUID) -> NotebookOverview | None:
    """요약 + 추천 질문 생성 후 upsert. ready 소스가 없으면 None."""
    sources = await _ready_sources(db, notebook_id)
    if not sources:
        return None

    # 모든 ready 소스가 한 줄씩 들어간다(#449). 소스당 몫이 하한보다 작으면 소스 묶음의 배치
    # 요약이 한 줄씩 들어간다(#450). 본문은 비신뢰 데이터 블록이다(HWP-05) — 결과가 저장돼
    # 사용자에게 보인다.
    texts = await _source_texts(db, sources)
    share_tokens = _batch_share_tokens(texts)
    if share_tokens is None:
        prompt, lines = OVERVIEW_PROMPT, texts
    else:
        prompt, lines = OVERVIEW_BATCH_PROMPT, await _batch_texts(db, sources, texts, share_tokens)
    context = _overview_context(lines)
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"{context}\n\n위 소스를 요약하고 질문을 제안하세요."},
    ]

    try:
        result = StreamResult()
        stream = await complete_stream_with_fallback(db, None, messages, result, temperature=0.3)
        raw = "".join([delta async for delta in stream])
    except GatewayError as e:
        logger.warning("overview generation skipped", notebook_id=str(notebook_id), error=str(e))
        return None

    summary, questions, title_suggestions = _parse_overview_response(
        raw, [s.title for s in sources]
    )

    existing = (
        await db.execute(
            select(NotebookOverview).where(NotebookOverview.notebook_id == notebook_id)
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = NotebookOverview(notebook_id=notebook_id)
        db.add(existing)
    existing.summary = summary
    existing.suggested_questions = questions
    existing.title_suggestions = title_suggestions
    existing.source_count = len(sources)
    existing.generated_at = datetime.now(UTC)
    await db.commit()
    logger.info(
        "overview generated",
        notebook_id=str(notebook_id),
        sources=len(sources),
        batches=len(lines) if share_tokens is not None else 0,
    )
    return existing


async def compute_staleness(
    db: AsyncSession, notebook_id: uuid.UUID, overview: NotebookOverview | None
) -> bool:
    """stale = 개요 없음 / ready 소스 수 변화 / 개요 생성 후 소스 갱신."""
    count = (
        await db.execute(
            select(func.count())
            .select_from(Source)
            .where(Source.notebook_id == notebook_id, Source.status == "ready")
        )
    ).scalar_one()
    if overview is None or overview.generated_at is None:
        return count > 0
    if count != overview.source_count:
        return True
    latest = (
        await db.execute(
            select(func.max(Source.updated_at)).where(
                Source.notebook_id == notebook_id, Source.status == "ready"
            )
        )
    ).scalar_one_or_none()
    return latest is not None and latest > overview.generated_at
