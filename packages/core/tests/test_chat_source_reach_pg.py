# 신규(#447). 필요: PostgreSQL·Valkey(PG 테스트).
"""채팅 도구 `source_list`·`source_load`·`rag_search`의 새 인자(#447) — 실제 DB 경계.

실제 DB의 노트북 소속·run 범위(선택 소스)·ready 상태로 거른다. 원문은 청크 재조립 폴백으로
읽는다(스토리지 없음). 벡터 검색(`search_spaces`)만 대역이고, 대역은 소스 목록을 받으면 노트북
필터를 무시하는 최악의 저장소다 — 다른 노트북 청크가 나오지 않는 것은 코어가 검색 전에 범위를
좁히기 때문이다.
"""

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from biznotes_core.models import Notebook, Source, SourceChunk, SourceDigest, User
from biznotes_core.services import embedding_spaces
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.nodes import llm as llm_node
from biznotes_core.services.embedding_spaces import LOCAL_SPACE


async def _notebook(
    db: Any, owner: User, *titles: str, status: str = "ready"
) -> tuple[Notebook, list[Source]]:
    nb = Notebook(owner_id=owner.id, title="범위")
    db.add(nb)
    await db.flush()
    sources = [
        Source(notebook_id=nb.id, type="text", title=t, status=status, chunk_count=1)
        for t in titles
    ]
    db.add_all(sources)
    await db.flush()
    db.add_all(
        SourceChunk(source_id=s.id, seq=0, text=f"{s.title} 본문")
        for s in sources
    )
    await db.commit()
    return nb, sources


def _ctx(db: Any, nb: Notebook, source_ids: list[uuid.UUID] | None) -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(), notebook_id=nb.id, db=db, params={}, source_ids=source_ids
    )


def _parse_list(out: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    head, _, rest = out.partition("\n")
    if not rest:
        return json.loads(head), []
    body = rest.split("\n", 1)[1].rsplit("\n", 1)[0]
    return json.loads(head), [json.loads(line) for line in body.split("\n")]


async def _list(ctx: RunContext, **args: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return _parse_list(await llm_node._dispatch_builtin_tool(ctx, "source_list", args))


async def _load(ctx: RunContext, **args: Any) -> dict[str, Any]:
    return json.loads(await llm_node._dispatch_builtin_tool(ctx, "source_load", args))


async def test_source_list_and_load_reach_the_500th_source(db, user_a):
    nb, _ = await _notebook(db, user_a, *(f"자료 {i:03d}" for i in range(1, 501)))
    ctx = _ctx(db, nb, None)

    seen: list[dict[str, Any]] = []
    offset: int | None = 0
    while offset is not None:
        meta, items = await _list(ctx, offset=offset, limit=200)
        assert meta["total"] == 500
        seen.extend(items)
        offset = meta["next_offset"]

    assert len({i["id"] for i in seen}) == 500
    last = seen[-1]
    out = await _load(ctx, source_ids=[last["id"]])
    assert [s["id"] for s in out["sources"]] == [last["id"]]
    assert f"{last['title']} 본문" in out["context"]


async def test_source_list_summary_and_digest_come_from_the_db(db, user_a):
    nb, (a1, a2) = await _notebook(db, user_a, "a1", "a2")
    a1.summary = "가이드 요약"
    db.add(
        SourceDigest(
            source_id=a2.id,
            content_hash="0" * 64,
            digest="다이제스트 요약 " * 100,
            token_count=10,
            prompt_version="test",
        )
    )
    await db.commit()

    _, items = await _list(_ctx(db, nb, None))

    # 한 트랜잭션에서 넣은 소스는 created_at이 같아 id 순이다 — 제목으로 짝짓는다
    summaries = {i["title"]: i["summary"] for i in items}
    assert summaries["a1"] == "가이드 요약"
    assert summaries["a2"].startswith("다이제스트 요약")


async def test_source_list_never_shows_other_notebooks_or_unselected_sources(
    db, user_a, user_b
):
    nb, (a1, a2, a3) = await _notebook(db, user_a, "a1", "a2", "a3")
    other_nb, (b1,) = await _notebook(db, user_b, "b1")
    db.add(Source(notebook_id=nb.id, type="text", title="대기", status="pending"))
    await db.commit()

    _, items = await _list(_ctx(db, nb, None))
    assert sorted(i["title"] for i in items) == ["a1", "a2", "a3"]  # pending 소스는 빠진다
    # 선택 범위에 다른 노트북 id가 섞여도 이 노트북의 선택 소스만 보인다
    _, items = await _list(_ctx(db, nb, [a2.id, b1.id]))
    assert [i["title"] for i in items] == ["a2"]
    _, items = await _list(_ctx(db, other_nb, None))
    assert [i["title"] for i in items] == ["b1"]


async def test_source_load_source_ids_never_widen_the_run_scope(db, user_a, user_b):
    nb, (a1, a2, a3) = await _notebook(db, user_a, "a1", "a2", "a3")
    _, (b1,) = await _notebook(db, user_b, "b1")
    selection = [a1.id, a2.id]

    async def titles(ids: list[str], run_scope: list[uuid.UUID] | None) -> list[str]:
        out = await _load(_ctx(db, nb, run_scope), source_ids=ids)
        return [s["title"] for s in out["sources"]]

    # 선택 범위 밖(a3)·다른 노트북(b1)·형식 오류는 존재 여부와 상관없이 빠진다
    assert await titles([str(a2.id), str(a3.id), str(b1.id), "x"], selection) == ["a2"]
    assert await titles([str(b1.id)], selection) == []
    # run 범위가 노트북 전체여도 다른 노트북 소스는 빠진다
    assert await titles([str(a3.id), str(b1.id)], None) == ["a3"]
    assert await titles([str(b1.id)], None) == []


async def test_source_load_offset_pages_through_the_run_scope(db, user_a):
    nb, sources = await _notebook(db, user_a, *(f"자료 {i:02d}" for i in range(1, 13)))
    ctx = _ctx(db, nb, None)

    first = await _load(ctx)
    second = await _load(ctx, offset=first["next_offset"])
    third = await _load(ctx, offset=second["next_offset"])

    read = [s["title"] for page in (first, second, third) for s in page["sources"]]
    # 빠짐·겹침 없이 모두 읽는다(같은 created_at은 id 순이라 넣은 순서와 다를 수 있다)
    assert sorted(read) == sorted(s.title for s in sources) and len(set(read)) == 12
    assert (first["remaining"], second["remaining"]) == (7, 2)
    assert "next_offset" not in third


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


async def test_rag_search_tool_source_ids_never_widen_the_run_scope(
    db, user_a, user_b, vector_store
):
    nb, (a1, a2, a3) = await _notebook(db, user_a, "a1", "a2", "a3")
    other_nb, (b1,) = await _notebook(db, user_b, "b1")
    for src, owner_nb in ((a1, nb), (a2, nb), (a3, nb), (b1, other_nb)):
        vector_store["chunks"][src.id] = (owner_nb.id, f"{src.title} 본문")

    async def titles(ids: list[str] | None, run_scope: list[uuid.UUID] | None) -> list[str]:
        ctx = _ctx(db, nb, run_scope)
        args: dict[str, Any] = {"query": "q"}
        if ids is not None:
            args["source_ids"] = ids
        await llm_node._dispatch_builtin_tool(ctx, "rag_search", args)
        return sorted(e["title"] for e in ctx.tool_evidence)

    selection = [a1.id, a2.id]
    assert await titles([str(a2.id)], selection) == ["a2"]
    assert await titles([str(a2.id), str(a3.id), str(b1.id), "x"], selection) == ["a2"]
    assert await titles([str(a3.id), str(b1.id)], None) == ["a3"]
    # 교집합이 비면 검색하지 않는다
    calls = len(vector_store["calls"])
    assert await titles([str(b1.id)], selection) == []
    assert await titles([str(b1.id)], None) == []
    assert len(vector_store["calls"]) == calls
    # source_ids가 없으면 지금과 같다
    assert await titles(None, selection) == ["a1", "a2"]
