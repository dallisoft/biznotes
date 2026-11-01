"""`_embed_batch`의 프로바이더 동시성 게이트 통합(#196).

계획 `docs/plans/embedding-concurrency-limit/plan.md`의 AC3·AC11·AC13·AC14를 확인한다.
가짜 OpenAI 호환 서버를 띄워 **서버에 몇 번이나 나갔는지**를 세고, 게이트를 Redis 없이 대역으로
바꾼다. 실제 Redis 상한 강제(`tests/test_embedding_gate_redis_pg.py`)와 게이트 자체
(`tests/test_concurrency_gate.py`)는 다른 파일이 담당한다.
"""

import http.server
import threading
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from biznotes_core.core import concurrency_gate as gate
from biznotes_core.services import embedding_spaces as es

PROVIDER = uuid.uuid4()


class _Server:
    """요청 횟수만 세는 최소 서버 — 게이트가 요청을 보내지 않았는지 판정하는 데 쓴다."""

    def __init__(self) -> None:
        self.count = 0
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 이름
                self.rfile.read(int(self.headers["Content-Length"]))
                server.count += 1
                body = (
                    b'{"object":"list","data":[{"object":"embedding","index":0,'
                    b'"embedding":[1.0]}],"usage":{"prompt_tokens":1,"total_tokens":1}}'
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
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


def _space(base: str, limit: int | None) -> es.EmbeddingSpace:
    call = es.GatewayCall(
        provider_type="custom",
        model="openai/bge-m3",
        display_name="bge-m3",
        params={"api_base": base, "api_key": "test-key"},
        provider_id=PROVIDER,
        max_concurrency=limit,
    )
    return es.EmbeddingSpace(key="gateway:t", display_name="bge-m3", call=call)


class _Allow:
    def __enter__(self) -> float:
        return 0.0

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Deny:
    """항상 거절하는 게이트."""

    def __init__(self, retry_after: float = 2.0) -> None:
        self.retry_after = retry_after

    def __enter__(self) -> float:
        raise gate.GateTimeout("busy", waited=0.0, retry_after=self.retry_after)

    def __exit__(self, *exc: Any) -> bool:
        return False


def _install_gate(
    monkeypatch: pytest.MonkeyPatch, factory: Any
) -> list[dict[str, Any]]:
    """게이트를 대역으로 바꾸고, 획득 시도를 기록할 목록을 돌려준다."""
    seen: list[dict[str, Any]] = []

    def lease(provider_id: Any, limit: Any, **kw: Any) -> Any:
        seen.append({"provider_id": provider_id, "limit": limit, **kw})
        return factory(len(seen))

    monkeypatch.setattr(gate, "lease", lease)
    return seen


# ── AC1: 상한이 없으면 게이트를 부르지 않는다 ──────────────────────────────


def test_no_limit_passes_none_through_to_the_gate(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 상한이 없으면 게이트가 Redis를 만지지 않는다(`tests/test_concurrency_gate.py`가 그것을
    # 단언한다). 여기서는 코드가 `None`을 그대로 내려보내는 것만 확인한다.
    seen = _install_gate(monkeypatch, lambda n: _Allow())

    assert _space(server.base, None).embed(["a"]) == [[1.0]]
    assert server.count == 1
    assert [s["limit"] for s in seen] == [None]


# ── AC3: 슬롯이 없으면 요청을 보내지 않는다 ────────────────────────────────


def test_gate_denial_sends_no_request(
    server: _Server, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _install_gate(monkeypatch, lambda n: _Deny())

    with pytest.raises(es.TransientEmbeddingError) as info:
        _space(server.base, 1).embed(["a"], retry_budget=es.RetryBudget(5.0))

    assert server.count == 0  # 요청이 한 번도 나가지 않았다
    assert info.value.reason == "gate"
    assert info.value.retry_after == 2.0
    # 예산 5초에 재시도 지연 2초 → 2회 미룸 후 3번째에서 잔여 1초 < 2초로 끊긴다
    assert len(seen) == 3
    assert all(s["provider_id"] == PROVIDER and s["limit"] == 1 for s in seen)


def test_gate_denial_exhausts_the_retry_budget_not_attempts(
    server: _Server, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _install_gate(monkeypatch, lambda n: _Deny(retry_after=3.0))

    with pytest.raises(es.TransientEmbeddingError):
        _space(server.base, 1).embed(["a"], retry_budget=es.RetryBudget(9.0))

    # 게이트 소진 뒤의 재시도 지연(3초)만 `RetryBudget`에서 차감된다 — 3회면 9초를 다 쓴다
    assert sleeps == [3.0, 3.0, 3.0]
    assert len(seen) == 4  # 4번째 시도에서 잔여 3초 ≥ 지연 3초 → 한 번 더, 5번째는 0초 예산


# ── AC13: 게이트 대기는 RetryBudget을 차감하지 않는다 ──────────────────────


def test_gate_wait_does_not_consume_retry_budget(
    server: _Server, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """게이트 대기는 용량 대기다 — `RetryBudget`("재시도 대기 예산")에서 빼면 안 된다."""
    state = {"n": 0}

    class _Waited:
        """실제로 30초를 기다린 뒤 잡았다고 보고한다."""

        def __enter__(self) -> float:
            return 30.0

        def __exit__(self, *exc: Any) -> bool:
            return False

    def factory(n: int) -> Any:
        state["n"] += 1
        # 첫 시도는 30초 대기 후 획득, 이후는 즉시 획득
        return _Waited() if state["n"] == 1 else _Allow()

    seen = _install_gate(monkeypatch, factory)
    budget = es.RetryBudget(300.0)

    _space(server.base, 1).embed(["a"], retry_budget=budget)

    assert server.count == 1
    # 게이트 대기는 30초였지만 차감되지 않았다 — 270이었다면 "차감 안 함"이 아니라 다른 결론이다
    assert budget.seconds == pytest.approx(300.0)
    assert sleeps == []
    # 리스 TTL = timeout(60) * 2 + 여유(30)
    assert seen[0]["lease_ttl"] == pytest.approx(150.0)


def test_last_attempt_does_not_wait_for_the_gate(
    server: _Server, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    # 기다렸다 실패할 요청이 없다 — 마지막 시도는 0초 예산으로 한 번만 시도한다
    seen = _install_gate(monkeypatch, lambda n: _Deny(retry_after=0.001))

    with pytest.raises(es.TransientEmbeddingError):
        _space(server.base, 1).embed(["a"], retry_budget=es.RetryBudget(1000.0))

    assert len(seen) == es._MAX_ATTEMPTS
    assert seen[-1]["wait_budget"] == 0.0
    assert all(s["wait_budget"] > 0 for s in seen[:-1])


# ── AC11: 게이트 소진은 영구 오류로 새어나가지 않는다 ───────────────────────


def test_gate_denial_raises_transient_not_permanent(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_gate(monkeypatch, lambda n: _Deny())

    # `EmbeddingError`(영구)를 내면 소스가 `failed`로 닫힌다 — transient여야 한다
    with pytest.raises(es.TransientEmbeddingError) as info:
        _space(server.base, 1).embed(["a"], retry_budget=es.RetryBudget(0.0))

    assert type(info.value) is es.TransientEmbeddingError
    assert info.value.reason == "gate"


# ── AC14: 질의는 짧게만 기다린다 ───────────────────────────────────────────


def test_query_wait_cap_is_two_seconds(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _install_gate(monkeypatch, lambda n: _Allow())

    _space(server.base, 1).embed_query("질문")

    assert server.count == 1
    # 질의는 리스 TTL(150초)이 아니라 2초만 기다린다 — 사용자가 기다리는 경로다
    assert seen[0]["wait_budget"] == es._QUERY_GATE_WAIT_SECONDS == 2.0
    assert seen[0]["lease_ttl"] == pytest.approx(150.0)


def test_query_gate_denial_is_catchable_as_embedding_error(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_gate(monkeypatch, lambda n: _Deny())

    with pytest.raises(es.TransientEmbeddingError) as info:
        _space(server.base, 1).embed_query("질문")

    # `search_spaces`가 공간 단위로 삼킬 수 있어야 하므로 `EmbeddingError` 하위여야 한다
    assert isinstance(info.value, es.EmbeddingError)
    assert info.value.reason == "gate"