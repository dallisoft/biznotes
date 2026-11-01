"""내 그룹 조회 — `/me/groups` (#278).

노트북 그룹 공유의 대상 선택용이다. 요청자 자신의 소속만 준다(admin도 같다 — 전체 그룹은
관리자 API를 쓴다). 그룹 생성·구성원 관리는 관리자 API만 한다.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.api.deps import get_current_user
from biznotes_core.core.db import get_db
from biznotes_core.models import Group, User, UserGroup
from biznotes_core.schemas.group import MyGroupOut

router = APIRouter(tags=["groups"])


@router.get("/me/groups", response_model=list[MyGroupOut])
async def list_my_groups(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MyGroupOut]:
    """요청자가 속한 그룹(id·이름)을 이름순으로 준다."""
    rows = await db.execute(
        select(Group.id, Group.name)
        .join(UserGroup, UserGroup.group_id == Group.id)
        .where(UserGroup.user_id == user.id)
        .order_by(Group.name, Group.id)
    )
    return [MyGroupOut(id=gid, name=name) for gid, name in rows.all()]
