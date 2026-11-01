"""청크 벡터 검색 — PostgreSQL pgvector, dense + sparse(BM25) RRF 융합.

벡터는 청크 행에 있다(`source_chunks.embedding`·`sparse`). 색인 교체는 청크 행 교체와 같은
트랜잭션이라 벡터 저장소와 DB가 어긋날 일(고아 벡터)이 없고, 소스를 지우면 CASCADE로 함께
사라진다.

**임베딩 공간(#7)**: 소스마다 자기 청크를 만든 공간 키를 `sources.embedding_model`에 기록한다
(NULL은 로컬 기본 모델). 공간마다 차원이 달라 `embedding` 열은 차원을 고정하지 않는다. 검색은
언제나 공간 하나로 걸러 낸 행(그 공간·그 차원)에서만 거리를 잰다 — 서로 다른 공간의 벡터를
비교하지 않는다.

**인덱스 없는 정확 탐색**: 검색은 항상 노트북(필요하면 소스)으로 먼저 걸러져 후보가 노트북의
청크 수다. 데이터가 커지면 활성 공간의 차원을 고정한 HNSW를 더한다(계획 §5 v0.2 후보).

**점수는 Qdrant RRF와 같다**: dense(코사인)·sparse(내적) 후보 목록을 각각 `limit × 3`개 뽑고,
청크마다 `Σ 1/(순위 + 2)`(순위는 0부터, k=2)를 더한다. 두 목록 모두 1위면 1.0, 한 목록에서만
1위면 0.5다 — `insufficient_context_threshold_*`가 이 눈금을 전제한다. sparse 후보는 질의와
겹치는 토큰이 있는 청크만이다(내적 > 0). 동률은 dense 순위 → sparse 순위 → 청크 id로 정한다.
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.models.vector_types import dense_literal, sparse_literal
from biznotes_core.services.ingestion.embedder import SparseVector

#: Qdrant RRF의 순위 상수 — 점수 = Σ 1/(순위 + k), 순위는 0부터
RRF_K = 2
#: 후보 목록 길이 = 결과 수 × 이 값(Qdrant prefetch `limit * 3`과 같다)
CANDIDATE_FACTOR = 3


@dataclass
class ScoredChunk:
    """검색 결과 한 건 — 인용의 `chunk_id`는 `id`(청크 행 id)다."""

    id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


# 범위(노트북·소스·공간)를 먼저 고정한다. MATERIALIZED로 두는 이유: 거리 연산(`<=>`)이 다른
# 공간(다른 차원)의 행에서 평가되면 오류다 — 범위 밖 행은 아예 거리 계산에 닿지 않게 한다.
# 순위 계산에 필요한 열만 싣고, 결과 행의 본문은 마지막에 기본 키로 가져온다 — CTE에는 통계가
# 없어 CTE끼리 다시 조인하면 플래너가 중첩 루프를 고른다(1만 청크 묶음 검색 2.9초 실측).
_SCOPED = """
scoped AS MATERIALIZED (
    SELECT c.id, c.source_id, c.embedding, c.sparse
    FROM source_chunks c
    JOIN sources s ON s.id = c.source_id
    WHERE s.notebook_id = ANY(CAST(:notebook_ids AS uuid[]))
      {source_filter}
      AND coalesce(s.embedding_model, :local_space) = :space
      AND c.embedding IS NOT NULL
      AND vector_dims(c.embedding) = :dim
)"""

_RANKED = """
dense AS (
    SELECT id, source_id, row_number() OVER (ORDER BY d, id) - 1 AS r
    FROM (
        SELECT id, source_id, embedding <=> CAST(:dense AS vector) AS d FROM scoped
        ORDER BY d, id {candidate_limit}
    ) top
),
sparse AS (
    SELECT id, source_id, row_number() OVER (ORDER BY ip, id) - 1 AS r
    FROM (
        SELECT id, source_id, sparse <#> CAST(:sparse AS sparsevec) AS ip FROM scoped
        WHERE sparse IS NOT NULL AND (sparse <#> CAST(:sparse AS sparsevec)) < 0
        ORDER BY ip, id {candidate_limit}
    ) top
),
fused AS (
    SELECT id,
           (array_agg(source_id))[1] AS source_id,
           sum(1.0 / CAST(r + :rrf_k AS float8)) AS score,
           min(CASE WHEN src = 0 THEN r END) AS dense_r,
           min(CASE WHEN src = 1 THEN r END) AS sparse_r
    FROM (
        SELECT id, source_id, r, 0 AS src FROM dense
        UNION ALL
        SELECT id, source_id, r, 1 AS src FROM sparse
    ) both_lists
    GROUP BY id
)"""

# 결과 행의 본문 — 기본 키로 가져온다(상위 결과만이라 색인 조회 몇 번이다)
_ROWS = """
SELECT f.id, f.score, c.source_id, s.notebook_id, c.seq, c.text, c.meta
FROM {ranked} f
JOIN source_chunks c ON c.id = f.id
JOIN sources s ON s.id = c.source_id"""

_ORDER = "f.score DESC, f.dense_r ASC NULLS LAST, f.sparse_r ASC NULLS LAST, f.id"


def _payload(row: Any) -> dict[str, Any]:
    meta = json.loads(row.meta) if isinstance(row.meta, str) else row.meta
    meta = meta if isinstance(meta, dict) else {}
    return {
        **meta,
        "notebook_id": str(row.notebook_id),
        "source_id": str(row.source_id),
        "seq": row.seq,
        "text": row.text,
    }


def _params(
    notebook_ids: Sequence[uuid.UUID],
    dense_query: Sequence[float],
    sparse_query: SparseVector,
    source_ids: Sequence[uuid.UUID] | None,
    space: str,
    local_space: str,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "notebook_ids": list(notebook_ids),
        "space": space,
        "local_space": local_space,
        "dim": len(dense_query),
        "dense": dense_literal(dense_query),
        "sparse": sparse_literal(sparse_query),
        "rrf_k": RRF_K,
    }
    if source_ids is not None:
        params["source_ids"] = list(source_ids)
    return params


def _source_filter(source_ids: Sequence[uuid.UUID] | None) -> str:
    return "" if source_ids is None else "AND c.source_id = ANY(CAST(:source_ids AS uuid[]))"


async def _ranked(
    db: AsyncSession,
    notebook_ids: Sequence[uuid.UUID],
    dense_query: Sequence[float],
    sparse_query: SparseVector,
    *,
    source_ids: Sequence[uuid.UUID] | None,
    limit: int,
    space: str,
    local_space: str,
) -> list[ScoredChunk]:
    sql = (
        "WITH"
        + _SCOPED.format(source_filter=_source_filter(source_ids))
        + ","
        + _RANKED.format(candidate_limit="LIMIT :candidates")
        + f""",
best AS (SELECT * FROM fused f ORDER BY {_ORDER} LIMIT :limit)"""
        + _ROWS.format(ranked="best")
        + f"\nORDER BY {_ORDER}"
    )
    params = _params(notebook_ids, dense_query, sparse_query, source_ids, space, local_space)
    params |= {"candidates": limit * CANDIDATE_FACTOR, "limit": limit}
    rows = (await db.execute(text(sql), params)).all()
    return [ScoredChunk(str(r.id), float(r.score), _payload(r)) for r in rows]


async def hybrid_search(
    db: AsyncSession,
    notebook_id: uuid.UUID,
    dense_query: Sequence[float],
    sparse_query: SparseVector,
    source_ids: list[uuid.UUID] | None = None,
    limit: int = 8,
    *,
    space: str,
    local_space: str,
) -> list[ScoredChunk]:
    """노트북 하나의 dense + sparse RRF 검색.

    source_ids: `None`은 노트북 전체, `[]`는 선택한 소스 없음(빈 결과)이다 (#8).
    """
    if source_ids is not None and not source_ids:
        return []
    return await _ranked(
        db,
        [notebook_id],
        dense_query,
        sparse_query,
        source_ids=source_ids,
        limit=limit,
        space=space,
        local_space=local_space,
    )


async def hybrid_search_multi(
    db: AsyncSession,
    notebook_ids: list[uuid.UUID],
    dense_query: Sequence[float],
    sparse_query: SparseVector,
    limit: int = 8,
    *,
    space: str,
    local_space: str,
) -> list[ScoredChunk]:
    """여러 노트북에 걸친 RRF 검색 — 전체 검색(시맨틱 모드)용.

    권한 필터는 호출부가 접근 가능한 notebook_ids만 넘기는 것으로 보장한다.
    """
    if not notebook_ids:
        return []
    return await _ranked(
        db,
        notebook_ids,
        dense_query,
        sparse_query,
        source_ids=None,
        limit=limit,
        space=space,
        local_space=local_space,
    )


async def hybrid_search_groups(
    db: AsyncSession,
    notebook_id: uuid.UUID,
    dense_query: Sequence[float],
    sparse_query: SparseVector,
    *,
    group_size: int,
    limit: int,
    source_ids: list[uuid.UUID] | None = None,
    space: str,
    local_space: str,
) -> list[ScoredChunk]:
    """소스별 묶음 RRF 검색(#446) — 소스 `limit`개까지, 소스마다 청크 `group_size`개.

    묶음을 채우려고 후보 목록을 자르지 않는다(범위 전체에서 순위를 매긴다) — 범위의 소스마다
    가장 관련 있는 청크가 들어온다. 묶음 순서는 묶음의 최고 점수다. 결과는 묶음을 풀어 점수
    내림차순으로 돌려준다. 필터는 `hybrid_search`와 같다(`[]`는 빈 결과).
    """
    if source_ids is not None and not source_ids:
        return []
    sql = (
        "WITH"
        + _SCOPED.format(source_filter=_source_filter(source_ids))
        + ","
        + _RANKED.format(candidate_limit="")
        + f""",
grouped AS (
    SELECT f.*, row_number() OVER (PARTITION BY f.source_id ORDER BY {_ORDER}) AS gr
    FROM fused f
),
top_groups AS (
    SELECT source_id FROM grouped WHERE gr = 1
    ORDER BY score DESC, dense_r ASC NULLS LAST, sparse_r ASC NULLS LAST, id
    LIMIT :limit
),
picked AS (
    SELECT g.* FROM grouped g JOIN top_groups t ON t.source_id = g.source_id
    WHERE g.gr <= :group_size
)"""
        + _ROWS.format(ranked="picked")
        + f"\nORDER BY {_ORDER}"
    )
    params = _params([notebook_id], dense_query, sparse_query, source_ids, space, local_space)
    params |= {"limit": limit, "group_size": group_size}
    rows = (await db.execute(text(sql), params)).all()
    return [ScoredChunk(str(r.id), float(r.score), _payload(r)) for r in rows]


async def space_has_chunks(db: AsyncSession, space: str) -> bool:
    """이 공간으로 색인한 소스가 하나라도 있는가 — 없으면 질의 임베딩부터 할 필요가 없다."""
    found = await db.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM sources WHERE embedding_model = :space"
            " AND chunk_count > 0)"
        ),
        {"space": space},
    )
    return bool(found.scalar())
