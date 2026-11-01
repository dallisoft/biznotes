"""라우터 밖에서 소스를 만드는 서비스 관문(#403) — 워크플로우 노드(`save_sources`)가 쓴다.

업로드·텍스트 라우터(`api/v1/sources.py`)는 생성을 인라인으로 한다. 노드는 api를 import할 수 없어
(§4.1) 같은 규칙을 이 모듈에 둔다. 라우터는 옮기지 않는다(쓰기 대장 앵커가 움직인다 — 별도 작업).

순서(계획 `docs/plans/save-sources-node.md` §3):
1. 원본 오브젝트를 트랜잭션 밖에서 먼저 올린다 — 잠금을 쥔 채 네트워크를 기다리지 않는다.
2. 한 트랜잭션에서 노트북 잠금 → 중복 → 용량 → INSERT → 인제스트 발행 예약(커밋 뒤 발행).
   중복을 용량보다 먼저 본다 — 이미 있는 항목이 꽉 찬 노트북 때문에 넘김(새 노트북)을 일으키지
   않게 한다(리뷰 M2).
3. 행을 커밋하지 못했으면(중복·상한·오류·취소) 올린 원본을 보상 삭제한다(업로드 선례). 취소로
   끝나도 삭제는 취소에서 보호해 끝까지 한다. 커밋한 뒤에는 지우지 않는다(리뷰 M4).

소스 id를 미리 만들고 `original_object_key`까지 생성자 keyword로 넣는다 — 행 속성 대입이 없어
`sources` 쓰기 대장(`docs/plans/hwp-ingest/gates/source-writers.allow.tsv`)에 앵커가 생기지 않는다.
용량 초과는 예외(HTTP 422)가 아니라 결과 값(`full`)이다 — 호출자가 넘김(다음 노트북)에 쓴다.
"""

import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

import anyio
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.config import get_settings
from biznotes_core.core.uow import UnitOfWork
from biznotes_core.models import Source
from biznotes_core.services import storage
from biznotes_core.services.ingestion import enqueue

logger = structlog.get_logger()

# `sources.external_ref`에 넣는 중복 키의 상한(글자 수)
EXTERNAL_REF_MAX_CHARS = 512
# 묶음 중복 조회 한 번에 넣는 키 수(IN 목록)
_LOOKUP_CHUNK = 500
# 커밋 도중에 끝났을 때 행이 남았는지 확인하는 시간 한도(초) — 취소에서 보호한 구간이다
_COMMIT_CHECK_SEC = 10.0

AdmissionOutcome = Literal["created", "duplicate", "full"]


@dataclass(frozen=True)
class SourceAdmission:
    """소스 생성 시도 결과.

    - `created`: 새 소스(`source_id`)를 `notebook_id`에 만들었다. 커밋 뒤 인제스트가 발행된다.
    - `duplicate`: 같은 중복 키의 소스(`source_id`)가 `notebook_id`에 이미 있다.
    - `full`: `notebook_id`가 소스 상한에 닿았다(`source_id`는 None).
    """

    outcome: AdmissionOutcome
    notebook_id: uuid.UUID
    source_id: uuid.UUID | None = None


# 항목 트랜잭션 안에서 대상 노트북을 정하고 `admit_source`를 부르는 함수 — `(db, uow, build)`.
# `build(notebook_id)`는 그 노트북에 넣을 행(미리 정한 id·원본 키)을 만든다(노트북 규칙, #403 PR-C)
PlaceSource = Callable[
    [AsyncSession, UnitOfWork, Callable[[uuid.UUID], Source]], Awaitable[SourceAdmission]
]


async def find_external_source(
    db: AsyncSession, notebook_ids: Sequence[uuid.UUID], external_ref: str
) -> SourceAdmission | None:
    """`notebook_ids` 안에서 중복 키가 같은 소스(가장 먼저 만든 것). 없으면 None.

    상태와 무관하게 센다(failed 행도 중복이다, D2). 사용자가 지운 소스는 행이 없으므로(하드 삭제)
    다음에 다시 들어온다. 잠금 없이 부르면 사전 조회일 뿐이다 — 판정은 `admit_source`가 잠금
    안에서 한다.
    """
    if not notebook_ids:
        return None
    row = (
        await db.execute(
            select(Source.id, Source.notebook_id)
            .where(
                Source.notebook_id.in_(list(notebook_ids)),
                Source.external_ref.is_not(None),
                Source.external_ref == external_ref,
            )
            .order_by(Source.created_at, Source.id)
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return SourceAdmission("duplicate", notebook_id=row.notebook_id, source_id=row.id)


async def find_external_sources(
    db: AsyncSession, notebook_ids: Sequence[uuid.UUID], external_refs: Iterable[str]
) -> dict[str, SourceAdmission]:
    """여러 중복 키를 한 번에 찾는다 — 키 → 가장 먼저 만든 소스.

    규칙은 `find_external_source`와 같다.

    잠금 없는 사전 조회다(처리할 항목 수를 셀 때 이미 있는 항목을 빼는 데 쓴다).
    """
    refs = list(dict.fromkeys(external_refs))
    found: dict[str, SourceAdmission] = {}
    if not notebook_ids or not refs:
        return found
    for start in range(0, len(refs), _LOOKUP_CHUNK):
        rows = await db.execute(
            select(Source.external_ref, Source.id, Source.notebook_id)
            .where(
                Source.notebook_id.in_(list(notebook_ids)),
                Source.external_ref.is_not(None),
                Source.external_ref.in_(refs[start : start + _LOOKUP_CHUNK]),
            )
            .order_by(Source.created_at, Source.id)
        )
        for ref, source_id, notebook_id in rows.tuples():
            if ref is not None and ref not in found:
                found[ref] = SourceAdmission(
                    "duplicate", notebook_id=notebook_id, source_id=source_id
                )
    return found


async def admit_source(
    db: AsyncSession,
    uow: UnitOfWork,
    source: Source,
    *,
    duplicate_scope: Sequence[uuid.UUID] = (),
) -> SourceAdmission:
    """호출자의 UoW 안에서 `source`(아직 세션에 넣지 않은 행)를 받아들인다.

    노트북 잠금 → 중복(`source.notebook_id` + `duplicate_scope`) → 용량(상태 무관 전 행) → INSERT →
    발행 예약 순서다. 잠금은 트랜잭션이 끝날 때 풀린다. 중복 범위가 여러 노트북이면 그 범위의 쓰기를
    직렬화하는 잠금(계열 잠금)을 호출자가 먼저 쥐어야 한다 — 이 함수는 대상 노트북만 잠근다.
    """
    notebook_id = source.notebook_id
    await enqueue.lock_notebook_sources(db, notebook_id)
    if source.external_ref is not None:
        scope = [notebook_id, *(n for n in duplicate_scope if n != notebook_id)]
        existing = await find_external_source(db, scope, source.external_ref)
        if existing is not None:
            return existing
    if await enqueue.source_count(db, notebook_id) >= await enqueue.notebook_source_limit(db):
        return SourceAdmission("full", notebook_id=notebook_id)
    db.add(source)
    await db.flush()
    enqueue.stage_ingest_job(db, source, uow)
    return SourceAdmission("created", notebook_id=notebook_id, source_id=source.id)


def source_object_key(source_id: uuid.UUID, suffix: str) -> str:
    """원본 오브젝트 키 — 업로드 라우터와 같은 `<source_id>/raw<suffix>`."""
    return f"{source_id}/raw{suffix}"


async def store_source_object(
    source_id: uuid.UUID, data: bytes, *, suffix: str, content_type: str
) -> str:
    """원본을 sources 버킷의 `<source_id>/raw<suffix>`에 올리고 키를 돌려준다."""
    key = source_object_key(source_id, suffix)
    bucket = get_settings().storage_bucket_sources

    def _put() -> None:
        storage.ensure_buckets()
        storage.put_object(bucket, key, data, content_type=content_type)

    await anyio.to_thread.run_sync(_put)
    return key


async def discard_source_object(key: str) -> None:
    """행을 만들지 못한 원본의 보상 삭제 — 실패해도 로그만 남긴다(업로드 선례)."""
    bucket = get_settings().storage_bucket_sources
    try:
        await anyio.to_thread.run_sync(storage.delete_object, bucket, key)
    except Exception:  # noqa: BLE001 — 보상 삭제 실패가 원래 결과를 가리지 않게 한다
        logger.warning("소스 원본 오브젝트 보상 삭제 실패", object_key=key)


async def create_text_source(
    db: AsyncSession,
    *,
    title: str,
    text: str,
    notebook_id: uuid.UUID | None = None,
    place: PlaceSource | None = None,
    mime_type: str | None = None,
    external_ref: str | None = None,
    duplicate_scope: Sequence[uuid.UUID] = (),
) -> SourceAdmission:
    """텍스트 소스(`type="text"`)를 만든다 — 자기 UoW로 커밋한다(호출자 UoW 안에서 부르지 않는다).

    대상은 `notebook_id`(그 노트북 + `duplicate_scope`를 중복 범위로 `admit_source`) 또는
    `place`(대상을 트랜잭션 안에서 정하는 함수) 중 하나다. `external_ref`가 있으면 잠금 없이
    `notebook_id` + `duplicate_scope`에서 먼저 찾아보고, 있으면 원본을 올리지 않고 `duplicate`다.
    잠금 안에서 다시 판정하므로 동시 실행에서도 같은 키가 두 번 생기지 않는다. 쓰기 권한은
    호출자가 본다.
    """
    return await _create_source(
        db,
        source_type="text",
        title=title,
        data=text.encode("utf-8"),
        suffix=".md" if mime_type == "text/markdown" else ".txt",
        content_type=mime_type or "text/plain",
        mime_type=mime_type,
        origin_url=None,
        notebook_id=notebook_id,
        place=place,
        external_ref=external_ref,
        duplicate_scope=duplicate_scope,
    )


async def create_file_source(
    db: AsyncSession,
    *,
    title: str,
    data: bytes,
    mime_type: str,
    suffix: str,
    origin_url: str | None = None,
    notebook_id: uuid.UUID | None = None,
    place: PlaceSource | None = None,
    external_ref: str | None = None,
    duplicate_scope: Sequence[uuid.UUID] = (),
) -> SourceAdmission:
    """관문(`admit_bytes`)을 통과한 바이트로 파일 소스(`type="file"`)를 만든다(#403 PR-B).

    업로드 라우터와 같은 행 모양이다 — 원본은 `<id>/raw<suffix>`, 워커가 `mime_type`으로 파싱한다.
    `origin_url`은 받아 온 주소다(파싱에는 쓰지 않는다). 그 밖의 규칙은 `create_text_source`와 같다.
    `origin_url`이 INSERT에 들어가므로 DB 예외의 문자열에 그 값(쿼리 포함)이 실린다 — 호출자는 이
    함수의 예외 문자열을 로그·출력에 남기지 않는다.
    """
    return await _create_source(
        db,
        source_type="file",
        title=title,
        data=data,
        suffix=suffix,
        content_type=mime_type,
        mime_type=mime_type,
        origin_url=origin_url,
        notebook_id=notebook_id,
        place=place,
        external_ref=external_ref,
        duplicate_scope=duplicate_scope,
    )


async def _create_source(
    db: AsyncSession,
    *,
    source_type: str,
    title: str,
    data: bytes,
    suffix: str,
    content_type: str,
    mime_type: str | None,
    origin_url: str | None,
    notebook_id: uuid.UUID | None,
    place: PlaceSource | None,
    external_ref: str | None,
    duplicate_scope: Sequence[uuid.UUID],
) -> SourceAdmission:
    if (notebook_id is None) == (place is None):
        raise ValueError("notebook_id와 place 중 정확히 하나가 필요합니다")
    if external_ref is not None:
        if len(external_ref) > EXTERNAL_REF_MAX_CHARS:
            raise ValueError("external_ref가 너무 깁니다")
        scope = [*([notebook_id] if notebook_id is not None else []), *duplicate_scope]
        existing = await find_external_source(db, scope, external_ref)
        if existing is not None:
            return existing
    source_id = uuid.uuid4()
    key = source_object_key(source_id, suffix)
    # 커밋 여부 — 커밋 직후 콜백(발행보다 먼저 등록)이 세운다. `reached_commit`은 본문을 다 돌고
    # 커밋에 들어갔다는 표시다(그 뒤의 예외는 커밋됐을 수도 있다)
    committed = False
    reached_commit = False

    def mark_committed() -> None:
        nonlocal committed
        committed = True

    def build(target: uuid.UUID) -> Source:
        return Source(
            id=source_id,
            notebook_id=target,
            type=source_type,
            title=title[:500],
            origin_url=origin_url,
            mime_type=mime_type,
            size_bytes=len(data),
            status="pending",
            external_ref=external_ref,
            original_object_key=key,
        )

    try:
        await store_source_object(source_id, data, suffix=suffix, content_type=content_type)
        async with enqueue.ingest_uow(db) as uow:
            uow.after_commit(mark_committed)
            if place is not None:
                result = await place(db, uow, build)
            else:
                assert notebook_id is not None
                result = await admit_source(
                    db, uow, build(notebook_id), duplicate_scope=duplicate_scope
                )
            reached_commit = True
    except BaseException:
        if not committed:
            # 취소(lease 회수 등)로 끝나도 원본을 남기지 않는다 — 보호 구간에서 끝까지 지운다
            with anyio.CancelScope(shield=True):
                await _discard_uncommitted(db, source_id, key, maybe_committed=reached_commit)
        raise
    if result.outcome != "created":
        await discard_source_object(key)
    return result


async def _discard_uncommitted(
    db: AsyncSession, source_id: uuid.UUID, key: str, *, maybe_committed: bool
) -> None:
    """커밋하지 못한 행의 원본을 지운다. 커밋 도중에 끝났으면 행이 남았는지 먼저 본다.

    행이 있거나 확인할 수 없으면 원본을 남긴다 — 행만 있고 원본이 없으면 그 키가 실패 행으로
    굳어 다시 수집되지 않는다(고아 원본보다 나쁘다).
    """
    if maybe_committed:
        try:
            with anyio.fail_after(_COMMIT_CHECK_SEC):
                exists = await db.scalar(select(Source.id).where(Source.id == source_id))
        except Exception:  # noqa: BLE001 — 확인하지 못하면 원본을 남긴다
            logger.warning("커밋 여부를 확인하지 못해 원본을 남김", object_key=key)
            return
        if exists is not None:
            return
    await discard_source_object(key)


__all__ = [
    "EXTERNAL_REF_MAX_CHARS",
    "AdmissionOutcome",
    "PlaceSource",
    "SourceAdmission",
    "admit_source",
    "create_file_source",
    "create_text_source",
    "discard_source_object",
    "find_external_source",
    "find_external_sources",
    "source_object_key",
    "store_source_object",
]
