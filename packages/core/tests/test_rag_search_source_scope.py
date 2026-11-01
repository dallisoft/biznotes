"""rag_search 노드의 `source_ids` 범위 좁히기(#362) — 해석·교집합 규칙.

검색(`retrieve_context`)과 노트북 소속 조회는 대역이다. 실제 DB에서 다른 노트북·run 범위 밖 소스가
빠지는지는 `tests/test_rag_search_source_scope_pg.py`가 본다.
"""

import uuid
from typing import Any

import pytest
from structlog.testing import capture_logs

from biznotes_core.services import rag_chat
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.graph import _LOG_INPUT_KEYS
from biznotes_core.services.agents.nodes import retrieval
from biznotes_core.services.agents.nodes.retrieval import _exec_rag_search
from biznotes_core.services.rag_chat import RetrievalResult

A1, A2, A3 = (uuid.uuid4() for _ in range(3))
OTHER = uuid.uuid4()  # 다른 노트북 소스
NOTEBOOK_SOURCES = {A1, A2, A3}


class _Rows:
    def __init__(self, ids: set[uuid.UUID]) -> None:
        self.ids = ids

    def scalars(self) -> "_Rows":
        return self

    def all(self) -> list[uuid.UUID]:
        return list(self.ids)


class _NotebookDb:
    """`Source.id IN (…) AND notebook_id = …` 조회를 흉내 낸다 — 이 노트북 소스만 돌려준다."""

    def __init__(self) -> None:
        self.queries = 0

    async def execute(self, stmt: Any) -> _Rows:
        self.queries += 1
        params = stmt.compile().params
        wanted = {v for vals in params.values() if isinstance(vals, list) for v in vals}
        return _Rows(wanted & NOTEBOOK_SOURCES)


@pytest.fixture
def searched(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    seen: list[Any] = []

    async def fake(_db: Any, _nb: Any, _q: str, source_ids: Any, top_k: Any = None) -> Any:
        seen.append(source_ids)
        return RetrievalResult(citations=[], insufficient_context=True)

    monkeypatch.setattr(rag_chat, "retrieve_context", fake)
    return seen


def _ctx(db: Any, source_ids: list[uuid.UUID] | None, params: dict | None = None) -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params=params or {},
        source_ids=source_ids,
    )


async def _scope(node: dict, run_scope: list[uuid.UUID] | None, searched: list[Any], **kw):
    db = _NotebookDb()
    state = kw.get("state", {})
    await _exec_rag_search(_ctx(db, run_scope), {"id": "s", "query": "q", **node}, state)
    return searched[-1], db.queries


async def test_no_source_ids_keeps_the_run_scope(searched):
    assert await _scope({}, [A1, A2], searched) == ([A1, A2], 0)
    assert await _scope({}, None, searched) == (None, 0)
    assert await _scope({"source_ids": None}, [A1], searched) == ([A1], 0)


async def test_subset_of_the_run_scope_narrows_the_search(searched):
    assert await _scope({"source_ids": [str(A2)]}, [A1, A2], searched) == ([A2], 1)


async def test_ids_outside_the_run_scope_or_notebook_are_dropped(searched):
    node = {"source_ids": [str(A2), str(A3), str(OTHER)]}
    # A3는 run 범위 밖, OTHER는 다른 노트북 — run 범위가 노트북 전체여도 OTHER는 빠진다
    assert await _scope(node, [A1, A2], searched) == ([A2], 1)
    assert await _scope(node, None, searched) == ([A2, A3], 1)


async def test_empty_intersection_does_not_search_wider(searched):
    # 교집합이 비면 [] — retrieve_context는 []를 검색하지 않는다(#13). DB도 묻지 않는다
    assert await _scope({"source_ids": [str(A3)]}, [A1, A2], searched) == ([], 0)
    assert await _scope({"source_ids": []}, [A1, A2], searched) == ([], 0)
    # 노트북 소속이 아니면 run 범위가 노트북 전체여도 빈 범위다
    assert await _scope({"source_ids": [str(OTHER)]}, None, searched) == ([], 1)


async def test_malformed_ids_are_ignored_and_logged(searched):
    node = {"source_ids": ["nope", str(A1), 7, str(A1), {"id": str(A2)}]}
    with capture_logs() as logs:
        assert await _scope(node, None, searched) == ([A1], 1)
    warned = [e for e in logs if e["event"] == "rag_search source_ids 일부를 무시"]
    assert warned and warned[0]["invalid"] == 3 and warned[0]["node_id"] == "s"
    # 전부 틀리면 빈 범위다 — 범위를 넓히지 않는다
    assert await _scope({"source_ids": ["all"]}, None, searched) == ([], 0)


@pytest.mark.parametrize(
    ("template", "state", "expected"),
    [
        # 경로 하나 — 값(목록)을 그대로 쓴다
        ("{params.item.source_ids}", {"params": {"item": {"source_ids": [str(A2)]}}}, [A2]),
        # 원소마다 템플릿
        (["{params.first}", str(A3)], {"params": {"first": str(A1)}}, [A1, A3]),
        # 렌더 결과가 JSON 배열
        ("{outline.ids} ", {"outline": {"ids": f'["{A1}"]'}}, [A1]),
        # JSON이지만 id가 아니면 빈 범위다
        ('{"x": 1}', {}, []),
        # id 하나
        ("{outline.one}", {"outline": {"one": str(A2)}}, [A2]),
    ],
)
async def test_templates_resolve_against_state(searched, template, state, expected):
    got, _ = await _scope({"source_ids": template}, None, searched, state=state)
    assert got == expected


async def test_unresolved_template_keeps_the_run_scope(searched):
    # 항목에 source_ids가 없으면 좁히지 않는다 — run 범위 그대로(넓히지 않는다)
    assert await _scope({"source_ids": "{params.item.source_ids}"}, [A1], searched) == ([A1], 0)
    assert await _scope({"source_ids": "{params.missing}"}, None, searched) == (None, 0)


async def test_too_many_ids_are_capped(searched, monkeypatch):
    monkeypatch.setattr(retrieval, "_RAG_SEARCH_SOURCE_IDS_LIMIT", 2)
    node = {"source_ids": [str(A1), str(A2), str(A3)]}
    assert await _scope(node, None, searched) == ([A1, A2], 1)


def test_source_ids_definition_is_logged_as_node_input():
    assert "source_ids" in _LOG_INPUT_KEYS
