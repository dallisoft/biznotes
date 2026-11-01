"""overview 재생성·태그 부착·해제가 무관 사용자에게 노트북 존재를 드러내지 않는다 (#269).

인증(`get_current_user`)과 DB 세션(`get_db`)만 대역이고, 권한 판정(`get_permission` →
`resolve_permission`)·핸들러·응답은 실경로다. 대역 세션은 질의 종류를 기록해, 거부 경로가
쓰기·커밋·Redis에 닿지 않음을 확인한다. 실제 권한 행렬은
`tests/test_overview_tag_existence_pg.py`가 확인한다.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import redis.asyncio
from conftest import FakeResult
from fastapi import FastAPI

from biznotes_core.api import deps
from biznotes_core.api.v1 import notebooks, tags
from biznotes_core.core.db import get_db
from biznotes_core.models import Notebook, NotebookTag, Tag, User

NOW = datetime(2026, 10, 17, tzinfo=UTC)
NOT_FOUND = {"detail": "Notebook not found"}
WRITE_REQUIRED = {"detail": "Write permission required"}
OWNER_REQUIRED = {"detail": "Owner permission required"}


class _Result(FakeResult):
    def scalar_one_or_none(self) -> Any:
        return self._rows[0][0] if self._rows else None


class FakeDB:
    """행 조회·공유 권한에 정해 둔 값을 돌려주고 질의 종류를 기록한다."""

    def __init__(self, *rows: Any, share_perms: tuple[str, ...] = ()) -> None:
        self.rows = {(type(r), r.id): r for r in rows}
        self.share_perms = share_perms
        self.calls: list[str] = []

    async def get(self, model: type, key: Any, **_kw: Any) -> Any:
        self.calls.append(f"get:{model.__name__}")
        return self.rows.get((model, key))

    async def execute(self, stmt: Any) -> _Result:
        sql = str(stmt)
        if "FROM user_groups" in sql:
            self.calls.append("groups")
            return _Result([])
        if "FROM notebook_shares" in sql:
            self.calls.append("shares")
            return _Result([(p,) for p in self.share_perms])
        if "FROM notebook_tags" in sql:
            self.calls.append("notebook_tags")
            return _Result([])
        raise AssertionError(f"예상하지 못한 질의: {sql}")

    def add(self, obj: Any) -> None:
        self.calls.append(f"add:{type(obj).__name__}")

    async def delete(self, obj: Any) -> None:
        self.calls.append(f"delete:{type(obj).__name__}")

    async def commit(self) -> None:
        self.calls.append("commit")


@pytest.fixture(autouse=True)
def no_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """거부 경로는 Redis 잠금·예약에 닿지 않는다 — 닿으면 테스트가 실패한다."""

    def _fail(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("Redis에 닿았다")

    monkeypatch.setattr(redis.asyncio.Redis, "from_url", _fail)


def _user(role: str = "user") -> User:
    email = f"{uuid.uuid4().hex[:6]}@test.dev"
    return User(id=uuid.uuid4(), email=email, role=role, is_active=True)


def _notebook(owner: User, visibility: str = "private") -> Notebook:
    return Notebook(
        id=uuid.uuid4(),
        owner_id=owner.id,
        title="t",
        emoji="📓",
        description="",
        visibility=visibility,
        created_at=NOW,
        updated_at=NOW,
    )


async def _call(me: User, db: FakeDB, method: str, path: str) -> httpx.Response:
    app = FastAPI()
    app.include_router(notebooks.router)
    app.include_router(tags.router)
    app.dependency_overrides[deps.get_current_user] = lambda: me
    app.dependency_overrides[get_db] = lambda: db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.request(method, path)


def _routes(nb_id: uuid.UUID, tag_id: uuid.UUID) -> dict[str, tuple[str, str]]:
    return {
        "regenerate": ("POST", f"/notebooks/{nb_id}/overview/regenerate"),
        "attach": ("POST", f"/notebooks/{nb_id}/tags/{tag_id}"),
        "detach": ("DELETE", f"/notebooks/{nb_id}/tags/{tag_id}"),
    }


ROUTES = ["regenerate", "attach", "detach"]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("tag_owner", ["me", "other"])
@pytest.mark.parametrize("visibility", ["private", "group"])  # group 가시성은 권한을 주지 않는다
async def test_unrelated_user_gets_the_same_404_as_a_missing_notebook(
    route: str, tag_owner: str, visibility: str
) -> None:
    owner, me = _user(), _user()
    nb = _notebook(owner, visibility)
    tag = Tag(id=uuid.uuid4(), owner_id=(me if tag_owner == "me" else owner).id, name="t")
    db = FakeDB(nb, tag)

    method, path = _routes(nb.id, tag.id)[route]
    resp = await _call(me, db, method, path)
    missing_method, missing_path = _routes(uuid.uuid4(), tag.id)[route]
    missing = await _call(me, FakeDB(tag), missing_method, missing_path)

    assert (resp.status_code, resp.json()) == (404, NOT_FOUND)
    assert (missing.status_code, missing.json()) == (404, NOT_FOUND)
    # 권한 판정만 하고 태그·쓰기·커밋에 닿지 않는다
    assert db.calls == ["get:Notebook", "groups", "shares"]


@pytest.mark.parametrize(
    ("share_perms", "visibility"),
    [(("read",), "private"), ((), "public")],
    ids=["읽기-공유", "public-열람"],
)
@pytest.mark.parametrize("route", ROUTES)
async def test_read_permission_keeps_403(
    route: str, share_perms: tuple[str, ...], visibility: str
) -> None:
    owner, me = _user(), _user()
    nb = _notebook(owner, visibility)
    tag = Tag(id=uuid.uuid4(), owner_id=me.id, name="t")
    db = FakeDB(nb, tag, share_perms=share_perms)

    method, path = _routes(nb.id, tag.id)[route]
    resp = await _call(me, db, method, path)

    expected = WRITE_REQUIRED if route == "regenerate" else OWNER_REQUIRED
    assert (resp.status_code, resp.json()) == (403, expected)
    assert db.calls == ["get:Notebook", "groups", "shares"]


@pytest.mark.parametrize("route", ["attach", "detach"])
async def test_write_sharer_cannot_tag(route: str) -> None:
    owner, me = _user(), _user()
    nb = _notebook(owner)
    tag = Tag(id=uuid.uuid4(), owner_id=me.id, name="t")
    db = FakeDB(nb, tag, share_perms=("write",))

    method, path = _routes(nb.id, tag.id)[route]
    resp = await _call(me, db, method, path)

    assert (resp.status_code, resp.json()) == (403, OWNER_REQUIRED)
    assert db.calls == ["get:Notebook", "groups", "shares"]


@pytest.mark.parametrize("route", ["attach", "detach"])
async def test_admin_who_is_not_the_owner_cannot_tag(route: str) -> None:
    """admin은 owner로 승격되지만 태그는 실제 소유자만 다룬다(불변식)."""
    owner, admin = _user(), _user(role="admin")
    nb = _notebook(owner)
    tag = Tag(id=uuid.uuid4(), owner_id=admin.id, name="t")
    db = FakeDB(nb, tag)

    method, path = _routes(nb.id, tag.id)[route]
    resp = await _call(admin, db, method, path)

    assert (resp.status_code, resp.json()) == (403, OWNER_REQUIRED)
    # admin은 공유 조회 없이 승격 판정된다
    assert db.calls == ["get:Notebook"]


@pytest.mark.parametrize("route", ["attach", "detach"])
async def test_owner_tagging_is_unchanged_and_skips_share_queries(route: str) -> None:
    owner = _user()
    nb = _notebook(owner)
    tag = Tag(id=uuid.uuid4(), owner_id=owner.id, name="t")
    db = FakeDB(nb, tag)

    method, path = _routes(nb.id, tag.id)[route]
    resp = await _call(owner, db, method, path)

    assert resp.status_code == 204
    if route == "attach":
        assert db.calls == ["get:Notebook", "get:Tag", "notebook_tags", "add:NotebookTag", "commit"]
    else:
        assert db.calls == ["get:Notebook", "notebook_tags"]


def test_tag_link_model_is_the_one_the_fake_answers() -> None:
    # 대역의 `FROM notebook_tags` 분기가 실제 테이블 이름과 맞는지 고정한다
    assert NotebookTag.__tablename__ == "notebook_tags"
