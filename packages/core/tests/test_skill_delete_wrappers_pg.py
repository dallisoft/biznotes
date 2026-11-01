# 신규(#116, docs/plans/skill-delete-wrappers/). 필요: PostgreSQL·Valkey(PG 테스트).
"""스킬 삭제 때 wrapper Agent 처분 — 참조 없으면 삭제, 있으면 비활성 보존.

실제 FK CASCADE·SET NULL과 행 잠금이 필요해 PostgreSQL에서만 검증한다. 동시성 테스트는 삭제
세션이 실제로 잠금 대기에 들어간 것을 `pg_stat_activity`로 확인한 뒤 상대 세션을 진행한다(시간
대기에 기대지 않는다). `create_all` 기반이라 Alembic 마이그레이션 검증이 아니다.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text

from biznotes_core.core.db import get_session_factory
from biznotes_core.models import (
    Agent,
    AgentRun,
    AgentRunApproval,
    AuditLog,
    Notebook,
    Skill,
    SkillVersion,
    User,
)
from biznotes_core.services.skills.service import WrapperRemoval, remove_skill_wrappers


async def _version(db: Any, skill: Skill, number: int) -> SkillVersion:
    version = SkillVersion(
        skill_id=skill.id,
        version=number,
        package_uri=f"versions/{uuid.uuid4()}/",
        skill_md_hash="h",
        package_hash=f"p{number}",
        manifest={},
        file_tree=[],
        security_report={},
        status="draft_frozen",
    )
    db.add(version)
    await db.flush()
    return version


def _wrapper(owner: User, version: SkillVersion, name: str = "wrapper") -> Agent:
    return Agent(
        name=name,
        kind="skill",
        source_kind="skill_package",
        definition={},
        params_schema={},
        scope="user",
        owner_id=owner.id,
        enabled=True,
        skill_version_id=version.id,
    )


async def _skill(db: Any, owner: User) -> tuple[Skill, SkillVersion, Agent]:
    """스킬·version 1·wrapper(그 version)를 커밋한다 — `_upsert_wrapper`가 만드는 모양이다."""
    slug = f"s-{uuid.uuid4().hex[:8]}"
    skill = Skill(owner_id=owner.id, name=slug, description="", slug=slug, visibility="private")
    db.add(skill)
    await db.flush()
    version = await _version(db, skill, 1)
    skill.current_version_id = version.id
    wrapper = _wrapper(owner, version)
    db.add(wrapper)
    await db.commit()
    return skill, version, wrapper


@pytest.fixture
async def notebook(db, user_a: User) -> Notebook:
    nb = Notebook(owner_id=user_a.id, title="t")
    db.add(nb)
    await db.commit()
    return nb


def _run(agent: Agent, nb: Notebook, user: User) -> AgentRun:
    return AgentRun(agent_id=agent.id, notebook_id=nb.id, user_id=user.id, status="succeeded")


async def _refer_with_run(db: Any, agent: Agent, nb: Notebook, user: User) -> None:
    """wrapper를 참조하는 run 하나와 그 승인 행을 만든다."""
    run = _run(agent, nb, user)
    db.add(run)
    await db.flush()
    now = datetime.now(UTC)
    db.add(
        AgentRunApproval(
            run_id=run.id,
            tool_name="t",
            arguments_summary={},
            requested_at=now,
            expires_at=now + timedelta(hours=1),
        )
    )


async def _count(model: type, *where: Any) -> int:
    async with get_session_factory()() as s:
        return int(await s.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def _agent(agent_id: uuid.UUID) -> Agent | None:
    async with get_session_factory()() as s:
        return await s.get(Agent, agent_id)


async def _delete_via_route(db: Any, skill: Skill, user: User, monkeypatch) -> None:
    from biznotes_core.api.v1 import skills as route
    from biznotes_core.services.skills import package as pkg

    monkeypatch.setattr(pkg, "delete_skill_objects", lambda *_: True)  # 스토리지 대역
    await route.delete_skill(skill.id, user=user, db=db)


async def _audit(skill: Skill) -> dict[str, Any]:
    async with get_session_factory()() as s:
        rows = (
            await s.execute(select(AuditLog.detail).where(AuditLog.action == "skill.delete"))
        ).scalars()
        return next(d for d in rows if d["skill_id"] == str(skill.id))


# ---------- 처분 정책 ----------


async def test_unreferenced_wrapper_is_deleted_with_the_skill(
    db, user_a: User, notebook: Notebook, monkeypatch
) -> None:
    """수락 기준 1·4·5 — 라우트가 스킬보다 먼저 wrapper를 지우고 audit에 id를 남긴다."""
    skill, _, wrapper = await _skill(db, user_a)
    _, _, other_wrapper = await _skill(db, user_a)
    legacy = Agent(name="legacy", kind="workflow", definition={}, params_schema={})
    db.add(legacy)
    await db.commit()

    await _delete_via_route(db, skill, user_a, monkeypatch)

    assert await _count(Skill, Skill.id == skill.id) == 0
    assert await _agent(wrapper.id) is None
    assert await _agent(other_wrapper.id) is not None
    assert await _agent(legacy.id) is not None
    assert await _audit(skill) == {
        "skill_id": str(skill.id),
        "wrappers_deleted": [str(wrapper.id)],
        "wrappers_disabled": [],
    }


async def test_referenced_wrapper_is_disabled_and_keeps_its_history(
    db, user_a: User, notebook: Notebook, monkeypatch
) -> None:
    """수락 기준 2·5 — run 참조가 있으면 지우지 않는다(CASCADE로 이력이 사라지지 않게)."""
    skill, _, wrapper = await _skill(db, user_a)
    await _refer_with_run(db, wrapper, notebook, user_a)
    await db.commit()

    await _delete_via_route(db, skill, user_a, monkeypatch)

    kept = await _agent(wrapper.id)
    assert kept is not None
    assert (kept.enabled, kept.skill_version_id) == (False, None)
    assert await _count(AgentRun, AgentRun.agent_id == wrapper.id) == 1
    assert await _count(AgentRunApproval) == 1
    assert (await _audit(skill))["wrappers_disabled"] == [str(wrapper.id)]


async def test_every_wrapper_of_the_skill_is_handled(
    db, user_a: User, notebook: Notebook
) -> None:
    """수락 기준 3 — `find_wrapper`(LIMIT 1)가 아니라 조인으로 모두 찾는다. 반환은 id 순이다."""
    skill, v1, latest_wrapper = await _skill(db, user_a)
    v2 = await _version(db, skill, 2)
    latest_wrapper.skill_version_id = v2.id
    hand_made = [_wrapper(user_a, v1, f"hand-{n}") for n in range(3)]
    db.add_all(hand_made)
    await db.flush()
    db.add(_run(hand_made[1], notebook, user_a))
    await db.commit()

    removal = await remove_skill_wrappers(db, skill.id)
    await db.delete(skill)
    await db.commit()

    referenced = hand_made[1].id
    others = sorted([latest_wrapper.id, hand_made[0].id, hand_made[2].id])
    assert removal == WrapperRemoval(deleted=tuple(others), disabled=(referenced,))
    assert [await _agent(i) for i in others] == [None, None, None]
    assert await _count(Agent, Agent.source_kind == "skill_package", Agent.enabled.is_(True)) == 0


async def test_skill_without_wrappers_is_a_no_op(db, user_a: User) -> None:
    skill, _, wrapper = await _skill(db, user_a)
    await db.delete(wrapper)
    await db.commit()
    assert await remove_skill_wrappers(db, skill.id) == WrapperRemoval(deleted=(), disabled=())


async def test_rollback_restores_the_wrappers(db, user_a: User) -> None:
    """수락 기준 6 — 처분은 호출측 트랜잭션에 합류한다(커밋하지 않는다)."""
    skill, _, wrapper = await _skill(db, user_a)
    skill_id, wrapper_id = skill.id, wrapper.id  # 롤백 뒤에는 객체가 만료된다
    removal = await remove_skill_wrappers(db, skill_id)
    assert removal.deleted == (wrapper_id,)
    await db.rollback()

    kept = await _agent(wrapper_id)
    assert kept is not None and kept.enabled
    assert await _count(Skill, Skill.id == skill_id) == 1


# ---------- 동시성 (리뷰 M-1·M-2) ----------


async def _pid(session: Any) -> int:
    return int(await session.scalar(text("SELECT pg_backend_pid()")))


async def _wait_until_lock_blocked(pid: int) -> None:
    """`pid` 세션이 행 잠금을 기다릴 때까지 확인한다.

    통계 스냅샷은 트랜잭션마다 고정되므로 읽을 때마다 트랜잭션을 끝낸다.
    """
    async with get_session_factory()() as probe:
        for _ in range(500):
            wait = await probe.scalar(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}
            )
            await probe.rollback()
            if wait == "Lock":
                return
            await asyncio.sleep(0.01)
    raise AssertionError(f"session {pid} never waited on a lock")


async def _delete_skill(session: Any, skill: Skill) -> WrapperRemoval:
    removal = await remove_skill_wrappers(session, skill.id)
    await session.delete(skill)
    await session.commit()
    return removal


async def _stage(
    session: Any, wrapper: Agent, version: SkillVersion, nb: Notebook, user: User
) -> None:
    """`stage_agent_run`의 스킬 꼬리 그대로 — `_pin_skill_version` 뒤 한 번의 flush.

    그 flush는 `INSERT agent_runs` → `UPDATE skills`(run_count) 순서다(재리뷰 M-3 실측).
    """
    from biznotes_core.services.agent_runs import _pin_skill_version

    run = _run(wrapper, nb, user)
    await _pin_skill_version(session, run, version.id)
    session.add(run)
    await session.flush()


async def test_run_staged_first_keeps_the_wrapper(db, user_a: User, notebook: Notebook) -> None:
    """수락 기준 7(a) — staging이 먼저면 삭제는 그 커밋을 기다렸다가 새 run을 보고 보존한다."""
    skill, version, wrapper = await _skill(db, user_a)
    factory = get_session_factory()
    async with factory() as staging, factory() as deleting:
        await _stage(staging, wrapper, version, notebook, user_a)
        pid = await _pid(deleting)
        target = await deleting.get(Skill, skill.id)
        task = asyncio.create_task(_delete_skill(deleting, target))
        await _wait_until_lock_blocked(pid)

        await staging.commit()
        removal = await asyncio.wait_for(task, timeout=10)

    assert removal == WrapperRemoval(deleted=(), disabled=(wrapper.id,))
    kept = await _agent(wrapper.id)
    assert kept is not None and not kept.enabled
    assert await _count(AgentRun, AgentRun.agent_id == wrapper.id) == 1


async def test_staging_behind_a_delete_does_not_deadlock(
    db, user_a: User, notebook: Notebook
) -> None:
    """수락 기준 7(b) — 삭제가 스킬을 잠근 뒤 staging이 오면 staging이 INSERT 전에 기다린다.

    staging이 스킬을 잠그지 않으면 `INSERT agent_runs`(wrapper `KEY SHARE`)를 먼저 하고
    `UPDATE skills`에서 기다린다. 삭제의 wrapper `FOR UPDATE`가 그 `KEY SHARE`를 기다려 교착한다.
    """
    from biznotes_core.services.agent_runs import SkillNotRunnable

    skill, version, wrapper = await _skill(db, user_a)
    factory = get_session_factory()
    async with factory() as staging, factory() as deleting:
        await deleting.execute(  # `remove_skill_wrappers`의 첫 문장과 같은 잠금
            select(Skill.id).where(Skill.id == skill.id).with_for_update()
        )
        pid = await _pid(staging)
        task = asyncio.create_task(_stage(staging, wrapper, version, notebook, user_a))
        await _wait_until_lock_blocked(pid)

        target = await deleting.get(Skill, skill.id)
        removal = await asyncio.wait_for(_delete_skill(deleting, target), timeout=10)
        with pytest.raises(SkillNotRunnable):
            await asyncio.wait_for(task, timeout=10)

    assert removal == WrapperRemoval(deleted=(wrapper.id,), disabled=())
    assert await _agent(wrapper.id) is None
    assert await _count(AgentRun) == 0


async def test_second_concurrent_delete_is_404(db, user_a: User, monkeypatch) -> None:
    """같은 스킬 삭제가 겹치면 늦은 요청은 잠금 뒤 스킬이 없음을 보고 404다(audit 중복 없음)."""
    from fastapi import HTTPException

    skill, _, _ = await _skill(db, user_a)
    factory = get_session_factory()
    async with factory() as first, factory() as second:
        await remove_skill_wrappers(first, skill.id)
        pid = await _pid(second)
        task = asyncio.create_task(_delete_via_route(second, skill, user_a, monkeypatch))
        await _wait_until_lock_blocked(pid)

        await first.delete(await first.get(Skill, skill.id))
        await first.commit()
        with pytest.raises(HTTPException) as caught:
            await asyncio.wait_for(task, timeout=10)

    assert caught.value.status_code == 404
    assert await _count(AuditLog, AuditLog.action == "skill.delete") == 0


async def test_concurrent_freeze_leaves_no_orphan(db, user_a: User) -> None:
    """수락 기준 8 — freeze가 wrapper를 새 version으로 옮기는 중이면 그 커밋을 기다려 함께 처분한다.

    wrapper만 잠그면 조인 `FOR UPDATE`의 재평가가 옮겨 간 행을 놓쳐 활성 고아가 남는다.
    """
    skill, _, wrapper = await _skill(db, user_a)
    factory = get_session_factory()
    async with factory() as freezing, factory() as deleting:
        frozen_skill = await freezing.get(Skill, skill.id)
        frozen_wrapper = await freezing.get(Agent, wrapper.id)
        assert frozen_skill is not None and frozen_wrapper is not None
        v2 = await _version(freezing, frozen_skill, 2)
        frozen_skill.current_version_id = v2.id
        frozen_wrapper.skill_version_id = v2.id
        await freezing.flush()

        pid = await _pid(deleting)
        target = await deleting.get(Skill, skill.id)
        task = asyncio.create_task(_delete_skill(deleting, target))
        await _wait_until_lock_blocked(pid)

        await freezing.commit()
        removal = await asyncio.wait_for(task, timeout=10)

    assert removal == WrapperRemoval(deleted=(wrapper.id,), disabled=())
    assert await _agent(wrapper.id) is None
    assert await _count(
        Agent, Agent.source_kind == "skill_package", Agent.skill_version_id.is_(None)
    ) == 0
