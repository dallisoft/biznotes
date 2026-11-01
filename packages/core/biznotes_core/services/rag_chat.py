"""RAG 채팅 — 검색 → 컨텍스트 조립(인용 번호) → 프롬프트 구성."""

import math
import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.models import ChatMessage, Source
from biznotes_core.services.untrusted import (
    UNTRUSTED_DATA_RULE,
    escape_untrusted_tags,
    format_source_excerpts,
)

SYSTEM_PROMPT = (
    """당신은 사용자의 소스 문서에 근거해 답하는 리서치 어시스턴트입니다.

"""
    # 발췌는 비신뢰 데이터 블록으로 넣는다(HWP-05). `{context}` 외의 중괄호가 없어야 한다.
    + UNTRUSTED_DATA_RULE
    + """

규칙:
1. 아래 제공된 소스 발췌만 근거로 답하세요.
   소스에 없는 내용은 반드시 "소스에서 찾을 수 없습니다"라고 명시하고,
   추측이나 일반 지식으로 채우지 마세요. 근거 부족은 실패가 아니라 정확한 답변입니다.
2. 근거 발췌 번호를 문장 끝에 [n] 형식으로 인용하세요.
   복수 인용은 [1][3]처럼 나란히 표기합니다.
3. 사용자의 언어로 답하세요 (한국어 질문 → 한국어 답변).
4. 답변은 명확하고 구조적으로 작성하세요.

소스 발췌:
{context}"""
)

INSUFFICIENT_CONTEXT_SYSTEM_NOTE = """

주의: 검색된 소스 발췌의 관련성 점수가 낮습니다. 발췌가 질문과 무관하거나
부분적으로만 관련될 가능성이 높습니다. 발췌에 명확한 근거가 없으면 절대
일반 지식으로 답하지 말고, "등록된 소스에서 충분한 근거를 찾지 못했습니다"라고
답하세요."""


@dataclass
class Citation:
    n: int
    source_id: str
    chunk_id: str
    quote: str
    source_title: str
    verified: bool = True  # 답변 후 인용-청크 매칭 검증 결과

    def to_dict(self) -> dict:
        return {
            "n": self.n,
            "source_id": self.source_id,
            "chunk_id": self.chunk_id,
            "quote": self.quote,
            "source_title": self.source_title,
            "verified": self.verified,
        }


@dataclass
class SourceCoverage:
    """검색 범위 중 근거가 나온 소스(#446) — 제품이 「N개 중 M개 반영」을 표시한다."""

    scope_count: int  # 범위 소스 수(`failed`와 다른 노트북 소스는 범위가 아니다)
    hit_sources: list[str]  # 근거가 나온 소스 id — 근거 순서
    missing: list[str]  # 범위에 있지만 근거가 없는 소스 id — `(created_at, id)` 순서

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_count": self.scope_count,
            "hit_sources": self.hit_sources,
            "missing": self.missing,
        }


@dataclass
class RetrievalResult:
    citations: list[Citation]
    insufficient_context: bool  # [Phase 18b] top-1 점수가 임계 미달 — "근거 부족" 판정
    # 커버리지 옵션(`per_source_limit`·`coverage`)을 줬을 때만 채운다(#446)
    coverage: SourceCoverage | None = None


async def _without_failed_sources(
    db: AsyncSession, notebook_id: uuid.UUID, source_ids: list[uuid.UUID] | None
) -> list[uuid.UUID] | None:
    """검색 범위에서 `failed` 소스를 뺀다 (#8). 반환값의 의미는 인자와 같다.

    `failed` 소스도 청크(벡터)가 남아 있을 수 있다(#6). UI에서는 고를 수 없는
    소스라 근거에서도 뺀다. 처리 중 상태는 뺄 이유가 없다 — 첫 인제스천이면 아직
    벡터가 없고, 재인덱싱이면 새 벡터가 올라가기 전까지 이전 벡터가 유효하다.
    범위에 `failed` 소스가 없으면 범위를 그대로 돌려준다 — 흔한 경우에 벡터 검색
    필터를 소스 목록으로 키우지 않는다.
    """
    q = select(Source.id).where(Source.notebook_id == notebook_id, Source.status == "failed")
    if source_ids is not None:
        q = q.where(Source.id.in_(source_ids))
    failed = set((await db.execute(q)).scalars().all())
    if not failed:
        return source_ids
    if source_ids is not None:
        return [s for s in source_ids if s not in failed]
    rest = select(Source.id).where(Source.notebook_id == notebook_id, Source.status != "failed")
    return list((await db.execute(rest)).scalars().all())


# 발췌 수 허용 범위 — 어드민 `RagSettings.top_k`(Field(ge=1, le=30))와 같은 범위여야
# 설정으로 넣을 수 없는 값이 도구 인자·노드 정의로 들어오지 않는다(#45).
_TOP_K_MIN = 1
_TOP_K_MAX = 30


def top_k_from_settings(rag_cfg: dict[str, Any], requested: Any) -> int:
    """발췌 수 해석 규칙 — 생략(None)이면 어드민 RAG 설정, 값은 1~30으로 제한(#45).

    react 도구 인자·워크플로우 노드·스킬 매니페스트가 모두 이 규칙을 쓴다. 경로마다 6을
    기본값으로 박으면 어드민이 설정을 바꿔도 경로별 발췌 수가 갈린다. 0은 결과를 비우고
    음수는 슬라이스로 뒤를 자르며 큰 값은 검색 limit으로 그대로 넘어가므로 범위로 막는다.
    """
    if requested is None:
        requested = rag_cfg.get("top_k", 6)
    return min(max(int(requested), _TOP_K_MIN), _TOP_K_MAX)


async def resolve_top_k(db: AsyncSession, requested: Any) -> int:
    """`top_k_from_settings`의 DB 판 — 어드민 RAG 설정을 읽어 해석한다(#45)."""
    from biznotes_core.services.settings_store import get_setting

    return top_k_from_settings(await get_setting(db, "rag"), requested)


# 검색 순서(#446): ranked는 범위 전체에서 점수 상위 `top_k`개, per_source는 범위의 소스마다
# 관련도가 가장 높은 청크 `per_source_limit`개(기본 1)를 보장한다.
RagCoverage = Literal["ranked", "per_source"]
_RAG_COVERAGES: tuple[RagCoverage, ...] = ("ranked", "per_source")
# 소스당 청크 수 허용 범위 — per_source 결과는 범위 소스 수 × 이 값까지 커진다
_PER_SOURCE_LIMIT_MIN = 1
_PER_SOURCE_LIMIT_MAX = 5
# per_source가 한 번에 묶는 소스 수 상한 — rag_search 노드 `source_ids` 상한(#362)과 같다
_PER_SOURCE_GROUPS_MAX = 500


def coverage_from(value: Any) -> RagCoverage | None:
    """`coverage` 인자 해석 — 생략(None)은 None, 그 밖에는 `ranked`·`per_source`만 받는다."""
    if value is None:
        return None
    for known in _RAG_COVERAGES:
        if value == known:
            return known
    raise ValueError(f"coverage는 ranked 또는 per_source여야 합니다: {value!r}")


def per_source_limit_from(value: Any) -> int | None:
    """`per_source_limit` 인자 해석 — 생략(None)은 None, 값은 1~5로 제한한다."""
    if value is None:
        return None
    return min(max(int(value), _PER_SOURCE_LIMIT_MIN), _PER_SOURCE_LIMIT_MAX)


async def _coverage_scope(
    db: AsyncSession, notebook_id: uuid.UUID, searchable: list[uuid.UUID] | None
) -> list[uuid.UUID]:
    """커버리지를 셀 범위 소스(#446) — 이 노트북의 `failed`가 아닌 소스, `(created_at, id)` 순서.

    `searchable`이 목록이면 그중 이 노트북 소스만 남긴다 — 다른 노트북 id는 범위가 아니다.
    청크가 아직 없는 소스(처리 중)도 범위다 — `missing`으로 보고된다.
    """
    q = select(Source.id).where(Source.notebook_id == notebook_id, Source.status != "failed")
    if searchable is not None:
        q = q.where(Source.id.in_(searchable))
    q = q.order_by(Source.created_at, Source.id)
    return list((await db.execute(q)).scalars().all())


def _cap_per_source(points: list[Any], limit: int) -> list[Any]:
    """순서를 지키며 소스마다 앞의 `limit`개만 남긴다 — 공간을 합친 결과에도 상한을 지킨다."""
    seen: dict[str, int] = {}
    kept: list[Any] = []
    for p in points:
        sid = str((p.payload or {}).get("source_id"))
        if seen.get(sid, 0) < limit:
            seen[sid] = seen.get(sid, 0) + 1
            kept.append(p)
    return kept


def _coverage_of(scope: list[uuid.UUID], citations: list[Citation]) -> SourceCoverage:
    hits = list(dict.fromkeys(c.source_id for c in citations))
    hit_set = set(hits)
    return SourceCoverage(
        scope_count=len(scope),
        hit_sources=hits,
        missing=[str(s) for s in scope if str(s) not in hit_set],
    )


async def retrieve_context(
    db: AsyncSession,
    notebook_id: uuid.UUID,
    query: str,
    source_ids: list[uuid.UUID] | None,
    top_k: int | None = None,
    *,
    per_source_limit: int | None = None,
    coverage: RagCoverage | None = None,
) -> RetrievalResult:
    """질의와 관련된 발췌를 찾아 [1]부터 번호를 매긴다.

    source_ids: `None`은 노트북 전체, `[]`는 선택한 소스 없음(검색하지 않는다),
    목록은 그 소스만이다 (#8·#13). 어느 경우든 `failed` 소스는 근거에서 뺀다.

    top_k: `None`이면 어드민 RAG 설정을 따르고 값은 1~30으로 제한한다(#45). `ranked`에만 걸린다.

    소스 커버리지(#446) — 둘 다 생략하면 지금과 같다.
    per_source_limit: 소스당 최대 청크 수(1~5). `per_source`에서는 소스마다 보장할 수(기본 1)다.
    coverage: `ranked`(기본)는 점수 상위 `top_k`개, `per_source`는 범위의 소스마다(최대 500개)
    관련도가 가장 높은 청크를 담는다 — 결과는 소스 수 × `per_source_limit`까지 커진다.
    옵션을 주면 결과에 `coverage`(범위 소스 수·근거가 나온 소스·빠진 소스)를 채운다.
    `insufficient_context`는 어느 경우든 상위 1개 점수로 판정한다.
    """
    from biznotes_core.services.embedding_spaces import search_spaces, spaces_in_scope
    from biznotes_core.services.settings_store import get_setting

    coverage = coverage_from(coverage)
    per_source_limit = per_source_limit_from(per_source_limit)
    with_coverage = coverage is not None or per_source_limit is not None
    per_source = coverage == "per_source"
    no_scope = RetrievalResult(
        citations=[],
        insufficient_context=True,
        coverage=SourceCoverage(scope_count=0, hit_sources=[], missing=[])
        if with_coverage
        else None,
    )

    searchable = (
        await _without_failed_sources(db, notebook_id, source_ids)
        if source_ids is None or source_ids
        else []
    )
    if searchable is not None and not searchable:
        return no_scope
    scope = await _coverage_scope(db, notebook_id, searchable) if with_coverage else []
    if per_source and not scope:
        return no_scope

    rag_cfg = await get_setting(db, "rag")
    top_k = top_k_from_settings(rag_cfg, top_k)
    rerank_enabled = bool(rag_cfg.get("rerank_enabled", False))
    # 리랭크 후보는 최소한 top_k만큼 가져온다 — `rerank_candidates`(최소 5)가 설정 `top_k`
    # (최대 30)보다 작으면 리랭크를 켠 것만으로 발췌 수가 조용히 줄어든다(#45).
    fetch_limit = max(int(rag_cfg.get("rerank_candidates", 20)), top_k) if rerank_enabled else top_k
    # 소스별 묶음 검색(#446): per_source는 범위 소스 수만큼 묶음을, 소스당 상한만 준 ranked는
    # 후보 수만큼 묶음을 가져온다. 묶음 안 청크 수가 소스당 상한이다.
    group_size = (per_source_limit or 1) if per_source else per_source_limit
    limit = min(len(scope), _PER_SOURCE_GROUPS_MAX) if per_source else fetch_limit
    # 생략하면 넘기지 않는다 — `search_spaces`를 대역으로 바꾸는 테스트의 호출 모양이 그대로다
    grouping: dict[str, Any] = {"group_size": group_size} if group_size is not None else {}
    # 소스가 속한 임베딩 공간마다 그 모델로 질의한다(#7) — 공간이 섞이지 않는다
    spaces = await spaces_in_scope(db, [notebook_id], searchable)

    points = await search_spaces(
        db,
        spaces,
        query,
        notebook_id=notebook_id,
        source_ids=searchable,
        limit=limit,
        **grouping,
    )
    if group_size is not None:
        points = _cap_per_source(points, group_size)
        if not per_source:
            points = points[:fetch_limit]  # 리랭크 후보 수는 상한이 없을 때와 같다
    top_reranked_score: float | None = None
    if rerank_enabled and points:
        # [codex D1·D4] 후보 수가 top_k 이하라도 리랭킹이 켜져 있으면 항상 재점수를 매겨야
        # insufficient_context_threshold_rerank가 hybrid 점수가 아닌 리랭커 점수와 비교된다.
        # [#209] 해석은 후보가 있을 때만 한다 — 리랭크할 것이 없는 검색까지 실패시키지 않는다.
        # 해석·호출 실패는 `RerankUnavailableError`로 올린다(하이브리드 점수로 넘어가면 임계값
        # 판정이 뒤집힌다). 점수는 백엔드와 무관하게 [0,1]이다(`rerank_relevance`).
        from biznotes_core.services.rerank import (
            DEFAULT_RERANK_TIMEOUT_SECONDS,
            rerank_relevance,
            resolve_reranker,
        )
        from biznotes_core.services.settings_store import DEFAULTS

        target = await resolve_reranker(
            db,
            str(rag_cfg.get("rerank_model", DEFAULTS["rag"]["rerank_model"])),
            rerank_timeout=float(
                rag_cfg.get("rerank_timeout_seconds", DEFAULT_RERANK_TIMEOUT_SECONDS)
            ),
        )
        # per_source는 소스를 빼지 않으므로 재정렬하지 않는다 — 앞쪽 후보만 재점수를 매겨
        # 상위 1개 점수(임계값 판정)를 리랭커 점수로 맞춘다(#446)
        candidates = points[:fetch_limit] if per_source else points
        texts = [p.payload.get("text", "") if p.payload else "" for p in candidates]
        scores = await anyio.to_thread.run_sync(rerank_relevance, target, query, texts)
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
        if not per_source:
            points = [points[i] for i in order]
        top_reranked_score = scores[order[0]] if order else None
    if not per_source:
        points = points[:top_k]
    if not points:
        return RetrievalResult(
            citations=[],
            insufficient_context=True,
            coverage=_coverage_of(scope, []) if with_coverage else None,
        )

    # [Phase 18b] 검색 모드(리랭킹 on/off)별로 점수 분포가 달라 임계값을 분리한다(CR-10).
    threshold_key = (
        "insufficient_context_threshold_rerank"
        if rerank_enabled
        else "insufficient_context_threshold_base"
    )
    threshold = float(rag_cfg.get(threshold_key, 0.5))
    if top_reranked_score is not None:
        top_score = top_reranked_score
    else:
        top_score = points[0].score if points[0].score is not None else 0.0
    insufficient = top_score < threshold

    # 소스 제목 일괄 조회
    src_ids = {p.payload["source_id"] for p in points if p.payload}
    rows = await db.execute(
        select(Source.id, Source.title).where(Source.id.in_([uuid.UUID(s) for s in src_ids]))
    )
    title_map = {str(sid): title for sid, title in rows.tuples().all()}

    citations: list[Citation] = []
    for i, p in enumerate(points, start=1):
        if not p.payload:
            continue
        citations.append(
            Citation(
                n=i,
                source_id=p.payload["source_id"],
                chunk_id=str(p.id),
                quote=p.payload.get("text", ""),
                source_title=title_map.get(p.payload["source_id"], "알 수 없는 소스"),
            )
        )
    return RetrievalResult(
        citations=citations,
        insufficient_context=insufficient,
        coverage=_coverage_of(scope, citations) if with_coverage else None,
    )


def build_messages(
    query: str,
    citations: list[Citation],
    history: list[ChatMessage],
    max_history: int = 6,
    insufficient_context: bool = False,
) -> list[dict[str, str]]:
    context = format_source_excerpts(citations, 1500) or "(검색된 발췌 없음)"

    system_content = SYSTEM_PROMPT.format(context=context)
    if insufficient_context:
        system_content += INSUFFICIENT_CONTEXT_SYSTEM_NOTE

    messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
    for m in history[-max_history:]:
        # 이전 답변에 블록 태그가 섞여도 블록 밖에 짝 없는 태그가 생기지 않게 한다(HWP-05)
        messages.append({"role": m.role, "content": escape_untrusted_tags(m.content[:4000])})
    messages.append({"role": "user", "content": query})
    return messages


def filter_cited(citations: list[Citation], answer: str) -> list[Citation]:
    """답변에 실제 인용된 번호만 남긴다 (미인용 발췌는 제외)."""
    used = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
    cited = [c for c in citations if c.n in used]
    return cited if cited else citations  # 인용 마커가 없으면 전체 반환(근거 노출)


# 하위호환 기본값 — DB 설정이 없을 때(예: 스크립트/테스트) 사용.
# 실제 런타임 임계값은 system_settings["rag"](settings_store.DEFAULTS)에서 온다.
VERIFY_OVERLAP_THRESHOLD = 0.25
VERIFY_EMBEDDING_THRESHOLD = 0.55
_TOKEN_RE = re.compile(r"[A-Za-z0-9가-힣]{2,}")


def _content_tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text)}


def _extract_marker_sentences(answer: str) -> dict[int, str]:
    """[n] 마커 기준으로 답변을 문장 단위로 분해: 마커 직전 텍스트가 근거 문장."""
    segments = re.split(r"(\[\d+\])", answer)
    sentence_by_n: dict[int, str] = {}
    for i, seg in enumerate(segments):
        m = re.match(r"^\[(\d+)\]$", seg)
        if not m:
            continue
        n = int(m.group(1))
        # 직전 비마커 세그먼트의 마지막 문장을 근거 문장으로 본다
        prev = next(
            (segments[j] for j in range(i - 1, -1, -1) if not re.match(r"^\[\d+\]$", segments[j])),
            "",
        )
        last_sentence = re.split(r"(?<=[.!?다요])\s+", prev.strip())[-1] if prev.strip() else ""
        sentence_by_n[n] = (sentence_by_n.get(n, "") + " " + last_sentence).strip()
    return sentence_by_n


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def verify_citations_sync(
    citations: list[Citation],
    answer: str,
    lexical_threshold: float = VERIFY_OVERLAP_THRESHOLD,
    embedding_threshold: float = VERIFY_EMBEDDING_THRESHOLD,
) -> list[Citation]:
    """인용-청크 매칭 검증 (환각 대응) — 각 [n] 마커가 붙은 문장이 해당 청크와
    의미적으로 일치하는지 2단계로 확인하고 Citation.verified를 채운다.

    1차: 어휘 겹침(저비용) — 통과하면 검증 완료.
    2차: 1차 미달 시에만 임베딩 코사인 유사도로 패러프레이즈 답변을 구제(18a).
    임베딩 호출은 CPU 바운드라 호출부에서 스레드로 오프로드해야 한다(async 래퍼 참고).
    """
    sentence_by_n = _extract_marker_sentences(answer)

    pending_embedding: list[tuple[Citation, str]] = []
    for c in citations:
        sentence = sentence_by_n.get(c.n)
        if not sentence:
            continue  # 마커가 없는 인용(전체 폴백 노출)은 검증 대상 아님 — verified 유지
        sent_tokens = _content_tokens(sentence)
        if not sent_tokens:
            continue
        chunk_tokens = _content_tokens(c.quote)
        overlap = len(sent_tokens & chunk_tokens) / len(sent_tokens)
        if overlap >= lexical_threshold:
            c.verified = True
        else:
            pending_embedding.append((c, sentence))

    if pending_embedding:
        from biznotes_core.services.ingestion.embedder import embed_dense

        texts = [sentence for _, sentence in pending_embedding] + [
            c.quote for c, _ in pending_embedding
        ]
        vectors = embed_dense(texts)
        n = len(pending_embedding)
        for i, (c, _sentence) in enumerate(pending_embedding):
            sim = _cosine(vectors[i], vectors[n + i])
            c.verified = sim >= embedding_threshold

    return citations


async def verify_citations(
    db: AsyncSession, citations: list[Citation], answer: str
) -> list[Citation]:
    """verify_citations_sync의 async 래퍼 — DB 설정 임계값 로드 + 스레드 오프로드.

    스트리밍 지연 없이 답변 완료 후 한 번 실행되며, 결과를 citation_verified_total
    카운터로 상시 관측한다(18a).
    """
    from biznotes_core.core.metrics import citation_verified_total
    from biznotes_core.services.settings_store import get_setting

    rag_cfg = await get_setting(db, "rag")
    lexical_threshold = float(rag_cfg.get("verify_lexical_threshold", VERIFY_OVERLAP_THRESHOLD))
    embedding_threshold = float(
        rag_cfg.get("verify_embedding_threshold", VERIFY_EMBEDDING_THRESHOLD)
    )

    result = await anyio.to_thread.run_sync(
        verify_citations_sync, citations, answer, lexical_threshold, embedding_threshold
    )
    for c in result:
        citation_verified_total.labels(result="verified" if c.verified else "unverified").inc()
    return result
