# 신규(#436). 필요: PostgreSQL·Valkey(PG 테스트).
"""관리자 전용 system 정의(`audience: "admin"`) — 실제 행으로 목록 SQL·채팅 진입·서브에이전트 실행.

판정 규칙과 저장 헬퍼는 `tests/test_agent_audience.py`가 DB 없이 고정한다. 여기서는 다음을 본다.

1. 공개 SQL 조건(`system_agent_audience_condition`)이 Python 판정과 같은 행을 고른다.
2. 채팅 진입 목록·일괄·단건·이름 풀기·관문이 비관리자에게 정의를 숨긴다.
3. 서브에이전트 경로(agent_call·sub_workflow·supervisor·error_handler·loop·react `call_agent`)가
   run 사용자 기준으로 막고, 관리자·사용자 없는 실행은 지금처럼 부른다.

`create_all` 기반이라 Alembic 마이그레이션 검증이 아니다.
"""

import json
import uuid
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import or_, select, true
from structlog.testing import capture_logs

from biznotes_core.models import Agent, User
from biznotes_core.services.agents.chat_entry import (
    ChatEntryReason,
    assert_chat_entry_allowed,
    list_chat_entry_agents,
    resolve_chat_entries,
    resolve_chat_entry,
    resolve_chat_entry_by_name,
)
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError
from biznotes_core.services.agents.nodes import composite, retrieval
from biznotes_core.services.agents.runtime import execute_agent
from biznotes_core.services.authz import (
    agent_audience_allows,
    can_list_agent,
    system_agent_audience_condition,
)
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult

R = ChatEntryReason
CALLABLE = {
    "nodes": [{"id": "a", "type": "react_agent", "allowed_tools": ["rag_search"]}],
    "edges": [],
}
RESULT = "관리자 결과"
TARGET = {"nodes": [{"id": "c", "type": "constant", "value": {"text": RESULT}}], "edges": []}
DENIED = "호출 권한이 없습니다"


async def _agent(
    db: Any,
    definition: Any,
    *,
    owner: User | None = None,
    scope: str | None = None,
    enabled: bool = True,
    handle: str | None = None,
    source_kind: str = "legacy",
) -> Agent:
    """`scope`를 생략하면 owner가 있으면 user, 없으면 system이다."""
    agent = Agent(
        name=f"aud-{uuid.uuid4().hex[:6]}",
        handle=handle,
        kind="workflow",
        definition=definition,
        params_schema={},
        scope=scope or ("user" if owner else "system"),
        owner_id=owner.id if owner else None,
        enabled=enabled,
        source_kind=source_kind,
    )
    db.add(agent)
    await db.commit()  # 분기 세션·관문 세션은 커밋된 행만 본다
    await db.refresh(agent)
    return agent


# ---------- 1. SQL 조건 = Python 판정 ----------


async def _parity_rows(db: Any, user_a: User, admin_user: User) -> list[Agent]:
    values: list[Any] = [
        {},
        {"audience": "admin"},
        {"audience": None},
        {"audience": ""},
        {"audience": "user"},
        {"audience": True},
        {"audience": False},
        {"audience": 0},
        {"audience": {}},
        {"audience": []},
        {**CALLABLE, "audience": "admin"},
        [],  # JSONB 배열·문자열 정의 — 키가 없다
        "admin",
    ]
    rows = [await _agent(db, value) for value in values]
    rows.append(await _agent(db, {"audience": "admin"}, owner=user_a))  # user scope 키는 무시
    rows.append(await _agent(db, {}, owner=user_a))
    rows.append(await _agent(db, {"audience": "admin"}, enabled=False))
    # owner가 남은 관리자 전용 system 정의 — 소유자여도 비관리자에게는 닫힌다(#436 리뷰)
    for owner in (user_a, admin_user):
        rows.append(await _agent(db, {"audience": "admin"}, owner=owner, scope="system"))
    rows.append(await _agent(db, {}, owner=user_a, scope="system"))
    return rows


async def test_sql_condition_matches_python(
    db: Any, user_a: User, user_b: User, admin_user: User
) -> None:
    rows = await _parity_rows(db, user_a, admin_user)
    ids = [row.id for row in rows]
    for user in (user_a, user_b, admin_user, None):
        selected = set(
            (
                await db.scalars(
                    select(Agent.id).where(
                        Agent.id.in_(ids), system_agent_audience_condition(user)
                    )
                )
            ).all()
        )
        expected = {row.id for row in rows if agent_audience_allows(row, user)}
        assert selected == expected, user.name if user else None
    # 비관리자에게는 관리자 전용 system 행이 실제로 빠진다(조건이 늘 참이 아니다)
    hidden = {row.id for row in rows if not agent_audience_allows(row, user_a)}
    assert len(hidden) == 12


async def test_list_sql_built_with_the_condition_matches_can_list_agent(
    db: Any, user_a: User, user_b: User, admin_user: User
) -> None:
    """제품 목록 SQL 모양 — 조건 ∧ (system ∨ 소유자 ∨ 관리자) + enabled + 스킬 wrapper 제외.

    조건은 소유자 분기보다 앞에 AND로 건다 — owner가 남은 관리자 전용 system 정의도 빠진다.
    """
    rows = await _parity_rows(db, user_a, admin_user)
    rows.append(await _agent(db, {}, source_kind="skill_package"))
    ids = [row.id for row in rows]
    for user in (user_a, user_b, admin_user):
        listed = set(
            (
                await db.scalars(
                    select(Agent.id).where(
                        Agent.id.in_(ids),
                        Agent.enabled.is_(True),
                        Agent.source_kind != "skill_package",
                        system_agent_audience_condition(user),
                        or_(
                            Agent.scope == "system",
                            Agent.owner_id == user.id,
                            true() if user.role == "admin" else Agent.id.is_(None),
                        ),
                    )
                )
            ).all()
        )
        assert listed == {row.id for row in rows if can_list_agent(user, row)}, user.name


# ---------- 2. 채팅 진입 ----------


@pytest.fixture
async def chat(db: Any) -> dict[str, Agent]:
    return {
        "admin_only": await _agent(db, {**CALLABLE, "audience": "admin"}, handle="stock-report"),
        "typo": await _agent(db, {**CALLABLE, "audience": "Admin"}, handle="typo-report"),
        "plain": await _agent(db, CALLABLE, handle="plain-report"),
    }


async def test_chat_list_batch_and_single_hide_admin_only_workflows(
    db: Any, chat: dict[str, Agent], user_a: User, admin_user: User
) -> None:
    ids = {key: agent.id for key, agent in chat.items()}
    listed = {agent.id for agent, _ in await list_chat_entry_agents(db, user_a)}
    assert listed == {ids["plain"]}
    admin_listed = {agent.id for agent, _ in await list_chat_entry_agents(db, admin_user)}
    assert admin_listed == set(ids.values())

    agents = list(chat.values())
    reasons: dict[str, dict[uuid.UUID, R]] = {}
    for user in (user_a, admin_user):
        batch = await resolve_chat_entries(db, user, agents)
        single = {agent.id: await resolve_chat_entry(db, user, agent) for agent in agents}
        assert batch == single, user.name
        reasons[user.role] = {agent_id: entry.reason for agent_id, entry in batch.items()}
    assert reasons["user"] == {
        ids["admin_only"]: R.NOT_VISIBLE,
        ids["typo"]: R.NOT_VISIBLE,  # 다른 값도 닫힌 쪽이다
        ids["plain"]: R.OK,
    }
    assert reasons["admin"] == dict.fromkeys(ids.values(), R.OK)


async def test_chat_name_and_gate_hide_admin_only_workflows(
    db: Any, chat: dict[str, Agent], user_a: User, admin_user: User
) -> None:
    hidden = await resolve_chat_entry_by_name(db, user_a, "stock-report")
    assert hidden == await resolve_chat_entry_by_name(db, user_a, "no-such-name")
    shown = await resolve_chat_entry_by_name(db, admin_user, "stock-report")
    assert (shown.status, [a.id for a, _ in shown.matches]) == ("one", [chat["admin_only"].id])

    for key in ("admin_only", "typo"):
        with pytest.raises(HTTPException) as exc:
            await assert_chat_entry_allowed(db, user_a, chat[key])
        assert (exc.value.status_code, exc.value.detail) == (404, "Agent not found")
        assert (await assert_chat_entry_allowed(db, admin_user, chat[key])).allowed
    assert (await assert_chat_entry_allowed(db, user_a, chat["plain"])).allowed


async def test_chat_owner_of_admin_only_system_workflow_is_hidden_unless_admin(
    db: Any, user_a: User, admin_user: User
) -> None:
    """#436 리뷰 — owner가 남은 관리자 전용 system 정의(활성·비활성 draft)도 비관리자에게 숨긴다."""
    definition = {**CALLABLE, "audience": "admin"}
    owned = await _agent(db, definition, owner=user_a, scope="system")
    draft = await _agent(db, definition, owner=user_a, scope="system", enabled=False)
    admin_draft = await _agent(db, definition, owner=admin_user, scope="system", enabled=False)

    assert await list_chat_entry_agents(db, user_a) == []
    for target in (owned, draft):
        assert (await resolve_chat_entry(db, user_a, target)).reason == R.NOT_VISIBLE
        with pytest.raises(HTTPException) as exc:
            await assert_chat_entry_allowed(db, user_a, target)
        assert (exc.value.status_code, exc.value.detail) == (404, "Agent not found")
    assert [a.id for a, _ in await list_chat_entry_agents(db, admin_user)] == [owned.id]
    assert (await assert_chat_entry_allowed(db, admin_user, owned)).allowed
    assert (await assert_chat_entry_allowed(db, admin_user, admin_draft)).allowed  # 소유자 draft


# ---------- 3. 서브에이전트 ----------


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """대상 디스패치를 기록하고 실제 실행으로 넘긴다 — 관문을 지났는지 본다."""
    seen: list[uuid.UUID] = []
    real = composite.dispatch_agent

    async def spy(ctx: RunContext, agent: Any, **kw: Any) -> str:
        seen.append(agent.id)
        return await real(ctx, agent, **kw)

    for module in (composite, retrieval):
        monkeypatch.setattr(module, "dispatch_agent", spy)
    return seen


def _parent(path: str, target: Agent) -> dict[str, Any]:
    t = str(target.id)
    node: dict[str, Any] = {
        "agent_call": {"type": "agent_call", "agent_id": t},
        "sub_workflow": {"type": "sub_workflow", "workflow_id": t, "output_key": "out"},
        "supervisor": {"type": "supervisor", "workers": [{"agent_id": t}], "parallel": False},
        "error_handler": {"type": "error_handler", "protected_agent_id": t},
        "loop": {"type": "loop", "loop_body_agent_id": t, "items": [1]},
    }[path]
    return {"nodes": [{"id": "n", **node}], "edges": []}


PATHS = ["agent_call", "sub_workflow", "supervisor", "error_handler", "loop"]


def _ctx(db: Any, user: User | None, role: str = "user") -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=user.id if user else None,
        user_role=role,
    )


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("owned", [False, True], ids=["no-owner", "run-user-owns"])
async def test_subagent_paths_hide_admin_only_targets_from_non_admins(
    db: Any, user_a: User, dispatched: list[uuid.UUID], path: str, owned: bool
) -> None:
    """owner가 run 사용자여도(#436 리뷰) 관리자 전용 system 정의는 보이지 않는다."""
    owner = user_a if owned else None
    target = await _agent(db, {**TARGET, "audience": "admin"}, owner=owner, scope="system")
    with pytest.raises(AgentRuntimeError, match=DENIED):
        await execute_agent(_ctx(db, user_a), "workflow", _parent(path, target))
    assert dispatched == []


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("who", ["admin", "system-run"])
async def test_subagent_paths_run_admin_only_targets_for_admins(
    db: Any, admin_user: User, dispatched: list[uuid.UUID], path: str, who: str
) -> None:
    target = await _agent(db, {**TARGET, "audience": "admin"})
    ctx = _ctx(db, admin_user, "admin") if who == "admin" else _ctx(db, None)
    await execute_agent(ctx, "workflow", _parent(path, target))
    assert dispatched == [target.id]


@pytest.mark.parametrize("path", PATHS)
async def test_subagent_paths_are_unchanged_without_the_key(
    db: Any, user_a: User, dispatched: list[uuid.UUID], path: str
) -> None:
    """회귀 — 키 없는 system 정의, user scope에 붙은 키(소유자 실행)는 지금처럼 부른다."""
    plain = await _agent(db, TARGET)
    mine = await _agent(db, {**TARGET, "audience": "admin"}, owner=user_a)
    for target in (plain, mine):
        await execute_agent(_ctx(db, user_a), "workflow", _parent(path, target))
    assert dispatched == [plain.id, mine.id]


def _turn(name: str | None, args: dict[str, Any] | None = None) -> ToolTurnResult:
    if name is None:
        return ToolTurnResult(
            content="끝",
            tool_calls=[],
            assistant_message={"role": "assistant", "content": "끝"},
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            model=None,
        )
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "c",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }
    return ToolTurnResult(
        content="",
        tool_calls=[ToolCall(id="c", name=name, arguments=args or {})],
        assistant_message=message,
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        model=None,
    )


@pytest.mark.parametrize(("role", "allowed"), [("user", False), ("admin", True)])
async def test_react_call_agent_follows_the_run_user(
    db: Any,
    user_a: User,
    admin_user: User,
    mock_llm: None,
    monkeypatch: pytest.MonkeyPatch,
    dispatched: list[uuid.UUID],
    role: str,
    allowed: bool,
) -> None:
    target = await _agent(db, {**TARGET, "audience": "admin"})
    seen: list[list[dict[str, Any]]] = []

    async def fake(db: Any, model_id: Any, messages: Any, tools: Any, **kwargs: Any) -> Any:
        seen.append(messages)
        return _turn("call_agent", {"agent_id": str(target.id)}) if len(seen) == 1 else _turn(None)

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake)
    definition = {
        "nodes": [
            {
                "id": "r",
                "type": "react_agent",
                "goal": "하위 에이전트 호출",
                "allowed_tools": ["call_agent"],
                "auto_approve_tools": {"call_agent": {"agent_ids": [str(target.id)]}},
            }
        ],
        "edges": [],
    }
    user = admin_user if role == "admin" else user_a
    with capture_logs() as logs:
        await execute_agent(_ctx(db, user, role), "workflow", definition)

    tool_messages = [m["content"] for m in seen[-1] if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    failures = [e for e in logs if e.get("event") == "도구 실행 실패"]
    if allowed:
        assert dispatched == [target.id]
        assert RESULT in tool_messages[0] and failures == []
    else:
        assert dispatched == []
        assert RESULT not in tool_messages[0]
        assert [e["tool"] for e in failures] == ["call_agent"]
        assert DENIED in failures[0]["error"]
