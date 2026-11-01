"""실행 로그 — 단일 run의 노드별 execution_log 드릴다운.

본인 실행이거나 노트북 읽기 권한이 있어야 본다. 채팅 진입 run은 실행자와 admin만 본다.
"""

import uuid
from datetime import datetime
from typing import Any

from biznotes_core.api.deps import get_current_user
from biznotes_core.core.db import get_db
from biznotes_core.models import AgentRun, ExecutionLog, Notebook, User
from biznotes_core.services.authz import assert_can_read_run, is_private_chat_run
from biznotes_core.services.notebooks import get_permission
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.agent import NodeModelOut
from app.services.run_models import node_model, reports_model

router = APIRouter(tags=["agent-runs"])


class NodeModelAttemptOut(NodeModelOut):
    """모든 후보가 실패한 노드가 시도한 모델 하나 — 코어 `model_attempts`(#359).

    공급자·원문은 없다.
    """

    error_code: str


class ExecutionLogOut(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    node_id: str
    node_type: str
    status: str
    error_message: str | None
    # 노드 실패 코드(코어 v0.19.0 #216 `node_errors.NODE_ERROR_CODES` — 더하기만 한다). 성공·
    # 코드 없는 실패는 None이다. 웹은 이 코드로 문구를 고른다(`lib/node-error-labels.ts`).
    # 기본값을 두지 않는다 — 엔티티 select라 늘 채워지고, 생성 타입이 필수 `string | null`이어야
    # 수동 미러 `ExecutionLog`와 드리프트 0이다(core-v0.20.0-upgrade §5.5)
    error_code: str | None
    token_usage: dict[str, Any] | None
    cost_usd: float | None
    duration_ms: int | None
    started_at: datetime | None
    finished_at: datetime | None
    # 사용 모델·폴백 여부(성공한 llm·react_agent 행)와 시도한 모델(실패 행) — 제품 #216.
    # 코어가 `output_data`에 남긴 값만 꺼내고(`_node_model_fields`), 해당 없으면 None이다.
    # 기본값을 두지 않는 이유는 `error_code`와 같다
    model: NodeModelOut | None
    fallback_used: bool | None
    model_attempts: list[NodeModelAttemptOut] | None

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def _from_log_row(cls, data: Any) -> Any:
        """실행 로그 행이면 열 값에 `output_data`에서 꺼낸 모델 필드를 더한다. `output_data` 자체는
        내보내지 않는다 — 노드 출력 원문(소스 발췌 등)이 담긴다."""
        if not isinstance(data, ExecutionLog):
            return data
        values = {
            name: getattr(data, name) for name in cls.model_fields if name not in _MODEL_FIELDS
        }
        return values | _node_model_fields(data)


_MODEL_FIELDS = ("model", "fallback_used", "model_attempts")


def _node_model_fields(log: ExecutionLog) -> dict[str, Any]:
    """`output_data`에서 사용 모델·폴백 여부·시도한 모델을 꺼낸다(#216) — 코어가 남긴 자리만 본다.

    - 성공한 llm·react_agent 행: `model`(`{id, display_name}`)·`fallback_used`(bool) — 규칙은
      run 목록(#234)과 함께 `app.services.run_models`에 있다
    - 실패 행: `model_attempts` — 실패 로그의 `output_data`는 예외가 고른 공개 값뿐이다(코어
      `node_log_output`). 모양이 다른 항목(절단 표시 `… (총 N개)` 등)은 뺀다
    모양이 다르면 그 필드만 None이다(옛 로그 포함). 응답을 깨지 않는다.
    """
    fields: dict[str, Any] = dict.fromkeys(_MODEL_FIELDS)
    output = log.output_data
    if not isinstance(output, dict):
        return fields
    if reports_model(log.status, log.node_type):
        fields["model"], fields["fallback_used"] = node_model(
            output.get("model"), output.get("fallback_used")
        )
    elif log.status == "failed" and isinstance(output.get("model_attempts"), list):
        attempts: list[NodeModelAttemptOut] = []
        for item in output["model_attempts"]:
            try:
                attempts.append(NodeModelAttemptOut.model_validate(item))
            except ValidationError:
                continue
        fields["model_attempts"] = attempts or None
    return fields


@router.get("/agent-runs/{run_id}/logs", response_model=list[ExecutionLogOut])
async def agent_run_logs(
    run_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ExecutionLogOut]:
    """단일 실행의 노드별 execution_log (드릴다운)."""
    run = await db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    # 채팅 진입 run은 실행자와 admin만 본다 — 타인은 404(코어 `assert_can_read_run`, #66)
    if is_private_chat_run(run):
        await assert_can_read_run(db, user, run)
    # 본인 실행 또는 노트북 읽기 권한
    if run.user_id != user.id:
        nb = await db.get(Notebook, run.notebook_id)
        perm = await get_permission(db, nb, user) if nb else None
        if perm is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "권한이 없습니다")

    rows = (
        await db.execute(
            select(ExecutionLog)
            .where(ExecutionLog.run_id == run_id)
            .order_by(ExecutionLog.started_at)
        )
    ).scalars().all()
    return [ExecutionLogOut.model_validate(r) for r in rows]
