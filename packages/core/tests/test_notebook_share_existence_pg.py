# 신규(#223). 필요: PostgreSQL·Valkey(PG 테스트).
"""공유 API의 권한 행렬 — 실제 라우터·인증·DB.

무관 사용자는 대상과 상관없이 단건 `GET`과 같은 404를 받는다. 읽기·쓰기 권한자(그룹 공유·
public 열람 포함)는 403을 받는다. 소유자·관리자는 대상 그룹 404를 받는다. 사용자(이메일) 대상은
#278 3단계에서 없앴다 — 누구에게나 같은 422다(#275).
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from conftest import share_to_user
from fastapi import FastAPI
from sqlalchemy import func, select

from biznotes_core.api.v1 import auth, notebooks
from biznotes_core.core.db import get_session_factory
from biznotes_core.core.security import hash_password
from biznotes_core.models import Group, Notebook, NotebookShare, User, UserGroup

PASSWORD = "pw-test-1234"  # tests/conftest.py 공용 해시의 평문
NOT_FOUND = {"detail": "Notebook not found"}


class _Api:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.tokens: dict[uuid.UUID, str] = {}

    async def call(
        self, user: User, method: str, path: str, body: Any = None
    ) -> tuple[int, Any]:
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
    for router in (auth.router, notebooks.router):
        app.include_router(router, prefix="/api/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield _Api(client)


async def _users(*names: str) -> list[User]:
    async with get_session_factory()() as s:
        hashed = hash_password(PASSWORD)
        users = [
            User(email=f"{n}-{uuid.uuid4().hex[:6]}@test.dev", password_hash=hashed, name=n)
            for n in names
        ]
        s.add_all(users)
        await s.commit()
        return users


async def _world(
    owner: User, reader: User, group_member: User, group_reader: User | None = None
) -> dict[str, Any]:
    """비공개 노트북(reader 읽기 공유, 그룹 쓰기 공유, 둘째 그룹 읽기 공유), public 노트북,
    읽기 공유 행의 id."""
    async with get_session_factory()() as s:
        group = Group(name=f"g-{uuid.uuid4().hex[:6]}")
        group_r = Group(name=f"gr-{uuid.uuid4().hex[:6]}")
        private = Notebook(owner_id=owner.id, title="비공개")
        public = Notebook(owner_id=owner.id, title="공개", visibility="public")
        s.add_all([group, group_r, private, public])
        await s.flush()
        *grant, read_share = share_to_user(private.id, reader.id, "read")
        rows: list[Any] = [
            UserGroup(user_id=group_member.id, group_id=group.id),
            *grant,
            read_share,
            NotebookShare(notebook_id=private.id, group_id=group.id, permission="write"),
            NotebookShare(notebook_id=private.id, group_id=group_r.id, permission="read"),
        ]
        if group_reader is not None:
            rows.append(UserGroup(user_id=group_reader.id, group_id=group_r.id))
        s.add_all(rows)
        await s.commit()
        return {
            "private": private.id,
            "public": public.id,
            "group": group.id,
            "share": read_share.id,
        }


async def _share_rows() -> int:
    async with get_session_factory()() as s:
        return int(await s.scalar(select(func.count()).select_from(NotebookShare)) or 0)


def _share_bodies(world: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"group_id": str(world["group"])}, {"group_id": str(uuid.uuid4())}]


async def test_unrelated_user_cannot_tell_notebooks_or_groups_apart(
    api: _Api, user_a: User, user_b: User
) -> None:
    reader, member = await _users("reader", "member")
    world = await _world(user_a, reader, member)
    before = await _share_rows()

    for nb in (world["private"],):
        for path in (f"/notebooks/{nb}/share", f"/notebooks/{uuid.uuid4()}/share"):
            for body in _share_bodies(world):
                assert await api.call(user_b, "POST", path, body) == (404, NOT_FOUND), (path, body)
        assert await api.call(user_b, "GET", f"/notebooks/{nb}/shares") == (404, NOT_FOUND)
        share_path = f"/notebooks/{nb}/shares/{world['share']}"
        assert await api.call(user_b, "DELETE", share_path) == (404, NOT_FOUND)
        # 단건 GET과 같은 응답이다
        assert await api.call(user_b, "GET", f"/notebooks/{nb}") == (404, NOT_FOUND)
    # 거부된 요청은 공유 행을 만들거나 지우지 않는다
    assert await _share_rows() == before


async def test_missing_notebook_is_the_same_404_for_every_actor(
    api: _Api, user_a: User, user_b: User, admin_user: User
) -> None:
    reader, member = await _users("reader", "member")
    world = await _world(user_a, reader, member)
    path = f"/notebooks/{uuid.uuid4()}/share"

    for actor in (user_a, admin_user, reader, user_b):
        for body in _share_bodies(world):
            assert await api.call(actor, "POST", path, body) == (404, NOT_FOUND), actor.name


async def test_permission_holders_keep_403(api: _Api, user_a: User, user_b: User) -> None:
    """읽기·쓰기·그룹 쓰기·그룹 읽기 공유자와 public 열람자."""
    reader, member, group_reader = await _users("reader", "member", "group-reader")
    world = await _world(user_a, reader, member, group_reader)
    before = await _share_rows()
    cases = [
        (reader, world["private"]),
        (member, world["private"]),
        (group_reader, world["private"]),
        (user_b, world["public"]),
    ]

    for viewer, nb in cases:
        for body in _share_bodies(world):
            status, out = await api.call(viewer, "POST", f"/notebooks/{nb}/share", body)
            assert (status, out) == (403, {"detail": "Only the owner can share"}), viewer.name
        assert await api.call(viewer, "GET", f"/notebooks/{nb}/shares") == (
            403,
            {"detail": "Only the owner can view shares"},
        )
        assert await api.call(viewer, "DELETE", f"/notebooks/{nb}/shares/{world['share']}") == (
            403,
            {"detail": "Only the owner can unshare"},
        )
    assert await _share_rows() == before


async def test_owner_and_admin_keep_target_errors(
    api: _Api, user_a: User, user_b: User, admin_user: User
) -> None:
    reader, member = await _users("reader", "member")
    world = await _world(user_a, reader, member)
    path = f"/notebooks/{world['private']}/share"

    for actor in (user_a, admin_user):
        assert await api.call(actor, "POST", path, {"group_id": str(uuid.uuid4())}) == (
            404,
            {"detail": "Target group not found"},
        )
        missing_share = f"/notebooks/{world['private']}/shares/{uuid.uuid4()}"
        assert await api.call(actor, "DELETE", missing_share) == (
            404,
            {"detail": "Share not found"},
        )
        status, _ = await api.call(actor, "GET", f"/notebooks/{world['private']}/shares")
        assert status == 200

    # 이메일 공유로 가입 여부를 떠볼 수 없다 — 있는 이메일도 422이고 행이 생기지 않는다(#275)
    before = await _share_rows()
    status, out = await api.call(user_a, "POST", path, {"user_email": user_b.email})
    assert status == 422 and "user_id" not in str(out)
    assert await _share_rows() == before


async def test_user_target_422_is_the_same_for_everyone(
    api: _Api, user_a: User, user_b: User
) -> None:
    """소유자·무관 사용자, 있는·없는 노트북, 있는·없는 이메일이 모두 같은 422다(#275)."""
    reader, member = await _users("reader", "member")
    world = await _world(user_a, reader, member)
    answers = set()

    for actor in (user_a, user_b):
        for nb in (world["private"], uuid.uuid4()):
            for email in ("ghost@test.dev", user_b.email):
                body = {"user_email": email, "group_id": str(world["group"])}
                status, out = await api.call(actor, "POST", f"/notebooks/{nb}/share", body)
                assert status == 422
                answers.add(tuple((e["type"], tuple(e["loc"])) for e in out["detail"]))
    assert answers == {(("extra_forbidden", ("body", "user_email")),)}
