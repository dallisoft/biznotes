"""게이트웨이 리랭커 호출 — Cohere 규격 `POST {api_base}/rerank` (#209).

`api_base`는 등록된 base_url 그대로다(관례상 `/v1`까지 포함). 요청은 `{model, query,
documents, top_n}`, 응답은 `results[{index, relevance_score}]`다. `relevance_score`는 이미
[0,1]이라(서버가 sigmoid를 건다) 다시 정규화하지 않는다.

리랭크는 채팅 첫 토큰 경로에 있다. 그래서 재시도는 짧은 벽시계 예산(`timeout`) 안에서만 하고,
타임아웃은 다시 보내지 않는다(임베딩 질의 경로와 같은 규칙). 예산은 **근사 상한**이다 — 요청마다
남은 시간을 httpx timeout으로 넘기는데, httpx는 연결·읽기 같은 단계마다 따로 재므로 한 요청이
단계 합만큼 더 걸릴 수 있다. 재시도 분류는
`embedding_spaces`의 규칙(429·503은 `Retry-After`, 5xx·연결 오류는 백오프)을 그대로 쓴다.
실패는 `RerankUnavailableError`로 올린다 — 하이브리드 점수로 조용히 넘어가지 않는다.

호출은 `retrieve_context`의 검색 스레드에서 돌므로 동기다. ORM 객체 대신 `RerankCall`
스냅샷을 받는다.
"""

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from biznotes_core.services.rerank import RerankUnavailableError

logger = structlog.get_logger()

# 총 요청 수 상한(첫 요청 포함, 타임아웃 재전송 없음)
_MAX_ATTEMPTS = 3
# 남은 시간이 이보다 짧으면 새 요청을 보내지 않는다(초)
_MIN_REQUEST_SECONDS = 0.5


@dataclass(frozen=True)
class RerankCall:
    """게이트웨이 리랭크 호출에 필요한 값만 담은 스냅샷."""

    api_base: str
    model: str
    display_name: str
    timeout: float
    # repr에 키가 찍히지 않게 한다 — 예외·로그에 스냅샷이 섞여도 새지 않는다
    api_key: str | None = field(default=None, repr=False)
    # 사용량 원장(#366)에 남길 모델 행 id·프로바이더 유형 — 기본값이라 기존 생성자 호출은 그대로다
    model_id: uuid.UUID | None = None
    provider_type: str | None = None


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _http_client(timeout: float) -> httpx.Client:
    # 리다이렉트를 따라가지 않는다 — 키가 다른 호스트로 넘어가지 않게 의도를 고정한다
    return httpx.Client(timeout=timeout, follow_redirects=False)


def _parse(body: Any, count: int) -> list[float]:
    """응답을 입력 순서의 점수 목록으로. 형태가 어긋나면 `ValueError`.

    메시지에는 위반 종류와 타입만 담는다 — 서버가 보낸 값은 길이 제한 없이 관리자 응답·
    경고 로그로 나가므로 싣지 않는다(#217, `_describe`와 같은 정책). 우리가 검증을 마친
    index와 건수는 담아도 된다.
    """
    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list):
        raise ValueError("응답에 results 목록이 없습니다")
    scores: list[float | None] = [None] * count
    for item in results:
        if not isinstance(item, dict):
            raise ValueError("results 항목이 객체가 아닙니다")
        index = item.get("index")
        score = item.get("relevance_score")
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError(f"잘못된 index 형식({type(index).__name__})")
        if not 0 <= index < count:
            raise ValueError("index 범위 밖")
        if scores[index] is not None:
            raise ValueError(f"중복 index: {index}")
        if isinstance(score, bool) or not isinstance(score, int | float):
            raise ValueError(f"잘못된 relevance_score 형식({type(score).__name__})")
        value = float(score)
        # [0,1] 밖이면 계약 위반이다 — 잘라내면 임계값 판정이 조용히 틀린다
        if math.isnan(value) or not 0.0 <= value <= 1.0:
            raise ValueError("relevance_score가 [0,1] 밖입니다")
        scores[index] = value
    missing = sum(1 for s in scores if s is None)
    if missing:
        raise ValueError(f"점수가 빠진 문서 {missing}건")
    return [s for s in scores if s is not None]


def rerank(call: RerankCall, query: str, documents: list[str]) -> list[float]:
    """문서별 관련도 점수([0,1], 입력 순서). 실패하면 `RerankUnavailableError`."""
    from biznotes_core.services.gateway.retry import _backoff, _classify

    if not documents:
        return []
    url = f"{call.api_base.rstrip('/')}/rerank"
    headers = {"Authorization": f"Bearer {call.api_key}"} if call.api_key else {}
    payload = {
        "model": call.model,
        "query": query,
        "documents": documents,
        "top_n": len(documents),
    }
    deadline = time.monotonic() + call.timeout
    last: str = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining < _MIN_REQUEST_SECONDS:
            break
        try:
            with _http_client(remaining) as client:
                resp = client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                body = resp.json()
        except Exception as exc:  # noqa: BLE001 - 분류한 뒤 재시도하거나 관찰 가능한 오류로 올린다
            failure = _classify(exc)
            last = _describe(exc)
            logger.warning(
                "게이트웨이 리랭크 실패",
                model=call.display_name,
                attempt=attempt,
                kind=failure.kind,
                error=last,
            )
            if failure.kind != "retry" or attempt == _MAX_ATTEMPTS:
                break
            wait = failure.retry_after if failure.retry_after is not None else _backoff(attempt)
            # 기다린 뒤 요청할 시간이 남지 않으면 기다리지 않는다
            if wait + _MIN_REQUEST_SECONDS > deadline - time.monotonic():
                break
            _sleep(wait)
            continue
        try:
            return _parse(body, len(documents))
        except ValueError as exc:
            # 응답 형식 위반은 다시 보내도 같다
            raise RerankUnavailableError(
                f"리랭커 {call.display_name} 응답이 계약과 다릅니다: {exc}"
            ) from exc
    raise RerankUnavailableError(f"리랭커 {call.display_name}를 호출하지 못했습니다: {last}")


def _describe(exc: BaseException) -> str:
    """오류 요약 — 응답 본문·헤더는 담지 않는다(키·내부 정보가 되돌아올 수 있다)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    return type(exc).__name__


__all__ = ["RerankCall", "rerank"]
