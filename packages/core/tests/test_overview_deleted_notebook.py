"""삭제된 노트북의 개요 생성 태스크(#220) — 노트북 부재를 정상 종료로 처리한다.

태스크 본문(`generate_notebook_overview`·`_run`)은 실경로이고, DB 세션·Redis·
개요 서비스만 가짜다. 실제 FK 위반은 `tests/test_overview_deleted_notebook_pg.py`가 본다.
"""

import asyncio
import uuid
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from structlog.testing import capture_logs

from biznotes_core.services import overview as overview_service
from biznotes_core.workers.tasks import overview

NOTEBOOK_ID = str(uuid.uuid4())


class _PgError(Exception):
    """asyncpg 어댑터 오류 모양 — sqlstate와 원본(__cause__)의 constraint_name."""

    def __init__(self, sqlstate: str, constraint_name: str | None):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate
        if constraint_name is not None:
            cause = Exception(constraint_name)
            cause.constraint_name = constraint_name  # type: ignore[attr-defined]
            self.__cause__ = cause


def _integrity(sqlstate: str, constraint_name: str | None) -> IntegrityError:
    return IntegrityError("INSERT INTO notebook_overviews", {}, _PgError(sqlstate, constraint_name))


NOTEBOOK_FK = _integrity("23503", "notebook_overviews_notebook_id_fkey")


class _Session:
    def __init__(self, notebook_exists: bool):
        self.notebook_exists = notebook_exists
        self.rolled_back = False

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, model: Any, ident: uuid.UUID) -> object | None:
        assert str(ident) == NOTEBOOK_ID
        # 노트북 행 — 사용량 귀속(#366)이 소유자를 읽는다
        notebook = SimpleNamespace(id=ident, owner_id=uuid.uuid4())
        return notebook if self.notebook_exists else None

    async def rollback(self) -> None:
        self.rolled_back = True


class _Redis:
    def __init__(self, token: str | None):
        self.token = token
        self.deleted: list[tuple[str, ...]] = []

    def get(self, key: str) -> bytes | None:
        return self.token.encode() if self.token is not None else None

    def delete(self, *keys: str) -> None:
        self.deleted.append(keys)

    def close(self) -> None:
        return None


def _run_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "session": _Session(notebook_exists=True),
        "error": None,
        "calls": 0,
    }

    async def generate(db: Any, notebook_id: uuid.UUID) -> object | None:
        state["calls"] += 1
        if state["error"] is not None:
            raise state["error"]
        return object()

    monkeypatch.setattr(overview, "get_session_factory", lambda: lambda: state["session"])
    monkeypatch.setattr(overview_service, "generate_overview", generate)
    return state


def test_missing_notebook_skips_without_generating(harness: dict[str, Any]) -> None:
    harness["session"] = _Session(notebook_exists=False)

    with capture_logs() as logs:
        result = _run_sync(overview._run(NOTEBOOK_ID))

    assert result == {"ok": True, "skipped": "notebook_deleted"}
    assert harness["calls"] == 0
    assert [(e["log_level"], e["event"]) for e in logs] == [
        ("info", "overview skipped: notebook deleted")
    ]


def test_notebook_fk_violation_during_generation_is_normal_finish(
    harness: dict[str, Any],
) -> None:
    harness["error"] = NOTEBOOK_FK

    with capture_logs() as logs:
        result = _run_sync(overview._run(NOTEBOOK_ID))

    assert result == {"ok": True, "skipped": "notebook_deleted"}
    assert harness["session"].rolled_back is True
    assert [e["log_level"] for e in logs] == ["info"]


@pytest.mark.parametrize(
    ("sqlstate", "constraint_name"),
    [
        ("23505", "notebook_overviews_notebook_id_key"),  # 유일 제약 — 동시 생성 경합
        ("23503", "some_other_fkey"),  # 다른 FK
        ("23505", "notebook_overviews_notebook_id_fkey"),  # 이름만 같고 SQLSTATE가 다름
        ("23503", None),  # 원본 제약명을 읽을 수 없음 — 추측하지 않는다
    ],
)
def test_other_integrity_errors_propagate(
    harness: dict[str, Any], sqlstate: str, constraint_name: str | None
) -> None:
    error = _integrity(sqlstate, constraint_name)
    harness["error"] = error

    with pytest.raises(IntegrityError) as exc_info:
        _run_sync(overview._run(NOTEBOOK_ID))

    assert exc_info.value is error


def test_task_cleans_token_and_regen_lock_when_notebook_deleted(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    harness["error"] = NOTEBOOK_FK
    client = _Redis(token="t1")
    monkeypatch.setattr(overview.redis_sync.Redis, "from_url", lambda url: client)
    monkeypatch.setattr(overview, "run_async", _run_sync)

    result = overview.generate_notebook_overview.run(NOTEBOOK_ID, "t1")

    assert result == {"ok": True, "skipped": "notebook_deleted"}
    assert client.deleted == [(f"overview:debounce:{NOTEBOOK_ID}", f"overview:regen:{NOTEBOOK_ID}")]
