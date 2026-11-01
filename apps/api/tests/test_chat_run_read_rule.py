"""채팅 run 열람 규칙(#66 PR-1, AC-4) — 제품 run 경로 11곳이 코어 `assert_can_read_run`을 따른다.

채팅 run(`trigger="chat"`)은 실행자와 admin만 본다. 노트북 owner·writer라도 타인 채팅 run은
목록에 없고 단건·로그·산출물·승인 목록·취소·숨기기·되돌리기·승인/거절은 404다. 노트 `origin_ref`는
없는 run과 같은 422 고정 문구다(provenance D7 존재 은닉 — owner 확인 2026-10-28). 목록·일괄 숨기기는
본인 채팅 run도 뺀다(D2). 채팅이 아닌 run의 규칙은 그대로다(기존 테스트가 단언을 바꾸지 않고 통과).
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from biznotes_core.models import Agent, AgentRun, AgentRunApproval, Notebook, User

from tests.chat_entry_fakes import (
    boundaries,  # noqa: F401 — 픽스처
    chat_run,
    chat_workflow,
    notebook,
    token,
)
from tests.conftest import _create_user, share_with

pytestmark = pytest.mark.usefixtures("boundaries")

ARTIFACT = {"name": "result.md", "key": "runs/result.md", "content_type": "text/markdown"}
NOT_FOUND = {"detail": "Run not found"}
ORIGIN_INVALID = {"detail": "origin_ref가 유효하지 않습니다"}


async def _approval(db, run: AgentRun, *, expires_in_minutes: int = 10) -> AgentRunApproval:
    now = datetime.now(UTC)
    approval = AgentRunApproval(
        run_id=run.id,
        tool_name="http_request",
        arguments_summary={"url": "https://example.com"},
        status="pending",
        requested_at=now,
        expires_at=now + timedelta(minutes=expires_in_minutes),
        checkpoint={"tool_name": "http_request"},
    )
    db.add(approval)
    await db.commit()
    await db.refresh(approval)
    return approval


async def _setup(db, owner: User, runner: User, *, runner_permission: str = "write"):
    """owner의 노트북에 runner가 공유로 들어와 만든 채팅 run(승인 대기·산출물 있음)."""
    nb = await notebook(db, owner)
    await share_with(db, nb.id, runner, runner_permission)
    await db.commit()
    wf = await chat_workflow(db, runner)
    run = await chat_run(
        db, wf, nb, runner, status="awaiting_approval", artifact_keys=[ARTIFACT]
    )
    approval = await _approval(db, run)
    return nb, wf, run, approval


def _paths(run: AgentRun, approval: AgentRunApproval) -> list[tuple[str, str]]:
    base = f"/api/v1/agent-runs/{run.id}"
    return [
        ("GET", base),
        ("GET", f"{base}/logs"),
        ("GET", f"{base}/artifacts/{ARTIFACT['name']}"),
        ("GET", f"{base}/approvals"),
        ("POST", f"{base}/cancel"),
        ("POST", f"{base}/hide"),
        ("POST", f"{base}/unhide"),
        ("POST", f"{base}/approvals/{approval.id}/approve"),
        ("POST", f"{base}/approvals/{approval.id}/reject"),
    ]


async def _call(client: httpx.AsyncClient, user: User, method: str, path: str) -> httpx.Response:
    return await client.request(method, path, headers=await token(client, user))


async def test_notebook_owner_and_writer_cannot_touch_others_chat_run(
    client: httpx.AsyncClient, db, user_a, user_b
):
    """owner(user_a)·writer(user_c)는 타인(user_b) 채팅 run의 9개 단건 경로가 모두 404다. 상태는
    그대로다.
    """
    nb, _, run, approval = await _setup(db, user_a, user_b)
    writer = await _create_user(db, "user-c")
    await share_with(db, nb.id, writer, "write")
    await db.commit()

    for user in (user_a, writer):
        for method, path in _paths(run, approval):
            res = await _call(client, user, method, path)
            assert res.status_code == 404, (user.name, method, path, res.text)
            assert res.json() == NOT_FOUND

    await db.refresh(run)
    await db.refresh(approval)
    assert run.status == "awaiting_approval"
    assert run.hidden_at is None
    assert approval.status == "pending"


async def test_chat_run_is_indistinguishable_from_missing_run_without_notebook_access(
    client: httpx.AsyncClient, db, user_a, user_b
):
    """노트북에 접근할 수 없는 사용자에게 타인 채팅 run과 없는 run의 응답이 같다(리뷰 Medium —
    노트북 검사보다 채팅 run 검사가 먼저다).
    """
    import uuid

    _, _, run, _ = await _setup(db, user_a, user_b)
    outsider = await _create_user(db, "user-c")

    for suffix in ("", "/logs", "/approvals"):
        existing = await _call(client, outsider, "GET", f"/api/v1/agent-runs/{run.id}{suffix}")
        missing = await _call(client, outsider, "GET", f"/api/v1/agent-runs/{uuid.uuid4()}{suffix}")
        assert existing.status_code == missing.status_code == 404, suffix
        assert existing.json() == missing.json() == NOT_FOUND, suffix


async def test_runner_and_admin_can_read_chat_run(
    client: httpx.AsyncClient, db, user_a, user_b, admin_user
):
    """실행자 본인과 admin은 단건·로그·승인 목록을 본다."""
    _, _, run, approval = await _setup(db, user_a, user_b)
    base = f"/api/v1/agent-runs/{run.id}"

    for user in (user_b, admin_user):
        for path in (base, f"{base}/logs", f"{base}/approvals"):
            res = await _call(client, user, "GET", path)
            assert res.status_code == 200, (user.name, path, res.text)
    listed = await _call(client, user_b, "GET", f"{base}/approvals")
    assert [a["id"] for a in listed.json()] == [str(approval.id)]


async def test_runner_reads_own_chat_run_artifact(
    client: httpx.AsyncClient, db, user_a, user_b, mem_storage
):
    from app.core.config import get_settings

    _, _, run, _ = await _setup(db, user_a, user_b)
    mem_storage[(get_settings().storage_bucket_artifacts, ARTIFACT["key"])] = b"# result"

    res = await _call(client, user_b, "GET", f"/api/v1/agent-runs/{run.id}/artifacts/result.md")

    assert res.status_code == 200, res.text
    assert res.content == b"# result"


@pytest.mark.parametrize("outcome", ["approve", "reject"])
async def test_admin_can_resolve_chat_run_approval(
    client: httpx.AsyncClient, db, user_a, user_b, admin_user, outcome
):
    """승인·거절은 타인이면 404(위), admin이면 허용(기존 `_assert_approval_permission`)."""
    _, _, run, approval = await _setup(db, user_a, user_b)

    res = await _call(
        client, admin_user, "POST", f"/api/v1/agent-runs/{run.id}/approvals/{approval.id}/{outcome}"
    )

    assert res.status_code == 200, res.text
    await db.refresh(approval)
    assert approval.status == {"approve": "approved", "reject": "rejected"}[outcome]


async def test_runner_can_cancel_own_chat_run(client: httpx.AsyncClient, db, user_a, user_b):
    """열람이 통과한 뒤 기존 취소 규칙(`_can_cancel`)이 판정한다 — read 공유 실행자도 자기 run은
    취소한다.
    """
    nb = await notebook(db, user_a)
    await share_with(db, nb.id, user_b, "read")
    await db.commit()
    run = await chat_run(db, await chat_workflow(db, user_b), nb, user_b, status="running")

    res = await _call(client, user_b, "POST", f"/api/v1/agent-runs/{run.id}/cancel")

    assert res.status_code == 200, res.text
    assert res.json()["status"] == "cancelled"


async def test_lists_exclude_chat_runs_even_for_their_runner(
    client: httpx.AsyncClient, db, user_a, user_b, admin_user
):
    """D2 — 채팅 run은 「최근 결과」에서 모두 빠진다(실행자·owner·admin 누구에게나). 수동 run은
    그대로다.
    """
    nb, wf, run, _ = await _setup(db, user_a, user_b)
    manual = AgentRun(
        agent_id=wf.id,
        notebook_id=nb.id,
        user_id=user_b.id,
        params={},
        status="succeeded",
        trigger="manual",
    )
    db.add(manual)
    await db.commit()

    for user in (user_b, user_a, admin_user):
        res = await _call(client, user, "GET", f"/api/v1/notebooks/{nb.id}/agent-runs")
        assert res.status_code == 200, res.text
        assert [r["id"] for r in res.json()] == [str(manual.id)], user.name


async def test_hidden_list_also_excludes_chat_runs(
    client: httpx.AsyncClient, db, user_a, user_b, admin_user
):
    """D2 × #221 — 숨긴 채팅 run도 「숨긴 결과 보기」(`?hidden=true`)에 나오지 않는다. 숨긴 수동
    run은 그대로 나온다.
    """
    nb, wf, run, _ = await _setup(db, user_a, user_b)
    run.hidden_at = datetime.now(UTC)
    db.add(run)
    manual = AgentRun(
        agent_id=wf.id,
        notebook_id=nb.id,
        user_id=user_b.id,
        params={},
        status="failed",
        trigger="manual",
        hidden_at=datetime.now(UTC),
    )
    db.add(manual)
    await db.commit()

    for user in (user_b, user_a, admin_user):
        path = f"/api/v1/notebooks/{nb.id}/agent-runs?hidden=true"
        res = await _call(client, user, "GET", path)
        assert res.status_code == 200, res.text
        assert [r["id"] for r in res.json()] == [str(manual.id)], user.name


async def test_hide_failed_skips_chat_runs(client: httpx.AsyncClient, db, user_a, user_b):
    """D2 — 일괄 숨기기는 목록에 없는 채팅 run을 숨기지 않는다."""
    nb = await notebook(db, user_a)
    await share_with(db, nb.id, user_b, "write")
    await db.commit()
    wf = await chat_workflow(db, user_b)
    failed_chat = await chat_run(db, wf, nb, user_b, status="failed")
    failed_manual = AgentRun(
        agent_id=wf.id, notebook_id=nb.id, user_id=user_b.id, params={}, status="failed"
    )
    db.add(failed_manual)
    await db.commit()

    for user in (user_a, user_b):
        res = await _call(client, user, "POST", f"/api/v1/notebooks/{nb.id}/agent-runs/hide-failed")
        assert res.status_code == 200, res.text
        assert str(failed_chat.id) not in res.json()["run_ids"]

    await db.refresh(failed_chat)
    await db.refresh(failed_manual)
    assert failed_chat.hidden_at is None
    assert failed_manual.hidden_at is not None


async def _note_from_run(
    client: httpx.AsyncClient, user: User, nb: Notebook, run_id
) -> httpx.Response:
    return await client.post(
        f"/api/v1/notebooks/{nb.id}/notes",
        json={
            "title": "결과 노트",
            "content": "무시됨",
            "origin": "agent",
            "origin_ref": str(run_id),
            "artifact_name": ARTIFACT["name"],
        },
        headers=await token(client, user),
    )


async def test_note_origin_hides_others_chat_run_like_a_missing_run(
    client: httpx.AsyncClient, db, user_a, user_b, monkeypatch
):
    """타인 채팅 run을 노트 출처로 옮기지 못한다 — 없는 run과 응답(상태·본문)이 같다. 실행자 본인은
    통과한다.
    """
    import uuid

    store = {ARTIFACT["key"]: "# 결과 본문".encode()}
    monkeypatch.setattr("biznotes_core.services.storage.get_object", lambda b, k: store[k])
    monkeypatch.setattr(
        "biznotes_core.services.storage.stat_object_size", lambda b, k: len(store[k])
    )
    nb = await notebook(db, user_a)
    await share_with(db, nb.id, user_b, "write")
    await db.commit()
    wf = await chat_workflow(db, user_b)
    run = await chat_run(db, wf, nb, user_b, status="succeeded", artifact_keys=[ARTIFACT])

    others = await _note_from_run(client, user_a, nb, run.id)
    missing = await _note_from_run(client, user_a, nb, uuid.uuid4())
    own = await _note_from_run(client, user_b, nb, run.id)

    assert others.status_code == missing.status_code == 422
    assert others.json() == missing.json() == ORIGIN_INVALID
    assert own.status_code == 201, own.text
    assert own.json()["content"] == "# 결과 본문"


async def test_non_chat_run_keeps_notebook_read_rule(
    client: httpx.AsyncClient, db, user_a, user_b
):
    """대조군 — 채팅이 아닌 run은 지금처럼 노트북 read 이상이면 타인 run도 본다."""
    nb = await notebook(db, user_a)
    await share_with(db, nb.id, user_b, "read")
    await db.commit()
    wf: Agent = await chat_workflow(db, user_b)
    manual = AgentRun(
        agent_id=wf.id, notebook_id=nb.id, user_id=user_b.id, params={}, status="succeeded"
    )
    db.add(manual)
    await db.commit()

    res = await _call(client, user_a, "GET", f"/api/v1/agent-runs/{manual.id}")
    logs = await _call(client, user_a, "GET", f"/api/v1/agent-runs/{manual.id}/logs")

    assert res.status_code == logs.status_code == 200
