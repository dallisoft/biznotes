import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from biznotes_core.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from biznotes_core.models.vector_types import SparseVec, Vector

SOURCE_STATUSES = ("pending", "parsing", "chunking", "embedding", "ready", "failed")
SOURCE_TYPES = ("file", "web", "text")


class Source(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "sources"
    __table_args__ = (
        # 정체 소스 reaper와 재인덱싱 대기열 흐름 판정이 쓴다(#44) — 상태 IN + 시각 범위.
        # 부분 인덱스가 아니라 복합 인덱스다: 상태 값이 바인드 파라미터여도 쓸 수 있다.
        Index("ix_sources_status_updated_at", "status", "updated_at"),
        # `save_sources` 중복 키(#403) — 노트북 안의 `external_ref` 조회. 비유일이다: 노트북 사이
        # 중복은 어차피 잠금으로 판정한다
        Index(
            "ix_sources_notebook_external_ref",
            "notebook_id",
            "external_ref",
            postgresql_where=text("external_ref IS NOT NULL"),
        ),
    )

    notebook_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notebooks.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # SOURCE_TYPES
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    origin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 외부 출처 식별자 — `save_sources`가 만든 행은 중복 키 `mcp:<namespace>:<external_id>`를
    # 둔다(#403)
    external_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)  # 원본
    parsed_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)  # 파싱 markdown
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 소스 가이드 캐시 — 온디맨드 LLM 생성(요약 + 키워드). 미생성/실패 시 null
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    keywords: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    # 벡터 출처(#7) — 청크 벡터를 만든 임베딩 공간 키와 청킹 설정 스냅샷.
    # NULL은 출처 기록 이전 = 로컬 기본 모델(chunks_v1). 키 형식은
    # services/embedding_spaces.py 참고. 현재 설정과 다르면 재인덱싱 대상(stale)이다.
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    chunk_config: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    # 문서가 스스로 가진 제목(HWP info.title·PDF 메타데이터·웹 <title>), 정화 후 저장(HWP-02).
    # `title`은 파일 소스의 정규화 파일명이자 중복 식별·HWP 라우팅 키라 그대로 둔다.
    # NULL은 제목이 없거나 이 컬럼 도입 전에 인제스트된 행이다. UI 표시용이다 — 프롬프트에
    # 넣으려면 별도의 구분·이스케이프가 필요하다.
    document_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 인제스트 경고(HWP-03) — `[{"code", "params"}]`, 코드는 services/ingestion/source_warnings.py.
    # 전체 파싱마다 교체한다. NULL은 기록 없음(도입 전 행·파싱 전), []는 파싱했고 경고 없음.
    warnings: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )


class SourceChunk(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "source_chunks"
    __table_args__ = (
        # 같은 소스에 같은 seq 행이 두 번 커밋되지 않는다(#52). 색인 교체는 같은 트랜잭션에서
        # 행을 지운 뒤 넣으므로 즉시 검사 제약이어도 통과한다.
        UniqueConstraint("source_id", "seq", name="uq_source_chunks_source_seq"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    # 청크 벡터(pgvector, `services/vectorstore.py`) — dense는 소스의 임베딩 공간
    # (`sources.embedding_model`) 모델로 만든 것이고 차원은 공간마다 다르다. sparse는 BM25다.
    # 색인 교체가 행과 함께 쓰고 지운다. 일반 조회가 벡터를 끌어오지 않게 지연 로드한다.
    # 인용의 `chunk_id`는 이 행의 `id`다
    embedding: Mapped[list[float] | None] = mapped_column(Vector(), nullable=True, deferred=True)
    sparse: Mapped[dict[int, float] | None] = mapped_column(
        SparseVec(), nullable=True, deferred=True
    )


class SourceDigest(Base, TimestampMixin):
    """에이전트용 소스 요약 캐시(#360) — 문서 **전체**를 덮는 요약(청크 묶음 요약 → 합치기).

    소스당 한 행이다. `content_hash`는 요약을 만든 청크 텍스트(`seq` 순)의 sha256이다 — 재인제스트로
    청크가 바뀌면 지문이 달라져 다음 run에서 다시 만든다. `prompt_version`이 코드 상수와 다르면 역시
    다시 만든다. 소스를 지우면 함께 지워진다. 읽기는 `source_digest` 노드의 run 범위 조회를 거친다.
    """

    __tablename__ = "source_digests"

    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="CASCADE"),
        primary_key=True,
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    digest: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # 요약을 만든(마지막 합치기) 모델과 그 표시 이름 스냅샷 — 모델을 지워도 이름은 남는다
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("llm_models.id", name="fk_source_digests_model_id", ondelete="SET NULL"),
        nullable=True,
    )
    model_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)


REINDEX_ITEM_STATUSES = ("queued", "done", "failed")


class ReindexRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """어드민 재인덱싱 요청 1건 (#5).

    재인덱싱은 소스 `status`를 바꾸지 않는다 — 대기 중에도 `ready`로 남아 검색·
    선택에서 빠지지 않는다. 대신 진행률은 소스별 `ReindexRunItem`으로 센다.
    완료 여부는 저장하지 않고 queued 항목 유무로 계산한다(동시 완료 경합 회피).

    `skipped`·`scope`·`notebook_title`(#179)은 그 컬럼이 생기기 전 run에는 NULL(알 수 없음)이다.
    """

    __tablename__ = "reindex_runs"

    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    notebook_id: Mapped[uuid.UUID | None] = mapped_column(  # None = 전체
        UUID(as_uuid=True), ForeignKey("notebooks.id", ondelete="SET NULL"), nullable=True
    )
    # 요청 옵션 스냅샷 — reaper가 정체된 항목을 같은 조건으로 재발행할 때 읽는다
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    requested: Mapped[int] = mapped_column(Integer, nullable=False)  # 발행 시점 소스 수
    # 후보 중 이미 대기·처리 중이라 건너뛴 소스 수 — 후보 수 = requested + skipped (#179)
    skipped: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 범위 스냅샷(#179) — 'all' | 'notebook'. 노트북이 지워져 notebook_id가 NULL이 돼도
    # 「전체」와 구분되고, 그때의 노트북 제목이 남는다
    scope: Mapped[str | None] = mapped_column(String(20), nullable=True)
    notebook_title: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ReindexRunItem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """재인덱싱 대상 소스 1건. 워커가 그 소스 처리를 마치면 done/failed로 닫는다.

    소스가 지워지면 `source_id`가 NULL이 되고 항목은 남는다(#179) — run 집계가 줄지 않는다.
    처리 전에 지워진 queued 항목(`source_id IS NULL`)은 "제외됨"(removed)으로 세고, 대기·
    재발행·활성 판정에서 뺀다. 상태 값을 바꾸지 않는 것은 `updated_at`이 대기열 흐름 판정에
    쓰이기 때문이다.
    """

    __tablename__ = "reindex_run_items"
    __table_args__ = (
        UniqueConstraint("run_id", "source_id", name="uq_reindex_run_items_run_source"),
        # 진행률·중복 발행 판정은 항상 queued 항목만 본다
        Index(
            "ix_reindex_run_items_queued",
            "source_id",
            postgresql_where=text("status = 'queued'"),
        ),
        # 대기열 흐름 판정 — cutoff 이후 닫힌 항목이 있는지(#44). 이력 크기와 무관하게 최근
        # 범위만 읽는다.
        Index("ix_reindex_run_items_updated_at", "updated_at"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reindex_runs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # reaper가 이 항목을 다시 발행한 횟수 — 상한을 넘으면 실패로 닫는다(무한 재발행 방지)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
