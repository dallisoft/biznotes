"""게이트웨이 HTTP 호출의 일시 오류 분류와 백오프 — 임베딩(#194)·리랭크(#209)·LLM 폴백(#380)이
같이 쓴다.

`embedding_spaces`는 모듈을 읽을 때 fastembed를 import한다. 리랭크 클라이언트가 그
스택 없이 같은 규칙을 쓰도록 분류 함수만 이 가벼운 모듈에 둔다. `embedding_spaces`는 같은
이름을 다시 노출한다(기존 테스트가 그 모듈 이름으로 본다).
"""

import random
import ssl
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Literal

# Retry-After를 따르는 상태 코드와, 백오프로 다시 시도하는 서버 오류
_RATE_LIMITED_STATUSES = frozenset({429, 503})
_SERVER_ERROR_STATUSES = frozenset({500, 502, 504})
# 키·권한 거부 — 요청이 아니라 프로바이더 설정 문제다(#380)
_AUTH_STATUSES = frozenset({401, 403})
# 백오프 밑수·상한(초)
_BACKOFF_BASE_SECONDS = 2.0
_BACKOFF_CAP_SECONDS = 60.0


@dataclass(frozen=True)
class _Failure:
    kind: str  # "timeout" | "retry" | "gate" | "permanent"
    retry_after: float | None = None
    # 사슬에서 읽은 첫 HTTP 상태(#380). 판정 비교에는 넣지 않는다
    status: int | None = field(default=None, compare=False)
    # 판정 근거(HTTP 응답·전송 오류·타임아웃·설정 오류)를 사슬에서 찾았는지(#380). 못 찾은
    # "permanent"만 거짓이다
    grounded: bool = field(default=True, compare=False)


def _parse_retry_after(value: str | None) -> float | None:
    """`Retry-After` — 초 또는 HTTP 날짜. 읽을 수 없으면 None."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """예외 사슬 — 바깥부터 `__cause__`, 없으면 `__context__`를 따라간다. 순환은 한 번만 돈다."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


def _classify(exc: BaseException) -> _Failure:
    """예외 체인의 HTTP 응답·전송 오류로 판정한다.

    LiteLLM 예외 유형은 프로바이더마다 다르다. 예를 들어 `lm_studio/`는 타임아웃·연결 거부를
    `InternalServerError`로 올리고, 키 누락 같은 클라이언트 측 오류도 500으로 올린다. 그래서
    유형 대신 체인 안의 실제 `httpx` 응답과 전송 오류를 본다.
    """
    import httpx
    import litellm

    status: int | None = None
    retry_after: float | None = None
    transport = False
    for cur in _chain(exc):
        if isinstance(cur, (TimeoutError, httpx.TimeoutException, litellm.Timeout)):
            return _Failure("timeout")
        if isinstance(cur, (httpx.UnsupportedProtocol, ssl.SSLCertVerificationError)):
            # 설정 오류 — base_url 스킴·인증서는 기다려도 바뀌지 않는다. DNS 실패는 compose
            # 컨테이너 재시작 중에도 나므로 일시 오류로 둔다
            return _Failure("permanent")
        if isinstance(cur, httpx.HTTPStatusError):
            if status is None:
                status = cur.response.status_code
                retry_after = _parse_retry_after(cur.response.headers.get("retry-after"))
        elif isinstance(cur, (httpx.TransportError, ConnectionError)):
            transport = True
    if status in _RATE_LIMITED_STATUSES:
        return _Failure("retry", retry_after, status=status)
    if status in _SERVER_ERROR_STATUSES or (status is None and transport):
        return _Failure("retry", status=status)
    return _Failure("permanent", status=status, grounded=status is not None)


def classify_call_failure(exc: BaseException) -> Literal["transient", "auth", "permanent"]:
    """모델 호출 실패를 재시도·서킷 관점으로 나눈다(#380) — LLM 폴백 경로가 쓴다.

    - `transient`: 타임아웃·429·503·5xx·연결 오류. 다시 보내면 결과가 달라질 수 있다.
    - `auth`: 401·403. 다시 보내도 같지만 프로바이더 설정(키·권한) 문제라 고칠 때까지 막을 대상이다.
    - `permanent`: 그 밖(400·404·422 등 요청 거부, 상태를 모르는 예외). 다시 보내도 결과가 같다.

    먼저 임베딩·리랭크와 같은 `_classify`(사슬의 실제 HTTP 응답·전송 오류·타임아웃)로 본다. 그
    근거가 사슬에 없을 때만 LiteLLM이 단 정수 `status_code`를 보조로 쓴다 — 첫 조각 전에 SSE로 온
    오류(Anthropic `overloaded_error` 등)는 HTTP 200 응답 안에 실려 httpx 응답이 없다. 임베딩·
    리랭크는 `_classify`만 쓰므로 이 보조 판정의 영향을 받지 않는다.
    """
    failure = _classify(exc)
    status = failure.status
    if failure.kind in ("timeout", "retry"):
        return "transient"
    if not failure.grounded:
        status = _litellm_status(exc)
        if status is not None and (status == 429 or 500 <= status < 600):
            return "transient"
    if status in _AUTH_STATUSES:
        return "auth"
    return "permanent"


def _litellm_status(exc: BaseException) -> int | None:
    """사슬에서 처음 만나는 정수 `status_code` 속성(LiteLLM 예외가 단다). 없으면 None."""
    for cur in _chain(exc):
        code = getattr(cur, "status_code", None)
        if isinstance(code, int) and not isinstance(code, bool):
            return code
    return None


def _backoff(attempt: int) -> float:
    """지수 백오프에 지터를 더한다(equal jitter) — 동시에 실패한 워커들이 같이 몰리지 않게."""
    ceiling = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
    return ceiling / 2 + random.uniform(0, ceiling / 2)
