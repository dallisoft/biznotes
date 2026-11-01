"""소스 타입별 파서 — 원본 bytes → markdown 텍스트.

경량 파서만 쓴다(pypdf / python-docx / python-pptx / openpyxl / trafilatura).
글자 층이 없는 스캔(이미지) PDF는 OCR하지 않고 거부한다.
"""

import io
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from biznotes_core.services.ingestion import source_warnings


@dataclass(frozen=True)
class Location:
    """조립된 markdown의 문자 구간 `[start, end)`와 그 원문 좌표.

    `page`는 1 기준(`## Page N`과 같은 값)이고, `section`·`paragraph`·`table_index`는
    원문 좌표(0 기준)다. 없는 좌표는 `None`이다.
    """

    start: int
    end: int
    page: int | None = None
    section: int | None = None
    paragraph: int | None = None
    table_index: int | None = None


@dataclass
class ParseResult:
    markdown: str
    title: str | None = None  # 파서가 더 나은 제목을 찾으면 설정
    # 이하 영속화하지 않는다 — 문자열 경고는 로그용이다. 저장할 경고는 source_warnings에 담는다
    warnings: list[str] = field(default_factory=list)
    page_count: int | None = None
    # 청커가 청크 meta에 쪽·표 위치를 싣는 통로(계획 A §3.3.1). 지금 채우는 파서는 없다
    locations: list[Location] = field(default_factory=list)
    # sources.warnings로 저장하는 구조화 경고(HWP-03) — `source_warnings.warning()` 항목
    source_warnings: list[dict[str, Any]] = field(default_factory=list)


class ParseError(Exception):
    pass


def replace_nul(text: str) -> str:
    """U+0000을 공백으로 바꾼다 — PostgreSQL text가 거부한다(#462).

    지우지 않고 같은 자리를 바꿔 길이를 지킨다 — `locations` 오프셋이 그대로 맞는다.
    한컴 폰트 PDF는 ToUnicode가 공백 글리프를 U+0000에 매핑해 단어 경계도 남는다.
    """
    return text.replace("\x00", " ")


# 글자 층이 없는 PDF(스캔 이미지)의 안내 — OCR을 하지 않으므로 업로드 전에 텍스트 PDF로 바꿔야 한다
SCANNED_PDF_MESSAGE = (
    "PDF에서 텍스트를 추출할 수 없습니다 — 스캔(이미지) PDF는 지원하지 않습니다. "
    "텍스트가 들어 있는 PDF로 다시 업로드하세요"
)


def _gfm_table(rows: list[list[str]], *, has_header: bool = True) -> str | None:
    """셀 목록 → GFM 표. 기본은 첫 행을 헤더로 쓴다(GFM 표는 헤더 행이 필수).

    `has_header=False`면 빈 헤더를 세우고 모든 행을 데이터로 남긴다 — 앞 페이지에서
    이어진 조각이라 헤더가 없을 때 쓴다. 셀 안의 줄바꿈은 공백으로, `|`는
    이스케이프한다(둘 다 행 구조를 깨뜨린다). 내용이 전부 빈 행은 버리고, 남는 행이
    없으면 None.
    """
    cleaned = [
        [" ".join(c.split()).replace("|", "\\|") for c in row]
        for row in rows
        if any(c.strip() for c in row)
    ]
    if not cleaned:
        return None
    ncols = max(len(r) for r in cleaned)
    lines = ["| " + " | ".join(r + [""] * (ncols - len(r))) + " |" for r in cleaned]
    if not has_header:
        lines.insert(0, "| " + " | ".join([""] * ncols) + " |")
    lines.insert(1, "|" + " --- |" * ncols)
    return "\n".join(lines)


# ── PDF 텍스트 레이어의 박스 표 복원 (#3) ────────────────────────────────
# pypdf는 표 구조를 모르고 글자 배치만 옮긴다. 원문이 ASCII/유니코드 박스 표
# (`+----+----+` / `| a | b |`)면 그 그림이 그대로 나와 어떤 렌더러에서도 표가
# 되지 않고, 페이지 경계에서는 머리말·꼬리말과 `## Page N`이 표 한가운데 끼인다.
_BOX_VERTICAL = "|│┃║"
_BOX_JUNCTION = "+┌┬┐├┼┤└┴┘┏┳┓┣╋┫┗┻┛╔╦╗╠╬╣╚╩╝╒╤╕╞╪╡╘╧╛╓╥╖╟╫╢╙╨╜"
_BOX_HORIZONTAL = "-=─━═"
_BOX_BORDER_RE = re.compile(
    f"[{re.escape(_BOX_JUNCTION)}][{re.escape(_BOX_JUNCTION + _BOX_HORIZONTAL)}: ]*"
)
# 머리말·꼬리말 후보로 볼 페이지 앞뒤 줄 수와, 반복으로 인정할 최소 페이지 수
_RUNNING_EDGE_LINES = 3
_RUNNING_MIN_PAGES = 3


def _is_box_border(line: str) -> bool:
    s = line.strip()
    return (
        _BOX_BORDER_RE.fullmatch(s) is not None
        and sum(s.count(h) for h in _BOX_HORIZONTAL) >= 3
    )


def _is_box_row(line: str) -> bool:
    s = line.strip()
    return len(s) > 1 and s[0] in _BOX_VERTICAL


def _is_table_line(line: str) -> bool:
    return _is_box_border(line) or _is_box_row(line)


def _box_row_cells(line: str) -> list[str]:
    s = line.strip()
    for v in _BOX_VERTICAL[1:]:
        s = s.replace(v, "|")
    s = s[1:]
    if s.endswith("|"):  # 오른쪽이 잘린 행(페이지 폭 초과)은 닫는 기호가 없다
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _box_block_to_markdown(block: list[str]) -> list[str] | None:
    """연속된 표 줄 한 덩어리 → GFM 표 줄. 박스 표가 아니면 None."""
    if not any(_is_box_border(line) for line in block):
        return None  # 테두리가 없으면 이미 GFM이거나 `|`로 시작하는 본문이다
    if not any(_is_box_row(line) for line in block):
        return None  # 글자 없는 테두리 줄(문서의 구분선 등)은 표가 아니다
    segments: list[list[str]] = [[]]
    for line in block:
        if _is_box_border(line):
            segments.append([])
        else:
            segments[-1].append(line)
    segments = [seg for seg in segments if seg]
    if len(segments) >= 3:
        # 행마다 테두리가 있는 격자 표 — 테두리 사이 여러 줄은 한 행의 줄바꿈된 셀이다
        rows = []
        for seg in segments:
            split = [_box_row_cells(line) for line in seg]
            ncols = max(len(cells) for cells in split)
            rows.append(
                [
                    " ".join(cells[i] for cells in split if i < len(cells) and cells[i])
                    for i in range(ncols)
                ]
            )
    else:
        rows = [_box_row_cells(line) for seg in segments for line in seg]
    # 위 테두리 없이 행부터 시작하면 앞 페이지에서 이어진 조각이다(잇지 못한 경우).
    # 첫 행을 헤더로 쓰면 데이터 한 줄이 사라지므로 빈 헤더를 세운다.
    table = _gfm_table(rows, has_header=not _is_box_row(block[0]))
    if table is None:
        # 표로 되살릴 수 없는 박스(셀이 전부 빈 그림 등) — 정렬이라도 유지한다
        return ["```", *block, "```"]
    return table.split("\n")


def _running_line_key(line: str) -> str:
    return re.sub(r"\d+", "#", " ".join(line.split()))


@dataclass(frozen=True)
class _RunningLines:
    """페이지마다 반복되는 머리말·꼬리말."""

    exact: frozenset[str]  # 글자까지 똑같이 반복되는 줄
    numbered: frozenset[str]  # 숫자만 달라지며 반복되는 줄의 정규화 키

    def matches(self, line: str) -> bool:
        collapsed = " ".join(line.split())
        return collapsed in self.exact or _running_line_key(collapsed) in self.numbered


def _number_may_vary(line: str) -> bool:
    """숫자만 다른 반복을 같은 줄로 봐도 되는가.

    "7/34" 같은 쪽 번호(글자가 거의 없음)나 "26. 9. 1. … | Medium" 같은 긴 머리말은
    숫자가 달라도 같은 줄이다. 반면 "제 11 조"·"Article 12"처럼 짧은 본문 제목은
    숫자가 바뀌면 다른 내용이다 — 그런 줄까지 머리말로 보면 본문을 잃는다.
    """
    letters = sum(1 for c in line if c.isalpha())
    return letters <= 3 or letters >= 20


def _running_lines(pages: list[list[str]]) -> _RunningLines:
    """여러 페이지의 앞뒤에 반복되는 줄을 찾는다.

    표 줄은 후보에서 뺀다 — 매 페이지 표가 있으면 테두리 줄이 반복돼 머리말로 오인된다.
    """
    if len(pages) < _RUNNING_MIN_PAGES:
        return _RunningLines(frozenset(), frozenset())
    exact: Counter[str] = Counter()
    numbered: Counter[str] = Counter()
    for lines in pages:
        content = [line for line in lines if line.strip() and not _is_table_line(line)]
        edge = content[:_RUNNING_EDGE_LINES] + content[-_RUNNING_EDGE_LINES:]
        collapsed = {" ".join(line.split()) for line in edge}
        exact.update(collapsed)
        numbered.update({_running_line_key(line) for line in collapsed})
    threshold = max(_RUNNING_MIN_PAGES, math.ceil(len(pages) / 2))
    return _RunningLines(
        exact=frozenset(key for key, n in exact.items() if n >= threshold),
        numbered=frozenset(
            key for key, n in numbered.items() if n >= threshold and _number_may_vary(key)
        ),
    )


def _table_block(
    lines: list[str], start: int, running: _RunningLines
) -> tuple[list[str], list[str], int]:
    """`start`에서 시작하는 표 줄 덩어리 → (표 줄, 사이에 낀 머리말·꼬리말, 다음 위치).

    표 한가운데 끼어든 머리말·꼬리말은 표 밖으로 빼낸다 — 페이지 경계뿐 아니라
    한 페이지 안에서도 일어난다(이슈 #3의 "표 중간에 머리말 삽입").
    """
    block: list[str] = []
    pulled: list[str] = []
    i = start
    while i < len(lines):
        if _is_table_line(lines[i]):
            block.append(lines[i])
            i += 1
            continue
        # 표 줄이 아니면, 뒤에 표가 이어지는 "낀 줄"인지 본다
        j = i
        while j < len(lines) and (not lines[j].strip() or running.matches(lines[j])):
            j += 1
        if j > i and j < len(lines) and _is_table_line(lines[j]):
            pulled.extend(line for line in lines[i:j] if line.strip())
            i = j
            continue
        break
    return block, pulled, i


def _stitch_page_tables(pages: list[tuple[int, list[str]]], running: _RunningLines) -> None:
    """페이지 경계에서 끊긴 표를 앞 페이지 표 끝으로 끌어올린다 (제자리 수정).

    앞 페이지가 표 줄로 끝나고 다음 페이지가 **표의 행**으로 시작하며 열 수가 같으면
    같은 표로 본다. 다음 페이지가 위 테두리로 시작하면 새 표다 — 그렇지 않으면 한
    페이지에 하나씩 있는 독립된 표들이 통째로 합쳐지고 그 페이지 절이 사라진다.
    사이에 낀 머리말·꼬리말과 `## Page N`은 지우지 않고 표 뒤에 남는다. 표가 여러
    페이지에 걸치면 계속 이어 붙인다.
    """

    def content_idx(lines: list[str]) -> list[int]:
        return [
            i for i, line in enumerate(lines) if line.strip() and not running.matches(line)
        ]

    def ncols(lines: list[str]) -> int | None:
        rows = [line for line in lines if _is_box_row(line)]
        return len(_box_row_cells(rows[-1])) if rows else None

    owner: int | None = None  # 열린 표를 끝에 품고 있는 페이지
    for j in range(1, len(pages)):
        prev = owner if owner is not None else j - 1
        owner = None
        if pages[j][0] != pages[j - 1][0] + 1:
            continue  # 사이에 빈(텍스트 없는) 페이지가 있으면 연속된 표가 아니다
        prev_lines, cur_lines = pages[prev][1], pages[j][1]
        pi, ci = content_idx(prev_lines), content_idx(cur_lines)
        if not pi or not ci:
            continue
        if not (_is_table_line(prev_lines[pi[-1]]) and _is_box_row(cur_lines[ci[0]])):
            continue
        block, _pulled, end = _table_block(cur_lines, ci[0], running)
        head = prev_lines[: pi[-1] + 1]
        prev_cols, cur_cols = ncols(head), ncols(block)
        if prev_cols is not None and cur_cols is not None and prev_cols != cur_cols:
            continue  # 열 구조가 다르면 별개의 표다
        del cur_lines[ci[0] : end]
        prev_lines[pi[-1] + 1 : pi[-1] + 1] = block
        if not content_idx(cur_lines):
            owner = prev  # 이 페이지가 표로만 채워져 있었다 — 다음 페이지로 이어질 수 있다


def _pdf_pages_to_markdown(pages: list[tuple[int, str]]) -> str:
    """(페이지 번호, 추출 텍스트) 목록 → `## Page N` 절로 나눈 markdown."""
    split = [(no, text.split("\n")) for no, text in pages]
    running = _running_lines([lines for _, lines in split])
    _stitch_page_tables(split, running)
    sections: list[str] = []
    for no, lines in split:
        out: list[str] = []
        i = 0
        while i < len(lines):
            if not _is_table_line(lines[i]):
                out.append(lines[i])
                i += 1
                continue
            block, pulled, j = _table_block(lines, i, running)
            converted = _box_block_to_markdown(block)
            if converted is None:
                out.extend(block)
                out.extend(pulled)
            else:
                # 표는 앞뒤가 빈 줄이어야 문단에 붙지 않고 표로 렌더된다
                if out and out[-1].strip():
                    out.append("")
                out.extend(converted)
                out.append("")
                out.extend(pulled)
                if pulled:
                    out.append("")
            i = j
        body = "\n".join(out).strip()
        if body:
            sections.append(f"## Page {no}\n\n{body}")
    return "\n\n".join(sections)


def parse_pdf(data: bytes) -> ParseResult:
    """pypdf 파싱. 텍스트가 없는 쪽은 건너뛰고, 모든 쪽이 비면(스캔 PDF) `ParseError`다.

    `reader.is_encrypted`는 owner-password만 걸린(사용자 암호 없이 바로 열리는)
    PDF도 True를 반환하며 `decrypt()` 이후에도 계속 True로 남는다(pypdf 자체
    문서화된 동작) — 이 플래그만으로 암호화 여부를 판정하면 실제로는 읽을 수
    있는 PDF까지 거부하게 된다. 대신 페이지 텍스트 추출을 직접 시도해
    `FileNotDecryptedError`(진짜 사용자 암호가 걸려 복호화가 안 된 경우에만
    발생)가 나는지로 판정한다. 그 외의 `PdfReadError`(손상된 파일 등)는
    별도의 "손상됨" 메시지로 구분한다."""
    from pypdf import PdfReader
    from pypdf.errors import FileNotDecryptedError, PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        pages: list[tuple[int, str]] = []
        for i, page in enumerate(reader.pages, start=1):
            # 공백 자리의 U+0000은 쪽 텍스트에서 바로 공백으로 — 머리말·표 판정도 공백을 본다
            text = replace_nul(page.extract_text() or "").strip()
            if text:
                pages.append((i, text))
        title = None
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title)
    except FileNotDecryptedError as e:
        raise ParseError(
            "암호화된 PDF는 처리할 수 없습니다 — 암호를 해제한 뒤 다시 업로드하세요"
        ) from e
    except PdfReadError as e:
        raise ParseError(f"손상된 PDF 파일입니다: {e}") from e

    if not pages:
        raise ParseError(SCANNED_PDF_MESSAGE)
    return ParseResult(markdown=_pdf_pages_to_markdown(pages), title=title)


# OOXML 패키지 관계 — docProps/core.xml(문서 속성)을 가리킨다
_OOXML_CORE_PROPERTIES_REL = (
    "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
)


def _blank_to_none(title: str | None) -> str | None:
    """빈 값·공백뿐인 제목은 None이다. 한 줄 접기·기만 문자 제거는 워커가 저장 직전에
    `source_warnings`의 제목 정화로 한다(HWP-02) — 여기서 하지 않는다."""
    return title if title and title.strip() else None


def _ooxml_core_part(package: Any) -> Any | None:
    """DOCX·PPTX 패키지의 문서 속성(core properties) 파트. 없으면 None(HWP-20).

    python-docx·python-pptx의 `core_properties`는 문서에 core.xml이 없으면 기본 속성을
    만들어 붙이고 제목을 "Word Document"·"PowerPoint Presentation"으로 채운다 — 문서가
    가진 제목이 아니므로 관계로 직접 찾는다.
    """
    try:
        return package.part_related_by(_OOXML_CORE_PROPERTIES_REL)
    except KeyError:
        return None


def parse_docx(data: bytes) -> ParseResult:
    """DOCX → markdown. 표는 본문 순서대로 GFM 표로 낸다.

    `Document.iter_inner_content()`는 **python-docx 1.1 이상**이다. 소비 제품은
    1.2 이상을 선언하고 있고, 코어는 파서 라이브러리를 런타임 의존으로 선언하지
    않으므로(테스트용 dev 의존만) 이 하한을 여기 적어 둔다.
    """
    import docx
    from docx.table import Table

    doc = docx.Document(io.BytesIO(data))
    lines: list[str] = []
    # 본문 순서대로 순회한다 — 표를 문서 끝에 몰아 두면 표가 설명 문단과 떨어진다(#3)
    for item in doc.iter_inner_content():
        if isinstance(item, Table):
            table = _gfm_table([[c.text for c in row.cells] for row in item.rows])
            if table:
                lines.append(table)
            continue
        text = item.text.strip()
        if not text:
            continue
        style = (item.style.name or "").lower() if item.style else ""
        if style.startswith("heading"):
            level = re.search(r"(\d+)", style)
            lines.append("#" * min(int(level.group(1)) if level else 1, 6) + " " + text)
        else:
            lines.append(text)
    if not lines:
        raise ParseError("DOCX에서 텍스트를 추출하지 못했습니다")
    core = _ooxml_core_part(doc.part.package)
    title = _blank_to_none(core.core_properties.title) if core is not None else None
    return ParseResult(markdown="\n\n".join(lines), title=title)


def parse_pptx(data: bytes) -> ParseResult:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    slides: list[str] = []
    for i, slide in enumerate(prs.slides, start=1):
        texts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t:
                    texts.append(t)
            elif shape.has_table:  # 표는 텍스트 프레임이 없어 통째로 빠지던 경로(#3)
                table = _gfm_table(
                    [[cell.text for cell in row.cells] for row in shape.table.rows]
                )
                if table:
                    texts.append(table)
        if texts:
            slides.append(f"## Slide {i}\n\n" + "\n\n".join(texts))
    if not slides:
        raise ParseError("PPTX에서 텍스트를 추출하지 못했습니다")
    core = _ooxml_core_part(prs.part.package)  # python-pptx는 파트 자체가 속성이다
    title = _blank_to_none(core.title) if core is not None else None
    return ParseResult(markdown="\n\n".join(slides), title=title)


def parse_xlsx(data: bytes) -> ParseResult:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    MAX_ROWS = 2000  # 시트당 행 상한 — 초대형 시트로 워커 메모리 폭주 방지
    sections: list[str] = []
    truncated_sheets: list[int] = []
    for index, ws in enumerate(wb.worksheets, start=1):
        rows: list[list[str]] = []
        truncated = False
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= MAX_ROWS:
                truncated = True
                break
            cells = ["" if c is None else str(c) for c in row]
            if any(c.strip() for c in cells):
                rows.append(cells)
        if truncated:
            # 상한에 닿았다 — 서식만 남은 빈 행도 세므로 "잘렸을 수 있다"는 뜻이다(HWP-03).
            # 시트 이름은 문서 원문이라 싣지 않고 순번만 싣는다
            truncated_sheets.append(index)
        # 셀 안의 줄바꿈·파이프 처리는 공용 헬퍼가 한다 — 직접 조립하면 행이 깨진다
        table = _gfm_table(rows)
        if table is None:
            continue
        block = [f"## {ws.title}", "", table]
        if truncated:
            block.append(f"\n_({MAX_ROWS}행 초과분 생략)_")
        sections.append("\n".join(block))
    # read_only 모드도 docProps/core.xml을 읽는다. 없으면 기본값(제목 None)이다
    title = _blank_to_none(wb.properties.title)
    wb.close()
    if not sections:
        raise ParseError("XLSX에서 데이터를 추출하지 못했습니다")
    # 행 상한으로 잘린 시트를 경고로 남긴다(HWP-03) — 지금까지는 본문 주석뿐이었다
    return ParseResult(
        markdown="\n\n".join(sections),
        title=title,
        source_warnings=[
            source_warnings.warning(
                source_warnings.SPREADSHEET_ROWS_TRUNCATED, sheet_index=i, max_rows=MAX_ROWS
            )
            for i in truncated_sheets
        ],
    )


def parse_plain(data: bytes) -> ParseResult:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp949", errors="replace")
    if not text.strip():
        raise ParseError("빈 문서입니다")
    return ParseResult(markdown=text)


# 업로드 라우터가 admit_bytes를 먼저 부른다.
FILE_PARSERS = {
    "application/pdf": parse_pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": parse_docx,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": parse_pptx,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": parse_xlsx,
    "text/plain": parse_plain,
    "text/markdown": parse_plain,
    "text/csv": parse_plain,
}

EXT_MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
}


def parse_file(data: bytes, mime_type: str) -> ParseResult:
    parser = FILE_PARSERS.get(mime_type)
    if parser is None:
        raise ParseError(f"지원하지 않는 파일 형식: {mime_type}")
    return parser(data)


# 브라우저 User-Agent — 기본 UA는 Cloudflare 등에서 403으로 차단되는 경우가 많다.
_WEB_FETCH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def parse_web(url: str) -> ParseResult:
    """웹 소스 fetch + 본문 추출.

    R5-1: fetch는 반드시 SSRF 단일 관문 `ssrf_safe_fetch`를 통과한다 —
    trafilatura.fetch_url은 리다이렉트를 내부에서 삼켜 매 홉 재검증이 불가하므로
    직접 fetch(매 홉 SSRF 재검증)하고 trafilatura는 추출에만 쓴다.
    """
    import trafilatura

    from biznotes_core.config import get_settings
    from biznotes_core.core.ssrf import SSRFBlockedError, ssrf_safe_fetch

    try:
        resp = ssrf_safe_fetch(
            url,
            allow_private=get_settings().ssrf_allow_private,
            timeout=20.0,
            headers={"User-Agent": _WEB_FETCH_UA},
        )
    except SSRFBlockedError:
        raise
    except Exception as e:  # noqa: BLE001 — 네트워크 오류를 파싱 오류로 정규화
        # 사유를 문구에 남긴다 — 모든 실패가 같은 한 줄이면 타임아웃·DNS·디코딩·차단을
        # 구분할 수 없어 소스 failed의 원인을 알 수 없다(#30).
        reason = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        raise ParseError(f"URL을 가져오지 못했습니다: {url} ({reason})") from e

    if not resp.is_success:
        raise ParseError(f"URL을 가져오지 못했습니다: {url} (HTTP {resp.status_code})")
    if not resp.text:
        raise ParseError(f"응답 본문이 비어 있습니다: {url}")
    downloaded = resp.text
    md = trafilatura.extract(downloaded, output_format="markdown", include_tables=True)
    if not md or not md.strip():
        raise ParseError("본문을 추출하지 못했습니다")
    meta = trafilatura.extract_metadata(downloaded)
    title = meta.title if meta and meta.title else None
    return ParseResult(markdown=md, title=title)
