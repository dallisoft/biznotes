"""강제 비밀번호 변경·본인 변경의 DB 없는 검증 (#165).

토큰·게이트는 실제 `create_token`·`get_current_user`를 쓴다. 바꾸는 것은 외부 경계뿐이다 —
DB 세션(`get_db`)은 사용자 한 명을 돌려주는 가짜, Redis denylist는 "폐기 안 됨" 대역이다.
실제 PostgreSQL·Redis 흐름은 `tests/test_forced_password_change_pg.py`가 본다.
"""

import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import jwt
import pytest
from fastapi import Depends, FastAPI
from pydantic import ValidationError

from biznotes_core.api import deps
from biznotes_core.config import CoreSettings, get_settings
from biznotes_core.core.db import get_db
from biznotes_core.core.security import (
    ALGORITHM,
    PasswordPolicyError,
    create_token,
    decode_token,
    password_version,
    validate_new_password,
    verify_password,
)
from biznotes_core.models import User
from biznotes_core.schemas.auth import PasswordChangeRequest, UserOut
from biznotes_core.services.user_passwords import require_password_change, set_password

CHANGED_AT = datetime(2026, 10, 9, 1, 2, 3, 456789, tzinfo=UTC)


def test_password_version_is_exact_epoch_microseconds() -> None:
    assert password_version(None) == 0
    assert password_version(CHANGED_AT) == int(CHANGED_AT.timestamp()) * 1_000_000 + 456789
    # 같은 순간이면 시간대 표현과 무관하게 같은 값(DB는 UTC로 돌려준다)
    kst = CHANGED_AT.astimezone(timezone(timedelta(hours=9)))
    assert password_version(kst) == password_version(CHANGED_AT)
    # 1마이크로초 차이도 구분한다 — 초 단위 iat 비교의 경계 문제가 없다
    later = CHANGED_AT + timedelta(microseconds=1)
    assert password_version(later) == password_version(CHANGED_AT) + 1


@pytest.mark.parametrize(
    "plain,reason",
    [
        ("a" * 7, "too_short"),
        ("a" * 8, None),
        ("가" * 24, None),
        ("가" * 25, "too_long_bytes"),
        ("a" * 73, "too_long_bytes"),
    ],
    ids=["7자", "8자", "72바이트", "75바이트", "73바이트"],
)
def test_validate_new_password(plain: str, reason: str | None) -> None:
    if reason is None:
        assert validate_new_password(plain) == plain
        return
    with pytest.raises(PasswordPolicyError) as e:
        validate_new_password(plain)
    assert e.value.reason == reason
    assert plain not in str(e.value)  # 메시지에 입력값을 싣지 않는다


def test_change_request_schema_only_caps_length() -> None:
    """규칙 검사를 스키마에 두지 않는다 — FastAPI 422 본문이 입력 비밀번호를 되돌려준다(리뷰 M1)."""
    PasswordChangeRequest(current_password="same-password", new_password="same-password")
    PasswordChangeRequest(current_password="x", new_password="short")
    with pytest.raises(ValidationError):
        PasswordChangeRequest(current_password="x", new_password="a" * 129)


def test_set_password_rotates_version_and_flag() -> None:
    user = _user()
    before = password_version(user.password_changed_at)

    set_password(user, "first-pass-1", must_change=True)
    first = password_version(user.password_changed_at)
    assert user.must_change_password is True and first != before
    assert verify_password("first-pass-1", user.password_hash)

    set_password(user, "second-pass-2", must_change=False)
    assert user.must_change_password is False
    assert password_version(user.password_changed_at) > first

    with pytest.raises(PasswordPolicyError):
        set_password(user, "가" * 25, must_change=False)
    assert verify_password("second-pass-2", user.password_hash)  # 실패하면 바꾸지 않는다

    hash_before = user.password_hash
    require_password_change(user)
    assert user.must_change_password is True and user.password_hash == hash_before


def test_password_change_route_is_rate_limited_per_user() -> None:
    """`APP_ENV=test`에서는 리밋이 꺼져 PG 테스트로 볼 수 없다 — 라우트 선언을 확인한다(리뷰 m7)."""
    from biznotes_core.api.v1 import auth
    from biznotes_core.core.rate_limit import RateLimiter

    [route] = [r for r in auth.router.routes if getattr(r, "path", "") == "/auth/password"]
    limiters = [d.dependency for d in route.dependencies if isinstance(d.dependency, RateLimiter)]
    assert [(lim.key, lim.scope) for lim in limiters] == [("password_change", "user")]


def test_create_token_carries_password_version() -> None:
    uid = uuid.uuid4()
    assert decode_token(create_token(uid, "access", "user"), "access")["pwv"] == 0
    claims = decode_token(create_token(uid, "refresh", "user", pwv=42), "refresh")
    assert claims["pwv"] == 42


def test_user_columns_default_to_unflagged() -> None:
    table = User.__table__
    flag, changed = table.c.must_change_password, table.c.password_changed_at

    assert flag.nullable is False and flag.server_default is not None
    assert "false" in str(flag.server_default.arg).lower()
    assert changed.nullable is True and changed.server_default is None
    assert UserOut.model_fields["must_change_password"].default is False


# ---------- 게이트: 실제 의존성 + 가짜 DB ----------


class _OneUserDb:
    def __init__(self, user: User) -> None:
        self.user = user

    async def get(self, model: type, key: uuid.UUID) -> User | None:
        return self.user if model is User and key == self.user.id else None


def _user(**kw: Any) -> User:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "email": "u@test.dev",
        "name": "u",
        "role": "user",
        "is_active": True,
        "password_hash": "x",
        "must_change_password": False,
        "password_changed_at": None,
        "created_at": CHANGED_AT,
    }
    base.update(kw)
    return User(**base)


async def _call(user: User, token: str) -> dict[str, httpx.Response]:
    app = FastAPI()

    @app.get("/normal")
    async def normal(u: User = Depends(deps.get_current_user)) -> dict[str, str]:
        return {"id": str(u.id)}

    @app.get("/admin")
    async def admin(u: User = Depends(deps.require_admin)) -> dict[str, str]:
        return {"id": str(u.id)}

    @app.get("/allowed")
    async def allowed(
        u: User = Depends(deps.get_current_user_allow_password_change),
    ) -> dict[str, str]:
        return {"id": str(u.id)}

    app.dependency_overrides[get_db] = lambda: _OneUserDb(user)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        paths = ("normal", "admin", "allowed")
        return {p: await client.get(f"/{p}", headers=headers) for p in paths}


@pytest.fixture(autouse=True)
def _not_revoked(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(jti: str | None) -> bool:
        return False

    monkeypatch.setattr(deps, "is_revoked", never)


async def test_flagged_user_is_blocked_everywhere_but_allowlist() -> None:
    user = _user(role="admin", must_change_password=True)
    res = await _call(user, create_token(user.id, "access", user.role))

    for path in ("normal", "admin"):
        assert res[path].status_code == 403
        assert res[path].json()["detail"]["code"] == deps.PASSWORD_CHANGE_REQUIRED
    assert res["allowed"].status_code == 200


async def test_token_from_before_password_change_is_rejected() -> None:
    user = _user(password_changed_at=CHANGED_AT)

    stale = await _call(user, create_token(user.id, "access", user.role, pwv=0))
    current = await _call(
        user, create_token(user.id, "access", user.role, pwv=password_version(CHANGED_AT))
    )

    assert {r.status_code for r in stale.values()} == {401}
    assert current["normal"].status_code == 200
    assert current["allowed"].status_code == 200


async def test_pre_upgrade_token_without_claim_still_works_for_unchanged_user() -> None:
    user = _user()
    now = datetime.now(UTC)
    legacy = jwt.encode(
        {
            "sub": str(user.id),
            "type": "access",
            "role": "user",
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "jti": str(uuid.uuid4()),
        },
        get_settings().jwt_secret,
        algorithm=ALGORITHM,
    )

    res = await _call(user, legacy)

    assert res["normal"].status_code == 200
    assert res["admin"].status_code == 403  # role 검사는 그대로
    assert res["admin"].json()["detail"] == "Admin role required"


# ---------------------------------------------------------------------------
# 제품 비밀번호 정책 훅(#174) — 설정 하위 클래스가 `check_password_policy`를 덮어쓴다
# ---------------------------------------------------------------------------

FORBIDDEN = "seed-default-1!"
_SEEN: list[str] = []
_CONTEXTS: list[dict[str, Any]] = []


class _PolicySettings(CoreSettings):
    def check_password_policy(self, plain: str, /, **context: Any) -> Any:
        _SEEN.append(plain)
        _CONTEXTS.append(context)
        if plain == FORBIDDEN:
            raise PasswordPolicyError("seed_default", "공개된 기본 비밀번호는 쓸 수 없습니다")
        if plain == "bad-reason-1":
            raise PasswordPolicyError("Bad Reason!", "형식이 어긋난 reason")
        if plain == "core-reason-1":
            raise PasswordPolicyError("too_short", "코어 reason을 빌려 씀")
        if plain == "echo-input-1":
            raise PasswordPolicyError("echoes", f"{plain}은(는) 금지")
        if plain in ("abcdefgh1", "Bad:abcdefgh2"):
            raise PasswordPolicyError(plain, "reason에 입력값을 넣은 버그")
        if plain == "returns-false":
            return False
        if plain == "buggy-hook-1":
            raise RuntimeError("정책 버그")
        return None


@pytest.fixture
def policy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from biznotes_core.core import security

    _SEEN.clear()
    _CONTEXTS.clear()
    current = get_settings()
    instance = _PolicySettings.model_construct(**dict(current))
    monkeypatch.setattr(security, "get_settings", lambda: instance)
    return _SEEN


def test_policy_hook_runs_after_core_length_rules(policy: list[str]) -> None:
    with pytest.raises(PasswordPolicyError) as exc:
        validate_new_password(FORBIDDEN)
    assert (exc.value.reason, str(exc.value)) == (
        "seed_default",
        "공개된 기본 비밀번호는 쓸 수 없습니다",
    )
    with pytest.raises(PasswordPolicyError, match="8자") as short:
        validate_new_password("short")
    assert short.value.reason == "too_short"
    assert validate_new_password("pw-ok-1234") == "pw-ok-1234"
    # 코어 규칙에 걸린 값은 제품 정책에 닿지 않는다
    assert policy == [FORBIDDEN, "pw-ok-1234"]


def test_policy_reason_format_is_enforced_and_other_errors_propagate(policy: list[str]) -> None:
    with pytest.raises(PasswordPolicyError) as exc:
        validate_new_password("bad-reason-1")
    assert exc.value.reason == "policy"  # 거부는 유지, 응답 코드만 안전한 값으로
    with pytest.raises(PasswordPolicyError) as core:
        validate_new_password("core-reason-1")
    assert core.value.reason == "policy"  # 코어 reason은 예약돼 있다
    with pytest.raises(RuntimeError, match="정책 버그"):
        validate_new_password("buggy-hook-1")


def test_policy_message_never_echoes_input_and_non_none_return_rejects(policy) -> None:
    with pytest.raises(PasswordPolicyError) as echo:
        validate_new_password("echo-input-1")
    assert echo.value.reason == "echoes" and "echo-input-1" not in str(echo.value)
    with pytest.raises(PasswordPolicyError) as falsy:
        validate_new_password("returns-false")
    assert falsy.value.reason == "policy"  # 거부하려던 훅을 통과로 바꾸지 않는다


def test_set_password_passes_context_to_policy(policy) -> None:
    user = _user(email="ctx@test.dev")
    set_password(user, "pw-ok-1234", must_change=True)
    set_password(user, "pw-ok-5678", must_change=False)
    assert _CONTEXTS == [
        {"email": "ctx@test.dev", "must_change": True},
        {"email": "ctx@test.dev", "must_change": False},
    ]


def test_core_default_policy_allows_everything() -> None:
    assert CoreSettings.model_construct().check_password_policy(FORBIDDEN) is None


def test_policy_registered_via_settings_class(monkeypatch: pytest.MonkeyPatch) -> None:
    """제품이 쓰는 등록 경로(`set_settings_class`)로 바꾼 클래스가 그대로 적용된다."""
    from biznotes_core import config

    original = config._settings_cls
    config.reset_settings(_PolicySettings)
    try:
        assert isinstance(get_settings(), _PolicySettings)
        with pytest.raises(PasswordPolicyError, match="기본 비밀번호"):
            validate_new_password(FORBIDDEN)
    finally:
        config.reset_settings(original)


def test_set_password_rejected_by_policy_changes_nothing(policy: list[str]) -> None:
    user = _user(password_hash="old-hash", must_change_password=True)

    with pytest.raises(PasswordPolicyError):
        set_password(user, FORBIDDEN, must_change=False)

    assert (user.password_hash, user.must_change_password, user.password_changed_at) == (
        "old-hash",
        True,
        None,
    )


class _AdminDb:
    def __init__(self, user: User | None = None) -> None:
        self.user = user
        self.added: list[Any] = []
        self.commits = 0

    async def get(self, model: type, key: uuid.UUID, **kw: Any) -> User | None:
        return self.user

    async def execute(self, stmt: Any) -> Any:
        class _R:
            def scalar_one_or_none(self) -> None:
                return None

        return _R()

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def admin_routes(monkeypatch: pytest.MonkeyPatch) -> Any:
    from biznotes_core.api.v1.admin import users

    async def no_audit(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(users, "record", no_audit)
    return users


async def test_admin_create_with_forbidden_password_is_422(policy, admin_routes) -> None:
    from fastapi import HTTPException

    db = _AdminDb()
    body = admin_routes.UserCreate(email="new@test.dev", name="n", password=FORBIDDEN)
    with pytest.raises(HTTPException) as exc:
        await admin_routes.create_user(body, db=db, admin=_user(role="admin"))

    assert exc.value.status_code == 422
    assert exc.value.detail["reason"] == "seed_default"
    assert FORBIDDEN not in str(exc.value.detail)
    assert db.added == [] and db.commits == 0


async def test_admin_reset_regenerates_temporary_password_rejected_by_policy(
    policy, admin_routes, monkeypatch
) -> None:
    temps = iter([FORBIDDEN, FORBIDDEN, "fresh-temp-ok"])
    monkeypatch.setattr(admin_routes.secrets, "token_urlsafe", lambda n: next(temps))
    user = _user(password_hash="old-hash")
    db = _AdminDb(user)

    out = await admin_routes.reset_password(user.id, db=db, admin=_user(role="admin"))

    assert out.temporary_password == "fresh-temp-ok"
    assert verify_password("fresh-temp-ok", user.password_hash)
    assert user.must_change_password is True and db.commits == 1


async def test_admin_reset_gives_up_without_changing_password(
    policy, admin_routes, monkeypatch
) -> None:
    from fastapi import HTTPException

    monkeypatch.setattr(admin_routes.secrets, "token_urlsafe", lambda n: FORBIDDEN)
    user = _user(password_hash="old-hash")
    db = _AdminDb(user)

    with pytest.raises(HTTPException) as exc:
        await admin_routes.reset_password(user.id, db=db, admin=_user(role="admin"))

    assert exc.value.status_code == 500
    assert exc.value.detail == {"code": "password_policy_rejected_temporary"}
    assert policy.count(FORBIDDEN) == admin_routes._TEMP_PASSWORD_ATTEMPTS
    assert (user.password_hash, user.password_changed_at, db.commits) == ("old-hash", None, 0)


@pytest.mark.parametrize("plain", ["abcdefgh1", "Bad:abcdefgh2"])
def test_policy_reason_with_input_is_coerced_and_not_logged(policy, plain) -> None:
    """훅 버그로 reason에 입력값이 들어가도 응답·로그에 나가지 않는다(재리뷰 r1)."""
    import structlog.testing

    with structlog.testing.capture_logs() as logs, pytest.raises(PasswordPolicyError) as exc:
        validate_new_password(plain)
    assert exc.value.reason == "policy"
    assert plain not in str(exc.value) and plain not in str(logs)
