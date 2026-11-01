"""태그 CRUD 및 노트북 부착 — owner 전용(phase-22-design.md §3.1 A안, D-19 확정)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.api.deps import get_current_user
from biznotes_core.core.db import get_db
from biznotes_core.models import Notebook, NotebookTag, Tag, User
from biznotes_core.schemas.tag import TagCreate, TagOut
from biznotes_core.services.notebooks import get_permission, is_actual_owner

router = APIRouter(tags=["tags"])


@router.get("/tags", response_model=list[TagOut])
async def list_tags(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TagOut]:
    from sqlalchemy import select

    rows = await db.execute(
        select(Tag).where(Tag.owner_id == user.id).order_by(Tag.name)
    )
    return [TagOut.model_validate(t) for t in rows.scalars().all()]


@router.post("/tags", response_model=TagOut, status_code=status.HTTP_201_CREATED)
async def create_tag(
    body: TagCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TagOut:
    tag = Tag(owner_id=user.id, name=body.name)
    db.add(tag)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 존재하는 태그입니다") from e
    await db.refresh(tag)
    return TagOut.model_validate(tag)


@router.delete("/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tag(
    tag_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    tag = await db.get(Tag, tag_id)
    if tag is None or tag.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tag not found")
    await db.delete(tag)
    await db.commit()


async def _notebook_owner_only(db: AsyncSession, notebook_id: uuid.UUID, user: User) -> Notebook:
    """태그 부착/해제는 노트북의 실제 소유자만 — `get_permission()`은 admin을 owner로
    간주하므로 여기서 그대로 쓰면 admin이 자신의 태그를 남의 노트북에 붙일 수 있다
    (codex 리뷰 지적, A안 불변식 위반). 통과 여부는 실제 소유자 비교로 정한다.

    소유자가 아니면 `get_permission()`은 존재 노출 여부만 가린다 — 무관 사용자는 단건 GET과
    같은 404, 권한자(admin 포함)는 403이다(#269).
    """
    nb = await db.get(Notebook, notebook_id)
    if nb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    if not is_actual_owner(nb.owner_id, user.id):
        if await get_permission(db, nb, user) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Owner permission required")
    return nb


@router.post(
    "/notebooks/{notebook_id}/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def attach_tag(
    notebook_id: uuid.UUID,
    tag_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _notebook_owner_only(db, notebook_id, user)
    tag = await db.get(Tag, tag_id)
    if tag is None or tag.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tag not found")

    from sqlalchemy import select

    existing = (
        await db.execute(
            select(NotebookTag).where(
                NotebookTag.notebook_id == notebook_id, NotebookTag.tag_id == tag_id
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(NotebookTag(notebook_id=notebook_id, tag_id=tag_id))
        await db.commit()


@router.delete(
    "/notebooks/{notebook_id}/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def detach_tag(
    notebook_id: uuid.UUID,
    tag_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _notebook_owner_only(db, notebook_id, user)

    from sqlalchemy import select

    link = (
        await db.execute(
            select(NotebookTag).where(
                NotebookTag.notebook_id == notebook_id, NotebookTag.tag_id == tag_id
            )
        )
    ).scalar_one_or_none()
    if link is not None:
        await db.delete(link)
        await db.commit()
