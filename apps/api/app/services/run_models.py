"""run에 실제로 응답한 모델과 폴백 여부 — 노드 실행 로그의 `output_data`에서 꺼낸다(#216·#234).

규칙은 여기 한 곳에 둔다. 상세 「실행 로그」(`api/v1/run_logs.py` `_node_model_fields`)는 행마다,
「최근 결과」 목록(`api/v1/agents.py` `list_agent_runs`)은 run마다 이 규칙을 쓴다.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from biznotes_core.models import ExecutionLog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.agent import NodeModelOut

# 사용 모델·폴백 여부를 출력에 남기는 행 — 성공한 llm·react_agent 노드(코어 `nodes/llm.py`·
# `react/loop.py`, #359). 다른 노드 출력의 같은 이름 키는 사용자 데이터일 수 있어 읽지 않는다
MODEL_NODE_TYPES = frozenset({"llm", "react_agent"})
MODEL_LOG_STATUS = "succeeded"


def reports_model(status: str, node_type: str) -> bool:
    """이 실행 로그 행이 사용 모델·폴백 여부를 남기는 자리인가."""
    return status == MODEL_LOG_STATUS and node_type in MODEL_NODE_TYPES


def node_model(raw_model: Any, raw_fallback: Any) -> tuple[NodeModelOut | None, bool | None]:
    """`output_data`의 `model`·`fallback_used` 원값을 검증한다 — 모양이 다르면 그 값만 None이다.

    옛 로그도 응답을 깨지 않는다. 폴백은 bool만 받는다 — 검증기에 맡기면 "yes" 같은 문자열도
    True가 된다.
    """
    try:
        model = NodeModelOut.model_validate(raw_model)
    except ValidationError:
        model = None
    return model, raw_fallback if isinstance(raw_fallback, bool) else None


@dataclass
class RunModels:
    """run 하나에 응답한 모델(처음 응답한 순서, 중복 없음)과 폴백 여부."""

    used_models: list[NodeModelOut] = field(default_factory=list)
    fallback_used: bool = False

    def add(self, model: NodeModelOut | None, fallback: bool | None) -> None:
        # 웹 상세 머리(`run-detail-view.tsx` `usedModels`)와 같다 — 모델이 없는 행은 폴백도
        # 세지 않는다
        if model is None:
            return
        if all(seen.id != model.id for seen in self.used_models):
            self.used_models.append(model)
        if fallback:
            self.fallback_used = True


async def load_run_models(
    db: AsyncSession, run_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, RunModels]:
    """run들의 사용 모델·폴백 여부를 실행 로그 한 번의 조회로 모은다(#234).

    모델 행이 없는 run은 결과에서 빠진다.

    `output_data` 전체가 아니라 두 경로만 읽는다 — 노드 출력 원문(소스 발췌 등)이 크다. JSON 객체가
    아닌 출력은 경로가 NULL이라 무시된다. 순서는 상세 「실행 로그」와 같은 시작 시각 순이다.
    """
    if not run_ids:
        return {}
    output = ExecutionLog.output_data
    rows = await db.execute(
        select(ExecutionLog.run_id, output["model"], output["fallback_used"])
        .where(
            ExecutionLog.run_id.in_(run_ids),
            ExecutionLog.status == MODEL_LOG_STATUS,
            ExecutionLog.node_type.in_(MODEL_NODE_TYPES),
        )
        .order_by(ExecutionLog.started_at, ExecutionLog.id)
    )
    by_run: dict[uuid.UUID, RunModels] = {}
    for run_id, raw_model, raw_fallback in rows.all():
        by_run.setdefault(run_id, RunModels()).add(*node_model(raw_model, raw_fallback))
    return by_run
