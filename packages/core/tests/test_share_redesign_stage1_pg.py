# 신규(#278 1단계). 필요: PostgreSQL·Valkey(PG 테스트).
"""내 그룹 조회·그룹 공유의 소유자 소속 확인·`visibility` 소유자 전용 — 실제 라우터·인증·DB.

그룹 공유는 노트북 실제 소유자가 속한 그룹만 대상이다(admin이 공유해도 소유자 소속을 본다).
비구성원 그룹은 없는 그룹과 같은 404다. 이미 있는 그룹 공유 행은 그대로 권한을 준다.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from conftest import share_to_user
from fastapi import FastAPI
from sqlalchemy import func, select

from biznotes_core.api.v1 import auth, groups, notebooks
from biznotes_core.core.db import get_session_factory
from biznotes_core.core.security import hash_password
from biznotes_core.models import Group, Notebook, NotebookShare, User, UserGroup

PASSWORD = "pw-test-1234"  # tests/conftest.py 공용 해시의 평문
GROUP_NOT_FOUND = {"detail": "Target group not found"}


class _Api:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.tokens: dict[uuid.UUID, str] = {}

    async def call(self, user: User, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        if user.id not in self.tokens:
            self.client.cookies.clear()
            res = await self.client.post(
                "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
            )
            assert res.status_code == 200, res.text
            self.tokens[user.id] = res.json()["access_token"]
        self.client.cookies.clear()
        headers = {"Authorization": f"Bearer {self.tokens[user.id]}"}
        res = await self.client.request(method, f"/api/v1{path}", json=body, headers=headers)
        return res.status_code, (res.json() if res.content else None)


@pytest.fixture
async def api() -> AsyncIterator[_Api]:
    app = FastAPI()
    for router in (auth.router, notebooks.router, groups.router):
        app.include_router(router, prefix="/api/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield _Api(client)


@pytest.fixture
async def world() -> dict[str, Any]:
    """소유자는 「팀」의 구성원, 「남의 팀」에는 없다. member는 둘 다, admin은 「남의 팀」만."""
    async with get_session_factory()() as s:
        hashed = hash_password(PASSWORD)
        u = {
            n: User(
                email=f"{n}-{uuid.uuid4().hex[:6]}@test.dev", password_hash=hashed, name=n,
                role="admin" if n == "admin" else "user",
            )
            for n in ("owner", "member", "writer", "reader", "stranger", "admin", "loner")
        }
        tag = uuid.uuid4().hex[:6]
        team, other = Group(name=f"나-팀-{tag}"), Group(name=f"가-남의-팀-{tag}")
        s.add_all([*u.values(), team, other])
        await s.flush()
        nb = Notebook(owner_id=u["owner"].id, title="원래 제목")
        s.add(nb)
        await s.flush()
        s.add_all(
            [
                UserGroup(user_id=u["owner"].id, group_id=team.id),
                UserGroup(user_id=u["member"].id, group_id=team.id),
                UserGroup(user_id=u["member"].id, group_id=other.id),
                UserGroup(user_id=u["admin"].id, group_id=other.id),
                *share_to_user(nb.id, u["writer"].id, "write"),
                *share_to_user(nb.id, u["reader"].id, "read"),
            ]
        )
        await s.commit()
        return {**u, "team": team, "other": other, "nb": nb.id}


async def _shares(nb_id: uuid.UUID, group_id: uuid.UUID) -> int:
    async with get_session_factory()() as s:
        q = select(func.count()).select_from(NotebookShare).where(
            NotebookShare.notebook_id == nb_id, NotebookShare.group_id == group_id
        )
        return int(await s.scalar(q) or 0)


async def _notebook(nb_id: uuid.UUID) -> tuple[str, str]:
    async with get_session_factory()() as s:
        nb = await s.get(Notebook, nb_id)
        assert nb is not None
        return nb.visibility, nb.title


async def test_my_groups_returns_only_own_memberships(api: _Api, world: dict[str, Any]) -> None:
    def _g(g: Group) -> dict[str, str]:
        return {"id": str(g.id), "name": g.name}

    assert await api.call(world["member"], "GET", "/me/groups") == (
        200, [_g(world["other"]), _g(world["team"])]  # 이름순
    )
    assert await api.call(world["owner"], "GET", "/me/groups") == (200, [_g(world["team"])])
    assert await api.call(world["admin"], "GET", "/me/groups") == (200, [_g(world["other"])])
    assert await api.call(world["loner"], "GET", "/me/groups") == (200, [])
    api.client.cookies.clear()
    assert (await api.client.get("/api/v1/me/groups")).status_code == 401
    # 비밀번호 변경이 필요한 사용자는 get_current_user 규칙대로 403이다
    async with get_session_factory()() as s:
        row = await s.get(User, world["member"].id)
        assert row is not None
        row.must_change_password = True
        await s.commit()
    status, body = await api.call(world["member"], "GET", "/me/groups")
    assert (status, body["detail"]["code"]) == (403, "password_change_required")


async def test_group_share_follows_the_notebook_owners_memberships(
    api: _Api, world: dict[str, Any]
) -> None:
    path = f"/notebooks/{world['nb']}/share"
    team, other = str(world["team"].id), str(world["other"].id)

    for me in (world["owner"], world["admin"]):
        # 소유자가 비구성원인 그룹 — admin이 구성원이어도 없는 그룹과 같은 404다
        assert await api.call(me, "POST", path, {"group_id": other}) == (404, GROUP_NOT_FOUND)
        assert await api.call(me, "POST", path, {"group_id": str(uuid.uuid4())}) == (
            404, GROUP_NOT_FOUND
        )
    assert await _shares(world["nb"], world["other"].id) == 0

    # admin은 「팀」의 구성원이 아니지만 소유자가 구성원이라 공유할 수 있다
    status, body = await api.call(world["admin"], "POST", path, {"group_id": team})
    assert (status, body["group_id"]) == (201, team)
    write = {"group_id": team, "permission": "write"}
    status, _ = await api.call(world["owner"], "POST", path, write)
    assert status == 201
    assert await _shares(world["nb"], world["team"].id) == 1
    status, body = await api.call(world["member"], "GET", f"/notebooks/{world['nb']}")
    assert (status, body["permission"]) == (200, "write")


async def test_existing_share_to_a_non_member_group_keeps_working(
    api: _Api, world: dict[str, Any]
) -> None:
    async with get_session_factory()() as s:
        legacy = NotebookShare(
            notebook_id=world["nb"], group_id=world["other"].id, permission="write"
        )
        s.add(legacy)
        await s.commit()
        legacy_id = legacy.id

    # 1단계는 만들거나 바꿀 때만 소속을 본다 — 기존 행은 계속 권한을 준다
    status, body = await api.call(world["admin"], "GET", f"/notebooks/{world['nb']}")
    assert status == 200
    status, body = await api.call(world["member"], "GET", f"/notebooks/{world['nb']}")
    assert (status, body["permission"]) == (200, "write")
    # 다시 공유(하향 포함)는 소속 확인에 걸려 404다 — 이 행은 해제만 할 수 있다
    path = f"/notebooks/{world['nb']}/share"
    downgrade = {"group_id": str(world["other"].id), "permission": "read"}
    assert await api.call(world["owner"], "POST", path, downgrade) == (404, GROUP_NOT_FOUND)
    status, body = await api.call(world["member"], "GET", f"/notebooks/{world['nb']}")
    assert (status, body["permission"]) == (200, "write")
    unshare = f"/notebooks/{world['nb']}/shares/{legacy_id}"
    assert (await api.call(world["owner"], "DELETE", unshare))[0] == 204


async def test_only_owner_permission_changes_visibility(api: _Api, world: dict[str, Any]) -> None:
    path = f"/notebooks/{world['nb']}"

    denied = await api.call(world["writer"], "PATCH", path, {"visibility": "public", "title": "x"})
    assert denied == (403, {"detail": "Only the owner can change visibility"})
    assert await _notebook(world["nb"]) == ("private", "원래 제목")

    same_value = {"visibility": "private", "title": "쓰기"}
    same = await api.call(world["writer"], "PATCH", path, same_value)
    assert same[0] == 200
    assert await _notebook(world["nb"]) == ("private", "쓰기")

    assert await api.call(world["reader"], "PATCH", path, {"visibility": "public"}) == (
        403, {"detail": "Write permission required"}
    )
    assert await api.call(world["stranger"], "PATCH", path, {"visibility": "public"}) == (
        404, {"detail": "Notebook not found"}
    )
    assert (await api.call(world["admin"], "PATCH", path, {"visibility": "public"}))[0] == 200
    assert (await api.call(world["owner"], "PATCH", path, {"visibility": "private"}))[0] == 200
    assert await _notebook(world["nb"]) == ("private", "쓰기")
