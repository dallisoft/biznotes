"""에이전트 실행 Celery 태스크 — agent 큐."""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from biznotes_core.models import Agent, AgentRun, Notebook, User
from biznotes_core.services.agents.node_errors import describe_node_failure
from biznotes_core.services.events import channel_for_agent_run, publish, publish_source_event
from biznotes_core.services.notebooks import get_permission
from biznotes_core.workers.celery_app import celery_app
from biznotes_core.workers.runner import get_session_factory, run_async
from redis.asyncio import Redis as AsyncRedis

from app.core.config import get_settings
from app.services.chat_entry import (
    ChatRunContext,
    ChatRunContextInvalid,
    entry_failed_text,
    load_chat_run_context,
    write_entry_result,
)

if TYPE_CHECKING:
    from biznotes_core.models import AgentRunApproval
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()
settings = get_settings()

# 실행 직전 재검사의 거부 문구 — 계정·실행 인가·노트북 권한 거부가 같은 문구를 쓴다(사유는 로그에만)
_DENIED_AT_START = "에이전트가 실행 직전 인가 조건을 더 이상 만족하지 않습니다"
# 실행 직전 재검사 자체가 예기치 않게 실패했을 때의 문구(#68) — 예외 원문은 로그로만 보낸다
_RECHECK_FAILED = "에이전트 실행 직전 인가 재검사 중 오류가 발생해 실행하지 않았습니다"

# 실행 직전 노트북 권한(BL-217, core-v0.17.1-upgrade §5.4·D-7). 대화형 trigger(코어
# `TRIGGER_SOURCES` 전부 — 수동·채팅)는 진입 시점(`_notebook_readable`)과 같은 read 이상을 다시
# 본다. 그 밖의 값(옛 무인 trigger 등)은 쓰기 이상이다(fail-closed). 코어 `TRIGGER_SOURCES`가
# 모두 대화형인지는 `tests/test_worker_recheck.py`가 본다.
_INTERACTIVE_TRIGGERS = frozenset({"manual", "chat"})
_WRITE_PERMISSIONS = frozenset({"owner", "write"})
_READ_PERMISSIONS = frozenset({"owner", "write", "read"})


def _required_notebook_permissions(trigger: str) -> frozenset[str]:
    if trigger in _INTERACTIVE_TRIGGERS:
        return _READ_PERMISSIONS
    # 대화형이 아닌 값 — 쓰기 이상
    return _WRITE_PERMISSIONS


async def _notebook_denial(db: "AsyncSession", run: AgentRun, user: User) -> str | None:
    """run의 노트북을 `user`가 지금 그 trigger에 필요한 수준으로 쓸 수 있는가. 아니면 사유 코드."""
    notebook = await db.get(Notebook, run.notebook_id)
    if notebook is None:
        return "notebook_missing"
    if await get_permission(db, notebook, user) not in _required_notebook_permissions(run.trigger):
        return "notebook_forbidden"
    return None


def _publish(run_id: str, event: dict[str, Any]) -> None:
    publish(channel_for_agent_run(run_id), event)


def _inject_resume_hint(
    definition: dict[str, Any], node_id: str, resume_payload: dict[str, Any]
) -> dict[str, Any]:
    """워크플로우 definition을 복사해 재개 대상 react_agent 노드에 `_resume` 힌트를
    주입한다(phase-19-design.md §2.5). 원본 definition은 변경하지 않는다."""
    import copy

    patched = copy.deepcopy(definition)
    for node in patched.get("nodes", []):
        if node.get("id") == node_id:
            node["_resume"] = resume_payload
            break
    return patched


async def _resolve_pending_approval(
    db: "AsyncSession", run_id: uuid.UUID
) -> "AgentRunApproval | None":
    """resolved(approved/rejected) & 미소비 approval을 원자적으로 소비(CAS).

    CDX-D1 반영 — 재개 판단은 run.status가 아니라 이 CAS의 성공 여부로 한다.
    """
    from biznotes_core.models import AgentRunApproval
    from biznotes_core.services.agents.react.approval import CONSUMABLE_STATUSES
    from sqlalchemy import select, update

    # 소비 CAS의 SQL 조립·실행·commit은 worker 소유(R2b, CR-R2B-01) — "무엇이 소비
    # 대상인가"라는 순수 판정 규칙(CONSUMABLE_STATUSES)만 approval 모듈에서 가져온다.
    result = await db.execute(
        update(AgentRunApproval)
        .where(
            AgentRunApproval.run_id == run_id,
            AgentRunApproval.status.in_(CONSUMABLE_STATUSES),
            AgentRunApproval.consumed.is_(False),
        )
        .values(consumed=True)
        .returning(AgentRunApproval.id)
    )
    approval_id = result.scalar_one_or_none()
    await db.commit()
    if approval_id is None:
        return None
    row = await db.execute(
        select(AgentRunApproval).where(AgentRunApproval.id == approval_id)
    )
    return row.scalar_one()


async def _persist_pending_approval(
    db: "AsyncSession", run: AgentRun, checkpoint: dict[str, Any]
) -> None:
    """execute_agent가 캐치한 ApprovalRequired checkpoint를 agent_run_approvals에 영속화.

    승인 카드에는 마스킹된 arguments_summary만 노출(SEC-D1·CDX-D7 반영, §2.6).
    """
    from datetime import timedelta

    from biznotes_core.models import AgentRunApproval

    from app.services.limits import approval_timeout_minutes

    # 관리자가 「사용 제한」에서 정한다(코어 `agents` 키). 범위 밖 저장값은 범위 안으로 읽는다
    timeout_minutes = await approval_timeout_minutes(db)

    now = datetime.now(UTC)
    approval = AgentRunApproval(
        run_id=run.id,
        tool_name=checkpoint["tool_name"],
        arguments_summary=checkpoint["arguments_summary"],
        status="pending",
        requested_at=now,
        expires_at=now + timedelta(minutes=timeout_minutes),
        checkpoint=checkpoint,
    )
    db.add(approval)
    run.status = "awaiting_approval"
    await db.commit()
    await db.refresh(approval)
    _publish(
        str(run.id),
        {
            "type": "approval_required",
            "approval_id": str(approval.id),
            "tool": approval.tool_name,
            "arguments_summary": approval.arguments_summary,
            "expires_at": approval.expires_at.isoformat(),
        },
    )


async def _claim_queued_run(db: "AsyncSession", run_id: uuid.UUID) -> bool:
    """`queued` run의 소유권을 원자적으로 주장한다 (R32CR-01).

    `UPDATE ... WHERE status='queued' RETURNING id`은 단일 문장이라 동시 실행되는
    두 워커 중 정확히 하나만 성공한다. `_resolve_pending_approval`이 승인 재개
    경로에서 하는 것과 같은 CAS 패턴이다(R2b/CDX-D1).

    성공하면 run은 이미 `running`이므로 호출자는 status를 다시 쓰지 않아도 된다.
    """
    from sqlalchemy import update

    result = await db.execute(
        update(AgentRun)
        .where(AgentRun.id == run_id, AgentRun.status == "queued")
        .values(status="running", started_at=datetime.now(UTC))
        .returning(AgentRun.id)
    )
    claimed = result.scalar_one_or_none() is not None
    await db.commit()
    return claimed


def _entry_usage(ctx: Any) -> dict[str, Any]:
    """결과 행 `token_usage` — run 누적 사용량에 폴백 표식(정적 채팅과 같은 키)을 더한다."""
    usage = dict(ctx.usage)
    if ctx.chat_fallback_used:
        usage["fallback"] = 1
    return usage


async def _fail_run(db: "AsyncSession", run: AgentRun, error: str) -> dict[str, Any]:
    """run을 고정 문구로 `failed`로 닫는다.

    채팅 진입 run이면 같은 커밋에 실패 결과 행을 쓴다(#66).
    """
    run.status = "failed"
    run.error = error
    run.finished_at = datetime.now(UTC)
    if run.trigger == "chat":
        await write_entry_result(
            db,
            run_id=run.id,
            session_id=run.chat_session_id,
            content=entry_failed_text(error),
            no_result=True,
        )
    await db.commit()
    return {"ok": False, "error": error}


async def _lock_chat_run(db: "AsyncSession", run: AgentRun) -> bool:
    """채팅 진입 run의 마무리 전에 행을 잠그고 다시 읽는다 — 취소 API가 먼저 닫았으면 False.

    취소 API도 같은 행을 잠그고 「중지했습니다」를 쓴다. 잠금으로 둘 중 하나만 결과 행을 쓰고,
    취소된 run을 `succeeded`·`failed`로 덮지 않는다(#66 §3 워커 — 결과 행은 run마다 1개).
    """
    await db.refresh(run, with_for_update=True)
    if run.status == "cancelled":
        await db.commit()
        return False
    return True


async def _fail_recheck_error(db: "AsyncSession", run_id: str) -> dict[str, Any]:
    """실행 직전 재검사의 예기치 않은 예외 → run을 failed로 닫는다(fail-closed, #68).

    예외 원문(DSN·SQL 등)은 노트북 공유 사용자에게 보이는 `run.error`에 싣지 않고 서버 로그로만
    보낸다. DB 오류로 트랜잭션이 aborted일 수 있으므로 롤백한 뒤, 롤백으로 만료된 ORM 객체를
    다시 읽지 않도록 조건부 UPDATE 한 문장으로 쓴다. 그 사이 취소·종료된 run은 덮어쓰지 않는다.
    """
    from sqlalchemy import update

    logger.exception("agent_run_recheck_error", run_id=run_id)
    await db.rollback()
    result = await db.execute(
        update(AgentRun)
        .where(
            AgentRun.id == uuid.UUID(run_id),
            AgentRun.status.in_(("running", "awaiting_approval")),
        )
        .values(status="failed", error=_RECHECK_FAILED, finished_at=datetime.now(UTC))
        .returning(AgentRun.id, AgentRun.trigger, AgentRun.chat_session_id)
    )
    row = result.one_or_none()
    closed = row is not None
    if row is not None and row.trigger == "chat":
        # 채팅 진입 run의 실패 결과 행 — 상태 전이와 한 커밋(#66)
        await write_entry_result(
            db,
            run_id=row.id,
            session_id=row.chat_session_id,
            content=entry_failed_text(_RECHECK_FAILED),
            no_result=True,
        )
    await db.commit()
    if closed:
        _publish(run_id, {"type": "status", "status": "failed", "error": _RECHECK_FAILED})
    return {"ok": False, "error": _RECHECK_FAILED}


async def _run(run_id: str) -> dict[str, Any]:
    from biznotes_core.models import AgentRunApproval
    from biznotes_core.services.agents.react.approval import CheckpointKeys
    from biznotes_core.services.agents.runtime import AgentRuntimeError, RunContext, execute_agent

    factory = get_session_factory()
    async with factory() as db:
        run = await db.get(AgentRun, uuid.UUID(run_id))
        if run is None:
            return {"ok": False, "error": "run not found"}
        if run.status == "cancelled":
            return {"ok": False, "error": "cancelled"}
        # [R2b/R2B-01] 중복 Celery 전달 방어 — 이 태스크는 (a) 최초 실행(queued) 또는
        # (b) 승인 재개(awaiting_approval + CAS 성공)에서만 진행한다. 이미 종료
        # (succeeded/failed)됐거나 실행 중(running)인 run에 중복 전달이 도착하면
        # 아무 일도 하지 않고 no-op으로 반환한다 — 그렇지 않으면 종료된 run을 다시
        # running으로 되돌려 게이트되지 않은 워크플로우를 처음부터 재실행(replay)한다.
        if run.status not in ("queued", "awaiting_approval"):
            return {"ok": True, "skipped": f"non_runnable_status:{run.status}"}

        # [R3-1c/R32CR-01] 위 status 확인은 read-then-write라 원자적이지 않다. 아래
        # `await`들 사이에 다른 워커가 같은 run을 집어가면 둘 다 `queued`를 보고
        # 실행해 도구 부수효과가 중복된다(실측 확인). `acks_late` 재전달로도
        # 발생하지만 reaper가 의도적으로 중복 전달을 만들므로 확률이 커졌다.
        # 최초 실행 경로는 여기서 원자적으로 소유권을 주장한다 — 재개 경로는
        # `_resolve_pending_approval`의 CAS가 같은 역할을 한다.
        was_queued = run.status == "queued"
        if was_queued:
            if not await _claim_queued_run(db, run.id):
                return {"ok": True, "skipped": "already_claimed"}
            # CAS는 raw UPDATE라 세션의 run 객체가 낡았다. 아래 분기들이 status를
            # 다시 읽으므로 DB 값(running)으로 맞춘다.
            await db.refresh(run)
        agent = await db.get(Agent, run.agent_id)
        # 대화형 run의 enabled 판정은 아래 재검사(코어 `assert_agent_execution_allowed` —
        # enabled ∨ 소유자 draft)에 맡긴다 — 편집기 「테스트 실행」이 미게시 정의를 돌린다(#79).
        # 무인 run과 알 수 없는 trigger는 큐잉 뒤 게시 취소된 에이전트를 여기서 막는다
        # (발화 시점 필터와 같은 기준).
        if agent is None or (not agent.enabled and run.trigger not in _INTERACTIVE_TRIGGERS):
            return await _fail_run(db, run, "에이전트를 찾을 수 없거나 비활성화됨")

        # 큐잉 이후 스킬이 비공개·비활성이 되었을 수 있다 — 실행 직전에 다시 검사한다
        # (biznotes-core 표준 계획 D8: 워커 실행 시점 재검사). 이미 고정된 version은
        # 그대로 실행되므로 즉시 차단 수단은 `enabled=false`다.
        # 재검사 자체가 HTTPException 밖의 예외(DB 오류 등)로 실패하면 run이 `running`에 남는다
        # — reaper는 `running`을 다루지 않는다(#68). 판정할 수 없으면 실행하지 않고 failed로 닫는다.
        chat_context: ChatRunContext | None = None
        try:
            recheck_user = await db.get(User, run.user_id)
            # 사용자 행이 사라졌으면 인가를 판정할 수 없다 — 열어 두지 않고 닫는다(fail-closed)
            execution_denied = recheck_user is None or not recheck_user.is_active
            if recheck_user is not None:
                from biznotes_core.services.agents.policy import assert_agent_execution_allowed
                from fastapi import HTTPException

                try:
                    await assert_agent_execution_allowed(db, recheck_user, agent)
                except HTTPException:
                    execution_denied = True
            if execution_denied or recheck_user is None:
                return await _fail_run(db, run, _DENIED_AT_START)

            # 노트북 권한 재검사(BL-217) — 큐잉 뒤(승인 대기 포함)의 공유 철회·강등을
            # 실행 직전에 막는다. 승인 CAS보다 앞이라 최초 실행과 승인 재개 모두에 적용되고,
            # 거부해도 승인을 소비하지 않는다. 트리거 행은 끄지 않는다(run에 트리거 id가
            # 없다) — 다음 발화는 발화 시점 관문이 막고 끈다.
            notebook_denial = await _notebook_denial(db, run, recheck_user)
            if notebook_denial is not None:
                logger.warning(
                    "agent_run_denied_at_start",
                    run_id=run_id,
                    trigger=run.trigger,
                    reason=notebook_denial,
                )
                return await _fail_run(db, run, _DENIED_AT_START)

            # 채팅 진입 재검사(#66, 코어 #85 계약) — 큐잉 뒤 스킬이 꺼지거나 공개가 내려가면 최초
            # 실행과 승인 재개 모두 시작 전에 막는다. 승인 CAS보다 앞이라 거부해도 승인을 소비하지
            # 않는다. 박제 version은 run 행의 값만 넘긴다(클라이언트 값이 아니다)
            if run.trigger == "chat":
                from biznotes_core.services.agents.chat_entry import assert_chat_entry_allowed

                try:
                    await assert_chat_entry_allowed(
                        db, recheck_user, agent, pinned_version_id=run.skill_version_id
                    )
                except HTTPException:
                    logger.warning(
                        "agent_run_denied_at_start",
                        run_id=run_id,
                        trigger=run.trigger,
                        reason="chat_entry_denied",
                    )
                    return await _fail_run(db, run, _DENIED_AT_START)
                # 질문과 그 앞 이력(D5) — 질문 행이 사라졌거나 세션과 맞지 않으면 실행하지 않는다
                try:
                    chat_context = await load_chat_run_context(db, run)
                except ChatRunContextInvalid:
                    logger.warning(
                        "agent_run_denied_at_start",
                        run_id=run_id,
                        trigger=run.trigger,
                        reason="chat_context_invalid",
                    )
                    return await _fail_run(db, run, _DENIED_AT_START)
        except Exception:
            return await _fail_recheck_error(db, run_id)

        # [phase-19-design.md §2.4·§2.5, CDX-D1] 재개 판단은 run.status가 아니라
        # "소비 대기 중인 해결된 approval이 있는가"(원자적 CAS)로 한다. approval이
        # 없으면(미해결 또는 중복 enqueue로 이미 소비됨) 이 태스크는 아무 일도 하지
        # 않고 즉시 종료 — run.status를 running으로 덮어쓰지 않는다.
        resolved_approval: AgentRunApproval | None = None
        definition = agent.definition
        resume_state: dict[str, Any] | None = None
        if run.status == "awaiting_approval":
            resolved_approval = await _resolve_pending_approval(db, run.id)
            if resolved_approval is None:
                return {"ok": True, "skipped": "unresolved_or_already_consumed"}
            if resolved_approval.checkpoint is None:
                # 이론상 도달 불가(pending 저장 시 항상 checkpoint를 채움) — 방어적 처리.
                return await _fail_run(db, run, "승인 체크포인트가 소실되었습니다")
            checkpoint = dict(resolved_approval.checkpoint)
            checkpoint[CheckpointKeys.REACT_APPROVAL_STATUS] = resolved_approval.status
            definition = _inject_resume_hint(
                agent.definition, checkpoint[CheckpointKeys.REACT_NODE_ID], checkpoint
            )
            resume_state = checkpoint[CheckpointKeys.GRAPH_STATE]
            # 체크포인트는 재개 소비 즉시 NULL화 — 감사 보존은 arguments_summary로만(SEC-D1).
            resolved_approval.checkpoint = None

        run.status = "running"
        run.started_at = run.started_at or datetime.now(UTC)
        # checkpoint NULL화(있다면)와 status=running 전이는 서로 의존하는 읽기가
        # 없으므로 한 트랜잭션으로 묶는다(효율성 리뷰 지적 — 불필요한 추가 round trip).
        await db.commit()
        _publish(run_id, {"type": "status", "status": "running", "progress": run.progress})

        async def on_progress(node_id: str, done: int, total: int) -> None:
            progress = int(done / total * 100)
            run.progress = progress
            await db.commit()
            _publish(
                run_id,
                {"type": "progress", "node": node_id, "progress": progress},
            )

        run_user = await db.get(User, run.user_id) if run.user_id else None
        source_ids = (
            [uuid.UUID(s) for s in run.input_source_ids] if run.input_source_ids else None
        )
        ctx = RunContext(
            run_id=run.id,
            notebook_id=run.notebook_id,
            db=db,
            params=run.params,
            # 실행 진입점 — 모든 run에 넘긴다. 코어는 `== "chat"`일 때만 동작을 바꾼다(정의의
            # `auto_approve_tools`를 쓰지 않는다 등, 코어 `react/approval.py`). 넘기지 않으면
            # 채팅에서 자동 승인이 일어난다(#66 §5 승인 경계)
            trigger=run.trigger,
            # 채팅 진입 run의 질문과 그 앞 이력(D5) — 코어 예약 이름공간으로만 넘긴다(params 아님)
            chat_message=chat_context.message if chat_context is not None else "",
            chat_history=chat_context.history if chat_context is not None else [],
            user_id=run.user_id,
            user_email=run_user.email if run_user else None,
            user_name=run_user.name if run_user else None,
            user_role=run_user.role if run_user else "user",
            source_ids=source_ids,
            on_progress=on_progress,
            agent_id=agent.id,
            # run 단위 요청 모델(#216, 코어 #359) — `model_id` 없는 llm·react 노드의 1차
            # 모델이다. `child()`가 loop·서브에이전트 본문에도 넘긴다. 승인 재개도 이 경로라
            # 같은 모델로 잇는다
            run_model_id=run.model_id,
        )
        if resolved_approval is not None:
            # [phase-19-design.md §2.3, CDX-D4] 승인 대기 이전에 누적된 RunContext
            # 가변 상태(토큰/비용/아티팩트/서브에이전트 카운터)를 복원 — 그렇지 않으면
            # 재개 후 예산 우회·사용량 누락·아티팩트 소실이 발생한다. 복원 로직은
            # approval 모듈 단일 소유(R2b) — worker는 세션/Celery 어댑터로만 남는다.
            from biznotes_core.services.agents.react import approval

            approval.restore_run_context(ctx, checkpoint)

        try:
            # 스킬 패키지 실행: run에 박제된 버전으로 builder 경로, 아니면 기존 kind 경로(§9.5).
            # 승인 재개도 같은 경로다 — 스킬 wrapper의 빈 정의로 execute_agent를 부르면 구 prompt
            # 경로가 돈다(biznotes-core 표준 계획 D6 재개).
            if run.skill_version_id is not None:
                from biznotes_core.services.skills.builder import execute_skill_package

                summary = await execute_skill_package(
                    ctx,
                    run.skill_version_id,
                    resume_checkpoint=checkpoint if resolved_approval is not None else None,
                )
            elif resume_state is not None:
                summary = await execute_agent(
                    ctx, agent.kind, definition, resume_state=resume_state
                )
            else:
                summary = await execute_agent(ctx, agent.kind, definition)

            if ctx.pending_approval is not None:
                # 재개된 실행이 도구 루프 중 또 다른 승인을 요구(다중 승인 라운드,
                # phase-19-design.md §2.5) — run을 다시 awaiting_approval로 전환.
                # 채팅 run은 그 사이 취소됐으면 승인 대기로 덮지 않는다(결과 행 「중지했습니다」와
                # 상태가 어긋나고 세션이 다시 잠긴다 — #66 리뷰)
                if run.trigger == "chat" and not await _lock_chat_run(db, run):
                    return {"ok": False, "error": "cancelled"}
                await _persist_pending_approval(db, run, ctx.pending_approval)
                return {"ok": True, "awaiting_approval": True}

            if run.trigger == "chat" and not await _lock_chat_run(db, run):
                return {"ok": False, "error": "cancelled"}
            run.status = "succeeded"
            run.progress = 100
            run.result_summary = summary
            run.artifact_keys = ctx.artifact_keys
            run.token_usage = ctx.usage
            run.finished_at = datetime.now(UTC)
            if run.trigger == "chat":
                # 결과 전문(500자 요약이 아니다) — 코어가 출력 노드에서 고른 본문, 비면 요약
                # (#66 AC-6)
                await write_entry_result(
                    db,
                    run_id=run.id,
                    session_id=run.chat_session_id,
                    content=ctx.chat_output_text or summary or "",
                    model_id=ctx.chat_used_model_id or run.model_id,
                    token_usage=_entry_usage(ctx),
                )
            await db.commit()
            _publish(
                run_id,
                {
                    "type": "status",
                    "status": "succeeded",
                    "progress": 100,
                    "artifact_keys": ctx.artifact_keys,
                    "result_summary": summary,
                },
            )
            return {"ok": True}
        except (AgentRuntimeError, Exception) as e:  # noqa: B014
            logger.exception("agent run failed", run_id=run_id)
            # run 오류는 노트북 공유 사용자에게 보인다 — 관리자 설정 원문(모델·공급자 이름, 공급자
            # 응답)은 코어 판정의 고정 문구로 바꾸고 원문은 서버 로그로만 보낸다(코어 v0.19.0 #216,
            # core-v0.20.0-upgrade §5.5 ①·D-12·HCI-8). 그래프 안 노드 실패는 코어가 이미 판정한
            # 「노드 [x] 실행 실패: …」 그대로이고, 사슬에 고정 문구 예외(`GatewayCallError` 등)가
            # 있으면 그 고정 문구가 된다
            failure = describe_node_failure(e)
            if failure.raw is not None:
                logger.warning(
                    "agent_run_failed_raw",
                    run_id=run_id,
                    error_code=failure.code,
                    error=failure.raw,
                )
            if run.trigger == "chat":
                # 실패 중 트랜잭션이 깨졌을 수 있다 — 되돌린 뒤 잠그고 다시 읽는다
                # (취소 API와 직렬화)
                await db.rollback()
                if not await _lock_chat_run(db, run):
                    return {"ok": False, "error": "cancelled"}
            await _fail_run(db, run, failure.message[:2000])
            _publish(run_id, {"type": "status", "status": "failed", "error": run.error})
            return {"ok": False, "error": failure.message}


@celery_app.task(name="app.workers.tasks.agent.run_agent", bind=True)
def run_agent(self, run_id: str) -> dict[str, Any]:
    return run_async(_run(run_id))


# 발행 실패로 고아가 된 queued run을 회수하기까지 기다리는 시간.
# 정상 경로에서는 커밋 직후 발행되므로 queued 체류는 초 단위다. 넉넉히 잡아
# 브로커가 잠시 느린 경우를 고아로 오인하지 않는다.
ORPHANED_RUN_THRESHOLD_MINUTES = 10


async def _orphaned_run_ids(db: "AsyncSession", cutoff: "datetime") -> list[uuid.UUID]:
    """발행이 누락된 것으로 보이는 run id 목록 — 두 종류가 있다.

    ① 최초 실행(`enqueue_agent_run`): 커밋 후 `apply_async` 실패 → `queued`로 정체.
    ② 승인 재개(`_resolve_approval`): 커밋 후 `apply_async` 실패 → **`awaiting_approval`
       그대로** 정체한다(`agents.py`는 재개 시 run.status를 바꾸지 않는다).

    ②를 "그냥 오래된 `awaiting_approval`"로 잡으면 **사람의 승인을 기다리는 정상
    run까지 재발행**한다. 구분 신호는 approval 행에 있다 — 이미 해결됐는데
    (`status in CONSUMABLE_STATUSES`) 아직 아무 워커도 소비하지 않은
    (`consumed is False`) 경우만 고아다. 이는 `_resolve_pending_approval`의 CAS
    조건과 동일하며, 재발행돼도 그 CAS가 하나만 통과시키므로 안전하다.
    """
    from biznotes_core.models import AgentRunApproval
    from biznotes_core.services.agents.react.approval import CONSUMABLE_STATUSES
    from sqlalchemy import and_, or_, select

    orphaned_resume = (
        select(AgentRunApproval.run_id)
        .where(
            AgentRunApproval.status.in_(CONSUMABLE_STATUSES),
            AgentRunApproval.consumed.is_(False),
        )
        .scalar_subquery()
    )
    rows = await db.execute(
        select(AgentRun.id).where(
            AgentRun.updated_at < cutoff,
            or_(
                AgentRun.status == "queued",  # ① 최초 실행 발행 누락
                and_(  # ② 승인 재개 발행 누락 (사람 대기 중인 run은 제외)
                    AgentRun.status == "awaiting_approval",
                    AgentRun.id.in_(orphaned_resume),
                ),
            ),
        )
    )
    return list(rows.scalars().all())


async def _reap_orphaned_runs() -> dict[str, Any]:
    """발행되지 못한 채 방치된 run을 재발행한다 (R3-1c).

    `enqueue_agent_run`·`_resolve_approval`은 run을 커밋한 뒤 `apply_async`로
    발행한다. 그 사이 프로세스가 죽거나 브로커가 응답하지 않으면 run은 영원히
    정체한다 — 회수 장치가 없었다(R3-0 설계 §2, HCI-R3-3(a)).

    재발행은 안전하다: `_run`이 `status not in ("queued","awaiting_approval")`이면
    no-op하고, 재개 경로는 approval 소비 CAS가 하나만 통과시킨다.

    `running` 상태는 건드리지 않는다 — 크래시로 정체된 run(stuck-running)은
    별개 문제이며 범위 밖이다(HCI-R3-6).

    재발행 전에 만료 승인 sweep(`_expire_stale_approvals`)을 먼저 돌린다 — 만료로 `failed`가
    된 run은 재발행 후보(`awaiting_approval`)에서 빠진다. sweep이 실패해도 기록만 하고 재발행은
    계속한다.
    """
    from datetime import timedelta

    cutoff = datetime.now(UTC) - timedelta(minutes=ORPHANED_RUN_THRESHOLD_MINUTES)
    factory = get_session_factory()
    try:
        expired = await _expire_stale_approvals(factory)
    except Exception:  # noqa: BLE001 — sweep 실패가 같은 tick의 고아 재발행(기존 안전망)을 막지 않는다
        # sweep은 자기 세션이라 실패는 그 세션 안에서 롤백되고 아래 재발행 세션에 번지지 않는다.
        # 다음 tick이 같은 만료 승인을 다시 집는다(CAS가 아직 pending인 행만 바꾼다). 리뷰 P2-02.
        logger.exception("승인 만료 sweep 실패 — 고아 재발행은 계속한다")
        expired = {"expired_approvals": 0, "failed_runs": 0}
    requeued: list[str] = []
    async with factory() as db:
        for run_id in await _orphaned_run_ids(db, cutoff):
            # [R32CR-03] **발행에 성공한 뒤에만** 마커를 갱신한다. 반대로 하면
            # 발행 실패 시 updated_at이 이미 갱신돼 threshold 동안 재시도 대상에서
            # 빠진다. 이 순서에서는 발행 성공 후 커밋이 실패하면 다음 tick이 다시
            # 발행하지만, `_claim_queued_run`의 CAS가 한 워커만 통과시키므로
            # 중복 실행되지 않는다 — reaper는 at-least-once가 옳다.
            task_id = str(uuid.uuid4())
            try:
                run_agent.apply_async(args=[str(run_id)], task_id=task_id, queue="agent")
            except Exception as e:  # noqa: BLE001 — 한 건의 실패가 나머지 회수를 막지 않는다
                logger.warning("고아 run 재발행 실패", run_id=str(run_id), error=str(e))
                continue
            # celery_task_id가 실제로 바뀌므로 행이 dirty가 되고 `onupdate`가
            # updated_at을 갱신한다 — 다음 tick의 cutoff 밖으로 밀려난다.
            # (status를 같은 값으로 재대입하면 net-change가 없어 UPDATE가 생략된다.)
            run = await db.get(AgentRun, run_id)
            if run is not None:
                run.celery_task_id = task_id
                await db.commit()
            requeued.append(str(run_id))
    if requeued:
        logger.warning("고아 agent run 재발행", count=len(requeued), run_ids=requeued)
    try:
        expired_seq_keys = await _expire_terminal_run_seq_keys(factory)
    except Exception:  # noqa: BLE001 — seq 키 정리 실패가 만료 승인·고아 재발행 결과를 버리지 않는다
        logger.exception("종료 run seq 키 만료 sweep 실패 — 다음 tick에 다시 시도한다")
        expired_seq_keys = 0
    return {"requeued": len(requeued), **expired, "expired_seq_keys": expired_seq_keys}


# 종료 run의 이벤트 seq 키 만료 sweep(#97, BL-174) — tick당 만료를 거는 키 수 상한과 SCAN 묶음 크기.
SEQ_KEY_SWEEP_LIMIT = 1000
_SEQ_KEY_SCAN_BATCH = 200


async def _expire_terminal_run_seq_keys(factory: Any) -> int:
    """terminal run의 TTL 없는 이벤트 seq 키에 보존 기간을 건다. 만료를 건 키 수를 돌려준다.

    코어 `initialize_run_event_sequence`가 run마다 seq 키(`sequence_key_for_agent_run`)를 만들고,
    만료(`run_event_sequence_retention_sec`)는 코어 `publish_run_event`의 terminal status 경로에서만
    건다. 제품 워커는 `publish(channel, dict)`로 발행해 그 경로를 타지 않으므로 키가 `TTL -1`로
    쌓인다. 키 이름·보존 기간·terminal 집합은 모두 코어 정의를 쓴다.

    - 대상은 Redis `SCAN MATCH`로 찾는다. DB에서 terminal run을 훑으면 이미 정리된 run을 매번 다시
      읽거나 오래 전 누적된 키를 놓친다. SCAN은 증분이라 Redis를 막지 않는다.
    - `TTL == -1`인 키 중 DB에서 run이 terminal로 확인된 것만 건드린다. 진행 중 run, DB에 없는
      run(다른 환경·갓 만든 run)의 키는 그대로 둔다 — 진행 중 run의 이벤트 순서를 깨지 않는다.
    - `EXPIRE … NX`라 확인과 설정 사이에 다른 경로가 만료를 걸었어도 그 TTL을 바꾸지 않는다.
    - tick당 `SEQ_KEY_SWEEP_LIMIT`개까지 건다. 만료를 건 키는 다음 SCAN에서 걸러지므로 남은 키는
      다음 tick이 이어 처리한다.
    """
    from biznotes_core.schemas.enums import TERMINAL_RUN_STATUSES
    from biznotes_core.services.events import sequence_key_for_agent_run
    from sqlalchemy import select

    pattern = sequence_key_for_agent_run("*")
    prefix, suffix = pattern.split("*")
    retention = settings.run_event_sequence_retention_sec

    async def expire_batch(client: AsyncRedis, keys: list[str], budget: int) -> int:
        async with client.pipeline(transaction=False) as pipe:
            for key in keys:
                pipe.ttl(key)
            ttls = await pipe.execute()
        candidates: dict[uuid.UUID, str] = {}
        for key, ttl in zip(keys, ttls, strict=True):
            if ttl != -1:
                continue
            try:
                candidates[uuid.UUID(key[len(prefix) : len(key) - len(suffix)])] = key
            except ValueError:
                continue
        if not candidates:
            return 0
        async with factory() as db:
            terminal = (
                (
                    await db.execute(
                        select(AgentRun.id).where(
                            AgentRun.id.in_(candidates),
                            AgentRun.status.in_(TERMINAL_RUN_STATUSES),
                        )
                    )
                )
                .scalars()
                .all()
            )
        targets = [candidates[run_id] for run_id in terminal][:budget]
        if not targets:
            return 0
        async with client.pipeline(transaction=False) as pipe:
            for key in targets:
                pipe.expire(key, retention, nx=True)
            results = await pipe.execute()
        return sum(1 for ok in results if ok)

    client = AsyncRedis.from_url(settings.redis_url, decode_responses=True)
    expired = 0
    try:
        batch: list[str] = []
        async for key in client.scan_iter(match=pattern, count=_SEQ_KEY_SCAN_BATCH):
            batch.append(key)
            if len(batch) >= _SEQ_KEY_SCAN_BATCH:
                expired += await expire_batch(client, batch, SEQ_KEY_SWEEP_LIMIT - expired)
                batch = []
                if expired >= SEQ_KEY_SWEEP_LIMIT:
                    break
        if batch and expired < SEQ_KEY_SWEEP_LIMIT:
            expired += await expire_batch(client, batch, SEQ_KEY_SWEEP_LIMIT - expired)
    finally:
        await client.aclose()
    if expired:
        logger.info("종료 run seq 키 만료 설정", count=expired, retention_sec=retention)
    return expired


async def _expire_stale_approvals(factory: Any) -> dict[str, int]:
    """기한이 지난 pending 승인을 만료시키고, 커밋한 **뒤에** 이벤트를 발행한다(코어 #153 A-7).

    무인 run(스케줄·웹훅·이벤트)이 승인 대기에 들어가면 같은 (agent, notebook)의 다음 트리거가
    `has_active_run`에 막힌다. 지금까지는 누가 승인·거부를 시도할 때만 만료를 평가했으므로
    reaper(beat) 주기마다 코어 `expire_stale_approvals`를 부른다(core-v0.11.0-upgrade HCI-7 ③).

    코어 함수는 approval CAS(`pending ∧ expires_at < now`)와 run CAS(`awaiting_approval`)를 한
    트랜잭션에서 하고 커밋하지 않는다. 발행은 커밋 뒤에만 한다 — 롤백된 전이를 알리지 않는다.

    - 만료된 승인마다 `approval_resolved(expired)` 1개.
    - `status(failed)`는 `run_failed=True`인 run에만, **run당 한 번**(반환 순서를 가정하지 않는다).
      이미 `cancelled`인 run은 코어가 바꾸지 않으므로(run_failed=False) failed 이벤트를 내지 않는다.
    - 발행은 best-effort다(Redis 실패가 이미 커밋된 만료나 뒤의 고아 회수를 막지 않는다).
      제품 승인 라우터의 지연 만료(`agents.py::_resolve_approval`)와 같은 이벤트 모양이다.
    """
    from biznotes_core.services.agent_runs import APPROVAL_EXPIRED_ERROR, expire_stale_approvals
    from sqlalchemy import select

    async with factory() as db:
        expired = await expire_stale_approvals(db)
        # 만료로 실패한 채팅 진입 run의 실패 결과 행 — 만료 전이와 한 커밋(#66)
        failed_ids = {item.run_id for item in expired if item.run_failed}
        if failed_ids:
            chat_runs = await db.execute(
                select(AgentRun.id, AgentRun.chat_session_id).where(
                    AgentRun.id.in_(failed_ids), AgentRun.trigger == "chat"
                )
            )
            for chat_run_id, chat_session_id in chat_runs.all():
                await write_entry_result(
                    db,
                    run_id=chat_run_id,
                    session_id=chat_session_id,
                    content=entry_failed_text(APPROVAL_EXPIRED_ERROR),
                    no_result=True,
                )
        await db.commit()

    failed_runs: set[uuid.UUID] = set()
    for item in expired:
        _publish_quietly(
            str(item.run_id),
            {
                "type": "approval_resolved",
                "approval_id": str(item.approval_id),
                "status": "expired",
            },
        )
        # 스키마(uq_agent_run_approvals_run_pending)상 run당 pending 1건 — 코어 제약 변경 대비 방어
        if item.run_failed and item.run_id not in failed_runs:
            failed_runs.add(item.run_id)
            _publish_quietly(
                str(item.run_id),
                {"type": "status", "status": "failed", "error": APPROVAL_EXPIRED_ERROR},
            )
    if expired:
        logger.warning(
            "만료된 승인 정리",
            approvals=len(expired),
            failed_runs=sorted(str(r) for r in failed_runs),
        )
    return {"expired_approvals": len(expired), "failed_runs": len(failed_runs)}


def _publish_quietly(run_id: str, event: dict[str, Any]) -> None:
    """이미 커밋된 상태 전이의 알림 — 발행 실패는 기록만 하고 삼킨다(폴링이 정본이다)."""
    try:
        _publish(run_id, event)
    except Exception as e:  # noqa: BLE001 — 알림 실패가 sweep·고아 회수를 막지 않는다
        logger.warning("승인 만료 이벤트 발행 실패", run_id=run_id, error=str(e))


@celery_app.task(name="app.workers.tasks.agent.reap_orphaned_runs")
def reap_orphaned_runs() -> dict[str, Any]:
    """주기 안전망 (beat) — 만료 승인 정리(코어 #153 A-7) 뒤 발행 누락 run을 재발행한다."""
    return run_async(_reap_orphaned_runs())


# 미사용 import 경고 방지용 재노출 (소스 이벤트 채널과 동일 헬퍼 사용처)
__all__ = ["reap_orphaned_runs", "run_agent", "publish_source_event"]
