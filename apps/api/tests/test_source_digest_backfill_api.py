"""소스 요약 백필 관리 경로(코어 v0.30.0 #454) 조립 — #285, core-v0.30.0-upgrade AC-10.

경계만 대신한다: 브로커 발행(`celery_app.send_task`). Redis(진행률·대기 표식)는 워커별 테스트 DB를
그대로 쓴다. 일반 사용자는 403(비인가), 없는 노트북은 404, 같은 범위를 두 번 시작하면 두 번째는
발행하지 않는다(`scheduled: false`).
"""

import uuid

import httpx
import pytest
from biznotes_core.services import source_digest_backfill as backfill
from biznotes_core.workers import task_names
from biznotes_core.workers.celery_app import celery_app

from tests.conftest import bearer, login

PATH = "/api/v1/admin/source-digests/backfill"


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, list]]:
    calls: list[tuple[str, list]] = []

    def fake_send_task(name, args=None, kwargs=None, **options):
        calls.append((name, list(args or [])))

    monkeypatch.setattr(celery_app, "send_task", fake_send_task)
    return calls


@pytest.fixture
async def _clear_pending():
    """테스트 사이에 Redis 대기 표식·진행률이 남지 않게 지운다(같은 워커 DB를 다시 쓴다)."""

    async def clear():
        client = backfill.backfill_redis()
        try:
            keys = [k async for k in client.scan_iter(match="*source_digest*backfill*")]
            if keys:
                await client.delete(*keys)
        finally:
            await client.aclose()

    await clear()
    yield
    await clear()


async def _notebook(client: httpx.AsyncClient, token: str) -> str:
    res = await client.post("/api/v1/notebooks", json={"title": "bf"}, headers=bearer(token))
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def test_backfill_routes_are_admin_only(client: httpx.AsyncClient, user_a, sent):
    token = await login(client, user_a.email)
    res = await client.post(PATH, json={}, headers=bearer(token))
    assert res.status_code == 403
    res = await client.get(PATH, headers=bearer(token))
    assert res.status_code == 403
    assert sent == []


async def test_admin_starts_notebook_backfill_once_and_reads_progress(
    client: httpx.AsyncClient, admin_user, sent, _clear_pending
):
    token = await login(client, admin_user.email)
    nb_id = await _notebook(client, token)

    res = await client.post(PATH, json={"notebook_id": nb_id}, headers=bearer(token))
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["scheduled"] is True
    assert body["progress"]["status"] == "queued"
    assert sent == [(task_names.BACKFILL_SOURCE_DIGESTS, [nb_id])]

    # 같은 범위가 대기 중이면 새로 발행하지 않는다
    res = await client.post(PATH, json={"notebook_id": nb_id}, headers=bearer(token))
    assert res.status_code == 202
    assert res.json()["scheduled"] is False
    assert len(sent) == 1

    res = await client.get(PATH, params={"notebook_id": nb_id}, headers=bearer(token))
    assert res.status_code == 200
    assert res.json()["progress"]["status"] == "queued"


async def test_admin_backfill_unknown_notebook_is_404(
    client: httpx.AsyncClient, admin_user, sent, _clear_pending
):
    token = await login(client, admin_user.email)
    res = await client.post(PATH, json={"notebook_id": str(uuid.uuid4())}, headers=bearer(token))
    assert res.status_code == 404
    assert sent == []


async def test_admin_starts_all_notebooks_backfill(
    client: httpx.AsyncClient, admin_user, sent, _clear_pending
):
    token = await login(client, admin_user.email)
    res = await client.post(PATH, json={}, headers=bearer(token))
    assert res.status_code == 202, res.text
    assert res.json()["scope"] == backfill.backfill_scope(None)
    assert sent == [(task_names.BACKFILL_SOURCE_DIGESTS, [None])]
