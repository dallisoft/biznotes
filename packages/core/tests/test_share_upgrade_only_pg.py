# 신규(#300). 필요: PostgreSQL·Valkey(PG 테스트).
"""공유 POST의 하향 금지 모드 `mode: "upgrade_only"` — 실제 라우터·인증·DB·잠금.

계획 `docs/plans/share-upgrade-only/plan.md` 수락 기준 1·3·6이다(대상은 그룹 하나다 — #278 3단계.
수락 기준 2의 중복 행은 DB 유니크가 막는다). 경합은 다른 연결이 노트북 공유
잠금(`pg_advisory_xact_lock`)을 쥔 채 두 요청을 차례로 대기시킨다. `pg_locks`로 대기를 확인하며
순서를 고정한다 — PostgreSQL은 대기 순서대로 잠금을 넘긴다.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from test_notebook_share_idempotent_pg import _Api, _rows, _world

from biznotes_core.api.v1 import auth, notebooks
from biznotes_core.core.db import get_session_factory
from biznotes_core.services.notebook_shares import _notebook_shares_lock_key


@pytest.fixture
async def api() -> AsyncIterator[_Api]:
    app = FastAPI()
    for router in (auth.router, notebooks.router):
        app.include_router(router, prefix="/api/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield _Api(client)


def _share(target: dict[str, str], permission: str, mode: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {**target, "permission": permission}
    if mode is not None:
        body["mode"] = mode
    return body


@pytest.fixture
async def setup() -> dict[str, Any]:
    w = await _world()
    w["target"] = {"group_id": str(w["group"])}
    w["path"] = f"/notebooks/{w['nb']}/share"
    return w


# ── 수락 기준 1: 단건 ─────────────────────────────────────────────────────


async def test_new_target_gets_the_requested_permission(api: _Api, setup: dict) -> None:
    status, body = await api.call(
        setup["owner"], "POST", setup["path"], _share(setup["target"], "read", "upgrade_only")
    )
    assert (status, body["permission"]) == (201, "read")


async def test_upgrade_only_raises_but_never_lowers(api: _Api, setup: dict) -> None:
    owner, path, target = setup["owner"], setup["path"], setup["target"]
    _, first = await api.call(owner, "POST", path, _share(target, "read"))

    _, raised = await api.call(owner, "POST", path, _share(target, "write", "upgrade_only"))
    status, kept = await api.call(owner, "POST", path, _share(target, "read", "upgrade_only"))

    assert raised["permission"] == "write"
    assert (status, kept["permission"], kept["id"]) == (201, "write", first["id"])
    assert [p for *_, p in await _rows(setup["nb"])] == ["write"]


# ── 수락 기준 3: 경합 — 잠금을 쥔 채 두 요청을 차례로 대기시킨다 ────────────


async def _waiting_on_advisory_locks() -> int:
    async with get_session_factory()() as s:
        result = await s.execute(
            text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
        )
        return int(result.scalar_one())


async def _until_waiting(n: int) -> None:
    async with asyncio.timeout(10):
        while await _waiting_on_advisory_locks() < n:  # noqa: ASYNC110 — DB 상태를 폴링한다
            await asyncio.sleep(0.02)


async def _race_in_order(api: _Api, w: dict, first: dict, second: dict) -> list[int]:
    """다른 연결이 잠금을 쥔 동안 `first`, `second` 순서로 대기시킨 뒤 푼다."""
    await api.login(w["owner"])
    async with get_session_factory()() as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": _notebook_shares_lock_key(w["nb"])}
        )
        a = asyncio.create_task(api.call(w["owner"], "POST", w["path"], first))
        await _until_waiting(1)
        b = asyncio.create_task(api.call(w["owner"], "POST", w["path"], second))
        await _until_waiting(2)
        await holder.commit()  # 잠금을 푼다 — 대기 순서대로 넘어간다
    async with asyncio.timeout(30):
        results = await asyncio.gather(a, b)
    return [status for status, _ in results]


@pytest.mark.parametrize("order", ["set-first", "upgrade-first"])
async def test_racing_upgrade_only_never_lowers_a_concurrent_write(
    api: _Api, setup: dict, order: str
) -> None:
    raise_write = _share(setup["target"], "write")
    keep_read = _share(setup["target"], "read", "upgrade_only")
    first, second = (raise_write, keep_read) if order == "set-first" else (keep_read, raise_write)

    statuses = await _race_in_order(api, setup, first, second)

    assert statuses == [201, 201]
    assert [p for *_, p in await _rows(setup["nb"])] == ["write"]


async def test_racing_set_read_does_lower_it_in_contrast(api: _Api, setup: dict) -> None:
    """대조 — 같은 순서에서 기본 모드(`set`)는 다른 요청이 올린 write를 낮춘다(#300의 동기)."""
    statuses = await _race_in_order(
        api, setup, _share(setup["target"], "write"), _share(setup["target"], "read")
    )
    assert statuses == [201, 201]
    assert [p for *_, p in await _rows(setup["nb"])] == ["read"]


# ── 수락 기준 6: 판정 순서·존재 은닉은 mode와 상관없다 ───────────────────


async def test_permission_checks_come_first_regardless_of_mode(api: _Api) -> None:
    w = await _world()
    path = f"/notebooks/{w['nb']}/share"
    upgrade = {"group_id": str(w["group"]), "permission": "read", "mode": "upgrade_only"}

    stranger, _ = await api.call(w["member"], "POST", path, upgrade)  # 아직 공유 전 — 무관 사용자
    _, granted = await api.call(w["owner"], "POST", path, upgrade)
    sharer, _ = await api.call(w["member"], "POST", path, upgrade)  # 그룹 읽기 공유자
    other_group, _ = await api.call(
        w["owner"], "POST", path,
        {"group_id": str(uuid.uuid4()), "permission": "read", "mode": "upgrade_only"},
    )  # fmt: skip

    assert (stranger, granted["permission"], sharer, other_group) == (404, "read", 403, 404)
