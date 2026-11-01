import json
import uuid

import httpx
import pytest

from tests.conftest import _create_user, bearer, login, share_group


async def _create_notebook(client, token, title="My Notebook") -> dict:
    res = await client.post(
        "/api/v1/notebooks", json={"title": title, "emoji": "🧪"}, headers=bearer(token)
    )
    assert res.status_code == 201, res.text
    return res.json()


async def test_crud_flow(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)

    nb = await _create_notebook(client, token)
    assert nb["title"] == "My Notebook"
    assert nb["is_owner"] is True
    assert nb["permission"] == "owner"

    res = await client.get("/api/v1/notebooks?filter=mine", headers=bearer(token))
    assert res.status_code == 200
    assert any(n["id"] == nb["id"] for n in res.json())

    res = await client.patch(
        f"/api/v1/notebooks/{nb['id']}", json={"title": "Renamed"}, headers=bearer(token)
    )
    assert res.status_code == 200
    assert res.json()["title"] == "Renamed"

    res = await client.delete(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token))
    assert res.status_code == 204

    res = await client.get(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token))
    assert res.status_code == 404


async def test_unauthenticated_rejected(client: httpx.AsyncClient):
    res = await client.get("/api/v1/notebooks")
    assert res.status_code == 401


async def test_private_notebook_invisible_to_others(client: httpx.AsyncClient, user_a, user_b):
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "Private A")

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    res = await client.get(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_b))
    assert res.status_code == 404

    res = await client.get("/api/v1/notebooks?filter=all", headers=bearer(token_b))
    assert all(n["id"] != nb["id"] for n in res.json())


async def test_share_read_permission(client: httpx.AsyncClient, db, user_a, user_b):
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "Shared Read")

    group = await share_group(db, user_a, user_b)
    res = await client.post(
        f"/api/v1/notebooks/{nb['id']}/share",
        json={"group_id": str(group.id), "permission": "read"},
        headers=bearer(token_a),
    )
    assert res.status_code == 201, res.text

    client.cookies.clear()
    token_b = await login(client, user_b.email)

    # 공유 필터에 노출
    res = await client.get("/api/v1/notebooks?filter=shared", headers=bearer(token_b))
    assert any(n["id"] == nb["id"] for n in res.json())

    # 읽기 가능
    res = await client.get(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_b))
    assert res.status_code == 200
    assert res.json()["permission"] == "read"

    # 쓰기/삭제 불가
    res = await client.patch(
        f"/api/v1/notebooks/{nb['id']}", json={"title": "Hacked"}, headers=bearer(token_b)
    )
    assert res.status_code == 403
    res = await client.delete(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_b))
    assert res.status_code == 403


async def test_share_write_permission(client: httpx.AsyncClient, db, user_a, user_b):
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "Shared Write")

    group = await share_group(db, user_a, user_b)
    await client.post(
        f"/api/v1/notebooks/{nb['id']}/share",
        json={"group_id": str(group.id), "permission": "write"},
        headers=bearer(token_a),
    )

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    res = await client.patch(
        f"/api/v1/notebooks/{nb['id']}", json={"title": "Edited by B"}, headers=bearer(token_b)
    )
    assert res.status_code == 200

    # write라도 삭제는 owner만
    res = await client.delete(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_b))
    assert res.status_code == 403


async def test_share_requires_owner(client: httpx.AsyncClient, db, user_a, user_b):
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "Owner Only Share")
    group = await share_group(db, user_a, user_b)
    await client.post(
        f"/api/v1/notebooks/{nb['id']}/share",
        json={"group_id": str(group.id), "permission": "write"},
        headers=bearer(token_a),
    )

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    res = await client.post(
        f"/api/v1/notebooks/{nb['id']}/share",
        json={"group_id": str(group.id), "permission": "read"},
        headers=bearer(token_b),
    )
    assert res.status_code == 403


async def test_group_share(client: httpx.AsyncClient, db, user_a, user_b, admin_user):
    """그룹 공유는 노트북 **실제 소유자**가 속한 그룹만 대상이다(코어 v0.19.0 #278, AC-1.8 ①ⓔ).

    소유자가 구성원인 그룹 → 201·구성원 열람 가능. 소유자가 비구성원인 그룹 → 404
    `Target group not found`·행 0. admin이 남의 노트북을 공유해도 소유자 소속을 본다 — 소유자 소속
    그룹 201, admin 소속 그룹(소유자 비구성원) 404.
    """
    from biznotes_core.models import Group, NotebookShare, UserGroup
    from sqlalchemy import func, select

    suffix = user_b.id.hex[:8]
    team = Group(name=f"team-{suffix}")  # 소유자(user_a)·user_b
    outside = Group(name=f"outside-{suffix}")  # user_b만
    admins = Group(name=f"admins-{suffix}")  # admin만
    db.add_all([team, outside, admins])
    await db.flush()
    db.add_all(
        [
            UserGroup(user_id=user_a.id, group_id=team.id, role="member"),
            UserGroup(user_id=user_b.id, group_id=team.id, role="member"),
            UserGroup(user_id=user_b.id, group_id=outside.id, role="member"),
            UserGroup(user_id=admin_user.id, group_id=admins.id, role="member"),
        ]
    )
    await db.commit()
    team_id, outside_id, admins_id = str(team.id), str(outside.id), str(admins.id)

    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "Group Shared")
    res = await _share(client, token_a, nb["id"], group_id=team_id, permission="read")
    assert res.status_code == 201, res.text

    res = await _share(client, token_a, nb["id"], group_id=outside_id, permission="read")
    assert res.status_code == 404
    assert res.json()["detail"] == "Target group not found"
    outside_rows = await db.scalar(
        select(func.count())
        .select_from(NotebookShare)
        .where(NotebookShare.group_id == uuid.UUID(outside_id))
    )
    assert outside_rows == 0

    token_b = await login(client, user_b.email)
    res = await client.get(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_b))
    assert res.status_code == 200

    # admin 승격 — 남의 노트북도 공유할 수 있지만 대상 그룹은 소유자 소속이어야 한다
    nb2 = await _create_notebook(client, token_a, "Group Shared By Admin")
    token_admin = await login(client, admin_user.email)
    res = await _share(client, token_admin, nb2["id"], group_id=team_id, permission="write")
    assert res.status_code == 201, res.text
    res = await _share(client, token_admin, nb2["id"], group_id=admins_id, permission="read")
    assert res.status_code == 404
    assert res.json()["detail"] == "Target group not found"
    res = await client.get(f"/api/v1/notebooks/{nb2['id']}/shares", headers=bearer(token_a))
    assert [(s["group_id"], s["permission"]) for s in res.json()] == [(team_id, "write")]


async def test_existing_non_member_group_share_still_grants_and_can_only_be_removed(
    client: httpx.AsyncClient, db, user_a, user_b
):
    """v0.19.0 전에 생긴 「소유자가 비구성원인 그룹」 공유 행은 계속 권한을 주고, 다시 공유는 404,
    해제는 204다(코어 #278 1단계 — AC-1.8 ①ⓕ). 행은 DB에 직접 둔다(코어 API로는 더 못 만든다)."""
    from biznotes_core.models import Group, Notebook, NotebookShare, UserGroup

    outside = Group(name=f"legacy-{user_b.id.hex[:8]}")  # user_b만, 소유자(user_a)는 비구성원
    db.add(outside)
    await db.flush()
    db.add(UserGroup(user_id=user_b.id, group_id=outside.id, role="member"))
    nb = Notebook(owner_id=user_a.id, title="Legacy Group Share")
    db.add(nb)
    await db.flush()
    row = NotebookShare(notebook_id=nb.id, group_id=outside.id, permission="read")
    db.add(row)
    await db.commit()
    nb_id, group_id, share_id = str(nb.id), str(outside.id), str(row.id)

    token_b = await login(client, user_b.email)
    res = await client.get(f"/api/v1/notebooks/{nb_id}", headers=bearer(token_b))
    assert res.status_code == 200
    assert res.json()["permission"] == "read"

    token_a = await login(client, user_a.email)
    res = await _share(client, token_a, nb_id, group_id=group_id, permission="write")
    assert res.status_code == 404
    assert res.json()["detail"] == "Target group not found"
    res = await client.get(f"/api/v1/notebooks/{nb_id}/shares", headers=bearer(token_a))
    assert [(s["id"], s["permission"]) for s in res.json()] == [(share_id, "read")]

    res = await client.delete(
        f"/api/v1/notebooks/{nb_id}/shares/{share_id}", headers=bearer(token_a)
    )
    assert res.status_code == 204
    token_b = await login(client, user_b.email)
    res = await client.get(f"/api/v1/notebooks/{nb_id}", headers=bearer(token_b))
    assert res.status_code == 404


async def test_share_user_target_is_422_before_notebook_check(
    client: httpx.AsyncClient, db, user_a, user_b
):
    """사용자(이메일·id) 대상 공유는 없앴다 — 보내면 422다(코어 v0.28.0 #278 3단계, core-v0.28.0
    AC-5). 스키마 검증이라 노트북 판정보다 먼저다: 소유자·무관 사용자·없는 노트북 모두 422이고
    존재 여부가 드러나지 않는다. `group_id`가 빠져도, 권한이 `read`·`write` 밖이어도 422다. 행은
    생기지 않는다."""
    from biznotes_core.models import NotebookShare
    from sqlalchemy import func, select

    group = await share_group(db, user_a, user_b)
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "User Target Removed")
    bodies = [
        {"user_email": user_b.email, "permission": "read"},
        {"user_id": str(user_b.id), "permission": "read"},
        {"user_email": user_b.email, "group_id": str(group.id), "permission": "read"},
        {"permission": "read"},
        {"group_id": str(group.id), "permission": "owner"},
    ]
    for body in bodies:
        res = await _share(client, token_a, nb["id"], **body)
        assert res.status_code == 422, (body, res.text)

    token_b = await login(client, user_b.email)  # 공유 없는 무관 사용자
    res = await _share(client, token_b, nb["id"], user_email=user_b.email, permission="read")
    assert res.status_code == 422
    token_a = await login(client, user_a.email)
    res = await _share(
        client, token_a, str(uuid.uuid4()), user_email="ghost@test.dev", permission="read"
    )
    assert res.status_code == 422

    rows = await db.scalar(
        select(func.count())
        .select_from(NotebookShare)
        .where(NotebookShare.notebook_id == uuid.UUID(nb["id"]))
    )
    assert rows == 0


async def test_visibility_group_is_422(client: httpx.AsyncClient, user_a):
    """공개 범위는 `private`·`public`뿐이다 — `group`은 422이고 값은 그대로다(코어 v0.28.0
    #278 3단계·#277)."""
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "No Group Visibility")
    res = await client.patch(
        f"/api/v1/notebooks/{nb['id']}", json={"visibility": "group"}, headers=bearer(token_a)
    )
    assert res.status_code == 422
    res = await client.get(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_a))
    assert res.json()["visibility"] == "private"


# ---- notebook-bulk-actions AC-2.1 ----
# 웹 일괄 삭제가 의존하는 코어 단건 DELETE 동작을 고정한다(특성 테스트). 웹은 서버 판정을
# 그대로 따르므로(plan §6) 코어가 이 응답을 바꾸면 여기서 실패해 웹 분류표를 다시 보게 한다.


async def test_delete_by_unrelated_user_is_404(client: httpx.AsyncClient, user_a, user_b):
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "Unrelated Delete")

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    res = await client.delete(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_b))
    assert res.status_code == 404
    assert res.json()["detail"] == "Notebook not found"

    # 노트북은 그대로 남는다
    client.cookies.clear()
    token_a = await login(client, user_a.email)
    res = await client.get(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_a))
    assert res.status_code == 200


async def test_delete_by_read_sharer_is_403_with_owner_detail(
    client: httpx.AsyncClient, db, user_a, user_b
):
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "Read Sharer Delete")
    group = await share_group(db, user_a, user_b)
    res = await client.post(
        f"/api/v1/notebooks/{nb['id']}/share",
        json={"group_id": str(group.id), "permission": "read"},
        headers=bearer(token_a),
    )
    assert res.status_code == 201, res.text

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    res = await client.delete(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_b))
    assert res.status_code == 403
    assert res.json()["detail"] == "Only the owner can delete"


async def test_admin_can_delete_others_notebook_by_promotion(
    client: httpx.AsyncClient, user_a, admin_user
):
    """서버는 admin을 owner로 승격해 남의 노트북 삭제를 허용한다(204).

    웹 일괄 UI는 이 승격을 쓰지 않는다(D-02 — `is_owner` 기준). UI가 쓰지 않는 서버 동작을
    고정해 두어, 바뀌면 계획 §6 권한 매트릭스를 다시 보게 한다. 단건 GET은 `permission`을
    owner로 승격하지만 `is_owner`는 거짓이고, 목록(`filter=all`)에는 아예 없다(F-4).
    """
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "Admin Promotion Delete")

    client.cookies.clear()
    token_admin = await login(client, admin_user.email)
    res = await client.get(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_admin))
    assert res.status_code == 200
    assert res.json()["permission"] == "owner"  # 단건 판정은 승격한다
    assert res.json()["is_owner"] is False  # 웹 일괄 자격(is_owner)은 실제 소유자만
    res = await client.get("/api/v1/notebooks?filter=all", headers=bearer(token_admin))
    assert all(n["id"] != nb["id"] for n in res.json())  # 목록에는 남의 private이 없다(F-4)
    res = await client.delete(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_admin))
    assert res.status_code == 204

    # 삭제 뒤 소유자 GET → 404
    client.cookies.clear()
    token_a = await login(client, user_a.email)
    res = await client.get(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_a))
    assert res.status_code == 404


async def test_delete_twice_is_404_for_owner(client: httpx.AsyncClient, user_a):
    """이미 지운 노트북을 다시 지우면 404 — 웹이 DELETE를 자동 재시도하지 않는 근거(D-05)."""
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "Delete Twice")
    res = await client.delete(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_a))
    assert res.status_code == 204
    res = await client.delete(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_a))
    assert res.status_code == 404
    assert res.json()["detail"] == "Notebook not found"
    res = await client.get(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_a))
    assert res.status_code == 404


# ---- notebook-bulk-actions AC-3.1 ----
# 코어 공유 API 동작을 고정한다(특성 테스트). 공유 대상은 그룹(`{group_id, permission}`)뿐이다
# (코어 v0.28.0 #278 3단계 — 사용자 대상 `user_email`·`user_id`는 422).


async def _share(client, token, notebook_id, **body):
    return await client.post(
        f"/api/v1/notebooks/{notebook_id}/share", json=body, headers=bearer(token)
    )


async def test_share_list_and_unshare_forbidden_for_write_sharer(
    client: httpx.AsyncClient, db, user_a, user_b
):
    group = await share_group(db, user_a, user_b)
    token_a = await login(client, user_a.email)
    token_b = await login(client, user_b.email)
    nb = await _create_notebook(client, token_a, "Shares Owner Only")
    res = await _share(client, token_a, nb["id"], group_id=str(group.id), permission="write")
    assert res.status_code == 201, res.text
    share_id = res.json()["id"]

    res = await client.get(f"/api/v1/notebooks/{nb['id']}/shares", headers=bearer(token_b))
    assert res.status_code == 403
    assert res.json()["detail"] == "Only the owner can view shares"
    res = await client.delete(
        f"/api/v1/notebooks/{nb['id']}/shares/{share_id}", headers=bearer(token_b)
    )
    assert res.status_code == 403
    assert res.json()["detail"] == "Only the owner can unshare"

    # 거부된 DELETE 뒤에도 공유 행은 그대로다
    res = await client.get(f"/api/v1/notebooks/{nb['id']}/shares", headers=bearer(token_a))
    assert [(s["id"], s["permission"]) for s in res.json()] == [(share_id, "write")]


async def test_share_endpoints_by_unrelated_user_are_404(
    client: httpx.AsyncClient, db, user_a, user_b
):
    """무관 사용자(공유 없음)의 POST /share·GET /shares·DELETE /shares/{id}는 단건 GET과 같은 404
    `Notebook not found`다 — 노트북 존재를 드러내지 않는다(코어 v0.18.0 #223, AC-1.8 ①ⓐ). 공유 행은
    그대로다."""
    third = await _create_user(db, "share-target")
    group = await share_group(db, user_a, third)
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "Unrelated Share")
    res = await _share(client, token_a, nb["id"], group_id=str(group.id), permission="write")
    assert res.status_code == 201, res.text
    share_id = res.json()["id"]

    token_b = await login(client, user_b.email)
    responses = [
        await _share(client, token_b, nb["id"], group_id=str(group.id), permission="read"),
        await client.get(f"/api/v1/notebooks/{nb['id']}/shares", headers=bearer(token_b)),
        await client.delete(
            f"/api/v1/notebooks/{nb['id']}/shares/{share_id}", headers=bearer(token_b)
        ),
    ]
    assert [(r.status_code, r.json()["detail"]) for r in responses] == [
        (404, "Notebook not found")
    ] * 3

    token_a = await login(client, user_a.email)
    res = await client.get(f"/api/v1/notebooks/{nb['id']}/shares", headers=bearer(token_a))
    assert [(s["id"], s["permission"]) for s in res.json()] == [(share_id, "write")]


async def test_share_target_missing_details(client: httpx.AsyncClient, db, user_a, user_b):
    """대상 해석 문구와 권한 판정의 순서(코어 v0.18.0 #223·v0.28.0 #278 3단계, AC-1.8 ①ⓑ).

    소유자의 없는 그룹 id는 404 `Target group not found`다. 권한 판정이 대상 해석보다 먼저라 read
    공유자가 없는 그룹으로 보내면 403 `Only the owner can share`이고, 무관 사용자는 404
    `Notebook not found`다 — 그룹 존재 여부가 드러나지 않는다.
    """
    group = await share_group(db, user_a, user_b)
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "Share Target Missing")
    res = await _share(client, token_a, nb["id"], group_id=str(group.id), permission="read")
    assert res.status_code == 201, res.text
    read_share_id = res.json()["id"]

    ghost = str(uuid.uuid4())
    res = await _share(client, token_a, nb["id"], group_id=ghost, permission="read")
    assert (res.status_code, res.json()["detail"]) == (404, "Target group not found")

    token_b = await login(client, user_b.email)  # read 공유자
    res = await _share(client, token_b, nb["id"], group_id=ghost, permission="read")
    assert (res.status_code, res.json()["detail"]) == (403, "Only the owner can share")

    third = await _create_user(db, "unrelated")
    token_c = await login(client, third.email)
    res = await _share(client, token_c, nb["id"], group_id=ghost, permission="read")
    assert (res.status_code, res.json()["detail"]) == (404, "Notebook not found")

    token_a = await login(client, user_a.email)
    res = await client.get(f"/api/v1/notebooks/{nb['id']}/shares", headers=bearer(token_a))
    assert [(s["id"], s["permission"]) for s in res.json()] == [(read_share_id, "read")]


async def test_group_share_response_has_no_user_id(client: httpx.AsyncClient, db, user_a, user_b):
    """그룹 공유 응답은 `id`·`notebook_id`·`group_id`·`permission`이고 `user_id`가 없다(코어
    v0.28.0 #278 3단계 — 웹 대상 표기는 `group_id`만 쓴다). 목록도 같다."""
    group = await share_group(db, user_a, user_b)
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "Share Response Shape")
    res = await _share(client, token_a, nb["id"], group_id=str(group.id), permission="write")
    assert res.status_code == 201, res.text
    body = res.json()
    assert uuid.UUID(body["id"])
    assert set(body) == {"id", "notebook_id", "group_id", "permission"}
    assert (body["notebook_id"], body["group_id"], body["permission"]) == (
        nb["id"],
        str(group.id),
        "write",
    )
    res = await client.get(f"/api/v1/notebooks/{nb['id']}/shares", headers=bearer(token_a))
    assert [set(row) for row in res.json()] == [{"id", "notebook_id", "group_id", "permission"}]


async def test_reshare_same_target_is_idempotent(client: httpx.AsyncClient, db, user_a, user_b):
    """같은 그룹 재공유는 행을 늘리지 않고 그 행의 권한을 요청 값으로 바꾼다(하향 포함). 응답은
    201이고 `id`는 처음 공유와 같다(코어 v0.19.0 #222, AC-1.8 ①ⓓ — 옛 F-1 「두 번째 행」 대체).
    v0.28.0부터 `(notebook_id, group_id)`는 유일 제약이다(#277).

    웹 일괄 공유가 probe 응답의 `id`를 보상 DELETE하면 원래 있던 공유를 지우게 된다 — 그래서
    보상 DELETE를 없앴다(D-5, AC-1.9 ①).
    """
    from biznotes_core.models.notebook_share import NotebookShare
    from sqlalchemy import func, select

    async def rows() -> int:
        return await db.scalar(
            select(func.count())
            .select_from(NotebookShare)
            .where(
                NotebookShare.notebook_id == uuid.UUID(nb["id"]),
                NotebookShare.group_id == group.id,
            )
        )

    group = await share_group(db, user_a, user_b)
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "Idempotent Share")
    first = await _share(client, token_a, nb["id"], group_id=str(group.id), permission="write")
    assert first.status_code == 201, first.text
    share_id = first.json()["id"]

    # 같은 그룹에 read → 같은 행이 read로 내려간다
    second = await _share(client, token_a, nb["id"], group_id=str(group.id), permission="read")
    assert second.status_code == 201, second.text
    assert (second.json()["id"], second.json()["permission"]) == (share_id, "read")
    assert await rows() == 1
    res = await client.get(f"/api/v1/notebooks/{nb['id']}/shares", headers=bearer(token_a))
    assert [(s["id"], s["permission"]) for s in res.json()] == [(share_id, "read")]

    third = await _share(client, token_a, nb["id"], group_id=str(group.id), permission="write")
    assert third.status_code == 201, third.text
    assert (third.json()["id"], third.json()["permission"]) == (share_id, "write")
    assert await rows() == 1

    res = await client.delete(
        f"/api/v1/notebooks/{nb['id']}/shares/{share_id}", headers=bearer(token_a)
    )
    assert res.status_code == 204
    assert await rows() == 0


# ---- core-v0.20.0-upgrade AC-1.8 ②③ · AC-1.9 ③ ----
# 코어 v0.18.0 #267(PATCH 명시적 null 422)·#221(목록 유효 권한), v0.19.0 #278(visibility
# 소유자 전용)을 제품 조립 앱의 실제 라우터로 고정한다. 웹 선택지 규칙
# (`is_owner || permission === "write"`)의 입력은 아래 목록 API의 실제 (`is_owner`,
# `permission`) 쌍이다(K11).


async def test_write_sharer_cannot_change_visibility(
    client: httpx.AsyncClient, db, user_a, user_b
):
    """쓰기 공유자가 `visibility` 값을 바꾸면 403 `Only the owner can change visibility`이고
    같은 요청의 다른 필드도 적용되지 않는다. 같은 값은 200, 소유자는 바꿀 수 있다
    (코어 #278, AC-1.8 ②)."""
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "Visibility Owner Only")
    group = await share_group(db, user_a, user_b)
    res = await _share(client, token_a, nb["id"], group_id=str(group.id), permission="write")
    assert res.status_code == 201, res.text

    token_b = await login(client, user_b.email)
    res = await client.patch(
        f"/api/v1/notebooks/{nb['id']}",
        json={"visibility": "public", "title": "Widened"},
        headers=bearer(token_b),
    )
    assert (res.status_code, res.json()["detail"]) == (403, "Only the owner can change visibility")
    res = await client.get(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_b))
    assert (res.json()["title"], res.json()["visibility"]) == ("Visibility Owner Only", "private")

    res = await client.patch(
        f"/api/v1/notebooks/{nb['id']}",
        json={"visibility": "private", "title": "Same Visibility"},
        headers=bearer(token_b),
    )
    assert res.status_code == 200, res.text
    assert (res.json()["title"], res.json()["visibility"]) == ("Same Visibility", "private")

    token_a = await login(client, user_a.email)
    res = await client.patch(
        f"/api/v1/notebooks/{nb['id']}", json={"visibility": "public"}, headers=bearer(token_a)
    )
    assert res.status_code == 200, res.text
    assert res.json()["visibility"] == "public"


async def test_notebook_patch_explicit_null_is_422(client: httpx.AsyncClient, user_a):
    """명시적 null은 422(문자열 detail)이고 아무것도 쓰지 않는다 — 이전에는 500
    (코어 #267, AC-1.8 ②)."""
    token_a = await login(client, user_a.email)
    nb = await _create_notebook(client, token_a, "Null Patch")
    for field in ("title", "description"):
        res = await client.patch(
            f"/api/v1/notebooks/{nb['id']}", json={field: None}, headers=bearer(token_a)
        )
        assert res.status_code == 422, res.text
        assert res.json() == {"detail": f"null로 바꿀 수 없는 필드: {field}"}
    res = await client.get(f"/api/v1/notebooks/{nb['id']}", headers=bearer(token_a))
    assert (res.json()["title"], res.json()["description"]) == ("Null Patch", "")


# 관계 → (is_owner, permission) 기대값. admin은 남의 노트북도 owner로 승격되고(#221) 실제 소유만
# is_owner다. 일반 사용자는 공유 값 그대로다(public 열람은 read).
LIST_PAIRS_EXPECTED = {
    "admin": {
        "group_write": (False, "owner"),
        "group_read": (False, "owner"),
        "public": (False, "owner"),
        "own": (True, "owner"),
    },
    "user": {
        "group_write": (False, "write"),
        "group_read": (False, "read"),
        "public": (False, "read"),
        "own": (True, "owner"),
    },
}


@pytest.mark.parametrize("viewer_kind", ["admin", "user"])
async def test_list_permission_pairs_by_relation(
    client: httpx.AsyncClient, db, user_a, user_b, admin_user, viewer_kind, record_property
):
    """목록 `GET /notebooks?filter=all`의 실제 (`is_owner`, `permission`) 쌍 — 네 관계(AC-1.9 ③).

    소유자(user_a)의 노트북을 보는 사람(admin 또는 일반 사용자 user_b)에게 그룹 write 공유·
    그룹 read 공유·public(공유 없음)으로 두고, 보는 사람 자신의 노트북을 하나 둔다(사용자 대상
    공유는 코어 v0.28.0 #278 3단계에서 없앴다).
    이 쌍이 웹 선택지 Vitest의 입력이다(K11 — 가상 응답으로 보존을 입증하지 않는다). 남의
    private(공유 없음)은 목록에 없다.
    """
    from biznotes_core.models import Group, UserGroup

    viewer = admin_user if viewer_kind == "admin" else user_b
    group = Group(name=f"pairs-{viewer_kind}-{viewer.id.hex[:8]}")  # 소유자·보는 사람
    db.add(group)
    await db.flush()
    db.add_all(
        [
            UserGroup(user_id=user_a.id, group_id=group.id, role="member"),
            UserGroup(user_id=viewer.id, group_id=group.id, role="member"),
        ]
    )
    await db.commit()
    group_id, viewer_email = str(group.id), viewer.email

    token_a = await login(client, user_a.email)
    ids: dict[str, str] = {}
    for relation in ("group_write", "group_read", "public", "private_other"):
        ids[relation] = (await _create_notebook(client, token_a, f"pairs-{relation}"))["id"]
    shares = {
        "group_write": {"group_id": group_id, "permission": "write"},
        "group_read": {"group_id": group_id, "permission": "read"},
    }
    for relation, body in shares.items():
        res = await _share(client, token_a, ids[relation], **body)
        assert res.status_code == 201, res.text
    res = await client.patch(
        f"/api/v1/notebooks/{ids['public']}", json={"visibility": "public"}, headers=bearer(token_a)
    )
    assert res.status_code == 200, res.text

    token_v = await login(client, viewer_email)
    ids["own"] = (await _create_notebook(client, token_v, "pairs-own"))["id"]
    res = await client.get("/api/v1/notebooks?filter=all", headers=bearer(token_v))
    assert res.status_code == 200
    listed = {n["id"]: (n["is_owner"], n["permission"]) for n in res.json()}
    pairs = {relation: listed.get(ids[relation]) for relation in LIST_PAIRS_EXPECTED[viewer_kind]}
    record_property(f"list_pairs_{viewer_kind}", json.dumps(pairs, ensure_ascii=False))

    assert pairs == LIST_PAIRS_EXPECTED[viewer_kind]
    assert ids["private_other"] not in listed
