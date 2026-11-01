# 신규(#244, docs/plans/ingest-stage-tasks/). 필요: PostgreSQL·Valkey(PG 테스트).
"""인제스트 단계 분리(#244) — 실제 PostgreSQL에서 점유·조건부 전이·reaper 단계별 재개를 본다.

`_run`(진입·파싱)·`_run_index`(청킹·임베딩·색인)·`_reap_stalled`는 실경로다. 파서(`_parse`),
원격 임베딩 모델(게이트웨이 공간), sparse 임베딩, 스토리지(`ingest_objects`), 브로커 발행, 개요
예약만 대역이다. 벡터는 청크 행(pgvector)에 실제로 쓴다 — 색인 교체와 같은 트랜잭션이다.

동시성은 asyncio 태스크 둘이 각자 세션(연결)으로 같은 행을 다투게 해 만든다. 경합 시점은 실경로
함수를 감싸(원 함수를 그대로 부른다) 고정하거나, 다른 세션의 커밋으로 다른 실행(reaper·재파싱)을
흉내 낸다. `create_all` 기반이라 Alembic 마이그레이션 검증이 아니다.

계획: `docs/plans/ingest-stage-tasks/plan.md` 수락 기준 2·3·4·9, 리뷰 B1·B2·M1·M2.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.sql import Select

from biznotes_core.config import get_settings
from biznotes_core.core.db import async_session_factory
from biznotes_core.models import Notebook, ReindexRun, ReindexRunItem, Source, SourceChunk, User
from biznotes_core.services import vectorstore
from biznotes_core.services.embedding_spaces import LOCAL_SPACE_KEY, TransientEmbeddingError
from biznotes_core.services.ingestion import source_warnings, stage_artifacts
from biznotes_core.services.ingestion.embedder import SparseVector
from biznotes_core.services.ingestion.parsers import Location, ParseResult
from biznotes_core.workers.celery_app import celery_app
from biznotes_core.workers.tasks import ingestion, overview

PAGE1 = "## Page 1\n\n" + "첫 쪽 본문 문장입니다. " * 50 + "\n\n"
PAGE2 = "## Page 2\n\n| 항목 | 값 |\n| --- | --- |\n" + "| 가 | 1 |\n" * 80
MARKDOWN = PAGE1 + PAGE2
# HWP 파서가 내는 모양의 위치 — 1쪽 본문, 2쪽 표(rhwp 좌표)
LOCATIONS = [
    Location(0, len(PAGE1), page=1, section=0, paragraph=0),
    Location(len(PAGE1), len(MARKDOWN), page=2, section=0, paragraph=3, table_index=0),
]
# 기본 RAG 설정(chunk_size 800)에서는 쪽마다 청크 하나다 — (page, table_index)
POSITIONS = [(1, None), (2, 0)]
NO_POSITIONS = [(None, None), (None, None)]
DIGEST = stage_artifacts.parsed_digest(MARKDOWN.encode())
HWP = "application/x-hwp"
STALE_MINUTES = ingestion.STALLED_THRESHOLD_MINUTES + 5


class FakeSpace:
    """게이트웨이 임베딩 공간 대역 — 2차원 벡터를 낸다(로컬 384차원과 섞이지 않는 공간)."""

    is_local = False
    key = "gateway:fake-244"
    display_name = "fake"

    def __init__(self) -> None:
        self.fail = False
        self.embedded = 0  # 임베딩한 텍스트 수 — 물러난 실행은 0을 더한다

    def embed(self, texts: list[str], **kw: Any) -> list[list[float]]:
        if self.fail:
            raise TransientEmbeddingError("503 busy")
        self.embedded += len(texts)
        return [[1.0, 0.0] for _ in texts]


class FakeParser:
    """`_parse` 대역 — 부른 횟수를 센다(재시도·재인덱싱은 파서를 부르지 않는다)."""

    def __init__(self, result: ParseResult) -> None:
        self.result = result
        self.calls = 0

    def __call__(self, source: Any, raw: bytes | None) -> ParseResult:
        self.calls += 1
        return self.result


@pytest.fixture
def space(monkeypatch: pytest.MonkeyPatch, ingest_objects: Any) -> FakeSpace:
    fake = FakeSpace()

    async def active_space(db: Any) -> Any:
        return fake

    monkeypatch.setattr(ingestion, "active_space", active_space)
    monkeypatch.setattr(
        ingestion, "embed_sparse", lambda texts: [SparseVector([0], [1.0]) for _ in texts]
    )
    monkeypatch.setattr(overview, "schedule_overview_refresh", lambda nid, **kw: None)
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: None)
    return fake


@pytest.fixture
def parser(monkeypatch: pytest.MonkeyPatch) -> FakeParser:
    fake = FakeParser(ParseResult(markdown=MARKDOWN, locations=list(LOCATIONS)))
    monkeypatch.setattr(ingestion, "_parse", fake)
    return fake


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """발행한 상태 이벤트 `(source_id, status)` — 물러난 실행이 아무것도 쓰지 않았는지 본다."""
    seen: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        ingestion,
        "publish_source_event",
        lambda sid, event: seen.append((sid, event.get("status"))),
    )
    return seen


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str], dict[str, Any]]]:
    """reaper의 재발행 `(태스크, args, 발행 kwargs)` — 실제 브로커 발행은 막는다."""
    calls: list[tuple[str, list[str], dict[str, Any]]] = []

    def recorder(name: str) -> Any:
        def apply_async(*, args: list[str], **kw: Any) -> None:
            calls.append((name, args, kw))

        return apply_async

    monkeypatch.setattr(ingestion.ingest_source, "apply_async", recorder("ingest_source"))
    monkeypatch.setattr(ingestion.index_source, "apply_async", recorder("index_source"))
    return calls


def _sources_bucket() -> str:
    return get_settings().storage_bucket_sources


def _parsed_bucket() -> str:
    return get_settings().storage_bucket_parsed


async def _source(
    owner: User, status: str, *, parsed: bool = True, type_: str = "file"
) -> uuid.UUID:
    async with async_session_factory() as s:
        nb = Notebook(owner_id=owner.id, title="단계 분리")
        s.add(nb)
        await s.flush()
        src = Source(
            notebook_id=nb.id,
            type=type_,
            title="문서.hwp",
            mime_type=HWP if type_ == "file" else None,
            status=status,
        )
        s.add(src)
        await s.flush()
        src.original_object_key = f"{src.id}/raw.hwp"
        if parsed:
            src.parsed_object_key = f"{src.id}/parsed.md"
        await s.commit()
        return src.id


def _seed_raw(store: Any, sid: uuid.UUID) -> None:
    store.objects[(_sources_bucket(), f"{sid}/raw.hwp")] = b"raw-hwp"


def _seed_parsed(
    store: Any, sid: uuid.UUID, *, locations: list[Location] | None = None
) -> None:
    """파싱을 마친 소스의 저장 산출물 — parsed.md(+ 같은 세대의 locations.json)."""
    store.objects[(_parsed_bucket(), f"{sid}/parsed.md")] = MARKDOWN.encode()
    if locations:
        store.objects[(_parsed_bucket(), stage_artifacts.locations_key(sid))] = (
            stage_artifacts.encode_locations(DIGEST, locations)
        )


async def _row(sid: uuid.UUID) -> Source:
    async with async_session_factory() as s:
        row = await s.get(Source, sid)
        assert row is not None
        return row


async def _vector_ids(sid: uuid.UUID) -> set[uuid.UUID]:
    """벡터가 있는 청크 행 id — 검색이 볼 수 있는 색인이다."""
    async with async_session_factory() as s:
        rows = await s.scalars(
            select(SourceChunk.id).where(
                SourceChunk.source_id == sid, SourceChunk.embedding.is_not(None)
            )
        )
        return set(rows.all())


async def _chunks(sid: uuid.UUID) -> list[SourceChunk]:
    async with async_session_factory() as s:
        rows = await s.scalars(
            select(SourceChunk).where(SourceChunk.source_id == sid).order_by(SourceChunk.seq)
        )
        return list(rows.all())


def _positions(chunks: list[SourceChunk]) -> list[tuple[Any, Any]]:
    return [(c.meta.get("page"), c.meta.get("table_index")) for c in chunks]


def _statuses(events: list[tuple[str, Any]], sid: uuid.UUID) -> list[Any]:
    return [status for source_id, status in events if source_id == str(sid)]


async def _write(sid: uuid.UUID, **values: Any) -> datetime:
    """다른 실행(reaper·heartbeat·재파싱)의 커밋을 흉내 낸다. 반환: 새 `updated_at`."""
    values.setdefault("updated_at", func.now())
    async with async_session_factory() as s:
        result = await s.execute(
            update(Source)
            .where(Source.id == sid)
            .values(**values)
            .returning(Source.updated_at)
            .execution_options(synchronize_session=False)
        )
        updated_at: datetime = result.scalar_one()
        await s.commit()
        return updated_at


async def _age(sid: uuid.UUID, minutes: int = STALE_MINUTES) -> datetime:
    return await _write(sid, updated_at=datetime.now(UTC) - timedelta(minutes=minutes))


def _cutoff() -> datetime:
    return datetime.now(UTC) - timedelta(minutes=ingestion.STALLED_THRESHOLD_MINUTES)


async def _open_reindex(sid: uuid.UUID, *, refetch: bool = False) -> uuid.UUID:
    async with async_session_factory() as s:
        run = ReindexRun(options={"refetch": refetch}, requested=1, scope="all")
        s.add(run)
        await s.flush()
        item = ReindexRunItem(run_id=run.id, source_id=sid, status="queued")
        s.add(item)
        await s.commit()
        return item.id


async def _item_status(item_id: uuid.UUID) -> str:
    async with async_session_factory() as s:
        item = await s.get(ReindexRunItem, item_id)
        assert item is not None
        return item.status


async def _close_item(item_id: uuid.UUID) -> None:
    async with async_session_factory() as s:
        await s.execute(
            update(ReindexRunItem).where(ReindexRunItem.id == item_id).values(status="done")
        )
        await s.commit()


# ---------------------------------------------------------------------------
# 색인 단계 점유 — 한 실행만 점유하고, 진 쪽은 아무것도 쓰지 않는다(수락 기준 4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("claim_from", ["chunking", "ready"])
async def test_concurrent_index_runs_claim_once(
    user_a: User,
    space: FakeSpace,
    ingest_objects: Any,
    events: list[tuple[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    claim_from: str,
) -> None:
    """같은 소스의 색인 단계 둘(중복 메시지·reaper 재발행)이 점유 직전에서 만나도 점유는 하나다.

    `chunking`은 파싱을 마친 신규 소스, `ready`는 열린 항목이 있는 재인덱싱이다.
    """
    sid = await _source(user_a, claim_from)
    _seed_parsed(ingest_objects, sid, locations=LOCATIONS)
    keep_previous = claim_from == "ready"
    item = await _open_reindex(sid) if keep_previous else None
    barrier = asyncio.Barrier(2)
    claims: list[bool] = []
    real_claim = ingestion._claim_embedding

    async def claim_together(*args: Any, **kwargs: Any) -> bool:
        # 두 실행이 모두 출발 상태를 읽고 점유 직전까지 온 뒤에 점유 문장을 낸다 — 사전 확인이
        # 아니라 조건부 UPDATE가 승자를 고르는지 본다
        await barrier.wait()
        ok = await real_claim(*args, **kwargs)
        claims.append(ok)
        return ok

    monkeypatch.setattr(ingestion, "_claim_embedding", claim_together)
    kwargs = {"keep_previous": keep_previous, "claim_from": claim_from}

    async with asyncio.timeout(60):
        results = await asyncio.gather(
            ingestion._run_index(str(sid), **kwargs), ingestion._run_index(str(sid), **kwargs)
        )

    assert sorted(claims) == [False, True]
    winner, loser = sorted(results, key=lambda r: not r["ok"])
    assert winner == {"ok": True, "chunks": len(POSITIONS)}
    assert loser == {"ok": False, "error": "claim_lost"}
    # 진 쪽은 임베딩·상태 쓰기를 하나도 하지 않았다 — 색인 교체 커밋은 한 번이다
    assert space.embedded == len(POSITIONS)
    assert _statuses(events, sid) == ["embedding", "ready"]
    row = await _row(sid)
    assert (row.status, row.error_message, row.chunk_count) == ("ready", None, len(POSITIONS))
    chunks = await _chunks(sid)
    assert [c.seq for c in chunks] == [0, 1]
    assert _positions(chunks) == POSITIONS
    assert await _vector_ids(sid) == {c.id for c in chunks}
    if item is not None:
        assert await _item_status(item) == "done"


async def test_run_that_never_claimed_writes_nothing_over_the_winner(
    user_a: User,
    space: FakeSpace,
    ingest_objects: Any,
    events: list[tuple[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """점유하지 못한 실행은 실패를 써도 남의 행을 덮지 않는다(리뷰 M2, 이전 보고 결함 1).

    - 진 쪽 A: 출발 상태(`chunking`)를 읽은 뒤 점유 전 읽기(`active_space`)에서 실패한다 — 그
      사이 B가 점유했다. A의 실패는 B의 `embedding`을 `failed`로 덮으면 안 된다.
    - 늦게 온 C: B가 점유한 뒤 시작한다 — 출발 상태가 아니라 곧바로 물러난다.
    """
    sid = await _source(user_a, "chunking")
    _seed_parsed(ingest_objects, sid)
    a_waiting, b_claimed, release_b = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def active_space(db: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:  # A — B가 점유할 때까지 기다렸다가 실패한다
            a_waiting.set()
            await b_claimed.wait()
            raise RuntimeError("DB 일시 오류")
        return space

    real_claim = ingestion._claim_embedding

    async def claim_and_hold(*args: Any, **kwargs: Any) -> bool:
        ok = await real_claim(*args, **kwargs)
        if ok:  # B — 점유를 커밋한 채 임베딩 전에 멈춘다
            b_claimed.set()
            await release_b.wait()
        return ok

    monkeypatch.setattr(ingestion, "active_space", active_space)
    monkeypatch.setattr(ingestion, "_claim_embedding", claim_and_hold)

    async with asyncio.timeout(60):
        a = asyncio.create_task(ingestion._run_index(str(sid)))
        await a_waiting.wait()
        b = asyncio.create_task(ingestion._run_index(str(sid)))
        lost = await a
        after_a = await _row(sid)
        late = await ingestion._run_index(str(sid))
        after_c = await _row(sid)
        release_b.set()
        won = await b

    assert lost == {"ok": False, "error": "DB 일시 오류"}
    assert late == {"ok": False, "error": "claim_lost"}
    # A·C 모두 B의 embedding을 그대로 두었다
    assert (after_a.status, after_a.error_message) == ("embedding", None)
    assert (after_c.status, after_c.updated_at) == ("embedding", after_a.updated_at)
    assert won == {"ok": True, "chunks": len(POSITIONS)}
    assert _statuses(events, sid) == ["embedding", "ready"]
    assert (await _row(sid)).status == "ready"


async def test_swap_does_not_overwrite_row_reset_before_lock(
    user_a: User,
    space: FakeSpace,
    ingest_objects: Any,
    events: list[tuple[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """교체의 `ready`는 이 실행이 쥔 `embedding`에서만 쓴다(리뷰 B1, 이전 보고 결함 2).

    임베딩 뒤 교체 잠금 직전에 reaper가 행을 `chunking`으로 되돌렸다(다른 세션 커밋). 이 실행은
    savepoint를 되돌린 뒤 아무것도 쓰지 않고 물러난다 — 행은 reaper가 낸 색인 단계의 것이다.
    """
    sid = await _source(user_a, "chunking")
    _seed_parsed(ingest_objects, sid)
    before = await _row(sid)
    reset_at: list[datetime] = []
    real_lock = ingestion._lock_source_index

    async def reset_then_lock(db: Any, source_id: uuid.UUID) -> None:
        reset_at.append(await _write(source_id, status="chunking", error_message=None))
        await real_lock(db, source_id)

    monkeypatch.setattr(ingestion, "_lock_source_index", reset_then_lock)

    result = await ingestion._run_index(str(sid))

    assert result == {"ok": False, "error": "claim_lost"}
    row = await _row(sid)
    assert (row.status, row.updated_at) == ("chunking", reset_at[0])
    assert (row.chunk_count, row.embedding_model) == (before.chunk_count, before.embedding_model)
    assert await _chunks(sid) == []
    # 벡터는 행과 함께 되돌려졌다 — 남은 벡터가 없다
    assert await _vector_ids(sid) == set()
    assert _statuses(events, sid) == ["embedding"]  # 점유만 알렸다 — ready를 알리지 않는다


async def test_failure_after_losing_the_row_returns_without_writing(
    user_a: User,
    space: FakeSpace,
    ingest_objects: Any,
    events: list[tuple[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """점유한 뒤 실패했는데 그 사이 reaper가 행을 되돌렸다 — 실패를 쓰지 않고 결과로 물러난다.

    parsed.md가 없어 `_load_parsed`가 실패한다(prep). 그 직전에 reaper가 `embedding`을
    `chunking`으로 되돌렸다. 조건부 실패 기록(`owned="embedding"`)은 0행이다.
    """
    sid = await _source(user_a, "chunking")  # parsed.md를 올리지 않았다
    real_load = ingestion._load_parsed
    reset_at: list[datetime] = []

    async def reset_then_load(source: Any) -> Any:
        reset_at.append(await _write(sid, status="chunking", error_message=None))
        return await real_load(source)

    monkeypatch.setattr(ingestion, "_load_parsed", reset_then_load)

    result = await ingestion._run_index(str(sid))

    assert result["ok"] is False
    row = await _row(sid)
    assert (row.status, row.error_message, row.updated_at) == ("chunking", None, reset_at[0])
    assert _statuses(events, sid) == ["embedding"]


# ---------------------------------------------------------------------------
# 진입 단계 가드 — 색인 중인 세대를 다시 파싱하지 않는다(리뷰 B1)
# ---------------------------------------------------------------------------


_BEING_INDEXED = pytest.mark.parametrize(
    ("status", "reindex_open", "error"),
    [
        ("chunking", False, "already_parsed"),
        ("embedding", False, "already_parsed"),
        # 재인덱싱 항목이 열려 있어도 다른 실행이 처리 중이면 넘기지 않는다
        ("chunking", True, "in_flight"),
        ("embedding", True, "in_flight"),
        # 끝난 행에 온 일반 인제스트 메시지는 낡은 중복이다 — 다시 파싱하지 않는다(재리뷰 R-M1)
        ("ready", False, "stale_message"),
        ("failed", False, "stale_message"),
    ],
)


async def _enter_row_being_indexed(
    user: User, store: Any, status: str, reindex_open: bool
) -> tuple[uuid.UUID, Source, dict[str, Any]]:
    sid = await _source(user, status)
    _seed_raw(store, sid)
    _seed_parsed(store, sid)
    if reindex_open:
        await _open_reindex(sid)
    before = await _row(sid)
    return sid, before, await ingestion._run(str(sid))


@_BEING_INDEXED
async def test_entry_refuses_row_being_indexed(
    user_a: User,
    ingest_objects: Any,
    parser: FakeParser,
    events: list[tuple[str, Any]],
    status: str,
    reindex_open: bool,
    error: str,
) -> None:
    """재배달·중복 `ingest_source`가 색인 중이거나 끝난 행을 만나면 아무것도 쓰지 않는다."""
    sid, before, result = await _enter_row_being_indexed(
        user_a, ingest_objects, status, reindex_open
    )

    assert result["ok"] is False and "handoff" not in result
    after = await _row(sid)
    assert (after.status, after.updated_at, after.error_message) == (
        before.status,
        before.updated_at,
        before.error_message,
    )
    assert parser.calls == 0
    assert ingest_objects.puts == [] and events == []


@_BEING_INDEXED
async def test_entry_refusal_reports_its_reason(
    user_a: User,
    ingest_objects: Any,
    parser: FakeParser,
    status: str,
    reindex_open: bool,
    error: str,
) -> None:
    """물러난 사유가 결과의 `error`로 구분된다(계획 §3.7) — 진단·제품 대역이 이 값을 본다."""
    _, _, result = await _enter_row_being_indexed(user_a, ingest_objects, status, reindex_open)

    assert result == {"ok": False, "error": error}


# ---------------------------------------------------------------------------
# reaper — 멈춘 단계부터 조건부 전이로 재개한다(수락 기준 4·9)
# ---------------------------------------------------------------------------


async def test_reaper_resumes_each_stalled_stage(
    user_a: User, published: list[tuple[str, list[str], dict[str, Any]]]
) -> None:
    rows: dict[str, uuid.UUID] = {}
    for name, status, parsed in [
        ("chunking", "chunking", True),
        ("embedding", "embedding", True),
        ("parsing", "parsing", True),  # 파싱 중 멈춤 — 이전 parsed.md가 있어도 처음부터다
        ("no_parsed", "chunking", False),  # 예외 상황 — 색인할 parsed.md가 없다
    ]:
        sid = await _source(user_a, status, parsed=parsed, type_="text")
        await _write(
            sid,
            error_message="옛 오류",
            updated_at=datetime.now(UTC) - timedelta(minutes=STALE_MINUTES),
        )
        rows[name] = sid
    cutoff = _cutoff()

    assert await ingestion._reap_stalled() == {"requeued": 4, "reindex_requeued": 0}

    # 색인 단계는 해시·출발 상태 없이(`chunking` 기본값) 낸다. 큐 인자는 어느 쪽도 없다(HWP 꺼짐)
    assert sorted(published) == sorted(
        [
            ("index_source", [str(rows["chunking"])], {}),
            ("index_source", [str(rows["embedding"])], {}),
            ("ingest_source", [str(rows["parsing"])], {}),
            ("ingest_source", [str(rows["no_parsed"])], {}),
        ]
    )
    expected = {
        "chunking": "chunking",
        "embedding": "chunking",
        "parsing": "pending",
        "no_parsed": "pending",
    }
    for name, sid in rows.items():
        row = await _row(sid)
        assert (row.status, row.error_message) == (expected[name], None), name
        assert row.updated_at > cutoff, name


async def test_reaper_bumps_already_pending_row_so_next_sweep_skips_it(
    user_a: User, published: list[tuple[str, list[str], dict[str, Any]]]
) -> None:
    """이미 `pending`·`error_message=NULL`인 행도 `updated_at`을 갱신한다 — 같은 값 대입으로
    UPDATE가 빠지면 다음 스윕(5분 뒤)이 같은 행을 또 낸다."""
    sid = await _source(user_a, "pending", parsed=False, type_="text")
    stale = await _age(sid)
    cutoff = _cutoff()

    first = await ingestion._reap_stalled()
    row = await _row(sid)
    second = await ingestion._reap_stalled()

    assert first == {"requeued": 1, "reindex_requeued": 0}
    assert (row.status, row.error_message) == ("pending", None)
    assert row.updated_at > cutoff > stale
    assert second == {"requeued": 0, "reindex_requeued": 0}
    assert published == [("ingest_source", [str(sid)], {})]


async def test_reaper_does_not_overwrite_rows_moved_after_its_select(
    user_a: User,
    published: list[tuple[str, list[str], dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """조회와 전이 사이에 바뀐 행은 덮지 않는다 — 전이 조건이 본 상태와 cutoff를 다시 본다."""
    finished = await _source(user_a, "embedding", type_="text")  # 그 사이 교체를 끝냈다
    beating = await _source(user_a, "embedding", type_="text")  # 그 사이 heartbeat가 갱신했다
    control = await _source(user_a, "chunking", type_="text")
    for sid in (finished, beating, control):
        await _age(sid)
    moved_at: dict[str, datetime] = {}
    real_factory = ingestion.get_session_factory()
    hooked = False

    def factory() -> Any:
        session = real_factory()
        execute = session.execute

        async def execute_then_move(stmt: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal hooked
            result = await execute(stmt, *args, **kwargs)
            if not hooked and isinstance(stmt, Select):
                # 정체 조회가 끝난 직후 다른 실행들이 커밋한다
                hooked = True
                moved_at["finished"] = await _write(finished, status="ready")
                moved_at["beating"] = await _write(beating)
            return result

        session.execute = execute_then_move  # type: ignore[method-assign]
        return session

    monkeypatch.setattr(ingestion, "get_session_factory", lambda: factory)

    result = await ingestion._reap_stalled()

    assert hooked
    assert result == {"requeued": 1, "reindex_requeued": 0}
    assert published == [("index_source", [str(control)], {})]
    row = await _row(finished)
    assert (row.status, row.updated_at) == ("ready", moved_at["finished"])
    row = await _row(beating)
    assert (row.status, row.updated_at) == ("embedding", moved_at["beating"])
    assert (await _row(control)).status == "chunking"


async def _defer_new_source(
    user: User, space: FakeSpace, store: Any
) -> tuple[uuid.UUID, dict[str, Any]]:
    """파싱을 마친 신규 소스의 색인 단계가 일시 오류로 미뤘다 — `(소스, retry_kwargs)`."""
    sid = await _source(user, "chunking")
    _seed_parsed(store, sid, locations=LOCATIONS)
    space.fail = True
    deferred = await ingestion._run_index(str(sid), parsed_sha256=DIGEST)
    space.fail = False
    row = await _row(sid)
    assert row.status == "pending" and row.error_message.startswith("임베딩 지연 — ")
    assert deferred["retry_kwargs"]["claim_from"] == "pending"
    assert deferred["retry_kwargs"]["deferred_at"] == row.updated_at.isoformat()
    return sid, deferred["retry_kwargs"]


async def test_stale_pending_deferral_still_claims_with_matching_token(
    user_a: User, space: FakeSpace, ingest_objects: Any
) -> None:
    """대조군 — 큐 적체로 재시도가 reaper 창을 넘겨도, 그 사이 아무도 행을 건드리지 않았으면
    미룰 때의 토큰으로 점유해 끝낸다."""
    sid, retry = await _defer_new_source(user_a, space, ingest_objects)
    # 미룸이 15분 넘게 지난 것처럼 옮긴다 — 토큰도 그 시각이다(미룬 실행이 그때 쓴 값)
    retry = {**retry, "deferred_at": (await _age(sid)).isoformat()}

    result = await ingestion._run_index(str(sid), **retry)

    assert result == {"ok": True, "chunks": len(POSITIONS)}
    assert (await _row(sid)).status == "ready"


async def test_reaper_bump_invalidates_pending_deferral_token(
    user_a: User,
    space: FakeSpace,
    ingest_objects: Any,
    events: list[tuple[str, Any]],
    published: list[tuple[str, list[str], dict[str, Any]]],
) -> None:
    """reaper가 멈춘 `pending`을 처음부터 다시 내면(D3-B 한계) `updated_at`이 바뀌어 낡은 재시도의
    토큰이 맞지 않는다 — 낡은 재시도는 점유하지 못하고 아무것도 쓰지 않는다."""
    sid, retry = await _defer_new_source(user_a, space, ingest_objects)
    retry = {**retry, "deferred_at": (await _age(sid)).isoformat()}
    assert (await ingestion._reap_stalled())["requeued"] == 1
    assert published == [("ingest_source", [str(sid)], {})]
    reaped = await _row(sid)
    seen = len(events)

    result = await ingestion._run_index(str(sid), **retry)

    assert result == {"ok": False, "error": "claim_lost"}
    after = await _row(sid)
    assert (after.status, after.updated_at) == ("pending", reaped.updated_at)
    assert await _chunks(sid) == []
    assert events[seen:] == []


# ---------------------------------------------------------------------------
# 미룬 신규 소스의 재시도 — 색인 단계만 다시 돈다(수락 기준 2)
# ---------------------------------------------------------------------------


async def test_deferred_new_source_retries_index_stage_only(
    user_a: User,
    space: FakeSpace,
    ingest_objects: Any,
    parser: FakeParser,
    events: list[tuple[str, Any]],
) -> None:
    sid = await _source(user_a, "pending", parsed=False)
    _seed_raw(ingest_objects, sid)
    entry = await ingestion._run(str(sid))
    assert entry["handoff"] == {
        "parsed_sha256": DIGEST, "keep_previous": False, "claim_from": "chunking",
        "transient_attempt": 0, "gate_attempt": 0,
    }

    space.fail = True
    deferred = await ingestion._run_index(str(sid), **entry["handoff"])
    row = await _row(sid)
    assert row.status == "pending" and row.error_message.startswith("임베딩 지연 — ")
    assert deferred["retry_kwargs"] == {
        "parsed_sha256": DIGEST, "keep_previous": False, "claim_from": "pending",
        "deferred_at": row.updated_at.isoformat(), "transient_attempt": 1, "gate_attempt": 0,
    }
    assert await _chunks(sid) == []

    space.fail = False
    result = await ingestion._run_index(str(sid), **deferred["retry_kwargs"])

    assert result == {"ok": True, "chunks": len(POSITIONS)}
    assert parser.calls == 1  # 재시도는 파서를 부르지 않는다
    row = await _row(sid)
    assert (row.status, row.error_message) == ("ready", None)
    # 위치는 첫 파싱이 남긴 locations.json에서 다시 읽었다
    assert _positions(await _chunks(sid)) == POSITIONS
    assert _statuses(events, sid) == [
        "parsing", "chunking", "embedding", "pending", "embedding", "ready",
    ]


async def test_reparse_after_deferral_beats_the_stale_retry(
    user_a: User, space: FakeSpace, ingest_objects: Any, parser: FakeParser
) -> None:
    """미룬 `pending`을 처음부터 다시 처리한 경로(reaper 재발행, D3-B 한계) — 재파싱이 행을
    가져가면 낡은 재시도는 점유하지 못하고, 재파싱이 넘긴 색인 단계가 끝낸다."""
    sid = await _source(user_a, "pending", parsed=False)
    _seed_raw(ingest_objects, sid)
    entry = await ingestion._run(str(sid))
    space.fail = True
    deferred = await ingestion._run_index(str(sid), **entry["handoff"])
    space.fail = False

    again = await ingestion._run(str(sid))  # pending → parsing → chunking
    assert again["ok"] is True and parser.calls == 2
    stale = await ingestion._run_index(str(sid), **deferred["retry_kwargs"])

    assert stale == {"ok": False, "error": "claim_lost"}
    assert (await _row(sid)).status == "chunking"
    assert await _chunks(sid) == []
    assert (await ingestion._run_index(str(sid), **again["handoff"]))["ok"] is True
    assert (await _row(sid)).status == "ready"


# ---------------------------------------------------------------------------
# 재인덱싱 — 점유 전까지 이전 색인으로 검색된다(리뷰 B2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["ready", "failed"])
async def test_stored_reindex_entry_leaves_row_untouched(
    user_a: User,
    ingest_objects: Any,
    parser: FakeParser,
    events: list[tuple[str, Any]],
    status: str,
) -> None:
    """저장된 parsed.md로 하는 재인덱싱 진입은 파싱도 상태 쓰기도 하지 않는다 — status와
    `updated_at`이 그대로다. 색인 단계가 그 상태에서 점유한다."""
    sid = await _source(user_a, status)
    _seed_parsed(ingest_objects, sid)
    await _open_reindex(sid)
    before = await _row(sid)

    entry = await ingestion._run(str(sid))

    assert entry["handoff"] == {
        "parsed_sha256": None, "keep_previous": status == "ready", "claim_from": status,
        "transient_attempt": 0, "gate_attempt": 0,
    }
    after = await _row(sid)
    assert (after.status, after.updated_at, after.error_message) == (
        before.status,
        before.updated_at,
        before.error_message,
    )
    assert parser.calls == 0 and events == [] and ingest_objects.puts == []


async def test_refetch_reindex_entry_returns_row_to_ready_after_parsing(
    user_a: User,
    space: FakeSpace,
    ingest_objects: Any,
    parser: FakeParser,
    events: list[tuple[str, Any]],
) -> None:
    """원본부터 다시 파싱하는 재인덱싱(refetch)은 파싱 뒤 `ready`로 되돌리고 ready에서 점유한다."""
    sid = await _source(user_a, "ready")
    _seed_raw(ingest_objects, sid)
    item = await _open_reindex(sid, refetch=True)

    entry = await ingestion._run(str(sid))

    assert entry["handoff"] == {
        "parsed_sha256": DIGEST, "keep_previous": True, "claim_from": "ready",
        "transient_attempt": 0, "gate_attempt": 0,
    }
    assert parser.calls == 1
    assert (await _row(sid)).status == "ready"
    assert await _item_status(item) == "queued"

    assert (await ingestion._run_index(str(sid), **entry["handoff"]))["ok"] is True
    assert _positions(await _chunks(sid)) == POSITIONS
    assert await _item_status(item) == "done"
    assert _statuses(events, sid) == ["parsing", "ready", "embedding", "ready"]


async def test_reindex_claim_needs_open_item(
    user_a: User,
    space: FakeSpace,
    ingest_objects: Any,
    events: list[tuple[str, Any]],
) -> None:
    """ready·failed에서의 점유는 열린 재인덱싱 항목이 같은 문장 안에서 있어야 한다 — 그 사이 다른
    실행이 처리를 끝내 항목이 닫혔으면 이전 색인을 건드리지 않는다."""
    sid = await _source(user_a, "chunking")
    _seed_parsed(ingest_objects, sid)
    assert (await ingestion._run_index(str(sid)))["ok"] is True  # 이전 색인
    previous = await _chunks(sid)
    item = await _open_reindex(sid)
    entry = await ingestion._run(str(sid))
    await _close_item(item)
    before = await _row(sid)
    seen = len(events)

    result = await ingestion._run_index(str(sid), **entry["handoff"])

    assert result == {"ok": False, "error": "claim_lost"}
    after = await _row(sid)
    assert (after.status, after.updated_at) == ("ready", before.updated_at)
    assert [c.id for c in await _chunks(sid)] == [c.id for c in previous]
    assert await _vector_ids(sid) == {c.id for c in previous}
    assert events[seen:] == []


# ---------------------------------------------------------------------------
# HWP 위치 — 단계 사이와 refetch=false 재인덱싱에서 유지된다(수락 기준 3, REINDEX-02)
# ---------------------------------------------------------------------------


async def test_hwp_locations_survive_handoff_and_stored_reindex(
    user_a: User,
    space: FakeSpace,
    ingest_objects: Any,
    parser: FakeParser,
) -> None:
    sid = await _source(user_a, "pending", parsed=False)
    _seed_raw(ingest_objects, sid)

    entry = await ingestion._run(str(sid))

    # 위치는 parsed.md보다 먼저 올라간다 — parsed.md가 보이는 시점에 같은 세대의 위치가 있다
    parsed_puts = [key for bucket, key in ingest_objects.puts if bucket == _parsed_bucket()]
    assert parsed_puts == [stage_artifacts.locations_key(sid), f"{sid}/parsed.md"]
    payload = ingest_objects.objects[(_parsed_bucket(), stage_artifacts.locations_key(sid))]
    assert (
        stage_artifacts.decode_locations(payload, markdown=MARKDOWN, parsed_sha256=DIGEST)
        == LOCATIONS
    )
    assert (await _row(sid)).status == "chunking"
    assert (await ingestion._run_index(str(sid), **entry["handoff"]))["ok"] is True
    first = await _chunks(sid)
    assert _positions(first) == POSITIONS

    # 재인덱싱(refetch=false) — 저장된 parsed.md와 locations.json에서 다시 청킹한다
    item = await _open_reindex(sid)
    before = await _row(sid)
    reindex = await ingestion._run(str(sid))
    mid = await _row(sid)
    assert (mid.status, mid.updated_at) == ("ready", before.updated_at)  # 점유 전까지 검색된다
    assert (await ingestion._run_index(str(sid), **reindex["handoff"]))["ok"] is True

    assert parser.calls == 1
    second = await _chunks(sid)
    assert _positions(second) == POSITIONS
    assert {c.id for c in second}.isdisjoint({c.id for c in first})
    # 옛 색인 벡터는 행과 함께 사라졌고, 검색 결과 payload에도 위치가 실린다
    assert await _vector_ids(sid) == {c.id for c in second}
    nb = (await _row(sid)).notebook_id
    async with async_session_factory() as s:
        query = ([1.0, 0.0], SparseVector([0], [1.0]))
        hits = await vectorstore.hybrid_search(
            s, nb, *query, space=space.key, local_space=LOCAL_SPACE_KEY
        )
    payloads = sorted((h.payload for h in hits), key=lambda p: p["seq"])
    assert [(p.get("page"), p.get("table_index")) for p in payloads] == POSITIONS
    assert await _item_status(item) == "done"


@pytest.mark.parametrize("artifact", ["missing", "other_generation"])
async def test_stored_reindex_without_matching_locations_has_no_positions(
    user_a: User, space: FakeSpace, ingest_objects: Any, artifact: str
) -> None:
    """locations.json이 없거나(이 기능 전에 파싱된 소스) 다른 세대면 지금처럼 위치 없이 색인한다."""
    sid = await _source(user_a, "ready")
    _seed_parsed(ingest_objects, sid)
    if artifact == "other_generation":
        older = stage_artifacts.parsed_digest(b"older parse")
        ingest_objects.objects[(_parsed_bucket(), stage_artifacts.locations_key(sid))] = (
            stage_artifacts.encode_locations(older, LOCATIONS)
        )
    await _open_reindex(sid)

    entry = await ingestion._run(str(sid))
    assert (await ingestion._run_index(str(sid), **entry["handoff"]))["ok"] is True

    chunks = await _chunks(sid)
    assert _positions(chunks) == NO_POSITIONS
    assert all("locations" not in c.meta for c in chunks)


# ---------------------------------------------------------------------------
# #230 — 재인덱싱 안내는 warnings 코드다. ready 행의 error_message는 비어 있다
# ---------------------------------------------------------------------------


def _codes(row: Source) -> list[str]:
    return [w["code"] for w in row.warnings or []]


async def test_reindex_notes_live_in_warnings_on_real_rows(
    user_a: User, space: FakeSpace, ingest_objects: Any
) -> None:
    """미룸 → 재시도 점유 → 성공의 수명을 실제 행으로 본다.

    - 미룸: ready + `reindex_deferred`, error_message 없음
    - 재시도 점유: `reindex_deferred`가 빠진다
    - 성공: 재인덱싱 안내가 하나도 남지 않고 파싱 경고는 남는다
    """
    sid = await _source(user_a, "ready")
    _seed_parsed(ingest_objects, sid)
    parse_warning = source_warnings.warning(source_warnings.TEXT_TRUNCATED, omitted=2)
    await _write(sid, warnings=[parse_warning], embedding_model=space.key)
    await _open_reindex(sid)

    space.fail = True
    deferred = await ingestion._run_index(str(sid), keep_previous=True, claim_from="ready")
    row = await _row(sid)
    assert "retry_in" in deferred
    assert (row.status, row.error_message) == ("ready", None)
    assert _codes(row) == ["text_truncated", "reindex_deferred"]

    space.fail = False
    done = await ingestion._run_index(str(sid), **deferred["retry_kwargs"])
    row = await _row(sid)
    assert done["ok"] is True
    assert (row.status, row.error_message) == ("ready", None)
    assert row.warnings == [parse_warning]


async def test_kept_previous_failure_is_a_warning_on_real_rows(
    user_a: User, space: FakeSpace, ingest_objects: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = await _source(user_a, "ready")
    _seed_parsed(ingest_objects, sid)
    await _write(sid, embedding_model=space.key)
    item = await _open_reindex(sid)

    async def usable(db: Any, key: str | None) -> Any:
        return space

    monkeypatch.setattr(ingestion, "resolve_space", usable)

    def boom(text: str) -> int:
        raise RuntimeError("교체 중 실패")

    monkeypatch.setattr(ingestion, "count_tokens", boom)

    result = await ingestion._run_index(str(sid), keep_previous=True, claim_from="ready")

    row = await _row(sid)
    assert result["ok"] is False
    assert (row.status, row.error_message) == ("ready", None)
    assert row.warnings == [
        {"code": "reindex_kept_previous", "params": {"reason": "error"}}
    ]
    assert await _item_status(item) == "failed"  # 상세 오류는 재인덱싱 항목에 남는다


async def test_backing_off_retry_drops_its_note_only_without_open_items(
    user_a: User, space: FakeSpace, ingest_objects: Any
) -> None:
    """재리뷰 R-m3 — 물러나는 미룬 재시도는 열린 항목이 없을 때만 자기 안내를 지운다."""
    sid = await _source(user_a, "ready")
    _seed_parsed(ingest_objects, sid)
    await _write(sid, embedding_model=space.key)
    item = await _open_reindex(sid)
    space.fail = True
    retry = (
        await ingestion._run_index(str(sid), keep_previous=True, claim_from="ready")
    )["retry_kwargs"]

    # 그 사이 재인덱싱 회수가 상한으로 항목을 닫았다 — 재시도는 점유하지 못한다
    await _close_item(item)
    # 항목이 모두 닫혔다 — 재시도는 점유하지 못하고 자기 안내를 지운다
    result = await ingestion._run_index(str(sid), **retry)
    row = await _row(sid)
    assert result == {"ok": False, "error": "claim_lost"}
    assert row.status == "ready"
    assert "reindex_deferred" not in _codes(row)


async def test_backing_off_retry_keeps_a_note_of_an_open_run(
    user_a: User, space: FakeSpace, ingest_objects: Any
) -> None:
    sid = await _source(user_a, "ready")
    _seed_parsed(ingest_objects, sid)
    await _write(sid, embedding_model=space.key)
    item = await _open_reindex(sid)
    space.fail = True
    retry = (
        await ingestion._run_index(str(sid), keep_previous=True, claim_from="ready")
    )["retry_kwargs"]
    seen_at = (await _row(sid)).updated_at

    # 항목이 열려 있으면(진행 중인 run의 안내일 수 있다) 정리 문장은 0행이다
    async with async_session_factory() as s:
        cleaned = (
            await s.execute(
                ingestion._drop_stale_deferral_stmt(sid, seen_at, [])
            )
        ).first()
        await s.commit()
    assert cleaned is None
    assert "reindex_deferred" in _codes(await _row(sid))
    assert await _item_status(item) == "queued"
    assert retry["claim_from"] == "ready"
