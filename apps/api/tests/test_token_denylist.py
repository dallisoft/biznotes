"""refresh 토큰 무효화 / jti denylist 테스트 (OPS_CATCHUP_PLAN §3d).

denylist는 Redis를 사용하므로 Redis 미가용 시 모듈 전체를 skip한다
(conftest의 PostgreSQL 미가용 skip 패턴과 동일 — 가용성 우선 degrade 검증은 별개).
"""

import asyncio

import httpx
import pytest
from biznotes_core.core.security import create_token, decode_token
from biznotes_core.core.token_denylist import is_revoked, remaining_seconds, revoke
from redis.asyncio import Redis

from app.core.config import get_settings
from tests.conftest import bearer, login

settings = get_settings()


async def _redis_available() -> bool:
    try:
        r = Redis.from_url(settings.redis_url)
        try:
            await r.ping()
            return True
        finally:
            await r.aclose()
    except Exception:
        return False


@pytest.fixture(scope="module", autouse=True)
async def _require_redis():
    if not await _redis_available():
        pytest.skip("Redis not reachable — denylist tests skipped", allow_module_level=True)
    yield


async def test_logout_revokes_access_token(client: httpx.AsyncClient, user_a):
    """로그아웃 후 동일 access 토큰(Bearer)으로의 요청이 거부된다."""
    access = await login(client, user_a.email)
    # 로그아웃 전에는 통과
    assert (await client.get("/api/v1/auth/me", headers=bearer(access))).status_code == 200

    res = await client.post("/api/v1/auth/logout", headers=bearer(access))
    assert res.status_code == 204

    # 쿠키 삭제와 무관하게 Bearer로 보낸 동일 토큰도 거부 (denylist)
    res = await client.get("/api/v1/auth/me", headers=bearer(access))
    assert res.status_code == 401


async def test_refresh_reuse_detected(client: httpx.AsyncClient, user_a, test_password):
    """refresh 회전 후 이전 refresh 재사용 시도가 거부된다 (도난 신호)."""
    res = await client.post(
        "/api/v1/auth/login", json={"email": user_a.email, "password": test_password}
    )
    old_refresh = res.json()["refresh_token"]

    # 쿠키(refresh)가 Bearer보다 우선하므로 Bearer 토큰을 명시적으로 검증하려면 매번 비운다.
    # 1차 회전: 정상
    client.cookies.clear()
    res = await client.post("/api/v1/auth/refresh", headers=bearer(old_refresh))
    assert res.status_code == 200
    new_refresh = res.json()["refresh_token"]
    assert new_refresh != old_refresh

    # 이전(회전된) refresh 재사용 → 거부
    client.cookies.clear()
    res = await client.post("/api/v1/auth/refresh", headers=bearer(old_refresh))
    assert res.status_code == 401

    # 새 refresh는 여전히 유효
    client.cookies.clear()
    res = await client.post("/api/v1/auth/refresh", headers=bearer(new_refresh))
    assert res.status_code == 200


async def test_revoke_roundtrip_and_zero_ttl_noop(user_a):
    """revoke/is_revoked 왕복 + 잔여 만료시간 0 이하면 등록하지 않음."""
    token = create_token(user_a.id, "access", user_a.role)
    jti = decode_token(token, "access")["jti"]

    assert await is_revoked(jti) is False
    await revoke(jti, 60)
    assert await is_revoked(jti) is True

    # ttl <= 0 (만료 토큰)은 등록 무시
    await revoke("never-stored-jti", 0)
    assert await is_revoked("never-stored-jti") is False

    # 정리
    r = Redis.from_url(settings.redis_url)
    try:
        await r.delete(f"denylist:jti:{jti}")
    finally:
        await r.aclose()


async def test_denylist_ttl_expires(user_a):
    """denylist TTL 만료 후에는 더 이상 차단하지 않는다 (정상 흐름 복귀)."""
    token = create_token(user_a.id, "access", user_a.role)
    jti = decode_token(token, "access")["jti"]
    await revoke(jti, 1)
    assert await is_revoked(jti) is True
    await asyncio.sleep(1.3)
    assert await is_revoked(jti) is False


def test_remaining_seconds():
    assert remaining_seconds({}) == 0
    assert remaining_seconds({"exp": 0}) == 0  # 과거
    future = remaining_seconds({"exp": 2**31})  # 먼 미래
    assert future > 0
