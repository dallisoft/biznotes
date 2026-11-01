"""노트북 그룹 공유 행 쓰기 — 같은 그룹에 다시 공유하면 그 행을 갱신한다 (#222).

불변식: 공유 행을 만들거나 바꾸거나 지우는 쓰기는 먼저 `lock_notebook_shares`로 그 노트북의
공유를 잠근다. 잠금은 트랜잭션 범위라 커밋·롤백 때 풀린다. 기존 행은 잠근 **뒤에** 읽는다.

- 공유 행에 `FOR UPDATE`를 걸지 않는다. 같은 노트북의 공유 쓰기는 잠금이 이미 직렬화한다. 행
  잠금은 FK 검사의 `KEY SHARE`와 엇갈려 교착 표면을 만든다(`admin_roster` 참조).
- READ COMMITTED가 전제다. 잠근 뒤의 새 문장이 앞 요청의 커밋을 본다.
- DB 유니크 `uq_share_notebook_group(notebook_id, group_id)`가 중복을 막는다(#278 3단계). 잠금은
  같은 그룹의 동시 공유가 유니크 위반 대신 갱신으로 끝나게 직렬화한다.
"""

import hashlib
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.config import get_settings
from biznotes_core.models import NotebookShare
from biznotes_core.schemas.enums import SharePermission

__all__ = ["lock_notebook_shares", "upsert_notebook_share"]

# 공유 권한 순위 — `resolve_permission`과 같은 순서다. 저장 값은 CHECK가 막는다(#277)
_SHARE_PERMISSION_RANK = {"read": 1, "write": 2}


def _notebook_shares_lock_key(notebook_id: uuid.UUID) -> int:
    namespace = get_settings().service_name
    digest = hashlib.sha256(f"{namespace}:shares:{notebook_id}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def lock_notebook_shares(db: AsyncSession, notebook_id: uuid.UUID) -> None:
    """한 노트북의 공유 쓰기를 커밋·롤백까지 하나씩 처리한다(트랜잭션 잠금)."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:k)"), {"k": _notebook_shares_lock_key(notebook_id)}
    )


async def upsert_notebook_share(
    db: AsyncSession,
    notebook_id: uuid.UUID,
    *,
    group_id: uuid.UUID,
    permission: SharePermission,
    upgrade_only: bool = False,
) -> NotebookShare:
    """같은 그룹 행이 있으면 그 행의 권한을 갱신하고, 없으면 새 행을 더한다(요청 권한).

    - 기본(`upgrade_only=False`): 요청 권한으로 바꾼다(하향 포함, #222).
    - `upgrade_only=True`(#300): 기존 권한과 요청 중 높은 쪽을 남긴다. 잠금 안에서 다시 읽은
      값이라, 호출자의 결정(GET)과 이 쓰기 사이에 다른 요청이 올린 권한을 낮추지 않는다.

    남긴 행을 돌려준다 — 다시 공유해도 `id`가 처음 공유와 같다. 사용자 대상 공유는 없앴다
    (#278 3단계, `user_id` 키워드 제거).

    호출자 계약:
    - `lock_notebook_shares`를 먼저 잡는다. 트랜잭션의 다른 쓰기(행 잠금)보다 앞에 둔다 — 잠금을
      기다리는 동안 행 잠금을 쥐고 있으면 잠금 보유자의 FK `KEY SHARE`와 교착할 수 있다.
    - 노트북 권한·그룹 존재 확인은 호출자 책임이다. 「노트북 실제 소유자가 그 그룹 구성원」인지도
      호출자가 확인한다(#278, 라우터 `share`가 선례다).
    - 커밋 전에 응답을 만든다. 커밋으로 잠금이 풀리면 같은 행이 바로 해제될 수 있다.
    """
    row = (
        await db.execute(
            select(NotebookShare)
            .where(NotebookShare.notebook_id == notebook_id, NotebookShare.group_id == group_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        row = NotebookShare(notebook_id=notebook_id, group_id=group_id, permission=permission)
        db.add(row)
        return row
    if upgrade_only:
        row.permission = max(
            (permission, row.permission), key=_SHARE_PERMISSION_RANK.__getitem__
        )
    else:
        row.permission = permission
    return row
