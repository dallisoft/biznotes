"""웹 능력 목록 ↔ 코어 정본 대조 (core-v0.15.0 AC-2.5, plan-amendment-2).

생성 계약의 `capabilities`는 `string[]`이라 웹 `Modality`가 닫힌 유니온이 아니다. 그래서 웹
`MODALITIES`의 누락·오타를 typecheck로 잡을 수 없다(`reports/phase-2-blocked.md`). 대신 이
테스트가 웹 배열 리터럴을 읽어 코어 `biznotes_core.models.llm.MODALITIES`와 **순서까지** 같은지
본다. 코어 핀 상향으로 능력이 늘거나 줄면 `make ci`가 여기서 실패한다. 코어가 OpenAPI에 enum을
노출하면(biznotes-core#218) typecheck 검사로 바꾸는 것을 검토한다.

같은 방식으로 웹 리랭커 키 정규화의 공백 집합(`apps/web/src/lib/reranker.ts`)이
Python `str.isspace()`와 같은지도 본다 — 코어 `normalize_rerank_key`가 `str.strip()`을
쓰기 때문이다(리뷰 P2-07).
"""

import ast
import re
import sys
from pathlib import Path

import pytest
from biznotes_core.models.llm import MODALITIES

WEB_SRC = Path(__file__).resolve().parents[2] / "web/src"
PICKER = WEB_SRC / "components/admin/llm/capability-picker.tsx"
RERANKER_LIB = WEB_SRC / "lib/reranker.ts"


def _decl(name: str) -> re.Pattern[str]:
    return re.compile(rf"\bexport\s+const\s+{name}\s*=\s*\[")


def _mask_comments_and_strings(source: str) -> str:
    """주석과 문자열 리터럴의 내용을 공백으로 가린다(위치·줄바꿈·따옴표는 유지).

    주석에 남긴 옛 선언이나 문자열 속 선언을 실제 선언으로 읽지 않게 한다(리뷰 P2-04).
    """
    out = list(source)
    i, n = 0, len(source)
    while i < n:
        c, nxt = source[i], source[i + 1 : i + 2]
        if c == "/" and nxt == "/":
            j = source.find("\n", i)
            j = n if j == -1 else j
        elif c == "/" and nxt == "*":
            j = source.find("*/", i + 2)
            j = n if j == -1 else j + 2
        elif c in "'\"`":
            j = i + 1
            while j < n and source[j] != c:
                j += 2 if source[j] == "\\" else 1
            for k in range(i + 1, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 1
            continue
        else:
            i += 1
            continue
        for k in range(i, j):
            if out[k] != "\n":
                out[k] = " "
        i = j
    return "".join(out)


def web_array_literal(source: str, name: str, element: type) -> tuple:
    """실제 코드의 `export const <name> = [...]` 배열 리터럴 값. 모호하면 실패한다.

    - 선언 텍스트는 파일 전체에 정확히 1개여야 한다 — 주석·문자열에 남긴 옛 선언이 있어도 실패한다
    - 그 1개는 주석·문자열 밖(실제 코드)에 있어야 한다
    - 배열은 `element` 타입 원소만 담은 리터럴이어야 한다
    """
    decl = _decl(name)
    found = list(decl.finditer(source))
    if len(found) != 1:
        raise AssertionError(
            f"{name} 선언 텍스트가 {len(found)}개다 — 정확히 1개여야 한다(주석·문자열 포함)"
        )
    masked = _mask_comments_and_strings(source)
    start = found[0].end() - 1  # "["
    if not decl.match(masked, found[0].start()):
        raise AssertionError(f"{name} 선언이 주석이나 문자열 안에 있다 — 실제 선언이 없다")
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
        raise AssertionError(f"{name} 배열 리터럴이 닫히지 않았다")
    try:
        values = ast.literal_eval(source[start : end + 1])
    except (SyntaxError, ValueError) as exc:
        raise AssertionError(f"{name} 배열 리터럴을 읽지 못했다: {exc}") from exc
    if not isinstance(values, list) or not all(type(v) is element for v in values):
        raise AssertionError(f"{name}는 {element.__name__} 배열 리터럴이어야 한다")
    return tuple(values)


def web_modalities(source: str) -> tuple[str, ...]:
    return web_array_literal(source, "MODALITIES", str)


def test_web_modalities_match_core_canonical_order() -> None:
    assert web_modalities(PICKER.read_text(encoding="utf-8")) == MODALITIES


@pytest.mark.parametrize(
    "source",
    [
        "export const PROVIDER_TYPES = ['openai'];",  # 리터럴 없음
        "export const MODALITIES = buildModalities();",  # 리터럴이 아님
        'export const MODALITIES = ["llm", someVar] as const;',  # 읽을 수 없는 원소
        # 리뷰 P2-04 — 주석에 완전한 옛 목록 + 실제 목록 누락
        '/* export const MODALITIES = ["llm", "vlm", "embedding", "tts", "stt", "reranker"]; */\n'
        'export const MODALITIES = ["llm"] as const;',
        '// export const MODALITIES = ["llm", "reranker"];\n',  # 주석에만 선언
        '/*\nexport const MODALITIES = ["llm", "reranker"];\n*/\n',  # 블록 주석에만 선언
        'const s = "export const MODALITIES = [\\"llm\\"]";\n',  # 문자열에만 선언
        'export const MODALITIES = ["llm"];\nexport const MODALITIES = ["vlm"];',  # 중복
    ],
)
def test_web_modalities_parser_fails_loudly(source: str) -> None:
    """리터럴을 못 찾거나 못 읽으면 조용히 통과하지 않고 실패한다."""
    with pytest.raises(AssertionError):
        web_modalities(source)


def test_web_modalities_parser_reads_multiline_const_literal() -> None:
    source = 'export const MODALITIES = [\n  "llm",\n  "reranker",\n] as const satisfies X[];'
    assert web_modalities(source) == ("llm", "reranker")



def test_web_modalities_parser_ignores_brackets_inside_strings() -> None:
    source = '// 주석의 [괄호]\nexport const MODALITIES = ["a]b", "c"] as const;'
    assert web_modalities(source) == ("a]b", "c")


def test_web_rerank_key_whitespace_matches_python_isspace() -> None:
    """리뷰 P2-07: 웹 `pythonStrip`의 공백 집합 == Python `str.isspace()` 전체.

    코어 `normalize_rerank_key`의 `str.strip()`과 같은 기준이어야 한다.
    """
    web = web_array_literal(
        RERANKER_LIB.read_text(encoding="utf-8"), "PYTHON_WHITESPACE_CODE_POINTS", int
    )
    python = tuple(c for c in range(sys.maxunicode + 1) if chr(c).isspace())
    assert web == python
