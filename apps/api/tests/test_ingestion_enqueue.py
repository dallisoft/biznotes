"""`enqueue_ingest`의 커밋·발행 순서 회귀 테스트 (R3-1b).

결함 β(R3-0 설계 §1.4): 이전 구현은 `ingest_source.delay()`를 먼저 부르고
`ingestion_jobs` 행을 커밋해서, 커밋이 실패하면 추적 행 없는 태스크만 발행됐다.
순서를 뒤집었으므로 **커밋이 성공한 경우에만 발행**되어야 한다.

task id는 미리 만들어 `send_task(task_id=...)`로 발행한다(R31CR-03).

코어 v0.11.0(#163)에서 `stage_ingest_job`이 `ingestion_jobs` 행 쓰기를 멈췄고, v0.14.0(#168)로
테이블이 제거됐다(제품 리비전 `7b31ec352a4f`). 이 파일의 발행 판정은 **Celery 발행 경계
(`celery_app.send_task`) 스파이**로만 한다(core-v0.11.0-upgrade AC-1.12).
"""

import uuid

import pytest
from biznotes_core.core.db import async_session_factory
from biznotes_core.models import Notebook, Source, User
from biznotes_core.services.ingestion.enqueue import enqueue_ingest
from biznotes_core.workers import task_names
from biznotes_core.workers.celery_app import celery_app
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
async def source(db: AsyncSession, user_a: User) -> Source:
    """호출자 규약대로 이미 커밋된 source를 만든다."""
    nb = Notebook(owner_id=user_a.id, title="t")
    db.add(nb)
    await db.commit()
    src = Source(notebook_id=nb.id, type="text", title="s", status="pending")
    db.add(src)
    await db.commit()
    await db.refresh(src)
    return src


async def test_커밋_성공시_사전_생성한_task_id로_발행된다(
    db: AsyncSession, source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[tuple[list[str], str]] = []

    # 코어는 태스크를 import하지 않고 **이름으로** 발행한다(send_task).
    # 태스크 모듈을 patch하면 발행 경로를 못 잡으므로 celery_app을 patch한다.
    def _send_task(name, args, task_id, **kwargs):
        assert name == task_names.INGEST_SOURCE
        assert kwargs == {}  # 텍스트 소스는 queue 인자 없이 현행 라우팅(publish_kwargs(None))
        published.append((args, task_id))

    monkeypatch.setattr(celery_app, "send_task", _send_task)

    await enqueue_ingest(db, source)

    assert len(published) == 1
    args, task_id = published[0]
    assert args == [str(source.id)]
    assert str(uuid.UUID(task_id)) == task_id, "발행 전에 미리 만든 task id(uuid4)로 발행한다"


async def test_커밋_실패시_태스크가_발행되지_않는다(
    db: AsyncSession, source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    """β 회귀 방지의 핵심 — 커밋이 깨지면 브로커에 아무것도 나가면 안 된다."""
    published: list[object] = []
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: published.append(a))

    async def boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(db, "commit", boom)

    with pytest.raises(RuntimeError, match="commit failed"):
        await enqueue_ingest(db, source)

    assert published == [], "커밋 실패에도 발행되면 추적 행 없는 인제스트가 돈다"


async def test_발행_실패는_삼켜지고_source는_pending으로_남는다(
    db: AsyncSession, source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HCI-R3B-1(a): 발행 실패로 500을 내면 사용자 재시도가 중복 소스를 만든다.

    DB는 이미 커밋됐고 source가 `pending`으로 남아 reaper가 재발행하므로,
    예외를 올리지 않고 로그만 남긴다. (#163 전 이름: `…_job_행은_남는다` — 코어가 job 행을
    더는 쓰지 않아 「발행을 시도했고, 실패를 삼켰고, source가 회수 대상으로 남는다」로 단언한다)
    """
    attempts: list[str] = []

    def boom(name: str, args: list[str], task_id: str, **k: object) -> None:
        attempts.append(args[0])
        raise RuntimeError("broker down")

    monkeypatch.setattr(celery_app, "send_task", boom)

    await enqueue_ingest(db, source)  # 예외가 오르지 않아야 한다

    assert attempts == [str(source.id)], "커밋 뒤 발행을 실제로 시도했다"
    async with async_session_factory() as s:
        assert (await s.get(Source, source.id)).status == "pending", "reaper 회수 대상"


async def test_커밋_실패는_삼켜지지_않는다(
    db: AsyncSession, source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    """발행 실패만 삼킨다 — 커밋 실패는 진짜 실패이므로 전파되어야 한다."""
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: None)

    async def boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(db, "commit", boom)

    with pytest.raises(RuntimeError, match="commit failed"):
        await enqueue_ingest(db, source)


async def test_호출자_UoW_안에서_enqueue_ingest는_중첩이_거부된다(
    db: AsyncSession, source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    """래퍼는 자체 UoW를 연다 — 호출자 UoW 안에서는 stage_ingest_job을 써야 한다."""
    from biznotes_core.core.uow import NestedUnitOfWorkError, unit_of_work

    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: None)

    with pytest.raises(NestedUnitOfWorkError):
        async with unit_of_work(db):
            await enqueue_ingest(db, source)


# --- stage_ingest_job: 호출자 UoW에 참여 (R3-1e의 토대) ---


async def test_stage_ingest_job은_호출자_UoW에_참여한다(
    db: AsyncSession, user_a: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """source 생성과 job 기록이 한 커밋으로 묶이고, 발행은 커밋 후에만 일어난다."""
    from biznotes_core.core.uow import unit_of_work
    from biznotes_core.services.ingestion.enqueue import stage_ingest_job

    published: list[str] = []
    monkeypatch.setattr(
        celery_app, "send_task", lambda name, args, task_id: published.append(args[0])
    )

    nb = Notebook(owner_id=user_a.id, title="t")
    db.add(nb)
    await db.commit()

    async with unit_of_work(db) as uow:
        src = Source(notebook_id=nb.id, type="text", title="s", status="pending")
        db.add(src)
        await db.flush()
        stage_ingest_job(db, src, uow)
        assert published == [], "커밋 전에는 발행되지 않는다"

    assert published == [str(src.id)]


async def test_stage_ingest_job은_커밋_실패시_source와_job을_함께_롤백한다(
    db: AsyncSession, user_a: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """α 해소 입증 — 단일 트랜잭션이므로 부분 쓰기가 없다."""
    from biznotes_core.core.uow import unit_of_work
    from biznotes_core.services.ingestion.enqueue import stage_ingest_job

    published: list[str] = []
    monkeypatch.setattr(
        celery_app, "send_task", lambda name, args, task_id: published.append(args[0])
    )

    nb = Notebook(owner_id=user_a.id, title="t")
    db.add(nb)
    await db.commit()

    with pytest.raises(RuntimeError, match="boom"):
        async with unit_of_work(db) as uow:
            src = Source(notebook_id=nb.id, type="text", title="s", status="pending")
            db.add(src)
            await db.flush()
            src_id = src.id
            stage_ingest_job(db, src, uow)
            raise RuntimeError("boom")

    async with async_session_factory() as s:
        assert await s.get(Source, src_id) is None, "source가 롤백되어야 한다"
    assert published == [], "커밋되지 않았으므로 발행도 없다"


async def test_stage_ingest_job은_flush하지_않은_source를_거부한다(
    db: AsyncSession, user_a: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#163 — job 행 FK가 사라져 미flush source(`id is None`)는 코어가 `ValueError`로 거부한다.

    제품 호출 2곳(`connections.py`·`notes.py`)이 flush 뒤에 부르는 이유다.
    """
    from biznotes_core.core.uow import unit_of_work
    from biznotes_core.services.ingestion.enqueue import stage_ingest_job

    published: list[object] = []
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: published.append(a))
    nb = Notebook(owner_id=user_a.id, title="t")
    db.add(nb)
    await db.commit()

    with pytest.raises(ValueError, match="flush하지 않은 source"):
        async with unit_of_work(db) as uow:
            src = Source(notebook_id=nb.id, type="text", title="s", status="pending")
            db.add(src)
            stage_ingest_job(db, src, uow)  # flush 없이

    assert published == []
