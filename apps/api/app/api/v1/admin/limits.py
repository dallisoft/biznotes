"""어드민 — 사용 제한(채팅 1회 한도·source_load 상한·HITL 승인 대기). 관리자 검사는 상위
`admin_router`가 한다."""

from biznotes_core.api.deps import get_current_user
from biznotes_core.core.db import get_db
from biznotes_core.models import User
from biznotes_core.services.audit import record
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.limits import LimitSettingsOut, LimitSettingsUpdate
from app.services.limits import LIMITS_SETTING_KEY, get_limit_settings, update_limit_settings

router = APIRouter(prefix="/limits")


@router.get("", response_model=LimitSettingsOut)
async def get_limits(db: AsyncSession = Depends(get_db)) -> LimitSettingsOut:
    return LimitSettingsOut(**await get_limit_settings(db))


@router.put("", response_model=LimitSettingsOut)
async def update_limits(
    body: LimitSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> LimitSettingsOut:
    changes = body.model_dump(exclude_none=True)
    if not changes:
        return LimitSettingsOut(**await get_limit_settings(db))
    # 행을 잠그고 보낸 항목만 합친다 — 다른 관리자가 동시에 바꾼 항목을 덮어쓰지 않는다
    before, after = await update_limit_settings(db, changes)
    # 코어 재정의 키(`update_overrides`)와 같은 형태 — 보낸 항목과 적용값 전후
    await record(
        db,
        admin.id,
        "limits.settings_update",
        resource=LIMITS_SETTING_KEY,
        detail={"changes": changes, "before": before, "after": after},
    )
    await db.commit()
    return LimitSettingsOut(**after)
