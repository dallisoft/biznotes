"""노트북 인가(authorization) 순수 함수 계층 (R1-5, CR-R06).

권한 판정 로직을 DB 접근에서 분리한 순수 함수로 모은다. 목적:

1. **"실제 소유자"와 "admin-as-owner 승격"을 명시적으로 분리** (phase-22c HCI-2).
   기존 `get_permission`은 소유자와 admin 양쪽에 "owner"를 반환해 둘을 구분할 수
   없었다. 여기서는 실제 소유권(`is_actual_owner`)과 승격(`resolve_permission`의
   admin 분기)을 별도 함수/분기로 두어, 감사·향후 tenancy 필터가 "누가 진짜
   소유자인가"를 물을 수 있게 한다.
2. **라우터·워커·runtime 도구가 동일 규칙을 공유**. DB 로딩 래퍼
   (`services.notebooks.get_permission`)와 FastAPI 의존성은 이 순수 함수를 감싼다.

SaaS 전환 가드레일(§4): 권한 판정이 이 한 곳으로 모이면 향후 org_id/RLS 필터를
삽입할 단일 지점이 된다.
"""

from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException, status
from sqlalchemy import or_, true
from sqlalchemy.sql.elements import ColumnElement

from biznotes_core.models import Agent, AgentRun, Notebook
from biznotes_core.services.agents.policy import chat_output_reference_error

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from biznotes_core.models import User

Permission = Literal["owner", "write", "read"]


def is_actual_owner(owner_id, user_id) -> bool:
    """user가 노트북의 **실제** 소유자인지 — admin 승격을 포함하지 않는다."""
    return owner_id == user_id


def resolve_permission(
    *,
    owner_id,
    visibility: str,
    user_id,
    user_role: str,
    share_perms: set[str],
) -> Permission | None:
    """노트북 권한을 판정 — None은 접근 불가.

    판정 순서(기존 get_permission과 동일):
      1. 실제 소유자 → "owner"
      2. admin 승격 → "owner" (실제 소유자가 아니어도 admin은 소유자 권한으로 승격)
      3. 공유(share) 권한 write/read
      4. public 가시성 → "read"

    admin 분기(2)는 (1)과 명시적으로 분리돼 있다 — 감사/tenancy가 승격 여부를
    구분해야 할 때 `is_actual_owner`로 (1)만 따로 물을 수 있다.
    """
    if is_actual_owner(owner_id, user_id):
        return "owner"
    if user_role == "admin":
        # admin-as-owner 승격 — 실제 소유권과 구분되는 별도 경로(phase-22c HCI-2).
        return "owner"
    if "write" in share_perms:
        return "write"
    if "read" in share_perms:
        return "read"
    if visibility == "public":
        return "read"
    return None


# 정의 최상위 `audience`의 관리자 전용 값(#436). 저장 헬퍼(`agent_audience_error`)는 이것만 받는다
ADMIN_AUDIENCE = "admin"


def is_admin_only_agent(agent: "Agent") -> bool:
    """관리자 전용 system 정의인가(#436 D1).

    system scope이고 정의의 `audience`가 있고 null이 아니면 참이다. 값은 보지 않는다 — 오타·다른
    값도 닫힌 쪽(관리자 전용)으로 본다. user scope에 붙은 키는 무시한다(소유자·관리자는 원래
    통과한다). 정의가 없거나 dict가 아니면 키가 없는 것과 같다.
    """
    if getattr(agent, "scope", None) != "system":
        return False
    definition = getattr(agent, "definition", None)
    if not isinstance(definition, dict):
        return False
    return definition.get("audience") is not None


def agent_audience_allows(agent: "Agent", user: "User | None") -> bool:
    """`user`가 이 정의의 대상인가(#436). 관리자 전용이 아니면 모두 대상이다.

    `user` None은 사용자 없는 시스템 실행이라 관리자로 본다(D2, `_is_admin_audience`와 같다).
    목록·실행(`agent_base_policy`)·소유자 draft·채팅 진입·서브에이전트 가시성이 이 판정을 같이
    쓴다(D3). 거짓이면 소유자 예외보다 먼저 거부한다 — owner가 남은 관리자 전용 system 정의를
    강등된 소유자가 계속 쓰지 못한다(#436 리뷰).
    """
    if not is_admin_only_agent(agent):
        return True
    return user is None or user.role == "admin"


def _agent_audience_allows_run(agent: "Agent", user_id, user_role: str) -> bool:
    """`agent_audience_allows`를 ORM `User` 없이 쓴다 — 서브에이전트 가시성(run 사용자)용.

    `user_id` None은 사용자 없는 시스템 실행이다(D2).
    """
    if not is_admin_only_agent(agent):
        return True
    return user_id is None or user_role == "admin"


def system_agent_audience_condition(user: "User | None") -> ColumnElement[bool]:
    """`agent_audience_allows`의 SQL 조건 — 제품 목록 쿼리에 AND로 합성한다(#436).

    관리자·사용자 없는 실행은 늘 참이다. 비관리자에게는 system 정의 중 `audience`가 없거나 null인
    행만 남긴다. `->> 'audience' IS NULL`로 본다 — `!= 'admin'`은 NULL 비교라 키 없는 행을 빠뜨리고,
    다른 값을 통과시킨다. user scope 행은 키와 무관하게 참이다(Python 판정과 같다). 소유자·관리자
    분기와 OR로 묶지 말고 그 바깥에 AND로 건다 — `can_list_agent`처럼 소유자여도 거부한다.
    """
    if user is None or user.role == "admin":
        return true()
    return or_(Agent.scope != "system", Agent.definition["audience"].astext.is_(None))


def agent_base_policy(user: "User", agent: "Agent") -> bool:
    """에이전트 목록·실행 predicate가 공유하는 가시성 기반 정책.

    관리자 전용 system 정의(#436)는 비관리자에게 닫힌다 — 소유자여도 그렇다.
    """
    if not agent_audience_allows(agent, user):
        return False
    return agent.scope == "system" or agent.owner_id == user.id or user.role == "admin"


def can_list_agent(user: "User", agent: "Agent") -> bool:
    return (
        agent_base_policy(user, agent)
        and agent.enabled
        and agent.source_kind != "skill_package"
    )


def can_run_agent(user: "User", agent: "Agent") -> bool:
    return (
        agent_base_policy(user, agent)
        and agent.enabled
        and agent.source_kind != "skill_package"
    )


def can_run_owner_draft(user: "User", agent: "Agent") -> bool:
    """소유자의 workflow 실행(비활성 draft 포함). 관리자 전용 system 정의는 관리자만(#436)."""
    return (
        agent.kind == "workflow"
        and agent.owner_id == user.id
        and agent_audience_allows(agent, user)
    )


def is_chat_callable(agent: "Agent") -> bool:
    """저장된 정의에서 채팅 진입 capability를 매번 파생한다."""
    if agent.kind != "workflow" or not isinstance(agent.definition, dict):
        return False
    nodes = agent.definition.get("nodes")
    if not isinstance(nodes, list) or len(nodes) != 1:
        return False
    node = nodes[0]
    if not isinstance(node, dict) or node.get("type") != "react_agent":
        return False

    if "allowed_tools" not in node:
        return False
    allowed_tools = node.get("allowed_tools")
    if not isinstance(allowed_tools, list) or not allowed_tools:
        return False
    if not all(isinstance(tool, str) and tool.strip() for tool in allowed_tools):
        return False
    effective_tools = {tool.strip() for tool in allowed_tools}
    if not effective_tools or "call_agent" in effective_tools:
        return False

    if chat_output_reference_error(agent.definition) is not None:
        return False
    chat_output = agent.definition.get("chat_output")
    if chat_output is not None:
        return True
    # 위에서 정확히 하나인 react_agent 노드만 남았으므로 D-07 fallback이
    # 항상 성립한다. 조건 2를 완화하려면 이 분기도 함께 재설계해야 한다.
    return True


def is_private_chat_run(run: AgentRun) -> bool:
    return run.trigger == "chat"


async def assert_can_read_run(
    db: "AsyncSession", user: "User", run: AgentRun
) -> None:
    """단건 run 열람을 assertion 계약으로 강제한다. 실패는 존재 은닉 404다."""
    if is_private_chat_run(run):
        if run.user_id != user.id and user.role != "admin":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
        return

    notebook = await db.get(Notebook, run.notebook_id)
    if notebook is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")

    from biznotes_core.services.notebooks import get_permission

    if await get_permission(db, notebook, user) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")


def private_chat_run_exclusion(user: "User") -> ColumnElement[bool]:
    """목록 쿼리에 합성하는 타인 chat run 제외 조건."""
    return private_chat_run_condition(user.id, user.role)


def private_chat_run_condition(user_id, user_role: str) -> ColumnElement[bool]:
    """`private_chat_run_exclusion`의 판정을 ORM `User` 없이 쓴다 — 런타임 도구용(#133).

    `user_id`가 None이면 시스템 실행이다. 시스템 실행에는 채팅 run이 보이지 않는다.
    """
    if user_id is None:
        return AgentRun.trigger != "chat"
    if user_role == "admin":
        return true()
    return or_(AgentRun.trigger != "chat", AgentRun.user_id == user_id)
