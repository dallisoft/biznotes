"""내 그룹 `GET /api/v1/me/groups` — 제품 조립(core-v0.20.0-upgrade AC-2.2, D-10·HCI-10).

코어 v0.19.0 #278 1단계의 `groups.router`를 제품 `api_router`가 `admin_router` 앞에 조립한다.
요청자 자신의 소속만 이름순으로 준다 — admin도 같다(전체 그룹은 관리자 API).
비인가 관점(루트 §3.4-5): 무토큰 401, 강제 비밀번호 변경 사용자 403, 남의 그룹 미포함.

모두 제품 조립 앱(`client` — `app.main:app`)에 실 HTTP로 부른다. 대체 없음.
"""

import uuid

import httpx
from biznotes_core.models import Group, User, UserGroup

from app.api.v1.router import api_router
from tests.conftest import bearer, login

ME_GROUPS = "/api/v1/me/groups"


async def _group(db, name: str, *members: User) -> Group:
    group = Group(name=name)
    db.add(group)
    await db.flush()
    db.add_all([UserGroup(user_id=m.id, group_id=group.id, role="member") for m in members])
    await db.commit()
    await db.refresh(group)
    return group


def test_groups_router_is_assembled_once_before_admin() -> None:
    """조립 위치(D-10): `/me/groups`는 `/admin` 앞에 한 번만 온다."""
    paths = [getattr(r, "path", "") for r in api_router.routes]
    assert paths.count("/me/groups") == 1
    at = paths.index("/me/groups")
    first_admin = min(i for i, p in enumerate(paths) if p.startswith("/admin"))
    assert at < first_admin


async def test_me_groups_without_token_is_401(client: httpx.AsyncClient) -> None:
    client.cookies.clear()
    res = await client.get(ME_GROUPS)
    assert res.status_code == 401, res.text


async def test_me_groups_password_change_required_user_is_403(
    client: httpx.AsyncClient, db, user_a: User
) -> None:
    await _group(db, f"forced-{uuid.uuid4().hex[:8]}", user_a)
    token = await login(client, user_a.email)  # 토큰을 받은 뒤 강제 변경 대상이 된다
    row = await db.get(User, user_a.id)
    assert row is not None
    row.must_change_password = True
    await db.commit()

    res = await client.get(ME_GROUPS, headers=bearer(token))
    assert res.status_code == 403, res.text
    assert res.json()["detail"]["code"] == "password_change_required"


async def test_me_groups_lists_own_groups_by_name_without_others(
    client: httpx.AsyncClient, db, user_a: User, user_b: User
) -> None:
    suffix = uuid.uuid4().hex[:8]
    # 이름순과 다른 순서로 만든다 — 응답 순서가 생성 순서가 아니라 이름순인지 본다
    zeta = await _group(db, f"zeta-{suffix}", user_a, user_b)
    alpha = await _group(db, f"alpha-{suffix}", user_a)
    other = await _group(db, f"beta-{suffix}", user_b)  # user_a 비소속

    token = await login(client, user_a.email)
    res = await client.get(ME_GROUPS, headers=bearer(token))
    assert res.status_code == 200, res.text
    assert res.json() == [
        {"id": str(alpha.id), "name": alpha.name},
        {"id": str(zeta.id), "name": zeta.name},
    ]
    assert str(other.id) not in res.text

    token_b = await login(client, user_b.email)
    res_b = await client.get(ME_GROUPS, headers=bearer(token_b))
    assert res_b.status_code == 200, res_b.text
    assert [g["id"] for g in res_b.json()] == [str(other.id), str(zeta.id)]


async def test_me_groups_admin_sees_only_own_memberships(
    client: httpx.AsyncClient, db, admin_user: User, user_a: User
) -> None:
    suffix = uuid.uuid4().hex[:8]
    mine = await _group(db, f"admins-{suffix}", admin_user)
    not_mine = await _group(db, f"members-{suffix}", user_a)
    empty = await _group(db, f"empty-{suffix}")

    token = await login(client, admin_user.email)
    res = await client.get(ME_GROUPS, headers=bearer(token))
    assert res.status_code == 200, res.text
    assert res.json() == [{"id": str(mine.id), "name": mine.name}]
    assert str(not_mine.id) not in res.text and str(empty.id) not in res.text


async def test_me_groups_without_membership_is_empty_list(
    client: httpx.AsyncClient, db, user_a: User, user_b: User
) -> None:
    await _group(db, f"elsewhere-{uuid.uuid4().hex[:8]}", user_b)

    token = await login(client, user_a.email)
    res = await client.get(ME_GROUPS, headers=bearer(token))
    assert res.status_code == 200, res.text
    assert res.json() == []
