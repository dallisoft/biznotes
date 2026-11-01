"""기존 소스 요약 백필(#454) — 예약(범위마다 중복 발행 방지)·진행률·하루 상한 장부.

백필 태스크(`BACKFILL_SOURCE_DIGESTS`)는 노트북 하나 또는 모든 노트북(`all`)의 ready 소스 중 최신
요약이 없는 것만 만든다. 캐시 규칙은 `services.source_digest`와 같다(지문·`PROMPT_VERSION`이 같은
행이 있으면 건너뛴다) — 그래서 중단 뒤 다시 실행하면 이어서 진행한다. 재개의 정본은 `source_digests`
행이고, 이 모듈의 Redis 상태(예약 표식·진행률·하루 장부)는 운영 표시와 상한용이다(models 변경 없음).

- 예약: 범위마다 대기 표식을 `SET NX`로 잡은 쪽만 발행한다. 태스크가 진행하며 표식 TTL을 늘리고
  끝나면(성공·실패 모두) 지운다. 워커가 죽어 지우지 못하면 `PENDING_TTL_SECONDS` 뒤 풀린다.
- 진행률: 범위마다 마지막 실행의 `SourceDigestBackfillProgress`(JSON, `PROGRESS_TTL_SECONDS`).
- 하루 상한: 백필 전체가 함께 쓰는 날짜별 장부(비용·생성 소스 수, 표시 시간대 기준 날짜). 소스를
  만들기 직전에 확인한다 — 비용은 끝난 생성만 쌓이므로 동시에 진행 중인 생성만큼 넘을 수 있다.

태스크는 import하지 않고 이름으로 발행한다(`ARCHITECTURE.md` §2, `task_publish`).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from biznotes_core.config import CoreSettings, get_settings
from biznotes_core.core.timezones import to_display
from biznotes_core.schemas.source_digest import BackfillStopReason, SourceDigestBackfillProgress
from biznotes_core.services.task_publish import publish_task
from biznotes_core.workers.task_names import BACKFILL_SOURCE_DIGESTS

#: 모든 노트북 백필의 범위 이름
SCOPE_ALL = "all"
# 범위마다 대기·진행 중 표식 — 있으면 같은 범위를 다시 발행하지 않는다
PENDING_KEY = "source_digest:backfill:pending:{scope}"
# 범위마다 마지막 실행의 진행률(JSON)
PROGRESS_KEY = "source_digest:backfill:progress:{scope}"
# 날짜별 하루 장부(해시: `cost_usd`·`sources`)
SPEND_KEY = "source_digest:backfill:spend:{day}"
#: 대기 표식 TTL — 태스크가 진행률을 쓸 때마다 다시 늘린다
PENDING_TTL_SECONDS = 3600
PROGRESS_TTL_SECONDS = 7 * 86_400
SPEND_TTL_SECONDS = 2 * 86_400
#: `source_digest` run이 예약할 때의 지연(초) — 이어지는 run들이 표식 하나로 합쳐진다
RUN_SCHEDULE_COUNTDOWN_SECONDS = 60
# Redis 연결·명령 시간 상한(초) — run 안 예약이 느린 Redis에 붙잡히지 않게 한다
_REDIS_TIMEOUT = 2.0


def backfill_scope(notebook_id: uuid.UUID | None) -> str:
    """백필 범위 이름 — 노트북 id 문자열, 없으면 `all`."""
    return str(notebook_id) if notebook_id is not None else SCOPE_ALL


def backfill_redis() -> Any:
    """백필 상태용 Redis 클라이언트(문자열 응답). 호출자가 `aclose()`한다."""
    from redis.asyncio import Redis

    return Redis.from_url(
        get_settings().redis_url,
        decode_responses=True,
        socket_connect_timeout=_REDIS_TIMEOUT,
        socket_timeout=_REDIS_TIMEOUT,
    )


def _spend_key(now: datetime | None = None) -> str:
    day = to_display(now or datetime.now(UTC)).strftime("%Y%m%d")
    return SPEND_KEY.format(day=day)


async def save_progress(client: Any, progress: SourceDigestBackfillProgress) -> None:
    """진행률을 쓴다. 대기·진행 중이면 그 범위의 대기 표식 TTL도 늘린다."""
    progress.updated_at = datetime.now(UTC)
    await client.set(
        PROGRESS_KEY.format(scope=progress.scope),
        progress.model_dump_json(),
        ex=PROGRESS_TTL_SECONDS,
    )
    if progress.status in ("queued", "running"):
        await client.expire(PENDING_KEY.format(scope=progress.scope), PENDING_TTL_SECONDS)


async def release_pending(client: Any, scope: str) -> None:
    """대기 표식을 지운다 — 다음 예약이 다시 발행할 수 있다."""
    await client.delete(PENDING_KEY.format(scope=scope))


async def schedule_backfill(notebook_id: uuid.UUID | None, *, countdown: int = 0) -> bool:
    """백필을 발행한다. 같은 범위에 대기·진행 중인 백필이 있으면 발행하지 않고 False다.

    Redis·브로커 오류는 예외로 올린다(발행이 실패하면 대기 표식을 지운다).
    """
    scope = backfill_scope(notebook_id)
    client = backfill_redis()
    try:
        claimed = await client.set(
            PENDING_KEY.format(scope=scope), "1", nx=True, ex=PENDING_TTL_SECONDS
        )
        if not claimed:
            return False
        try:
            publish_task(
                BACKFILL_SOURCE_DIGESTS,
                args=[str(notebook_id) if notebook_id is not None else None],
                countdown=countdown,
            )
        except Exception:
            await release_pending(client, scope)
            raise
        await save_progress(
            client,
            SourceDigestBackfillProgress(scope=scope, notebook_id=notebook_id, status="queued"),
        )
    finally:
        await client.aclose()
    return True


async def read_progress(notebook_id: uuid.UUID | None) -> SourceDigestBackfillProgress | None:
    """그 범위의 마지막 진행률. 없거나 깨진 값이면 None이다."""
    client = backfill_redis()
    try:
        raw = await client.get(PROGRESS_KEY.format(scope=backfill_scope(notebook_id)))
    finally:
        await client.aclose()
    if raw is None:
        return None
    try:
        return SourceDigestBackfillProgress.model_validate_json(raw)
    except ValueError:
        return None


async def reserve_generation(client: Any, settings: CoreSettings) -> BackfillStopReason | None:
    """오늘 상한 안이면 생성 하나를 장부에 잡고 None, 상한에 닿았으면 멈출 이유를 준다.

    비용 상한은 끝난 생성의 비용 합으로 판정한다. 개수 상한은 잡을 때 세므로 넘지 않는다.
    """
    key = _spend_key()
    cost_cap = settings.source_digest_backfill_daily_cost_usd
    if cost_cap > 0 and float(await client.hget(key, "cost_usd") or 0) >= cost_cap:
        return "daily_cost_cap"
    count = int(await client.hincrby(key, "sources", 1))
    await client.expire(key, SPEND_TTL_SECONDS)
    source_cap = settings.source_digest_backfill_daily_max_sources
    if source_cap and count > source_cap:
        await client.hincrby(key, "sources", -1)
        return "daily_source_cap"
    return None


async def record_spend(client: Any, cost_usd: float) -> None:
    """생성 하나가 쓴 비용을 오늘 장부에 더한다(실패한 생성이 쓴 것도)."""
    if cost_usd <= 0:
        return
    key = _spend_key()
    await client.hincrbyfloat(key, "cost_usd", cost_usd)
    await client.expire(key, SPEND_TTL_SECONDS)


__all__ = [
    "PENDING_TTL_SECONDS",
    "PROGRESS_TTL_SECONDS",
    "RUN_SCHEDULE_COUNTDOWN_SECONDS",
    "SCOPE_ALL",
    "SPEND_TTL_SECONDS",
    "backfill_redis",
    "backfill_scope",
    "read_progress",
    "record_spend",
    "release_pending",
    "reserve_generation",
    "save_progress",
    "schedule_backfill",
]
