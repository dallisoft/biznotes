# 신규(#85, docs/plans/skill-chat-entry/ 단계 5). 필요: PostgreSQL·Valkey(PG 테스트).
"""채팅 진입 목록·일괄 적재 쿼리 회귀 (재리뷰 m3).

`list_chat_entry_agents`와 `resolve_chat_entries`는 `execute`로 행을 적재한다. 판정 규칙은
`tests/test_chat_entry.py`가 순수 함수로 고정하고, 여기서는 실제 PostgreSQL에서 두 가지를 본다.

1. 후보 SQL(조인·LEFT JOIN·prefilter)이 허용된 것만, 빠짐없이 올린다.
2. 같은 데이터에서 일괄(`resolve_chat_entries`)과 단건(`resolve_chat_entry`)이 같은 판정을 낸다.
3. 본인·org 스킬(#410): org 비소유자의 승인 조회, 박제 재검사, 이름 풀기
   (`resolve_chat_entry_by_name`).

`create_all` 기반이라 Alembic 마이그레이션 검증이 아니다.
"""

import uuid
from typing import Any

import pytest
from fastapi import HTTPException

from biznotes_core.models import Agent, Skill, SkillReviewRequest, SkillVersion, User
from biznotes_core.models.skill import SKILL_RUNTIME_LEGACY, SKILL_RUNTIME_STANDARD
from biznotes_core.services.agents.chat_entry import (
    ChatEntry,
    ChatEntryReason,
    assert_chat_entry_allowed,
    list_chat_entry_agents,
    resolve_chat_entries,
    resolve_chat_entry,
    resolve_chat_entry_by_name,
    resolve_wrapper_chat_entries,
)

R = ChatEntryReason
TOPIC = {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}
TWO_REQUIRED = {
    "type": "object",
    "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
    "required": ["a", "b"],
}
CALLABLE = {
    "nodes": [{"id": "a", "type": "react_agent", "allowed_tools": ["rag_search"]}],
    "edges": [],
}


async def _version(
    db: Any,
    skill: Skill,
    number: int,
    *,
    label: str,
    profile: str = SKILL_RUNTIME_LEGACY,
    schema: dict[str, Any] | None = None,
    rag_query: str | None = None,
) -> SkillVersion:
    manifest: dict[str, Any] = {"params_schema": schema or TOPIC}
    if rag_query is not None:
        manifest["rag_query"] = rag_query
    version = SkillVersion(
        skill_id=skill.id,
        version=number,
        package_uri=f"versions/{uuid.uuid4()}/",
        skill_md_hash="h",
        package_hash=f"p{number}",
        manifest={} if profile == SKILL_RUNTIME_STANDARD else manifest,
        file_tree=[],
        security_report={},
        status="draft_frozen",
        runtime_profile=profile,
        standard_metadata={"name": label, "description": f"{label} 설명"},
    )
    db.add(version)
    await db.flush()
    return version


async def _skill(
    db: Any,
    slug: str,
    *,
    owner: User | None = None,
    visibility: str = "system",
    enabled: bool = True,
    publish: bool = True,
    profile: str = SKILL_RUNTIME_LEGACY,
    schema: dict[str, Any] | None = None,
    newer: dict[str, Any] | None = None,
) -> tuple[Skill, Agent, SkillVersion, SkillVersion | None]:
    """스킬과 wrapper. `newer`면 공개 뒤 새 freeze를 하나 더 만든다(wrapper는 최신을 가리킨다)."""
    skill = Skill(
        owner_id=owner.id if owner else None,
        name=f"{slug} 행 이름",
        description=f"{slug} 행 설명",
        slug=slug,
        visibility=visibility,
        enabled=enabled,
    )
    db.add(skill)
    await db.flush()
    first = await _version(db, skill, 1, label=f"{slug}-v1", profile=profile, schema=schema)
    latest = (
        await _version(db, skill, 2, label=f"{slug}-v2", **newer) if newer is not None else None
    )
    skill.current_version_id = (latest or first).id
    skill.published_version_id = first.id if publish else None
    wrapper = Agent(
        name=f"{slug}-wrapper-latest",
        description="wrapper 최신 freeze 설명",
        icon="🧩",
        kind="skill",
        source_kind="skill_package",
        definition={},
        params_schema={},
        scope="system" if visibility == "system" else "user",
        owner_id=skill.owner_id,
        enabled=enabled,
        skill_version_id=(latest or first).id,
    )
    db.add(wrapper)
    await db.flush()
    return skill, wrapper, first, latest


async def _review(
    db: Any, version: SkillVersion, *, kind: str = "expose_org", status: str = "approved"
) -> None:
    """version 하나의 검토 요청 행(D8). 기본은 승인된 `expose_org`다."""
    target = None if kind == "scripts" else kind.removeprefix("expose_")
    db.add(
        SkillReviewRequest(
            version_id=version.id, kind=kind, target_visibility=target, status=status
        )
    )
    await db.flush()


async def _workflow(
    db: Any,
    name: str,
    *,
    owner: User | None = None,
    enabled: bool = True,
    definition: dict[str, Any] | None = None,
    handle: str | None = None,
) -> Agent:
    agent = Agent(
        name=name,
        handle=handle,
        kind="workflow",
        definition=definition if definition is not None else CALLABLE,
        params_schema={},
        scope="user" if owner else "system",
        owner_id=owner.id if owner else None,
        enabled=enabled,
    )
    db.add(agent)
    await db.flush()
    return agent


@pytest.fixture
async def world(db, user_a: User) -> dict[str, Any]:
    """user_a가 system 스킬 하나(current ≠ published), org 스킬 둘(미승인·승인), private 스킬,
    private workflow·draft를 가진다."""
    w: dict[str, Any] = {}
    w["sys"] = await _skill(db, "sys-legacy")
    w["owned"] = await _skill(
        db,
        "sys-owned",
        owner=user_a,
        newer={
            "schema": {"type": "object", "properties": {"q": {"type": "string"}}},
            "rag_query": "{params.q}",  # 채팅 대상이 검색어에 들어가야 한다(#115)
        },
    )
    # 비소유자에게 보이지만 거부되는 행(노출 version 미승인, #410 D1) — 소유자의 새 freeze(v2)가
    # 있어 표시 필드 누출을 본다
    w["org"] = await _skill(db, "org-skill", owner=user_a, visibility="org", newer={})
    # 노출 version(v1)이 승인된 org 스킬 — 비소유자는 v1, 소유자는 v2를 실행한다
    w["org_ok"] = await _skill(db, "org-approved", owner=user_a, visibility="org", newer={})
    await _review(db, w["org_ok"][2])
    w["disabled"] = await _skill(db, "sys-disabled", enabled=False)
    w["unpublished"] = await _skill(db, "sys-unpublished", publish=False)
    w["standard"] = await _skill(db, "sys-standard", profile=SKILL_RUNTIME_STANDARD)
    w["params"] = await _skill(db, "sys-params", schema=TWO_REQUIRED)
    w["private"] = await _skill(db, "private-skill", owner=user_a, visibility="private")
    w["wf_system"] = await _workflow(db, "wf-system")
    w["wf_private"] = await _workflow(db, "wf-private", owner=user_a)
    w["wf_draft"] = await _workflow(db, "wf-draft", owner=user_a, enabled=False)
    w["wf_shape"] = await _workflow(db, "wf-shape", definition={"nodes": [], "edges": []})
    await db.commit()
    return w


def _agents(world: dict[str, Any]) -> list[Agent]:
    return [v[1] if isinstance(v, tuple) else v for v in world.values()]


async def test_list_for_a_non_owner(db, world, user_b: User) -> None:
    listed = await list_chat_entry_agents(db, user_b)
    assert [agent.id for agent, _ in listed] == [
        world["org_ok"][1].id,
        world["sys"][1].id,
        world["owned"][1].id,
        world["wf_system"].id,
    ]  # 표시 이름 순 — org-approved-v1 < sys-legacy-v1 < sys-owned-v1 < wf-system
    by_id = {agent.id: entry for agent, entry in listed}
    owned_skill, owned_wrapper, owned_v1, _ = world["owned"]
    entry = by_id[owned_wrapper.id]
    assert entry.skill_version_id == owned_v1.id  # 비소유자는 공개 version
    assert entry.param_key == "topic"
    assert (entry.display_name, entry.display_description) == ("sys-owned-v1", "sys-owned-v1 설명")
    assert entry.display_name != owned_wrapper.name
    assert by_id[world["wf_system"].id].reason == R.OK
    org_ok = by_id[world["org_ok"][1].id]  # 승인된 노출 version(v1)이다
    assert (org_ok.skill_version_id, org_ok.display_name) == (
        world["org_ok"][2].id,
        "org-approved-v1",
    )
    assert (org_ok.slash_name, org_ok.source) == ("org-approved", "org")
    assert (entry.slash_name, entry.source) == ("sys-owned", "system")


async def test_list_for_the_system_skill_owner(db, world, user_a: User) -> None:
    listed = await list_chat_entry_agents(db, user_a)
    by_id = {agent.id: entry for agent, entry in listed}
    assert set(by_id) == {
        world["sys"][1].id,
        world["owned"][1].id,
        world["wf_system"].id,
        world["wf_private"].id,
        world["org"][1].id,
        world["org_ok"][1].id,
        world["private"][1].id,
    }  # 본인 org·private 스킬은 승인과 무관하게 오른다(#410). 소유자 draft workflow는 없다
    _, owned_wrapper, owned_v1, owned_v2 = world["owned"]
    assert owned_v1.id != owned_v2.id
    entry = by_id[owned_wrapper.id]
    assert entry.skill_version_id == owned_v2.id  # 소유자는 최신 freeze(B1)
    assert entry.param_key == "q"
    assert entry.display_name == "sys-owned-v2"
    assert (entry.slash_name, entry.source) == ("sys-owned", "system")  # system이 소유보다 먼저
    for key in ("org", "org_ok"):  # 소유자는 최신 freeze(v2)
        _, wrapper, _, v2 = world[key]
        assert (by_id[wrapper.id].skill_version_id, by_id[wrapper.id].source) == (v2.id, "own")
    private = by_id[world["private"][1].id]
    assert (private.skill_version_id, private.slash_name, private.source) == (
        world["private"][2].id,
        "private-skill",
        "own",
    )
    assert (by_id[world["wf_private"].id].source, by_id[world["wf_system"].id].source) == (
        "own",
        "system",
    )


async def test_admin_does_not_list_another_users_private_workflow(
    db, world, admin_user: User
) -> None:
    listed = {agent.id for agent, _ in await list_chat_entry_agents(db, admin_user)}
    assert listed == {
        world["sys"][1].id,
        world["owned"][1].id,
        world["wf_system"].id,
        world["org_ok"][1].id,  # 관리자도 org 비소유자다 — 승인된 org만(#410 D1)
    }


async def test_standard_skill_is_listed_when_the_runtime_is_on(
    db, world, user_b: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biznotes_core.config import reset_settings

    monkeypatch.setenv("SKILL_STANDARD_RUNTIME_ENABLED", "true")
    reset_settings()
    try:
        listed = {agent.id: entry for agent, entry in await list_chat_entry_agents(db, user_b)}
    finally:
        monkeypatch.delenv("SKILL_STANDARD_RUNTIME_ENABLED")
        reset_settings()
    assert listed[world["standard"][1].id].param_key == "request"


async def test_batch_matches_single_resolution(
    db, world, user_a: User, user_b: User, admin_user: User
) -> None:
    agents = _agents(world)
    enabled = {agent.id for agent in agents if agent.enabled}
    for user in (user_a, user_b, admin_user):
        batch = await resolve_chat_entries(db, user, agents)
        single = {agent.id: await resolve_chat_entry(db, user, agent) for agent in agents}
        assert batch == single, user.name
        # 목록 = 허용된 판정 − 소유자 draft(판정상 허용이지만 목록에 올리지 않는다)
        listed = {agent.id: entry for agent, entry in await list_chat_entry_agents(db, user)}
        expected = {i: e for i, e in batch.items() if e.allowed and i in enabled}
        assert listed == expected, user.name


async def test_batch_reasons_for_a_non_owner(db, world, user_b: User) -> None:
    batch = await resolve_chat_entries(db, user_b, _agents(world))
    reasons = {name: batch[a.id].reason for name, a in zip(world, _agents(world), strict=True)}
    assert reasons == {
        "sys": R.OK,
        "owned": R.OK,
        "org": R.SKILL_NOT_PUBLISHED,  # 노출 version 미승인(#410 D1)
        "org_ok": R.OK,
        "disabled": R.NOT_VISIBLE,
        "unpublished": R.SKILL_NOT_PUBLISHED,
        "standard": R.SKILL_RUNTIME_DISABLED,
        "params": R.SKILL_PARAMS_INCOMPATIBLE,
        "private": R.NOT_VISIBLE,
        "wf_system": R.OK,
        "wf_private": R.NOT_VISIBLE,
        "wf_draft": R.NOT_VISIBLE,
        "wf_shape": R.WORKFLOW_SHAPE,
    }
    for hidden in ("disabled", "private"):
        entry = batch[world[hidden][1].id]
        assert entry.display_name is None and entry.skill_version_id is None
    org = batch[world["org"][1].id]  # 거부돼도 표시 필드는 공개 version에서 온다(N2)
    assert (org.display_name, org.skill_version_id) == ("org-skill-v1", None)


async def test_batch_owner_sees_detailed_reasons(db, world, user_a: User) -> None:
    batch = await resolve_chat_entries(db, user_a, _agents(world))
    assert batch[world["private"][1].id].reason == R.OK  # 본인 스킬은 허용(#410)
    assert batch[world["org"][1].id].reason == R.OK  # 소유자에게는 승인이 필요 없다
    assert batch[world["org"][1].id].display_name == "org-skill-v2"  # 소유자는 최신 freeze
    assert batch[world["wf_draft"].id].reason == R.OK  # 소유자 draft는 판정상 허용(목록에는 없다)


async def test_wrapper_whose_version_is_gone_is_not_visible(db, world, user_b: User) -> None:
    """조인에서 빠지는 wrapper(version 없음)는 일괄에서도 단건에서도 `NOT_VISIBLE`이다."""
    _, wrapper, _, _ = world["sys"]
    wrapper.skill_version_id = None
    await db.commit()
    assert (await resolve_chat_entries(db, user_b, [wrapper]))[wrapper.id].reason == R.NOT_VISIBLE
    assert (await resolve_chat_entry(db, user_b, wrapper)).reason == R.NOT_VISIBLE
    assert wrapper.id not in {a.id for a, _ in await list_chat_entry_agents(db, user_b)}


# ---------- 본인·org 스킬 (#410) ----------


def _full(entry: ChatEntry) -> tuple[Any, ...]:
    """동등 비교에서 빠지는 표시 메타데이터(`slash_name`·`source`)까지 함께 본다."""
    return (entry, entry.slash_name, entry.source)


async def test_list_batch_and_single_agree_on_every_scope(
    db, world, user_a: User, user_b: User, admin_user: User
) -> None:
    """본인 private·org 승인·org 미승인·타인 private·org 소유자에서 네 적재 경로가 같다."""
    agents = _agents(world)
    wrapper_ids = [a.id for a in agents if a.source_kind == "skill_package"]
    enabled = {a.id for a in agents if a.enabled}
    for user in (user_a, user_b, admin_user):
        single = {a.id: _full(await resolve_chat_entry(db, user, a)) for a in agents}
        batch = await resolve_chat_entries(db, user, agents)
        assert {i: _full(e) for i, e in batch.items()} == single, user.name
        by_wrapper = await resolve_wrapper_chat_entries(db, user, wrapper_ids)
        assert {i: _full(e) for i, e in by_wrapper.items()} == {
            i: single[i] for i in wrapper_ids
        }, user.name
        listed = {a.id: _full(e) for a, e in await list_chat_entry_agents(db, user)}
        assert listed == {
            i: full for i, full in single.items() if full[0].allowed and i in enabled
        }, user.name

    cases = {
        # (사용자, 스킬) → (사유, source)
        ("a", "private"): (R.OK, "own"),  # 본인 private
        ("a", "org"): (R.OK, "own"),  # org 소유자 — 승인 불필요
        ("a", "org_ok"): (R.OK, "own"),
        ("b", "private"): (R.NOT_VISIBLE, None),  # 타인 private
        ("b", "org"): (R.SKILL_NOT_PUBLISHED, None),  # org 미승인
        ("b", "org_ok"): (R.OK, "org"),  # org 승인
        ("admin", "private"): (R.NOT_VISIBLE, None),
        ("admin", "org"): (R.SKILL_NOT_PUBLISHED, None),
        ("admin", "org_ok"): (R.OK, "org"),
    }
    users = {"a": user_a, "b": user_b, "admin": admin_user}
    for (who, key), (reason, source) in cases.items():
        entry = await resolve_chat_entry(db, users[who], world[key][1])
        assert (entry.reason, entry.source) == (reason, source), (who, key)


async def test_gate_404_for_another_users_private_wrapper(
    db, world, user_a: User, user_b: User, admin_user: User
) -> None:
    wrapper = world["private"][1]
    assert (await assert_chat_entry_allowed(db, user_a, wrapper)).allowed
    for user in (user_b, admin_user):
        with pytest.raises(HTTPException) as exc:
            await assert_chat_entry_allowed(db, user, wrapper)
        assert (exc.value.status_code, exc.value.detail) == (404, "Agent not found")


async def test_owner_skill_needs_a_freeze_but_no_pointer(db, user_a: User, user_b: User) -> None:
    """freeze가 없는 draft는 목록에 없다. freeze가 있으면 포인터 없이 최신 freeze로 오른다."""
    db.add(
        Skill(
            owner_id=user_a.id,
            name="초안",
            description="",
            slug="draft-only",
            visibility="private",
        )
    )
    _, wrapper, _, latest = await _skill(
        db, "mine-unpublished", owner=user_a, visibility="private", publish=False, newer={}
    )
    await db.commit()
    listed = await list_chat_entry_agents(db, user_a)
    assert [(a.id, e.skill_version_id, e.slash_name) for a, e in listed] == [
        (wrapper.id, latest.id, "mine-unpublished")
    ]
    assert (await resolve_chat_entry_by_name(db, user_a, "draft-only")).status == "none"
    assert await list_chat_entry_agents(db, user_b) == []


@pytest.mark.parametrize(
    ("kind", "status", "allowed"),
    [
        ("expose_org", "approved", True),
        ("expose_system", "approved", True),
        ("expose_org", "pending", False),
        ("expose_org", "rejected", False),
        ("expose_org", "withdrawn", False),
        ("expose_org", "cancelled", False),
        ("scripts", "approved", False),
    ],
)
async def test_org_approval_counts_only_approved_expose_requests(
    db, user_a: User, user_b: User, kind: str, status: str, allowed: bool
) -> None:
    """D1 — 노출 version에 approved `expose_*`가 있어야 한다(S-HCI-12와 같은 조건)."""
    _, wrapper, v1, v2 = await _skill(db, "org-review", owner=user_a, visibility="org", newer={})
    await _review(db, v1, kind=kind, status=status)
    await _review(db, v2)  # 미공개 freeze의 승인은 노출 version을 덮지 않는다
    await db.commit()
    entry = await resolve_chat_entry(db, user_b, wrapper)
    assert entry.allowed is allowed
    assert entry.reason == (R.OK if allowed else R.SKILL_NOT_PUBLISHED)
    listed = {a.id for a, _ in await list_chat_entry_agents(db, user_b)}
    assert (wrapper.id in listed) is allowed
    match = await resolve_chat_entry_by_name(db, user_b, "org-review")
    assert match.status == ("one" if allowed else "none")


async def test_pinned_org_recheck_uses_the_pinned_version_approval(
    db, user_a: User, user_b: User
) -> None:
    """미승인 박제는 404, 승인된 박제는 포인터가 옮겨 가도 통과, 소유자 박제는 enabled만 본다."""
    skill, wrapper, v1, v2 = await _skill(
        db, "org-pinned", owner=user_a, visibility="org", newer={}
    )
    await _review(db, v1)
    await db.commit()
    staged = await assert_chat_entry_allowed(db, user_b, wrapper)
    assert staged.skill_version_id == v1.id
    skill.published_version_id = v2.id  # 판정과 박제 사이에 포인터가 미승인 version으로 옮겨 갔다
    await db.commit()
    assert (await resolve_chat_entry(db, user_b, wrapper)).reason == R.SKILL_NOT_PUBLISHED
    entry = await assert_chat_entry_allowed(db, user_b, wrapper, pinned_version_id=v1.id)
    assert entry.allowed and entry.skill_version_id == v1.id
    with pytest.raises(HTTPException) as exc:
        await assert_chat_entry_allowed(db, user_b, wrapper, pinned_version_id=v2.id)
    assert (exc.value.status_code, exc.value.detail) == (404, "Agent not found")
    skill.published_version_id = None  # 노출 철회 — 승인된 박제도 거부
    await db.commit()
    with pytest.raises(HTTPException):
        await assert_chat_entry_allowed(db, user_b, wrapper, pinned_version_id=v1.id)
    owner = await assert_chat_entry_allowed(db, user_a, wrapper, pinned_version_id=v1.id)
    assert owner.allowed and owner.source == "own"
    skill.enabled = False
    wrapper.enabled = False
    await db.commit()
    with pytest.raises(HTTPException):
        await assert_chat_entry_allowed(db, user_a, wrapper, pinned_version_id=v1.id)


# ---------- 이름 풀기 (#410 D3) ----------


@pytest.fixture
async def named(db, user_a: User, user_b: User, admin_user: User) -> dict[str, Any]:
    """같은 이름 `dup`이 범위마다 있다 — user_a 본인 스킬·workflow, system, user_b의 승인된 org,
    관리자 private. 그 밖에 이름이 하나뿐인 스킬과 미승인 org 스킬."""
    n: dict[str, Any] = {}
    n["own"] = await _skill(db, "dup", owner=user_a, visibility="private", publish=False)
    n["system"] = await _skill(db, "dup")
    n["org"] = await _skill(db, "dup", owner=user_b, visibility="org")
    await _review(db, n["org"][2])
    n["admin_private"] = await _skill(db, "dup", owner=admin_user, visibility="private")
    n["own_wf"] = await _workflow(db, "wf-dup", owner=user_a, handle="dup")
    n["no_handle"] = await _workflow(db, "wf-no-handle")
    n["only"] = await _skill(db, "only-mine", owner=user_a, visibility="private")
    n["solo_org"] = await _skill(db, "org-solo", owner=user_b, visibility="org")
    await db.commit()
    return n


def _ids(match: Any) -> list[uuid.UUID]:
    return [agent.id for agent, _ in match.matches]


async def test_name_resolves_to_many_in_own_system_org_order(
    db, named, user_a: User, user_b: User, admin_user: User
) -> None:
    match = await resolve_chat_entry_by_name(db, user_a, "dup")
    assert match.status == "many"
    # own(표시 이름 dup-v1 < wf-dup) > system > org. 관리자 private은 섞이지 않는다
    assert _ids(match) == [
        named["own"][1].id,
        named["own_wf"].id,
        named["system"][1].id,
        named["org"][1].id,
    ]
    assert [e.source for _, e in match.matches] == ["own", "own", "system", "org"]
    assert {e.slash_name for _, e in match.matches} == {"dup"}
    assert all(e.allowed for _, e in match.matches)
    # user_b에게 자기 org 스킬은 own이다. user_a의 private·workflow, 관리자 private은 없다
    match_b = await resolve_chat_entry_by_name(db, user_b, "dup")
    assert (match_b.status, _ids(match_b)) == ("many", [named["org"][1].id, named["system"][1].id])
    assert [e.source for _, e in match_b.matches] == ["own", "system"]
    match_admin = await resolve_chat_entry_by_name(db, admin_user, "dup")
    assert _ids(match_admin) == [
        named["admin_private"][1].id,
        named["system"][1].id,
        named["org"][1].id,
    ]


async def test_name_resolves_to_one_or_none(db, named, user_a: User, user_b: User) -> None:
    one = await resolve_chat_entry_by_name(db, user_a, "only-mine")
    assert (one.status, _ids(one)) == ("one", [named["only"][1].id])
    entry = one.matches[0][1]
    assert (entry.slash_name, entry.source, entry.param_key) == ("only-mine", "own", "topic")
    assert entry == await resolve_chat_entry(db, user_a, named["only"][1])
    # 타인 private은 후보에 섞이지 않는다(존재 은닉)
    assert await resolve_chat_entry_by_name(db, user_b, "only-mine") == (
        await resolve_chat_entry_by_name(db, user_b, "nope")
    )
    for name in ("", "nope", "DUP", " dup", "x" * 161):
        none = await resolve_chat_entry_by_name(db, user_a, name)
        assert (none.status, none.matches) == ("none", []), name
    # 미승인 org는 비소유자에게 없고, 소유자에게는 own 하나다
    assert (await resolve_chat_entry_by_name(db, user_a, "org-solo")).status == "none"
    owner = await resolve_chat_entry_by_name(db, user_b, "org-solo")
    assert (owner.status, owner.matches[0][1].source) == ("one", "own")
    await _review(db, named["solo_org"][2])
    await db.commit()
    approved = await resolve_chat_entry_by_name(db, user_a, "org-solo")
    assert (approved.status, approved.matches[0][1].source) == ("one", "org")


async def test_two_wrappers_of_one_skill_count_once(db, named, user_a: User) -> None:
    _, first, v1, _ = named["only"]
    second = Agent(
        name="only-mine-second-wrapper",
        kind="skill",
        source_kind="skill_package",
        definition={},
        params_schema={},
        scope="user",
        owner_id=user_a.id,
        enabled=True,
        skill_version_id=v1.id,
    )
    db.add(second)
    await db.commit()
    match = await resolve_chat_entry_by_name(db, user_a, "only-mine")
    assert match.status == "one"
    assert _ids(match) == [min(first.id, second.id, key=str)]  # 가장 작은 agent id 문자열


async def test_workflow_without_a_handle_is_listed_but_not_named(
    db, named, user_a: User
) -> None:
    listed = {a.id: e for a, e in await list_chat_entry_agents(db, user_a)}
    assert listed[named["no_handle"].id].slash_name is None
    assert listed[named["own_wf"].id].slash_name == "dup"
    assert (await resolve_chat_entry_by_name(db, user_a, "wf-no-handle")).status == "none"
