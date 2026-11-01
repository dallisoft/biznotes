"""프로바이더 동시성 게이트의 Redis 없는 경로(#196).

계획 `docs/plans/embedding-concurrency-limit/plan.md`의 AC1·AC6·AC12를 확인한다.
Redis가 살아 있는 경로(상한 강제·죽은 holder 회수)는 `tests/test_embedding_gate_redis_pg.py`가 본다.
여기는 **상한 없음·fail-open·provider_id 없음·대기 예산 소진**만 검증한다 — Redis 클라이언트를
만드는지 안 만드는지까지 포함해, 상한이 없으면 아무것도 하지 않는 것을 단언한다.
"""

import uuid
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from biznotes_core.core import concurrency_gate as gate

PROVIDER = uuid.uuid4()


@pytest.fixture(autouse=True)
def _reset_warning_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "_warned_missing_provider_id", False)


class _FakeClient:
    """`eval`/`set`만 있는 대역. `raises`가 있으면 모든 명령이 그 예외를 낸다."""

    def __init__(self, raises: Exception | None = None, script: Any = None) -> None:
        self.raises = raises
        self.script = script or [1, 0]
        self.calls: list[tuple[Any, ...]] = []
        self.closed = False

    def eval(self, *args: Any) -> Any:
        self.calls.append(("eval", *args))
        if self.raises is not None:
            raise self.raises
        return list(self.script)

    def get(self, *args: Any) -> Any:
        self.calls.append(("get", *args))
        if self.raises is not None:
            raise self.raises
        return None

    def set(self, *args: Any, **kw: Any) -> Any:
        self.calls.append(("set", *args, *kw.values()))
        if self.raises is not None:
            raise self.raises
        return True

    def zrem(self, *args: Any) -> Any:
        self.calls.append(("zrem", *args))
        if self.raises is not None:
            raise self.raises
        return 1

    def close(self) -> None:
        self.closed = True


def _install(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> list[_FakeClient]:
    made: list[_FakeClient] = []

    def factory() -> _FakeClient:
        made.append(client)
        return client

    monkeypatch.setattr(gate, "_client", factory)
    return made


# ── AC1: 상한이 없으면 클라이언트를 만들지 않는다 ───────────────────────────


@pytest.mark.parametrize("limit", [None, 0, -1])
def test_no_limit_never_touches_redis(monkeypatch: pytest.MonkeyPatch, limit: int | None) -> None:
    made = _install(monkeypatch, _FakeClient())

    with gate.lease(PROVIDER, limit, wait_budget=5.0, lease_ttl=60.0) as waited:
        assert waited == 0.0

    assert made == []  # 상한이 없으면 Redis를 만지지 않는다 — 지금 동작과 동일하다


def test_missing_provider_id_skips_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    made = _install(monkeypatch, _FakeClient())
    logs: list[str] = []
    monkeypatch.setattr(gate.logger, "warning", lambda msg, **kw: logs.append(msg))

    with gate.lease(None, 4, wait_budget=5.0, lease_ttl=60.0) as waited:
        assert waited == 0.0
    # 키가 `embgate:svc:None`이 되어서는 안 된다
    assert made == []
    assert len(logs) == 1

    # 경고는 프로세스당 한 번만이다
    with gate.lease(None, 4, wait_budget=5.0, lease_ttl=60.0):
        pass
    assert len(logs) == 1


# ── AC6: Redis 장애는 fail-open이고, 원인을 구분한다 ───────────────────────


@pytest.mark.parametrize(
    "exc,kind",
    [
        (RedisConnectionError("refused"), "ConnectionError"),
        (RedisTimeoutError("slow"), "timeout"),
    ],
)
def test_redis_error_fails_open_and_records_kind(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, kind: str
) -> None:
    """AC6 — 연결 실패와 소켓 타임아웃이 **구분되어** 기록된다."""
    client = _FakeClient(raises=exc)
    _install(monkeypatch, client)

    with gate.lease(PROVIDER, 1, wait_budget=0.0, lease_ttl=60.0) as waited:
        assert waited == 0.0  # 예외 없이 통과한다

    marks = [c for c in client.calls if c[0] == "set"]
    assert len(marks) == 1
    # 세 번째 인자가 표식 값이다 — timeout과 refused를 운영자가 구분할 수 있어야 한다
    assert marks[0][2] == kind
    assert marks[0][3] == gate.LAST_ERROR_TTL_SECONDS  # TTL이 설정된다
    assert client.closed


def test_fail_open_is_not_counted_as_acquired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis가 죽었는데 `acquired`가 오르면 관측이 실패 순간에 거짓말한다(리뷰 M2)."""
    from prometheus_client import REGISTRY

    _install(monkeypatch, _FakeClient(raises=RedisConnectionError("refused")))

    # prometheus_client은 Counter 이름 끝의 `_total`을 잘라 표본 이름을 되돌린다
    def delta(result: str) -> float:
        return REGISTRY.get_sample_value("embedding_gate_total", {"result": result}) or 0.0

    before = (delta("acquired"), delta("bypass"), delta("redis_error"))
    with gate.lease(PROVIDER, 1, wait_budget=0.0, lease_ttl=60.0):
        pass
    after = (delta("acquired"), delta("bypass"), delta("redis_error"))

    assert after[0] == before[0], "fail-open이 acquired로 집계됐다"
    assert after[1] == before[1] + 1
    assert after[2] == before[2] + 1


def test_redis_error_does_not_release_a_lease_it_never_took(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(raises=RedisConnectionError("refused"))
    _install(monkeypatch, client)

    with gate.lease(PROVIDER, 1, wait_budget=0.0, lease_ttl=60.0):
        pass

    assert not any(call[0] == "zrem" for call in client.calls)


def test_acquire_script_failure_fails_open_too(monkeypatch: pytest.MonkeyPatch) -> None:
    # PEXPIRE 정수 오류 같은 스크립트 실패도 fail-open으로 조용히 통과한다(계획 §8 R-4)
    from redis.exceptions import ResponseError

    client = _FakeClient(raises=ResponseError("value is not an integer or out of range"))
    _install(monkeypatch, client)

    with gate.lease(PROVIDER, 1, wait_budget=0.0, lease_ttl=60.0) as waited:
        assert waited == 0.0


# ── 대기 예산 소진 ────────────────────────────────────────────────────────


def test_zero_budget_raises_without_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    # 0초 예산 = "기다리지 않는다". 한 번은 시도하고 실패하면 바로 낸다
    client = _FakeClient(script=[0, 5_000])  # 5초 뒤에 자리가 난다고 해도
    _install(monkeypatch, client)
    slept: list[float] = []
    monkeypatch.setattr(gate.time, "sleep", slept.append)

    with pytest.raises(gate.GateTimeout) as exc:
        with gate.lease(PROVIDER, 1, wait_budget=0.0, lease_ttl=60.0):
            pytest.fail("획득 실패했는데 문맥에 들어갔다")

    assert slept == []  # 기다리지 않는다
    assert exc.value.waited == 0.0
    assert exc.value.retry_after == 5.0


def test_timeout_carries_wait_and_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(script=[0, 1_000])  # 1초 뒤 해제
    _install(monkeypatch, client)
    monkeypatch.setattr(gate.time, "sleep", lambda _s: None)  # 실제로는 안 잔다

    with pytest.raises(gate.GateTimeout) as exc:
        with gate.lease(PROVIDER, 1, wait_budget=0.05, lease_ttl=60.0):
            pytest.fail("획득 실패했는데 문맥에 들어갔다")

    assert exc.value.retry_after == 1.0
    assert exc.value.waited >= 0.0


def test_gate_timeout_is_not_an_embedding_error() -> None:
    # `GateTimeout`이 `EmbeddingError` 계열로 새어나가면 소스가 `failed`로 닫힌다(AC11)
    from biznotes_core.services.embedding_spaces import EmbeddingError

    assert not issubclass(gate.GateTimeout, EmbeddingError)
    assert issubclass(gate.GateTimeout, Exception)


def test_retry_after_has_a_floor() -> None:
    # 0에 가까워지면 무한 즉시 재시도가 된다 — 최소값을 둔다
    assert gate.GateTimeout("x", waited=0.0, retry_after=0.0).retry_after == 0.001


# ── 해제와 관측 ───────────────────────────────────────────────────────────


def test_release_uses_the_same_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(script=[1, 0])
    _install(monkeypatch, client)

    with gate.lease(PROVIDER, 1, wait_budget=0.0, lease_ttl=60.0):
        pass

    # eval(lua, numkeys, key, limit, lease_ttl_ms, token, pad) — 토큰은 6번째 인자
    acquired = next(c for c in client.calls if c[0] == "eval")
    removed = next(c for c in client.calls if c[0] == "zrem")
    # ZADD에 실린 토큰과 ZREM의 토큰이 같아야 자리가 반납된다
    assert removed[2] == acquired[6]
    assert client.closed


def test_status_reports_in_use_and_next_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    class _StatusClient(_FakeClient):
        def zrange(self, *args: Any, **kw: Any) -> Any:
            return [(b"a", 11_000.0), (b"b", 13_000.0)]

        def time(self) -> Any:
            return (10, 0)  # 10초 → 10000ms

        def get(self, *args: Any) -> Any:
            return b"timeout"

    client = _StatusClient()
    _install(monkeypatch, client)

    state = gate.status(PROVIDER)

    assert state.in_use == 2
    assert state.earliest_expiry_in == pytest.approx(1.0)  # 1000ms 스코어 - 10000ms 현재
    assert state.last_error == "timeout"
    assert client.closed


def test_status_on_empty_key(monkeypatch: pytest.MonkeyPatch) -> None:
    class _EmptyClient(_FakeClient):
        def zrange(self, *args: Any, **kw: Any) -> Any:
            return []

        def time(self) -> Any:
            return (10, 0)

        def get(self, *args: Any) -> Any:
            return None

    _install(monkeypatch, _EmptyClient())

    state = gate.status(PROVIDER)

    assert state.in_use == 0
    assert state.earliest_expiry_in is None
    assert state.last_error is None


def test_read_last_error_swallows_redis_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _FakeClient(raises=RedisConnectionError("refused")))

    assert gate.read_last_error(PROVIDER) is None


def test_module_does_not_read_settings_at_import_time() -> None:
    # `tests/test_no_import_time_settings.py`가 import 시점 설정을 막는다 — 게이트도 지켜야 한다
    import importlib

    module = importlib.reload(gate)
    assert module.get_settings is not None

def test_lease_ttl_has_a_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """리스 TTL이 0이면 멤버가 즉시 만료돼 상한이 조용히 무효가 된다(리뷰 m4)."""
    seen: list[tuple[Any, ...]] = []

    class _Capture(_FakeClient):
        def eval(self, *args: Any) -> Any:
            seen.append(args)
            return [1, 0]

    _install(monkeypatch, _Capture())
    with gate.lease(PROVIDER, 1, wait_budget=0.0, lease_ttl=0.0):
        pass

    ttl_ms = seen[0][4]  # ARGV[2] = lease_ttl_ms
    assert ttl_ms >= 1000, f"리스 TTL이 0이었다({ttl_ms}ms) — 상한이 즉시 풀린다"
