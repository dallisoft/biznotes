"""소스 인제스트 경고 코드와 문서 제목 정화 (HWP-02·HWP-03).

`sources.warnings`에 들어가는 항목은 `{"code": str, "params": {...}}`다. 코드는 이 모듈의
상수에서만 나오고 **추가만** 한다(제품은 모르는 코드를 일반 경고로 보여 준다). 문서 원문은
넣지 않는다.

**params 값 타입 계약**: 영구히 int·float·str 가운데 하나다. int는 0 이상 JS 안전 정수 이하,
float은 유한하고 소수 넷째 자리로 맞춘다. 새 형태가 필요하면 기존 코드를 바꾸지 않고 새
코드를 만든다(옛 코어가 새 행을 읽어도 응답이 깨지지 않게).

`sources.document_title`은 문서가 스스로 가진 제목(HWP `info.title`, PDF 메타데이터,
DOCX·PPTX·XLSX 문서 속성(core properties), 웹 `<title>`)이다. 외부가 정한 문자열이고
제품 UI에 보이므로, 저장 전에 한 줄로 접고 보이지 않는 글자를 지운다.

계획: `docs/plans/source-metadata/plan.md`. DB·모델 무의존 순수 모듈이다.
"""

import math
import re
import unicodedata
from typing import Any

# 보이지 않으면서 문자열 비교·표시를 어긋나게 하는 코드포인트 — 제로폭(`U+200B`~`U+200D`·
# `U+2060`·`U+FEFF`), bidi 제어(`U+202A`~`U+202E`·`U+2066`~`U+2069`),
# 태그 문자(`U+E0000`~`U+E007F`).
# 코드포인트로 적는다 — 보이지 않는 문자를 리터럴로 두면 편집기·머지가 조용히 바꿀 수 있다.
_ZERO_WIDTH = frozenset(chr(c) for c in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF))
_BIDI = frozenset(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))
_TAGS = frozenset(chr(c) for c in range(0xE0000, 0xE0080))
DECEPTIVE_CODEPOINTS = _ZERO_WIDTH | _BIDI | _TAGS

# 본문 처리 경고 — OCR 폴백이 본문을 교체하면 의미가 없어진다
TEXT_TRUNCATED = "text_truncated"
TABLE_APPENDIX_TRUNCATED = "table_appendix_truncated"
NESTED_TABLE_FLATTENED = "nested_table_flattened"
DECEPTIVE_CODEPOINTS_REMOVED = "deceptive_codepoints_removed"
SPREADSHEET_ROWS_TRUNCATED = "spreadsheet_rows_truncated"
# 보안 신호 — 원본 파일의 성질이라 OCR 폴백 뒤에도 남는다
THREAT_FINDINGS = "threat_findings"
HIDDEN_TEXT = "hidden_text"
INJECTION_SIGNALS = "injection_signals"
# threat-scan이 DocInfo·BodyText 레코드 해석을 건너뛰고 통과시켰다(배포용 HWP5, #231). BinData·
# OLE 검사는 했다. params는 없다
THREAT_SCAN_RECORDS_SKIPPED = "threat_scan_records_skipped"
# HWP 구조 사본(에이전트 도구용)을 만들지 못했다 — 본문 인제스트는 성공했다(HWP-07)
STRUCTURE_UNAVAILABLE = "structure_unavailable"
# HWP 쪽 단위 OCR(HWP-12) — 그림만 있는 빈 쪽을 OCR로 채웠다(`count`), 상한·deadline·글자 예산으로
# 멈췄다(`done`·`candidates`·`reason`), rhwp·OCR 엔진 실패로 건너뛰었다(`count`). 본문 처리 경고다
OCR_PAGES = "ocr_pages"
OCR_PAGES_TRUNCATED = "ocr_pages_truncated"
OCR_PAGE_FAILED = "ocr_page_failed"
# 재인덱싱 안내(#230) — 재인덱싱이 이전 색인을 유지한 `ready` 행의 안내다. 원본 파일의 성질이
# 아니라 색인 단계가 쓰고 지운다(`REINDEX_CODES`). 오류 원문은 싣지 않는다 — 상세는 재인덱싱
# 항목의 `error`와 로그에 있다.
# - `reindex_kept_previous`: 재인덱싱이 실패했고 이전 색인을 유지한다. params `reason`
#   (`parse`·`embedding_transient`·`embedding_gate`·`error`)
# - `reindex_deferred`: 재인덱싱이 임베딩 일시 오류로 미뤄졌고 이전 색인으로 검색된다. params
#   `reason`(`embedding_transient`·`embedding_gate`)·`attempt`·`max_attempts`·`retry_in_seconds`
REINDEX_KEPT_PREVIOUS = "reindex_kept_previous"
REINDEX_DEFERRED = "reindex_deferred"

WARNING_CODES: frozenset[str] = frozenset(
    {
        TEXT_TRUNCATED,
        TABLE_APPENDIX_TRUNCATED,
        NESTED_TABLE_FLATTENED,
        DECEPTIVE_CODEPOINTS_REMOVED,
        SPREADSHEET_ROWS_TRUNCATED,
        THREAT_FINDINGS,
        HIDDEN_TEXT,
        INJECTION_SIGNALS,
        THREAT_SCAN_RECORDS_SKIPPED,
        STRUCTURE_UNAVAILABLE,
        OCR_PAGES,
        OCR_PAGES_TRUNCATED,
        OCR_PAGE_FAILED,
        REINDEX_KEPT_PREVIOUS,
        REINDEX_DEFERRED,
    }
)
REINDEX_CODES: frozenset[str] = frozenset({REINDEX_KEPT_PREVIOUS, REINDEX_DEFERRED})
SECURITY_CODES: frozenset[str] = frozenset(
    {THREAT_FINDINGS, HIDDEN_TEXT, INJECTION_SIGNALS, THREAT_SCAN_RECORDS_SKIPPED}
)

# 문자열 params(시트 이름·등급)의 상한
_PARAM_TEXT_LIMIT = 100
DOCUMENT_TITLE_LIMIT = 500
# 공백류(줄·문단 구분자, 탭·줄바꿈 포함)는 공백 하나로 접는다.
# 지우기 전에 접어야 단어가 붙지 않는다
_WS_RE = re.compile(r"[\s\u2028\u2029]+")
# 보이지 않거나 저장할 수 없는 글자의 범주 — 제어(Cc: NUL·ESC), 형식(Cf: bidi·ZWSP·LRM/RLM·
# soft hyphen), 서로게이트(Cs: PostgreSQL이 거부한다), 사용자 정의(Co)
_DROP_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co"})
# 범주로는 안 잡히는 보이지 않는 글자 — CGJ, 한글 채움 문자, 변형 선택자
_INVISIBLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0xE0100, 0xE01EF),
)


def _dropped(ch: str) -> bool:
    if ch in DECEPTIVE_CODEPOINTS or unicodedata.category(ch) in _DROP_CATEGORIES:
        return True
    cp = ord(ch)
    return any(a <= cp <= b for a, b in _INVISIBLE_RANGES)


def clean_text(value: str, limit: int) -> str:
    """외부 문자열을 한 줄로 접고 보이지 않는·저장할 수 없는 글자를 지운 뒤 자른다."""
    folded = _WS_RE.sub(" ", value)
    visible = "".join(ch for ch in folded if not _dropped(ch))
    return _WS_RE.sub(" ", visible).strip()[:limit].rstrip()


_JS_SAFE_INT = 2**53 - 1


def nat(value: Any) -> int:
    """비신뢰 개수 — 0 이상 정수가 아니면 0, JS 안전 정수에서 자른다."""
    return min(value, _JS_SAFE_INT) if type(value) is int and value >= 0 else 0


def _param(value: Any) -> int | float | str:
    """params 값 — JSONB에 넣을 수 있는 유한한 숫자나 정화한 짧은 문자열만 남긴다."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return nat(value)
    if isinstance(value, float):
        return round(value, 4) if math.isfinite(value) else 0.0
    return clean_text(str(value), _PARAM_TEXT_LIMIT)


def warning(code: str, **params: Any) -> dict[str, Any]:
    """경고 항목 하나. 코드는 `WARNING_CODES`에서만, params는 `_param`으로 정리한다."""
    if code not in WARNING_CODES:
        raise ValueError(f"알 수 없는 소스 경고 코드: {code}")
    return {"code": code, "params": {key: _param(value) for key, value in params.items()}}


def without_codes(items: Any, codes: frozenset[str]) -> Any:
    """`codes`를 뺀 **새** 목록(JSONB 변경 감지).

    `None`(기록 없음)과 목록이 아닌 값(다른 버전이 쓴 형식 등)은 그대로 둔다 — 읽는 쪽
    (`SourceOut`)이 관용적으로 다룬다.
    """
    if not isinstance(items, list):
        return items
    return [w for w in items if not (isinstance(w, dict) and w.get("code") in codes)]


def has_code(items: list[dict[str, Any]] | None, code: str) -> bool:
    return any(isinstance(w, dict) and w.get("code") == code for w in items or ())


def sanitize_document_title(title: str | None) -> str | None:
    """문서 제목을 한 줄로 접고 보이지 않는 글자·기만 코드포인트를 지운다. 비면 None."""
    if not title:
        return None
    clean = clean_text(title, DOCUMENT_TITLE_LIMIT)
    return clean or None
