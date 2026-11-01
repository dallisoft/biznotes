"""소스 파서 단위 테스트 (sl-agents `apps/api/tests/test_parsers.py`에서 이관 — #1).

파서 라이브러리(pypdf·python-docx·openpyxl)는 코어 dev 그룹에 있다. OCR은 하지 않는다.
"""

import importlib.util
import pathlib
import sys
import types

import httpx
import pytest

from biznotes_core.services.ingestion.parsers import (
    ParseError,
    parse_file,
    parse_pdf,
    parse_plain,
    parse_web,
    replace_nul,
)


def test_parse_plain_utf8():
    res = parse_plain("# 제목\n\n본문".encode())
    assert "제목" in res.markdown


def test_parse_plain_cp949_fallback():
    res = parse_plain("한글 텍스트".encode("cp949"))
    assert "한글" in res.markdown


def test_parse_plain_empty_raises():
    with pytest.raises(ParseError):
        parse_plain(b"   ")


def test_parse_file_unsupported_mime():
    with pytest.raises(ParseError, match="지원하지 않는"):
        parse_file(b"x", "application/zip")


def test_parse_file_markdown():
    res = parse_file("## 섹션\n내용".encode(), "text/markdown")
    assert "섹션" in res.markdown


def test_parse_xlsx_to_markdown_table():
    import io

    from openpyxl import Workbook

    from biznotes_core.services.ingestion.parsers import parse_file

    wb = Workbook()
    ws = wb.active
    ws.title = "매출"
    ws.append(["월", "금액"])
    ws.append(["1월", 1000])
    ws.append(["2월", 2000])
    buf = io.BytesIO()
    wb.save(buf)

    res = parse_file(
        buf.getvalue(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert "## 매출" in res.markdown
    assert "| 월 | 금액 |" in res.markdown
    assert "1월" in res.markdown and "2000" in res.markdown


def test_parse_docx_roundtrip():
    import io

    import docx

    doc = docx.Document()
    doc.add_heading("문서 제목", level=1)
    doc.add_paragraph("첫 문단입니다.")
    buf = io.BytesIO()
    doc.save(buf)

    res = parse_file(
        buf.getvalue(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert "# 문서 제목" in res.markdown
    assert "첫 문단입니다." in res.markdown


def test_parse_pdf_without_text_layer_is_rejected():
    """글자 층이 없는 PDF(스캔 이미지)는 OCR하지 않고 명확한 안내로 거부한다."""
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)

    with pytest.raises(ParseError, match=r"스캔\(이미지\) PDF는 지원하지 않습니다"):
        parse_file(buf.getvalue(), "application/pdf")


def test_parse_pdf_extracts_page_text(monkeypatch):
    """쪽에서 추출한 텍스트가 본문이 된다."""
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)

    long_text = "충분한 텍스트가 있는 페이지입니다. " * 5  # 50자 이상
    monkeypatch.setattr("pypdf._page.PageObject.extract_text", lambda self: long_text)

    res = parse_pdf(buf.getvalue())
    assert long_text.strip() in res.markdown


def test_parse_pdf_mixed_document_keeps_text_pages(monkeypatch):
    """일부 쪽만 비어 있으면 실패하지 않고 글이 있는 쪽만 본문이 된다."""
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)

    texts = iter(["충분한 텍스트가 있는 첫 페이지입니다 " * 5, ""])
    monkeypatch.setattr(
        "pypdf._page.PageObject.extract_text", lambda self: next(texts)
    )

    res = parse_pdf(buf.getvalue())
    assert "충분한 텍스트가 있는 첫 페이지입니다" in res.markdown


def test_parse_pdf_user_password_encrypted_raises_parse_error():
    """사용자 암호(열람 시 필요)가 걸린 PDF는 실제로 복호화가 안 되므로 거부."""
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt(user_password="secret")
    buf = io.BytesIO()
    writer.write(buf)

    with pytest.raises(ParseError, match="암호화된 PDF"):
        parse_pdf(buf.getvalue())


def test_parse_pdf_owner_password_only_does_not_raise(monkeypatch):
    """owner-password만 걸린 PDF는 암호 없이 바로 열람 가능하므로 거부하면 안 된다
    (reader.is_encrypted가 True로 남아있어도 실제로는 읽을 수 있는 문서, 코드
    리뷰 지적 반영 — pypdf PdfReader.is_encrypted 독스트링: decrypt() 이후에도
    계속 True로 남음)."""
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt(user_password="", owner_password="owner-secret")
    buf = io.BytesIO()
    writer.write(buf)

    long_text = "권한 제한만 걸린 문서의 본문 텍스트입니다 " * 5
    monkeypatch.setattr("pypdf._page.PageObject.extract_text", lambda self: long_text)

    res = parse_pdf(buf.getvalue())
    assert long_text.strip() in res.markdown


def test_parse_pdf_corrupted_raises_distinct_message():
    """암호화가 아닌 손상된 파일은 별도의 명확한 메시지로 구분한다."""
    with pytest.raises(ParseError, match="손상된 PDF"):
        parse_pdf(b"%PDF-1.4\ncorrupted garbage not a real pdf structure")


# 한컴 폰트 PDF처럼 ToUnicode CMap이 공백 글리프(0x20)를 U+0000에 매핑한 PDF(#462).
# 비압축 평문이다 — 폰트 객체(4 0 obj)와 CMap 스트림(6 0 obj)을 그대로 읽을 수 있다
NUL_SPACE_PDF = pathlib.Path(__file__).parent / "fixtures" / "pdf_nul_space.pdf"


def test_parse_pdf_replaces_nul_mapped_space_with_space():
    """공백 자리의 U+0000은 공백이 된다 — 단어 경계가 남고 PostgreSQL이 거부할 글자가 없다."""
    import io

    from pypdf import PdfReader

    data = NUL_SPACE_PDF.read_bytes()
    # 픽스처가 실제로 NUL을 낸다(대조) — pypdf가 이 매핑을 고치면 이 테스트가 알려 준다
    assert "\x00" in (PdfReader(io.BytesIO(data)).pages[0].extract_text() or "")

    res = parse_pdf(data)

    assert "\x00" not in res.markdown
    assert "Technology analysis report for investment decisions" in res.markdown
    assert "(2026.10.23 basis) Revenue grew while margins held" in res.markdown


def test_parse_pdf_nul_only_document_is_rejected(monkeypatch):
    """U+0000만 있는 쪽은 글이 없는 쪽이다 — 그런 쪽뿐이면 글자 층이 없는 PDF로 거부한다."""
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    monkeypatch.setattr("pypdf._page.PageObject.extract_text", lambda self: "\x00" * 80)

    with pytest.raises(ParseError, match="스캔"):
        parse_pdf(buf.getvalue())


def test_replace_nul_keeps_length():
    """지우지 않고 같은 자리를 공백으로 바꾼다 — locations 오프셋이 그대로 맞는다."""
    text = "■\x00본\x00보고서는\x00\x00끝"

    out = replace_nul(text)

    assert out == "■ 본 보고서는  끝"
    assert len(out) == len(text)


def _find_korean_font() -> str | None:
    """시스템에 설치된 한글 지원 폰트 경로를 찾는다(CI/로컬 환경 이식성)."""
    import os

    candidates = [
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",  # macOS
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",  # Ubuntu(fonts-nanum)
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",  # Ubuntu(fonts-noto-cjk)
    ]
    return next((p for p in candidates if os.path.exists(p)), None)


# ---- parse_web 실패 사유 (#30) ---------------------------------------------
# 네트워크 경계(ssrf_safe_fetch)만 대체한다 — 사유 정규화는 parse_web 본문 그대로 검증.

_WEB_URL = "https://example.com/article"


def _stub_fetch(monkeypatch, result):
    # trafilatura는 코어 의존성이 아니다(지연 import). 실패 경로는 추출에 닿지 않으므로
    # 미설치 환경에서는 빈 모듈로 import만 통과시킨다.
    if importlib.util.find_spec("trafilatura") is None:
        monkeypatch.setitem(sys.modules, "trafilatura", types.ModuleType("trafilatura"))

    def fake_fetch(url, **_kwargs):
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr("biznotes_core.core.ssrf.ssrf_safe_fetch", fake_fetch)


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (httpx.DecodingError("Error -3 while decompressing data"), "(DecodingError: Error -3"),
        (httpx.ConnectTimeout("timed out"), "(ConnectTimeout: timed out)"),
        (httpx.ReadTimeout(""), "(ReadTimeout)"),
    ],
)
def test_parse_web_network_error_keeps_reason(monkeypatch, exc, reason):
    _stub_fetch(monkeypatch, exc)
    with pytest.raises(ParseError) as ei:
        parse_web(_WEB_URL)
    assert str(ei.value).startswith(f"URL을 가져오지 못했습니다: {_WEB_URL} ")
    assert reason in str(ei.value)
    assert ei.value.__cause__ is exc


def test_parse_web_http_error_reports_status(monkeypatch):
    _stub_fetch(monkeypatch, httpx.Response(403, text="blocked"))
    with pytest.raises(ParseError, match=r"\(HTTP 403\)$"):
        parse_web(_WEB_URL)


def test_parse_web_empty_body_is_distinct(monkeypatch):
    _stub_fetch(monkeypatch, httpx.Response(200, text=""))
    with pytest.raises(ParseError, match="^응답 본문이 비어 있습니다: "):
        parse_web(_WEB_URL)


def test_parse_web_ssrf_block_is_not_wrapped(monkeypatch):
    from biznotes_core.core.ssrf import SSRFBlockedError

    _stub_fetch(monkeypatch, SSRFBlockedError("내부망 주소로의 요청이 차단되었습니다"))
    with pytest.raises(SSRFBlockedError):
        parse_web(_WEB_URL)


# ── 문서 속성 제목 (HWP-20) ─────────────────────────────────────────────────────────
# python-pptx는 dev 그룹에 없다 — PPTX 경우는 설치된 환경에서만 돈다(test_parser_tables와 같다)

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _docx_bytes(title: str | None) -> bytes:
    import io

    import docx

    doc = docx.Document()
    if title is not None:
        doc.core_properties.title = title
    doc.add_heading("본문 제목", level=1)
    doc.add_paragraph("첫 문단입니다.")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _pptx_bytes(title: str | None) -> bytes:
    import io

    pptx = pytest.importorskip("pptx")
    prs = pptx.Presentation()
    if title is not None:
        prs.core_properties.title = title
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "분기 실적"
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _xlsx_bytes(title: str | None) -> bytes:
    import io

    from openpyxl import Workbook

    wb = Workbook()
    if title is not None:
        wb.properties.title = title
    ws = wb.active
    ws.title = "매출"
    ws.append(["월", "금액"])
    ws.append(["1월", 1000])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _without_core_properties(data: bytes) -> bytes:
    """docProps/core.xml과 그 관계·콘텐츠 타입을 걷어 낸 OOXML 패키지."""
    import io
    import re
    import zipfile

    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if info.filename == "docProps/core.xml":
                continue
            body = src.read(info.filename)
            if info.filename == "_rels/.rels":
                body = re.sub(rb"<Relationship [^>]*core-properties[^>]*/>", b"", body)
                assert b"core-properties" not in body
            elif info.filename == "[Content_Types].xml":
                body = re.sub(rb'<Override [^>]*PartName="/docProps/core.xml"[^>]*/>', b"", body)
            dst.writestr(info, body)
    return out.getvalue()


_BUILDERS = {_DOCX_MIME: _docx_bytes, _PPTX_MIME: _pptx_bytes, _XLSX_MIME: _xlsx_bytes}


@pytest.mark.parametrize("mime", list(_BUILDERS))
def test_office_document_property_title_becomes_parse_title(mime: str) -> None:
    res = parse_file(_BUILDERS[mime]("2026년 사업 계획"), mime)

    assert res.title == "2026년 사업 계획"


@pytest.mark.parametrize("mime", list(_BUILDERS))
@pytest.mark.parametrize("title", [None, "", "   ", "\t\n"])
def test_office_blank_document_title_is_none(mime: str, title: str | None) -> None:
    assert parse_file(_BUILDERS[mime](title), mime).title is None


@pytest.mark.parametrize("mime", list(_BUILDERS))
def test_office_without_core_properties_has_no_title(mime: str) -> None:
    # python-docx·python-pptx는 core.xml이 없으면 "Word Document"·"PowerPoint Presentation"을
    # 지어 낸다 — 문서가 가진 제목이 아니므로 싣지 않는다
    data = _without_core_properties(_BUILDERS[mime]("지워질 제목"))

    res = parse_file(data, mime)

    assert res.title is None
    assert res.markdown == parse_file(_BUILDERS[mime](None), mime).markdown


@pytest.mark.parametrize("mime", list(_BUILDERS))
def test_office_document_title_does_not_change_markdown(mime: str) -> None:
    with_title = parse_file(_BUILDERS[mime]("속성 제목"), mime)
    without = parse_file(_BUILDERS[mime](None), mime)

    assert with_title.markdown == without.markdown
    assert "속성 제목" not in with_title.markdown


@pytest.mark.parametrize("mime", list(_BUILDERS))
def test_office_document_title_is_sanitized_by_the_shared_rule(mime: str) -> None:
    from biznotes_core.services.ingestion.source_warnings import sanitize_document_title

    # 파서는 원문을 그대로 넘기고, 한 줄 접기·보이지 않는 글자 제거는 저장 직전 정화가 한다
    raw = "사업​ 계획\n초안‮"
    res = parse_file(_BUILDERS[mime](raw), mime)

    assert res.title == raw
    assert sanitize_document_title(res.title) == "사업 계획 초안"
