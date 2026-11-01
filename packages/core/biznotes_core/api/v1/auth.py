import functools
import secrets
import uuid

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.api.deps import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    get_current_user_allow_password_change,
)
from biznotes_core.api.routing import RedactedValidationRoute
from biznotes_core.config import get_settings
from biznotes_core.core.db import get_db
from biznotes_core.core.rate_limit import RateLimiter
from biznotes_core.core.security import (
    PasswordPolicyError,
    create_token,
    decode_token,
    hash_password,
    password_version,
    run_password_kdf,
    verify_password,
)
from biznotes_core.core.token_denylist import is_revoked, remaining_seconds, revoke
from biznotes_core.models import User
from biznotes_core.schemas.auth import LoginRequest, PasswordChangeRequest, TokenResponse, UserOut
from biznotes_core.services.audit import record
from biznotes_core.services.user_passwords import set_password_async

router = APIRouter(prefix="/auth", tags=["auth"], route_class=RedactedValidationRoute)


@functools.cache
def _dummy_password_hash() -> str:
    """없는 이메일 로그인이 검증하는 해시(#270). 프로세스에서 한 번 만든다.

    실제 사용자 해시와 같은 `hash_password`로 만들어 cost가 늘 같다. 평문은 버린다. import 시점이
    아니라 지연 생성이다(코어 관례 — cost가 설정을 읽게 돼도 등록 전에 굳지 않는다). `login`이
    조회 결과와 상관없이 맨 앞에서 부르므로, 첫 요청이 치르는 생성 비용도 경로에 따라 갈리지 않는다.
    """
    return hash_password(secrets.token_urlsafe(16))


def _issue_tokens(user: User) -> tuple[str, str]:
    """access·refresh 발급 — 현재 비밀번호 버전(`pwv`)을 싣는다(#165)."""
    pwv = password_version(user.password_changed_at)
    return (
        create_token(user.id, "access", user.role, pwv=pwv),
        create_token(user.id, "refresh", user.role, pwv=pwv),
    )


def _set_auth_cookies(response: Response, access: str, refresh: str) -> None:
    secure = get_settings().app_env == "prod"
    response.set_cookie(
        ACCESS_COOKIE,
        access,
        max_age=get_settings().jwt_access_expire_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh,
        max_age=get_settings().jwt_refresh_expire_days * 86400,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/api/v1/auth",  # refresh 토큰은 auth 엔드포인트에만 전송
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    dependencies=[Depends(RateLimiter("login", limit=10, window_seconds=60))],
)
async def login(
    body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    # bcrypt는 이벤트 루프 밖 스레드에서 돈다(#289). 조회 전, 조건 없이 — 생성 비용도 경로와
    # 무관하다
    dummy_hash = await run_password_kdf(_dummy_password_hash, purpose="login")
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    # bcrypt를 기다리는 동안 풀 연결을 쥐지 않는다. close는 객체를 분리하되 적재된 값은 남긴다
    # (`expire_on_commit`과 상관없다). 두 경로가 같은 close를 지나 시간 균형이 유지된다
    await db.close()
    # 없는 이메일도 같은 비용의 bcrypt 검증을 한 번 치른다 — 응답 시간으로 가입 여부를 가를 수
    # 없게 한다(#270). 더미 검증의 결과는 판정에 쓰지 않는다.
    hashed = user.password_hash if user is not None else dummy_hash
    password_ok = await run_password_kdf(verify_password, body.password, hashed, purpose="login")
    if user is None or not password_ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is disabled")

    access, refresh = _issue_tokens(user)
    _set_auth_cookies(response, access, refresh)
    return TokenResponse(
        access_token=access, refresh_token=refresh, user=UserOut.model_validate(user)
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else None
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token missing")
    try:
        payload = decode_token(token, "refresh")
    except pyjwt.InvalidTokenError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token") from e

    # 재사용 탐지(§3d): 이미 회전/무효화된 refresh가 다시 오면 도난 신호 → 거부
    if await is_revoked(payload.get("jti")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token has been revoked")

    user = await db.get(User, uuid.UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive")
    # 비밀번호 변경 전에 발급된 refresh 거부(#165). 강제 변경 대상도 회전은 허용한다 —
    # 변경 화면에서 세션을 유지해야 하고, 다른 API는 get_current_user가 계속 막는다.
    if payload.get("pwv", 0) != password_version(user.password_changed_at):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token invalidated by password change")

    # 회전: 사용된 refresh 토큰을 즉시 무효화 (TTL=잔여 만료시간)
    await revoke(payload.get("jti"), remaining_seconds(payload))

    access, new_refresh = _issue_tokens(user)
    _set_auth_cookies(response, access, new_refresh)
    return TokenResponse(
        access_token=access, refresh_token=new_refresh, user=UserOut.model_validate(user)
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response) -> None:
    # 쿠키 삭제 + 현재 access·refresh 토큰의 jti를 denylist 등록 (§3d).
    # 토큰이 만료/누락이면 revoke가 무시한다.
    access_token = request.cookies.get(ACCESS_COOKIE)
    if not access_token:
        auth = request.headers.get("Authorization", "")
        access_token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else None
    for raw, ttype in ((access_token, "access"), (request.cookies.get(REFRESH_COOKIE), "refresh")):
        if not raw:
            continue
        try:
            claims = decode_token(raw, ttype)  # type: ignore[arg-type]
        except pyjwt.InvalidTokenError:
            continue
        await revoke(claims.get("jti"), remaining_seconds(claims))

    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user_allow_password_change)) -> UserOut:
    # 강제 변경 대상도 읽는다 — 클라이언트가 `must_change_password`로 변경 화면을 띄운다(#165)
    return UserOut.model_validate(user)


@router.post(
    "/password",
    response_model=TokenResponse,
    dependencies=[
        Depends(RateLimiter("password_change", limit=5, window_seconds=60, scope="user"))
    ],
)
async def change_password(
    body: PasswordChangeRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_allow_password_change),
) -> TokenResponse:
    """본인 비밀번호 변경(#165). 성공하면 강제 변경 플래그를 내리고, 그 전에 발급된 모든
    토큰(다른 기기 포함)을 무효로 한다. 현재 클라이언트에는 새 토큰을 발급한다.

    판정 순서: 스키마 상한 → 현재 비밀번호(400) → 새 비밀번호 규칙(422). 오류 본문에
    입력 비밀번호를 싣지 않는다.
    """
    # 관리자 재설정·다른 변경 요청과 겹치지 않게 행을 잠그고 다시 읽는다. 인증 뒤에 비밀번호가
    # 바뀌었다면 이 요청의 토큰은 이미 무효다 — 재설정(유출 대응)을 덮어쓰지 않는다.
    seen = password_version(user.password_changed_at)
    locked = await db.execute(
        select(User)
        .where(User.id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    user = locked.scalar_one()
    if password_version(user.password_changed_at) != seen:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token invalidated by password change")

    if not await run_password_kdf(verify_password, body.current_password, user.password_hash):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_current_password",
                "detail": "현재 비밀번호가 올바르지 않습니다",
            },
        )
    if body.new_password == body.current_password:
        raise _invalid_new_password(
            "same_as_current", "새 비밀번호는 현재 비밀번호와 달라야 합니다"
        )
    try:
        await set_password_async(user, body.new_password, must_change=False)
    except PasswordPolicyError as e:
        raise _invalid_new_password(e.reason, str(e)) from e
    await record(db, user.id, "user.password_change", user.email)
    await db.commit()

    access, refresh = _issue_tokens(user)
    _set_auth_cookies(response, access, refresh)
    return TokenResponse(
        access_token=access, refresh_token=refresh, user=UserOut.model_validate(user)
    )


def _invalid_new_password(reason: str, message: str) -> HTTPException:
    """입력값 없는 422 — `reason`: too_short·too_long_bytes·same_as_current·제품 정책 값(#174)."""
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"code": "invalid_new_password", "reason": reason, "detail": message},
    )
