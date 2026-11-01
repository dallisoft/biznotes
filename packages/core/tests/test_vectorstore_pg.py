"""청크 벡터 검색(pgvector) — RRF 순서·점수, 소스별 묶음, 노트북·소스 필터, 삭제 반영, 공간 분리.

Qdrant에서 옮긴 검색이 같은 결과를 내는지 본다(계획 Phase 2 수락 기준 「하이브리드 검색 회귀」).
점수는 Qdrant RRF와 같다: dense·sparse 후보 목록(각 `limit × 3`)의 순위(0부터)마다
`1/(순위 + 2)`를 더한다. 옮기기 전 Qdrant(인메모리)와 같은 데이터·질의 3,000건을 대조해 순서·점수가
모두 같았다(Phase 2b 기록). 여기서는 손으로 계산할 수 있는 배치와 작은 기준 구현으로 고정한다.
"""

import random
from typing import Any

import pytest
from conftest import add_chunks
from sqlalchemy import delete

from biznotes_core.models import Notebook, Source, SourceChunk
from biznotes_core.models.vector_types import SPARSE_DIM, sparse_literal
from biznotes_core.services import vectorstore
from biznotes_core.services.embedding_spaces import LOCAL_SPACE_KEY
from biznotes_core.services.ingestion.embedder import SparseVector

LOCAL = {"space": LOCAL_SPACE_KEY, "local_space": LOCAL_SPACE_KEY}


def _sv(*pairs: tuple[int, float]) -> SparseVector:
    return SparseVector(indices=[i for i, _ in pairs], values=[v for _, v in pairs])


async def _notebook(db: Any, owner: Any, *titles: str) -> tuple[Notebook, list[Source]]:
    nb = Notebook(owner_id=owner.id, title="검색")
    db.add(nb)
    await db.flush()
    sources = [Source(notebook_id=nb.id, type="text", title=t, status="ready") for t in titles]
    db.add_all(sources)
    await db.commit()
    return nb, sources


async def test_rrf_score_is_qdrants_scale(db, user_a) -> None:
    """두 목록 모두 1위면 1/2 + 1/2 = 1.0, 한 목록만 1위면 0.5 — 근거 부족 임계값이 이 눈금이다."""
    nb, (src,) = await _notebook(db, user_a, "자료")
    both, dense_only, sparse_only = await add_chunks(
        db,
        src,
        [
            ([1.0, 0.0], _sv((7, 2.0))),  # dense 1위·sparse 1위
            ([0.9, 0.1], _sv((99, 1.0))),  # dense 2위, 질의 토큰 없음
            ([0.0, 1.0], _sv((7, 1.0))),  # dense 3위·sparse 2위
        ],
    )

    hits = await vectorstore.hybrid_search(db, nb.id, [1.0, 0.0], _sv((7, 1.0)), limit=3, **LOCAL)

    assert [h.id for h in hits] == [str(both.id), str(sparse_only.id), str(dense_only.id)]
    assert [h.score for h in hits] == pytest.approx([1.0, 1 / 4 + 1 / 3, 1 / 3])
    # payload는 청크 행이다 — 인용이 쓰는 키
    assert hits[0].payload == {
        "notebook_id": str(nb.id),
        "source_id": str(src.id),
        "seq": 0,
        "text": "자료 청크 0",
    }


def _reference_rrf(
    rows: list[tuple[str, list[float], dict[int, float]]],
    dense_q: list[float],
    sparse_q: dict[int, float],
    candidates: int,
) -> list[tuple[str, float]]:
    """Qdrant RRF(인메모리 구현과 같은 규칙)의 작은 기준 구현 — 동률은 dense 목록 순서."""

    def cos(a: list[float], b: list[float]) -> float:
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb)

    dense = sorted(rows, key=lambda r: (-cos(r[1], dense_q), r[0]))[:candidates]
    overlap = [r for r in rows if any(k in sparse_q for k in r[2])]
    ip = {r[0]: sum(v * sparse_q.get(k, 0.0) for k, v in r[2].items()) for r in overlap}
    sparse = sorted(overlap, key=lambda r: (-ip[r[0]], r[0]))[:candidates]
    scores: dict[str, float] = {}
    for ranked in (dense, sparse):
        for pos, r in enumerate(ranked):
            scores[r[0]] = scores.get(r[0], 0.0) + 1 / (pos + 2)
    order = {r[0]: i for i, r in enumerate(dense + [r for r in sparse if r not in dense])}
    return sorted(scores.items(), key=lambda kv: (-kv[1], order[kv[0]]))


async def test_random_queries_match_the_reference_rrf(db, user_a) -> None:
    rng = random.Random(11)
    nb, sources = await _notebook(db, user_a, "가", "나", "다")
    rows: list[tuple[str, list[float], dict[int, float]]] = []
    for src in sources:
        vectors = []
        for _ in range(6):
            dense = [rng.uniform(-1, 1) for _ in range(4)]
            tokens = {rng.randrange(12): round(rng.uniform(0.2, 2.0), 3) for _ in range(3)}
            vectors.append((dense, tokens))
        for chunk, (dense, tokens) in zip(
            await add_chunks(db, src, [(d, t) for d, t in vectors]), vectors, strict=True
        ):
            rows.append((str(chunk.id), dense, tokens))

    for _ in range(40):
        dense_q = [rng.uniform(-1, 1) for _ in range(4)]
        sparse_q = {rng.randrange(12): 1.0}
        limit = rng.choice([1, 3, 5])
        hits = await vectorstore.hybrid_search(
            db, nb.id, dense_q, _sv(*sparse_q.items()), limit=limit, **LOCAL
        )
        expected = _reference_rrf(rows, dense_q, sparse_q, limit * 3)[:limit]
        assert [(h.id, round(h.score, 9)) for h in hits] == [
            (cid, round(score, 9)) for cid, score in expected
        ]


async def test_notebook_and_source_filters(db, user_a, user_b) -> None:
    nb, (a, b) = await _notebook(db, user_a, "a", "b")
    other_nb, (other,) = await _notebook(db, user_b, "남의 자료")
    [in_a] = await add_chunks(db, a, [([1.0, 0.0], _sv((1, 1.0)))])
    [in_b] = await add_chunks(db, b, [([0.5, 0.5], _sv((1, 1.0)))])
    # 다른 노트북 청크가 질의와 가장 가깝다 — 범위에 새면 반드시 1위다
    await add_chunks(db, other, [([1.0, 0.0], _sv((1, 9.0)))])
    q = ([1.0, 0.0], _sv((1, 1.0)))

    whole = await vectorstore.hybrid_search(db, nb.id, *q, source_ids=None, **LOCAL)
    only_b = await vectorstore.hybrid_search(db, nb.id, *q, source_ids=[b.id], **LOCAL)
    # 범위 밖 소스 id(다른 노트북)를 넣어도 노트북 필터가 먼저다
    leaked = await vectorstore.hybrid_search(db, nb.id, *q, source_ids=[other.id], **LOCAL)
    empty = await vectorstore.hybrid_search(db, nb.id, *q, source_ids=[], **LOCAL)

    assert [h.id for h in whole] == [str(in_a.id), str(in_b.id)]
    assert [h.id for h in only_b] == [str(in_b.id)]
    assert leaked == [] and empty == []
    multi = await vectorstore.hybrid_search_multi(db, [nb.id, other_nb.id], *q, **LOCAL)
    assert {h.payload["notebook_id"] for h in multi} == {str(nb.id), str(other_nb.id)}


async def test_groups_cover_each_source_and_cap_its_chunks(db, user_a) -> None:
    """#446 — 소스 `limit`개, 소스마다 `group_size`개. 후보를 자르지 않아 약한 소스도 든다."""
    nb, (hot, warm, cold) = await _notebook(db, user_a, "hot", "warm", "cold")
    await add_chunks(db, hot, [([1.0, 0.01 * i], _sv((0, 5.0))) for i in range(30)])
    await add_chunks(db, warm, [([0.8, 0.6], _sv((0, 1.0))), ([0.7, 0.7], _sv((3, 1.0)))])
    [cold_chunk] = await add_chunks(db, cold, [([0.0, 1.0], _sv((9, 1.0)))])
    q = ([1.0, 0.0], _sv((0, 1.0)))

    hits = await vectorstore.hybrid_search_groups(db, nb.id, *q, group_size=2, limit=3, **LOCAL)

    per_source: dict[str, int] = {}
    for h in hits:
        per_source[h.payload["source_id"]] = per_source.get(h.payload["source_id"], 0) + 1
    assert per_source == {str(hot.id): 2, str(warm.id): 2, str(cold.id): 1}
    assert str(cold_chunk.id) in {h.id for h in hits}  # 범위 전체에서 순위를 매긴다
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)

    two = await vectorstore.hybrid_search_groups(db, nb.id, *q, group_size=1, limit=2, **LOCAL)
    assert [h.payload["source_id"] for h in two] == [str(hot.id), str(warm.id)]
    narrowed = await vectorstore.hybrid_search_groups(
        db, nb.id, *q, group_size=3, limit=5, source_ids=[cold.id], **LOCAL
    )
    assert [h.id for h in narrowed] == [str(cold_chunk.id)]


async def test_deleting_a_source_removes_its_vectors(db, user_a) -> None:
    """옛 Qdrant의 `delete_by_source` 보상 잡 대신 CASCADE다 — 지운 소스는 바로 검색에서 빠진다."""
    nb, (keep, gone) = await _notebook(db, user_a, "남김", "지움")
    [kept] = await add_chunks(db, keep, [([0.5, 0.5], _sv((1, 1.0)))])
    await add_chunks(db, gone, [([1.0, 0.0], _sv((1, 1.0)))])
    q = ([1.0, 0.0], _sv((1, 1.0)))
    assert len(await vectorstore.hybrid_search(db, nb.id, *q, **LOCAL)) == 2

    await db.execute(delete(Source).where(Source.id == gone.id))
    await db.commit()

    assert [h.id for h in await vectorstore.hybrid_search(db, nb.id, *q, **LOCAL)] == [str(kept.id)]


async def test_spaces_never_compare_vectors_of_another_model(db, user_a) -> None:
    """공간(모델)마다 차원이 다르다 — 한 공간의 질의는 그 공간 소스의 행만 본다(차원 오류 없음)."""
    nb, (local_src, gw_src) = await _notebook(db, user_a, "로컬", "게이트웨이")
    [local_chunk] = await add_chunks(db, local_src, [([1.0, 0.0, 0.0], _sv((1, 1.0)))])
    [gw_chunk] = await add_chunks(
        db, gw_src, [([1.0, 0.0], _sv((1, 1.0)))], space_key="gateway:m:x"
    )

    local_hits = await vectorstore.hybrid_search(db, nb.id, [1.0, 0.0, 0.0], _sv((1, 1.0)), **LOCAL)
    gw_hits = await vectorstore.hybrid_search(
        db, nb.id, [1.0, 0.0], _sv((1, 1.0)), space="gateway:m:x", local_space=LOCAL_SPACE_KEY
    )

    assert [h.id for h in local_hits] == [str(local_chunk.id)]  # NULL 공간 = 로컬 기본
    assert [h.id for h in gw_hits] == [str(gw_chunk.id)]
    assert await vectorstore.space_has_chunks(db, "gateway:m:x")
    assert not await vectorstore.space_has_chunks(db, "gateway:other:y")


async def test_a_stray_dimension_in_the_same_space_is_skipped_not_an_error(db, user_a) -> None:
    """같은 공간 키에 차원이 다른 행이 섞여도(모델 출력 차원 변경) 거리 계산 오류가 나지 않는다."""
    nb, (src,) = await _notebook(db, user_a, "자료")
    [ok] = await add_chunks(db, src, [([1.0, 0.0], _sv((1, 1.0)))])
    await add_chunks(db, src, [([1.0, 0.0, 0.0], _sv((1, 1.0)))])

    hits = await vectorstore.hybrid_search(db, nb.id, [1.0, 0.0], _sv((1, 1.0)), **LOCAL)

    assert [h.id for h in hits] == [str(ok.id)]


async def test_bm25_ids_beyond_the_sparsevec_limit_are_folded(db, user_a) -> None:
    """fastembed BM25 id는 2³¹ 미만이라 sparsevec 상한(10억)을 넘는다 — 같은 규칙으로 접는다."""
    big = 2**31 - 5
    assert sparse_literal(_sv((big, 1.0), (big % SPARSE_DIM, 2.0))) == (
        "{" + f"{big % SPARSE_DIM + 1}:3.0" + "}/" + str(SPARSE_DIM)
    )
    nb, (src,) = await _notebook(db, user_a, "자료")
    [hit] = await add_chunks(db, src, [([0.0, 1.0], _sv((big, 1.0)))])
    await add_chunks(db, src, [([0.0, 1.0], _sv((5, 1.0)))])

    hits = await vectorstore.hybrid_search(db, nb.id, [1.0, 0.0], _sv((big, 1.0)), limit=1, **LOCAL)

    # dense는 두 청크가 동률(id 순)이고 sparse 1위가 가른다 — dense 순위에 따라 1.0 또는 1/3 + 1/2
    assert [h.id for h in hits] == [str(hit.id)]
    assert hits[0].score in (pytest.approx(1.0), pytest.approx(1 / 3 + 1 / 2))


async def test_rows_without_vectors_are_not_searched(db, user_a) -> None:
    nb, (src,) = await _notebook(db, user_a, "자료")
    db.add(SourceChunk(source_id=src.id, seq=0, text="벡터 없음", meta={}))
    await db.commit()

    assert await vectorstore.hybrid_search(db, nb.id, [1.0, 0.0], _sv((1, 1.0)), **LOCAL) == []
