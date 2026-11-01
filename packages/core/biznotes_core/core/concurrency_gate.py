"""Redis 리스 기반 동시성 게이트 — 프로바이더가 동시에 처리할 수 있는 요청 수(#196).

배경: 코어는 워커 프로세스 수만큼 임베딩 요청을 동시에 보낸다. 서버의 동시 처리 한도를
요청 쪽이 알 수단이 없다(#194). 그래서 상한이 없는 요청이 서버 큐에 쌓여 타임아웃을 만든다.
여기서는 **요청을 서버로 보내기 전에** 슬롯을 잡고, 없으면 기다린다.

구조 — 정렬 집합(ZSET) 리스:
- 멤버는 획득 토큰(uuid4), 스코어는 리스 만료 시각(epoch **밀리초**).
- 만료된 멤버를 먼저 걷어내므로 **리스를 잡은 프로세스가 죽어도(SIGKILL) 스스로 회복한다.**
  카운터(`INCR`/`DECR`)를 쓰면 죽은 워커가 슬롯을 영구히 새기므로 쓰지 않는다.
- **대기 중인 요청은 집합에 흔적을 남기지 않는다.** 대기자를 등록하면 "죽은 대기자 회수"를
  새로 풀어야 한다. 대기 소진은 지표와 로그로 드러낸다.
- 키 TTL은 **집합의 마지막 리스 만료**에 맞춘다. 자기 리스 TTL로 두면 더 오래 사는 형제 리스가
  있는 요청의 키가 통째로 사라져 실제 동시 수가 상한을 넘는다.

시계는 Redis가 정본이다(`TIME`). 클라이언트 벽시계로 잡으면 호스트 간 NTP 오차가 그대로
어드미션 오차가 된다. 관측 조회도 같은 이유로 Redis 시계를 쓴다.

`core/` 계층이라 `config`와 같은 계층(`core.metrics`)·서드파티만 import한다.
모듈 최상위에서 `get_settings()`를 호출하지 않는다(`tests/test_no_import_time_settings.py`).
"""

import contextlib
import random
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import cast

import redis as redis_sync
import structlog
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from biznotes_core.config import get_settings
from biznotes_core.core.metrics import embedding_gate_total, embedding_gate_wait_seconds

logger = structlog.get_logger()

# 폴링 간격의 하한·상한. 지터를 섞어 동시에 풀린 워커들이 같은 시각에 다시 몰리지 않게 한다
_POLL_MIN_SECONDS = 0.05
_POLL_MAX_SECONDS = 2.0
# 마지막 오류 표식의 TTL — 워커가 쓰고 API가 읽는다. 오래 남으면 복구된 장애가 오래 보인다
LAST_ERROR_TTL_SECONDS = 60
# 리스 TTL 하한(초). 이 밑으로 낮추면 리스가 즉시 만료돼 상한이 무효가 된다
_MIN_LEASE_TTL_SECONDS = 1.0
# 키 TTL에 더하는 여유(밀리초). 마지막 리스가 만료된 뒤 키가 바로 사라지지 않게 한다
_KEY_TTL_PAD_MS = 10_000

# KEYS[1]=zset, ARGV[1]=limit, ARGV[2]=lease_ttl_ms, ARGV[3]=token, ARGV[4]=key_ttl_pad_ms
# 반환: {1, 0}=획득 성공 / {0, wait_ms}=실패 + 가장 이른 리스 해제까지 남은 밀리초
_ACQUIRE_LUA = """
local limit = tonumber(ARGV[1])
if limit < 1 then return {1, 0} end
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local got = 0
local wait_ms = 0
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) < limit then
  redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[3])
  got = 1
else
  local head = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  wait_ms = tonumber(head[2]) - now
end
if redis.call('ZCARD', KEYS[1]) > 0 then
  local last = redis.call('ZREVRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  redis.call('PEXPIRE', KEYS[1], tonumber(last[2]) - now + tonumber(ARGV[4]))
end
return {got, wait_ms}
"""

_warned_missing_provider_id = False


def _count_result(result: str) -> None:
    """게이트 시도 결과를 센다 — 라벨이 잘못되면 지표가 조용히 무시된다."""
    embedding_gate_total.labels(result=result).inc()


@dataclass(frozen=True)
class GateStatus:
    """관리용 조회 결과 — 지금 잡힌 리스 수와 마지막 오류."""

    in_use: int
    earliest_expiry_in: float | None
    last_error: str | None


class GateTimeout(Exception):
    """대기 예산 안에 슬롯을 잡지 못했다.

    `waited`는 실제로 소비한 대기(초), `retry_after`는 마지막 폴링에서 본 가장 이른
    리스 해제까지 남은 초다(최소 0.001). `retry_after`는 자리가 언제 나느냐의 **하한**이며
    보장값이 아니다 — 뒤에서 리스가 더 길어질 수 있다.
    """

    def __init__(self, message: str, *, waited: float, retry_after: float) -> None:
        super().__init__(message)
        self.waited = waited
        self.retry_after = max(retry_after, 0.001)


def _key(provider_id: uuid.UUID) -> str:
    return f"embgate:{get_settings().service_name}:{provider_id}"


def _last_error_key(provider_id: uuid.UUID) -> str:
    return f"{_key(provider_id)}:lasterr"


def _client() -> redis_sync.Redis:
    # 호출마다 만들고 닫는다 — `services/events.py:189`의 워커 동기 발행과 같은 형태.
    # 임베딩 호출 1회당 1번이므로 풀을 유지할 이유가 없다
    return redis_sync.Redis.from_url(get_settings().redis_url)


def _note_error(provider_id: uuid.UUID | None, exc: BaseException) -> None:
    """오류를 표식에 남긴다. 표식 쓰기 자체가 실패하면 삼킨다.

    연결 실패와 타임아웃을 구분해 남긴다 — 둘 다 상한은 사라지지만 원인이 다르다.
    `lasterr`를 쓰려면 Redis가 응답해야 하므로, **Redis가 죽은 동안에는 표식도 못 남는다.**
    그 상황은 워커 로그와 `embedding_gate_total{result="redis_error"}`로만 보인다.
    """
    kind = "timeout" if isinstance(exc, RedisTimeoutError) else type(exc).__name__
    embedding_gate_total.labels(result="redis_error").inc()
    logger.warning(
        "임베딩 동시성 게이트 Redis 오류 — 상한 없이 진행한다", kind=kind, exc_info=True
    )
    if provider_id is None:
        return
    try:
        client = _client()
        try:
            client.set(_last_error_key(provider_id), kind, ex=LAST_ERROR_TTL_SECONDS)
        finally:
            client.close()
    except Exception:  # noqa: BLE001 - 표식은 관측용이다. 실패해도 경로는 계속된다
        logger.debug("임베딩 게이트 오류 표식 기록 실패", exc_info=True)


def _text(raw: object) -> str | None:
    """Redis 문자열을 `str`로. 클라이언트에 `decode_responses`를 걸지 않았으므로 바이트로 온다."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return raw if isinstance(raw, str) and raw else None


def read_last_error(provider_id: uuid.UUID) -> str | None:
    """워커가 마지막으로 기록한 게이트 오류. Redis가 응답해야 읽힌다(문서화된 관측 공백)."""
    client = _client()
    try:
        raw = client.get(_last_error_key(provider_id))
    except RedisError:
        return None
    finally:
        client.close()
    return _text(raw)


def status(provider_id: uuid.UUID) -> GateStatus:
    """관리용 조회 — 지금 잡힌 리스 수, 가장 이른 해제 시각, 마지막 오류.

    스코어와 현재 시각을 **둘 다 Redis에서** 읽는다. 클라이언트 시계로 비교하면 호스트 간
    NTP 오차가 리스 남은 시간에 그대로 들어간다.
    """
    try:
        # 클라이언트 생성도 try 안이다 — 설정이나 URL이 잘못되면 여기서도 같은 오류로 닫혀야 한다
        client = _client()
    except RedisError as exc:
        raise RuntimeError(f"임베딩 게이트 상태를 읽지 못했습니다: {exc}") from exc
    try:
        # withscores=True면 항상 (멤버, 스코어) 쌍의 목록이 온다 — 스텁이 합집합으로 넓혀서
        # 캐스팅한다. 멤버는 bytes(클라이언트에 decode_responses를 걸지 않았음), 스코어는 float
        members = cast(
            "list[tuple[bytes, float]]",
            client.zrange(_key(provider_id), 0, -1, withscores=True),
        )
        raw_time = client.time()
        raw = client.get(_last_error_key(provider_id))
    except RedisError as exc:
        raise RuntimeError(f"임베딩 게이트 상태를 읽지 못했습니다: {exc}") from exc
    finally:
        client.close()
    # 클라이언트에 decode_responses를 걸지 않았으므로 TIME이 바이트로 온다
    now_ms = int(raw_time[0]) * 1000 + int(raw_time[1]) / 1000.0
    scores = [float(score) for _, score in members if float(score) > now_ms]
    earliest = min(scores, default=None)
    return GateStatus(
        # **만료된 멤버는 세지 않는다.** 키 TTL 패드(10초) 동안 ZSET에는 남지만 슬롯은 이미
        # 비어 있다 — 세지 않으면 `in_use`가 상한에 붙어 보여 "상한이 걸렸다"는 운영 지침이
        # 거짓말한다(#196 리뷰 m7)
        in_use=len(scores),
        earliest_expiry_in=None if earliest is None else (earliest - now_ms) / 1000.0,
        last_error=_text(raw),
    )


def _try_acquire(
    client: redis_sync.Redis, key: str, limit: int, lease_ttl: float
) -> tuple[bool, str, float]:
    """한 번의 획득 시도. 반환: (성공 여부, 토큰, 실패 시 남은 초)"""
    token = uuid.uuid4().hex
    result = client.eval(
        _ACQUIRE_LUA,
        1,
        key,
        int(limit),
        int(lease_ttl * 1000),
        token,
        _KEY_TTL_PAD_MS,
    )
    if int(result[0]) == 1:
        return True, token, 0.0
    return False, "", max(int(result[1]) / 1000.0, 0.001)


def _release(client: redis_sync.Redis, key: str, token: str) -> None:
    """해제. 실패해도 삼킨다 — 리스 TTL이 회수한다."""
    try:
        client.zrem(key, token)
    except RedisError:
        logger.debug("임베딩 게이트 해제 실패 — TTL이 회수한다", exc_info=True)


@contextlib.contextmanager
def _lease(
    provider_id: uuid.UUID, limit: int, *, wait_budget: float, lease_ttl: float
) -> Iterator[float]:
    """슬롯을 잡은 상태로 문맥을 열고, 닫을 때 놓는다. 획득에 쓴 대기(초)를 yield한다.

    Redis 오류는 여기서 fail-open으로 흡수한다 — 표식은 provider_id가 있어야 남으므로
    경로로 받은 id를 쓴다.
    """
    key = _key(provider_id)
    # 리스 TTL 하한 — 0 이하면 멤버 스코어가 "지금"이라 다음 획득이 즉시 지워 버려
    # `limit=1`인데도 두 번째 획득이 통과한다. 상한이 조용히 무효가 되는 상태(리뷰 m4)
    ttl = max(lease_ttl, _MIN_LEASE_TTL_SECONDS)
    started = time.monotonic()
    waited = 0.0
    token = ""
    held = False
    fail_open = False
    client: redis_sync.Redis | None = None
    try:
        client = _client()
        while True:
            try:
                ok, token, wait_s = _try_acquire(client, key, limit, ttl)
            except RedisError as exc:
                # fail-open — 상한 없이 진행한다. held가 False라 해제도 하지 않는다.
                # `acquired`로 세면 Redis가 죽었는데도 획득 성공률 100%로 보인다(리뷰 M2)
                _note_error(provider_id, exc)
                fail_open = True
                break
            if ok:
                held = True
                break
            if wait_budget <= waited:
                raise GateTimeout(
                    "임베딩 동시성 슬롯을 잡지 못했다", waited=waited, retry_after=wait_s
                )
            nap = min(
                _POLL_MAX_SECONDS,
                max(_POLL_MIN_SECONDS, wait_s * random.uniform(0.5, 1.0)),
                wait_budget - waited,
            )
            time.sleep(nap)
            waited = time.monotonic() - started
        yield waited
    finally:
        if client is not None:
            try:
                if held:
                    _release(client, key, token)
            finally:
                client.close()
        # `finally` 안이어야 한다 — 컨텍스트 종료는 `yield`에 GeneratorExit를 던지므로
        # 그 아래 코드는 도달하지 않는다. fail-open은 `acquired`가 아니라 `bypass`로 센다
        # (Redis가 죽었는데도 획득 성공률 100%로 보이면 관측이 거짓말한다 — 리뷰 M2)
        _count_result("bypass" if fail_open else "acquired")


@contextlib.contextmanager
def lease(
    provider_id: uuid.UUID | None,
    limit: int | None,
    *,
    wait_budget: float,
    lease_ttl: float,
) -> Iterator[float]:
    """프로바이더 단위 동시성 상한 하나를 잡는다. 획득에 실제로 쓴 대기(초)를 yield한다.

    - `limit`이 None이거나 0 이하: **클라이언트를 만들지 않고** 통과한다(상한 없음 = 지금 동작).
    - `provider_id`가 None: 게이트를 건너뛴다. `GatewayCall`은 공개 데이터클래스라 외부에서
      만들 수 있고, 키가 `embgate:svc:None`이 되어서는 안 된다. 경고는 프로세스당 한 번.
    - Redis 오류: 경고와 오류 표식을 남기고 **fail-open**으로 통과한다.
    - `wait_budget`초 안에 못 잡으면 `GateTimeout`. 0이면 기다리지 않고 한 번만 시도한다.
    """
    global _warned_missing_provider_id
    if limit is None or limit <= 0:
        _count_result("bypass")
        yield 0.0
        return
    if provider_id is None:
        if not _warned_missing_provider_id:
            _warned_missing_provider_id = True
            logger.warning("provider_id 없이 임베딩 동시성 게이트를 요청해 건너뜁니다")
        _count_result("bypass")
        yield 0.0
        return
    try:
        with _lease(
            provider_id, limit, wait_budget=wait_budget, lease_ttl=lease_ttl
        ) as waited:
            if waited:
                embedding_gate_wait_seconds.observe(waited)
            yield waited
    except GateTimeout:
        embedding_gate_total.labels(result="timeout").inc()
        raise


__all__ = [
    "LAST_ERROR_TTL_SECONDS",
    "GateStatus",
    "GateTimeout",
    "lease",
    "status",
]
