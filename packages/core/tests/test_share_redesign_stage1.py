"""공유 재설계 1단계 (#278) — 내 그룹 조회, 그룹 공유의 소유자 소속 확인, `visibility` 소유자 전용.

1단계의 deprecated 표기·로그는 3단계에서 대상과 함께 없앴다(`tests/test_share_redesign_stage3.py`).

인증과 DB 세션만 대역이고 핸들러·권한 판정·응답은 실경로다. 대역 세션은 질의 종류와 쓰기를
기록한다. 실제 라우터·DB 행렬은 `tests/test_share_redesign_stage1_pg.py`가 확인한다.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from conftest import FakeResult
from fastapi import FastAPI

from biznotes_core.api import deps
from biznotes_core.api.v1 import groups, notebooks
from biznotes_core.core.db import get_db
from biznotes_core.models import Group, Notebook, User, UserGroup

NOW = datetime(2026, 10, 17, tzinfo=UTC)
GROUP_NOT_FOUND = {"detail": "Target group not found"}
OWNER_ONLY_VISIBILITY = {"detail": "Only the owner can change visibility"}


class _Result(FakeResult):
    def scalar_one_or_none(self) -> Any:
        return self._rows[0][0] if self._rows else None


class FakeDB:
    """행·소속·공유 권한에 정해 둔 값을 돌려주고 질의 종류·쓰기를 기록한다."""

    def __init__(
        self,
        *rows: Any,
        share_perms: tuple[str, ...] = (),
        my_groups: list[tuple[uuid.UUID, str]] | None = None,
    ) -> None:
        self.rows = {
            (type(r), (r.user_id, r.group_id) if isinstance(r, UserGroup) else r.id): r
            for r in rows
        }
        self.share_perms = share_perms
        self.my_groups = my_groups or []
        self.calls: list[str] = []
        self.statements: list[Any] = []
        self.get_keys: list[Any] = []

    async def get(self, model: type, key: Any, **_kw: Any) -> Any:
        self.calls.append(f"get:{model.__name__}")
        self.get_keys.append(key)
        return self.rows.get((model, key))

    async def execute(self, stmt: Any, params: Any = None) -> _Result:
        sql = str(stmt)
        self.statements.append(stmt)
        if "pg_advisory_xact_lock" in sql:
            self.calls.append("lock")
            return _Result([])
        if sql.startswith("SELECT groups.id, groups.name"):
            self.calls.append("my_groups")
            return _Result(list(self.my_groups))
        if "FROM user_groups" in sql:
            self.calls.append("groups")
            return _Result([])
        if sql.startswith("SELECT notebook_shares.permission"):
            self.calls.append("shares")
            return _Result([(p,) for p in self.share_perms])
        if "FROM notebook_shares" in sql:
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
        title="원래 제목",
        emoji="📓",
        description="",
        visibility=visibility,
        created_at=NOW,
        updated_at=NOW,
    )


async def _call(me: User, db: FakeDB, method: str, path: str, body: Any = None) -> httpx.Response:
    app = FastAPI()
    app.include_router(notebooks.router)
    app.include_router(groups.router)
    app.dependency_overrides[deps.get_current_user] = lambda: me
    app.dependency_overrides[get_db] = lambda: db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.request(method, path, json=body)


# ---------- 내 그룹 조회 ----------


async def test_my_groups_lists_only_the_requesters_memberships_by_name() -> None:
    me = _user("admin")  # admin도 자기 소속만 받는다
    a, b = uuid.uuid4(), uuid.uuid4()
    db = FakeDB(my_groups=[(a, "가"), (b, "나")])

    resp = await _call(me, db, "GET", "/me/groups")

    assert resp.status_code == 200
    assert resp.json() == [{"id": str(a), "name": "가"}, {"id": str(b), "name": "나"}]
    [stmt] = db.statements
    assert "JOIN user_groups ON user_groups.group_id = groups.id" in str(stmt)
    assert "ORDER BY groups.name, groups.id" in str(stmt)
    assert list(stmt.compile().params.values()) == [me.id]


# ---------- 그룹 공유의 소유자 소속 확인 ----------


@pytest.mark.parametrize("requester", ["owner", "admin"])
async def test_group_share_needs_the_notebook_owner_to_be_a_member(requester: str) -> None:
    owner, admin = _user(), _user("admin")
    group = Group(id=uuid.uuid4(), name="g")
    nb = _notebook(owner)
    me = owner if requester == "owner" else admin
    # admin 자신이 구성원이어도 소유자가 아니면 404다
    db = FakeDB(nb, group, UserGroup(user_id=admin.id, group_id=group.id))

    resp = await _call(me, db, "POST", f"/notebooks/{nb.id}/share", {"group_id": str(group.id)})
    missing = await _call(
        me, FakeDB(nb), "POST", f"/notebooks/{nb.id}/share", {"group_id": str(uuid.uuid4())}
    )

    assert (resp.status_code, resp.json()) == (404, GROUP_NOT_FOUND)
    assert (missing.status_code, missing.json()) == (404, GROUP_NOT_FOUND)
    # 소유자 소속 하나만 보고, 잠금·공유 행에 닿지 않는다
    assert db.calls[-1] == "get:UserGroup" and "lock" not in db.calls
    assert db.get_keys[-1] == (owner.id, group.id)


@pytest.mark.parametrize("requester", ["owner", "admin"])
async def test_group_share_succeeds_when_the_owner_is_a_member(requester: str) -> None:
    owner, admin = _user(), _user("admin")
    group = Group(id=uuid.uuid4(), name="g")
    nb = _notebook(owner)
    me = owner if requester == "owner" else admin
    db = FakeDB(nb, group, UserGroup(user_id=owner.id, group_id=group.id))

    resp = await _call(me, db, "POST", f"/notebooks/{nb.id}/share", {"group_id": str(group.id)})

    assert resp.status_code == 201
    assert resp.json()["group_id"] == str(group.id)
    assert db.calls[-5:] == ["lock", "existing", "add:NotebookShare", "flush", "commit"]


async def test_non_owners_still_get_the_223_answers_before_the_membership_check() -> None:
    owner, stranger, writer = _user(), _user(), _user()
    group = Group(id=uuid.uuid4(), name="g")
    nb = _notebook(owner)
    body = {"group_id": str(group.id)}

    gone = await _call(stranger, FakeDB(nb, group), "POST", f"/notebooks/{nb.id}/share", body)
    denied_db = FakeDB(nb, group, share_perms=("write",))
    denied = await _call(writer, denied_db, "POST", f"/notebooks/{nb.id}/share", body)

    assert (gone.status_code, gone.json()) == (404, {"detail": "Notebook not found"})
    assert (denied.status_code, denied.json()) == (403, {"detail": "Only the owner can share"})
    assert "get:UserGroup" not in denied_db.calls


# ---------- visibility 소유자 전용 ----------


@pytest.mark.parametrize("new", ["public", "private"])
async def test_write_sharer_cannot_change_visibility(new: str) -> None:
    owner, writer = _user(), _user()
    nb = _notebook(owner, "public" if new != "public" else "private")
    before = nb.visibility
    db = FakeDB(nb, share_perms=("write",))

    resp = await _call(
        writer, db, "PATCH", f"/notebooks/{nb.id}", {"visibility": new, "title": "새 제목"}
    )

    assert (resp.status_code, resp.json()) == (403, OWNER_ONLY_VISIBILITY)
    # 같은 요청의 다른 필드도 적용하지 않는다
    assert (nb.visibility, nb.title) == (before, "원래 제목")
    assert "commit" not in db.calls


async def test_write_sharer_may_resend_the_same_visibility() -> None:
    owner, writer = _user(), _user()
    nb = _notebook(owner, "public")
    db = FakeDB(nb, share_perms=("write",))

    resp = await _call(
        writer, db, "PATCH", f"/notebooks/{nb.id}", {"visibility": "public", "title": "새 제목"}
    )

    assert resp.status_code == 200
    assert (nb.visibility, nb.title) == ("public", "새 제목")


@pytest.mark.parametrize("requester", ["owner", "admin"])
async def test_owner_and_admin_change_visibility(requester: str) -> None:
    owner, admin = _user(), _user("admin")
    nb = _notebook(owner)
    me = owner if requester == "owner" else admin

    resp = await _call(me, FakeDB(nb), "PATCH", f"/notebooks/{nb.id}", {"visibility": "public"})

    assert resp.status_code == 200
    assert nb.visibility == "public"


async def test_read_sharer_and_stranger_keep_their_answers() -> None:
    owner, reader, stranger = _user(), _user(), _user()
    nb = _notebook(owner)
    body = {"visibility": "public"}

    path = f"/notebooks/{nb.id}"
    read = await _call(reader, FakeDB(nb, share_perms=("read",)), "PATCH", path, body)
    gone = await _call(stranger, FakeDB(nb), "PATCH", path, body)

    assert (read.status_code, read.json()) == (403, {"detail": "Write permission required"})
    assert (gone.status_code, gone.json()) == (404, {"detail": "Notebook not found"})
