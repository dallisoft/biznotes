"""소스 가이드(요약 + 키워드) 테스트 — mock LLM, 온디맨드 생성·캐시."""

import uuid

import httpx
import pytest
from biznotes_core.services.source_guide import _parse_guide_response, generate_source_guide

from tests.conftest import bearer, login


@pytest.fixture
async def llm_seed(db):
    from biznotes_core.models import LLMModel, LLMProvider

    provider = LLMProvider(name=f"mock-{uuid.uuid4().hex[:6]}", type="mock", enabled=True)
    db.add(provider)
    await db.flush()
    model = LLMModel(
        provider_id=provider.id,
        model_name="mock-guide",
        display_name="Mock Guide",
        capabilities=["llm"],
        is_default=True,
        enabled=True,
    )
    db.add(model)
    await db.commit()
    return model


async def _ready_source_with_chunk(db, user):
    from biznotes_core.models import Notebook, Source, SourceChunk

    nb = Notebook(owner_id=user.id, title="guide-nb")
    db.add(nb)
    await db.flush()
    source = Source(notebook_id=nb.id, type="text", title="녹차 가이드", status="ready")
    db.add(source)
    await db.flush()
    db.add(
        SourceChunk(
            source_id=source.id,
            seq=0,
            text="녹차는 70~80도의 물에서 2분간 우린다. 카테킨이 풍부하다.",
            meta={},
        )
    )
    await db.commit()
    return nb, source


def test_parse_guide_json():
    raw = '{"summary": "녹차 우리기 요약.", "keywords": ["녹차", "우림 온도", "카테킨"]}'
    summary, keywords = _parse_guide_response(raw, "폴백")
    assert summary == "녹차 우리기 요약."
    assert keywords == ["녹차", "우림 온도", "카테킨"]


def test_parse_guide_fallback():
    raw = "JSON이 아닌 자유 텍스트입니다."
    summary, keywords = _parse_guide_response(raw, "폴백 요약")
    assert summary == raw
    assert keywords == []


async def test_generate_source_guide_with_mock_llm(db, user_a, llm_seed):
    _, source = await _ready_source_with_chunk(db, user_a)

    updated = await generate_source_guide(db, source)
    assert updated.summary  # mock 응답 폴백 경로 — 비어있지 않음
    assert isinstance(updated.keywords, list)


async def test_guide_endpoint_generates_and_caches(
    client: httpx.AsyncClient, user_a, llm_seed, db
):
    _, source = await _ready_source_with_chunk(db, user_a)
    token = await login(client, user_a.email)

    res = await client.get(f"/api/v1/sources/{source.id}/guide", headers=bearer(token))
    assert res.status_code == 200
    body = res.json()
    assert body["summary"]
    assert isinstance(body["keywords"], list)

    # 캐시됨 — DB에 요약 저장
    await db.refresh(source)
    assert source.summary is not None


async def test_guide_endpoint_permission(client: httpx.AsyncClient, user_a, user_b, db, llm_seed):
    _, source = await _ready_source_with_chunk(db, user_a)
    client.cookies.clear()
    token_b = await login(client, user_b.email)
    res = await client.get(f"/api/v1/sources/{source.id}/guide", headers=bearer(token_b))
    assert res.status_code == 404
