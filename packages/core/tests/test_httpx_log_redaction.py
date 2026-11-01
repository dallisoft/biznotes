"""httpx 요청 로그의 URL 출처 축약(#408).

httpx 로깅은 실제 클라이언트로 남기고(전송만 `MockTransport`), 워커 경로는 별도 프로세스에서 코어
`celery_app` import + Celery `-l INFO` 로깅 설정을 그대로 재현한다.
"""

import asyncio
import logging
import pathlib
import subprocess
import sys
from collections.abc import Iterator

import httpx
import pytest
import structlog

from biznotes_core.core.http_logging import HttpxUrlRedactionFilter, install_httpx_log_redaction

SECRET_URL = "https://user:pw@example.com:8443/bot123:TOKEN/x?api_key=SECRET&sig=abc#frag"
REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def httpx_logger() -> Iterator[logging.Logger]:
    """테스트가 단 필터를 되돌린다 — `httpx` 로거는 프로세스 전역이다."""
    logger = logging.getLogger("httpx")
    saved = list(logger.filters)
    try:
        yield logger
    finally:
        logger.filters[:] = saved


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="ok")


def _request_logs(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "httpx"]


def test_request_log_keeps_only_origin(httpx_logger, caplog) -> None:
    install_httpx_log_redaction()
    caplog.set_level(logging.INFO, logger="httpx")
    with httpx.Client(transport=httpx.MockTransport(_ok)) as client:
        client.get(SECRET_URL)
    assert _request_logs(caplog) == ['HTTP Request: GET https://example.com:8443 "HTTP/1.1 200 OK"']
    for leaked in ("user", "pw", "bot123", "TOKEN", "/x", "api_key", "SECRET", "sig", "frag"):
        assert leaked not in caplog.text


def test_async_client_log_is_redacted(httpx_logger, caplog) -> None:
    install_httpx_log_redaction()
    caplog.set_level(logging.INFO, logger="httpx")

    async def fetch() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_ok)) as client:
            await client.head("http://10.0.0.5/hook/abc?token=SECRET")

    asyncio.run(fetch())
    assert _request_logs(caplog) == ['HTTP Request: HEAD http://10.0.0.5 "HTTP/1.1 200 OK"']


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("http://127.0.0.1:8080/p?q=1", "http://127.0.0.1:8080"),
        ("https://[::1]:8443/a?b=SECRET", "https://[::1]:8443"),
        ("https://[2001:db8::1]/a", "https://[2001:db8::1]"),
        # httpx가 IDNA ASCII로 남긴다
        ("https://한국.kr/경로?q=SECRET", "https://xn--3e0b707e.kr"),
        ("https://EXAMPLE.com/a?SECRET", "https://example.com"),
    ],
)
def test_origin_keeps_scheme_host_port(httpx_logger, caplog, url, origin) -> None:
    install_httpx_log_redaction()
    caplog.set_level(logging.INFO, logger="httpx")
    with httpx.Client(transport=httpx.MockTransport(_ok)) as client:
        client.get(url)
    assert _request_logs(caplog) == [f'HTTP Request: GET {origin} "HTTP/1.1 200 OK"']
    assert "SECRET" not in caplog.text


def test_install_is_idempotent(httpx_logger) -> None:
    install_httpx_log_redaction()
    install_httpx_log_redaction()
    assert sum(isinstance(f, HttpxUrlRedactionFilter) for f in httpx_logger.filters) == 1


def _record(msg: object, args: tuple[object, ...]) -> logging.LogRecord:
    return logging.LogRecord("httpx", logging.INFO, __file__, 1, msg, args, None)


def test_unformattable_record_drops_args_without_raising() -> None:
    # 인자 수가 템플릿과 어긋나면 getMessage가 TypeError를 낸다 — 필터는 예외 없이 템플릿만 남긴다
    record = _record("HTTP Request: %s %s %s", ("GET", "https://a.example/b?c=SECRET"))
    assert HttpxUrlRedactionFilter().filter(record) is True
    assert record.getMessage() == "HTTP Request: %s %s %s"


def test_url_in_template_and_unparsable_url_are_hidden() -> None:
    record = _record("redirect to https://a.example/b?c=SECRET then http://[::1/x?SECRET", ())
    HttpxUrlRedactionFilter().filter(record)
    assert record.getMessage() == "redirect to https://a.example then <url>"


def test_non_url_text_is_untouched() -> None:
    record = _record('HTTP Request: %s %s "%s %d %s"', ("GET", "plain", "HTTP/2", 404, "Not Found"))
    HttpxUrlRedactionFilter().filter(record)
    assert record.getMessage() == 'HTTP Request: GET plain "HTTP/2 404 Not Found"'


def test_configure_logging_installs_the_filter(httpx_logger) -> None:
    from biznotes_core.app_factory import configure_logging
    from biznotes_core.config import CoreSettings

    httpx_logger.filters[:] = [
        f for f in httpx_logger.filters if not isinstance(f, HttpxUrlRedactionFilter)
    ]
    old = structlog.get_config()
    try:
        configure_logging(CoreSettings())
    finally:
        structlog.configure(**old)
    assert any(isinstance(f, HttpxUrlRedactionFilter) for f in httpx_logger.filters)


WORKER_PROBE = """
import httpx
from biznotes_core.workers.celery_app import celery_app

celery_app.log.setup(loglevel="INFO")  # `celery worker -l INFO`가 하는 root 로깅 설정
with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as c:
    c.get(%r)
"""


def test_worker_request_log_keeps_only_origin() -> None:
    """코어 `celery_app`을 import한 워커는 `-l INFO`에서도 URL 출처만 남긴다(재현 시나리오)."""
    result = subprocess.run(
        [sys.executable, "-c", WORKER_PROBE % SECRET_URL],
        cwd=REPO,  # `-c`는 cwd를 sys.path 앞에 둔다 — 이 트리의 biznotes_core를 쓴다
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert 'HTTP Request: GET https://example.com:8443 "HTTP/1.1 200 OK"' in result.stderr
    for leaked in ("pw", "TOKEN", "SECRET", "api_key"):
        assert leaked not in result.stderr
