"""마지막 활성 관리자를 잃는 비활성화·강등·삭제를 422로 거부한다 (#241). 잠근 뒤 요청자가 권한을
잃었으면 403이다 (#276).

`test_admin_user_self_protection.py`의 하네스를 쓴다 — 인증과 DB 세션만 대역이고 판정·응답은
실경로다. 대역 세션의 집계·요청자 재확인은 기록된 행에서 답한다. 요청자 행을 「DB에서는 이미
강등됨」으로 두면, 처리 중에 다른 관리자가 요청자를 바꾼 경합 상황이 된다. 그 요청은 재확인에서
403이다. 마지막 관리자 422는 재확인을 통과한 뒤의 안전망이라(요청자가 남으므로 이 순서에서는
나오지 않는다), 재확인을 대역으로 통과시켜 판정만 확인한다(`recheck_passes`).
실제 경합은 `tests/test_last_active_admin_pg.py`가 확인한다.
"""

import uuid
from typing import Any

import httpx
import pytest
from conftest import make_settings
from fastapi import APIRouter, Depends, FastAPI
from test_admin_user_self_protection import _RecordingDb, _user

from biznotes_core.api import deps
from biznotes_core.api.v1.admin import users
from biznotes_core.core.db import get_db
from biznotes_core.models import User
from biznotes_core.services import admin_roster

LAST_UPDATE = {"detail": "마지막 활성 관리자는 비활성화하거나 강등할 수 없습니다"}
LAST_DELETE = {"detail": "마지막 활성 관리자는 삭제할 수 없습니다"}
ADMIN_REQUIRED = {"detail": "Admin role required"}


@pytest.fixture
def recheck_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """요청자 재확인(#276)을 통과시킨다 — 그 뒤의 마지막 관리자 판정만 본다."""

    async def _passes(db: Any, user_id: uuid.UUID) -> bool:
        return True

    monkeypatch.setattr(users, "still_active_admin", _passes)


async def _send(me: User, db: _RecordingDb, method: str, user_id: uuid.UUID, body: Any = None):
    admin = APIRouter(prefix="/admin", dependencies=[Depends(deps.require_admin)])
    admin.include_router(users.router)
    app = FastAPI()
    app.include_router(admin)
    app.dependency_overrides[deps.get_current_user] = lambda: me
    app.dependency_overrides[get_db] = lambda: db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.request(method, f"/admin/users/{user_id}", json=body)


def _stale_me() -> tuple[User, User]:
    """인증된 요청자(활성 관리자)와, 그 사이 다른 관리자가 강등해 DB에 남은 같은 행."""
    me = _user()
    return me, _user(id=me.id, email=me.email, role="user")


@pytest.mark.parametrize(
    "body",
    [{"is_active": False}, {"role": "user"}, {"is_active": False, "role": "user", "name": "x"}],
    ids=["비활성화", "강등", "둘다+이름"],
)
async def test_update_that_leaves_no_active_admin_is_422(
    body: dict[str, Any], recheck_passes: None
) -> None:
    me, me_in_db = _stale_me()
    target = _user(email="t@test.dev", name="대상")
    db = _RecordingDb(me_in_db, target)

    resp = await _send(me, db, "PATCH", target.id, body)

    assert (resp.status_code, resp.json()) == (422, LAST_UPDATE)
    # 잠근 뒤 대상을 읽고 센 다음 거부한다 — 변경·감사·커밋이 없다
    assert db.calls == ["lock", "get", "count"]
    assert (target.name, target.role, target.is_active) == ("대상", "admin", True)


async def test_delete_that_leaves_no_active_admin_is_422(recheck_passes: None) -> None:
    me, me_in_db = _stale_me()
    target = _user(email="t@test.dev")
    db = _RecordingDb(me_in_db, target)

    resp = await _send(me, db, "DELETE", target.id)

    assert (resp.status_code, resp.json()) == (422, LAST_DELETE)
    assert db.calls == ["lock", "get", "count"]


async def test_inactive_admins_are_not_counted(recheck_passes: None) -> None:
    me, me_in_db = _stale_me()
    target = _user(email="t@test.dev")
    dormant = _user(email="d@test.dev", is_active=False)  # 로그인할 수 없는 관리자
    db = _RecordingDb(me_in_db, target, dormant)

    resp = await _send(me, db, "PATCH", target.id, {"is_active": False})

    assert (resp.status_code, resp.json()) == (422, LAST_UPDATE)


async def test_change_is_applied_when_another_active_admin_remains() -> None:
    me = _user()
    target = _user(email="t@test.dev")
    other = _user(email="o@test.dev")
    db = _RecordingDb(me, target, other)

    resp = await _send(me, db, "PATCH", target.id, {"role": "user"})

    assert resp.status_code == 200
    assert target.role == "user"
    assert db.calls == [
        "lock", "requester", "get", "count", "add:AuditLog", "commit", "refresh"
    ]


@pytest.mark.parametrize(
    ("target_kw", "body"),
    [
        ({"role": "user"}, {"is_active": False}),  # 일반 사용자 비활성화
        ({"is_active": False}, {"role": "user"}),  # 비활성 관리자 강등
        ({"role": "user"}, {"role": "admin"}),  # 승격
        ({"is_active": False}, {"is_active": True}),  # 관리자 재활성화
        ({}, {"is_active": True, "role": "admin"}),  # 그대로 유지
    ],
    ids=["사용자-비활성화", "비활성-관리자-강등", "승격", "재활성화", "유지"],
)
async def test_changes_that_cannot_lose_an_active_admin_lock_but_do_not_count(
    target_kw: dict[str, Any], body: dict[str, Any], recheck_passes: None
) -> None:
    me, me_in_db = _stale_me()  # 남는 관리자가 0명이어도 판정 대상이 아니다
    target = _user(email="t@test.dev", **target_kw)
    db = _RecordingDb(me_in_db, target)

    resp = await _send(me, db, "PATCH", target.id, body)

    assert resp.status_code == 200
    assert db.calls == ["lock", "get", "add:AuditLog", "commit", "refresh"]


async def test_name_only_update_does_not_lock() -> None:
    me, me_in_db = _stale_me()
    target = _user(email="t@test.dev")
    db = _RecordingDb(me_in_db, target)

    resp = await _send(me, db, "PATCH", target.id, {"name": "새 이름"})

    # 이름만 바꾸면 잠그지 않고 요청자도 다시 확인하지 않는다(#276 범위 밖)
    assert resp.status_code == 200
    assert db.calls == ["get", "add:AuditLog", "commit", "refresh"]


@pytest.mark.parametrize(
    ("method", "body", "detail"),
    [
        ("PATCH", {"is_active": False}, "자기 계정은 비활성화할 수 없습니다"),
        ("PATCH", {"role": None}, "자기 자신의 관리자 권한은 해제할 수 없습니다"),
        ("DELETE", None, "자기 자신은 삭제할 수 없습니다"),
    ],
    ids=["자기-비활성화", "자기-역할-null", "자기-삭제"],
)
async def test_self_protection_still_comes_before_the_lock(
    method: str, body: Any, detail: str
) -> None:
    me = _user()
    db = _RecordingDb(me)

    resp = await _send(me, db, method, me.id, body)

    assert (resp.status_code, resp.json()) == (422, {"detail": detail})
    assert db.calls == []


async def test_null_rejection_still_comes_before_the_lock() -> None:
    me = _user()
    target = _user(email="t@test.dev")
    db = _RecordingDb(me, target)

    resp = await _send(me, db, "PATCH", target.id, {"is_active": None})

    assert resp.status_code == 422
    assert resp.json() == {"detail": "null로 바꿀 수 없는 필드: is_active"}
    assert db.calls == []


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
async def test_missing_target_is_404_after_the_lock(method: str) -> None:
    me = _user()
    db = _RecordingDb(me)
    body = {"role": "user"} if method == "PATCH" else None

    resp = await _send(me, db, method, uuid.uuid4(), body)

    assert resp.status_code == 404
    assert db.calls == ["lock", "requester", "get"]


@pytest.mark.parametrize(
    "in_db",
    [{"role": "user"}, {"is_active": False}, None],
    ids=["강등됨", "비활성화됨", "삭제됨"],
)
@pytest.mark.parametrize(
    ("method", "body"),
    [
        ("PATCH", {"is_active": False}),
        ("PATCH", {"role": "admin"}),
        ("PATCH", {"is_active": True, "name": "x"}),
        ("DELETE", None),
    ],
    ids=["비활성화", "승격", "활성화+이름", "삭제"],
)
async def test_requester_who_lost_admin_while_waiting_is_403(
    in_db: dict[str, Any] | None, method: str, body: Any
) -> None:
    """잠금을 기다리는 사이 강등·비활성화·삭제된 요청자는 대상과 무관하게 거부된다(#276)."""
    me = _user()
    target = _user(email="t@test.dev", name="대상", role="user")
    rows = [target] if in_db is None else [target, _user(id=me.id, email=me.email, **in_db)]
    db = _RecordingDb(*rows)

    resp = await _send(me, db, method, target.id, body)

    assert (resp.status_code, resp.json()) == (403, ADMIN_REQUIRED)
    # 대상 조회(404 여부)·집계·변경·감사·커밋 전에 거부한다
    assert db.calls == ["lock", "requester"]
    assert (target.name, target.role, target.is_active) == ("대상", "user", True)


@pytest.mark.parametrize(
    ("in_db", "body"),
    [({"role": "user"}, {"role": "admin"}), ({"is_active": False}, {"is_active": True})],
    ids=["자기-재승격", "자기-재활성화"],
)
async def test_requester_cannot_undo_own_demotion_while_waiting(
    in_db: dict[str, Any], body: dict[str, Any]
) -> None:
    """자기 보호는 활성 관리자 유지를 허용하지만, 그 사이 강등·비활성화됐으면 되돌리지 못한다."""
    me = _user()
    me_in_db = _user(id=me.id, email=me.email, **in_db)
    db = _RecordingDb(me_in_db)

    resp = await _send(me, db, "PATCH", me.id, body)

    assert (resp.status_code, resp.json()) == (403, ADMIN_REQUIRED)
    assert db.calls == ["lock", "requester"]
    assert (me_in_db.role, me_in_db.is_active) == (
        in_db.get("role", "admin"),
        in_db.get("is_active", True),
    )


def test_lock_key_is_stable_and_namespaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_roster, "get_settings", lambda: make_settings(service_name="svc-a"))
    key_a, key_a2 = admin_roster._admin_roster_lock_key(), admin_roster._admin_roster_lock_key()
    monkeypatch.setattr(admin_roster, "get_settings", lambda: make_settings(service_name="svc-b"))

    assert key_a == key_a2
    assert admin_roster._admin_roster_lock_key() != key_a  # 제품마다 네임스페이스가 다르다
    assert -(2**63) <= key_a < 2**63
