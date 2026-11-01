"""가이드 캐시 쓰기의 제3자 writer 차단(§5.1)과 재인제스트 뒤 stale 캐시(§5.2).

`generate_source_guide`·`retry_source`·`get_source_guide`는 실경로다. DB 세션(인메모리 행에
컴파일된 UPDATE를 평가)과 LLM 게이트웨이만 스텁이다.
"""

import ast
import pathlib
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from conftest import SourcesTable, TableSession, pg

from biznotes_core.api.v1 import sources
from biznotes_core.services import source_guide
from biznotes_core.services.ingestion import enqueue

GUIDE_FILE = pathlib.Path(source_guide.__file__)


class GuideSource:
    """읽기 순서를 기록하는 소스 객체(generate_source_guide가 읽는 필드만)."""

    def __init__(self, table: SourcesTable, log: list[str]):
        self._log = log
        for key, value in table.row.items():
            setattr(self, "_updated_at" if key == "updated_at" else key, value)

    @property
    def updated_at(self) -> Any:
        self._log.append("read updated_at")
        return self._updated_at

    @updated_at.setter
    def updated_at(self, value: Any) -> None:
        self._updated_at = value


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """LLM 호출 중에 끼워 넣을 동작(hook)과 호출 순서를 기록한다."""
    state: dict[str, Any] = {
        "log": [],
        "hook": None,
        "reply": '{"summary": "S_NEW", "keywords": ["새"]}',
    }

    async def fake_stream(db: Any, model: Any, messages: Any, result: Any, **kw: Any) -> Any:
        state["log"].append("llm")
        if state["hook"] is not None:
            await state["hook"]()

        async def gen() -> Any:
            yield state["reply"]

        return gen()

    monkeypatch.setattr(source_guide, "complete_stream_with_fallback", fake_stream)
    return state


@pytest.fixture
def retry_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    env: dict[str, Any] = {"published": [], "before_uow": None}

    async def permitted(db: Any, notebook_id: Any, user: Any, need_write: bool) -> None:
        if env["before_uow"] is not None:
            hook, env["before_uow"] = env["before_uow"], None
            await hook()

    monkeypatch.setattr(sources, "_notebook_with_permission", permitted)
    monkeypatch.setattr(
        enqueue, "_publish", lambda sid, tid, queue=None: env["published"].append((sid, queue))
    )
    return env


async def _guide(table: SourcesTable, log: list[str] | None = None) -> tuple[Any, TableSession]:
    db = TableSession(table)
    src = GuideSource(table, log if log is not None else [])
    out = await source_guide.generate_source_guide(db, src)  # type: ignore[arg-type]
    return out, db


async def _retry(table: SourcesTable) -> TableSession:
    db = TableSession(table)
    await sources.retry_source(table.row["id"], user=object(), db=db)  # type: ignore[arg-type]
    return db


# ---------------------------------------------------------------------------
# §5.1 조건부 캐시 쓰기
# ---------------------------------------------------------------------------


async def test_ready_row_is_cached(llm) -> None:
    table = SourcesTable(status="ready")
    seen_at = table.row["updated_at"]
    out, db = await _guide(table)

    [stmt] = db.updates
    sql = pg(stmt)
    assert sql.startswith("UPDATE sources SET summary=%(summary)s, keywords=%(keywords)s")
    assert "sources.status = %(status_1)s" in sql
    assert "sources.updated_at = %(updated_at_1)s" in sql
    params = stmt.compile().params
    assert params["status_1"] == "ready" and params["updated_at_1"] == seen_at
    assert len(db.refreshed) == 1 and db.expunged == []
    assert table.row["summary"] == "S_NEW" and table.row["keywords"] == ["새"]
    assert out.summary == "S_NEW"


async def test_reingesting_row_is_not_cached(llm) -> None:
    table = SourcesTable(status="parsing")
    with structlog.testing.capture_logs() as logs:
        out, db = await _guide(table)
    assert len(db.updates) == 1  # 재시도하지 않는다
    assert db.refreshed == [] and db.expunged == [out]
    assert table.row["summary"] is None
    assert (out.summary, out.keywords) == ("S_NEW", ["새"])  # 응답에만 싣는다
    assert [e["event"] for e in logs] == ["source guide cache skipped — 소스가 재인제스트 중"]


async def test_seen_at_is_read_before_llm_call(llm) -> None:
    table = SourcesTable(status="ready")
    await _guide(table, llm["log"])
    assert llm["log"].index("read updated_at") < llm["log"].index("llm")


async def test_no_chunks_returns_without_update(llm) -> None:
    table = SourcesTable(status="ready")
    db = TableSession(table, chunks=[])
    src = GuideSource(table, [])
    out = await source_guide.generate_source_guide(db, src)  # type: ignore[arg-type]
    assert out is src and db.updates == [] and llm["log"] == []


def test_orm_assignment_only_in_skip_branch() -> None:
    """`source.summary/keywords` 대입은 rowcount 0 분기(expunge 뒤)에만 있고 그 뒤 커밋이 없다."""
    tree = ast.parse(GUIDE_FILE.read_text(encoding="utf-8"))
    func = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "generate_source_guide"
    )
    guard = next(
        n for n in ast.walk(func)
        if isinstance(n, ast.If) and ast.unparse(n.test) == "res.rowcount == 1"
    )

    def targets(nodes: list[ast.stmt]) -> list[str]:
        found = []
        for node in nodes:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        for leaf in ast.walk(t):
                            if isinstance(leaf, ast.Attribute) and leaf.attr in (
                                "summary",
                                "keywords",
                            ):
                                found.append(ast.unparse(leaf))
        return found

    body_outside_guard = [n for n in func.body if n is not guard]
    assert targets(body_outside_guard) == []
    assert targets(guard.body) == []
    assert targets(guard.orelse) == ["source.summary", "source.keywords"]
    orelse = ast.unparse(guard.orelse)
    assert orelse.index("db.expunge(source)") < orelse.index("source.summary")
    assert "commit" not in orelse and "flush" not in orelse


# ---------------------------------------------------------------------------
# §5.2 경쟁 5종
# ---------------------------------------------------------------------------


def _set_clause(stmt: Any) -> str:
    return pg(stmt).split(" SET ", 1)[1].split(" WHERE ", 1)[0]


async def test_race1_retry_clears_cache_and_guide_regenerates(llm, retry_env, monkeypatch):
    table = SourcesTable(status="ready")
    await _guide(table)
    assert table.row["summary"] == "S_NEW"

    db = await _retry(table)
    [stmt] = db.updates
    columns = [part.split("=")[0] for part in _set_clause(stmt).split(", ")]
    assert columns == [
        "status",
        "error_message",
        "summary",
        "keywords",
        "document_title",  # 옛 본문의 문서 제목·경고도 함께 지운다(HWP-02·03)
        "warnings",
        "updated_at",
    ]
    assert db.commits == 1  # 상태 전이와 캐시 무효화가 한 커밋
    assert (table.row["status"], table.row["summary"], table.row["keywords"]) == (
        "pending",
        None,
        None,
    )
    assert retry_env["published"] == [(str(table.row["id"]), None)]  # 플래그 off → 큐 인자 없음

    # 재인제스트가 끝나 ready가 되면 가이드 라우트가 다시 생성한다
    table.write(status="ready")
    regenerated: list[Any] = []
    real = source_guide.generate_source_guide

    async def spy(db: Any, source: Any) -> Any:
        regenerated.append(source.id)
        return await real(db, source)

    monkeypatch.setattr(source_guide, "generate_source_guide", spy)
    llm["reply"] = '{"summary": "S_REGEN", "keywords": []}'
    # 사용량 원장(#366)이 요청 사용자에게 귀속하므로 id가 있는 사용자를 넘긴다
    reader = SimpleNamespace(id=uuid.uuid4())
    out = await sources.get_source_guide(table.row["id"], user=reader, db=TableSession(table))  # type: ignore[arg-type]
    assert regenerated == [table.row["id"]]
    assert out.summary == "S_REGEN" and table.row["summary"] == "S_REGEN"


async def test_race2_guide_commits_before_retry_reads(llm, retry_env) -> None:
    table = SourcesTable(status="ready")
    out, db = await _guide(table)
    assert db.expunged == [] and table.row["summary"] == "S_NEW"
    await _retry(table)
    assert table.row["summary"] is None and table.row["status"] == "pending"


async def test_race3_retry_commits_before_guide(llm, retry_env) -> None:
    table = SourcesTable(status="ready")

    async def retry_during_llm() -> None:
        await _retry(table)

    llm["hook"] = retry_during_llm
    out, db = await _guide(table)
    assert db.expunged == [out]
    assert table.row["summary"] is None and table.row["status"] == "pending"


async def test_race4_reingest_finishes_before_guide_commit(llm, retry_env) -> None:
    table = SourcesTable(status="ready", summary=None)

    async def retry_and_reingest() -> None:
        await _retry(table)
        table.write(status="parsing")
        table.write(status="ready")  # 새 본문으로 다시 ready

    llm["hook"] = retry_and_reingest
    llm["reply"] = '{"summary": "S_OLD", "keywords": ["옛"]}'
    out, db = await _guide(table)

    # status는 ready로 통과하지만 updated_at이 seen_at과 달라 캐시하지 않는다
    assert table.row["status"] == "ready"
    assert db.expunged == [out]
    assert table.row["summary"] is None


async def test_race5_retry_reads_first_then_guide_commits(llm, retry_env) -> None:
    table = SourcesTable(status="ready", summary=None, keywords=None)
    async def guide_between_read_and_write() -> None:
        # retry_source는 이미 행을 읽었다(summary=None) — 그 뒤 가이드가 커밋한다
        llm["reply"] = '{"summary": "S_OLD", "keywords": ["k"]}'
        await _guide(table)
        assert table.row["summary"] == "S_OLD"  # 그 시점엔 정당한 커밋

    retry_env["before_uow"] = guide_between_read_and_write
    db = await _retry(table)

    [stmt] = db.updates
    clause = _set_clause(stmt)
    # 로드 시 summary가 None이었어도 명시 UPDATE는 두 컬럼을 SET 절에 넣는다
    assert "summary=%(summary)s" in clause and "keywords=%(keywords)s" in clause
    assert (table.row["status"], table.row["summary"], table.row["keywords"]) == (
        "pending",
        None,
        None,
    )


def test_retry_uses_explicit_update_not_orm_assignment() -> None:
    import inspect
    import re

    src = inspect.getsource(sources)
    orm = r"^\s*source\.(status|error_message|summary|keywords|document_title|warnings)\s*[,=]"
    assert not re.search(orm, src, re.M)
    explicit = (
        r'\.values\(\s*status="pending",\s*error_message=None,\s*summary=None,\s*keywords=None,'
        r"\s*document_title=None,\s*warnings=None,?\s*\)"
    )
    assert len(re.findall(explicit, src)) == 1


async def test_retry_does_not_overwrite_row_that_started_ingesting(retry_env) -> None:
    """P1R-05 — 409 판정 뒤 인제스트가 시작되면 UPDATE가 0행이고 발행도 없다."""
    from fastapi import HTTPException

    table = SourcesTable(status="failed", error_message="이전 실패")

    async def ingest_started() -> None:
        table.write(status="parsing")

    retry_env["before_uow"] = ingest_started
    db = TableSession(table)
    with pytest.raises(HTTPException) as exc:
        await sources.retry_source(table.row["id"], user=object(), db=db)  # type: ignore[arg-type]
    assert exc.value.status_code == 409
    assert table.row["status"] == "parsing" and table.row["error_message"] == "이전 실패"
    assert db.rollbacks == 1 and db.commits == 0
    assert retry_env["published"] == []
    assert "sources.status IN" in pg(db.updates[0])


async def test_retry_rejects_in_flight_rows(retry_env) -> None:
    from fastapi import HTTPException

    table = SourcesTable(status="parsing")
    db = TableSession(table)
    with pytest.raises(HTTPException) as exc:
        await sources.retry_source(table.row["id"], user=object(), db=db)  # type: ignore[arg-type]
    assert exc.value.status_code == 409
    assert db.updates == [] and table.row["status"] == "parsing"

