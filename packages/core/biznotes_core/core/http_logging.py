"""httpx 요청 로그의 URL을 출처(`scheme://host[:port]`)로 줄인다(#408).

httpx는 표준 logging `httpx` 로거에 `HTTP Request: GET <URL> "HTTP/1.1 200 OK"`를 INFO로 남긴다.
URL에는 userinfo·경로 토큰·쿼리(서명 URL·API 키)가 실릴 수 있고, Celery 워커(`-l INFO`)는 root를
INFO로 두어 이 줄이 그대로 남는다. 필터는 `httpx` 로거에 붙으므로 프로세스 안의 모든 httpx 호출
(코어 SSRF 관문·게이트웨이·LiteLLM 등)에 걸린다. 메서드·상태 줄은 남긴다.

httpcore DEBUG 로그는 이 필터 밖이다(응답 헤더를 남긴다) — 운영에서는 DEBUG로 두지 않는다.
"""

import logging
import re
from urllib.parse import urlsplit

_HTTPX_LOGGER = "httpx"
# httpx 메시지는 URL 뒤에 공백과 상태 줄이 온다 — 공백·따옴표·꺾쇠 전까지를 URL로 본다
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]*")
_UNPARSABLE = "<url>"


def _origin(raw: str) -> str:
    """userinfo·경로·쿼리·조각을 뺀다. 해석할 수 없으면 URL 전체를 감춘다."""
    try:
        parts = urlsplit(raw)
        host, port = parts.hostname, parts.port
    except ValueError:
        return _UNPARSABLE
    if not host:
        return _UNPARSABLE
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")


def _redact(text: str) -> str:
    return _URL_RE.sub(lambda m: _origin(m.group(0)), text)


class HttpxUrlRedactionFilter(logging.Filter):
    """레코드 메시지의 URL을 출처로 바꾼다. 레코드는 버리지 않는다.

    필터 예외는 로깅 호출부(= HTTP 요청)로 올라가므로 어떤 레코드에도 예외를 내지 않는다.
    포맷할 수 없는 레코드는 인자를 버리고 템플릿만 남긴다(인자에 URL 원문이 있을 수 있다).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = _redact(record.getMessage())
        except Exception:
            try:
                message = _redact(str(record.msg))
            except Exception:
                message = _UNPARSABLE
        record.msg, record.args = message, None
        return True


def install_httpx_log_redaction() -> None:
    """`httpx` 로거에 `HttpxUrlRedactionFilter`를 단다. 여러 번 불러도 하나만 단다.

    코어 `workers.celery_app` import와 `app_factory.configure_logging`이 부른다. 둘을 거치지 않는
    프로세스(제품이 직접 조립한 API 등)는 로깅 설정에서 직접 부른다.
    """
    logger = logging.getLogger(_HTTPX_LOGGER)
    if not any(isinstance(f, HttpxUrlRedactionFilter) for f in logger.filters):
        logger.addFilter(HttpxUrlRedactionFilter())


__all__ = ["HttpxUrlRedactionFilter", "install_httpx_log_redaction"]
