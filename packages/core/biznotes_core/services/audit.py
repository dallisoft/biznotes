"""감사 로그 기록 — 어드민 변경·공유·삭제 등 민감 행위."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.models.audit import AuditLog


async def record(
    db: AsyncSession,
    user_id: uuid.UUID | None,
    action: str,
    resource: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    db.add(AuditLog(user_id=user_id, action=action, resource=resource, detail=detail or {}))
    # 커밋은 호출측 트랜잭션에 합류
