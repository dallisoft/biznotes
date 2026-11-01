import pytest
from biznotes_core.services.ingestion.parsers import (
    ParseError,
    parse_file,
    parse_pdf,
    parse_plain,
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

    from biznotes_core.services.ingestion.parsers import parse_file
    from openpyxl import Workbook

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


def _find_korean_font() -> str | None:
    """시스템에 설치된 한글 지원 폰트 경로를 찾는다(CI/로컬 환경 이식성)."""
    import os

    candidates = [
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",  # macOS
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",  # Ubuntu(fonts-nanum)
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",  # Ubuntu(fonts-noto-cjk)
    ]
    return next((p for p in candidates if os.path.exists(p)), None)

