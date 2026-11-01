"""프롬프트 버전 관리 API (Phase F) — 템플릿 + 버전 이력 + 롤백.

스코프: 본인 소유(scope=user) + 시스템 공용(scope=system, 읽기). 편집은 소유자만.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.api.deps import get_current_user
from biznotes_core.core.db import get_db
from biznotes_core.models import PromptTemplate, PromptVersion, User

router = APIRouter(prefix="/prompts", tags=["prompts"])


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    content: str = Field(min_length=1)
    system: str = ""
    note: str = "초기 버전"


class VersionCreate(BaseModel):
    content: str = Field(min_length=1)
    system: str = ""
    note: str = ""


class VersionOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    template_id: uuid.UUID
    version: int
    content: str
    system: str
    note: str
    created_at: datetime


class TemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    scope: str
    owner_id: uuid.UUID
    active_version_id: uuid.UUID | None
    active_version: int | None
    version_count: int
    can_edit: bool


async def _resolve_active_version(db: AsyncSession, tpl: PromptTemplate) -> PromptVersion | None:
    if tpl.active_version_id is None:
        return None
    return await db.get(PromptVersion, tpl.active_version_id)


async def _to_out_bulk(
    db: AsyncSession, templates: list[PromptTemplate], user: User
) -> list[TemplateOut]:
    """목록용 — 템플릿마다 쿼리를 날리지 않고 두 번의 벌크 쿼리로 조합한다 (R3-2).

    `_to_out`을 리스트 컴프리헨션으로 돌리면 템플릿당 버전 count 쿼리가 1회씩
    붙어 N+1이 된다(실측: 템플릿 20개 → 22쿼리). `services/notebooks.py`가 쓰는
    벌크 2차 쿼리 관행을 따른다 — 이 코드베이스는 ORM `relationship()`을 쓰지 않는다.
    """
    if not templates:
        return []

    ids = [t.id for t in templates]
    counts: dict[uuid.UUID, int] = {
        tpl_id: n
        for tpl_id, n in (
            await db.execute(
                select(PromptVersion.template_id, func.count())
                .where(PromptVersion.template_id.in_(ids))
                .group_by(PromptVersion.template_id)
            )
        ).all()
    }

    active_ids = [t.active_version_id for t in templates if t.active_version_id is not None]
    actives: dict[uuid.UUID, PromptVersion] = {}
    if active_ids:
        actives = {
            v.id: v
            for v in (
                await db.execute(select(PromptVersion).where(PromptVersion.id.in_(active_ids)))
            ).scalars()
        }

    return [
        TemplateOut(
            id=t.id,
            name=t.name,
            description=t.description,
            scope=t.scope,
            owner_id=t.owner_id,
            active_version_id=t.active_version_id,
            active_version=(
                actives[t.active_version_id].version if t.active_version_id in actives else None
            ),
            version_count=int(counts.get(t.id, 0)),
            can_edit=t.owner_id == user.id,
        )
        for t in templates
    ]


async def _to_out(db: AsyncSession, tpl: PromptTemplate, user: User) -> TemplateOut:
    active = await _resolve_active_version(db, tpl)
    count = (
        await db.execute(
            select(func.count())
            .select_from(PromptVersion)
            .where(PromptVersion.template_id == tpl.id)
        )
    ).scalar() or 0
    return TemplateOut(
        id=tpl.id,
        name=tpl.name,
        description=tpl.description,
        scope=tpl.scope,
        owner_id=tpl.owner_id,
        active_version_id=tpl.active_version_id,
        active_version=active.version if active else None,
        version_count=int(count),
        can_edit=tpl.owner_id == user.id,
    )


async def _get_template(db: AsyncSession, template_id: uuid.UUID, user: User) -> PromptTemplate:
    tpl = await db.get(PromptTemplate, template_id)
    if tpl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt template not found")
    if tpl.scope != "system" and tpl.owner_id != user.id and user.role != "admin":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt template not found")
    return tpl


def _assert_edit(tpl: PromptTemplate, user: User) -> None:
    if tpl.owner_id != user.id and user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "소유자만 편집할 수 있습니다")


@router.get("", response_model=list[TemplateOut])
async def list_templates(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TemplateOut]:
    rows = (
        await db.execute(
            select(PromptTemplate)
            .where(or_(PromptTemplate.owner_id == user.id, PromptTemplate.scope == "system"))
            .order_by(PromptTemplate.created_at.desc())
        )
    ).scalars().all()
    return await _to_out_bulk(db, list(rows), user)


@router.post("", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(
    body: TemplateCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TemplateOut:
    tpl = PromptTemplate(
        name=body.name, description=body.description, owner_id=user.id, scope="user"
    )
    db.add(tpl)
    await db.flush()
    v1 = PromptVersion(
        template_id=tpl.id,
        version=1,
        content=body.content,
        system=body.system,
        note=body.note,
        created_by=user.id,
    )
    db.add(v1)
    await db.flush()
    tpl.active_version_id = v1.id
    await db.commit()
    await db.refresh(tpl)
    return await _to_out(db, tpl, user)


@router.get("/{template_id}/versions", response_model=list[VersionOut])
async def list_versions(
    template_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[VersionOut]:
    await _get_template(db, template_id, user)
    rows = (
        await db.execute(
            select(PromptVersion)
            .where(PromptVersion.template_id == template_id)
            .order_by(PromptVersion.version.desc())
        )
    ).scalars().all()
    return [VersionOut.model_validate(v) for v in rows]


@router.post(
    "/{template_id}/versions", response_model=VersionOut, status_code=status.HTTP_201_CREATED
)
async def add_version(
    template_id: uuid.UUID,
    body: VersionCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VersionOut:
    tpl = await _get_template(db, template_id, user)
    _assert_edit(tpl, user)
    max_v = (
        await db.execute(
            select(func.coalesce(func.max(PromptVersion.version), 0)).where(
                PromptVersion.template_id == template_id
            )
        )
    ).scalar() or 0
    ver = PromptVersion(
        template_id=template_id,
        version=int(max_v) + 1,
        content=body.content,
        system=body.system,
        note=body.note,
        created_by=user.id,
    )
    db.add(ver)
    await db.flush()
    tpl.active_version_id = ver.id  # 새 버전을 활성으로
    await db.commit()
    await db.refresh(ver)
    return VersionOut.model_validate(ver)


@router.post("/{template_id}/rollback/{version_id}", response_model=TemplateOut)
async def rollback(
    template_id: uuid.UUID,
    version_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TemplateOut:
    tpl = await _get_template(db, template_id, user)
    _assert_edit(tpl, user)
    ver = await db.get(PromptVersion, version_id)
    if ver is None or ver.template_id != template_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found")
    tpl.active_version_id = ver.id
    await db.commit()
    await db.refresh(tpl)
    return await _to_out(db, tpl, user)


@router.get("/{template_id}", response_model=TemplateOut)
async def get_template(
    template_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TemplateOut:
    tpl = await _get_template(db, template_id, user)
    return await _to_out(db, tpl, user)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    tpl = await _get_template(db, template_id, user)
    _assert_edit(tpl, user)
    await db.delete(tpl)
    await db.commit()
