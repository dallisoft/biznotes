"""소스 범위의 세 상태와 `failed` 소스 제외 (#8·#13).

`None`은 노트북 전체, `[]`는 선택한 소스 없음, 목록은 그 소스만이다. DB 없이 SQL 조건과 검색
호출 인자를 확인한다. 벡터 검색의 소스 필터 자체는 `test_vectorstore_pg.py`가 실제
PostgreSQL로 본다.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from biznotes_core.models import Agent
from biznotes_core.services import (
    agent_runs,
    embedding_spaces,
    rag_chat,
    settings_store,
    vectorstore,
)
from biznotes_core.services.embedding_spaces import LOCAL_SPACE, spaces_in_scope


def _sql(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


class FakeDB:
    """execute()에 넘어온 문장을 기록하고, 정해 둔 행을 차례로 돌려준다."""

    def __init__(self, *results: list[Any]) -> None:
        self.results = list(results)
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> Any:
        self.statements.append(stmt)
        rows = self.results.pop(0) if self.results else []
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: rows),
            tuples=lambda: SimpleNamespace(all=lambda: rows),
        )


def _point(source_id: uuid.UUID, chunk: str, score: float = 0.9) -> Any:
    return SimpleNamespace(
        id=chunk, score=score, payload={"source_id": str(source_id), "text": f"본문 {chunk}"}
    )


@pytest.fixture
def retrieval(monkeypatch):
    """retrieve_context가 부르는 설정·공간·검색을 가짜로 두고 받은 인자를 기록한다."""
    seen: dict[str, Any] = {}

    async def get_setting(db, key):
        return {}

    async def fake_spaces(db, notebook_ids, source_ids=None):
        seen["spaces_source_ids"] = source_ids
        return [LOCAL_SPACE]

    async def fake_search(db, spaces, query, *, notebook_id=None, source_ids=None, limit=8, **kw):
        seen["search_source_ids"] = source_ids
        return seen.get("points", [])

    monkeypatch.setattr(settings_store, "get_setting", get_setting)
    monkeypatch.setattr(embedding_spaces, "spaces_in_scope", fake_spaces)
    monkeypatch.setattr(embedding_spaces, "search_spaces", fake_search)
    return seen


# ── retrieve_context ────────────────────────────────────────────────────


async def test_empty_selection_returns_insufficient_without_searching(retrieval):
    db = FakeDB()

    result = await rag_chat.retrieve_context(db, uuid.uuid4(), "q", source_ids=[])  # type: ignore[arg-type]

    assert result.citations == [] and result.insufficient_context is True
    assert db.statements == [] and "search_source_ids" not in retrieval


async def test_no_failed_sources_keeps_the_scope_unchanged(retrieval):
    sid = uuid.uuid4()
    retrieval["points"] = [_point(sid, "c1")]
    db = FakeDB([], [(sid, "소스")])

    result = await rag_chat.retrieve_context(db, uuid.uuid4(), "q", source_ids=None)  # type: ignore[arg-type]

    failed_sql = _sql(db.statements[0])
    assert "sources.status = " in failed_sql and "sources.id IN" not in failed_sql
    # 흔한 경우(failed 없음)는 이전과 같은 호출 — 필터 없이 노트북 전체
    assert retrieval["search_source_ids"] is None
    assert retrieval["spaces_source_ids"] is None
    assert [(c.n, c.chunk_id, c.source_title) for c in result.citations] == [(1, "c1", "소스")]


async def test_failed_source_narrows_the_whole_notebook_scope(retrieval):
    failed, ready, reindexing = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db = FakeDB([failed], [ready, reindexing])

    await rag_chat.retrieve_context(db, uuid.uuid4(), "q", source_ids=None)  # type: ignore[arg-type]

    assert db.statements[0].compile().params["status_1"] == "failed"
    rest_sql = _sql(db.statements[1])
    assert "sources.status != " in rest_sql and "sources.id IN" not in rest_sql
    # failed 소스의 잔존 청크가 섞이지 않도록 명시 목록으로 검색한다
    assert retrieval["search_source_ids"] == [ready, reindexing]
    assert retrieval["spaces_source_ids"] == [ready, reindexing]


async def test_failed_source_is_removed_from_a_selection(retrieval):
    picked, failed = uuid.uuid4(), uuid.uuid4()
    db = FakeDB([failed])

    await rag_chat.retrieve_context(db, uuid.uuid4(), "q", source_ids=[picked, failed])  # type: ignore[arg-type]

    assert "sources.id IN" in _sql(db.statements[0]) and len(db.statements) == 1
    assert retrieval["search_source_ids"] == [picked]


async def test_selection_without_failed_sources_is_kept_as_given(retrieval):
    picked = [uuid.uuid4(), uuid.uuid4()]
    db = FakeDB([])

    await rag_chat.retrieve_context(db, uuid.uuid4(), "q", source_ids=picked)  # type: ignore[arg-type]

    assert retrieval["search_source_ids"] == picked


@pytest.mark.parametrize("source_ids", [None, "failed-only"])
async def test_scope_with_only_failed_sources_does_not_search(retrieval, source_ids):
    failed = uuid.uuid4()
    selection = [failed] if source_ids else None
    db = FakeDB([failed], [])

    result = await rag_chat.retrieve_context(db, uuid.uuid4(), "q", source_ids=selection)  # type: ignore[arg-type]

    assert result.citations == [] and result.insufficient_context is True
    assert "search_source_ids" not in retrieval


# ── 하위 검색 계층 ──────────────────────────────────────────────────────


async def test_hybrid_search_empty_selection_asks_nothing():
    """빈 선택(`[]`)은 DB에 묻지도 않는다 — 세션이 없어도 빈 결과다. `None`과 목록의 SQL 필터는
    `test_vectorstore_pg.py`가 실제 행으로 본다."""
    sparse = SimpleNamespace(indices=[1], values=[1.0])
    found = await vectorstore.hybrid_search(
        None, uuid.uuid4(), [0.1], sparse, source_ids=[], space="s", local_space="s"
    )
    assert found == []


async def test_spaces_in_scope_keeps_empty_selection_empty(monkeypatch):
    async def active(db):
        return LOCAL_SPACE

    monkeypatch.setattr(embedding_spaces, "active_space", active)
    db = FakeDB(["gateway:x:y"])

    spaces = await spaces_in_scope(db, [uuid.uuid4()], [])  # type: ignore[arg-type]

    assert "sources.id IN" in _sql(db.statements[0])
    assert spaces[0] == LOCAL_SPACE


async def test_source_load_reads_nothing_for_an_empty_selection():
    from biznotes_core.services.agents.context import RunContext
    from biznotes_core.services.agents.nodes.retrieval import _exec_source_load

    db = FakeDB([])
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, source_ids=[])  # type: ignore[arg-type]

    out = await _exec_source_load(ctx, {}, {})

    # [] 선택은 빈 IN 조건 — 노트북 전체로 넓어지지 않는다
    assert "sources.id IN" in _sql(db.statements[0])
    assert out["sources"] == [] and out["context"] == ""


# ── stage_agent_run (#13) ──────────────────────────────────────────────


PICKED = "11111111-1111-1111-1111-111111111111"


@pytest.mark.parametrize(
    ("source_ids", "stored"), [(None, None), ([], []), ([uuid.UUID(PICKED)], [PICKED])]
)
async def test_stage_agent_run_keeps_the_three_scope_states(source_ids, stored):
    added: list[Any] = []

    class DB:
        def add(self, obj: Any) -> None:
            added.append(obj)

        async def flush(self) -> None:
            added[-1].id = uuid.uuid4()

    uow = SimpleNamespace(after_commit=lambda fn: None)
    agent = Agent(id=uuid.uuid4(), params_schema=None, source_kind=None)

    run = await agent_runs.stage_agent_run(
        DB(),  # type: ignore[arg-type]
        uow,  # type: ignore[arg-type]
        agent=agent,
        notebook_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        user_role="user",
        params={},
        source_ids=source_ids,
        enforce_limit=False,
    )

    assert run.input_source_ids == stored
