"""보안 통합 테스트 — 업로드 매직바이트, 내부망 URL 거부, 메트릭 노출.

SSRF 관문 자체의 단위 테스트는 구현과 함께 biznotes-core가 소유한다
(`biznotes-core/tests/test_ssrf.py`).
"""

import httpx

from tests.conftest import bearer, login


async def test_web_source_rejects_internal_url(client: httpx.AsyncClient, user_a, monkeypatch):
    class FakeTask:
        id = "t"

    monkeypatch.setattr(
        "biznotes_core.workers.tasks.ingestion.ingest_source.delay",
        lambda sid: FakeTask(),
    )
    monkeypatch.setattr(
        "biznotes_core.workers.tasks.ingestion.ingest_source.apply_async",
        lambda *a, **k: FakeTask(),
    )
    token = await login(client, user_a.email)
    nb = await client.post("/api/v1/notebooks", json={"title": "ssrf-nb"}, headers=bearer(token))
    nb_id = nb.json()["id"]

    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources",
        json={"type": "web", "url": "http://169.254.169.254/latest/meta-data"},
        headers=bearer(token),
    )
    assert res.status_code == 422
    assert "차단" in res.json()["detail"] or "내부망" in res.json()["detail"]


# ---------- 매직바이트 ----------


async def test_upload_rejects_fake_pdf(client: httpx.AsyncClient, user_a, monkeypatch):
    monkeypatch.setattr("biznotes_core.api.v1.sources.ensure_buckets", lambda: None)
    monkeypatch.setattr("biznotes_core.api.v1.sources.put_object", lambda *a, **k: None)

    token = await login(client, user_a.email)
    nb = await client.post("/api/v1/notebooks", json={"title": "magic-nb"}, headers=bearer(token))
    nb_id = nb.json()["id"]

    # 텍스트를 .pdf로 위장
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources/upload",
        files={"file": ("fake.pdf", b"this is not a pdf", "application/pdf")},
        headers=bearer(token),
    )
    assert res.status_code == 422
    assert "일치하지 않습니다" in res.json()["detail"]


# ---------- 메트릭 ----------


async def test_metrics_endpoint(client: httpx.AsyncClient):
    res = await client.get("/metrics")
    assert res.status_code == 200
    assert "http_request" in res.text or "python_info" in res.text
