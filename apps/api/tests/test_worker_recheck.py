"""워커 실행 직전 인가 재검사의 거부 분기 (계획 AC-35·AC-36, §4.5 T-28~T-31) +
노트북 권한 재검사(core-v0.17.1-upgrade AC-2.3 — BL-217, 계획 §5.4·D-7).

`workers/tasks/agent.py`의 재검사는 웹훅·코어 스케줄러·이벤트 트리거 run과 비-스킬 수동 run에
대한 마지막 인가 지점이다. 아래 테스트는 전부 `agent_task._run(run_id)`을 실제로 호출하며
`_run` 본문과 `assert_agent_execution_allowed`·`get_permission`을 mock하지 않는다. 대체하는 것은
경계뿐이다 — LLM 게이트웨이(`complete_with_tools`)와 이벤트 발행(`_publish`).
"""

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import httpx
import pytest
from biznotes_core.models import (
    Agent,
    AgentRun,
    AgentRunApproval,
    ChatMessage,
    ChatSession,
    Notebook,
    NotebookShare,
    Skill,
    User,
    UserGroup,
)
from biznotes_core.models.agent import TRIGGER_SOURCES
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult
from sqlalchemy import select
from structlog.testing import capture_logs

import app.workers.tasks.agent as agent_task
from tests.conftest import bearer, login, share_with

DENIED = "에이전트가 실행 직전 인가 조건을 더 이상 만족하지 않습니다"


@pytest.fixture(autouse=True)
def _boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Task:
        id = "fake"

    monkeypatch.setattr(agent_task.run_agent, "apply_async", lambda *a, **k: _Task())
    monkeypatch.setattr(agent_task, "_publish", lambda run_id, event: None)


async def _notebook(db, owner: User) -> Notebook:
    notebook = Notebook(title="recheck-nb", owner_id=owner.id)
    db.add(notebook)
    await db.commit()
    await db.refresh(notebook)
    return notebook


async def _workflow(db, *, scope: str, owner_id: uuid.UUID | None, definition=None) -> Agent:
    agent = Agent(
        name=f"wf-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        icon="⚙️",
        definition=definition
        or {"nodes": [{"id": "c", "type": "constant", "value": "v"}], "edges": []},
        params_schema={},
        scope=scope,
        owner_id=owner_id,
        enabled=True,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


async def _queued(
    db, agent: Agent, notebook: Notebook, user: User, *, trigger: str = "manual"
) -> AgentRun:
    # 채팅 run은 세션·질문 행 연결이 있어야 한다(코어 스테이징 `_validate_chat_run_link`와 같은
    # 모양) — 워커가 질문·이력을 읽는다(#66)
    link: dict[str, Any] = {}
    if trigger == "chat":
        chat_session = ChatSession(notebook_id=notebook.id, user_id=user.id)
        db.add(chat_session)
        await db.flush()
        question = ChatMessage(session_id=chat_session.id, role="user", content="질문")
        db.add(question)
        await db.flush()
        link = {"chat_session_id": chat_session.id, "prompt_message_id": question.id}
    run = AgentRun(
        agent_id=agent.id,
        notebook_id=notebook.id,
        user_id=user.id,
        params={},
        status="queued",
        trigger=trigger,
        **link,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def _assert_denied(db, run: AgentRun, result: dict[str, Any]) -> None:
    assert result == {"ok": False, "error": DENIED}
    await db.refresh(run)
    assert run.status == "failed"
    assert run.error == DENIED
    assert run.finished_at is not None


async def test_t28_skill_disabled_after_queue_fails_the_run(
    client: httpx.AsyncClient, db, user_a, mem_storage
):
    """T-28: 큐잉 뒤 스킬이 꺼지면(`enabled=false`) 워커가 실행하지 않고 failed로 닫는다.

    wrapper Agent의 enabled는 그대로다 — 스킬 상태는 재검사(`assert_can_run_skill_wrapper`)만 본다.
    """
    token = await login(client, user_a.email)
    created = await client.post(
        "/api/v1/skills", json={"name": "끌스킬", "description": "d"}, headers=bearer(token)
    )
    skill_id = created.json()["id"]
    frozen = await client.post(
        f"/api/v1/skills/{skill_id}/freeze", json={"changelog": "v1"}, headers=bearer(token)
    )
    wrapper_id = frozen.json()["wrapper_agent_id"]
    nb = await client.post("/api/v1/notebooks", json={"title": "nb"}, headers=bearer(token))
    queued = await client.post(
        f"/api/v1/notebooks/{nb.json()['id']}/agent-runs",
        json={"agent_id": wrapper_id, "params": {"topic": "x"}},
        headers=bearer(token),
    )
    assert queued.status_code == 202, queued.text

    skill = await db.get(Skill, uuid.UUID(skill_id))
    assert skill is not None
    skill.enabled = False
    await db.commit()
    wrapper = await db.get(Agent, uuid.UUID(wrapper_id))
    assert wrapper is not None and wrapper.enabled is True

    run = await db.get(AgentRun, uuid.UUID(queued.json()["id"]))
    assert run is not None
    await _assert_denied(db, run, await agent_task._run(str(run.id)))


async def test_t29_non_owner_user_scope_workflow_run_fails(db, user_a, user_b):
    """T-29: 비소유자의 user-scope 워크플로 run(DB 직접 생성 — 웹훅·스케줄 경로 모사) → failed."""
    workflow = await _workflow(db, scope="user", owner_id=user_a.id)
    run = await _queued(db, workflow, await _notebook(db, user_b), user_b)

    await _assert_denied(db, run, await agent_task._run(str(run.id)))


async def test_t29_owner_run_of_the_same_workflow_is_allowed(db, user_a):
    """T-29 대조: 같은 정의를 소유자 run으로 돌리면 재검사를 통과해 실행된다."""
    workflow = await _workflow(db, scope="user", owner_id=user_a.id)
    run = await _queued(db, workflow, await _notebook(db, user_a), user_a)

    assert await agent_task._run(str(run.id)) == {"ok": True}
    await db.refresh(run)
    assert run.status == "succeeded", run.error


def _call_agent_then_finish(target_id: str):
    turns = {"n": 0}

    async def fake_complete(db_, model_id, messages, tools, **kwargs):
        idx = turns["n"]
        turns["n"] += 1
        if idx == 0:
            arguments = {"agent_id": target_id}
            return ToolTurnResult(
                content="",
                tool_calls=[ToolCall(id="c1", name="call_agent", arguments=arguments)],
                assistant_message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "call_agent", "arguments": json.dumps(arguments)},
                        }
                    ],
                },
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                model=None,
            )
        return ToolTurnResult(
            content="완료",
            tool_calls=[],
            assistant_message={"role": "assistant", "content": "완료"},
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            model=None,
        )

    return fake_complete


async def test_t30_permission_revoked_while_awaiting_approval_does_not_consume_approval(
    db, user_a, user_b, mock_llm, monkeypatch
):
    """T-30: 승인 대기 중 권한이 철회되면 재개 태스크는 failed로 닫고 승인을 소비하지 않는다.

    재검사가 승인 CAS(`_resolve_pending_approval`)보다 앞에 있다는 순서를 고정한다.
    """
    target = await _workflow(
        db,
        scope="system",
        owner_id=None,
        definition={"nodes": [{"id": "g", "type": "llm", "prompt": "x"}], "edges": []},
    )
    gated = await _workflow(
        db,
        scope="system",  # user_b가 실행할 수 있는 정의
        owner_id=user_a.id,
        definition={"nodes": [{"id": "r", "type": "react_agent", "goal": "x"}], "edges": []},
    )
    run = await _queued(db, gated, await _notebook(db, user_b), user_b)
    monkeypatch.setattr(gateway_llm, "complete_with_tools", _call_agent_then_finish(str(target.id)))

    assert await agent_task._run(str(run.id)) == {"ok": True, "awaiting_approval": True}
    approval = (
        await db.execute(select(AgentRunApproval).where(AgentRunApproval.run_id == run.id))
    ).scalar_one()
    approval.status = "approved"
    approval.consumed = False
    await db.commit()

    # 권한 철회 — 정의가 user_a의 private(user scope)가 되어 user_b는 더 이상 실행할 수 없다
    gated.scope = "user"
    await db.commit()

    await _assert_denied(db, run, await agent_task._run(str(run.id)))
    await db.refresh(approval)
    assert approval.status == "approved"
    assert approval.consumed is False
    assert approval.checkpoint is not None


async def test_t31_inactive_user_run_fails(db, user_a):
    """T-31(AC-36): 큐잉 뒤 비활성화된 계정의 run은 그 계정 권한으로 실행되지 않는다."""
    workflow = await _workflow(db, scope="system", owner_id=None)
    run = await _queued(db, workflow, await _notebook(db, user_a), user_a)
    user_a.is_active = False
    await db.commit()

    await _assert_denied(db, run, await agent_task._run(str(run.id)))


async def test_t8_deleted_user_run_is_never_executed(db, user_a):
    """T-8 관찰(계획 대비 정정 — 보고서 ⑥): `AgentRun.user_id`는 `ondelete="CASCADE"`라
    사용자 행을 지우면 run도 함께 지워진다. 계획이 기대한 "run이 failed로 남는다"는
    성립하지 않고, 워커는 run을 찾지 못해 아무것도 실행하지 않는다(fail-closed)."""
    workflow = await _workflow(db, scope="system", owner_id=None)
    run = await _queued(db, workflow, await _notebook(db, user_a), user_a)
    run_id = run.id
    await db.delete(user_a)
    await db.commit()

    assert await agent_task._run(str(run_id)) == {"ok": False, "error": "run not found"}
    db.expire_all()
    assert await db.get(AgentRun, run_id) is None


async def test_t8_variant_deleted_sharee_run_on_others_notebook_is_never_executed(
    db, user_a, user_b
):
    """T-8 변형(BL-132): 노트북은 살아남는 user_b 소유이고, run은 공유받은 user_a가 만든다.

    T-8은 삭제 사용자가 노트북 소유자이기도 해서 users→notebooks→agent_runs CASCADE와
    users→agent_runs CASCADE를 구분하지 못한다. 여기서는 노트북이 지워지지 않으므로
    run 행을 지우는 경로는 `AgentRun.user_id`의 `ondelete="CASCADE"`뿐이다.
    워커는 run을 찾지 못해 아무것도 실행하지 않고(fail-closed), user_b의 노트북은 남는다.
    """
    workflow = await _workflow(db, scope="system", owner_id=None)
    notebook = await _notebook(db, user_b)
    share = await share_with(db, notebook.id, user_a, "write")
    await db.commit()
    run = await _queued(db, workflow, notebook, user_a)
    run_id, notebook_id, owner_id = run.id, notebook.id, user_b.id
    member_key = (user_a.id, share.group_id)

    await db.delete(user_a)
    await db.commit()

    assert await agent_task._run(str(run_id)) == {"ok": False, "error": "run not found"}
    db.expire_all()
    assert await db.get(AgentRun, run_id) is None
    surviving = await db.get(Notebook, notebook_id)
    assert surviving is not None
    assert surviving.owner_id == owner_id
    assert await db.get(User, owner_id) is not None
    # 공유는 그룹 대상이라(코어 v0.28.0 #278 3단계) 공유 행이 아니라 그 사용자의 소속이
    # 함께 지워진다
    assert await db.get(UserGroup, member_key) is None


# ---------- AC-2.3 노트북 권한 재검사 — trigger 행렬 (BL-217, 계획 §5.4·D-7) ----------

DENIED_AT_START_EVENT = "agent_run_denied_at_start"
# 코어 `TRIGGER_SOURCES`는 대화형 2종뿐이다. 옛 무인 trigger 값은 더 만들지 않지만, 오면 쓰기
# 이상으로 본다(fail-closed) — 워커 상수를 쓰지 않고 따로 적는다
UNATTENDED = ["schedule", "webhook", "slack", "github", "event"]
INTERACTIVE = ["manual", "chat"]


async def _shared_notebook(db, owner: User, member: User, permission: str | None) -> Notebook:
    """`owner` 소유 노트북 — `permission`이 있으면 `member`에게 그 수준으로 직접 공유한다."""
    notebook = await _notebook(db, owner)
    if permission is not None:
        await share_with(db, notebook.id, member, permission)
        await db.commit()
    return notebook


def _start_denials(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e.get("event") == DENIED_AT_START_EVENT]


async def _run_denied_at_start(db, run: AgentRun, *, trigger: str) -> None:
    """실 `_run` → 고정 문구로 failed + `agent_run_denied_at_start`(run_id·trigger·reason만)."""
    with capture_logs() as logs:
        result = await agent_task._run(str(run.id))
    await _assert_denied(db, run, result)
    denials = _start_denials(logs)
    assert len(denials) == 1, logs
    assert set(denials[0]) == {"event", "log_level", "run_id", "trigger", "reason"}
    assert (denials[0]["run_id"], denials[0]["trigger"], denials[0]["reason"]) == (
        str(run.id),
        trigger,
        "notebook_forbidden",
    )
    assert denials[0]["log_level"] == "warning"


async def _run_succeeds(db, run: AgentRun) -> None:
    with capture_logs() as logs:
        assert await agent_task._run(str(run.id)) == {"ok": True}
    await db.refresh(run)
    assert run.status == "succeeded", run.error
    assert _start_denials(logs) == []


@pytest.mark.parametrize("trigger", UNATTENDED)
async def test_unattended_run_with_read_share_is_denied_at_start(db, user_a, user_b, trigger: str):
    """ⓐ 무인 run + 등록자 read 공유 → failed(무인은 쓰기 이상 — 발화 시점 관문과 같다)."""
    workflow = await _workflow(db, scope="system", owner_id=None)
    notebook = await _shared_notebook(db, user_a, user_b, "read")
    run = await _queued(db, workflow, notebook, user_b, trigger=trigger)

    await _run_denied_at_start(db, run, trigger=trigger)


@pytest.mark.parametrize("trigger", UNATTENDED)
async def test_unattended_run_with_write_share_succeeds(db, user_a, user_b, trigger: str):
    """ⓑ 같은 trigger + write 공유 → succeeded."""
    workflow = await _workflow(db, scope="system", owner_id=None)
    notebook = await _shared_notebook(db, user_a, user_b, "write")
    run = await _queued(db, workflow, notebook, user_b, trigger=trigger)

    await _run_succeeds(db, run)


def _interactive_definition(trigger: str, monkeypatch: pytest.MonkeyPatch) -> dict | None:
    """채팅 run은 채팅 진입 형태 정의여야 한다 — 워커가 실행 전에 코어 `assert_chat_entry_allowed`로
    다시 본다(#66). react 노드 하나와 LLM 경계(최종 답 1턴)를 준비한다. 그 밖의 trigger는 기본 정의.
    """
    if trigger != "chat":
        return None

    async def final(*_a: Any, **_k: Any) -> ToolTurnResult:
        return ToolTurnResult(
            content="완료",
            tool_calls=[],
            assistant_message={"role": "assistant", "content": "완료"},
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            model=None,
        )

    monkeypatch.setattr(gateway_llm, "complete_with_tools", final)
    node = {
        "id": "r",
        "type": "react_agent",
        "goal": "{chat_message}",
        "allowed_tools": ["rag_search"],
    }
    return {"nodes": [node], "edges": []}


@pytest.mark.parametrize("trigger", INTERACTIVE)
async def test_interactive_run_with_read_share_succeeds(
    db, user_a, user_b, trigger: str, mock_llm, monkeypatch
):
    """ⓒ 대화형 run + read 공유 → succeeded(진입 시점 `_notebook_readable`과 같은 수준)."""
    definition = _interactive_definition(trigger, monkeypatch)
    workflow = await _workflow(db, scope="system", owner_id=None, definition=definition)
    notebook = await _shared_notebook(db, user_a, user_b, "read")
    run = await _queued(db, workflow, notebook, user_b, trigger=trigger)

    await _run_succeeds(db, run)


async def test_manual_run_after_share_revoked_is_denied_at_start(db, user_a, user_b):
    """ⓓ 큐잉 뒤 공유가 철회된 수동 run → failed(read도 잃었다)."""
    workflow = await _workflow(db, scope="system", owner_id=None)
    notebook = await _shared_notebook(db, user_a, user_b, "read")
    run = await _queued(db, workflow, notebook, user_b, trigger="manual")
    share = (
        await db.execute(select(NotebookShare).where(NotebookShare.notebook_id == notebook.id))
    ).scalar_one()
    await db.delete(share)
    await db.commit()

    await _run_denied_at_start(db, run, trigger="manual")


async def test_admin_unattended_run_without_share_succeeds(db, user_a, admin_user):
    """ⓔ 관리자 등록자는 공유 없는 남의 노트북도 admin 승격으로 owner다."""
    workflow = await _workflow(db, scope="system", owner_id=None)
    notebook = await _notebook(db, user_a)
    run = await _queued(db, workflow, notebook, admin_user, trigger="schedule")

    await _run_succeeds(db, run)


async def test_unknown_trigger_requires_write(db, user_a, user_b):
    """ⓕ 두 집합에 없는 trigger 값 + read 공유 → failed(엄격한 쪽, fail-closed)."""
    assert "legacy" not in TRIGGER_SOURCES
    workflow = await _workflow(db, scope="system", owner_id=None)
    notebook = await _shared_notebook(db, user_a, user_b, "read")
    run = await _queued(db, workflow, notebook, user_b, trigger="legacy")

    await _run_denied_at_start(db, run, trigger="legacy")


async def test_awaiting_unattended_run_demoted_to_read_fails_without_consuming_approval(
    db, user_a, user_b, mock_llm, monkeypatch
):
    """ⓖ 승인 대기 무인 run에서 write → read 강등 뒤 재개 → failed, 승인 미소비(T-30 단언 재사용).

    노트북 권한 재검사가 승인 CAS(`_resolve_pending_approval`)보다 앞이라는 순서를 고정한다.
    """
    target = await _workflow(
        db,
        scope="system",
        owner_id=None,
        definition={"nodes": [{"id": "g", "type": "llm", "prompt": "x"}], "edges": []},
    )
    gated = await _workflow(
        db,
        scope="system",
        owner_id=None,
        definition={"nodes": [{"id": "r", "type": "react_agent", "goal": "x"}], "edges": []},
    )
    notebook = await _shared_notebook(db, user_a, user_b, "write")
    run = await _queued(db, gated, notebook, user_b, trigger="schedule")
    monkeypatch.setattr(gateway_llm, "complete_with_tools", _call_agent_then_finish(str(target.id)))

    assert await agent_task._run(str(run.id)) == {"ok": True, "awaiting_approval": True}
    approval = (
        await db.execute(select(AgentRunApproval).where(AgentRunApproval.run_id == run.id))
    ).scalar_one()
    approval.status = "approved"
    approval.consumed = False
    share = (
        await db.execute(select(NotebookShare).where(NotebookShare.notebook_id == notebook.id))
    ).scalar_one()
    share.permission = "read"  # 승인 대기 중 강등
    await db.commit()

    await _run_denied_at_start(db, run, trigger="schedule")
    await db.refresh(approval)
    assert approval.status == "approved"
    assert approval.consumed is False
    assert approval.checkpoint is not None


def test_trigger_permission_mapping_covers_every_core_trigger():
    """ⓗ 매핑 완전성 — 코어 `TRIGGER_SOURCES`는 모두 대화형이고, 그 밖의 값은 쓰기 이상이다.

    코어가 trigger를 더하면 이 테스트가 `make ci`에서 실패한다(계획 §5.4·`run-trigger-authz` 계약).
    """
    assert set(TRIGGER_SOURCES) == agent_task._INTERACTIVE_TRIGGERS == set(INTERACTIVE)
    for trigger in UNATTENDED:
        assert agent_task._required_notebook_permissions(trigger) == frozenset({"owner", "write"})
    assert agent_task._required_notebook_permissions("manual") == frozenset(
        {"owner", "write", "read"}
    )


# ---------- #68(BL-121) 재검사 중 예기치 않은 예외 — fail-closed ----------

RECHECK_ERROR = "에이전트 실행 직전 인가 재검사 중 오류가 발생해 실행하지 않았습니다"
SECRET = "postgresql://svc:s3cr3t@db/rapid"  # 예외 원문 — run.error·이벤트로 새면 안 된다


@pytest.fixture
def _no_execution(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """실행 경로 경계(`execute_agent`·`execute_skill_package`) — 불리면 기록한다."""
    from biznotes_core.services.agents import runtime
    from biznotes_core.services.skills import builder

    called: list[str] = []

    async def _execute_agent(*a: Any, **k: Any) -> str:
        called.append("execute_agent")
        return "ran"

    async def _execute_skill_package(*a: Any, **k: Any) -> str:
        called.append("execute_skill_package")
        return "ran"

    monkeypatch.setattr(runtime, "execute_agent", _execute_agent)
    monkeypatch.setattr(builder, "execute_skill_package", _execute_skill_package)
    return called


@pytest.fixture
def _events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        agent_task, "_publish", lambda run_id, event: published.append((run_id, event))
    )
    return published


async def _assert_recheck_error(
    db,
    run: AgentRun,
    result: dict[str, Any],
    logs: Sequence[Mapping[str, Any]],
    events: list[tuple[str, dict[str, Any]]],
    called: list[str],
) -> None:
    assert result == {"ok": False, "error": RECHECK_ERROR}
    run_id = run.id
    db.expire_all()
    fresh = await db.get(AgentRun, run_id)
    assert fresh is not None
    assert fresh.status == "failed"
    assert fresh.error == RECHECK_ERROR
    assert SECRET not in (fresh.error or "")
    assert fresh.finished_at is not None
    assert called == []  # 실행하지 않았다(fail-closed)
    assert events == [(str(run_id), {"type": "status", "status": "failed", "error": RECHECK_ERROR})]
    errors = [e for e in logs if e.get("event") == "agent_run_recheck_error"]
    assert len(errors) == 1, logs
    assert errors[0]["log_level"] == "error"
    assert errors[0]["run_id"] == str(run_id)


async def test_recheck_unexpected_error_in_execution_policy_fails_the_run(
    db, user_a, monkeypatch, _no_execution, _events
):
    """#68: `assert_agent_execution_allowed`가 HTTPException이 아닌 예외를 던지면 run이
    `running`에 남지 않고 고정 문구로 failed가 되며 실행하지 않는다. 원문은 로그로만 간다."""
    from biznotes_core.services.agents import policy

    async def _boom(*a: Any, **k: Any) -> None:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(policy, "assert_agent_execution_allowed", _boom)
    workflow = await _workflow(db, scope="system", owner_id=None)
    run = await _queued(db, workflow, await _notebook(db, user_a), user_a)

    with capture_logs() as logs:
        result = await agent_task._run(str(run.id))
    await _assert_recheck_error(db, run, result, logs, _events, _no_execution)


async def test_recheck_db_error_in_notebook_permission_rolls_back_and_fails_the_run(
    db, user_a, monkeypatch, _no_execution, _events
):
    """#68: 노트북 권한 재검사 중 실제 DB 오류(트랜잭션 aborted)가 나도 롤백 뒤 failed를 쓴다."""
    from sqlalchemy import text

    async def _broken_permission(session, notebook, user) -> str:
        await session.execute(text("SELECT * FROM no_such_table_bl121"))
        return "owner"

    monkeypatch.setattr(agent_task, "get_permission", _broken_permission)
    workflow = await _workflow(db, scope="system", owner_id=None)
    run = await _queued(db, workflow, await _notebook(db, user_a), user_a)

    with capture_logs() as logs:
        result = await agent_task._run(str(run.id))
    await _assert_recheck_error(db, run, result, logs, _events, _no_execution)


async def test_recheck_unexpected_error_on_resume_fails_without_consuming_approval(
    db, user_a, user_b, mock_llm, monkeypatch, _events
):
    """#68: 승인 재개 경로에서도 재검사 예외는 failed로 닫고 승인을 소비하지 않는다."""
    from biznotes_core.services.agents import runtime

    target = await _workflow(
        db,
        scope="system",
        owner_id=None,
        definition={"nodes": [{"id": "g", "type": "llm", "prompt": "x"}], "edges": []},
    )
    gated = await _workflow(
        db,
        scope="system",
        owner_id=None,
        definition={"nodes": [{"id": "r", "type": "react_agent", "goal": "x"}], "edges": []},
    )
    run = await _queued(db, gated, await _notebook(db, user_b), user_b)
    monkeypatch.setattr(gateway_llm, "complete_with_tools", _call_agent_then_finish(str(target.id)))
    assert await agent_task._run(str(run.id)) == {"ok": True, "awaiting_approval": True}
    approval = (
        await db.execute(select(AgentRunApproval).where(AgentRunApproval.run_id == run.id))
    ).scalar_one()
    approval.status = "approved"
    approval.consumed = False
    await db.commit()
    _events.clear()

    called: list[str] = []

    async def _execute_agent(*a: Any, **k: Any) -> str:
        called.append("execute_agent")
        return "ran"

    async def _boom(*a: Any, **k: Any) -> None:
        raise RuntimeError(SECRET)

    from biznotes_core.services.agents import policy

    monkeypatch.setattr(runtime, "execute_agent", _execute_agent)
    monkeypatch.setattr(policy, "assert_agent_execution_allowed", _boom)

    with capture_logs() as logs:
        result = await agent_task._run(str(run.id))
    await _assert_recheck_error(db, run, result, logs, _events, called)
    await db.refresh(approval)
    assert approval.status == "approved"
    assert approval.consumed is False
    assert approval.checkpoint is not None


async def test_recheck_http_exception_keeps_existing_denial(db, user_a, monkeypatch, _events):
    """#68 회귀: HTTPException 거부는 기존 고정 문구·이벤트 없음 그대로다."""
    from biznotes_core.services.agents import policy
    from fastapi import HTTPException

    async def _deny(*a: Any, **k: Any) -> None:
        raise HTTPException(status_code=403, detail=SECRET)

    monkeypatch.setattr(policy, "assert_agent_execution_allowed", _deny)
    workflow = await _workflow(db, scope="system", owner_id=None)
    run = await _queued(db, workflow, await _notebook(db, user_a), user_a)

    await _assert_denied(db, run, await agent_task._run(str(run.id)))
    assert _events == []


# ---------- #79 소유자 draft 테스트 실행 (docs/plans/owner-draft-run.md) ----------

DISABLED = "에이전트를 찾을 수 없거나 비활성화됨"


async def _draft(db, *, scope: str, owner_id: uuid.UUID | None, definition=None) -> Agent:
    """미게시(`enabled=false`) workflow — 편집기 「테스트 실행」 대상."""
    agent = await _workflow(db, scope=scope, owner_id=owner_id, definition=definition)
    agent.enabled = False
    await db.commit()
    return agent


@pytest.mark.parametrize("trigger", INTERACTIVE)
async def test_owner_draft_interactive_run_succeeds(
    db, user_a, trigger: str, mock_llm, monkeypatch
):
    """AC-1·2: 소유자의 draft workflow 대화형 run은 진입 시점과 같이 실행된다(코어
    `can_run_owner_draft`) — 워커가 enabled로 먼저 막지 않는다."""
    definition = _interactive_definition(trigger, monkeypatch)
    draft = await _draft(db, scope="user", owner_id=user_a.id, definition=definition)
    run = await _queued(db, draft, await _notebook(db, user_a), user_a, trigger=trigger)

    await _run_succeeds(db, run)


async def test_non_owner_draft_interactive_run_is_denied(db, user_a, user_b):
    """AC-3(비인가): 노트북 write 공유자라도 남의 draft는 실행하지 않는다.

    scope=system이라 enabled였다면 user_b도 실행할 수 있는 정의다 — 거부 사유가 draft뿐이다.
    """
    draft = await _draft(db, scope="system", owner_id=user_a.id)
    notebook = await _shared_notebook(db, user_a, user_b, "write")
    run = await _queued(db, draft, notebook, user_b)

    await _assert_denied(db, run, await agent_task._run(str(run.id)))


async def test_admin_draft_interactive_run_of_others_is_denied(db, user_a, admin_user):
    """AC-4(비인가): admin도 남의 draft는 실행하지 않는다.

    `can_run_owner_draft`에는 admin 예외가 없다.
    """
    draft = await _draft(db, scope="system", owner_id=user_a.id)
    run = await _queued(db, draft, await _notebook(db, admin_user), admin_user)

    await _assert_denied(db, run, await agent_task._run(str(run.id)))


@pytest.mark.parametrize("trigger", UNATTENDED)
async def test_unattended_run_of_unpublished_workflow_is_stopped(db, user_a, trigger: str):
    """AC-5(D-1 A): 무인 run은 큐잉 뒤 게시 취소(enabled=false)되면 소유자 것이라도 실행하지 않는다
    — 발화 시점 필터와 같은 kill switch. 종료 시각도 남긴다."""
    workflow = await _workflow(db, scope="user", owner_id=user_a.id)
    run = await _queued(db, workflow, await _notebook(db, user_a), user_a, trigger=trigger)
    workflow.enabled = False
    await db.commit()

    assert await agent_task._run(str(run.id)) == {"ok": False, "error": DISABLED}
    await db.refresh(run)
    assert run.status == "failed"
    assert run.error == DISABLED
    assert run.finished_at is not None


async def test_unknown_trigger_run_of_unpublished_workflow_is_stopped(db, user_a):
    """AC-5: 두 집합에 없는 trigger도 무인처럼 enabled를 요구한다(fail-closed)."""
    draft = await _draft(db, scope="user", owner_id=user_a.id)
    run = await _queued(db, draft, await _notebook(db, user_a), user_a, trigger="future-kind")

    assert await agent_task._run(str(run.id)) == {"ok": False, "error": DISABLED}


async def test_disabled_skill_wrapper_interactive_run_is_denied(
    client: httpx.AsyncClient, db, user_a, mem_storage
):
    """AC-6: wrapper는 `kind="skill"`이라 소유자 draft가 아니다 — 큐잉 뒤 꺼지면 실행하지 않는다."""
    token = await login(client, user_a.email)
    created = await client.post(
        "/api/v1/skills", json={"name": "끌래퍼", "description": "d"}, headers=bearer(token)
    )
    frozen = await client.post(
        f"/api/v1/skills/{created.json()['id']}/freeze",
        json={"changelog": "v1"},
        headers=bearer(token),
    )
    wrapper_id = frozen.json()["wrapper_agent_id"]
    nb = await client.post("/api/v1/notebooks", json={"title": "nb"}, headers=bearer(token))
    queued = await client.post(
        f"/api/v1/notebooks/{nb.json()['id']}/agent-runs",
        json={"agent_id": wrapper_id, "params": {"topic": "x"}},
        headers=bearer(token),
    )
    assert queued.status_code == 202, queued.text

    wrapper = await db.get(Agent, uuid.UUID(wrapper_id))
    assert wrapper is not None and wrapper.owner_id == user_a.id
    wrapper.enabled = False
    await db.commit()

    run = await db.get(AgentRun, uuid.UUID(queued.json()["id"]))
    assert run is not None
    await _assert_denied(db, run, await agent_task._run(str(run.id)))


async def test_owner_draft_run_resumes_after_approval(db, user_a, mock_llm, monkeypatch):
    """AC-7: 소유자 draft의 대화형 run은 승인 대기 뒤 재개 태스크에서도 실행된다(승인 소비)."""
    target = await _workflow(
        db,
        scope="system",
        owner_id=None,
        definition={"nodes": [{"id": "g", "type": "llm", "prompt": "x"}], "edges": []},
    )
    draft = await _draft(
        db,
        scope="user",
        owner_id=user_a.id,
        definition={"nodes": [{"id": "r", "type": "react_agent", "goal": "x"}], "edges": []},
    )
    run = await _queued(db, draft, await _notebook(db, user_a), user_a)
    monkeypatch.setattr(gateway_llm, "complete_with_tools", _call_agent_then_finish(str(target.id)))

    assert await agent_task._run(str(run.id)) == {"ok": True, "awaiting_approval": True}
    approval = (
        await db.execute(select(AgentRunApproval).where(AgentRunApproval.run_id == run.id))
    ).scalar_one()
    approval.status = "approved"
    approval.consumed = False
    await db.commit()

    assert await agent_task._run(str(run.id)) == {"ok": True}
    await db.refresh(run)
    await db.refresh(approval)
    assert run.status == "succeeded", run.error
    assert approval.consumed is True
