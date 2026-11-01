"""관리자 전용 system 정의(`audience: "admin"`, #436) — 판정·저장 헬퍼·채팅 진입·서브에이전트.

순수 판정과 `db.get`만 쓰는 단건 경로를 DB 없이 본다. 목록 SQL·사용자 자동화 API·무인 관문·
서브에이전트 실행 경로는 PG 테스트(`*_pg.py`)(`test_admin_audience_pg.py` 등)가 PostgreSQL로 본다.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from biznotes_core.services.agents.chat_entry import (
    ChatEntryReason,
    _decide_workflow,
    assert_chat_entry_allowed,
)
from biznotes_core.services.agents.nodes.composite import agent_visible_to, skill_agent_visible_to
from biznotes_core.services.agents.policy import (
    agent_audience_error,
    assert_agent_execution_allowed,
)
from biznotes_core.services.authz import (
    ADMIN_AUDIENCE,
    agent_audience_allows,
    agent_base_policy,
    can_list_agent,
    can_run_agent,
    can_run_owner_draft,
    is_admin_only_agent,
)

R = ChatEntryReason
USER = SimpleNamespace(id=uuid.uuid4(), role="user")
ADMIN = SimpleNamespace(id=uuid.uuid4(), role="admin")
_MISSING = object()
CALLABLE = {
    "nodes": [{"id": "a", "type": "react_agent", "allowed_tools": ["rag_search"]}],
    "edges": [],
}
# 저장 헬퍼는 거부하지만 런타임은 관리자 전용으로 보는 값(D1 닫힌 쪽)
OTHER_VALUES = ["", "user", "Admin", "admin ", True, False, 0, 1, {}, [], ["admin"]]


def agent(audience: Any = "admin", **overrides: Any) -> SimpleNamespace:
    definition: Any = dict(CALLABLE)
    if audience is not _MISSING:
        definition["audience"] = audience
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "name": "종목 리포트 수집",
        "kind": "workflow",
        "source_kind": "legacy",
        "scope": "system",
        "owner_id": None,
        "enabled": True,
        "definition": definition,
        "skill_version_id": None,
        "handle": "stock-report",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class FakeDB:
    """`assert_chat_entry_allowed`의 workflow 경로는 DB를 읽지 않는다."""

    async def get(self, _model: Any, _key: Any) -> Any:
        return None


# ---------- 판정 (D1) ----------


def test_admin_audience_value() -> None:
    assert ADMIN_AUDIENCE == "admin"


def test_system_definition_with_admin_is_admin_only() -> None:
    assert is_admin_only_agent(agent()) is True


@pytest.mark.parametrize("value", OTHER_VALUES, ids=repr)
def test_any_other_value_is_admin_only_at_runtime(value: Any) -> None:
    """오타·다른 값도 닫힌 쪽(관리자 전용)으로 본다."""
    assert is_admin_only_agent(agent(value)) is True
    assert agent_audience_allows(agent(value), USER) is False


@pytest.mark.parametrize(
    "target",
    [
        agent(_MISSING),
        agent(None),
        agent("admin", scope="user", owner_id=USER.id),
        agent("admin", definition=None),
        agent("admin", definition=["audience"]),
        agent("admin", definition="admin"),
        SimpleNamespace(scope="system", enabled=True, owner_id=None),  # definition 없는 대역
    ],
    ids=["no-key", "null", "user-scope", "no-definition", "list", "string", "no-attr"],
)
def test_not_admin_only(target: Any) -> None:
    assert is_admin_only_agent(target) is False
    assert agent_audience_allows(target, USER) is True


def test_audience_allows_admins_and_runs_without_a_user() -> None:
    admin_only = agent()
    assert agent_audience_allows(admin_only, USER) is False
    assert agent_audience_allows(admin_only, ADMIN) is True
    assert agent_audience_allows(admin_only, None) is True  # 사용자 없는 실행(D2)


# ---------- 목록·실행 인가 ----------


def test_list_and_run_are_denied_to_non_admins_only() -> None:
    admin_only = agent()
    for check in (agent_base_policy, can_list_agent, can_run_agent):
        assert check(USER, admin_only) is False, check.__name__
        assert check(ADMIN, admin_only) is True, check.__name__


def test_list_and_run_are_unchanged_without_the_key() -> None:
    """회귀 — 키 없는 system 정의, user scope에 붙은 키는 지금과 같다."""
    plain = agent(_MISSING)
    mine = agent("admin", scope="user", owner_id=USER.id)
    for check in (agent_base_policy, can_list_agent, can_run_agent):
        assert check(USER, plain) is True
        assert check(USER, mine) is True
        assert check(SimpleNamespace(id=uuid.uuid4(), role="user"), mine) is False


async def test_execution_gate_is_404_for_non_admins() -> None:
    with pytest.raises(HTTPException) as exc:
        await assert_agent_execution_allowed(None, USER, agent())  # type: ignore[arg-type]
    assert (exc.value.status_code, exc.value.detail) == (404, "Agent not found")
    await assert_agent_execution_allowed(None, ADMIN, agent())  # type: ignore[arg-type]
    await assert_agent_execution_allowed(None, USER, agent(_MISSING))  # type: ignore[arg-type]


# ---------- 소유자 예외보다 먼저 (#436 리뷰 High) ----------


@pytest.mark.parametrize("enabled", [True, False], ids=["enabled", "draft"])
async def test_owner_exception_does_not_bypass_admin_only(enabled: bool) -> None:
    """비관리자가 관리자 전용 system 정의의 owner여도(강등 등) 모든 경로가 닫힌다.

    목록·실행·비활성 draft·채팅 진입·서브에이전트 가시성을 본다.
    """
    owned = agent(owner_id=USER.id, enabled=enabled)
    for check in (agent_base_policy, can_list_agent, can_run_agent, can_run_owner_draft):
        assert check(USER, owned) is False, check.__name__
    with pytest.raises(HTTPException) as exc:
        await assert_agent_execution_allowed(None, USER, owned)  # type: ignore[arg-type]
    assert (exc.value.status_code, exc.value.detail) == (404, "Agent not found")
    assert _decide_workflow(USER, owned).reason == R.NOT_VISIBLE
    with pytest.raises(HTTPException):
        await assert_chat_entry_allowed(FakeDB(), USER, owned)  # type: ignore[arg-type]
    assert agent_visible_to(owned, USER.id, "user") is False
    assert await skill_agent_visible_to(None, owned, USER.id, "user") is False  # type: ignore[arg-type]


@pytest.mark.parametrize("enabled", [True, False], ids=["enabled", "draft"])
async def test_admin_owner_keeps_owner_access(enabled: bool) -> None:
    owned = agent(owner_id=ADMIN.id, enabled=enabled)
    assert can_run_owner_draft(ADMIN, owned) is True
    assert agent_base_policy(ADMIN, owned) is True
    await assert_agent_execution_allowed(None, ADMIN, owned)  # type: ignore[arg-type]
    assert _decide_workflow(ADMIN, owned).allowed
    assert agent_visible_to(owned, ADMIN.id, "admin") is enabled


def test_owner_draft_is_unchanged_for_user_scope_keys() -> None:
    draft = agent("admin", scope="user", owner_id=USER.id, enabled=False)
    assert can_run_owner_draft(USER, draft) is True
    assert _decide_workflow(USER, draft).allowed


# ---------- 채팅 진입 (D3) ----------


@pytest.mark.parametrize(
    "target",
    [
        agent(),
        agent("user"),
        agent(enabled=False),  # 꺼져 있어도 NOT_ENABLED가 아니라 NOT_VISIBLE이다(존재 은닉)
        agent(definition={"audience": "admin", "nodes": []}),  # 형태보다 먼저
    ],
    ids=["admin", "other-value", "disabled", "bad-shape"],
)
def test_chat_entry_hides_admin_only_workflows_from_non_admins(target: Any) -> None:
    assert _decide_workflow(USER, target).reason == R.NOT_VISIBLE


def test_chat_entry_is_unchanged_for_admins_and_without_the_key() -> None:
    entry = _decide_workflow(ADMIN, agent())
    assert (entry.allowed, entry.slash_name, entry.source) == (True, "stock-report", "system")
    assert _decide_workflow(USER, agent(_MISSING)).allowed
    assert _decide_workflow(USER, agent("admin", scope="user", owner_id=USER.id)).allowed


async def test_chat_entry_gate_is_the_same_404() -> None:
    with pytest.raises(HTTPException) as exc:
        await assert_chat_entry_allowed(FakeDB(), USER, agent())  # type: ignore[arg-type]
    assert (exc.value.status_code, exc.value.detail, exc.value.headers) == (
        404,
        "Agent not found",
        None,
    )
    assert (await assert_chat_entry_allowed(FakeDB(), ADMIN, agent())).allowed  # type: ignore[arg-type]


# ---------- 서브에이전트 가시성 (D3) ----------


async def test_subagent_visibility_follows_the_run_user() -> None:
    admin_only = agent()
    for user_id, role, visible in (
        (USER.id, "user", False),
        (ADMIN.id, "admin", True),
        (None, "user", True),  # 사용자 없는 시스템 실행은 관리자로 본다(D2)
    ):
        assert agent_visible_to(admin_only, user_id, role) is visible, role
        assert await skill_agent_visible_to(None, admin_only, user_id, role) is visible  # type: ignore[arg-type]


async def test_subagent_visibility_is_unchanged_without_the_key() -> None:
    for target in (agent(_MISSING), SimpleNamespace(scope="system", enabled=True, owner_id=None)):
        assert agent_visible_to(target, USER.id, "user") is True
        assert await skill_agent_visible_to(None, target, USER.id, "user") is True  # type: ignore[arg-type]
    mine = agent("admin", scope="user", owner_id=USER.id)
    assert agent_visible_to(mine, USER.id, "user") is True
    assert agent_visible_to(mine, uuid.uuid4(), "user") is False
    assert agent_visible_to(agent(enabled=False), ADMIN.id, "admin") is False


# ---------- 저장 헬퍼 (D1) ----------


@pytest.mark.parametrize(
    "definition",
    [{"audience": "admin"}, {}, {"audience": None}, {**CALLABLE, "audience": "admin"}, None, []],
    ids=["admin", "no-key", "null", "workflow", "none", "list"],
)
def test_audience_error_accepts_admin_or_nothing(definition: Any) -> None:
    assert agent_audience_error(definition) is None


@pytest.mark.parametrize("value", OTHER_VALUES, ids=repr)
def test_audience_error_rejects_other_values(value: Any) -> None:
    error = agent_audience_error({"audience": value})
    assert isinstance(error, str) and "admin" in error
    # 입력값을 되돌려주지 않는다 — 값과 무관하게 같은 문구다
    assert error == agent_audience_error({"audience": "x"})
