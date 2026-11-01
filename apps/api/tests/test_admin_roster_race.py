"""관리자 명부 잠금의 실제 경합 — 코어 v0.18.0 #241·v0.19.0 #276(core-v0.20.0-upgrade AC-1.8 ⑩ⓑ,
1차 리뷰 PR-09, BL-213).

격리 PG(conftest 워커 DB)에 활성 관리자 A·B 둘만 둔다. 테스트 세션이 코어 공개 함수
`lock_admin_roster`로 명부 잠금을 먼저 쥔 채 A의 「B 변경」과 B의 「A 변경」 두 HTTP 요청을 동시에
보낸다. 둘 다 인증을 마치고 잠금에서 기다리는 것을 `pg_locks`(이 DB의 `advisory`·`granted=false`
2건)로 확인한 뒤, 테스트 세션을 롤백해 잠금을 놓는다. 결과는 한쪽만 성공하고, 진 쪽은 잠금 뒤
요청자 재확인에서 403 `Admin role required`다. 활성 관리자는 정확히 1명이고, 진 요청의 `users`
변경·감사는 0이다.

대체는 없다 — 제품 조립 앱(`app.main.app`)의 코어 관리자 사용자 라우터·명부 잠금·DB를 그대로 쓴다.
제품에 같은 쓰기 경로가 없다는 것은 K6·G9 재실행이 따로 보인다(코어 경합 보장과 별개의 증거).
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable

import httpx
from biznotes_core.core.db import async_session_factory
from biznotes_core.models import AuditLog, User
from biznotes_core.services.admin_roster import lock_admin_roster
from sqlalchemy import func, select, text

from tests.conftest import _create_user, bearer, login

ADMIN_REQUIRED = {"detail": "Admin role required"}
WAITING_ADVISORY_LOCKS = text(
    "SELECT count(*) FROM pg_locks"
    " WHERE locktype = 'advisory' AND NOT granted"
    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
)

Send = Callable[[str, uuid.UUID], Awaitable[httpx.Response]]


async def _race(
    requests: list[tuple[str, uuid.UUID]], send: Send
) -> tuple[list[httpx.Response], int]:
    """명부 잠금을 쥔 채 요청들을 동시에 보내고, 모두 잠금에서 기다릴 때 잠금을 놓는다.

    응답 목록과 잠금을 놓기 직전에 본 대기 잠금 수를 돌려준다. 대기가 모이기 전에 끝난 요청이 있으면
    잠금 밖에서 판정된 것이라 경합이 아니므로 실패한다.
    """
    async with async_session_factory() as holder:
        await lock_admin_roster(holder)
        tasks = [asyncio.create_task(send(token, target)) for token, target in requests]
        deadline = time.monotonic() + 15
        waiting = 0
        while waiting < len(requests):
            assert not any(task.done() for task in tasks), "잠금을 기다리기 전에 끝난 요청이 있다"
            assert time.monotonic() < deadline, f"대기 잠금이 모이지 않았다({waiting}건)"
            await asyncio.sleep(0.05)
            waiting = int(await holder.scalar(WAITING_ADVISORY_LOCKS) or 0)
        await holder.rollback()  # 트랜잭션 잠금(pg_advisory_xact_lock)을 놓는다
    return list(await asyncio.gather(*tasks)), waiting


async def _active_admins() -> list[uuid.UUID]:
    async with async_session_factory() as s:
        return list(
            await s.scalars(select(User.id).where(User.role == "admin", User.is_active.is_(True)))
        )


async def _audits(action: str, *emails: str) -> dict[str, int]:
    async with async_session_factory() as s:
        rows = await s.execute(
            select(AuditLog.resource, func.count())
            .where(AuditLog.action == action, AuditLog.resource.in_(emails))
            .group_by(AuditLog.resource)
        )
        return {resource: int(count) for resource, count in rows.all()}


async def _user_state(user_id: uuid.UUID) -> tuple[str, bool, str] | None:
    async with async_session_factory() as s:
        row = (
            await s.execute(select(User.role, User.is_active, User.name).where(User.id == user_id))
        ).first()
        return None if row is None else (row[0], row[1], row[2])


async def test_two_admins_deactivating_each_other_leaves_exactly_one(
    client: httpx.AsyncClient, db, admin_user
):
    """서로를 동시에 비활성화 — 한쪽만 200, 다른 쪽 403, 활성 관리자 1명, 진 요청의 변경·감사 0."""
    admin_b = await _create_user(db, "admin-b", role="admin")
    a = (admin_user.id, admin_user.email)
    b = (admin_b.id, admin_b.email)
    token_a = await login(client, a[1])
    token_b = await login(client, b[1])
    before = {a[0]: await _user_state(a[0]), b[0]: await _user_state(b[0])}
    assert await _active_admins() and set(await _active_admins()) == {a[0], b[0]}

    async def deactivate(token: str, target: uuid.UUID) -> httpx.Response:
        return await client.patch(
            f"/api/v1/admin/users/{target}", json={"is_active": False}, headers=bearer(token)
        )

    (res_a, res_b), waiting = await _race([(token_a, b[0]), (token_b, a[0])], deactivate)

    assert waiting == 2
    assert sorted([res_a.status_code, res_b.status_code]) == [200, 403]
    winner, loser = (a, b) if res_a.status_code == 200 else (b, a)
    lost = res_b if res_a.status_code == 200 else res_a
    assert lost.json() == ADMIN_REQUIRED
    assert await _active_admins() == [winner[0]]
    # 이긴 요청만 반영 — 진 쪽(=이긴 관리자를 겨눈 요청)은 이긴 관리자의 행을 바꾸지 못했다
    assert await _user_state(winner[0]) == before[winner[0]]
    assert await _user_state(loser[0]) == ("admin", False, before[loser[0]][2])
    assert await _audits("user.update", a[1], b[1]) == {loser[1]: 1}


async def test_two_admins_deleting_each_other_leaves_exactly_one(
    client: httpx.AsyncClient, db, admin_user, deleted_objects
):
    """서로를 동시에 삭제 — 한쪽만 204, 다른 쪽 403, 활성 관리자 1명, 진 요청의 삭제·감사 0."""
    admin_b = await _create_user(db, "admin-b", role="admin")
    a = (admin_user.id, admin_user.email)
    b = (admin_b.id, admin_b.email)
    token_a = await login(client, a[1])
    token_b = await login(client, b[1])

    async def delete(token: str, target: uuid.UUID) -> httpx.Response:
        return await client.delete(f"/api/v1/admin/users/{target}", headers=bearer(token))

    (res_a, res_b), waiting = await _race([(token_a, b[0]), (token_b, a[0])], delete)

    assert waiting == 2
    assert sorted([res_a.status_code, res_b.status_code]) == [204, 403]
    winner, loser = (a, b) if res_a.status_code == 204 else (b, a)
    lost = res_b if res_a.status_code == 204 else res_a
    assert lost.json() == ADMIN_REQUIRED
    assert await _active_admins() == [winner[0]]
    assert await _user_state(winner[0]) == ("admin", True, "admin" if winner == a else "admin-b")
    assert await _user_state(loser[0]) is None
    assert await _audits("user.delete", a[1], b[1]) == {loser[1]: 1}


async def test_non_admin_cannot_change_roster(client: httpx.AsyncClient, db, admin_user, user_a):
    """비인가 관점(§3.4-5): 일반 사용자의 관리자 비활성화·삭제는 명부 잠금 전에 403이다
    (기존 유지)."""
    token = await login(client, user_a.email)
    for res in (
        await client.patch(
            f"/api/v1/admin/users/{admin_user.id}", json={"is_active": False}, headers=bearer(token)
        ),
        await client.delete(f"/api/v1/admin/users/{admin_user.id}", headers=bearer(token)),
    ):
        assert res.status_code == 403, res.text
    assert await _active_admins() == [admin_user.id]
    assert await _audits("user.update", admin_user.email) == {}
    assert await _audits("user.delete", admin_user.email) == {}
