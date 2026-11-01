"""어드민 — 스킬 관리: enabled 토글, 공개 범위, 공개 version, 검토 큐 (표준 계획 D8·D12).

두 제품의 `api/v1/admin/skills.py`를 코어로 옮겼다. 프로젝트가 `/admin` 집계 라우터에
`require_admin`을 걸어 조립한다. 이 모듈도 각 경로에서 관리자를 다시 확인한다.

관리자도 이 모듈의 **검토 경로 밖에서는 비소유자 규칙**을 따른다(D8). 비공개 version의
파일은 검토 전용 엔드포인트(`/admin/skills/versions/{vid}/files`)로만 읽는다.
"""

import uuid
from typing import cast

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.api.deps import require_admin
from biznotes_core.core.db import get_db
from biznotes_core.models import Skill, SkillReviewRequest, SkillVersion, User
from biznotes_core.schemas.enums import ChatEntryReasonValue, SkillReviewStatus
from biznotes_core.schemas.skill import (
    AdminPublishedVersionSet,
    AdminSkillOut,
    AdminSkillUpdate,
    SkillFileOut,
    SkillReviewDecision,
    SkillReviewRequestOut,
)
from biznotes_core.services.agents.chat_entry import resolve_wrapper_chat_entries
from biznotes_core.services.audit import record
from biznotes_core.services.skills import exposure
from biznotes_core.services.skills import package as pkg
from biznotes_core.services.skills import service as skill_service

router = APIRouter(prefix="/skills")


def _exposure_http(e: exposure.ExposureError) -> HTTPException:
    return HTTPException(e.status_code, f"[{e.reason}] {e}")


async def _pending_counts(db: AsyncSession, skill_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not skill_ids:
        return {}
    rows = await db.execute(
        select(SkillVersion.skill_id, func.count(SkillReviewRequest.id))
        .join(SkillReviewRequest, SkillReviewRequest.version_id == SkillVersion.id)
        .where(SkillVersion.skill_id.in_(skill_ids), SkillReviewRequest.status == "pending")
        .group_by(SkillVersion.skill_id)
    )
    return dict(rows.tuples().all())


async def _admin_outs(db: AsyncSession, skills: list[Skill], admin: User) -> list[AdminSkillOut]:
    ids = [skill.id for skill in skills]
    wrappers = await skill_service.get_wrapper_ids(db, ids)
    pending = await _pending_counts(db, ids)
    # 채팅 진입 판정은 호출자(관리자) 기준이다 — 관리자 예외 없이 #85 정본 규칙 그대로(#135)
    entries = await resolve_wrapper_chat_entries(db, admin, list(wrappers.values()))
    result: list[AdminSkillOut] = []
    for skill in skills:
        item = AdminSkillOut.model_validate(skill)
        item.wrapper_agent_id = wrapper_id = wrappers.get(skill.id)
        item.pending_review_count = pending.get(skill.id, 0)
        if wrapper_id is not None:
            item.chat_entry_reason = cast(ChatEntryReasonValue, entries[wrapper_id].reason.value)
        result.append(item)
    return result


async def _get_skill(db: AsyncSession, skill_id: uuid.UUID) -> Skill:
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
    return skill


@router.get("", response_model=list[AdminSkillOut])
async def list_skills_admin(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[AdminSkillOut]:
    rows = await db.execute(select(Skill).order_by(Skill.visibility, Skill.name))
    return await _admin_outs(db, list(rows.scalars().all()), admin)


@router.patch("/{skill_id}", response_model=AdminSkillOut)
async def update_skill_admin(
    skill_id: uuid.UUID,
    body: AdminSkillUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AdminSkillOut:
    """enabled 토글과 공개 범위 변경(D8 경로 3).

    - 승격(→system, private→org)에는 `version_id`가 필요하고, 그 version의 `expose_*`
      승인으로 기록된다. 철회되었거나 private로 강등된 시드의 복원 경로이기도 하다.
    - system→org는 `expose_system` 승인만 철회한다. →private는 철회와 같다(보류).
    - 즉시 차단(kill-switch)은 `enabled=false`다.
    """
    skill = await _get_skill(db, skill_id)
    if body.visibility is not None:
        try:
            await exposure.admin_change_visibility(
                db, skill, body.visibility, version_id=body.version_id, admin_id=admin.id
            )
        except exposure.ExposureError as e:
            raise _exposure_http(e) from e
    if body.enabled is not None and body.enabled != skill.enabled:
        skill.enabled = body.enabled
        await exposure.sync_wrapper_flags(db, skill)
        action = "skill.disable" if body.enabled is False else "skill.enable"
        await record(db, admin.id, action, skill.name, {"skill_id": str(skill.id)})
    await db.commit()
    await db.refresh(skill)
    return (await _admin_outs(db, [skill], admin))[0]


@router.post("/{skill_id}/published-version", response_model=AdminSkillOut)
async def set_published_version_admin(
    skill_id: uuid.UUID,
    body: AdminPublishedVersionSet,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AdminSkillOut:
    """공개 version 지정(D8 경로 4). `null`은 철회이고 보류(`exposure_hold`)를 세운다."""
    skill = await _get_skill(db, skill_id)
    try:
        await exposure.admin_set_published_version(
            db, skill, body.version_id, admin_id=admin.id
        )
    except exposure.ExposureError as e:
        raise _exposure_http(e) from e
    await db.commit()
    await db.refresh(skill)
    return (await _admin_outs(db, [skill], admin))[0]


@router.get("/review-requests", response_model=list[SkillReviewRequestOut])
async def list_review_requests(
    status_filter: SkillReviewStatus | None = Query(default="pending", alias="status"),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[SkillReviewRequestOut]:
    """검토 큐 — 기본은 `pending` 요청이다."""
    stmt = (
        select(SkillReviewRequest, Skill.id, Skill.name, SkillVersion.version)
        .join(SkillVersion, SkillReviewRequest.version_id == SkillVersion.id)
        .join(Skill, SkillVersion.skill_id == Skill.id)
        .order_by(SkillReviewRequest.created_at)
    )
    if status_filter is not None:
        stmt = stmt.where(SkillReviewRequest.status == status_filter)
    result: list[SkillReviewRequestOut] = []
    for request, skill_id, skill_name, version_number in (await db.execute(stmt)).tuples().all():
        item = SkillReviewRequestOut.model_validate(request)
        item.skill_id, item.skill_name, item.version = skill_id, skill_name, version_number
        result.append(item)
    return result


async def _request_out(db: AsyncSession, request: SkillReviewRequest) -> SkillReviewRequestOut:
    out = SkillReviewRequestOut.model_validate(request)
    version = await db.get(SkillVersion, request.version_id)
    if version is not None:
        skill = await db.get(Skill, version.skill_id)
        out.version = version.version
        out.skill_id = version.skill_id
        out.skill_name = skill.name if skill is not None else None
    return out


@router.post("/review-requests/{request_id}/approve", response_model=SkillReviewRequestOut)
async def approve_review_request(
    request_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> SkillReviewRequestOut:
    """검토 승인(D8 경로 5). `expose_*`는 같은 트랜잭션에서 공개까지 끝낸다.

    요청 뒤 상태가 바뀌었으면 409 `stale`이고 요청은 pending으로 남는다.
    """
    try:
        request = await exposure.approve_review_request(db, request_id, admin_id=admin.id)
    except exposure.ExposureError as e:
        await db.rollback()
        raise _exposure_http(e) from e
    await db.commit()
    return await _request_out(db, request)


@router.post("/review-requests/{request_id}/reject", response_model=SkillReviewRequestOut)
async def reject_review_request(
    request_id: uuid.UUID,
    body: SkillReviewDecision | None = None,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> SkillReviewRequestOut:
    """검토 거절. 거절된 (version, kind)는 다시 요청할 수 없다 — 새 version이 필요하다."""
    try:
        request = await exposure.reject_review_request(
            db, request_id, admin_id=admin.id, note=body.note if body else None
        )
    except exposure.ExposureError as e:
        await db.rollback()
        raise _exposure_http(e) from e
    await db.commit()
    return await _request_out(db, request)


@router.post("/review-requests/{request_id}/withdraw", response_model=SkillReviewRequestOut)
async def withdraw_review_approval(
    request_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> SkillReviewRequestOut:
    """`scripts` 승인 철회(R4-12). 공개 승인은 공개 version 철회(경로 4)로 되돌린다."""
    try:
        request = await exposure.withdraw_scripts_approval(db, request_id, admin_id=admin.id)
    except exposure.ExposureError as e:
        await db.rollback()
        raise _exposure_http(e) from e
    await db.commit()
    return await _request_out(db, request)


async def _get_version(db: AsyncSession, version_id: uuid.UUID) -> SkillVersion:
    version = await db.get(SkillVersion, version_id)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found")
    return version


@router.get("/versions/{version_id}/files", response_model=list[SkillFileOut])
async def list_version_files_for_review(
    version_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[SkillFileOut]:
    """검토 전용 — 비공개 version의 파일 트리도 읽는다."""
    version = await _get_version(db, version_id)
    return [SkillFileOut.model_validate(item) for item in (version.file_tree or [])]


@router.get("/versions/{version_id}/files/{path:path}")
async def get_version_file_for_review(
    version_id: uuid.UUID,
    path: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> Response:
    """검토 전용 — 비공개 version의 파일 원문(스크립트 포함)을 텍스트로 읽는다.

    소유자만 볼 수 있던 내용을 관리자가 읽는 자리이므로 **감사 로그를 남긴다**.
    사용자 내보내기(`skill.export`)와 같은 등급이다.
    """
    version = await _get_version(db, version_id)
    skill = await db.get(Skill, version.skill_id)
    await record(
        db,
        admin.id,
        "skill.review.read",
        skill.name if skill is not None else "",
        {
            "skill_id": str(version.skill_id),
            "version_id": str(version_id),
            "path": path,
        },
    )
    await db.commit()
    try:
        data = await anyio.to_thread.run_sync(pkg.get_version_file, version_id, path)
    except Exception as e:  # 파일 없음/검증 실패 등 어떤 사유든 404로 응답 (의도적 catch-all)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found") from e
    return Response(
        content=data,
        media_type="text/plain; charset=utf-8",
        headers={"X-Content-Type-Options": "nosniff"},
    )
