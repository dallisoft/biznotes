"""프로바이더 동시성 게이트의 실제 Redis 동작(#196).

계획 `docs/plans/embedding-concurrency-limit/plan.md`의 AC2·AC5·AC9를 확인한다.
PG 테스트(`*_pg.py`)다 — 실제 Valkey가 필요하다(`tests/conftest.py` docstring).

여기서 검증하는 것은 `tests/test_concurrency_gate.py`(대역)와 다른 층이다:
**여러 스레드가 실제로 같은 상한을 지킨다**, **죽은 holder의 리스가 스스로 회복된다**,
**키 TTL이 형제 리스를 자르지 않는다**(계획 M2).
"""

import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import redis as redis_sync

from biznotes_core.core import concurrency_gate as gate


@pytest.fixture
def redis_client() -> Iterator[redis_sync.Redis]:
    from biznotes_core.config import get_settings

    client = redis_sync.Redis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        yield client
    finally:
        client.close()


def _key(provider_id: uuid.UUID) -> str:
    return f"embgate:{gate.get_settings().service_name}:{provider_id}"


@pytest.fixture
def provider() -> uuid.UUID:
    return uuid.uuid4()


def _track(redis_client: redis_sync.Redis, provider: uuid.UUID) -> str:
    key = _key(provider)
    redis_client.delete(key)
    return key


# ── AC2: 동시 요청 수가 상한을 넘지 않는다 ────────────────────────────────


@pytest.mark.parametrize("limit,threads", [(1, 4), (2, 8), (3, 6)])
def test_concurrency_never_exceeds_the_limit(
    redis_client: redis_sync.Redis, provider: uuid.UUID, limit: int, threads: int
) -> None:
    """여러 스레드가 실제로 같은 상한 아래에서만 통과하는지.

    워커 프로세스 수만큼 동시에 보낸다는 것이 이 기능의 전제였고, ZSET 리스가 그것을 막는다.
    스레드 수를 상한보다 크게 두는 것이 요점이다 — 모두 동시에 자리에 들어갈 수는 없다.
    """
    key = _track(redis_client, provider)
    concurrent = 0
    peak = 0
    lock = threading.Lock()
    errors: list[BaseException] = []

    def worker() -> None:
        nonlocal concurrent, peak
        try:
            with gate.lease(provider, limit, wait_budget=30.0, lease_ttl=60.0):
                with lock:
                    concurrent += 1
                    peak = max(peak, concurrent)
                time.sleep(0.05)  # 자리를 쥔 채 겹치게 만든다
                with lock:
                    concurrent -= 1
        except BaseException as exc:  # noqa: BLE001 - 스레드 예외를 본 스레드로 옮긴다
            errors.append(exc)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=60)
        assert not w.is_alive()

    assert errors == []
    assert peak <= limit, f"실제 동시 {peak}이 상한 {limit}을 넘었다"
    assert peak == limit  # 상한은 실제로 채워진다 — 테스트가 비어 있지 않다는 증거
    assert redis_client.zcard(key) == 0  # 모두 반납했다


def test_release_frees_the_slot_for_the_next_caller(
    redis_client: redis_sync.Redis, provider: uuid.UUID
) -> None:
    key = _track(redis_client, provider)
    with gate.lease(provider, 1, wait_budget=1.0, lease_ttl=30.0):
        assert redis_client.zcard(key) == 1
    assert redis_client.zcard(key) == 0

    # 반납 후에는 기다리지 않고 바로 잡힌다
    started = time.monotonic()
    with gate.lease(provider, 1, wait_budget=1.0, lease_ttl=30.0):
        pass
    assert time.monotonic() - started < 0.5


def test_zero_budget_fails_fast_instead_of_waiting(
    redis_client: redis_sync.Redis, provider: uuid.UUID
) -> None:
    key = _track(redis_client, provider)
    with gate.lease(provider, 1, wait_budget=30.0, lease_ttl=30.0):
        started = time.monotonic()
        with pytest.raises(gate.GateTimeout) as info:
            with gate.lease(provider, 1, wait_budget=0.0, lease_ttl=30.0):
                pytest.fail("상한이 찼는데 획득에 성공했다")
        assert time.monotonic() - started < 0.5  # 기다리지 않는다
        assert info.value.retry_after > 0  # 자리가 언제 나는지 알려 준다
        # 거절된 시도는 자리를 잡지 않았고, 밖에서 잡은 리스는 그대로다
        assert redis_client.zcard(key) == 1
    assert redis_client.zcard(key) == 0


# ── AC5: 죽은 holder의 리스가 스스로 회복한다 ─────────────────────────────


def test_expired_lease_is_reclaimed_but_live_one_still_blocks(
    redis_client: redis_sync.Redis, provider: uuid.UUID
) -> None:
    """만료된 것만 걷어내야 한다 — 살아 있는 형제 리스를 지우면 상한이 뚫린다(M2)."""
    key = _track(redis_client, provider)
    now_ms = int(redis_client.time()[0]) * 1000
    redis_client.zadd(key, {b"live": now_ms + 60_000, b"dead": now_ms - 60_000})

    # 살아 있는 리스 1개가 상한 1을 다 차지한다 → 만료분만 회수해서는 여전히 막혀야 한다
    with pytest.raises(gate.GateTimeout):
        with gate.lease(provider, 1, wait_budget=0.0, lease_ttl=30.0):
            pytest.fail("살아 있는 리스가 있는데 획득에 성공했다")

    assert redis_client.zscore(key, "live") is not None

    # 상한을 2로 올리면 만료분은 이미 걷어내어졌고 살아 있는 리스만 남아 자리가 난다
    with gate.lease(provider, 2, wait_budget=0.0, lease_ttl=30.0):
        pass
    assert redis_client.zscore(key, "dead") is None


def test_crashed_holder_does_not_leak_the_slot_forever(
    redis_client: redis_sync.Redis, provider: uuid.UUID
) -> None:
    """죽은 holder의 슬롯을 **실제 만료**로 회수한다.

    키를 지워 버리면 회수를 증명하는 게 아니라 회수를 우회하는 것이다 — 슬롯이 비는 것을
    눈으로 보고 지우는 것과, 만료로 스스로 비는 것은 다르다. TTL이 실제로 지나가게 둔다.
    """
    key = _track(redis_client, provider)
    now_ms = int(redis_client.time()[0]) * 1000
    # 이미 만료된 시각의 토큰을 남겨 둔다 — holder가 죽은 상태를 재현
    redis_client.zadd(key, {b"abandoned": now_ms - 1_000})
    assert redis_client.zcard(key) == 1  # 아직 키에는 남아 있다

    # 다음 획득이 스스로 걷어내며 통과한다 — 카운터였다면 영구히 새겼을 것이다
    with gate.lease(provider, 1, wait_budget=0.0, lease_ttl=30.0):
        pass
    assert redis_client.zscore(key, "abandoned") is None


# ── M2: 키 TTL이 형제 리스를 자르지 않는다 ───────────────────────────────


def test_key_ttl_covers_the_longest_sibling_lease(
    redis_client: redis_sync.Redis, provider: uuid.UUID
) -> None:
    """짧은 리스를 가진 요청이 긴 리스를 가진 형제의 키를 지우면 안 된다(계획 M2)."""
    key = _track(redis_client, provider)
    now_ms = int(redis_client.time()[0]) * 1000
    redis_client.zadd(key, {b"long": now_ms + 390_000, b"short": now_ms + 5_000})
    redis_client.pexpire(key, 5_000)  # 짧은 리스의 TTL로 잘못 설정된 상태

    # 짧은 TTL을 가진 요청이 획득을 시도한다 (상한 3이라 성공)
    with gate.lease(provider, 3, wait_budget=0.0, lease_ttl=5):
        pass

    ttl_ms = redis_client.pttl(key)
    assert ttl_ms > 390_000, f"키 TTL({ttl_ms}ms)이 가장 긴 리스(390초)를 덮지 못했다"
    assert redis_client.zscore(key, "long") is not None


# ── AC9: 관리용 조회가 실제 상태를 반영한다 ──────────────────────────────


def test_status_reflects_live_leases_and_last_error(
    redis_client: redis_sync.Redis, provider: uuid.UUID
) -> None:
    key = _track(redis_client, provider)
    redis_client.set(f"{key}:lasterr", "timeout", ex=gate.LAST_ERROR_TTL_SECONDS)

    with gate.lease(provider, 2, wait_budget=0.0, lease_ttl=30.0):
        state = gate.status(provider)
        assert state.in_use == 1
        assert 0 < state.earliest_expiry_in <= 30.0
        assert state.last_error == "timeout"

    idle = gate.status(provider)
    assert idle.in_use == 0
    assert idle.earliest_expiry_in is None


def test_status_does_not_count_expired_leases(
    redis_client: redis_sync.Redis, provider: uuid.UUID
) -> None:
    """만료된 리스는 슬롯이 이미 비었는데 키 TTL 패드 동안 ZSET에 남는다.

    세면 `in_use`가 상한에 붙어 보여 "상한이 걸렸다"는 운영 지침이 거짓말한다(리뷰 m7).
    """
    key = _track(redis_client, provider)
    now_ms = int(redis_client.time()[0]) * 1000
    redis_client.zadd(key, {b"dead": now_ms - 5_000, b"live": now_ms + 60_000})

    state = gate.status(provider)

    assert state.in_use == 1, "만료된 리스를 세었다"
    assert state.earliest_expiry_in and state.earliest_expiry_in > 50


def test_status_raises_when_redis_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def dead() -> Any:
        raise redis_sync.RedisError("redis is down")

    monkeypatch.setattr(gate, "_client", dead)

    with pytest.raises(RuntimeError):
        gate.status(uuid.uuid4())


def test_status_has_no_waiter_field() -> None:
    # 대기자는 ZSET에 남지 않는다 — 관측에 없는 값을 만들어 내지 않는다(계획 N4)
    assert not hasattr(gate.GateStatus, "waiters")
    assert set(gate.GateStatus.__dataclass_fields__) == {
        "in_use",
        "earliest_expiry_in",
        "last_error",
    }