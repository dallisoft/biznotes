import uuid

from biznotes_core.api.deps import get_current_user
from biznotes_core.api.v1.artifacts import artifact_response
from biznotes_core.core.db import get_db
from biznotes_core.core.rate_limit import RateLimiter
from biznotes_core.core.uow import publishing_unit_of_work
from biznotes_core.models import Agent, AgentRun, Notebook, User
from biznotes_core.services.agent_runs import (
    ACTIVE_RUN_STATUSES,
    run_is_visible,
    set_run_hidden,
)
from biznotes_core.services.agents.policy import assert_agent_execution_allowed
from biznotes_core.services.authz import (
    assert_can_read_run,
    is_private_chat_run,
    system_agent_audience_condition,
)
from biznotes_core.services.notebooks import get_permission
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.schemas.agent import (
    AgentOut,
    AgentRunApprovalOut,
    AgentRunCreate,
    AgentRunOut,
    HiddenRunsOut,
)
from app.services.chat_entry import (
    ENTRY_CANCELLED_TEXT,
    entry_failed_text,
    write_entry_result,
)
from app.services.run_models import load_run_models

router = APIRouter(tags=["agents"])
settings = get_settings()


@router.get("/agents", response_model=list[AgentOut])
async def list_agents(
    kind: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AgentOut]:
    # 코어 `authz.can_list_agent`와 같은 조건을 SQL로 건다 — system ∨ 소유자 ∨ admin, audience,
    # 그리고 enabled ∧ 스킬 wrapper 아님(`agent_base_policy`, `can_list_agent`).
    # 타인의 user/group-scope 정의는 이름·params_schema·cost_budget_usd까지 나열되지 않는다
    # (계획 AC-34, P0-01). 스킬 wrapper(source_kind="skill_package")는 스킬 UI로만 노출(§12.5).
    # 관리자 전용 system 정의(`audience`, 코어 v0.29.0 #436)는 비관리자에게 소유자여도 나열하지
    # 않는다 — 코어 `system_agent_audience_condition`을 소유자 분기 바깥에 AND로 건다.
    q = (
        select(Agent)
        .where(
            Agent.enabled.is_(True),
            Agent.source_kind != "skill_package",
            system_agent_audience_condition(user),
        )
        .order_by(Agent.name)
    )
    if user.role != "admin":
        q = q.where(or_(Agent.scope == "system", Agent.owner_id == user.id))
    if kind:
        q = q.where(Agent.kind == kind)
    rows = await db.execute(q)
    return [_to_agent_out(a) for a in rows.scalars().all()]


def _to_agent_out(agent: Agent) -> AgentOut:
    out = AgentOut.model_validate(agent)
    out.cost_budget_usd = _extract_cost_budget_usd(agent)
    return out


def _extract_cost_budget_usd(agent: Agent) -> float | None:
    """definition의 첫 react_agent 노드에 설정된 cost_budget_usd를 추출(phase-22-design.md
    §2.4) — 없으면 None(비 react_agent 워크플로우·skill 등)."""
    if agent.kind != "workflow" or not isinstance(agent.definition, dict):
        return None
    for node in agent.definition.get("nodes", []) or []:
        if isinstance(node, dict) and node.get("type") == "react_agent":
            budget = node.get("cost_budget_usd")
            if isinstance(budget, int | float):
                return float(budget)
    return None


async def _notebook_permission(
    db: AsyncSession, notebook_id: uuid.UUID, user: User
) -> tuple[Notebook, str]:
    nb = await db.get(Notebook, notebook_id)
    if nb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    perm = await get_permission(db, nb, user)
    if perm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    return nb, perm


async def _notebook_readable(db: AsyncSession, notebook_id: uuid.UUID, user: User) -> Notebook:
    nb, _perm = await _notebook_permission(db, notebook_id, user)
    return nb


def _can_cancel(run: AgentRun, user: User, permission: str) -> bool:
    """실행 취소 인가 — 실행 소유자 ∨ 노트북 write 이상(admin은 `get_permission`이 owner로
    승격). read 독자(공유·공개 열람)는 자기 run만 취소한다(#173, docs/plans/run-cancel-authz.md)."""
    return run.user_id == user.id or permission in ("owner", "write")


#: 숨길 수 있는 상태(#220 결정 3) — 실패·취소만. 성공 run은 범위 밖이고 진행 중 run은 코어가 막는다.
_HIDEABLE_STATUSES = ("failed", "cancelled")


def _can_hide(run: AgentRun, user: User, permission: str) -> bool:
    """숨김 인가(#220) — 숨길 수 있는 상태 ∧ 취소와 같은 규칙(`_can_cancel`)."""
    return run.status in _HIDEABLE_STATUSES and _can_cancel(run, user, permission)


def _run_out_for(run: AgentRun, user: User, permission: str) -> AgentRunOut:
    out = AgentRunOut.model_validate(run)
    out.can_cancel = _can_cancel(run, user, permission)
    out.can_hide = _can_hide(run, user, permission)
    out.hidden = run.hidden_at is not None
    return out


@router.post(
    "/notebooks/{notebook_id}/agent-runs",
    response_model=AgentRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[
        Depends(RateLimiter("agent-run", scope="user", limit_setting="rate_limit_agent_run"))
    ],
)
async def create_agent_run(
    notebook_id: uuid.UUID,
    body: AgentRunCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentRunOut:
    await _notebook_readable(db, notebook_id, user)
    agent = await db.get(Agent, body.agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    # 실행 인가 단일 관문(코어 policy.py:41-53) — enabled·소유자 draft 테스트 실행
    # (`can_run_owner_draft`)·스킬 wrapper 노출 모델(biznotes-core D8)·비-스킬 에이전트의
    # system ∨ 소유자 ∨ admin(`can_run_agent`)을 한 번에 판정한다. 불가면 404(존재 은닉).
    # 코어 `stage_agent_run`은 비-스킬 인가를 하지 않으므로 이 관문이 API의 유일한 방어선이다.
    await assert_agent_execution_allowed(db, user, agent)

    from biznotes_core.services.agent_runs import (
        enqueue_agent_run,
    )

    # UsageLimitExceeded(→429)·ParamsInvalid(→422)·ModelUnavailable(→422, #216)는 전역 예외
    # 핸들러가 매핑한다(R1-1). 모델 검사는 위 노트북·실행 인가(404) 뒤라 모델 상태를 캐 볼 수 없다.
    run = await enqueue_agent_run(
        db,
        agent=agent,
        notebook_id=notebook_id,
        user_id=user.id,
        user_role=user.role,
        params=body.params,
        source_ids=body.source_ids,
        trigger="manual",
        model_id=body.model_id,
    )

    out = AgentRunOut.model_validate(run)
    out.agent_name = agent.name
    out.agent_icon = agent.icon
    out.can_cancel = True  # 실행 소유자 본인
    return out


@router.get("/notebooks/{notebook_id}/agent-runs", response_model=list[AgentRunOut])
async def list_agent_runs(
    notebook_id: uuid.UUID,
    hidden: bool = Query(
        default=False, description="true면 숨긴 run만 돌려준다(「숨긴 결과 보기」, #221)"
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AgentRunOut]:
    _nb, perm = await _notebook_permission(db, notebook_id, user)
    # 숨긴 run은 기본 목록에서만 뺀다 — 단건 조회·집계·산출물에는 남는다(#220, 코어 #371).
    # 숨긴 목록도 같은 read 관문이다 — read 이상은 숨긴 run을 단건 조회로 이미 볼 수 있다.
    # 되돌리기 인가는 항목의 `can_hide`(취소 규칙)가 알린다
    visibility = AgentRun.hidden_at.is_not(None) if hidden else run_is_visible()
    rows = await db.execute(
        select(AgentRun, Agent.name, Agent.icon)
        .join(Agent, AgentRun.agent_id == Agent.id)
        .where(AgentRun.notebook_id == notebook_id, visibility)
        # 채팅 진입 run은 채팅 안에서만 보인다 — 본인 것도 뺀다(#66 D2). 코어
        # `private_chat_run_exclusion`(타인 것만 뺀다)보다 넓다. 숨김 조건과 따로 둔 절이라
        # 숨긴 목록(`?hidden=true`, #221)에도 그대로 걸린다
        .where(AgentRun.trigger != "chat")
        .order_by(AgentRun.created_at.desc())
        .limit(50)
    )
    runs = rows.all()
    # 실제로 응답한 모델·폴백 여부(#234) — 이 run들의 실행 로그를 한 번에 조회한다.
    # 로그 원문은 싣지 않는다
    run_models = await load_run_models(db, [run.id for run, _name, _icon in runs])
    result = []
    for run, name, icon in runs:
        out = _run_out_for(run, user, perm)
        out.agent_name = name
        out.agent_icon = icon
        if (models := run_models.get(run.id)) is not None:
            out.used_models = models.used_models
            out.fallback_used = models.fallback_used
        result.append(out)
    return result



async def _get_run_with_permission(
    db: AsyncSession, run_id: uuid.UUID, user: User
) -> tuple[AgentRun, str]:
    run = await db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    # 채팅 진입 run은 실행자와 admin만 본다(코어 `assert_can_read_run`, #66) — 노트북 owner·write
    # 라도 타인 채팅 run은 404다. 단건·취소·숨기기·되돌리기·승인·산출물이 모두 이 함수를 거친다.
    # 노트북 검사보다 먼저 한다 — 노트북에 접근할 수 없는 사용자에게도 없는 run과 같은 404다
    if is_private_chat_run(run):
        await assert_can_read_run(db, user, run)
    _nb, perm = await _notebook_permission(db, run.notebook_id, user)
    return run, perm


async def _get_run(db: AsyncSession, run_id: uuid.UUID, user: User) -> AgentRun:
    run, _perm = await _get_run_with_permission(db, run_id, user)
    return run


@router.get("/agent-runs/{run_id}", response_model=AgentRunOut)
async def get_agent_run(
    run_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentRunOut:
    run, perm = await _get_run_with_permission(db, run_id, user)
    agent = await db.get(Agent, run.agent_id)
    out = _run_out_for(run, user, perm)
    if agent:
        out.agent_name = agent.name
        out.agent_icon = agent.icon
    return out


@router.post("/agent-runs/{run_id}/cancel", response_model=AgentRunOut)
async def cancel_agent_run(
    run_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentRunOut:
    run, perm = await _get_run_with_permission(db, run_id, user)
    # 존재·read 검사(404) → 취소 인가(403) → 종료 상태(409). read 이상은 목록·상세로 run을
    # 이미 보므로 거부를 404로 숨기지 않는다(#173).
    if not _can_cancel(run, user, perm):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "이 실행을 취소할 권한이 없습니다")
    if run.status in ("succeeded", "failed", "cancelled"):
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 종료된 실행입니다")
    chat_run = is_private_chat_run(run)
    if chat_run:
        # 워커의 결과 기록(같은 행을 잠근다)과 직렬화한다 — 잠근 뒤 종료 여부를 다시 본다
        await db.refresh(run, with_for_update=True)
        if run.status in ("succeeded", "failed", "cancelled"):
            raise HTTPException(status.HTTP_409_CONFLICT, "이미 종료된 실행입니다")
    run.status = "cancelled"
    if chat_run:
        # 채팅 진입 run의 결과 행은 「중지했습니다」다 — 상태 전이와 한 커밋(#66 §3 워커)
        await write_entry_result(
            db,
            run_id=run.id,
            session_id=run.chat_session_id,
            content=ENTRY_CANCELLED_TEXT,
            no_result=True,
        )
    await db.commit()
    if run.celery_task_id:
        from biznotes_core.workers.celery_app import celery_app

        celery_app.control.revoke(run.celery_task_id, terminate=False)
    out = AgentRunOut.model_validate(run)
    out.can_cancel = True
    out.can_hide = True  # 취소한 run은 같은 규칙으로 바로 숨길 수 있다(#220)
    return out


async def _set_run_hidden(
    db: AsyncSession, run_id: uuid.UUID, user: User, *, hidden: bool
) -> AgentRunOut:
    """숨김·되돌리기 공용(#220) — 존재·read 검사(404) → 취소 규칙(403) → 상태(409).

    진행 중 run은 코어 `set_run_hidden`이 `RunNotFinished`로 막고 전역 핸들러가 409로 바꾼다.
    성공 run은 숨김 범위 밖이라 여기서 같은 409로 막는다. 이미 그 상태면 바꾸지 않고 200이다
    (코어 멱등 — 처음 숨긴 사람·감사 로그 1건 유지). 되돌리기는 상태와 무관하다.
    """
    run, perm = await _get_run_with_permission(db, run_id, user)
    if not _can_cancel(run, user, perm):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "이 실행을 숨길 권한이 없습니다")
    # 행을 잠가 동시 숨김·상태 전이와 직렬화한다(코어 `set_run_hidden` 계약)
    await db.refresh(run, with_for_update=True)
    if hidden and run.status not in (*_HIDEABLE_STATUSES, *ACTIVE_RUN_STATUSES):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "실패하거나 취소한 실행만 숨길 수 있습니다"
        )
    await set_run_hidden(db, run, hidden=hidden, actor_id=user.id)
    await db.commit()
    return _run_out_for(run, user, perm)


@router.post("/agent-runs/{run_id}/hide", response_model=AgentRunOut)
async def hide_agent_run(
    run_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentRunOut:
    """실패·취소 run을 노트북 목록에서 숨긴다 — 노트북의 모든 사용자에게 숨겨진다(#220 결정 1)."""
    return await _set_run_hidden(db, run_id, user, hidden=True)


@router.post("/agent-runs/{run_id}/unhide", response_model=AgentRunOut)
async def unhide_agent_run(
    run_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentRunOut:
    """숨김을 되돌린다 — 웹 「되돌리기」 토스트가 부른다(#220 결정 2)."""
    return await _set_run_hidden(db, run_id, user, hidden=False)


@router.post("/notebooks/{notebook_id}/agent-runs/hide-failed", response_model=HiddenRunsOut)
async def hide_failed_agent_runs(
    notebook_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiddenRunsOut:
    """「실패한 결과 모두 숨기기」(#220) — 호출자가 숨길 수 있는 실패 run만 숨긴다.

    write 이상(admin 승격 포함)은 노트북의 모든 실패 run, read 독자는 자기 실패 run만이다
    (`_can_cancel`과 같은 규칙). 취소한 run은 일괄 대상이 아니다(항목 메뉴로 숨긴다).
    """
    _nb, perm = await _notebook_permission(db, notebook_id, user)
    q = select(AgentRun).where(
        AgentRun.notebook_id == notebook_id, AgentRun.status == "failed", run_is_visible()
    )
    # 채팅 진입 run은 목록에 없으므로 일괄 숨기기 대상도 아니다(#66 D2)
    q = q.where(AgentRun.trigger != "chat")
    if perm not in ("owner", "write"):
        q = q.where(AgentRun.user_id == user.id)
    rows = await db.execute(q.order_by(AgentRun.created_at.desc()).with_for_update())
    hidden_ids: list[uuid.UUID] = []
    for run in rows.scalars().all():
        if await set_run_hidden(db, run, hidden=True, actor_id=user.id):
            hidden_ids.append(run.id)
    await db.commit()
    return HiddenRunsOut(hidden=len(hidden_ids), run_ids=hidden_ids)


# [R5-3] agent-run `/events` SSE 엔드포인트 제거 — 프론트가 폴링(use-agents)으로 대체해
# 소비하지 않던 죽은 계층. router/worker의 publish(_publish, R3 UoW·after_commit과
# 얽힘)는 존치한다(no-op 발행).


def _assert_approval_permission(run: AgentRun, user: User) -> None:
    """승인/거부 권한 — run 트리거 사용자 본인 또는 admin만(CR-01, §3.3)."""
    if user.role == "admin":
        return
    if run.user_id == user.id:
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "이 실행의 승인 권한이 없습니다")


async def _resolve_approval(
    run_id: uuid.UUID,
    approval_id: uuid.UUID,
    outcome: str,
    user: User,
    db: AsyncSession,
) -> "AgentRunOut":
    """approve/reject 공용 로직 — 원자적 상태 전이(SEC-D4) + 만료 지연평가(SEC-D2)."""
    from datetime import UTC, datetime

    from biznotes_core.models import AgentRunApproval
    from sqlalchemy import update

    run, perm = await _get_run_with_permission(db, run_id, user)
    approval = await db.get(AgentRunApproval, approval_id)
    if approval is None or approval.run_id != run.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found")

    _assert_approval_permission(run, user)

    if run.status == "cancelled":
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 취소된 실행입니다")

    # [phase-19-design.md §2.7, SEC-D2] 만료 지연평가는 소유자/admin 권한 검증을
    # 통과한 이 경로 내부에서만 수행 — read-only 경로(SSE 구독)는 상태를 바꾸지 않는다.
    now = datetime.now(UTC)
    expire_result = await db.execute(
        update(AgentRunApproval)
        .where(
            AgentRunApproval.id == approval.id,
            AgentRunApproval.status == "pending",
            AgentRunApproval.expires_at < now,
        )
        .values(status="expired")
        .returning(AgentRunApproval.id)
    )
    if expire_result.scalar_one_or_none() is not None:
        run.status = "failed"
        run.error = "승인 대기 시간이 만료되었습니다"
        run.finished_at = now
        if is_private_chat_run(run):
            # 채팅 진입 run의 실패 결과 행 — 상태 전이와 한 커밋(#66 §3 워커)
            await write_entry_result(
                db,
                run_id=run.id,
                session_id=run.chat_session_id,
                content=entry_failed_text(run.error),
                no_result=True,
            )
        await db.commit()
        _publish(
            str(run.id),
            {"type": "approval_resolved", "approval_id": str(approval.id), "status": "expired"},
        )
        _publish(str(run.id), {"type": "status", "status": "failed", "error": run.error})
        raise HTTPException(status.HTTP_409_CONFLICT, "승인 시간이 만료되었습니다")

    # [SEC-D4] 조건부 원자적 UPDATE — TOCTOU 없이 단일 SQL로 check+act.
    resolve_result = await db.execute(
        update(AgentRunApproval)
        .where(AgentRunApproval.id == approval.id, AgentRunApproval.status == "pending")
        .values(status=outcome, resolved_by=user.id, resolved_at=now)
        .returning(AgentRunApproval.id)
    )
    if resolve_result.scalar_one_or_none() is None:
        # 이미 처리됨 — 동일 결과 재요청이면 멱등 200, 아니면 409.
        await db.refresh(approval)
        await db.commit()
        if approval.status == outcome:
            return _run_out_for(run, user, perm)
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 다른 결과로 처리된 승인입니다")

    from app.workers.tasks.agent import run_agent

    # [R3-1c/R32CR-04] 이전에는 approval 해결을 커밋하고, 발행한 뒤, task id를 또
    # 커밋했다(α+β). 발행이 실패하면 500이 오르는데 approval은 이미 resolved라
    # 사용자가 재시도해도 CAS가 걸려 **재발행 없이 200**을 돌려준다 — run은 영영
    # 시작되지 않은 것처럼 보인다.
    #
    # task id를 미리 만들어 approval 해결과 한 커밋으로 묶고, 커밋 후에 발행한다.
    # 발행이 실패해도 `reap_orphaned_runs`가 resolved-but-unconsumed approval을
    # 보고 회수하므로 예외를 삼키고 정상 응답한다(HCI-R3B-1(a)).
    task_id = str(uuid.uuid4())
    run.celery_task_id = task_id
    run_id_str = str(run.id)
    approval_id_str = str(approval.id)
    resolved_by = str(user.id)

    async with publishing_unit_of_work(db) as uow:
        uow.after_commit(
            lambda: _publish(
                run_id_str,
                {
                    "type": "approval_resolved",
                    "approval_id": approval_id_str,
                    "status": outcome,
                    "resolved_by": resolved_by,
                },
            )
        )
        uow.after_commit(
            lambda: run_agent.apply_async(args=[run_id_str], task_id=task_id, queue="agent")
        )

    return _run_out_for(run, user, perm)


def _publish(run_id: str, event: dict) -> None:
    """events:agent_run:{id} 채널 재사용(기존 worker._publish와 동일 헬퍼)."""
    from app.workers.tasks.agent import _publish as _worker_publish

    _worker_publish(run_id, event)


@router.post(
    "/agent-runs/{run_id}/approvals/{approval_id}/approve", response_model=AgentRunOut
)
async def approve_agent_run_approval(
    run_id: uuid.UUID,
    approval_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentRunOut:
    return await _resolve_approval(run_id, approval_id, "approved", user, db)


@router.post(
    "/agent-runs/{run_id}/approvals/{approval_id}/reject", response_model=AgentRunOut
)
async def reject_agent_run_approval(
    run_id: uuid.UUID,
    approval_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentRunOut:
    return await _resolve_approval(run_id, approval_id, "rejected", user, db)


@router.get(
    "/agent-runs/{run_id}/approvals", response_model=list[AgentRunApprovalOut]
)
async def list_agent_run_approvals(
    run_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AgentRunApprovalOut]:
    """승인/거부/자동승인 이력 — run 상세 뷰(19c)."""
    from biznotes_core.models import AgentRunApproval

    await _get_run(db, run_id, user)
    rows = await db.execute(
        select(AgentRunApproval)
        .where(AgentRunApproval.run_id == run_id)
        .order_by(AgentRunApproval.requested_at.desc())
    )
    return [AgentRunApprovalOut.model_validate(a) for a in rows.scalars().all()]


@router.get("/agent-runs/{run_id}/artifacts/{artifact_name}")
async def get_artifact(
    run_id: uuid.UUID,
    artifact_name: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    import anyio
    from biznotes_core.services.storage import get_object

    run = await _get_run(db, run_id, user)
    artifact = next((a for a in run.artifact_keys if a.get("name") == artifact_name), None)
    if artifact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact not found")

    data = await anyio.to_thread.run_sync(
        lambda: get_object(settings.storage_bucket_artifacts, artifact["key"])
    )
    # 산출물은 스킬 지시문이 만든 내용이다 — 능동 콘텐츠로 inline 서빙하지 않는다(biznotes-core D9)
    return artifact_response(data, artifact_name, artifact.get("content_type"))
