"""단일 입장 관문 — admit_bytes·allowed_ext_mime.

순수 함수 테스트다.
"""

from typing import Any

import pytest
from conftest import make_settings

from biznotes_core.services.ingestion.admission import (
    MAX_UPLOAD_BYTES,
    AdmissionError,
    admit_bytes,
    allowed_ext_mime,
)

SETTINGS = make_settings()
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME = "application/pdf"


def code_of(data: bytes, mime: str, settings: Any = SETTINGS) -> str | None:
    try:
        admit_bytes(data, mime, settings=settings)
    except AdmissionError as e:
        return e.code
    return None


def test_size_and_empty() -> None:
    assert code_of(b"%PDF" + b"\0" * (MAX_UPLOAD_BYTES - 4), PDF_MIME) is None
    assert code_of(b"%PDF" + b"\0" * (MAX_UPLOAD_BYTES - 3), PDF_MIME) == "too_large"
    assert code_of(b"", "text/plain") == "empty"


def test_existing_magic_regression() -> None:
    assert code_of(b"%PDF-1.7", PDF_MIME) is None
    assert code_of(b"<html>", PDF_MIME) == "magic_mismatch"
    assert code_of(b"PK\x03\x04rest", DOCX_MIME) is None
    assert code_of(b"%PDF", DOCX_MIME) == "magic_mismatch"
    assert code_of(b"anything", "text/plain") is None
    with pytest.raises(AdmissionError) as exc:
        admit_bytes(b"<html>", PDF_MIME, settings=SETTINGS)
    assert exc.value.message == "파일 내용이 확장자와 일치하지 않습니다"


def test_allowed_ext_mime_is_a_fresh_copy() -> None:
    allowed = allowed_ext_mime(SETTINGS)
    assert allowed[".pdf"] == PDF_MIME and ".hwp" not in allowed
    # 요청마다 새 dict — 모듈 상수를 바꾸지 않는다
    allowed[".exe"] = "x"
    assert ".exe" not in allowed_ext_mime(SETTINGS)
