import uuid

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.core.db import get_db
from biznotes_core.core.security import decode_token, password_version
from biznotes_core.core.token_denylist import is_revoked
from biznotes_core.models import User
from biznotes_core.services import rate_limit_settings

ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"
# 강제 비밀번호 변경 대상의 403 `detail.code` — 클라이언트가 변경 화면으로 보낸다(#165)
PASSWORD_CHANGE_REQUIRED = "password_change_required"

# 분당 레이트 리밋의 관리자 재정의(#386)를 이 프로세스의 `RateLimiter`에 연결한다. 모든 인증
# 라우터가 이 모듈을 import하므로 API 프로세스마다 한 번 등록된다(제품 라우터의 리미터 포함)
rate_limit_settings.install_override_provider()


def _extract_token(request: Request) -> str | None:
    """httpOnly 쿠키 우선, Bearer 헤더 폴백 (API 클라이언트/테스트용)."""
    token = request.cookies.get(ACCESS_COOKIE)
    if token:
        return token
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth.removeprefix("Bearer ")
    return None


async def _authenticate(request: Request, db: AsyncSession) -> User:
    """access 토큰 → 사용자. 서명·만료·jti 폐기·활성·비밀번호 버전을 본다(플래그는 보지 않는다)."""
    token = _extract_token(request)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        payload = decode_token(token, "access")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token") from e

    # 로그아웃 등으로 무효화된 토큰 거부 (§3d) — Redis 장애 시 통과(가용성 우선)
    if await is_revoked(payload.get("jti")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token has been revoked")

    user = await db.get(User, uuid.UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive")
    # 비밀번호를 바꾸기 전에 발급된 토큰 거부(#165) — DB 값 기준이라 Redis 장애와 무관하다
    if payload.get("pwv", 0) != password_version(user.password_changed_at):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token invalidated by password change")
    return user


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """인증된 사용자. 비밀번호 변경이 필요한 사용자는 403 `password_change_required`(#165).

    모든 라우터·`require_admin`이 이 의존성을 거치므로 강제 변경에 우회 경로가 없다.
    변경 전에도 열어 둘 경로는 `get_current_user_allow_password_change`를 쓴다.
    """
    user = await _authenticate(request, db)
    if user.must_change_password:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"code": PASSWORD_CHANGE_REQUIRED, "detail": "비밀번호를 변경해야 합니다"},
        )
    return user


async def get_current_user_allow_password_change(
    request: Request, db: AsyncSession = Depends(get_db)
) -> User:
    """강제 변경 대상도 통과시키는 인증 — 허용 목록(`/auth/me`, `/auth/password`) 전용(#165).

    일반 라우트에 쓰지 않는다. 쓰면 그 라우트가 강제 변경 게이트를 우회한다.
    """
    return await _authenticate(request, db)


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return user


async def get_user_group_ids(db: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    from biznotes_core.models import UserGroup

    rows = await db.execute(select(UserGroup.group_id).where(UserGroup.user_id == user_id))
    return list(rows.scalars().all())
