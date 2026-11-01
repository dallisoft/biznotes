# 신규(#446). 필요: PostgreSQL·Valkey(PG 테스트).
"""rag_search 소스 커버리지(`coverage: per_source`)의 비인가 경계 — 실제 DB(pgvector).

질의 임베딩과 임베딩 공간 조회만 대역이다. 소스 범위(`failed` 제외·노트북 소속), 커버리지 범위
조회, 묶음 검색, 제목 조회, 인용 번호는 실경로다. 다른 노트북 소스는 질의와 가장 가까운 청크를
가진다 — 범위에 새면 반드시 결과에 보인다.
"""

import uuid
from typing import Any

import pytest
from conftest import add_chunks

from biznotes_core.models import Notebook, Source, User
from biznotes_core.services import embedding_spaces
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.nodes.retrieval import _exec_rag_search
from biznotes_core.services.embedding_spaces import LOCAL_SPACE
from biznotes_core.services.ingestion import embedder
from biznotes_core.services.ingestion.embedder import DENSE_DIM, SparseVector


def _vector(i: int, weight: float) -> list[float]:
    v = [0.0] * DENSE_DIM
    v[0] = weight
    v[1 + i] = 1.0
    return v


@pytest.fixture(autouse=True)
def _query_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    async def spaces(_db: Any, _nbs: Any, _ids: Any = None) -> list[Any]:
        return [LOCAL_SPACE]

    monkeypatch.setattr(embedding_spaces, "spaces_in_scope", spaces)
    monkeypatch.setattr(embedder, "embed_query_dense", lambda _q: [1.0] + [0.0] * (DENSE_DIM - 1))
    monkeypatch.setattr(embedder, "embed_query_sparse", lambda _q: SparseVector([0], [1.0]))


async def _notebook(db: Any, owner: User, specs: list[tuple[str, str, int]]) -> tuple[Any, list]:
    nb = Notebook(owner_id=owner.id, title="커버리지")
    db.add(nb)
    await db.flush()
    sources = [
        Source(notebook_id=nb.id, type="text", title=t, status=status, chunk_count=0)
        for t, status, _chunks in specs
    ]
    db.add_all(sources)
    await db.commit()
    return nb, sources


async def _index(db: Any, src: Source, weights: list[float]) -> None:
    vectors = [(_vector(i, w), SparseVector([0], [w])) for i, w in enumerate(weights)]
    await add_chunks(db, src, vectors)


async def test_per_source_covers_the_notebook_and_nothing_else(db, user_a, user_b):
    nb, (hot, cold1, cold2, empty, failed) = await _notebook(
        db,
        user_a,
        [
            ("hot", "ready", 10),
            ("cold1", "ready", 1),
            ("cold2", "ready", 1),
            ("empty", "processing", 0),
            ("failed", "failed", 1),
        ],
    )
    other_nb, (other,) = await _notebook(db, user_b, [("other", "ready", 3)])
    await _index(db, hot, [10.0] * 10)
    await _index(db, cold1, [0.1])
    await _index(db, cold2, [0.1])
    await _index(db, failed, [10.0])
    await _index(db, other, [10.0] * 3)

    def ctx(source_ids: list[uuid.UUID] | None) -> RunContext:
        return RunContext(
            run_id=uuid.uuid4(), notebook_id=nb.id, db=db, params={}, source_ids=source_ids
        )

    node = {"id": "search", "query": "q", "coverage": "per_source"}
    out = await _exec_rag_search(ctx(None), node, {})
    titles = [c["source_title"] for c in out["citations"]]
    assert sorted(titles) == ["cold1", "cold2", "hot"]  # 소스마다 1개 — hot이 독점하지 않는다
    assert out["scope_count"] == 4  # failed·다른 노트북은 범위가 아니다
    assert out["missing"] == [str(empty.id)]
    assert out["fit"] == "shrink_each" and out["dropped"] == []

    # run 범위에 다른 노트북·실패 소스 id가 섞여도 검색·커버리지에 들어가지 않는다
    out = await _exec_rag_search(ctx([cold1.id, other.id, failed.id]), node, {})
    assert [c["source_title"] for c in out["citations"]] == ["cold1"]
    assert out["scope_count"] == 1 and out["hit_sources"] == [str(cold1.id)]
    assert out["missing"] == []

    # 노드 source_ids로 좁혀도 같다
    scoped = {**node, "source_ids": [str(other.id), str(cold2.id)]}
    out = await _exec_rag_search(ctx(None), scoped, {})
    assert [c["source_title"] for c in out["citations"]] == ["cold2"]
    assert out["scope_count"] == 1


async def test_per_source_limit_caps_each_source_in_ranked_search(db, user_a):
    nb, (hot, cold) = await _notebook(db, user_a, [("hot", "ready", 10), ("cold", "ready", 2)])
    await _index(db, hot, [10.0] * 10)
    await _index(db, cold, [0.1, 0.1])

    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=nb.id, db=db, params={})
    node = {"id": "search", "query": "q", "top_k": 6, "per_source_limit": 2}
    out = await _exec_rag_search(ctx, node, {})

    titles = [c["source_title"] for c in out["citations"]]
    assert titles.count("hot") == 2 and titles.count("cold") == 2
    assert out["scope_count"] == 2 and out["missing"] == []
