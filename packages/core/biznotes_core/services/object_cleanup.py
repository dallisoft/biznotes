"""DB 행과 함께 사라지는 스토리지 오브젝트 정리 (#177).

키는 행 삭제를 커밋하기 **전에** 모은다 — CASCADE로 행이 사라지면 키를 다시 알 수 없다.
삭제는 커밋 뒤 best-effort다: 실패는 로그만 남기고 응답을 바꾸지 않는다(소스 삭제와 같은 정책).
"""

import uuid
from collections.abc import Iterable
from typing import Any

import structlog
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.config import get_settings
from biznotes_core.models import AgentRun, Notebook, Source
from biznotes_core.services import storage
from biznotes_core.services.ingestion import stage_artifacts

ObjectRef = tuple[str, str]  # (bucket, key)

logger = structlog.get_logger()


def source_object_refs(
    source_id: uuid.UUID, original_object_key: str | None, parsed_object_key: str | None
) -> list[ObjectRef]:
    """소스 하나가 남기는 오브젝트 — 원본, parsed, 위치(#244)."""
    settings = get_settings()
    refs = [
        (settings.storage_bucket_sources, original_object_key),
        (settings.storage_bucket_parsed, parsed_object_key),
        # 위치는 위치를 내는 파서의 소스에만 있다 — 없는 키 삭제는 오류가 아니다(S3 의미)
        (settings.storage_bucket_parsed, stage_artifacts.locations_key(source_id)),
    ]
    return [(bucket, key) for bucket, key in refs if key]


def _run_artifact_refs(artifact_keys: Any) -> list[ObjectRef]:
    """`AgentRun.artifact_keys`(`[{key, name, content_type}]`)의 key.

    형식이 어긋난 항목은 건너뛴다.
    """
    if not isinstance(artifact_keys, list):
        return []
    bucket = get_settings().storage_bucket_artifacts
    return [
        (bucket, entry["key"])
        for entry in artifact_keys
        if isinstance(entry, dict) and isinstance(entry.get("key"), str) and entry["key"]
    ]


async def _collect_refs(
    db: AsyncSession,
    source_where: ColumnElement[bool],
    run_where: ColumnElement[bool],
) -> list[ObjectRef]:
    """조건에 맞는 소스·run 산출물 오브젝트 — 종류마다 질의 한 번."""
    refs: list[ObjectRef] = []
    sources = await db.execute(
        select(Source.id, Source.original_object_key, Source.parsed_object_key).where(source_where)
    )
    for source_id, raw_key, parsed_key in sources.all():
        refs.extend(source_object_refs(source_id, raw_key, parsed_key))
    runs = await db.execute(select(AgentRun.artifact_keys).where(run_where))
    for (artifact_keys,) in runs.all():
        refs.extend(_run_artifact_refs(artifact_keys))
    return refs


async def notebook_object_refs(db: AsyncSession, notebook_id: uuid.UUID) -> list[ObjectRef]:
    """노트북 삭제로 CASCADE되는 행들이 가리키는 오브젝트 — 소스, run 산출물."""
    return await _collect_refs(
        db,
        Source.notebook_id == notebook_id,
        AgentRun.notebook_id == notebook_id,
    )


async def user_object_refs(db: AsyncSession, user_id: uuid.UUID) -> list[ObjectRef]:
    """사용자 삭제로 CASCADE되는 행들이 가리키는 오브젝트 (#191).

    소유 노트북의 소스·run 산출물에 더해, 다른 사람 노트북에서 이 사용자가 실행한
    run(`agent_runs.user_id`)도 CASCADE된다. 노트북 수와 무관하게 서브쿼리로 묶어 종류마다 한
    번만 질의한다.
    """
    owned = select(Notebook.id).where(Notebook.owner_id == user_id).scalar_subquery()
    return await _collect_refs(
        db,
        Source.notebook_id.in_(owned),
        or_(AgentRun.user_id == user_id, AgentRun.notebook_id.in_(owned)),
    )


def delete_objects_best_effort(refs: Iterable[ObjectRef]) -> None:
    """버킷별 일괄 삭제. 실패는 예외 없이 로그로 남긴다 — orphan 추적용."""
    by_bucket: dict[str, list[str]] = {}
    for bucket, key in refs:
        by_bucket.setdefault(bucket, []).append(key)
    for bucket, keys in by_bucket.items():
        try:
            rejected = storage.delete_objects(bucket, keys)
        except Exception:
            logger.warning("오브젝트 삭제 실패", bucket=bucket, count=len(keys), exc_info=True)
            continue
        for key in rejected:
            logger.warning("오브젝트 삭제 실패", bucket=bucket, key=key)
