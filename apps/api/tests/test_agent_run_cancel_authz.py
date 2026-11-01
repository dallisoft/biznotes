"""에이전트 실행 취소 인가 — 실행 소유자 ∨ 노트북 write 이상(admin 승격) (#173).

권한 표는 docs/plans/run-cancel-authz.md. read 독자(공유·공개 열람)는 타인 run을 403으로
거부당하고, 상태·Celery revoke가 바뀌지 않는다. `can_cancel`은 같은 판정을 목록·상세에 싣는다.
"""

import uuid

import httpx
import pytest
from biznotes_core.models import Agent, AgentRun, Notebook, User

from tests.conftest import _create_user, bearer, login, share_with


@pytest.fixture
def revoked(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Celery revoke 스파이 — 실제 브로커로 보내지 않는다."""
    from biznotes_core.workers.celery_app import celery_app

    calls: list[str] = []
    monkeypatch.setattr(celery_app.control, "revoke", lambda task_id, **_: calls.append(task_id))
    return calls


async def _agent(db) -> Agent:
    agent = Agent(
        name=f"취소 인가 {uuid.uuid4().hex[:6]}",
        kind="skill",
        icon="🧪",
        definition={"prompt_template": "p", "use_rag": False},
        params_schema={},
        scope="system",
        enabled=True,
    )
    db.add(agent)
    await db.flush()
    return agent


async def _notebook(db, owner: User, *, visibility: str = "private") -> Notebook:
    nb = Notebook(
        title=f"cancel-authz-{uuid.uuid4().hex[:6]}", owner_id=owner.id, visibility=visibility
    )
    db.add(nb)
    await db.flush()
    return nb


async def _share(db, nb: Notebook, user: User, permission: str) -> None:
    await share_with(db, nb.id, user, permission)
    await db.flush()


async def _run(db, agent: Agent, nb: Notebook, by: User, status: str = "running") -> AgentRun:
    run = AgentRun(
        agent_id=agent.id,
        notebook_id=nb.id,
        user_id=by.id,
        params={},
        status=status,
        celery_task_id=f"task-{uuid.uuid4().hex[:8]}",
    )
    db.add(run)
    await db.commit()
    return run


async def _cancel(client: httpx.AsyncClient, user: User, run: AgentRun) -> httpx.Response:
    client.cookies.clear()
    token = await login(client, user.email)
    return await client.post(f"/api/v1/agent-runs/{run.id}/cancel", headers=bearer(token))


async def _status(db, run: AgentRun) -> str:
    await db.refresh(run)
    return run.status


@pytest.mark.parametrize("visibility", ["private", "public"])
async def test_read_reader_cannot_cancel_others_run(
    client: httpx.AsyncClient, db, user_a, user_b, revoked, visibility
):
    """AC-1·AC-2 — read 공유자·공개 열람자의 타인 run 취소는 403, 상태·revoke 불변."""
    agent = await _agent(db)
    nb = await _notebook(db, user_a, visibility=visibility)
    if visibility == "private":
        await _share(db, nb, user_b, "read")
    run = await _run(db, agent, nb, user_a)

    res = await _cancel(client, user_b, run)

    assert res.status_code == 403, res.text
    assert await _status(db, run) == "running"
    assert revoked == []


async def test_read_reader_gets_403_before_409_on_finished_run(
    client: httpx.AsyncClient, db, user_a, user_b, revoked
):
    """판정 순서 404 → 403 → 409 — 종료된 타인 run도 read 독자에게는 403이다."""
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    await _share(db, nb, user_b, "read")
    run = await _run(db, agent, nb, user_a, status="succeeded")

    res = await _cancel(client, user_b, run)

    assert res.status_code == 403, res.text
    assert await _status(db, run) == "succeeded"
    assert revoked == []


async def test_read_sharer_can_cancel_own_run(
    client: httpx.AsyncClient, db, user_a, user_b, revoked
):
    """AC-3 — read 공유자도 자기가 시작한 run은 취소한다."""
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    await _share(db, nb, user_b, "read")
    run = await _run(db, agent, nb, user_b)

    res = await _cancel(client, user_b, run)

    assert res.status_code == 200, res.text
    assert res.json()["status"] == "cancelled"
    assert revoked == [run.celery_task_id]


@pytest.mark.parametrize("who", ["write_sharer", "owner", "admin"])
async def test_writer_owner_admin_can_cancel_others_run(
    client: httpx.AsyncClient, db, user_a, user_b, admin_user, revoked, who
):
    """AC-4 — write 공유자·노트북 owner·admin(owner 승격)은 타인 run을 취소한다."""
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    await _share(db, nb, user_b, "write")
    starter, caller = {
        "write_sharer": (user_a, user_b),
        "owner": (user_b, user_a),
        "admin": (user_a, admin_user),
    }[who]
    run = await _run(db, agent, nb, starter)

    res = await _cancel(client, caller, run)

    assert res.status_code == 200, res.text
    assert await _status(db, run) == "cancelled"
    assert revoked == [run.celery_task_id]


async def test_stranger_still_gets_404(client: httpx.AsyncClient, db, user_a, user_b, revoked):
    """AC-5 — 노트북 권한이 없으면 존재를 숨긴다(403이 아니라 404)."""
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    run = await _run(db, agent, nb, user_a)

    res = await _cancel(client, user_b, run)

    assert res.status_code == 404, res.text
    assert await _status(db, run) == "running"
    assert revoked == []


async def test_can_cancel_in_list_and_detail(
    client: httpx.AsyncClient, db, user_a, user_b, admin_user
):
    """AC-6 — `can_cancel`은 호출자 기준: read 독자는 자기 run만, write·owner·admin은 모두."""
    writer = await _create_user(db, "cancel-writer")
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    await _share(db, nb, user_b, "read")
    await _share(db, nb, writer, "write")
    by_owner = await _run(db, agent, nb, user_a)
    by_reader = await _run(db, agent, nb, user_b)
    expected = {
        user_b.email: {str(by_owner.id): False, str(by_reader.id): True},
        writer.email: {str(by_owner.id): True, str(by_reader.id): True},
        user_a.email: {str(by_owner.id): True, str(by_reader.id): True},
        admin_user.email: {str(by_owner.id): True, str(by_reader.id): True},
    }

    for email, want in expected.items():
        client.cookies.clear()
        token = await login(client, email)
        listed = await client.get(f"/api/v1/notebooks/{nb.id}/agent-runs", headers=bearer(token))
        assert listed.status_code == 200, listed.text
        assert {r["id"]: r["can_cancel"] for r in listed.json()} == want, email
        for run_id, can in want.items():
            detail = await client.get(f"/api/v1/agent-runs/{run_id}", headers=bearer(token))
            assert detail.json()["can_cancel"] is can, (email, run_id)
