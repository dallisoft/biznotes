"""레이트 리미터 단위 테스트 — Redis db 15 (conftest와 동일).

conftest가 APP_ENV=test로 리미터를 우회시키므로,
여기서는 settings 인스턴스를 직접 패치해 활성화 상태를 시뮬레이션한다.
"""

import uuid

import pytest
from biznotes_core.core.rate_limit import RateLimiter, _client_key
from biznotes_core.core.security import create_token
from fastapi import HTTPException
from starlette.requests import Request

from app.core.config import get_settings


def _request(headers: dict[str, str] | None = None, ip: str = "10.0.0.1") -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": raw_headers,
        "client": (ip, 1234),
        "query_string": b"",
    }
    return Request(scope)


@pytest.fixture
def active_settings(monkeypatch):
    """리미터가 동작하도록 app_env를 일시 변경 (lru_cache 인스턴스 직접 패치)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "dev")
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    return settings


async def test_ip_scope_blocks_after_limit(active_settings):
    limiter = RateLimiter(f"t-{uuid.uuid4().hex}", limit=3, window_seconds=60, scope="ip")
    req = _request()
    for _ in range(3):
        await limiter(req)
    with pytest.raises(HTTPException) as exc:
        await limiter(req)
    assert exc.value.status_code == 429
    assert "Retry-After" in (exc.value.headers or {})


async def test_user_scope_separates_users_on_same_ip(active_settings):
    limiter = RateLimiter(f"t-{uuid.uuid4().hex}", limit=2, window_seconds=60, scope="user")
    token_a = create_token(uuid.uuid4(), "access", "user")
    token_b = create_token(uuid.uuid4(), "access", "user")
    req_a = _request({"Authorization": f"Bearer {token_a}"})
    req_b = _request({"Authorization": f"Bearer {token_b}"})

    for _ in range(2):
        await limiter(req_a)
    with pytest.raises(HTTPException):
        await limiter(req_a)
    # 같은 IP의 다른 사용자는 독립 버킷
    await limiter(req_b)


async def test_user_scope_falls_back_to_ip_on_invalid_token(active_settings):
    key = _client_key(_request({"Authorization": "Bearer not-a-jwt"}, ip="1.2.3.4"), "user")
    assert key == "ip:1.2.3.4"


async def test_user_scope_uses_jwt_sub():
    user_id = uuid.uuid4()
    token = create_token(user_id, "access", "user")
    key = _client_key(_request({"Authorization": f"Bearer {token}"}), "user")
    assert key == f"user:{user_id}"


async def test_forwarded_for_is_ignored_for_ip_key():
    """코어 v0.21.0 #290 — 키는 `request.client.host`만 쓴다. 프록시 헤더 적용은 ASGI 서버
    (`FORWARDED_ALLOW_IPS`) 몫이라 위조한 `X-Forwarded-For`로 버킷을 바꿀 수 없다."""
    key = _client_key(_request({"X-Forwarded-For": "9.9.9.9, 10.0.0.2"}, ip="10.0.0.7"), "ip")
    assert key == "ip:10.0.0.7"


async def test_limit_setting_zero_disables(active_settings, monkeypatch):
    monkeypatch.setattr(active_settings, "rate_limit_chat", 0, raising=False)
    limiter = RateLimiter(
        f"t-{uuid.uuid4().hex}", limit=1, scope="ip", limit_setting="rate_limit_chat"
    )
    req = _request()
    for _ in range(5):  # 0 이하 = 비활성 — 예외 없음
        await limiter(req)


async def test_file_upload_bucket_uses_1000_setting_and_separate_key(active_settings, monkeypatch):
    from biznotes_core.api.v1.sources import _source_create_limiter, _source_upload_limiter

    assert _source_upload_limiter.key == "source-upload"
    assert _source_upload_limiter.window == 60
    assert _source_upload_limiter._effective_limit() == 1000
    assert _source_create_limiter.key == "source-create"
    assert _source_create_limiter._effective_limit() == 30

    monkeypatch.setattr(active_settings, "rate_limit_source_upload", 1)
    limiter = RateLimiter(
        f"source-upload-test-{uuid.uuid4().hex}",
        window_seconds=60,
        scope="user",
        limit_setting="rate_limit_source_upload",
    )
    req = _request()
    await limiter(req)
    with pytest.raises(HTTPException) as exc:
        await limiter(req)
    assert exc.value.status_code == 429


async def test_test_env_bypasses():
    # conftest 기본 상태(app_env=test)에서는 어떤 한도도 적용되지 않는다
    limiter = RateLimiter(f"t-{uuid.uuid4().hex}", limit=1, scope="ip")
    req = _request()
    for _ in range(5):
        await limiter(req)
