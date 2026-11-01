"""소스 문서 제목·인제스트 경고 (HWP-02·HWP-03).

계획 `docs/plans/source-metadata/plan.md`의 수락 기준 1~9를 DB 없이 확인한다. 워커 경로는
`RunHarness`(세션·저장소·파서만 가짜)로 실제 `_run`을 태운다.
"""

import io
import pathlib
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.dialects.postgresql import JSONB

import biznotes_core
from biznotes_core.models import Source
from biznotes_core.schemas.source import SourceOut
from biznotes_core.services.ingestion import parsers, source_warnings
from biznotes_core.services.ingestion.parsers import ParseError, ParseResult
from biznotes_core.workers.tasks import ingestion

TRUNCATED = source_warnings.warning(source_warnings.TEXT_TRUNCATED, omitted=3)
THREAT = source_warnings.warning(source_warnings.THREAT_FINDINGS, severity="medium", count=2)


# ── 수락 기준 1·2: 모델·스키마 ──────────────────────────────────────────


def test_columns_are_nullable_and_warnings_is_jsonb_none_as_null() -> None:
    cols = Source.__table__.c
    assert cols.document_title.nullable and cols.document_title.type.length == 500
    assert cols.warnings.nullable and isinstance(cols.warnings.type, JSONB)
    assert cols.warnings.type.none_as_null is True
    # 옛 앱의 INSERT가 그대로 동작하도록 server default를 두지 않는다
    assert cols.document_title.server_default is None and cols.warnings.server_default is None


def _row(**kw: Any) -> Source:
    now = datetime(2026, 10, 2, tzinfo=UTC)
    return Source(
        id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        type="file",
        title="공문.hwp",
        status="ready",
        token_count=0,
        chunk_count=0,
        created_at=now,
        updated_at=now,
        **kw,
    )


def test_source_out_serializes_both_fields() -> None:
    out = SourceOut.model_validate(_row(document_title="2026년 계획", warnings=[TRUNCATED]))

    dumped = out.model_dump(mode="json")
    assert dumped["title"] == "공문.hwp"
    assert dumped["document_title"] == "2026년 계획"
    assert dumped["warnings"] == [{"code": "text_truncated", "params": {"omitted": 3}}]


def test_source_out_keeps_null_for_rows_without_a_record() -> None:
    dumped = SourceOut.model_validate(_row()).model_dump(mode="json")
    assert dumped["document_title"] is None and dumped["warnings"] is None


# ── 경고 코드 ──────────────────────────────────────────────────────────


def test_unknown_code_is_rejected_and_text_params_are_folded() -> None:
    with pytest.raises(ValueError):
        source_warnings.warning("made_up")
    w = source_warnings.warning(
        source_warnings.SPREADSHEET_ROWS_TRUNCATED, sheet="시트\n\u202e이름" + "가" * 200
    )
    assert w["params"]["sheet"].startswith("시트 이름") and len(w["params"]["sheet"]) == 100


def test_xlsx_row_cap_becomes_a_warning() -> None:
    from openpyxl import Workbook

    wb = Workbook()
    big = wb.active
    big.title = "큰 시트"
    for i in range(2005):
        big.append([i, "값"])
    small = wb.create_sheet("작은 시트")
    small.append(["a", "b"])
    # 앞 2000행이 비어 표가 없어도 상한에 닿았으면 경고한다(리뷰 m2)
    blank = wb.create_sheet("빈 앞부분")
    blank.cell(row=2003, column=1, value="뒤")
    buf = io.BytesIO()
    wb.save(buf)

    result = parsers.parse_xlsx(buf.getvalue())

    # 시트 이름(문서 원문) 대신 1부터 센 순번을 싣는다
    assert result.source_warnings == [
        {"code": "spreadsheet_rows_truncated", "params": {"sheet_index": 1, "max_rows": 2000}},
        {"code": "spreadsheet_rows_truncated", "params": {"sheet_index": 3, "max_rows": 2000}},
    ]


# ── 수락 기준 8: 문서 제목 정화 ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  2026년\n사업\t계획  ", "2026년 사업 계획"),
        ("제목\u202e\u200b\u2028끝\x00", "제목 끝"),
        # 리뷰 M3 — NUL·ESC·RLM·한글 채움 문자·서로게이트(PostgreSQL이 거부하는 글자 포함)
        ("Report\x00\u202e\u200fgpj.exe\x1b[31m\u3164", "Reportgpj.exe[31m"),
        ("a\ud800b\ufe0f\u034fc", "abc"),
        ("", None),
        (None, None),
        (" \n\u200b ", None),
    ],
)
def test_document_title_is_one_visible_line(raw: str | None, expected: str | None) -> None:
    assert source_warnings.sanitize_document_title(raw) == expected


def test_params_are_storable_in_jsonb() -> None:
    """리뷰 M3·M4 — NaN·무한대는 JSONB가 거부한다. 문자열은 정화한다."""
    w = source_warnings.warning(
        source_warnings.HIDDEN_TEXT, chars=True, ratio=float("nan"), note="a\x00b"
    )
    assert w["params"] == {"chars": 1, "ratio": 0.0, "note": "ab"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(3, 3), (-1, 0), ("7", 0), (None, 0), (True, 0), (1.5, 0), (2**60, 2**53 - 1)],
)
def test_untrusted_counts_become_safe_naturals(raw: Any, expected: int) -> None:
    """리뷰 M4 — rhwp `omittedCount` 같은 비신뢰 개수."""
    assert source_warnings.nat(raw) == expected


def test_source_out_drops_malformed_warning_items() -> None:
    """리뷰 M4 — 형식이 다른 항목 하나가 목록 응답 전체를 깨뜨리지 않는다."""
    stored = [TRUNCATED, {"code": "x", "params": {"v": [1]}}, "문자열", {"params": {}}]

    dumped = SourceOut.model_validate(_row(warnings=stored)).model_dump(mode="json")

    assert dumped["warnings"] == [TRUNCATED]
    assert SourceOut.model_validate(_row(warnings={"a": 1})).warnings is None


def test_document_title_is_capped() -> None:
    title = source_warnings.sanitize_document_title("가" * 900)
    assert title is not None and len(title) == 500


# ── 수락 기준 3~7: 워커 쓰기 규칙 ─────────────────────────────────────


async def test_full_parse_writes_title_and_warnings_without_touching_title(run_harness) -> None:
    h = run_harness
    h.source.title = "공문.hwp"
    h.parser(
        lambda data: ParseResult(markdown="본문", title="사업\n계획", source_warnings=[TRUNCATED])
    )

    assert (await h.run())["ok"] is True

    assert h.source.title == "공문.hwp"
    assert h.source.document_title == "사업 계획"
    assert h.source.warnings == [TRUNCATED]


async def test_parse_without_title_or_warnings_records_empty(run_harness) -> None:
    h = run_harness
    h.parser(lambda data: ParseResult(markdown="본문"))

    await h.run()

    assert h.source.document_title is None
    assert h.source.warnings == []  # 파싱했고 경고 없음 — NULL(기록 없음)과 다르다


async def test_placeholder_title_and_document_title_agree(run_harness) -> None:
    """웹 자리표시자 제목도 정화한 같은 값으로 바꾼다(리뷰 M3)."""
    h = run_harness
    h.source.title = "(제목 추출 중) https://example.test"
    h.parser(lambda data: ParseResult(markdown="본문", title="웹\x00 문서\n제목"))

    await h.run()

    assert h.source.title == "웹 문서 제목" and h.source.document_title == "웹 문서 제목"


async def test_placeholder_stays_when_the_title_is_empty_after_cleaning(run_harness) -> None:
    h = run_harness
    h.source.title = "(제목 추출 중) https://example.test"
    h.parser(lambda data: ParseResult(markdown="본문", title="\u200b\x00"))

    await h.run()

    assert h.source.title.startswith("(제목 추출 중)") and h.source.document_title is None


async def test_reparse_replaces_previous_warnings(run_harness) -> None:
    h = run_harness
    h.source.document_title = "옛 제목"
    h.source.warnings = [TRUNCATED, THREAT]
    h.source.parsed_object_key = f"{h.source.id}/parsed.md"
    h.parser(lambda data: ParseResult(markdown="다시 파싱"))

    h.open_reindex = (True, True)  # 열린 재인덱싱 항목
    await h.run(reindex=True, refetch=True)

    assert h.source.document_title is None and h.source.warnings == []


async def test_reindex_from_parsed_keeps_both(run_harness) -> None:
    h = run_harness
    h.source.document_title = "옛 제목"
    h.source.warnings = [TRUNCATED]
    h.source.status = "ready"
    h.source.parsed_object_key = f"{h.source.id}/parsed.md"
    h.raw = "# 저장된 본문\n\n본문".encode()

    h.open_reindex = (True, False)  # 열린 재인덱싱 항목
    assert (await h.run(reindex=True, refetch=False))["ok"] is True

    assert h.source.document_title == "옛 제목" and h.source.warnings == [TRUNCATED]


async def test_failure_after_parse_keeps_the_new_parse_result(run_harness) -> None:
    """리뷰 M1 — 파싱이 성공하면 뒤 단계(청킹·임베딩)가 실패해도 새 값이 남는다.

    parsed.md·자리표시자 제목과 같은 정책이다. 첫 인제스트면 소스는 failed다.
    """
    h = run_harness
    h.parser(lambda data: ParseResult(markdown="본문", title="새 제목", source_warnings=[THREAT]))

    def boom(texts: Any) -> Any:
        raise RuntimeError("임베딩 실패")

    h.mp.setattr(ingestion, "embed_dense", boom)
    assert (await h.run())["ok"] is False

    assert h.source.status == "failed"
    assert h.source.document_title == "새 제목" and h.source.warnings == [THREAT]


async def test_keep_previous_reindex_failure_describes_the_new_parse(run_harness) -> None:
    """리뷰 M1 — refetch 재인덱싱이 파싱 뒤 실패해 이전 색인을 유지해도 두 컬럼은 새 파싱 값이다.

    이전 색인과 어긋나지만 parsed.md도 이미 새 파싱으로 바뀌었다(같은 정책). 다음 재인덱싱이
    그 parsed.md에서 시작하므로 경고가 그 본문을 설명하는 편이 맞다.
    """
    from biznotes_core.services.embedding_spaces import LOCAL_SPACE

    h = run_harness
    h.source.status = "ready"
    h.source.parsed_object_key = f"{h.source.id}/parsed.md"
    h.source.embedding_model = LOCAL_SPACE.key
    h.source.warnings = [TRUNCATED]
    h.parser(lambda data: ParseResult(markdown="다시 파싱", source_warnings=[THREAT]))

    def boom(text: str) -> int:
        raise RuntimeError("청킹 실패")

    h.mp.setattr(ingestion, "count_tokens", boom)
    h.open_reindex = (True, True)  # 열린 재인덱싱 항목
    assert (await h.run(reindex=True, refetch=True))["ok"] is False

    assert h.source.status == "ready"
    # 유지 안내는 경고 코드로 새 파싱 경고 뒤에 붙는다 — error_message는 비어 있다(#230)
    assert h.source.error_message is None
    assert h.source.warnings == [
        THREAT,
        {"code": "reindex_kept_previous", "params": {"reason": "error"}},
    ]


async def test_failed_parse_keeps_both(run_harness) -> None:
    h = run_harness
    h.source.document_title = "옛 제목"
    h.source.warnings = [TRUNCATED]

    def broken(data: bytes) -> ParseResult:
        raise ParseError("손상")

    h.parser(broken)
    await h.run()

    assert h.source.status == "failed"
    assert h.source.document_title == "옛 제목" and h.source.warnings == [TRUNCATED]


# ── 수락 기준 9: 중복 식별·HWP 라우팅·프롬프트는 계속 title ─────────────


def test_document_title_is_read_only_by_the_response_schema() -> None:
    """`document_title`을 쓰는 곳은 모델·응답 스키마·워커 쓰기·retry 초기화뿐이다.

    중복 식별(`ensure_file_source_capacity_and_uniqueness`), HWP 라우팅(`hwp_queue_for`),
    프롬프트(인용 제목·`source_load`)는 `title`을 쓴다. 외부가 정한 문서 제목이 키나 프롬프트로
    새지 않게 사용처를 고정한다 — 넓히려면 계획을 고친다.
    """
    root = pathlib.Path(biznotes_core.__file__).parent
    users = sorted(
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if "document_title" in p.read_text(encoding="utf-8")
    )
    assert users == [
        "api/v1/sources.py",
        "models/source.py",
        "schemas/source.py",
        "services/ingestion/source_warnings.py",
        "workers/tasks/ingestion.py",
    ]
