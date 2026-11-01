"""웹 인제스트 경고 문구 사전 ↔ 코어 경고 코드 정본 대조 (core-v0.16.0-upgrade AC-1.3 ⑧).

생성 계약의 `SourceWarning.code`는 `string`이라(코드 enum 아님) 웹 사전(`MESSAGES`)의 누락을
typecheck로 잡을 수 없다. 모르는 코드는 화면에 「알 수 없는 인제스트 경고(code)」로만 보인다.
그래서 이 테스트가 웹 `MESSAGES` 객체 리터럴의 키를 읽어 코어
`biznotes_core.services.ingestion.source_warnings.WARNING_CODES`와 같은 집합인지 본다 — 코어 핀
상향으로 코드가 늘면 `make ci`가 여기서 실패한다(`test_web_modalities_contract.py`와 같은 방식,
주석·문자열 속 선언은 읽지 않는다).

웹 보안 코드 집합 `SECURITY_WARNING_CODES`(주의 스타일)도 코어 `SECURITY_CODES`와 같은 집합인지 본다
(core-v0.20.0-upgrade D-7, AC-1.8 ⑨ — 코어 v0.20.0 #231 `threat_scan_records_skipped`가 보안 코드로
늘었다). 집합을 코어와 같게 두면 이 계약 하나로 드리프트를 막는다.
"""

import ast
import re
from pathlib import Path

import pytest
from biznotes_core.services.ingestion import source_warnings

from tests.test_web_modalities_contract import _mask_comments_and_strings

SOURCE_WARNINGS_TS = Path(__file__).resolve().parents[2] / "web/src/lib/source-warnings.ts"

# 타입 주석의 화살표(`(p: Params) => string`)를 건너 할당 `= {`까지 — `=>`는 할당이 아니다
_DECL = re.compile(r"\bconst\s+MESSAGES\b(?:[^=]|=>)*=\s*\{")
# `export const SECURITY_WARNING_CODES: ReadonlySet<string> = new Set([` — 배열 리터럴 `[` 직전까지
_SECURITY_DECL = re.compile(
    r"\bexport\s+const\s+SECURITY_WARNING_CODES\b[^=]*=\s*new\s+Set\s*(?:<[^>]*>)?\(\s*\["
)
_KEY = re.compile(r"\s*(?:([A-Za-z_$][\w$]*)|([\"'])\s*\2)\s*:")
_OPEN, _CLOSE = "{[(", "}])"


def web_message_keys(source: str) -> tuple[str, ...]:
    """실제 코드의 `const MESSAGES ... = { key: ..., ... }` 키 목록. 모호하면 실패한다.

    - 선언 텍스트는 파일 전체에 정확히 1개여야 하고, 그 1개는 주석·문자열 밖에 있어야 한다
    - 최상위 항목은 모두 `키: 값` 형태여야 한다(전개·계산 키·축약 속성은 읽을 수 없어 실패)
    - 키가 중복되면 실패한다
    """
    found = list(_DECL.finditer(source))
    if len(found) != 1:
        raise AssertionError(
            f"MESSAGES 선언 텍스트가 {len(found)}개다 — 정확히 1개여야 한다(주석·문자열 포함)"
        )
    masked = _mask_comments_and_strings(source)
    if not _DECL.match(masked, found[0].start()):
        raise AssertionError("MESSAGES 선언이 주석이나 문자열 안에 있다 — 실제 선언이 없다")
    start = found[0].end()  # "{" 바로 뒤
    depth, segments, seg_start, end = 1, [], start, None
    for k in range(start, len(masked)):
        ch = masked[k]
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth -= 1
            if depth == 0:
                end = k
                break
        elif ch == "," and depth == 1:
            segments.append((seg_start, k))
            seg_start = k + 1
    if end is None:
        raise AssertionError("MESSAGES 객체 리터럴이 닫히지 않았다")
    segments.append((seg_start, end))
    keys: list[str] = []
    for a, b in segments:
        if not masked[a:b].strip():
            continue  # 끝 쉼표 뒤의 빈 항목
        m = _KEY.match(masked, a, b)
        if m is None:
            raise AssertionError(f"MESSAGES 항목을 읽지 못했다: {source[a:b].strip()[:60]!r}")
        if m.group(1):
            keys.append(m.group(1))
        else:  # 따옴표 키 — 가린 텍스트가 아니라 원문에서 읽는다
            q = masked.index(m.group(2), a)
            keys.append(source[q + 1 : masked.index(m.group(2), q + 1)])
    if len(set(keys)) != len(keys):
        raise AssertionError(f"MESSAGES 키가 중복된다: {sorted(keys)}")
    return tuple(keys)


def _blank_comments(source: str) -> str:
    """주석만 공백으로 가린다(문자열 리터럴은 그대로) — 배열 리터럴을 `ast`로 읽기 위한 사본."""
    out = list(source)
    i, n = 0, len(source)
    while i < n:
        c, nxt = source[i], source[i + 1 : i + 2]
        if c in "'\"`":
            j = i + 1
            while j < n and source[j] != c:
                j += 2 if source[j] == "\\" else 1
            i = j + 1
            continue
        if c == "/" and nxt in ("/", "*"):
            j = source.find("\n" if nxt == "/" else "*/", i + 2)
            j = n if j == -1 else (j if nxt == "/" else j + 2)
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out)


def web_security_codes(source: str) -> frozenset[str]:
    """실제 코드의 `export const SECURITY_WARNING_CODES ... = new Set([...])` 문자열 집합.

    선언 텍스트는 파일 전체에 정확히 1개이고 주석·문자열 밖에 있어야 한다. 배열은 문자열 원소만 담은
    리터럴이어야 하고 중복이 없어야 한다. 모호하면 실패한다.
    """
    found = list(_SECURITY_DECL.finditer(source))
    if len(found) != 1:
        raise AssertionError(
            f"SECURITY_WARNING_CODES 선언 텍스트가 {len(found)}개다 — 정확히 1개여야 한다"
        )
    masked = _mask_comments_and_strings(source)
    if not _SECURITY_DECL.match(masked, found[0].start()):
        raise AssertionError("SECURITY_WARNING_CODES 선언이 주석이나 문자열 안에 있다")
    start = found[0].end() - 1  # "["
    depth, end = 0, None
    for k in range(start, len(masked)):
        if masked[k] == "[":
            depth += 1
        elif masked[k] == "]":
            depth -= 1
            if depth == 0:
                end = k
                break
    if end is None:
        raise AssertionError("SECURITY_WARNING_CODES 배열 리터럴이 닫히지 않았다")
    try:
        values = ast.literal_eval(_blank_comments(source)[start : end + 1])
    except (SyntaxError, ValueError) as exc:
        raise AssertionError(f"SECURITY_WARNING_CODES 배열을 읽지 못했다: {exc}") from exc
    if not isinstance(values, list) or not all(type(v) is str for v in values):
        raise AssertionError("SECURITY_WARNING_CODES는 문자열 배열 리터럴이어야 한다")
    if len(set(values)) != len(values):
        raise AssertionError(f"SECURITY_WARNING_CODES 원소가 중복된다: {sorted(values)}")
    return frozenset(values)


def test_web_messages_cover_core_warning_codes() -> None:
    """웹 사전의 키 집합 == 코어 `WARNING_CODES`(v0.20.0: 15종 — 재인덱싱 2종 #230,
    배포용 HWP 검사 범위 축소 #231 포함)."""
    web = web_message_keys(SOURCE_WARNINGS_TS.read_text(encoding="utf-8"))
    assert set(web) == set(source_warnings.WARNING_CODES)
    assert len(web) == len(source_warnings.WARNING_CODES) == 15


def test_web_security_codes_match_core_security_codes() -> None:
    """웹 `SECURITY_WARNING_CODES` == 코어 `SECURITY_CODES`(집합, v0.20.0: 4종)."""
    web = web_security_codes(SOURCE_WARNINGS_TS.read_text(encoding="utf-8"))
    assert web == source_warnings.SECURITY_CODES
    assert web <= source_warnings.WARNING_CODES


@pytest.mark.parametrize(
    "source",
    [
        'const OTHER = new Set(["a"]);',  # 선언 없음
        '// export const SECURITY_WARNING_CODES = new Set(["a"]);\n',  # 주석에만 선언
        "export const SECURITY_WARNING_CODES = new Set(BASE);",  # 리터럴이 아님
        'export const SECURITY_WARNING_CODES = new Set(["a", x]);',  # 읽을 수 없는 원소
        'export const SECURITY_WARNING_CODES = new Set(["a", "a"]);',  # 중복
        'export const SECURITY_WARNING_CODES = new Set(["a"]);\n'
        'export const SECURITY_WARNING_CODES = new Set(["b"]);',  # 중복 선언
    ],
)
def test_web_security_codes_parser_fails_loudly(source: str) -> None:
    with pytest.raises(AssertionError):
        web_security_codes(source)


def test_web_security_codes_parser_reads_real_shape() -> None:
    source = (
        "export const SECURITY_WARNING_CODES: ReadonlySet<string> = new Set([\n"
        '  "threat_findings", // 주석 "x"\n'
        '  "a]b",\n'
        "]);\n"
    )
    assert web_security_codes(source) == frozenset({"threat_findings", "a]b"})


@pytest.mark.parametrize(
    "source",
    [
        "const OTHER = { a: () => 'x' };",  # 선언 없음
        "// const MESSAGES = { a: () => 'x' };\n",  # 주석에만 선언
        'const s = "const MESSAGES = { a: 1 }";\n',  # 문자열에만 선언
        "const MESSAGES = { a: 1 };\nconst MESSAGES = { b: 1 };",  # 중복 선언
        "const MESSAGES = { ...BASE, a: () => 'x' };",  # 전개 — 키를 알 수 없다
        "const MESSAGES = { [dynamic]: () => 'x' };",  # 계산 키
        "const MESSAGES = { a, b: () => 'x' };",  # 축약 속성
        "const MESSAGES = { a: 1, a: 2 };",  # 키 중복
        "const MESSAGES = { a: 1",  # 닫히지 않음
    ],
)
def test_web_message_keys_parser_fails_loudly(source: str) -> None:
    """키를 못 찾거나 못 읽으면 조용히 통과하지 않고 실패한다."""
    with pytest.raises(AssertionError):
        web_message_keys(source)


def test_web_message_keys_parser_reads_real_shape() -> None:
    """타입 주석의 화살표·템플릿 리터럴 속 `${}`·쉼표·괄호·따옴표 키를 항목 경계로
    오인하지 않는다."""
    source = (
        "const MESSAGES: Record<string, (p: Params) => string> = {\n"
        "  // a_comment: () => 'x',\n"
        '  text_truncated: (p) => `생략 ${int(p, "omitted")}개, {괄호}`,\n'
        "  'quoted_code': () => \"a, b: c\",\n"
        "  reindex_deferred: (p) =>\n"
        '    `${label(p, "reason", { x: 1, y: [2, 3] })}`,\n'
        "};\n"
    )
    assert web_message_keys(source) == ("text_truncated", "quoted_code", "reindex_deferred")
