# 필요: PostgreSQL·Valkey(PG 테스트).
"""overview 입력 본문·노트북 내보내기의 DB-in-loop 제거(#164).

쿼리 수가 소스·세션 수와 무관하게 상수인지를 실제 PostgreSQL로 본다. overview는 모든 ready 소스의
본문(가이드 요약 → 지문이 맞는 digest → 첫 청크 → 제목)을 고른다(#449).
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.models import (
    Notebook,
    Source,
    SourceChunk,
    SourceDigest,
)
from biznotes_core.services import overview
from biznotes_core.services.source_digest import PROMPT_VERSION, content_hash_of


@contextmanager
def _count_queries(db: AsyncSession) -> Iterator[list[str]]:
    statements: list[str] = []
    engine = db.bind.sync_engine  # type: ignore[union-attr]

    def before(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before)


async def _notebook(db: AsyncSession, owner_id: uuid.UUID) -> Notebook:
    nb = Notebook(owner_id=owner_id, title="DB-in-loop")
    db.add(nb)
    await db.commit()
    return nb


async def _sources_with_chunks(
    db: AsyncSession, notebook_id: uuid.UUID, chunk_counts: list[int]
) -> list[Source]:
    # 한 트랜잭션의 server default now()는 모두 같다 — 시각을 벌려 (created_at, id) 순서를 고정한다
    base = datetime(2026, 1, 1, tzinfo=UTC)
    sources = [
        Source(
            notebook_id=notebook_id,
            type="file",
            title=f"s{i}",
            status="ready",
            created_at=base + timedelta(seconds=i),
        )
        for i in range(len(chunk_counts))
    ]
    db.add_all(sources)
    await db.flush()
    for i, (source, count) in enumerate(zip(sources, chunk_counts, strict=True)):
        # seq를 역순으로 넣어 삽입 순서가 아니라 seq 순서로 첫 청크를 고르는지 본다
        for seq in reversed(range(count)):
            db.add(
                SourceChunk(
                    source_id=source.id, seq=seq, text=f"[{i}:{seq}]"
                )
            )
    await db.commit()
    return await overview._ready_sources(db, notebook_id)


async def test_source_texts_picks_guide_fresh_digest_first_chunk_then_title(db, user_a):
    nb = await _notebook(db, user_a.id)
    guide, fresh, stale, old_version, chunk_only, empty = await _sources_with_chunks(
        db, nb.id, [2, 2, 2, 2, 2, 0]
    )
    guide.summary = "가이드 요약"
    current = content_hash_of(["[1:0]", "[1:1]"])
    rows = [
        (fresh, "최신 digest", current, PROMPT_VERSION),
        (stale, "옛 digest", content_hash_of(["옛 본문"]), PROMPT_VERSION),
        (old_version, "옛 버전 digest", content_hash_of(["[3:0]", "[3:1]"]), "old"),
    ]
    for source, text, content_hash, version in rows:
        db.add(
            SourceDigest(
                source_id=source.id,
                content_hash=content_hash,
                digest=text,
                token_count=1,
                prompt_version=version,
            )
        )
    await db.commit()
    sources = await overview._ready_sources(db, nb.id)

    with _count_queries(db) as statements:
        texts = await overview._source_texts(db, sources)

    assert texts == ["가이드 요약", "최신 digest", "[2:0]", "[3:0]", "[4:0]", "s5"]
    assert len(statements) == 3


async def test_source_texts_500_sources_all_included_with_constant_queries(db, user_a):
    nb = await _notebook(db, user_a.id)
    sources = await _sources_with_chunks(db, nb.id, [2] * 500)

    with _count_queries(db) as statements:
        texts = await overview._source_texts(db, sources)

    assert texts == [f"[{i}:0]" for i in range(500)]
    assert len(statements) == 2  # digest 행이 없으면 지문 쿼리는 건너뛴다
    context = overview._overview_context(texts)
    assert context.count("\n[") == 500 and "[500] [499:0]" in context


async def test_source_texts_empty_sources_issues_no_query(db):
    with _count_queries(db) as statements:
        assert await overview._source_texts(db, []) == []
    assert statements == []

