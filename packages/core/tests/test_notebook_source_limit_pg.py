# 신규(#445). 필요: PostgreSQL·Valkey(PG 테스트).
"""노트북당 소스 상한(#445) — env 500이면 진입점마다 501번째를 거부하고, 관리자 재정의가
env보다 우선하며(null은 env로 복귀), 공개 조회가 판정 값을 돌려준다. 실제 라우터·인증·DB·
advisory lock으로 본다.

상한 판정이 스토리지·SSRF보다 먼저라 거부 경로는 외부 경계를 건드리지 않는다.
인제스트 발행만 대역이다. `create_all` 기반이라 Alembic 마이그레이션 검증이 아니다.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from sqlalchemy import func, insert, select

from biznotes_core.api.deps import require_admin
from biznotes_core.api.v1 import auth, sources
from biznotes_core.api.v1.admin import limits_router
from biznotes_core.config import get_settings
from biznotes_core.core.db import get_session_factory
from biznotes_core.models import AuditLog, Notebook, Source, SystemSetting, User
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.nodes.llm import _dispatch_register_source
from biznotes_core.services.ingestion import enqueue, source_create

PASSWORD = "pw-test-1234"  # tests/conftest.py 공용 해시의 평문
FULL = "노트북당 소스는 최대 500개입니다"


def _app() -> FastAPI:
    app = FastAPI()
    admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
    admin.include_router(limits_router)
    for router in (auth.router, sources.router, admin):
        app.include_router(router, prefix="/api/v1")
    return app


class _Api:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.tokens: dict[uuid.UUID, str] = {}

    async def call(self, user: User, method: str, path: str, **kw: Any) -> httpx.Response:
        if user.id not in self.tokens:
            self.client.cookies.clear()
            res = await self.client.post(
                "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
            )
            assert res.status_code == 200, res.text
            self.tokens[user.id] = res.json()["access_token"]
        self.client.cookies.clear()
        headers = {"Authorization": f"Bearer {self.tokens[user.id]}", **kw.pop("headers", {})}
        return await self.client.request(method, f"/api/v1{path}", headers=headers, **kw)


@pytest.fixture
async def api() -> AsyncIterator[_Api]:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield _Api(client)


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(
        enqueue, "_publish", lambda source_id, task_id, queue=None: sent.append(source_id)
    )
    return sent


@pytest.fixture
def env_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """env `MAX_SOURCES_PER_NOTEBOOK=500`과 같다 — 캐시된 설정 인스턴스의 값을 바꾼다."""
    monkeypatch.setattr(get_settings(), "max_sources_per_notebook", 500)


async def _notebook(db, owner: User, existing: int) -> Notebook:
    nb = Notebook(owner_id=owner.id, title="상한")
    db.add(nb)
    await db.flush()
    if existing:
        await db.execute(
            insert(Source),
            [
                {"notebook_id": nb.id, "type": "text", "title": f"기존 {i}", "status": "ready"}
                for i in range(existing)
            ],
        )
    await db.commit()
    return nb


async def _count(notebook_id: uuid.UUID) -> int:
    async with get_session_factory()() as s:
        return (
            await s.execute(
                select(func.count()).select_from(Source).where(Source.notebook_id == notebook_id)
            )
        ).scalar_one()


async def _stored() -> dict[str, Any] | None:
    async with get_session_factory()() as s:
        row = await s.get(SystemSetting, "notebook_source_limit")
        return None if row is None else dict(row.value)


# ── 기본값: 설정이 없으면 지금과 같은 1000 ─────────────────────────────────────


async def test_default_is_1000_for_admin_and_public(api, admin_user, user_a) -> None:
    assert get_settings().max_sources_per_notebook == 1000
    res = await api.call(admin_user, "GET", "/admin/notebook-source-limit")
    assert res.status_code == 200, res.text
    assert res.json() == {
        "max_sources_per_notebook": 1000,
        "env_max_sources_per_notebook": 1000,
        "override": None,
    }
    public = await api.call(user_a, "GET", "/sources/upload-limits")
    assert public.status_code == 200, public.text
    assert public.json()["max_sources_per_notebook"] == 1000


# ── env 500: 진입점마다 501번째를 거부 ─────────────────────────────────────────


async def test_upload_501st_is_rejected(env_500, api, db, user_a) -> None:
    nb = await _notebook(db, user_a, 500)
    files = {"file": ("a.txt", b"hello", "text/plain")}
    res = await api.call(user_a, "POST", f"/notebooks/{nb.id}/sources/upload", files=files)
    assert (res.status_code, res.json()["detail"]) == (422, FULL)
    assert await _count(nb.id) == 500


async def test_url_501st_is_rejected(env_500, api, db, user_a) -> None:
    nb = await _notebook(db, user_a, 500)
    body = {"type": "web", "url": "https://example.com/a"}
    res = await api.call(user_a, "POST", f"/notebooks/{nb.id}/sources", json=body)
    assert (res.status_code, res.json()["detail"]) == (422, FULL)
    assert await _count(nb.id) == 500


async def test_register_source_501st_is_rejected(env_500, db, user_a, monkeypatch) -> None:
    monkeypatch.setattr(
        "biznotes_core.core.ssrf.validate_external_url", lambda url, allow_private=False: None
    )
    nb = await _notebook(db, user_a, 500)
    nb_id = nb.id  # commit·rollback 뒤 만료된 속성을 다시 읽지 않는다
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=nb_id, db=db, params={})
    out = await _dispatch_register_source(ctx, {"url": "https://example.com/a"})
    assert out == FULL
    assert ctx.register_source_calls[0] == 0
    assert await _count(nb_id) == 500


async def test_save_sources_admission_is_full_at_501st(env_500, db, user_a, published) -> None:
    """`save_sources`·`notebook_series`의 판정 — 500번째는 받고 501번째는 `full`이다."""
    nb = await _notebook(db, user_a, 499)
    async with enqueue.ingest_uow(db) as uow:
        created = await source_create.admit_source(
            db, uow, Source(notebook_id=nb.id, type="text", title="500번째")
        )
    async with enqueue.ingest_uow(db) as uow:
        full = await source_create.admit_source(
            db, uow, Source(notebook_id=nb.id, type="text", title="501번째")
        )
    assert created.outcome == "created"
    assert (full.outcome, full.notebook_id) == ("full", nb.id)
    assert await _count(nb.id) == 500
    assert published == [str(created.source_id)]


async def test_note_import_capacity_guard_rejects_501st(env_500, db, user_a) -> None:
    """제품 노트 가져오기가 쓰는 공개 관문(`ensure_source_capacity`)."""
    nb = await _notebook(db, user_a, 499)
    nb_id = nb.id  # commit·rollback 뒤 만료된 속성을 다시 읽지 않는다
    await enqueue.ensure_source_capacity(db, nb_id)  # 500번째 자리는 있다
    await db.rollback()
    db.add(Source(notebook_id=nb_id, type="text", title="500번째", status="ready"))
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await enqueue.ensure_source_capacity(db, nb_id)
    await db.rollback()
    assert (exc.value.status_code, exc.value.detail) == (422, FULL)


async def test_concurrent_admissions_never_exceed_limit(env_500, db, user_a) -> None:
    """advisory lock 아래에서 센다 — 남은 자리 1개에 동시에 둘이 들어와도 하나만 들어간다."""
    nb = await _notebook(db, user_a, 499)

    async def add_one(title: str) -> None:
        async with get_session_factory()() as s:
            await enqueue.ensure_source_capacity(s, nb.id)
            s.add(Source(notebook_id=nb.id, type="text", title=title, status="ready"))
            await asyncio.sleep(0.05)  # 잠금을 쥔 채 머물러 다른 쪽이 기다리게 한다
            await s.commit()

    results = await asyncio.gather(add_one("a"), add_one("b"), return_exceptions=True)
    errors = [r for r in results if isinstance(r, HTTPException)]
    assert len(errors) == 1 and errors[0].detail == FULL
    assert await _count(nb.id) == 500


async def test_notebook_over_limit_keeps_existing_sources(env_500, api, db, user_a) -> None:
    """상한을 넘는 기존 노트북은 소스를 그대로 두고 추가만 거부한다."""
    nb = await _notebook(db, user_a, 503)
    res = await api.call(
        user_a, "POST", f"/notebooks/{nb.id}/sources", json={"type": "web", "url": "https://e.x/"}
    )
    assert (res.status_code, res.json()["detail"]) == (422, FULL)
    assert await _count(nb.id) == 503
    listed = await api.call(user_a, "GET", f"/notebooks/{nb.id}/sources")
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) == 503


# ── 관리자 재정의 > env, null이면 env로 ────────────────────────────────────────


async def test_regular_user_gets_403(api, user_a) -> None:
    assert (await api.call(user_a, "GET", "/admin/notebook-source-limit")).status_code == 403
    res = await api.call(
        user_a, "PUT", "/admin/notebook-source-limit", json={"max_sources_per_notebook": 1}
    )
    assert res.status_code == 403
    assert await _stored() is None


@pytest.mark.parametrize(
    "body",
    [
        {"max_sources_per_notebook": 0},
        {"max_sources_per_notebook": -1},
        {"max_sources_per_notebook": 2_147_483_648},
        {},
        {"max_sources_per_notebook": 5, "extra": 1},
    ],
    ids=["zero", "negative", "over-int", "missing", "unknown-key"],
)
async def test_out_of_range_is_422_and_writes_nothing(api, admin_user, body) -> None:
    res = await api.call(admin_user, "PUT", "/admin/notebook-source-limit", json=body)
    assert res.status_code == 422, res.text
    assert await _stored() is None


async def test_override_wins_over_env_and_null_returns_to_env(
    env_500, api, db, admin_user, user_a
) -> None:
    nb = await _notebook(db, user_a, 3)
    res = await api.call(
        admin_user, "PUT", "/admin/notebook-source-limit", json={"max_sources_per_notebook": 3}
    )
    assert res.status_code == 200, res.text
    assert res.json() == {
        "max_sources_per_notebook": 3,
        "env_max_sources_per_notebook": 500,
        "override": 3,
    }
    assert await _stored() == {"max_sources_per_notebook": 3}
    async with get_session_factory()() as s:
        [log] = (
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.action == "notebook_source_limit.update")
                )
            )
            .scalars()
            .all()
        )
    assert (log.user_id, log.resource) == (admin_user.id, "notebook_source_limit")
    assert log.detail == {
        "changes": {"max_sources_per_notebook": 3},
        "before": {},
        "after": {"max_sources_per_notebook": 3},
    }

    # 공개 값 = 판정 값
    public = (await api.call(user_a, "GET", "/sources/upload-limits")).json()
    assert public["max_sources_per_notebook"] == 3
    files = {"file": ("a.txt", b"hello", "text/plain")}
    res = await api.call(user_a, "POST", f"/notebooks/{nb.id}/sources/upload", files=files)
    assert (res.status_code, res.json()["detail"]) == (422, "노트북당 소스는 최대 3개입니다")

    # null은 재정의 삭제 — env(500)로 돌아간다
    res = await api.call(
        admin_user, "PUT", "/admin/notebook-source-limit", json={"max_sources_per_notebook": None}
    )
    assert res.status_code == 200, res.text
    assert res.json()["override"] is None
    assert res.json()["max_sources_per_notebook"] == 500
    assert await _stored() == {}
    public = (await api.call(user_a, "GET", "/sources/upload-limits")).json()
    assert public["max_sources_per_notebook"] == 500
    await enqueue.ensure_source_capacity(db, nb.id)  # 4번째 자리가 다시 열렸다
    await db.rollback()
