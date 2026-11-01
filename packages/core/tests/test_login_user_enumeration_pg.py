# 필요: PostgreSQL·Valkey(PG 테스트).
"""로그인 실패 경로 — 실제 라우터·DB로 없는 이메일도 bcrypt 검증을 한 번 치른다 (#270).

시간은 재지 않는다. 검증 대역은 실제 `verify_password`를 감싸 호출만 기록한다. `APP_ENV=test`라
레이트 리밋은 꺼져 있다.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from biznotes_core.api.v1 import auth
from biznotes_core.core import security

PASSWORD = "pw-test-1234"  # tests/conftest.py 공용 해시의 평문
INVALID = {"detail": "Invalid email or password"}


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(auth.router, prefix="/api/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def verify_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    hashes: list[str] = []

    def spy(plain: str, hashed: str) -> bool:
        hashes.append(hashed)
        return security.verify_password(plain, hashed)

    monkeypatch.setattr(auth, "verify_password", spy)
    return hashes


async def _login(client: httpx.AsyncClient, email: str, password: str) -> httpx.Response:
    client.cookies.clear()
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


async def test_login_failures_all_pay_one_bcrypt_check(client, verify_calls, db, user_a):
    missing = await _login(client, "nobody@test.dev", "pw-wrong-1234")
    wrong = await _login(client, user_a.email, "pw-wrong-1234")

    assert (missing.status_code, missing.json()) == (401, INVALID)
    assert (wrong.status_code, wrong.json()) == (401, INVALID)
    assert dict(missing.headers) == dict(wrong.headers)
    assert verify_calls == [auth._dummy_password_hash(), user_a.password_hash]

    ok = await _login(client, user_a.email, PASSWORD)
    assert ok.status_code == 200, ok.text
    assert len(verify_calls) == 3


async def test_disabled_user_is_403_only_with_the_right_password(client, verify_calls, db, user_a):
    user_a.is_active = False
    await db.commit()

    right = await _login(client, user_a.email, PASSWORD)
    wrong = await _login(client, user_a.email, "pw-wrong-1234")

    assert (right.status_code, right.json()) == (403, {"detail": "Account is disabled"})
    assert (wrong.status_code, wrong.json()) == (401, INVALID)
    assert verify_calls == [user_a.password_hash, user_a.password_hash]
