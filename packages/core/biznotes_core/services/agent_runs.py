"""에이전트 실행 생성 공용 헬퍼 — 수동/스케줄/웹훅 트리거가 공유.

params 스키마 검증 → 사용량 한도 검사 → AgentRun 생성 → Celery 큐잉 →
사용량 기록을 한 곳에 모은다. HTTP 의존성 없이 도메인 예외만 던진다.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import ColumnElement, any_, func, literal, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.core.uow import (
    UnitOfWork,
    publishing_unit_of_work,
    unique_violation_constraint_name,
)
from biznotes_core.models import (
    Agent,
    AgentRun,
    AgentRunApproval,
    ChatMessage,
    ChatSession,
    LLMModel,
    LLMProvider,
    Skill,
    SkillVersion,
)
from biznotes_core.services.audit import record
from biznotes_core.services.usage_guard import UsageLimitExceeded, check_usage, record_usage
from biznotes_core.workers.celery_app import celery_app
from biznotes_core.workers.task_names import RUN_AGENT

#: 진행 중인 run 상태 — 동시 1회 가드(`has_active_run`)와 숨김 거부(#371)가 같이 쓴다
ACTIVE_RUN_STATUSES: tuple[str, ...] = ("queued", "running", "awaiting_approval")


class ParamsInvalid(ValueError):
    """params가 agent.params_schema를 위반."""


class SkillNotRunnable(ParamsInvalid):
    """이 사용자가 실행할 스킬 version이 없다(표준 계획 D8, R3-09).

    예전에는 version이 None이면 조용히 반환해 biznotes 워커가 구 prompt 경로를 실행했다.
    `ParamsInvalid`의 하위 클래스라 스케줄러·이벤트 루프의 기존 처리(건너뛰고 계속)를 탄다.
    """


class ModelUnavailable(ValueError):
    """run에 고른 모델을 쓸 수 없다(#359) — 없거나, 모델·프로바이더가 꺼졌거나, llm 능력이 없다.

    없는 모델과 꺼진 모델을 같은 문구로 거부한다. 제품 라우터는 422로 바꾼다.
    """


class ChatRunLinkInvalid(ValueError):
    """chat run의 session/prompt 교차 행 연결이 유효하지 않다."""


class RunNotFinished(ValueError):
    """진행 중인 run은 숨기지 못한다(#371 D1) — 숨기면 진행 상황과 취소할 곳이 목록에서 사라진다.

    제품 라우터는 409로 바꾼다.
    """


class ActiveChatRunExists(Exception):
    """같은 chat session에 이미 활성 run이 존재한다."""

    def __init__(self) -> None:
        super().__init__("이 대화에서 이미 실행 중인 에이전트가 있습니다")


_CHAT_RUN_CONSTRAINTS = frozenset(
    {"uq_agent_runs_chat_active", "uq_agent_runs_idempotency"}
)


def validate_params(agent: Agent, params: dict[str, Any]) -> None:
    _validate_against(agent.params_schema, params)


def _validate_against(schema: dict[str, Any] | None, params: dict[str, Any]) -> None:
    if not schema:
        return
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(params), key=lambda e: e.path)
    if errors:
        raise ParamsInvalid(
            "; ".join(
                f"{'/'.join(map(str, e.path)) or 'params'}: {e.message}" for e in errors
            )
        )


async def has_active_run(db: AsyncSession, agent_id: uuid.UUID, notebook_id: uuid.UUID) -> bool:
    """동일 에이전트·노트북에 진행 중(queued|running|awaiting_approval) 실행이 있는지.

    스케줄 동시 1회 가드. awaiting_approval을 포함하지 않으면(CDX-D8) 승인 대기
    중인 run이 "활성 아님"으로 오판되어 동일 스케줄이 중복 큐잉될 수 있다.
    """
    count = await db.scalar(
        select(func.count())
        .select_from(AgentRun)
        .where(
            AgentRun.agent_id == agent_id,
            AgentRun.notebook_id == notebook_id,
            AgentRun.status.in_(ACTIVE_RUN_STATUSES),
        )
    )
    return bool(count)


def run_is_visible() -> ColumnElement[bool]:
    """노트북 run 목록 조건(#371) — 숨긴 run을 뺀다. 집계·단건 조회에는 쓰지 않는다."""
    return AgentRun.hidden_at.is_(None)


async def set_run_hidden(
    db: AsyncSession, run: AgentRun, *, hidden: bool, actor_id: uuid.UUID
) -> bool:
    """run을 목록에서 숨기거나(`hidden=True`) 되돌린다(#371). 바꿨으면 True.

    숨기면 `hidden_at`(지금)·`hidden_by`(행위자)를 채우고, 해제하면 둘 다 비운다. 이미 그
    상태면 아무것도 바꾸지 않고 False다(멱등). 진행 중인 run(`ACTIVE_RUN_STATUSES`)은 숨기지
    못한다(`RunNotFinished`) — 해제는 상태와 무관하다. 감사 로그를 남기고, 커밋은 호출자가 한다.
    누가 숨길 수 있는지는 제품 라우터가 정한다. 동시 요청은 호출자가 행을 잠가 직렬화한다.
    """
    if hidden == (run.hidden_at is not None):
        return False
    if hidden and run.status in ACTIVE_RUN_STATUSES:
        raise RunNotFinished("진행 중인 실행은 숨길 수 없습니다")
    run.hidden_at = datetime.now(UTC) if hidden else None
    run.hidden_by = actor_id if hidden else None
    await record(db, actor_id, "agent_run.hide" if hidden else "agent_run.unhide", str(run.id))
    return True


#: 승인 만료로 run을 실패시킬 때의 오류 문구 — 제품 승인 라우터의 지연 만료와 같다.
APPROVAL_EXPIRED_ERROR = "승인 대기 시간이 만료되었습니다"


@dataclass(frozen=True)
class ExpiredApproval:
    """`expire_stale_approvals`가 만료시킨 승인 하나. 제품이 이벤트를 발행할 때 쓴다."""

    approval_id: uuid.UUID
    run_id: uuid.UUID
    #: 이 승인으로 run을 `failed`로 바꿨는가(run이 이미 다른 상태면 False)
    run_failed: bool


async def expire_stale_approvals(
    db: AsyncSession, *, now: datetime | None = None
) -> list[ExpiredApproval]:
    """기한이 지난 pending 승인을 `expired`로, 그 run을 `failed`로 바꾼다(#153 A-7).

    무인 run(스케줄·웹훅·이벤트)이 승인 대기에 들어가면 `has_active_run` 때문에 같은 (agent,
    notebook)의 다음 트리거가 모두 막힌다. 지금은 누군가 승인·거부를 시도할 때만 만료를 평가하므로,
    제품 reaper가 이 함수를 주기적으로 부른다. react 승인(채팅 포함)에도 똑같이 적용된다.

    - approval CAS(`status='pending' AND expires_at < now`)와 run CAS
      (`status='awaiting_approval'`)를 같은 트랜잭션에서 한다. 제품 라우터의 지연 만료와 경합해도
      approval CAS 때문에 한쪽만 이긴다.
    - **커밋하지 않는다.** 호출자가 커밋한 뒤 반환값으로 `approval_resolved(expired)`·
      `status(failed)`를 발행한다. 세션에 이미 올라온 행은 갱신하지 않는다
      (`synchronize_session=False`).
    - 시각 비교는 DB `now()`다. `now`는 테스트 주입용이다.
    """
    cutoff: Any = now if now is not None else func.now()
    expired = (
        await db.execute(
            update(AgentRunApproval)
            .where(AgentRunApproval.status == "pending", AgentRunApproval.expires_at < cutoff)
            .values(status="expired")
            .returning(AgentRunApproval.id, AgentRunApproval.run_id)
            .execution_options(synchronize_session=False)
        )
    ).all()
    if not expired:
        return []
    failed = set(
        (
            await db.execute(
                update(AgentRun)
                .where(
                    AgentRun.id.in_({run_id for _, run_id in expired}),
                    AgentRun.status == "awaiting_approval",
                )
                .values(status="failed", error=APPROVAL_EXPIRED_ERROR, finished_at=cutoff)
                .returning(AgentRun.id)
                .execution_options(synchronize_session=False)
            )
        ).scalars()
    )
    return [
        ExpiredApproval(approval_id=approval_id, run_id=run_id, run_failed=run_id in failed)
        for approval_id, run_id in expired
    ]


async def resolve_wrapper_run_version(
    db: AsyncSession, agent: Agent, user_id: uuid.UUID
) -> tuple[Skill, SkillVersion]:
    """스킬 wrapper를 이 사용자가 실행할 (스킬, version)으로 푼다(D8 `resolve_run_version`).

    소유자는 가장 최근 freeze, 그 밖(관리자 포함)은 노출 version이다. wrapper가 가리키는
    version을 그대로 실행하지 않는다 — 소유자의 비공개 freeze가 새기 때문이다(R1-02).
    """
    from biznotes_core.services.skills.exposure import resolve_run_version_id

    wrapper_version = (
        await db.get(SkillVersion, agent.skill_version_id)
        if agent.skill_version_id is not None
        else None
    )
    skill = await db.get(Skill, wrapper_version.skill_id) if wrapper_version else None
    if skill is None:
        raise SkillNotRunnable("스킬을 찾을 수 없습니다")
    version_id = resolve_run_version_id(skill, user_id)
    version = await db.get(SkillVersion, version_id) if version_id is not None else None
    if version is None or version.skill_id != skill.id:
        raise SkillNotRunnable("실행할 수 있는 스킬 version이 없습니다(공개되지 않았거나 꺼짐)")
    return skill, version


async def _pin_skill_version(
    db: AsyncSession, run: AgentRun, skill_version_id: uuid.UUID
) -> None:
    """실행 재현성: 실행할 버전을 run에 스냅샷 복사(§9.4). 없으면 `SkillNotRunnable`.

    run을 INSERT하기 **전에** 스킬 행을 `FOR NO KEY UPDATE`로 잠근다(#116). 스킬 삭제
    (`remove_skill_wrappers`)·freeze와 잠금 순서를 skills → agents로 맞춘다. 잠그지 않으면 한
    flush의 `INSERT agent_runs`(wrapper `KEY SHARE`) → `UPDATE skills`(run_count) 사이에 끼어든
    삭제와 교착한다. 같은 스킬의 staging끼리는 run_count UPDATE로 이미 직렬화돼 경합이 늘지
    않는다. 기다리는 사이 스킬이 삭제됐으면 `SkillNotRunnable`이다. `populate_existing`이 세션의
    Skill 속성을 DB 값으로 덮으므로 `no_autoflush` 안에서 미반영 변경을 둔 채 부르지 않는다.
    """
    version = await db.get(SkillVersion, skill_version_id)
    if version is None:
        raise SkillNotRunnable("실행할 수 있는 스킬 version이 없습니다")
    skill = (
        await db.execute(
            select(Skill)
            .where(Skill.id == version.skill_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if skill is None:
        raise SkillNotRunnable("스킬을 찾을 수 없습니다")
    run.skill_version_id = version.id
    run.skill_package_hash = version.package_hash
    run.skill_snapshot = {
        "skill_version_id": str(version.id),
        "package_hash": version.package_hash,
        "name": skill.name,
        "description": skill.description,
        "manifest": version.manifest,
        "files": [{"path": f.get("path"), "hash": f.get("hash")} for f in version.file_tree or []],
    }
    skill.run_count += 1


async def _validate_chat_run_link(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    chat_session_id: uuid.UUID | None,
    prompt_message_id: uuid.UUID | None,
) -> None:
    if chat_session_id is None or prompt_message_id is None:
        raise ChatRunLinkInvalid("chat run에는 chat_session_id와 prompt_message_id가 필요합니다")
    session = await db.get(ChatSession, chat_session_id)
    message = await db.get(ChatMessage, prompt_message_id)
    if session is None or session.user_id != user_id:
        raise ChatRunLinkInvalid("chat session 소유자가 run 사용자와 일치하지 않습니다")
    if message is None or message.role != "user":
        raise ChatRunLinkInvalid("prompt_message_id는 user 메시지를 가리켜야 합니다")
    if message.session_id != chat_session_id:
        raise ChatRunLinkInvalid("prompt message와 chat session이 일치하지 않습니다")


async def validate_run_params(
    db: AsyncSession, agent: Agent, user_id: uuid.UUID, params: dict[str, Any]
) -> uuid.UUID | None:
    """실행 시점과 같은 규칙으로 params를 검증한다. 실패는 `ParamsInvalid`(`SkillNotRunnable` 포함).

    스킬 wrapper면 이 사용자가 실행할 version의 매니페스트 스키마로, 그 밖은 `agent.params_schema`로
    검증한다. 스킬 wrapper면 그 version id를 돌려준다. 자동화 등록 시점 검증(#255)도 이 함수를 쓴다.
    """
    if agent.source_kind == "skill_package":
        # 파라미터는 wrapper가 아니라 실행할 version의 매니페스트로 검증한다(D8 run staging)
        from biznotes_core.services.skills.runtime import params_schema_for_version

        _, version = await resolve_wrapper_run_version(db, agent, user_id)
        _validate_against(params_schema_for_version(version), params)
        return version.id
    validate_params(agent, params)
    return None


async def check_run_model(db: AsyncSession, model_id: uuid.UUID) -> None:
    """run에 고를 수 있는 모델인지 확인한다(#359) — 켜진 llm 모델이고 프로바이더도 켜져 있다.

    아니면 `ModelUnavailable`. 실행 시점에 다시 꺼지면 노드가 실패한다(게이트웨이 `resolve_model`).
    """
    found = await db.scalar(
        select(LLMModel.id)
        .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
        .where(
            LLMModel.id == model_id,
            LLMModel.enabled.is_(True),
            LLMProvider.enabled.is_(True),
            literal("llm") == any_(LLMModel.capabilities),
        )
    )
    if found is None:
        raise ModelUnavailable("요청한 모델을 사용할 수 없습니다")


async def stage_agent_run(
    db: AsyncSession,
    uow: UnitOfWork,
    *,
    agent: Agent,
    notebook_id: uuid.UUID,
    user_id: uuid.UUID,
    user_role: str,
    params: dict[str, Any],
    trigger: str = "manual",
    source_ids: list[uuid.UUID] | None = None,
    enforce_limit: bool = True,
    chat_session_id: uuid.UUID | None = None,
    prompt_message_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
    model_id: uuid.UUID | None = None,
) -> AgentRun:
    """호출자 소유 UoW에 검증된 AgentRun과 커밋 후 큐잉을 등록한다.

    source_ids: 입력 소스 스코프 핀(관측·재현성). `None`은 노트 전체, `[]`는 선택한
        소스 없음, 목록은 그 소스만이다 — `[]`를 `None`으로 바꾸지 않고 그대로 저장한다(#13).
    model_id: run 단위 요청 모델(#359). 켜진 llm 모델만 받는다(`ModelUnavailable`). 노드
        model_id가 없는 llm·react 노드의 1차 모델이 된다 — 제품 워커가 `RunContext.run_model_id`로
        넘긴다.

    이 함수는 UoW를 열거나 커밋하지 않는다.
    """
    pinned_version_id = await validate_run_params(db, agent, user_id, params)
    if model_id is not None:
        await check_run_model(db, model_id)
    if enforce_limit:
        await check_usage(db, user_id, user_role, "agent_runs")
    if trigger == "chat":
        await _validate_chat_run_link(
            db,
            user_id=user_id,
            chat_session_id=chat_session_id,
            prompt_message_id=prompt_message_id,
        )
    elif (
        chat_session_id is not None
        or prompt_message_id is not None
        or idempotency_key is not None
    ):
        raise ChatRunLinkInvalid("chat 연결 필드는 trigger='chat' 실행에서만 사용할 수 있습니다")
    if idempotency_key is not None and not 1 <= len(idempotency_key) <= 64:
        raise ChatRunLinkInvalid("idempotency_key 길이는 1~64자여야 합니다")

    run = AgentRun(
        agent_id=agent.id,
        notebook_id=notebook_id,
        user_id=user_id,
        params=params,
        input_source_ids=[str(s) for s in source_ids] if source_ids is not None else None,
        trigger=trigger,
        status="queued",
        chat_session_id=chat_session_id,
        prompt_message_id=prompt_message_id,
        idempotency_key=idempotency_key,
        model_id=model_id,
    )
    # 스킬 wrapper면 실행 시점 버전을 run에 박제 — 공개 version이 바뀌어도 과거 run은 불변(§9.4).
    if pinned_version_id is not None:
        await _pin_skill_version(db, run, pinned_version_id)


    # [R3-1c/R32CR-04] 이전에는 run을 커밋하고, 발행한 뒤, task id를 또 커밋했다
    # (결함 α+β). 발행이 실패하면 예외가 라우터로 올라 500이 되는데 run은 이미
    # 커밋돼 있어 사용자 재시도가 중복 run을 만들었다.
    #
    # task id를 미리 만들어 run과 함께 한 번에 커밋하고, 커밋 성공 후에 발행한다.
    # 발행이 실패해도 `reap_orphaned_runs`가 회수하므로 예외를 삼키고 202를
    # 반환한다(HCI-R3B-1(a)). 취소 경로(`agents.py`)가 읽는 celery_task_id는
    # 발행 전에 이미 커밋돼 있어 revoke가 항상 유효한 id를 본다.
    task_id = str(uuid.uuid4())
    run.celery_task_id = task_id

    db.add(run)
    await db.flush()  # PK 확정 — 콜백이 ORM 객체가 아닌 값만 붙들게 한다
    run_id_str = str(run.id)

    async def initialize_then_enqueue() -> None:
        from biznotes_core.services.events import initialize_run_event_sequence_async

        await initialize_run_event_sequence_async(run_id_str)
        # 태스크를 import하지 않고 이름으로 발행한다 — 태스크 모듈은 프로젝트에
        # 있으므로 import하면 코어가 프로젝트에 의존하게 된다.
        celery_app.send_task(RUN_AGENT, args=[run_id_str], task_id=task_id, queue="agent")

    uow.after_commit(initialize_then_enqueue)

    return run


async def enqueue_agent_run(
    db: AsyncSession,
    *,
    agent: Agent,
    notebook_id: uuid.UUID,
    user_id: uuid.UUID,
    user_role: str,
    params: dict[str, Any],
    trigger: str = "manual",
    source_ids: list[uuid.UUID] | None = None,
    enforce_limit: bool = True,
    model_id: uuid.UUID | None = None,
) -> AgentRun:
    """기존 호출 계약을 유지하는 독립 UoW wrapper."""
    async with publishing_unit_of_work(db) as uow:
        run = await stage_agent_run(
            db,
            uow,
            agent=agent,
            notebook_id=notebook_id,
            user_id=user_id,
            user_role=user_role,
            params=params,
            trigger=trigger,
            source_ids=source_ids,
            enforce_limit=enforce_limit,
            model_id=model_id,
        )

    await db.refresh(run)
    await record_usage(user_id, "agent_runs", 1)
    return run


async def recover_chat_run_conflict(
    db: AsyncSession,
    error: IntegrityError,
    *,
    user_id: uuid.UUID,
    chat_session_id: uuid.UUID,
    idempotency_key: str | None,
) -> AgentRun:
    """rollback 완료 뒤 D-10b의 allowlist·멱등 조회 절차를 수행한다."""
    constraint_name = unique_violation_constraint_name(error)
    if constraint_name not in _CHAT_RUN_CONSTRAINTS:
        raise error

    if idempotency_key is not None:
        existing = await db.scalar(
            select(AgentRun).where(
                AgentRun.user_id == user_id,
                AgentRun.chat_session_id == chat_session_id,
                AgentRun.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return existing
    raise ActiveChatRunExists()


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "ParamsInvalid",
    "RunNotFinished",
    "SkillNotRunnable",
    "ActiveChatRunExists",
    "ChatRunLinkInvalid",
    "ModelUnavailable",
    "UsageLimitExceeded",
    "check_run_model",
    "enqueue_agent_run",
    "has_active_run",
    "recover_chat_run_conflict",
    "resolve_wrapper_run_version",
    "run_is_visible",
    "set_run_hidden",
    "stage_agent_run",
    "validate_params",
    "validate_run_params",
]
