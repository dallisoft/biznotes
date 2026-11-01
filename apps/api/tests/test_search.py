"""Phase 13a 전체 검색 · 16a 시맨틱 검색 테스트."""

import uuid
from typing import Any

import httpx
import pytest
from biznotes_core.models import Note, Notebook, Source
from sqlalchemy import select

from tests.conftest import bearer, login


async def _login(client, email):
    token = await login(client, email)
    client.cookies.clear()
    return token


# ---------- 13a 전체 검색 ----------


@pytest.fixture
async def search_fixture(db, user_a):
    nb = Notebook(owner_id=user_a.id, title="커피 연구 노트북", description="원두 비교")
    other = Notebook(owner_id=user_a.id, title="여행 기록")
    db.add_all([nb, other])
    await db.flush()
    db.add(Source(notebook_id=nb.id, type="text", title="에스프레소 추출 가이드", status="ready"))
    db.add(
        Note(
            notebook_id=nb.id,
            author_id=user_a.id,
            title="커피 회의록",
            content="다음 분기에는 콜드브루 원두를 바꾸기로 결정했다.",
        )
    )
    await db.commit()
    return nb


async def test_search_finds_all_types(client: httpx.AsyncClient, user_a, search_fixture):
    token = await _login(client, user_a.email)

    res = await client.get("/api/v1/search", params={"q": "커피"}, headers=bearer(token))
    assert res.status_code == 200
    body = res.json()
    assert any(n["title"] == "커피 연구 노트북" for n in body["notebooks"])
    assert any(n["title"] == "커피 회의록" for n in body["notes"])

    res = await client.get("/api/v1/search", params={"q": "에스프레소"}, headers=bearer(token))
    assert any(s["title"] == "에스프레소 추출 가이드" for s in res.json()["sources"])

    # 메모 본문 매치 + 스니펫
    res = await client.get("/api/v1/search", params={"q": "콜드브루"}, headers=bearer(token))
    notes = res.json()["notes"]
    assert len(notes) == 1 and "콜드브루" in notes[0]["snippet"]


async def test_search_respects_permissions(client: httpx.AsyncClient, user_b, search_fixture):
    """비공유 사용자에게는 어떤 타입도 노출되지 않는다."""
    token_b = await _login(client, user_b.email)
    res = await client.get("/api/v1/search", params={"q": "커피"}, headers=bearer(token_b))
    body = res.json()
    assert body["notebooks"] == [] and body["sources"] == [] and body["notes"] == []


# ---------- 16a 시맨틱 검색 ----------


class _FakePoint:
    def __init__(self, pid: str, payload: dict[str, Any], score: float):
        self.id = pid
        self.payload = payload
        self.score = score


@pytest.fixture
def fake_semantic(monkeypatch, db):
    """semantic_search를 캔드 결과로 교체 — 호출 인자(notebook_ids)를 캡처."""
    from biznotes_core.api.v1 import search as search_module

    captured: dict[str, Any] = {}

    async def _fake(db, notebook_ids, q, limit=8, spaces=None):
        captured["notebook_ids"] = list(notebook_ids)
        captured["q"] = q
        return captured.get("points", [])

    monkeypatch.setattr(search_module, "semantic_search", _fake)
    return captured


async def test_semantic_mode_returns_chunk_hits(
    client: httpx.AsyncClient, user_a, search_fixture, fake_semantic, db
):
    token = await _login(client, user_a.email)
    nb = search_fixture
    source = (await db.execute(select(Source).where(Source.notebook_id == nb.id))).scalars().first()
    fake_semantic["points"] = [
        _FakePoint(
            str(uuid.uuid4()),
            {
                "notebook_id": str(nb.id),
                "source_id": str(source.id),
                "text": "에스프레소는  9기압으로   추출한다",
            },
            0.91,
        )
    ]

    res = await client.get(
        "/api/v1/search", params={"q": "추출 압력", "mode": "semantic"}, headers=bearer(token)
    )
    assert res.status_code == 200
    body = res.json()
    # semantic 모드는 청크만 반환 (키워드 그룹은 비움)
    assert body["notebooks"] == [] and body["sources"] == [] and body["notes"] == []
    assert len(body["chunks"]) == 1
    hit = body["chunks"][0]
    assert hit["source_title"] == "에스프레소 추출 가이드"
    assert hit["notebook_id"] == str(nb.id)
    assert "9기압" in hit["snippet"] and "  " not in hit["snippet"]  # 공백 정규화
    assert fake_semantic["q"] == "추출 압력"


async def test_semantic_mode_passes_only_accessible_notebooks(
    client: httpx.AsyncClient, user_a, user_b, search_fixture, fake_semantic
):
    """권한 필터 — semantic_search에는 접근 가능한 노트북 id만 전달된다."""
    token_b = await _login(client, user_b.email)
    res = await client.get(
        "/api/v1/search", params={"q": "커피", "mode": "semantic"}, headers=bearer(token_b)
    )
    assert res.status_code == 200
    # user_b는 접근 가능한 노트북이 없음 → semantic_search 호출 자체가 생략
    assert res.json()["chunks"] == []
    assert "notebook_ids" not in fake_semantic

    token_a = await _login(client, user_a.email)
    await client.get(
        "/api/v1/search", params={"q": "커피", "mode": "semantic"}, headers=bearer(token_a)
    )
    assert str(search_fixture.id) in [str(n) for n in fake_semantic["notebook_ids"]]


async def test_keyword_mode_skips_semantic(
    client: httpx.AsyncClient, user_a, search_fixture, fake_semantic
):
    token = await _login(client, user_a.email)
    res = await client.get("/api/v1/search", params={"q": "커피"}, headers=bearer(token))
    assert res.status_code == 200
    assert res.json()["chunks"] == []
    assert "q" not in fake_semantic  # 임베딩 경로 미호출
