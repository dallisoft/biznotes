"""라우터·서비스가 태스크를 import하지 않고 이름으로 발행한다 (#321).

`api/v1`이 태스크 모듈(`workers.tasks.*`)을 직접 import하던 네 곳을 `task_names` +
`services.task_publish.publish_task`로 바꿨다. 발행되는 태스크 이름·인자·countdown이 이전
(`apply_async`·`delay`)과 같고, 태스크가 등록된 프로세스에서는 태스크의 `apply_async`(태스크 단위
옵션·eager)를 그대로 거치는지 고정한다. 브로커(`celery_app.send_task`)와 Redis만 가짜다.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import SourcesTable, TableSession, make_settings

from biznotes_core.api.v1 import sources
from biznotes_core.services import overview_refresh, storage
from biznotes_core.services.task_publish import publish_task
from biznotes_core.workers.celery_app import celery_app
from biznotes_core.workers.task_names import (
    GENERATE_NOTEBOOK_OVERVIEW,
    RUN_AGENT,
)
from biznotes_core.workers.tasks import overview as overview_task


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[Any], dict[str, Any]]]:
    """브로커 경계. 등록된 태스크의 `apply_async`도 결국 `app.send_task(name, args, …)`다."""
    calls: list[tuple[str, list[Any], dict[str, Any]]] = []

    def send_task(name: str, args: Any = None, kwargs: Any = None, **options: Any) -> None:
        calls.append((name, list(args or []), options))

    monkeypatch.setattr(celery_app, "send_task", send_task)
    return calls


class _FakeRedis:
    def __init__(self, store: dict[str, str]) -> None:
        self.store = store

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.store.pop(key, None)

    def set(self, key: str, value: str, ex: int, nx: bool = False) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def close(self) -> None:
        pass


@pytest.fixture
def redis_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    store: dict[str, str] = {}
    monkeypatch.setattr(
        overview_refresh.redis_sync.Redis, "from_url", lambda *_a, **_k: _FakeRedis(store)
    )
    return store


def test_worker_module_reexports_the_same_helper() -> None:
    """기존 import 경로(`workers.tasks.overview`)와 토큰 키는 그대로다 — 공개 표면 유지."""
    assert overview_task.schedule_overview_refresh is overview_refresh.schedule_overview_refresh
    assert overview_task.DEBOUNCE_SECONDS == overview_refresh.DEBOUNCE_SECONDS == 10
    key = "overview:debounce:{notebook_id}"
    assert overview_task._TOKEN_KEY == overview_refresh.TOKEN_KEY == key


def test_schedule_publishes_by_name_with_token_and_countdown(sent, redis_store) -> None:
    overview_refresh.schedule_overview_refresh("nb-1")
    [(name, args, options)] = sent
    assert name == GENERATE_NOTEBOOK_OVERVIEW
    assert args == ["nb-1", redis_store["overview:debounce:nb-1"]]
    assert options["countdown"] == 10

    overview_refresh.schedule_overview_refresh("nb-1", countdown=0)  # 수동 재생성
    assert sent[1][2]["countdown"] == 0
    assert sent[1][1][1] == redis_store["overview:debounce:nb-1"]  # 토큰을 갱신했다


def test_registered_task_goes_through_its_own_apply_async(monkeypatch, redis_store) -> None:
    """태스크가 등록된 프로세스(워커 등)에서는 태스크 객체의 `apply_async`를 거친다 — 태스크 단위
    발행 옵션(annotation·ignore_result 등)이 직접 호출과 같게 붙는다(#321 리뷰)."""
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        overview_task.generate_notebook_overview,
        "apply_async",
        # 서명은 인자를 튜플로 넘긴다 — JSON 직렬화 결과(배열)는 예전 list와 같다
        lambda args, kwargs, **options: calls.append((list(args), kwargs, options)),
    )
    overview_refresh.schedule_overview_refresh("nb-1")
    assert calls == [(["nb-1", redis_store["overview:debounce:nb-1"]], {}, {"countdown": 10})]


def test_always_eager_runs_the_task_in_process(sent, monkeypatch, redis_store) -> None:
    """`task_always_eager`면 예전처럼 태스크를 동기 실행하고 브로커에 발행하지 않는다(#321 리뷰)."""
    ran: list[str] = []

    def run_async(coro: Any) -> dict[str, Any]:
        coro.close()
        ran.append("run")
        return {"ok": True}

    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(overview_task, "run_async", run_async)

    overview_refresh.schedule_overview_refresh("nb-1", countdown=0)

    assert ran == ["run"]
    assert sent == []
    assert "overview:debounce:nb-1" not in redis_store  # 태스크 본문이 토큰을 정리했다


def test_unregistered_task_falls_back_to_send_task_by_name(sent) -> None:
    """이 프로세스에 없는 태스크(제품 소유 `run_agent`)는 이름 발행으로 떨어진다."""
    assert RUN_AGENT not in celery_app.tasks
    publish_task(RUN_AGENT, args=["run-1"], countdown=5)
    assert sent == [(RUN_AGENT, ["run-1"], {"countdown": 5})]


def test_poll_path_does_not_reschedule_a_pending_refresh(sent, redis_store) -> None:
    redis_store["overview:debounce:nb-1"] = "pending"
    overview_refresh.schedule_overview_refresh("nb-1", debounce=False)
    assert sent == []
    assert redis_store["overview:debounce:nb-1"] == "pending"


async def test_delete_source_publishes_nothing(sent, monkeypatch) -> None:
    """청크 벡터는 행과 함께 CASCADE로 사라진다(pgvector) — 소스 삭제는 태스크를 내지 않는다."""

    async def permitted(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(sources, "_notebook_with_permission", permitted)
    monkeypatch.setattr(sources, "get_settings", lambda: make_settings())
    monkeypatch.setattr(storage, "delete_objects", lambda b, keys: [])
    table = SourcesTable(status="ready")

    await sources.delete_source(table.row["id"], user=object(), db=TableSession(table))

    assert sent == []


class _ExportSession:
    """`publishing_unit_of_work`가 쓰는 만큼만 흉내 낸다 — 커밋 전 발행이 없는지 본다."""

    def __init__(self, sent: list[tuple[str, dict[str, Any]]]) -> None:
        self.info: dict[str, Any] = {}
        self.sent = sent
        self.sent_before_commit: int | None = None
        self.row: Any = None

    def add(self, row: Any) -> None:
        self.row = row

    async def flush(self) -> None:
        self.row.id = uuid.uuid4()
        self.row.requested_at = datetime.now(UTC)

    async def commit(self) -> None:
        self.sent_before_commit = len(self.sent)

    async def rollback(self) -> None:
        pass

    async def refresh(self, row: Any) -> None:
        pass

