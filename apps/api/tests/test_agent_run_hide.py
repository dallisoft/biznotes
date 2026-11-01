"""실패한 실행 숨기기 — run 단위 숨김·되돌리기·일괄 숨기기 (#220, 코어 #371)·숨긴 결과 보기 (#221).

권한은 취소와 같다(`_can_cancel`: 실행 소유자 ∨ 노트북 write 이상, admin은 owner 승격).
판정 순서는 404(run 없음·노트북 접근 없음) → 403(취소 규칙 밖) → 409(진행 중·성공 run)다.
숨긴 run은 노트북 목록에서만 빠지고 단건 조회·「실행 기록」 집계·관리자 사용량에는 남는다.
권한 표는 docs/plans/core-v0.25.0-upgrade.md §4.
"""

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from biznotes_core.models import (
    Agent,
    AgentRun,
    AuditLog,
    Notebook,
    User,
)
from sqlalchemy import select

from tests.conftest import _create_user, bearer, login, share_with


async def _agent(db) -> Agent:
    agent = Agent(
        name=f"숨김 {uuid.uuid4().hex[:6]}",
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


async def _notebook(db, owner: User) -> Notebook:
    nb = Notebook(title=f"run-hide-{uuid.uuid4().hex[:6]}", owner_id=owner.id)
    db.add(nb)
    await db.flush()
    return nb


async def _share(db, nb: Notebook, user: User, permission: str) -> None:
    await share_with(db, nb.id, user, permission)
    await db.flush()


async def _run(db, agent: Agent, nb: Notebook, by: User, status: str = "failed") -> AgentRun:
    finished = status in ("succeeded", "failed", "cancelled")
    run = AgentRun(
        agent_id=agent.id,
        notebook_id=nb.id,
        user_id=by.id,
        params={},
        status=status,
        error="실패" if status == "failed" else None,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC) if finished else None,
    )
    db.add(run)
    # 커밋으로 끝낸다 — 테스트 세션에 쓰기 트랜잭션이 열려 있으면 라우터의 행 잠금이 기다린다
    await db.commit()
    return run


async def _token(client: httpx.AsyncClient, user: User) -> dict[str, str]:
    client.cookies.clear()
    return bearer(await login(client, user.email))


async def _post(client: httpx.AsyncClient, user: User, path: str) -> httpx.Response:
    return await client.post(f"/api/v1{path}", headers=await _token(client, user))


async def _listed_ids(client: httpx.AsyncClient, user: User, nb: Notebook) -> set[str]:
    res = await client.get(
        f"/api/v1/notebooks/{nb.id}/agent-runs", headers=await _token(client, user)
    )
    assert res.status_code == 200, res.text
    return {r["id"] for r in res.json()}


async def _hidden_at(db, run: AgentRun) -> datetime | None:
    await db.refresh(run)
    return run.hidden_at


async def _audit(db, run: AgentRun) -> list[tuple[str, uuid.UUID | None]]:
    rows = await db.execute(
        select(AuditLog.action, AuditLog.user_id)
        .where(AuditLog.resource == str(run.id))
        .order_by(AuditLog.created_at)
    )
    return [(action, user_id) for action, user_id in rows.all()]


# ---------- AC-1 항목 메뉴로 숨기면 「최근 결과」에서 사라진다 ----------


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_hidden_run_leaves_list_but_single_get_remains(
    client: httpx.AsyncClient, db, user_a, status
):
    """AC-1 — 실패·취소 run을 숨기면 목록에서 빠지고, 단건 조회는 그대로 돌려준다."""
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    run = await _run(db, agent, nb, user_a, status=status)
    other = await _run(db, agent, nb, user_a, status="succeeded")

    res = await _post(client, user_a, f"/agent-runs/{run.id}/hide")

    assert res.status_code == 200, res.text
    assert res.json()["id"] == str(run.id)
    assert await _listed_ids(client, user_a, nb) == {str(other.id)}
    detail = await client.get(
        f"/api/v1/agent-runs/{run.id}", headers=await _token(client, user_a)
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["status"] == status
    await db.refresh(run)
    assert run.hidden_at is not None
    assert run.hidden_by == user_a.id


async def test_unhide_restores_run_to_list(client: httpx.AsyncClient, db, user_a):
    """「되돌리기」 — 숨김을 해제하면 목록에 다시 보이고 숨김 열이 비워진다."""
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    run = await _run(db, agent, nb, user_a)
    assert (await _post(client, user_a, f"/agent-runs/{run.id}/hide")).status_code == 200

    res = await _post(client, user_a, f"/agent-runs/{run.id}/unhide")

    assert res.status_code == 200, res.text
    assert await _listed_ids(client, user_a, nb) == {str(run.id)}
    await db.refresh(run)
    assert run.hidden_at is None
    assert run.hidden_by is None


# ---------- AC-2 집계는 숨긴 run도 그대로 센다 ----------


# ---------- AC-3 진행 중·대기 run은 409 ----------


@pytest.mark.parametrize("status", ["queued", "running", "awaiting_approval", "succeeded"])
async def test_active_or_succeeded_run_cannot_be_hidden(
    client: httpx.AsyncClient, db, user_a, status
):
    """AC-3 — 진행 중·대기·승인 대기 run(코어 `RunNotFinished`)과 성공 run(범위 밖)은 409."""
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    run = await _run(db, agent, nb, user_a, status=status)

    res = await _post(client, user_a, f"/agent-runs/{run.id}/hide")

    assert res.status_code == 409, res.text
    # 진행 중은 코어 `RunNotFinished`(전역 핸들러 409), 성공은 제품 범위 검사의 문구다
    assert res.json()["detail"] == (
        "실패하거나 취소한 실행만 숨길 수 있습니다"
        if status == "succeeded"
        else "진행 중인 실행은 숨길 수 없습니다"
    )
    assert await _hidden_at(db, run) is None
    assert await _audit(db, run) == []
    assert await _listed_ids(client, user_a, nb) == {str(run.id)}


# ---------- AC-4 비인가: read 독자의 타인 run 403, 접근 없음 404 ----------


@pytest.mark.parametrize("action", ["hide", "unhide"])
async def test_read_reader_cannot_hide_others_run(
    client: httpx.AsyncClient, db, user_a, user_b, action
):
    """AC-4 — read 공유자가 타인의 run을 숨기거나 되돌리면 403, 상태·감사 로그 불변."""
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    await _share(db, nb, user_b, "read")
    run = await _run(db, agent, nb, user_a)
    if action == "unhide":
        assert (await _post(client, user_a, f"/agent-runs/{run.id}/hide")).status_code == 200
    before = await _hidden_at(db, run)

    res = await _post(client, user_b, f"/agent-runs/{run.id}/{action}")

    assert res.status_code == 403, res.text
    assert await _hidden_at(db, run) == before
    assert all(user_id != user_b.id for _action, user_id in await _audit(db, run))


async def test_read_reader_gets_403_before_409_on_active_run(
    client: httpx.AsyncClient, db, user_a, user_b
):
    """판정 순서 404 → 403 → 409 — 진행 중인 타인 run도 read 독자에게는 403이다."""
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    await _share(db, nb, user_b, "read")
    run = await _run(db, agent, nb, user_a, status="running")

    res = await _post(client, user_b, f"/agent-runs/{run.id}/hide")

    assert res.status_code == 403, res.text


@pytest.mark.parametrize("action", ["hide", "unhide"])
async def test_stranger_gets_404(client: httpx.AsyncClient, db, user_a, user_b, action):
    """AC-4 — 노트북 접근 권한이 없으면 존재를 숨긴다(403이 아니라 404). 없는 run도 404."""
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    run = await _run(db, agent, nb, user_a)

    res = await _post(client, user_b, f"/agent-runs/{run.id}/{action}")
    missing = await _post(client, user_a, f"/agent-runs/{uuid.uuid4()}/{action}")

    assert res.status_code == 404, res.text
    assert res.json()["detail"] == "Notebook not found"
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"] == "Run not found"
    assert await _hidden_at(db, run) is None


async def test_read_sharer_can_hide_own_failed_run(
    client: httpx.AsyncClient, db, user_a, user_b
):
    """read 공유자도 자기가 시작한 실패 run은 숨긴다(취소와 같은 규칙)."""
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    await _share(db, nb, user_b, "read")
    run = await _run(db, agent, nb, user_b)

    res = await _post(client, user_b, f"/agent-runs/{run.id}/hide")

    assert res.status_code == 200, res.text
    assert await _hidden_at(db, run) is not None


@pytest.mark.parametrize("who", ["write_sharer", "owner", "admin"])
async def test_writer_owner_admin_can_hide_others_run(
    client: httpx.AsyncClient, db, user_a, user_b, admin_user, who
):
    """write 공유자·노트북 owner·admin(owner 승격)은 타인의 실패 run을 숨긴다."""
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    await _share(db, nb, user_b, "write")
    starter, caller = {
        "write_sharer": (user_a, user_b),
        "owner": (user_b, user_a),
        "admin": (user_a, admin_user),
    }[who]
    run = await _run(db, agent, nb, starter)

    res = await _post(client, caller, f"/agent-runs/{run.id}/hide")

    assert res.status_code == 200, res.text
    await db.refresh(run)
    assert run.hidden_by == caller.id


async def test_bulk_hide_stranger_gets_404(client: httpx.AsyncClient, db, user_a, user_b):
    """AC-4 — 접근 권한이 없는 노트북의 일괄 숨기기는 404이고 아무것도 숨기지 않는다."""
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    run = await _run(db, agent, nb, user_a)

    res = await _post(client, user_b, f"/notebooks/{nb.id}/agent-runs/hide-failed")

    assert res.status_code == 404, res.text
    assert res.json()["detail"] == "Notebook not found"
    assert await _hidden_at(db, run) is None


# ---------- AC-5 「실패한 결과 모두 숨기기」는 숨길 권한이 있는 실패 run만 ----------


async def test_bulk_hide_read_sharer_hides_only_own_failed_runs(
    client: httpx.AsyncClient, db, user_a, user_b
):
    """AC-5 — read 공유자의 일괄 숨기기는 자기 실패 run만 숨기고 타인 run은 남긴다."""
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    await _share(db, nb, user_b, "read")
    own_1 = await _run(db, agent, nb, user_b)
    own_2 = await _run(db, agent, nb, user_b)
    others = await _run(db, agent, nb, user_a)

    res = await _post(client, user_b, f"/notebooks/{nb.id}/agent-runs/hide-failed")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["hidden"] == 2
    assert set(body["run_ids"]) == {str(own_1.id), str(own_2.id)}
    assert await _listed_ids(client, user_b, nb) == {str(others.id)}
    assert await _hidden_at(db, others) is None


async def test_bulk_hide_writer_hides_failed_runs_only(
    client: httpx.AsyncClient, db, user_a, user_b
):
    """AC-5 — write 공유자는 모든 실패 run을 숨긴다. 취소·성공·진행 중 run과 이미 숨긴 run은
    건드리지 않는다(이미 숨긴 run은 세지 않고 첫 숨긴 사람을 지킨다)."""
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    await _share(db, nb, user_b, "write")
    failed_a = await _run(db, agent, nb, user_a)
    failed_b = await _run(db, agent, nb, user_b)
    already = await _run(db, agent, nb, user_a)
    assert (await _post(client, user_a, f"/agent-runs/{already.id}/hide")).status_code == 200
    kept = [
        await _run(db, agent, nb, user_a, status=s)
        for s in ("cancelled", "succeeded", "running")
    ]

    res = await _post(client, user_b, f"/notebooks/{nb.id}/agent-runs/hide-failed")

    assert res.status_code == 200, res.text
    assert res.json()["hidden"] == 2
    assert set(res.json()["run_ids"]) == {str(failed_a.id), str(failed_b.id)}
    assert await _listed_ids(client, user_b, nb) == {str(r.id) for r in kept}
    await db.refresh(already)
    assert already.hidden_by == user_a.id


# ---------- AC-6 숨김이 감사 로그에 남는다 ----------


async def test_hide_and_unhide_write_audit_logs_once(
    client: httpx.AsyncClient, db, user_a, user_b
):
    """AC-6 — 숨김·해제가 행위자와 함께 감사 로그에 남는다. 같은 상태로 다시 부르면 200이지만
    기록하지 않고, 처음 숨긴 사람을 지킨다(코어 `set_run_hidden` 멱등)."""
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    await _share(db, nb, user_b, "write")
    run = await _run(db, agent, nb, user_a)
    bulk_run = await _run(db, agent, nb, user_a)

    assert (await _post(client, user_a, f"/agent-runs/{run.id}/hide")).status_code == 200
    again = await _post(client, user_b, f"/agent-runs/{run.id}/hide")
    assert again.status_code == 200, again.text
    await db.refresh(run)
    assert run.hidden_by == user_a.id
    assert (await _post(client, user_b, f"/agent-runs/{run.id}/unhide")).status_code == 200
    assert (await _post(client, user_b, f"/agent-runs/{run.id}/unhide")).status_code == 200

    assert await _audit(db, run) == [
        ("agent_run.hide", user_a.id),
        ("agent_run.unhide", user_b.id),
    ]
    # 일괄 숨기기도 run마다 같은 감사 로그를 남긴다
    assert (
        await _post(client, user_b, f"/notebooks/{nb.id}/agent-runs/hide-failed")
    ).status_code == 200
    assert await _audit(db, bulk_run) == [("agent_run.hide", user_b.id)]


# ---------- `can_hide` — 실패·취소 ∧ 취소 규칙 ----------


async def test_can_hide_in_list_and_detail(
    client: httpx.AsyncClient, db, user_a, user_b, admin_user
):
    """`can_hide`는 호출자 기준: 실패·취소 run ∧ 취소 규칙. 성공·진행 중 run은 모두 False."""
    writer = await _create_user(db, "hide-writer")
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    await _share(db, nb, user_b, "read")
    await _share(db, nb, writer, "write")
    failed_owner = await _run(db, agent, nb, user_a)
    cancelled_reader = await _run(db, agent, nb, user_b, status="cancelled")
    succeeded_reader = await _run(db, agent, nb, user_b, status="succeeded")
    running_reader = await _run(db, agent, nb, user_b, status="running")
    ids = [failed_owner, cancelled_reader, succeeded_reader, running_reader]
    expected = {
        user_b.email: [False, True, False, False],
        writer.email: [True, True, False, False],
        user_a.email: [True, True, False, False],
        admin_user.email: [True, True, False, False],
    }

    for email, flags in expected.items():
        client.cookies.clear()
        headers = bearer(await login(client, email))
        want = {str(r.id): flag for r, flag in zip(ids, flags, strict=True)}
        listed = await client.get(f"/api/v1/notebooks/{nb.id}/agent-runs", headers=headers)
        assert listed.status_code == 200, listed.text
        assert {r["id"]: r["can_hide"] for r in listed.json()} == want, email
        for run_id, can in want.items():
            detail = await client.get(f"/api/v1/agent-runs/{run_id}", headers=headers)
            assert detail.json()["can_hide"] is can, (email, run_id)


async def test_cancelled_run_is_hideable_right_away(client: httpx.AsyncClient, db, user_a):
    """취소 응답은 `can_hide=True`다 — 방금 취소한 run을 같은 화면에서 바로 숨길 수 있다."""
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    run = await _run(db, agent, nb, user_a, status="queued")

    cancelled = await _post(client, user_a, f"/agent-runs/{run.id}/cancel")

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["can_hide"] is True
    hidden = await _post(client, user_a, f"/agent-runs/{run.id}/hide")
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["can_hide"] is True


# ---------- #221 숨긴 결과 보기 — `?hidden=true` ----------


async def _hidden_listed(
    client: httpx.AsyncClient, user: User, nb: Notebook
) -> httpx.Response:
    return await client.get(
        f"/api/v1/notebooks/{nb.id}/agent-runs",
        params={"hidden": "true"},
        headers=await _token(client, user),
    )


async def test_hidden_query_lists_only_hidden_runs(client: httpx.AsyncClient, db, user_a):
    """`?hidden=true`는 숨긴 run만 돌려주고, 기본 목록은 지금처럼 숨긴 run을 뺀다.

    응답의 `hidden`이 둘을 가른다.
    """
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    hidden = await _run(db, agent, nb, user_a)
    shown = await _run(db, agent, nb, user_a)
    succeeded = await _run(db, agent, nb, user_a, status="succeeded")
    assert (await _post(client, user_a, f"/agent-runs/{hidden.id}/hide")).status_code == 200

    res = await _hidden_listed(client, user_a, nb)
    default = await client.get(
        f"/api/v1/notebooks/{nb.id}/agent-runs", headers=await _token(client, user_a)
    )

    assert res.status_code == 200, res.text
    assert [(r["id"], r["hidden"], r["can_hide"]) for r in res.json()] == [
        (str(hidden.id), True, True)
    ]
    assert res.json()[0]["agent_name"] == agent.name
    assert default.status_code == 200, default.text
    assert {r["id"]: r["hidden"] for r in default.json()} == {
        str(shown.id): False,
        str(succeeded.id): False,
    }

    # 되돌리면 숨긴 목록에서 빠지고 기본 목록으로 돌아온다
    assert (await _post(client, user_a, f"/agent-runs/{hidden.id}/unhide")).status_code == 200
    assert (await _hidden_listed(client, user_a, nb)).json() == []
    assert str(hidden.id) in await _listed_ids(client, user_a, nb)


async def test_hidden_list_can_hide_follows_unhide_rule(
    client: httpx.AsyncClient, db, user_a, user_b, admin_user
):
    """숨긴 목록은 read 이상이면 보인다. 「다시 보이기」(`can_hide`)는 되돌리기 규칙을 따른다.

    되돌리기 규칙은 취소 규칙과 같다. read 공유자는 남이 숨긴 run을 볼 수만 있고
    (되돌리기 403 — `test_read_reader_cannot_hide_others_run`), write 공유자·owner·admin
    (owner 승격)은 모두 되돌릴 수 있다.
    """
    writer = await _create_user(db, "hidden-list-writer")
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    await _share(db, nb, user_b, "read")
    await _share(db, nb, writer, "write")
    owners = await _run(db, agent, nb, user_a)
    readers = await _run(db, agent, nb, user_b, status="cancelled")
    for run in (owners, readers):
        assert (await _post(client, user_a, f"/agent-runs/{run.id}/hide")).status_code == 200
    expected = {
        user_b.email: {str(owners.id): False, str(readers.id): True},
        writer.email: {str(owners.id): True, str(readers.id): True},
        user_a.email: {str(owners.id): True, str(readers.id): True},
        admin_user.email: {str(owners.id): True, str(readers.id): True},
    }

    for email, want in expected.items():
        client.cookies.clear()
        res = await client.get(
            f"/api/v1/notebooks/{nb.id}/agent-runs",
            params={"hidden": "true"},
            headers=bearer(await login(client, email)),
        )
        assert res.status_code == 200, (email, res.text)
        assert {r["id"]: r["can_hide"] for r in res.json()} == want, email
        assert all(r["hidden"] for r in res.json()), email


async def test_hidden_list_stranger_gets_404(client: httpx.AsyncClient, db, user_a, user_b):
    """비인가 — 노트북 접근 권한이 없으면 숨긴 목록도 404다(존재 은닉, 기본 목록과 같다)."""
    agent = await _agent(db)
    nb = await _notebook(db, user_a)
    run = await _run(db, agent, nb, user_a)
    assert (await _post(client, user_a, f"/agent-runs/{run.id}/hide")).status_code == 200

    res = await _hidden_listed(client, user_b, nb)

    assert res.status_code == 404, res.text
    assert res.json()["detail"] == "Notebook not found"
