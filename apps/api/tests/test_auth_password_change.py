"""비밀번호 강제 변경·본인 변경·토큰 무효화 회귀 — 제품 조립 앱 경유(core-v0.11.0-upgrade AC-1.13).

코어 v0.11.0 #165를 제품 라우터 전체가 상속하는지 **제품 앱**(`app.main.app`)으로 확인한다. 제품
pytest는 제품 `tests`만 수집하므로 상류 테스트로 갈음하지 않는다(v2 PR-10). 대체 없음 — 레이트
리밋 테스트만 설정 값(`app_env`·`rate_limit_enabled`)을 바꾼다.

권한 표(PLAN §6.6): E-A1 `POST /auth/password`, E-A2 `GET /auth/me`, E-A3 제품 라우터 게이트,
E-A4 관리자 생성·재설정.

**쿠키 위생**: 코어는 쿠키를 Bearer 헤더보다 우선한다. 토큰을 지정해 부르는 모든 호출 전에
쿠키를 비운다(`_get`·`_post`). 그렇지 않으면 마지막 로그인·변경 응답의 쿠키로 인증된다.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from biznotes_core.models import ChatSession, Notebook

from app.core.config import get_settings
from tests.conftest import bearer, login

PASSWORD_CHANGE_REQUIRED = "password_change_required"
TEMP_PASSWORD = "Temp-pass-1234"
NEW_PASSWORD = "New-pass-5678"


async def _get(client: httpx.AsyncClient, path: str, token: str) -> httpx.Response:
    client.cookies.clear()
    return await client.get(path, headers=bearer(token))


async def _post(
    client: httpx.AsyncClient, path: str, token: str, body: dict | None = None
) -> httpx.Response:
    client.cookies.clear()
    res = await client.post(path, json=body, headers=bearer(token))
    client.cookies.clear()
    return res


async def _login_pair(client: httpx.AsyncClient, email: str, password: str) -> tuple[str, str]:
    client.cookies.clear()
    res = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    client.cookies.clear()
    return res.json()["access_token"], res.json()["refresh_token"]


async def _refresh(client: httpx.AsyncClient, refresh_token: str) -> httpx.Response:
    client.cookies.clear()
    res = await client.post("/api/v1/auth/refresh", headers=bearer(refresh_token))
    client.cookies.clear()
    return res


async def _change(
    client: httpx.AsyncClient, token: str, current: str, new: str
) -> httpx.Response:
    return await _post(
        client,
        "/api/v1/auth/password",
        token,
        {"current_password": current, "new_password": new},
    )


async def _admin_create(
    client: httpx.AsyncClient, admin_token: str, role: str = "user"
) -> tuple[str, str]:
    """관리자 생성 — 코어가 `set_password(must_change=True)`로 강제 변경 대상을 만든다."""
    email = f"forced-{uuid.uuid4().hex[:8]}@test.dev"
    res = await _post(
        client,
        "/api/v1/admin/users",
        admin_token,
        {"email": email, "name": "강제", "password": TEMP_PASSWORD, "role": role},
    )
    assert res.status_code == 201, res.text
    assert res.json()["must_change_password"] is True
    return email, res.json()["id"]


def _legacy_token(user, token_type: str) -> str:
    """업그레이드 전(v0.10.6) 발급 형태 — `pwv` 클레임이 없는 토큰(테스트가 서명한다)."""
    now = datetime.now(UTC)
    ttl = timedelta(minutes=10) if token_type == "access" else timedelta(days=1)
    payload = {
        "sub": str(user.id),
        "type": token_type,
        "role": user.role,
        "iat": now,
        "exp": now + ttl,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, get_settings().jwt_secret, algorithm="HS256")


def _assert_forced(res: httpx.Response) -> None:
    assert res.status_code == 403, res.text
    assert res.json()["detail"]["code"] == PASSWORD_CHANGE_REQUIRED


async def test_admin_created_user_forced_change_flow(client: httpx.AsyncClient, admin_user):
    """① 관리자 생성 → 로그인 → `/auth/me` 200·플래그 → 403 code → 400·422·200 → 200."""
    admin_token = await login(client, admin_user.email)
    email, _ = await _admin_create(client, admin_token)
    access, _ = await _login_pair(client, email, TEMP_PASSWORD)

    me = await _get(client, "/api/v1/auth/me", access)  # E-A2 — 강제 변경 대상도 허용
    assert me.status_code == 200 and me.json()["must_change_password"] is True
    _assert_forced(await _get(client, "/api/v1/notebooks", access))

    wrong = await _change(client, access, "not-the-password", NEW_PASSWORD)
    assert wrong.status_code == 400
    assert wrong.json()["detail"]["code"] == "invalid_current_password"
    short = await _change(client, access, TEMP_PASSWORD, "short")
    assert short.status_code == 422
    assert short.json()["detail"] == {
        "code": "invalid_new_password",
        "reason": "too_short",
        "detail": short.json()["detail"]["detail"],
    }
    assert "short" not in short.json()["detail"]["detail"]  # 입력값을 되돌리지 않는다
    ok = await _change(client, access, TEMP_PASSWORD, NEW_PASSWORD)
    assert ok.status_code == 200, ok.text
    assert ok.json()["user"]["must_change_password"] is False

    new_access = ok.json()["access_token"]
    assert (await _get(client, "/api/v1/notebooks", new_access)).status_code == 200


async def test_password_change_requires_auth(client: httpx.AsyncClient):
    """E-A1 ① — 비인증 401(E-A2 비인증은 기존 test_auth.py가 본다)."""
    client.cookies.clear()
    res = await client.post(
        "/api/v1/auth/password",
        json={"current_password": "x" * 8, "new_password": "y" * 8},
    )
    assert res.status_code == 401


async def test_self_change_rejects_old_access_and_refresh(client: httpx.AsyncClient, user_a):
    """② 자기 변경 — 변경 전 access·refresh 각각 401(E-A1 ② 변경 전 토큰으로 다시 호출도 401)."""
    from tests.conftest import _PASSWORD

    access, refresh = await _login_pair(client, user_a.email, _PASSWORD)
    ok = await _change(client, access, _PASSWORD, NEW_PASSWORD)
    assert ok.status_code == 200, ok.text

    assert (await _get(client, "/api/v1/auth/me", access)).status_code == 401
    assert (await _refresh(client, refresh)).status_code == 401
    again = await _change(client, access, NEW_PASSWORD, "Another-pass-9")
    assert again.status_code == 401


async def test_new_refresh_works_after_change(client: httpx.AsyncClient, user_a):
    """② 변경 응답의 새 refresh는 `/auth/refresh` 200."""
    from tests.conftest import _PASSWORD

    access, _ = await _login_pair(client, user_a.email, _PASSWORD)
    ok = await _change(client, access, _PASSWORD, NEW_PASSWORD)
    assert ok.status_code == 200

    res = await _refresh(client, ok.json()["refresh_token"])
    assert res.status_code == 200, res.text
    assert (await _get(client, "/api/v1/auth/me", res.json()["access_token"])).status_code == 200


async def test_self_change_keeps_other_users_sessions(client: httpx.AsyncClient, user_a, user_b):
    """② 다른 사용자의 세션은 그대로다."""
    from tests.conftest import _PASSWORD

    access_a, _ = await _login_pair(client, user_a.email, _PASSWORD)
    access_b, refresh_b = await _login_pair(client, user_b.email, _PASSWORD)

    assert (await _change(client, access_a, _PASSWORD, NEW_PASSWORD)).status_code == 200

    assert (await _get(client, "/api/v1/auth/me", access_b)).status_code == 200
    assert (await _refresh(client, refresh_b)).status_code == 200


async def test_reset_invalidates_target_access_and_refresh(
    client: httpx.AsyncClient, user_a, admin_user
):
    """③ 관리자 재설정 — 대상의 이전 access·refresh 각각 401, 플래그 true. 관리자 세션은 유지."""
    from tests.conftest import _PASSWORD

    access, refresh = await _login_pair(client, user_a.email, _PASSWORD)
    admin_token = await login(client, admin_user.email)

    reset = await _post(client, f"/api/v1/admin/users/{user_a.id}/password-reset", admin_token)
    assert reset.status_code == 200, reset.text

    assert (await _get(client, "/api/v1/auth/me", access)).status_code == 401
    assert (await _refresh(client, refresh)).status_code == 401
    users = await _get(client, "/api/v1/admin/users", admin_token)
    assert users.status_code == 200  # 관리자 자신의 세션 유지
    target = next(u for u in users.json() if u["id"] == str(user_a.id))
    assert target["must_change_password"] is True


async def test_relogin_after_reset_refresh_ok_while_forced(
    client: httpx.AsyncClient, user_a, admin_user
):
    """③ 재설정 직후 임시 비밀번호로 다시 로그인한 새 refresh는 강제 변경 상태에서도 200."""
    admin_token = await login(client, admin_user.email)
    reset = await _post(client, f"/api/v1/admin/users/{user_a.id}/password-reset", admin_token)
    temporary = reset.json()["temporary_password"]

    access, refresh = await _login_pair(client, user_a.email, temporary)
    _assert_forced(await _get(client, "/api/v1/notebooks", access))

    res = await _refresh(client, refresh)
    assert res.status_code == 200, res.text  # 변경 화면에서 세션 유지(코어 계약)
    assert res.json()["user"]["must_change_password"] is True
    assert (await _get(client, "/api/v1/admin/users", admin_token)).status_code == 200


async def test_legacy_access_without_pwv_until_change(client: httpx.AsyncClient, user_a):
    """④ `password_changed_at` NULL 사용자의 `pwv` 없는 access는 변경 전 200, 변경 뒤 401."""
    from tests.conftest import _PASSWORD

    assert user_a.password_changed_at is None
    legacy_access = _legacy_token(user_a, "access")
    assert (await _get(client, "/api/v1/auth/me", legacy_access)).status_code == 200

    assert (await _change(client, legacy_access, _PASSWORD, NEW_PASSWORD)).status_code == 200

    assert (await _get(client, "/api/v1/auth/me", legacy_access)).status_code == 401


async def test_legacy_refresh_without_pwv_until_change(client: httpx.AsyncClient, user_a):
    """④ 보관한 legacy refresh(변경 전 미사용)는 변경 뒤 401. 대조 legacy refresh는 변경 전 200.

    보관 refresh는 변경 전에 쓰지 않는다 — 회전으로 소비되면 401의 원인이 바뀐다(v3 PR2-06).
    """
    from tests.conftest import _PASSWORD

    kept_refresh = _legacy_token(user_a, "refresh")
    control_refresh = _legacy_token(user_a, "refresh")
    assert (await _refresh(client, control_refresh)).status_code == 200  # 업그레이드 전 토큰 유효

    access, _ = await _login_pair(client, user_a.email, _PASSWORD)
    assert (await _change(client, access, _PASSWORD, NEW_PASSWORD)).status_code == 200

    assert (await _refresh(client, kept_refresh)).status_code == 401


async def test_password_change_rate_limited(client: httpx.AsyncClient, user_a, monkeypatch):
    """⑤ 5/60s(사용자 단위) — 6번째 호출은 429(v2 PR-10). 레이트 리밋만 설정 값으로 켠다."""
    access = await login(client, user_a.email)
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "dev")
    monkeypatch.setattr(settings, "rate_limit_enabled", True)

    statuses = [
        (await _change(client, access, "wrong-current-pw", NEW_PASSWORD)).status_code
        for _ in range(6)
    ]

    assert statuses == [400, 400, 400, 400, 400, 429]


async def test_wrong_current_password_400_without_echo(client: httpx.AsyncClient, user_a):
    """E-A1 ③ — 현재 비밀번호가 틀리면 400 `invalid_current_password`, 본문에 입력값이 없다."""
    access = await login(client, user_a.email)
    typed_current = "Typed-Current-Secret-42"
    typed_new = "Typed-New-Secret-43"

    res = await _change(client, access, typed_current, typed_new)

    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "invalid_current_password"
    assert typed_current not in res.text and typed_new not in res.text


@pytest.fixture
def _no_retrieval(monkeypatch):
    """채팅 경로의 벡터 검색 경계 — 소스가 없으므로 「맥락 없음」을 돌려준다."""

    async def fake_retrieve(db, notebook_id, query, source_ids, top_k=6):
        from biznotes_core.services.rag_chat import RetrievalResult

        return RetrievalResult(citations=[], insufficient_context=True)

    monkeypatch.setattr("app.api.v1.chat.retrieve_context", fake_retrieve)


@pytest.mark.parametrize("route", ["notebooks", "chat_message", "admin_users"])
async def test_gate_covers_product_routers(
    client: httpx.AsyncClient, db, admin_user, mock_llm, _no_retrieval, route
):
    """E-A3 — 제품 라우터 대표 3개가 강제 변경 대상에게 403 code, 변경 뒤 200.

    관리자 전용 `GET /admin/users`는 **강제 변경 admin**(관리자가 만든 admin)으로 본다 — 일반
    사용자로는 변경 뒤 200이 성립하지 않는다(v2 PR-10).
    """
    admin_token = await login(client, admin_user.email)
    email, user_id = await _admin_create(
        client, admin_token, role="admin" if route == "admin_users" else "user"
    )
    access, _ = await _login_pair(client, email, TEMP_PASSWORD)

    session_id = None
    if route == "chat_message":
        # 변경 전에는 노트북·세션을 API로 만들 수 없다(403) — 테스트 데이터로 둔다
        notebook = Notebook(owner_id=uuid.UUID(user_id), title="강제 변경 채팅")
        db.add(notebook)
        await db.flush()
        session = ChatSession(notebook_id=notebook.id, user_id=uuid.UUID(user_id), title="s")
        db.add(session)
        await db.commit()
        session_id = session.id

    async def call(token: str) -> httpx.Response:
        if route == "notebooks":
            return await _get(client, "/api/v1/notebooks", token)
        if route == "admin_users":
            return await _get(client, "/api/v1/admin/users", token)
        return await _post(
            client, f"/api/v1/chat/sessions/{session_id}/messages", token, {"content": "안녕"}
        )

    _assert_forced(await call(access))

    ok = await _change(client, access, TEMP_PASSWORD, NEW_PASSWORD)
    assert ok.status_code == 200, ok.text
    after = await call(ok.json()["access_token"])
    assert after.status_code == 200, after.text
