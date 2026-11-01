"""로그인이 없는 이메일에도 같은 비용의 bcrypt 검증을 치른다 (#270).

없는 이메일이면 `verify_password`를 건너뛰어 응답 시간으로 가입 여부가 드러났다. 시간은 재지 않고,
두 경로가 같은 호출(같은 cost의 해시로 한 번)을 지나는지와 응답이 같은지를 본다. 검증 대역은 실제
`verify_password`를 감싸 호출만 기록한다.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import bcrypt
import httpx
import pytest
from fastapi import FastAPI, HTTPException, Response

from biznotes_core.api.v1 import auth
from biznotes_core.core import security
from biznotes_core.core.db import get_db
from biznotes_core.core.rate_limit import RateLimiter
from biznotes_core.models import User
from biznotes_core.schemas.auth import LoginRequest

PASSWORD = "pw-correct-1234"
# bcrypt는 느리므로 모듈에서 한 번만 만든다
USER_HASH = security.hash_password(PASSWORD)


def _user(**kw: Any) -> User:
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "email": "known@test.dev",
        "name": "known",
        "role": "user",
        "is_active": True,
        "password_hash": USER_HASH,
        "must_change_password": False,
        "created_at": datetime.now(UTC),
    }
    fields.update(kw)
    return User(**fields)


class _Db:
    """이메일 조회 한 번만 흉내 낸다 — `user`가 None이면 없는 이메일이다.

    `events`에 조회·닫기 순서를 남긴다(bcrypt 전에 연결을 돌려주는지, #289).
    """

    def __init__(self, user: User | None) -> None:
        self.user = user
        self.events: list[str] = []

    async def execute(self, stmt: Any) -> Any:
        user = self.user
        self.events.append("execute")

        class _R:
            def scalar_one_or_none(self) -> User | None:
                return user

        return _R()

    async def close(self) -> None:
        self.events.append("close")


def _cost(hashed: str) -> str:
    return hashed.split("$")[2]


class _Calls(list[tuple[str, str]]):
    """(평문, 해시) 호출 기록과 실제 반환값(`results`)."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[bool] = []


@pytest.fixture
def verify_calls(monkeypatch: pytest.MonkeyPatch) -> _Calls:
    """실제 `verify_password`를 감싸 호출과 반환값을 기록한다."""
    calls = _Calls()

    def spy(plain: str, hashed: str) -> bool:
        calls.append((plain, hashed))
        calls.results.append(security.verify_password(plain, hashed))
        return calls.results[-1]

    # auth가 이름을 직접 import하므로 auth 모듈의 이름을 바꾼다
    monkeypatch.setattr(auth, "verify_password", spy)
    return calls


async def _login(db: _Db, email: str, password: str) -> Any:
    return await auth.login(LoginRequest(email=email, password=password), Response(), db=db)


async def test_missing_email_runs_one_bcrypt_check_with_the_same_cost(verify_calls) -> None:
    with pytest.raises(HTTPException) as exc:
        await _login(_Db(None), "nobody@test.dev", "pw-wrong-1234")

    assert (exc.value.status_code, exc.value.detail) == (401, "Invalid email or password")
    assert len(verify_calls) == 1
    plain, hashed = verify_calls[0]
    assert plain == "pw-wrong-1234"
    assert hashed == auth._dummy_password_hash()
    assert verify_calls.results == [False]
    # 형식만 맞는 해시는 `ValueError`로 즉시 False가 돼 시간이 다시 갈린다 — 실제 KDF를 끝까지
    # 도는 해시인지(예외 없이 False), cost가 실제 사용자 해시와 같은지 본다
    assert bcrypt.checkpw(b"anything", hashed.encode()) is False
    assert _cost(hashed) == _cost(USER_HASH) == _cost(security.hash_password("x"))


async def test_dummy_hash_is_made_once_before_the_lookup(verify_calls) -> None:
    """지연 생성이지만 조회 전에 조건 없이 만든다 — 첫 요청의 생성 비용이 경로와 무관하다."""
    auth._dummy_password_hash.cache_clear()
    user = _user()
    with pytest.raises(HTTPException):
        await _login(_Db(user), user.email, "pw-wrong-1234")  # 있는 이메일이 먼저 와도
    assert auth._dummy_password_hash.cache_info().currsize == 1
    first = auth._dummy_password_hash()
    with pytest.raises(HTTPException):
        await _login(_Db(None), "nobody@test.dev", "pw-wrong-1234")
    assert verify_calls[-1][1] == first


async def test_over_72_byte_passwords_fail_alike_on_both_paths(verify_calls) -> None:
    """bcrypt 입력 상한(72바이트)을 넘으면 두 경로 모두 KDF 전에 False다 — 시간이 같다."""
    long = "가" * 25  # 75바이트 — 스키마 상한(128자)은 통과한다
    user = _user()
    for db, email in ((_Db(None), "nobody@test.dev"), (_Db(user), user.email)):
        with pytest.raises(HTTPException) as exc:
            await _login(db, email, long)
        assert exc.value.status_code == 401
    assert len(verify_calls) == 2
    assert verify_calls.results == [False, False]


async def test_wrong_password_checks_the_user_hash_once(verify_calls) -> None:
    user = _user()
    with pytest.raises(HTTPException) as exc:
        await _login(_Db(user), user.email, "pw-wrong-1234")

    assert (exc.value.status_code, exc.value.detail) == (401, "Invalid email or password")
    assert verify_calls == [("pw-wrong-1234", USER_HASH)]


async def test_dummy_check_result_never_logs_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """더미 검증이 참이어도(어떤 이유로든) 없는 이메일은 401이다."""
    monkeypatch.setattr(auth, "verify_password", lambda plain, hashed: True)

    with pytest.raises(HTTPException) as exc:
        await _login(_Db(None), "nobody@test.dev", "pw-any-12345")

    assert exc.value.status_code == 401


async def test_disabled_user_is_403_only_with_the_right_password(verify_calls) -> None:
    user = _user(is_active=False)
    with pytest.raises(HTTPException) as right:
        await _login(_Db(user), user.email, PASSWORD)
    with pytest.raises(HTTPException) as wrong:
        await _login(_Db(user), user.email, "pw-wrong-1234")

    assert (right.value.status_code, right.value.detail) == (403, "Account is disabled")
    assert (wrong.value.status_code, wrong.value.detail) == (401, "Invalid email or password")
    assert [h for _, h in verify_calls] == [USER_HASH, USER_HASH]


async def test_right_password_logs_in_after_one_check(verify_calls) -> None:
    user = _user()
    out = await _login(_Db(user), user.email, PASSWORD)

    assert out.user.email == user.email
    assert out.access_token and out.refresh_token
    assert verify_calls == [(PASSWORD, USER_HASH)]


def _app(db: _Db) -> FastAPI:
    app = FastAPI()
    app.include_router(auth.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    for route in auth.router.routes:
        for dep in getattr(route, "dependencies", []):
            if isinstance(dep.dependency, RateLimiter):
                app.dependency_overrides[dep.dependency] = lambda: None
    return app


async def _post_login(db: _Db, email: str, password: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app(db))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


async def test_missing_email_and_wrong_password_responses_are_identical(verify_calls) -> None:
    user = _user()
    missing = await _post_login(_Db(None), "nobody@test.dev", "pw-wrong-1234")
    wrong = await _post_login(_Db(user), user.email, "pw-wrong-1234")

    assert missing.status_code == wrong.status_code == 401
    assert missing.content == wrong.content
    assert missing.headers == wrong.headers
    assert len(verify_calls) == 2
