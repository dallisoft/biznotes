# 신규(#269). 필요: PostgreSQL·Valkey(PG 테스트).
"""overview 재생성·태그 부착·해제의 권한 행렬 — 실제 라우터·인증·DB·Redis.

무관 사용자는 없는 노트북과 같은 404를 받는다. 권한자는 지금의 403을 받는다. 태그는 실제
소유자만 다루므로 admin(비소유자)도 403이다. 재생성 예약(Celery)만 대역이다.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from conftest import share_to_user
from fastapi import FastAPI
from sqlalchemy import func, select

from biznotes_core.api.v1 import auth, notebooks, tags
from biznotes_core.core.db import get_session_factory
from biznotes_core.core.security import hash_password
from biznotes_core.models import Group, Notebook, NotebookShare, NotebookTag, Tag, User, UserGroup
from biznotes_core.services import overview_refresh

PASSWORD = "pw-test-1234"  # tests/conftest.py 공용 해시의 평문
NOT_FOUND = {"detail": "Notebook not found"}
WRITE_REQUIRED = {"detail": "Write permission required"}
OWNER_REQUIRED = {"detail": "Owner permission required"}


class _Api:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.tokens: dict[uuid.UUID, str] = {}
        self.scheduled: list[str] = []  # 재생성 예약(대역)이 받은 노트북 id

    async def call(self, user: User, method: str, path: str) -> tuple[int, Any]:
        if user.id not in self.tokens:
            self.client.cookies.clear()
            res = await self.client.post(
                "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
            )
            assert res.status_code == 200, res.text
            self.tokens[user.id] = res.json()["access_token"]
        self.client.cookies.clear()
        headers = {"Authorization": f"Bearer {self.tokens[user.id]}"}
        res = await self.client.request(method, f"/api/v1{path}", headers=headers)
        return res.status_code, (res.json() if res.content else None)


@pytest.fixture
async def api(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Api]:
    # 재생성 예약은 브로커 경계 밖이다 — Redis 잠금은 실제로 잡는다
    scheduled: list[str] = []
    # 라우터는 서비스 헬퍼를 부른다(#321) — 워커 모듈의 재노출이 아니라 정의 지점을 막는다
    monkeypatch.setattr(
        overview_refresh, "schedule_overview_refresh", lambda nid, **_kw: scheduled.append(nid)
    )
    app = FastAPI()
    for router in (auth.router, notebooks.router, tags.router):
        app.include_router(router, prefix="/api/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        api = _Api(client)
        api.scheduled = scheduled
        yield api


async def _users(**roles: str) -> dict[str, User]:
    async with get_session_factory()() as s:
        hashed = hash_password(PASSWORD)
        users = {
            n: User(
                email=f"{n}-{uuid.uuid4().hex[:6]}@test.dev", password_hash=hashed, name=n, role=r
            )
            for n, r in roles.items()
        }
        s.add_all(users.values())
        await s.commit()
        return users


async def _world(u: dict[str, User]) -> dict[str, uuid.UUID]:
    """비공개 노트북(reader 읽기·writer 쓰기·그룹 읽기 공유), public 노트북, 사용자별 태그."""
    async with get_session_factory()() as s:
        group = Group(name=f"g-{uuid.uuid4().hex[:6]}")
        private = Notebook(owner_id=u["owner"].id, title="비공개")
        public = Notebook(owner_id=u["owner"].id, title="공개", visibility="public")
        tag_rows = {n: Tag(owner_id=user.id, name=f"t-{n}") for n, user in u.items()}
        s.add_all([group, private, public, *tag_rows.values()])
        await s.flush()
        s.add_all(
            [
                UserGroup(user_id=u["member"].id, group_id=group.id),
                *share_to_user(private.id, u["reader"].id, "read"),
                *share_to_user(private.id, u["writer"].id, "write"),
                NotebookShare(notebook_id=private.id, group_id=group.id, permission="read"),
            ]
        )
        await s.commit()
        return {
            "private": private.id,
            "public": public.id,
            **{f"tag:{n}": t.id for n, t in tag_rows.items()},
        }


async def _tag_links() -> int:
    async with get_session_factory()() as s:
        return int(await s.scalar(select(func.count()).select_from(NotebookTag)) or 0)


def _paths(nb_id: uuid.UUID, tag_id: uuid.UUID) -> list[tuple[str, str, str]]:
    return [
        ("regenerate", "POST", f"/notebooks/{nb_id}/overview/regenerate"),
        ("attach", "POST", f"/notebooks/{nb_id}/tags/{tag_id}"),
        ("detach", "DELETE", f"/notebooks/{nb_id}/tags/{tag_id}"),
    ]


@pytest.fixture
async def world() -> tuple[dict[str, User], dict[str, uuid.UUID]]:
    u = await _users(
        owner="user", reader="user", writer="user", member="user", stranger="user", admin="admin"
    )
    return u, await _world(u)


async def test_unrelated_user_sees_the_same_404_as_a_missing_notebook(
    api: _Api, world: tuple[dict[str, User], dict[str, uuid.UUID]]
) -> None:
    u, w = world
    for nb_key in ("private",):
        for tag_owner in ("stranger", "owner"):  # 자기 태그든 남의 태그든 노트북 404가 먼저다
            tag_id = w[f"tag:{tag_owner}"]
            for (name, method, path), (_, m_method, m_path) in zip(
                _paths(w[nb_key], tag_id), _paths(uuid.uuid4(), tag_id), strict=True
            ):
                assert await api.call(u["stranger"], method, path) == (404, NOT_FOUND), name
                assert await api.call(u["stranger"], m_method, m_path) == (404, NOT_FOUND), name
    assert await _tag_links() == 0
    assert api.scheduled == []  # 거부는 예약에 닿지 않는다


async def test_permission_holders_keep_403(
    api: _Api, world: tuple[dict[str, User], dict[str, uuid.UUID]]
) -> None:
    u, w = world
    cases = [
        ("reader", w["private"]),  # 사용자 읽기 공유
        ("member", w["private"]),  # 그룹 읽기 공유
        ("stranger", w["public"]),  # public 열람
    ]
    for actor, nb_id in cases:
        regen, attach, detach = _paths(nb_id, w[f"tag:{actor}"])
        assert await api.call(u[actor], regen[1], regen[2]) == (403, WRITE_REQUIRED), actor
        assert await api.call(u[actor], attach[1], attach[2]) == (403, OWNER_REQUIRED), actor
        assert await api.call(u[actor], detach[1], detach[2]) == (403, OWNER_REQUIRED), actor
    assert await _tag_links() == 0
    assert api.scheduled == []


async def test_writer_and_admin_regenerate_but_cannot_tag(
    api: _Api, world: tuple[dict[str, User], dict[str, uuid.UUID]]
) -> None:
    u, w = world
    for actor in ("writer", "admin"):
        regen, attach, detach = _paths(w["private"], w[f"tag:{actor}"])
        status, _ = await api.call(u[actor], regen[1], regen[2])
        assert status == 202, actor
        assert await api.call(u[actor], attach[1], attach[2]) == (403, OWNER_REQUIRED), actor
        assert await api.call(u[actor], detach[1], detach[2]) == (403, OWNER_REQUIRED), actor
    assert await _tag_links() == 0


async def test_owner_tags_and_regenerates_as_before(
    api: _Api, world: tuple[dict[str, User], dict[str, uuid.UUID]]
) -> None:
    u, w = world
    regen, attach, detach = _paths(w["private"], w["tag:owner"])
    assert (await api.call(u["owner"], regen[1], regen[2]))[0] == 202
    assert api.scheduled == [str(w["private"])]
    assert (await api.call(u["owner"], attach[1], attach[2]))[0] == 204
    assert await _tag_links() == 1
    assert (await api.call(u["owner"], detach[1], detach[2]))[0] == 204
    assert await _tag_links() == 0
