"""노출 모델 — 공개 version, 공개 관문, 검토 요청, 철회와 보류 (표준 계획 D8).

**정본과 불변식**
- 다른 사용자에게 드러나는 스킬 내용의 정본은 `skills.published_version_id` 하나다.
- 비소유자가 보거나 실행하는 내용은 모두 `exposed_version_id(skill)`에서 나온다.
  `enabled`이고 visibility가 org·system일 때만 포인터가 유효하다.
- 포인터는 `set_published_version`만 바꾼다. 안에서 `assert_can_expose`를 부르고
  version이 같은 스킬 소속인지 검사한다. 관리자도 검토 경로 밖에서는 비소유자 규칙을 따른다.
- `skills.status`는 파생값(포인터 유무)이고, `skill_versions.status='published'`는
  관문이 붙이는 이력 표식이다. 노출 판정에는 쓰지 않는다.

**성격**: D8은 거버넌스다. `origin`은 사용자가 우회할 수 있다(가져온 내용을 파일 PUT으로
직접 작성한 스킬로 다시 만들 수 있다). 도구 권한은 게이트가 정한다.

순수 판정 함수(`exposed_version_id`·`resolve_run_version_id`·`required_review_kind`·
`is_stale_request`)는 DB 없이 검증한다. DB 경로는 호출측 트랜잭션에 합류하며 커밋하지 않는다.
"""

import uuid
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from biznotes_core.core.uow import unique_violation_constraint_name
from biznotes_core.models import Agent, Skill, SkillReviewRequest, SkillVersion
from biznotes_core.services.audit import record

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

VISIBILITY_RANK = {"private": 0, "org": 1, "system": 2}
EXPOSED_VISIBILITIES = ("org", "system")
EXPOSE_KINDS = ("expose_org", "expose_system")
SCRIPTS_KIND = "scripts"
# org·system 슬롯의 전역 slug 부분 유니크 인덱스(models/skill.py) — 소유자 범위 인덱스는 뺀다
_EXPOSED_SLUG_INDEXES = frozenset({"uq_skills_org_slug", "uq_skills_system_slug"})


class ExposureError(ValueError):
    """관문 규칙 위반. `reason`은 API 응답 코드, `status_code`는 HTTP 상태다.

    호출 계약: 이 예외가 나면 호출자는 트랜잭션을 rollback한다(커밋하지 않는다). 승인 경로는
    승인 기록이 공개보다 먼저 쓰이므로, 커밋하면 승인만 남는다.
    """

    def __init__(self, reason: str, message: str, *, status_code: int = 409) -> None:
        self.reason = reason
        self.status_code = status_code
        super().__init__(message)


def _slug_conflict() -> ExposureError:
    # 다른 스킬의 이름·id·소유자는 넣지 않는다 — 비공개 소유자를 드러내지 않는다(#412)
    return ExposureError(
        "slug_conflict",
        "같은 slug의 공개 스킬이 이미 있습니다 — 복제해 다른 이름으로 공개하세요",
        status_code=409,
    )


def _slug_conflict_from(error: IntegrityError) -> ExposureError | None:
    """공개 슬롯의 전역 slug unique 위반이면 409 `slug_conflict`, 아니면 None(재전파용)."""
    if unique_violation_constraint_name(error) in _EXPOSED_SLUG_INDEXES:
        return _slug_conflict()
    return None


class ReviewRequired(Exception):
    """이 공개에는 아직 없는 관리자 승인(`kind`)이 필요하다 — 관문 내부 신호."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(f"{kind} 승인이 필요합니다")


class ReviewPending(Exception):
    """공개 대신 검토 요청을 남겼다. 호출부는 요청만 커밋하고 다른 변경은 적용하지 않는다."""

    def __init__(self, request: SkillReviewRequest) -> None:
        self.request = request
        super().__init__("관리자 검토가 필요합니다")


@dataclass(frozen=True)
class PublishOutcome:
    status: Literal["published", "review_pending"]
    version_id: uuid.UUID
    request: SkillReviewRequest | None = None


# ---------- 순수 판정 ----------


def _rank(visibility: str) -> int:
    try:
        return VISIBILITY_RANK[visibility]
    except KeyError as e:
        raise ExposureError(
            "invalid_visibility", f"알 수 없는 visibility: {visibility!r}", status_code=422
        ) from e


def exposed_version_id(skill: Skill) -> uuid.UUID | None:
    """비소유자에게 드러나는 version — enabled이고 org·system일 때만 포인터(R3-01)."""
    if not skill.enabled or skill.visibility not in EXPOSED_VISIBILITIES:
        return None
    return skill.published_version_id


def is_owner(skill: Skill, actor_id: uuid.UUID | None) -> bool:
    return actor_id is not None and skill.owner_id == actor_id


def resolve_run_version_id(skill: Skill, actor_id: uuid.UUID | None) -> uuid.UUID | None:
    """actor가 실행할 version — 소유자는 가장 최근 freeze, 그 밖(관리자 포함)은 노출 version.

    None이면 실행할 수 없다. 조용히 다른 경로로 대체하지 않는다(R3-09).
    """
    if is_owner(skill, actor_id):
        return skill.current_version_id if skill.enabled else None
    return exposed_version_id(skill)


def visible_version_id(skill: Skill, actor_id: uuid.UUID | None) -> uuid.UUID | None:
    """actor가 조회할 version — 소유자는 꺼져 있어도 자기 최신 freeze를 본다."""
    if is_owner(skill, actor_id):
        return skill.current_version_id
    return exposed_version_id(skill)


def can_view(skill: Skill, actor_id: uuid.UUID | None) -> bool:
    """소유자이거나, 꺼지지 않은 org·system 스킬이다.

    목록·상세에는 남지만 **내용은 노출 version에서만** 나온다 — 공개 version이 없으면
    version 목록이 비고, 파일·내보내기는 404, 복제는 409, 실행은 불가다. private·`enabled=false`는
    존재 자체를 숨긴다(R3-01).
    """
    return is_owner(skill, actor_id) or (
        skill.enabled and skill.visibility in EXPOSED_VISIBILITIES
    )


def target_visibility_for_publish(skill: Skill) -> str:
    """version publish의 목표 — max(현재, org). system을 org로 강등하지 않는다(R3-11)."""
    return skill.visibility if _rank(skill.visibility) >= _rank("org") else "org"


def required_review_kind(skill: Skill, target_visibility: str) -> str | None:
    """관문 규칙표(S-HCI-5·13·14) — 이 공개에 필요한 관리자 승인 종류. 필요 없으면 None.

    | 목표 | authored | imported | system_seed |
    | org | 허용(보류 중이면 승인) | expose_org 승인 | 불가 |
    | system | expose_system 승인 | expose_system 승인 | seed 경로 또는 관리자 경로 |
    """
    if target_visibility == "system":
        return "expose_system"
    if target_visibility != "org":
        raise ExposureError(
            "invalid_visibility",
            f"공개 대상 visibility가 아닙니다: {target_visibility!r}",
            status_code=422,
        )
    if skill.origin == "system_seed":
        raise ExposureError(
            "seed_org_forbidden", "시드 스킬은 system 또는 private로만 둘 수 있습니다"
        )
    if skill.exposure_hold or skill.origin == "imported":
        return "expose_org"
    return None


def approval_publishes(skill: Skill, kind: str) -> bool:
    """이 승인이 **공개까지** 뜻하는가(S-HCI-12).

    `authored` 스킬의 org 공개는 애초에 승인이 필요 없다. 그 스킬에 달린 `expose_org`
    요청은 "다른 사용자의 채팅 카탈로그에 올려 달라"는 뜻이므로, 승인해도 노출 version을
    옮기지 않는다. 공개는 소유자의 publish가 한다.
    """
    if kind not in EXPOSE_KINDS:
        return False
    if kind == "expose_system":
        return True
    try:
        return required_review_kind(skill, "org") is not None
    except ExposureError:
        return False


def accepted_kinds(required: str) -> tuple[str, ...]:
    """필요한 승인을 대신할 수 있는 승인 — system 공개 승인은 org 공개도 덮는다."""
    if required == "expose_org":
        return EXPOSE_KINDS
    return (required,)


def is_stale_request(
    *,
    request_version: int,
    pointer_version: int | None,
    current_visibility: str,
    target_visibility: str,
) -> bool:
    """검토 승인 시점의 stale 판정(R5-01).

    (a) 요청 version이 현재 포인터보다 오래됐다, (b) 현재 visibility가 요청 목표보다 높다
    (그대로 승인하면 강등이 된다). 현재 private인 것은 근거가 아니다 — 공개가 끝나야
    visibility가 바뀌므로 private→org 요청은 승인 시점에 늘 private이다.
    """
    if pointer_version is not None and request_version < pointer_version:
        return True
    return _rank(current_visibility) > _rank(target_visibility)


# ---------- DB 경로 ----------


def _now() -> datetime:
    return datetime.now(UTC)


async def find_wrapper(db: "AsyncSession", skill_id: uuid.UUID) -> Agent | None:
    """스킬의 안정적인 wrapper Agent (어느 version을 가리키든 skill_id로 역추적)."""
    row = await db.execute(
        select(Agent)
        .join(SkillVersion, Agent.skill_version_id == SkillVersion.id)
        .where(SkillVersion.skill_id == skill_id, Agent.source_kind == "skill_package")
        .limit(1)
    )
    return row.scalar_one_or_none()


async def sync_wrapper_flags(db: "AsyncSession", skill: Skill) -> None:
    """wrapper의 scope·enabled를 스킬에 맞춘다 — 구 가시성 판정을 쓰는 제품 경로의 호환용.

    코어 런타임 가드는 wrapper가 아니라 `resolve_run_version_id`로 판정한다(R3-02).
    """
    wrapper = await find_wrapper(db, skill.id)
    if wrapper is not None:
        wrapper.enabled = skill.enabled
        wrapper.scope = "system" if skill.visibility == "system" else "user"


async def load_version_of(
    db: "AsyncSession", skill: Skill, version_id: uuid.UUID
) -> SkillVersion:
    """같은 스킬 소속 version만 돌려준다. 다른 스킬의 vid는 없는 것으로 본다."""
    version = await db.get(SkillVersion, version_id)
    if version is None or version.skill_id != skill.id:
        raise ExposureError(
            "version_not_found", "이 스킬의 version이 아닙니다", status_code=404
        )
    return version


async def _assert_slot_free(
    db: "AsyncSession", slug: str, visibility: str, *, exclude_id: uuid.UUID | None = None
) -> None:
    """org·system 슬롯 `visibility`에 같은 slug의 스킬이 있으면 409 `slug_conflict`(#412)."""
    query = select(Skill.id).where(Skill.slug == slug, Skill.visibility == visibility)
    if exclude_id is not None:
        query = query.where(Skill.id != exclude_id)
    if (await db.execute(query.limit(1))).scalar_one_or_none() is not None:
        raise _slug_conflict()


async def _assert_slug_free(db: "AsyncSession", skill: Skill, target_visibility: str) -> None:
    """대상 org·system 슬롯에 같은 slug의 **다른** 스킬이 있으면 409 `slug_conflict`(#412).

    이미 그 visibility인 스킬은 자기 슬롯을 쥐고 있으므로 검사하지 않는다(재공개 허용).
    """
    if target_visibility not in EXPOSED_VISIBILITIES or target_visibility == skill.visibility:
        return
    await _assert_slot_free(db, skill.slug, target_visibility, exclude_id=skill.id)


@asynccontextmanager
async def _slot_savepoint(db: "AsyncSession") -> AsyncIterator[None]:
    """블록의 쓰기를 savepoint 안에서 flush한다 — 전역 slug 위반은 409 `slug_conflict`(#412).

    조회 검사를 함께 통과한 동시 공개는 인덱스 대기 뒤 unique 위반으로 끝난다. 그 위반도 같은
    409로 바꾸고, 바깥 트랜잭션은 계속 쓸 수 있다. savepoint 안에서 바꾼 속성은 만료된다.
    `begin_nested()`는 savepoint를 만들기 **전에** 대기 중인 변경을 flush한다. 그 변경은
    롤백해도 남으므로, 충돌 때 없던 일이 되어야 할 쓰기는 이 블록 뒤에 둔다.
    """
    try:
        async with db.begin_nested():
            yield
            await db.flush()
    except IntegrityError as e:
        converted = _slug_conflict_from(e)
        if converted is None:
            raise
        raise converted from e


async def _move_visibility(db: "AsyncSession", skill: Skill, target_visibility: str) -> None:
    """org·system으로 옮기는 visibility 대입 — 전역 slug 위반은 409 `slug_conflict`(#412).

    호출측이 먼저 `_assert_slug_free`로 검사한다.
    """
    if target_visibility not in EXPOSED_VISIBILITIES or target_visibility == skill.visibility:
        skill.visibility = target_visibility
        return
    async with _slot_savepoint(db):
        skill.visibility = target_visibility


async def _add_system_skill(db: "AsyncSession", skill: Skill) -> None:
    """새 system 스킬 행(시드)을 넣는다 — 슬롯이 차 있으면 409 `slug_conflict`(#412).

    `service.seed_system_skill`의 새 시드 INSERT가 쓴다. 승격된 사용자 스킬이 같은 slug를
    쥐고 있거나, 조회 뒤 커밋 전인 승격과 겹쳐도 같은 409다.
    """
    await _assert_slot_free(db, skill.slug, "system")
    async with _slot_savepoint(db):
        db.add(skill)


async def has_approval(
    db: "AsyncSession", version_id: uuid.UUID, kinds: tuple[str, ...]
) -> bool:
    found = await db.scalar(
        select(SkillReviewRequest.id)
        .where(
            SkillReviewRequest.version_id == version_id,
            SkillReviewRequest.kind.in_(kinds),
            SkillReviewRequest.status == "approved",
        )
        .limit(1)
    )
    return found is not None


async def approved_exposure_version_ids(
    db: "AsyncSession", version_ids: Collection[uuid.UUID]
) -> set[uuid.UUID]:
    """approved `expose_*` 검토 요청이 있는 version id — 쿼리 한 번(#410 D1).

    다른 사용자의 org 스킬은 **그 version**이 승인됐을 때만 채팅 진입에 오른다. 조건은 모델
    카탈로그(`runtime._catalog_candidates`, S-HCI-12)와 같다. 빈 입력이면 쿼리하지 않는다.
    """
    if not version_ids:
        return set()
    rows = await db.execute(
        select(SkillReviewRequest.version_id).where(
            SkillReviewRequest.version_id.in_(list(version_ids)),
            SkillReviewRequest.status == "approved",
            SkillReviewRequest.kind.in_(EXPOSE_KINDS),
        )
    )
    return set(rows.scalars())


async def assert_can_expose(
    db: "AsyncSession", skill: Skill, version: SkillVersion, target_visibility: str
) -> None:
    """관문 판정 — 필요한 승인이 없으면 `ReviewRequired`, 규칙상 불가면 `ExposureError`."""
    if version.skill_id != skill.id:
        raise ExposureError("version_not_found", "이 스킬의 version이 아닙니다", status_code=404)
    required = required_review_kind(skill, target_visibility)
    if required is None:
        return
    if not await has_approval(db, version.id, accepted_kinds(required)):
        raise ReviewRequired(required)


async def set_published_version(
    db: "AsyncSession",
    skill: Skill,
    version: SkillVersion | None,
    *,
    actor_id: uuid.UUID | None,
    target_visibility: str,
    seed: bool = False,
) -> None:
    """공개 포인터를 바꾸는 **유일한** 함수. `None`은 포인터를 비운다(철회).

    `seed=True`는 시드 경로(시스템 행위자)다 — `system_seed` 스킬의 system 공개만 허용한다.
    org·system으로 옮길 때 같은 slug의 다른 스킬이 그 슬롯에 있으면 409 `slug_conflict`다.
    `ExposureError`가 나면 호출자는 트랜잭션을 rollback한다 — 앞서 쓴 승인 기록이 남는다.
    """
    if version is None:
        previous = skill.published_version_id
        skill.published_version_id = None
        skill.status = "draft"
        await record(
            db,
            actor_id,
            "skill.retract",
            skill.name,
            {"skill_id": str(skill.id), "version_id": str(previous) if previous else None},
        )
        return

    if seed:
        if skill.origin != "system_seed" or target_visibility != "system":
            raise ExposureError(
                "seed_path_forbidden", "시드 경로는 시드 스킬의 system 공개만 합니다"
            )
        if version.skill_id != skill.id:
            raise ExposureError(
                "version_not_found", "이 스킬의 version이 아닙니다", status_code=404
            )
        await _assert_slug_free(db, skill, target_visibility)
    else:
        # 관문(검토 요청)보다 먼저 본다 — 승인해도 공개할 수 없는 요청을 남기지 않는다
        await _assert_slug_free(db, skill, target_visibility)
        await assert_can_expose(db, skill, version, target_visibility)

    await _move_visibility(db, skill, target_visibility)
    skill.published_version_id = version.id
    skill.status = "published"
    version.status = "published"
    await sync_wrapper_flags(db, skill)
    await record(
        db,
        actor_id,
        "skill.expose",
        skill.name,
        {
            "skill_id": str(skill.id),
            "version_id": str(version.id),
            "version": version.version,
            "visibility": target_visibility,
        },
    )


async def request_review(
    db: "AsyncSession",
    skill: Skill,
    version: SkillVersion,
    kind: str,
    *,
    actor_id: uuid.UUID | None,
    target_visibility: str | None = None,
    note: str | None = None,
) -> SkillReviewRequest:
    """검토 요청을 만든다. 같은 (version, kind)의 pending이 있으면 그것을 돌려준다.

    `rejected`된 (version, kind)는 다시 요청할 수 없다 — 새 version을 만들어야 한다.
    """
    if version.skill_id != skill.id:
        raise ExposureError("version_not_found", "이 스킬의 version이 아닙니다", status_code=404)
    if kind not in (*EXPOSE_KINDS, SCRIPTS_KIND):
        raise ExposureError("invalid_kind", f"알 수 없는 검토 종류: {kind!r}", status_code=422)
    if kind in EXPOSE_KINDS:
        target_visibility = target_visibility or ("system" if kind == "expose_system" else "org")
        if skill.origin == "system_seed" and target_visibility == "org":
            raise ExposureError(
                "seed_org_forbidden", "시드 스킬은 system 또는 private로만 둘 수 있습니다"
            )
    else:
        target_visibility = None

    rows = (
        await db.execute(
            select(SkillReviewRequest).where(
                SkillReviewRequest.version_id == version.id,
                SkillReviewRequest.kind == kind,
                SkillReviewRequest.status.in_(("pending", "rejected")),
            )
        )
    ).scalars().all()
    for existing in rows:
        if existing.status == "rejected":
            raise ExposureError(
                "review_rejected",
                "이 version의 같은 검토 요청이 거절되었습니다. 새 version을 만들어 요청하세요",
            )
    for existing in rows:
        if existing.status == "pending":
            return existing

    request = SkillReviewRequest(
        version_id=version.id,
        kind=kind,
        target_visibility=target_visibility,
        status="pending",
        requested_by=actor_id,
        note=note,
    )
    db.add(request)
    await db.flush()
    await record(
        db,
        actor_id,
        "skill.review.request",
        skill.name,
        {
            "skill_id": str(skill.id),
            "version_id": str(version.id),
            "request_id": str(request.id),
            "kind": kind,
            "target_visibility": target_visibility,
        },
    )
    return request


async def _record_admin_approval(
    db: "AsyncSession",
    skill: Skill,
    version: SkillVersion,
    kind: str,
    *,
    target_visibility: str,
    admin_id: uuid.UUID,
) -> SkillReviewRequest:
    """관리자의 직접 지정을 승인 요청으로 기록한다(R4-05) — pending이 있으면 그것을 승인한다."""
    pending = await db.scalar(
        select(SkillReviewRequest).where(
            SkillReviewRequest.version_id == version.id,
            SkillReviewRequest.kind == kind,
            SkillReviewRequest.status == "pending",
        )
    )
    request = pending or SkillReviewRequest(
        version_id=version.id, kind=kind, requested_by=admin_id
    )
    request.target_visibility = target_visibility
    request.status = "approved"
    request.decided_by = admin_id
    request.decided_at = _now()
    if pending is None:
        db.add(request)
    await db.flush()
    await record(
        db,
        admin_id,
        "skill.review.approve",
        skill.name,
        {
            "skill_id": str(skill.id),
            "version_id": str(version.id),
            "request_id": str(request.id),
            "kind": kind,
            "target_visibility": target_visibility,
            "via": "admin_designation",
        },
    )
    return request


async def _skill_version_ids(db: "AsyncSession", skill: Skill) -> list[uuid.UUID]:
    return list(
        (await db.execute(select(SkillVersion.id).where(SkillVersion.skill_id == skill.id)))
        .scalars()
        .all()
    )


async def _transition_requests(
    db: "AsyncSession",
    skill: Skill,
    *,
    kinds: tuple[str, ...],
    from_status: str,
    to_status: str,
    actor_id: uuid.UUID | None,
    action: str,
) -> int:
    version_ids = await _skill_version_ids(db, skill)
    if not version_ids:
        return 0
    rows = (
        await db.execute(
            select(SkillReviewRequest).where(
                SkillReviewRequest.version_id.in_(version_ids),
                SkillReviewRequest.kind.in_(kinds),
                SkillReviewRequest.status == from_status,
            )
        )
    ).scalars().all()
    decided_at = _now()
    for request in rows:
        request.status = to_status
        request.decided_by = actor_id
        request.decided_at = decided_at
    count = len(rows)
    if count:
        await record(
            db,
            actor_id,
            action,
            skill.name,
            {"skill_id": str(skill.id), "kinds": list(kinds), "count": count},
        )
    return count


async def cancel_pending_exposure_requests(
    db: "AsyncSession", skill: Skill, actor_id: uuid.UUID | None
) -> int:
    """private 전환 시 pending `expose_*`를 자동 취소한다(R4-02)."""
    return await _transition_requests(
        db,
        skill,
        kinds=EXPOSE_KINDS,
        from_status="pending",
        to_status="cancelled",
        actor_id=actor_id,
        action="skill.review.cancel",
    )


async def _withdraw_exposure_approvals(
    db: "AsyncSession", skill: Skill, kinds: tuple[str, ...], admin_id: uuid.UUID | None
) -> int:
    return await _transition_requests(
        db,
        skill,
        kinds=kinds,
        from_status="approved",
        to_status="withdrawn",
        actor_id=admin_id,
        action="skill.review.withdraw",
    )


# ---------- 관문 경로 (D8 경로 1~7) ----------


async def publish_version(
    db: "AsyncSession", skill: Skill, version: SkillVersion, *, actor_id: uuid.UUID | None
) -> PublishOutcome:
    """경로 1 — 소유자의 version publish. 검토가 필요하면 요청을 남기고 공개하지 않는다."""
    await _lock_skill(db, skill.id)
    if version.skill_id != skill.id:
        raise ExposureError("version_not_found", "이 스킬의 version이 아닙니다", status_code=404)
    target = target_visibility_for_publish(skill)
    try:
        await set_published_version(
            db, skill, version, actor_id=actor_id, target_visibility=target
        )
    except ReviewRequired as e:
        request = await request_review(
            db, skill, version, e.kind, actor_id=actor_id, target_visibility=target
        )
        return PublishOutcome("review_pending", version.id, request)
    return PublishOutcome("published", version.id)


async def change_visibility_by_owner(
    db: "AsyncSession", skill: Skill, target: str, *, actor_id: uuid.UUID | None
) -> None:
    """경로 2 — 사용자 PATCH의 visibility. 검토가 필요하면 `ReviewPending`을 던진다.

    - private→org: 대상은 현재 포인터, 없으면 최신 freeze. 공개가 끝나야 visibility가 바뀐다.
    - org·system→private: 관문이 필요 없다. 노출은 즉시 끊기고 pending `expose_*`는 취소된다.
    - system→org는 관리자 경로다.
    """
    _rank(target)
    # 쓰기 경로는 모두 **스킬 행 → 요청 행** 순서로 잠근다. 순서가 갈리면 소유자의 private
    # 전환과 관리자 승인이 서로를 기다려 교착한다(관리자 경로는 `_locked_pending_request`).
    await _lock_skill(db, skill.id)
    if target == "private":
        # 이미 private이어도 처리한다 — 소유자가 명시적으로 비공개를 요청하면 대기 중인
        # 공개 요청도 함께 취소되어야 한다(R4-02). 공개는 승인 시점에 일어나므로
        # private→org 요청은 대기 중 내내 private 상태다.
        was_exposed = skill.visibility != "private"
        was_system = skill.visibility == "system"
        skill.visibility = "private"
        cancelled = await cancel_pending_exposure_requests(db, skill, actor_id)
        if was_system:
            # 관리자가 준 system 승격은 소유자가 비공개로 내리는 순간 끝난다. 남겨 두면
            # `system → private → org` 2단계로 관리자 경로를 우회해 재공개된다(E-02).
            await _withdraw_exposure_approvals(db, skill, ("expose_system",), actor_id)
        if was_exposed:
            await sync_wrapper_flags(db, skill)
        if was_exposed or cancelled:
            await record(
                db, actor_id, "skill.unexpose", skill.name, {"skill_id": str(skill.id)}
            )
        return
    if target == skill.visibility:
        return
    if target == "system" or skill.visibility == "system":
        raise ExposureError(
            "admin_only", "system 승격·강등은 관리자 경로에서만 할 수 있습니다", status_code=403
        )
    version_id = skill.published_version_id or skill.current_version_id
    if version_id is None:
        raise ExposureError("no_version", "공개할 version이 없습니다. 먼저 freeze하세요")
    version = await load_version_of(db, skill, version_id)
    outcome = await publish_version(db, skill, version, actor_id=actor_id)
    if outcome.request is not None:
        raise ReviewPending(outcome.request)


async def admin_change_visibility(
    db: "AsyncSession",
    skill: Skill,
    target: str,
    *,
    version_id: uuid.UUID | None,
    admin_id: uuid.UUID,
) -> None:
    """경로 3 — 관리자 visibility 변경(R4-05).

    - 승격(→system, private→org): `version_id` 필수. 관리자의 승격은 그 version의
      `expose_*` 승인으로 기록되고 보류를 푼다.
    - system→org: `expose_system` 승인만 철회한다. 현재 포인터가 org 관문을 통과하면
      org 노출을 유지하고, 아니면 포인터를 비운다. 보류는 세우지 않는다.
    - →private: 철회와 같다. 승인을 철회하고 보류를 세우며 pending `expose_*`를 취소한다.
    - 시드 스킬은 private로만 강등할 수 있다(R4-01).
    """
    await _lock_skill(db, skill.id)
    current = skill.visibility
    _rank(target)
    if target == "private":
        # 이미 private이어도 철회로 처리한다 — 승인 철회와 보류가 목적이기 때문이다.
        await _withdraw_exposure_approvals(db, skill, EXPOSE_KINDS, admin_id)
        await cancel_pending_exposure_requests(db, skill, admin_id)
        skill.exposure_hold = True
        skill.visibility = "private"
        await sync_wrapper_flags(db, skill)
        await record(
            db, admin_id, "skill.demote", skill.name, {"skill_id": str(skill.id), "to": "private"}
        )
        return
    if target == current:
        return
    if _rank(target) > _rank(current):
        if version_id is None:
            raise ExposureError(
                "version_required", "승격에는 공개할 version_id가 필요합니다", status_code=422
            )
        if target == "org" and skill.origin == "system_seed":
            raise ExposureError(
                "seed_org_forbidden", "시드 스킬은 system 또는 private로만 둘 수 있습니다"
            )
        version = await load_version_of(db, skill, version_id)
        kind = "expose_system" if target == "system" else "expose_org"
        await _record_admin_approval(
            db, skill, version, kind, target_visibility=target, admin_id=admin_id
        )
        skill.exposure_hold = False
        await set_published_version(
            db, skill, version, actor_id=admin_id, target_visibility=target
        )
        return

    if target == "org":  # system → org
        if skill.origin == "system_seed":
            raise ExposureError(
                "seed_org_forbidden", "시드 스킬은 private로만 강등할 수 있습니다"
            )
        await _assert_slug_free(db, skill, "org")
        # visibility를 먼저 옮긴다 — 슬롯 경합이면 승인 철회 전에 409로 끝나 바뀐 것이 없다.
        # 철회는 visibility를 보지 않으므로 순서를 바꿔도 뜻은 같다
        await _move_visibility(db, skill, "org")
        await _withdraw_exposure_approvals(db, skill, ("expose_system",), admin_id)
        if skill.published_version_id is not None:
            pointer = await db.get(SkillVersion, skill.published_version_id)
            try:
                if pointer is None:
                    raise ReviewRequired("expose_org")
                await assert_can_expose(db, skill, pointer, "org")
            except ReviewRequired:
                await set_published_version(
                    db, skill, None, actor_id=admin_id, target_visibility="org"
                )
        await sync_wrapper_flags(db, skill)
        await record(
            db, admin_id, "skill.demote", skill.name, {"skill_id": str(skill.id), "to": "org"}
        )
        return



async def admin_set_published_version(
    db: "AsyncSession",
    skill: Skill,
    version_id: uuid.UUID | None,
    *,
    admin_id: uuid.UUID,
) -> None:
    """경로 4 — 관리자의 공개 version 지정. `None`은 철회(보류를 세운다).

    지정은 해당 `expose_*` 승인으로 기록되고 보류를 푼다. `visibility='system'`인 채로
    철회된 시드 스킬도 이 경로로 복원한다. private 스킬은 visibility 변경(경로 3)으로 공개한다.
    """
    await _lock_skill(db, skill.id)
    if version_id is None:
        await _withdraw_exposure_approvals(db, skill, EXPOSE_KINDS, admin_id)
        skill.exposure_hold = True
        await set_published_version(
            db, skill, None, actor_id=admin_id, target_visibility=skill.visibility
        )
        return
    if skill.visibility not in EXPOSED_VISIBILITIES:
        raise ExposureError(
            "not_exposed", "private 스킬은 visibility 승격으로 공개하세요", status_code=409
        )
    version = await load_version_of(db, skill, version_id)
    kind = "expose_system" if skill.visibility == "system" else "expose_org"
    await _record_admin_approval(
        db, skill, version, kind, target_visibility=skill.visibility, admin_id=admin_id
    )
    skill.exposure_hold = False
    await set_published_version(
        db, skill, version, actor_id=admin_id, target_visibility=skill.visibility
    )


async def _lock_skill(db: "AsyncSession", skill_id: uuid.UUID) -> Skill | None:
    return (
        await db.execute(
            select(Skill)
            .where(Skill.id == skill_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _lock_request(
    db: "AsyncSession", request_id: uuid.UUID
) -> SkillReviewRequest | None:
    return (
        await db.execute(
            select(SkillReviewRequest)
            .where(SkillReviewRequest.id == request_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _locked_pending_request(
    db: "AsyncSession", request_id: uuid.UUID
) -> tuple[SkillReviewRequest, SkillVersion, Skill]:
    """스킬 행 → 요청 행 순서로 잠그고 pending인지 다시 확인한다(R4-02, R5-01).

    잠금 순서를 고정해야 동시 승인·소유자 변경이 교착 없이 직렬화된다.
    """
    request = await db.get(SkillReviewRequest, request_id)
    if request is None:
        raise ExposureError("request_not_found", "검토 요청이 없습니다", status_code=404)
    version = await db.get(SkillVersion, request.version_id)
    if version is None:
        raise ExposureError("request_not_found", "검토 요청이 없습니다", status_code=404)
    skill = await _lock_skill(db, version.skill_id)
    locked = await _lock_request(db, request_id)
    if skill is None or locked is None:
        raise ExposureError("request_not_found", "검토 요청이 없습니다", status_code=404)
    if locked.status != "pending":
        raise ExposureError("not_pending", f"이미 처리된 요청입니다({locked.status})")
    return locked, version, skill


async def approve_review_request(
    db: "AsyncSession", request_id: uuid.UUID, *, admin_id: uuid.UUID
) -> SkillReviewRequest:
    """경로 5 — 검토 승인. `expose_*`는 같은 트랜잭션에서 공개까지 끝낸다.

    stale이면 409로 거부하고 요청은 pending으로 남긴다. `scripts` 승인은 공개하지 않는다.
    """
    request, version, skill = await _locked_pending_request(db, request_id)
    publishes = approval_publishes(skill, request.kind)
    if publishes:
        target = request.target_visibility or (
            "system" if request.kind == "expose_system" else "org"
        )
        pointer_version: int | None = None
        if skill.published_version_id is not None:
            pointer = await db.get(SkillVersion, skill.published_version_id)
            pointer_version = pointer.version if pointer is not None else None
        if is_stale_request(
            request_version=version.version,
            pointer_version=pointer_version,
            current_visibility=skill.visibility,
            target_visibility=target,
        ):
            raise ExposureError(
                "stale",
                "요청 뒤 스킬 상태가 바뀌어 승인할 수 없습니다(더 새 공개 version이 있거나 "
                "현재 공개 범위가 요청보다 넓음)",
            )
    request.status = "approved"
    request.decided_by = admin_id
    request.decided_at = _now()
    await db.flush()
    await record(
        db,
        admin_id,
        "skill.review.approve",
        skill.name,
        {
            "skill_id": str(skill.id),
            "version_id": str(version.id),
            "request_id": str(request.id),
            "kind": request.kind,
            "publishes": publishes,
        },
    )
    if publishes:
        skill.exposure_hold = False
        await set_published_version(
            db,
            skill,
            version,
            actor_id=admin_id,
            target_visibility=request.target_visibility or target,
        )
    return request


async def reject_review_request(
    db: "AsyncSession", request_id: uuid.UUID, *, admin_id: uuid.UUID, note: str | None = None
) -> SkillReviewRequest:
    request, version, skill = await _locked_pending_request(db, request_id)
    request.status = "rejected"
    request.decided_by = admin_id
    request.decided_at = _now()
    if note is not None:
        request.note = note
    await record(
        db,
        admin_id,
        "skill.review.reject",
        skill.name,
        {
            "skill_id": str(skill.id),
            "version_id": str(version.id),
            "request_id": str(request.id),
            "kind": request.kind,
        },
    )
    return request


async def withdraw_scripts_approval(
    db: "AsyncSession", request_id: uuid.UUID, *, admin_id: uuid.UUID
) -> SkillReviewRequest:
    """관리자의 `scripts` 승인 철회(R4-12). 공개 승인 철회는 경로 3·4로 한다."""
    request = await db.get(SkillReviewRequest, request_id)
    if request is None:
        raise ExposureError("request_not_found", "검토 요청이 없습니다", status_code=404)
    version = await db.get(SkillVersion, request.version_id)
    skill = await _lock_skill(db, version.skill_id) if version is not None else None
    locked = await _lock_request(db, request_id)
    if skill is None or locked is None:
        raise ExposureError("request_not_found", "검토 요청이 없습니다", status_code=404)
    if locked.kind != SCRIPTS_KIND:
        raise ExposureError(
            "not_scripts", "공개 승인은 공개 version 철회(경로 4)로 되돌리세요"
        )
    if locked.status != "approved":
        raise ExposureError("not_approved", f"승인된 요청이 아닙니다({locked.status})")
    locked.status = "withdrawn"
    locked.decided_by = admin_id
    locked.decided_at = _now()
    await record(
        db,
        admin_id,
        "skill.review.withdraw",
        skill.name,
        {"skill_id": str(skill.id), "request_id": str(locked.id), "kind": SCRIPTS_KIND},
    )
    return locked


async def publish_seed_version(
    db: "AsyncSession", skill: Skill, version: SkillVersion
) -> SkillReviewRequest | None:
    """경로 7 — 시드의 새 version 공개(시스템 행위자, R4-01·R5-02).

    - 보류도 없고 private 강등도 아니면 포인터를 옮긴다.
    - 보류 중이면 version만 두고 `expose_system` 요청(요청자 NULL)을 남긴다.
      배포 한 번으로 관리자 철회가 풀리지 않는다.
    - private로 강등된 시드는 version만 두고 요청도 만들지 않는다(복원은 경로 3).
    """
    await _lock_skill(db, skill.id)
    if skill.visibility == "private":
        return None
    if skill.exposure_hold:
        return await request_review(
            db, skill, version, "expose_system", actor_id=None, target_visibility="system"
        )
    await set_published_version(
        db, skill, version, actor_id=None, target_visibility="system", seed=True
    )
    return None
