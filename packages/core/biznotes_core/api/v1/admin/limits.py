"""어드민 — 「사용 제한」 설정: 분당 레이트 리밋(#386), 일일 사용 한도 기본값(#387),
소스 업로드 최대 크기(#388), 노트북당 소스 상한(#445).

값은 `system_settings` 재정의이고 기본값은 env·코드 상수다. 프로젝트가 `/admin` 집계 라우터에
`require_admin`을 걸어 조립한다. 이 모듈도 각 경로에서 관리자를 다시 확인한다(남용 방지 경계).
변경은 감사 로그에 남는다(`detail = {changes, before, after}`).
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.api.deps import require_admin
from biznotes_core.config import get_settings
from biznotes_core.core import rate_limit
from biznotes_core.core.db import get_db
from biznotes_core.models import User
from biznotes_core.schemas.limits import (
    NotebookSourceLimitOut,
    NotebookSourceLimitUpdate,
    RateLimitItem,
    RateLimitsOut,
    RateLimitsUpdate,
    SourceUploadSettingsOut,
    SourceUploadSettingsUpdate,
    UsageDefaults,
)
from biznotes_core.services import rate_limit_settings, usage_guard
from biznotes_core.services.ingestion import source_limits, upload_limits
from biznotes_core.services.settings_store import update_overrides

router = APIRouter()


async def _rate_limits_out(db: AsyncSession) -> RateLimitsOut:
    settings = get_settings()
    overrides = await rate_limit_settings.get_rate_limit_overrides(db)
    return RateLimitsOut(
        enabled=settings.rate_limit_enabled,
        cache_ttl_seconds=rate_limit.RATE_LIMIT_OVERRIDE_TTL_SECONDS,
        items=[
            RateLimitItem(**item)
            for item in rate_limit_settings.describe_rate_limits(settings, overrides)
        ],
    )


@router.get("/rate-limits", response_model=RateLimitsOut)
async def get_rate_limits(
    _admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> RateLimitsOut:
    """키별 env 값·재정의·적용값. 전체 켜기·끄기(`enabled`)는 env 전용이라 읽기만 한다."""
    return await _rate_limits_out(db)


@router.put("/rate-limits", response_model=RateLimitsOut)
async def put_rate_limits(
    body: RateLimitsUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> RateLimitsOut:
    """부분 갱신 — 본문에 없는 키는 그대로, `null`은 재정의 삭제(env 값으로 돌아간다).

    범위(`min_value`~`max_value`) 밖은 422다. 이 프로세스는 곧바로, 다른 API 프로세스는
    `cache_ttl_seconds` 안에 반영한다.
    """
    changes = {key: getattr(body, key) for key in sorted(body.model_fields_set)}
    if changes:
        await update_overrides(
            db,
            rate_limit_settings.SETTING_KEY,
            changes,
            actor_id=admin.id,
            action="rate_limits.update",
        )
        await db.commit()
        rate_limit.invalidate_overrides()
    return await _rate_limits_out(db)


@router.get("/usage-defaults", response_model=UsageDefaults)
async def get_usage_defaults(
    _admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> UsageDefaults:
    """시스템 기본 일일 한도 — null은 기본값 없음(무제한)."""
    return UsageDefaults(**await usage_guard.get_usage_defaults(db))


@router.put("/usage-defaults", response_model=UsageDefaults)
async def put_usage_defaults(
    body: UsageDefaults,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> UsageDefaults:
    """부분 갱신 — 본문에 없는 키는 그대로, `null`은 기본값 삭제(무제한).

    사용자별 값(`PUT /admin/users/{id}/limits`)이 있는 필드는 그 값이 우선한다. 범위는
    0~2,147,483,647(0은 막음)이고 밖은 422다. 다음 한도 검사부터 적용된다(캐시 없음).
    """
    changes = {key: getattr(body, key) for key in sorted(body.model_fields_set)}
    if changes:
        await update_overrides(
            db,
            usage_guard.USAGE_DEFAULTS_KEY,
            changes,
            actor_id=admin.id,
            action="usage_defaults.update",
        )
        await db.commit()
    return UsageDefaults(**await usage_guard.get_usage_defaults(db))


async def _source_upload_out(db: AsyncSession) -> SourceUploadSettingsOut:
    settings = get_settings()
    override = await upload_limits.get_upload_override(db)
    return SourceUploadSettingsOut(
        max_upload_bytes=override if override is not None else settings.source_max_upload_bytes,
        env_max_upload_bytes=settings.source_max_upload_bytes,
        override=override,
    )


@router.get("/source-upload", response_model=SourceUploadSettingsOut)
async def get_source_upload(
    _admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> SourceUploadSettingsOut:
    """소스 업로드 최대 크기 — 적용값·env 값·재정의·하드 상한(바이트)."""
    return await _source_upload_out(db)


@router.put("/source-upload", response_model=SourceUploadSettingsOut)
async def put_source_upload(
    body: SourceUploadSettingsUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SourceUploadSettingsOut:
    """`max_upload_bytes`(1~하드 상한) 재정의, `null`은 삭제(env 값). 범위 밖은 422다.

    다음 업로드부터 적용된다(캐시 없음). 운영 프록시의 본문 상한이 더 작으면 그 값이 실제 상한이다.
    """
    await update_overrides(
        db,
        upload_limits.SETTING_KEY,
        {upload_limits.OVERRIDE_FIELD: body.max_upload_bytes},
        actor_id=admin.id,
        action="source_upload.update",
    )
    await db.commit()
    return await _source_upload_out(db)


async def _notebook_source_limit_out(db: AsyncSession) -> NotebookSourceLimitOut:
    settings = get_settings()
    override = await source_limits.get_max_sources_override(db)
    return NotebookSourceLimitOut(
        max_sources_per_notebook=(
            override if override is not None else settings.max_sources_per_notebook
        ),
        env_max_sources_per_notebook=settings.max_sources_per_notebook,
        override=override,
    )


@router.get("/notebook-source-limit", response_model=NotebookSourceLimitOut)
async def get_notebook_source_limit(
    _admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> NotebookSourceLimitOut:
    """노트북당 소스 상한 — 적용값·env 값·재정의."""
    return await _notebook_source_limit_out(db)


@router.put("/notebook-source-limit", response_model=NotebookSourceLimitOut)
async def put_notebook_source_limit(
    body: NotebookSourceLimitUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> NotebookSourceLimitOut:
    """`max_sources_per_notebook`(1~2,147,483,647) 재정의, `null`은 삭제(env 값). 범위 밖은 422다.

    다음 추가부터 적용된다(캐시 없음). 낮춰도 기존 소스는 그대로 두고 추가만 거부한다.
    """
    await update_overrides(
        db,
        source_limits.SETTING_KEY,
        {source_limits.OVERRIDE_FIELD: body.max_sources_per_notebook},
        actor_id=admin.id,
        action="notebook_source_limit.update",
    )
    await db.commit()
    return await _notebook_source_limit_out(db)
