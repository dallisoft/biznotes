# 신규(#462). 필요: PostgreSQL·Valkey(PG 테스트).
"""본문의 U+0000(NUL) — 실제 PostgreSQL이 `source_chunks.text`를 거부하지 않는다(#462).

PostgreSQL text는 U+0000을 받지 않는다(`CharacterNotInRepertoireError`). 그래서 이 실패는 실제
PostgreSQL에서만 재현된다. `_run`(진입·파싱)과 `_run_index`(청킹·임베딩·색인)는 실경로다. 원격
임베딩 모델(게이트웨이 공간), sparse 임베딩, 스토리지(`ingest_objects`), 브로커 발행, 개요 예약만
대역이다. 벡터는 실제 pgvector 행이다. `create_all` 기반이라 Alembic 마이그레이션
검증이 아니다.
"""

import pathlib
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from biznotes_core.config import get_settings
from biznotes_core.core.db import async_session_factory
from biznotes_core.models import Notebook, ReindexRun, ReindexRunItem, Source, SourceChunk, User
from biznotes_core.services.ingestion import stage_artifacts
from biznotes_core.services.ingestion.embedder import SparseVector
from biznotes_core.services.ingestion.parsers import Location, ParseResult
from biznotes_core.workers.celery_app import celery_app
from biznotes_core.workers.tasks import ingestion, overview

# 공백 글리프를 U+0000에 매핑한 ToUnicode를 가진 PDF — tests/test_parsers.py와 같은 픽스처
NUL_SPACE_PDF = (
    pathlib.Path(__file__).parent.parent / "tests" / "fixtures" / "pdf_nul_space.pdf"
).read_bytes()
# PDF 이외 파서(웹·docx·커넥터·OCR·STT)가 NUL을 섞어 낸 모양
NUL_MARKDOWN = "## 제목\x00\n\n■\x00본\x00보고서는\x00투자\x00의사결정을\x00위한 자료다.\n"
# 이전 세대가 저장한 parsed.md — 쪽마다 위치가 있다(HWP 모양). 기본 청크 크기에서 쪽마다 청크 하나다
OLD_PAGE1 = "## Page 1\n\n" + "첫\x00쪽\x00본문\x00문장입니다. " * 50 + "\n\n"
OLD_PAGE2 = "## Page 2\n\n" + "(2026.10.23\x00기준)\x00둘째\x00쪽\x00본문. " * 50
OLD_PARSED = OLD_PAGE1 + OLD_PAGE2
OLD_LOCATIONS = [
    Location(0, len(OLD_PAGE1), page=1, section=0, paragraph=0),
    Location(len(OLD_PAGE1), len(OLD_PARSED), page=2, section=0, paragraph=3),
]


class FakeSpace:
    """게이트웨이 임베딩 공간 대역 — 컬렉션 이름은 `chunk_collections()`가 아는 접두사다."""

    is_local = False
    key = "gateway:fake-462"
    collection = "chunks_m_fake_462"
    display_name = "fake"

    def embed(self, texts: list[str], **kw: Any) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


@pytest.fixture
def space(monkeypatch: pytest.MonkeyPatch, ingest_objects: Any) -> FakeSpace:
    fake = FakeSpace()

    async def active_space(db: Any) -> Any:
        return fake

    monkeypatch.setattr(ingestion, "active_space", active_space)
    monkeypatch.setattr(
        ingestion, "embed_sparse", lambda texts: [SparseVector([0], [1.0]) for _ in texts]
    )
    monkeypatch.setattr(overview, "schedule_overview_refresh", lambda nid, **kw: None)
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: None)
    return fake


def _bucket(kind: str) -> str:
    settings = get_settings()
    return settings.storage_bucket_sources if kind == "sources" else settings.storage_bucket_parsed


async def _source(owner: User, status: str, *, mime: str, parsed: bool = False) -> uuid.UUID:
    async with async_session_factory() as s:
        nb = Notebook(owner_id=owner.id, title="NUL")
        s.add(nb)
        await s.flush()
        src = Source(
            notebook_id=nb.id, type="file", title="보고서", mime_type=mime, status=status
        )
        s.add(src)
        await s.flush()
        src.original_object_key = f"{src.id}/raw"
        if parsed:
            src.parsed_object_key = f"{src.id}/parsed.md"
        await s.commit()
        return src.id


async def _row(sid: uuid.UUID) -> Source:
    async with async_session_factory() as s:
        row = await s.get(Source, sid)
        assert row is not None
        return row


async def _chunks(sid: uuid.UUID) -> list[SourceChunk]:
    async with async_session_factory() as s:
        rows = await s.scalars(
            select(SourceChunk).where(SourceChunk.source_id == sid).order_by(SourceChunk.seq)
        )
        return list(rows.all())


async def _ingest(sid: uuid.UUID) -> None:
    entry = await ingestion._run(str(sid))
    assert entry["ok"] is True, entry
    result = await ingestion._run_index(str(sid), **entry["handoff"])
    assert result["ok"] is True, result


async def test_pdf_with_nul_mapped_space_becomes_ready(
    user_a: User, space: FakeSpace, ingest_objects: Any
) -> None:
    """수락 기준 1 — 실제 `parse_pdf`가 읽은 PDF가 `ready`가 되고 공백 자리가 공백으로 남는다."""
    sid = await _source(user_a, "pending", mime="application/pdf")
    ingest_objects.objects[(_bucket("sources"), f"{sid}/raw")] = NUL_SPACE_PDF

    await _ingest(sid)

    row = await _row(sid)
    assert (row.status, row.error_message) == ("ready", None)
    parsed = ingest_objects.objects[(_bucket("parsed"), f"{sid}/parsed.md")].decode()
    assert "\x00" not in parsed
    text = "\n".join(c.text for c in await _chunks(sid))
    assert "\x00" not in text
    assert "Technology analysis report for investment decisions" in text
    assert "(2026.10.23 basis) Revenue grew" in text


async def test_other_parser_nul_is_not_stored(
    user_a: User, space: FakeSpace, ingest_objects: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """수락 기준 2 — PDF가 아닌 파서가 NUL 섞인 markdown을 내도 parsed.md·청크에 남지 않는다."""
    monkeypatch.setattr(ingestion, "_parse", lambda source, raw: ParseResult(markdown=NUL_MARKDOWN))
    sid = await _source(
        user_a,
        "pending",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    ingest_objects.objects[(_bucket("sources"), f"{sid}/raw")] = b"raw-docx"

    await _ingest(sid)

    assert (await _row(sid)).status == "ready"
    parsed = ingest_objects.objects[(_bucket("parsed"), f"{sid}/parsed.md")].decode()
    assert parsed == NUL_MARKDOWN.replace("\x00", " ")
    chunks = await _chunks(sid)
    assert chunks and all("\x00" not in c.text for c in chunks)
    assert "■ 본 보고서는 투자 의사결정을 위한 자료다." in chunks[0].text


async def test_reindex_of_stored_nul_parsed_md_keeps_locations(
    user_a: User, space: FakeSpace, ingest_objects: Any
) -> None:
    """수락 기준 3 — 이전 세대가 NUL째 저장한 parsed.md로 재인덱싱해도 실패하지 않는다.

    이슈의 4건처럼 색인에서 `failed`가 된 소스다. 같은 세대의 위치(locations.json)도 그대로
    맞는다 — NUL을 지우지 않고 같은 길이의 공백으로 바꾸기 때문이다.
    """
    sid = await _source(user_a, "failed", mime="application/x-hwp", parsed=True)
    stored = OLD_PARSED.encode()
    ingest_objects.objects[(_bucket("parsed"), f"{sid}/parsed.md")] = stored
    ingest_objects.objects[(_bucket("parsed"), stage_artifacts.locations_key(sid))] = (
        stage_artifacts.encode_locations(stage_artifacts.parsed_digest(stored), OLD_LOCATIONS)
    )
    async with async_session_factory() as s:
        run = ReindexRun(options={"refetch": False}, requested=1, scope="all")
        s.add(run)
        await s.flush()
        s.add(ReindexRunItem(run_id=run.id, source_id=sid, status="queued"))
        await s.commit()

    await _ingest(sid)

    row = await _row(sid)
    assert (row.status, row.error_message) == ("ready", None)
    chunks = await _chunks(sid)
    assert all("\x00" not in c.text for c in chunks)
    # 위치가 어긋나지 않았다 — 청크마다 자기 본문의 쪽이 실린다
    pages = {c.meta.get("page") for c in chunks if "첫 쪽 본문 문장입니다." in c.text}
    assert pages == {1}
    pages = {c.meta.get("page") for c in chunks if "(2026.10.23 기준) 둘째 쪽 본문." in c.text}
    assert pages == {2}
