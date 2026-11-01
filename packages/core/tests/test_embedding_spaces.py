"""임베딩 공간 — 모델 라우팅·공간 키·다중 공간 검색 (#7).

DB·외부 임베딩 API 없이 공간 규칙과 검색 조립을 검증한다(벡터 검색은 대역). 공간별 벡터
검색 자체(공간·차원 분리)는 `test_vectorstore_pg.py`가 실제 PostgreSQL로 본다. 실제 전환
시나리오(로컬 → 게이트웨이 → 모델 삭제)는 docs/plans/embedding-model-routing/plan.md의
스모크 기록을 참고한다.
"""

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from biznotes_core.models import LLMModel, LLMProvider, Source
from biznotes_core.services import embedding_spaces as es
from biznotes_core.services import vectorstore
from biznotes_core.services.gateway.llm import ResolvedModel
from biznotes_core.services.ingestion import embedder


def _resolved(provider_type: str = "mock", name: str = "embed-1") -> ResolvedModel:
    provider = LLMProvider(id=uuid.uuid4(), name=f"p-{provider_type}", type=provider_type)
    model = LLMModel(
        id=uuid.uuid4(),
        provider_id=provider.id,
        model_name=name,
        display_name=name.upper(),
        capabilities=["embedding"],
    )
    return ResolvedModel(model=model, provider=provider)


# ── 공간 규칙 ──────────────────────────────────────────────────────────


def test_local_space_is_the_fastembed_default():
    assert es.LOCAL_SPACE.key == es.LOCAL_SPACE_KEY == f"fastembed:{embedder.DEFAULT_DENSE_MODEL}"
    assert es.LOCAL_SPACE.is_local


def test_gateway_space_key_is_the_model_identity():
    r = _resolved()
    space = es._gateway_space(r)

    assert space.key == f"gateway:{r.model.id}:embed-1"
    assert not space.is_local
    # 같은 행에서 model_name만 바꿔도 실제 모델이 바뀐 것이다 — 다른 공간
    r.model.model_name = "embed-2"
    assert es._gateway_space(r).key != space.key


def test_source_records_vector_provenance():
    cols = Source.__table__.c
    assert cols.embedding_model.nullable and cols.embedding_model.type.length == 255
    assert cols.chunk_config.nullable and cols.chunk_config.type.none_as_null


def test_stale_condition_sql():
    sql = str(
        es.stale_condition("k", {"chunk_size": 800, "chunk_overlap": 100}).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "coalesce(sources.embedding_model" in sql
    assert "jsonb_typeof(sources.chunk_config)" in sql  # SQL NULL·JSON null은 "모름"


# ── 게이트웨이 임베딩 ────────────────────────────────────────────────────


def test_mock_provider_embeds_deterministically_without_keys():
    space = es._gateway_space(_resolved("mock"))

    a, b = space.embed(["같은 문장", "같은 문장"])
    assert a == b and len(a) == es.MOCK_EMBEDDING_DIM
    assert abs(sum(x * x for x in a) - 1.0) < 1e-9


def test_gateway_embed_batches_and_restores_input_order(monkeypatch):
    import litellm

    calls: list[dict[str, Any]] = []

    def fake_embedding(model: str, input: list[str], **kw: Any) -> Any:
        calls.append({"model": model, "n": len(input), **kw})
        # 제공자가 순서를 섞어 돌려줘도 index로 되돌린다
        data = [{"index": i, "embedding": [float(len(t))]} for i, t in enumerate(input)]
        return SimpleNamespace(data=list(reversed(data)))

    monkeypatch.setattr(litellm, "embedding", fake_embedding)
    monkeypatch.setattr(es, "GATEWAY_BATCH_SIZE", 2)
    texts = ["a", "bb", "ccc", "dddd", "eeeee"]

    vectors = es._gateway_space(_resolved("openai")).embed(texts)

    assert vectors == [[1.0], [2.0], [3.0], [4.0], [5.0]]
    assert [c["n"] for c in calls] == [2, 2, 1]
    assert calls[0]["model"] == "embed-1"


def test_gateway_embed_errors_are_explicit(monkeypatch):
    import litellm

    def boom(**kw: Any) -> Any:
        raise RuntimeError("401 unauthorized")

    monkeypatch.setattr(litellm, "embedding", boom)

    with pytest.raises(es.EmbeddingError, match="EMBED-1"):
        es._gateway_space(_resolved("openai")).embed(["x"])

    monkeypatch.setattr(
        litellm, "embedding", lambda **kw: SimpleNamespace(data=[{"index": 0, "embedding": [1]}])
    )
    with pytest.raises(es.EmbeddingError, match="응답 수"):
        es._gateway_space(_resolved("openai")).embed(["x", "y"])


# ── 다중 공간 검색 ───────────────────────────────────────────────────────


@dataclass
class _Point:
    id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


@pytest.fixture
def fake_store(monkeypatch):
    """공간 키별 결과를 돌려주는 벡터 검색 대역 — 호출(공간 키, 인자)을 기록한다."""
    calls: list[tuple[str, dict[str, Any]]] = []
    results: dict[str, list[_Point]] = {}
    failing: set[str] = set()
    indexed: set[str] = set()  # 색인한 소스가 있는 게이트웨이 공간

    async def hybrid(
        db, notebook_id, dense, sparse, source_ids=None, limit=10, *, space, local_space
    ):
        assert local_space == es.LOCAL_SPACE_KEY
        calls.append((space, {"limit": limit, "dim": len(dense)}))
        if space in failing:
            raise RuntimeError(f"{space} down")
        return results.get(space, [])[:limit]

    async def hybrid_multi(db, notebook_ids, dense, sparse, limit=8, *, space, local_space):
        calls.append((space, {"notebook_ids": notebook_ids}))
        return results.get(space, [])[:limit]

    async def has_chunks(db, space):
        return space in indexed

    monkeypatch.setattr(vectorstore, "hybrid_search", hybrid)
    monkeypatch.setattr(vectorstore, "hybrid_search_multi", hybrid_multi)
    monkeypatch.setattr(vectorstore, "space_has_chunks", has_chunks)
    monkeypatch.setattr(embedder, "embed_query_dense", lambda q: [0.0] * 384)
    monkeypatch.setattr(
        embedder, "embed_query_sparse", lambda q: SimpleNamespace(indices=[], values=[])
    )
    return SimpleNamespace(calls=calls, results=results, failing=failing, indexed=indexed)


LOCAL = es.LOCAL_SPACE_KEY


async def test_each_space_is_queried_with_its_own_model(fake_store):
    gw = es._gateway_space(_resolved("mock"))
    fake_store.indexed.add(gw.key)
    fake_store.results[LOCAL] = [_Point("l1", 0.4), _Point("l2", 0.1)]
    fake_store.results[gw.key] = [_Point("g1", 0.9), _Point("g2", 0.2)]

    points = await es.search_spaces(
        None, [gw, es.LOCAL_SPACE], "q", notebook_id=uuid.uuid4(), limit=3
    )

    assert [p.id for p in points] == ["g1", "l1", "g2"]
    # 게이트웨이 공간은 자기 공간을 자기 모델 차원(mock 64)으로 질의한다
    assert (gw.key, {"limit": 3, "dim": es.MOCK_EMBEDDING_DIM}) in fake_store.calls
    assert (LOCAL, {"limit": 3, "dim": 384}) in fake_store.calls


async def test_merge_pairs_ranks_so_a_small_new_space_cannot_take_over(fake_store):
    """RRF 점수는 공간 안 순위만 담는다 — 점수로 합치면 갓 옮겨 간 작은 공간이
    상위를 독식한다(모델 전환 중 한 소스만 재인덱싱된 상태). 순위끼리 묶어 섞는다."""
    gw = es._gateway_space(_resolved("mock"))
    fake_store.indexed.add(gw.key)
    fake_store.results[gw.key] = [_Point("g1", 0.9), _Point("g2", 0.8), _Point("g3", 0.7)]
    fake_store.results[LOCAL] = [_Point("l1", 0.5), _Point("l2", 0.4)]

    points = await es.search_spaces(
        None, [gw, es.LOCAL_SPACE], "q", notebook_id=uuid.uuid4(), limit=4
    )

    assert [p.id for p in points] == ["g1", "l1", "g2", "l2"]  # 점수순이면 g1,g2,g3,l1


async def test_failed_space_is_skipped_but_all_failing_raises(fake_store):
    gw = es._gateway_space(_resolved("mock"))
    fake_store.indexed.add(gw.key)
    fake_store.failing.add(gw.key)
    fake_store.results[LOCAL] = [_Point("l1", 0.4)]

    points = await es.search_spaces(
        None, [gw, es.LOCAL_SPACE], "q", notebook_id=uuid.uuid4(), limit=3
    )
    assert [p.id for p in points] == ["l1"]

    with pytest.raises(RuntimeError, match="down"):
        await es.search_spaces(None, [gw], "q", notebook_id=uuid.uuid4(), limit=3)


async def test_error_is_not_hidden_when_another_space_was_merely_skipped(fake_store):
    """아직 색인 전이라 건너뛴 공간은 '시도'가 아니다 — 시도한 공간이 모두 실패하면
    빈 결과로 감추지 않고 오류를 올린다."""
    gw = es._gateway_space(_resolved("mock"))  # 색인한 소스 없음 → 건너뜀
    fake_store.failing.add(LOCAL)

    with pytest.raises(RuntimeError, match="down"):
        await es.search_spaces(None, [gw, es.LOCAL_SPACE], "q", notebook_id=uuid.uuid4(), limit=3)
    assert [space for space, _ in fake_store.calls] == [LOCAL]


async def test_gateway_space_without_indexed_sources_returns_nothing(fake_store, monkeypatch):
    """첫 인제스천 전의 게이트웨이 공간 — 질의 임베딩(API 호출)조차 하지 않는다."""
    gw = es._gateway_space(_resolved("mock"))

    def must_not_embed(*a: Any, **k: Any) -> Any:
        raise AssertionError("색인 전 공간의 질의를 임베딩했다")

    monkeypatch.setattr(es, "_gateway_embed", must_not_embed)

    assert await es.search_spaces(None, [gw], "q", notebook_id=uuid.uuid4(), limit=3) == []
    assert fake_store.calls == []


async def test_global_search_uses_multi_notebook_query(fake_store):
    ids = [uuid.uuid4(), uuid.uuid4()]
    fake_store.results[LOCAL] = [_Point("l1", 0.5)]

    points = await es.search_spaces(None, [es.LOCAL_SPACE], "q", notebook_ids=ids, limit=8)

    assert [p.id for p in points] == ["l1"]
    assert fake_store.calls == [(LOCAL, {"notebook_ids": ids})]
