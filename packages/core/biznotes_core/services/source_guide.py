"""소스 가이드(요약 + 키워드) 생성 서비스.

소스 상세 인라인 뷰에서 온디맨드로 호출되어 단일 소스의 요약·키워드를
LLM으로 생성하고 sources.summary / sources.keywords에 캐시한다.
LLM 응답은 JSON을 우선 파싱하고, 실패하면 발췌 원문/제목 기반으로 폴백한다
(mock 프로바이더는 JSON을 생성하지 않으므로 폴백 경로가 기본 동작).
"""

import json
import re
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.models import Source, SourceChunk
from biznotes_core.services.gateway.llm import (
    GatewayError,
    StreamResult,
    complete_stream_with_fallback,
)
from biznotes_core.services.untrusted import UNTRUSTED_DATA_RULE, wrap_numbered_excerpt

logger = structlog.get_logger()

MAX_CONTEXT_CHUNKS = 8
MAX_CHUNK_CHARS = 1000
MAX_KEYWORDS = 8

GUIDE_PROMPT = (
    "당신은 단일 문서를 분석하는 어시스턴트입니다.\n"
    "아래 [n] 발췌는 하나의 소스 문서에서 뽑았고, 발췌마다 untrusted_data 블록에 들어 있습니다.\n"
    + UNTRUSTED_DATA_RULE
    + "\n\n"
    "다음 JSON 형식으로만 답하세요 (다른 텍스트 금지):\n"
    '{"summary": "<문서 전체를 아우르는 3~5문장 한국어 요약>", '
    '"keywords": ["<핵심 키워드/주제>", "..."]}\n\n'
    "키워드는 5~8개, 짧은 한국어 명사구로 작성하세요."
)


async def _sample_chunks(db: AsyncSession, source_id: Any) -> list[str]:
    """소스 선두 청크를 순서대로 샘플링."""
    rows = await db.execute(
        select(SourceChunk.text)
        .where(SourceChunk.source_id == source_id)
        .order_by(SourceChunk.seq)
        .limit(MAX_CONTEXT_CHUNKS)
    )
    return [text[:MAX_CHUNK_CHARS] for text in rows.scalars().all()]


def _extract_json_string(key: str, text: str) -> str | None:
    """truncate/말줄임된 JSON에서도 단일 문자열 필드를 추출한다."""
    m = re.search(rf'"{key}"\s*:\s*"((?:\\.|[^"\\])*)"', text, re.S)
    if not m:
        return None
    try:
        return json.loads(f'"{m.group(1)}"').strip()
    except json.JSONDecodeError:
        return m.group(1).strip()


def _extract_json_keywords(text: str) -> list[str]:
    """keywords 배열에서 (배열이 닫히지 않았더라도) 완결된 문자열만 모은다."""
    arr = re.search(r'"keywords"\s*:\s*\[(.*)', text, re.S)
    if not arr:
        return []
    keywords: list[str] = []
    for m in re.finditer(r'"((?:\\.|[^"\\])*)"', arr.group(1)):
        try:
            kw = json.loads(f'"{m.group(1)}"').strip()
        except json.JSONDecodeError:
            kw = m.group(1).strip()
        if kw:
            keywords.append(kw)
    return keywords


def _parse_guide_response(raw: str, fallback_summary: str) -> tuple[str, list[str]]:
    """JSON 응답 파싱.

    1) 완전한 JSON이면 정상 파싱
    2) truncate된 JSON이면 정규식으로 summary/keywords를 부분 추출
    3) 그래도 실패하면 발췌 원문을 요약으로, 키워드는 빈 목록
    """
    text = raw.strip()

    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            data: dict[str, Any] = json.loads(match.group(0))
            summary = str(data.get("summary", "")).strip()
            keywords = [str(k).strip() for k in data.get("keywords", []) if str(k).strip()]
            if summary:
                return summary, keywords[:MAX_KEYWORDS]
        except (json.JSONDecodeError, AttributeError):
            pass

    if '"summary"' in text:
        partial = _extract_json_string("summary", text)
        if partial:
            return partial, _extract_json_keywords(text)[:MAX_KEYWORDS]

    return (text or fallback_summary), []


async def generate_source_guide(db: AsyncSession, source: Source) -> Source:
    """단일 소스의 요약 + 키워드를 생성해 캐시(upsert) 후 source 반환.

    청크가 없으면 캐시 없이 그대로 반환한다(인제스천 미완료 등).

    캐시 쓰기는 커밋 시점에 행이 여전히 `ready`이고 LLM 호출 전에 본 `updated_at`과 같을
    때만 나간다. 긴 LLM 호출 사이에 재인제스트가 시작되거나 끝났다면 캐시하지 않고 이번
    응답에만 값을 싣는다 — parsing heartbeat가 `updated_at`을 소유권 토큰으로 쓰고, 옛
    청크로 만든 요약이 새 본문의 캐시로 박히면 안 된다(계획 A §5.1·§5.2).
    """
    seen_at = source.updated_at
    chunks = await _sample_chunks(db, source.id)
    if not chunks:
        return source

    # 발췌는 비신뢰 데이터 블록이다(HWP-05) — 결과가 저장돼 사용자에게 보인다.
    context = "\n\n".join(
        wrap_numbered_excerpt(i + 1, "", text) for i, text in enumerate(chunks)
    )
    messages = [
        {"role": "system", "content": GUIDE_PROMPT},
        {"role": "user", "content": f"{context}\n\n위 소스를 요약하고 키워드를 추출하세요."},
    ]

    try:
        result = StreamResult()
        stream = await complete_stream_with_fallback(db, None, messages, result, temperature=0.3)
        raw = "".join([delta async for delta in stream])
    except GatewayError as e:
        logger.warning("source guide skipped", source_id=str(source.id), error=str(e))
        return source

    fallback = " ".join(chunks[0].split())[:300]
    summary, keywords = _parse_guide_response(raw, fallback)

    res = await db.execute(
        update(Source)
        .where(Source.id == source.id, Source.status == "ready", Source.updated_at == seen_at)
        .values(summary=summary, keywords=keywords)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if res.rowcount == 1:  # type: ignore[attr-defined]
        await db.refresh(source)
        logger.info("source guide generated", source_id=str(source.id), keywords=len(keywords))
    else:
        # 세션에서 빼 이후 어떤 flush도 이 행을 쓰지 못하게 한 뒤 응답에만 값을 싣는다
        db.expunge(source)
        source.summary, source.keywords = summary, keywords
        logger.info("source guide cache skipped — 소스가 재인제스트 중", source_id=str(source.id))
    return source
