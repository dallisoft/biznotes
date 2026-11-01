"""노트북당 소스 상한의 유효값(#445) — 관리자 재정의 > env > 기본값(1000).

- env `MAX_SOURCES_PER_NOTEBOOK`(`max_sources_per_notebook`)가 기본값이다. 0 이하면 설정
  검증이 기동을 막는다.
- 관리자 재정의는 `system_settings["notebook_source_limit"]["max_sources_per_notebook"]`이고
  1~`NOTEBOOK_SOURCE_LIMIT_OVERRIDE_MAX`다.
- 추가 판정(`enqueue.notebook_source_limit`)과 공개 조회(`GET /sources/upload-limits`)가 이
  값을 쓴다. 요청마다 읽으므로(캐시 없음) 바꾼 값은 다음 추가부터 적용된다. 상한을 낮춰도
  기존 소스는 지우지 않는다.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.config import CoreSettings
from biznotes_core.schemas.limits import NOTEBOOK_SOURCE_LIMIT_OVERRIDE_MAX
from biznotes_core.services.settings_store import get_overrides, valid_int

SETTING_KEY = "notebook_source_limit"
OVERRIDE_FIELD = "max_sources_per_notebook"


async def get_max_sources_override(db: AsyncSession) -> int | None:
    """관리자 재정의(없거나 범위 밖이면 None — env 값을 쓴다)."""
    raw = await get_overrides(db, SETTING_KEY)
    return valid_int(raw.get(OVERRIDE_FIELD), 1, NOTEBOOK_SOURCE_LIMIT_OVERRIDE_MAX)


async def effective_max_sources_per_notebook(db: AsyncSession, settings: CoreSettings) -> int:
    """지금 적용되는 노트북당 소스 상한."""
    override = await get_max_sources_override(db)
    return override if override is not None else settings.max_sources_per_notebook
