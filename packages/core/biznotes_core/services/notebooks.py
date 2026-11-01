"""노트북 접근 권한 및 목록 조회 로직."""

import uuid
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.models import Notebook, NotebookShare, User, UserGroup
from biznotes_core.services.authz import Permission, is_actual_owner, resolve_permission

__all__ = [
    "Permission",
    "create_notebook",
    "get_permission",
    "is_actual_owner",
    "list_notebooks",
    "user_group_ids",
]


async def user_group_ids(db: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await db.execute(select(UserGroup.group_id).where(UserGroup.user_id == user_id))
    return list(rows.scalars().all())


def _share_filter(group_ids: list[uuid.UUID]):
    """요청자 소속 그룹의 공유 — 공유 대상은 그룹 하나다(#278 3단계). 빈 목록은 거짓 식이다."""
    return NotebookShare.group_id.in_(group_ids)


async def create_notebook(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    title: str,
    emoji: str | None = None,
    description: str = "",
) -> Notebook:
    """노트북을 만든다 — flush만 하고 커밋하지 않는다(트랜잭션은 호출자가 소유한다, #403).

    `emoji`가 없으면 모델 기본값이다. 생성 라우터(`api/v1/notebooks.py`)는 아직 인라인으로 만든다.
    """
    fields: dict[str, str] = {"emoji": emoji} if emoji else {}
    notebook = Notebook(owner_id=owner_id, title=title, description=description, **fields)
    db.add(notebook)
    await db.flush()
    return notebook


async def get_permission(db: AsyncSession, notebook: Notebook, user: User) -> Permission | None:
    """None = 접근 불가.

    공유 권한만 DB에서 로드하고, 실제 판정(소유자·admin 승격·share·public)은
    services.authz.resolve_permission 순수 함수에 위임한다(R1-5). 실제 소유자와 admin
    승격을 구분해야 하는 곳은 authz.is_actual_owner를 직접 쓴다.
    """
    # 소유자/admin은 share 조회 없이 즉시 판정 — 불필요한 쿼리 회피(기존 동작 유지).
    if is_actual_owner(notebook.owner_id, user.id) or user.role == "admin":
        return resolve_permission(
            owner_id=notebook.owner_id,
            visibility=notebook.visibility,
            user_id=user.id,
            user_role=user.role,
            share_perms=set(),
        )

    gids = await user_group_ids(db, user.id)
    rows = await db.execute(
        select(NotebookShare.permission).where(
            NotebookShare.notebook_id == notebook.id,
            _share_filter(gids),
        )
    )
    return resolve_permission(
        owner_id=notebook.owner_id,
        visibility=notebook.visibility,
        user_id=user.id,
        user_role=user.role,
        share_perms=set(rows.scalars().all()),
    )


async def list_notebooks(
    db: AsyncSession, user: User, filter_: Literal["all", "mine", "shared"]
) -> list[tuple[Notebook, Permission]]:
    gids = await user_group_ids(db, user.id)

    shared_ids_q = select(NotebookShare.notebook_id).where(_share_filter(gids))

    if filter_ == "mine":
        q = select(Notebook).where(Notebook.owner_id == user.id)
    elif filter_ == "shared":
        q = select(Notebook).where(Notebook.id.in_(shared_ids_q), Notebook.owner_id != user.id)
    else:  # all = 내 것 + 공유받은 것 + public
        q = select(Notebook).where(
            or_(
                Notebook.owner_id == user.id,
                Notebook.id.in_(shared_ids_q),
                Notebook.visibility == "public",
            )
        )
    q = q.order_by(Notebook.updated_at.desc())
    notebooks = list((await db.execute(q)).scalars().all())

    # 권한 일괄 계산 (N+1 방지: 공유 행을 한 번에 로드)
    perm_map: dict[uuid.UUID, set[str]] = {}
    if notebooks:
        share_rows = await db.execute(
            select(NotebookShare.notebook_id, NotebookShare.permission).where(
                NotebookShare.notebook_id.in_([n.id for n in notebooks]),
                _share_filter(gids),
            )
        )
        for nb_id, perm in share_rows.all():
            perm_map.setdefault(nb_id, set()).add(perm)

    # 단건 판정(get_permission)과 같은 규칙이다 — admin은 목록에서도 owner다(#221).
    # filter 범위는 실제 관계로 정하고, 실제 소유 여부는 응답의 is_owner가 따로 싣는다.
    # 판정이 None인 행은 단건처럼 빠진다(공유 권한 값은 `ck_share_permission`이 read·write로 막는다)
    result: list[tuple[Notebook, Permission]] = []
    for nb in notebooks:
        perm = resolve_permission(
            owner_id=nb.owner_id,
            visibility=nb.visibility,
            user_id=user.id,
            user_role=user.role,
            share_perms=perm_map.get(nb.id, set()),
        )
        if perm is not None:
            result.append((nb, perm))
    return result
