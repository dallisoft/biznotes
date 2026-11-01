# 신규(#222). 필요: PostgreSQL·Valkey(PG 테스트).
"""같은 그룹에 다시 공유하면 같은 행의 권한을 갱신한다 — 실제 라우터·인증·DB.

대상은 그룹 하나이고 DB 유니크 `uq_share_notebook_group`이 중복을 막는다(#278 3단계). 핸들러는
노트북별 잠금 안에서 기존 행을 읽어 갱신한다 — 동시 공유가 유니크 위반 대신 갱신으로 끝난다.
동시 공유와 공유×해제 경합은 잠금 직전 Barrier로 맞추고, 잠금 뒤 지연으로 경합 창을 넓힌다
(잠금이 있으면 결과가 같다).
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.api.v1 import auth, notebooks
from biznotes_core.core.db import get_session_factory
from biznotes_core.core.security import hash_password
from biznotes_core.models import Group, Notebook, NotebookShare, User, UserGroup

PASSWORD = "pw-test-1234"  # tests/conftest.py 공용 해시의 평문


class _Api:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.tokens: dict[uuid.UUID, str] = {}

    async def login(self, user: User) -> None:
        self.client.cookies.clear()
        res = await self.client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
        )
        assert res.status_code == 200, res.text
        self.tokens[user.id] = res.json()["access_token"]
        # 인증은 쿠키를 Bearer보다 먼저 본다 — 남기면 동시 요청이 모두 마지막 로그인 사용자다
        self.client.cookies.clear()

    async def call(self, user: User, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        if user.id not in self.tokens:
            await self.login(user)
        headers = {"Authorization": f"Bearer {self.tokens[user.id]}"}
        res = await self.client.request(method, f"/api/v1{path}", json=body, headers=headers)
        return res.status_code, (res.json() if res.content else None)

    async def race(self, *calls: tuple[User, str, str, Any]) -> list[tuple[int, Any]]:
        """동시에 보낸다. ASGITransport는 앱 예외(교착·StaleDataError 등)를 예외로 올린다."""
        async with asyncio.timeout(30):
            results = await asyncio.gather(
                *(self.call(*c) for c in calls), return_exceptions=True
            )
        errors = [r for r in results if isinstance(r, BaseException)]
        assert not errors, errors
        return [r for r in results if not isinstance(r, BaseException)]


@pytest.fixture
async def api() -> AsyncIterator[_Api]:
    app = FastAPI()
    for router in (auth.router, notebooks.router):
        app.include_router(router, prefix="/api/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield _Api(client)


async def _world() -> dict[str, Any]:
    """소유자·그룹 구성원과, 소유자가 속한 그룹 둘·비공개 노트북 둘."""
    async with get_session_factory()() as s:
        hashed = hash_password(PASSWORD)
        owner, member = (
            User(email=f"{n}-{uuid.uuid4().hex[:6]}@test.dev", password_hash=hashed, name=n)
            for n in ("owner", "member")
        )
        group = Group(name=f"g-{uuid.uuid4().hex[:6]}")
        group2 = Group(name=f"g2-{uuid.uuid4().hex[:6]}")
        s.add_all([owner, member, group, group2])
        await s.flush()
        nb = Notebook(owner_id=owner.id, title="공유 대상")
        other_nb = Notebook(owner_id=owner.id, title="다른 노트북")
        s.add_all(
            [
                nb,
                other_nb,
                # 소유자도 구성원이다 — 그룹 공유가 소유자 소속을 확인해도 같은 결과다(#278)
                UserGroup(user_id=owner.id, group_id=group.id),
                UserGroup(user_id=owner.id, group_id=group2.id),
                UserGroup(user_id=member.id, group_id=group.id),
            ]
        )
        await s.commit()
        return {
            "owner": owner,
            "member": member,
            "group": group.id,
            "group2": group2.id,
            "nb": nb.id,
            "other_nb": other_nb.id,
        }


Row = tuple[uuid.UUID, uuid.UUID, str]


async def _rows(nb_id: uuid.UUID) -> list[Row]:
    async with get_session_factory()() as s:
        rows = await s.execute(
            select(
                NotebookShare.id,
                NotebookShare.group_id,
                NotebookShare.permission,
            )
            .where(NotebookShare.notebook_id == nb_id)
            .order_by(NotebookShare.created_at, NotebookShare.id)
        )
        return [tuple(r) for r in rows.all()]


async def _perm(api: _Api, user: User, nb_id: uuid.UUID) -> str | None:
    status, body = await api.call(user, "GET", f"/notebooks/{nb_id}")
    return body["permission"] if status == 200 else None


async def test_resharing_a_group_updates_the_same_row(api: _Api) -> None:
    w = await _world()
    owner, member, path = w["owner"], w["member"], f"/notebooks/{w['nb']}/share"

    body = {"group_id": str(w["group"])}
    first = await api.call(owner, "POST", path, {**body, "permission": "write"})
    assert await _perm(api, member, w["nb"]) == "write"
    again = await api.call(owner, "POST", path, {**body, "permission": "read"})

    assert again[1]["id"] == first[1]["id"]
    assert await _rows(w["nb"]) == [(uuid.UUID(first[1]["id"]), w["group"], "read")]
    assert await _perm(api, member, w["nb"]) == "read"


async def test_other_groups_and_notebooks_are_untouched(api: _Api) -> None:
    w = await _world()
    owner = w["owner"]
    async with get_session_factory()() as s:
        other = NotebookShare(notebook_id=w["other_nb"], group_id=w["group"], permission="write")
        s.add(other)
        await s.commit()
        other_id = other.id
    path = f"/notebooks/{w['nb']}/share"

    await api.call(owner, "POST", path, {"group_id": str(w["group"]), "permission": "write"})
    await api.call(owner, "POST", path, {"group_id": str(w["group2"]), "permission": "read"})
    await api.call(owner, "POST", path, {"group_id": str(w["group"]), "permission": "read"})

    rows = await _rows(w["nb"])
    assert sorted(((g, p) for _, g, p in rows), key=str) == sorted(
        [(w["group"], "read"), (w["group2"], "read")], key=str
    )
    assert await _rows(w["other_nb"]) == [(other_id, w["group"], "write")]


def _gate(monkeypatch: pytest.MonkeyPatch, parties: int) -> None:
    """모든 요청이 잠금 직전에 모인 뒤 잠그게 하고, 경합 창을 두 곳에서 넓힌다.

    - 기존 행을 읽은 뒤(UPDATE 전) — 공유 해제가 잠금 없이 끼어들면 0행 UPDATE(StaleDataError)다.
    - 커밋 직후(잠금이 풀린 뒤) — 응답을 커밋 뒤에 refresh로 만들면 그 사이 해제된 행에서 실패한다.
    잠금이 있고 응답을 커밋 전에 만들면 결과가 같다.
    """
    barrier = asyncio.Barrier(parties)
    real_lock, real_upsert = notebooks.lock_notebook_shares, notebooks.upsert_notebook_share
    real_commit = AsyncSession.commit

    async def gated(db: Any, notebook_id: uuid.UUID) -> None:
        await barrier.wait()
        await real_lock(db, notebook_id)

    async def slow_upsert(*args: Any, **kwargs: Any) -> Any:
        row = await real_upsert(*args, **kwargs)
        await asyncio.sleep(0.3)
        return row

    async def slow_commit(self: AsyncSession) -> None:
        await real_commit(self)
        await asyncio.sleep(0.2)

    monkeypatch.setattr(notebooks, "lock_notebook_shares", gated)
    monkeypatch.setattr(notebooks, "upsert_notebook_share", slow_upsert)
    monkeypatch.setattr(AsyncSession, "commit", slow_commit)


async def test_concurrent_shares_to_one_target_leave_one_row(
    api: _Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _world()
    owner, path = w["owner"], f"/notebooks/{w['nb']}/share"
    await api.login(owner)
    perms = ["read", "write", "read", "write", "read"]
    _gate(monkeypatch, len(perms))

    results = await api.race(
        *((owner, "POST", path, {"group_id": str(w["group"]), "permission": p}) for p in perms)
    )

    assert [s for s, _ in results] == [201] * len(perms)
    assert len({b["id"] for _, b in results}) == 1
    [(_, group_id, permission)] = await _rows(w["nb"])
    assert group_id == w["group"] and permission in perms


async def test_reshare_holding_the_lock_then_unshare_of_the_same_row(
    api: _Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """공유 갱신이 잠금을 쥔 사이 같은 행을 해제한다 — 순서를 고정한다.

    해제는 잠금을 기다렸다가 갱신이 커밋한 뒤 지운다. 해제가 잠그지 않으면 갱신의 UPDATE가 0행이
    되고(StaleDataError), 갱신이 응답을 커밋 뒤에 refresh로 만들면 그 사이 지워진 행에서 실패한다.
    """
    w = await _world()
    owner, target = w["owner"], {"group_id": str(w["group"])}
    status, body = await api.call(
        owner, "POST", f"/notebooks/{w['nb']}/share", {**target, "permission": "write"}
    )
    assert status == 201
    await api.login(owner)
    locked = asyncio.Event()
    real_lock, real_upsert = notebooks.lock_notebook_shares, notebooks.upsert_notebook_share
    real_commit = AsyncSession.commit

    async def signalling_lock(db: Any, notebook_id: uuid.UUID) -> None:
        await real_lock(db, notebook_id)
        locked.set()

    async def slow_upsert(*args: Any, **kwargs: Any) -> Any:
        row = await real_upsert(*args, **kwargs)
        await asyncio.sleep(0.3)  # 기존 행을 읽은 뒤, UPDATE 전
        return row

    async def slow_commit(self: AsyncSession) -> None:
        await real_commit(self)
        await asyncio.sleep(0.2)  # 커밋(잠금 해제) 직후

    monkeypatch.setattr(notebooks, "lock_notebook_shares", signalling_lock)
    monkeypatch.setattr(notebooks, "upsert_notebook_share", slow_upsert)
    monkeypatch.setattr(AsyncSession, "commit", slow_commit)

    async with asyncio.timeout(30):
        share = asyncio.create_task(
            api.call(owner, "POST", f"/notebooks/{w['nb']}/share", {**target, "permission": "read"})
        )
        await locked.wait()
        unshare = await api.call(owner, "DELETE", f"/notebooks/{w['nb']}/shares/{body['id']}")
        shared = await share

    assert (shared[0], shared[1]["id"], shared[1]["permission"]) == (201, body["id"], "read")
    assert unshare[0] == 204
    assert await _rows(w["nb"]) == []
