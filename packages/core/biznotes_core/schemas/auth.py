import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from biznotes_core.schemas.enums import UserRole


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    name: str
    role: UserRole
    is_active: bool
    # 켜져 있으면 클라이언트는 비밀번호 변경 화면으로 보낸다(#165)
    must_change_password: bool = False
    created_at: datetime


class TokenResponse(BaseModel):
    """쿠키와 함께 바디로도 반환 — 비브라우저 클라이언트용."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserOut


class PasswordChangeRequest(BaseModel):
    """본인 비밀번호 변경(`POST /auth/password`, #165).

    상한만 스키마로 둔다. 새 비밀번호 규칙은 핸들러가 검사한다 — 스키마 validator로 거부하면
    FastAPI 422 본문이 입력 비밀번호(`input`)를 그대로 돌려준다.
    """

    current_password: str = Field(max_length=128)
    new_password: str = Field(max_length=128)
