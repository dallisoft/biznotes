"""기존 소스 요약 백필(#454) — 태스크(재개·상한·진행률·귀속), 예약 중복 방지, run 예약, 관리 API.

모델 호출(`complete_stream_with_fallback`)·DB(대상 소스 목록·지문·캐시·청크·저장)·Redis·브로커는
대역이다. 실제 DB 대상 조회·캐시 저장·실 Redis 장부는 `tests/test_source_digest_backfill_pg.py`가
본다.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from biznotes_core.api import deps
from biznotes_core.api.v1.admin import source_digest as admin_digest
from biznotes_core.config import get_settings
from biznotes_core.core.db import get_db
from biznotes_core.models import Notebook, User
from biznotes_core.models.audit import AuditLog
from biznotes_core.services import source_digest as sd
from biznotes_core.services import source_digest_backfill as backfill
from biznotes_core.services.agents.nodes import digest as digest_node
from biznotes_core.services.agents.nodes.digest import _exec_source_digest
from biznotes_core.services.gateway.llm import GatewayCallError
from biznotes_core.workers import task_names
from biznotes_core.workers.celery_app import celery_app
from biznotes_core.workers.tasks import source_digest as digest_task
from tests.test_source_digest import FakeLLM, Store, _ctx, _Db, _src


class FakeRedis:
    """백필 상태 대역 — 문자열 값·해시·TTL만."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        value = self.data.get(key)
        return value if isinstance(value, str) else None

    async def delete(self, key: str) -> int:
        self.ttls.pop(key, None)
        return 1 if self.data.pop(key, None) is not None else 0

    async def expire(self, key: str, seconds: int) -> bool:
        if key not in self.data:
            return False
        self.ttls[key] = seconds
        return True

    async def hget(self, key: str, field: str) -> str | None:
        value = self.data.get(key, {}).get(field)
        return None if value is None else str(value)

    async def hincrby(self, key: str, field: str, amount: int) -> int:
        bucket = self.data.setdefault(key, {})
        bucket[field] = int(bucket.get(field, 0)) + amount
        return int(bucket[field])

    async def hincrbyfloat(self, key: str, field: str, amount: float) -> float:
        bucket = self.data.setdefault(key, {})
        bucket[field] = float(bucket.get(field, 0)) + amount
        return float(bucket[field])

    async def aclose(self) -> None:
        return None


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(backfill, "backfill_redis", lambda: fake)
    return fake


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[Any], dict[str, Any]]]:
    """브로커 경계 — (이름, 인자, `countdown`). 등록된 태스크의 `apply_async`도 결국
    `app.send_task(name, args, …)`다."""
    calls: list[tuple[str, list[Any], dict[str, Any]]] = []

    def send_task(name: str, args: Any = None, kwargs: Any = None, **options: Any) -> None:
        calls.append((name, list(args or []), {"countdown": options.get("countdown")}))

    monkeypatch.setattr(celery_app, "send_task", send_task)
    return calls


def _progress(redis: FakeRedis, scope: str) -> dict[str, Any]:
    raw = redis.data[backfill.PROGRESS_KEY.format(scope=scope)]
    return backfill.SourceDigestBackfillProgress.model_validate_json(raw).model_dump()


# ── 브로커 계약 ───────────────────────────────────────────────────────────


def test_backfill_task_name_is_added_routed_to_ingestion_and_registered() -> None:
    name = task_names.BACKFILL_SOURCE_DIGESTS

    assert name == "app.workers.tasks.source_digest.backfill_source_digests"
    assert digest_task.backfill_source_digests.name == name
    assert celery_app.amqp.router.route({}, name)["queue"].name == "ingestion"
    assert "biznotes_core.workers.tasks.source_digest" in celery_app.conf.imports
    # 기존 이름은 그대로다(추가만 한다)
    assert task_names.GENERATE_SOURCE_DIGEST == (
        "app.workers.tasks.source_digest.generate_source_digest"
    )
    assert digest_task.generate_source_digest.name == task_names.GENERATE_SOURCE_DIGEST


def test_backfill_settings_default_to_unlimited() -> None:
    from biznotes_core.config import CoreSettings

    fields = CoreSettings.model_fields
    assert fields["source_digest_backfill_daily_cost_usd"].default == 0
    assert fields["source_digest_backfill_daily_max_sources"].default == 0
    assert fields["source_digest_backfill_concurrency"].default == 2


# ── 예약(중복 방지) ───────────────────────────────────────────────────────


async def test_schedule_publishes_once_per_scope_until_released(redis, sent) -> None:
    nb = uuid.uuid4()

    assert await backfill.schedule_backfill(nb, countdown=60) is True
    assert await backfill.schedule_backfill(nb, countdown=60) is False  # 대기 중 — 다시 내지 않는다
    assert await backfill.schedule_backfill(None) is True  # 범위가 다르다

    assert sent == [
        (task_names.BACKFILL_SOURCE_DIGESTS, [str(nb)], {"countdown": 60}),
        (task_names.BACKFILL_SOURCE_DIGESTS, [None], {"countdown": 0}),
    ]
    assert redis.ttls[backfill.PENDING_KEY.format(scope=str(nb))] == backfill.PENDING_TTL_SECONDS
    assert _progress(redis, str(nb))["status"] == "queued"
    assert _progress(redis, "all")["notebook_id"] is None

    await backfill.release_pending(redis, str(nb))
    assert await backfill.schedule_backfill(nb) is True
    assert len(sent) == 3


async def test_publish_failure_releases_the_pending_mark(redis, monkeypatch) -> None:
    def broken(*_a: Any, **_k: Any) -> None:
        raise ConnectionError("broker down")

    monkeypatch.setattr(celery_app, "send_task", broken)
    nb = uuid.uuid4()

    with pytest.raises(ConnectionError):
        await backfill.schedule_backfill(nb)

    assert backfill.PENDING_KEY.format(scope=str(nb)) not in redis.data


# ── 백필 태스크 ───────────────────────────────────────────────────────────


class _Session:
    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


def _target(title: str, notebook_id: uuid.UUID, owner_id: uuid.UUID, k: int) -> Any:
    base = datetime(2024, 1, 1, tzinfo=UTC)
    return digest_task.BackfillTarget(
        id=uuid.uuid4(), title=title, notebook_id=notebook_id, owner_id=owner_id,
        created_at=base + timedelta(seconds=k),
    )


class Library:
    """대상 소스 목록·지문·캐시·청크·저장 대역 — 저장한 요약은 다음 실행에서 캐시로 읽힌다."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, targets: list[Any]) -> None:
        self.targets = targets
        self.chunks: dict[uuid.UUID, list[str]] = {t.id: [f"{t.title} 본문"] for t in targets}
        self.digests: dict[uuid.UUID, str] = {}
        self.stored: list[uuid.UUID] = []
        self.pages: list[int] = []

        async def count(_db: Any, notebook_id: Any) -> int:
            return len(self._scope(notebook_id))

        async def page(_db: Any, notebook_id: Any, after: Any) -> list[Any]:
            rows = self._scope(notebook_id)
            if after is not None:
                rows = [t for t in rows if (t.created_at, t.id) > after]
            rows = rows[: digest_task.BACKFILL_PAGE_SIZE]
            self.pages.append(len(rows))
            return rows

        async def hashes(_db: Any, ids: Any) -> dict[uuid.UUID, str]:
            return {i: sd.content_hash_of(self.chunks[i]) for i in ids if self.chunks.get(i)}

        async def cached(_db: Any, hashes: Any) -> dict[uuid.UUID, Any]:
            return {
                i: SimpleNamespace(content_hash=h)
                for i, h in hashes.items()
                if self.digests.get(i) == h
            }

        async def texts(_db: Any, sid: uuid.UUID) -> list[str]:
            return list(self.chunks.get(sid, []))

        async def store(sid: uuid.UUID, h: str, result: Any) -> bool:
            self.digests[sid] = h
            self.stored.append(sid)
            return True

        monkeypatch.setattr(digest_task, "_count_targets", count)
        monkeypatch.setattr(digest_task, "_target_page", page)
        monkeypatch.setattr(digest_task, "get_session_factory", lambda: _Session)
        monkeypatch.setattr(sd, "content_hashes", hashes)
        monkeypatch.setattr(sd, "load_cached", cached)
        monkeypatch.setattr(sd, "chunk_texts", texts)
        monkeypatch.setattr(sd, "store_digest", store)

    def _scope(self, notebook_id: Any) -> list[Any]:
        rows = [t for t in self.targets if notebook_id is None or t.notebook_id == notebook_id]
        return sorted(rows, key=lambda t: (t.created_at, t.id))


def _settings(monkeypatch: pytest.MonkeyPatch, **update: Any) -> None:
    settings = get_settings().model_copy(update=update)
    monkeypatch.setattr(digest_task, "get_settings", lambda: settings)


def _library(
    monkeypatch: pytest.MonkeyPatch, n: int, notebook_id: uuid.UUID | None = None
) -> tuple[Library, uuid.UUID, uuid.UUID]:
    nb, owner = notebook_id or uuid.uuid4(), uuid.uuid4()
    return Library(monkeypatch, [_target(f"자료 {i}", nb, owner, i) for i in range(n)]), nb, owner


async def test_backfill_makes_missing_digests_for_the_scope(
    monkeypatch, redis
):
    lib, nb, _ = _library(monkeypatch, 4)
    lib.digests[lib.targets[1].id] = sd.content_hash_of(lib.chunks[lib.targets[1].id])  # 최신
    del lib.chunks[lib.targets[3].id]  # 청크 없음
    def reply(system: str, user: str) -> str:
        return "백필 요약"

    fake = FakeLLM(reply)
    monkeypatch.setattr(sd, "complete_stream_with_fallback", fake)
    _settings(monkeypatch)
    redis.data[backfill.PENDING_KEY.format(scope=str(nb))] = "1"

    out = await digest_task._backfill(str(nb))

    assert lib.stored == [lib.targets[0].id, lib.targets[2].id]
    assert out == {
        "ok": True, "scope": str(nb), "status": "done", "total": 4, "generated": 2,
        "skipped": 2, "failed": 0, "stop_reason": None,
    }
    progress = _progress(redis, str(nb))
    assert (progress["status"], progress["generated"], progress["skipped"]) == ("done", 2, 2)
    assert progress["started_at"] is not None and progress["finished_at"] is not None
    assert fake.model_ids == [None, None]  # 기본 모델 + 폴백 체인(미리 만들기와 같다)
    # 끝나면 대기 표식을 지운다 — 다음 예약이 다시 발행할 수 있다
    assert backfill.PENDING_KEY.format(scope=str(nb)) not in redis.data


async def test_interrupted_backfill_resumes_where_it_stopped(monkeypatch, redis):
    """수락 기준 1 — 중단 뒤 다시 실행하면 이미 최신 요약이 있는 소스를 건너뛰고 이어서 만든다."""
    lib, nb, _ = _library(monkeypatch, 5)
    monkeypatch.setattr(digest_task, "BACKFILL_PAGE_SIZE", 2)  # 여러 쪽에 걸쳐 진행한다
    crash_on = lib.targets[3].title

    def reply(system: str, user: str) -> Any:
        if f"문서 제목: {crash_on}\n" in user:
            raise RuntimeError("워커 종료")
        return "백필 요약"

    monkeypatch.setattr(sd, "complete_stream_with_fallback", FakeLLM(reply))
    _settings(monkeypatch, source_digest_backfill_concurrency=1)

    with pytest.raises(RuntimeError):
        await digest_task._backfill(str(nb))

    assert lib.stored == [t.id for t in lib.targets[:3]]
    progress = _progress(redis, str(nb))
    assert (progress["status"], progress["generated"]) == ("failed", 3)
    assert backfill.PENDING_KEY.format(scope=str(nb)) not in redis.data

    crash_on = "없는 제목"
    fake = FakeLLM(reply)
    monkeypatch.setattr(sd, "complete_stream_with_fallback", fake)

    out = await digest_task._backfill(str(nb))

    assert lib.stored[3:] == [lib.targets[3].id, lib.targets[4].id]
    assert len(fake.calls) == 2  # 이미 만든 3개는 다시 부르지 않는다
    assert (out["status"], out["generated"], out["skipped"]) == ("done", 2, 3)


async def test_daily_cost_cap_stops_the_backfill_and_records_it(monkeypatch, redis):
    """수락 기준 2 — 하루 비용 상한에 닿으면 멈추고 진행률에 남긴다. 다시 실행해도 그날은 멈춘다."""
    lib, nb, _ = _library(monkeypatch, 5)
    fake = FakeLLM()  # 호출마다 0.02달러(입력 10·출력 5토큰, 1k당 1·2달러)
    monkeypatch.setattr(sd, "complete_stream_with_fallback", fake)
    _settings(
        monkeypatch,
        source_digest_backfill_concurrency=1,
        source_digest_backfill_daily_cost_usd=0.03,
    )

    out = await digest_task._backfill(str(nb))

    assert len(fake.calls) == 2 and len(lib.stored) == 2
    assert (out["status"], out["stop_reason"], out["generated"]) == ("stopped", "daily_cost_cap", 2)
    progress = _progress(redis, str(nb))
    assert (progress["status"], progress["stop_reason"], progress["total"]) == (
        "stopped", "daily_cost_cap", 5,
    )
    spend_key = backfill._spend_key()
    assert float(redis.data[spend_key]["cost_usd"]) == pytest.approx(0.04)
    assert redis.ttls[spend_key] == backfill.SPEND_TTL_SECONDS

    again = await digest_task._backfill(str(nb))

    assert len(fake.calls) == 2  # 같은 날에는 더 부르지 않는다
    assert (again["stop_reason"], again["skipped"]) == ("daily_cost_cap", 2)


async def test_daily_source_cap_is_shared_across_backfills_and_never_exceeded(monkeypatch, redis):
    nb, nb2, owner = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    targets = [_target(f"자료 {i}", nb, owner, i) for i in range(3)]
    targets += [_target(f"다른 자료 {i}", nb2, owner, 10 + i) for i in range(3)]
    Library(monkeypatch, targets)
    fake = FakeLLM()
    monkeypatch.setattr(sd, "complete_stream_with_fallback", fake)
    _settings(
        monkeypatch,
        source_digest_backfill_concurrency=3,
        source_digest_backfill_daily_max_sources=4,
    )

    first = await digest_task._backfill(str(nb))
    second = await digest_task._backfill(str(nb2))

    assert (first["generated"], first["status"]) == (3, "done")
    assert (second["generated"], second["stop_reason"]) == (1, "daily_source_cap")
    assert len(fake.calls) == 4
    assert int(redis.data[backfill._spend_key()]["sources"]) == 4


async def test_failed_sources_are_counted_and_retried_next_time(monkeypatch, redis):
    lib, nb, _ = _library(monkeypatch, 3)
    broken = lib.targets[1].title

    def reply(system: str, user: str) -> Any:
        if f"문서 제목: {broken}\n" in user:
            return GatewayCallError("LLM 호출 실패: 공급자 원문")
        return "백필 요약"

    monkeypatch.setattr(sd, "complete_stream_with_fallback", FakeLLM(reply))
    _settings(monkeypatch)

    out = await digest_task._backfill(str(nb))

    assert (out["status"], out["generated"], out["failed"]) == ("done", 2, 1)
    assert "공급자 원문" not in str(_progress(redis, str(nb)))

    broken = "없는 제목"
    again = await digest_task._backfill(str(nb))

    assert (again["generated"], again["skipped"], again["failed"]) == (1, 2, 0)


async def test_all_scope_covers_every_notebook_with_capped_concurrency(monkeypatch, redis):
    nb_a, nb_b = uuid.uuid4(), uuid.uuid4()
    owner = uuid.uuid4()
    targets = [_target(f"A{i}", nb_a, owner, i) for i in range(4)]
    targets += [_target(f"B{i}", nb_b, owner, 10 + i) for i in range(4)]
    lib = Library(monkeypatch, targets)
    monkeypatch.setattr(digest_task, "BACKFILL_PAGE_SIZE", 3)
    fake = FakeLLM()
    monkeypatch.setattr(sd, "complete_stream_with_fallback", fake)
    _settings(monkeypatch, source_digest_backfill_concurrency=2)

    out = await digest_task._backfill(None)

    assert sorted(lib.stored) == sorted(t.id for t in targets)
    assert (out["scope"], out["total"], out["generated"]) == ("all", 8, 8)
    assert fake.peak <= 2
    assert lib.pages == [3, 3, 2, 0]


# ── run 생성 상한 초과 → 노트북 백필 예약 ───────────────────────────────


def _cap(monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    monkeypatch.setattr(
        digest_node,
        "get_settings",
        lambda: get_settings().model_copy(update={"source_digest_run_max_generate": limit}),
    )


async def test_deferred_sources_schedule_one_backfill_for_the_notebook(monkeypatch, redis, sent):
    """수락 기준 3 — `generation_deferred`가 생긴 run 뒤 그 노트북 백필이 예약된다(중복 없음)."""
    sources = [_src(f"자료 {i}") for i in range(4)]
    Store(monkeypatch, sources)
    monkeypatch.setattr(sd, "complete_stream_with_fallback", FakeLLM())
    _cap(monkeypatch, 1)
    ctx = _ctx(_Db(sources))

    first = await _exec_source_digest(ctx, {"id": "digest"}, {})
    second = await _exec_source_digest(ctx, {"id": "digest"}, {})

    assert [d["reason"] for d in first["degraded"]] == ["generation_deferred"] * 3
    assert len(second["degraded"]) == 3
    assert sent == [
        (
            task_names.BACKFILL_SOURCE_DIGESTS,
            [str(ctx.notebook_id)],
            {"countdown": backfill.RUN_SCHEDULE_COUNTDOWN_SECONDS},
        )
    ]


async def test_run_without_deferred_sources_schedules_nothing(monkeypatch, redis, sent):
    sources = [_src(f"자료 {i}") for i in range(3)]
    Store(monkeypatch, sources)
    monkeypatch.setattr(sd, "complete_stream_with_fallback", FakeLLM())

    out = await _exec_source_digest(_ctx(_Db(sources)), {"id": "digest"}, {})

    assert out["degraded"] == [] and sent == []


async def test_schedule_failure_does_not_fail_the_run(monkeypatch):
    sources = [_src(f"자료 {i}") for i in range(3)]
    Store(monkeypatch, sources)
    monkeypatch.setattr(sd, "complete_stream_with_fallback", FakeLLM())
    _cap(monkeypatch, 1)

    async def broken(*_a: Any, **_k: Any) -> bool:
        raise ConnectionError("redis down")

    monkeypatch.setattr(backfill, "schedule_backfill", broken)

    out = await _exec_source_digest(_ctx(_Db(sources)), {"id": "digest"}, {})

    assert len(out["sources"]) == 3
    assert [d["reason"] for d in out["degraded"]] == ["generation_deferred"] * 2


# ── 관리 API ─────────────────────────────────────────────────────────────


class _AdminDb:
    def __init__(self, notebooks: set[uuid.UUID]) -> None:
        self.notebooks = notebooks
        self.added: list[Any] = []
        self.commits = 0

    async def get(self, model: type, key: uuid.UUID, **_kw: Any) -> Any:
        return SimpleNamespace(id=key) if model is Notebook and key in self.notebooks else None

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1


def _user(role: str) -> User:
    return User(id=uuid.uuid4(), email=f"{role}@example.com", role=role, is_active=True)


def _client(user: User, db: _AdminDb) -> httpx.AsyncClient:
    admin = APIRouter(prefix="/admin")
    admin.include_router(admin_digest.router)
    app = FastAPI()
    app.include_router(admin)
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_admin_starts_a_notebook_backfill_and_reads_progress(redis, sent):
    nb = uuid.uuid4()
    admin, db = _user("admin"), _AdminDb({nb})

    async with _client(admin, db) as client:
        started = await client.post(
            "/admin/source-digests/backfill", json={"notebook_id": str(nb)}
        )
        again = await client.post(
            "/admin/source-digests/backfill", json={"notebook_id": str(nb)}
        )
        status = await client.get(
            "/admin/source-digests/backfill", params={"notebook_id": str(nb)}
        )
        empty = await client.get("/admin/source-digests/backfill")

    assert started.status_code == 202
    body = started.json()
    assert (body["scope"], body["scheduled"], body["progress"]["status"]) == (
        str(nb), True, "queued",
    )
    assert again.json()["scheduled"] is False  # 대기 중 — 다시 발행하지 않는다
    assert sent == [(task_names.BACKFILL_SOURCE_DIGESTS, [str(nb)], {"countdown": 0})]
    assert status.status_code == 200 and status.json()["progress"]["scope"] == str(nb)
    assert empty.json() == {"scope": "all", "progress": None}
    audits = [a for a in db.added if isinstance(a, AuditLog)]
    assert [(a.action, a.user_id, a.detail["scheduled"]) for a in audits] == [
        ("source_digest.backfill", admin.id, True),
        ("source_digest.backfill", admin.id, False),
    ]
    assert db.commits == 2


async def test_admin_backfill_of_all_notebooks(redis, sent):
    async with _client(_user("admin"), _AdminDb(set())) as client:
        res = await client.post("/admin/source-digests/backfill", json={})

    assert res.status_code == 202 and res.json()["scope"] == "all"
    assert sent == [(task_names.BACKFILL_SOURCE_DIGESTS, [None], {"countdown": 0})]


async def test_admin_backfill_of_a_missing_notebook_is_404(redis, sent):
    async with _client(_user("admin"), _AdminDb(set())) as client:
        res = await client.post(
            "/admin/source-digests/backfill", json={"notebook_id": str(uuid.uuid4())}
        )

    assert res.status_code == 404 and sent == []


async def test_non_admin_cannot_start_or_read_a_backfill(redis, sent):
    async with _client(_user("user"), _AdminDb(set())) as client:
        post = await client.post("/admin/source-digests/backfill", json={})
        get = await client.get("/admin/source-digests/backfill")

    assert (post.status_code, get.status_code) == (403, 403) and sent == []


async def test_admin_backfill_reports_unavailable_redis_as_503(monkeypatch, sent):
    class Down(FakeRedis):
        async def set(self, *_a: Any, **_k: Any) -> bool:
            raise ConnectionError("redis down")

    monkeypatch.setattr(backfill, "backfill_redis", Down)

    async with _client(_user("admin"), _AdminDb(set())) as client:
        res = await client.post("/admin/source-digests/backfill", json={})

    assert res.status_code == 503 and sent == []
