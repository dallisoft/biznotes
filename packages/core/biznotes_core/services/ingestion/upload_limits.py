"""소스 업로드 최대 크기의 유효값(#388) — 관리자 재정의 > env > 기본값(50 MiB).

- env `SOURCE_MAX_UPLOAD_BYTES`(`source_max_upload_bytes`)가 기본값이다. 0 이하·하드 상한
  (`config.SOURCE_MAX_UPLOAD_BYTES_CEILING`, 500 MiB) 초과면 설정 검증이 기동을 막는다.
- 관리자 재정의는 `system_settings["source_upload"]["max_upload_bytes"]`이고 1~하드 상한이다.
- 업로드 라우터의 판정과 공개 조회(`GET /sources/upload-limits`)가 이 함수 하나를 쓴다 — 공개 값이
  곧 판정 값이다. 요청마다 읽으므로(캐시 없음) 바꾼 값은 다음 업로드부터 적용된다.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.config import SOURCE_MAX_UPLOAD_BYTES_CEILING, CoreSettings
from biznotes_core.services.settings_store import get_overrides, valid_int

SETTING_KEY = "source_upload"
OVERRIDE_FIELD = "max_upload_bytes"


async def get_upload_override(db: AsyncSession) -> int | None:
    """관리자 재정의(없거나 범위 밖이면 None — env 값을 쓴다)."""
    raw = await get_overrides(db, SETTING_KEY)
    return valid_int(raw.get(OVERRIDE_FIELD), 1, SOURCE_MAX_UPLOAD_BYTES_CEILING)


async def effective_max_upload_bytes(db: AsyncSession, settings: CoreSettings) -> int:
    """지금 적용되는 업로드 최대 크기(바이트)."""
    override = await get_upload_override(db)
    return override if override is not None else settings.source_max_upload_bytes
