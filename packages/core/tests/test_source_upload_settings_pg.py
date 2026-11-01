# 신규(#388). 필요: PostgreSQL·Valkey(PG 테스트).
"""소스 업로드 최대 크기 — 관리자 재정의 API·감사 로그·공개 조회·실제 업로드 413(라우터·인증·DB).

관문을 통과한 뒤 단계(용량·중복 검사, 스토리지)는 경계 밖이라 대역이 `_Admitted`로 끝낸다.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from biznotes_core.api.deps import require_admin
from biznotes_core.api.v1 import auth, sources
from biznotes_core.api.v1.admin import limits_router
from biznotes_core.config import SOURCE_MAX_UPLOAD_BYTES_CEILING, get_settings
from biznotes_core.core.db import get_session_factory
from biznotes_core.models import AuditLog, Notebook, SystemSetting, User

PASSWORD = "pw-test-1234"  # tests/conftest.py 공용 해시의 평문


class _Admitted(Exception):
    pass


def _app(*, guarded: bool = True) -> FastAPI:
    app = FastAPI()
    admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)] if guarded else [])
    admin.include_router(limits_router)
    for router in (auth.router, sources.router, admin):
        app.include_router(router, prefix="/api/v1")

    @app.exception_handler(_Admitted)
    async def _admitted(request: Request, exc: _Admitted) -> JSONResponse:
        return JSONResponse({"admitted": True}, status_code=299)

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
        return await self.client.request(
            method,
            f"/api/v1{path}",
            headers={"Authorization": f"Bearer {self.tokens[user.id]}"},
            **kw,
        )


async def _client(app: FastAPI) -> AsyncIterator[_Api]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield _Api(client)


@pytest.fixture
async def api(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Api]:
    async def after_admission(db: Any, notebook_id: Any, filename: str, size: int) -> str:
        raise _Admitted()

    monkeypatch.setattr(sources, "ensure_file_source_capacity_and_uniqueness", after_admission)
    async for a in _client(_app()):
        yield a


@pytest.fixture
async def unguarded_api() -> AsyncIterator[_Api]:
    async for a in _client(_app(guarded=False)):
        yield a


@pytest.fixture
async def notebook(db, user_a) -> Notebook:
    nb = Notebook(owner_id=user_a.id, title="업로드")
    db.add(nb)
    await db.commit()
    return nb


async def _stored() -> dict[str, Any] | None:
    async with get_session_factory()() as s:
        row = await s.get(SystemSetting, "source_upload")
        return None if row is None else dict(row.value)


async def _audits() -> list[AuditLog]:
    async with get_session_factory()() as s:
        rows = await s.execute(select(AuditLog).where(AuditLog.action == "source_upload.update"))
        return list(rows.scalars().all())


async def _upload(api: _Api, user: User, nb: Notebook, size: int) -> httpx.Response:
    files = {"file": ("a.txt", b"a" * size, "text/plain")}
    return await api.call(user, "POST", f"/notebooks/{nb.id}/sources/upload", files=files)


# ── 미설정이면 지금과 같다(50 MB) ───────────────────────────────────────────


async def test_defaults_without_override(api, admin_user, user_a) -> None:
    res = await api.call(admin_user, "GET", "/admin/source-upload")
    assert res.status_code == 200, res.text
    env_value = get_settings().source_max_upload_bytes
    assert env_value == 50 * 1024 * 1024
    assert res.json() == {
        "max_upload_bytes": env_value,
        "env_max_upload_bytes": env_value,
        "override": None,
        "hard_max_upload_bytes": SOURCE_MAX_UPLOAD_BYTES_CEILING,
    }
    public = await api.call(user_a, "GET", "/sources/upload-limits")
    assert public.status_code == 200, public.text
    assert public.json() == {"max_upload_bytes": env_value, "max_sources_per_notebook": 1000}


# ── 관리자 전용·범위·감사 ───────────────────────────────────────────────────


@pytest.mark.parametrize("guarded", [True, False], ids=["admin-router", "route-guard"])
async def test_regular_user_gets_403(guarded, api, unguarded_api, user_a) -> None:
    client = api if guarded else unguarded_api
    assert (await client.call(user_a, "GET", "/admin/source-upload")).status_code == 403
    res = await client.call(user_a, "PUT", "/admin/source-upload", json={"max_upload_bytes": 1})
    assert res.status_code == 403
    assert await _stored() is None


@pytest.mark.parametrize(
    "body",
    [
        {"max_upload_bytes": SOURCE_MAX_UPLOAD_BYTES_CEILING + 1},
        {"max_upload_bytes": 0},
        {"max_upload_bytes": -5},
        {},
        {"max_upload_bytes": 10, "extra": 1},
    ],
    ids=["over-ceiling", "zero", "negative", "missing", "unknown-key"],
)
async def test_out_of_range_is_422_and_writes_nothing(api, admin_user, body) -> None:
    res = await api.call(admin_user, "PUT", "/admin/source-upload", json=body)
    assert res.status_code == 422, res.text
    assert await _stored() is None
    assert await _audits() == []


async def test_override_applies_to_upload_and_public_value(api, admin_user, user_a, notebook):
    res = await api.call(admin_user, "PUT", "/admin/source-upload", json={"max_upload_bytes": 2048})
    assert res.status_code == 200, res.text
    assert res.json()["max_upload_bytes"] == 2048
    assert res.json()["override"] == 2048
    assert await _stored() == {"max_upload_bytes": 2048}
    [log] = await _audits()
    assert (log.user_id, log.resource) == (admin_user.id, "source_upload")
    assert log.detail == {
        "changes": {"max_upload_bytes": 2048},
        "before": {},
        "after": {"max_upload_bytes": 2048},
    }

    # 공개 값 = 판정 값
    public = (await api.call(user_a, "GET", "/sources/upload-limits")).json()
    assert public == {"max_upload_bytes": 2048, "max_sources_per_notebook": 1000}
    assert (await _upload(api, user_a, notebook, 2048)).status_code == 299
    over = await _upload(api, user_a, notebook, 2049)
    assert over.status_code == 413
    assert over.json()["detail"] == "파일이 2KB를 초과합니다"
    assert over.headers["x-admission-code"] == "too_large"

    # null은 재정의 삭제 — env(50 MB)로 돌아간다
    res = await api.call(admin_user, "PUT", "/admin/source-upload", json={"max_upload_bytes": None})
    assert res.status_code == 200, res.text
    assert res.json()["override"] is None
    assert res.json()["max_upload_bytes"] == get_settings().source_max_upload_bytes
    assert await _stored() == {}
    assert len(await _audits()) == 2
    assert (await _upload(api, user_a, notebook, 2049)).status_code == 299
