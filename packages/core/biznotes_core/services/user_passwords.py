"""사용자 비밀번호 쓰기의 단일 지점 (#165).

토큰 무효화(`pwv`)는 `password_changed_at`만 본다. 그래서 `password_hash`를 바꾸는 곳은 반드시
그 시각과 강제 변경 플래그를 함께 바꿔야 한다 — 코어 라우터와 제품 CLI·시드는 이 함수만 쓴다.
커밋은 호출 측 트랜잭션이 한다.
"""

from datetime import UTC, datetime

from biznotes_core.core.security import hash_password, run_password_kdf, validate_new_password
from biznotes_core.models import User


def set_password(user: User, plain: str, *, must_change: bool) -> None:
    """비밀번호를 바꾸고 그 전에 발급된 토큰을 무효로 한다.

    `must_change=True`는 관리자가 정해 전달한 비밀번호(생성·재설정)다 — 다음 로그인 때
    본인이 바꾼다. 규칙을 어기면 `PasswordPolicyError`. 동기 함수다 — 제품 CLI·시드용이고,
    async 핸들러는 `set_password_async`를 쓴다.
    """
    validate_new_password(plain, email=user.email, must_change=must_change)
    _apply(user, hash_password(plain), must_change=must_change)


async def set_password_async(user: User, plain: str, *, must_change: bool) -> None:
    """`set_password`와 같다. bcrypt만 이벤트 루프 밖 스레드에서 돈다(#289).

    규칙 검사(제품 훅 포함)는 루프에서 먼저 한다 — 거부되면 해시를 만들지 않는다.
    """
    validate_new_password(plain, email=user.email, must_change=must_change)
    _apply(user, await run_password_kdf(hash_password, plain), must_change=must_change)


def _apply(user: User, password_hash: str, *, must_change: bool) -> None:
    user.password_hash = password_hash
    user.must_change_password = must_change
    # Python 값으로 넣는다 — server now()는 flush 뒤 만료돼 커밋 직후 `pwv` 발급에 쓸 수 없다
    user.password_changed_at = datetime.now(UTC)


def require_password_change(user: User) -> None:
    """비밀번호는 그대로 두고 강제 변경을 켠다. 기존 세션도 끊는다(그 세션으로 바꾸지 못하게)."""
    user.must_change_password = True
    user.password_changed_at = datetime.now(UTC)
