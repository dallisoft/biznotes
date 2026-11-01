# 신규(#135, docs/plans/skill-chat-entry-reason/). 필요: PostgreSQL·Valkey(PG 테스트).
"""스킬 응답의 호출자 기준 `chat_entry_reason` — 라우트 함수를 실제 PostgreSQL로 직접 부른다.

판정 규칙은 `tests/test_chat_entry.py`가 고정한다. 여기서는 스킬·관리자 라우터가 같은 판정을
쿼리 수를 늘리지 않고 싣는지, 비소유자에게 미공개 freeze가 사유 값으로 새지 않는지를 본다.
스토리지 쓰기만 대역으로 바꾼다. `create_all` 기반이라 Alembic 마이그레이션 검증이 아니다.
"""

import io
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi import UploadFile
from sqlalchemy import event, select

from biznotes_core.api.v1 import skills as skills_api
from biznotes_core.api.v1.admin import skills as admin_api
from biznotes_core.models import Agent, Skill, SkillReviewRequest, SkillVersion, User
from biznotes_core.schemas.skill import AdminSkillUpdate, SkillCreate, SkillUpdate
from biznotes_core.services.agents.chat_entry import (
    list_chat_entry_agents,
    resolve_chat_entry,
    resolve_wrapper_chat_entries,
)
from biznotes_core.services.skills import package as pkg

TOPIC = {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}
# 채팅 메시지를 한 파라미터로 옮길 수 없다 → skill_params_incompatible
TWO_REQUIRED = {
    "type": "object",
    "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
    "required": ["a", "b"],
}


@pytest.fixture(autouse=True)
def _no_object_storage(monkeypatch) -> None:
    monkeypatch.setattr(pkg, "save_draft", lambda skill_id, files: None)


async def _version(db: Any, skill: Skill, number: int, schema: dict[str, Any]) -> SkillVersion:
    version = SkillVersion(
        skill_id=skill.id,
        version=number,
        package_uri=f"versions/{uuid.uuid4()}/",
        skill_md_hash="h",
        package_hash=f"p{number}",
        manifest={"params_schema": schema},
        file_tree=[],
        security_report={},
        status="draft_frozen",
    )
    db.add(version)
    await db.flush()
    return version


async def _skill(
    db: Any,
    slug: str,
    *,
    owner: User | None,
    visibility: str = "system",
    published: dict[str, Any] | None = TOPIC,
    latest: dict[str, Any] | None = None,
    frozen: bool = True,
) -> Skill:
    """스킬 하나. `published`면 v1을 공개하고, `latest`면 미공개 v2를 더 freeze한다.

    `published=None`이면 v1을 freeze만 하고 공개 포인터를 두지 않는다. `frozen=False`면 freeze
    전 draft라 version도 wrapper도 없다. wrapper는 `_upsert_wrapper`처럼 최신 freeze를 가리킨다.
    """
    skill = Skill(
        owner_id=owner.id if owner else None,
        name=slug,
        description="",
        slug=slug,
        visibility=visibility,
    )
    db.add(skill)
    await db.flush()
    if not frozen:
        return skill
    first = await _version(db, skill, 1, published or TOPIC)
    newest = await _version(db, skill, 2, latest) if latest is not None else first
    skill.current_version_id = newest.id
    skill.published_version_id = first.id if published is not None else None
    db.add(
        Agent(
            name=slug,
            kind="skill",
            source_kind="skill_package",
            definition={},
            params_schema={},
            scope="system" if visibility == "system" else "user",
            owner_id=skill.owner_id,
            enabled=True,
            skill_version_id=newest.id,
        )
    )
    await db.flush()
    return skill


@pytest.fixture
async def world(db, user_a: User) -> dict[str, Skill]:
    """user_a 소유 스킬과 시드(system, 소유자 없음) 스킬."""
    w = {
        "seed": await _skill(db, "seed", owner=None),
        # 공개 v1은 채팅 가능, 소유자의 미공개 v2는 불가
        "pub_ok": await _skill(db, "pub-ok", owner=user_a, latest=TWO_REQUIRED),
        # 공개 v1은 불가, 소유자의 미공개 v2는 가능
        "pub_bad": await _skill(db, "pub-bad", owner=user_a, published=TWO_REQUIRED, latest=TOPIC),
        "unpublished": await _skill(db, "unpublished", owner=user_a, published=None),
        "org": await _skill(db, "org", owner=user_a, visibility="org"),
        "private": await _skill(db, "private", owner=user_a, visibility="private"),
        "draft": await _skill(db, "draft", owner=user_a, visibility="private", frozen=False),
    }
    await db.commit()
    return w


async def _listed(db: Any, user: User) -> dict[str, str | None]:
    outs = await skills_api.list_skills(q=None, tag=None, visibility=None, user=user, db=db)
    return {out.slug: out.chat_entry_reason for out in outs}


async def _admin_listed(db: Any, admin: User) -> dict[str, str | None]:
    outs = await admin_api.list_skills_admin(db=db, admin=admin)
    return {out.slug: out.chat_entry_reason for out in outs}


async def test_owner_sees_the_latest_freeze(db, world, user_a: User) -> None:
    assert await _listed(db, user_a) == {
        "seed": "ok",
        "pub-ok": "skill_params_incompatible",  # 소유자는 최신 freeze(v2)로 실행한다
        "pub-bad": "ok",
        "unpublished": "skill_not_published",
        "org": "ok",  # 본인 스킬은 최신 freeze로 허용(#410)
        "private": "ok",
        "draft": None,  # freeze 전 — wrapper가 없다
    }


async def test_non_owner_is_judged_by_the_published_version(db, world, user_b: User) -> None:
    assert await _listed(db, user_b) == {
        "seed": "ok",
        "pub-ok": "ok",  # 미공개 v2의 params는 새지 않는다
        "pub-bad": "skill_params_incompatible",
        "unpublished": "skill_not_published",
        "org": "skill_not_published",  # 노출 version 미승인(#410 D1)
    }  # private·draft는 지금처럼 목록에서 빠진다


async def test_admin_list_uses_the_admin_as_caller(db, world, admin_user: User) -> None:
    assert await _admin_listed(db, admin_user) == {
        "seed": "ok",
        "pub-ok": "ok",
        "pub-bad": "skill_params_incompatible",
        "unpublished": "skill_not_published",
        "org": "skill_not_published",  # 관리자도 org 비소유자다(#410 D1)
        "private": "not_visible",  # 관리자 예외 없음(#85 정본)
        "draft": None,
    }


async def test_owner_listed_through_the_admin_router_matches(db, world, user_a: User) -> None:
    """관리자 라우터도 호출자 기준이다 — 소유자가 부르면 소유자 판정이 나온다."""
    assert await _admin_listed(db, user_a) == await _listed(db, user_a)


async def _approve(db: Any, version_id: uuid.UUID | None) -> None:
    assert version_id is not None
    db.add(
        SkillReviewRequest(
            version_id=version_id, kind="expose_org", target_visibility="org", status="approved"
        )
    )
    await db.flush()


async def test_approved_org_skill_is_ok_for_non_owners(
    db, world, user_a: User, user_b: User, admin_user: User
) -> None:
    """#410 D1 — 노출 version이 승인되면 비소유자(관리자 포함)의 사유가 `ok`가 된다."""
    await _approve(db, world["org"].published_version_id)
    await db.commit()
    assert (await _listed(db, user_b))["org"] == "ok"
    assert (await _admin_listed(db, admin_user))["org"] == "ok"
    assert (await _listed(db, user_a))["org"] == "ok"


async def test_detail_and_patch_carry_the_reason(db, world, user_a: User, user_b: User) -> None:
    skill = world["pub_ok"]
    detail = await skills_api.get_skill(skill_id=skill.id, user=user_b, db=db)
    assert detail.chat_entry_reason == "ok"
    patched = await skills_api.update_skill(
        skill_id=skill.id, body=SkillUpdate(name="이름만 바꿈"), user=user_a, db=db
    )
    assert not isinstance(patched, skills_api.JSONResponse)
    assert patched.chat_entry_reason == "skill_params_incompatible"
    # 응답 JSON에는 enum 객체가 아니라 문자열이 실린다
    assert patched.model_dump(mode="json")["chat_entry_reason"] == "skill_params_incompatible"
    assert type(patched.chat_entry_reason) is str


async def test_publishing_to_system_turns_the_reason_ok(db, world, user_b: User) -> None:
    skill = world["unpublished"]
    before = await skills_api.get_skill(skill_id=skill.id, user=user_b, db=db)
    assert before.chat_entry_reason == "skill_not_published"
    skill.published_version_id = skill.current_version_id
    await db.commit()
    after = await skills_api.get_skill(skill_id=skill.id, user=user_b, db=db)
    assert after.chat_entry_reason == "ok"


async def test_admin_patch_and_pointer_responses(
    db, world, admin_user: User, user_a: User
) -> None:
    skill = world["org"]
    disabled = await admin_api.update_skill_admin(
        skill_id=skill.id, body=AdminSkillUpdate(enabled=False), db=db, admin=admin_user
    )
    # 꺼진 타인 스킬은 관리자에게도 비가시다(관리자 예외 없음). 소유자는 꺼짐 사유를 본다
    assert disabled.chat_entry_reason == "not_visible"
    owner_view = await skills_api.get_skill(skill_id=skill.id, user=user_a, db=db)
    assert owner_view.chat_entry_reason == "skill_disabled"
    withdrawn = await admin_api.set_published_version_admin(
        skill_id=world["pub_ok"].id,
        body=admin_api.AdminPublishedVersionSet(version_id=None),
        db=db,
        admin=admin_user,
    )
    assert withdrawn.chat_entry_reason == "skill_not_published"


async def test_create_and_import_have_no_wrapper_yet(db, user_a: User) -> None:
    created = await skills_api.create_skill(body=SkillCreate(name="새 스킬"), user=user_a, db=db)
    assert created.wrapper_agent_id is None and created.chat_entry_reason is None

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("imported/SKILL.md", "---\nname: imported\ndescription: 가져옴\n---\n본문\n")
    upload = UploadFile(file=io.BytesIO(buf.getvalue()), filename="imported.zip")
    imported = await skills_api.import_skill_zip(
        file=upload, selected_skill_path=None, user=user_a, db=db
    )
    assert imported.wrapper_agent_id is None and imported.chat_entry_reason is None
    assert "chat_entry_reason" in imported.model_dump()


async def test_batch_matches_single_resolution(db, world, user_a, user_b, admin_user) -> None:
    rows = await db.execute(select(Agent.id).where(Agent.source_kind == "skill_package"))
    wrappers = list(rows.scalars().all())
    assert len(wrappers) == 6
    for user in (user_a, user_b, admin_user):
        batch = await resolve_wrapper_chat_entries(db, user, wrappers)
        for wrapper_id in wrappers:
            agent = await db.get(Agent, wrapper_id)
            assert batch[wrapper_id] == await resolve_chat_entry(db, user, agent)


async def test_unknown_wrapper_id_is_not_visible(db, user_a: User) -> None:
    missing = uuid.uuid4()
    entries = await resolve_wrapper_chat_entries(db, user_a, [missing])
    assert entries[missing].reason.value == "not_visible"
    assert await resolve_wrapper_chat_entries(db, user_a, []) == {}


@contextmanager
def _count_statements(db: Any) -> Iterator[list[str]]:
    engine = db.bind.sync_engine
    statements: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before)


async def test_list_query_count_does_not_grow_with_skills(
    db, user_a: User, user_b: User, admin_user: User
) -> None:
    """N+1 없음 — 스킬 1개와 5개 목록의 SQL 문 수가 같다(사용자·관리자 목록 모두)."""
    await _skill(db, "only", owner=user_a)
    await db.commit()
    with _count_statements(db) as one:
        assert len(await _listed(db, user_b)) == 1
    with _count_statements(db) as admin_one:
        assert len(await _admin_listed(db, admin_user)) == 1
    for n in range(4):
        await _skill(db, f"more-{n}", owner=user_a, latest=TWO_REQUIRED)
    await db.commit()
    with _count_statements(db) as five:
        assert len(await _listed(db, user_b)) == 5
    with _count_statements(db) as admin_five:
        assert len(await _admin_listed(db, admin_user)) == 5
    assert len(one) == len(five)
    assert len(admin_one) == len(admin_five)


async def test_org_approval_lookup_does_not_grow_with_skills(
    db, user_a: User, user_b: User
) -> None:
    """승인 조회도 한 번이다 — 승인된 org 스킬 1개와 5개의 SQL 문 수가 같다(응답·채팅 목록)."""

    async def approved_org(slug: str) -> None:
        skill = await _skill(db, slug, owner=user_a, visibility="org")
        await _approve(db, skill.published_version_id)

    await approved_org("org-0")
    await db.commit()
    with _count_statements(db) as one:
        assert await _listed(db, user_b) == {"org-0": "ok"}
        assert len(await list_chat_entry_agents(db, user_b)) == 1
    for n in range(1, 5):
        await approved_org(f"org-{n}")
    await db.commit()
    with _count_statements(db) as five:
        assert set((await _listed(db, user_b)).values()) == {"ok"}
        assert len(await list_chat_entry_agents(db, user_b)) == 5
    assert len(one) == len(five)
