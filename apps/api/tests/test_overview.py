"""노트북 개요(요약+추천 질문) 테스트 — mock LLM, Celery/Redis 모킹."""

import uuid

import httpx
import pytest
from biznotes_core.services.overview import (
    _parse_overview_response,
    compute_staleness,
    generate_overview,
)
from biznotes_core.workers.tasks.overview import _run as run_overview_task

from tests.conftest import bearer, login, share_group


@pytest.fixture(autouse=True)
def _mock_schedule(monkeypatch):
    """Celery/Redis 디바운스 예약 차단 — 예약 호출 여부만 기록.

    코어 v0.22.0 #326부터 API 라우터는 `services.overview_refresh`의 헬퍼를 부르고, 워커 경로는
    `workers.tasks.overview`의 재노출을 부른다 — 두 지점을 같은 기록기로 막는다.
    """
    calls: list[str] = []

    def _record(notebook_id, debounce=True, countdown=10):
        calls.append(notebook_id)

    monkeypatch.setattr(
        "biznotes_core.services.overview_refresh.schedule_overview_refresh", _record
    )
    monkeypatch.setattr("biznotes_core.workers.tasks.overview.schedule_overview_refresh", _record)
    return calls


@pytest.fixture
async def llm_seed(db):
    from biznotes_core.models import LLMModel, LLMProvider

    provider = LLMProvider(name=f"mock-{uuid.uuid4().hex[:6]}", type="mock", enabled=True)
    db.add(provider)
    await db.flush()
    model = LLMModel(
        provider_id=provider.id,
        model_name="mock-overview",
        display_name="Mock Overview",
        capabilities=["llm"],
        is_default=True,
        enabled=True,
    )
    db.add(model)
    await db.commit()
    return model


async def _make_notebook_with_ready_source(db, user) -> uuid.UUID:
    from biznotes_core.models import Notebook, Source, SourceChunk

    nb = Notebook(owner_id=user.id, title="overview-nb")
    db.add(nb)
    await db.flush()
    source = Source(notebook_id=nb.id, type="text", title="녹차 가이드", status="ready")
    db.add(source)
    await db.flush()
    db.add(
        SourceChunk(
            source_id=source.id,
            seq=0,
            text="녹차는 70~80도의 물에서 2분간 우린다. 홍차는 끓는 물에 3~5분.",
            meta={},
        )
    )
    await db.commit()
    return nb.id


def test_parse_overview_json():
    raw = (
        '{"summary": "차 우리기 요약.", "questions": ["녹차 온도는?", "홍차 시간은?"], '
        '"title_suggestions": ["차 우리기 가이드"]}'
    )
    summary, questions, titles = _parse_overview_response(raw, ["소스"])
    assert summary == "차 우리기 요약."
    assert questions == ["녹차 온도는?", "홍차 시간은?"]
    assert titles == ["차 우리기 가이드"]


def test_parse_overview_fallback():
    raw = "JSON이 아닌 자유 텍스트 요약입니다."
    summary, questions, titles = _parse_overview_response(raw, ["녹차 가이드", "홍차 노트"])
    assert summary == raw
    assert questions == [
        "녹차 가이드의 핵심 내용은 무엇인가요?",
        "홍차 노트의 핵심 내용은 무엇인가요?",
    ]
    assert titles == []


def test_parse_overview_json_without_title_suggestions():
    """title_suggestions 필드가 없어도(구버전 응답 등) 빈 배열로 안전하게 처리."""
    raw = '{"summary": "요약.", "questions": ["질문?"]}'
    summary, questions, titles = _parse_overview_response(raw, ["소스"])
    assert summary == "요약."
    assert titles == []


async def test_generate_overview_with_mock_llm(db, user_a, llm_seed):
    nb_id = await _make_notebook_with_ready_source(db, user_a)

    overview = await generate_overview(db, nb_id)
    assert overview is not None
    assert overview.summary  # mock 응답 폴백 경로 — 비어있지 않음
    assert len(overview.suggested_questions) >= 1
    assert overview.source_count == 1
    assert overview.generated_at is not None

    # 생성 직후 stale 아님
    assert await compute_staleness(db, nb_id, overview) is False


async def test_generate_overview_stores_title_suggestions_field(db, user_a, llm_seed):
    """mock LLM은 JSON을 생성하지 않아 폴백 경로를 타므로 title_suggestions는 빈 배열이지만,
    필드 자체가 저장·조회 가능해야 한다(§3.3)."""
    nb_id = await _make_notebook_with_ready_source(db, user_a)
    overview = await generate_overview(db, nb_id)
    assert overview is not None
    assert overview.title_suggestions == []


async def test_generate_overview_no_sources(db, user_a, llm_seed):
    from biznotes_core.models import Notebook

    nb = Notebook(owner_id=user_a.id, title="empty-nb")
    db.add(nb)
    await db.commit()
    assert await generate_overview(db, nb.id) is None


async def test_overview_task_skips_deleted_notebook(monkeypatch):
    """코어 v0.15.1 #220(core-v0.16.0 AC-1.3 ⑥) — 예약 뒤 지워진 노트북의 개요 태스크는
    정상 종료한다.

    개요 생성(LLM)을 부르지 않고 `skipped: notebook_deleted`를 돌려준다(ERROR 로그·재시도 없음).
    """
    generated: list[object] = []

    async def _spy(db, notebook_id):
        generated.append(notebook_id)

    monkeypatch.setattr("biznotes_core.services.overview.generate_overview", _spy)

    result = await run_overview_task(str(uuid.uuid4()))

    assert result == {"ok": True, "skipped": "notebook_deleted"}
    assert generated == []


async def test_staleness_on_source_change(db, user_a, llm_seed):
    from biznotes_core.models import Source

    nb_id = await _make_notebook_with_ready_source(db, user_a)
    overview = await generate_overview(db, nb_id)

    # 새 ready 소스 추가 → 소스 수 불일치 → stale
    db.add(Source(notebook_id=nb_id, type="text", title="추가 소스", status="ready"))
    await db.commit()
    assert await compute_staleness(db, nb_id, overview) is True


async def test_overview_api(client: httpx.AsyncClient, user_a, llm_seed, db, _mock_schedule):
    token = await login(client, user_a.email)
    res = await client.post(
        "/api/v1/notebooks", json={"title": "ov-api"}, headers=bearer(token)
    )
    nb_id = res.json()["id"]

    # 소스 없음 → stale 아님(생성할 것도 없음), 예약 없음
    res = await client.get(f"/api/v1/notebooks/{nb_id}/overview", headers=bearer(token))
    assert res.status_code == 200
    body = res.json()
    assert body["summary"] is None
    assert body["stale"] is False
    assert _mock_schedule == []

    # ready 소스 추가 → stale → 재생성 예약됨
    from biznotes_core.models import Source

    db.add(Source(notebook_id=uuid.UUID(nb_id), type="text", title="자료", status="ready"))
    await db.commit()
    res = await client.get(f"/api/v1/notebooks/{nb_id}/overview", headers=bearer(token))
    body = res.json()
    assert body["stale"] is True
    assert _mock_schedule == [nb_id]


async def test_overview_api_exposes_title_suggestions(
    client: httpx.AsyncClient, user_a, llm_seed, db
):
    from biznotes_core.models import NotebookOverview

    token = await login(client, user_a.email)
    nb_id = await _make_notebook_with_ready_source(db, user_a)

    overview = NotebookOverview(
        notebook_id=nb_id,
        summary="요약",
        suggested_questions=["질문?"],
        title_suggestions=["추천 제목 1", "추천 제목 2"],
        source_count=1,
    )
    from datetime import UTC, datetime

    overview.generated_at = datetime.now(UTC)
    db.add(overview)
    await db.commit()

    res = await client.get(f"/api/v1/notebooks/{nb_id}/overview", headers=bearer(token))
    assert res.status_code == 200
    assert res.json()["title_suggestions"] == ["추천 제목 1", "추천 제목 2"]


async def test_overview_api_permission(client: httpx.AsyncClient, user_a, user_b):
    token_a = await login(client, user_a.email)
    client.cookies.clear()
    token_b = await login(client, user_b.email)
    client.cookies.clear()
    res = await client.post(
        "/api/v1/notebooks", json={"title": "ov-perm"}, headers=bearer(token_a)
    )
    nb_id = res.json()["id"]
    res = await client.get(f"/api/v1/notebooks/{nb_id}/overview", headers=bearer(token_b))
    assert res.status_code == 404


async def test_overview_regenerate(client: httpx.AsyncClient, user_a, _mock_schedule):
    """수동 재생성 — 202 + 예약, 락 유지 중 중복 요청은 already-running."""
    token = await login(client, user_a.email)
    client.cookies.clear()
    res = await client.post(
        "/api/v1/notebooks", json={"title": "ov-regen"}, headers=bearer(token)
    )
    nb_id = res.json()["id"]

    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/overview/regenerate", headers=bearer(token)
    )
    assert res.status_code == 202
    assert res.json()["status"] == "scheduled"
    assert _mock_schedule == [nb_id]

    # 락이 살아있는 동안 중복 요청은 enqueue하지 않음
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/overview/regenerate", headers=bearer(token)
    )
    assert res.json()["status"] == "already-running"
    assert _mock_schedule == [nb_id]

    # 정리 — 다른 테스트에 락이 남지 않도록
    from redis.asyncio import Redis

    from app.core.config import get_settings

    redis = Redis.from_url(get_settings().redis_url)
    await redis.delete(f"overview:regen:{nb_id}")
    await redis.aclose()


async def test_overview_regenerate_requires_write(
    client: httpx.AsyncClient, db, user_a, user_b, _mock_schedule
):
    """무관 사용자는 단건 GET과 같은 404 `Notebook not found`, read 공유자는 403
    `Write permission required`다(코어 v0.19.0 #269, core-v0.20.0-upgrade AC-1.8 ④). 둘 다 재생성을
    예약하지 않는다."""
    token_a = await login(client, user_a.email)
    res = await client.post(
        "/api/v1/notebooks", json={"title": "ov-regen-perm"}, headers=bearer(token_a)
    )
    nb_id = res.json()["id"]

    token_b = await login(client, user_b.email)
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/overview/regenerate", headers=bearer(token_b)
    )
    assert (res.status_code, res.json()["detail"]) == (404, "Notebook not found")

    token_a = await login(client, user_a.email)
    group = await share_group(db, user_a, user_b)
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/share",
        json={"group_id": str(group.id), "permission": "read"},
        headers=bearer(token_a),
    )
    assert res.status_code == 201, res.text
    token_b = await login(client, user_b.email)
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/overview/regenerate", headers=bearer(token_b)
    )
    assert (res.status_code, res.json()["detail"]) == (403, "Write permission required")
    assert _mock_schedule == []
