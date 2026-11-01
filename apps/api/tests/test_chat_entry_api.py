"""채팅 `/` 진입 API(#66 PR-1) — 팔레트 목록·이름 풀기·진입 실행·비인가·동시성·이력·회귀.

설계: docs/plans/chat-slash-entry.md AC-1·AC-2·AC-3·AC-6(이력)·AC-7·AC-8. 판정은 코어
(`list_chat_entry_agents`·`resolve_chat_entry_by_name`·`assert_chat_entry_allowed`)를 대체하지 않고
실제로 돈다. LLM은 정적·자율 채팅 경계(`boundary`)만 대체한다.
"""

import json
import uuid

import httpx
import pytest
from biznotes_core.models import AgentRun, ChatMessage
from biznotes_core.services.agent_runs import ActiveChatRunExists, ChatRunLinkInvalid
from biznotes_core.services.agents.chat_entry import list_chat_entry_agents
from fastapi import FastAPI
from sqlalchemy import func, select

from app.core.exception_handlers import register_exception_handlers
from app.services.chat_entry import (
    ENTRY_NO_RESULT_MARK,
    entry_failed_text,
    history_for_llm,
    write_entry_result,
)
from tests.chat_entry_fakes import (
    boundaries,  # noqa: F401 — 픽스처
    chat_run,
    chat_workflow,
    expose,
    legacy_skill,
    notebook,
    post_entry,
    session,
    token,
)
from tests.conftest import bearer, login, share_with
from tests.test_chat_mindmap_origin import boundary, llm_seed  # noqa: F401 — 픽스처

pytestmark = pytest.mark.usefixtures("boundaries", "mem_storage")


async def _run_count(db) -> int:
    return int(await db.scalar(select(func.count()).select_from(AgentRun)) or 0)


async def _message_count(db) -> int:
    return int(await db.scalar(select(func.count()).select_from(ChatMessage)) or 0)


async def _entries(client: httpx.AsyncClient, user) -> list[dict]:
    res = await client.get("/api/v1/chat/entries", headers=await token(client, user))
    assert res.status_code == 200, res.text
    return res.json()


# ---------- AC-1 팔레트 목록 ----------


async def test_entries_match_core_list_and_hide_private_or_unapproved(
    client: httpx.AsyncClient, db, user_a, user_b, admin_user
):
    """`/chat/entries`는 코어 목록을 그 순서대로 직렬화한다 — 본인 private·system·승인된 org 스킬,
    채팅 형태 workflow. 타인 private 스킬·workflow와 미승인 org 스킬은 없다."""
    own_skill, _, own_wrapper = await legacy_skill(db, user_a, "my-brief")
    _, _, b_private = await legacy_skill(db, user_b, "b-secret")
    sys_skill, sys_version, sys_wrapper = await legacy_skill(db, admin_user, "sys-brief")
    await expose(db, sys_skill, sys_version, "system", approved=False)
    org_skill, org_version, org_wrapper = await legacy_skill(db, user_b, "org-ok")
    await expose(db, org_skill, org_version, "org", approved=True)
    pending_skill, pending_version, pending_wrapper = await legacy_skill(db, user_b, "org-pending")
    await expose(db, pending_skill, pending_version, "org", approved=False)
    own_wf = await chat_workflow(db, user_a, handle="my-flow")
    b_wf = await chat_workflow(db, user_b, handle="b-flow")

    entries = await _entries(client, user_a)

    core = await list_chat_entry_agents(db, user_a)
    assert [e["agent_id"] for e in entries] == [str(agent.id) for agent, _ in core]
    by_id = {e["agent_id"]: e for e in entries}
    core_by_id = {str(agent.id): entry for agent, entry in core}
    assert by_id[str(own_wrapper.id)] == {
        "agent_id": str(own_wrapper.id),
        "kind": "skill",
        "slash_name": own_skill.slug,
        "source": "own",
        "display_name": core_by_id[str(own_wrapper.id)].display_name,
        "description": core_by_id[str(own_wrapper.id)].display_description,
    }
    assert by_id[str(sys_wrapper.id)]["source"] == "system"
    assert by_id[str(org_wrapper.id)]["source"] == "org"
    assert by_id[str(own_wf.id)] == {
        "agent_id": str(own_wf.id),
        "kind": "workflow",
        "slash_name": "my-flow",
        "source": "own",
        "display_name": own_wf.name,
        "description": "채팅 워크플로",
    }
    for hidden in (b_private, pending_wrapper, b_wf):
        assert str(hidden.id) not in by_id


async def test_admin_entries_have_no_other_users_private_items(
    client: httpx.AsyncClient, db, user_b, admin_user
):
    """admin에게도 타인 private 스킬·workflow가 없다(코어 판정에 admin 승격이 없다)."""
    _, _, b_private = await legacy_skill(db, user_b, "b-secret")
    b_wf = await chat_workflow(db, user_b, handle="b-flow")
    sys_wf = await chat_workflow(db, None, scope="system", handle="sys-flow")

    ids = {e["agent_id"] for e in await _entries(client, admin_user)}

    assert str(sys_wf.id) in ids
    assert str(b_private.id) not in ids
    assert str(b_wf.id) not in ids


async def test_entries_require_login(client: httpx.AsyncClient):
    assert (await client.get("/api/v1/chat/entries")).status_code == 401


# ---------- AC-2 이름 풀기 ----------


async def test_entry_by_name_one_runs_that_item(client: httpx.AsyncClient, db, user_a):
    """`name`이 한 항목에 맞으면 실행한다 — 질문 행과 chat run을 한 번에 만들고 202."""
    skill, version, wrapper = await legacy_skill(db, user_a, "brief-writer")
    nb = await notebook(db, user_a)
    s = await session(db, nb, user_a)

    res = await post_entry(client, user_a, s.id, name=skill.slug, content="이번 주 요약해 줘")

    assert res.status_code == 202, res.text
    body = res.json()
    run = body["run"]
    assert run["agent_id"] == str(wrapper.id)
    assert run["trigger"] == "chat"
    assert run["status"] == "queued"
    assert run["params"] == {"topic": "이번 주 요약해 줘"}
    assert run["skill_version_id"] == str(version.id)
    assert run["can_cancel"] is True
    message = body["user_message"]
    assert message["role"] == "user"
    assert message["content"] == "이번 주 요약해 줘"
    assert message["run_id"] is None
    assert message["entry_run"] == {
        "run_id": run["id"],
        "agent_id": str(wrapper.id),
        "display_name": skill.name,
        "slash_name": skill.slug,
        "status": "queued",
    }
    stored = await db.get(AgentRun, uuid.UUID(run["id"]))
    assert stored is not None
    assert stored.chat_session_id == s.id
    assert stored.prompt_message_id == uuid.UUID(message["id"])
    # 첫 메시지면 세션 제목을 질문으로 정한다(일반 메시지와 같다)
    await db.refresh(s)
    assert s.title == "이번 주 요약해 줘"


async def test_entry_by_name_none_is_404_without_reason(
    client: httpx.AsyncClient, db, user_a, user_b
):
    """없는 이름과 타인 private slug가 같은 404다 — 판정 사유를 싣지 않는다. 아무것도 만들지
    않는다.
    """
    _, _, _ = await legacy_skill(db, user_b, "b-secret")
    nb = await notebook(db, user_a)
    s = await session(db, nb, user_a)

    missing = await post_entry(client, user_a, s.id, name="no-such-entry")
    others = await post_entry(client, user_a, s.id, name="b-secret")

    assert missing.status_code == others.status_code == 404
    assert missing.json() == others.json() == {"detail": "Agent not found"}
    assert await _run_count(db) == 0
    assert await _message_count(db) == 0


async def test_entry_by_name_many_is_409_with_candidates(
    client: httpx.AsyncClient, db, user_a, user_b
):
    """이름이 여러 항목에 맞으면 실행하지 않고 409와 후보를 준다. 타인 private 같은 slug는 섞이지
    않는다.
    """
    skill, _, wrapper = await legacy_skill(db, user_a, "dup")
    b_skill, _, b_same = await legacy_skill(db, user_b, "dup")
    assert b_skill.slug == skill.slug  # 타인 private slug는 겹칠 수 있다(코어 `_unique_slug`)
    workflow = await chat_workflow(db, user_a, handle=skill.slug)
    nb = await notebook(db, user_a)
    s = await session(db, nb, user_a)

    res = await post_entry(client, user_a, s.id, name=skill.slug)

    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "chat_entry_ambiguous"
    assert {c["agent_id"] for c in detail["candidates"]} == {str(wrapper.id), str(workflow.id)}
    assert str(b_same.id) not in {c["agent_id"] for c in detail["candidates"]}
    assert await _run_count(db) == 0
    assert await _message_count(db) == 0


async def test_entry_body_needs_exactly_one_target(client: httpx.AsyncClient, db, user_a):
    skill, _, wrapper = await legacy_skill(db, user_a)
    s = await session(db, await notebook(db, user_a), user_a)

    both = await post_entry(client, user_a, s.id, name=skill.slug, agent_id=wrapper.id)
    neither = await post_entry(client, user_a, s.id)

    assert both.status_code == neither.status_code == 422


# ---------- AC-3 비인가 ----------


async def test_entry_without_notebook_permission_is_404(
    client: httpx.AsyncClient, db, user_a, user_b
):
    """본인 세션이라도 노트북을 읽을 수 없으면(공유 해제 등) 404다."""
    _, _, wrapper = await legacy_skill(db, user_b)
    nb = await notebook(db, user_a)
    stale = await session(db, nb, user_b)  # 공유가 풀린 노트북에 남은 user_b의 대화

    res = await post_entry(client, user_b, stale.id, agent_id=wrapper.id)

    assert res.status_code == 404
    assert res.json() == {"detail": "Notebook not found"}
    assert await _run_count(db) == 0


async def test_entry_into_other_users_session_is_404(
    client: httpx.AsyncClient, db, user_a, user_b
):
    _, _, wrapper = await legacy_skill(db, user_b)
    nb = await notebook(db, user_a)
    await share_with(db, nb.id, user_b, "write")
    await db.commit()
    a_session = await session(db, nb, user_a)

    res = await post_entry(client, user_b, a_session.id, agent_id=wrapper.id)

    assert res.status_code == 404
    assert res.json() == {"detail": "Session not found"}
    assert await _run_count(db) == 0


async def test_other_users_private_skill_is_hidden_by_id_and_name(
    client: httpx.AsyncClient, db, user_a, user_b
):
    _, _, b_wrapper = await legacy_skill(db, user_b, "b-secret")
    s = await session(db, await notebook(db, user_a), user_a)

    by_id = await post_entry(client, user_a, s.id, agent_id=b_wrapper.id)
    by_name = await post_entry(client, user_a, s.id, name="b-secret")
    missing_id = await post_entry(client, user_a, s.id, agent_id=uuid.uuid4())

    assert by_id.status_code == by_name.status_code == missing_id.status_code == 404
    assert by_id.json() == by_name.json() == missing_id.json() == {"detail": "Agent not found"}
    assert await _run_count(db) == 0


async def test_admin_cannot_run_other_users_private_skill(
    client: httpx.AsyncClient, db, user_b, admin_user
):
    """admin 승격 경계 — 노트북은 owner로 승격되지만 타인 private 스킬 진입은 404다."""
    _, _, b_wrapper = await legacy_skill(db, user_b, "b-secret")
    b_wf = await chat_workflow(db, user_b, handle="b-flow")
    s = await session(db, await notebook(db, admin_user), admin_user)

    for body in (
        {"agent_id": b_wrapper.id},
        {"name": "b-secret"},
        {"agent_id": b_wf.id},
        {"name": "b-flow"},
    ):
        res = await post_entry(client, admin_user, s.id, **body)
        assert res.status_code == 404, (body, res.text)
    assert await _run_count(db) == 0


async def test_read_shared_user_can_run_entry(client: httpx.AsyncClient, db, user_a, user_b):
    """결정 2 — 노트북 읽기 공유 사용자도 자기 대화에서 실행한다(수동 run 규칙과 같다)."""
    skill, _, _ = await legacy_skill(db, user_b, "b-brief")
    nb = await notebook(db, user_a)
    await share_with(db, nb.id, user_b, "read")
    await db.commit()
    b_session = await session(db, nb, user_b)

    res = await post_entry(client, user_b, b_session.id, name=skill.slug)

    assert res.status_code == 202, res.text
    assert res.json()["run"]["notebook_id"] == str(nb.id)


async def test_entry_requires_login(client: httpx.AsyncClient):
    res = await client.post(
        f"/api/v1/chat/sessions/{uuid.uuid4()}/entry-runs",
        json={"name": "x", "content": "q"},
    )
    assert res.status_code == 401


# ---------- AC-8 동시성 ----------


async def test_second_entry_in_same_session_is_409(client: httpx.AsyncClient, db, user_a):
    skill, _, _ = await legacy_skill(db, user_a)
    s = await session(db, await notebook(db, user_a), user_a)

    first = await post_entry(client, user_a, s.id, name=skill.slug, content="첫 질문")
    second = await post_entry(client, user_a, s.id, name=skill.slug, content="둘째 질문")

    assert first.status_code == 202, first.text
    assert second.status_code == 409
    assert second.json() == {"detail": str(ActiveChatRunExists())}
    assert await _run_count(db) == 1
    contents = (await db.execute(select(ChatMessage.content))).scalars().all()
    assert contents == ["첫 질문"]  # 충돌한 질문 행은 같은 트랜잭션에서 되돌려졌다


async def test_same_idempotency_key_returns_the_same_run(client: httpx.AsyncClient, db, user_a):
    skill, _, _ = await legacy_skill(db, user_a)
    s = await session(db, await notebook(db, user_a), user_a)

    first = await post_entry(client, user_a, s.id, name=skill.slug, idempotency_key="k-1")
    again = await post_entry(client, user_a, s.id, name=skill.slug, idempotency_key="k-1")

    assert first.status_code == again.status_code == 202, again.text
    assert again.json()["run"]["id"] == first.json()["run"]["id"]
    assert again.json()["user_message"]["id"] == first.json()["user_message"]["id"]
    assert await _run_count(db) == 1
    assert await _message_count(db) == 1


async def test_normal_message_is_409_while_entry_run_is_active(
    client: httpx.AsyncClient, db, user_a, llm_seed, boundary  # noqa: F811
):
    """D3 — 활성 진입 run이 있는 세션의 일반 메시지는 409, 끝난 뒤에는 그대로 받는다."""
    skill, _, _ = await legacy_skill(db, user_a)
    s = await session(db, await notebook(db, user_a), user_a)
    entry = await post_entry(client, user_a, s.id, name=skill.slug)
    assert entry.status_code == 202
    headers = await token(client, user_a)

    blocked = await client.post(
        f"/api/v1/chat/sessions/{s.id}/messages", json={"content": "끼어들기"}, headers=headers
    )
    assert blocked.status_code == 409
    assert blocked.json() == {"detail": str(ActiveChatRunExists())}
    assert await _message_count(db) == 1
    assert boundary.static_messages == []

    run = await db.get(AgentRun, uuid.UUID(entry.json()["run"]["id"]))
    assert run is not None
    run.status = "succeeded"
    await db.commit()
    allowed = await client.post(
        f"/api/v1/chat/sessions/{s.id}/messages", json={"content": "이제 질문"}, headers=headers
    )
    assert allowed.status_code == 200, allowed.text


# ---------- AC-6 이력 · AC-7 회귀 ----------


async def _entry_turn(db, nb, user, s, *, question: str, result: str, failed: bool):
    wf = await chat_workflow(db, user)
    run = await chat_run(db, wf, nb, user, prompt=question, chat_session=s)
    await write_entry_result(
        db,
        run_id=run.id,
        session_id=s.id,
        content=entry_failed_text(result) if failed else result,
        no_result=failed,
    )
    await db.commit()
    return run


async def _normal_turn(db, s, question: str, answer: str) -> None:
    db.add(ChatMessage(session_id=s.id, role="user", content=question))
    await db.flush()
    db.add(ChatMessage(session_id=s.id, role="assistant", content=answer))
    await db.commit()


def _sse(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def _joined(messages: list[dict]) -> str:
    return "\n".join(str(m.get("content", "")) for m in messages)


@pytest.mark.parametrize("autonomous", [False, True])
async def test_next_turn_history_has_entry_question_and_result_only(
    client: httpx.AsyncClient, db, user_a, llm_seed, boundary, autonomous  # noqa: F811
):
    """결정 3·D5 — 정적·자율 이력에 진입 턴은 「질문 + 결과」만 들어가고 실패·중지 턴은 질문까지
    빠진다.
    """
    nb = await notebook(db, user_a)
    s = await session(db, nb, user_a)
    await _entry_turn(db, nb, user_a, s, question="성공 질문 Q1", result="결과 R1", failed=False)
    await _entry_turn(db, nb, user_a, s, question="실패 질문 Q2", result="고정 문구", failed=True)
    await _normal_turn(db, s, "일반 질문 Q3", "일반 답변 A3")

    res = await client.post(
        f"/api/v1/chat/sessions/{s.id}/messages",
        json={"content": "다음 질문", "autonomous": autonomous},
        headers=await token(client, user_a),
    )

    assert res.status_code == 200, res.text
    assert _sse(res.text)[-1]["type"] == "done"
    sent = boundary.react_messages[-1] if autonomous else boundary.static_messages[-1]
    joined = _joined(sent)
    for kept in ("성공 질문 Q1", "결과 R1", "일반 질문 Q3", "일반 답변 A3"):
        assert kept in joined
    for dropped in ("실패 질문 Q2", entry_failed_text("고정 문구")):
        assert dropped not in joined


def test_history_rule_drops_unfinished_and_orphan_entry_turns():
    """결과 행이 없는 진입 질문(진행 중·결과 미기록)과 질문이 없는 결과 행은 빠진다. 표식은
    assistant만 본다.
    """
    q_done, q_open = uuid.uuid4(), uuid.uuid4()
    run_done, run_open, run_orphan = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    history = [
        ChatMessage(id=q_done, role="user", content="Q-done", token_usage={}),
        ChatMessage(role="assistant", content="R-done", token_usage={}, run_id=run_done),
        ChatMessage(id=q_open, role="user", content="Q-open", token_usage={}),
        ChatMessage(role="assistant", content="R-orphan", token_usage={}, run_id=run_orphan),
        ChatMessage(role="user", content="일반", token_usage={ENTRY_NO_RESULT_MARK: 1}),
    ]

    out = history_for_llm(history, {q_done: run_done, q_open: run_open})

    assert [m.content for m in out] == ["Q-done", "R-done", "일반"]


async def test_dollar_prefixed_and_plain_messages_stay_normal_chat(
    client: httpx.AsyncClient, db, user_a, llm_seed, boundary  # noqa: F811
):
    """AC-7 — `$`로 시작하는 입력과 일반 입력은 그대로 일반 메시지다(run 없음, 진입 필드 null)."""
    s = await session(db, await notebook(db, user_a), user_a)
    headers = await token(client, user_a)

    for content in ("$brief-writer 요약", "/brief-writer 요약"):
        res = await client.post(
            f"/api/v1/chat/sessions/{s.id}/messages", json={"content": content}, headers=headers
        )
        assert res.status_code == 200, res.text
        assert _sse(res.text)[-1]["type"] == "done"

    assert await _run_count(db) == 0
    listed = await client.get(f"/api/v1/chat/sessions/{s.id}/messages", headers=headers)
    assert [m["content"] for m in listed.json() if m["role"] == "user"] == [
        "$brief-writer 요약",
        "/brief-writer 요약",
    ]
    assert all(m["run_id"] is None and m["entry_run"] is None for m in listed.json())


async def test_regenerate_on_entry_turn_is_409(
    client: httpx.AsyncClient, db, user_a, llm_seed, boundary  # noqa: F811
):
    """진입 턴에는 「다시 생성」이 없다 — 마지막 쌍이 진입 턴이면 409이고 아무것도 바꾸지 않는다."""
    nb = await notebook(db, user_a)
    s = await session(db, nb, user_a)
    await _entry_turn(db, nb, user_a, s, question="진입 질문", result="진입 결과", failed=False)
    before = await _message_count(db)

    res = await client.post(
        f"/api/v1/chat/sessions/{s.id}/messages",
        json={"content": "진입 질문", "regenerate": True},
        headers=await token(client, user_a),
    )

    assert res.status_code == 409
    assert res.json() == {"detail": "실행 결과는 다시 생성할 수 없습니다"}
    assert await _message_count(db) == before
    assert boundary.static_messages == []


async def test_messages_carry_entry_fields(client: httpx.AsyncClient, db, user_a):
    """`ChatMessageOut` 추가형 필드 — 질문 행은 `entry_run`, 결과 행은 `run_id`, 그 밖은 null."""
    nb = await notebook(db, user_a)
    s = await session(db, nb, user_a)
    run = await _entry_turn(db, nb, user_a, s, question="질문", result="결과", failed=False)
    await _normal_turn(db, s, "일반", "답")

    res = await client.get(
        f"/api/v1/chat/sessions/{s.id}/messages", headers=await token(client, user_a)
    )

    rows = res.json()
    assert [(m["content"], m["run_id"]) for m in rows] == [
        ("질문", None),
        ("결과", str(run.id)),
        ("일반", None),
        ("답", None),
    ]
    assert rows[0]["entry_run"]["run_id"] == str(run.id)
    assert rows[0]["entry_run"]["status"] == "succeeded"
    assert rows[0]["entry_run"]["slash_name"] is None  # handle 없는 workflow
    assert all(m["entry_run"] is None for m in rows[1:])


async def test_messages_of_other_users_session_stay_hidden(
    client: httpx.AsyncClient, db, user_a, user_b
):
    s = await session(db, await notebook(db, user_a), user_a)
    res = await client.get(
        f"/api/v1/chat/sessions/{s.id}/messages", headers=bearer(await login(client, user_b.email))
    )
    assert res.status_code == 404


# ---------- 예외 매핑 ----------


async def test_chat_run_exceptions_map_to_409_and_422():
    probe = FastAPI()
    register_exception_handlers(probe)

    @probe.get("/active")
    async def active() -> None:
        raise ActiveChatRunExists()

    @probe.get("/link")
    async def link() -> None:
        raise ChatRunLinkInvalid("prompt message와 chat session이 일치하지 않습니다")

    transport = httpx.ASGITransport(app=probe)
    async with httpx.AsyncClient(transport=transport, base_url="http://probe") as c:
        active_res = await c.get("/active")
        link_res = await c.get("/link")

    assert active_res.status_code == 409
    assert active_res.json() == {"detail": "이 대화에서 이미 실행 중인 에이전트가 있습니다"}
    assert link_res.status_code == 422
    assert link_res.json() == {"detail": "prompt message와 chat session이 일치하지 않습니다"}
