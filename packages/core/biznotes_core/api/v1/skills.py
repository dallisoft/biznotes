"""파일 기반 Agent Skill API (skill_plan.md §8 · 표준 계획 D8·D10·D12).

두 제품의 `api/v1/skills.py`를 코어로 옮겼다(S-HCI-3). 제품은 이 라우터를 그대로
조립한다 — 두 제품 사이의 차이가 남지 않아 팩토리 인자가 필요 없다.

실행은 신규 엔드포인트 없이 기존 POST /notebooks/{id}/agent-runs로 한다(§8.3) — freeze가
스킬의 wrapper Agent(per-Skill 1개)를 upsert하고 그 agent_id를 반환한다.

**노출 모델(D8)**: 비소유자에게 보이는 내용은 모두 공개 version(`exposed_version_id`)에서
나온다. 목록·상세·version 목록·version 파일·복제·내보내기가 같은 규칙을 쓴다. 공개
포인터는 `exposure.py`의 관문만 바꾼다.
"""

import uuid
from typing import Annotated, Literal, cast
from urllib.parse import quote

import anyio
import structlog
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.api.deps import get_current_user
from biznotes_core.api.patch import non_null_changes
from biznotes_core.api.routing import RedactedValidationRoute
from biznotes_core.config import get_settings
from biznotes_core.core.db import get_db
from biznotes_core.models import AgentRun, Skill, SkillReviewRequest, SkillVersion, User
from biznotes_core.schemas.agent import AgentInput, ParamsSchema
from biznotes_core.schemas.enums import ChatEntryReasonValue, SkillVersionStatus
from biznotes_core.schemas.skill import (
    SkillCatalogEntryOut,
    SkillCatalogOut,
    SkillCloneOut,
    SkillCreate,
    SkillFileOut,
    SkillFileWrite,
    SkillFreezeOut,
    SkillFreezeRequest,
    SkillFromArtifactRequest,
    SkillFrontmatterOut,
    SkillFrontmatterWrite,
    SkillImportCandidateOut,
    SkillImportCandidatesOut,
    SkillImportOut,
    SkillImportUrlCandidatesRequest,
    SkillImportUrlRequest,
    SkillOut,
    SkillPublishRequest,
    SkillReviewRequestCreate,
    SkillReviewRequestOut,
    SkillUpdate,
    SkillValidateOut,
    SkillVersionMetadataOut,
    SkillVersionOut,
    SkillVersionPublishOut,
)
from biznotes_core.services.agents.chat_entry import ChatEntry, resolve_wrapper_chat_entries
from biznotes_core.services.audit import record
from biznotes_core.services.skills import exposure
from biznotes_core.services.skills import package as pkg
from biznotes_core.services.skills import service as svc
from biznotes_core.services.skills.authoring import frontmatter_form, rewrite_frontmatter
from biznotes_core.services.skills.parser import SkillParseError
from biznotes_core.services.skills.runtime import SkillCatalog, params_schema_for_version
from biznotes_core.services.skills.runtime import runtime_enabled as skill_runtime_enabled
from biznotes_core.services.skills.sandbox import backend_enabled as skill_scripts_enabled
from biznotes_core.services.skills.validator import SkillValidationError

logger = structlog.get_logger()
# 원격 가져오기의 URL(서명·토큰 query)과 `source_ref`가 요청 검증 422 본문으로 되돌아가지
# 않게 입력값을 가린다(#402 리뷰). 라우터 전체에 걸려 다른 스킬 엔드포인트의 422도 `input`이 없다.
router = APIRouter(prefix="/skills", tags=["skills"], route_class=RedactedValidationRoute)


def content_disposition_attachment(filename: str) -> str:
    """RFC 6266 `Content-Disposition` 값을 만든다(D10). 어떤 입력에도 예외를 던지지 않는다.

    Starlette는 응답 헤더를 **latin-1**로 인코딩한다. oracle이 허용하는 Unicode 표준
    이름(`도구`)을 그대로 넣으면 500이 나므로 ASCII fallback `filename=`과 RFC 5987
    `filename*=UTF-8''…`를 함께 보낸다(sl 리뷰 `2-03`).

    **응답 분할 방어(sl 리뷰 `2-16`)**: `encode("ascii","ignore")`는 제어 문자를 지우지
    않는다. 이 함수가 헤더 생성의 단일 경계이므로 CR/LF를 포함한 제어 문자를 먼저 제거한다.
    """
    if any(ch in filename for ch in "\r\n"):
        logger.warning("skill.export.filename_control_chars", reason="control_chars_stripped")
    filename = "".join(ch for ch in filename if ch.isprintable())
    stem, _, suffix = filename.rpartition(".")
    if not stem:  # 확장자가 없는 이름
        stem, suffix = filename, ""
    ascii_stem = stem.encode("ascii", "ignore").decode("ascii")
    ascii_stem = "".join(c for c in ascii_stem if c not in '"\\').strip(" .-")
    ascii_suffix = suffix.encode("ascii", "ignore").decode("ascii")
    ascii_suffix = "".join(c for c in ascii_suffix if c not in '"\\').strip(" .-")
    if not ascii_stem:
        # 이름 전체가 비ASCII였다 — fallback은 의미를 잃지만 헤더는 항상 유효해야 한다.
        ascii_stem = "skill-package"
    ascii_name = f"{ascii_stem}.{ascii_suffix}" if ascii_suffix else ascii_stem
    quoted = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


def _unprocessable(e: SkillValidationError | SkillParseError) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"[{e.reason}] {e}")


def _exposure_http(e: exposure.ExposureError) -> HTTPException:
    return HTTPException(e.status_code, f"[{e.reason}] {e}")


# PDF는 뷰어 스크립트를 품을 수 있어 사용자 업로드 파일로는 inline에서 뺀다(리뷰 G-07).
_SAFE_INLINE_BINARY_TYPES = frozenset({"image/png", "image/jpeg"})


def _safe_file_response(data: bytes, path: str) -> Response:
    """스킬 파일 응답. 능동 콘텐츠 타입(html·svg 등)으로는 절대 내리지 않는다.

    - png·jpeg는 그 타입으로 inline
    - UTF-8 텍스트는 기존 계약대로 `text/plain; charset=utf-8` inline(편집기가 읽는다)
    - 그 밖의 바이너리는 `application/octet-stream` attachment
    """
    headers = {"X-Content-Type-Options": "nosniff"}
    media_type = pkg.media_type_for_path(path)
    if media_type in _SAFE_INLINE_BINARY_TYPES:
        return Response(content=data, media_type=media_type, headers=headers)
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        headers["Content-Disposition"] = content_disposition_attachment(path.rsplit("/", 1)[-1])
        return Response(content=data, media_type="application/octet-stream", headers=headers)
    return Response(content=data, media_type="text/plain; charset=utf-8", headers=headers)


def review_pending_response(
    request: SkillReviewRequest, *, status_code: int = status.HTTP_409_CONFLICT
) -> JSONResponse:
    """검토 대기 응답 — `{"reason": "review_pending", "request_id": …}`(D8 경로 2·6)."""
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": "관리자 검토가 필요합니다. 요청이 검토 큐에 등록되었습니다",
            "reason": "review_pending",
            "request_id": str(request.id),
        },
    )


def _to_out(
    skill: Skill,
    user: User,
    wrapper_id: uuid.UUID | None,
    version: SkillVersion | None,
    out_cls: type[SkillOut] = SkillOut,
    chat_entry: ChatEntry | None = None,
) -> SkillOut:
    out = out_cls.model_validate(skill)
    if not exposure.is_owner(skill, user.id):
        # 비소유자에게 current_version_id는 노출 version의 별칭이다(R3-12, 한 릴리스 병행)
        exposed = exposure.exposed_version_id(skill)
        out.current_version_id = exposed
        out.published_version_id = exposed
    out.wrapper_agent_id = wrapper_id
    if version is not None:
        manifest = version.manifest or {}
        out.params_schema = ParamsSchema.model_validate(params_schema_for_version(version))
        if manifest.get("input"):
            out.input = AgentInput.model_validate(manifest["input"])
    out.can_edit = svc.can_edit(skill, user)
    out.can_run = svc.can_run(skill, user)
    if chat_entry is not None:
        out.chat_entry_reason = cast(ChatEntryReasonValue, chat_entry.reason.value)
    return out


async def _outs(db: AsyncSession, skills: list[Skill], user: User) -> list[SkillOut]:
    wrappers = await svc.get_wrapper_ids(db, [s.id for s in skills])
    versions = await svc.get_view_versions(db, skills, user)
    # 호출자 기준 채팅 진입 판정 — wrapper 전체를 쿼리 한 번으로(#135)
    entries = await resolve_wrapper_chat_entries(db, user, list(wrappers.values()))
    out: list[SkillOut] = []
    for s in skills:
        wrapper_id = wrappers.get(s.id)
        entry = entries.get(wrapper_id) if wrapper_id is not None else None
        out.append(_to_out(s, user, wrapper_id, versions.get(s.id), chat_entry=entry))
    return out


async def _get_skill_for_view(db: AsyncSession, skill_id: uuid.UUID, user: User) -> Skill:
    skill = await db.get(Skill, skill_id)
    if skill is None or not svc.can_view(skill, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
    return skill


async def _get_skill_for_edit(db: AsyncSession, skill_id: uuid.UUID, user: User) -> Skill:
    skill = await db.get(Skill, skill_id)
    if skill is None or not svc.can_edit(skill, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
    return skill


async def _lock_draft(db: AsyncSession, skill: Skill) -> None:
    """draft를 쓰기 전에 스킬 행을 잠근다 — freeze·다시 가져오기와 직렬화(#402 리뷰)."""
    try:
        await svc.lock_skill_row(db, skill.id)
    except svc.SkillGone as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found") from e


async def _get_version_for_view(
    db: AsyncSession, version_id: uuid.UUID, user: User
) -> tuple[SkillVersion, Skill]:
    """소유자는 자기 스킬의 모든 version, 비소유자는 노출 version 하나만 본다(D8)."""
    version = await db.get(SkillVersion, version_id)
    skill = await db.get(Skill, version.skill_id) if version is not None else None
    if version is None or skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found")
    if not exposure.is_owner(skill, user.id) and exposure.exposed_version_id(skill) != version.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found")
    return version, skill


# ---------- 목록 / 상세 ----------


@router.get("", response_model=list[SkillOut])
async def list_skills(
    q: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    visibility: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SkillOut]:
    """내 것 + 꺼지지 않은 org·system 스킬. 내용은 노출 version에서만 나온다(D8)."""
    stmt = select(Skill).where(
        or_(
            Skill.owner_id == user.id,
            Skill.visibility.in_(exposure.EXPOSED_VISIBILITIES) & Skill.enabled.is_(True),
        )
    )
    if visibility:
        stmt = stmt.where(Skill.visibility == visibility)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(func.lower(Skill.name).like(like), func.lower(Skill.description).like(like))
        )
    stmt = stmt.order_by(Skill.updated_at.desc())
    rows = list((await db.execute(stmt)).scalars().all())
    if tag:
        rows = [s for s in rows if tag in (s.tags or [])]
    return await _outs(db, rows, user)


@router.get("/catalog", response_model=SkillCatalogOut)
async def get_skill_catalog(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SkillCatalogOut:
    """이 사용자의 채팅·에이전트 스킬 카탈로그(D6-1). 같은 name에 가려진 항목도 함께 준다(D2)."""
    catalog = await SkillCatalog.for_user(db, user.id)
    source = {0: "own", 1: "system", 2: "org"}
    entries = [
        SkillCatalogEntryOut(
            name=e.name,
            description=e.description,
            skill_id=e.skill_id,
            version_id=e.version_id,
            source=source[e.rank],  # type: ignore[arg-type]
        )
        for e in catalog.entries
    ]
    entries += [
        SkillCatalogEntryOut(
            name=hidden.name,
            description=hidden.description,
            skill_id=hidden.skill_id,
            version_id=hidden.version_id,
            source=source[hidden.rank],  # type: ignore[arg-type]
            shadowed_by=winner.skill_id,
        )
        for hidden, winner in catalog.shadowed
    ]
    return SkillCatalogOut(
        enabled=skill_runtime_enabled(),
        scripts_enabled=skill_runtime_enabled() and skill_scripts_enabled(),
        entries=entries,
        truncated=catalog.truncated,
    )


@router.get("/versions/{version_id}/files", response_model=list[SkillFileOut])
async def list_version_files(
    version_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SkillFileOut]:
    version, _ = await _get_version_for_view(db, version_id, user)
    return [SkillFileOut.model_validate(item) for item in (version.file_tree or [])]


@router.get("/versions/{version_id}/files/{path:path}")
async def get_version_file(
    version_id: uuid.UUID,
    path: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await _get_version_for_view(db, version_id, user)
    try:
        data = await anyio.to_thread.run_sync(pkg.get_version_file, version_id, path)
    except Exception as e:  # 파일 없음/검증 실패 등 어떤 사유든 404로 응답 (의도적 catch-all)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found") from e
    return _safe_file_response(data, path)


@router.get("/versions/{version_id}/metadata", response_model=SkillVersionMetadataOut)
async def get_version_metadata(
    version_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SkillVersionMetadataOut:
    version, _ = await _get_version_for_view(db, version_id, user)
    return SkillVersionMetadataOut.model_validate(svc.version_metadata(version))


@router.get("/versions/{version_id}/export")
async def export_version(
    version_id: uuid.UUID,
    layout: Literal["wrapped", "flat"] = Query(default="wrapped"),
    portable: bool = Query(default=False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """동결 version을 표준 ZIP으로 내보낸다(D10). 저장된 바이트를 그대로 담는다.

    비소유자는 공개 version만 내보낼 수 있다. `legacy_v1`은 `X-Skill-Validation-Profile`
    헤더로 표준 비준수를 알린다.
    """
    version, skill = await _get_version_for_view(db, version_id, user)
    try:
        filename, data = await svc.export_version_zip(
            version, skill, layout=layout, portable=portable
        )
    except SkillValidationError as e:
        raise _unprocessable(e) from e
    await record(
        db,
        user.id,
        "skill.export",
        skill.name,
        {
            "skill_id": str(skill.id),
            "version_id": str(version.id),
            "version": version.version,
            "layout": layout,
            "portable": portable,
        },
    )
    await db.commit()
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": content_disposition_attachment(filename),
            "X-Skill-Validation-Profile": version.validation_profile,
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{skill_id}", response_model=SkillOut)
async def get_skill(
    skill_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SkillOut:
    skill = await _get_skill_for_view(db, skill_id, user)
    return (await _outs(db, [skill], user))[0]


@router.get("/{skill_id}/versions", response_model=list[SkillVersionOut])
async def list_skill_versions(
    skill_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SkillVersionOut]:
    """소유자는 모든 version, 비소유자는 공개 version 하나만 본다(D8)."""
    skill = await _get_skill_for_view(db, skill_id, user)
    stmt = select(SkillVersion).where(SkillVersion.skill_id == skill_id)
    if not exposure.is_owner(skill, user.id):
        stmt = stmt.where(SkillVersion.id == exposure.exposed_version_id(skill))
    rows = await db.execute(stmt.order_by(SkillVersion.version.desc()))
    return [SkillVersionOut.model_validate(v) for v in rows.scalars().all()]


@router.get("/{skill_id}/files", response_model=list[SkillFileOut])
async def list_draft_files(
    skill_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SkillFileOut]:
    """draft 작업본 파일 트리 (편집기용) — owner만."""
    await _get_skill_for_edit(db, skill_id, user)
    files = await anyio.to_thread.run_sync(pkg.load_draft_files, skill_id)
    return [SkillFileOut.model_validate(item) for item in pkg.build_file_tree(files)]


@router.get("/{skill_id}/files/{path:path}")
async def get_draft_file(
    skill_id: uuid.UUID,
    path: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await _get_skill_for_edit(db, skill_id, user)
    try:
        data = await anyio.to_thread.run_sync(pkg.get_draft_file, skill_id, path)
    except Exception as e:  # 파일 없음/검증 실패 등 어떤 사유든 404로 응답 (의도적 catch-all)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found") from e
    return _safe_file_response(data, path)


@router.get("/{skill_id}/review-requests", response_model=list[SkillReviewRequestOut])
async def list_skill_review_requests(
    skill_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SkillReviewRequestOut]:
    """소유자가 자기 스킬의 검토 요청 상태를 본다."""
    skill = await _get_skill_for_edit(db, skill_id, user)
    rows = await db.execute(
        select(SkillReviewRequest, SkillVersion.version)
        .join(SkillVersion, SkillReviewRequest.version_id == SkillVersion.id)
        .where(SkillVersion.skill_id == skill.id)
        .order_by(SkillReviewRequest.created_at.desc())
    )
    result: list[SkillReviewRequestOut] = []
    for request, version_number in rows.tuples().all():
        item = SkillReviewRequestOut.model_validate(request)
        item.skill_id, item.skill_name, item.version = skill.id, skill.name, version_number
        result.append(item)
    return result


# ---------- 생성 / import / clone ----------


@router.post("", response_model=SkillOut, status_code=status.HTTP_201_CREATED)
async def create_skill(
    body: SkillCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SkillOut:
    skill = await svc.create_skill(db, user, body.name, body.description)
    await record(db, user.id, "skill.create", body.name, {"skill_id": str(skill.id)})
    await db.commit()
    await db.refresh(skill)
    return _to_out(skill, user, None, None)


async def _read_skill_zip_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if len(data) > get_settings().skill_import_max_upload_bytes:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "ZIP이 너무 큽니다")
    return data


def _candidates_out(result: svc.ImportCandidates) -> SkillImportCandidatesOut:
    return SkillImportCandidatesOut(
        candidates=[
            SkillImportCandidateOut.model_validate(candidate, from_attributes=True)
            for candidate in result.candidates
        ],
        archive_sha256=result.archive_sha256,
        truncated=result.truncated,
    )


@router.post("/import-zip/candidates", response_model=SkillImportCandidatesOut)
async def list_skill_zip_candidates(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
) -> SkillImportCandidatesOut:
    """ZIP 안의 스킬 후보(임의 깊이)와 사전 점검 요약을 돌려준다(D4 Phase 2·#402)."""
    del user  # 인증 의존성을 통과한 요청이며 파일을 저장하지 않는다.
    try:
        data = await _read_skill_zip_upload(file)
        result = await anyio.to_thread.run_sync(svc.zip_import_candidates, data)
    except SkillValidationError as e:
        raise _unprocessable(e) from e
    return _candidates_out(result)


@router.post("/import-url/candidates", response_model=SkillImportCandidatesOut)
async def list_skill_url_candidates(
    body: SkillImportUrlCandidatesRequest,
    user: User = Depends(get_current_user),
) -> SkillImportCandidatesOut:
    """URL archive의 스킬 후보와 `archive_sha256`을 돌려준다(#402).

    fetch는 `import-url`과 같은 SSRF 관문·업로드 상한을 쓰고 아무것도 저장하지 않는다. 이어지는
    `import-url`은 이 `archive_sha256`을 `sha256`으로 보내 같은 archive임을 고정한다.
    """
    del user  # 인증 의존성을 통과한 요청이며 archive를 저장하지 않는다.
    try:
        result = await svc.url_import_candidates(body.url)
    except SkillValidationError as e:
        raise _unprocessable(e) from e
    return _candidates_out(result)


async def _get_reimport_target(db: AsyncSession, skill_id: uuid.UUID, user: User) -> Skill:
    """다시 가져오기 대상(#402 D2) — 소유자의 가져온 스킬만. 원격 fetch·압축 해제 전에 부른다.

    공개 범위와 무관하다(공개·`scripts` 승인은 version 단위라 새 version이 다시 검토된다).
    소유자가 아니면(system 시드 포함) 편집 계열처럼 존재를 숨겨 404다.
    """
    skill = await _get_skill_for_edit(db, skill_id, user)
    if skill.origin != "imported":
        raise _not_imported()
    return skill


def _not_imported() -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT, "[target_not_imported] 가져온 스킬만 다시 가져올 수 있습니다"
    )


async def _import_out(
    db: AsyncSession, user: User, result: svc.ImportResult, *, source: str, reimport: bool
) -> SkillImportOut:
    """감사 기록·커밋 뒤 응답. 다시 가져온 스킬은 기존 wrapper·version을 그대로 보여 준다."""
    skill = result.skill
    # 표시명은 바뀔 수 있어 가져온 스킬을 skill_id로 추적한다(D13 백필)
    detail: dict[str, object] = {"source": source, "skill_id": str(skill.id)}
    if reimport:
        detail["reimport"] = True
    await record(db, user.id, "skill.import", skill.name, detail)
    await db.commit()
    await db.refresh(skill)
    if reimport:
        out = SkillImportOut.model_validate((await _outs(db, [skill], user))[0].model_dump())
    else:
        out = cast(SkillImportOut, _to_out(skill, user, None, None, SkillImportOut))
    out.import_report = result.import_report
    return out


@router.post("/import-zip", response_model=SkillImportOut, status_code=status.HTTP_201_CREATED)
async def import_skill_zip(
    file: UploadFile = File(...),
    selected_skill_path: str | None = Form(default=None),
    target_skill_id: Annotated[uuid.UUID | None, Form()] = None,
    source_ref: Annotated[str | None, Form()] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SkillOut:
    """ZIP·`.skill` 가져오기(D4). 표준 위반은 거부하지 않고 `import_report`로 보고한다.

    `target_skill_id`가 있으면 그 스킬의 draft를 바꾼다(#402) — 응답은 같은 스킬이다.
    """
    if target_skill_id is not None:
        await _get_reimport_target(db, target_skill_id, user)
    try:
        result = await svc.import_zip_detailed(
            db,
            user,
            await _read_skill_zip_upload(file),
            selected_skill_path=selected_skill_path,
            target_skill_id=target_skill_id,
            source_ref=source_ref,
        )
    except svc.SkillGone as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found") from e
    except svc.SkillNotImported as e:
        raise _not_imported() from e
    except (SkillParseError, SkillValidationError) as e:
        raise _unprocessable(e) from e
    return await _import_out(db, user, result, source="zip", reimport=target_skill_id is not None)


@router.post("/import-url", response_model=SkillImportOut, status_code=status.HTTP_201_CREATED)
async def import_skill_url(
    body: SkillImportUrlRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SkillOut:
    """URL의 ZIP·`.skill`·tar.gz를 안전하게 가져온다(Phase 5).

    실제 fetch는 redirect·DNS rebinding까지 검사하는 코어 SSRF 관문만 사용한다. URL 원문은
    signed URL credential을 보존하지 않도록 DB·감사 detail에 기록하지 않는다.
    `target_skill_id`의 권한은 fetch **전에** 확인한다(#402).
    """
    if body.target_skill_id is not None:
        await _get_reimport_target(db, body.target_skill_id, user)
    try:
        result = await svc.import_url_detailed(
            db,
            user,
            body.url,
            sha256=body.sha256,
            selected_skill_path=body.selected_skill_path,
            target_skill_id=body.target_skill_id,
            source_ref=body.source_ref,
        )
    except svc.SkillGone as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found") from e
    except svc.SkillNotImported as e:
        raise _not_imported() from e
    except (SkillParseError, SkillValidationError) as e:
        raise _unprocessable(e) from e
    return await _import_out(
        db, user, result, source="url", reimport=body.target_skill_id is not None
    )


@router.post("/{skill_id}/clone", response_model=SkillCloneOut)
async def clone_skill(
    skill_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SkillCloneOut:
    """복제 — 비소유자는 공개 version만 복사한다. 공개 version이 없으면 409(R2-01)."""
    src = await _get_skill_for_view(db, skill_id, user)
    try:
        clone = await svc.clone_skill(db, src, user)
    except svc.SkillCloneUnavailable as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    except svc.SkillError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e)) from e
    await record(
        db,
        user.id,
        "skill.clone",
        src.name,
        {
            "from": str(src.id),
            "skill_id": str(clone.id),
            "version_id": str(clone.forked_from_version_id)
            if clone.forked_from_version_id
            else None,
        },
    )
    await db.commit()
    await db.refresh(clone)
    return SkillCloneOut(skill_id=clone.id, status="draft")


@router.post("/from-artifact", response_model=SkillCloneOut)
async def create_skill_from_artifact(
    body: SkillFromArtifactRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SkillCloneOut:
    """'스킬 생성 도우미' 실행 아티팩트(SKILL.md+manifest 패키지)를 내 draft 스킬로 1클릭 생성."""
    from biznotes_core.services.authz import assert_can_read_run
    from biznotes_core.services.storage import get_object

    run = await db.get(AgentRun, body.run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    await assert_can_read_run(db, user, run)

    artifact = next((a for a in run.artifact_keys if a.get("name") == body.artifact_name), None)
    if artifact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact not found")

    bucket = get_settings().storage_bucket_artifacts
    data = await anyio.to_thread.run_sync(lambda: get_object(bucket, artifact["key"]))
    try:
        files = svc.parse_package_artifact(data)
        skill = await svc.create_skill_from_package(db, user, files)
    except (SkillParseError, SkillValidationError) as e:
        raise _unprocessable(e) from e
    except Exception as e:  # parser 등
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"패키지 파싱 실패: {e}"
        ) from e
    await record(
        db, user.id, "skill.create", skill.name, {"source": "artifact", "skill_id": str(skill.id)}
    )
    await db.commit()
    await db.refresh(skill)
    return SkillCloneOut(skill_id=skill.id, status="draft")


# ---------- 편집 / 파일 ----------


@router.patch(
    "/{skill_id}",
    response_model=SkillOut,
    responses={409: {"description": "검토 필요(`reason: review_pending`) 또는 관문 규칙 위반"}},
)
async def update_skill(
    skill_id: uuid.UUID,
    body: SkillUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SkillOut | JSONResponse:
    """표시용 필드와 visibility 변경(D8 경로 2).

    visibility는 공개 관문을 거친다. 검토가 필요하면 요청만 커밋하고 409
    `review_pending`을 반환한다 — 같은 요청의 다른 필드도 적용하지 않는다(원자적 거부).
    `visibility` 외 필드의 명시적 null은 관문보다 먼저 422로 거부한다(#267). `visibility`
    null은 생략과 같다.
    """
    skill = await _get_skill_for_edit(db, skill_id, user)
    changes = non_null_changes(body, nullable=("visibility",))
    visibility = changes.pop("visibility", None)
    if visibility is not None:
        try:
            await exposure.change_visibility_by_owner(db, skill, visibility, actor_id=user.id)
        except exposure.ReviewPending as pending:
            await db.commit()
            return review_pending_response(pending.request)
        except exposure.ExposureError as e:
            raise _exposure_http(e) from e
    for field, value in changes.items():
        setattr(skill, field, value)
    await record(db, user.id, "skill.update", skill.name, {"skill_id": str(skill.id)})
    await db.commit()
    await db.refresh(skill)
    return (await _outs(db, [skill], user))[0]


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(
    skill_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """스킬 삭제. wrapper는 참조가 없으면 지우고, 있으면 비활성으로 남긴다(#116).

    패키지 오브젝트(draft·모든 version prefix)는 DB 삭제를 커밋한 뒤 지운다(#151). DB 단계가
    실패하면 오브젝트는 그대로이고, 오브젝트 삭제가 실패하면 경고만 남기고 204를 준다.
    """
    skill = await _get_skill_for_edit(db, skill_id, user)
    try:
        removal = await svc.remove_skill_wrappers(db, skill.id)
    except svc.SkillGone as e:  # 같은 스킬을 동시에 지운 요청이 먼저 커밋했다
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found") from e
    # 스킬 행을 잠근 뒤에 모은다 — 동시 freeze가 커밋한 version까지 포함한다.
    # CASCADE로 행이 사라지기 전에 모아야 한다.
    version_ids = list(
        await db.scalars(select(SkillVersion.id).where(SkillVersion.skill_id == skill.id))
    )
    await record(
        db,
        user.id,
        "skill.delete",
        skill.name,
        {
            "skill_id": str(skill.id),
            "wrappers_deleted": [str(i) for i in removal.deleted],
            "wrappers_disabled": [str(i) for i in removal.disabled],
        },
    )
    await db.delete(skill)
    await db.commit()
    await anyio.to_thread.run_sync(pkg.delete_skill_objects, skill_id, version_ids)


@router.get("/{skill_id}/frontmatter", response_model=SkillFrontmatterOut)
async def get_draft_frontmatter(
    skill_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SkillFrontmatterOut:
    """원문을 잃지 않는 경우에만 편집 폼으로 노출한다."""
    skill = await _get_skill_for_edit(db, skill_id, user)
    try:
        data = await anyio.to_thread.run_sync(pkg.get_draft_file, skill.id, "SKILL.md")
        source = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "SKILL.md는 UTF-8이어야 합니다"
        ) from e
    except Exception as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SKILL.md not found") from e
    state = frontmatter_form(source)
    return SkillFrontmatterOut(
        editable=state.editable,
        fields=state.fields,
        reason=state.reason,
        source_hash=pkg.file_hash(data),
    )


@router.put("/{skill_id}/frontmatter", response_model=SkillFrontmatterOut)
async def write_draft_frontmatter(
    skill_id: uuid.UUID,
    body: SkillFrontmatterWrite,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SkillFrontmatterOut:
    """폼 값만 SKILL.md frontmatter에 반영한다. 본문은 바이트 단위로 보존한다.

    `source_hash` 확인과 쓰기는 스킬 행 잠금 안에서 한다 — 같은 잠금을 잡는 draft 쓰기·freeze·
    다시 가져오기와 섞이지 않는다(#402 리뷰, 예전 G-06의 창).
    """
    skill = await _get_skill_for_edit(db, skill_id, user)
    await _lock_draft(db, skill)
    try:
        current = await anyio.to_thread.run_sync(pkg.get_draft_file, skill.id, "SKILL.md")
        if body.source_hash != pkg.file_hash(current):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "SKILL.md 원문이 바뀌었습니다. 최신 원문을 불러온 뒤 다시 저장하세요.",
            )
        updated = rewrite_frontmatter(current.decode("utf-8"), body.fields)
        await anyio.to_thread.run_sync(
            pkg.put_draft_file, skill.id, "SKILL.md", updated.encode("utf-8")
        )
    except UnicodeDecodeError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "SKILL.md는 UTF-8이어야 합니다"
        ) from e
    except (SkillValidationError, SkillParseError) as e:
        raise _unprocessable(e) from e
    state = frontmatter_form(updated)
    # `skills.name`·`description`은 표시용 필드라 표준 `name`과 따로 둔다(D2, D8 적용 범위).
    await record(db, user.id, "skill.frontmatter.update", skill.name, {"skill_id": str(skill.id)})
    await db.commit()
    return SkillFrontmatterOut(
        editable=state.editable,
        fields=state.fields,
        reason=state.reason,
        source_hash=pkg.file_hash(updated.encode("utf-8")),
    )


@router.post("/{skill_id}/frontmatter/validate", response_model=SkillValidateOut)
async def validate_draft_frontmatter(
    skill_id: uuid.UUID,
    body: SkillFrontmatterWrite,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SkillValidateOut:
    """저장하지 않은 폼 값을 현재 draft에 대입해 실시간 검증한다."""
    skill = await _get_skill_for_edit(db, skill_id, user)
    try:
        files = await anyio.to_thread.run_sync(pkg.load_draft_files, skill.id)
        current = files["SKILL.md"]
        if body.source_hash != pkg.file_hash(current):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "SKILL.md 원문이 바뀌었습니다. 최신 원문을 불러온 뒤 다시 검증하세요.",
            )
        preview = rewrite_frontmatter(current.decode("utf-8"), body.fields)
        files["SKILL.md"] = preview.encode("utf-8")
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SKILL.md not found") from e
    except UnicodeDecodeError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "SKILL.md는 UTF-8이어야 합니다"
        ) from e
    except (SkillValidationError, SkillParseError) as e:
        return SkillValidateOut.model_validate(
            {
                "ok": False,
                "reason": e.reason,
                "message": str(e),
                "report": svc.failure_report(e),
            }
        )
    return SkillValidateOut.model_validate(svc.validate_files_report(files))


async def _read_body_limited(request: Request, limit: int) -> bytes:
    """본문을 `limit` 바이트까지만 읽는다. 넘으면 끝까지 읽지 않고 413으로 끊는다."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "파일이 너무 큽니다")
        chunks.append(chunk)
    return b"".join(chunks)


async def _draft_file_bytes(request: Request) -> bytes:
    """기존 JSON 텍스트 계약과 비-JSON 바이트 본문을 같은 PUT에서 수용한다."""
    max_file = get_settings().skill_import_max_file_bytes
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    is_json = content_type in {"", "application/json"}
    # JSON 문자열 이스케이프(`\uXXXX`)는 원문을 최대 6배로 늘린다. 디코드한 크기는
    # `put_draft_file`이 다시 파일 한도로 검사한다.
    raw = await _read_body_limited(request, max_file * 6 + 1024 if is_json else max_file)
    if is_json:
        try:
            return SkillFileWrite.model_validate_json(raw).content.encode("utf-8")
        except ValueError as e:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "텍스트 파일 JSON 본문이 올바르지 않습니다",
            ) from e
    return raw


@router.put(
    "/{skill_id}/files/{path:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": SkillFileWrite.model_json_schema()},
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
            },
        }
    },
)
async def write_draft_file(
    skill_id: uuid.UUID,
    path: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """draft 파일 생성/수정 (upsert) — 하위 경로 수용(§8.2).

    `skills.status`를 바꾸지 않는다 — 공개 포인터에서 파생한 값이다(D8, R3-03).
    예전에는 공개 뒤 편집하면 status가 draft가 되어 `run_skill`에서 사라졌다.
    """
    skill = await _get_skill_for_edit(db, skill_id, user)
    try:
        data = await _draft_file_bytes(request)
        # 본문을 다 받은 뒤 잠근다 — 느린 업로드가 잠금을 쥐지 않게
        await _lock_draft(db, skill)
        await anyio.to_thread.run_sync(pkg.put_draft_file, skill.id, path, data)
    except SkillValidationError as e:
        raise _unprocessable(e) from e
    await db.commit()  # 잠금을 바로 놓는다


@router.delete("/{skill_id}/files/{path:path}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_draft_file(
    skill_id: uuid.UUID,
    path: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    skill = await _get_skill_for_edit(db, skill_id, user)
    if path == "SKILL.md":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "SKILL.md는 삭제할 수 없습니다")
    await _lock_draft(db, skill)
    try:
        await anyio.to_thread.run_sync(pkg.delete_draft_file, skill.id, path)
    except SkillValidationError as e:
        raise _unprocessable(e) from e
    await db.commit()  # 잠금을 바로 놓는다


# ---------- 검증 / 동결 / 공개 ----------


@router.post("/{skill_id}/validate", response_model=SkillValidateOut)
async def validate_skill(
    skill_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SkillValidateOut:
    """draft 검증 — 기존 키를 모두 유지하고 네 층 보고서 `report`를 더한다(D5).

    `ok`는 "정책 오류 없음"이다. 표준 위반(legacy)이어도 `ok=true`다.
    """
    skill = await _get_skill_for_edit(db, skill_id, user)
    return SkillValidateOut.model_validate(await svc.validate_draft_report(skill))


def _freeze_out(version: SkillVersion, wrapper_id: uuid.UUID) -> SkillFreezeOut:
    return SkillFreezeOut(
        version_id=version.id,
        version=version.version,
        wrapper_agent_id=wrapper_id,
        status=cast(SkillVersionStatus, version.status),
    )


@router.post("/{skill_id}/freeze", response_model=SkillFreezeOut)
async def freeze_skill(
    skill_id: uuid.UUID,
    body: SkillFreezeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SkillFreezeOut:
    """draft → 불변 version. 공개 포인터는 옮기지 않는다(소유자 시험 실행용, D8)."""
    skill = await _get_skill_for_edit(db, skill_id, user)
    try:
        version, wrapper = await svc.freeze_skill(
            db, skill, user, publish=False, changelog=body.changelog
        )
    except (SkillValidationError, SkillParseError) as e:
        raise _unprocessable(e) from e
    except Exception as e:
        # freeze 실패는 사유 불문 422로 응답(기존 동작 보존)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    await record(
        db,
        user.id,
        "skill.freeze",
        skill.name,
        {"skill_id": str(skill.id), "version_id": str(version.id), "version": version.version},
    )
    await db.commit()
    return _freeze_out(version, wrapper.id)


@router.post(
    "/{skill_id}/versions/{version_id}/publish",
    response_model=SkillVersionPublishOut,
    responses={202: {"model": SkillVersionPublishOut, "description": "관리자 검토 대기"}},
)
async def publish_skill_version(
    skill_id: uuid.UUID,
    version_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SkillVersionPublishOut | JSONResponse:
    """version publish(D8 경로 1). 목표 visibility는 max(현재, org)다.

    검토가 필요 없으면 즉시 공개하고 200, 필요하면 검토 요청을 커밋하고 202다.
    """
    skill = await _get_skill_for_edit(db, skill_id, user)
    try:
        version = await exposure.load_version_of(db, skill, version_id)
        outcome = await exposure.publish_version(db, skill, version, actor_id=user.id)
    except exposure.ExposureError as e:
        raise _exposure_http(e) from e
    await db.commit()
    body = SkillVersionPublishOut(
        status=outcome.status,
        version_id=outcome.version_id,
        request_id=outcome.request.id if outcome.request is not None else None,
    )
    if outcome.status == "review_pending":
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED, content=body.model_dump(mode="json")
        )
    return body


@router.post(
    "/{skill_id}/versions/{version_id}/review-requests",
    response_model=SkillReviewRequestOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_review_request(
    skill_id: uuid.UUID,
    version_id: uuid.UUID,
    body: SkillReviewRequestCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SkillReviewRequestOut:
    """소유자의 검토 요청 — 스크립트 실행 허가(`scripts`), 카탈로그 등재(`expose_org`) 등."""
    skill = await _get_skill_for_edit(db, skill_id, user)
    try:
        version = await exposure.load_version_of(db, skill, version_id)
        request = await exposure.request_review(
            db, skill, version, body.kind, actor_id=user.id, note=body.note
        )
    except exposure.ExposureError as e:
        raise _exposure_http(e) from e
    await db.commit()
    out = SkillReviewRequestOut.model_validate(request)
    out.skill_id, out.skill_name, out.version = skill.id, skill.name, version.version
    return out


@router.post(
    "/{skill_id}/publish",
    response_model=SkillFreezeOut,
    deprecated=True,
    responses={409: {"description": "검토 필요(`reason: review_pending`)"}},
)
async def publish_skill(
    skill_id: uuid.UUID,
    body: SkillPublishRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SkillFreezeOut | JSONResponse:
    """**deprecated** — freeze 뒤 version publish 관문에 위임한다(D8 경로 6).

    대체: `POST /skills/{id}/freeze` 뒤 `POST /skills/{id}/versions/{vid}/publish`.
    `0.13.0` 이상에서 두 제품의 이관을 확인한 뒤 제거한다. 검토가 필요하면 version과
    요청을 커밋하고 409 `review_pending`을 반환한다(구 응답 모양 호환).
    """
    skill = await _get_skill_for_edit(db, skill_id, user)
    try:
        version, wrapper = await svc.freeze_skill(
            db, skill, user, publish=True, changelog=body.changelog
        )
    except exposure.ReviewPending as pending:
        await db.commit()
        return review_pending_response(pending.request)
    except exposure.ExposureError as e:
        raise _exposure_http(e) from e
    except (SkillValidationError, SkillParseError) as e:
        raise _unprocessable(e) from e
    except Exception as e:
        # publish 실패는 사유 불문 422로 응답(기존 동작 보존)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    await record(
        db,
        user.id,
        "skill.publish",
        skill.name,
        {"skill_id": str(skill.id), "version_id": str(version.id), "version": version.version},
    )
    await db.commit()
    return _freeze_out(version, wrapper.id)
