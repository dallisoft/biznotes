# 신규(#362). 필요: PostgreSQL·Valkey(PG 테스트).
"""rag_search 노드 `source_ids`의 비인가 경계 — 실제 DB의 노트북 소속·run 범위로 거른다.

벡터 검색(`search_spaces`)만 대역이다. 대역은 최악의 저장소를 흉내 낸다: 소스 목록을 받으면
노트북 필터를 무시하고 그 소스의 청크를 모두 돌려준다. 그래도 다른 노트북·run 범위 밖 청크가
나오지 않아야 한다 — 범위는 코어가 검색 전에 좁힌다. 검색 이후 경로(실패 소스 제외·제목 조회·
인용 번호)는 실경로다.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from biznotes_core.models import Notebook, Source, User
from biznotes_core.services import embedding_spaces
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.nodes.retrieval import _exec_rag_search
from biznotes_core.services.embedding_spaces import LOCAL_SPACE


async def _notebook(db: Any, owner: User, *titles: str) -> tuple[Notebook, list[Source]]:
    nb = Notebook(owner_id=owner.id, title="범위")
    db.add(nb)
    await db.flush()
    sources = [
        Source(notebook_id=nb.id, type="text", title=t, status="ready", chunk_count=1)
        for t in titles
    ]
    db.add_all(sources)
    await db.commit()
    return nb, sources


@pytest.fixture
def vector_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """소스마다 청크 하나. 소스 목록이 오면 노트북을 보지 않는다(최악의 필터)."""
    store: dict[str, Any] = {"chunks": {}, "calls": []}

    async def search(
        _db: Any, _spaces: Any, _query: str, *, notebook_id: Any, source_ids: Any, limit: int
    ):
        store["calls"].append(source_ids)
        chunks = store["chunks"]
        if source_ids is None:
            keys = [k for k, (nb, _) in chunks.items() if nb == notebook_id]
        else:
            keys = [k for k in chunks if k in set(source_ids)]
        return [
            SimpleNamespace(
                id=uuid.uuid5(uuid.NAMESPACE_OID, str(k)),
                score=0.9,
                payload={"source_id": str(k), "text": chunks[k][1]},
            )
            for k in keys[:limit]
        ]

    async def spaces(_db: Any, _nbs: Any, _ids: Any = None) -> list[Any]:
        return [LOCAL_SPACE]

    monkeypatch.setattr(embedding_spaces, "search_spaces", search)
    monkeypatch.setattr(embedding_spaces, "spaces_in_scope", spaces)
    return store


def _ctx(db: Any, nb: Notebook, source_ids: list[uuid.UUID] | None, params: dict) -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(), notebook_id=nb.id, db=db, params=params, source_ids=source_ids
    )


async def test_node_source_ids_never_widen_the_run_scope(db, user_a, user_b, vector_store):
    nb, (a1, a2, a3) = await _notebook(db, user_a, "a1", "a2", "a3")
    other_nb, (b1,) = await _notebook(db, user_b, "b1")
    for src, owner_nb in ((a1, nb), (a2, nb), (a3, nb), (b1, other_nb)):
        vector_store["chunks"][src.id] = (owner_nb.id, f"{src.title} 본문")

    async def titles(source_ids: Any, run_scope: list[uuid.UUID] | None) -> list[str]:
        node = {"id": "search", "query": "q", "source_ids": source_ids}
        out = await _exec_rag_search(_ctx(db, nb, run_scope, {}), node, {})
        return sorted(c["source_title"] for c in out["citations"])

    run_scope = [a1.id, a2.id]
    # 부분집합이면 그 소스의 청크만
    assert await titles([str(a2.id)], run_scope) == ["a2"]
    # run 범위 밖(a3)·다른 노트북(b1)·형식 오류는 빠진다
    assert await titles([str(a2.id), str(a3.id), str(b1.id), "x"], run_scope) == ["a2"]
    # run 범위가 노트북 전체여도 다른 노트북 소스는 빠진다
    assert await titles([str(a3.id), str(b1.id)], None) == ["a3"]
    # 교집합이 비면 검색하지 않는다
    calls = len(vector_store["calls"])
    assert await titles([str(b1.id)], run_scope) == []
    assert await titles([str(b1.id)], None) == []
    assert len(vector_store["calls"]) == calls
    # source_ids가 없으면 지금과 같다(run 범위·노트북 전체)
    assert await titles(None, run_scope) == ["a1", "a2"]
    assert await titles(None, None) == ["a1", "a2", "a3"]


async def test_loop_item_template_scopes_each_search(db, user_a, user_b, vector_store):
    nb, (a1, a2) = await _notebook(db, user_a, "a1", "a2")
    other_nb, (b1,) = await _notebook(db, user_b, "b1")
    for src, owner_nb in ((a1, nb), (a2, nb), (b1, other_nb)):
        vector_store["chunks"][src.id] = (owner_nb.id, f"{src.title} 본문")

    # 루프 본문이 받는 항목(params.item) — 개요가 정한 장의 소스
    item = {"title": "1장", "source_ids": [str(a1.id), str(b1.id)]}
    ctx = _ctx(db, nb, None, {"item": item})
    node = {
        "id": "search",
        "query": "{params.item.title}",
        "source_ids": "{params.item.source_ids}",
    }
    out = await _exec_rag_search(ctx, node, {"params": ctx.params})
    assert [c["source_title"] for c in out["citations"]] == ["a1"]
    assert vector_store["calls"][-1] == [a1.id]
