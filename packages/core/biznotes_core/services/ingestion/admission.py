"""파일 bytes 단일 입장 관문 — 업로드 라우터가 파서에 넘기기 전에 부른다.

모든 검사는 본문을 풀지 않는다 — 비용은 O(len(data))다.
"""

from typing import Literal

import structlog

from biznotes_core.config import SOURCE_MAX_UPLOAD_BYTES_CEILING, CoreSettings
from biznotes_core.services.ingestion.events import INGEST_PARSE_ADMISSION_REJECTED
from biznotes_core.services.ingestion.parsers import EXT_MIME

logger = structlog.get_logger()

# 업로드 최대 크기의 **기본값**(#388 이전에는 고정 상한). 실제 상한은 `source_max_upload_bytes`
# (env, 기본 이 값)이고, 업로드 라우터는 관리자 재정의까지 반영한 유효값(`upload_limits`)을 넘긴다
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# 설정·재정의가 넘지 못하는 하드 상한(`config.SOURCE_MAX_UPLOAD_BYTES_CEILING`)
MAX_UPLOAD_BYTES_CEILING = SOURCE_MAX_UPLOAD_BYTES_CEILING

_OOXML_MIMES = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
_ZIP_LOCAL = b"PK\x03\x04"
MAGIC: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF",),
    **{m: (_ZIP_LOCAL,) for m in _OOXML_MIMES},
}

AdmissionCode = Literal[
    "too_large",
    "empty",
    "ext_not_allowed",
    "magic_mismatch",
]

_MSG_MAGIC = "파일 내용이 확장자와 일치하지 않습니다"


class AdmissionError(Exception):
    """관문 거부. `message`는 한국어이며 사용자에게 그대로 보여도 된다."""

    def __init__(self, code: AdmissionCode, message: str):
        super().__init__(message)
        self.code: AdmissionCode = code
        self.message = message


def allowed_ext_mime(settings: CoreSettings) -> dict[str, str]:
    """요청 시점의 허용 확장자 → canonical MIME."""
    return dict(EXT_MIME)


def too_large_message(max_bytes: int) -> str:
    """크기 초과 문구 — 기본값(50 MiB)이면 지금과 같은 「파일이 50MB를 초과합니다」다."""
    mib = 1024 * 1024
    if max_bytes % mib == 0:
        size = f"{max_bytes // mib}MB"
    elif max_bytes >= mib:
        size = f"{max_bytes / mib:.1f}MB"
    elif max_bytes % 1024 == 0:
        size = f"{max_bytes // 1024}KB"
    else:
        size = f"{max_bytes:,}바이트"
    return f"파일이 {size}를 초과합니다"


def admit_bytes(
    data: bytes, mime: str, *, settings: CoreSettings, max_bytes: int | None = None
) -> None:
    """파서에 넘기기 전 검증. 통과하면 반환, 아니면 `AdmissionError`.

    `max_bytes`를 주지 않으면 `settings.source_max_upload_bytes`(env)가 상한이다(#388).
    """
    limit = settings.source_max_upload_bytes if max_bytes is None else max_bytes
    if len(data) > limit:
        raise AdmissionError("too_large", too_large_message(limit))
    if not data:
        raise AdmissionError("empty", "빈 파일입니다")

    expected = MAGIC.get(mime)
    if expected and not any(data.startswith(sig) for sig in expected):
        raise AdmissionError("magic_mismatch", _MSG_MAGIC)


def log_admission_rejected(err: AdmissionError, *, mime: str | None, size_bytes: int) -> None:
    logger.warning(
        INGEST_PARSE_ADMISSION_REJECTED,
        entry="upload",
        code=err.code,
        mime=mime,
        size_bytes=size_bytes,
    )
