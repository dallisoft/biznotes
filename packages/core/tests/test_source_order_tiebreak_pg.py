# 신규(#391). 필요: PostgreSQL·Valkey(PG 테스트).
"""소스 정렬의 두 번째 키 `Source.id` — `created_at`이 같아도 순서가 힙 위치에 달리지 않는다.

한 트랜잭션에서 넣은 소스는 `created_at`(server_default `now()` = 트랜잭션 시각)이 같다. 소스를
고치면 새 튜플이 힙 뒤쪽에 놓여 `created_at`만으로 정렬한 결과가 바뀌었다. 소스 목록을 읽는
경로(source_digest·source_load 노드, 개요, 목록 API, 내보내기)가 모두 `(created_at, id)` 순이고
UPDATE 앞뒤로 같음을 본다.
"""

import uuid
from typing import Any

from sqlalchemy import text

from biznotes_core.api.v1.sources import list_sources
from biznotes_core.models import Notebook, Source, User
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.nodes.digest import _exec_source_digest
from biznotes_core.services.agents.nodes.retrieval import _exec_source_load
from biznotes_core.services.overview import _ready_sources

# id 순서가 넣는 순서와 다르다 — 힙 순서는 [2, 3, 1], 첫 행을 고치면 [3, 1, 2]다
IDS = [uuid.UUID(int=n) for n in (1, 2, 3)]
INSERT_ORDER = [IDS[1], IDS[2], IDS[0]]


async def _tied_sources(db: Any, owner: User) -> Notebook:
    nb = Notebook(owner_id=owner.id, title="정렬")
    db.add(nb)
    await db.flush()
    # 청크가 없는 ready 소스 — 요약 생성(LLM)·원문 읽기(스토리지) 없이 순서만 본다
    db.add_all(
        Source(id=sid, notebook_id=nb.id, type="text", title=f"자료 {sid.int}", status="ready")
        for sid in INSERT_ORDER
    )
    await db.commit()  # 한 트랜잭션 — created_at이 같다
    return nb


async def _heap_order(db: Any, nb: Notebook) -> list[uuid.UUID]:
    rows = await db.execute(
        text("SELECT id FROM sources WHERE notebook_id = :nb ORDER BY ctid"), {"nb": nb.id}
    )
    return [r for (r,) in rows.all()]


async def _orders(db: Any, nb: Notebook, owner: User) -> dict[str, list[uuid.UUID]]:
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=nb.id, db=db, params={}, source_ids=None)
    digest = await _exec_source_digest(ctx, {"id": "digest"}, {})
    loaded = await _exec_source_load(ctx, {"id": "load"}, {})
    listed = await list_sources(nb.id, user=owner, db=db)
    return {
        "source_digest": [uuid.UUID(s["id"]) for s in digest["sources"]],
        # 청크가 없어 모두 no_content로 빠진다 — 빠진 순서가 읽은 순서다
        "source_load": [uuid.UUID(d["id"]) for d in loaded["dropped"]],
        "overview": [s.id for s in await _ready_sources(db, nb.id)],
        "list_api": [s.id for s in listed],
    }


async def test_tied_created_at_orders_by_id_before_and_after_update(db, user_a):
    nb = await _tied_sources(db, user_a)
    created = await db.execute(
        text("SELECT count(DISTINCT created_at) FROM sources WHERE notebook_id = :nb"),
        {"nb": nb.id},
    )
    assert created.scalar_one() == 1  # 전제: 셋 다 같은 시각
    heap_before = await _heap_order(db, nb)
    assert heap_before == INSERT_ORDER

    before = await _orders(db, nb, user_a)
    assert before == {path: IDS for path in before}

    await db.execute(
        text("UPDATE sources SET title = '고친 자료' WHERE id = :id"), {"id": INSERT_ORDER[0]}
    )
    await db.commit()
    heap_after = await _heap_order(db, nb)
    assert heap_after != heap_before  # 전제: 고친 행의 힙 위치가 실제로 바뀌었다
    assert heap_after != IDS

    after = await _orders(db, nb, user_a)
    assert after == before
