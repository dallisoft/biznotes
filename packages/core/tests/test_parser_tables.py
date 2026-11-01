"""파서 표 출력 — PDF 박스 표 복원·페이지 경계 봉합, DOCX/PPTX 표의 GFM화 (#3).

PDF 후처리는 순수 문자열 함수라 pypdf 없이 검증한다. 파서 라이브러리가 필요한
배선 테스트는 해당 라이브러리가 설치된 환경에서만 실행된다.
"""

import io

import pytest

from biznotes_core.services.ingestion.parsers import _gfm_table, _pdf_pages_to_markdown

HEADER = "26. 9. 1. 오후  4:43 Where Agents Live, Part 3: ... | Medium"


def _footer(no: int) -> str:
    return f"https://medium.com/@adnanmasood/where-agents-live-part-3 {no}/34"


def _raw(no: int, *lines: str) -> tuple[int, str]:
    """머리말·꼬리말 없이 줄 그대로인 페이지."""
    return no, "\n".join(lines)


def _page(no: int, *body: str) -> tuple[int, str]:
    return no, "\n".join([*body, HEADER, _footer(no)])


def _table_lines(md: str) -> list[str]:
    return [line for line in md.splitlines() if line.startswith("|")]


def _is_contiguous_table(md: str, first: str, last: str) -> bool:
    lines = md.splitlines()
    block = lines[lines.index(first) : lines.index(last) + 1]
    return all(line.startswith("|") for line in block)


def test_issue_example_table_split_across_pages_becomes_one_gfm_table():
    """이슈 본문의 실제 산출물 재현 — 머리말·꼬리말과 `## Page 8`이 표 사이에 낀다."""
    border = "+----------------------------------------------+-------------------------------"
    pages = [
        _page(6, "앞 페이지 본문입니다."),
        _page(
            7,
            "The following table shows the default quotas most likely to constrain an enterprise",
            "deployment.",
            border,
            "| Quota (default, per project and region)      | Value                         ",
            border,
        ),
        _page(
            8,
            "| Query / StreamQuery requests                 | 90 per minute                 ",
            "| Session event appends                        | 300 per minute                ",
            border,
            "Quotas can be raised through the console.",
        ),
        _page(9, "다음 절 본문입니다."),
    ]

    md = _pdf_pages_to_markdown(pages)

    assert "+---" not in md
    header = "| Quota (default, per project and region) | Value |"
    last_row = "| Session event appends | 300 per minute |"
    assert _table_lines(md) == [
        header,
        "| --- | --- |",
        "| Query / StreamQuery requests | 90 per minute |",
        last_row,
    ]
    assert _is_contiguous_table(md, header, last_row)
    # 표는 7쪽 절에 붙고, 머리말·꼬리말과 8쪽 표지는 지워지지 않고 표 뒤에 남는다
    assert md.index("## Page 7") < md.index(header) < md.index(_footer(7)) < md.index("## Page 8")
    assert md.index("## Page 8") < md.index("Quotas can be raised")
    # 표 앞뒤가 빈 줄이어야 앞 문단에 붙지 않고 표로 렌더된다
    assert "deployment.\n\n| Quota" in md
    assert "300 per minute |\n\n" + HEADER in md


def test_table_spanning_three_pages_is_stitched_through_the_middle_page():
    border = "+-----+-----+"
    pages = [
        _page(1, "표 앞 문단", border, "| 키  | 값  |", border, "| a   | 1   |"),
        _page(2, "| b   | 2   |", "| c   | 3   |"),
        _page(3, "| d   | 4   |", border, "표 뒤 문단"),
    ]

    md = _pdf_pages_to_markdown(pages)

    assert _table_lines(md) == [
        "| 키 | 값 |",
        "| --- | --- |",
        "| a | 1 |",
        "| b | 2 |",
        "| c | 3 |",
        "| d | 4 |",
    ]
    assert _is_contiguous_table(md, "| 키 | 값 |", "| d | 4 |")
    assert md.index("| d | 4 |") < md.index("## Page 2") < md.index("## Page 3")
    assert md.index("## Page 3") < md.index("표 뒤 문단")


def test_tables_with_different_columns_are_not_stitched():
    pages = [
        _page(1, "+----+----+", "| a  | b  |", "+----+----+"),
        _page(2, "+----+----+----+", "| x  | y  | z  |", "+----+----+----+"),
        _page(3, "끝"),
    ]

    md = _pdf_pages_to_markdown(pages)

    assert md.index("| a | b |") < md.index("## Page 2") < md.index("| x | y | z |")


def test_blank_page_between_breaks_stitching():
    pages = [
        _page(1, "+----+----+", "| a  | b  |", "| c  | d  |"),
        _page(3, "| e  | f  |", "+----+----+"),  # 2쪽은 텍스트가 없어 빠졌다
        _page(4, "끝"),
    ]

    md = _pdf_pages_to_markdown(pages)

    assert md.index("## Page 3") < md.index("e | f")


def test_grid_table_rows_with_wrapped_cells_are_merged():
    lines = [
        "+--------+----------------+",
        "| 이름   | 설명           |",
        "+========+================+",
        "| alpha  | 첫 줄          |",
        "|        | 둘째 줄        |",
        "+--------+----------------+",
        "| beta   | 한 줄          |",
        "+--------+----------------+",
    ]

    md = _pdf_pages_to_markdown([(1, "\n".join(lines))])

    assert _table_lines(md) == [
        "| 이름 | 설명 |",
        "| --- | --- |",
        "| alpha | 첫 줄 둘째 줄 |",
        "| beta | 한 줄 |",
    ]


def test_unicode_box_drawing_table_is_converted():
    lines = [
        "┌──────┬──────┐",
        "│ 항목 │ 수량 │",
        "├──────┼──────┤",
        "│ 사과 │ 3    │",
        "└──────┴──────┘",
    ]

    md = _pdf_pages_to_markdown([(1, "\n".join(lines))])

    assert _table_lines(md) == ["| 항목 | 수량 |", "| --- | --- |", "| 사과 | 3 |"]
    assert "─" not in md


def test_decorative_rule_is_left_alone():
    """서명란·구분선 같은 장식용 가로줄은 표가 아니다 — 코드 펜스로 감싸지 않는다."""
    lines = ["그림:", "+-----------+", "+-----------+"]

    md = _pdf_pages_to_markdown([(1, "\n".join(lines))])

    assert md == "## Page 1\n\n그림:\n+-----------+\n+-----------+"


def test_box_with_only_empty_cells_falls_back_to_code_fence():
    """행은 있는데 셀이 전부 비었으면 표로 되살릴 수 없다 — 정렬만 유지한다."""
    lines = ["+-----+-----+", "|     |     |", "+-----+-----+"]

    md = _pdf_pages_to_markdown([(1, "\n".join(lines))])

    assert "```\n+-----+-----+" in md


def test_separate_tables_on_consecutive_pages_are_not_merged():
    """페이지마다 완결된 표가 하나씩 있는 부록 — 이어 붙이면 표가 뒤섞이고
    그 페이지 절이 통째로 사라진다. 이어짐은 다음 페이지가 **행**으로 시작할 때만이다."""
    border = "+------+-----------+"

    def table(no: int, head: tuple[str, str], row: tuple[str, str]) -> tuple[int, str]:
        return _raw(
            no,
            border,
            f"| {head[0]} | {head[1]} |",
            border,
            f"| {row[0]} | {row[1]} |",
            border,
        )

    pages = [
        _raw(1, "부록 A — 참조표"),
        table(2, ("코드", "의미"), ("E01", "시간초과")),
        table(3, ("지역", "엔드포인트"), ("kr", "kr.api")),
        table(4, ("요금", "상한"), ("free", "10")),
    ]

    md = _pdf_pages_to_markdown(pages)

    for page in ("## Page 2", "## Page 3", "## Page 4"):
        assert page in md  # 절이 사라지지 않는다
    # 각 표의 첫 행은 그 표의 헤더로 남는다
    assert "| 코드 | 의미 |\n| --- | --- |\n| E01 | 시간초과 |" in md
    assert "| 지역 | 엔드포인트 |\n| --- | --- |\n| kr | kr.api |" in md
    assert md.index("| 코드 | 의미 |") < md.index("## Page 3") < md.index("| 지역 | 엔드포인트 |")


def test_unstitchable_continuation_keeps_its_rows_as_data():
    """이을 수 없는 조각(2쪽 문서라 머리말 판정 불가)이라도 데이터 행을 헤더로
    승격시키지 않는다 — 행 하나가 사라진다."""
    border = "+------+-------+"
    pages = [
        _raw(1, "할당량:", border, "| 이름 | 값    |", border, "| a    | 1     |", "Acme Inc. 1"),
        _raw(2, "| b    | 2     |", "| c    | 3     |", border, "Acme Inc. 2"),
    ]

    md = _pdf_pages_to_markdown(pages)

    page2 = md.split("## Page 2")[1]
    assert "| b | 2 |" in page2 and "| c | 3 |" in page2
    assert "|  |  |\n| --- | --- |" in page2  # 빈 헤더를 세워 데이터를 지킨다


def test_header_footer_inside_a_table_on_one_page_is_pulled_out():
    """페이지 경계가 아니라 한 페이지 안에서 표 중간에 머리말·꼬리말이 끼는 경우."""
    head = "26. 9. 1. Where Agents Live, Part 3 | Medium"

    def foot(no: int) -> str:
        return f"https://medium.com/@x/where-agents-live {no}/4"

    border = "+-------+--------+"
    pages = [
        _raw(1, head, "본문 1", foot(1)),
        _raw(
            2,
            "할당량 표:",
            border,
            "| 항목  | 값     |",
            border,
            "| 질의  | 90/분  |",
            head,
            foot(2),
            "| 세션  | 300/분 |",
            "| 벡터  | 10/초  |",
        ),
        _raw(3, head, "본문 3", foot(3)),
        _raw(4, head, "본문 4", foot(4)),
    ]

    md = _pdf_pages_to_markdown(pages)

    page2 = md.split("## Page 2")[1]
    assert [line for line in page2.splitlines() if line.startswith("|")] == [
        "| 항목 | 값 |",
        "| --- | --- |",
        "| 질의 | 90/분 |",
        "| 세션 | 300/분 |",
        "| 벡터 | 10/초 |",
    ]
    assert _is_contiguous_table(md, "| 항목 | 값 |", "| 벡터 | 10/초 |")
    assert head in page2  # 지우지 않고 표 뒤에 남긴다


def test_numbered_body_headings_are_not_treated_as_running_headers():
    """"제 11 조"처럼 숫자만 다른 짧은 본문 제목을 머리말로 보면 본문이 표에 묻힌다."""
    border = "+------+------+"
    pages = [
        _raw(
            1,
            "제 11 조",
            "요금:",
            border,
            "| 항목 | 금액 |",
            border,
            "| 설치 | 100  |",
            border,
        ),
        _raw(2, "제 12 조", border, "| 위약 | 금액 |", border, "| 지연 | 50   |", border),
        _raw(3, "제 13 조", "본문"),
        _raw(4, "제 14 조", "본문"),
    ]

    md = _pdf_pages_to_markdown(pages)

    assert "제 12 조" in md
    assert md.index("| 설치 | 100 |") < md.index("제 12 조") < md.index("| 위약 | 금액 |")


def test_existing_gfm_table_and_plain_pages_are_left_as_is():
    gfm = "| a | b |\n|---|---|\n| 1 | 2 |"
    pages = [(1, "본문 첫 줄\n\n\n본문 둘째 줄"), (2, gfm)]

    md = _pdf_pages_to_markdown(pages)

    assert md == f"## Page 1\n\n본문 첫 줄\n\n\n본문 둘째 줄\n\n## Page 2\n\n{gfm}"


def test_gfm_table_escapes_pipes_flattens_newlines_and_pads():
    table = _gfm_table([["열 | A", "열B"], ["", ""], ["첫 줄\n둘째 줄"]])

    assert table == "| 열 \\| A | 열B |\n| --- | --- |\n| 첫 줄 둘째 줄 |  |"


def test_gfm_table_none_when_all_rows_empty():
    assert _gfm_table([["", " "], []]) is None


def test_parse_pdf_applies_table_restoration(monkeypatch):
    pypdf = pytest.importorskip("pypdf")
    from biznotes_core.services.ingestion.parsers import parse_pdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    text = "\n".join(
        [
            "표 앞 문단입니다. 충분히 긴 텍스트를 둡니다.",
            "+-----+-----+",
            "| k   | v   |",
            "+-----+-----+",
        ]
    )
    monkeypatch.setattr("pypdf._page.PageObject.extract_text", lambda self: text)

    res = parse_pdf(buf.getvalue())

    assert "| k | v |\n| --- | --- |" in res.markdown
    assert res.markdown.startswith("## Page 1\n\n")


def test_parse_docx_emits_gfm_table_in_document_order():
    docx = pytest.importorskip("docx")
    from biznotes_core.services.ingestion.parsers import parse_file

    doc = docx.Document()
    doc.add_paragraph("표 앞 문단")
    table = doc.add_table(rows=2, cols=2)
    for r, row in enumerate([["이름", "값"], ["a|b", "1"]]):
        for c, text in enumerate(row):
            table.cell(r, c).text = text
    doc.add_paragraph("표 뒤 문단")
    buf = io.BytesIO()
    doc.save(buf)

    md = parse_file(
        buf.getvalue(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ).markdown

    assert "| 이름 | 값 |\n| --- | --- |\n| a\\|b | 1 |" in md
    assert md.index("표 앞 문단") < md.index("| 이름 |") < md.index("표 뒤 문단")


def test_parse_pptx_includes_tables():
    pptx = pytest.importorskip("pptx")
    from pptx.util import Inches

    from biznotes_core.services.ingestion.parsers import parse_file

    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "분기 실적"
    shape = slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1))
    for r, row in enumerate([["분기", "매출"], ["Q1", "100"]]):
        for c, text in enumerate(row):
            shape.table.cell(r, c).text = text
    buf = io.BytesIO()
    prs.save(buf)

    md = parse_file(
        buf.getvalue(),
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ).markdown

    assert "분기 실적" in md
    assert "| 분기 | 매출 |\n| --- | --- |\n| Q1 | 100 |" in md
