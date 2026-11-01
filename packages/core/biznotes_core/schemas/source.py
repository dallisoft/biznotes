import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

import structlog
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from biznotes_core.schemas.enums import SourceStatus, SourceType

logger = structlog.get_logger()


class SourceCreate(BaseModel):
    """비파일 소스 등록 (file은 multipart 업로드 사용)."""

    type: Literal["web", "text"]
    url: str | None = Field(default=None, max_length=2000)
    text: str | None = Field(default=None, max_length=500_000)
    title: str | None = Field(default=None, max_length=500)


# `save_sources` 항목(#403)의 텍스트 본문 MIME과 URL 항목(PDF)의 MIME
SAVE_SOURCE_TEXT_MIME_TYPES = ("text/plain", "text/markdown")
SAVE_SOURCE_URL_MIME_TYPES = ("application/pdf",)
# 중복 키 뒷부분으로 쓰는 외부 id — URL·경로에 쓰이는 비예약 글자와 `:/=+`만
SAVE_SOURCE_EXTERNAL_ID_PATTERN = r"^[A-Za-z0-9._~:/=+-]+$"


class SaveSourceItem(BaseModel):
    """`save_sources` 노드가 받는 항목 하나(#403). 모르는 키는 무시한다.

    - `title`: 노드가 먼저 `clean_text`로 한 줄로 접고 보이지 않는 글자를 지워 500자로 자른 뒤
      이 스키마로 검증한다(1..500).
    - `external_id`: 중복 키의 뒷부분이다(`mcp:<namespace>:<external_id>`). 문자열만 받는다.
    - `url`(https, PDF 전용)과 `content`(텍스트 본문) 중 정확히 하나를 둔다. `mime_type`은
      `content`면 `text/plain`·`text/markdown`, `url`이면 `application/pdf`만 받는다.
    """

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=500)
    external_id: str = Field(min_length=1, max_length=200, pattern=SAVE_SOURCE_EXTERNAL_ID_PATTERN)
    url: str | None = Field(default=None, max_length=2000)
    content: str | None = Field(default=None, max_length=500_000)
    mime_type: str | None = None

    @field_validator("url")
    @classmethod
    def _https_url(cls, value: str | None) -> str | None:
        if value is not None:
            parts = urlsplit(value)
            if parts.scheme != "https" or not parts.hostname:
                raise ValueError("url은 호스트가 있는 https 주소여야 합니다")
        return value

    @model_validator(mode="after")
    def _one_body(self) -> Self:
        if (self.url is None) == (self.content is None):
            raise ValueError("url과 content 중 정확히 하나가 필요합니다")
        if self.content is not None and not self.content.strip():
            raise ValueError("content가 비어 있습니다")
        allowed = SAVE_SOURCE_TEXT_MIME_TYPES if self.content is not None else (
            SAVE_SOURCE_URL_MIME_TYPES
        )
        if self.mime_type is not None and self.mime_type not in allowed:
            raise ValueError(f"mime_type은 {', '.join(allowed)} 중 하나여야 합니다")
        return self


class SourceWarning(BaseModel):
    """인제스트 경고 하나 — 코드 목록은 `services/ingestion/source_warnings.py`, 추가만 한다.

    params 값은 영구히 int·float·str이다. 새 형태는 새 코드로 낸다.
    """

    code: str
    params: dict[str, int | float | str] = Field(default_factory=dict)


def _tolerant_warnings(value: Any) -> list[dict[str, Any]] | None:
    """저장된 경고를 관용적으로 읽는다 — 형식이 다른 항목 하나가 소스 목록 응답 전체를
    500으로 만들지 않게 그 항목만 버리고 남긴다(다른 버전의 코어가 쓴 행, 롤백 등)."""
    if value is None:
        return None
    if not isinstance(value, list):
        logger.warning("소스 경고 형식 오류 — 무시", kind=type(value).__name__)
        return None
    kept: list[dict[str, Any]] = []
    for item in value:
        try:
            kept.append(SourceWarning.model_validate(item).model_dump())
        except ValidationError:
            continue
    if len(kept) < len(value):
        # 목록 응답마다 찍히므로 항목별이 아니라 행마다 한 번만 남긴다
        logger.warning("소스 경고 항목 형식 오류 — 버림", dropped=len(value) - len(kept))
    return kept


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    notebook_id: uuid.UUID
    type: SourceType
    title: str
    origin_url: str | None
    mime_type: str | None
    size_bytes: int | None
    status: SourceStatus
    error_message: str | None
    token_count: int
    chunk_count: int
    # 문서 자체 제목(HWP-02)과 인제스트 경고(HWP-03). NULL은 기록 없음.
    # document_title은 UI 표시용이다 — 외부가 정한 문자열이라 프롬프트에 넣으려면 별도의
    # 구분·이스케이프가 필요하다. 기본값이 있어 OpenAPI에서 선택 필드다(제품 타입 호환)
    document_title: str | None = None
    warnings: Annotated[list[SourceWarning] | None, BeforeValidator(_tolerant_warnings)] = None
    created_at: datetime
    updated_at: datetime


class SourceDetailOut(SourceOut):
    parsed_preview: str | None = None  # 파싱된 markdown 앞부분
    summary: str | None = None  # 소스 가이드 캐시 (없으면 /guide로 생성)
    keywords: list[str] | None = None


class SourceUploadLimitsOut(BaseModel):
    """추가 전에 클라이언트가 거를 수 있는 공개 값(#388·#445) — 서버 판정과 같은 값이다."""

    max_upload_bytes: int  # 이 크기를 넘는 파일 업로드는 413
    # 노트북당 소스 상한(#445) — 소스가 이만큼 있는 노트북에 더하는 요청
    # (업로드·URL 등)은 422
    max_sources_per_notebook: int


class SourceGuideOut(BaseModel):
    """소스 가이드(요약 + 키워드). ready 소스에 대해 온디맨드 생성·캐시."""

    summary: str | None = None
    keywords: list[str] = Field(default_factory=list)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    source_ids: list[uuid.UUID] | None = None
    limit: int = Field(default=8, ge=1, le=30)


class SearchHit(BaseModel):
    chunk_id: str
    source_id: str
    seq: int
    text: str
    score: float
    heading: str | None = None


class WebSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)


class WebSearchResult(BaseModel):
    title: str
    url: str
    snippet: str | None = None
    engine: str | None = None
