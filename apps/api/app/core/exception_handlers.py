"""전역 도메인 예외 → HTTP 응답 매핑 (R1-1).

라우터마다 흩어져 있던 `try/except DomainError → raise HTTPException` 매핑을
한 곳으로 모은다. 도메인 예외가 라우터 밖으로 그대로 전파되면 이 핸들러가
일관된 상태코드와 `{"detail": str(exc)}` 본문(FastAPI 기본 형태와 동일)으로
변환한다.

원칙:
- 라우터가 예외를 **다르게** 처리하던 곳(그레이스풀 스킵·SSE 이벤트 전달·커스텀
  메시지 포맷 등)은 그대로 둔다. 전역 핸들러는 "잡지 않고 전파되면 이렇게 매핑"
  이라는 기본값만 제공하므로, 라우터의 명시적 처리가 항상 우선한다.
- GatewayError는 대부분 502였고 chat resolve_model만 503이었으나, 전역 매핑은
  **502로 통일**한다(2026-07-07 인간 승인, HCI). 스트림 내부의 GatewayError는
  라우터가 SSE error 이벤트로 처리하므로 이 핸들러에 닿지 않는다.
- 예외 문자열에 비신뢰 데이터가 섞이는 타입은 `str(exc)` 대신 고정 공개 문구를 본문으로
  쓴다(`_PUBLIC_DETAIL`). 코어 리랭크 파서 오류는 리랭커 서버가 돌려준 값을 메시지에 담는다
  (core-v0.15.0-upgrade PR-01·HCI-6). 다른 예외의 `str(exc)` 본문은 그대로다.
"""

import math
from typing import Any

from biznotes_core.core.ssrf import SSRFBlockedError
from biznotes_core.services.agent_runs import (
    ActiveChatRunExists,
    ChatRunLinkInvalid,
    ModelUnavailable,
    ParamsInvalid,
    RunNotFinished,
)
from biznotes_core.services.gateway.discovery import DiscoveryError
from biznotes_core.services.gateway.llm import GatewayCallError, GatewayError
from biznotes_core.services.rerank import RerankUnavailableError
from biznotes_core.services.skills.parser import SkillParseError
from biznotes_core.services.skills.service import SkillError
from biznotes_core.services.skills.validator import SkillValidationError
from biznotes_core.services.usage_guard import UsageLimitExceeded
from biznotes_core.services.websearch import SearchProviderError
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

# 도메인 예외 → HTTP 상태코드. 각 값은 라우터가 기존에 반환하던 코드를 보존한다
# (GatewayError만 502로 통일 — 위 docstring 참조).
_STATUS_MAP: dict[type[Exception], int] = {
    UsageLimitExceeded: status.HTTP_429_TOO_MANY_REQUESTS,
    SSRFBlockedError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    ParamsInvalid: status.HTTP_422_UNPROCESSABLE_CONTENT,
    # 진행 중 run 숨기기(코어 #371 D1) — 취소의 「이미 종료된 실행」과 같은 상태 충돌이다(#220)
    RunNotFinished: status.HTTP_409_CONFLICT,
    # run에 고른 모델을 쓸 수 없다(#216, 코어 #359) — 없는 모델과 꺼진 모델이 같은 고정 문구다
    ModelUnavailable: status.HTTP_422_UNPROCESSABLE_CONTENT,
    # 채팅 진입 run(#66) — 세션당 활성 run 1개(코어 `uq_agent_runs_chat_active`)를 넘는 진입·일반
    # 메시지는 상태 충돌이다. 세션·질문 행 연결 오류는 요청 내용 오류다
    # (코어 `_validate_chat_run_link`)
    ActiveChatRunExists: status.HTTP_409_CONFLICT,
    ChatRunLinkInvalid: status.HTTP_422_UNPROCESSABLE_CONTENT,
    SkillValidationError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    SkillParseError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    SkillError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    GatewayError: status.HTTP_502_BAD_GATEWAY,
    DiscoveryError: status.HTTP_502_BAD_GATEWAY,
    SearchProviderError: status.HTTP_502_BAD_GATEWAY,
    RerankUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
}

# 정적 채팅 SSE `error` 이벤트와 503 본문이 함께 쓰는 공개 문구 — 모델명·URL·서버 응답을 담지 않는다
RERANK_UNAVAILABLE_DETAIL = (
    "리랭커를 사용할 수 없어 답변을 만들지 못했습니다. "
    "관리자에게 RAG 리랭커 설정 확인을 요청하세요."
)

# 예외 메시지 대신 본문으로 쓸 고정 공개 문구(모듈 docstring 참조). 모델 호출 실패
# (`GatewayCallError` — `GatewayError` 하위, 502 유지)는 모델명·공급자 응답 원문을 담으므로 코어의
# 고정 문구를 쓴다(코어 v0.19.0 #216, core-v0.20.0-upgrade §5.5 ③·HCI-8). `GatewayError` 본체는
# 모델 해석·입력 검증 안내라 원문 그대로다(MRO에서 하위 클래스가 먼저 맞는다)
_PUBLIC_DETAIL: dict[type[Exception], str] = {
    RerankUnavailableError: RERANK_UNAVAILABLE_DETAIL,
    GatewayCallError: GatewayCallError.node_log_message,
}


def _json_safe(value: Any) -> Any:
    """JSON에 없는 값(`NaN`·`±Infinity`)을 문자열로 바꾼다 — 나머지는 그대로다."""
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


async def _handle_request_validation(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI 기본 422와 같은 본문(`{"detail": errors}`)이되, 비유한 수를 문자열로 바꾼다(#244).

    요청 본문의 `NaN`·`Infinity`는 Python `json`이 읽어 float로 넘긴다. 검증 오류의 `input`에 그
    값이 실리면 기본 핸들러의 `JSONResponse`(`allow_nan=False`)가 직렬화에 실패해 500이 된다.
    `input`을 남길지 버릴지는 #93·#96에서 정한다 — 여기서는 형태를 바꾸지 않는다.
    """
    if not isinstance(exc, RequestValidationError):  # 등록한 타입 밖은 오지 않는다
        raise exc
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": _json_safe(jsonable_encoder(exc.errors()))},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """FastAPI 앱에 도메인 예외 핸들러를 등록한다."""

    async def _handle(request: Request, exc: Exception) -> JSONResponse:
        # 등록된 정확한 타입이 없으면 MRO를 따라 가장 가까운 등록 조상으로 매핑한다
        # (예: TranscriptUnavailableError → ParseError는 미등록이므로 500 유지되지만,
        #  향후 서브클래스가 등록 조상을 갖도록 확장돼도 올바른 코드를 반환).
        mro = type(exc).__mro__
        code = next(
            (_STATUS_MAP[t] for t in mro if t in _STATUS_MAP),
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
        # 공개 문구 타입은 예외 문자열을 아예 평가하지 않는다 — `next()`의 기본값 인자로 두면
        # 공개 문구를 찾기 전에 먼저 평가된다(리뷰 P2-03)
        detail = next((_PUBLIC_DETAIL[t] for t in mro if t in _PUBLIC_DETAIL), None)
        if detail is None:
            detail = str(exc)
        return JSONResponse(status_code=code, content={"detail": detail})

    for exc_type in _STATUS_MAP:
        app.add_exception_handler(exc_type, _handle)
    app.add_exception_handler(RequestValidationError, _handle_request_validation)
