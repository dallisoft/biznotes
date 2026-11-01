# 신규(#401). 필요: PostgreSQL·Valkey(PG 테스트).
"""system_settings 부분 갱신의 동시성 — 두 관리자가 다른 항목을 함께 저장해도 둘 다 남는다.

`set_setting`(RAG 설정)과 `update_overrides`(사용 제한·속도 제한)가 같은 규칙을 쓴다: 행을
`FOR UPDATE`로 잠그고 커밋된 값을 다시 읽어 보낸 항목만 합친다. 행이 없을 때의 첫 저장은
`ON CONFLICT DO NOTHING`으로 PK 충돌 없이 이어진다. 순서는 시간 대기가 아니라 뒤 세션이
앞 세션의 잠금을 기다리는 것을 `pg_stat_activity`로 확인해 고정한다. `create_all` 기반이라
Alembic 마이그레이션 검증이 아니다.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.core.db import get_session_factory
from biznotes_core.models import SystemSetting
from biznotes_core.services.settings_store import (
    get_overrides,
    get_setting,
    set_setting,
    update_overrides,
)

KEY = "test_settings_401"

Writer = Callable[[AsyncSession, dict[str, Any]], Awaitable[Any]]


async def _set_setting(session: AsyncSession, value: dict[str, Any]) -> Any:
    return await set_setting(session, KEY, value)


async def _update_overrides(session: AsyncSession, value: dict[str, Any]) -> Any:
    return await update_overrides(session, KEY, value, actor_id=None, action="test.settings")


WRITERS = pytest.mark.parametrize(
    "write", [_set_setting, _update_overrides], ids=["set_setting", "update_overrides"]
)


async def _seed(value: dict[str, Any]) -> None:
    async with get_session_factory()() as s:
        s.add(SystemSetting(key=KEY, value=value))
        await s.commit()


async def _stored() -> dict[str, Any]:
    async with get_session_factory()() as s:
        return await get_overrides(s, KEY)


async def _pid(session: AsyncSession) -> int:
    return int(await session.scalar(text("SELECT pg_backend_pid()")))


async def _wait_until_blocked_by(holder_pid: int) -> None:
    """다른 연결이 `holder_pid`의 잠금 때문에 기다리는 상태가 될 때까지 기다린다(상한 10초).

    통계 스냅샷은 트랜잭션마다 고정되므로 읽을 때마다 트랜잭션을 끝낸다.
    """
    async with asyncio.timeout(10), get_session_factory()() as probe:
        while not await probe.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock' AND :holder = ANY(pg_blocking_pids(pid))"
            ),
            {"holder": holder_pid},
        ):
            await probe.rollback()
            await asyncio.sleep(0.01)


async def _race(write: Writer, first: dict[str, Any], second: dict[str, Any]) -> None:
    """앞 세션이 저장(flush)한 채 커밋하지 않은 사이 뒤 세션이 저장을 시작한다."""
    factory = get_session_factory()
    later: asyncio.Task[None] | None = None

    async def write_later() -> None:
        async with factory() as s:
            await write(s, second)
            await s.commit()

    try:
        async with factory() as earlier:
            await write(earlier, first)
            later = asyncio.create_task(write_later())
            await _wait_until_blocked_by(await _pid(earlier))
            await earlier.commit()
        async with asyncio.timeout(10):
            await later
    finally:
        if later is not None and not later.done():
            later.cancel()


@WRITERS
async def test_concurrent_partial_updates_keep_both_changes(write: Writer) -> None:
    """이미 있는 행 — 뒤 요청이 앞 요청 전의 값으로 합쳐 앞 변경을 되돌리지 않는다."""
    await _seed({"top_k": 6, "chunk_size": 800})

    await _race(write, {"top_k": 9}, {"chunk_size": 500})

    assert await _stored() == {"top_k": 9, "chunk_size": 500}


@WRITERS
async def test_concurrent_first_saves_do_not_conflict(write: Writer) -> None:
    """행이 없을 때 동시에 처음 저장해도 PK 충돌로 한쪽이 실패하지 않고 둘 다 남는다."""
    await _race(write, {"top_k": 9}, {"chunk_size": 500})

    assert await _stored() == {"top_k": 9, "chunk_size": 500}


@WRITERS
async def test_a_stale_session_merges_onto_the_committed_value(write: Writer) -> None:
    """세션이 앞서 읽어 둔 낡은 행(identity map)이 아니라 커밋된 값 위에 합친다."""
    await _seed({"top_k": 6})
    factory = get_session_factory()
    async with factory() as stale:
        held = await stale.get(SystemSetting, KEY)  # 참조를 붙잡아야 identity map에 남는다
        assert held is not None and held.value == {"top_k": 6}
        await stale.commit()
        async with factory() as other:
            await write(other, {"top_k": 9})
            await other.commit()

        await write(stale, {"chunk_size": 500})
        await stale.commit()

    assert await _stored() == {"top_k": 9, "chunk_size": 500}


async def test_set_setting_still_fills_defaults_and_overwrites_sent_keys() -> None:
    """반환값과 읽기는 이전과 같다 — 기본값 위에 저장값, 보낸 항목은 덮어쓴다."""
    factory = get_session_factory()
    async with factory() as s:
        out = await set_setting(s, "rag", {"top_k": 9})
        await s.commit()
    assert out["top_k"] == 9 and out["chunk_size"] == 800  # 기본값이 채워진다
    async with factory() as s:
        out = await set_setting(s, "rag", {"top_k": 3, "chunk_size": 500})
        await s.commit()
    async with factory() as s:
        assert await get_setting(s, "rag") == out
        assert (out["top_k"], out["chunk_size"]) == (3, 500)
