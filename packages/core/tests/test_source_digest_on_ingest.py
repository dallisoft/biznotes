"""인제스트 완료 때 소스 요약 미리 만들기(#378) — 예약·디바운스·설정 토글·브로커 계약.

색인 교체는 `conftest.RunHarness`(메모리 DB·스토리지)로 돌린다. 브로커(`celery_app.send_task`)와
Redis만 가짜다. 실제 캐시 적중·재인제스트 경합·사용량 귀속은
`tests/test_source_digest_on_ingest_pg.py`가 본다.
"""

from typing import Any

import pytest
from conftest import RunHarness

from biznotes_core.services import source_digest_refresh
from biznotes_core.workers import task_names
from biznotes_core.workers.celery_app import celery_app
from biznotes_core.workers.tasks import source_digest as digest_task

MARKDOWN = "# 제목\n\n본문 문단입니다. " * 20


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

    def set(self, key: str, value: str, ex: int) -> bool:
        self.store[key] = value
        return True

    def close(self) -> None:
        pass


@pytest.fixture
def redis_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    store: dict[str, str] = {}

    def from_url(*_a: Any, **_k: Any) -> _FakeRedis:
        return _FakeRedis(store)

    monkeypatch.setattr(source_digest_refresh.redis_sync.Redis, "from_url", from_url)
    return store


@pytest.fixture
def scheduled(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """색인 교체가 부른 예약 — 실제 예약 헬퍼 대신 소스 id만 모은다."""
    calls: list[str] = []
    monkeypatch.setattr(source_digest_refresh, "schedule_digest_refresh", calls.append)
    return calls


def _harness(run_harness: RunHarness, *, enabled: bool | None) -> RunHarness:
    from biznotes_core.services.ingestion.parsers import ParseResult

    run_harness.parser(lambda data: ParseResult(markdown=MARKDOWN))
    if enabled is not None:
        run_harness.settings.source_digest_on_ingest = enabled
    return run_harness


# ── 브로커 계약 ───────────────────────────────────────────────────────────


def test_task_is_registered_routed_to_ingestion_and_imported_by_the_core_app() -> None:
    name = task_names.GENERATE_SOURCE_DIGEST

    assert name == "app.workers.tasks.source_digest.generate_source_digest"
    assert digest_task.generate_source_digest.name == name
    assert celery_app.amqp.router.route({}, name)["queue"].name == "ingestion"
    # 코어 앱을 쓰는 워커는 모듈을 따로 import하지 않아도 이 태스크를 등록한다
    assert "biznotes_core.workers.tasks.source_digest" in celery_app.conf.imports


def test_setting_is_off_by_default() -> None:
    from biznotes_core.config import CoreSettings

    assert CoreSettings.model_fields["source_digest_on_ingest"].default is False


# ── 색인 교체 뒤 예약 ─────────────────────────────────────────────────────


@pytest.mark.parametrize("enabled", [None, False])
async def test_index_completion_does_not_schedule_when_off(run_harness, scheduled, enabled):
    h = _harness(run_harness, enabled=enabled)

    result = await h.run()

    assert result["ok"] is True
    assert scheduled == []


async def test_index_completion_schedules_the_source_when_on(run_harness, scheduled):
    h = _harness(run_harness, enabled=True)

    result = await h.run()

    assert result["ok"] is True
    assert h.committed[-1]["status"] == "ready"
    assert scheduled == [str(h.source.id)]


async def test_schedule_failure_keeps_the_ready_index(run_harness, monkeypatch):
    h = _harness(run_harness, enabled=True)

    def boom(source_id: str) -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(source_digest_refresh, "schedule_digest_refresh", boom)

    result = await h.run()

    assert result["ok"] is True
    assert h.committed[-1]["status"] == "ready"
    assert h.closed_reindex[-1][1] is True


# ── 디바운스 ─────────────────────────────────────────────────────────────


def test_schedule_publishes_by_name_with_a_fresh_token(sent, redis_store) -> None:
    source_digest_refresh.schedule_digest_refresh("src-1")
    source_digest_refresh.schedule_digest_refresh("src-1")

    key = "source_digest:debounce:src-1"
    assert [(name, options["countdown"]) for name, _, options in sent] == [
        (task_names.GENERATE_SOURCE_DIGEST, 10),
        (task_names.GENERATE_SOURCE_DIGEST, 10),
    ]
    first, second = (args for _, args, _ in sent)
    assert first[0] == second[0] == "src-1"
    assert first[1] != second[1] == redis_store[key]  # 두 번째 예약이 토큰을 갈아 끼웠다


@pytest.fixture
def task_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """태스크의 토큰 저장소와 본문(`_run`) 실행 기록 — 실제 생성은 PG 테스트가 본다."""
    env: dict[str, Any] = {"store": {}, "ran": []}

    def run_async(coro: Any) -> dict[str, Any]:
        coro.close()
        env["ran"].append("run")
        return {"ok": True}

    def from_url(*_a: Any, **_k: Any) -> _FakeRedis:
        return _FakeRedis(env["store"])

    monkeypatch.setattr(digest_task, "run_async", run_async)
    monkeypatch.setattr(digest_task.redis_sync.Redis, "from_url", from_url)
    return env


def test_only_the_latest_reservation_generates(task_env) -> None:
    """짧게 여러 번 재인제스트해도 마지막 예약 하나만 LLM을 부른다(수락 기준 3)."""
    key = "source_digest:debounce:src-1"
    task_env["store"][key] = "t2"  # t1 뒤에 t2가 예약됐다

    assert digest_task.generate_source_digest("src-1", "t1") == {
        "ok": True, "skipped": "superseded"
    }
    assert task_env["ran"] == []
    assert digest_task.generate_source_digest("src-1", "t2") == {"ok": True}
    assert task_env["ran"] == ["run"]
    # 토큰을 지우지 않는다 — 실행 중 새로 예약된 토큰을 지워 그 예약의 최신 판정을 잃지 않게
    assert task_env["store"][key] == "t2"


def test_expired_token_still_generates(task_env) -> None:
    assert digest_task.generate_source_digest("src-1", "t1") == {"ok": True}
    assert task_env["ran"] == ["run"]


async def test_turning_the_setting_off_skips_queued_reservations(monkeypatch) -> None:
    from conftest import make_settings

    monkeypatch.setattr(
        digest_task, "get_settings", lambda: make_settings(source_digest_on_ingest=False)
    )

    assert await digest_task._run("00000000-0000-0000-0000-000000000001") == {
        "ok": True, "skipped": "disabled"
    }
