"""공유 API가 권한 판정 전에 가입 여부·노트북 존재를 드러내지 않는다 (#223).

인증(`get_current_user`)과 DB 세션(`get_db`)만 대역이고, 권한 판정(`get_permission` →
`resolve_permission`)·핸들러·응답은 실경로다. 대역 세션은 질의 종류를 기록해, 소유자가 아닌
사용자에게 대상 조회(`get(UserGroup)`)가 나가지 않음을 확인한다. 사용자(이메일) 대상은 #278
3단계에서 없앴다 — 보내면 질의 없이 422다(#275가 설계로 닫힌다). 실제 권한 행렬은
`tests/test_notebook_share_existence_pg.py`가 확인한다.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from conftest import FakeResult
from fastapi import FastAPI

from biznotes_core.api import deps
from biznotes_core.api.v1 import notebooks
from biznotes_core.core.db import get_db
from biznotes_core.models import Notebook, User

NOW = datetime(2026, 10, 17, tzinfo=UTC)
NOT_FOUND = {"detail": "Notebook not found"}


class _Result(FakeResult):
    def scalar_one_or_none(self) -> Any:
        return self._rows[0][0] if self._rows else None


class FakeDB:
    """행 조회·공유 권한에 정해 둔 값을 돌려주고 질의 종류를 기록한다."""

    def __init__(self, *rows: Any, share_perms: tuple[str, ...] = ()) -> None:
        self.rows = {(type(r), r.id): r for r in rows}
        self.share_perms = share_perms
        self.calls: list[str] = []

    async def get(self, model: type, key: Any, **_kw: Any) -> Any:
        self.calls.append(f"get:{model.__name__}")
        return self.rows.get((model, key))

    async def execute(self, stmt: Any, params: Any = None) -> _Result:
        sql = str(stmt)
        if "pg_advisory_xact_lock" in sql:  # 공유 쓰기 잠금(#222)
            self.calls.append("lock")
            return _Result([])
        if "FROM user_groups" in sql:
            self.calls.append("groups")
            return _Result([])
        if sql.startswith("SELECT notebook_shares.permission"):
            self.calls.append("shares")
            return _Result([(p,) for p in self.share_perms])
        if "FROM notebook_shares" in sql:  # 같은 대상의 기존 행(#222) — 없다
            self.calls.append("existing")
            return _Result([])
        raise AssertionError(f"예상하지 못한 질의: {sql}")

    def add(self, obj: Any) -> None:
        obj.id = uuid.uuid4()
        self.calls.append(f"add:{type(obj).__name__}")

    async def flush(self) -> None:
        self.calls.append("flush")

    async def commit(self) -> None:
        self.calls.append("commit")

    async def refresh(self, _obj: Any) -> None:
        self.calls.append("refresh")


def _user(role: str = "user") -> User:
    email = f"{uuid.uuid4().hex[:6]}@test.dev"
    return User(id=uuid.uuid4(), email=email, role=role, is_active=True)


def _notebook(owner: User, visibility: str = "private") -> Notebook:
    return Notebook(
        id=uuid.uuid4(),
        owner_id=owner.id,
        title="t",
        emoji="📓",
        description="",
        visibility=visibility,
        created_at=NOW,
        updated_at=NOW,
    )


async def _call(me: User, db: FakeDB, method: str, path: str, body: Any = None) -> httpx.Response:
    app = FastAPI()
    app.include_router(notebooks.router)
    app.dependency_overrides[deps.get_current_user] = lambda: me
    app.dependency_overrides[get_db] = lambda: db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.request(method, path, json=body)


async def test_unrelated_user_gets_404_before_the_group_lookup() -> None:
    owner, me = _user(), _user()
    nb = _notebook(owner)
    db = FakeDB(nb)

    resp = await _call(me, db, "POST", f"/notebooks/{nb.id}/share", {"group_id": str(uuid.uuid4())})

    assert (resp.status_code, resp.json()) == (404, NOT_FOUND)
    assert db.calls == ["get:Notebook", "groups", "shares"]


async def test_missing_notebook_is_404_before_the_group_lookup() -> None:
    db = FakeDB()

    resp = await _call(
        _user(), db, "POST", f"/notebooks/{uuid.uuid4()}/share", {"group_id": str(uuid.uuid4())}
    )

    assert (resp.status_code, resp.json()) == (404, NOT_FOUND)
    assert db.calls == ["get:Notebook"]


@pytest.mark.parametrize(
    ("method", "suffix"), [("GET", "shares"), ("DELETE", f"shares/{uuid.uuid4()}")]
)
async def test_unrelated_user_gets_404_on_share_listing_and_removal(
    method: str, suffix: str
) -> None:
    owner, me = _user(), _user()
    nb = _notebook(owner)

    resp = await _call(me, FakeDB(nb), method, f"/notebooks/{nb.id}/{suffix}")

    assert (resp.status_code, resp.json()) == (404, NOT_FOUND)


@pytest.mark.parametrize(
    ("visibility", "share_perms"),
    [("private", ("read",)), ("private", ("write",)), ("public", ())],
    ids=["읽기-공유", "쓰기-공유", "public-열람"],
)
@pytest.mark.parametrize(
    ("method", "suffix", "detail"),
    [
        ("POST", "share", "Only the owner can share"),
        ("GET", "shares", "Only the owner can view shares"),
        ("DELETE", f"shares/{uuid.uuid4()}", "Only the owner can unshare"),
    ],
    ids=["share", "list", "unshare"],
)
async def test_permission_holders_keep_403(
    visibility: str, share_perms: tuple[str, ...], method: str, suffix: str, detail: str
) -> None:
    """공유는 대상 그룹 확인보다 403이 먼저다(수락 기준 4b)."""
    owner, me = _user(), _user()
    nb = _notebook(owner, visibility)
    body = {"group_id": str(uuid.uuid4())} if method == "POST" else None
    db = FakeDB(nb, share_perms=share_perms)

    resp = await _call(me, db, method, f"/notebooks/{nb.id}/{suffix}", body)

    assert (resp.status_code, resp.json()) == (403, {"detail": detail})
    assert db.calls == ["get:Notebook", "groups", "shares"]


@pytest.mark.parametrize(
    ("body", "errors"),
    [
        ({}, {("missing", "group_id")}),
        (
            {"user_email": "a@test.dev"},
            {("missing", "group_id"), ("extra_forbidden", "user_email")},
        ),
        ({"user_id": str(uuid.uuid4())}, {("missing", "group_id"), ("extra_forbidden", "user_id")}),
        (
            {"user_email": "a@test.dev", "group_id": str(uuid.uuid4())},
            {("extra_forbidden", "user_email")},
        ),
    ],
    ids=["대상-없음", "이메일", "사용자", "이메일+그룹"],
)
async def test_user_targets_are_rejected_before_any_query(
    body: dict[str, Any], errors: set[tuple[str, str]]
) -> None:
    """사용자(이메일) 대상은 #278 3단계에서 없앴다 — 이메일 조회 없이 422다(#275)."""
    db = FakeDB()

    resp = await _call(_user(), db, "POST", f"/notebooks/{uuid.uuid4()}/share", body)

    assert resp.status_code == 422
    assert {(e["type"], e["loc"][-1]) for e in resp.json()["detail"]} == errors
    assert db.calls == []
