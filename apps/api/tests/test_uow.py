"""`app/core/uow.py` 계약 테스트 (R3-1a).

설계 문서 §2 "콜백 계약" 표의 각 규정을 테스트 하나로 고정한다
(`docs/plans/refactoring/design/r3-0-transaction-architecture.md`).
"""

import asyncio
import uuid

import pytest
from biznotes_core.core.db import async_session_factory
from biznotes_core.core.uow import AfterCommitError, NestedUnitOfWorkError, unit_of_work
from biznotes_core.models import Tag, User
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def _tag_count(name: str) -> int:
    """UoW 세션 밖의 독립 세션으로 실제 영속 여부를 확인한다."""
    async with async_session_factory() as s:
        return (
            await s.execute(select(func.count()).select_from(Tag).where(Tag.name == name))
        ).scalar_one()


# --- 계약: 실행 시점 (정상 종료 시 1회 커밋) ---


async def test_정상_종료시_커밋된다(db: AsyncSession, user_a: User) -> None:
    name = f"uow-commit-{uuid.uuid4().hex[:8]}"
    async with unit_of_work(db) as uow:
        db.add(Tag(owner_id=user_a.id, name=name))
        assert uow is not None

    assert await _tag_count(name) == 1


async def test_블록_내_예외시_롤백된다(db: AsyncSession, user_a: User) -> None:
    name = f"uow-rollback-{uuid.uuid4().hex[:8]}"
    with pytest.raises(RuntimeError, match="boom"):
        async with unit_of_work(db):
            db.add(Tag(owner_id=user_a.id, name=name))
            raise RuntimeError("boom")

    assert await _tag_count(name) == 0


async def test_예외시_콜백은_실행되지_않는다(db: AsyncSession, user_a: User) -> None:
    """커밋이 성사되지 않았으므로 부수효과도 발행되면 안 된다(β 방지의 핵심)."""
    calls: list[str] = []
    with pytest.raises(RuntimeError):
        async with unit_of_work(db) as uow:
            db.add(Tag(owner_id=user_a.id, name=f"uow-{uuid.uuid4().hex[:8]}"))
            uow.after_commit(lambda: calls.append("published"))
            raise RuntimeError("boom")

    assert calls == []


# --- 계약: 시그니처 (동기·비동기 콜백 모두 허용) ---


async def test_동기_콜백과_비동기_콜백을_모두_실행한다(db: AsyncSession) -> None:
    calls: list[str] = []

    async def async_cb() -> None:
        calls.append("async")

    async with unit_of_work(db) as uow:
        uow.after_commit(lambda: calls.append("sync"))
        uow.after_commit(async_cb)

    assert calls == ["sync", "async"]


# --- 계약: 실행 시점 (등록 순서대로 순차 실행) ---


async def test_콜백은_등록_순서대로_실행된다(db: AsyncSession) -> None:
    calls: list[int] = []
    async with unit_of_work(db) as uow:
        for i in range(5):
            uow.after_commit(lambda i=i: calls.append(i))

    assert calls == [0, 1, 2, 3, 4]


async def test_콜백은_커밋_이후에_실행된다(db: AsyncSession, user_a: User) -> None:
    """콜백 시점에 이미 다른 세션에서 행이 보여야 한다 — 커밋 트랜잭션 '밖'."""
    name = f"uow-order-{uuid.uuid4().hex[:8]}"
    seen: list[int] = []

    async def observe() -> None:
        seen.append(await _tag_count(name))

    async with unit_of_work(db) as uow:
        db.add(Tag(owner_id=user_a.id, name=name))
        uow.after_commit(observe)

    assert seen == [1]


# --- 계약: 예외 정책 (일부 실패해도 나머지 실행, 종료 시 AfterCommitError) ---


async def test_콜백_실패해도_나머지_콜백을_실행하고_AfterCommitError를_올린다(
    db: AsyncSession,
) -> None:
    calls: list[str] = []

    def boom() -> None:
        raise ValueError("publish failed")

    with pytest.raises(AfterCommitError) as ei:
        async with unit_of_work(db) as uow:
            uow.after_commit(boom)
            uow.after_commit(lambda: calls.append("second"))

    assert calls == ["second"], "첫 콜백 실패가 뒤따르는 콜백을 건너뛰면 안 된다"
    assert len(ei.value.failures) == 1
    assert isinstance(ei.value.failures[0], ValueError)


async def test_콜백_실패시_DB_쓰기는_남는다_at_most_once(
    db: AsyncSession, user_a: User
) -> None:
    """커밋 후 콜백 실패는 롤백 불가 — DB는 커밋, 부수효과만 누락(회수는 reaper 몫)."""
    name = f"uow-atmostonce-{uuid.uuid4().hex[:8]}"

    def boom() -> None:
        raise ValueError("publish failed")

    with pytest.raises(AfterCommitError):
        async with unit_of_work(db) as uow:
            db.add(Tag(owner_id=user_a.id, name=name))
            uow.after_commit(boom)

    assert await _tag_count(name) == 1


# --- 계약: 세션 재사용 금지 (콜백에 UoW 세션을 넘기지 않는다) ---


async def test_콜백은_인자를_받지_않는다_세션_미전달(db: AsyncSession) -> None:
    """콜백은 무인자로 호출된다 — 커밋된 세션이 새 트랜잭션을 여는 것을 막는다."""
    received: list[tuple] = []

    def cb(*args: object, **kwargs: object) -> None:
        received.append((args, tuple(kwargs)))

    async with unit_of_work(db) as uow:
        uow.after_commit(cb)

    assert received == [((), ())]


# --- 계약: 워커 오용 방지 (전역 상태 없음 — 세션을 명시 인자로 받음) ---


async def test_두_UoW는_콜백을_공유하지_않는다(db: AsyncSession) -> None:
    """전역 상태가 없으므로 UoW 인스턴스 간 콜백이 새지 않는다."""
    calls: list[str] = []
    async with unit_of_work(db) as uow1:
        uow1.after_commit(lambda: calls.append("first"))
    async with unit_of_work(db) as uow2:
        uow2.after_commit(lambda: calls.append("second"))

    assert calls == ["first", "second"]


# --- 계약: 취소(CancelledError는 Exception이 아니다) — R31CR-01 ---


async def test_취소시_롤백되고_세션에_열린_트랜잭션이_남지_않는다(
    db: AsyncSession, user_a: User
) -> None:
    """`except Exception`만 잡으면 취소 시 열린 트랜잭션이 세션에 남는다."""
    name = f"uow-cancel-{uuid.uuid4().hex[:8]}"

    async def work() -> None:
        async with unit_of_work(db):
            db.add(Tag(owner_id=user_a.id, name=name))
            await asyncio.sleep(5)

    task = asyncio.create_task(work())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _tag_count(name) == 0
    assert not db.in_transaction(), "취소 후 열린 트랜잭션이 남으면 커넥션이 묶인다"


async def test_콜백_취소는_수집되지_않고_즉시_전파된다(db: AsyncSession) -> None:
    """취소는 '이 콜백이 실패했다'가 아니라 '더 진행하지 말라'는 신호다."""
    calls: list[str] = []

    async def cancelled() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        async with unit_of_work(db) as uow:
            uow.after_commit(cancelled)
            uow.after_commit(lambda: calls.append("should not run"))

    assert calls == [], "취소 후 남은 부수효과를 계속 발행하면 안 된다"


# --- 계약: 중첩 금지 — R31CR-02 ---


async def test_중첩_UoW는_거부된다(db: AsyncSession) -> None:
    with pytest.raises(NestedUnitOfWorkError):
        async with unit_of_work(db):
            async with unit_of_work(db):
                pass


async def test_UoW_종료_후에는_다시_열_수_있다(db: AsyncSession) -> None:
    """중첩 마커가 정상/예외 경로 모두에서 해제되어야 한다."""
    async with unit_of_work(db):
        pass
    with pytest.raises(RuntimeError):
        async with unit_of_work(db):
            raise RuntimeError("boom")
    async with unit_of_work(db):  # 마커가 남아 있으면 여기서 NestedUnitOfWorkError
        pass


async def test_중첩이_바깥의_미커밋_변경을_조기_커밋하지_않는다(
    db: AsyncSession, user_a: User
) -> None:
    """거부가 없으면 안쪽 커밋이 바깥 행까지 영속시켜 α가 되살아난다."""
    outer = f"uow-outer-{uuid.uuid4().hex[:8]}"

    with pytest.raises(NestedUnitOfWorkError):
        async with unit_of_work(db):
            db.add(Tag(owner_id=user_a.id, name=outer))
            async with unit_of_work(db):
                pass

    assert await _tag_count(outer) == 0


# --- 계약: 롤백 실패가 원래 예외를 가리지 않는다 — R31CR-04 ---


async def test_롤백_실패시에도_원래_예외가_전파된다(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def rollback_boom() -> None:
        raise RuntimeError("rollback failed")

    monkeypatch.setattr(db, "rollback", rollback_boom)

    with pytest.raises(ValueError, match="original cause"):
        async with unit_of_work(db):
            raise ValueError("original cause")


# --- publishing_unit_of_work: 커밋 후 콜백 실패만 삼킨다 (R32CR-05) ---


async def test_publishing_uow는_콜백_실패를_삼킨다(db: AsyncSession, user_a: User) -> None:
    from biznotes_core.core.uow import publishing_unit_of_work

    name = f"pub-swallow-{uuid.uuid4().hex[:8]}"

    def boom() -> None:
        raise RuntimeError("broker down")

    async with publishing_unit_of_work(db) as uow:  # 예외가 오르지 않아야 한다
        db.add(Tag(owner_id=user_a.id, name=name))
        uow.after_commit(boom)

    assert await _tag_count(name) == 1, "DB는 커밋된 채로 남는다"


async def test_publishing_uow는_호출자_블록의_AfterCommitError를_삼키지_않는다(
    db: AsyncSession,
) -> None:
    """이 헬퍼가 삼키는 것은 자신의 커밋 이후 콜백 실패뿐이다."""
    from biznotes_core.core.uow import publishing_unit_of_work

    with pytest.raises(AfterCommitError):
        async with publishing_unit_of_work(db):
            raise AfterCommitError([RuntimeError("from caller body")])


async def test_publishing_uow는_커밋_실패를_삼키지_않는다(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biznotes_core.core.uow import publishing_unit_of_work

    async def boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(db, "commit", boom)

    with pytest.raises(RuntimeError, match="commit failed"):
        async with publishing_unit_of_work(db):
            pass
