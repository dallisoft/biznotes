"""관리자가 `PATCH /admin/users/{id}`로 자기 계정을 잠그지 못한다 (#219).

명시적 null을 대상과 무관하게 422로 거부하는 것(#242)도 같은 하네스로 확인한다. 마지막 활성
관리자 보호(#241)의 잠금·요청자 재확인(#276)·집계 호출 순서도 기록한다(판정은
`test_admin_last_active_admin.py`).

제품처럼 코어 라우터를 `require_admin`을 건 `/admin` 라우터에 include한 앱으로 확인한다.
인증(`get_current_user`)과 DB 세션(`get_db`)만 대역이고, 요청 검증·핸들러·응답 직렬화는 실경로다.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from conftest import FakeResult
from fastapi import APIRouter, Depends, FastAPI

from biznotes_core.api import deps
from biznotes_core.api.v1.admin import users
from biznotes_core.core.db import get_db
from biznotes_core.models import User
from biznotes_core.models.audit import AuditLog

DEACTIVATE = "자기 계정은 비활성화할 수 없습니다"
DEMOTE = "자기 자신의 관리자 권한은 해제할 수 없습니다"
NULL = "null로 바꿀 수 없는 필드: {}"
APPLIED = ["get", "add:AuditLog", "commit", "refresh"]  # 정상 변경의 DB 호출 순서
# role·is_active를 담은 변경은 명부 잠금 뒤 요청자(#276)와 대상을 다시 읽는다.
# 대상이 활성 관리자 자리를 잃으면 남는 수를 센다(#241)
LOCKED = ["lock", "requester", *APPLIED]
COUNTED = ["lock", "requester", "get", "count", *APPLIED[1:]]


class _RecordingDb:
    """사용자 행을 돌려주고 DB 호출 순서를 기록한다."""

    def __init__(self, *rows: User) -> None:
        self.rows = {u.id: u for u in rows}
        self.calls: list[str] = []
        self.added: list[Any] = []

    async def get(self, model: type, key: uuid.UUID, **_kw: Any) -> User | None:
        self.calls.append("get")
        return self.rows.get(key) if model is User else None

    async def execute(self, stmt: Any, params: Any = None) -> FakeResult:
        if "pg_advisory_xact_lock" in str(stmt):
            self.calls.append("lock")
            return FakeResult()
        # 잠근 뒤 요청자 다시 확인(#276) — 열만 읽는 질의다. 기록된 행에서 답한다
        assert str(stmt).startswith("SELECT users.role, users.is_active"), str(stmt)
        self.calls.append("requester")
        (user_id,) = stmt.compile().params.values()
        u = self.rows.get(user_id)
        return FakeResult([(u.role, u.is_active)] if u is not None else [])

    async def scalar(self, stmt: Any) -> int:
        """남는 활성 관리자 수 — 기록된 행에서 제외 대상(질의 인자)을 빼고 센다."""
        self.calls.append("count")
        excluded = set(stmt.compile().params.values())
        admins = [u for u in self.rows.values() if u.role == "admin" and u.is_active]
        return sum(1 for u in admins if u.id not in excluded)

    def add(self, obj: Any) -> None:
        self.calls.append(f"add:{type(obj).__name__}")
        self.added.append(obj)

    async def commit(self) -> None:
        self.calls.append("commit")

    async def refresh(self, obj: Any) -> None:
        self.calls.append("refresh")


def _user(**kw: Any) -> User:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "email": "admin@test.dev",
        "name": "관리자",
        "role": "admin",
        "is_active": True,
        "password_hash": "x",
        "must_change_password": False,
        "created_at": datetime(2026, 10, 16, tzinfo=UTC),
    }
    base.update(kw)
    return User(**base)


async def _patch(
    me: User, db: _RecordingDb, user_id: uuid.UUID | str, body: Any, *, content: str | None = None
) -> httpx.Response:
    admin = APIRouter(prefix="/admin", dependencies=[Depends(deps.require_admin)])
    admin.include_router(users.router)
    app = FastAPI()
    app.include_router(admin)
    app.dependency_overrides[deps.get_current_user] = lambda: me
    app.dependency_overrides[get_db] = lambda: db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        if content is not None:  # 중복 키처럼 dict로 만들 수 없는 본문
            headers = {"content-type": "application/json"}
            return await client.patch(f"/admin/users/{user_id}", content=content, headers=headers)
        return await client.patch(f"/admin/users/{user_id}", json=body)


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"is_active": False}, DEACTIVATE),
        ({"role": "user"}, DEMOTE),
        # 명시적 null도 "활성 관리자 유지"가 아니다 — 거부한다(계획 D2)
        ({"is_active": None}, DEACTIVATE),
        ({"role": None}, DEMOTE),
        # 함께 보낸 무관 필드도 적용하지 않는다
        ({"name": "새 이름", "is_active": False}, DEACTIVATE),
        ({"name": "새 이름", "role": "user"}, DEMOTE),
        ({"is_active": False, "role": "user"}, DEACTIVATE),
    ],
    ids=[
        "비활성화",
        "강등",
        "활성-null",
        "역할-null",
        "이름+비활성화",
        "이름+강등",
        "비활성화+강등",
    ],
)
async def test_self_lockout_is_rejected_without_touching_db(
    body: dict[str, Any], detail: str
) -> None:
    me = _user()
    db = _RecordingDb(me)

    resp = await _patch(me, db, me.id, body)

    assert resp.status_code == 422
    # 고정 문구만 싣는다 — 입력값(이름·역할 값)을 되돌리지 않는다
    assert resp.json() == {"detail": detail}
    assert "새 이름" not in resp.text
    # delete_user처럼 DB를 조회·변경·커밋하지 않고 감사 로그도 남기지 않는다
    assert db.calls == []
    assert (me.name, me.role, me.is_active) == ("관리자", "admin", True)


@pytest.mark.parametrize(
    ("path_form", "body"),
    [
        # 경로 UUID는 정규화되어 비교된다 — 표기를 바꿔도 자기 자신이다
        (str.upper, {"is_active": False}),
        # 스키마의 lax bool 변환 뒤 값(False)으로 판정한다
        (str, {"is_active": "false"}),
    ],
    ids=["대문자-UUID-경로", "강제변환-문자열-false"],
)
async def test_self_check_cannot_be_bypassed_by_notation(
    path_form: Callable[[str], str], body: dict[str, Any]
) -> None:
    me = _user()
    db = _RecordingDb(me)

    resp = await _patch(me, db, path_form(str(me.id)), body)

    assert resp.status_code == 422
    assert resp.json() == {"detail": DEACTIVATE}
    assert db.calls == []
    assert me.is_active is True


@pytest.mark.parametrize(
    "body",
    [
        {"name": "새 이름"},
        {"is_active": True},
        {"role": "admin"},
        {"name": "새 이름", "is_active": True, "role": "admin"},
    ],
    ids=["이름", "활성-유지", "관리자-유지", "모두-유지"],
)
async def test_self_update_keeping_active_admin_still_succeeds(body: dict[str, Any]) -> None:
    me = _user()
    db = _RecordingDb(me)

    resp = await _patch(me, db, me.id, body)

    assert resp.status_code == 200
    out = resp.json()
    assert (out["name"], out["role"], out["is_active"]) == (
        body.get("name", "관리자"),
        "admin",
        True,
    )
    assert me.name == out["name"]
    # 자기 자신은 활성 관리자로 남으므로 세지 않는다
    assert db.calls == (LOCKED if {"role", "is_active"} & set(body) else APPLIED)
    [audit] = db.added
    assert isinstance(audit, AuditLog)
    assert (audit.action, audit.user_id, audit.detail) == ("user.update", me.id, body)


@pytest.mark.parametrize(
    ("target_role", "body"),
    [
        ("user", {"is_active": False}),
        ("admin", {"is_active": False}),
        ("admin", {"role": "user"}),
    ],
    ids=["사용자-비활성화", "다른-관리자-비활성화", "다른-관리자-강등"],
)
async def test_other_user_can_still_be_deactivated_or_demoted(
    target_role: str, body: dict[str, Any]
) -> None:
    me = _user()
    other = _user(email="other@test.dev", name="다른 사람", role=target_role)
    db = _RecordingDb(me, other)

    resp = await _patch(me, db, other.id, body)

    assert resp.status_code == 200
    for field, value in body.items():
        assert getattr(other, field) == value
        assert resp.json()[field] == value
    # 다른 활성 관리자(요청자)가 남는다 — 관리자 대상만 센다
    assert db.calls == (COUNTED if target_role == "admin" else LOCKED)
    # 요청자 자신은 그대로다
    assert (me.role, me.is_active) == ("admin", True)


async def test_unknown_user_is_still_404() -> None:
    me = _user()
    db = _RecordingDb(me)

    resp = await _patch(me, db, uuid.uuid4(), {"is_active": False})

    assert resp.status_code == 404
    assert db.calls == ["lock", "requester", "get"]


# ---------- 명시적 null (#242) ----------


@pytest.mark.parametrize(
    ("target", "body", "fields"),
    [
        ("other", {"name": None}, "name"),
        ("other", {"role": None}, "role"),
        ("other", {"is_active": None}, "is_active"),
        # 함께 보낸 정상 필드도 적용하지 않는다. 필드 이름은 정렬해 싣는다
        ("other", {"name": "새 이름", "role": None}, "role"),
        ("other", {"name": None, "is_active": None}, "is_active, name"),
        # 자기 대상이라도 자기 보호(#219)가 보지 않는 필드의 null은 같은 규칙이다
        ("self", {"name": None}, "name"),
        ("self", {"name": None, "is_active": True, "role": "admin"}, "name"),
        # 입력만으로 판정한다 — 대상 조회 전에 거부하므로 없는 사용자도 422다
        ("missing", {"name": None}, "name"),
    ],
    ids=[
        "타인-이름",
        "타인-역할",
        "타인-활성",
        "타인-이름+역할null",
        "타인-둘다null",
        "자기-이름",
        "자기-이름null+유지",
        "없는-사용자",
    ],
)
async def test_explicit_null_is_422_without_touching_db(
    target: str, body: dict[str, Any], fields: str
) -> None:
    me = _user()
    other = _user(email="other@test.dev", name="다른 사람")
    db = _RecordingDb(me, other)
    user_id = {"self": me.id, "other": other.id, "missing": uuid.uuid4()}[target]

    resp = await _patch(me, db, user_id, body)

    assert resp.status_code == 422
    assert resp.json() == {"detail": NULL.format(fields)}
    assert "새 이름" not in resp.text
    assert db.calls == []
    assert (other.name, other.role, other.is_active) == ("다른 사람", "admin", True)
    assert (me.name, me.role, me.is_active) == ("관리자", "admin", True)


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"name": None, "is_active": None}, DEACTIVATE),
        ({"name": None, "role": None}, DEMOTE),
        ({"name": None, "is_active": False}, DEACTIVATE),
    ],
    ids=["활성null+이름null", "역할null+이름null", "비활성화+이름null"],
)
async def test_self_protection_message_comes_before_null_rejection(
    body: dict[str, Any], detail: str
) -> None:
    me = _user()
    db = _RecordingDb(me)

    resp = await _patch(me, db, me.id, body)

    assert (resp.status_code, resp.json()) == (422, {"detail": detail})
    assert db.calls == []


@pytest.mark.parametrize(
    ("raw", "status", "name"),
    [
        ('{"name": "새 이름", "name": null}', 422, "다른 사람"),
        ('{"name": null, "name": "새 이름"}', 200, "새 이름"),
    ],
    ids=["마지막이-null", "마지막이-값"],
)
async def test_duplicate_keys_are_judged_by_the_last_value(
    raw: str, status: int, name: str
) -> None:
    # 판정과 적용이 같은 파싱 결과를 쓴다 — 중복 키로 거부를 비켜 갈 수 없다
    me = _user()
    other = _user(email="other@test.dev", name="다른 사람")
    db = _RecordingDb(me, other)

    resp = await _patch(me, db, other.id, None, content=raw)

    assert resp.status_code == status
    assert other.name == name
    if status == 422:
        assert resp.json() == {"detail": NULL.format("name")}
        assert db.calls == []
    else:
        assert db.calls == APPLIED
