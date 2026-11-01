"""색인 교체 직렬화 (#27) — 잠금·savepoint·실패 경로.

계획 `docs/plans/ingest-index-swap-lock/plan.md`의 수락 기준 2~6을 DB 없이 확인한다. 벡터는 청크
행(pgvector)에 있어 교체가 한 트랜잭션이다 — 옛 Qdrant 포인트의 스냅샷·보상 삭제는 없어졌다.
동시 실행 인터리빙(수락 기준 1)은 `test_ingest_stage_tasks_pg.py`가 실제 PostgreSQL로 본다.
"""

import uuid
from typing import Any

import pytest
from conftest import RunHarness
from sqlalchemy.exc import DBAPIError, OperationalError

from biznotes_core.services.embedding_spaces import LOCAL_SPACE
from biznotes_core.workers.tasks import ingestion

MARKDOWN = "# 제목\n\n" + "본문 문장입니다. " * 40


class _PgError(Exception):
    def __init__(self, sqlstate: str):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _db_error(sqlstate: str) -> DBAPIError:
    return DBAPIError("stmt", {}, _PgError(sqlstate))


def _events(h: RunHarness) -> list[str]:
    """타임라인에서 교체 구간의 사건만 추린다."""
    out: list[str] = []
    for kind, value in h.timeline:
        if kind == "db.execute":
            if "pg_advisory_xact_lock" in value:
                out.append("lock")
            elif "FOR KEY SHARE" in value:
                out.append("key_share")
            elif value.startswith("DELETE FROM source_chunks"):
                out.append("delete_rows")
        elif kind in ("db.savepoint", "db.release", "db.rollback_to_savepoint", "db.rollback"):
            out.append(kind.removeprefix("db."))
        elif kind == "db.add_chunks":
            out.append("insert_rows")
        elif kind == "db.commit":
            out.append(f"commit:{value}")
    return out


@pytest.fixture
def h(run_harness: RunHarness) -> RunHarness:
    from biznotes_core.services.ingestion.parsers import ParseResult

    harness = run_harness
    harness.parser(lambda data: ParseResult(markdown=MARKDOWN))
    return harness


def _after_lock(h: RunHarness) -> list[str]:
    ev = _events(h)
    return ev[ev.index("lock") :]


# ── 성공 경로 ────────────────────────────────────────────────────────────


async def test_swap_order_under_the_lock(h: RunHarness) -> None:
    """수락 기준 3 — 잠금 → FOR KEY SHARE → savepoint → 옛 행 삭제 → 새 행(벡터 포함) → 커밋.

    옛 벡터는 옛 행과 함께 지워진다 — 커밋 뒤에 따로 지울 것이 없다."""
    result = await h.run()

    assert result["ok"] is True
    assert _after_lock(h) == [
        "lock",
        "key_share",
        "savepoint",
        "delete_rows",
        "insert_rows",
        "release",
        "commit:ready",
    ]
    [rows] = h.indexed
    assert rows and all(r.embedding is not None and r.sparse is not None for r in rows)


async def test_lock_timeout_is_scoped_to_the_lock_statement(h: RunHarness) -> None:
    """D5 — 대기 상한은 잠금 문장에만 걸고 획득 직후 기본값으로 되돌린다."""
    await h.run()

    sqls = [v for k, v in h.timeline if k == "db.execute"]
    i_set = next(i for i, v in enumerate(sqls) if "set_config('lock_timeout'" in v)
    i_lock = next(i for i, v in enumerate(sqls) if "pg_advisory_xact_lock" in v)
    i_reset = next(i for i, v in enumerate(sqls) if "lock_timeout TO DEFAULT" in v)
    assert i_set < i_lock < i_reset


async def test_read_transaction_ends_before_embedding(h: RunHarness) -> None:
    """D1 — 임베딩 동안 읽기 트랜잭션을 붙잡지 않는다: 잠금은 새 트랜잭션에서 잡는다."""
    embedded_at: list[int] = []

    def embed(texts: list[str]) -> list[list[float]]:
        embedded_at.append(len(h.timeline))
        return [[0.0] for _ in texts]

    h.mp.setattr(ingestion, "embed_dense", embed)
    await h.run()

    before_embedding = h.timeline[: embedded_at[0]]
    assert before_embedding[-1] == ("db.commit", "embedding")


# ── savepoint 안 실패: 이전 색인을 지우지 않는다 ─────────────────────────


@pytest.mark.parametrize("where", ["after_insert", "flush_fails"])
async def test_failure_inside_savepoint_keeps_the_previous_index(h: RunHarness, where: str) -> None:
    """수락 기준 2 — savepoint째 되돌려 새 행(벡터)을 버리고, 옛 행·출처 컬럼은 그대로 둔다(V5)."""
    h.source.chunk_count = 7
    h.source.token_count = 70
    h.source.embedding_model = "old-model"
    h.source.chunk_config = {"chunk_size": 400, "chunk_overlap": 50}

    if where == "after_insert":

        def boom(text: str) -> int:
            raise RuntimeError("교체 중 실패")

        h.mp.setattr(ingestion, "count_tokens", boom)
    else:
        # 출처 컬럼·ready를 대입한 **뒤** 실패 — savepoint 되돌림 뒤 다시 읽어야 이전 값이다
        h.flush_error = RuntimeError("flush 실패")

    result = await h.run()

    assert result["ok"] is False
    assert len(h.indexed) == 1  # 새 행은 넣었다가 savepoint째 되돌렸다
    assert (h.source.chunk_count, h.source.token_count) == (7, 70)
    assert h.source.embedding_model == "old-model"
    assert h.source.chunk_config == {"chunk_size": 400, "chunk_overlap": 50}
    # 잠금을 쥔 채 savepoint만 되돌리고 같은 트랜잭션에서 실패를 커밋한다 — 잠금 뒤 커밋 1회
    after = _after_lock(h)
    assert "rollback" not in after
    assert after[-2:] == ["rollback_to_savepoint", "commit:failed"]
    assert [e for e in after if e.startswith("commit:")] == ["commit:failed"]
    assert h.closed_reindex[-1][1] is False


async def test_reindex_failure_keeps_previous_index_ready(h: RunHarness) -> None:
    """keep_previous — 이전 색인은 유효하므로 ready + 경고로 둔다."""
    h.source.status = "ready"
    h.source.parsed_object_key = f"{h.source.id}/parsed.md"
    h.source.embedding_model = LOCAL_SPACE.key
    h.raw = MARKDOWN.encode()

    def boom(text: str) -> int:
        raise RuntimeError("교체 중 실패")

    h.mp.setattr(ingestion, "count_tokens", boom)
    h.open_reindex = (True, False)  # 열린 재인덱싱 항목
    result = await h.run(reindex=True, refetch=False)

    assert result["ok"] is False
    assert h.committed[-1]["status"] == "ready"
    assert h.committed[-1]["error_message"] is None  # 유지 안내는 경고 코드다(#230)
    assert h.source.warnings[-1] == {
        "code": "reindex_kept_previous",
        "params": {"reason": "error"},
    }
    assert ("db.rollback_to_savepoint", None) in h.timeline


async def test_lock_timeout_on_a_later_statement_is_not_busy(h: RunHarness) -> None:
    """수락 기준 5 — 잠금 뒤 문장의 55P03은 busy가 아니다: 되돌리고 실패를 쓴다(R2-04)."""
    h.execute_error = lambda sql: (
        _db_error("55P03") if sql.startswith("DELETE FROM source_chunks") else None
    )

    result = await h.run()

    assert result["ok"] is False and result["error"] != "index_lock_busy"
    assert h.indexed == []  # 옛 행 삭제에서 막혀 새 행을 넣기 전이다
    assert h.committed[-1]["status"] == "failed"


# ── 잠금 전·잠금 문장·잠금 뒤 DB 오류 ─────────────────────────────────────


@pytest.mark.parametrize("sqlstate", ["55P03", "57014"])
async def test_lock_busy_writes_nothing(h: RunHarness, sqlstate: str) -> None:
    """수락 기준 4 — 잠금을 못 얻으면 상태·재인덱싱 항목을 쓰지 않고 물러난다."""
    h.execute_error = lambda sql: (
        _db_error(sqlstate) if "pg_advisory_xact_lock" in sql else None
    )

    result = await h.run()

    assert result == {"ok": False, "error": "index_lock_busy"}
    assert h.committed[-1]["status"] == "embedding"  # 잠금 뒤에는 아무것도 커밋하지 않았다
    assert h.indexed == []
    assert h.closed_reindex == []


async def test_source_deleted_before_the_swap_writes_nothing(h: RunHarness) -> None:
    """D2 — 잠금 뒤 소스 행이 없으면 아무것도 넣거나 쓰지 않는다."""
    h.source_exists = False

    result = await h.run()

    assert result == {"ok": False, "error": "source not found"}
    assert h.indexed == []
    assert h.committed[-1]["status"] == "embedding"
    assert h.closed_reindex == []


async def test_db_error_after_the_lock_writes_nothing(h: RunHarness) -> None:
    """예외 규칙 — 잠금 뒤 savepoint 전 DB 오류는 트랜잭션이 깨졌다: 롤백만 하고 reaper에 맡긴다."""
    h.execute_error = lambda sql: (
        OperationalError("stmt", {}, Exception("conn")) if "FOR KEY SHARE" in sql else None
    )

    result = await h.run()

    assert result["ok"] is False
    assert _after_lock(h)[-1] == "rollback"
    assert h.committed[-1]["status"] == "embedding"
    assert h.indexed == []


async def test_ambiguous_commit_writes_nothing(h: RunHarness) -> None:
    """수락 기준 5 — COMMIT이 예외면(서버는 커밋했을 수 있다) 상태를 쓰지 않고 reaper에 맡긴다."""
    h.commit_error = lambda status: ConnectionError("lost") if status == "ready" else None

    result = await h.run()

    assert result["ok"] is False
    assert all(c["status"] != "failed" for c in h.committed)  # 상태도 쓰지 않는다
    assert h.closed_reindex == []


@pytest.mark.parametrize("failing", ["publish", "overview"])
async def test_post_commit_side_effect_failure_keeps_ready(h: RunHarness, failing: str) -> None:
    """커밋 뒤 이벤트 발행(Valkey)·개요 예약 실패는 색인과 무관하다 — failed로 덮지 않고,
    재인덱싱 항목 닫기를 건너뛰지 않는다(phase 리뷰 F-01)."""
    from biznotes_core.workers.tasks import overview

    def boom(*a: Any, **k: Any) -> None:
        raise ConnectionError("redis down")

    real_publish = ingestion._publish_status

    def publish(source: Any, status: str, error: str | None) -> None:
        if status == "ready":
            raise ConnectionError("redis down")
        real_publish(source, status, error)

    if failing == "publish":
        h.mp.setattr(ingestion, "_publish_status", publish)
    else:
        h.mp.setattr(overview, "schedule_overview_refresh", boom)
    result = await h.run()

    assert result["ok"] is True
    assert h.committed[-1]["status"] == "ready"
    assert h.closed_reindex[-1][1] is True


def _refresh_shows_ready(h: RunHarness) -> Any:
    """잠금 아래 다시 읽은 행이 ready — 그 사이 다른 실행이 교체를 커밋했다."""
    from conftest import MainSession

    real = MainSession.refresh

    async def refresh(self: Any, obj: Any) -> None:
        await real(self, obj)
        h.source.status = "ready"
        h.source.error_message = None

    return refresh


async def test_failure_after_another_run_committed_keeps_its_ready(h: RunHarness) -> None:
    """phase 리뷰 F-02 — 다른 실행이 이미 교체를 커밋했으면 그 ready를 failed로 덮지 않는다."""
    from conftest import MainSession

    def boom(text: str) -> int:
        raise RuntimeError("교체 중 실패")

    h.mp.setattr(ingestion, "count_tokens", boom)
    h.mp.setattr(MainSession, "refresh", _refresh_shows_ready(h))

    result = await h.run()

    assert result["ok"] is False
    assert all(c["status"] != "failed" for c in h.committed)
    assert h.closed_reindex == []  # 항목은 교체를 커밋한 실행이 닫는다


async def test_refresh_failure_after_savepoint_rollback_writes_nothing(h: RunHarness) -> None:
    """phase 리뷰 F-04 — 되돌림 뒤 행을 다시 읽지 못하면(연결 상실) 트랜잭션을 되살릴 수 없다:
    상태는 쓰지 않고 롤백한다."""
    from conftest import MainSession

    def boom(text: str) -> int:
        raise RuntimeError("교체 중 실패")

    async def refresh(self: Any, obj: Any) -> None:
        raise OperationalError("stmt", {}, Exception("conn lost"))

    h.mp.setattr(ingestion, "count_tokens", boom)
    h.mp.setattr(MainSession, "refresh", refresh)
    result = await h.run()

    assert result["ok"] is False
    assert _after_lock(h)[-1] == "rollback"
    assert all(c["status"] != "failed" for c in h.committed)


async def test_unreadable_row_after_prep_failure_writes_nothing(h: RunHarness) -> None:
    """수락 기준 5 — 잠금 전 실패 뒤 행을 다시 읽지 못하면(행 부재 외 오류) 상태를 쓰지 않는다."""
    from biznotes_core.services.ingestion.parsers import ParseError

    def parser(data: bytes) -> Any:
        raise ParseError("손상된 문서")

    h.parser(parser)

    async def refresh(obj: Any) -> None:
        raise OperationalError("stmt", {}, Exception("conn"))

    h.mp.setattr(ingestion, "get_session_factory", _factory_with_refresh(h, refresh))
    result = await h.run()

    assert result["ok"] is False
    assert all(c["status"] != "failed" for c in h.committed)
    assert h.indexed == [] and h.closed_reindex == []


# ── 잠금 밖 실패: 시작 뒤 다른 실행이 교체를 커밋했으면 덮지 않는다(#50) ──────────


def _embedding_fails(h: RunHarness, *, other_run_commits: bool) -> None:
    """임베딩 단계(잠금 전)에서 실패한다. `other_run_commits`면 그 전에 다른 실행이 교체를
    커밋한 것처럼 청크 행을 바꾼다 — 교체는 행을 전부 새 id로 만든다."""

    def embed(texts: list[str]) -> list[list[float]]:
        if other_run_commits:
            h.chunk_ids = [uuid.uuid4(), uuid.uuid4()]
        raise RuntimeError("임베딩 모델 호출 실패")

    h.mp.setattr(ingestion, "embed_dense", embed)


def _prep_failure_events(h: RunHarness) -> list[str]:
    """실패 처리 구간(잠금 전 실패의 첫 롤백부터)의 사건."""
    first = next(i for i, e in enumerate(h.timeline) if e[0] == "db.rollback")
    out: list[str] = []
    for kind, value in h.timeline[first:]:
        if kind == "db.scalar":
            out.append("try_lock" if "pg_try_advisory_xact_lock" in value else "marker")
        elif kind in ("db.rollback", "db.refresh"):
            out.append(kind.removeprefix("db."))
        elif kind == "db.commit":
            out.append(f"commit:{value}")
    return out


@pytest.mark.parametrize("had_index", [False, True])
async def test_prep_failure_after_another_run_swapped_restores_ready(
    h: RunHarness, had_index: bool
) -> None:
    """#50 재현 — 이 실행이 시작한 뒤 다른 실행이 교체를 커밋했다. 이 실행이 그 뒤에 쓴 진행
    상태(embedding)를 failed가 아니라 ready로 되돌린다. 첫 인제스천(행 없음)도 같다."""
    if had_index:
        h.chunk_ids = [uuid.uuid4()]
    _embedding_fails(h, other_run_commits=True)

    result = await h.run()

    assert result["ok"] is False
    assert all(c["status"] != "failed" for c in h.committed)
    assert h.committed[-1] == {
        "status": "ready",
        "error_message": None,
        "parsed_object_key": h.source.parsed_object_key,
    }
    # 잠금을 쥔 뒤에 다시 읽고 표식을 대조한다
    assert _prep_failure_events(h) == ["rollback", "try_lock", "refresh", "marker", "commit:ready"]
    assert h.closed_reindex == []  # 항목은 교체를 커밋한 실행이 닫는다


async def test_prep_failure_after_another_run_swapped_keeps_terminal_status(
    h: RunHarness,
) -> None:
    """#50 — 다른 실행의 ready가 이 실행의 마지막 기록보다 뒤라 행이 이미 ready면 그대로 둔다."""
    from conftest import MainSession

    _embedding_fails(h, other_run_commits=True)
    h.mp.setattr(MainSession, "refresh", _refresh_shows_ready(h))

    result = await h.run()

    assert result["ok"] is False
    assert all(c["status"] != "failed" for c in h.committed)
    assert _prep_failure_events(h)[-1] == "commit:ready"
    assert h.closed_reindex == []


async def test_prep_failure_without_a_new_swap_still_records_failure(h: RunHarness) -> None:
    """#50 음성 대조 — 청크 행이 시작 때와 같으면 기존처럼 실패를 쓰고 항목을 닫는다."""
    h.chunk_ids = [uuid.uuid4()]
    _embedding_fails(h, other_run_commits=False)

    result = await h.run()

    assert result["ok"] is False
    assert h.committed[-1]["status"] == "failed"
    assert _prep_failure_events(h) == [
        "rollback",
        "try_lock",
        "refresh",
        "marker",
        "commit:failed",
    ]
    assert [ok for _, ok, _ in h.closed_reindex] == [False]


async def test_prep_failure_while_another_run_holds_the_lock_writes_nothing(
    h: RunHarness,
) -> None:
    """#50 — 다른 실행이 지금 교체 중이면(비차단 잠금 실패) 그 실행이 결과를 쓴다."""
    h.try_lock_result = False
    _embedding_fails(h, other_run_commits=False)

    result = await h.run()

    assert result["ok"] is False
    assert h.committed[-1]["status"] == "embedding"
    assert _prep_failure_events(h) == ["rollback", "try_lock", "rollback"]
    assert h.closed_reindex == []


async def test_prep_failure_lock_error_writes_nothing(h: RunHarness) -> None:
    """#50 — 비차단 잠금 문장이 실패하면(연결 오류 등) 상태를 쓰지 않고 reaper에 맡긴다."""
    h.execute_error = lambda sql: (
        OperationalError("stmt", {}, Exception("conn"))
        if "pg_try_advisory_xact_lock" in sql
        else None
    )
    _embedding_fails(h, other_run_commits=False)

    result = await h.run()

    assert result["ok"] is False
    assert h.committed[-1]["status"] == "embedding"
    assert h.closed_reindex == []


async def test_prep_failure_inside_open_transaction_does_not_read_expired_source(
    h: RunHarness,
) -> None:
    """#50 리뷰 — 트랜잭션이 열린 채(예: RAG 설정 조회 뒤 빈 문서) 잠금 전 실패하면 롤백이
    기본 키까지 만료시킨다. 잠금 키를 source에서 읽으면 실패를 쓰지 못하고 행이 진행 상태로
    남는다. 키는 태스크 인자로 만들고, 행은 refresh 뒤에만 읽는다."""
    from biznotes_core.services.ingestion.parsers import ParseResult

    h.parser(lambda data: ParseResult(markdown="   "))  # 청크 0개 → ParseError
    h.expire_on_rollback = True

    result = await h.run()

    assert result["ok"] is False
    assert h.committed[-1]["status"] == "failed"
    assert "빈 문서" in (h.committed[-1]["error_message"] or "")
    assert [ok for _, ok, _ in h.closed_reindex] == [False]


def _factory_with_refresh(h: RunHarness, refresh: Any) -> Any:
    from conftest import HeartbeatSession, MainSession

    main = MainSession(h)
    main.refresh = refresh  # type: ignore[method-assign]
    sessions = iter([main])
    return lambda: lambda: next(sessions, None) or HeartbeatSession(h)


# ── 잠금 키 ─────────────────────────────────────────────────────────────


def test_lock_key_is_per_source_and_namespaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """수락 기준 6 — 다른 소스는 다른 키(서로 기다리지 않는다), 같은 소스는 같은 키."""
    from conftest import make_settings

    a, b = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(ingestion, "get_settings", lambda: make_settings(service_name="svc-a"))
    key_a, key_a2, key_b = (
        ingestion._source_index_lock_key(a),
        ingestion._source_index_lock_key(a),
        ingestion._source_index_lock_key(b),
    )
    monkeypatch.setattr(ingestion, "get_settings", lambda: make_settings(service_name="svc-b"))

    assert key_a == key_a2 and key_a != key_b
    assert ingestion._source_index_lock_key(a) != key_a  # 제품마다 네임스페이스가 다르다
    assert -(2**63) <= key_a < 2**63


def test_sqlstate_reads_the_driver_error() -> None:
    assert ingestion._sqlstate(_db_error("55P03")) == "55P03"
    assert ingestion._sqlstate(RuntimeError("x")) is None
