"""게이트웨이 임베딩의 일시 오류 재시도와 배치 크기 (#194).

OpenAI 호환 임베딩 서버를 로컬 HTTP 서버로 띄우고 실제 LiteLLM으로 호출한다. 서버는
요청마다 미리 정한 응답(상태 코드·헤더·지연)을 돌려준다. 대기(`_sleep`)만 기록형으로
바꾼다. 타임아웃 간격을 재는 테스트는 실제로 기다린다.
"""

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from biznotes_core.services import embedding_spaces as es


@dataclass
class _Reply:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0


class _Server:
    """`script`의 응답을 차례로 돌려주고, 다 쓰면 200으로 답한다."""

    def __init__(self) -> None:
        self.script: list[_Reply] = []
        self.requests: list[tuple[float, int]] = []  # (도착 시각, 입력 수)
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                server.requests.append((time.monotonic(), len(body["input"])))
                reply = server.script.pop(0) if server.script else _Reply()
                time.sleep(reply.delay)
                if reply.status == 200:
                    data = [
                        {"object": "embedding", "index": i, "embedding": [float(i)]}
                        for i, _ in enumerate(body["input"])
                    ]
                    payload: dict[str, Any] = {
                        "object": "list", "model": body["model"], "data": data,
                        "usage": {"prompt_tokens": 1, "total_tokens": 1},
                    }
                else:
                    payload = {"error": {"message": f"status {reply.status}"}}
                raw = json.dumps(payload).encode()
                try:
                    self.send_response(reply.status)
                    for k, v in reply.headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    pass  # 클라이언트가 타임아웃으로 끊었다

            def log_message(self, *args: Any) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._httpd.server_port}/v1"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Server]:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    srv = _Server()
    yield srv
    srv.close()


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(es, "_sleep", recorded.append)
    return recorded


def _call(base: str, prefix: str = "openai", api_key: str | None = "test-key") -> es.GatewayCall:
    params: dict[str, Any] = {"api_base": base}
    if api_key:
        params["api_key"] = api_key
    return es.GatewayCall(
        provider_type="custom", model=f"{prefix}/bge-m3", display_name="bge-m3", params=params
    )


def _space(call: es.GatewayCall) -> es.EmbeddingSpace:
    return es.EmbeddingSpace(key="gateway:t", display_name="bge-m3", call=call)


# ── 수락 기준 1: 429는 Retry-After만큼 기다린 뒤 성공한다 ─────────────────────


@pytest.mark.parametrize("prefix", ["openai", "hosted_vllm", "lm_studio"])
def test_429_waits_retry_after_then_succeeds(
    server: _Server, sleeps: list[float], prefix: str
) -> None:
    server.script = [_Reply(429, {"Retry-After": "3"})]

    vectors = _space(_call(server.base, prefix)).embed(["a", "b"])

    assert vectors == [[0.0], [1.0]]
    assert sleeps == [3.0]
    assert len(server.requests) == 2  # SDK 자동 재시도가 끼어들지 않는다


def test_retry_after_http_date(server: _Server, sleeps: list[float]) -> None:
    server.script = [_Reply(503, {"Retry-After": formatdate(time.time() + 30, usegmt=True)})]

    _space(_call(server.base)).embed(["a"])

    assert len(sleeps) == 1 and 25 <= sleeps[0] <= 30


def test_5xx_without_retry_after_backs_off_until_attempt_cap(
    server: _Server, sleeps: list[float]
) -> None:
    server.script = [_Reply(503) for _ in range(10)]

    with pytest.raises(es.TransientEmbeddingError) as info:
        _space(_call(server.base)).embed(["a"])

    assert len(server.requests) == es._MAX_ATTEMPTS
    assert len(sleeps) == es._MAX_ATTEMPTS - 1
    # 지터를 더한 지수 백오프 — 상한 절반 이상, 상한 이하
    for n, slept in enumerate(sleeps, start=1):
        ceiling = min(es._BACKOFF_CAP_SECONDS, es._BACKOFF_BASE_SECONDS * 2 ** (n - 1))
        assert ceiling / 2 <= slept <= ceiling
    assert info.value.retry_after is None


def test_retry_after_beyond_budget_gives_up_without_waiting(
    server: _Server, sleeps: list[float]
) -> None:
    server.script = [_Reply(429, {"Retry-After": "200"}), _Reply(429, {"Retry-After": "200"})]

    with pytest.raises(es.TransientEmbeddingError) as info:
        _space(_call(server.base)).embed(["a"])

    # 첫 200초는 예산(300초) 안이라 기다리고, 두 번째는 예산을 넘으므로 기다리지 않는다
    assert sleeps == [200.0]
    assert len(server.requests) == 2
    assert info.value.retry_after == 200.0


def test_connection_refused_is_transient(sleeps: list[float]) -> None:
    with pytest.raises(es.TransientEmbeddingError):
        _space(_call("http://127.0.0.1:1/v1")).embed(["a"])

    assert len(sleeps) == es._MAX_ATTEMPTS - 1


# ── 수락 기준 2: 느린 서버에 고아 요청을 쌓지 않는다 ───────────────────────


@pytest.mark.parametrize("prefix", ["openai", "lm_studio"])
def test_timeout_resends_once_after_waiting(server: _Server, prefix: str) -> None:
    timeout = 0.5
    server.script = [_Reply(delay=1.5), _Reply(delay=1.5)]

    with pytest.raises(es.TransientEmbeddingError):
        _space(_call(server.base, prefix)).embed(["a"], timeout=timeout)

    assert len(server.requests) == 2  # 첫 요청 + 기다린 뒤 재전송 1회
    gap = server.requests[1][0] - server.requests[0][0]
    assert gap >= 2 * timeout  # 첫 요청의 타임아웃 + 같은 시간만큼의 대기


def test_timeout_then_success(server: _Server, sleeps: list[float]) -> None:
    server.script = [_Reply(delay=1.5)]

    vectors = _space(_call(server.base)).embed(["a"], timeout=0.5)

    assert vectors == [[0.0]]
    assert sleeps == [0.5]


def test_query_does_not_resend_after_timeout(
    server: _Server, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(es, "GATEWAY_TIMEOUT_SECONDS", 0.5)
    server.script = [_Reply(delay=1.5)]

    with pytest.raises(es.TransientEmbeddingError):
        _space(_call(server.base)).embed_query("질의")

    assert len(server.requests) == 1
    assert sleeps == []


# ── 수락 기준 3: 배치 크기 ────────────────────────────────────────────────


def test_batch_size_splits_requests(server: _Server, sleeps: list[float]) -> None:
    texts = [f"청크 {i}" for i in range(64)]

    vectors = _space(_call(server.base)).embed(texts, batch_size=8)

    assert [n for _, n in server.requests] == [8] * 8
    assert len(vectors) == 64


def test_default_batch_size_is_unchanged(server: _Server, sleeps: list[float]) -> None:
    _space(_call(server.base)).embed([f"청크 {i}" for i in range(65)])

    assert [n for _, n in server.requests] == [es.GATEWAY_BATCH_SIZE, 1]


# ── 수락 기준 4: 영구 오류는 다시 시도하지 않는다 ──────────────────────────


@pytest.mark.parametrize("status", [400, 401, 404, 422])
def test_client_errors_are_permanent(server: _Server, sleeps: list[float], status: int) -> None:
    server.script = [_Reply(status)]

    with pytest.raises(es.EmbeddingError) as info:
        _space(_call(server.base)).embed(["a"])

    assert not isinstance(info.value, es.TransientEmbeddingError)
    assert len(server.requests) == 1
    assert sleeps == []


def test_client_side_500_without_response_is_permanent(
    server: _Server, sleeps: list[float]
) -> None:
    # 키가 없으면 LiteLLM이 요청을 만들기 전에 500(InternalServerError)으로 올린다(#193)
    with pytest.raises(es.EmbeddingError) as info:
        _space(_call(server.base, api_key=None)).embed(["a"])

    assert not isinstance(info.value, es.TransientEmbeddingError)
    assert server.requests == []
    assert sleeps == []


# ── 예산 공유와 배치 벽시계 상한 ───────────────────────────────────────────


def test_budget_is_shared_across_calls(server: _Server, sleeps: list[float]) -> None:
    # 한 인제스트 시도의 배치들이 같은 예산을 나눠 쓴다
    budget = es.RetryBudget(5.0)
    space = _space(_call(server.base))
    server.script = [_Reply(429, {"Retry-After": "3"})]
    space.embed(["a"], retry_budget=budget)
    server.script = [_Reply(429, {"Retry-After": "3"})]

    with pytest.raises(es.TransientEmbeddingError):
        space.embed(["b"], retry_budget=budget)

    assert sleeps == [3.0]
    assert budget.seconds == 2.0


def test_batch_wall_clock_cap_stops_resending(
    server: _Server, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    # 다음 대기와 다음 요청 timeout을 더해 상한을 넘으면 다시 보내지 않는다
    monkeypatch.setattr(es, "_BATCH_WALL_SECONDS", 30.0)
    server.script = [_Reply(503, {"Retry-After": "1"})]

    with pytest.raises(es.TransientEmbeddingError):
        _space(_call(server.base)).embed(["a"], timeout=30)

    assert len(server.requests) == 1
    assert sleeps == []


def test_unsupported_scheme_is_permanent(sleeps: list[float]) -> None:
    # base_url 스킴 오류는 기다려도 바뀌지 않는다
    with pytest.raises(es.EmbeddingError) as info:
        _space(_call("ftp://127.0.0.1:1/v1")).embed(["a"])

    assert not isinstance(info.value, es.TransientEmbeddingError)
    assert sleeps == []
