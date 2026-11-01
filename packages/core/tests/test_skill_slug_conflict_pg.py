# 신규(#412). 필요: PostgreSQL·Valkey(PG 테스트).
"""org·system 공개의 전역 slug 충돌 — 409 `slug_conflict`와 생성 때 공개 slug 피하기.

`uq_skills_org_slug`·`uq_skills_system_slug`는 visibility별 **전역** 부분 유니크 인덱스다.
수정 전에는 공개·승격·강등이 이 인덱스와 부딪히면 처리되지 않은 IntegrityError(500)였다.
부분 인덱스와 동시 공개의 잠금 대기가 필요해 PostgreSQL에서만 검증한다. 경합 순서는 시간
대기가 아니라 `pg_blocking_pids`로 고정한다. `create_all` 기반이라 마이그레이션 검증이 아니다.
"""

import asyncio
import contextlib
import io
import uuid
import zipfile
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import select, text

from biznotes_core.api.deps import require_admin
from biznotes_core.api.v1 import auth, skills
from biznotes_core.api.v1.admin import skills as admin_skills
from biznotes_core.core.db import get_session_factory
from biznotes_core.models import Skill, SkillReviewRequest, SkillVersion, User
from biznotes_core.services.skills import exposure
from biznotes_core.services.skills import package as pkg
from biznotes_core.services.skills import service as svc

PASSWORD = "pw-test-1234"  # tests/conftest.py 공용 해시의 평문
SLUG = "summarize"
CONFLICT_PREFIX = "[slug_conflict] "


def _app() -> FastAPI:
    app = FastAPI()
    admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
    admin.include_router(admin_skills.router)
    for router in (auth.router, skills.router, admin):
        app.include_router(router, prefix="/api/v1")
    return app


class _Api:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.tokens: dict[uuid.UUID, str] = {}

    async def login(self, user: User) -> str:
        if user.id not in self.tokens:
            self.client.cookies.clear()
            res = await self.client.post(
                "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
            )
            assert res.status_code == 200, res.text
            self.tokens[user.id] = res.json()["access_token"]
        return self.tokens[user.id]

    async def call(self, user: User, method: str, path: str, **kw: Any) -> httpx.Response:
        token = await self.login(user)
        self.client.cookies.clear()
        return await self.client.request(
            method, f"/api/v1{path}", headers={"Authorization": f"Bearer {token}"}, **kw
        )


@pytest.fixture
async def api() -> AsyncIterator[_Api]:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield _Api(client)


async def _skill(
    owner: User | None,
    *,
    slug: str = SLUG,
    name: str | None = None,
    visibility: str = "private",
    origin: str = "authored",
    published: bool = False,
) -> tuple[uuid.UUID, uuid.UUID]:
    """스킬과 version 1을 커밋한다. `published`면 포인터가 version 1을 가리킨다."""
    async with get_session_factory()() as s:
        skill = Skill(
            owner_id=owner.id if owner is not None else None,
            name=name or f"{slug}-{uuid.uuid4().hex[:6]}",
            description="d",
            slug=slug,
            visibility=visibility,
            status="published" if published else "draft",
            origin=origin,
        )
        s.add(skill)
        await s.flush()
        version = SkillVersion(
            skill_id=skill.id,
            version=1,
            package_uri=f"versions/{uuid.uuid4()}/",
            skill_md_hash="h",
            package_hash="p1",
            manifest={},
            file_tree=[],
            security_report={},
            status="published" if published else "draft_frozen",
        )
        s.add(version)
        await s.flush()
        skill.current_version_id = version.id
        if published:
            skill.published_version_id = version.id
        await s.commit()
        return skill.id, version.id


async def _add_version(skill_id: uuid.UUID, number: int) -> uuid.UUID:
    async with get_session_factory()() as s:
        version = SkillVersion(
            skill_id=skill_id,
            version=number,
            package_uri=f"versions/{uuid.uuid4()}/",
            skill_md_hash="h",
            package_hash=f"p{number}",
            manifest={},
            file_tree=[],
            security_report={},
            status="draft_frozen",
        )
        s.add(version)
        await s.flush()
        skill = await s.get(Skill, skill_id)
        assert skill is not None
        skill.current_version_id = version.id
        await s.commit()
        return version.id


async def _request(
    version_id: uuid.UUID, kind: str, *, status: str = "pending", by: User | None = None
) -> uuid.UUID:
    async with get_session_factory()() as s:
        request = SkillReviewRequest(
            version_id=version_id,
            kind=kind,
            target_visibility="system" if kind == "expose_system" else "org",
            status=status,
            requested_by=by.id if by is not None else None,
        )
        s.add(request)
        await s.commit()
        return request.id


async def _fresh(model: type, key: uuid.UUID) -> Any:
    async with get_session_factory()() as s:
        return await s.get(model, key)


def _assert_conflict(res: httpx.Response, *hidden: object) -> None:
    """409 `slug_conflict` — 응답에 다른 스킬의 이름·id·소유자가 없다."""
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail.startswith(CONFLICT_PREFIX), detail
    assert "어드민" not in detail
    for value in hidden:
        assert str(value) not in res.text


# ---------- 차례로 공개 ----------


@pytest.mark.parametrize("route", ["publish", "patch"])
async def test_second_org_publish_of_the_same_slug_is_409_and_the_first_stays(
    api: _Api, user_a: User, user_b: User, route: str
) -> None:
    """두 사용자의 `summarize`를 차례로 org 공개하면 두 번째는 409이고 첫 번째는 그대로다."""
    a_id, a_v1 = await _skill(user_a, name="A의 요약")
    b_id, b_v1 = await _skill(user_b)

    first = await api.call(user_a, "POST", f"/skills/{a_id}/versions/{a_v1}/publish")
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "published"

    if route == "publish":
        second = await api.call(user_b, "POST", f"/skills/{b_id}/versions/{b_v1}/publish")
    else:
        second = await api.call(user_b, "PATCH", f"/skills/{b_id}", json={"visibility": "org"})
    _assert_conflict(second, a_id, a_v1, "A의 요약", user_a.id, user_a.email)

    a = await _fresh(Skill, a_id)
    assert (a.visibility, a.published_version_id, a.status) == ("org", a_v1, "published")
    b = await _fresh(Skill, b_id)
    assert (b.visibility, b.published_version_id, b.status) == ("private", None, "draft")


async def test_republishing_an_own_org_skill_is_not_a_conflict(api: _Api, user_a: User) -> None:
    """이미 org인 자기 스킬을 새 version으로 다시 publish하는 것은 자기 슬롯이다."""
    a_id, _ = await _skill(user_a, visibility="org", published=True)
    a_v2 = await _add_version(a_id, 2)

    res = await api.call(user_a, "POST", f"/skills/{a_id}/versions/{a_v2}/publish")

    assert res.status_code == 200, res.text
    assert res.json()["status"] == "published"
    a = await _fresh(Skill, a_id)
    assert (a.visibility, a.published_version_id) == ("org", a_v2)


async def test_slug_conflict_is_reported_before_a_review_request_is_left(
    api: _Api, user_a: User, user_b: User
) -> None:
    """검토가 필요한 공개(가져온 스킬)도 슬롯이 막혀 있으면 요청을 남기지 않고 409다.

    승인해도 공개할 수 없는 요청이 검토 큐에 쌓이지 않게 한다.
    """
    await _skill(user_a, visibility="org", published=True)
    b_id, b_v1 = await _skill(user_b, origin="imported")

    res = await api.call(user_b, "POST", f"/skills/{b_id}/versions/{b_v1}/publish")

    _assert_conflict(res)
    async with get_session_factory()() as s:
        assert (await s.scalars(select(SkillReviewRequest.id))).all() == []


# ---------- 관리자 경로 ----------


async def test_admin_promotion_to_system_conflicts_with_an_existing_system_slug(
    api: _Api, user_a: User, admin_user: User
) -> None:
    seed_id, _ = await _skill(None, visibility="system", origin="system_seed", published=True)
    a_id, a_v1 = await _skill(user_a)

    res = await api.call(
        admin_user,
        "PATCH",
        f"/admin/skills/{a_id}",
        json={"visibility": "system", "version_id": str(a_v1)},
    )

    _assert_conflict(res, seed_id)
    a = await _fresh(Skill, a_id)
    assert (a.visibility, a.published_version_id) == ("private", None)
    async with get_session_factory()() as s:  # 관리자 지정 승인도 남지 않는다
        assert (await s.scalars(select(SkillReviewRequest.id))).all() == []


@pytest.mark.parametrize(
    ("kind", "origin", "holder_visibility"),
    [("expose_system", "authored", "system"), ("expose_org", "imported", "org")],
    ids=["expose_system", "expose_org"],
)
async def test_expose_approval_conflict_is_409_and_the_request_stays_pending(
    api: _Api,
    user_a: User,
    user_b: User,
    admin_user: User,
    kind: str,
    origin: str,
    holder_visibility: str,
) -> None:
    holder_id, _ = await _skill(user_b, visibility=holder_visibility, published=True)
    a_id, a_v1 = await _skill(user_a, origin=origin)
    request_id = await _request(a_v1, kind, by=user_a)

    res = await api.call(admin_user, "POST", f"/admin/skills/review-requests/{request_id}/approve")

    _assert_conflict(res, holder_id, user_b.id)
    request = await _fresh(SkillReviewRequest, request_id)
    assert (request.status, request.decided_by) == ("pending", None)
    a = await _fresh(Skill, a_id)
    assert (a.visibility, a.published_version_id) == ("private", None)


async def test_admin_demotion_system_to_org_conflicting_with_an_org_slot_is_409(
    api: _Api, user_a: User, user_b: User, admin_user: User
) -> None:
    """system→org 강등도 org 슬롯과 겹치면 409이고 system 공개·승인은 그대로다."""
    a_id, a_v1 = await _skill(user_a, visibility="system", published=True)
    approval_id = await _request(a_v1, "expose_system", status="approved", by=user_a)
    holder_id, _ = await _skill(user_b, visibility="org", published=True)

    res = await api.call(admin_user, "PATCH", f"/admin/skills/{a_id}", json={"visibility": "org"})

    _assert_conflict(res, holder_id, user_b.id)
    a = await _fresh(Skill, a_id)
    assert (a.visibility, a.published_version_id) == ("system", a_v1)
    assert (await _fresh(SkillReviewRequest, approval_id)).status == "approved"


async def test_seed_path_checks_the_system_slot(user_a: User) -> None:
    """시드 경로(`seed=True`)도 system 슬롯을 검사한다. 쓰기 전에 거부하므로 세션은 그대로다."""
    seed_id, seed_v1 = await _skill(None, visibility="private", origin="system_seed")
    await _skill(user_a, visibility="system", published=True)

    async with get_session_factory()() as s:
        seed = await s.get(Skill, seed_id)
        version = await s.get(SkillVersion, seed_v1)
        with pytest.raises(exposure.ExposureError) as caught:
            await exposure.set_published_version(
                s, seed, version, actor_id=None, target_visibility="system", seed=True
            )
        assert (caught.value.reason, caught.value.status_code) == ("slug_conflict", 409)
        assert await s.scalar(text("SELECT 1")) == 1

    seed = await _fresh(Skill, seed_id)
    assert (seed.visibility, seed.published_version_id) == ("private", None)


# ---------- 동시 공개 경합 ----------


async def _wait_until_blocked_by(holder_pid: int) -> None:
    """다른 연결이 `holder_pid`의 잠금 때문에 기다리는 상태가 될 때까지 기다린다(상한 10초)."""
    async with asyncio.timeout(10), get_session_factory()() as s:
        while not await s.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock' AND :holder = ANY(pg_blocking_pids(pid))"
            ),
            {"holder": holder_pid},
        ):
            await s.rollback()


async def test_concurrent_org_publish_one_wins_and_the_other_is_409_not_500(
    api: _Api, user_a: User, user_b: User
) -> None:
    """두 공개가 모두 사전 검사를 통과해도 unique 위반은 같은 409로 바뀐다.

    A의 공개가 커밋 전에 org 슬롯을 쥔 동안 B의 요청이 인덱스 대기에 들어간 것을 확인하고
    A를 커밋한다. B는 이때 unique 위반을 받는다.
    """
    a_id, a_v1 = await _skill(user_a)
    b_id, b_v1 = await _skill(user_b)
    await api.login(user_b)  # 로그인 커밋이 대기 확인을 가리지 않게 먼저 한다

    task: asyncio.Task[httpx.Response] | None = None
    try:
        async with get_session_factory()() as holder:
            holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            skill = await holder.get(Skill, a_id)
            version = await holder.get(SkillVersion, a_v1)
            outcome = await exposure.publish_version(holder, skill, version, actor_id=user_a.id)
            assert outcome.status == "published"
            task = asyncio.create_task(
                api.call(user_b, "POST", f"/skills/{b_id}/versions/{b_v1}/publish")
            )
            await _wait_until_blocked_by(holder_pid)
            await holder.commit()
        async with asyncio.timeout(10):
            res = await task
    finally:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    _assert_conflict(res, a_id, user_a.id)
    a = await _fresh(Skill, a_id)
    assert (a.visibility, a.published_version_id) == ("org", a_v1)
    b = await _fresh(Skill, b_id)
    assert (b.visibility, b.published_version_id) == ("private", None)


async def test_concurrent_system_promotion_409_leaves_the_transaction_usable(
    user_a: User, user_b: User, admin_user: User
) -> None:
    """system 슬롯 경합도 같은 409다. 바뀐 위반은 savepoint 안이라 진 쪽 트랜잭션을 계속 쓴다."""
    a_id, a_v1 = await _skill(user_a)
    b_id, b_v1 = await _skill(user_b)

    async def promote_b(session: Any) -> None:
        skill = await session.get(Skill, b_id)
        await exposure.admin_change_visibility(
            session, skill, "system", version_id=b_v1, admin_id=admin_user.id
        )

    task: asyncio.Task[None] | None = None
    try:
        async with get_session_factory()() as holder, get_session_factory()() as loser:
            holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            await exposure.admin_change_visibility(
                holder,
                await holder.get(Skill, a_id),
                "system",
                version_id=a_v1,
                admin_id=admin_user.id,
            )
            task = asyncio.create_task(promote_b(loser))
            await _wait_until_blocked_by(holder_pid)
            await holder.commit()
            with pytest.raises(exposure.ExposureError) as caught:
                async with asyncio.timeout(10):
                    await task
            assert (caught.value.reason, caught.value.status_code) == ("slug_conflict", 409)
            # 같은 트랜잭션에서 계속 읽을 수 있고, savepoint 안의 대입은 되돌아갔다
            b = await loser.get(Skill, b_id, populate_existing=True)
            assert (b.visibility, b.published_version_id) == ("private", None)
            await loser.rollback()
    finally:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    a = await _fresh(Skill, a_id)
    assert (a.visibility, a.published_version_id) == ("system", a_v1)
    b = await _fresh(Skill, b_id)
    assert (b.visibility, b.published_version_id) == ("private", None)


# ---------- 생성 때 공개 slug 피하기 ----------


@pytest.fixture
def draft_store(monkeypatch: pytest.MonkeyPatch) -> dict[uuid.UUID, dict[str, bytes]]:
    """패키지 스토리지 대역 — draft는 스킬 id, version은 version id로 읽는다."""
    drafts: dict[uuid.UUID, dict[str, bytes]] = {}

    def save_draft(skill_id: uuid.UUID, files: dict[str, bytes]) -> None:
        drafts[skill_id] = dict(files)

    def load_version_files(version_id: uuid.UUID) -> dict[str, bytes]:
        return {"SKILL.md": b"---\nname: summarize\ndescription: d\n---\n\nbody"}

    monkeypatch.setattr(pkg, "save_draft", save_draft)
    monkeypatch.setattr(pkg, "load_version_files", load_version_files)
    monkeypatch.setattr(pkg, "load_draft_files", lambda skill_id: dict(drafts[skill_id]))
    monkeypatch.setattr(pkg, "freeze_draft_to_version", lambda skill_id, version_id: None)
    return drafts


def _zip(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{name}/SKILL.md", f"---\nname: {name}\ndescription: d\n---\n\nbody")
    return buf.getvalue()


async def test_new_skill_import_and_clone_avoid_exposed_slugs(
    draft_store: dict, user_a: User, user_b: User
) -> None:
    """새 스킬·가져오기·복제의 slug가 기존 org·system slug와 겹치면 `-2`가 붙는다."""
    org_id, _ = await _skill(
        user_b, slug="summarize", name="summarize", visibility="org", published=True
    )
    await _skill(None, slug="report", visibility="system", origin="system_seed", published=True)

    async with get_session_factory()() as s:
        owner = await s.get(User, user_a.id)
        created = await svc.create_skill(s, owner, "Report", "d")
        imported = (await svc.import_zip_detailed(s, owner, _zip("summarize"))).skill
        clone = await svc.clone_skill(s, await s.get(Skill, org_id), owner)
        slugs = (created.slug, imported.slug, clone.slug)
        await s.commit()

    # 가져오기가 먼저 summarize-2를 가졌으므로 복제는 소유자 범위 접미로 summarize-3이다
    assert slugs == ("report-2", "summarize-2", "summarize-3")


async def test_slug_avoidance_keeps_owner_scope_and_ignores_others_private_skills(
    draft_store: dict, user_a: User, user_b: User
) -> None:
    """기존 동작(소유자 범위 접미)은 그대로다. 다른 사용자의 private slug는 피하지 않는다."""
    await _skill(user_a, slug="mine")
    await _skill(user_b, slug="draft-only")

    async with get_session_factory()() as s:
        owner = await s.get(User, user_a.id)
        own = await svc.create_skill(s, owner, "Mine", "d")
        others_private = await svc.create_skill(s, owner, "Draft Only", "d")
        await s.commit()

    assert (own.slug, others_private.slug) == ("mine-2", "draft-only")


# ---------- 새 시드 INSERT ----------


async def _seed(db: Any, slug: str = SLUG) -> Skill:
    return await svc.seed_system_skill(
        db,
        slug=slug,
        name="시드 요약",
        description="d",
        skill_md=svc.STARTER_SKILL_MD.format(slug=slug, name="시드 요약", description="d"),
        manifest=svc.STARTER_MANIFEST,
    )


async def test_new_seed_whose_slug_holds_a_promoted_system_skill_is_409(
    draft_store: dict, user_a: User
) -> None:
    """새 시드의 slug를 승격된 사용자 스킬이 쥐고 있으면 INSERT 전에 409다(건너뛰지 않는다)."""
    holder_id, _ = await _skill(user_a, visibility="system", published=True)

    async with get_session_factory()() as s:
        with pytest.raises(exposure.ExposureError) as caught:
            await _seed(s)
        assert (caught.value.reason, caught.value.status_code) == ("slug_conflict", 409)
        assert str(holder_id) not in str(caught.value)

    assert draft_store == {}  # 패키지도 저장하지 않았다
    async with get_session_factory()() as s:
        rows = (await s.execute(select(Skill.id, Skill.origin).where(Skill.slug == SLUG))).all()
    assert [tuple(r) for r in rows] == [(holder_id, "authored")]


async def test_existing_demoted_seed_keeps_its_branch_despite_a_system_holder(
    draft_store: dict, user_a: User
) -> None:
    """이미 있는 시드(private로 강등)는 기존 분기 그대로다 — 새 version만 두고 공개하지 않는다."""
    seed_id, _ = await _skill(None, visibility="private", origin="system_seed")
    await _skill(user_a, visibility="system", published=True)

    async with get_session_factory()() as s:
        seed = await _seed(s)
        await s.commit()

    assert seed.id == seed_id
    seed = await _fresh(Skill, seed_id)
    assert (seed.visibility, seed.published_version_id) == ("private", None)
    async with get_session_factory()() as s:
        versions = (
            await s.scalars(select(SkillVersion.version).where(SkillVersion.skill_id == seed_id))
        ).all()
    assert sorted(versions) == [1, 2]


async def test_seed_insert_racing_an_uncommitted_system_promotion_is_409(
    draft_store: dict, user_a: User, admin_user: User
) -> None:
    """사전 조회를 통과한 새 시드 INSERT가 승격 커밋을 기다린 뒤에도 같은 409다(리뷰 High).

    A가 사용자 스킬을 system으로 올리고 커밋 전에 슬롯을 쥔다. B의 시드 INSERT가 그 인덱스
    대기에 든 것을 `pg_blocking_pids`로 확인한 뒤 A를 커밋한다.
    """
    a_id, a_v1 = await _skill(user_a)

    task: asyncio.Task[Skill] | None = None
    try:
        async with get_session_factory()() as holder, get_session_factory()() as seeder:
            holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            await exposure.admin_change_visibility(
                holder,
                await holder.get(Skill, a_id),
                "system",
                version_id=a_v1,
                admin_id=admin_user.id,
            )
            task = asyncio.create_task(_seed(seeder))
            await _wait_until_blocked_by(holder_pid)
            await holder.commit()
            with pytest.raises(exposure.ExposureError) as caught:
                async with asyncio.timeout(10):
                    await task
            assert (caught.value.reason, caught.value.status_code) == ("slug_conflict", 409)
            assert await seeder.scalar(text("SELECT 1")) == 1  # savepoint — 트랜잭션은 살아 있다
            await seeder.rollback()
    finally:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    assert draft_store == {}
    async with get_session_factory()() as s:
        rows = (await s.execute(select(Skill.id, Skill.visibility).where(Skill.slug == SLUG))).all()
    assert [tuple(r) for r in rows] == [(a_id, "system")]


async def test_demotion_race_409_then_commit_keeps_the_expose_system_approval(
    user_a: User, user_b: User, admin_user: User
) -> None:
    """강등이 org 슬롯 경합으로 409가 된 뒤 같은 세션을 커밋해도 바뀐 것이 없다(리뷰 Medium).

    savepoint를 만들기 전에 flush된 쓰기는 savepoint 롤백으로 되돌아가지 않는다. 그래서 강등은
    visibility를 먼저 옮기고, 성공한 뒤에 `expose_system` 승인을 철회한다.
    """
    a_id, a_v1 = await _skill(user_a, visibility="system", published=True)
    approval_id = await _request(a_v1, "expose_system", status="approved", by=user_a)
    b_id, b_v1 = await _skill(user_b)

    task: asyncio.Task[None] | None = None

    async def demote_a(session: Any) -> None:
        await exposure.admin_change_visibility(
            session, await session.get(Skill, a_id), "org", version_id=None, admin_id=admin_user.id
        )

    try:
        async with get_session_factory()() as holder, get_session_factory()() as demoter:
            holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            outcome = await exposure.publish_version(
                holder,
                await holder.get(Skill, b_id),
                await holder.get(SkillVersion, b_v1),
                actor_id=user_b.id,
            )
            assert outcome.status == "published"
            task = asyncio.create_task(demote_a(demoter))
            await _wait_until_blocked_by(holder_pid)
            await holder.commit()
            with pytest.raises(exposure.ExposureError) as caught:
                async with asyncio.timeout(10):
                    await task
            assert caught.value.reason == "slug_conflict"
            await demoter.commit()  # 계약 밖의 호출자 — 그래도 반쯤 된 강등이 남지 않는다
    finally:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    a = await _fresh(Skill, a_id)
    assert (a.visibility, a.published_version_id) == ("system", a_v1)
    approval = await _fresh(SkillReviewRequest, approval_id)
    assert (approval.status, approval.decided_by) == ("approved", None)
