import uuid

from biznotes_core.api.deps import get_current_user
from biznotes_core.api.v1.sources import _notebook_with_permission
from biznotes_core.core.db import get_db
from biznotes_core.models import Note, Notebook, Source, User
from biznotes_core.schemas.source import SourceOut
from biznotes_core.services.ingestion.enqueue import (
    ensure_source_capacity,
    ingest_uow,
    stage_ingest_job,
)
from biznotes_core.services.notebooks import get_permission
from biznotes_core.services.storage import ensure_buckets, put_object
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.schemas.note import NoteCreate, NoteOut, NoteUpdate
from app.services.provenance import ORIGIN_REF_INVALID_DETAIL, bind_note_origin, note_out

router = APIRouter(tags=["notes"])
settings = get_settings()


@router.get("/notebooks/{notebook_id}/notes", response_model=list[NoteOut])
async def list_notes(
    notebook_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[NoteOut]:
    await _notebook_with_permission(db, notebook_id, user, need_write=False)
    rows = await db.execute(
        select(Note).where(Note.notebook_id == notebook_id).order_by(Note.updated_at.desc())
    )
    return [note_out(n) for n in rows.scalars().all()]


@router.post(
    "/notebooks/{notebook_id}/notes",
    response_model=NoteOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_note(
    notebook_id: uuid.UUID,
    body: NoteCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NoteOut:
    await _notebook_with_permission(db, notebook_id, user, need_write=True)
    try:
        content, citations, provenance = await bind_note_origin(
            db, notebook_id, user.id, body, user_role=user.role
        )
    except ValueError as e:
        # D7: detail은 항상 이 상수 하나 — bind_note_origin이 무엇을 던지든(예: 내부
        # 스키마 검증 실패) 예외 문자열을 그대로 노출하지 않는다(B-02, 정보 최소 노출).
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, ORIGIN_REF_INVALID_DETAIL) from e
    note = Note(
        notebook_id=notebook_id,
        author_id=user.id,
        title=body.title,
        content=content,
        origin=body.origin,
        origin_ref=body.origin_ref,
        citations=citations,
        provenance=provenance,
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)

    return note_out(note)


async def _get_note_with_notebook(
    db: AsyncSession, note_id: uuid.UUID, user: User
) -> tuple[Note, Notebook, str]:
    note = await db.get(Note, note_id)
    if note is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    nb = await db.get(Notebook, note.notebook_id)
    if nb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    perm = await get_permission(db, nb, user)
    if perm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    return note, nb, perm


def _require_note_edit(note: Note, nb: Notebook, user: User) -> None:
    """수정·삭제는 작성자 또는 노트북 소유자만."""
    if note.author_id != user.id and nb.owner_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "작성자 또는 노트북 소유자만 가능합니다")


@router.get("/notes/{note_id}", response_model=NoteOut)
async def get_note(
    note_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NoteOut:
    note, _, _ = await _get_note_with_notebook(db, note_id, user)
    return note_out(note)


@router.patch("/notes/{note_id}", response_model=NoteOut)
async def update_note(
    note_id: uuid.UUID,
    body: NoteUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NoteOut:
    note, nb, _ = await _get_note_with_notebook(db, note_id, user)
    _require_note_edit(note, nb, user)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(note, field, value)
    await db.commit()
    await db.refresh(note)
    return note_out(note)


@router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    note_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    note, nb, _ = await _get_note_with_notebook(db, note_id, user)
    _require_note_edit(note, nb, user)
    await db.delete(note)
    await db.commit()


@router.post(
    "/notes/{note_id}/convert-to-source",
    response_model=SourceOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def convert_to_source(
    note_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SourceOut:
    """메모를 텍스트 소스로 변환 — 기존 텍스트 인제스천 파이프라인 재사용."""
    note, nb, perm = await _get_note_with_notebook(db, note_id, user)
    if perm not in ("owner", "write"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Write permission required")
    if not note.content.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "메모 내용이 비어 있습니다")
    text = f"# {note.title}\n\n{note.content}"
    object_key: str | None = None
    try:
        async with ingest_uow(db) as uow:
            await ensure_source_capacity(db, nb.id)
            source = Source(
                notebook_id=nb.id,
                type="text",
                title=note.title[:500],
                size_bytes=len(text.encode()),
                status="pending",
            )
            db.add(source)
            await db.flush()
            ensure_buckets()
            object_key = f"{source.id}/raw.txt"
            put_object(
                settings.storage_bucket_sources,
                object_key,
                text.encode("utf-8"),
                content_type="text/plain",
            )
            source.original_object_key = object_key
            stage_ingest_job(db, source, uow)
    except Exception:
        if object_key:
            from biznotes_core.services.storage import delete_object

            try:
                delete_object(settings.storage_bucket_sources, object_key)
            except Exception:
                import structlog

                structlog.get_logger().warning(
                    "메모 변환 실패 후 원본 오브젝트 보상 삭제 실패", object_key=object_key
                )
        raise

    await db.refresh(source)
    return SourceOut.model_validate(source)
