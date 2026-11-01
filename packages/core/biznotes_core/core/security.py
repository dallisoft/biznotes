import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import anyio
import bcrypt
import jwt
import structlog
from anyio.lowlevel import RunVar

from biznotes_core.config import get_settings

ALGORITHM = "HS256"
TokenType = Literal["access", "refresh"]
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_BYTES = 72  # bcrypt 입력 상한 — 넘으면 bcrypt 5.x가 ValueError를 낸다
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# 제품 정책이 정하는 reason의 형식 — 응답의 기계 판독 코드다(#174)
_POLICY_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")
# 코어가 쓰는 reason — 제품 정책은 쓸 수 없다(클라이언트가 고정 문구로 매핑한다)
_CORE_REASONS = frozenset({"too_short", "too_long_bytes", "same_as_current", "policy"})
_POLICY_MESSAGE = "비밀번호가 정책에 맞지 않습니다"


class PasswordPolicyError(ValueError):
    """새 비밀번호 규칙 위반. `reason`은 기계가 읽는 코드다 — 메시지에 입력값을 싣지 않는다."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def validate_new_password(plain: str, /, **context: Any) -> str:
    """새 비밀번호 규칙 — 코어 길이 규칙(#165) 뒤에 제품 정책 훅(#174)을 부른다.

    코어 규칙은 8자 이상, UTF-8 72바이트 이하다(`too_short`·`too_long_bytes`). 그다음
    `get_settings().check_password_policy(plain, **context)`가 제품 규칙을 본다(문맥 키는
    `CoreSettings.check_password_policy` 참조). 어기면 `PasswordPolicyError`.

    요청 스키마의 validator로 쓰지 않는다 — FastAPI 422 본문은 입력값(`input`)을
    되돌려주므로, 핸들러에서 불러 입력값 없는 오류로 바꾼다.
    """
    if len(plain) < PASSWORD_MIN_LENGTH:
        raise PasswordPolicyError(
            "too_short", f"비밀번호는 {PASSWORD_MIN_LENGTH}자 이상이어야 합니다"
        )
    if len(plain.encode()) > PASSWORD_MAX_BYTES:
        raise PasswordPolicyError(
            "too_long_bytes", f"비밀번호는 UTF-8 {PASSWORD_MAX_BYTES}바이트 이하여야 합니다"
        )
    try:
        # 기본 구현은 None만 돌려주지만 제품 오버라이드가 무엇을 돌려줄지는 모른다
        verdict = get_settings().check_password_policy(  # type: ignore[func-returns-value]
            plain, **context
        )
    except PasswordPolicyError as e:
        raise _sanitized_policy_error(e, plain) from None
    if verdict is not None:
        # `return False`로 거부하려는 훅을 통과로 바꾸지 않는다
        structlog.get_logger().warning("비밀번호 정책 훅이 None이 아닌 값을 돌려줌 — 거부로 처리")
        raise PasswordPolicyError("policy", _POLICY_MESSAGE)
    return plain


def _sanitized_policy_error(e: PasswordPolicyError, plain: str) -> PasswordPolicyError:
    """제품 정책 오류를 응답에 실을 수 있게 고른다 — 입력값·코어 reason·형식 위반을 걸러낸다."""
    reason, message = e.reason, str(e)
    if (
        not (isinstance(reason, str) and _POLICY_REASON.fullmatch(reason))
        or reason in _CORE_REASONS
        or plain in reason
    ):
        # 원래 값은 로그에도 남기지 않는다 — 훅 버그로 입력값이 섞였을 수 있다
        structlog.get_logger().warning(
            "비밀번호 정책 reason을 policy로 바꿈",
            reason_type=type(reason).__name__,
            reason_len=len(reason) if isinstance(reason, str) else None,
        )
        reason = "policy"
    if not message or plain in message:
        message = _POLICY_MESSAGE
    return PasswordPolicyError(reason, message)


def password_version(changed_at: datetime | None) -> int:
    """토큰 `pwv` 클레임 값 — 마지막 비밀번호 변경 시각의 epoch 마이크로초(없으면 0).

    `iat`는 초 단위로 잘려 변경 시각과 같은 초의 경계를 가를 수 없다. 그래서 발급 당시의
    값을 토큰에 싣고 DB 값과 **일치**하는지 본다. 부동소수 오차가 없게 정수로 계산한다.
    """
    if changed_at is None:
        return 0
    delta = changed_at - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        return False


PasswordKdfPurpose = Literal["login", "write"]
# 이벤트 루프마다 하나 — `CapacityLimiter`는 만든 루프에 묶인다
_PASSWORD_KDF_LIMITERS: dict[PasswordKdfPurpose, RunVar[anyio.CapacityLimiter]] = {
    "login": RunVar("password_kdf_login_limiter"),
    "write": RunVar("password_kdf_write_limiter"),
}


def _password_kdf_limiter(purpose: PasswordKdfPurpose) -> anyio.CapacityLimiter:
    size = get_settings().password_hash_concurrency
    var = _PASSWORD_KDF_LIMITERS[purpose]
    try:
        limiter = var.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(size)
        var.set(limiter)
    # 호출 때 설정을 읽는다 — 제품 설정 등록·변경이 루프 수명과 상관없이 반영된다
    limiter.total_tokens = size
    return limiter


async def run_password_kdf[T](
    func: Callable[..., T], *args: Any, purpose: PasswordKdfPurpose = "write"
) -> T:
    """bcrypt 해시·검증(`func(*args)`)을 이벤트 루프 밖 스레드에서 돈다(#289).

    bcrypt(cost 12, 1회 약 170ms)는 GIL을 놓으므로 스레드에서 돌면 같은 프로세스의 다른 요청이
    기다리지 않는다. 동시 실행은 `password_hash_concurrency`로 묶고, 비인증 로그인(`"login"`)과
    인증된 비밀번호 쓰기(`"write"`)가 상한을 따로 쓴다 — 로그인 폭주 중에도 행 잠금을 쥔 본인 변경·
    관리자 재설정이 그 대기열 뒤에 서지 않는다. 공용 스레드 상한(anyio 기본 40)도 쓰지 않아 다른
    오프로딩(업로드·검색)이 막히지 않는다.

    `abandon_on_cancel`은 쓰지 않는다. 취소돼도 진행 중인 bcrypt는 끝까지 돌고, 그동안 상한 토큰을
    쥔다 — 그래야 상한이 실제 bcrypt 수(CPU 사용)를 정확히 센다. 함수를 인자로 받으므로 호출 측은
    모듈 전역 이름을 호출 시점에 넘긴다(테스트 대역이 그대로 걸린다).
    """
    return await anyio.to_thread.run_sync(func, *args, limiter=_password_kdf_limiter(purpose))


def create_token(user_id: uuid.UUID, token_type: TokenType, role: str, *, pwv: int = 0) -> str:
    """JWT 발급. 사용자 토큰은 `pwv=password_version(user.password_changed_at)`을 넘긴다.

    기본값 0은 `password_changed_at`이 NULL인 사용자에게만 유효하다(#165).
    """
    now = datetime.now(UTC)
    if token_type == "access":
        expires = now + timedelta(minutes=get_settings().jwt_access_expire_minutes)
    else:
        expires = now + timedelta(days=get_settings().jwt_refresh_expire_days)
    payload = {
        "sub": str(user_id),
        "type": token_type,
        "role": role,
        "iat": now,
        "exp": expires,
        "jti": str(uuid.uuid4()),
        "pwv": pwv,
    }
    return jwt.encode(payload, get_settings().jwt_secret, algorithm=ALGORITHM)


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """유효하지 않으면 jwt.InvalidTokenError 계열 예외 발생."""
    payload = jwt.decode(token, get_settings().jwt_secret, algorithms=[ALGORITHM])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"expected {expected_type} token")
    return payload
