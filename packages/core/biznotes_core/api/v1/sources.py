import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.api.deps import get_current_user
from biznotes_core.api.object_stream import ObjectStreamResponse
from biznotes_core.config import get_settings
from biznotes_core.core.db import get_db
from biznotes_core.core.rate_limit import RateLimiter
from biznotes_core.models import Notebook, Source, User
from biznotes_core.schemas.source import (
    SearchHit,
    SearchRequest,
    SourceCreate,
    SourceDetailOut,
    SourceGuideOut,
    SourceOut,
    SourceUploadLimitsOut,
    WebSearchRequest,
    WebSearchResult,
)
from biznotes_core.services.ingestion.admission import (
    MAX_UPLOAD_BYTES,
    AdmissionError,
    admit_bytes,
    allowed_ext_mime,
    log_admission_rejected,
)
from biznotes_core.services.ingestion.enqueue import (
    canonicalize_file_name,
    ensure_file_source_capacity_and_uniqueness,
    ensure_source_capacity,
    ingest_uow,
    notebook_source_limit,
    stage_ingest_job,
)
from biznotes_core.services.ingestion.upload_limits import effective_max_upload_bytes
from biznotes_core.services.notebooks import get_permission
from biznotes_core.services.storage import (
    ObjectNotFound,
    ensure_buckets,
    get_object,
    open_object_stream,
    put_object,
)
from biznotes_core.services.websearch import web_search

router = APIRouter(tags=["sources"])
# 공개 심볼 호환 alias — 정의는 admission이 소유한다. 업로드 **기본** 상한이다 — 실제 판정은
# `effective_max_upload_bytes`(관리자 재정의 > `SOURCE_MAX_UPLOAD_BYTES`, #388)
MAX_FILE_SIZE = MAX_UPLOAD_BYTES


async def _notebook_with_permission(
    db: AsyncSession, notebook_id: uuid.UUID, user: User, need_write: bool
) -> Notebook:
    nb = await db.get(Notebook, notebook_id)
    if nb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    perm = await get_permission(db, nb, user)
    if perm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    if need_write and perm not in ("owner", "write"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Write permission required")
    return nb


@router.get("/notebooks/{notebook_id}/sources", response_model=list[SourceOut])
async def list_sources(
    notebook_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SourceOut]:
    await _notebook_with_permission(db, notebook_id, user, need_write=False)
    rows = await db.execute(
        select(Source)
        .where(Source.notebook_id == notebook_id)
        .order_by(Source.created_at, Source.id)  # 같은 시각이면 id 순(#391)
    )
    return [SourceOut.model_validate(s) for s in rows.scalars().all()]


_source_create_limiter = RateLimiter(
    "source-create", scope="user", limit_setting="rate_limit_source_create"
)
_source_upload_limiter = RateLimiter(
    "source-upload",
    window_seconds=60,
    scope="user",
    limit_setting="rate_limit_source_upload",
)


@router.post(
    "/notebooks/{notebook_id}/sources/upload",
    response_model=SourceOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_source_upload_limiter)],
)
async def upload_file_source(
    notebook_id: uuid.UUID,
    file: UploadFile,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SourceOut:
    await _notebook_with_permission(db, notebook_id, user, need_write=True)

    settings = get_settings()
    allowed = allowed_ext_mime(settings)
    filename = canonicalize_file_name(file.filename)
    ext = Path(filename).suffix.lower()
    mime = allowed.get(ext)
    if mime is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"지원하지 않는 확장자: {ext} (지원: {', '.join(allowed)})",
            headers={"X-Admission-Code": "ext_not_allowed"},
        )
    # 상한+1바이트까지만 읽는다 — 본문 전체를 힙에 올린 뒤 검사하지 않는다. 상한은 공개 조회
    # (`GET /sources/upload-limits`)와 같은 유효값이다(#388)
    max_bytes = await effective_max_upload_bytes(db, settings)
    data = await file.read(max_bytes + 1)
    try:
        admit_bytes(data, mime, settings=settings, max_bytes=max_bytes)
    except AdmissionError as e:
        log_admission_rejected(e, mime=mime, size_bytes=len(data))
        code = (
            status.HTTP_413_CONTENT_TOO_LARGE
            if e.code == "too_large"
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        # 관문 코드는 헤더로 — `detail`은 제품 UI가 쓰는 문자열 그대로 둔다(HWP-04)
        raise HTTPException(code, e.message, headers={"X-Admission-Code": e.code}) from e

    # [R3-1e] source 생성과 발행 예약을 한 UoW로 묶는다. 이전에는 source를 커밋한 뒤
    # enqueue_ingest가 따로 커밋해, 그 사이 실패 시 발행 안 된 source가 남았다(결함 α).
    # 태스크 발행은 커밋 성공 후에만 일어난다.
    object_key: str | None = None
    try:
        async with ingest_uow(db) as uow:
            filename = await ensure_file_source_capacity_and_uniqueness(
                db, notebook_id, filename, len(data)
            )
            source = Source(
                notebook_id=notebook_id,
                type="file",
                title=filename,
                mime_type=mime,
                size_bytes=len(data),
                status="pending",
            )
            db.add(source)
            await db.flush()

            ensure_buckets()
            object_key = f"{source.id}/raw{ext}"
            put_object(get_settings().storage_bucket_sources, object_key, data, content_type=mime)
            source.original_object_key = object_key

            stage_ingest_job(db, source, uow)
    except Exception:
        if object_key:
            from biznotes_core.services.storage import delete_object

            try:
                delete_object(get_settings().storage_bucket_sources, object_key)
            except Exception:
                import structlog

                structlog.get_logger().warning(
                    "업로드 실패 후 원본 오브젝트 보상 삭제 실패", object_key=object_key
                )
        raise

    await db.refresh(source)
    return SourceOut.model_validate(source)


@router.post(
    "/notebooks/{notebook_id}/sources",
    response_model=SourceOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_source_create_limiter)],
)
async def create_source(
    notebook_id: uuid.UUID,
    body: SourceCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SourceOut:
    await _notebook_with_permission(db, notebook_id, user, need_write=True)

    # [R3-1e] 두 분기 모두 source를 커밋한 뒤 enqueue_ingest가 따로 커밋했다
    # (결함 α). 하나의 경계로 묶어 source 커밋과 발행 예약이 함께 일어나게 한다.
    async with ingest_uow(db) as uow:
        await ensure_source_capacity(db, notebook_id)
        if body.type == "web":
            if not body.url:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "url이 필요합니다")
            from biznotes_core.core.ssrf import validate_external_url

            # SSRFBlockedError(→422)는 전역 예외 핸들러가 매핑한다(R1-1).
            validate_external_url(body.url, allow_private=get_settings().ssrf_allow_private)
            title = body.title or "(제목 추출 중) " + body.url[:400]
            source = Source(
                notebook_id=notebook_id,
                type=body.type,
                title=title[:500],
                origin_url=body.url,
                status="pending",
            )
            db.add(source)
            await db.flush()
        else:  # text
            if not body.text or not body.text.strip():
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "text가 필요합니다")
            source = Source(
                notebook_id=notebook_id,
                type="text",
                title=(body.title or body.text.strip().splitlines()[0][:100])[:500],
                size_bytes=len(body.text.encode()),
                status="pending",
            )
            db.add(source)
            await db.flush()
            ensure_buckets()
            object_key = f"{source.id}/raw.txt"
            put_object(
                get_settings().storage_bucket_sources,
                object_key,
                body.text.encode("utf-8"),
                content_type="text/plain",
            )
            source.original_object_key = object_key

        stage_ingest_job(db, source, uow)

    await db.refresh(source)
    return SourceOut.model_validate(source)


@router.get("/sources/upload-limits", response_model=SourceUploadLimitsOut)
async def get_source_upload_limits(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SourceUploadLimitsOut:
    """추가 전에 거를 수 있게 공개하는 업로드 최대 크기(#388)·노트북당 소스 상한(#445).

    둘 다 서버 판정과 같은 함수로 구한 값이다. `/sources/{source_id}`보다 먼저 등록해야 한다
    (경로가 겹친다).
    """
    return SourceUploadLimitsOut(
        max_upload_bytes=await effective_max_upload_bytes(db, get_settings()),
        max_sources_per_notebook=await notebook_source_limit(db),
    )


@router.get("/sources/{source_id}", response_model=SourceDetailOut)
async def get_source(
    source_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SourceDetailOut:
    source = await db.get(Source, source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found")
    await _notebook_with_permission(db, source.notebook_id, user, need_write=False)

    out = SourceDetailOut.model_validate(source)
    if source.parsed_object_key:
        try:
            md = get_object(get_settings().storage_bucket_parsed, source.parsed_object_key)
            out.parsed_preview = md.decode("utf-8")[:20_000]
        except Exception:
            import structlog

            structlog.get_logger().warning(
                "parsed preview 로드 실패", source_id=str(source.id)
            )
            out.parsed_preview = None
    return out


# 테스트·호환용 이름 — 정의는 `api/object_stream.py`에 있다
_ObjectStreamResponse = ObjectStreamResponse

_source_content_limiter = RateLimiter(
    "source-content", scope="user", limit_setting="rate_limit_source_content"
)


@router.get(
    "/sources/{source_id}/content",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/markdown": {}}, "description": "파싱된 markdown 전문"}},
    dependencies=[Depends(_source_content_limiter)],
)
async def get_source_content(
    source_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """파싱된 markdown 전문 — 상세 조회의 `parsed_preview`(앞 20,000자)로 부족할 때 쓴다.

    본문은 메모리에 올리지 않고 청크로 흘려보낸다(#26) — 전문을 읽어 응답 본문으로
    복사하면 병렬 요청마다 문서 크기의 두 배가 API 프로세스에 쌓인다.
    """
    source = await db.get(Source, source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found")
    await _notebook_with_permission(db, source.notebook_id, user, need_write=False)
    if not source.parsed_object_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "파싱된 본문이 없습니다")

    import anyio

    bucket, key = get_settings().storage_bucket_parsed, source.parsed_object_key
    source_ref = str(source.id)
    # DB는 여기서 끝이다 — 요청 스코프 세션은 응답 전송이 끝나야 정리되므로, 그대로 두면
    # 느린 다운로드 동안 풀 커넥션을 붙잡는다. 스트리밍 전에 반납한다.
    await db.close()
    try:
        # 동기 스토리지 호출은 스레드로 보내 루프를 막지 않는다. 여기서는 오브젝트를 열기만
        # 한다 — 없는 오브젝트·장애를 응답 헤더를 보내기 전에 404·503으로 바꾸기 위해서다.
        stream = await anyio.to_thread.run_sync(lambda: open_object_stream(bucket, key))
    except ObjectNotFound as e:
        # 오브젝트가 지워졌다 — 기다려도 생기지 않으므로 404다
        raise HTTPException(status.HTTP_404_NOT_FOUND, "파싱된 본문이 없습니다") from e
    except Exception as e:
        import structlog

        structlog.get_logger().warning("parsed content 로드 실패", source_id=source_ref)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "본문을 불러오지 못했습니다"
        ) from e
    # 동기 이터레이터는 Starlette가 스레드풀에서 순회한다 — 청크 읽기도 루프를 막지 않는다
    return _ObjectStreamResponse(stream, media_type="text/markdown; charset=utf-8")


@router.get("/sources/{source_id}/guide", response_model=SourceGuideOut)
async def get_source_guide(
    source_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SourceGuideOut:
    """소스 가이드(요약 + 키워드) — 캐시가 있으면 반환, 없으면 온디맨드 생성."""
    source = await db.get(Source, source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found")
    await _notebook_with_permission(db, source.notebook_id, user, need_write=False)

    if source.summary is None and source.status == "ready":
        from biznotes_core.services.source_guide import generate_source_guide

        source = await generate_source_guide(db, source)

    return SourceGuideOut(summary=source.summary, keywords=source.keywords or [])


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(
    source_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    import anyio

    from biznotes_core.services.object_cleanup import (
        delete_objects_best_effort,
        source_object_refs,
    )

    source = await db.get(Source, source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found")
    await _notebook_with_permission(db, source.notebook_id, user, need_write=True)

    # PG 삭제(청크·벡터 CASCADE) → 오브젝트 정리
    refs = source_object_refs(source.id, source.original_object_key, source.parsed_object_key)
    await db.delete(source)
    await db.commit()
    # orphan 추적을 위해 실패는 로그로 남긴다
    await anyio.to_thread.run_sync(delete_objects_best_effort, refs)


@router.post(
    "/sources/{source_id}/retry", response_model=SourceOut, status_code=status.HTTP_202_ACCEPTED
)
async def retry_source(
    source_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SourceOut:
    """실패한 소스 인제스천 재시도."""
    source = await db.get(Source, source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found")
    await _notebook_with_permission(db, source.notebook_id, user, need_write=True)
    if source.status not in ("failed", "ready"):
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 처리 중인 소스입니다")

    # [R3-1e] status 전이와 발행 예약을 한 커밋으로 (결함 α).
    # 가이드 캐시(summary·keywords)도 같은 커밋에서 지운다 — 옛 본문의 요약이 재인제스트 뒤에
    # 재사용되지 않게(계획 A §5.2). 문서 제목·인제스트 경고(HWP-02·03)도 옛 본문의 것이라 지운다 —
    # 재시도 파싱이 실패해도 failed 행에 옛 경고가 남지 않는다(NULL = 기록 없음).
    # ORM 대입은 불러온 값이 이미 None이면 SET 절에서 빠지므로
    # 값과 무관하게 컬럼을 싣는 명시 UPDATE를 쓴다.
    # WHERE에도 상태를 다시 건다 — 위 409 판정 뒤 인제스트가 시작됐으면 덮지 않는다.
    async with ingest_uow(db) as uow:
        res = await db.execute(
            update(Source)
            .where(Source.id == source.id, Source.status.in_(("failed", "ready")))
            .values(
                status="pending",
                error_message=None,
                summary=None,
                keywords=None,
                document_title=None,
                warnings=None,
            )
            .execution_options(synchronize_session=False)
        )
        if res.rowcount != 1:  # type: ignore[attr-defined]
            raise HTTPException(status.HTTP_409_CONFLICT, "이미 처리 중인 소스입니다")
        stage_ingest_job(db, source, uow)

    await db.refresh(source)
    return SourceOut.model_validate(source)


# [R5-3] source `/events` SSE 엔드포인트 제거 — 프론트가 폴링(use-sources)으로 대체해
# 소비하지 않던 죽은 계층. 워커 publish_source_event(생산자)는 존치(no-op 발행).


@router.post(
    "/notebooks/{notebook_id}/search",
    response_model=list[SearchHit],
    dependencies=[Depends(RateLimiter("search", scope="user", limit_setting="rate_limit_search"))],
)
async def search_chunks(
    notebook_id: uuid.UUID,
    body: SearchRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SearchHit]:
    """하이브리드 검색 (Phase 2 검증 + Phase 3 채팅 기반)."""
    from biznotes_core.services.embedding_spaces import search_spaces, spaces_in_scope

    await _notebook_with_permission(db, notebook_id, user, need_write=False)

    spaces = await spaces_in_scope(db, [notebook_id], body.source_ids)
    points = await search_spaces(
        db,
        spaces,
        body.query,
        notebook_id=notebook_id,
        source_ids=body.source_ids,
        limit=body.limit,
    )
    return [
        SearchHit(
            chunk_id=str(p.id),
            source_id=p.payload.get("source_id", ""),
            seq=p.payload.get("seq", 0),
            text=p.payload.get("text", ""),
            score=p.score,
            heading=p.payload.get("heading"),
        )
        for p in points
        if p.payload
    ]


@router.post(
    "/notebooks/{notebook_id}/sources/web-search",
    response_model=list[WebSearchResult],
    dependencies=[
        Depends(RateLimiter("web-search", scope="user", limit_setting="rate_limit_web_search"))
    ],
)
async def source_web_search(
    notebook_id: uuid.UUID,
    body: WebSearchRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[WebSearchResult]:
    await _notebook_with_permission(db, notebook_id, user, need_write=True)
    # SearchProviderError(→502)는 전역 예외 핸들러가 매핑한다(R1-1).
    results = await web_search(body.query)
    return [WebSearchResult(**r) for r in results]
