import uuid
from typing import Literal

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.api.deps import get_current_user
from biznotes_core.api.patch import non_null_changes
from biznotes_core.core.db import get_db
from biznotes_core.models import (
    Notebook,
    NotebookFavorite,
    NotebookShare,
    NotebookTag,
    Tag,
    User,
    UserGroup,
)
from biznotes_core.schemas.notebook import (
    NotebookCreate,
    NotebookOut,
    NotebookOverviewOut,
    NotebookShareOut,
    NotebookShareRequest,
    NotebookUpdate,
)
from biznotes_core.services.notebook_shares import lock_notebook_shares, upsert_notebook_share
from biznotes_core.services.notebooks import get_permission, list_notebooks
from biznotes_core.services.object_cleanup import delete_objects_best_effort, notebook_object_refs

router = APIRouter(prefix="/notebooks", tags=["notebooks"])


def _to_out(
    nb: Notebook,
    permission: str,
    user_id: uuid.UUID,
    source_count: int = 0,
    tags: list[str] | None = None,
    is_favorite: bool = False,
) -> NotebookOut:
    out = NotebookOut.model_validate(nb)
    out.is_owner = nb.owner_id == user_id
    out.permission = permission  # type: ignore[assignment]
    out.source_count = source_count
    out.tags = tags or []
    out.is_favorite = is_favorite
    return out


async def _source_counts(db: AsyncSession, notebook_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    from sqlalchemy import func, select

    from biznotes_core.models import Source

    if not notebook_ids:
        return {}
    rows = await db.execute(
        select(Source.notebook_id, func.count())
        .where(Source.notebook_id.in_(notebook_ids))
        .group_by(Source.notebook_id)
    )
    return {nb_id: cnt for nb_id, cnt in rows.tuples().all()}


async def _tag_map(
    db: AsyncSession, notebook_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """노트북별 부착 태그명 — owner 소유 태그만 존재할 수 있으므로(A안) join으로 충분."""
    from sqlalchemy import select

    if not notebook_ids:
        return {}
    rows = await db.execute(
        select(NotebookTag.notebook_id, Tag.name)
        .join(Tag, Tag.id == NotebookTag.tag_id)
        .where(NotebookTag.notebook_id.in_(notebook_ids))
        .order_by(Tag.name)
    )
    result: dict[uuid.UUID, list[str]] = {}
    for nb_id, name in rows.tuples().all():
        result.setdefault(nb_id, []).append(name)
    return result


async def _favorite_set(
    db: AsyncSession, user_id: uuid.UUID, notebook_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    from sqlalchemy import select

    if not notebook_ids:
        return set()
    rows = await db.execute(
        select(NotebookFavorite.notebook_id).where(
            NotebookFavorite.user_id == user_id,
            NotebookFavorite.notebook_id.in_(notebook_ids),
        )
    )
    return set(rows.scalars().all())


@router.get("", response_model=list[NotebookOut])
async def list_(
    # 범위 규칙을 계약으로 적는다(#221) — 승격은 행 권한에만 반영하고 범위는 넓히지 않는다
    filter: Literal["all", "mine", "shared"] = Query(
        default="all",
        description=(
            "목록 범위 — 실제 관계로 정한다. mine: 소유, shared: 공유받음(소유 제외), "
            "all: 소유·공유·public. admin도 범위는 같고, 승격은 각 행의 permission 값에만 "
            "반영된다"
        ),
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[NotebookOut]:
    rows = await list_notebooks(db, user, filter)
    nb_ids = [nb.id for nb, _ in rows]
    counts = await _source_counts(db, nb_ids)
    tag_map = await _tag_map(db, nb_ids)
    favorites = await _favorite_set(db, user.id, nb_ids)
    return [
        _to_out(
            nb,
            perm,
            user.id,
            counts.get(nb.id, 0),
            tag_map.get(nb.id, []),
            nb.id in favorites,
        )
        for nb, perm in rows
    ]


@router.post("", response_model=NotebookOut, status_code=status.HTTP_201_CREATED)
async def create(
    body: NotebookCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotebookOut:
    nb = Notebook(
        owner_id=user.id, title=body.title, emoji=body.emoji, description=body.description
    )
    db.add(nb)
    await db.commit()
    await db.refresh(nb)
    return _to_out(nb, "owner", user.id)


async def _get_notebook_or_404(db: AsyncSession, notebook_id: uuid.UUID) -> Notebook:
    nb = await db.get(Notebook, notebook_id)
    if nb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    return nb


@router.get("/{notebook_id}", response_model=NotebookOut)
async def get(
    notebook_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotebookOut:
    nb = await _get_notebook_or_404(db, notebook_id)
    perm = await get_permission(db, nb, user)
    if perm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    counts = await _source_counts(db, [nb.id])
    tag_map = await _tag_map(db, [nb.id])
    favorites = await _favorite_set(db, user.id, [nb.id])
    return _to_out(
        nb, perm, user.id, counts.get(nb.id, 0), tag_map.get(nb.id, []), nb.id in favorites
    )


@router.get("/{notebook_id}/overview", response_model=NotebookOverviewOut)
async def get_overview(
    notebook_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotebookOverviewOut:
    """노트북 요약 + 추천 질문. stale이면 백그라운드 재생성을 예약한다."""
    from sqlalchemy import select

    from biznotes_core.models import NotebookOverview
    from biznotes_core.services.overview import compute_staleness

    nb = await _get_notebook_or_404(db, notebook_id)
    perm = await get_permission(db, nb, user)
    if perm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")

    overview = (
        await db.execute(
            select(NotebookOverview).where(NotebookOverview.notebook_id == notebook_id)
        )
    ).scalar_one_or_none()
    stale = await compute_staleness(db, notebook_id, overview)

    # 수동 재생성 진행 중이면 stale로 보고 — 클라이언트 폴링이 완료를 감지하게 한다
    regen_running = False
    if not stale:
        from redis.asyncio import Redis

        from biznotes_core.config import get_settings

        redis = Redis.from_url(get_settings().redis_url)
        try:
            regen_running = bool(await redis.exists(f"overview:regen:{notebook_id}"))
        finally:
            await redis.aclose()

    if stale:
        from biznotes_core.services.overview_refresh import schedule_overview_refresh

        # 조회 경로는 디바운스 토큰을 갱신하지 않음 — 폴링이 예약을 무효화하지 않도록
        schedule_overview_refresh(str(notebook_id), debounce=False)

    return NotebookOverviewOut(
        summary=overview.summary if overview and overview.generated_at else None,
        suggested_questions=(
            list(overview.suggested_questions) if overview and overview.generated_at else []
        ),
        title_suggestions=(
            list(overview.title_suggestions) if overview and overview.generated_at else []
        ),
        generated_at=overview.generated_at if overview else None,
        source_count=overview.source_count if overview and overview.generated_at else 0,
        stale=stale or regen_running,
    )


@router.post("/{notebook_id}/overview/regenerate", status_code=status.HTTP_202_ACCEPTED)
async def regenerate_overview(
    notebook_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """개요 수동 재생성 — 쓰기 권한자만, 진행 중 중복 요청은 무시(Redis 락)."""
    nb = await _get_notebook_or_404(db, notebook_id)
    perm = await get_permission(db, nb, user)
    # 무관 사용자는 단건 GET과 같은 404다 — 노트북 존재를 드러내지 않는다(#269)
    if perm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    if perm not in ("owner", "write"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Write permission required")

    from redis.asyncio import Redis

    from biznotes_core.config import get_settings
    from biznotes_core.services.overview_refresh import schedule_overview_refresh

    redis = Redis.from_url(get_settings().redis_url)
    try:
        # 60초 락 — 생성 작업이 돌고 있는 동안 중복 enqueue 방지
        acquired = await redis.set(f"overview:regen:{notebook_id}", "1", ex=60, nx=True)
    finally:
        await redis.aclose()
    if not acquired:
        return {"status": "already-running"}

    schedule_overview_refresh(str(notebook_id), debounce=True, countdown=0)
    return {"status": "scheduled"}


@router.patch("/{notebook_id}", response_model=NotebookOut)
async def update(
    notebook_id: uuid.UUID,
    body: NotebookUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotebookOut:
    nb = await _get_notebook_or_404(db, notebook_id)
    perm = await get_permission(db, nb, user)
    if perm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    if perm not in ("owner", "write"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Write permission required")

    changes = non_null_changes(body)
    # 공개 범위는 소유자 권한(실제 소유자·admin 승격)만 바꾼다 — 쓰기 공유자가 넓히거나 좁히지
    # 못한다(#278). 같은 값은 허용한다(편집 화면이 전체 필드를 보낼 수 있다). 판정이 적용보다 먼저다
    if "visibility" in changes and changes["visibility"] != nb.visibility and perm != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can change visibility")
    for field, value in changes.items():
        setattr(nb, field, value)
    await db.commit()
    await db.refresh(nb)
    return _to_out(nb, perm, user.id)


@router.delete("/{notebook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(
    notebook_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    nb = await _get_notebook_or_404(db, notebook_id)
    perm = await get_permission(db, nb, user)
    if perm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    if perm != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can delete")
    # 하위 행은 CASCADE로 지워진다 — 그 행들이 가리키는 오브젝트 키는 커밋 전에 모은다(#177)
    refs = await notebook_object_refs(db, nb.id)
    await db.delete(nb)
    await db.commit()
    await anyio.to_thread.run_sync(delete_objects_best_effort, refs)


@router.post(
    "/{notebook_id}/share",
    response_model=NotebookShareOut,
    status_code=status.HTTP_201_CREATED,
)
async def share(
    notebook_id: uuid.UUID,
    body: NotebookShareRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotebookShareOut:
    """노트북을 그룹에 공유한다 — 소유자(admin 승격 포함)만.

    그룹은 노트북 실제 소유자가 속한 그룹만 대상이 된다. 아니면 없는 그룹과 같은 404다(#278).
    같은 그룹에 다시 공유하면 새 행을 만들지 않고 기존 행의 권한을 갱신한다. 기본(`mode="set"`)은
    요청 값으로 바꾸고(하향 포함), `mode="upgrade_only"`는 기존 권한과 요청 중 높은 쪽을
    남긴다(#300). 응답은 201이고 `id`는 처음 공유와 같다(#222).

    사용자(이메일) 대상 공유는 없앴다 — `user_id`·`user_email`을 보내면 422다(#278 3단계).
    """
    # 무관 사용자에게 노트북·그룹 존재를 드러내지 않게 권한 판정이 대상 확인보다 먼저다(#223)
    nb = await _get_notebook_or_404(db, notebook_id)
    perm = await get_permission(db, nb, user)
    if perm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    if perm != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can share")
    # 노트북 실제 소유자가 속한 그룹만 대상이다 — admin이 남의 노트북을 공유해도 소유자 소속을
    # 본다. 비구성원 그룹은 없는 그룹과 같은 404라 소유자 밖의 그룹 존재를 드러내지 않는다(#278)
    membership = await db.get(UserGroup, (nb.owner_id, body.group_id))
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Target group not found")

    # 같은 대상의 동시 공유가 행을 둘 만들지 않게 잠근 뒤 기존 행을 읽는다.
    # 거부는 모두 위에서 끝났다
    await lock_notebook_shares(db, nb.id)
    share_row = await upsert_notebook_share(
        db,
        nb.id,
        group_id=body.group_id,
        permission=body.permission,
        upgrade_only=body.mode == "upgrade_only",
    )
    # 응답은 잠금 안에서 만든다 — 커밋으로 잠금이 풀린 뒤 같은 행이 해제되면 refresh가 실패한다.
    # 응답 필드는 모두 요청·조회로 정해져 있다(세션은 expire_on_commit=False)
    await db.flush()
    out = NotebookShareOut.model_validate(share_row)
    await db.commit()
    return out


@router.get("/{notebook_id}/shares", response_model=list[NotebookShareOut])
async def list_shares(
    notebook_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[NotebookShareOut]:
    from sqlalchemy import select

    nb = await _get_notebook_or_404(db, notebook_id)
    perm = await get_permission(db, nb, user)
    if perm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    if perm != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can view shares")
    rows = await db.execute(select(NotebookShare).where(NotebookShare.notebook_id == nb.id))
    return [NotebookShareOut.model_validate(s) for s in rows.scalars().all()]


@router.delete("/{notebook_id}/shares/{share_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unshare(
    notebook_id: uuid.UUID,
    share_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    nb = await _get_notebook_or_404(db, notebook_id)
    perm = await get_permission(db, nb, user)
    if perm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    if perm != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can unshare")
    # 공유 갱신(#222)과 엇갈려 지운 행을 갱신하지 않게 같은 잠금 안에서 읽는다. 거부는 위에서 끝났다
    await lock_notebook_shares(db, nb.id)
    share_row = await db.get(NotebookShare, share_id, populate_existing=True)
    if share_row is None or share_row.notebook_id != nb.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share not found")
    await db.delete(share_row)
    await db.commit()
