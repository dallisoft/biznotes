# 신규(#278 3단계·#277). 필요: PostgreSQL·Valkey(PG 테스트).
"""공유 제약 — 실제 PostgreSQL이 비정상 공유·공개 범위 값을 거부한다.

계획 `docs/plans/share-redesign/stage-3.md` 수락 기준 7이다. `create_all` 기반이라 소비 제품
리비전의 검증이 아니다 — 제약 이름·식이 정본과 같은지를 실제 DB로 본다. 정상 공유·해제·판정은
라우터 PG 테스트(`test_notebook_share_*_pg.py`)가 본다.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from biznotes_core.core.db import get_session_factory
from biznotes_core.models import Group, Notebook, NotebookShare, User, UserGroup


async def _world(owner: User) -> dict[str, uuid.UUID]:
    async with get_session_factory()() as s:
        group = Group(name=f"g-{uuid.uuid4().hex[:6]}")
        nb = Notebook(owner_id=owner.id, title="공유 대상")
        s.add_all([group, nb])
        await s.flush()
        s.add(UserGroup(user_id=owner.id, group_id=group.id))
        await s.commit()
        return {"group": group.id, "nb": nb.id}


async def _insert(row: Any) -> None:
    async with get_session_factory()() as s:
        s.add(row)
        await s.commit()


async def _constraint_of(row: Any) -> str:
    with pytest.raises(IntegrityError) as exc:
        await _insert(row)
    return str(exc.value.orig)


@pytest.mark.parametrize("permission", ["owner", "admin", ""])
async def test_share_permission_outside_read_write_is_rejected(
    user_a: User, permission: str
) -> None:
    w = await _world(user_a)

    error = await _constraint_of(
        NotebookShare(notebook_id=w["nb"], group_id=w["group"], permission=permission)
    )

    assert "ck_share_permission" in error


async def test_share_without_a_group_is_rejected(user_a: User) -> None:
    w = await _world(user_a)

    error = await _constraint_of(NotebookShare(notebook_id=w["nb"], permission="read"))

    assert "group_id" in error and "null" in error.lower()


async def test_second_share_to_the_same_group_is_rejected(user_a: User) -> None:
    w = await _world(user_a)
    await _insert(NotebookShare(notebook_id=w["nb"], group_id=w["group"], permission="read"))

    error = await _constraint_of(
        NotebookShare(notebook_id=w["nb"], group_id=w["group"], permission="write")
    )

    assert "uq_share_notebook_group" in error


async def test_notebook_shares_has_no_user_column(user_a: User) -> None:
    async with get_session_factory()() as s:
        columns = set(
            (
                await s.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'notebook_shares'"
                    )
                )
            ).scalars()
        )
    assert "user_id" not in columns and "group_id" in columns


@pytest.mark.parametrize("visibility", ["group", "org", ""])
async def test_notebook_visibility_outside_private_public_is_rejected(
    user_a: User, visibility: str
) -> None:
    error = await _constraint_of(
        Notebook(owner_id=user_a.id, title="공개 범위", visibility=visibility)
    )

    assert "ck_notebook_visibility" in error


async def test_valid_rows_are_accepted(user_a: User) -> None:
    w = await _world(user_a)
    await _insert(NotebookShare(notebook_id=w["nb"], group_id=w["group"], permission="write"))
    await _insert(Notebook(owner_id=user_a.id, title="공개", visibility="public"))
