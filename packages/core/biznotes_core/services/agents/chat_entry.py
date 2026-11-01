"""채팅 진입(`/` 팔레트 목록 선택) 판정 — workflow와 스킬 wrapper (#85, #410).

**판정은 순수 함수 하나가 한다.** `_decide_workflow`·`_decide_skill`이다.
단건(`resolve_chat_entry`), 일괄(`resolve_chat_entries`), 목록(`list_chat_entry_agents`),
관문(`assert_chat_entry_allowed`)은 행을 적재하는 방식만 다르고 모두 이 함수를 거친다. 그래서
목록과 단건이 어긋나지 않는다. 제품은 결과(`ChatEntry`)를 직렬화만 하고 `visibility`·
`published_version_id`·`params_schema`로 정책을 다시 계산하지 않는다.

- workflow: 현 제품 채팅 관문과 같다 — (system 또는 소유자) + 실행 가능 + `is_chat_callable`.
  admin 예외는 없다. `is_chat_callable`의 의미는 바꾸지 않는다.
- 스킬 wrapper(`source_kind == "skill_package"`, `kind`와 무관): enabled 스킬이고 **실행될
  version**(`resolve_run_version_id`, 소유자는 최신 freeze)이 범위별 공개 조건, 런타임 플래그,
  채팅 파라미터 규칙을 만족해야 한다. `stage_agent_run`과 같은 함수로 version을 풀어 판정과
  박제가 일치한다. 범위는 system → 소유자 → org 순으로 고른다(#410).
  - system: 공개 포인터가 있어야 한다. 소유자도 같다.
  - 소유자(private·org): 포인터를 요구하지 않는다. 직접 실행과 같은 최신 freeze다.
  - org 비소유자(관리자 포함): 포인터가 있고 **그 version에 approved `expose_*` 검토 요청**이
    있어야 한다(#410 D1). 모델 카탈로그(S-HCI-12)와 같은 기준이다.

**`run_skill`과의 기준 차이(의도적).** `run_skill`의 `skill_manifest_is_read_only` 가드를
적용하지 않는다. `run_skill`은 모델이 자율로 부르는 도구이고, 이 진입은 사람이 고르는 것이다.
이 가드의 부재는 버그가 아니라 결정이다(계획 D3 Fork 1). #410 D2로 본인·org 스킬에도 넓혔다 —
직접 실행(S-HCI-5)보다 권한이 넓어지지 않는다. 채팅 진입 run은 AgentRun 직접 실행이라 스크립트
백엔드가 켜져 있으면 승인된 `scripts` version의 `run_skill_script`가 노출되고, 호출은 항상 사람
승인 대기로 멈춘다(#86, `skills.sandbox._direct_run`). 채팅은 자동 승인 정책을 쓰지 않는다.

**슬래시 이름(#410 D3).** 허용 항목에는 `slash_name`(스킬은 slug, workflow는 `Agent.handle`)과
`source`(own·system·org)를 싣는다. `resolve_chat_entry_by_name`은 `/<이름>`을 목록과 같은
판정으로 풀고, 여럿이면 실행하지 않고 후보를 돌려준다.

**존재 은닉.** `exposure.can_view`가 거짓인 스킬(타인의 private·disabled)은 사유를
`NOT_VISIBLE`로 접고 표시 필드를 비운다. 관문의 404 메시지에는 이름·slug·version·공개 상태를
넣지 않는다(§13.2).
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, get_args

from fastapi import HTTPException, status

from biznotes_core.schemas.enums import ChatEntryReasonValue

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from biznotes_core.models import Agent, Skill, SkillVersion, User


class ChatEntryReason(StrEnum):
    OK = "ok"
    NOT_VISIBLE = "not_visible"  # 볼 수 없다(비가시 스킬의 세분 사유도 여기로 접는다)
    NOT_ENABLED = "not_enabled"  # workflow: 꺼져 있고 소유자 draft도 아니다
    WORKFLOW_SHAPE = "workflow_shape"  # workflow 정의가 채팅 진입 형태가 아니다
    SKILL_DISABLED = "skill_disabled"  # 스킬 또는 wrapper가 꺼져 있다
    # system 공개가 아니다 — #410부터 내지 않는다. 응답 계약(`ChatEntryReasonValue`)이라 남긴다
    SKILL_NOT_SYSTEM = "skill_not_system"
    SKILL_NOT_PUBLISHED = "skill_not_published"  # 공개 포인터가 없다 / 실행할 version이 없다
    # 실행될 version이 standard인데 플래그가 꺼져 있다
    SKILL_RUNTIME_DISABLED = "skill_runtime_disabled"
    SKILL_PARAMS_INCOMPATIBLE = "skill_params_incompatible"  # 채팅 메시지를 파라미터로 옮길 수 없다


# 응답 스키마용 Literal(`ChatEntryReasonValue`, #135)과 1:1이어야 한다. 값을 더하고 Literal을
# 잊으면 응답 직렬화가 500이 되므로 import 시점에 막는다(`schemas/enums.py`와 같은 방식).
if set(get_args(ChatEntryReasonValue)) != {reason.value for reason in ChatEntryReason}:
    raise RuntimeError("ChatEntryReasonValue가 ChatEntryReason과 어긋납니다")


@dataclass(frozen=True)
class ChatEntry:
    """채팅 진입 판정 결과.

    `skill_version_id`는 실행 규칙으로 푼 version이다(표시·보조용 — 실행 시점 정본은
    `stage_agent_run`). `display_*`는 wrapper가 아니라 실행 version의 frontmatter(없으면 Skill 행)
    에서 온다. wrapper의 `name`·`description`·`params_schema`는 최신 freeze 값이라 비소유자에게
    쓰지 않는다. 비가시 스킬과 workflow에서는 스킬 필드가 모두 None이다.

    `slash_name`·`source`(#410)는 표시 메타데이터라 **허용 항목에만** 채우고 동등 비교에 넣지
    않는다. `slash_name`은 스킬 slug, workflow `Agent.handle`(없으면 None)이다. `source`는 범위
    라벨이다 — 스킬은 system → 소유자(own) → org 순으로, workflow는 system 아니면 own이다.
    """

    allowed: bool
    reason: ChatEntryReason
    skill_version_id: uuid.UUID | None = None
    param_key: str | None = None
    display_name: str | None = None
    display_description: str | None = None
    slash_name: str | None = field(default=None, compare=False)
    source: Literal["own", "system", "org"] | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ChatEntryNameMatch:
    """`/<이름>` 풀기 결과(#410 D3). `many`면 이름만으로 실행하지 않고 제품이 고르게 한다.

    `matches`는 목록과 같은 판정을 통과한 허용 항목뿐이다(타인 private은 섞이지 않는다). 같은
    스킬의 wrapper는 하나로 센다. 순서는 own > system > org → 표시 이름 → agent id다.
    """

    status: Literal["none", "one", "many"]
    matches: list[tuple["Agent", ChatEntry]]


_NOT_VISIBLE = ChatEntry(allowed=False, reason=ChatEntryReason.NOT_VISIBLE)
# 이름 풀기 순서(#410 D3) — 모델 카탈로그 순위(`runtime._rank_of`)와 같다
_SOURCE_ORDER: dict[str | None, int] = {"own": 0, "system": 1, "org": 2}
# `Skill.slug` 길이 — 이보다 긴 이름은 어떤 slug·handle과도 같을 수 없다
_NAME_MAX = 160

# 채팅 메시지를 넣을 수 있는 legacy params_schema의 형태(재리뷰 N3). 허용 목록 밖의 키가 있으면
# `stage_agent_run`의 Draft 2020-12 검증에서 422가 날 수 있으므로 판정 단계에서 제외한다.
_ROOT_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "title",
        "description",
        "$schema",
        "$comment",
    }
)
_TARGET_KEYS = frozenset({"type", "title", "description", "default", "examples", "$comment"})
# 채팅 메시지가 항상 만족하는 제약(#114). 메시지는 비어 있지 않으므로(제품 요청 스키마) `minLength`
# 0·1은 허용한다. `maxLength`·`pattern`·`enum`처럼 메시지가 어길 수 있는 제약은 계속 거부한다.
_MESSAGE_SAFE_KEYS = frozenset({"minLength"})


# ---------- 순수 판정 ----------


def _is_plain_string(prop: Any) -> bool:
    if not isinstance(prop, dict) or prop.get("type") != "string":
        return False
    if not set(prop) <= _TARGET_KEYS | _MESSAGE_SAFE_KEYS:
        return False
    min_length = prop.get("minLength", 0)
    return type(min_length) is int and 0 <= min_length <= 1


def _rag_query_carries(manifest: dict[str, Any], key: str) -> bool:
    """legacy 스킬의 RAG 검색어에 채팅 메시지(`params.<key>`)가 들어가는지(#115).

    `build_nodes`가 `rag_search` 노드를 만드는 조건(`input.mode == "rag"`, 노트 접근 허용)에서만
    본다. 검색어가 `params`를 참조하는데 `{params.<key>}`·`{params}`가 없으면 메시지가 검색어에
    들어가지 않는다. 전부 빈 렌더는 params JSON으로 대체되고(`_exec_rag_search`), 일부만 빈
    템플릿("요약: ")은 그대로 검색된다. `params`를 참조하지 않는 고정 검색어는 작성자 의도로 본다.
    """
    from biznotes_core.services.agents.templating import template_paths
    from biznotes_core.services.skills.validator import SkillValidationError, resolve_input

    try:
        mode = resolve_input(manifest)["mode"]
    except SkillValidationError:
        return False  # 실행 시 build_nodes도 같은 매니페스트에서 실패한다
    permissions = manifest.get("permissions")
    note_access = permissions.get("rag", True) if isinstance(permissions, dict) else True
    if mode != "rag" or not note_access:
        return True
    query = manifest.get("rag_query", "{params.topic}")
    if not isinstance(query, str):
        return False
    refs = [p for p in template_paths(query) if p == "params" or p.startswith("params.")]
    return not refs or "params" in refs or f"params.{key}" in refs


def _chat_param_key(version: Any) -> str | None:
    """채팅 메시지를 넣을 파라미터 이름. 옮길 수 없으면 None(계획 D2 파라미터 규칙).

    standard version은 `request`다. legacy는 매니페스트 `params_schema`가 문자열 속성 하나로
    정해질 때만이다 — 필수가 그 하나이거나, 필수가 없고 조건을 만족하는 속성이 정확히 하나다.
    legacy RAG 스킬은 그 속성이 검색어에 들어가야 한다(`_rag_query_carries`, #115).
    """
    from biznotes_core.models.skill import SKILL_RUNTIME_STANDARD

    if getattr(version, "runtime_profile", None) == SKILL_RUNTIME_STANDARD:
        return "request"
    manifest = getattr(version, "manifest", None)
    if not isinstance(manifest, dict):
        return None
    schema = manifest.get("params_schema")
    if not isinstance(schema, dict) or not schema:
        return None
    if not set(schema) <= _ROOT_KEYS or schema.get("type", "object") != "object":
        return None
    properties = schema.get("properties")
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        return None
    if required:
        if len(required) != 1 or not isinstance(required[0], str):
            return None
        target = required[0]
        if not _is_plain_string(properties.get(target)):
            return None
    else:
        candidates = [name for name, prop in properties.items() if _is_plain_string(prop)]
        if len(candidates) != 1:
            return None
        target = candidates[0]
    return target if _rag_query_carries(manifest, target) else None


def _display(skill: "Skill", version: "SkillVersion | None") -> tuple[str, str]:
    """표시 이름·설명 — 실행 version의 frontmatter, 비면 Skill 행(`can_view` 범위에서 이미 노출)."""
    from biznotes_core.services.skills.runtime import frontmatter_label

    name, description = frontmatter_label(version) if version is not None else ("", "")
    return name or skill.name, description or skill.description or ""


def _decide_workflow(user: "User", agent: "Agent") -> ChatEntry:
    """workflow 경로 — 현 제품 채팅 관문과 같다. 처음 걸리는 조건의 사유를 낸다."""
    from biznotes_core.services.authz import (
        agent_audience_allows,
        can_run_agent,
        can_run_owner_draft,
        is_chat_callable,
    )

    # 관리자 전용 system 정의(#436)는 비관리자에게 존재를 숨긴다 — 소유자여도 그렇다. NOT_ENABLED로
    # 새지 않게 가장 먼저 본다
    if not agent_audience_allows(agent, user):
        return _NOT_VISIBLE
    if not (agent.scope == "system" or agent.owner_id == user.id):
        return _NOT_VISIBLE  # admin 예외 없음
    if not (can_run_agent(user, agent) or can_run_owner_draft(user, agent)):
        return ChatEntry(allowed=False, reason=ChatEntryReason.NOT_ENABLED)
    if not is_chat_callable(agent):
        return ChatEntry(allowed=False, reason=ChatEntryReason.WORKFLOW_SHAPE)
    return ChatEntry(
        allowed=True,
        reason=ChatEntryReason.OK,
        slash_name=agent.handle,
        source="system" if agent.scope == "system" else "own",
    )


def _skill_source(skill: "Skill", user: "User") -> Literal["own", "system", "org"]:
    """스킬의 범위 — system 먼저, 그다음 소유자, 그다음 org(`runtime._rank_of`와 같은 순서)."""
    from biznotes_core.services.skills import exposure

    if skill.visibility == "system":
        return "system"
    if exposure.is_owner(skill, user.id):
        return "own"
    return "org"


def _needs_org_approval(skill: "Skill", user: "User") -> bool:
    """판정에 노출 version 승인 조회가 필요한가 — enabled org 스킬의 비소유자(#410 D1)."""
    return skill.enabled and skill.visibility == "org" and _skill_source(skill, user) == "org"


def _decide_skill(
    user: "User",
    agent: "Agent",
    skill: "Skill | None",
    run_version: "SkillVersion | None",
    *,
    standard_enabled: bool,
    pinned: bool = False,
    org_approved: bool = False,
) -> ChatEntry:
    """스킬 wrapper 경로. 처음 걸리는 조건의 사유를 낸다.

    `run_version`은 호출측이 `resolve_run_version_id(skill, user.id)`로 적재한 version이다.
    `pinned`면 run에 이미 박제한 version이다 — 1–5단계는 현재 상태로, 런타임 플래그는 박제
    version으로 판정하고, version 일치(6단계 중 실행 규칙 대조)와 파라미터 규칙(8단계)은 생략한다.

    공개 조건(5단계)은 범위별이다(#410). system은 포인터, 소유자는 실행 version만, org 비소유자는
    포인터와 `org_approved`(판정 version — 박제면 박제 version — 에 approved `expose_*`가 있는가,
    적재 쪽이 조회한다)를 본다. 어느 쪽이든 아니면 `SKILL_NOT_PUBLISHED`다.
    """
    from biznotes_core.models.skill import SKILL_RUNTIME_STANDARD
    from biznotes_core.services.skills import exposure

    if skill is None or not exposure.can_view(skill, user.id):
        return _NOT_VISIBLE
    # 실행 규칙으로 푼 version(재검사는 박제 version)이고 이 스킬 소속일 때만 쓴다
    version = (
        run_version
        if run_version is not None
        and run_version.skill_id == skill.id
        and (pinned or run_version.id == exposure.resolve_run_version_id(skill, user.id))
        else None
    )
    name, description = _display(skill, version)

    def reject(reason: ChatEntryReason) -> ChatEntry:
        return ChatEntry(
            allowed=False, reason=reason, display_name=name, display_description=description
        )

    if not skill.enabled or not agent.enabled:
        return reject(ChatEntryReason.SKILL_DISABLED)
    source = _skill_source(skill, user)
    if version is None:
        return reject(ChatEntryReason.SKILL_NOT_PUBLISHED)
    if source != "own" and exposure.exposed_version_id(skill) is None:
        return reject(ChatEntryReason.SKILL_NOT_PUBLISHED)
    if source == "org" and not org_approved:
        return reject(ChatEntryReason.SKILL_NOT_PUBLISHED)
    if version.runtime_profile == SKILL_RUNTIME_STANDARD and not standard_enabled:
        return reject(ChatEntryReason.SKILL_RUNTIME_DISABLED)
    param_key = _chat_param_key(version)
    if param_key is None and not pinned:
        return reject(ChatEntryReason.SKILL_PARAMS_INCOMPATIBLE)
    return ChatEntry(
        allowed=True,
        reason=ChatEntryReason.OK,
        skill_version_id=version.id,
        param_key=param_key,
        display_name=name,
        display_description=description,
        slash_name=skill.slug,
        source=source,
    )


def _is_skill_wrapper(agent: "Agent") -> bool:
    return agent.source_kind == "skill_package"


def _standard_enabled() -> bool:
    from biznotes_core.services.skills.runtime import runtime_enabled

    return runtime_enabled()


# ---------- 적재 ----------


async def _resolve(
    db: "AsyncSession",
    user: "User",
    agent: "Agent",
    *,
    pinned_version_id: uuid.UUID | None = None,
) -> ChatEntry:
    if not _is_skill_wrapper(agent):
        return _decide_workflow(user, agent)

    from biznotes_core.models import Skill, SkillVersion
    from biznotes_core.services.skills import exposure

    wrapper_version = (
        await db.get(SkillVersion, agent.skill_version_id)
        if agent.skill_version_id is not None
        else None
    )
    skill = await db.get(Skill, wrapper_version.skill_id) if wrapper_version else None
    if skill is None:
        return _NOT_VISIBLE
    version_id = (
        pinned_version_id
        if pinned_version_id is not None
        else exposure.resolve_run_version_id(skill, user.id)
    )
    run_version = await db.get(SkillVersion, version_id) if version_id is not None else None
    # 승인은 판정 version 기준이다 — 박제 재검사면 박제 version이다(#410 D1)
    org_approved = (
        run_version is not None
        and _needs_org_approval(skill, user)
        and run_version.id in await exposure.approved_exposure_version_ids(db, [run_version.id])
    )
    return _decide_skill(
        user,
        agent,
        skill,
        run_version,
        standard_enabled=_standard_enabled(),
        pinned=pinned_version_id is not None,
        org_approved=org_approved,
    )


def _skill_rows_query(*conditions: Any) -> Any:
    """(wrapper Agent, Skill, current version, published version) 행 쿼리.

    실행 version은 Python에서 `resolve_run_version_id`로 고른다(CASE 조인보다 단순하다).

    wrapper → wrapper version → Skill은 내부 조인이다(빠지는 행은 판정 1단계에서도 거부된다).
    version 두 개는 LEFT JOIN이다(재리뷰 m5). 소속 스킬 대조는 `_decide_skill`이 한다.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import aliased

    from biznotes_core.models import Agent, Skill, SkillVersion

    wrapper_version = aliased(SkillVersion, name="wrapper_version")
    current = aliased(SkillVersion, name="current_version")
    published = aliased(SkillVersion, name="published_version")
    return (
        select(Agent, Skill, current, published)
        .join(wrapper_version, Agent.skill_version_id == wrapper_version.id)
        .join(Skill, wrapper_version.skill_id == Skill.id)
        .outerjoin(current, Skill.current_version_id == current.id)
        .outerjoin(published, Skill.published_version_id == published.id)
        .where(Agent.source_kind == "skill_package", *conditions)
    )


def _pick_run_version(
    user: "User", skill: "Skill", current: "SkillVersion | None", published: "SkillVersion | None"
) -> "SkillVersion | None":
    """`resolve_run_version_id`가 고른 version을 두 LEFT JOIN 결과에서 찾는다."""
    from biznotes_core.services.skills.exposure import resolve_run_version_id

    version_id = resolve_run_version_id(skill, user.id)
    if version_id is None:
        return None
    return next((v for v in (current, published) if v is not None and v.id == version_id), None)


async def _skill_rows(
    db: "AsyncSession",
    user: "User",
    *conditions: Any,
    given: dict[uuid.UUID, "Agent"] | None = None,
) -> list[tuple["Agent", "Skill", ChatEntry]]:
    """`given`이 있으면 행의 Agent 대신 호출자가 넘긴 Agent로 판정한다(단건 판정과 같은 입력).

    org 비소유자 행의 노출 version 승인은 쿼리 한 번으로 모아 조회한다(N+1 없음, #410 D1).
    """
    from biznotes_core.services.skills import exposure

    standard_enabled = _standard_enabled()
    loaded: list[tuple[Agent, Skill, SkillVersion | None]] = []
    for row_agent, skill, current, published in (
        await db.execute(_skill_rows_query(*conditions))
    ).all():
        agent = given.get(row_agent.id, row_agent) if given is not None else row_agent
        loaded.append((agent, skill, _pick_run_version(user, skill, current, published)))
    approved = await exposure.approved_exposure_version_ids(
        db,
        {v.id for _, skill, v in loaded if v is not None and _needs_org_approval(skill, user)},
    )
    return [
        (
            agent,
            skill,
            _decide_skill(
                user,
                agent,
                skill,
                run_version,
                standard_enabled=standard_enabled,
                org_approved=run_version is not None and run_version.id in approved,
            ),
        )
        for agent, skill, run_version in loaded
    ]


async def _skill_entries(
    db: "AsyncSession",
    user: "User",
    *conditions: Any,
    given: dict[uuid.UUID, "Agent"] | None = None,
) -> list[tuple["Agent", ChatEntry]]:
    return [
        (agent, entry) for agent, _, entry in await _skill_rows(db, user, *conditions, given=given)
    ]


async def _allowed_candidates(
    db: "AsyncSession", user: "User", *, name: str | None = None
) -> list[tuple["Agent", "Skill | None", ChatEntry]]:
    """목록 후보 중 허용된 것 — `(Agent, 스킬(workflow는 None), 판정)`. `name`이면 정확 일치만.

    후보 SQL은 필요조건만 담은 상위집합 prefilter이고 최종 판정은 `_decide_*`가 한다. 소유자
    draft(비활성 workflow)는 올리지 않는다(현 제품 목록과 같다). 스킬은 enabled이고 (system·org
    공개 포인터가 있거나) (본인 소유이고 freeze가 있다) — freeze 전 draft는 저절로 빠진다(#410).
    """
    from sqlalchemy import and_, or_, select

    from biznotes_core.models import Agent, Skill
    from biznotes_core.services.skills.exposure import EXPOSED_VISIBILITIES

    workflow_name = [] if name is None else [Agent.handle == name]
    skill_name = [] if name is None else [Skill.slug == name]
    workflows = (
        await db.execute(
            select(Agent).where(
                Agent.source_kind != "skill_package",
                Agent.kind == "workflow",
                Agent.enabled.is_(True),
                or_(Agent.scope == "system", Agent.owner_id == user.id),
                *workflow_name,
            )
        )
    ).scalars()
    candidates: list[tuple[Agent, Skill | None, ChatEntry]] = [
        (agent, None, _decide_workflow(user, agent)) for agent in workflows
    ]
    candidates += await _skill_rows(
        db,
        user,
        Skill.enabled.is_(True),
        or_(
            and_(
                Skill.visibility.in_(EXPOSED_VISIBILITIES),
                Skill.published_version_id.is_not(None),
            ),
            and_(Skill.owner_id == user.id, Skill.current_version_id.is_not(None)),
        ),
        *skill_name,
    )
    return [row for row in candidates if row[2].allowed]


# ---------- 공개 API ----------


async def resolve_chat_entry(db: "AsyncSession", user: "User", agent: "Agent") -> ChatEntry:
    """에이전트 하나의 채팅 진입 판정. 실행 인가(`assert_agent_execution_allowed`)는 하지 않는다."""
    return await _resolve(db, user, agent)


async def resolve_chat_entries(
    db: "AsyncSession", user: "User", agents: Sequence["Agent"]
) -> dict[uuid.UUID, ChatEntry]:
    """이미 가진 에이전트 목록에 판정을 붙인다(레지스트리의 "채팅 불가" 사유 등). 키는 `agent.id`다.

    스킬 wrapper는 쿼리 한 번으로 적재한다(N+1 없음). 조인에서 빠진 wrapper는 `NOT_VISIBLE`이다.
    """
    from biznotes_core.models import Agent

    out: dict[uuid.UUID, ChatEntry] = {}
    wrappers: dict[uuid.UUID, Agent] = {}
    for agent in agents:
        if _is_skill_wrapper(agent):
            out[agent.id] = _NOT_VISIBLE
            wrappers[agent.id] = agent
        else:
            out[agent.id] = _decide_workflow(user, agent)
    if wrappers:
        rows = await _skill_entries(db, user, Agent.id.in_(list(wrappers)), given=wrappers)
        for agent, entry in rows:
            out[agent.id] = entry
    return out


async def resolve_wrapper_chat_entries(
    db: "AsyncSession", user: "User", wrapper_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, ChatEntry]:
    """스킬 wrapper id로 판정한다 — Agent를 따로 적재하지 않고 쿼리 한 번이다(#135).

    스킬 응답(`SkillOut.chat_entry_reason`)이 쓴다. 스킬 wrapper 전용이다 — workflow는 판정하지
    않는다. 판정은 `resolve_chat_entries`와 같고, 조인에서 빠진 id(workflow·wrapper version이
    없는 행·없는 id)는 `NOT_VISIBLE`이다. 빈 목록이면 쿼리하지 않는다.
    """
    from biznotes_core.models import Agent

    if not wrapper_ids:
        return {}
    out: dict[uuid.UUID, ChatEntry] = dict.fromkeys(wrapper_ids, _NOT_VISIBLE)
    for agent, entry in await _skill_entries(db, user, Agent.id.in_(list(wrapper_ids))):
        out[agent.id] = entry
    return out


async def list_chat_entry_agents(
    db: "AsyncSession", user: "User"
) -> list[tuple["Agent", ChatEntry]]:
    """채팅 진입이 허용된 에이전트만 `(Agent, ChatEntry)`로 돌려준다.

    순서는 표시 이름(workflow는 `Agent.name`, 스킬은 `display_name`) 순이다. 후보 SQL은 필요조건만
    담은 상위집합 prefilter이고 최종 판정은 `_decide_*`가 한다. 소유자 draft(비활성 workflow)는
    목록에 올리지 않는다(현 제품 목록과 같다). 스킬은 system·본인·승인된 org 스킬이다(#410).
    """
    allowed = [(agent, entry) for agent, _, entry in await _allowed_candidates(db, user)]
    allowed.sort(key=lambda pair: (pair[1].display_name or pair[0].name, str(pair[0].id)))
    return allowed


async def resolve_chat_entry_by_name(
    db: "AsyncSession", user: "User", name: str
) -> ChatEntryNameMatch:
    """`/<이름>`을 채팅 진입 항목으로 푼다(#410 D3) — 스킬 slug·workflow handle 정확 일치.

    후보는 `list_chat_entry_agents`와 같은 판정을 통과한 허용 항목뿐이다. 같은 스킬의 wrapper가
    여럿이면 가장 작은 agent id(문자열) 하나만 남긴다. 둘 이상이면 `many`다 — 이름만으로 실행하지
    않는다. 빈 이름이나 slug 길이를 넘는 이름은 조회 없이 `none`이다.
    """
    if not name or len(name) > _NAME_MAX:
        return ChatEntryNameMatch(status="none", matches=[])
    picked: dict[tuple[str, uuid.UUID], tuple[Agent, ChatEntry]] = {}
    for agent, skill, entry in await _allowed_candidates(db, user, name=name):
        key = ("skill", skill.id) if skill is not None else ("agent", agent.id)
        held = picked.get(key)
        if held is None or str(agent.id) < str(held[0].id):
            picked[key] = (agent, entry)
    matches = sorted(
        picked.values(),
        key=lambda pair: (
            _SOURCE_ORDER[pair[1].source],  # 허용 항목에는 늘 있다
            pair[1].display_name or pair[0].name,
            str(pair[0].id),
        ),
    )
    if not matches:
        return ChatEntryNameMatch(status="none", matches=[])
    return ChatEntryNameMatch(status="one" if len(matches) == 1 else "many", matches=matches)


async def assert_chat_entry_allowed(
    db: "AsyncSession",
    user: "User",
    agent: "Agent",
    *,
    pinned_version_id: uuid.UUID | None = None,
) -> ChatEntry:
    """채팅 진입 관문 — 실행 인가 후 채팅 진입 판정. 불가면 404(존재 은닉, §13.2).

    `pinned_version_id`는 이미 version을 박제한 run을 실행 직전·승인 재개 전에 다시 검사할 때
    넘긴다(재리뷰 N1). 가시성·kill-switch·범위별 공개 조건은 현재 상태로, 런타임 플래그는 박제
    version으로 판정한다. 공개 version이 바뀌어도 박제된 run은 거부하지 않는다(과거 run 불변, §9.4).
    범위별로 system은 현재 포인터, 소유자는 enabled만, org 비소유자는 현재 포인터와 **박제 version의
    승인**을 본다(#410 D1) — 판정과 박제 사이에 포인터가 미승인 version으로 옮겨 가도 막힌다.
    workflow 경로에서는 무시한다.

    박제 version은 같은 스킬 소속인지(org 비소유자는 승인까지)만 확인하므로, system 스킬에서는
    공개된 적 없는 version도 통과한다. 그래서 `run.skill_version_id`(`stage_agent_run`이 실행
    규칙으로 박제한 값)만 넘긴다. 요청 본문 등 클라이언트가 준 id를 넘기지 않는다.
    """
    from biznotes_core.services.agents.policy import assert_agent_execution_allowed

    await assert_agent_execution_allowed(db, user, agent)
    entry = await _resolve(
        db,
        user,
        agent,
        pinned_version_id=pinned_version_id if _is_skill_wrapper(agent) else None,
    )
    if not entry.allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    return entry


def chat_entry_params(entry: ChatEntry, message: str) -> dict[str, Any]:
    """스킬 wrapper 채팅의 `stage_agent_run(params=…)` — 채팅 메시지를 `param_key`에 넣는다.

    최종 검증은 여전히 `stage_agent_run`이 한다. workflow 채팅은 기존 `agent_params`를 쓴다.
    """
    if not entry.allowed or entry.param_key is None:
        raise ValueError("채팅 메시지를 파라미터로 옮길 수 없는 진입입니다")
    return {entry.param_key: message}


__all__ = [
    "ChatEntry",
    "ChatEntryNameMatch",
    "ChatEntryReason",
    "assert_chat_entry_allowed",
    "chat_entry_params",
    "list_chat_entry_agents",
    "resolve_chat_entries",
    "resolve_chat_entry",
    "resolve_chat_entry_by_name",
    "resolve_wrapper_chat_entries",
]
