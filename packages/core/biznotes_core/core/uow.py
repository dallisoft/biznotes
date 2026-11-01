"""명시적 트랜잭션 경계(Unit of Work)와 커밋 후 부수효과 훅 (R3-1a).

`get_db()`는 세션만 제공하고 트랜잭션 경계를 정하지 않는다. 라우터·서비스가
각자 `.commit()`을 부르다 보니 두 종류의 결함이 생겼다
(`docs/plans/refactoring/design/r3-0-transaction-architecture.md` §1.3·§1.4):

- 결함 α — 한 요청에서 커밋이 2회 이상이라, 중간에 실패하면 앞부분만 남는다.
- 결함 β — 커밋과 브로커 발행이 원자적이지 않다. 특히 `enqueue_ingest`는
  `delay()` 후에 `commit()` 해서, 커밋이 실패해도 태스크는 이미 발행돼 있다.

`unit_of_work`는 블록을 단일 커밋으로 묶어 α를 없애고, `after_commit`으로
등록한 부수효과를 **커밋이 성공한 뒤에만** 실행해 β의 발행 순서를 뒤집는다.

β가 완전히 사라지는 것은 아니다. 커밋 후 콜백이 실패하면 DB는 남고 부수효과만
누락되는 at-most-once가 된다. 따라서 `after_commit`은 **누락을 회수할 장치가
있는 대상에만** 쓴다(예: `_reap_stalled`가 재발행하는 인제스트). 회수 장치가
없는 대상은 reaper를 함께 만들어야 한다 — 설계 문서 §2 회수 전략 표 참조.

주의: 워커(Celery)는 별도 이벤트 루프·자체 세션 팩토리를 쓰므로 이 모듈의
적용 대상이 아니다. 전역 상태 없이 `AsyncSession`을 인자로 받는 명시 호출만
지원하는 것도 그 오용을 막기 위해서다.
"""

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

AfterCommitCallback = Callable[[], Awaitable[Any] | Any]

# 세션에 활성 UoW가 있는지 표시하는 키 (중첩 진입 거부용)
_ACTIVE_KEY = "_uow_active"


def unique_violation_constraint_name(error: IntegrityError) -> str | None:
    """asyncpg 고정 경로에서 23505 제약명을 보수적으로 추출한다.

    다른 SQLSTATE이거나 원본 cause/제약명을 읽을 수 없으면 추측하지 않고 None을
    반환한다. 호출자는 허용 제약명과 대조한 뒤 원래 예외를 재전파해야 한다.
    """
    if getattr(error.orig, "sqlstate", None) != "23505":
        return None
    cause = getattr(error.orig, "__cause__", None)
    constraint_name = getattr(cause, "constraint_name", None)
    return constraint_name if isinstance(constraint_name, str) else None


class NestedUnitOfWorkError(RuntimeError):
    """같은 세션에서 `unit_of_work`를 중첩해 열려고 했다.

    안쪽 블록의 `commit()`은 세션 전체를 커밋하므로 바깥 블록의 미완료 변경까지
    영속시킨다. 그 뒤 바깥이 실패해도 롤백할 것이 남아 있지 않다 — UoW가 막으려던
    조기 커밋(α)이 그대로 재현된다.
    """


class AfterCommitError(Exception):
    """커밋은 성공했으나 커밋 후 부수효과가 하나 이상 실패했다.

    DB는 이미 커밋되어 롤백할 수 없다. 호출자는 이 예외를 "쓰기는 남았고
    부수효과만 누락됐다"는 신호로 다뤄야 한다 — 재시도 시 DB 쓰기를 다시
    하면 중복이 된다.
    """

    def __init__(self, failures: list[BaseException]):
        self.failures = failures
        super().__init__(
            f"커밋 후 부수효과 {len(failures)}건 실패 — DB는 커밋됨 (첫 실패: {failures[0]!r})"
        )


class UnitOfWork:
    """`unit_of_work` 블록의 핸들. 커밋 후 실행할 부수효과를 등록한다."""

    def __init__(self, session: AsyncSession):
        # 세션은 블록 안에서 커밋/롤백하기 위해서만 보관한다. 콜백에는 넘기지
        # 않는다 — 이미 커밋된 세션을 재사용하면 새 트랜잭션이 조용히 열린다.
        self._session = session
        self._callbacks: list[AfterCommitCallback] = []

    def after_commit(self, fn: AfterCommitCallback) -> None:
        """커밋 성공 직후, 커밋 트랜잭션 밖에서 실행할 콜백을 등록한다.

        동기·비동기 콜백을 모두 받는다. 등록 순서대로 순차 실행된다.
        DB를 갱신해야 하면 콜백 안에서 **새 세션**을 열어라.
        """
        self._callbacks.append(fn)

    async def _run_callbacks(self) -> None:
        """등록된 콜백을 순서대로 실행한다.

        하나가 실패해도 나머지를 계속 실행한다 — 콜백끼리는 독립적이고, 첫
        실패가 뒤따르는 발행을 조용히 건너뛰게 두면 누락 범위가 커진다.
        모두 시도한 뒤 실패가 있으면 한데 묶어 raise한다.

        여기서 `Exception`만 잡는 것은 의도적이다. 취소(`CancelledError`)와
        인터프리터 종료(`KeyboardInterrupt`, `SystemExit`)는 `BaseException`
        계열이라 이 절에 걸리지 않고 그대로 전파된다 — "이 콜백이 실패했다"가
        아니라 "더 진행하지 말라"는 신호이므로, 수집하지 않고 남은 부수효과
        발행을 즉시 중단하는 것이 옳다.
        """
        failures: list[BaseException] = []
        for fn in self._callbacks:
            try:
                result = fn()
                if inspect.isawaitable(result):
                    await result
            except Exception as e:  # noqa: BLE001 — 개별 콜백 실패를 격리해 수집
                logger.warning(
                    "after_commit 콜백 실패 — DB는 커밋됨(부수효과 누락)",
                    callback=getattr(fn, "__name__", repr(fn)),
                    error=str(e),
                )
                failures.append(e)
        if failures:
            raise AfterCommitError(failures)


@asynccontextmanager
async def unit_of_work(session: AsyncSession) -> AsyncIterator[UnitOfWork]:
    """블록을 단일 트랜잭션으로 묶는다 — 정상 종료 시 1회 커밋, 예외 시 롤백.

        async with unit_of_work(db) as uow:
            db.add(source)
            uow.after_commit(lambda: ingest_source.delay(str(source.id)))

    커밋이 실패하면 콜백은 실행되지 않는다. 커밋 후 콜백이 실패하면
    `AfterCommitError`가 오르지만 DB 쓰기는 그대로 남는다(롤백 불가).

    블록 안에서 `session.commit()`을 직접 부르지 말 것 — 그러면 조기 커밋(α)이
    되살아난다. 같은 이유로 **중첩도 금지**한다(`NestedUnitOfWorkError`).
    안쪽 블록의 커밋이 바깥 블록의 미완료 변경까지 함께 커밋해버려, 바깥이
    실패해도 되돌릴 수 없기 때문이다. UoW를 여는 서비스 함수를 다른 UoW 안에서
    부르지 말고, 호출자가 경계를 소유하도록 구조를 잡아라.
    """
    if session.info.get(_ACTIVE_KEY):
        raise NestedUnitOfWorkError(
            "unit_of_work를 중첩할 수 없다 — 안쪽 커밋이 바깥의 미완료 변경까지 "
            "커밋해 단일 트랜잭션 계약이 깨진다. 트랜잭션 경계를 호출자로 올려라."
        )
    session.info[_ACTIVE_KEY] = True

    uow = UnitOfWork(session)
    try:
        yield uow
        await session.commit()
    except BaseException:
        # BaseException으로 잡는다 — `asyncio.CancelledError`는 Exception이 아니다.
        # 클라이언트 연결 끊김으로 요청이 취소되면 세션은 열린 트랜잭션을 든 채
        # 남고(`in_transaction() == True`), 커넥션이 `idle in transaction`으로
        # 묶인다. 세션이 닫힐 때 풀이 롤백해 주긴 하지만, 그 사이 같은 세션을
        # 재사용하는 코드는 죽은 트랜잭션의 미커밋 행을 본다.
        try:
            await session.rollback()
        except Exception:
            # 롤백 실패가 원래 원인을 가리면 운영에서 디버깅이 불가능해진다.
            # 로깅만 하고 원래 예외를 그대로 올린다(rollback 예외는 __context__로 남는다).
            logger.exception("unit_of_work 롤백 실패 — 원래 예외를 보존한다")
        raise
    finally:
        session.info.pop(_ACTIVE_KEY, None)

    await uow._run_callbacks()


@asynccontextmanager
async def publishing_unit_of_work(session: AsyncSession) -> AsyncIterator[UnitOfWork]:
    """`unit_of_work`와 같되, **커밋 후 부수효과 실패를 삼키고 로그만 남긴다**.

    브로커 발행처럼 "커밋이 끝난 뒤 실행되며, 실패해도 reaper가 회수하는"
    부수효과를 등록하는 경계에 쓴다 (HCI-R3B-1(a) 인간 승인, 2026-07-09).

    근거: `after_commit` 시점에 DB는 이미 커밋됐다. 여기서 예외를 올리면 라우터가
    500을 반환하는데 리소스는 생성돼 있어, 사용자가 재시도하면 **중복 리소스**가
    만들어진다. 반면 발행 누락은 주기적 reaper가 회수하므로 사용자에게 실패를
    알릴 이유가 없다. **회수 장치가 없는 부수효과에는 쓰지 말 것** — 그 경우
    누락이 영구 유실이 되므로 `unit_of_work`로 실패를 전파해야 한다.

    커밋 실패는 삼키지 않는다 — 그건 진짜 실패다. 호출자 블록 안에서 던진
    `AfterCommitError`(예: 중첩된 다른 UoW의 콜백 실패)도 삼키지 않는다 —
    이 헬퍼가 삼키는 것은 **자신의 커밋 이후 콜백 단계**에서 난 실패뿐이다.
    """
    body_completed = False
    try:
        async with unit_of_work(session) as uow:
            yield uow
            body_completed = True  # 이후 발생하는 예외는 commit/콜백 단계의 것
    except AfterCommitError:
        if not body_completed:
            raise  # 호출자 블록에서 올라온 것 — 이 헬퍼의 책임이 아니다
        logger.error("커밋 후 부수효과 실패 — 행은 커밋됨, reaper가 회수한다")


__all__ = [
    "AfterCommitError",
    "NestedUnitOfWorkError",
    "UnitOfWork",
    "publishing_unit_of_work",
    "unit_of_work",
    "unique_violation_constraint_name",
]
