"""어드민 — 감사 로그."""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.core.db import get_db
from biznotes_core.models import AuditLog, User

router = APIRouter()


class AuditLogOut(BaseModel):
    id: uuid.UUID
    user_email: str | None
    action: str
    resource: str
    detail: dict[str, Any]
    created_at: datetime


@router.get("/audit-logs", response_model=list[AuditLogOut])
async def audit_logs(
    limit: int = Query(default=100, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[AuditLogOut]:
    rows = await db.execute(
        select(AuditLog, User.email)
        .outerjoin(User, AuditLog.user_id == User.id)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    return [
        AuditLogOut(
            id=log.id,
            user_email=email,
            action=log.action,
            resource=log.resource,
            detail=log.detail,
            created_at=log.created_at,
        )
        for log, email in rows.all()
    ]
