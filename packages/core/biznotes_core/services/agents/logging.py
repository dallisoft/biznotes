"""에이전트 실행 로깅 부수효과 + 도구 인자 마스킹(R2-3 runtime.py에서 추출).

동작 변경 없음 — runtime.py는 이 모듈을 재수출하고, 실행기·react 루프가 여기의
_log_node_start/_log_node_finish/_truncate_output/_mask_tool_args를 참조한다
(R0-6 특성화 계약 및 test_react_agent monkeypatch seam 유지).

참고: 모듈명이 stdlib `logging`과 같지만 항상 절대 경로
`app.services.agents.logging`으로 임포트되어 셰도잉 위험이 없다.
"""

import uuid
from datetime import datetime
from typing import Any

import structlog

logger = structlog.get_logger()


def _truncate_output(data: dict[str, Any], limit: int = 2000) -> dict[str, Any]:
    """output_data 영속화 시 긴 문자열/리스트를 절단 — 로그 JSONB 비대화 방지."""
    out: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, str) and len(v) > limit:
            out[k] = v[:limit] + f"… (총 {len(v)}자)"
        elif isinstance(v, list) and len(v) > 20:
            out[k] = v[:20] + [f"… (총 {len(v)}개)"]
        else:
            out[k] = v
    return out


async def _log_node_start(
    run_id: uuid.UUID,
    node_id: str,
    node_type: str,
    input_data: dict[str, Any],
    started_at: "datetime",
) -> uuid.UUID | None:
    """ExecutionLog 'running' 행을 격리 세션에 기록 (best-effort).

    워크플로우 트랜잭션과 분리해 (a) FK/오류로 본 실행이 중단되지 않고,
    (b) 병렬 노드의 동시 커밋 충돌을 피하며, (c) 실행 실패 시에도 로그가 남는다.
    실제 AgentRun이 없는 단위 테스트에서는 FK 위반으로 조용히 None 반환.
    """
    from biznotes_core.core.db import async_session_factory
    from biznotes_core.models.execution_log import ExecutionLog

    log_id = uuid.uuid4()
    try:
        async with async_session_factory() as s:
            s.add(
                ExecutionLog(
                    id=log_id,
                    run_id=run_id,
                    node_id=node_id,
                    node_type=node_type,
                    status="running",
                    input_data=input_data,
                    started_at=started_at,
                )
            )
            await s.commit()
        return log_id
    except Exception as e:
        logger.debug("execution_log start skipped", node_id=node_id, error=str(e))
        return None


async def _log_node_finish(
    log_id: uuid.UUID | None,
    *,
    status: str,
    finished_at: "datetime",
    output_data: dict[str, Any] | None = None,
    error_message: str | None = None,
    token_usage: dict[str, int] | None = None,
    cost_usd: float | None = None,
    duration_ms: int | None = None,
    error_code: str | None = None,
) -> bool:
    """앞서 생성한 ExecutionLog 행을 종료 상태로 갱신 (best-effort).

    기록했으면 True, 행이 없거나(시작 기록 실패 포함) 오류면 False — 예외는 삼킨다.
    반환값을 무시하던 호출자는 그대로 호환된다(#23 D2).
    """
    if log_id is None:
        return False
    from decimal import Decimal

    from biznotes_core.core.db import async_session_factory
    from biznotes_core.models.execution_log import ExecutionLog

    try:
        async with async_session_factory() as s:
            log = await s.get(ExecutionLog, log_id)
            if log is None:
                return False
            log.status = status
            log.finished_at = finished_at
            log.output_data = output_data
            log.error_message = error_message
            log.error_code = error_code
            log.token_usage = token_usage
            log.cost_usd = Decimal(str(round(cost_usd, 6))) if cost_usd else None
            log.duration_ms = duration_ms
            await s.commit()
        return True
    except Exception as e:
        logger.debug("execution_log finish skipped", log_id=str(log_id), error=str(e))
        return False


# 도구 인자 로깅 시 마스킹 대상 키 패턴(부분 일치) — 시크릿 누출 방지(T7·SEC-07).
# `cookie`는 직접 http 노드의 승인 카드(#153)에 헤더가 그대로 실리므로 더한다(`set-cookie` 포함).
_SECRET_ARG_KEYS = (
    "authorization",
    "token",
    "key",
    "password",
    "secret",
    "credential",
    "cookie",
)


def _mask_value(v: Any) -> Any:
    """중첩 dict/list를 재귀 순회하며 시크릿 키를 마스킹(리스트 내부 포함)."""
    if isinstance(v, dict):
        return _mask_tool_args(v)
    if isinstance(v, list):
        return [_mask_value(item) for item in v]
    return v


def _mask_tool_args(args: dict[str, Any]) -> dict[str, Any]:
    """tool-call 인자를 로깅/영속화하기 전 시크릿 패턴 키를 마스킹.

    dict뿐 아니라 리스트 내부 dict(예: `{"headers": [{"Authorization": ...}]}`)도
    재귀 순회한다(SEC-07·C5).
    """
    if not isinstance(args, dict):
        return args
    masked: dict[str, Any] = {}
    for k, v in args.items():
        kl = str(k).lower()
        if any(p in kl for p in _SECRET_ARG_KEYS):
            masked[k] = "***"
        else:
            masked[k] = _mask_value(v)
    return masked
