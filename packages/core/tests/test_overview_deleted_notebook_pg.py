# 신규(#220). 필요: PostgreSQL·Valkey(PG 테스트).
"""삭제된 노트북의 개요 생성 태스크 — 실제 PostgreSQL FK 위반과 Redis 토큰 정리.

예약(`schedule_overview_refresh`) → 노트북 삭제 → 태스크 본문 실행을 실제 세션·Redis로 돈다.
브로커 발행(`celery_app.send_task`, #321)만 기록형 가짜다. 태스크는 동기 함수라 스레드에서 돌리고,
`run_async`는 테스트 루프에 코루틴을 넘긴다(워커 엔진이 이 루프에 묶여 있다).
`create_all` 기반이라 Alembic 마이그레이션 검증이 아니다.
"""

import asyncio
import uuid
from collections.abc import Coroutine, Iterator
from typing import Any

import pytest
import redis as redis_sync
from sqlalchemy import delete, func, select

from biznotes_core.config import get_settings
from biznotes_core.core.db import get_session_factory
from biznotes_core.models import Notebook, NotebookOverview, Source, SourceChunk, User
from biznotes_core.services import overview as overview_service
from biznotes_core.workers.celery_app import celery_app
from biznotes_core.workers.task_names import GENERATE_NOTEBOOK_OVERVIEW
from biznotes_core.workers.tasks import overview


@pytest.fixture
def redis_client() -> Iterator[redis_sync.Redis]:
    client = redis_sync.Redis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
async def scheduled(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """브로커 대신 발행 인자를 기록하고, `run_async`를 테스트 루프로 돌린다."""
    calls: list[list[str]] = []
    loop = asyncio.get_running_loop()

    def send_task(name: str, args: Any = None, kwargs: Any = None, **_kw: Any) -> None:
        assert name == GENERATE_NOTEBOOK_OVERVIEW
        calls.append(list(args))

    def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    monkeypatch.setattr(celery_app, "send_task", send_task)
    monkeypatch.setattr(overview, "run_async", run_async)
    return calls


async def _notebook_with_ready_source(db: Any, owner: User) -> Notebook:
    nb = Notebook(owner_id=owner.id, title="개요 NB")
    db.add(nb)
    await db.flush()
    src = Source(notebook_id=nb.id, type="file", title="문서", status="ready")
    db.add(src)
    await db.flush()
    db.add(SourceChunk(source_id=src.id, seq=0, text="본문 문장"))
    await db.commit()
    return nb


async def _delete_notebook(notebook_id: uuid.UUID) -> None:
    async with get_session_factory()() as other:
        await other.execute(delete(Notebook).where(Notebook.id == notebook_id))
        await other.commit()


async def _overview_rows(db: Any, notebook_id: uuid.UUID) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(NotebookOverview)
            .where(NotebookOverview.notebook_id == notebook_id)
        )
    ).scalar_one()


def _keys(notebook_id: uuid.UUID) -> tuple[str, str]:
    return f"overview:debounce:{notebook_id}", f"overview:regen:{notebook_id}"


async def test_deleted_before_run_finishes_normally_and_cleans_token(
    db, user_a: User, mock_llm, scheduled, redis_client
):
    nb = await _notebook_with_ready_source(db, user_a)
    token_key, regen_key = _keys(nb.id)
    redis_client.set(regen_key, "1", ex=60)
    overview.schedule_overview_refresh(str(nb.id))
    [(notebook_id, token)] = scheduled
    assert redis_client.get(token_key) == token

    await _delete_notebook(nb.id)
    result = await asyncio.to_thread(overview.generate_notebook_overview.run, notebook_id, token)

    assert result == {"ok": True, "skipped": "notebook_deleted"}
    assert await _overview_rows(db, nb.id) == 0
    assert redis_client.exists(token_key, regen_key) == 0


async def test_deleted_during_generation_fk_violation_is_normal_finish(
    db, user_a: User, mock_llm, scheduled, redis_client, monkeypatch: pytest.MonkeyPatch
):
    """존재 확인 뒤·삽입 전에 노트북이 지워지는 경합 — 실제 FK 위반을 정상 종료로 바꾼다."""
    nb = await _notebook_with_ready_source(db, user_a)
    token_key, regen_key = _keys(nb.id)
    redis_client.set(regen_key, "1", ex=60)
    overview.schedule_overview_refresh(str(nb.id))
    [(notebook_id, token)] = scheduled

    source_texts = overview_service._source_texts
    raced: list[bool] = []

    async def texts_then_delete(session: Any, sources: list[Source]) -> Any:
        texts = await source_texts(session, sources)
        await _delete_notebook(nb.id)
        raced.append(True)
        return texts

    monkeypatch.setattr(overview_service, "_source_texts", texts_then_delete)

    result = await asyncio.to_thread(overview.generate_notebook_overview.run, notebook_id, token)

    assert raced == [True]
    assert result == {"ok": True, "skipped": "notebook_deleted"}
    assert await _overview_rows(db, nb.id) == 0
    assert redis_client.exists(token_key, regen_key) == 0


async def test_live_notebook_still_generates(db, user_a: User, mock_llm, scheduled, redis_client):
    nb = await _notebook_with_ready_source(db, user_a)
    overview.schedule_overview_refresh(str(nb.id))
    [(notebook_id, token)] = scheduled

    result = await asyncio.to_thread(overview.generate_notebook_overview.run, notebook_id, token)

    assert result == {"ok": True}
    assert await _overview_rows(db, nb.id) == 1
    assert redis_client.exists(*_keys(nb.id)) == 0
