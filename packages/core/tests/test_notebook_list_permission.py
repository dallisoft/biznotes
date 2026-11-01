"""노트북 목록의 권한 표기가 단건 판정과 같은 규칙이다 (#221).

`list_notebooks`의 질의 조건(filter 범위)은 대역 밖이다 — 대역 세션은 질의 종류별로 정해 둔 행을
돌려준다. 행마다의 권한 계산은 실경로이고, 같은 입력의 `resolve_permission` 결과와 같아야 한다.
실제 라우터·DB에서 목록과 단건 `GET`을 대조하는 것은
`tests/test_notebook_list_permission_pg.py`다.
"""

import uuid
from typing import Any

import pytest
from conftest import FakeResult

from biznotes_core.models import Notebook, User
from biznotes_core.services.authz import resolve_permission
from biznotes_core.services.notebooks import list_notebooks


class FakeDB:
    """그룹·노트북·공유 질의에 정해 둔 행을 돌려준다."""

    def __init__(self, notebooks: list[Notebook], shares: list[tuple[uuid.UUID, str]]) -> None:
        self.notebooks = notebooks
        self.shares = shares

    async def execute(self, stmt: Any) -> FakeResult:
        sql = str(stmt)
        if "FROM user_groups" in sql:
            return FakeResult([])
        if "FROM notebooks" in sql:
            return FakeResult([(nb,) for nb in self.notebooks])
        if "FROM notebook_shares" in sql:
            return FakeResult(list(self.shares))
        raise AssertionError(f"예상하지 못한 질의: {sql}")


def _user(role: str = "user") -> User:
    return User(id=uuid.uuid4(), email=f"{role}@test.dev", role=role, is_active=True)


def _nb(owner: User, visibility: str = "private") -> Notebook:
    return Notebook(id=uuid.uuid4(), owner_id=owner.id, title="t", visibility=visibility)


async def _perms(
    user: User, notebooks: list[Notebook], shares: list[tuple[Any, str]], filter_: Any = "all"
) -> dict:
    rows = await list_notebooks(FakeDB(notebooks, shares), user, filter_)  # type: ignore[arg-type]
    return {nb.id: perm for nb, perm in rows}


def _expected(user: User, nb: Notebook, shares: list[tuple[Any, str]]) -> str | None:
    return resolve_permission(
        owner_id=nb.owner_id,
        visibility=nb.visibility,
        user_id=user.id,
        user_role=user.role,
        share_perms={p for i, p in shares if i == nb.id},
    )


@pytest.mark.parametrize("filter_", ["all", "mine", "shared"])
async def test_admin_sees_promoted_owner_in_list(filter_: str) -> None:
    """질의 조건은 대역 밖이라 세 filter 모두 같은 행을 받는다.

    행 권한 계산이 filter와 무관함을 본다.
    """
    admin, other = _user("admin"), _user()
    own, public, shared_r, shared_w = (
        _nb(admin),
        _nb(other, "public"),
        _nb(other),
        _nb(other),
    )
    shares = [(shared_r.id, "read"), (shared_w.id, "write")]

    perms = await _perms(admin, [own, public, shared_r, shared_w], shares, filter_)

    assert perms == dict.fromkeys([own.id, public.id, shared_r.id, shared_w.id], "owner")


@pytest.mark.parametrize(
    ("visibility", "share_perms", "expected"),
    [
        ("private", ["read"], "read"),
        ("private", ["write"], "write"),
        ("private", ["read", "write"], "write"),  # 본인·그룹 공유가 겹치면 높은 쪽
        ("public", [], "read"),
        ("public", ["write"], "write"),
        ("group", ["read"], "read"),
    ],
)
async def test_non_admin_permission_is_unchanged(
    visibility: str, share_perms: list[str], expected: str
) -> None:
    me, other = _user(), _user()
    mine, nb = _nb(me), _nb(other, visibility)
    shares = [(nb.id, p) for p in share_perms]

    perms = await _perms(me, [mine, nb], shares)

    assert perms == {mine.id: "owner", nb.id: expected}
    assert perms[nb.id] == _expected(me, nb, shares)


async def test_row_rejected_by_single_rule_is_not_listed() -> None:
    """공유 권한 값이 read·write 밖이면 public이 아닌 노트북(private·group)은 단건(404)처럼 목록에도
    없다. public 노트북은 공유와 무관하게 read로 남는다."""
    me, other = _user(), _user()
    private, group_vis, public = _nb(other), _nb(other, "group"), _nb(other, "public")
    shares = [(private.id, "admin"), (group_vis.id, "owner"), (public.id, "admin")]

    perms = await _perms(me, [private, group_vis, public], shares)

    assert perms == {public.id: "read"}
    assert _expected(me, private, shares) is None
    assert _expected(me, group_vis, shares) is None
