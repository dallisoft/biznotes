"""Phase 10 — 리랭킹 통합 + 인용-청크 매칭 검증 테스트.

cross-encoder 실모델 로드는 피하고 rerank_order를 모킹해 검색 통합 경로를 검증한다.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from biznotes_core.services.rag_chat import (
    Citation,
    filter_cited,
    retrieve_context,
    verify_citations,
    verify_citations_sync,
)

from tests.conftest import bearer, login

# ---------- 인용 검증 ----------


def _search_returning(points: list[Any], *, sliced: bool = False) -> Any:
    """`search_spaces` 대역(비동기, 세션을 먼저 받는다) — 정해 둔 결과를 돌려준다."""

    async def search(db: Any, spaces: Any, query: str, *, limit: int = 8, **kw: Any) -> list[Any]:
        return points[:limit] if sliced else points

    return search


def _cite(n: int, quote: str) -> Citation:
    return Citation(n=n, source_id="s", chunk_id="c", quote=quote, source_title="t")


def test_verify_supported_citation():
    citations = [_cite(1, "에스프레소는 9기압의 압력으로 25~30초간 추출한다")]
    out = verify_citations_sync(citations, "에스프레소는 9기압으로 25~30초간 추출합니다 [1]")
    assert out[0].verified is True


def test_verify_hallucinated_citation(monkeypatch):
    """1차(어휘) 미달 시 2차(임베딩) 폴백도 낮은 유사도로 미검증 확정."""
    monkeypatch.setattr(
        "biznotes_core.services.ingestion.embedder.embed_dense",
        lambda texts: [[1.0, 0.0] if i == 0 else [0.0, 1.0] for i in range(len(texts))],
    )
    citations = [_cite(1, "녹차는 70~80도의 물에서 2분간 우린다")]
    out = verify_citations_sync(citations, "비트코인 가격은 작년 대비 상승했습니다 [1]")
    assert out[0].verified is False


def test_verify_paraphrase_rescued_by_embedding(monkeypatch):
    """[Phase 18a] 어휘 겹침은 낮지만 의미가 같은 패러프레이즈는 임베딩 2차 검증으로 구제."""
    monkeypatch.setattr(
        "biznotes_core.services.ingestion.embedder.embed_dense",
        lambda texts: [[1.0, 0.0] for _ in texts],  # 모든 벡터 동일 → 코사인 1.0
    )
    citations = [_cite(1, "에스프레소는 9기압의 압력으로 추출한다")]
    out = verify_citations_sync(citations, "완전히 다른 어휘의 패러프레이즈 답변입니다 [1]")
    assert out[0].verified is True


def test_verify_mixed_citations(monkeypatch):
    monkeypatch.setattr(
        "biznotes_core.services.ingestion.embedder.embed_dense",
        lambda texts: [[1.0, 0.0] if i == 0 else [0.0, 1.0] for i in range(len(texts))],
    )
    citations = [
        _cite(1, "녹차는 70~80도의 물에서 2분간 우린다"),
        _cite(2, "홍차는 끓는 물에 3~5분 우린다"),
    ]
    answer = "녹차는 70~80도 물에서 우립니다 [1] 반면 화성에는 외계인이 산다고 합니다 [2]"
    out = verify_citations_sync(citations, answer)
    assert out[0].verified is True
    assert out[1].verified is False


def test_verify_skips_unmarked_citations():
    """마커 없는 인용(폴백 전체 노출)은 verified 기본값 유지."""
    citations = [_cite(1, "내용")]
    out = verify_citations_sync(citations, "마커가 전혀 없는 답변입니다")
    assert out[0].verified is True


def test_citation_to_dict_includes_verified():
    c = _cite(1, "본문")
    c.verified = False
    assert c.to_dict()["verified"] is False


def test_filter_then_verify_pipeline():
    citations = [
        _cite(1, "에스프레소는 9기압으로 추출한다"),
        _cite(2, "사용되지 않은 발췌"),
    ]
    answer = "에스프레소는 9기압으로 추출합니다 [1]"
    final = verify_citations_sync(filter_cited(citations, answer), answer)
    assert [c.n for c in final] == [1]
    assert final[0].verified is True


# ---------- 인용 검증 async 래퍼(DB 설정 임계값 + 메트릭) ----------


async def test_verify_citations_async_uses_db_thresholds(db, monkeypatch):
    from biznotes_core.services.settings_store import set_setting

    await set_setting(
        db, "rag", {"verify_lexical_threshold": 0.9, "verify_embedding_threshold": 0.9}
    )
    await db.commit()
    monkeypatch.setattr(
        "biznotes_core.services.ingestion.embedder.embed_dense",
        lambda texts: [[1.0, 0.0] if i == 0 else [0.0, 1.0] for i in range(len(texts))],
    )
    citations = [_cite(1, "에스프레소는 9기압의 압력으로 25~30초간 추출한다")]
    # 어휘 겹침은 있지만 임계값 0.9는 통과 못함 → 임베딩 2차도 낮은 유사도라 미검증
    out = await verify_citations(db, citations, "에스프레소는 9기압으로 25~30초간 추출합니다 [1]")
    assert out[0].verified is False

    await set_setting(db, "rag", {"verify_lexical_threshold": 0.25})
    await db.commit()


# ---------- 리랭킹 통합 (retrieve_context) ----------


@dataclass
class FakePoint:
    id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


@pytest.fixture
def _fake_search(monkeypatch):
    """Embedding-space search boundary stub — no model download or vector query."""
    source_id = str(uuid.uuid4())
    points = [
        FakePoint(
            id=str(uuid.uuid4()),
            score=0.9,
            payload={"source_id": source_id, "text": f"청크 {i}"},
        )
        for i in range(20)
    ]
    captured: dict[str, Any] = {}

    async def fake_search_spaces(
        db, spaces, query, *, notebook_id=None, notebook_ids=None, source_ids=None, limit=8
    ):
        captured["limit"] = limit
        return points[:limit]

    monkeypatch.setattr(
        "biznotes_core.services.embedding_spaces.search_spaces", fake_search_spaces
    )
    # 리랭커: 역순 점수 (마지막 청크가 최고점) — 재정렬 검증용.
    # [codex D1] retrieve_context는 rerank_order가 아닌 rerank_scores를 직접 호출해
    # 재정렬 전 hybrid 점수가 아닌 실제 cross-encoder 점수로 insufficient_context를 판정한다.
    monkeypatch.setattr(
        "biznotes_core.services.rerank.rerank_scores",
        lambda query, texts, model_name="": list(range(len(texts))),
    )
    return captured


async def test_retrieve_without_rerank(db, user_a, _fake_search):
    from biznotes_core.models import Notebook

    nb = Notebook(owner_id=user_a.id, title="rr-off")
    db.add(nb)
    await db.commit()

    result = await retrieve_context(db, nb.id, "질문", None, top_k=5)
    assert _fake_search["limit"] == 5  # 리랭킹 꺼짐 → top_k만 조회
    assert [c.quote for c in result.citations][:2] == ["청크 0", "청크 1"]
    assert result.insufficient_context is False  # score=0.9 >= 기본 임계값


async def test_retrieve_with_rerank(db, user_a, _fake_search):
    from biznotes_core.models import Notebook
    from biznotes_core.services.settings_store import set_setting

    nb = Notebook(owner_id=user_a.id, title="rr-on")
    db.add(nb)
    await set_setting(db, "rag", {"rerank_enabled": True, "rerank_candidates": 20})
    await db.commit()

    try:
        result = await retrieve_context(db, nb.id, "질문", None, top_k=5)
        assert _fake_search["limit"] == 20  # 후보 확대 조회
        # 역순 리랭커 → 마지막 후보(청크 19)가 1번 인용
        assert result.citations[0].quote == "청크 19"
        assert result.citations[0].n == 1  # 재번호 부여
        assert len(result.citations) == 5
        # [codex D1] top-1의 판정은 재정렬 전 hybrid 점수(0.9)가 아닌 cross-encoder
        # 점수(19, 임계값 0.5보다 훨씬 큼)를 사용해야 함 — 두 값 모두 임계값을 넘지만
        # 임계값을 19 초과로 올리면 hybrid 점수 경로였다면 여전히 False가 나왔을 것.
        assert result.insufficient_context is False
    finally:
        await set_setting(db, "rag", {"rerank_enabled": False})
        await db.commit()


async def test_retrieve_with_rerank_uses_reranked_score_for_insufficient_context(
    db, user_a, monkeypatch
):
    """[codex D1·D6] insufficient_context 판정이 재정렬 전 hybrid 점수가 아니라
    sigmoid로 정규화한 cross-encoder 점수를 사용하는지 검증한다.

    fake hybrid 점수는 0.9(리랭킹 임계값 기본 0.5 초과)지만, cross-encoder raw
    logit은 -10(무경계 raw logit — 매우 비관련 판정)을 반환한다. hybrid 점수를
    썼다면 insufficient=False가 나왔겠지만, sigmoid(-10)≈0.00005 < 0.5이므로
    실제로는 insufficient=True가 나와야 한다.
    """
    from biznotes_core.models import Notebook
    from biznotes_core.services.settings_store import set_setting

    source_id = str(uuid.uuid4())
    points = [
        FakePoint(
            id=str(uuid.uuid4()), score=0.9, payload={"source_id": source_id, "text": "청크 0"}
        )
    ]
    monkeypatch.setattr(
        "biznotes_core.services.embedding_spaces.search_spaces",
        _search_returning(points),
    )
    monkeypatch.setattr(
        "biznotes_core.services.rerank.rerank_scores", lambda query, texts, model_name="": [-10.0]
    )

    nb = Notebook(owner_id=user_a.id, title="rr-score-check")
    db.add(nb)
    await set_setting(
        db,
        "rag",
        {"rerank_enabled": True, "insufficient_context_threshold_rerank": 0.5},
    )
    await db.commit()

    try:
        result = await retrieve_context(db, nb.id, "질문", None, top_k=5)
        assert result.insufficient_context is True
    finally:
        await set_setting(
            db,
            "rag",
            {"rerank_enabled": False, "insufficient_context_threshold_rerank": 0.5},
        )
        await db.commit()


async def test_rerank_reranks_even_when_points_leq_top_k(db, user_a, monkeypatch):
    """[codex D4·D6] 후보 수가 top_k 이하(작은 노트북·좁은 소스 필터)라도 리랭킹이
    켜져 있으면 재점수를 매겨 insufficient_context가 hybrid 점수가 아닌 실제
    sigmoid 정규화된 cross-encoder 점수를 기준으로 판정되어야 한다."""
    from biznotes_core.models import Notebook
    from biznotes_core.services.settings_store import set_setting

    source_id = str(uuid.uuid4())
    points = [
        FakePoint(
            id=str(uuid.uuid4()), score=0.9, payload={"source_id": source_id, "text": "청크 0"}
        )
    ]  # top_k(5)보다 적은 후보 1건뿐
    monkeypatch.setattr(
        "biznotes_core.services.embedding_spaces.search_spaces",
        _search_returning(points),
    )
    # cross-encoder raw logit -10 — sigmoid(-10)≈0.00005, hybrid 점수(0.9)보다 훨씬 낮음
    monkeypatch.setattr(
        "biznotes_core.services.rerank.rerank_scores", lambda query, texts, model_name="": [-10.0]
    )

    nb = Notebook(owner_id=user_a.id, title="rr-small-set")
    db.add(nb)
    await set_setting(
        db,
        "rag",
        {"rerank_enabled": True, "insufficient_context_threshold_rerank": 0.5},
    )
    await db.commit()

    try:
        result = await retrieve_context(db, nb.id, "질문", None, top_k=5)
        # hybrid 점수(0.9)를 썼다면 False가 나왔을 것 — cross-encoder 점수(0.1)를
        # 써야 임계값(0.5) 미달로 True가 나온다.
        assert result.insufficient_context is True
    finally:
        await set_setting(
            db,
            "rag",
            {"rerank_enabled": False, "insufficient_context_threshold_rerank": 0.5},
        )
        await db.commit()


# ---------- 어드민 설정 ----------


async def test_rag_settings_roundtrip(client: httpx.AsyncClient, admin_user, db):
    token = await login(client, admin_user.email)
    res = await client.get("/api/v1/admin/rag/settings", headers=bearer(token))
    assert res.status_code == 200
    body = res.json()
    assert "rerank_enabled" in body and "rerank_candidates" in body

    body["rerank_enabled"] = True
    body["rerank_candidates"] = 30
    res = await client.put("/api/v1/admin/rag/settings", json=body, headers=bearer(token))
    assert res.status_code == 200
    assert res.json()["rerank_enabled"] is True
    assert res.json()["rerank_candidates"] == 30

    # 원복
    body["rerank_enabled"] = False
    body["rerank_candidates"] = 20
    await client.put("/api/v1/admin/rag/settings", json=body, headers=bearer(token))
