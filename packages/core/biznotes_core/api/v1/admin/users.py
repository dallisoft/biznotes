"""어드민 — 사용자/그룹 관리."""

import secrets
import uuid

import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.api.deps import get_current_user
from biznotes_core.api.patch import non_null_changes
from biznotes_core.api.routing import RedactedValidationRoute
from biznotes_core.core.db import get_db
from biznotes_core.core.security import PasswordPolicyError
from biznotes_core.models import Group, User, UserGroup
from biznotes_core.schemas.auth import UserOut
from biznotes_core.schemas.limits import EffectiveUsageLimit, EffectiveUsageLimits
from biznotes_core.services.admin_roster import (
    count_other_active_admins,
    lock_admin_roster,
    still_active_admin,
)
from biznotes_core.services.audit import record
from biznotes_core.services.object_cleanup import delete_objects_best_effort, user_object_refs
from biznotes_core.services.user_passwords import set_password_async

router = APIRouter(route_class=RedactedValidationRoute)

# 관리자 재설정의 임시 비밀번호 생성 시도 횟수 — 제품 정책이 거부할 때만 다시 만든다(#174)
_TEMP_PASSWORD_ATTEMPTS = 5

_LAST_ADMIN_UPDATE = "마지막 활성 관리자는 비활성화하거나 강등할 수 없습니다"
_LAST_ADMIN_DELETE = "마지막 활성 관리자는 삭제할 수 없습니다"
# 잠금을 기다리는 사이 요청자가 권한을 잃었을 때 — require_admin과 같은 문구다(#276)
_ADMIN_REQUIRED = "Admin role required"


def _is_active_admin(role: str, is_active: bool) -> bool:
    return role == "admin" and is_active


class UserCreate(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=100)
    # 하한은 핸들러의 set_password가 검사한다 — 스키마 422는 입력 비밀번호를 되돌려준다(#165)
    password: str = Field(max_length=128)
    role: str = Field(default="user", pattern="^(admin|user)$")


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    role: str | None = Field(default=None, pattern="^(admin|user)$")
    is_active: bool | None = None


class PasswordResetOut(BaseModel):
    temporary_password: str


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)


class GroupOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    member_count: int = 0


class MemberAdd(BaseModel):
    user_id: uuid.UUID
    role: str = Field(default="member", pattern="^(owner|member)$")


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    name: str
    role: str


# ---------- users ----------


@router.get("/users", response_model=list[UserOut])
async def list_users(db: AsyncSession = Depends(get_db)) -> list[UserOut]:
    rows = await db.execute(select(User).order_by(User.created_at))
    return [UserOut.model_validate(u) for u in rows.scalars().all()]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> UserOut:
    existing = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 존재하는 이메일입니다")
    user = User(email=body.email, name=body.name, role=body.role)
    try:
        # 관리자가 정한 비밀번호는 첫 로그인 때 본인이 바꾼다(#165). 72바이트 초과는 bcrypt가
        # ValueError로 500을 내므로 입력값 없는 422로 막는다
        await set_password_async(user, body.password, must_change=True)
    except PasswordPolicyError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_new_password", "reason": e.reason, "detail": str(e)},
        ) from e
    db.add(user)
    await record(db, admin.id, "user.create", body.email)
    await db.commit()
    await db.refresh(user)
    return UserOut.model_validate(user)


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> UserOut:
    changes = body.model_dump(exclude_unset=True)
    if user_id == admin.id:
        # 자기 잠금 방지(#219) — 자기 자신은 변경 뒤에도 활성 관리자로 남아야 한다.
        # 명시적 null도 거부한다. delete_user처럼 DB를 건드리기 전에 고정 문구로 거부한다
        if "is_active" in changes and changes["is_active"] is not True:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "자기 계정은 비활성화할 수 없습니다"
            )
        if "role" in changes and changes["role"] != "admin":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "자기 자신의 관리자 권한은 해제할 수 없습니다",
            )
    # 명시적 null은 대상과 무관하게 422다 — 세 열 모두 NOT NULL이라 커밋 때 500이 된다(#242).
    # 자기 보호 뒤에 두어 #219 문구를 지키고, 입력만으로 판정하므로 DB 조회 전에 거부한다
    changes = non_null_changes(body)
    # 활성 관리자 수를 바꿀 수 있는 변경(승격·활성화 포함)은 하나씩 처리한다(#241).
    # 잠근 뒤에 대상을 다시 읽어야 그 사이의 승격·강등을 놓치지 않는다. 행 잠금(FOR UPDATE)은
    # 걸지 않는다 — 명부 잠금이 이미 직렬화하고, 행 잠금은 감사 로그 FK의 KEY SHARE와 교착한다
    guarded = "role" in changes or "is_active" in changes
    if guarded:
        await lock_admin_roster(db)
        # 기다리는 사이 강등·비활성화·삭제된 요청자는 그 권한으로 바꾸지 못한다 — 자기 재승격도
        # 막는다(#276). 대상 존재(404)를 알려 주기 전에 거부한다
        if not await still_active_admin(db, admin.id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, _ADMIN_REQUIRED)
        user = await db.get(User, user_id, populate_existing=True)
    else:
        user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if (
        guarded
        and _is_active_admin(user.role, user.is_active)
        and not _is_active_admin(
            changes.get("role", user.role), changes.get("is_active", user.is_active)
        )
        and await count_other_active_admins(db, user.id) == 0
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, _LAST_ADMIN_UPDATE)
    for field, value in changes.items():
        setattr(user, field, value)
    await record(db, admin.id, "user.update", user.email, changes)
    await db.commit()
    await db.refresh(user)
    return UserOut.model_validate(user)


@router.post("/users/{user_id}/password-reset", response_model=PasswordResetOut)
async def reset_password(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> PasswordResetOut:
    # 본인 변경(`/auth/password`)과 겹치지 않게 행을 잠근다(#165)
    user = await db.get(User, user_id, with_for_update=True, populate_existing=True)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    # 임시 비밀번호는 첫 로그인 때 바꾸게 하고, 재설정 전 세션은 끊는다(#165).
    # 제품 정책(#174)이 무작위 값을 거부하면 새 값으로 다시 만든다
    for _ in range(_TEMP_PASSWORD_ATTEMPTS):
        temp = secrets.token_urlsafe(9)
        try:
            await set_password_async(user, temp, must_change=True)
            break
        except PasswordPolicyError as e:
            rejected = e.reason
    else:
        import structlog

        structlog.get_logger().error("임시 비밀번호가 정책에 계속 거부됨", reason=rejected)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "password_policy_rejected_temporary"},
        )
    await record(db, admin.id, "user.password_reset", user.email)
    await db.commit()
    return PasswordResetOut(temporary_password=temp)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> None:
    if user_id == admin.id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "자기 자신은 삭제할 수 없습니다")
    # 마지막 활성 관리자 삭제를 막는다 — update_user와 같은 잠금 안에서 대상을 다시 읽는다(#241).
    # 요청자도 잠근 뒤 다시 확인한다(#276)
    await lock_admin_roster(db)
    if not await still_active_admin(db, admin.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, _ADMIN_REQUIRED)
    user = await db.get(User, user_id, populate_existing=True)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if (
        _is_active_admin(user.role, user.is_active)
        and await count_other_active_admins(db, user.id) == 0
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, _LAST_ADMIN_DELETE)
    await record(db, admin.id, "user.delete", user.email)
    # 소유 노트북·실행 run·요청 내보내기는 CASCADE로 지워진다 — 오브젝트 키는 커밋 전에 모은다(#191)
    refs = await user_object_refs(db, user.id)
    await db.delete(user)
    await db.commit()
    await anyio.to_thread.run_sync(delete_objects_best_effort, refs)


# ---------- groups ----------


@router.get("/groups", response_model=list[GroupOut])
async def list_groups(db: AsyncSession = Depends(get_db)) -> list[GroupOut]:
    rows = await db.execute(
        select(Group, func.count(UserGroup.user_id))
        .outerjoin(UserGroup, UserGroup.group_id == Group.id)
        .group_by(Group.id)
        .order_by(Group.name)
    )
    return [
        GroupOut(id=g.id, name=g.name, description=g.description, member_count=cnt)
        for g, cnt in rows.all()
    ]


@router.post("/groups", response_model=GroupOut, status_code=status.HTTP_201_CREATED)
async def create_group(
    body: GroupCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> GroupOut:
    existing = (await db.execute(select(Group).where(Group.name == body.name))).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 존재하는 그룹명입니다")
    group = Group(name=body.name, description=body.description)
    db.add(group)
    await record(db, admin.id, "group.create", body.name)
    await db.commit()
    await db.refresh(group)
    return GroupOut(id=group.id, name=group.name, description=group.description)


@router.delete("/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> None:
    group = await db.get(Group, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    await record(db, admin.id, "group.delete", group.name)
    await db.delete(group)
    await db.commit()


@router.get("/groups/{group_id}/members", response_model=list[MemberOut])
async def list_members(group_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> list[MemberOut]:
    rows = await db.execute(
        select(UserGroup, User)
        .join(User, User.id == UserGroup.user_id)
        .where(UserGroup.group_id == group_id)
    )
    return [
        MemberOut(user_id=u.id, email=u.email, name=u.name, role=ug.role) for ug, u in rows.all()
    ]


@router.post("/groups/{group_id}/members", status_code=status.HTTP_201_CREATED)
async def add_member(
    group_id: uuid.UUID,
    body: MemberAdd,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> dict[str, str]:
    group = await db.get(Group, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    user = await db.get(User, body.user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    existing = await db.get(UserGroup, (body.user_id, group_id))
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 멤버입니다")
    db.add(UserGroup(user_id=body.user_id, group_id=group_id, role=body.role))
    await record(db, admin.id, "group.member_add", f"{group.name}:{user.email}")
    await db.commit()
    return {"status": "added"}


@router.delete("/groups/{group_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> None:
    membership = await db.get(UserGroup, (user_id, group_id))
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Membership not found")
    await record(db, admin.id, "group.member_remove", f"{group_id}:{user_id}")
    await db.delete(membership)
    await db.commit()


# ---------- usage limits (16b) ----------


class UsageLimitsIn(BaseModel):
    """null = 시스템 기본값을 따른다(기본값도 없으면 무제한, #387)."""

    daily_tokens: int | None = Field(default=None, ge=0)
    daily_agent_runs: int | None = Field(default=None, ge=0)


class UsageLimitsOut(UsageLimitsIn):
    today: dict[str, int] = Field(default_factory=dict)  # 오늘 누적 (Redis 가드 카운터 근사치)
    # 필드별 실제 적용 한도와 출처(user·default·unlimited, #387) — 위 필드는 사용자별 값 그대로다
    effective: EffectiveUsageLimits | None = None


async def _effective(db: AsyncSession, user_id: uuid.UUID) -> EffectiveUsageLimits:
    from biznotes_core.services.usage_guard import effective_limits

    resolved = await effective_limits(db, user_id)
    return EffectiveUsageLimits(
        **{
            field: EffectiveUsageLimit(value=value, source=source)
            for field, (value, source) in resolved.items()
        }
    )


@router.get("/users/{user_id}/limits", response_model=UsageLimitsOut)
async def get_user_limits(
    user_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> UsageLimitsOut:
    from biznotes_core.services.usage_guard import get_limits, get_today_usage

    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    limits = await get_limits(db, user_id)
    return UsageLimitsOut(
        daily_tokens=limits.daily_tokens if limits else None,
        daily_agent_runs=limits.daily_agent_runs if limits else None,
        today=await get_today_usage(user_id),
        effective=await _effective(db, user_id),
    )


@router.put("/users/{user_id}/limits", response_model=UsageLimitsOut)
async def put_user_limits(
    user_id: uuid.UUID,
    body: UsageLimitsIn,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UsageLimitsOut:
    from biznotes_core.models import UsageLimit
    from biznotes_core.services.usage_guard import get_limits, get_today_usage

    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    limits = await get_limits(db, user_id)
    if limits is None:
        limits = UsageLimit(user_id=user_id)
        db.add(limits)
    limits.daily_tokens = body.daily_tokens
    limits.daily_agent_runs = body.daily_agent_runs
    await record(db, admin.id, "usage_limits.update", target.email, body.model_dump())
    await db.commit()

    return UsageLimitsOut(
        **body.model_dump(),
        today=await get_today_usage(user_id),
        effective=await _effective(db, user_id),
    )
