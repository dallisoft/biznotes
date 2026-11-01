"""에이전트 run 숨김 상태(#371) — 열 계약과 숨기기·해제 규칙.

DB 동작(사용자 삭제 시 SET NULL, 목록 조건)은
`tests/test_agent_run_hidden_pg.py`가 본다.
"""

import uuid
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from biznotes_core.models import AgentRun, AuditLog
from biznotes_core.services import agent_runs
from biznotes_core.services.agent_runs import (
    ACTIVE_RUN_STATUSES,
    RunNotFinished,
    run_is_visible,
    set_run_hidden,
)


class _Db:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)


def _run(status: str = "failed") -> AgentRun:
    return AgentRun(id=uuid.uuid4(), status=status)


def test_columns_are_nullable_and_hidden_by_is_set_null() -> None:
    cols = AgentRun.__table__.c
    assert cols.hidden_at.nullable and cols.hidden_by.nullable
    assert cols.hidden_at.server_default is None and cols.hidden_by.server_default is None
    [fk] = cols.hidden_by.foreign_keys
    assert (fk.target_fullname, fk.ondelete, fk.name) == (
        "users.id",
        "SET NULL",
        "fk_agent_runs_hidden_by",
    )
    ddl = str(CreateTable(AgentRun.__table__).compile(dialect=postgresql.dialect()))
    assert "hidden_at TIMESTAMP WITH TIME ZONE" in ddl
    assert "hidden_by UUID" in ddl
    assert (
        "CONSTRAINT fk_agent_runs_hidden_by FOREIGN KEY(hidden_by) "
        "REFERENCES users (id) ON DELETE SET NULL"
    ) in ddl


async def test_hide_then_unhide_records_actor_and_audit() -> None:
    db, run, actor = _Db(), _run(), uuid.uuid4()

    assert await set_run_hidden(db, run, hidden=True, actor_id=actor)  # type: ignore[arg-type]
    assert isinstance(run.hidden_at, datetime) and run.hidden_at.tzinfo is not None
    assert run.hidden_by == actor

    other = uuid.uuid4()
    assert await set_run_hidden(db, run, hidden=False, actor_id=other)  # type: ignore[arg-type]
    assert (run.hidden_at, run.hidden_by) == (None, None)

    audits = [(a.user_id, a.action, a.resource) for a in db.added if isinstance(a, AuditLog)]
    assert audits == [
        (actor, "agent_run.hide", str(run.id)),
        (other, "agent_run.unhide", str(run.id)),
    ]


async def test_same_state_is_a_no_op() -> None:
    db, run = _Db(), _run()
    assert not await set_run_hidden(db, run, hidden=False, actor_id=uuid.uuid4())  # type: ignore[arg-type]
    await set_run_hidden(db, run, hidden=True, actor_id=uuid.uuid4())  # type: ignore[arg-type]
    first = (run.hidden_at, run.hidden_by)
    assert not await set_run_hidden(db, run, hidden=True, actor_id=uuid.uuid4())  # type: ignore[arg-type]
    assert (run.hidden_at, run.hidden_by) == first  # 처음 숨긴 사람·시각을 지킨다
    assert len(db.added) == 1


@pytest.mark.parametrize("status", ACTIVE_RUN_STATUSES)
async def test_running_runs_cannot_be_hidden(status: str) -> None:
    db, run = _Db(), _run(status)
    with pytest.raises(RunNotFinished):
        await set_run_hidden(db, run, hidden=True, actor_id=uuid.uuid4())  # type: ignore[arg-type]
    assert (run.hidden_at, db.added) == (None, [])


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
async def test_finished_runs_can_be_hidden(status: str) -> None:
    assert await set_run_hidden(_Db(), _run(status), hidden=True, actor_id=uuid.uuid4())  # type: ignore[arg-type]


def test_visibility_condition_is_hidden_at_null() -> None:
    assert str(run_is_visible()) == "agent_runs.hidden_at IS NULL"


def test_active_status_set_is_shared_with_the_schedule_guard() -> None:
    assert ACTIVE_RUN_STATUSES == ("queued", "running", "awaiting_approval")
    assert {"RunNotFinished", "set_run_hidden", "run_is_visible"} <= set(agent_runs.__all__)
