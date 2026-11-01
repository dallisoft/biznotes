"""코어 테스트 공용 픽스처 — 단일 스위트.

대부분의 테스트는 외부 경계만 대신한다 — DB·스토리지·벡터 검색은 기록형 가짜 객체다. 검증 대상
함수 본문은 치환하지 않는다.

**PG 테스트**: 파일 이름이 `_pg.py`로 끝나거나 `pytest.mark.pg`가 붙은 테스트는 실제
PostgreSQL(pgvector)과 Valkey를 쓴다. 실행마다 `core_test_<pid>_<난수>` DB를 만들어
`CREATE EXTENSION vector` + `create_all`(Alembic 검증이 아니다)을 하고 끝나면 지운다. 테스트마다
전 테이블을 DELETE한다 — 매 테스트가 빈 DB에서 시작한다. 서버에 닿지 않으면 코드 1로 끝난다
("0개 통과"가 초록으로 보이지 않게).

접속은 환경 변수로 바꾼다(기본값은 개발 compose — `make up`의 postgres·valkey):

    CORE_TEST_PG_HOST(127.0.0.1) CORE_TEST_PG_PORT(25433)
    CORE_TEST_PG_USER(biznotes) CORE_TEST_PG_PASSWORD(biznotes)  # CREATE DATABASE 권한
    CORE_TEST_VALKEY_URL(redis://localhost:26379/9)  # DB 번호를 그대로 쓰고 FLUSHDB하지 않는다
"""

import os
import re
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

import pytest
from sqlalchemy.dialects import postgresql

PG_HOST = os.environ.get("CORE_TEST_PG_HOST", "127.0.0.1")
PG_PORT = os.environ.get("CORE_TEST_PG_PORT", "25433")
PG_USER = os.environ.get("CORE_TEST_PG_USER", "biznotes")
PG_PASSWORD = os.environ.get("CORE_TEST_PG_PASSWORD", "biznotes")
VALKEY_URL = os.environ.get("CORE_TEST_VALKEY_URL", "redis://localhost:26379/9")
# PID만 쓰면 PID 번호가 겹치는 환경(컨테이너 등)에서 같은 PG를 쓰는 두 실행이 충돌한다
DB_NAME = f"core_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"

# 코어 모듈 다수가 import 시점에 설정을 읽으므로 import 전에 **대입**한다(setdefault가 아니다 —
# 셸의 값이 섞이지 않게).
os.environ.update(
    {
        "POSTGRES_HOST": PG_HOST,
        "POSTGRES_PORT": PG_PORT,
        "POSTGRES_USER": PG_USER,
        "POSTGRES_PASSWORD": PG_PASSWORD,
        "POSTGRES_DB": DB_NAME,
        "VALKEY_URL": VALKEY_URL,
        "JWT_SECRET": "test-secret",
        "APP_ENV": "test",  # 레이트 리밋 비활성화
    }
)
# 로컬 스토리지 백엔드의 기본 경로(상대 `data`)가 저장소 안에 파일을 만들지 않게 한다 —
# 실제 파일을 보는 테스트는 `local_storage` 픽스처로 자기 경로를 쓴다
os.environ.setdefault("BIZNOTES_DATA_DIR", tempfile.mkdtemp(prefix="core-test-data-"))

from pydantic_settings import SettingsConfigDict  # noqa: E402

from biznotes_core.config import CoreSettings, reset_settings  # noqa: E402


class _SuiteSettings(CoreSettings):
    """개발자 `.env`를 읽지 않는 설정 — 환경 변수만 쓴다."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")


reset_settings(_SuiteSettings)

LINUX = sys.platform.startswith("linux")


class AnySpecHash(str):
    """승인 대역용 고정 해시 — 어떤 현재 스펙 해시와도 같다고 답한다(#293).

    승인 시점 스펙 고정이 관심사가 아닌 디스커버리 테스트(캐시·annotations·노출 신호 등)가 이름 승인
    대역만으로 도구를 노출하게 한다. 고정 판정 자체는 `test_mcp_tool_spec_pinning.py`가 실제 해시로
    본다. `str` 하위 클래스라 `계산값 != 이 값`에서 이 값의 `__ne__`가 먼저 불린다.
    """

    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False

    __hash__ = str.__hash__


def stub_mcp_approvals(monkeypatch: pytest.MonkeyPatch, fake_approved_tools: Any) -> None:
    """이름 승인 대역(`approved_tools`)을 디스커버리의 고정 승인(`approved_tool_pins`)에도 쓴다."""
    from biznotes_core.services.agents import mcp_exposure

    async def pins(db: Any, server_ids: Any) -> dict[Any, dict[str, str]]:
        names = await fake_approved_tools(db, server_ids)
        return {sid: {n: AnySpecHash("any") for n in ns} for sid, ns in names.items()}

    monkeypatch.setattr(mcp_exposure, "approved_tools", fake_approved_tools)
    monkeypatch.setattr(mcp_exposure, "approved_tool_pins", pins)


@pytest.fixture(autouse=True)
def _mcp_tool_list_cache_isolation() -> Any:
    """`list_tools` 프로세스 캐시(#147)가 테스트 사이로 새지 않게 비운다."""
    from biznotes_core.services.agents import tool_registry

    tool_registry._clear_mcp_tool_list_cache()
    yield
    tool_registry._clear_mcp_tool_list_cache()


@pytest.fixture
def non_dumpable() -> Any:
    """이 테스트 동안만 테스트 프로세스를 non-dumpable로 둔다(Linux 전용 케이스에서 쓴다)."""
    from biznotes_core.core import dumpable

    assert dumpable.set_non_dumpable() is True
    yield
    dumpable._libc().prctl(4, 1, 0, 0, 0)  # PR_SET_DUMPABLE 1 — 다음 테스트에 새지 않게


def pg(stmt: Any) -> str:
    """PostgreSQL 방언으로 컴파일한 SQL 문자열."""
    return str(stmt.compile(dialect=postgresql.dialect()))


def pg_params(stmt: Any) -> dict[str, Any]:
    return dict(stmt.compile(dialect=postgresql.dialect()).params)


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------


def make_settings(**overrides: Any) -> CoreSettings:
    """검증 없이 기본값 위에 덮어쓴 설정."""
    base: dict[str, Any] = {}
    base.update(overrides)
    return CoreSettings.model_construct(**base)


# ---------------------------------------------------------------------------
# 워커 _run 하네스
# ---------------------------------------------------------------------------


class MissingObject(Exception):
    """MinIO `S3Error(NoSuchKey)` 대역 — 코어는 `code`만 본다."""

    code = "NoSuchKey"


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None):
        self._rows = rows or []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def all(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def first(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def scalars(self) -> Any:
        rows = self._rows

        class _S:
            def all(self) -> list[Any]:
                return [r[0] for r in rows]

        return _S()


class MainSession:
    """`_run`·`_run_index`의 `db` — 쓰기 호출을 타임라인에 기록한다. 단계마다 새로 만든다."""

    def __init__(self, harness: "RunHarness"):
        self.h = harness

    async def __aenter__(self) -> "MainSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.h.timeline.append(("db.close", None))

    @property
    def no_autoflush(self) -> Any:
        import contextlib

        return contextlib.nullcontext()

    async def get(self, model: Any, key: Any) -> Any:
        from biznotes_core.models import Source

        return self.h.source if model is Source else None

    async def commit(self) -> None:
        if self.h.commit_error is not None:
            err = self.h.commit_error(self.h.source.status)
            if err is not None:
                self.h.timeline.append(("db.commit_failed", self.h.source.status))
                raise err
        self.h.timeline.append(("db.commit", self.h.source.status))
        self.h.committed.append(
            {
                "status": self.h.source.status,
                "error_message": self.h.source.error_message,
                "parsed_object_key": self.h.source.parsed_object_key,
            }
        )

    async def flush(self) -> None:
        self.h.timeline.append(("db.flush", None))
        if self.h.flush_error is not None:
            raise self.h.flush_error

    async def rollback(self) -> None:
        self.h.timeline.append(("db.rollback", None))
        if self.h.expire_on_rollback:
            # 트랜잭션이 열린 채 롤백하면 실제 AsyncSession은 기본 키까지 만료시킨다 — refresh
            # 전에 읽으면 암묵 로드가 MissingGreenlet으로 실패한다(#50 리뷰)
            keys = ("id", *_SOURCE_STATE_KEYS)
            self.h.rolled_back_state = {k: getattr(self.h.source, k) for k in keys}
            for key in keys:
                setattr(self.h.source, key, _EXPIRED)

    async def refresh(self, obj: Any) -> None:
        self.h.timeline.append(("db.refresh", None))
        # savepoint를 되돌린 뒤의 refresh는 DB 값(= savepoint 진입 시점 값)을 다시 읽는다
        if self.h.rolled_back_state is not None:
            for key, value in self.h.rolled_back_state.items():
                setattr(self.h.source, key, value)
            self.h.rolled_back_state = None

    async def execute(self, stmt: Any, params: Any = None) -> FakeResult:
        sql = pg(stmt)
        self.h.timeline.append(("db.execute", sql))
        if self.h.execute_error is not None:
            err = self.h.execute_error(sql)
            if err is not None:
                raise err
        if "FOR KEY SHARE" in sql:
            # 색인 교체 잠금 뒤 소스 행 확인(#27) — 행이 있다고 답한다
            return FakeResult([(self.h.source.id,)] if self.h.source_exists else [])
        if sql.startswith("UPDATE sources SET status") and "RETURNING" in sql:
            return self._conditional_update(sql, pg_params(stmt))
        return FakeResult()

    def _conditional_update(self, sql: str, params: dict[str, Any]) -> FakeResult:
        """조건부 상태 전이(#244) — 출발 상태·미룸 토큰·열린 재인덱싱 항목을 대역 행으로 평가한다.

        색인 단계 점유(`status → embedding`)는 `claim_result`로 결과를 강제할 수 있다. 맞으면
        행의 `updated_at`을 한 칸 올리고 RETURNING 값(`updated_at` 또는 `id`)을 돌려준다.
        """
        h = self.h
        frm = params["status_1"]
        ok = h.source.status in (frm if isinstance(frm, list) else [frm])
        if "updated_at_1" in params:
            ok = ok and params["updated_at_1"] == h.row_updated_at
        if "reindex_run_items" in sql:
            ok = ok and h.open_reindex[0]
        if "sources.error_message IS NOT NULL" in sql:
            ok = ok and h.source.error_message is not None
        if params["status"] == "embedding" and h.claim_result is not None:
            ok = h.claim_result
        h.timeline.append(("db.update", (params["status"], ok)))
        if not ok:
            return FakeResult([])
        h.row_updated_at = h.row_updated_at + timedelta(seconds=1)
        returned = h.row_updated_at if "RETURNING sources.updated_at" in sql else h.source.id
        return FakeResult([(returned,)])

    async def scalar(self, stmt: Any, params: Any = None) -> Any:
        sql = pg(stmt)
        self.h.timeline.append(("db.scalar", sql))
        if self.h.execute_error is not None:
            err = self.h.execute_error(sql)
            if err is not None:
                raise err
        if "pg_try_advisory_xact_lock" in sql:
            return self.h.try_lock_result
        if "FROM source_chunks" in sql:
            # 잠금 밖 실패의 교체 표식(#50) — 커밋된 청크 행 하나의 id
            return self.h.chunk_ids[0] if self.h.chunk_ids else None
        return None

    def add(self, obj: Any) -> None:
        from biznotes_core.models import SourceChunk

        if isinstance(obj, SourceChunk):
            # 색인 교체가 savepoint 안에서 넣는 청크 행(벡터 포함) — 교체 한 번이 한 묶음이다
            if not self.h.batch_open:
                self.h.timeline.append(("db.add_chunks", None))
                self.h.indexed.append([])
                self.h.batch_open = True
            self.h.indexed[-1].append(obj)

    def begin_nested(self) -> "_FakeSavepoint":
        return _FakeSavepoint(self.h)


class _FakeSavepoint:
    """`begin_nested()` 대역 — savepoint 진입·해제·되돌림을 타임라인에 남긴다."""

    def __init__(self, harness: "RunHarness"):
        self.h = harness

    async def __aenter__(self) -> "_FakeSavepoint":
        self.h.timeline.append(("db.savepoint", None))
        self.h.batch_open = False
        self.state = {key: getattr(self.h.source, key) for key in _SOURCE_STATE_KEYS}
        return self

    async def __aexit__(self, exc_type: Any, *exc: Any) -> None:
        self.h.timeline.append(("db.rollback_to_savepoint" if exc_type else "db.release", None))
        if exc_type:
            # 실제 AsyncSession은 되돌림으로 만료된 속성을 읽으면 MissingGreenlet을 낸다 —
            # refresh 전에 읽는 회귀를 잡도록 만료 센티널을 넣어 둔다
            self.h.rolled_back_state = self.state
            for key in _SOURCE_STATE_KEYS:
                setattr(self.h.source, key, _EXPIRED)


class _Expired:
    """refresh 전에 읽힌 만료 속성 — 값으로 쓰이면 바로 실패한다."""

    def _fail(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("savepoint 되돌림 뒤 refresh 없이 만료된 속성을 읽었다")

    __bool__ = __eq__ = __ne__ = __str__ = __format__ = __int__ = __len__ = __iter__ = _fail
    __hash__ = None  # type: ignore[assignment]

    # 로그 인자로만 닿는 읽기도 잡는다 — 실제 AsyncSession은 그 읽기에서 MissingGreenlet을 낸다
    __repr__ = _fail  # type: ignore[assignment]


_EXPIRED = _Expired()


# savepoint 되돌림이 복원하는 sources 컬럼(대역 범위)
_SOURCE_STATE_KEYS = (
    "status",
    "error_message",
    "token_count",
    "chunk_count",
    "embedding_model",
    "chunk_config",
)


class HeartbeatSession:
    def __init__(self, harness: "RunHarness"):
        self.h = harness

    async def __aenter__(self) -> "HeartbeatSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, stmt: Any) -> FakeResult:
        self.h.hb_statements.append(pg(stmt))
        self.h.tick_times.append(time.monotonic())
        self.h.timeline.append(("hb.tick", len(self.h.hb_statements)))
        return FakeResult(self.h.hb_response(len(self.h.hb_statements)))

    async def commit(self) -> None:
        return None


class RunHarness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        from biznotes_core.models import Source
        from biznotes_core.services import settings_store
        from biznotes_core.workers.tasks import ingestion, overview

        self.mp = monkeypatch
        self.timeline: list[tuple[str, Any]] = []
        self.committed: list[dict[str, Any]] = []
        self.hb_statements: list[str] = []
        self.tick_times: list[float] = []
        self.put_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.loop_thread = threading.get_ident()
        # parsed 버킷 대역 — 올린 오브젝트는 키로 돌려준다. 없는 키는 원본(`raw`)으로 답한다
        # (기존 테스트가 `raw`를 저장된 parsed.md로 쓴다). 위치 파일만 "없음"으로 답한다
        self.objects: dict[str, bytes] = {}
        # 색인 단계 핸드오프(#244) — `run()`이 `_run`의 결과에서 꺼내 `_run_index`에 넘긴 kwargs
        self.handoffs: list[dict[str, Any]] = []
        # 색인 단계 점유 결과를 강제한다 — None이면 출발 상태·토큰·열린 항목과 대조한다
        self.claim_result: bool | None = None
        # 대역 행의 `updated_at` — 조건부 전이마다 한 칸 오른다(미룸 토큰 대조용)
        self.row_updated_at = datetime(2026, 10, 16, tzinfo=UTC)
        self.now = datetime(2026, 9, 17, tzinfo=UTC)
        # 기본: 모든 틱이 1행에 매칭
        self.hb_response: Callable[[int], list[tuple[Any, ...]]] = lambda n: [(self.now,)]
        self.get_object_delay = 0.0
        self.raw = b"raw"
        self.open_reindex: tuple[bool, bool] = (False, False)
        # 색인 교체(#27): 소스 행 존재 여부, execute에 오류를 넣는 훅(SQL → 예외 | None),
        # savepoint마다 넣은 청크 행(벡터 포함) — 되돌린 교체의 묶음도 남는다
        self.source_exists = True
        self.execute_error: Callable[[str], BaseException | None] | None = None
        self.commit_error: Callable[[str], BaseException | None] | None = None
        self.flush_error: BaseException | None = None
        self.rolled_back_state: dict[str, Any] | None = None
        self.indexed: list[list[Any]] = []
        self.batch_open = False
        self.closed_reindex: list[tuple[str, bool, str | None]] = []
        # 잠금 밖 실패(#50): 커밋된 청크 행 id(표식으로 첫 값을 돌려준다), 비차단 잠금 결과
        self.chunk_ids: list[uuid.UUID] = []
        self.try_lock_result = True
        self.expire_on_rollback = False
        self.source = Source(
            id=uuid.uuid4(),
            notebook_id=uuid.uuid4(),
            type="file",
            title="doc.test",
            mime_type="application/x-test",
            original_object_key="k/raw.test",
            status="pending",
            error_message=None,
            token_count=0,
            chunk_count=0,
        )
        self.settings = make_settings(
            parsing_heartbeat_sec=0.02, parsing_heartbeat_tick_timeout_sec=1
        )

        harness = self
        # 단계(`_run`·`_run_index`)마다 첫 세션이 그 단계의 db다 — 나머지는 heartbeat 세션
        self._sessions = iter([MainSession(self)])

        def factory() -> Any:
            return next(harness._sessions, None) or HeartbeatSession(harness)

        def get_object(bucket: str, key: str) -> bytes:
            call = {"thread": threading.get_ident(), "key": key, "start": time.monotonic()}
            time.sleep(self.get_object_delay)
            call["end"] = time.monotonic()
            self.get_calls.append(call)
            return self.objects.get(key, self.raw)

        def put_object(bucket: str, key: str, data: bytes, content_type: str = "") -> None:
            self.put_calls.append({"thread": threading.get_ident(), "key": key, "data": data})
            self.objects[key] = data

        def stat_object_size(bucket: str, key: str) -> int:
            if key in self.objects:
                return len(self.objects[key])
            if key.endswith("/locations.json"):
                raise MissingObject()
            return len(self.raw)

        async def get_setting(db: Any, name: str) -> dict[str, Any]:
            return {}

        mp = monkeypatch
        mp.setattr(ingestion, "get_session_factory", lambda: factory)
        mp.setattr(ingestion, "get_settings", lambda: self.settings)
        mp.setattr(ingestion, "publish_source_event", lambda *a, **k: None)
        mp.setattr(ingestion, "get_object", get_object)
        mp.setattr(ingestion, "put_object", put_object)
        mp.setattr(ingestion, "stat_object_size", stat_object_size)
        mp.setattr(ingestion, "embed_dense", lambda texts: [[0.0] for _ in texts])
        mp.setattr(ingestion, "embed_sparse", lambda texts: [{} for _ in texts])
        # 임베딩 공간·재인덱싱 기록은 이 하네스의 대상이 아니다 — 모델 레지스트리·
        # reindex_run_items는 다른 테이블이고, 여기서 보는 것은 sources 행의 쓰기 순서다.
        mp.setattr(ingestion, "active_space", self._active_space)
        mp.setattr(ingestion, "resolve_space", self._resolve_space)
        mp.setattr(ingestion, "reindex_mode", self._reindex_mode)
        mp.setattr(ingestion, "_close_reindex", self._close_reindex)
        mp.setattr(settings_store, "get_setting", get_setting)
        mp.setattr(overview, "schedule_overview_refresh", lambda nid: None)
        self.ingestion = ingestion

    async def _active_space(self, db: Any) -> Any:
        from biznotes_core.services.embedding_spaces import LOCAL_SPACE

        return LOCAL_SPACE

    async def _resolve_space(self, db: Any, key: str | None) -> Any:
        from biznotes_core.services.embedding_spaces import LOCAL_SPACE

        return LOCAL_SPACE

    async def _reindex_mode(self, db: Any, source_id: Any) -> tuple[bool, bool]:
        """열린 재인덱싱 항목 — 기본은 없음. 재인덱싱 경로는 `run(reindex=True)`로 부른다."""
        return self.open_reindex

    async def _close_reindex(self, source_id: Any, ok: bool, error: str | None) -> None:
        self.closed_reindex.append((str(source_id), ok, error))

    def parser(self, fn: Callable[[bytes], Any], mime: str = "application/x-test") -> None:
        from biznotes_core.services.ingestion import parsers

        self.mp.setitem(parsers.FILE_PARSERS, mime, fn)
        self.source.mime_type = mime

    async def run(self, **kw: Any) -> dict[str, Any]:
        """두 단계를 차례로 — 진입(`_run`)이 넘긴 핸드오프로 색인 단계(`_run_index`)를 부른다.

        진입 단계가 넘기지 않았으면(실패·물러남) 그 결과를 돌려준다.
        """
        result = await self.run_entry(**kw)
        handoff = result.pop("handoff", None)
        if handoff is None:
            return result
        self.handoffs.append(dict(handoff))
        return await self.run_index(**handoff)

    async def run_entry(self, **kw: Any) -> dict[str, Any]:
        """진입·파싱 단계만 — 결과에 `handoff`가 남는다."""
        self._sessions = iter([MainSession(self)])
        return await self.ingestion._run(str(self.source.id), **kw)

    async def run_index(self, **kw: Any) -> dict[str, Any]:
        """색인 단계만 — kwargs는 `index_source`와 같다."""
        self._sessions = iter([MainSession(self)])
        return await self.ingestion._run_index(str(self.source.id), **kw)

    def db_writes_during_parsing(self) -> list[tuple[str, Any]]:
        """진입 틱부터 핸드오프 전이(`parsing` → `chunking`·`ready`) 직전까지 db가 낸 쓰기."""
        start = next(i for i, e in enumerate(self.timeline) if e[0] == "hb.tick")
        end = next(
            (
                i
                for i, e in enumerate(self.timeline)
                if i > start
                and e[0] == "db.execute"
                and str(e[1]).startswith("UPDATE sources SET status")
            ),
            len(self.timeline),
        )
        return [e for e in self.timeline[start:end] if e[0].startswith("db.")]


@pytest.fixture
def run_harness(monkeypatch: pytest.MonkeyPatch) -> RunHarness:
    return RunHarness(monkeypatch)


# ---------------------------------------------------------------------------
# 인메모리 sources 행 — 가이드 캐시·retry·write-site 테스트용
# ---------------------------------------------------------------------------


class UpdateResult(FakeResult):
    def __init__(self, rowcount: int):
        super().__init__([])
        self.rowcount = rowcount


class SourcesTable:
    """행 하나. 컴파일된 `update(Source)`의 WHERE(id·status·updated_at)를 평가해 SET을 적용한다.

    Core UPDATE는 컬럼 `onupdate=func.now()`를 따라 `updated_at`도 바꾼다 — 적용할 때마다
    논리 시각을 1씩 올려 흉내 낸다.
    """

    def __init__(self, **row: Any):
        self.base = datetime(2026, 9, 17, tzinfo=UTC)
        self.tick = 0
        self.row: dict[str, Any] = {
            "id": uuid.uuid4(),
            "notebook_id": uuid.uuid4(),
            "type": "file",
            "title": "doc.pdf",
            "origin_url": None,
            "mime_type": "application/pdf",
            "size_bytes": 10,
            "status": "ready",
            "error_message": None,
            "token_count": 1,
            "chunk_count": 1,
            "summary": None,
            "keywords": None,
            "document_title": None,
            "warnings": None,
            "created_at": self.base,
            "updated_at": self.base,
        }
        self.row.update(row)
        self.applied: list[str] = []

    def bump(self) -> Any:
        from datetime import timedelta

        self.tick += 1
        self.row["updated_at"] = self.base + timedelta(seconds=self.tick)
        return self.row["updated_at"]

    def write(self, **values: Any) -> None:
        """다른 경로(재인제스트 등)가 행을 쓴 것처럼 바꾼다."""
        self.row.update(values)
        self.bump()

    def execute_update(self, stmt: Any) -> int:
        sql = pg(stmt)
        params = pg_params(stmt)
        assert sql.startswith("UPDATE sources SET"), sql
        where = sql.split(" WHERE ", 1)[1]
        ok = params["id_1"] == self.row["id"]
        if "sources.status = %(status_1)s" in where:
            ok = ok and self.row["status"] == params["status_1"]
        if "sources.status IN (__[POSTCOMPILE_status_1])" in where:
            ok = ok and self.row["status"] in params["status_1"]
        if "sources.updated_at = %(updated_at_1)s" in where:
            ok = ok and self.row["updated_at"] == params["updated_at_1"]
        self.applied.append(sql)
        if not ok:
            return 0
        set_clause = sql.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
        for part in set_clause.split(", "):
            column, value = part.split("=", 1)
            bound = re.match(r"%\((\w+)\)s", value)
            if bound:
                self.row[column] = params[bound.group(1)]
        if "updated_at=now()" in set_clause:
            self.bump()
        return 1

    def load(self) -> Any:
        from biznotes_core.models import Source

        return Source(**self.row)


class TableSession:
    """`SourcesTable`에 붙는 비동기 세션 — UoW·refresh·expunge·get을 지원한다."""

    def __init__(self, table: SourcesTable, chunks: list[str] | None = None):
        self.table = table
        self.chunks = chunks if chunks is not None else ["첫 청크 본문"]
        self.info: dict[str, Any] = {}
        self.updates: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self.refreshed: list[Any] = []
        self.expunged: list[Any] = []
        self.added: list[Any] = []
        self.deleted: list[Any] = []

    async def get(self, model: Any, key: Any) -> Any:
        return self.table.load() if key == self.table.row["id"] else None

    async def execute(self, stmt: Any) -> FakeResult:
        sql = pg(stmt)
        if sql.startswith("SELECT"):
            return FakeResult([(c,) for c in self.chunks])
        self.updates.append(stmt)
        return UpdateResult(self.table.execute_update(stmt))

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def refresh(self, obj: Any) -> None:
        self.refreshed.append(obj)
        for key, value in self.table.row.items():
            setattr(obj, key, value)

    def expunge(self, obj: Any) -> None:
        self.expunged.append(obj)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def delete(self, obj: Any) -> None:
        self.deleted.append(obj)


# ---------------------------------------------------------------------------
# 오브젝트 스토리지
# ---------------------------------------------------------------------------


@pytest.fixture
def use_s3_client(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], Any]:
    """S3 백엔드에 가짜 minio 클라이언트를 꽂는다 — `use_s3_client(fake)`.

    일괄 삭제·거부 항목처럼 S3에만 있는 동작을 보는 테스트가 쓴다. 로컬 백엔드는
    `local_storage`를 쓴다.
    """
    from biznotes_core.services import storage, storage_s3

    def install(client: Any) -> Any:
        monkeypatch.setattr(storage_s3, "get_minio", lambda: client)
        backend = storage_s3.S3Storage()
        monkeypatch.setattr(storage, "get_backend", lambda: backend)
        return backend

    return install


@pytest.fixture
def local_storage(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """이 테스트 전용 로컬 파일 백엔드(`tmp_path/data`)."""
    from biznotes_core.services import storage

    backend = storage.LocalStorage(tmp_path / "data")
    monkeypatch.setattr(storage, "get_backend", lambda: backend)
    return backend


# ---------------------------------------------------------------------------
# PG 테스트(`*_pg.py`·`pytest.mark.pg`) — 실제 PostgreSQL(pgvector)·Valkey
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "pg: 실제 PostgreSQL(pgvector)·Valkey를 쓴다 — 테스트마다 전 테이블을 비운다"
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.path.name.endswith("_pg.py"):
            item.add_marker(pytest.mark.pg)


def _exit(reason: str) -> NoReturn:
    pytest.exit(f"PG 테스트 전제 미충족: {reason} (tests/conftest.py docstring 참고)", returncode=1)


async def _admin_connection() -> Any:
    import asyncpg

    return await asyncpg.connect(
        host=PG_HOST, port=int(PG_PORT), user=PG_USER, password=PG_PASSWORD, database="postgres"
    )


async def _drop_database() -> None:
    conn = await _admin_connection()
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
    finally:
        await conn.close()


async def _dispose_engines() -> None:
    from biznotes_core.core import db as core_db
    from biznotes_core.workers import runner

    await core_db.get_engine().dispose()
    core_db.get_session_factory.cache_clear()
    core_db.get_engine.cache_clear()
    if runner._engine is not None:  # 워커 경로(`_run` 등)가 만든 별도 엔진
        await runner._engine.dispose()
        runner._engine = None
        runner._factory = None


@pytest.fixture(scope="session")
async def pg_database() -> Any:
    """실행 전용 DB 생성·확장·스키마 → (PG 테스트들) → 엔진 정리·DB 삭제.

    PG 테스트가 처음 쓸 때 만든다 — PG 테스트를 하나도 고르지 않은 실행은 서버가 없어도 돈다.
    """
    import asyncpg
    import redis.asyncio as aioredis
    from sqlalchemy import text

    from biznotes_core.models import Base

    try:
        conn = await _admin_connection()
    except (OSError, ValueError, asyncpg.PostgresError) as e:
        _exit(f"PostgreSQL {PG_HOST}:{PG_PORT}에 접속하지 못했습니다 ({e!r}) — make up")
    try:
        await conn.execute(f'CREATE DATABASE "{DB_NAME}"')
    finally:
        await conn.close()

    client = aioredis.from_url(VALKEY_URL)
    try:
        await client.ping()
    except Exception as e:  # noqa: BLE001
        await _drop_database()
        _exit(f"Valkey {VALKEY_URL}에 닿지 않습니다 ({e}) — LLM 게이트웨이·이벤트 경로가 쓴다")
    finally:
        await client.aclose()

    from biznotes_core.core.db import get_engine

    try:
        async with get_engine().begin() as c:
            await c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await c.run_sync(Base.metadata.create_all)
        yield DB_NAME
    finally:
        await _dispose_engines()
        await _drop_database()


@pytest.fixture
async def _pg_clean_tables(pg_database: str) -> Any:
    """테스트가 끝나면 전 테이블을 비운다 — FK 역순 DELETE(`sorted_tables` 역순이 자식→부모)."""
    yield
    from sqlalchemy import text

    from biznotes_core.core.db import get_engine
    from biznotes_core.models import Base

    async with get_engine().begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(text(f'DELETE FROM "{table.name}"'))


@pytest.fixture(autouse=True)
def _pg_isolation(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("pg") is not None:
        request.getfixturevalue("_pg_clean_tables")


@pytest.fixture
async def db(pg_database: str) -> Any:
    from biznotes_core.core.db import get_session_factory

    async with get_session_factory()() as session:
        yield session


_PASSWORD = "pw-test-1234"
_PASSWORD_HASH: str | None = None


async def _create_user(db: Any, prefix: str, role: str = "user") -> Any:
    global _PASSWORD_HASH
    from biznotes_core.core.security import hash_password
    from biznotes_core.models import User

    if _PASSWORD_HASH is None:  # bcrypt는 느리므로 해시 1회 재사용
        _PASSWORD_HASH = hash_password(_PASSWORD)
    email = f"{prefix}-{uuid.uuid4().hex[:8]}@test.dev"
    user = User(email=email, password_hash=_PASSWORD_HASH, name=prefix, role=role)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def share_to_user(notebook_id: uuid.UUID, user_id: uuid.UUID, permission: str) -> list[Any]:
    """사용자 한 명에게 노트북 공유 권한을 주는 행 — 그 사용자만 속한 그룹 + 그룹 공유.

    사용자 대상 공유는 #278 3단계에서 없앴다. 세 행을 한 번에 `add_all`한다(FK 순서는 flush가
    맞춘다). 마지막 원소가 공유 행이라 지우면 접근을 거둔다.
    """
    from biznotes_core.models import Group, NotebookShare, UserGroup

    group = Group(id=uuid.uuid4(), name=f"share-{uuid.uuid4().hex[:12]}")
    return [
        group,
        UserGroup(user_id=user_id, group_id=group.id),
        NotebookShare(notebook_id=notebook_id, group_id=group.id, permission=permission),
    ]


@pytest.fixture
async def user_a(db: Any) -> Any:
    return await _create_user(db, "user-a")


@pytest.fixture
async def user_b(db: Any) -> Any:
    return await _create_user(db, "user-b")


@pytest.fixture
async def admin_user(db: Any) -> Any:
    return await _create_user(db, "admin", role="admin")


@pytest.fixture
async def mock_llm(db: Any) -> None:
    """기본(is_default) mock LLM 프로바이더+모델.

    반환값 없음: "is_default mock 모델이 존재한다"는 부수효과만 제공한다.
    프로바이더 type="mock"이 게이트웨이의 mock 응답 경로를 결정하며 model_name은 임의값.
    """
    from biznotes_core.models import LLMModel, LLMProvider

    provider = LLMProvider(name=f"mock-{uuid.uuid4().hex[:6]}", type="mock", enabled=True)
    db.add(provider)
    await db.flush()
    db.add(
        LLMModel(
            provider_id=provider.id,
            model_name="mock-rag",
            display_name="Mock",
            capabilities=["llm"],
            is_default=True,
            enabled=True,
        )
    )
    await db.commit()


@pytest.fixture
def _mock_runtime_deps(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """에이전트 런타임 외부 의존(RAG 검색·아티팩트 저장) 모킹.

    retrieve는 "결과 없음"을 반환한다. 반환값은 put_object로 저장된 아티팩트 dict(key → bytes).
    """
    artifacts: dict[str, bytes] = {}

    async def fake_retrieve(db: Any, notebook_id: Any, query: Any, source_ids: Any, top_k: int = 6):
        from biznotes_core.services.rag_chat import RetrievalResult

        return RetrievalResult(citations=[], insufficient_context=True)

    monkeypatch.setattr("biznotes_core.services.rag_chat.retrieve_context", fake_retrieve)

    def fake_put(bucket: str, key: str, data: bytes, content_type: str = "text/plain") -> None:
        artifacts[key] = data

    monkeypatch.setattr("biznotes_core.services.storage.ensure_buckets", lambda: None)
    monkeypatch.setattr("biznotes_core.services.storage.put_object", fake_put)
    return artifacts


class FakeObjectStore:
    """인제스트 워커가 쓰는 스토리지 함수의 대역 — `(버킷, 키) → 바이트`. 올린 순서를 남긴다.

    없는 키는 `MissingObject`로 답한다 — 색인 단계(#244)는 없는 locations.json을 위치 없음으로,
    없는 parsed.md를 실패로 읽는다.
    """

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.puts: list[tuple[str, str]] = []

    def get(self, bucket: str, key: str) -> bytes:
        try:
            return self.objects[(bucket, key)]
        except KeyError:
            raise MissingObject(key) from None

    def put(
        self, bucket: str, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        self.puts.append((bucket, key))
        self.objects[(bucket, key)] = data

    def stat(self, bucket: str, key: str) -> int:
        return len(self.get(bucket, key))

    def delete(self, bucket: str, key: str) -> None:
        self.objects.pop((bucket, key), None)


@pytest.fixture
def ingest_objects(monkeypatch: pytest.MonkeyPatch) -> FakeObjectStore:
    """인제스트 워커(`workers.tasks.ingestion`)의 스토리지 함수를 사전 기반 대역으로 바꾼다.

    진입 단계가 올린 parsed.md·locations.json을 색인 단계가 그대로 읽는다(#244).
    """
    from biznotes_core.workers.tasks import ingestion

    store = FakeObjectStore()
    monkeypatch.setattr(ingestion, "get_object", store.get)
    monkeypatch.setattr(ingestion, "put_object", store.put)
    monkeypatch.setattr(ingestion, "stat_object_size", store.stat)
    monkeypatch.setattr(ingestion, "ensure_buckets", lambda: None)
    return store


async def add_chunks(
    db: Any,
    source: Any,
    vectors: list[tuple[list[float], Any]],
    *,
    texts: list[str] | None = None,
    space_key: str | None = None,
) -> list[Any]:
    """검색 대상 청크 행(벡터 포함)을 넣고 커밋한다 — 인제스트 색인 단계가 쓰는 모양 그대로.

    `vectors`는 `(dense, sparse)` 목록이다(sparse는 `SparseVector`). `space_key`를 주면 소스의
    임베딩 공간(`sources.embedding_model`)을 그 값으로 기록한다(None은 로컬 기본 공간).
    청크 수는 소스의 `chunk_count`에 더한다.
    """
    from biznotes_core.models import SourceChunk

    start = source.chunk_count or 0
    rows = [
        SourceChunk(
            source_id=source.id,
            seq=start + i,
            text=texts[i] if texts else f"{source.title} 청크 {start + i}",
            meta={},
            embedding=dense,
            sparse=sparse,
        )
        for i, (dense, sparse) in enumerate(vectors)
    ]
    db.add_all(rows)
    source.chunk_count = start + len(rows)
    if space_key is not None:
        source.embedding_model = space_key
    await db.commit()
    return rows
