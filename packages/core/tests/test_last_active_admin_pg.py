# 신규(#241). 필요: PostgreSQL·Valkey(PG 테스트). #276에서 요청자 재확인을 더했다.
"""두 관리자가 서로를 동시에 비활성화·강등·삭제해도 활성 관리자가 남는다 — 실제 라우터·인증·DB.

두 요청은 인증을 통과한 뒤 명부 잠금 **직전**에 Barrier로 만난다. 잠금이 없으면 두 요청이
서로의 미커밋 변경을 보지 못해 둘 다 성공하고 활성 관리자가 0명이 된다(서로 삭제는 잠금이 없으면
감사 로그 FK 때문에 교착한다 — origin/main과 같다). 어느 경합에서도 교착·서버 오류가 없어야 한다.

진 요청은 잠금을 기다리는 사이 요청자 권한을 잃었으므로 403이다(#276). 순서를 고정한 경합은
B가 인증을 마친 뒤 잠금 직전에 멈춘 사이 A가 B를 바꾸고 커밋한다.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import func, select

from biznotes_core.api.deps import require_admin
from biznotes_core.api.v1 import auth
from biznotes_core.api.v1.admin import users
from biznotes_core.core.db import get_session_factory
from biznotes_core.models import User
from biznotes_core.services.admin_roster import still_active_admin

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

    async def call(self, user: User, method: str, path: str, body: Any = None) -> int:
        headers = {"Authorization": f"Bearer {self.tokens[user.id]}"}
        res = await self.client.request(method, f"/api/v1{path}", json=body, headers=headers)
        return res.status_code

    async def race(self, *calls: tuple[User, str, str, Any]) -> list[int]:
        """동시에 보낸다. ASGITransport는 앱 예외(교착 등)를 응답이 아니라 예외로 올린다."""
        async with asyncio.timeout(30):
            results = await asyncio.gather(
                *(self.call(*c) for c in calls), return_exceptions=True
            )
        errors = [r for r in results if isinstance(r, BaseException)]
        assert not errors, errors
        return [r for r in results if isinstance(r, int)]


@pytest.fixture
async def api(local_storage: Any) -> AsyncIterator[_Api]:
    # 사용자 삭제 뒤의 오브젝트 정리는 경계 밖이다 — 빈 임시 로컬 스토리지에 보낸다
    app = FastAPI()
    admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
    admin.include_router(users.router)
    for router in (auth.router, admin):
        app.include_router(router, prefix="/api/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield _Api(client)


async def _active_admins() -> int:
    async with get_session_factory()() as s:
        count = await s.scalar(
            select(func.count())
            .select_from(User)
            .where(User.role == "admin", User.is_active.is_(True))
        )
        return int(count or 0)


async def _promote(user: User) -> None:
    async with get_session_factory()() as s:
        row = await s.get(User, user.id)
        assert row is not None
        row.role = "admin"
        await s.commit()


def _gate_lock(monkeypatch: pytest.MonkeyPatch, parties: int = 2) -> None:
    """두 요청이 모두 잠금 직전에 도착한 뒤에야 잠그게 하고, 집계 뒤 경합 창을 넓힌다.

    잠금이 있으면 뒤 요청은 앞 요청이 커밋할 때까지 집계하지 못하므로 지연이 결과를 바꾸지
    않는다. 잠금이 없으면 두 요청이 모두 상대를 활성으로 센 뒤 진행한다(음성 대조가 타이밍에
    좌우되지 않는다).
    """
    barrier = asyncio.Barrier(parties)
    real_lock, real_count = users.lock_admin_roster, users.count_other_active_admins

    async def gated(db: Any) -> None:
        await barrier.wait()
        await real_lock(db)

    async def slow_count(db: Any, exclude_id: uuid.UUID) -> int:
        count = await real_count(db, exclude_id)
        await asyncio.sleep(0.3)
        return count

    monkeypatch.setattr(users, "lock_admin_roster", gated)
    monkeypatch.setattr(users, "count_other_active_admins", slow_count)


DEACTIVATE = ("PATCH", {"is_active": False})
DEMOTE = ("PATCH", {"role": "user"})
DELETE = ("DELETE", None)


@pytest.mark.parametrize(
    ("a_does", "b_does"),
    [
        (DEACTIVATE, DEACTIVATE),
        (DEMOTE, DEMOTE),
        (DELETE, DELETE),
        (DEACTIVATE, DELETE),
        (DEMOTE, DELETE),
    ],
    ids=["서로-비활성화", "서로-강등", "서로-삭제", "비활성화×삭제", "강등×삭제"],
)
async def test_two_admins_cannot_remove_each_other_at_once(
    api: _Api,
    monkeypatch: pytest.MonkeyPatch,
    admin_user: User,
    user_a: User,
    a_does: tuple[str, Any],
    b_does: tuple[str, Any],
) -> None:
    await _promote(user_a)
    a, b = admin_user, user_a
    await api.login(a)  # 로그인은 경합 전에 — 두 요청 모두 활성 관리자로 인증된다
    await api.login(b)
    assert await _active_admins() == 2
    _gate_lock(monkeypatch)

    statuses = await api.race(
        (a, a_does[0], f"/admin/users/{b.id}", a_does[1]),
        (b, b_does[0], f"/admin/users/{a.id}", b_does[1]),
    )

    assert sorted(s // 100 for s in statuses) == [2, 4]
    # 진 쪽은 이미 바뀐 요청자다 — 잠근 뒤 재확인에서 거부된다(#276)
    assert 403 in statuses
    assert await _active_admins() == 1


async def test_change_with_a_third_admin_left_succeeds_for_both(
    api: _Api, monkeypatch: pytest.MonkeyPatch, admin_user: User, user_a: User, user_b: User
) -> None:
    for u in (user_a, user_b):
        await _promote(u)
    for u in (admin_user, user_a):
        await api.login(u)
    _gate_lock(monkeypatch)

    statuses = await api.race(
        (admin_user, "PATCH", f"/admin/users/{user_b.id}", {"role": "user"}),
        (user_a, "PATCH", f"/admin/users/{user_b.id}", {"is_active": False}),
    )

    # 같은 대상을 두 요청이 차례로 바꾼다 — 요청자 둘이 남는다
    assert statuses == [200, 200]
    assert await _active_admins() == 2


async def test_three_admins_deactivating_in_a_cycle_keep_one(
    api: _Api, monkeypatch: pytest.MonkeyPatch, admin_user: User, user_a: User, user_b: User
) -> None:
    """A→B, B→C, C→A를 동시에 — 직렬화되어, 먼저 비활성화된 관리자의 요청만 403이다(#276)."""
    for u in (user_a, user_b):
        await _promote(u)
    ring = [admin_user, user_a, user_b]
    for u in ring:
        await api.login(u)
    _gate_lock(monkeypatch, parties=3)

    statuses = await api.race(
        *((u, "PATCH", f"/admin/users/{ring[(i + 1) % 3].id}", {"is_active": False})
          for i, u in enumerate(ring))
    )

    assert sorted(statuses) == [200, 200, 403]
    assert await _active_admins() == 1


@pytest.mark.parametrize("b_does", [DEACTIVATE, DELETE], ids=["비활성화", "삭제"])
async def test_password_reset_crossing_a_roster_change_does_not_deadlock(
    api: _Api,
    monkeypatch: pytest.MonkeyPatch,
    admin_user: User,
    user_a: User,
    b_does: tuple[str, Any],
) -> None:
    """A의 B 비밀번호 재설정(B 행 FOR UPDATE)과 B의 A 변경이 감사 기록 직전에 엇갈린다.

    두 감사 INSERT가 각 요청자 행에 FK `KEY SHARE`를 건다. 명부 변경이 대상(A) 행에 `FOR UPDATE`를
    걸면 서로를 기다려 교착한다.
    """
    await _promote(user_a)
    a, b = admin_user, user_a
    await api.login(a)
    await api.login(b)
    barrier = asyncio.Barrier(2)
    real_record = users.record

    async def gated_record(*args: Any, **kwargs: Any) -> Any:
        await barrier.wait()
        return await real_record(*args, **kwargs)

    monkeypatch.setattr(users, "record", gated_record)

    statuses = await api.race(
        (a, "POST", f"/admin/users/{b.id}/password-reset", None),
        (b, b_does[0], f"/admin/users/{a.id}", b_does[1]),
    )

    assert sorted(statuses) == ([200, 200] if b_does is DEACTIVATE else [200, 204])
    assert await _active_admins() == 1


async def _state(user: User) -> tuple[str, bool] | None:
    async with get_session_factory()() as s:
        row = await s.get(User, user.id)
        return None if row is None else (row.role, row.is_active)


def _hold_first_lock(monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, asyncio.Event]:
    """첫 잠금 요청을 잠그기 직전에 세운다. `arrived`면 그 요청은 인증을 마쳤다.

    `release`를 켜면 잠금을 요청한다 — 그 사이 다른 요청이 잠그고 커밋한다(순서 고정).
    """
    arrived, release = asyncio.Event(), asyncio.Event()
    real_lock = users.lock_admin_roster
    held = False

    async def gated(db: Any) -> None:
        nonlocal held
        if not held:
            held = True
            arrived.set()
            await release.wait()
        await real_lock(db)

    monkeypatch.setattr(users, "lock_admin_roster", gated)
    return arrived, release


async def _after(api: _Api, monkeypatch: pytest.MonkeyPatch, b_call: tuple, a_call: tuple):
    """B 요청을 잠금 직전에 세우고, A 요청을 끝까지 처리한 뒤 B를 보낸다."""
    arrived, release = _hold_first_lock(monkeypatch)
    async with asyncio.timeout(30):
        b_task = asyncio.create_task(api.call(*b_call))
        await arrived.wait()
        a_status = await api.call(*a_call)
        release.set()
        b_status = await b_task
    return a_status, b_status


@pytest.mark.parametrize(
    ("a_does", "b_body", "b_after"),
    [
        ({"role": "user"}, {"role": "admin"}, ("user", True)),
        ({"is_active": False}, {"is_active": True}, ("admin", False)),
    ],
    ids=["강등-뒤-자기-재승격", "비활성화-뒤-자기-재활성화"],
)
async def test_demoted_admin_cannot_undo_it_with_an_in_flight_self_update(
    api: _Api,
    monkeypatch: pytest.MonkeyPatch,
    admin_user: User,
    user_a: User,
    a_does: dict[str, Any],
    b_body: dict[str, Any],
    b_after: tuple[str, bool],
) -> None:
    await _promote(user_a)
    a, b = admin_user, user_a
    await api.login(a)
    await api.login(b)

    statuses = await _after(
        api,
        monkeypatch,
        (b, "PATCH", f"/admin/users/{b.id}", b_body),
        (a, "PATCH", f"/admin/users/{b.id}", a_does),
    )

    assert statuses == (200, 403)
    assert await _state(b) == b_after


@pytest.mark.parametrize(
    "b_does",
    [DEACTIVATE, ("PATCH", {"role": "admin"}), DELETE],
    ids=["C-비활성화", "C-승격", "C-삭제"],
)
@pytest.mark.parametrize(
    ("a_does", "a_status"),
    [(DEMOTE, 200), (DEACTIVATE, 200), (DELETE, 204)],
    ids=["강등", "비활성화", "삭제"],
)
async def test_demoted_admin_cannot_change_another_user_in_flight(
    api: _Api,
    monkeypatch: pytest.MonkeyPatch,
    admin_user: User,
    user_a: User,
    user_b: User,
    a_does: tuple[str, Any],
    a_status: int,
    b_does: tuple[str, Any],
) -> None:
    """A가 B를 바꾸는 사이 B가 일반 사용자 C를 바꾸려 한다(공범 승격 포함) — C는 그대로다.

    B는 A가 시작하기 전에 인증·`require_admin`을 통과해 잠금 앞에서 멈춰 있다(`_after`). 그래서
    B의 403은 `require_admin`이 아니라 잠근 뒤 재확인에서 나온다.
    """
    await _promote(user_a)
    a, b, c = admin_user, user_a, user_b
    await api.login(a)
    await api.login(b)

    statuses = await _after(
        api,
        monkeypatch,
        (b, b_does[0], f"/admin/users/{c.id}", b_does[1]),
        (a, a_does[0], f"/admin/users/{b.id}", a_does[1]),
    )

    assert statuses == (a_status, 403)
    assert await _state(c) == ("user", True)


async def test_still_active_admin_reads_past_a_stale_entity(
    admin_user: User, user_a: User, user_b: User
) -> None:
    """공개 함수 계약(#276) — 같은 세션이 낡은 엔티티를 쥐고 있어도 다른 세션의 커밋을 본다."""
    await _promote(user_a)
    await _promote(user_b)
    async with get_session_factory()() as stale:
        # identity map에 올리고 강한 참조로 붙잡는다(인증 때 읽은 요청자 객체처럼) — identity map은
        # 약한 참조라, 붙잡지 않으면 다음 get이 새로 읽어 낡은 객체 시나리오가 되지 않는다
        held = [await stale.get(User, u.id) for u in (admin_user, user_a, user_b)]
        assert all(h is not None for h in held)
        async with get_session_factory()() as other:
            demoted = await other.get(User, user_a.id)
            assert demoted is not None
            demoted.role = "user"
            dormant = await other.get(User, user_b.id)
            assert dormant is not None
            dormant.is_active = False  # role은 admin이지만 비활성
            await other.commit()
        assert await still_active_admin(stale, admin_user.id) is True
        assert await still_active_admin(stale, user_a.id) is False
        assert await still_active_admin(stale, user_b.id) is False
        assert await still_active_admin(stale, uuid.uuid4()) is False
        # 낡은 엔티티는 그대로다 — 함수가 identity map을 거치지도, 갱신하지도 않았다
        assert held[1] is not None and held[1].role == "admin"
        assert await stale.get(User, user_a.id) is held[1]
