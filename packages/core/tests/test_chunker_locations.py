"""chunk_markdown(locations=...) — span 보존·다중 location 규칙·바이트 동일 회귀 (계획 A §3.3.1).

청커는 순수 함수다. 마지막 케이스만 워커 `_run`의 배선(ParseResult.locations 없음 → 청크 meta
불변)을 conftest의 RunHarness로 본다. biznotes에는 위치를 내는 파서가 없다(HWP 조립기를 뺐다).
"""

import itertools
import json
import pathlib
from bisect import bisect_left, bisect_right
from typing import Any

import pytest
import tiktoken

from biznotes_core.services.ingestion import chunker
from biznotes_core.services.ingestion.chunker import Chunk, _char_range, chunk_markdown
from biznotes_core.services.ingestion.parsers import Location, ParseResult

_enc = tiktoken.get_encoding("cl100k_base")
BASELINE = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "chunker_baseline.json").read_text("utf-8")
)
A29 = "가나다라마바사 한글 텍스트 테스트입니다"
_LOC_KEYS = {"locations", "page", "pages", "table_index", "section", "paragraph"}


def as_dicts(chunks: list[Chunk]) -> list[dict[str, Any]]:
    return [
        {"seq": c.seq, "text": c.text, "token_count": c.token_count, "meta": c.meta}
        for c in chunks
    ]


def tables(text: str) -> tuple[list[int], list[int]]:
    """토큰 바이트 시작표 B와 문자 바이트 경계표 CB(계획 §3.3.1-3)."""
    tokens = _enc.encode(text, disallowed_special=())
    b = [0, *itertools.accumulate(len(t) for t in _enc.decode_tokens_bytes(tokens))]
    cb = [0, *itertools.accumulate(len(ch.encode()) for ch in text)]
    return b, cb


def windows(n_tokens: int, max_tokens: int, overlap: int) -> list[tuple[int, int]]:
    out = []
    for start in range(0, n_tokens, max_tokens - overlap):
        out.append((start, min(start + max_tokens, n_tokens)))
        if start + max_tokens >= n_tokens:
            break
    return out


# ---------------------------------------------------------------------------
# locations=None — 이 기능 이전과 바이트 동일
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", BASELINE, ids=[c["id"] for c in BASELINE])
def test_no_locations_is_byte_identical_to_previous_chunker(case: dict[str, Any]) -> None:
    """fixture는 Phase 2 변경 전 청커(`7afb3d4`)의 출력을 그대로 고정한 것이다."""
    got = chunk_markdown(case["markdown"], max_tokens=case["max_tokens"], overlap=case["overlap"])
    assert as_dicts(got) == case["chunks"]
    assert all(not (_LOC_KEYS & c.meta.keys()) for c in got)


def test_baseline_covers_required_shapes() -> None:
    ids = {c["id"] for c in BASELINE}
    assert {"no_heading", "one_heading", "long_section_overlap", "crlf", "blank_lines",
            "korean_split", "endoftext"} <= ids
    assert "<|endoftext|>" in next(c for c in BASELINE if c["id"] == "endoftext")["markdown"]


@pytest.mark.parametrize("case", BASELINE, ids=[c["id"] for c in BASELINE])
def test_locations_do_not_change_text(case: dict[str, Any]) -> None:
    """위치를 줘도 seq·text·token_count는 같다 — meta에 위치 키만 더해진다."""
    md = case["markdown"]
    locs = [Location(0, len(md), page=1)]
    got = chunk_markdown(md, max_tokens=case["max_tokens"], overlap=case["overlap"],
                         locations=locs)
    plain = case["chunks"]
    assert [(c.seq, c.text, c.token_count) for c in got] == [
        (c["seq"], c["text"], c["token_count"]) for c in plain
    ]
    for c, p in zip(got, plain, strict=True):
        assert {k: v for k, v in c.meta.items() if k not in _LOC_KEYS} == p["meta"]


# ---------------------------------------------------------------------------
# Chunk.spans — 원문 슬라이스 대조
# ---------------------------------------------------------------------------


def test_spans_track_crlf_source_coordinates() -> None:
    """줄 구분자가 `\\r\\n`인 입력에서도 span은 원문 좌표 안에 있다(길이는 근사)."""
    md = "# 머리\r\n\r\n첫 줄\r\n둘째 줄"
    [chunk] = chunk_markdown(md)
    assert [md[s:e] for s, e in chunk.spans] == ["머리", "첫 줄\r\n둘째 줄"]


def test_multi_window_spans_match_exact_character_range() -> None:
    md = "## 긴 절\n\n" + "공문서 표본 문장입니다. " * 30
    chunks = chunk_markdown(md, max_tokens=20, overlap=5)
    assert len(chunks) > 3
    body_start = md.index("공문서")
    for c in chunks:
        assert c.spans
        assert all(md[s:e] in c.text for s, e in c.spans)  # 원문 조각은 창 텍스트 안에 그대로 있다
        assert all(s >= md.index("긴 절") for s, _ in c.spans)
    # 첫 창만 헤딩 조각을 갖는다 — 나머지는 본문 좌표 안이다
    assert all(s >= body_start for c in chunks[1:] for s, _ in c.spans)


# ---------------------------------------------------------------------------
# 문자 중간 토큰 경계 (§0 A29 · PLAN4-01·PLAN5-06·PLAN5-07)
# ---------------------------------------------------------------------------


def test_a29_char_range_helper() -> None:
    b, cb = tables(A29)
    tokens = _enc.encode(A29)
    assert _enc.decode(tokens[2:5]) == "다라\ufffd"
    assert _char_range(b, cb, 2, 5) == (2, 4) and A29[2:4] == "다라"
    assert _enc.decode(tokens[5:8]) == "\ufffd바"
    assert _char_range(b, cb, 5, 8) == (5, 6) and A29[5:6] == "바"
    assert _char_range(b, cb, 0, 3) == (0, 3)


def test_a29_windows_through_chunk_markdown() -> None:
    """`max_tokens=3, overlap=2`(보폭 1)이면 모든 창 `[a, a+3)`이 청크다 — `[2,5)`·`[5,8)` 포함.

    계획 문면의 `overlap=0`은 보폭이 3이라 `[2,5)`를 만들지 않는다(창 [0,3)·[3,6)·…).
    """
    chunks = chunk_markdown(A29, max_tokens=3, overlap=2)
    assert chunks[2].text == "다라\ufffd"
    assert [A29[s:e] for s, e in chunks[2].spans] == ["다라"]
    assert chunks[5].text == "\ufffd바"
    assert [A29[s:e] for s, e in chunks[5].spans] == ["바"]
    # 원문에 U+FFFD가 없을 때만 성립하는 짧은 형식(PLAN5-06)
    assert "\ufffd" not in A29
    for c in chunks:
        assert "".join(A29[s:e] for s, e in c.spans) == c.text.strip("\ufffd")


@pytest.mark.parametrize(
    ("text", "max_tokens", "overlap"),
    [
        (A29, 3, 0),
        (A29, 3, 2),
        ("가나\ufffd다라마바사 한글", 2, 1),  # 원문에 진짜 U+FFFD
        ("👨‍👩‍👧 가족 이모지와 한글 😀 끝", 3, 1),
        (("앞 <|endoftext|> 뒤 " * 3).strip(), 4, 1),  # 청커는 버퍼를 strip한 뒤 토큰화한다
    ],
    ids=["a29", "a29_step1", "real_fffd", "emoji_zwj", "endoftext"],
)
def test_general_window_invariant(text: str, max_tokens: int, overlap: int) -> None:
    """모든 창: text == pre + md[ci:cj] + suf (일반형) · B[a] ≤ CB[ci] · CB[cj] ≤ B[b] ·
    잘린 바이트 < 4."""
    b, cb = tables(text)
    raw = text.encode()
    wins = windows(len(b) - 1, max_tokens, overlap)
    chunks = chunk_markdown(text, max_tokens=max_tokens, overlap=overlap)
    assert len(chunks) == len(wins)
    for c, (a, z) in zip(chunks, wins, strict=True):
        ci = bisect_left(cb, b[a])
        cj = bisect_right(cb, b[z]) - 1
        pre, suf = raw[b[a] : cb[ci]], raw[cb[cj] : b[z]] if cj >= ci else b""
        if cj > ci:
            assert c.text == pre.decode("utf-8", "replace") + text[ci:cj] + suf.decode(
                "utf-8", "replace"
            )
            assert c.spans == [(ci, cj)]
            assert b[a] <= cb[ci] and cb[cj] <= b[z]
            assert len(pre) < 4 and len(suf) < 4
        else:
            assert c.spans == []


def test_real_replacement_char_is_inside_span() -> None:
    text = "가나\ufffd다라마바사 한글"
    chunks = chunk_markdown(text, max_tokens=3, overlap=0)
    covered = {i for c in chunks for s, e in c.spans for i in range(s, e)}
    assert text.index("\ufffd") in covered  # 원문의 진짜 U+FFFD는 합성 조각이 아니다


def test_endoftext_chunks_with_locations() -> None:
    md = "## 특수\n\n앞 <|endoftext|> 뒤 <|fim_prefix|> 끝 " * 4
    locs = [Location(0, len(md), page=1)]
    chunks = chunk_markdown(md, max_tokens=9, overlap=2, locations=locs)
    assert chunks and all(c.meta.get("page") == 1 for c in chunks if c.spans)


def test_fffd_fragment_does_not_pull_neighbour_location() -> None:
    """창 [2,5)는 '마'(두 번째 location)의 앞 바이트를 U+FFFD로 품지만 그 location을 갖지 않는다."""
    locs = [Location(0, 4, page=1), Location(4, len(A29), page=2)]
    chunks = chunk_markdown(A29, max_tokens=3, overlap=2, locations=locs)
    assert chunks[2].text == "다라\ufffd"
    assert chunks[2].meta == {"locations": [{"page": 1}], "page": 1, "pages": [1]}
    assert chunks[3].meta["pages"] == [1, 2]  # '라마' — 두 location에 실제로 걸친다


def test_window_inside_one_character_has_no_span() -> None:
    text = "마바"  # 두 글자 모두 토큰 두 개로 갈라진다
    assert len(_enc.encode(text)) == 4
    chunks = chunk_markdown(text, max_tokens=1, overlap=0, locations=[Location(0, 2, page=1)])
    assert [c.text for c in chunks] == ["\ufffd"] * 4
    assert all(c.spans == [] and not (_LOC_KEYS & c.meta.keys()) for c in chunks)


def test_decode_with_offsets_is_not_used() -> None:
    assert "decode_with_offsets" not in pathlib.Path(chunker.__file__).read_text("utf-8")


# ---------------------------------------------------------------------------
# 다중 location 규칙 (PLAN3-05)
# ---------------------------------------------------------------------------


def test_chunk_without_intersection_has_no_location_keys() -> None:
    md = "# 머리\n\n앞 절 본문\n\n# 다음\n\n위치가 있는 본문"
    start = md.index("위치가")
    locs = [Location(start, len(md), page=5)]
    chunks = chunk_markdown(md, max_tokens=8, overlap=0, locations=locs)
    first, last = chunks[0], chunks[-1]
    assert first.meta == {"heading": "머리"}
    assert last.meta["page"] == 5


def test_consecutive_identical_coordinates_are_merged() -> None:
    md = "가나다라 마바사"
    locs = [Location(0, 4, page=3), Location(4, len(md), page=3)]
    [chunk] = chunk_markdown(md, locations=locs)
    assert chunk.meta["locations"] == [{"page": 3}]


def test_location_without_coordinates_is_skipped() -> None:
    md = "본문"
    [chunk] = chunk_markdown(md, locations=[Location(0, 2)])
    assert chunk.meta == {}


@pytest.mark.parametrize(
    "locs",
    [
        [Location(5, 9, page=2), Location(0, 4, page=1)],
        [Location(0, 5, page=1), Location(4, 8, page=2)],
        [Location(3, 2, page=1)],
    ],
    ids=["unsorted", "overlapping", "negative"],
)
def test_invalid_locations_are_rejected(locs: list[Location]) -> None:
    with pytest.raises(ValueError, match="오름차순"):
        chunk_markdown("가나다라마바사", locations=locs)


async def test_worker_without_locations_keeps_meta_unchanged(run_harness, monkeypatch) -> None:
    h = run_harness
    h.parser(lambda data: ParseResult(markdown="# 제목\n\n본문"))

    await h.run()

    # 검색 payload는 청크 행의 meta + notebook_id·source_id·seq·text다 — 위치 키가 없다
    assert set(h.indexed[0][0].meta) == {"heading"}

