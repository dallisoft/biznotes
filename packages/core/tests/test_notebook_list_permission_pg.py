# 신규(#221). 필요: PostgreSQL·Valkey(PG 테스트).
"""노트북 목록의 `permission`·`is_owner`가 같은 노트북의 단건 `GET`과 같다 — 실제 라우터·인증·DB.

관리자는 목록에서도 승격된 `owner`이고, 실제 소유는 `is_owner`로 구분한다. `filter` 범위는 실제
관계 그대로다(관리자에게 남의 비공개 노트북을 넣지 않는다).
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from conftest import share_to_user
from fastapi import FastAPI

from biznotes_core.api.v1 import auth, notebooks
from biznotes_core.core.db import get_session_factory
from biznotes_core.core.security import hash_password
from biznotes_core.models import Group, Notebook, NotebookShare, User, UserGroup

PASSWORD = "pw-test-1234"  # tests/conftest.py 공용 해시의 평문


def _app() -> FastAPI:
    app = FastAPI()
    for router in (auth.router, notebooks.router):
        app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _get(client: httpx.AsyncClient, user: User, path: str) -> httpx.Response:
    client.cookies.clear()
    res = await client.post("/api/v1/auth/login", json={"email": user.email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    client.cookies.clear()
    headers = {"Authorization": f"Bearer {res.json()['access_token']}"}
    return await client.get(f"/api/v1{path}", headers=headers)


async def _world(owner: User, member: User, admin: User) -> dict[str, uuid.UUID]:
    """owner 소유 노트북(비공개·공유 없는 group 가시성·member 읽기 공유·그룹 쓰기 공유·public·
    admin 그룹 읽기 공유)과 admin 소유 하나.

    읽기 공유 노트북은 admin에게도 읽기로 공유한다 — admin의 `shared` 행. 비공개·group 가시성
    노트북은 admin과 무관하다 — admin 목록에는 없고 단건은 승격된 owner다(범위는 실제 관계).
    owner도 그룹 쓰기 공유의 그룹에 넣는다 — `shared`는 자기 소유를 빼야 한다.
    """
    async with get_session_factory()() as s:
        group = Group(name=f"g-{uuid.uuid4().hex[:6]}")
        admin_group = Group(name=f"ag-{uuid.uuid4().hex[:6]}")
        nbs = {
            "private": Notebook(owner_id=owner.id, title="비공개"),
            "read": Notebook(owner_id=owner.id, title="읽기 공유"),
            "group_write": Notebook(owner_id=owner.id, title="그룹 쓰기 공유"),
            "public": Notebook(owner_id=owner.id, title="공개", visibility="public"),
            "admin_group": Notebook(owner_id=owner.id, title="관리자 그룹 공유"),
            "admin_own": Notebook(owner_id=admin.id, title="관리자 것"),
        }
        s.add_all([group, admin_group, *nbs.values()])
        await s.flush()
        s.add_all(
            [
                UserGroup(user_id=member.id, group_id=group.id),
                UserGroup(user_id=owner.id, group_id=group.id),
                UserGroup(user_id=admin.id, group_id=admin_group.id),
                *share_to_user(nbs["read"].id, member.id, "read"),
                *share_to_user(nbs["read"].id, admin.id, "read"),
                NotebookShare(
                    notebook_id=nbs["group_write"].id, group_id=group.id, permission="write"
                ),
                NotebookShare(
                    notebook_id=nbs["admin_group"].id, group_id=admin_group.id, permission="read"
                ),
            ]
        )
        await s.commit()
        return {k: nb.id for k, nb in nbs.items()}


async def _listed(client: httpx.AsyncClient, user: User, filter_: str) -> dict[uuid.UUID, Any]:
    res = await _get(client, user, f"/notebooks?filter={filter_}")
    assert res.status_code == 200, res.text
    return {uuid.UUID(row["id"]): row for row in res.json()}


async def test_list_rows_match_single_get(
    client: httpx.AsyncClient, user_a: User, user_b: User, admin_user: User
) -> None:
    ids = await _world(user_a, user_b, admin_user)
    name = {v: k for k, v in ids.items()}
    async with get_session_factory()() as s:
        stranger = User(email=f"s-{uuid.uuid4().hex[:6]}@test.dev", name="stranger")
        stranger.password_hash = hash_password(PASSWORD)
        s.add(stranger)
        await s.commit()

    expected = {
        # (viewer, filter): {노트북: (permission, is_owner)}
        (user_a.id, "all"): {
            "private": ("owner", True),
            "read": ("owner", True),
            "group_write": ("owner", True),
            "public": ("owner", True),
            "admin_group": ("owner", True),
        },
        (user_a.id, "mine"): {
            "private": ("owner", True),
            "read": ("owner", True),
            "group_write": ("owner", True),
            "public": ("owner", True),
            "admin_group": ("owner", True),
        },
        # 자기 노트북이 자기 그룹에 공유돼 있어도 shared에는 없다
        (user_a.id, "shared"): {},
        # 아무 관계도 없는 사용자에게는 public만 보인다
        (stranger.id, "all"): {"public": ("read", False)},
        (stranger.id, "shared"): {},
        (user_b.id, "all"): {
            "read": ("read", False),
            "group_write": ("write", False),
            "public": ("read", False),
        },
        (user_b.id, "mine"): {},
        (user_b.id, "shared"): {"read": ("read", False), "group_write": ("write", False)},
        # 관리자: 범위는 실제 관계(자기 것·공유·public), 권한은 승격된 owner
        (admin_user.id, "all"): {
            "admin_own": ("owner", True),
            "read": ("owner", False),
            "public": ("owner", False),
            "admin_group": ("owner", False),
        },
        (admin_user.id, "mine"): {"admin_own": ("owner", True)},
        (admin_user.id, "shared"): {"read": ("owner", False), "admin_group": ("owner", False)},
    }
    viewers = {u.id: u for u in (user_a, user_b, admin_user, stranger)}

    for (viewer_id, filter_), want in expected.items():
        viewer = viewers[viewer_id]
        rows = await _listed(client, viewer, filter_)
        got = {name[i]: (r["permission"], r["is_owner"]) for i, r in rows.items()}
        # 범위(어떤 노트북이 나오는가)를 권한 표기와 따로 단언한다 — 범위는 수정 전과 같아야 한다
        assert set(got) == set(want), ("범위", viewer.name, filter_)
        assert got == want, ("권한", viewer.name, filter_)
        for nb_id, row in rows.items():
            single = await _get(client, viewer, f"/notebooks/{nb_id}")
            assert single.status_code == 200
            assert single.json()["permission"] == row["permission"], (viewer.name, name[nb_id])
            assert single.json()["is_owner"] == row["is_owner"]

    # 관리자와 무관한 비공개 노트북: 목록 범위 밖이지만 단건은 승격된 owner다
    for key in ("private",):
        single = await _get(client, admin_user, f"/notebooks/{ids[key]}")
        assert (single.status_code, single.json()["permission"]) == (200, "owner")
        assert single.json()["is_owner"] is False
