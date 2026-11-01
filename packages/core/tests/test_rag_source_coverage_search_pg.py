"""rag_search 소스 커버리지 옵션(#446) — `retrieve_context`를 실제 DB(pgvector)로 본다.

검색은 실경로다 — `retrieve_context` → `search_spaces` → `vectorstore` → PostgreSQL. 질의 임베딩과
어드민 설정, 임베딩 공간 조회만 대역이다. 소스 범위(`failed` 제외)·커버리지 범위·제목 조회도 실제
행이다. 노드·도구 출력은 `test_rag_source_coverage.py`가 대역 검색으로 본다.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from biznotes_core.models import Notebook, Source, SourceChunk
from biznotes_core.services import embedding_spaces, rag_chat, settings_store
from biznotes_core.services.embedding_spaces import LOCAL_SPACE
from biznotes_core.services.ingestion import embedder
from biznotes_core.services.ingestion.embedder import DENSE_DIM, SparseVector
from biznotes_core.services.rag_chat import Citation, SourceCoverage


def _unit(i: int, weight: float = 1.0) -> list[float]:
    v = [0.0] * DENSE_DIM
    v[i % DENSE_DIM] = weight
    return v


QUERY_DENSE = _unit(0)
QUERY_SPARSE = SparseVector(indices=[0], values=[1.0])


@pytest.fixture
def search_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    env: dict[str, Any] = {"rag": {"top_k": 30}}

    async def get_setting(_db: Any, key: str) -> dict[str, Any]:
        assert key == "rag"
        return env["rag"]

    async def spaces(_db: Any, _nbs: Any, _ids: Any = None) -> list[Any]:
        return [LOCAL_SPACE]

    monkeypatch.setattr(settings_store, "get_setting", get_setting)
    monkeypatch.setattr(embedding_spaces, "spaces_in_scope", spaces)
    monkeypatch.setattr(embedder, "embed_query_dense", lambda _q: QUERY_DENSE)
    monkeypatch.setattr(embedder, "embed_query_sparse", lambda _q: QUERY_SPARSE)
    return env


@pytest.fixture
async def notebook(
    search_env: dict[str, Any], db: Any, user_a: Any, user_b: Any
) -> SimpleNamespace:
    """소스 500개(청크 있음) + 청크 없는 소스 5개 + 실패 소스 1개, 그리고 다른 노트북.

    소스 3개(hot)가 질의와 거의 같은 청크를 20개씩 가져 점수 상위를 독점한다 — 이슈의 재현이다.
    다른 노트북 소스는 더 관련도가 높은 청크를 가진다. 범위에 들어오면 반드시 상위에 보인다.
    """
    nb = Notebook(owner_id=user_a.id, title="커버리지")
    other_nb = Notebook(owner_id=user_b.id, title="남의 노트북")
    db.add_all([nb, other_nb])
    await db.flush()

    def add(notebook_id: uuid.UUID, title: str, status: str = "ready") -> Source:
        source = Source(notebook_id=notebook_id, type="text", title=title, status=status)
        db.add(source)
        return source

    hot = [add(nb.id, f"핵심 {i}") for i in range(3)]
    cold = [add(nb.id, f"자료 {i}") for i in range(497)]
    empty = [add(nb.id, f"빈 자료 {i}") for i in range(5)]  # 처리 중 — 청크 없음
    failed = add(nb.id, "실패 자료", status="failed")
    other = [add(other_nb.id, f"남의 자료 {i}") for i in range(3)]
    await db.flush()

    def index(source: Source, vectors: list[list[float]], hot_token: bool) -> None:
        # 관련 청크만 질의 토큰(0)을 가진다 — sparse도 같은 소스로 몰린다
        token = SparseVector(indices=[0 if hot_token else 1], values=[1.0])
        db.add_all(
            SourceChunk(
                source_id=source.id,
                seq=i,
                text=f"{source.id} 청크 {i}",
                meta={},
                embedding=dense,
                sparse=token,
            )
            for i, dense in enumerate(vectors)
        )
        source.chunk_count = len(vectors)

    for source in hot:
        index(source, [[1.0, 0.01 * j] + [0.0] * (DENSE_DIM - 2) for j in range(20)], True)
    for i, source in enumerate(cold):
        weak = [0.2] + [0.0] * (DENSE_DIM - 1)
        weak[1 + i % (DENSE_DIM - 1)] = 1.0
        index(source, [weak, _unit(1 + (i + 7) % (DENSE_DIM - 1))], False)
    index(failed, [_unit(0)], True)
    for source in other:
        index(source, [_unit(0)] * 3, True)
    await db.commit()
    return SimpleNamespace(
        db=db,
        id=nb.id,
        hot=[s.id for s in hot],
        cold=[s.id for s in cold],
        empty=[s.id for s in empty],
        failed=failed.id,
        other=[s.id for s in other],
    )


def _per_source_counts(citations: list[Citation]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in citations:
        counts[c.source_id] = counts.get(c.source_id, 0) + 1
    return counts


async def test_ranked_default_concentrates_on_a_few_sources(notebook):
    """재현 — 옵션을 생략하면 지금처럼 점수 상위 30청크이고 소스 3개가 독점한다."""
    result = await rag_chat.retrieve_context(notebook.db, notebook.id, "q", None)

    assert len(result.citations) == 30
    assert set(_per_source_counts(result.citations)) == {str(s) for s in notebook.hot}
    assert result.coverage is None  # 옵션 생략 — 출력 모양도 지금과 같다


async def test_per_source_k1_covers_every_source_with_chunks(notebook):
    result = await rag_chat.retrieve_context(
        notebook.db, notebook.id, "q", None, coverage="per_source", per_source_limit=1
    )

    chunked = {str(s) for s in [*notebook.hot, *notebook.cold]}
    counts = _per_source_counts(result.citations)
    assert set(counts) == chunked  # 청크가 있는 소스 500개가 모두 근거에 나온다
    assert set(counts.values()) == {1}
    # top_k(30) 상한은 per_source에 걸리지 않는다
    assert len(result.citations) == 500
    # 번호는 [1]부터 이어진다 — run 근거표 규칙은 그대로다
    assert [c.n for c in result.citations] == list(range(1, 501))
    assert result.coverage == SourceCoverage(
        scope_count=505,
        hit_sources=[c.source_id for c in result.citations],
        # 범위 순서는 (생성 시각, id)다 — 한 트랜잭션에서 만든 행은 id 순이다
        missing=sorted(str(s) for s in notebook.empty),
    )


async def test_per_source_defaults_to_one_chunk_per_source(notebook):
    result = await rag_chat.retrieve_context(
        notebook.db, notebook.id, "q", None, coverage="per_source"
    )

    assert set(_per_source_counts(result.citations).values()) == {1}


async def test_per_source_k2_guarantees_and_caps_each_source(notebook):
    result = await rag_chat.retrieve_context(
        notebook.db, notebook.id, "q", None, coverage="per_source", per_source_limit=2
    )

    counts = _per_source_counts(result.citations)
    assert len(counts) == 500
    assert set(counts.values()) == {2}  # 소스마다 청크가 2개 이상이다


async def test_per_source_limit_caps_ranked_results(notebook):
    result = await rag_chat.retrieve_context(
        notebook.db, notebook.id, "q", None, top_k=30, per_source_limit=2
    )

    counts = _per_source_counts(result.citations)
    assert len(result.citations) == 30  # 상한 30은 ranked에 그대로 걸린다
    assert max(counts.values()) <= 2
    assert {str(s) for s in notebook.hot} <= set(counts)  # 관련도 순서는 지킨다
    assert result.coverage is not None and result.coverage.scope_count == 505
    assert set(result.coverage.hit_sources) == set(counts)
    assert len(result.coverage.missing) == 505 - len(counts)


async def test_insufficient_context_keeps_the_top_one_rule(notebook, search_env):
    search_env["rag"] = {"top_k": 30, "insufficient_context_threshold_base": 2.0}
    result = await rag_chat.retrieve_context(
        notebook.db, notebook.id, "q", None, coverage="per_source"
    )
    assert result.insufficient_context is True

    search_env["rag"] = {"top_k": 30, "insufficient_context_threshold_base": 0.0}
    result = await rag_chat.retrieve_context(
        notebook.db, notebook.id, "q", None, coverage="per_source"
    )
    assert result.insufficient_context is False


async def test_per_source_with_rerank_keeps_sources_and_scores_the_top(
    notebook, search_env, monkeypatch
):
    """리랭크를 켜도 per_source는 소스를 빼거나 재정렬하지 않는다. 임계값은 리랭커 점수로 본다."""
    from biznotes_core.services import rerank

    seen: dict[str, Any] = {}

    async def resolve(_db: Any, _value: Any, rerank_timeout: float = 30) -> Any:
        return object()

    def relevance(_target: Any, _query: str, texts: list[str]) -> list[float]:
        seen["texts"] = len(texts)
        return [0.1] * len(texts)

    monkeypatch.setattr(rerank, "resolve_reranker", resolve)
    monkeypatch.setattr(rerank, "rerank_relevance", relevance)
    search_env["rag"] = {
        "top_k": 6,
        "rerank_enabled": True,
        "rerank_candidates": 20,
        "insufficient_context_threshold_rerank": 0.5,
    }
    ranked = await rag_chat.retrieve_context(notebook.db, notebook.id, "q", None, top_k=6)
    result = await rag_chat.retrieve_context(
        notebook.db, notebook.id, "q", None, coverage="per_source"
    )

    assert seen["texts"] == 20  # 재점수는 앞쪽 후보(rerank_candidates)만
    assert len(_per_source_counts(result.citations)) == 500
    assert result.insufficient_context is True  # 리랭커 점수 0.1 < 0.5
    assert len(ranked.citations) == 6


async def test_per_source_never_reaches_other_notebooks(notebook):
    """비인가 — 다른 노트북 소스 id를 범위에 넣어도 검색·커버리지 어디에도 들어가지 않는다."""
    picked = [notebook.hot[0], notebook.cold[0], *notebook.other, notebook.failed]
    result = await rag_chat.retrieve_context(
        notebook.db, notebook.id, "q", picked, coverage="per_source"
    )

    assert {c.source_id for c in result.citations} == {
        str(notebook.hot[0]),
        str(notebook.cold[0]),
    }
    assert result.coverage == SourceCoverage(
        scope_count=2,
        hit_sources=[c.source_id for c in result.citations],
        missing=[],
    )

    # 노트북 전체 범위도 다른 노트북 청크를 보지 않는다
    whole = await rag_chat.retrieve_context(
        notebook.db, notebook.id, "q", None, coverage="per_source"
    )
    other = {str(s) for s in notebook.other}
    assert not other & {c.source_id for c in whole.citations}
    assert whole.coverage is not None and not other & set(whole.coverage.missing)


async def test_empty_scope_reports_zero_coverage(notebook):
    result = await rag_chat.retrieve_context(
        notebook.db, notebook.id, "q", [], coverage="per_source"
    )

    assert result.citations == [] and result.insufficient_context is True
    assert result.coverage == SourceCoverage(scope_count=0, hit_sources=[], missing=[])
