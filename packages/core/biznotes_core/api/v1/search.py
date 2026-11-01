"""전체 검색 — 접근 가능한 노트북 범위에서 노트북/소스/메모(키워드) + 소스 본문(시맨틱).

mode=keyword(기본): 제목/본문 ILIKE — 팔레트 타이핑 중 즉시 결과.
mode=semantic: pgvector 하이브리드(RRF)로 소스 청크 검색 — 임베딩 비용이 있어
               클라이언트가 명시적으로 트리거할 때만 사용.
mode=hybrid: 둘 다.
"""

import uuid
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.api.deps import get_current_user
from biznotes_core.core.db import get_db
from biznotes_core.core.rate_limit import RateLimiter
from biznotes_core.models import Note, NotebookFavorite, NotebookTag, Source, Tag, User
from biznotes_core.services.notebooks import list_notebooks

if TYPE_CHECKING:
    from biznotes_core.services.embedding_spaces import EmbeddingSpace

router = APIRouter(tags=["search"])

LIMIT_PER_TYPE = 10
SEMANTIC_LIMIT = 8


async def semantic_search(
    db: AsyncSession,
    notebook_ids: list[uuid.UUID],
    q: str,
    limit: int = SEMANTIC_LIMIT,
    spaces: "list[EmbeddingSpace] | None" = None,
) -> list[Any]:
    """접근 가능한 노트북들에 대한 청크 시맨틱 검색 (테스트에서 교체 지점).

    `spaces`는 검색할 임베딩 공간(#7). 생략하면 로컬 기본 공간만 검색한다(기존 동작).
    """
    from biznotes_core.services.embedding_spaces import LOCAL_SPACE, search_spaces

    return await search_spaces(
        db, spaces or [LOCAL_SPACE], q, notebook_ids=notebook_ids, limit=limit
    )


class NotebookHit(BaseModel):
    id: uuid.UUID
    title: str
    emoji: str
    tags: list[str] = []
    is_favorite: bool = False


class SourceHit(BaseModel):
    id: uuid.UUID
    notebook_id: uuid.UUID
    title: str
    type: str


class NoteHit(BaseModel):
    id: uuid.UUID
    notebook_id: uuid.UUID
    title: str
    snippet: str


class ChunkHit(BaseModel):
    chunk_id: str
    notebook_id: uuid.UUID
    source_id: str
    source_title: str
    snippet: str
    score: float


class GlobalSearchOut(BaseModel):
    notebooks: list[NotebookHit]
    sources: list[SourceHit]
    notes: list[NoteHit]
    chunks: list[ChunkHit] = []


def _snippet(content: str, q: str, width: int = 80) -> str:
    idx = content.lower().find(q.lower())
    if idx < 0:
        return content[:width]
    start = max(0, idx - width // 4)
    return ("…" if start > 0 else "") + content[start : start + width]


@router.get(
    "/search",
    response_model=GlobalSearchOut,
    dependencies=[Depends(RateLimiter("search", scope="user", limit_setting="rate_limit_search"))],
)
async def global_search(
    q: str = Query(min_length=1, max_length=200),
    mode: Literal["keyword", "semantic", "hybrid"] = Query(default="keyword"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GlobalSearchOut:
    # 접근 가능한 노트북 (소유 + 공유 + public) — 권한 필터의 기준
    accessible = await list_notebooks(db, user, "all")
    nb_by_id = {nb.id: nb for nb, _ in accessible}
    nb_ids = list(nb_by_id.keys())
    pattern = f"%{q}%"

    chunks: list[ChunkHit] = []
    if mode in ("semantic", "hybrid") and nb_ids:
        from biznotes_core.services.embedding_spaces import spaces_in_scope

        spaces = await spaces_in_scope(db, nb_ids)
        points = await semantic_search(db, nb_ids, q, spaces=spaces)
        source_ids = {p.payload.get("source_id") for p in points if p.payload}
        title_rows = await db.execute(
            select(Source.id, Source.title).where(
                Source.id.in_([uuid.UUID(s) for s in source_ids if s])
            )
        )
        titles = {str(sid): title for sid, title in title_rows.all()}
        chunks = [
            ChunkHit(
                chunk_id=str(p.id),
                notebook_id=uuid.UUID(p.payload["notebook_id"]),
                source_id=p.payload.get("source_id", ""),
                source_title=titles.get(p.payload.get("source_id", ""), "(삭제된 소스)"),
                snippet=" ".join(p.payload.get("text", "").split())[:160],
                score=p.score,
            )
            for p in points
            if p.payload and p.payload.get("notebook_id")
        ]
        if mode == "semantic":
            return GlobalSearchOut(notebooks=[], sources=[], notes=[], chunks=chunks)

    matched_notebooks = [
        nb
        for nb in nb_by_id.values()
        if q.lower() in nb.title.lower() or q.lower() in nb.description.lower()
    ][:LIMIT_PER_TYPE]
    matched_ids = [nb.id for nb in matched_notebooks]
    tag_rows = await db.execute(
        select(NotebookTag.notebook_id, Tag.name)
        .join(Tag, Tag.id == NotebookTag.tag_id)
        .where(NotebookTag.notebook_id.in_(matched_ids))
    )
    tag_map: dict[uuid.UUID, list[str]] = {}
    for nb_id, name in tag_rows.tuples().all():
        tag_map.setdefault(nb_id, []).append(name)
    favorite_rows = await db.execute(
        select(NotebookFavorite.notebook_id).where(
            NotebookFavorite.user_id == user.id,
            NotebookFavorite.notebook_id.in_(matched_ids),
        )
    )
    favorite_ids = set(favorite_rows.scalars().all())
    notebooks = [
        NotebookHit(
            id=nb.id,
            title=nb.title,
            emoji=nb.emoji,
            tags=tag_map.get(nb.id, []),
            is_favorite=nb.id in favorite_ids,
        )
        for nb in matched_notebooks
    ]

    sources: list[SourceHit] = []
    notes: list[NoteHit] = []
    if nb_ids:
        src_rows = await db.execute(
            select(Source)
            .where(Source.notebook_id.in_(nb_ids), Source.title.ilike(pattern))
            .order_by(Source.updated_at.desc())
            .limit(LIMIT_PER_TYPE)
        )
        sources = [
            SourceHit(id=s.id, notebook_id=s.notebook_id, title=s.title, type=s.type)
            for s in src_rows.scalars().all()
        ]

        note_rows = await db.execute(
            select(Note)
            .where(
                Note.notebook_id.in_(nb_ids),
                or_(Note.title.ilike(pattern), Note.content.ilike(pattern)),
            )
            .order_by(Note.updated_at.desc())
            .limit(LIMIT_PER_TYPE)
        )
        notes = [
            NoteHit(
                id=n.id,
                notebook_id=n.notebook_id,
                title=n.title,
                snippet=_snippet(n.content, q),
            )
            for n in note_rows.scalars().all()
        ]

    return GlobalSearchOut(notebooks=notebooks, sources=sources, notes=notes, chunks=chunks)
