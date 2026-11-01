"""노트북 즐겨찾기 — 사용자별 독립(owner 제약 없음, phase-22-design.md §3.2)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.api.deps import get_current_user
from biznotes_core.core.db import get_db
from biznotes_core.models import Notebook, NotebookFavorite, User
from biznotes_core.services.notebooks import get_permission

router = APIRouter(tags=["favorites"])


async def _notebook_readable(db: AsyncSession, notebook_id: uuid.UUID, user: User) -> Notebook:
    nb = await db.get(Notebook, notebook_id)
    if nb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    perm = await get_permission(db, nb, user)
    if perm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    return nb


@router.post("/notebooks/{notebook_id}/favorite", status_code=status.HTTP_204_NO_CONTENT)
async def add_favorite(
    notebook_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _notebook_readable(db, notebook_id, user)
    existing = (
        await db.execute(
            select(NotebookFavorite).where(
                NotebookFavorite.user_id == user.id,
                NotebookFavorite.notebook_id == notebook_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(NotebookFavorite(user_id=user.id, notebook_id=notebook_id))
        await db.commit()


@router.delete("/notebooks/{notebook_id}/favorite", status_code=status.HTTP_204_NO_CONTENT)
async def remove_favorite(
    notebook_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _notebook_readable(db, notebook_id, user)
    existing = (
        await db.execute(
            select(NotebookFavorite).where(
                NotebookFavorite.user_id == user.id,
                NotebookFavorite.notebook_id == notebook_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        await db.delete(existing)
        await db.commit()
