"""구조 인식 청킹 — markdown 헤딩 경계 보존, 토큰 기반 분할.

`locations`를 주면 청크마다 원문 위치(쪽·표)를 `meta`에 싣는다(계획 A §3.3.1). 청크 텍스트는
원문의 부분 문자열이 아니다(헤딩 marker 제거·섹션 결합·encode→decode 왕복). 그래서 역탐색하지
않고 원문 오프셋을 조각 목록으로 끝까지 들고 간다. `locations=None`이면
`seq`·`text`·`token_count`·`meta`가 이 기능 이전과 바이트 동일하다.
"""

import itertools
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field

import tiktoken

from biznotes_core.services.ingestion.parsers import Location

DEFAULT_CHUNK_TOKENS = 800
DEFAULT_OVERLAP_TOKENS = 100

_enc = tiktoken.get_encoding("cl100k_base")

Span = tuple[int, int]
_COORDS = ("page", "section", "paragraph", "table_index")


def count_tokens(text: str) -> int:
    return len(_enc.encode(text, disallowed_special=()))


@dataclass
class Chunk:
    seq: int
    text: str
    token_count: int
    meta: dict = field(default_factory=dict)
    # 원문 markdown 좌표 `[start, end)` 목록 — 창이 원문과 정확히 대응하는 부분만 담는다
    # (합성 구분자 `"\n\n"`·문자 중간에서 잘린 바이트는 빠진다). 벡터 payload에는 싣지 않는다.
    spans: list[Span] = field(default_factory=list)


@dataclass
class _Section:
    heading: str
    text: str
    heading_span: Span | None
    text_span: Span


@dataclass
class _Piece:
    """버퍼 구간 `[start, end)`이 원문 `[md_start, md_end)`에 대응한다(합성 조각은 목록에 없다)."""

    start: int
    end: int
    md_start: int
    md_end: int


def _split_by_headings(markdown: str) -> list[_Section]:
    """헤딩 경계로 나눈 섹션 목록. 헤딩이 없으면 전체 1개.

    줄 오프셋은 `splitlines(keepends=True)`의 누적 길이로 센다 — 줄 구분자 종류와 무관하게
    원문 좌표다. 줄 구분자가 `\\n`뿐인 입력(HWP 조립 결과)에서는 `markdown[s:e] == text`가
    정확히 성립한다.
    """
    sections: list[_Section] = []
    heading = ""
    heading_span: Span | None = None
    lines: list[str] = []
    body_start = body_end = 0
    pos = 0

    def close() -> None:
        if not lines:
            return
        text = "\n".join(lines).strip()
        if not text:
            return
        raw = markdown[body_start:body_end]
        lead = len(raw) - len(raw.lstrip())
        text_span = (body_start + lead, body_start + len(raw.rstrip()))
        sections.append(_Section(heading, text, heading_span, text_span))

    for raw_line in markdown.splitlines(keepends=True):
        line = raw_line.splitlines()[0]
        if line.startswith("#"):
            close()
            rest = line.lstrip("#")
            heading = rest.strip()
            start = pos + (len(line) - len(rest)) + (len(rest) - len(rest.lstrip()))
            heading_span = (start, start + len(heading)) if heading else None
            lines = []
        else:
            if not lines:
                body_start = pos
            lines.append(line)
            body_end = pos + len(line)
        pos += len(raw_line)
    close()
    return sections


def _char_range(b: Sequence[int], cb: Sequence[int], start: int, end: int) -> Span | None:
    """토큰 창 `[start, end)`의 바이트 구간 안에 **완전히** 들어가는 문자 범위.

    `b`는 토큰 바이트 시작표(`b[i]` = i번째 토큰의 첫 바이트), `cb`는 문자 바이트 경계표
    (`cb[k]` = k번째 문자의 첫 바이트)다. 창 가장자리의 잘린 바이트(각각 4바이트 미만)는
    `decode`에서 U+FFFD가 되는 합성 조각이라 원문 범위에 넣지 않는다.
    """
    ci = bisect_left(cb, b[start])
    cj = bisect_right(cb, b[end]) - 1
    return (ci, cj) if ci < cj else None


def _split_tokens(text: str, max_tokens: int, overlap: int) -> list[tuple[str, Span | None]]:
    """창 텍스트와 그 창의 `text` 좌표 문자 범위. 창 텍스트는 `decode(window)` 그대로다."""
    tokens = _enc.encode(text, disallowed_special=())
    if len(tokens) <= max_tokens:
        return [(text, (0, len(text)) if text else None)]
    b = [0, *itertools.accumulate(len(t) for t in _enc.decode_tokens_bytes(tokens))]
    cb = [0, *itertools.accumulate(len(ch.encode("utf-8", "surrogatepass")) for ch in text)]
    # 두 표가 같은 바이트열의 좌표일 때만 범위를 낸다(tiktoken이 입력을 고쳐 쓴 경우 등은 범위 없음)
    aligned = b[-1] == cb[-1]
    parts: list[tuple[str, Span | None]] = []
    step = max_tokens - overlap
    for start in range(0, len(tokens), step):
        window = tokens[start : start + max_tokens]
        end = start + len(window)
        parts.append((_enc.decode(window), _char_range(b, cb, start, end) if aligned else None))
        if start + max_tokens >= len(tokens):
            break
    return parts


def _map_spans(pieces: list[_Piece], start: int, end: int) -> list[Span]:
    """버퍼 구간 → 원문 구간 목록(`start` 오름차순, 맞닿은 구간은 병합)."""
    out: list[Span] = []
    for piece in pieces:
        lo, hi = max(start, piece.start), min(end, piece.end)
        if lo >= hi:
            continue
        # 조각 끝까지 덮으면 원문 끝까지다 — 조각과 원문 길이가 다른 입력(`\r\n` 등)에서도 온전하다.
        # 조각 안쪽 경계는 선형 사상이라 그런 입력에서는 근사값이다(HWP 조립 결과는 정확).
        s = piece.md_start + (lo - piece.start)
        e = piece.md_end if hi == piece.end else min(s + (hi - lo), piece.md_end)
        if s >= e:
            continue
        if out and out[-1][1] == s:
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def _check_locations(locations: Sequence[Location]) -> None:
    prev_end = None
    for loc in locations:
        if loc.start > loc.end or (prev_end is not None and loc.start < prev_end):
            raise ValueError("locations는 start 오름차순이고 서로 겹치지 않아야 합니다")
        prev_end = loc.end


def _location_meta(locations: Sequence[Location], ends: list[int], spans: list[Span]) -> dict:
    """spans와 비어 있지 않은 교집합을 갖는 Location들 → meta 위치 키(원문 등장 순서)."""
    hit: set[int] = set()
    for s, e in spans:
        # ends는 비중첩 불변식 덕에 오름차순이다 — end > s인 첫 Location부터 start < e인 동안
        k = bisect_right(ends, s)
        while k < len(locations) and locations[k].start < e:
            if max(locations[k].start, s) < min(locations[k].end, e):
                hit.add(k)
            k += 1
    coords: list[dict[str, int]] = []
    for k in sorted(hit):
        loc = locations[k]
        d = {name: v for name in _COORDS if (v := getattr(loc, name)) is not None}
        if d and (not coords or coords[-1] != d):
            coords.append(d)
    if not coords:
        return {}
    meta: dict = {"locations": coords}
    pages = [d["page"] for d in coords if "page" in d]
    if pages:
        meta["page"] = pages[0]
        meta["pages"] = sorted(set(pages))
    table = next((d for d in coords if "table_index" in d), None)
    if table is not None:
        for name in ("table_index", "section", "paragraph"):
            if name in table:
                meta[name] = table[name]
    return meta


def chunk_markdown(
    markdown: str,
    max_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap: int = DEFAULT_OVERLAP_TOKENS,
    *,
    locations: Sequence[Location] | None = None,
) -> list[Chunk]:
    """markdown → 청크.

    `locations`(원문 `markdown` 좌표, `start` 오름차순·비중첩)를 주면 청크마다 교집합이 있는 위치를
    `meta["locations"]`·`page`·`pages`·`table_index`·`section`·`paragraph`로 싣는다.
    """
    ends: list[int] = []
    if locations is not None:
        _check_locations(locations)
        ends = [loc.end for loc in locations]
    chunks: list[Chunk] = []
    seq = 0
    buffer = ""
    pieces: list[_Piece] = []
    buffer_heading = ""

    def flush() -> None:
        nonlocal seq, buffer, pieces
        if not buffer.strip():
            buffer = ""
            pieces = []
            return
        lead = len(buffer) - len(buffer.lstrip())
        for part, rng in _split_tokens(buffer.strip(), max_tokens, overlap):
            spans = _map_spans(pieces, rng[0] + lead, rng[1] + lead) if rng else []
            nonlocal_meta = {"heading": buffer_heading} if buffer_heading else {}
            if locations is not None:
                nonlocal_meta.update(_location_meta(locations, ends, spans))
            chunks.append(
                Chunk(
                    seq=seq,
                    text=part,
                    token_count=count_tokens(part),
                    meta=nonlocal_meta,
                    spans=spans,
                )
            )
            seq += 1
        buffer = ""
        pieces = []

    for sec in _split_by_headings(markdown):
        # heading·text는 이미 strip돼 있어 아래 strip()은 아무것도 깎지 않는다 — 조각 좌표가 맞는다
        section_text = (f"{sec.heading}\n\n{sec.text}" if sec.heading else sec.text).strip()
        # (섹션 안 시작 오프셋, 버퍼 길이, 원문 span) — 합성 구분자 "\n\n"은 조각이 아니다
        section_pieces: list[tuple[int, int, Span | None]] = []
        if sec.heading:
            section_pieces.append((0, len(sec.heading), sec.heading_span))
            section_pieces.append((len(sec.heading) + 2, len(sec.text), sec.text_span))
        else:
            section_pieces.append((0, len(sec.text), sec.text_span))
        candidate = f"{buffer}\n\n{section_text}" if buffer else section_text
        if count_tokens(candidate) <= max_tokens:
            base = len(buffer) + 2 if buffer else 0
            buffer = candidate
            buffer_heading = buffer_heading or sec.heading
        else:
            flush()
            base = 0
            buffer = section_text
            buffer_heading = sec.heading
        for offset, length, span in section_pieces:
            if span is not None:
                pieces.append(_Piece(base + offset, base + offset + length, span[0], span[1]))
    flush()
    return chunks
