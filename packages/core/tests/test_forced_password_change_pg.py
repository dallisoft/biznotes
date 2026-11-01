# 필요: PostgreSQL·Valkey(PG 테스트).
"""첫 로그인·임시 비밀번호 강제 변경과 본인 변경 — 실제 라우터·DB·Redis (#165).

제품 조립과 같은 모양으로 앱을 만든다. auth 라우터, 일반 라우터(tags), 그리고
`APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])` 아래 admin users다.
인증 의존성은 바꾸지 않는다. `APP_ENV=test`라 레이트 리밋은 꺼져 있다.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import select

from biznotes_core.api.deps import PASSWORD_CHANGE_REQUIRED, require_admin
from biznotes_core.api.v1 import auth, tags
from biznotes_core.api.v1.admin import users as admin_users
from biznotes_core.config import get_settings
from biznotes_core.core.db import async_session_factory
from biznotes_core.core.security import ALGORITHM, verify_password
from biznotes_core.models import AuditLog, User

PASSWORD = "pw-test-1234"  # tests/conftest.py 공용 해시의 평문


def _app() -> FastAPI:
    app = FastAPI()
    admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
    admin.include_router(admin_users.router)
    for router in (auth.router, tags.router, admin):
        app.include_router(router, prefix="/api/v1")
    return app


class _Api:
    """요청마다 쿠키를 비우고 Bearer만 쓴다 — 어떤 토큰으로 호출했는지 분명하게 한다."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def call(
        self, method: str, path: str, token: str | None = None, **kw: object
    ) -> httpx.Response:
        self.client.cookies.clear()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        url = f"/api/v1{path}"
        return await self.client.request(method, url, headers=headers, **kw)  # type: ignore[arg-type]

    async def login(self, email: str, password: str = PASSWORD) -> dict:
        res = await self.call("POST", "/auth/login", json={"email": email, "password": password})
        assert res.status_code == 200, res.text
        return res.json()


@pytest.fixture
async def api() -> AsyncIterator[_Api]:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield _Api(client)


async def _fresh(user_id: uuid.UUID) -> User:
    async with async_session_factory() as s:
        user = await s.get(User, user_id)
        assert user is not None
        return user


def _blocked(res: httpx.Response) -> bool:
    return res.status_code == 403 and res.json()["detail"]["code"] == PASSWORD_CHANGE_REQUIRED


async def test_admin_created_user_must_change_password_before_other_apis(api, admin_user):
    admin = await api.login(admin_user.email)
    created = await api.call(
        "POST",
        "/admin/users",
        admin["access_token"],
        json={
            "email": f"new-{uuid.uuid4().hex[:8]}@test.dev",
            "name": "n",
            "password": "temp-pass-1",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["must_change_password"] is True

    first = await api.login(created.json()["email"], "temp-pass-1")
    access, refresh = first["access_token"], first["refresh_token"]
    assert first["user"]["must_change_password"] is True

    # 허용 목록 밖은 403, 허용 목록(me·refresh)은 동작한다
    assert _blocked(await api.call("GET", "/tags", access))
    me = await api.call("GET", "/auth/me", access)
    assert me.status_code == 200 and me.json()["must_change_password"] is True
    rotated = await api.call("POST", "/auth/refresh", refresh)
    assert rotated.status_code == 200, rotated.text
    access, refresh = rotated.json()["access_token"], rotated.json()["refresh_token"]
    assert _blocked(await api.call("GET", "/tags", access))

    # 현재 비밀번호가 틀리면 400, 해시·플래그·변경 시각 그대로
    user_id = uuid.UUID(created.json()["id"])
    created_at_version = (await _fresh(user_id)).password_changed_at
    assert created_at_version is not None  # 생성도 set_password를 거친다
    wrong = await api.call(
        "POST",
        "/auth/password",
        access,
        json={"current_password": "not-the-password", "new_password": "brand-new-pass"},
    )
    assert wrong.status_code == 400 and wrong.json()["detail"]["code"] == "invalid_current_password"
    unchanged = await _fresh(user_id)
    assert unchanged.must_change_password is True
    assert unchanged.password_changed_at == created_at_version
    assert verify_password("temp-pass-1", unchanged.password_hash)

    changed = await api.call(
        "POST",
        "/auth/password",
        access,
        json={"current_password": "temp-pass-1", "new_password": "brand-new-pass"},
    )
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert body["user"]["must_change_password"] is False

    # 변경 전에 발급된 access·refresh는 401, 응답으로 받은 새 토큰은 통과
    assert (await api.call("GET", "/auth/me", access)).status_code == 401
    assert (await api.call("POST", "/auth/refresh", refresh)).status_code == 401
    assert (await api.call("GET", "/tags", body["access_token"])).status_code == 200
    assert (await api.call("POST", "/auth/refresh", body["refresh_token"])).status_code == 200

    stored = await _fresh(user_id)
    assert stored.must_change_password is False and stored.password_changed_at is not None
    assert verify_password("brand-new-pass", stored.password_hash)
    # 새 비밀번호로 다시 로그인한 토큰도 통과한다(발급 지점 모두 `pwv`를 싣는다)
    relogin = await api.login(stored.email, "brand-new-pass")
    assert (await api.call("GET", "/tags", relogin["access_token"])).status_code == 200
    async with async_session_factory() as s:
        actions = (
            await s.execute(select(AuditLog.action).where(AuditLog.user_id == user_id))
        ).scalars().all()
    assert "user.password_change" in actions


async def test_admin_reset_forces_change_and_ends_existing_sessions(api, admin_user, user_a):
    before = await api.login(user_a.email)
    assert (await api.call("GET", "/tags", before["access_token"])).status_code == 200

    admin = await api.login(admin_user.email)
    path = f"/admin/users/{user_a.id}/password-reset"
    reset = await api.call("POST", path, admin["access_token"])
    assert reset.status_code == 200, reset.text
    temp = reset.json()["temporary_password"]

    # 재설정 전 세션은 끊긴다
    assert (await api.call("GET", "/tags", before["access_token"])).status_code == 401
    assert (await api.call("POST", "/auth/refresh", before["refresh_token"])).status_code == 401
    # 임시 비밀번호로 로그인하면 변경을 요구한다
    after = await api.login(user_a.email, temp)
    assert after["user"]["must_change_password"] is True
    assert (await api.call("GET", "/auth/me", after["access_token"])).status_code == 200
    assert _blocked(await api.call("GET", "/tags", after["access_token"]))
    assert (await api.call("POST", "/auth/logout", after["access_token"])).status_code == 204


async def test_flagged_admin_is_blocked_on_admin_router(api, admin_user):
    token = (await api.login(admin_user.email))["access_token"]
    async with async_session_factory() as s:
        user = await s.get(User, admin_user.id)
        user.must_change_password = True
        await s.commit()

    assert _blocked(await api.call("GET", "/admin/users", token))
    assert (await api.call("GET", "/auth/me", token)).status_code == 200


@pytest.mark.parametrize(
    "new_password,reason",
    [("short-1", "too_short"), (PASSWORD, "same_as_current"), ("가" * 25, "too_long_bytes")],
    ids=["8자 미만", "현재와 같음", "72바이트 초과"],
)
async def test_weak_new_password_is_422_without_echo_and_changes_nothing(
    api, user_a, new_password, reason
):
    token = (await api.login(user_a.email))["access_token"]

    res = await api.call(
        "POST",
        "/auth/password",
        token,
        json={"current_password": PASSWORD, "new_password": new_password},
    )

    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "invalid_new_password"
    assert res.json()["detail"]["reason"] == reason
    # 오류 본문에 입력 비밀번호(현재·새)를 되돌려주지 않는다
    assert new_password not in res.text and PASSWORD not in res.text
    assert (await _fresh(user_a.id)).password_changed_at is None
    assert (await api.call("GET", "/tags", token)).status_code == 200


async def test_admin_create_over_bcrypt_limit_is_422_without_echo(api, admin_user):
    admin = await api.login(admin_user.email)
    password = "가" * 25

    res = await api.call(
        "POST",
        "/admin/users",
        admin["access_token"],
        json={"email": f"long-{uuid.uuid4().hex[:8]}@test.dev", "name": "n", "password": password},
    )

    assert res.status_code == 422, res.text
    assert res.json()["detail"]["reason"] == "too_long_bytes"
    assert password not in res.text


async def test_existing_user_and_pre_upgrade_token_keep_working(api, user_a):
    stored = await _fresh(user_a.id)
    assert stored.must_change_password is False and stored.password_changed_at is None

    now = datetime.now(UTC)
    legacy = jwt.encode(
        {
            "sub": str(user_a.id),
            "type": "access",
            "role": "user",
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "jti": str(uuid.uuid4()),
        },
        get_settings().jwt_secret,
        algorithm=ALGORITHM,
    )

    assert (await api.call("GET", "/tags", legacy)).status_code == 200
    assert (await api.login(user_a.email))["user"]["must_change_password"] is False


async def test_password_change_rechecks_version_under_row_lock(db, user_a):
    """인증 뒤 관리자 재설정이 먼저 커밋되면 잠금 뒤 재확인이 401로 막는다(재설정을 덮지 않는다)."""
    from fastapi import HTTPException, Response

    from biznotes_core.schemas.auth import PasswordChangeRequest
    from biznotes_core.services.user_passwords import set_password

    user_id = user_a.id
    stale = await db.get(User, user_id)  # 인증 시점에 읽은 사용자
    async with async_session_factory() as other:  # 그 사이 관리자 재설정이 커밋된다
        target = await other.get(User, user_a.id)
        set_password(target, "admin-temp-1", must_change=True)
        await other.commit()

    body = PasswordChangeRequest(current_password=PASSWORD, new_password="brand-new-pass")
    with pytest.raises(HTTPException) as e:
        await auth.change_password(body, Response(), db=db, user=stale)

    assert e.value.status_code == 401
    await db.rollback()
    stored = await _fresh(user_id)
    assert stored.must_change_password is True
    assert verify_password("admin-temp-1", stored.password_hash)


async def test_admin_create_short_password_is_422_without_echo(api, admin_user):
    admin = await api.login(admin_user.email)

    res = await api.call(
        "POST",
        "/admin/users",
        admin["access_token"],
        json={"email": f"short-{uuid.uuid4().hex[:8]}@test.dev", "name": "n", "password": "pw!"},
    )

    assert res.status_code == 422 and res.json()["detail"]["reason"] == "too_short"
    assert '"pw!"' not in res.text


async def test_product_policy_rejects_self_change_and_admin_create(
    api, user_a, admin_user, monkeypatch
):
    """제품 정책 훅(#174) — 금지 값이면 실제 저장 경로에서 422이고 아무것도 바뀌지 않는다."""
    from biznotes_core.core import security

    forbidden = "seed-default-1!"

    current = get_settings()

    # 스위트 설정 클래스(_PgSuiteSettings)를 이어받는다 — 제품 설정 하위 클래스와 같은 모양
    class Policy(type(current)):  # type: ignore[misc]
        def check_password_policy(self, plain: str, /, **context: object) -> None:
            if plain == forbidden:
                raise security.PasswordPolicyError("seed_default", "기본 비밀번호 금지")

    instance = Policy.model_construct(**dict(current))
    monkeypatch.setattr(security, "get_settings", lambda: instance)
    token = (await api.login(user_a.email))["access_token"]

    res = await api.call(
        "POST",
        "/auth/password",
        token,
        json={"current_password": PASSWORD, "new_password": forbidden},
    )

    assert res.status_code == 422, res.text
    assert res.json()["detail"] == {
        "code": "invalid_new_password",
        "reason": "seed_default",
        "detail": "기본 비밀번호 금지",
    }
    assert forbidden not in res.text
    fresh = await _fresh(user_a.id)
    assert fresh.password_changed_at is None and verify_password(PASSWORD, fresh.password_hash)

    admin = await api.login(admin_user.email)
    email = f"policy-{uuid.uuid4().hex[:8]}@test.dev"
    created = await api.call(
        "POST",
        "/admin/users",
        admin["access_token"],
        json={"email": email, "name": "n", "password": forbidden},
    )
    assert created.status_code == 422, created.text
    assert created.json()["detail"]["reason"] == "seed_default"
    async with async_session_factory() as s:
        assert (await s.execute(select(User).where(User.email == email))).first() is None
