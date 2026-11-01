"""`sources` write-site 대장(§12 Phase 1)의 앵커별 상태 전제.

verdict가 owner·legit_transfer·guarded인 앵커(아래 `ANCHORS`)마다 케이스가 정확히 하나다.
(원본 저장소에서는 docs/ 대장과 이 목록을 대조했다. biznotes는 docs/를 가져오지 않아 대조를 뺐다.)
각 케이스는 그 경로가 남의 in-flight 행(`parsing`·`chunking`·`embedding`)을 쓰지 않음을
실경로로 확인한다 — owner는 소유자 자신의 구간임을, legit_transfer는 status·시간 조건을
거치는 정당한 이전임을, guarded는 조건부 UPDATE가 0행으로 빠짐을.
"""

import ast
import contextlib
import pathlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import (
    FakeResult,
    RunHarness,
    SourcesTable,
    TableSession,
    make_settings,
    pg,
    pg_params,
)
from fastapi import HTTPException

from biznotes_core.api.v1 import sources
from biznotes_core.models import Source
from biznotes_core.services import source_guide, storage
from biznotes_core.services.ingestion import enqueue, source_warnings
from biznotes_core.services.ingestion.parsers import ParseResult
from biznotes_core.workers import parsing_heartbeat as hb
from biznotes_core.workers.celery_app import celery_app
from biznotes_core.workers.tasks import ingestion

IN_FLIGHT = ("parsing", "chunking", "embedding")

ANCHORS = [
    "biznotes_core/api/v1/sources.py::create_source::attr_assign::original_object_key",
    "biznotes_core/api/v1/sources.py::upload_file_source::attr_assign::original_object_key",
    "biznotes_core/api/v1/sources.py::delete_source::orm_delete::source",
    "biznotes_core/api/v1/sources.py::retry_source::stmt::Source",
    "biznotes_core/services/source_guide.py::generate_source_guide::stmt::Source",
    "biznotes_core/services/source_guide.py::generate_source_guide::attr_assign::summary",
    "biznotes_core/services/source_guide.py::generate_source_guide::attr_assign::keywords",
    "biznotes_core/workers/parsing_heartbeat.py::_entry_stmt::stmt::Source",
    "biznotes_core/workers/parsing_heartbeat.py::_tick_stmt::stmt::Source",
    "biznotes_core/workers/tasks/ingestion.py::_transition::stmt::Source",
    "biznotes_core/workers/tasks/ingestion.py::_parse_stage::attr_assign::title",
    "biznotes_core/workers/tasks/ingestion.py::_parse_stage::attr_assign::parsed_object_key",
    "biznotes_core/workers/tasks/ingestion.py::_run_index::attr_assign::token_count",
    "biznotes_core/workers/tasks/ingestion.py::_run_index::attr_assign::chunk_count",
    "biznotes_core/workers/tasks/ingestion.py::_run_index::attr_assign::embedding_model",
    "biznotes_core/workers/tasks/ingestion.py::_run_index::attr_assign::chunk_config",
    "biznotes_core/workers/tasks/ingestion.py::_reap_stalled::stmt::Source",
    "biznotes_core/workers/tasks/ingestion.py::_parse_stage::attr_assign::document_title",
    "biznotes_core/workers/tasks/ingestion.py::_parse_stage::attr_assign::warnings",
    "biznotes_core/workers/tasks/ingestion.py::_embedding_tick_stmt::stmt::Source",
    "biznotes_core/workers/tasks/ingestion.py::_claim_stmt::stmt::Source",
    "biznotes_core/workers/tasks/ingestion.py::_drop_stale_deferral_stmt::stmt::Source",
]


# ---------------------------------------------------------------------------
# 공용 준비
# ---------------------------------------------------------------------------


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    env: dict[str, Any] = {"puts": [], "published": [], "deleted_objects": [], "cleanups": []}

    async def permitted(db: Any, notebook_id: Any, user: Any, need_write: bool) -> None:
        return None

    async def capacity(db: Any, notebook_id: Any, *a: Any) -> Any:
        return a[0] if a else None

    monkeypatch.setattr(sources, "_notebook_with_permission", permitted)
    monkeypatch.setattr(sources, "ensure_source_capacity", capacity)
    monkeypatch.setattr(sources, "ensure_file_source_capacity_and_uniqueness", capacity)
    monkeypatch.setattr(sources, "ensure_buckets", lambda: None)
    monkeypatch.setattr(sources, "put_object", lambda *a, **k: env["puts"].append(a[1]))
    monkeypatch.setattr(sources, "get_settings", lambda: make_settings())
    monkeypatch.setattr(enqueue, "_publish", lambda sid, tid: env["published"].append(sid))
    monkeypatch.setattr(storage, "delete_object", lambda b, k: env["deleted_objects"].append(k))
    monkeypatch.setattr(
        storage, "delete_objects", lambda b, keys: env["deleted_objects"].extend(keys) or []
    )
    # 소스 삭제는 태스크를 내지 않는다 — 청크 벡터는 행과 함께 CASCADE로 사라진다(pgvector)
    def send_task(name: str, args: Any = None, kwargs: Any = None, **_kw: Any) -> None:
        env["cleanups"].append(name)

    monkeypatch.setattr(celery_app, "send_task", send_task)
    return env


class CreateSession(TableSession):
    """새 행을 만드는 요청 — add된 객체의 상태를 커밋 시점에 기록한다."""

    def __init__(self) -> None:
        super().__init__(SourcesTable())
        self.at_commit: list[dict[str, Any]] = []

    async def commit(self) -> None:
        await super().commit()
        for obj in self.added:
            if isinstance(obj, Source):
                self.at_commit.append(
                    {"status": obj.status, "original_object_key": obj.original_object_key}
                )

    async def refresh(self, obj: Any) -> None:
        obj.created_at = obj.updated_at = datetime.now(UTC)
        for key in ("token_count", "chunk_count"):
            setattr(obj, key, 0)


def _harness_with(h: RunHarness, markdown: str = "본문", **result: Any) -> None:
    h.parser(lambda data: ParseResult(markdown=markdown, **result))


def _in_window_writes(h: RunHarness) -> list[tuple[str, Any]]:
    return h.db_writes_during_parsing()


# ---------------------------------------------------------------------------
# 케이스 — 앵커마다 하나
# ---------------------------------------------------------------------------


async def _create_source(api: dict[str, Any], h: Any) -> None:
    from biznotes_core.schemas.source import SourceCreate

    db = CreateSession()
    await sources.create_source(
        uuid.uuid4(), SourceCreate(type="text", text="본문"), user=object(), db=db
    )
    # 새 행은 pending으로만 만들어지고, 키 대입은 그 행이 커밋되기 전에 끝난다
    assert [c["status"] for c in db.at_commit] == ["pending"]
    assert db.at_commit[0]["original_object_key"].endswith("/raw.txt")
    assert db.updates == []  # 기존 행을 쓰지 않는다


async def _upload_file_source(api: dict[str, Any], h: Any) -> None:
    class Upload:
        filename = "a.pdf"

        async def read(self, size: int = -1) -> bytes:
            return b"%PDF-1.7 body"

    db = CreateSession()
    await sources.upload_file_source(uuid.uuid4(), Upload(), user=object(), db=db)
    assert [c["status"] for c in db.at_commit] == ["pending"]
    assert db.at_commit[0]["original_object_key"].endswith("/raw.pdf")
    assert db.updates == []


async def _delete_source(api: dict[str, Any], h: Any) -> None:
    # 행 삭제는 정당한 이전이다 — parsing 중이었다면 heartbeat가 0행에 매칭돼 멈춘다
    table = SourcesTable(status="parsing")
    db = TableSession(table)
    await sources.delete_source(table.row["id"], user=object(), db=db)
    assert len(db.deleted) == 1 and db.commits == 1

    class Gone:
        async def __aenter__(self) -> "Gone":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def execute(self, stmt: Any) -> FakeResult:
            return FakeResult([])  # 행이 없다

        async def commit(self) -> None:
            return None

    with pytest.raises(hb.ParsingOwnershipLost):
        async with hb.parsing_heartbeat(
            Gone, table.row["id"], interval_sec=1, tick_timeout_sec=1
        ):
            pass


async def _retry_source(api: dict[str, Any], h: Any) -> None:
    for status in IN_FLIGHT + ("pending",):
        table = SourcesTable(status=status)
        db = TableSession(table)
        with pytest.raises(HTTPException) as exc:
            await sources.retry_source(table.row["id"], user=object(), db=db)
        assert exc.value.status_code == 409 and db.updates == []
    for status in ("failed", "ready"):
        old = [{"code": "text_truncated", "params": {"omitted": 1}}]
        table = SourcesTable(status=status, document_title="옛 제목", warnings=old)
        await sources.retry_source(table.row["id"], user=object(), db=TableSession(table))
        assert table.row["status"] == "pending"
        # 옛 본문의 문서 제목·경고도 지운다(HWP-02·03)
        assert table.row["document_title"] is None and table.row["warnings"] is None


@pytest.fixture
def guide_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    async def stream(*a: Any, **k: Any) -> Any:
        async def gen() -> Any:
            yield '{"summary": "요약", "keywords": ["키"]}'

        return gen()

    monkeypatch.setattr(source_guide, "complete_stream_with_fallback", stream)


async def _guide_in_flight(api: dict[str, Any], h: Any) -> tuple[SourcesTable, Any, TableSession]:
    table = SourcesTable(status="parsing")
    db = TableSession(table)
    source = table.load()
    out = await source_guide.generate_source_guide(db, source)
    return table, out, db


async def _guide_stmt(api: dict[str, Any], h: Any) -> None:
    for status in IN_FLIGHT:
        table = SourcesTable(status=status)
        db = TableSession(table)
        before = dict(table.row)
        await source_guide.generate_source_guide(db, table.load())
        assert table.row == before  # 조건부 UPDATE가 0행
        assert pg_params(db.updates[0])["status_1"] == "ready"


async def _guide_summary(api: dict[str, Any], h: Any) -> None:
    table, out, db = await _guide_in_flight(api, h)
    assert db.expunged == [out] and out.summary == "요약"
    assert table.row["summary"] is None and db.commits == 1  # 대입 뒤 커밋 없음


async def _guide_keywords(api: dict[str, Any], h: Any) -> None:
    table, out, db = await _guide_in_flight(api, h)
    assert db.expunged == [out] and out.keywords == ["키"]
    assert table.row["keywords"] is None and len(db.updates) == 1


def _hb_row_check(stmt: Any, *, last_seen: datetime | None) -> None:
    sql = pg(stmt)
    assert "sources.status = %(status_1)s" in sql
    assert pg_params(stmt)["status_1"] == "parsing"
    for status in ("chunking", "embedding", "pending", "ready", "failed"):
        table = SourcesTable(status=status)
        stmt_for_row = (
            hb._entry_stmt(table.row["id"])
            if last_seen is None
            else hb._tick_stmt(table.row["id"], table.row["updated_at"], None)
        )
        before = dict(table.row)
        assert table.execute_update(stmt_for_row) == 0
        assert table.row == before


async def _entry_stmt(api: dict[str, Any], h: Any) -> None:
    _hb_row_check(hb._entry_stmt(uuid.uuid4()), last_seen=None)


async def _tick_stmt(api: dict[str, Any], h: Any) -> None:
    seen = datetime(2026, 9, 17, tzinfo=UTC)
    _hb_row_check(hb._tick_stmt(uuid.uuid4(), seen, None), last_seen=seen)
    # 소유자가 마지막으로 본 값이 아니면 parsing 행이라도 쓰지 않는다
    table = SourcesTable(status="parsing")
    stale = hb._tick_stmt(table.row["id"], table.row["updated_at"] - timedelta(seconds=1), None)
    assert table.execute_update(stale) == 0


def _callers(name: str) -> set[str]:
    tree = ast.parse(pathlib.Path(ingestion.__file__).read_text(encoding="utf-8"))
    return {
        func.name
        for func in ast.walk(tree)
        if isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef)
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == name
    }


async def _embedding_tick_stmt(api: dict[str, Any], h: Any) -> None:
    # 소유자 자신 — `_run_index`의 embedding 구간(점유 뒤 게이트웨이 배치 루프)에서만 낸다(#244)
    assert _callers("_embedding_tick_stmt") == {"_embedding_heartbeat"}
    assert _callers("_embedding_heartbeat") == {"_embed_gateway"}
    assert _callers("_embed_gateway") == {"_run_index"}
    stmt = ingestion._embedding_tick_stmt(uuid.uuid4())
    assert pg_params(stmt)["status_1"] == "embedding"
    for status in ("parsing", "chunking", "pending", "ready", "failed"):
        table = SourcesTable(status=status)
        before = dict(table.row)
        assert table.execute_update(ingestion._embedding_tick_stmt(table.row["id"])) == 0
        assert table.row == before
    table = SourcesTable(status="embedding")
    before = dict(table.row)
    assert table.execute_update(ingestion._embedding_tick_stmt(table.row["id"])) == 1
    changed = {k for k in table.row if table.row[k] != before[k]}
    assert changed == {"updated_at"}


def _calls_of(name: str) -> list[tuple[str, dict[str, str]]]:
    """`name(...)` 호출마다 (부르는 함수, 키워드 인자의 소스 표기)."""
    tree = ast.parse(pathlib.Path(ingestion.__file__).read_text(encoding="utf-8"))
    return [
        (func.name, {k.arg: ast.unparse(k.value) for k in node.keywords if k.arg})
        for func in ast.walk(tree)
        if isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef)
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == name
    ]


def _transition_calls() -> set[tuple[str, str, str]]:
    return {(caller, kw["frm"], kw["to"]) for caller, kw in _calls_of("_transition")}


def _owned_calls() -> set[tuple[str, str, str]]:
    return {
        (caller, name, kw["owned"])
        for name in ("_reread_after_prep_failure", "_write_failure")
        for caller, kw in _calls_of(name)
    }


class _RowSession:
    """`SourcesTable` 행 하나에 조건부 `UPDATE … RETURNING`을 적용하는 세션."""

    def __init__(self, table: SourcesTable):
        self.table = table
        self.no_autoflush = contextlib.nullcontext()

    async def execute(self, stmt: Any) -> FakeResult:
        moved = self.table.execute_update(stmt)
        return FakeResult([(self.table.row["updated_at"],)] if moved else [])


async def _transition_stmt_shape() -> None:
    """출발 상태가 아니면 0행이고 행을 바꾸지 않는다. 맞으면 status·error_message·updated_at만."""
    statuses = ("pending", "parsing", "chunking", "embedding", "ready", "failed")
    cases = [
        (ingestion._PARSE_FROM_STATUSES, "parsing", None),  # 파싱 점유(일반 인제스트)
        (ingestion._REINDEX_PARSE_FROM_STATUSES, "parsing", None),  # 파싱 점유(재인덱싱)
        ("parsing", "chunking", None),  # 핸드오프
        ("embedding", "ready", None),  # 색인 교체(savepoint 안)
        ("embedding", "failed", "손상"),  # 실패 기록
    ]
    for frm, to, error in cases:
        allowed = (frm,) if isinstance(frm, str) else frm
        for status in statuses:
            table = SourcesTable(status=status, error_message="이전 안내")
            before = dict(table.row)
            source = table.load()
            got = await ingestion._transition(
                _RowSession(table), source, frm=frm, to=to, error=error  # type: ignore[arg-type]
            )
            if status not in allowed:
                assert got is None and table.row == before, (frm, status)
                assert source.status == status
                continue
            assert got == table.row["updated_at"] > before["updated_at"]
            assert (table.row["status"], table.row["error_message"]) == (to, error)
            changed = {k for k in table.row if table.row[k] != before[k]}
            # parsing → parsing(남은 parsing 점유)은 status 값이 같다 — 토큰(updated_at)이 바뀐다
            assert changed == {"error_message", "updated_at"} | (
                {"status"} if status != to else set()
            )
            assert (source.status, source.error_message) == (to, error)  # 메모리 값도 맞춘다
    # 경고는 넘길 때만 쓴다(#230이 쓴다) — 기본값은 컬럼을 건드리지 않는다
    table = SourcesTable(status="embedding", warnings=[{"code": "text_truncated"}])
    source = table.load()
    new = [{"code": "reindex_kept_previous", "params": {"reason": "parse"}}]
    await ingestion._transition(
        _RowSession(table), source, frm="embedding", to="ready", error=None, warnings=new  # type: ignore[arg-type]
    )
    assert table.row["warnings"] == new and source.warnings == new


def _updates(h: RunHarness) -> list[tuple[str, bool]]:
    return [e[1] for e in h.timeline if e[0] == "db.update"]


async def _transition_stmt(api: dict[str, Any], h: RunHarness) -> None:
    # 상태 쓰기는 모두 조건부 전이다(#244 리뷰 B1). 출발 상태는 이 실행이 쥔 상태뿐이다 —
    # 파싱 점유만 여러 출발 상태를 받고, chunking·embedding(다른 세대가 색인 중)은 받지 않는다.
    # 일반 인제스트는 pending·parsing에서만, 원본 재파싱 재인덱싱만 종결 상태에서도 받는다(R-M1)
    assert ingestion._PARSE_FROM_STATUSES == ("pending", "parsing")
    assert ingestion._REINDEX_PARSE_FROM_STATUSES == ("pending", "parsing", "ready", "failed")
    assert _transition_calls() == {
        ("_run", "parse_from", "'parsing'"),
        ("_run", "'parsing'", "claim_from"),
        ("_run", "'pending'", "'chunking'"),  # 구 버전이 미룬 신규 소스
        ("_run_index", "'embedding'", "'ready'"),
        ("_defer_transient", "'embedding'", "to"),
        ("_write_failure", "owned", "to"),
        ("_reread_after_prep_failure", "owned", "'ready'"),
    }
    # 실패 처리는 그 단계가 쥔 상태를 넘긴다 — 진입은 parsing, 색인은 embedding
    assert _owned_calls() == {
        ("_run", "_reread_after_prep_failure", "'parsing'"),
        ("_run", "_write_failure", "'parsing'"),
        ("_run_index", "_reread_after_prep_failure", "'embedding'"),
        ("_run_index", "_write_failure", "'embedding'"),
    }
    await _transition_stmt_shape()

    # 실경로 — 진입: 파싱 점유 → 핸드오프. 색인: 점유(`_claim_stmt`) → ready(savepoint 안)
    _harness_with(h)
    await h.run()
    assert _updates(h) == [
        ("parsing", True), ("chunking", True), ("embedding", True), ("ready", True)
    ]
    assert [c["status"] for c in h.committed] == ["parsing", "chunking", "embedding", "ready"]
    # 다른 세대가 색인 중인 행(재배달·중복 메시지)은 파싱하지 않고 아무것도 쓰지 않는다
    h.source.status = "embedding"
    commits = len(h.committed)
    assert await h.run_entry() == {"ok": False, "error": "already_parsed"}
    assert _updates(h)[-1] == ("parsing", False) and len(h.committed) == commits
    await _transition_failure(RunHarness(h.mp))


async def _transition_failure(h: RunHarness) -> None:
    """실패는 쥔 상태(parsing)에서만 쓴다 — 그 사이 행을 잃었으면 쓰지도 항목을 닫지도 않는다."""
    from biznotes_core.services.ingestion.parsers import ParseError

    def broken(data: bytes) -> ParseResult:
        raise ParseError("손상")

    h.parser(broken)
    await h.run()
    assert _updates(h) == [("parsing", True), ("failed", True)]
    assert [c["status"] for c in h.committed] == ["parsing", "failed"]
    assert h.committed[-1]["error_message"] == "손상"
    assert h.closed_reindex == [(str(h.source.id), False, "손상")]

    lost = RunHarness(h.mp)

    def taken(data: bytes) -> ParseResult:
        lost.source.status = "pending"  # 파싱 중 다른 실행(reaper·사용자 재시도)이 가져갔다
        raise ParseError("손상")

    lost.parser(taken)
    await lost.run()
    assert _updates(lost) == [("parsing", True), ("failed", False)]
    assert [c["status"] for c in lost.committed] == ["parsing"]
    assert lost.closed_reindex == []


async def _parse_stage_title(api: dict[str, Any], h: RunHarness) -> None:
    h.source.title = "(제목 추출 중) https://example.test"
    _harness_with(h, title="찾은 제목")
    await h.run()
    assert h.source.title == "찾은 제목"
    assert _in_window_writes(h) == []


async def _parse_stage_parsed_object_key(api: dict[str, Any], h: RunHarness) -> None:
    _harness_with(h)
    await h.run()
    assert _in_window_writes(h) == []
    assert h.committed[1] == {
        "status": "chunking",
        "error_message": None,
        "parsed_object_key": f"{h.source.id}/parsed.md",
    }


async def _parse_stage_document_metadata(api: dict[str, Any], h: RunHarness) -> None:
    """문서 제목·경고(HWP-02·03)도 parsing 구간 안에서 대입하고 핸드오프 전이에서 커밋한다."""
    found = [source_warnings.warning(source_warnings.TEXT_TRUNCATED, omitted=2)]
    _harness_with(h, title="문서\n제목", source_warnings=found)
    await h.run()
    assert h.source.title == "doc.test"  # 파일명(중복 식별 키)은 그대로다
    assert h.source.document_title == "문서 제목"
    assert h.source.warnings == [{"code": "text_truncated", "params": {"omitted": 2}}]
    assert _in_window_writes(h) == []


async def _index_stage(h: RunHarness) -> None:
    """진입 단계는 출처·개수 컬럼을 대입하지 않는다 — 색인 단계가 점유한 뒤에만 대입한다(#244)."""
    _harness_with(h, markdown="본문 " * 50)
    entry = await h.run_entry()
    assert (h.source.status, h.source.token_count, h.source.chunk_count) == ("chunking", 0, 0)
    assert h.source.embedding_model is None and h.source.chunk_config is None
    assert ("db.update", ("embedding", True)) not in h.timeline
    await h.run_index(**entry["handoff"])
    # 점유(embedding)한 소유자가 savepoint 안에서 대입·flush한 뒤, 조건부 ready 전이와 함께
    # 커밋한다(parsing 구간 밖, 리뷰 B1·M2)
    assert [c["status"] for c in h.committed][-2:] == ["embedding", "ready"]
    order = [
        h.timeline.index(e)
        for e in (
            ("db.update", ("embedding", True)),
            ("db.savepoint", None),
            ("db.flush", None),
            ("db.update", ("ready", True)),
            ("db.release", None),
            ("db.commit", "ready"),
        )
    ]
    assert order == sorted(order)


async def _run_counts(api: dict[str, Any], h: RunHarness) -> None:
    await _index_stage(h)
    assert h.source.token_count > 0 and h.source.chunk_count >= 1


async def _run_vector_origin(api: dict[str, Any], h: RunHarness) -> None:
    """벡터 출처 기록 — counts와 같은 자리(점유 뒤, ready 전이에서 함께 커밋)."""
    from biznotes_core.services.embedding_spaces import LOCAL_SPACE

    await _index_stage(h)
    assert h.source.embedding_model == LOCAL_SPACE.key
    assert h.source.chunk_config == {"chunk_size": 800, "chunk_overlap": 100}


class _Rows(FakeResult):
    def tuples(self) -> "_Rows":
        return self


class ReaperSession:
    """정체 소스 회수의 세션 — `sources` 행(`SourcesTable`)마다 조회와 조건부 UPDATE를 평가한다.

    `SourcesTable`은 `updated_at = …`만 평가하므로 reaper의 `updated_at < cutoff`는 여기서 본다.
    `between`은 조회와 전이 사이에 살아 있는 소유자가 행을 쓴 것처럼 바꾼다.
    """

    def __init__(self, tables: list[SourcesTable], between: Callable[[], None]):
        self.tables = {t.row["id"]: t for t in tables}
        self.between = between
        self.statements: list[Any] = []
        self.timeline: list[tuple[Any, ...]] = []

    async def __aenter__(self) -> "ReaperSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, stmt: Any) -> Any:
        if str(stmt).startswith("SET LOCAL"):  # 스윕의 계획 모드 고정(#44 D4) — 쓰기가 아니다
            return None
        self.statements.append(stmt)
        sql, params = pg(stmt), pg_params(stmt)
        if sql.startswith("UPDATE sources"):
            table = self.tables.get(params["id_1"])
            moved = 0
            if table is not None and table.row["updated_at"] < params["updated_at_1"]:
                moved = table.execute_update(stmt)
            self.timeline.append(("update", params["id_1"], moved))
            return FakeResult([(params["id_1"],)] if moved else [])  # RETURNING sources.id
        if "FROM sources" in sql and "sources.updated_at < " in sql:
            # 정체 행 조회 — 고른 컬럼 순서 그대로 튜플을 만든다
            keys = [c.key for c in stmt.selected_columns]
            rows = [
                tuple(t.row.get(k) for k in keys)
                for t in self.tables.values()
                if t.row["status"] in params["status_1"]
                and t.row["updated_at"] < params["updated_at_1"]
            ]
            self.between()
            return _Rows(rows)
        # 정체된 재인덱싱 항목은 없다 — 이 케이스가 보는 것은 sources 행의 쓰기다
        return _Rows([])

    async def scalar(self, stmt: Any) -> bool:
        # 재인덱싱 대기열 흐름 판정 — 흐르지 않는 것으로 두어 항목 조회까지 탄다
        return False

    async def commit(self) -> None:
        self.timeline.append(("commit",))

    async def rollback(self) -> None:
        self.timeline.append(("rollback",))


async def _reap_stmt(api: dict[str, Any], h: RunHarness) -> None:
    parsed = "k/parsed.md"
    rows = {
        # 이미 pending·NULL인 같은 값 행 — 명시한 updated_at 덕에 UPDATE가 나간다(#244 수락 기준 9)
        "pending": SourcesTable(status="pending"),
        "parsing": SourcesTable(status="parsing", error_message="x"),
        "chunking": SourcesTable(status="chunking", parsed_object_key=parsed),
        "embedding": SourcesTable(status="embedding", parsed_object_key=parsed),
        # 파싱 결과가 없는 색인 단계 행(예외 상황) — 처음부터 다시 한다
        "no_parsed": SourcesTable(status="chunking", parsed_object_key=None),
        "fresh": SourcesTable(status="parsing", updated_at=datetime.now(UTC)),  # 살아 있다
        "done": SourcesTable(status="ready", parsed_object_key=parsed),
        # 조회 뒤 살아 있는 소유자가 쓴 행 — heartbeat 틱, 색인 단계 점유
        "beat": SourcesTable(status="parsing"),
        "claimed": SourcesTable(status="chunking", parsed_object_key=parsed),
    }

    def owners_write() -> None:
        rows["beat"].row["updated_at"] = datetime.now(UTC)
        rows["claimed"].write(status="embedding")

    session = ReaperSession(list(rows.values()), between=owners_write)
    published: list[tuple[str, list[str], dict[str, Any]]] = []

    def publisher(task: str) -> Callable[..., None]:
        def apply_async(args: list[str], **kw: Any) -> None:
            session.timeline.append(("publish", uuid.UUID(args[0])))
            published.append((task, args, kw))

        return apply_async

    h.mp.setattr(ingestion, "get_session_factory", lambda: lambda: session)
    h.mp.setattr(ingestion.ingest_source, "apply_async", publisher("ingest_source"))
    h.mp.setattr(ingestion.index_source, "apply_async", publisher("index_source"))
    before_rows = {name: dict(t.row) for name, t in rows.items()}
    before = datetime.now(UTC)
    assert await ingestion._reap_stalled() == {"requeued": 5, "reindex_requeued": 0}
    after = datetime.now(UTC)

    select_stmt = session.statements[0]
    sql = pg(select_stmt)
    params = pg_params(select_stmt)
    # 15분 넘게 멈춘 in-flight 행만 고른다 — 살아 있는 소유자는 heartbeat로 updated_at을 갱신한다
    assert "sources.updated_at < %(updated_at_1)s" in sql
    assert set(params["status_1"]) == {"pending", *IN_FLIGHT}
    cutoff = params["updated_at_1"]
    assert before - timedelta(minutes=15) <= cutoff <= after - timedelta(minutes=15)

    # 전이는 행마다 조건부 UPDATE다 — 조회한 status 그대로, 같은 cutoff로 다시 건다
    updates = [s for s in session.statements if pg(s).startswith("UPDATE sources")]
    stalled = ("pending", "parsing", "chunking", "embedding", "no_parsed", "beat", "claimed")
    assert [pg_params(s)["id_1"] for s in updates] == [rows[n].row["id"] for n in stalled]
    for stmt in updates:
        where = pg(stmt).split(" WHERE ", 1)[1]
        assert where == (
            "sources.id = %(id_1)s::UUID AND sources.status = %(status_1)s "
            "AND sources.updated_at < %(updated_at_1)s RETURNING sources.id"
        )
        p = pg_params(stmt)
        assert p["status_1"] == before_rows[_name_of(rows, p["id_1"])]["status"]
        assert p["updated_at_1"] == cutoff

    # 파싱이 끝난 행(chunking·embedding + parsed.md)은 chunking으로 되돌려 색인 단계부터,
    # 나머지는 pending으로 처음부터. 모두 큐 인자 없이 낸다(`task_routes`가 `ingestion`으로 보낸다)
    sid = {name: str(t.row["id"]) for name, t in rows.items()}
    assert published == [
        ("ingest_source", [sid["pending"]], {}),
        ("ingest_source", [sid["parsing"]], {}),
        ("index_source", [sid["chunking"]], {}),
        ("index_source", [sid["embedding"]], {}),
        ("ingest_source", [sid["no_parsed"]], {}),
    ]
    expected_after = {
        "pending": "pending",
        "parsing": "pending",
        "chunking": "chunking",
        "embedding": "chunking",
        "no_parsed": "pending",
    }
    for name, status in expected_after.items():
        row = rows[name].row
        assert (row["status"], row["error_message"]) == (status, None), name
        assert row["updated_at"] > before_rows[name]["updated_at"], name  # 같은 값 행도 갱신
    # 고르지 않은 행과 조회 뒤 소유자가 쓴 행은 덮지 않는다
    assert rows["fresh"].row == before_rows["fresh"] and rows["done"].row == before_rows["done"]
    assert rows["beat"].row["status"] == "parsing" and rows["beat"].row["error_message"] is None
    assert rows["claimed"].row["status"] == "embedding"
    assert ("update", rows["beat"].row["id"], 0) in session.timeline
    assert ("update", rows["claimed"].row["id"], 0) in session.timeline
    # 전이를 커밋한 뒤에 발행한다 — 워커가 커밋 전 행을 읽지 않는다
    for name in expected_after:
        key = rows[name].row["id"]
        at = session.timeline.index(("update", key, 1))
        assert session.timeline[at + 1] == ("commit",)
        assert session.timeline[at + 2] == ("publish", key)
    # 재인덱싱 항목 회수는 같은 세션을 쓰지만 sources 행을 쓰지 않는다(다른 테이블이다)
    rest = [s for s in session.statements[1:] if not pg(s).startswith("UPDATE sources")]
    assert all(pg(s).lstrip().upper().startswith("SELECT") for s in rest)
    # 되돌린 pending의 updated_at 갱신은 미룬 재시도의 토큰도 무효로 만든다(D3-B) — 낡은
    # 재시도는 점유하지 못하고, reaper가 낸 진입 태스크가 처리한다
    stale_token = before_rows["pending"]["updated_at"]
    claim = ingestion._claim_stmt(rows["pending"].row["id"], "pending", stale_token)
    assert rows["pending"].execute_update(claim) == 0


def _name_of(rows: dict[str, SourcesTable], key: Any) -> str:
    return next(name for name, t in rows.items() if t.row["id"] == key)


def _claim_on(table: SourcesTable, items: list[dict[str, Any]], stmt: Any) -> int:
    """점유 문장을 행 하나와 재인덱싱 항목 목록에 적용한다 — EXISTS 부분은 여기서 평가한다."""
    sql, params = pg(stmt), pg_params(stmt)
    if "EXISTS (SELECT reindex_run_items.id" in sql:
        # 같은 소스의 queued 항목이어야 한다 — 문장 안의 상관 조건을 그대로 본다
        assert "reindex_run_items.source_id = sources.id" in sql
        assert params["status_2"] == "queued"
        if not any(
            i["source_id"] == table.row["id"] and i["status"] == params["status_2"] for i in items
        ):
            table.applied.append(sql)
            return 0
    return table.execute_update(stmt)


async def _claim_stmt(api: dict[str, Any], h: RunHarness) -> None:
    # 색인 단계(`_run_index`)만 낸다 — 점유가 색인 단계의 첫 쓰기다(리뷰 M2)
    assert _callers("_claim_stmt") == {"_claim_embedding"}
    assert _callers("_claim_embedding") == {"_run_index"}
    # 출발 상태는 브로커 메시지에서 온다 — 목록 밖(진행 상태 등)은 chunking으로 본다. 남의
    # parsing·embedding에서는 어떤 메시지로도 점유하지 않는다
    assert ingestion.CLAIM_FROM_STATUSES == ("chunking", "ready", "failed", "pending")
    for start in ("parsing", "embedding", "nonsense", None):
        assert ingestion._index_kwargs_of({"claim_from": start})["claim_from"] == "chunking"
    for start in ingestion.CLAIM_FROM_STATUSES:
        assert ingestion._index_kwargs_of({"claim_from": start})["claim_from"] == start
    # 미룸 토큰은 시간대가 있는 ISO 시각만 받는다
    token = datetime(2026, 9, 17, 0, 0, 5, tzinfo=UTC)
    assert ingestion._index_kwargs_of({"deferred_at": token.isoformat()})["deferred_at"] == token
    for raw in ("2026-09-17T00:00:05", "어제", 5):
        assert ingestion._index_kwargs_of({"deferred_at": raw})["deferred_at"] is None

    statuses = ("pending", "parsing", "chunking", "embedding", "ready", "failed")
    for start in ingestion.CLAIM_FROM_STATUSES:
        for status in statuses:
            table = SourcesTable(status=status, error_message="임베딩 지연 — x")
            sid = table.row["id"]
            queued = [{"source_id": sid, "status": "queued"}]
            before = dict(table.row)
            stmt = ingestion._claim_stmt(sid, start, table.row["updated_at"])
            moved = _claim_on(table, queued, stmt)
            if status != start:
                assert moved == 0 and table.row == before, (start, status)
                continue
            assert moved == 1
            assert (table.row["status"], table.row["error_message"]) == ("embedding", None)
            changed = {k for k in table.row if table.row[k] != before[k]}
            assert changed == {"status", "error_message", "updated_at"}

    def claims(start: str, items: list[dict[str, Any]], token: Any = "row") -> bool:
        table = SourcesTable(status=start)
        sid = table.row["id"]
        at = table.row["updated_at"] if token == "row" else token
        items = [{**i, "source_id": i.get("source_id", sid)} for i in items]
        return _claim_on(table, items, ingestion._claim_stmt(sid, start, at)) == 1

    queued, done = [{"status": "queued"}], [{"status": "done"}]
    other = [{"status": "queued", "source_id": uuid.uuid4()}]
    # chunking(파싱이 넘긴 행)은 토큰도 항목도 필요 없다
    assert claims("chunking", [], token=None)
    # pending(미룬 신규 소스)은 미룰 때의 updated_at과 같아야 한다 — reaper가 되돌렸거나 사용자가
    # 재시도한 pending(updated_at 갱신)은 낡은 재시도가 가로채지 않는다
    assert claims("pending", [])
    assert not claims("pending", [], token=datetime(2026, 9, 17, 0, 0, 1, tzinfo=UTC))
    # ready·failed(재인덱싱)는 같은 소스의 queued 항목이 같은 문장 안에서 있어야 한다
    for start in ("ready", "failed"):
        assert claims(start, queued, token=None)
        assert not claims(start, [], token=None)
        assert not claims(start, done, token=None)
        assert not claims(start, other, token=None)

    # 실경로 — 토큰 없는 pending은 문장을 내지 않고, 점유가 0행이면 아무것도 쓰지 않는다
    _harness_with(h)
    h.source.status = "pending"
    assert await h.run_index(claim_from="pending") == {"ok": False, "error": "claim_lost"}
    assert _updates(h) == [] and h.committed == []
    h.source.status = "pending"
    entry = await h.run_entry()
    h.claim_result = False
    reads = len(h.get_calls)
    assert await h.run_index(**entry["handoff"]) == {"ok": False, "error": "claim_lost"}
    assert _updates(h)[-1] == ("embedding", False)
    assert [c["status"] for c in h.committed] == ["parsing", "chunking"]
    # 점유 전에는 parsed.md도 읽지 않는다 — 점유한 실행만 지금 세대를 읽는다
    assert len(h.get_calls) == reads and h.indexed == [] and h.closed_reindex == []


async def _drop_stale_deferral_stmt(api: dict[str, Any], h: RunHarness) -> None:
    # 점유하지 않은 실행의 쓰기(#230, 재리뷰 R-m3) — 물러나는 미룬 재시도만 부른다. 같은
    # 문장 안에서 ready·읽은 뒤 무변경·열린 항목 없음을 확인하고 warnings만 쓴다
    assert _callers("_drop_stale_deferral_stmt") == {"_drop_stale_deferral"}
    assert _callers("_drop_stale_deferral") == {"_run_index"}
    seen = datetime(2026, 10, 16, tzinfo=UTC)
    stmt = ingestion._drop_stale_deferral_stmt(uuid.uuid4(), seen, [])
    sql, params = pg(stmt), pg_params(stmt)
    assert sql.startswith("UPDATE sources SET warnings=")
    assert "status=" not in sql.split(" WHERE ")[0]  # 상태를 바꾸지 않는다
    assert params["status_1"] == "ready" and params["updated_at_1"] == seen
    assert "NOT (EXISTS (SELECT reindex_run_items.id" in sql
    assert params["status_2"] == "queued"


CHECKS = {
    ANCHORS[0]: _create_source,
    ANCHORS[1]: _upload_file_source,
    ANCHORS[2]: _delete_source,
    ANCHORS[3]: _retry_source,
    ANCHORS[4]: _guide_stmt,
    ANCHORS[5]: _guide_summary,
    ANCHORS[6]: _guide_keywords,
    ANCHORS[7]: _entry_stmt,
    ANCHORS[8]: _tick_stmt,
    ANCHORS[9]: _transition_stmt,
    ANCHORS[10]: _parse_stage_title,
    ANCHORS[11]: _parse_stage_parsed_object_key,
    ANCHORS[12]: _run_counts,
    ANCHORS[13]: _run_counts,
    ANCHORS[14]: _run_vector_origin,
    ANCHORS[15]: _run_vector_origin,
    ANCHORS[16]: _reap_stmt,
    ANCHORS[17]: _parse_stage_document_metadata,
    ANCHORS[18]: _parse_stage_document_metadata,
    ANCHORS[19]: _embedding_tick_stmt,
    ANCHORS[20]: _claim_stmt,
    ANCHORS[21]: _drop_stale_deferral_stmt,
}


@pytest.mark.parametrize("anchor", ANCHORS, ids=ANCHORS)
async def test_write_site_precondition(anchor: str, api, guide_llm, run_harness) -> None:
    await CHECKS[anchor](api, run_harness)


async def test_index_ready_needs_the_claimed_row(api, guide_llm, run_harness) -> None:
    """출처·개수 대입(`_run_index` 앵커)은 ready 전이와 운명을 같이한다(#244 리뷰 B1).

    savepoint 안에서 reaper가 `embedding`을 되돌렸으면(긴 로컬 임베딩) 조건부 ready가 0행이다 —
    savepoint째 되돌려 출처 컬럼과 새 청크 행(벡터)을 커밋하지 않고 물러난다.
    """
    h = run_harness
    _harness_with(h, markdown="본문 " * 50)
    entry = await h.run_entry()

    real_count = ingestion.count_tokens

    def count_tokens(text: str) -> int:
        h.source.status = "chunking"  # 교체 도중 reaper가 정체로 보고 되돌렸다
        return real_count(text)

    h.mp.setattr(ingestion, "count_tokens", count_tokens)
    assert await h.run_index(**entry["handoff"]) == {"ok": False, "error": "claim_lost"}
    assert _updates(h)[-2:] == [("embedding", True), ("ready", False)]
    assert ("db.rollback_to_savepoint", None) in h.timeline
    assert [c["status"] for c in h.committed] == ["parsing", "chunking", "embedding"]
    assert (h.source.token_count, h.source.chunk_count, h.source.embedding_model) == (0, 0, None)
    assert len(h.indexed) == 1 and h.indexed[0] and h.closed_reindex == []
