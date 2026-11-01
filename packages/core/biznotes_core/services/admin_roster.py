"""활성 관리자 명부 — 마지막 활성 관리자를 잃는 변경을 직렬화해 막는다 (#241).

불변식: 사용자의 `role`·`is_active`를 바꾸거나 사용자 행을 지우는 쓰기는 **모두** 먼저
`lock_admin_roster`를 잡는다. 순서는 잠금 → 요청자 다시 확인(`still_active_admin`, #276) →
대상 다시 읽기(`populate_existing=True`) → `count_other_active_admins` → 쓰기 → 커밋이다. 코어의
관리자 사용자 라우터가 이 순서를 따르고, 같은 쓰기를 하는 제품 라우트·스크립트도 이 함수들을
같은 순서로 써야 한다.

- 대상 행에 `FOR UPDATE`를 걸지 않는다. 잠금이 쓰기를 이미 직렬화하고, 행 잠금은 감사 로그 FK
  검사의 `KEY SHARE`와 엇갈려 교착한다(비밀번호 재설정 × 비활성화).
- READ COMMITTED가 전제다. 잠근 뒤의 새 문장이 앞 요청의 커밋을 본다. REPEATABLE READ 이상에서는
  인증 때의 스냅샷을 읽어 판정이 틀린다.
- 두 관리자가 서로를 동시에 비활성화·강등·삭제해도 한쪽만 성공한다. 진 요청은 잠금을 기다리는 사이
  요청자 권한을 잃었으므로 403이다(#276). 요청자가 활성 관리자로 확인되면 대상을 빼도 요청자가
  남으므로, 「마지막 활성 관리자」 판정은 이 순서에서는 거부하지 않는 안전망이다.
"""

import hashlib
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.config import get_settings
from biznotes_core.models import User

__all__ = ["count_other_active_admins", "lock_admin_roster", "still_active_admin"]


def _admin_roster_lock_key() -> int:
    namespace = get_settings().service_name
    digest = hashlib.sha256(f"{namespace}:admin-roster".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def lock_admin_roster(db: AsyncSession) -> None:
    """활성 관리자 수를 바꿀 수 있는 변경을 커밋·롤백까지 하나씩 처리한다(트랜잭션 잠금).

    호출자는 잠근 **뒤에** 대상 행을 다시 읽어 판정한다. 잠그기 전의 읽기는 그 사이의
    승격·강등을 놓친다.
    """
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _admin_roster_lock_key()})


async def count_other_active_admins(db: AsyncSession, exclude_id: uuid.UUID) -> int:
    """`exclude_id`를 뺀 활성 관리자 수. 비활성 관리자는 세지 않는다.

    세션의 ORM 객체(인증 때 읽은 요청자 행 등)가 아니라 SQL로 센다 — 잠금을 기다리는 동안
    다른 요청이 커밋한 변경을 본다.
    """
    count = await db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.role == "admin", User.is_active.is_(True), User.id != exclude_id)
    )
    return int(count or 0)


async def still_active_admin(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """`user_id`가 지금 활성 관리자인지(#276). 행이 없으면(삭제됨) False다.

    잠근 **뒤에** 요청자를 다시 확인할 때 쓴다. ORM 엔티티가 아니라 열만 읽는다 — 인증 때 세션에
    올라온 요청자 객체(identity map)를 거치지 않고, 잠금을 기다리는 동안 다른 요청이 커밋한
    강등·비활성화·삭제를 본다.
    """
    row = (await db.execute(select(User.role, User.is_active).where(User.id == user_id))).first()
    if row is None:
        return False
    role, is_active = row
    return role == "admin" and is_active is True
