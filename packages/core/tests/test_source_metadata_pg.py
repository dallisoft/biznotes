"""소스 문서 제목·인제스트 경고의 커밋 의미 (HWP-02·HWP-03) — 실제 PostgreSQL.

`tests/test_source_metadata.py`는 가짜 세션 위에서 돈다. 여기서는 실제 세션의 커밋·롤백으로
계획 `docs/plans/source-metadata/plan.md` 수락 기준 6을 확인한다(리뷰 m-a·m-b):

- 파싱이 성공하면 chunking 커밋으로 확정된 두 컬럼이 뒤 단계 실패의 롤백에서 살아남는다. 뒤
  단계는 이제 별도 태스크(`_run_index`, #244)다.
- parse 단계 안의 실패(parsed.md 업로드 실패)는 두 컬럼을 바꾸지 않는다.
- NUL이 든 문서 제목도 정화돼 저장된다(정화가 없으면 PostgreSQL이 거부한다).

스토리지(`ingest_objects`)·임베딩·벡터스토어는 대역이다(`test_ingestion_ocr.py`와 같은 방식).
"""

import uuid
from typing import Any

import pytest

from biznotes_core.config import get_settings
from biznotes_core.models import Notebook, Source
from biznotes_core.services.ingestion import source_warnings
from biznotes_core.services.ingestion.embedder import SparseVector
from biznotes_core.services.ingestion.parsers import ParseResult
from biznotes_core.workers.tasks.ingestion import _run, _run_index

OLD = [source_warnings.warning(source_warnings.TEXT_TRUNCATED, omitted=1)]
NEW = [source_warnings.warning(source_warnings.THREAT_FINDINGS, severity="low", count=2)]
P = "biznotes_core.workers.tasks.ingestion"
RAW_KEY = "k/문서.pdf"


@pytest.fixture
def _pipeline(monkeypatch, ingest_objects):
    ingest_objects.objects[(get_settings().storage_bucket_sources, RAW_KEY)] = b"raw-bytes"
    monkeypatch.setattr(f"{P}.embed_dense", lambda texts: [[1.0] for _ in texts])
    monkeypatch.setattr(f"{P}.embed_sparse", lambda texts: [SparseVector([], []) for _ in texts])
    monkeypatch.setattr(
        "biznotes_core.workers.tasks.overview.schedule_overview_refresh",
        lambda notebook_id, **kwargs: None,
    )
    monkeypatch.setattr(
        f"{P}._parse",
        lambda source, raw: ParseResult(
            markdown="## 본문\n\n내용입니다.", title="새\x00 제목", source_warnings=NEW
        ),
    )


async def _source(db: Any, user: Any) -> uuid.UUID:
    nb = Notebook(owner_id=user.id, title="meta-nb")
    db.add(nb)
    await db.flush()
    source = Source(
        notebook_id=nb.id,
        type="file",
        title="문서.pdf",
        mime_type="application/pdf",
        original_object_key=RAW_KEY,
        status="pending",
        document_title="옛 제목",
        warnings=OLD,
    )
    db.add(source)
    await db.flush()
    sid = source.id
    await db.commit()
    return sid


async def _ingest(sid: uuid.UUID) -> dict[str, Any]:
    """진입(파싱) 뒤 색인 단계까지 — 진입이 넘기지 않았으면(실패) 그 결과다(#244)."""
    entry = await _run(str(sid))
    handoff = entry.pop("handoff", None)
    if handoff is None:
        return entry
    return await _run_index(str(sid), **handoff)


async def _reload(db: Any, sid: uuid.UUID) -> Source:
    await db.commit()
    row = await db.get(Source, sid, populate_existing=True)
    assert row is not None
    return row


async def test_parse_writes_sanitized_title_and_warnings(db, user_a, _pipeline) -> None:
    sid = await _source(db, user_a)

    assert (await _ingest(sid))["ok"] is True

    row = await _reload(db, sid)
    assert row.status == "ready" and row.title == "문서.pdf"
    assert row.document_title == "새 제목" and row.warnings == NEW


async def test_failure_after_parse_keeps_the_committed_parse_result(
    db, user_a, monkeypatch, _pipeline
) -> None:
    sid = await _source(db, user_a)

    def boom(texts: Any) -> Any:
        raise RuntimeError("임베딩 실패")

    monkeypatch.setattr(f"{P}.embed_dense", boom)
    entry = await _run(str(sid))
    assert entry["ok"] is True  # 파싱 단계는 chunking을 커밋하고 끝났다
    assert (await _run_index(str(sid), **entry["handoff"]))["ok"] is False

    row = await _reload(db, sid)
    assert row.status == "failed"
    assert row.document_title == "새 제목" and row.warnings == NEW


async def test_upload_failure_in_parse_stage_keeps_both(db, user_a, monkeypatch, _pipeline) -> None:
    sid = await _source(db, user_a)

    def upload_fails(*a: Any, **k: Any) -> None:
        raise RuntimeError("스토리지 업로드 실패")

    monkeypatch.setattr(f"{P}.put_object", upload_fails)
    assert (await _ingest(sid))["ok"] is False

    row = await _reload(db, sid)
    assert row.status == "failed"
    assert row.document_title == "옛 제목" and row.warnings == OLD
