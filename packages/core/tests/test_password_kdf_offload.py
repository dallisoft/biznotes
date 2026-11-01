"""bcrypt를 이벤트 루프 밖에서 돌린다 (#289).

계획 `docs/plans/password-kdf-offload/plan.md` 수락 기준 1~6·8이다. 핸들러·`run_password_kdf`·
`set_password_async`는 실경로다. DB는 대역 세션이고 bcrypt는 실제 함수를 감싸 스레드만 기록한다.
상한은 호출 때 설정을 읽으므로, 세션 범위 이벤트 루프 하나에서 돌아도 테스트마다 크기를 정할 수
있다.
"""

import ast
import asyncio
import pathlib
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import anyio
import pytest
from conftest import make_settings
from fastapi import HTTPException, Response
from pydantic import ValidationError
from test_login_user_enumeration import _Db, _user
from test_password_change import _AdminDb

from biznotes_core.api.v1 import auth
from biznotes_core.config import CoreSettings
from biznotes_core.core import security
from biznotes_core.core.security import _password_kdf_limiter, run_password_kdf
from biznotes_core.schemas.auth import LoginRequest
from biznotes_core.services import user_passwords

API = pathlib.Path(__file__).resolve().parent.parent / "biznotes_core" / "api"


@pytest.fixture
def concurrency(monkeypatch: pytest.MonkeyPatch) -> Any:
    """`password_hash_concurrency`를 바꾼다 — 상한은 호출 때 설정을 읽는다."""

    def set_to(n: int) -> None:
        settings = make_settings(password_hash_concurrency=n)
        monkeypatch.setattr(security, "get_settings", lambda: settings)

    set_to(4)
    return set_to


# ── 수락 기준 1: bcrypt는 루프 스레드 밖에서 돈다 ─────────────────────────


@pytest.fixture
def threads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """실제 bcrypt 함수를 감싸 (이름, 실행 스레드)를 기록한다."""
    seen: list[tuple[str, int]] = []

    def wrap(name: str, real: Any) -> Any:
        def spy(*args: Any) -> Any:
            seen.append((name, threading.get_ident()))
            return real(*args)

        return spy

    monkeypatch.setattr(auth, "verify_password", wrap("verify", security.verify_password))
    monkeypatch.setattr(user_passwords, "hash_password", wrap("hash", security.hash_password))
    auth._dummy_password_hash.cache_clear()
    monkeypatch.setattr(auth, "hash_password", wrap("dummy", security.hash_password))
    yield seen
    auth._dummy_password_hash.cache_clear()


async def test_login_runs_bcrypt_off_the_loop_thread(threads) -> None:
    loop_thread = threading.get_ident()
    body = LoginRequest(email="nobody@test.dev", password="pw-wrong-1234")
    with pytest.raises(HTTPException):
        await auth.login(body, Response(), db=_Db(None))

    assert [name for name, _ in threads] == ["dummy", "verify"]
    assert all(t != loop_thread for _, t in threads)


async def test_admin_create_and_reset_hash_off_the_loop_thread(threads, monkeypatch) -> None:
    from biznotes_core.api.v1.admin import users

    async def no_audit(*a: Any, **kw: Any) -> None:
        return None

    class _Db(_AdminDb):
        async def refresh(self, obj: Any) -> None:
            # 서버 기본값을 채운다(INSERT 뒤 refresh 흉내)
            obj.id, obj.is_active, obj.created_at = uuid.uuid4(), True, datetime.now(UTC)

    monkeypatch.setattr(users, "record", no_audit)
    loop_thread = threading.get_ident()
    admin = _user(role="admin")
    body = users.UserCreate(email="new@test.dev", name="n", password="pw-new-1234")
    await users.create_user(body, db=_Db(), admin=admin)
    target = _user(password_hash="old-hash")
    await users.reset_password(target.id, db=_AdminDb(target), admin=admin)

    assert [name for name, _ in threads] == ["hash", "hash"]
    assert all(t != loop_thread for _, t in threads)


async def test_set_password_async_matches_the_sync_rules(threads) -> None:
    user = _user(password_hash="old-hash", must_change_password=True)

    await user_passwords.set_password_async(user, "pw-new-1234", must_change=False)

    assert security.verify_password("pw-new-1234", user.password_hash)
    assert user.must_change_password is False and user.password_changed_at is not None
    assert threads[0][1] != threading.get_ident()


# ── 수락 기준 2: 루프 응답성 ──────────────────────────────────────────────


async def test_other_coroutines_keep_running_during_a_slow_login(monkeypatch) -> None:
    def slow_verify(plain: str, hashed: str) -> bool:
        time.sleep(0.3)  # 스레드에서 도는 bcrypt 대역
        return False

    monkeypatch.setattr(auth, "verify_password", slow_verify)
    ticks = 0
    done = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0.02)

    async def login() -> None:
        try:
            body = LoginRequest(email="x@test.dev", password="pw-wrong-1234")
            with pytest.raises(HTTPException):
                await auth.login(body, Response(), db=_Db(None))
        finally:
            done.set()

    await asyncio.gather(ticker(), login())

    assert ticks >= 8  # 동기 bcrypt였다면 0.3초 동안 한 번도 돌지 못한다


# ── 수락 기준 3: 전용 상한(로그인·쓰기 분리, 공용 풀 보존) ────────────────


async def test_concurrency_is_capped_per_purpose(concurrency) -> None:
    concurrency(2)
    active = peak = 0
    lock = threading.Lock()

    def work() -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1

    await asyncio.gather(*(run_password_kdf(work, purpose="login") for _ in range(6)))

    assert peak == 2


async def test_a_login_flood_does_not_block_password_writes_or_other_offloading(
    concurrency,
) -> None:
    concurrency(2)
    release = threading.Event()
    flood = [
        asyncio.create_task(run_password_kdf(release.wait, 5, purpose="login")) for _ in range(6)
    ]
    await asyncio.sleep(0.05)
    try:
        assert _password_kdf_limiter("login").borrowed_tokens == 2  # 로그인 상한은 찼다
        with anyio.fail_after(1):
            assert await run_password_kdf(lambda: "write", purpose="write") == "write"
            assert await anyio.to_thread.run_sync(lambda: "shared") == "shared"
    finally:
        release.set()
        await asyncio.gather(*flood)


async def test_cancelled_call_keeps_its_token_until_bcrypt_finishes(concurrency) -> None:
    """`abandon_on_cancel`을 쓰지 않는다 — 상한이 실제로 도는 bcrypt 수를 센다.

    Starlette·anyio가 쓰는 cancel scope로 취소한다. 날 asyncio `Task.cancel()`은 anyio 차폐를
    지나쳐 토큰을 일찍 돌려준다(anyio 한계, 계획 잔여 위험).
    """
    concurrency(1)
    started, release = threading.Event(), threading.Event()

    def blocking() -> None:
        started.set()
        release.wait(5)

    scope = anyio.CancelScope()

    async def call() -> None:
        with scope:
            await run_password_kdf(blocking, purpose="write")

    task = asyncio.create_task(call())
    await asyncio.to_thread(started.wait, 5)
    scope.cancel()
    await asyncio.sleep(0.05)
    assert _password_kdf_limiter("write").borrowed_tokens == 1  # 스레드가 아직 돈다
    assert not task.done()
    release.set()
    await task
    assert scope.cancel_called and _password_kdf_limiter("write").borrowed_tokens == 0


# ── 수락 기준 4·5: 시간 균형과 연결 반납 ─────────────────────────────────


@pytest.mark.parametrize("exists", [False, True])
async def test_both_login_paths_close_the_session_before_bcrypt(
    exists: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _Db(_user() if exists else None)

    def verify(plain: str, hashed: str) -> bool:
        db.events.append("verify")
        return False

    monkeypatch.setattr(auth, "verify_password", verify)
    body = LoginRequest(email="u@test.dev", password="pw-wrong-1234")
    with pytest.raises(HTTPException) as exc:
        await auth.login(body, Response(), db=db)

    assert exc.value.status_code == 401
    assert db.events == ["execute", "close", "verify"]  # 경로와 상관없이 같은 순서다


async def test_successful_login_builds_the_response_after_closing() -> None:
    user = _user(password_hash=security.hash_password("pw-right-1234"))
    db = _Db(user)

    body = LoginRequest(email=user.email, password="pw-right-1234")
    out = await auth.login(body, Response(), db=db)

    assert db.events == ["execute", "close"]
    assert out.user.email == user.email and out.access_token


# ── 수락 기준 6: async 핸들러에서 bcrypt를 직접 부르지 않는다 ─────────────

BCRYPT_CALLS = frozenset(
    {"hash_password", "verify_password", "set_password", "_dummy_password_hash"}
)


def _direct_bcrypt_calls(tree: ast.AST) -> list[int]:
    """async 함수 본문의 bcrypt 직접 호출. 이름을 인자로 넘기는 것은 허용하고, 람다·중첩 def는
    경계에서 멈춘다(그 안의 호출은 그 함수가 어디서 불리는지에 달렸다)."""
    hits: list[int] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        stack: list[ast.AST] = list(fn.body)
        while stack:
            node = stack.pop()
            if isinstance(node, ast.Lambda | ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in BCRYPT_CALLS:
                    hits.append(node.lineno)
            stack.extend(ast.iter_child_nodes(node))
    return hits


def test_async_handlers_do_not_call_bcrypt_directly() -> None:
    found = {
        str(path.relative_to(API)): lines
        for path in sorted(API.rglob("*.py"))
        if (lines := _direct_bcrypt_calls(ast.parse(path.read_text(encoding="utf-8"))))
    }
    assert found == {}


def test_the_guard_sees_through_attributes_but_not_lambdas() -> None:
    source = (
        "async def h():\n"
        "    security.verify_password(a, b)\n"
        "    await run_password_kdf(verify_password, a, b)\n"
        "    await run_password_kdf(lambda: hash_password(a))\n"
    )
    assert _direct_bcrypt_calls(ast.parse(source)) == [2]


# ── 수락 기준 8: 설정 ────────────────────────────────────────────────────


def test_concurrency_setting_default_and_bounds() -> None:
    assert CoreSettings.model_fields["password_hash_concurrency"].default == 4
    with pytest.raises(ValidationError):
        CoreSettings(password_hash_concurrency=0)


async def test_limiter_follows_the_setting_on_every_call(concurrency) -> None:
    concurrency(3)
    assert _password_kdf_limiter("login").total_tokens == 3
    concurrency(1)
    assert _password_kdf_limiter("login").total_tokens == 1
    assert _password_kdf_limiter("write").total_tokens == 1
