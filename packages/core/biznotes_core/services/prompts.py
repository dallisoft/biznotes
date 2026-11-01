"""프롬프트 템플릿 해석 (Phase F) — 런타임이 prompt_ref로 활성 버전을 가져온다."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.models import PromptTemplate, PromptVersion


async def resolve_active_prompt(
    db: AsyncSession, template_id: uuid.UUID
) -> tuple[str, str] | None:
    """템플릿의 활성 버전 (content, system) 반환. 없으면 None."""
    tpl = await db.get(PromptTemplate, template_id)
    if tpl is None or tpl.active_version_id is None:
        return None
    ver = await db.get(PromptVersion, tpl.active_version_id)
    if ver is None:
        return None
    return ver.content, ver.system
