"""sources·reindex 스윕 인덱스와 청크 유일 제약 (#44, #52) — 메타데이터 계약.

제품 Alembic 리비전이 이 이름·열을 그대로 만든다
(계획 `docs/plans/source-index-constraints/plan.md`).
실제 인덱스 선택과 제약 동작은 PostgreSQL이 필요해 계획 문서의 PG 스모크로 검증한다.
"""

from sqlalchemy import UniqueConstraint

from biznotes_core.models import ReindexRunItem, Source, SourceChunk


def _indexes(model: type) -> dict[str, list[str]]:
    return {ix.name: [c.name for c in ix.columns] for ix in model.__table__.indexes}


def test_sources_has_status_updated_at_composite_index() -> None:
    ix = next(i for i in Source.__table__.indexes if i.name == "ix_sources_status_updated_at")

    assert [c.name for c in ix.columns] == ["status", "updated_at"]
    # 부분 인덱스가 아니다 — 바인드 파라미터 상태 값에도 쓸 수 있다(D1)
    assert not ix.dialect_options["postgresql"].get("where")


def test_reindex_run_items_has_updated_at_index_and_keeps_queued_partial() -> None:
    indexes = _indexes(ReindexRunItem)

    assert indexes["ix_reindex_run_items_updated_at"] == ["updated_at"]
    assert indexes["ix_reindex_run_items_queued"] == ["source_id"]


def test_source_chunks_unique_per_source_seq() -> None:
    (uq,) = [c for c in SourceChunk.__table__.constraints if isinstance(c, UniqueConstraint)]

    assert uq.name == "uq_source_chunks_source_seq"
    assert [c.name for c in uq.columns] == ["source_id", "seq"]
    # 단일 source_id 인덱스는 그대로 둔다(비목표)
    assert _indexes(SourceChunk)["ix_source_chunks_source_id"] == ["source_id"]


def test_sources_has_partial_notebook_external_ref_index() -> None:
    """`save_sources` 중복 키(#403) — 비유일 부분 인덱스.

    제품 리비전이 같은 이름·열·조건으로 만든다.
    """
    ix = next(i for i in Source.__table__.indexes if i.name == "ix_sources_notebook_external_ref")

    assert [c.name for c in ix.columns] == ["notebook_id", "external_ref"]
    # 커넥터 행에 이미 중복이 있을 수 있다 — 유일 제약이 아니다(D2)
    assert ix.unique is False
    assert str(ix.dialect_options["postgresql"]["where"]) == "external_ref IS NOT NULL"
