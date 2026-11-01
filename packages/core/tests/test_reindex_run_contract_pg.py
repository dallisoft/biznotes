# 신규(#179, docs/plans/reindex-run-contract/). 필요: PostgreSQL·Valkey(PG 테스트).
"""재인덱싱 run 계약 — 후보 수·범위 스냅샷·활성 필터, 그리고 소스가 지워져도 집계가 맞는다.

실제 라우터(재인덱싱·소스·노트북·사용자 삭제)와 PostgreSQL FK 동작(`ON DELETE SET NULL`)으로
확인한다. 브로커 발행·스토리지·벡터 정리만 대역이다. `create_all` 기반이라 Alembic 마이그레이션
검증이 아니다.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import structlog.testing
from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import select, update

from biznotes_core.api.deps import require_admin
from biznotes_core.api.v1 import auth, notebooks, sources
from biznotes_core.api.v1.admin import rag, users
from biznotes_core.core.db import async_session_factory
from biznotes_core.models import Notebook, ReindexRun, ReindexRunItem, Source, User
from biznotes_core.services.ingestion.reindex import close_reindex_items, stalled_reindex_items
from biznotes_core.workers.celery_app import celery_app

PASSWORD = "pw-test-1234"  # tests/conftest.py 공용 해시의 평문


def _app() -> FastAPI:
    app = FastAPI()
    admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
    admin.include_router(rag.router)
    admin.include_router(users.router)
    for router in (auth.router, notebooks.router, sources.router, admin):
        app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture(autouse=True)
def _boundaries(monkeypatch: pytest.MonkeyPatch, local_storage: Any) -> None:
    # 오브젝트 정리는 빈 임시 로컬 스토리지로 간다
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: None)


@pytest.fixture
async def api(admin_user: User) -> AsyncIterator[Any]:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/auth/login", json={"email": admin_user.email, "password": PASSWORD}
        )
        assert res.status_code == 200, res.text
        token = res.json()["access_token"]

        async def call(method: str, path: str, **kw: Any) -> httpx.Response:
            client.cookies.clear()
            headers = {"Authorization": f"Bearer {token}"}
            return await client.request(method, f"/api/v1{path}", headers=headers, **kw)

        yield call


async def _notebook(owner: User, title: str, statuses: list[str]) -> tuple[uuid.UUID, list]:
    async with async_session_factory() as s:
        nb = Notebook(owner_id=owner.id, title=title)
        s.add(nb)
        await s.flush()
        srcs = [
            Source(notebook_id=nb.id, type="text", title=f"s{i}", status=st)
            for i, st in enumerate(statuses)
        ]
        s.add_all(srcs)
        await s.commit()
        return nb.id, [x.id for x in srcs]


async def _assert_active_matches_unfinished(api: Any) -> None:
    """active_only 결과 = finished_at이 없는 run(리뷰 제안 2)."""
    active = (await api("GET", "/admin/rag/reindex", params={"active_only": "true"})).json()
    everything = (await api("GET", "/admin/rag/reindex", params={"limit": 100})).json()
    assert {r["id"] for r in active} == {r["id"] for r in everything if r["finished_at"] is None}


def _balanced(run: dict) -> bool:
    return run["requested"] == run["queued"] + run["done"] + run["failed"] + run["removed"]


async def test_run_contract_survives_source_notebook_and_user_deletes(api, user_a, user_b):
    nb, (s1, s2, s3, busy) = await _notebook(
        user_a, "재인덱스 노트북", ["ready", "ready", "ready", "parsing"]
    )
    _, (t1,) = await _notebook(user_b, "남의 노트북", ["ready"])

    started = await api("POST", "/admin/rag/reindex", json={"notebook_id": str(nb)})
    assert started.json()["enqueued"] == 3 and started.json()["skipped"] == 1
    run_id = started.json()["run_id"]

    run = (await api("GET", f"/admin/rag/reindex/{run_id}")).json()
    assert {k: run[k] for k in ("requested", "skipped", "candidates", "queued", "removed")} == {
        "requested": 3,
        "skipped": 1,  # 처리 중(parsing)이라 건너뛴 후보
        "candidates": 4,
        "queued": 3,
        "removed": 0,
    }
    assert (run["scope"], run["notebook_title"], run["notebook_id"]) == (
        "notebook",
        "재인덱스 노트북",
        str(nb),
    )
    assert run["finished_at"] is None and run["last_update"] is not None
    assert (run["stale_only"], run["retrying"], run["max_attempts"]) == (False, 0, 3)

    # 전체 범위 run — 남의 노트북 소스(t1)만 새로 담긴다(나머지는 이미 대기 중이거나 처리 중)
    whole = (await api("POST", "/admin/rag/reindex", json={})).json()
    whole_run = (await api("GET", f"/admin/rag/reindex/{whole['run_id']}")).json()
    assert (whole_run["scope"], whole_run["notebook_title"], whole_run["requested"]) == (
        "all",
        None,
        1,
    )
    # 후보 5 = t1 + 다른 run에서 대기 중인 셋 + 처리 중 하나
    assert (whole_run["skipped"], whole_run["candidates"]) == (4, 5)

    # 하나는 처리되고, 하나는 처리 전에 소스 단건 삭제된다
    async with async_session_factory() as s:
        await close_reindex_items(s, s1, ok=True)
    assert (await api("DELETE", f"/sources/{s2}")).status_code == 204
    run = (await api("GET", f"/admin/rag/reindex/{run_id}")).json()
    assert (run["queued"], run["done"], run["removed"]) == (1, 1, 1)
    assert _balanced(run) and run["finished_at"] is None
    active = (await api("GET", "/admin/rag/reindex", params={"active_only": "true"})).json()
    assert {r["id"] for r in active} == {run_id, whole["run_id"]}
    await _assert_active_matches_unfinished(api)

    # 노트북 삭제(DB CASCADE) — 남은 항목도 제외되고 run은 완료로 끝난다. 범위 스냅샷은 남는다
    assert (await api("DELETE", f"/notebooks/{nb}")).status_code == 204
    run = (await api("GET", f"/admin/rag/reindex/{run_id}")).json()
    assert (run["queued"], run["done"], run["removed"]) == (0, 1, 2)
    assert _balanced(run) and run["finished_at"] is not None
    assert (run["scope"], run["notebook_title"], run["notebook_id"]) == (
        "notebook",
        "재인덱스 노트북",
        None,
    )

    # 사용자 삭제(users → notebooks → sources CASCADE)
    assert (await api("DELETE", f"/admin/users/{user_b.id}")).status_code == 204
    whole_run = (await api("GET", f"/admin/rag/reindex/{whole['run_id']}")).json()
    assert (whole_run["queued"], whole_run["removed"]) == (0, 1)
    assert _balanced(whole_run) and whole_run["finished_at"] is not None

    active = (await api("GET", "/admin/rag/reindex", params={"active_only": "true"})).json()
    assert active == []
    await _assert_active_matches_unfinished(api)
    assert {r["id"] for r in (await api("GET", "/admin/rag/reindex")).json()} >= {run_id}


async def test_active_only_finds_runs_beyond_recent_limit(api, user_a):
    nb, (src,) = await _notebook(user_a, "오래 걸리는", ["ready"])
    active_id = (await api("POST", "/admin/rag/reindex", json={"notebook_id": str(nb)})).json()[
        "run_id"
    ]
    # 끝난 run 두 건을 더 최근에 만든다
    async with async_session_factory() as s:
        for minutes in (1, 2):
            s.add(
                ReindexRun(
                    notebook_id=None,
                    options={},
                    requested=0,
                    skipped=0,
                    scope="all",
                    created_at=datetime.now(UTC) + timedelta(minutes=minutes),
                )
            )
        await s.commit()

    recent = (await api("GET", "/admin/rag/reindex", params={"limit": 1})).json()
    assert [r["id"] for r in recent] != [active_id]
    active = (
        await api("GET", "/admin/rag/reindex", params={"limit": 1, "active_only": "true"})
    ).json()
    assert [r["id"] for r in active] == [active_id]


async def test_removed_items_are_not_reissued_or_deferred(api, user_a):
    nb, (gone, kept) = await _notebook(user_a, "정체", ["ready", "ready"])
    run_id = (await api("POST", "/admin/rag/reindex", json={"notebook_id": str(nb)})).json()[
        "run_id"
    ]
    async with async_session_factory() as s:
        before = await s.scalar(
            select(ReindexRunItem.updated_at).where(ReindexRunItem.source_id == gone)
        )
    assert (await api("DELETE", f"/sources/{gone}")).status_code == 204
    async with async_session_factory() as s:
        # SET NULL은 updated_at을 바꾸지 않는다 — 삭제가 대기열 흐름 신호로 보이지 않는다
        after = await s.scalar(
            select(ReindexRunItem.updated_at).where(
                ReindexRunItem.run_id == uuid.UUID(run_id), ReindexRunItem.source_id.is_(None)
            )
        )
        assert after == before
    async with async_session_factory() as s:
        # 남은 항목은 방금 처리됐다(대기열이 흐르는 중) — 이제 열린 항목은 지워진 소스의 것뿐이고,
        # 그 항목은 오래 멈춘 것처럼 보이게 한다. 지워진 소스의 항목을 열린 항목으로 세면
        # "회수 보류" 경고가 스윕마다 나고, 절대 상한(hard_cutoff)에서 재발행 대상을 찾는다
        await close_reindex_items(s, kept, ok=True)
        old = datetime.now(UTC) - timedelta(hours=2)
        await s.execute(
            update(ReindexRunItem)
            .where(ReindexRunItem.run_id == uuid.UUID(run_id), ReindexRunItem.source_id.is_(None))
            .values(updated_at=old)
        )
        await s.commit()

    async with async_session_factory() as s:
        with structlog.testing.capture_logs() as logs:
            reissue = await stalled_reindex_items(
                s, datetime.now(UTC) - timedelta(minutes=15), datetime.now(UTC) - timedelta(hours=1)
            )
        await s.rollback()

    assert reissue == []
    assert not [e for e in logs if "보류" in e["event"]]
    run = (await api("GET", f"/admin/rag/reindex/{run_id}")).json()
    assert (run["queued"], run["done"], run["removed"]) == (0, 1, 1)
    # 삭제로 끝난 run의 finished_at은 마지막 항목 갱신 시각이다(삭제 시각이 아니다)
    async with async_session_factory() as s:
        done_at = await s.scalar(
            select(ReindexRunItem.updated_at).where(ReindexRunItem.source_id == kept)
        )
    assert datetime.fromisoformat(run["finished_at"]) == done_at
    await _assert_active_matches_unfinished(api)
