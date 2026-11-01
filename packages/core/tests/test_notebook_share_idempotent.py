"""같은 그룹에 다시 공유하면 그 행을 갱신한다 (#222) — 순서·분기 계약.

대상은 그룹 하나다(#278 3단계). DB 유니크 `uq_share_notebook_group`이 중복을 막으므로 기존 행은
0 또는 1개다.

인증과 DB 세션만 대역이고 핸들러·`upsert_notebook_share`는 실경로다. 대역 세션은 질의 종류와
쓰기를 기록한다. 실제 행 수·하향 적용·동시 공유는 `tests/test_notebook_share_idempotent_pg.py`가
확인한다.
"""

import inspect
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from conftest import FakeResult, make_settings
from fastapi import FastAPI

from biznotes_core.api import deps
from biznotes_core.api.v1 import notebooks
from biznotes_core.core.db import get_db
from biznotes_core.models import Group, Notebook, NotebookShare, User, UserGroup
from biznotes_core.services import notebook_shares

NOW = datetime(2026, 10, 17, tzinfo=UTC)


class _Result(FakeResult):
    def scalar_one_or_none(self) -> Any:
        return self._rows[0][0] if self._rows else None


class FakeDB:
    """같은 대상의 기존 행(`existing`)을 돌려주고, 질의 종류·쓰기 순서를 기록한다."""

    def __init__(
        self,
        *rows: Any,
        existing: list[NotebookShare] | None = None,
        share_perms: tuple[str, ...] = (),
    ) -> None:
        self.share_perms = share_perms
        # 그룹 소속은 복합 키 (user_id, group_id)로 찾는다(#278)
        self.rows = {
            (type(r), (r.user_id, r.group_id) if isinstance(r, UserGroup) else r.id): r
            for r in rows
        }
        self.existing = existing or []
        self.calls: list[str] = []
        self.deleted: list[Any] = []
        self.existing_stmt: Any = None

    async def get(self, model: type, key: Any, **_kw: Any) -> Any:
        self.calls.append(f"get:{model.__name__}")
        return self.rows.get((model, key))

    async def execute(self, stmt: Any, params: Any = None) -> _Result:
        sql = str(stmt)
        if "pg_advisory_xact_lock" in sql:
            self.calls.append("lock")
            return _Result([])
        if "FROM user_groups" in sql:  # 비소유자 권한 판정
            self.calls.append("groups")
            return _Result([])
        if sql.startswith("SELECT notebook_shares.permission"):
            self.calls.append("shares")
            return _Result([(p,) for p in self.share_perms])
        if "FROM notebook_shares" in sql:
            self.calls.append("existing")
            self.existing_stmt = stmt
            return _Result([(r,) for r in self.existing])
        raise AssertionError(f"예상하지 못한 질의: {sql}")

    def add(self, obj: Any) -> None:
        obj.id = uuid.uuid4()
        self.calls.append(f"add:{type(obj).__name__}")

    async def delete(self, obj: Any) -> None:
        self.calls.append(f"delete:{type(obj).__name__}")
        self.deleted.append(obj)

    async def flush(self) -> None:
        self.calls.append("flush")

    async def commit(self) -> None:
        self.calls.append("commit")

    async def refresh(self, _obj: Any) -> None:
        self.calls.append("refresh")


def _user(role: str = "user") -> User:
    email = f"{uuid.uuid4().hex[:6]}@test.dev"
    return User(id=uuid.uuid4(), email=email, role=role, is_active=True)


def _notebook(owner: User) -> Notebook:
    return Notebook(
        id=uuid.uuid4(),
        owner_id=owner.id,
        title="t",
        emoji="📓",
        description="",
        visibility="private",
        created_at=NOW,
        updated_at=NOW,
    )


def _share(nb: Notebook, permission: str, **target: Any) -> NotebookShare:
    return NotebookShare(id=uuid.uuid4(), notebook_id=nb.id, permission=permission, **target)


async def _call(me: User, db: FakeDB, method: str, path: str, body: Any = None) -> httpx.Response:
    app = FastAPI()
    app.include_router(notebooks.router)
    app.dependency_overrides[deps.get_current_user] = lambda: me
    app.dependency_overrides[get_db] = lambda: db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.request(method, path, json=body)


async def test_first_share_inserts_after_the_lock() -> None:
    owner = _user()
    group = Group(id=uuid.uuid4(), name="g")
    nb = _notebook(owner)
    db = FakeDB(nb, group, UserGroup(user_id=owner.id, group_id=group.id))

    path = f"/notebooks/{nb.id}/share"
    resp = await _call(owner, db, "POST", path, {"group_id": str(group.id), "permission": "write"})

    assert resp.status_code == 201
    assert resp.json()["permission"] == "write"
    # 대상 확인이 끝난 뒤 잠그고, 잠근 뒤에 기존 행을 읽는다. 응답은 커밋 전에 만든다(refresh 없음)
    assert db.calls == [
        "get:Notebook", "get:UserGroup", "lock", "existing", "add:NotebookShare", "flush", "commit"
    ]
    # 같은 노트북·같은 그룹만 찾는다
    params = set(db.existing_stmt.compile().params.values())
    assert {nb.id, group.id} <= params


@pytest.mark.parametrize(("before", "after"), [("write", "read"), ("read", "write")])
async def test_reshare_updates_the_existing_row(before: str, after: str) -> None:
    owner = _user()
    group = Group(id=uuid.uuid4(), name="g")
    nb = _notebook(owner)
    row = _share(nb, before, group_id=group.id)
    db = FakeDB(nb, UserGroup(user_id=owner.id, group_id=group.id), existing=[row])

    resp = await _call(
        owner, db, "POST", f"/notebooks/{nb.id}/share",
        {"group_id": str(group.id), "permission": after},
    )

    assert resp.status_code == 201
    assert (resp.json()["id"], resp.json()["permission"]) == (str(row.id), after)
    assert row.permission == after
    assert db.deleted == []
    assert db.calls == ["get:Notebook", "get:UserGroup", "lock", "existing", "flush", "commit"]


async def test_unshare_reads_the_row_after_the_lock() -> None:
    owner = _user()
    nb = _notebook(owner)
    row = _share(nb, "read", group_id=uuid.uuid4())
    db = FakeDB(nb, row)

    resp = await _call(owner, db, "DELETE", f"/notebooks/{nb.id}/shares/{row.id}")

    assert resp.status_code == 204
    assert db.calls == [
        "get:Notebook", "lock", "get:NotebookShare", "delete:NotebookShare", "commit"
    ]


@pytest.mark.parametrize("share_perms", [("write",), ("read",)])
async def test_unshare_rejection_happens_before_the_lock(share_perms: tuple[str, ...]) -> None:
    """공유 해제도 노트북 404 → 권한 404/403 → 잠금 → 행 조회 순서다."""
    owner, me = _user(), _user()
    nb = _notebook(owner)
    row = _share(nb, "read", group_id=uuid.uuid4())

    db = FakeDB(nb, row, share_perms=share_perms)
    resp = await _call(me, db, "DELETE", f"/notebooks/{nb.id}/shares/{row.id}")
    stranger_db = FakeDB(nb, row)
    gone = await _call(_user(), stranger_db, "DELETE", f"/notebooks/{nb.id}/shares/{row.id}")

    assert (resp.status_code, resp.json()) == (403, {"detail": "Only the owner can unshare"})
    assert db.calls == ["get:Notebook", "groups", "shares"]
    assert gone.status_code == 404 and "lock" not in stranger_db.calls


def test_upsert_takes_a_group_target_only() -> None:
    """사용자 대상 키워드는 #278 3단계에서 없앴다 — 공개 시그니처를 고정한다."""
    params = inspect.signature(notebook_shares.upsert_notebook_share).parameters

    assert list(params) == ["db", "notebook_id", "group_id", "permission", "upgrade_only"]
    assert all(params[n].kind is inspect.Parameter.KEYWORD_ONLY for n in list(params)[2:])


def test_lock_key_is_stable_per_notebook_and_namespaced(monkeypatch: pytest.MonkeyPatch) -> None:
    key = notebook_shares._notebook_shares_lock_key
    nb_a, nb_b = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(notebook_shares, "get_settings", lambda: make_settings(service_name="a"))
    a1, a2, b = key(nb_a), key(nb_a), key(nb_b)
    monkeypatch.setattr(notebook_shares, "get_settings", lambda: make_settings(service_name="z"))

    assert a1 == a2 and a1 != b
    assert key(nb_a) != a1
    assert -(2**63) <= a1 < 2**63
