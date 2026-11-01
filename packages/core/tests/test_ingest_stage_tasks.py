"""인제스트 단계 분리 — 진입·파싱(`_run`)과 색인(`_run_index`)의 경계 (#244).

계획 `docs/plans/ingest-stage-tasks/plan.md`의 수락 기준 1·3·6·8·10을 `RunHarness`로 확인한다.
미룸·재시도는 `test_ingest_transient_retry.py`, 실제 PostgreSQL의 점유 경합·reaper는
`tests/test_ingest_stage_tasks_pg.py`가 본다.
"""

import json
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from conftest import MissingObject, RunHarness

from biznotes_core.services import object_cleanup
from biznotes_core.services.ingestion import stage_artifacts
from biznotes_core.services.ingestion.chunker import chunk_markdown
from biznotes_core.services.ingestion.parsers import Location, ParseResult
from biznotes_core.workers.tasks import ingestion

MARKDOWN = "# 제목\n\n## Page 1\n첫 쪽 본문입니다.\n\n## Page 2\n둘째 쪽 본문입니다."
LOCATIONS = [
    Location(MARKDOWN.index("## Page 1"), MARKDOWN.index("## Page 2"), page=1),
    Location(MARKDOWN.index("## Page 2"), len(MARKDOWN), page=2),
]
_LOC_KEYS = {"page", "pages", "locations", "table_index", "section", "paragraph"}


class GatewaySpace:
    """게이트웨이 임베딩 공간 대역 — 호출을 센다."""

    is_local = False
    key = "gateway:fake"
    collection = "chunks_fake"
    display_name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str], **kw: Any) -> list[list[float]]:
        self.calls += 1
        return [[0.0] for _ in texts]


def _forbid(h: RunHarness, *names: str) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("진입·파싱 단계가 색인 단계의 일을 했다")

    for name in names:
        h.mp.setattr(ingestion, name, boom)


class _IndexedPoints(Sequence[dict[str, Any]]):
    """색인 교체가 넣은 청크 행을 검색 결과 모양(`payload`)으로 본다 — 교체 묶음을 이어 붙인다."""

    def __init__(self, h: RunHarness) -> None:
        self.h = h

    def _all(self) -> list[dict[str, Any]]:
        return [
            {
                "id": c.id,
                "payload": {
                    **c.meta,
                    "notebook_id": str(self.h.source.notebook_id),
                    "source_id": str(c.source_id),
                    "seq": c.seq,
                    "text": c.text,
                },
            }
            for batch in self.h.indexed
            for c in batch
        ]

    def __len__(self) -> int:
        return len(self._all())

    def __getitem__(self, i: Any) -> Any:
        return self._all()[i]

    def __eq__(self, other: object) -> bool:
        return self._all() == other

    __hash__ = None  # type: ignore[assignment]


@pytest.fixture
def points(run_harness: RunHarness) -> _IndexedPoints:
    return _IndexedPoints(run_harness)


def _hwp_parser(h: RunHarness) -> list[bytes]:
    calls: list[bytes] = []

    def parse(data: bytes) -> ParseResult:
        calls.append(data)
        return ParseResult(markdown=MARKDOWN, locations=list(LOCATIONS))

    h.parser(parse)
    h.source.title = "doc.hwp"
    return calls


# ---------------------------------------------------------------------------
# 수락 기준 1 — 진입·파싱 단계는 임베딩·색인을 하지 않는다
# ---------------------------------------------------------------------------


async def test_entry_stage_never_embeds_or_indexes(run_harness, points) -> None:
    """전용 HWP 워커에서 도는 단계다 — 활성 공간 조회·임베딩·색인 교체를 하지 않는다."""
    h = run_harness
    _hwp_parser(h)
    _forbid(
        h, "active_space", "embed_dense", "embed_sparse", "_embed_gateway",
        "_lock_source_index",
    )

    result = await h.run_entry()

    assert result["ok"] is True and result["stage"] == "parsed"
    digest = stage_artifacts.parsed_digest(MARKDOWN.encode())
    assert result["handoff"] == {
        "parsed_sha256": digest,
        "keep_previous": False,
        "claim_from": "chunking",
        "transient_attempt": 0,
        "gate_attempt": 0,
    }
    assert [c["status"] for c in h.committed] == ["parsing", "chunking"]
    assert not any(e[0] == "db.claim" for e in h.timeline)


async def test_index_stage_embeds_through_gateway_without_parsing(run_harness, points) -> None:
    """색인 단계는 저장된 parsed.md만 읽는다 — 파서를 부르지 않고 게이트웨이로 임베딩한다."""
    h = run_harness
    calls = _hwp_parser(h)
    handoff = (await h.run_entry())["handoff"]
    space = GatewaySpace()

    async def active_space(db: Any) -> Any:
        return space

    h.mp.setattr(ingestion, "active_space", active_space)

    result = await h.run_index(**handoff)

    assert result == {"ok": True, "chunks": len(points)}
    assert len(calls) == 1  # 진입 단계의 한 번뿐
    assert space.calls >= 1
    assert h.source.status == "ready" and h.source.embedding_model == "gateway:fake"
    assert [c["status"] for c in h.committed] == ["parsing", "chunking", "embedding", "ready"]
    assert points[0]["payload"]["page"] == 1


async def test_reindex_from_parsed_hands_off_without_parsing(run_harness) -> None:
    """저장된 parsed.md로 재인덱싱 — 진입 단계는 파싱 없이 넘기고 이전 색인 유지를 알린다."""
    h = run_harness
    calls = _hwp_parser(h)
    h.source.status = "ready"
    h.source.parsed_object_key = f"{h.source.id}/parsed.md"

    h.open_reindex = (True, False)  # 열린 재인덱싱 항목
    result = await h.run_entry(reindex=True, refetch=False)

    assert calls == []
    assert h.put_calls == [] and h.hb_statements == []
    assert result["handoff"]["keep_previous"] is True
    assert result["handoff"]["parsed_sha256"] is None  # 지금 parsed.md를 쓴다
    # 상태를 쓰지 않는다 — 색인 단계가 점유할 때까지 이전 색인으로 검색된다
    assert result["handoff"]["claim_from"] == "ready"
    assert [c["status"] for c in h.committed] == ["ready"]
    assert h.source.status == "ready"


@pytest.mark.parametrize("status", ["pending", "parsing", "chunking", "embedding"])
async def test_reindex_from_parsed_backs_off_while_in_flight(run_harness, status) -> None:
    """다른 실행이 처리 중인 소스 — 그 실행이 끝날 때 재인덱싱 항목을 닫는다. 쓰지 않는다."""
    h = run_harness
    h.source.status = status
    h.source.parsed_object_key = f"{h.source.id}/parsed.md"

    h.open_reindex = (True, False)  # 열린 재인덱싱 항목
    result = await h.run_entry(reindex=True, refetch=False)

    assert result == {"ok": False, "error": "in_flight"}
    assert h.committed == [] and h.closed_reindex == []


async def test_refetch_reindex_stays_searchable_until_the_index_claim(run_harness, points) -> None:
    """원본부터 재파싱한 재인덱싱 — 파싱 뒤 `ready`로 되돌려 이전 색인으로 검색되게 둔다."""
    h = run_harness
    calls = _hwp_parser(h)
    h.source.status = "ready"
    h.source.parsed_object_key = f"{h.source.id}/parsed.md"

    h.open_reindex = (True, True)  # 열린 재인덱싱 항목
    entry = await h.run_entry(reindex=True, refetch=True)

    assert len(calls) == 1
    assert [c["status"] for c in h.committed] == ["parsing", "ready"]
    handoff = entry["handoff"]
    assert handoff["claim_from"] == "ready" and handoff["keep_previous"] is True

    assert (await h.run_index(**handoff))["ok"] is True
    assert [c["status"] for c in h.committed][2:] == ["embedding", "ready"]
    assert points[0]["payload"]["page"] == 1


async def test_reindex_claim_requires_an_open_item(run_harness, points) -> None:
    """점유 전에 다른 실행이 재인덱싱을 끝냈다(항목이 닫혔다) — 같은 소스를 다시 색인하지 않는다."""
    h = run_harness
    h.source.status = "ready"
    h.source.parsed_object_key = f"{h.source.id}/parsed.md"
    h.raw = MARKDOWN.encode()
    h.open_reindex = (False, False)

    result = await h.run_index(keep_previous=True, claim_from="ready")

    # 열린 항목 확인은 점유 문장 안에 있다(리뷰 M1 — 따로 조회한 뒤 쓰면 그 사이 닫힐 수 있다)
    assert result == {"ok": False, "error": "claim_lost"}
    assert ("db.update", ("embedding", False)) in h.timeline
    assert h.committed == [] and points == [] and h.get_calls == []


async def test_entry_parse_failure_writes_failed_and_hands_off_nothing(run_harness) -> None:
    from biznotes_core.services.ingestion.parsers import ParseError

    h = run_harness

    def parse(data: bytes) -> ParseResult:
        raise ParseError("손상된 문서")

    h.parser(parse)

    result = await h.run_entry()

    assert result == {"ok": False, "error": "손상된 문서"}
    assert h.committed[-1]["status"] == "failed"
    assert h.closed_reindex == [(str(h.source.id), False, "손상된 문서")]


# ---------------------------------------------------------------------------
# 수락 기준 6 — 비HWP 소스는 분리 전과 같은 청크
# ---------------------------------------------------------------------------


async def test_non_hwp_chunks_match_the_chunker_without_locations(run_harness, points) -> None:
    h = run_harness
    h.parser(lambda data: ParseResult(markdown=MARKDOWN))

    assert (await h.run())["ok"] is True

    expected = chunk_markdown(MARKDOWN, max_tokens=800, overlap=100)
    assert [p["payload"] for p in points] == [
        {
            "notebook_id": str(h.source.notebook_id),
            "source_id": str(h.source.id),
            "seq": c.seq,
            "text": c.text,
            **c.meta,
        }
        for c in expected
    ]
    assert all(not (_LOC_KEYS & p["payload"].keys()) for p in points)
    # 위치가 없는 형식은 위치 파일을 쓰지 않는다
    assert [c["key"] for c in h.put_calls] == [f"{h.source.id}/parsed.md"]


# ---------------------------------------------------------------------------
# 수락 기준 8 — 세대·위치 산출물 검증
# ---------------------------------------------------------------------------


async def test_older_generation_message_indexes_the_current_parse(run_harness, points) -> None:
    """메시지의 세대가 지금 parsed.md와 다르다 — 더 새 파싱이 넘긴 `chunking`을 이 실행이 점유했다.

    점유한 동안에는 파싱이 행을 가져가지 못하므로(리뷰 B1) 점유 뒤 읽은 본문이 현재 세대다.
    옛 본문이 아니라 그 본문을 색인한다.
    """
    h = run_harness
    h.parser(lambda data: ParseResult(markdown=MARKDOWN))
    await h.run_entry()

    result = await h.run_index(parsed_sha256="0" * 64)

    assert result["ok"] is True
    assert "첫 쪽 본문입니다." in points[0]["payload"]["text"]
    assert h.source.status == "ready"


@pytest.mark.parametrize("status", ["chunking", "embedding"])
async def test_parse_entry_refuses_a_row_being_indexed(run_harness, status) -> None:
    """리뷰 B1 — 다른 세대가 색인 중인 행은 다시 파싱하지 않는다.

    그 색인이 읽는 parsed.md를 지킨다.
    """
    h = run_harness
    calls = _hwp_parser(h)
    h.source.status = status

    result = await h.run_entry()

    assert result == {"ok": False, "error": "already_parsed"}
    assert calls == [] and h.committed == [] and h.put_calls == []
    assert h.source.status == status


async def test_failure_before_the_claim_writes_nothing(run_harness, points) -> None:
    """리뷰 M2 — 점유 전(설정·공간 읽기) 실패는 남의 행일 수 있다. 아무것도 쓰지 않는다."""
    h = run_harness
    h.parser(lambda data: ParseResult(markdown=MARKDOWN))
    handoff = (await h.run_entry())["handoff"]
    committed = len(h.committed)

    async def broken(db: Any) -> Any:
        raise ConnectionError("db 순단")

    h.mp.setattr(ingestion, "active_space", broken)

    result = await h.run_index(**handoff)

    assert result == {"ok": False, "error": "db 순단"}
    assert len(h.committed) == committed and h.source.status == "chunking"
    assert h.closed_reindex == []


async def test_ready_is_written_only_while_the_claim_holds(
    run_harness, points, monkeypatch
) -> None:
    """리뷰 B1 — 교체 직전 행이 `embedding`이 아니면(reaper가 되돌림) ready를 쓰지 않는다.

    savepoint를 되돌리고 올린 포인트를 거둔 뒤 물러난다. 행은 되돌린 쪽의 상태로 남는다.
    """
    h = run_harness
    h.parser(lambda data: ParseResult(markdown=MARKDOWN))
    handoff = (await h.run_entry())["handoff"]

    def embed(texts: list[str]) -> list[list[float]]:
        h.source.status = "chunking"  # 임베딩 중 reaper가 되돌렸다
        return [[0.0] for _ in texts]

    monkeypatch.setattr(ingestion, "embed_dense", embed)

    result = await h.run_index(**handoff)

    assert result == {"ok": False, "error": "claim_lost"}
    assert h.source.status == "chunking"
    assert h.committed[-1]["status"] == "embedding"  # 점유 커밋 뒤로는 쓰지 않았다
    # 교체 savepoint째 되돌렸다 — 넣은 청크 행(벡터)은 커밋되지 않는다
    assert ("db.rollback_to_savepoint", None) in h.timeline and h.closed_reindex == []


async def test_locations_of_another_generation_are_ignored(run_harness, points) -> None:
    h = run_harness
    _hwp_parser(h)
    handoff = (await h.run_entry())["handoff"]
    h.objects[f"{h.source.id}/locations.json"] = stage_artifacts.encode_locations(
        stage_artifacts.parsed_digest(b"older"), LOCATIONS
    )

    assert (await h.run_index(**handoff))["ok"] is True

    assert all(not (_LOC_KEYS & p["payload"].keys()) for p in points)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(json.dumps({"v": 9}).encode(), id="schema"),
    ],
)
async def test_malformed_locations_do_not_fail_ingest(run_harness, points, payload) -> None:
    """위치는 부가 메타다 — 비신뢰 산출물이 틀려도 위치 없이 ready까지 간다."""
    h = run_harness
    _hwp_parser(h)
    handoff = (await h.run_entry())["handoff"]
    h.objects[f"{h.source.id}/locations.json"] = payload

    assert (await h.run_index(**handoff))["ok"] is True

    assert h.source.status == "ready"
    assert all(not (_LOC_KEYS & p["payload"].keys()) for p in points)


async def test_oversized_locations_are_not_downloaded(run_harness, points, monkeypatch) -> None:
    h = run_harness
    _hwp_parser(h)
    handoff = (await h.run_entry())["handoff"]
    monkeypatch.setattr(stage_artifacts, "LOCATIONS_MAX_BYTES", 1)
    h.get_calls.clear()

    assert (await h.run_index(**handoff))["ok"] is True

    assert [c["key"] for c in h.get_calls] == [h.source.parsed_object_key]
    assert all(not (_LOC_KEYS & p["payload"].keys()) for p in points)


async def test_oversized_parsed_md_fails_before_download(run_harness, monkeypatch) -> None:
    h = run_harness
    h.parser(lambda data: ParseResult(markdown=MARKDOWN))
    handoff = (await h.run_entry())["handoff"]
    monkeypatch.setattr(stage_artifacts, "PARSED_MAX_BYTES", 10)
    h.get_calls.clear()

    result = await h.run_index(**handoff)

    assert result["ok"] is False
    assert h.get_calls == []
    assert h.committed[-1] == {
        "status": "failed",
        "error_message": "파싱 결과가 너무 커서 처리할 수 없습니다",
        "parsed_object_key": h.source.parsed_object_key,
    }


async def test_missing_parsed_md_fails_the_index_stage(run_harness) -> None:
    h = run_harness
    h.source.status = "chunking"
    h.source.parsed_object_key = f"{h.source.id}/parsed.md"

    def stat(bucket: str, key: str) -> int:
        raise MissingObject()

    h.mp.setattr(ingestion, "stat_object_size", stat)

    result = await h.run_index()

    assert result["ok"] is False
    assert h.committed[-1]["status"] == "failed"
    assert h.committed[-1]["error_message"].startswith("파싱 결과를 찾을 수 없습니다")


# ---------------------------------------------------------------------------
# 점유 — 한 실행만 임베딩한다
# ---------------------------------------------------------------------------


async def test_lost_claim_writes_nothing_and_does_not_embed(run_harness, points) -> None:
    h = run_harness
    h.parser(lambda data: ParseResult(markdown=MARKDOWN))
    handoff = (await h.run_entry())["handoff"]
    committed = len(h.committed)
    h.claim_result = False
    _forbid(h, "embed_dense", "_embed_gateway")

    result = await h.run_index(**handoff)

    assert result == {"ok": False, "error": "claim_lost"}
    assert len(h.committed) == committed and points == []
    assert ("db.update", ("embedding", False)) in h.timeline
    assert h.closed_reindex == []


@pytest.mark.parametrize("status", ["pending", "parsing", "embedding", "ready", "failed"])
async def test_first_attempt_requires_the_handoff_status(run_harness, points, status) -> None:
    """첫 시도는 파싱 단계가 넘긴 `chunking`에서만 시작한다 — 그 밖은 다른 실행의 것이다."""
    h = run_harness
    h.source.status = status
    h.source.parsed_object_key = f"{h.source.id}/parsed.md"
    h.raw = MARKDOWN.encode()

    result = await h.run_index()

    assert result == {"ok": False, "error": "claim_lost"}
    assert h.committed == [] and h.get_calls == [] and points == []


async def test_claim_commit_precedes_embedding(run_harness, monkeypatch) -> None:
    """임베딩(비용) 전에 점유를 커밋한다 — 읽기 트랜잭션도 이 커밋으로 끝난다(#27)."""
    h = run_harness
    h.parser(lambda data: ParseResult(markdown=MARKDOWN))
    order: list[str] = []

    def embed(texts: list[str]) -> list[list[float]]:
        order.append(f"embed after {h.timeline[-1]}")
        return [[0.0] for _ in texts]

    monkeypatch.setattr(ingestion, "embed_dense", embed)

    assert (await h.run())["ok"] is True

    claim = h.timeline.index(("db.update", ("embedding", True)))
    assert h.timeline[claim + 1] == ("db.commit", "embedding")
    assert order == ["embed after ('db.commit', 'embedding')"]


# ---------------------------------------------------------------------------
# 수락 기준 10 — 삭제 정리
# ---------------------------------------------------------------------------


def test_source_cleanup_includes_the_locations_artifact() -> None:
    sid = uuid.uuid4()

    refs = object_cleanup.source_object_refs(sid, None, f"{sid}/parsed.md")

    assert ("parsed", f"{sid}/locations.json") in [(b, k) for b, k in refs]


def test_index_task_is_registered_under_the_contract_name() -> None:
    from biznotes_core.workers import task_names

    assert ingestion.index_source.name == task_names.INDEX_SOURCE
    assert ingestion.ingest_source.name == task_names.INGEST_SOURCE


@pytest.mark.parametrize(
    ("error_message", "expected"),
    [
        pytest.param("임베딩 지연 — 서버 일시 오류, 60초 뒤 …", "chunking", id="old-deferral"),
        pytest.param(None, "pending", id="user-retry"),
    ],
)
async def test_legacy_retry_takes_only_its_own_pending_row(
    run_harness, error_message, expected
) -> None:
    """구 버전의 미룬 재시도 메시지는 자기가 미룬 `pending`만 가져온다.

    구 버전의 미룸은 언제나 `error_message`를 썼다. 사용자 재시도·reaper의 `pending`은 그것을
    비운다 — 그 행은 그쪽 메시지가 처리한다(배포 전환 중 가로채기 방지).
    """
    h = run_harness
    h.source.status = "pending"
    h.source.error_message = error_message
    h.source.parsed_object_key = f"{h.source.id}/parsed.md"

    h.open_reindex = (False, False)
    result = await h.run_entry(reindex=True, refetch=False, transient_attempt=1)

    assert h.source.status == expected
    if expected == "pending":
        assert result == {"ok": False, "error": "retry_superseded"} and h.committed == []
    else:
        assert result["handoff"]["claim_from"] == "chunking"
        assert result["handoff"]["transient_attempt"] == 1


@pytest.mark.parametrize("status", ["ready", "failed"])
async def test_stale_ingest_message_on_a_finished_row_writes_nothing(run_harness, status) -> None:
    """재리뷰 R-M1 — 끝난 행에 온 일반 인제스트 메시지는 낡은 중복이다(reaper 재발행·재배달).

    끝난 HWP를 다시 파싱·임베딩하지 않고, 다시 가져오기가 실패해 좋은 색인을 `failed`로 덮지도
    않는다. 다시 인제스트하려면 사용자 재시도처럼 먼저 `pending`으로 바꾼다.
    """
    h = run_harness
    calls = _hwp_parser(h)
    h.source.status = status

    result = await h.run_entry()

    assert result == {"ok": False, "error": "stale_message"}
    assert calls == [] and h.committed == [] and h.put_calls == []
    assert h.source.status == status


async def test_reindex_handoff_keeps_the_run_alive(run_harness) -> None:
    """재리뷰 R-m1 — `ready`로 넘기는 재인덱싱은 열린 항목의 `updated_at`을 갱신한다.

    넘긴 동안 run이 정체로 보이면 재인덱싱 회수가 다시 내 파싱을 되풀이한다.
    """
    h = run_harness
    _hwp_parser(h)
    h.source.status = "ready"
    h.source.parsed_object_key = f"{h.source.id}/parsed.md"
    h.open_reindex = (True, False)  # 열린 재인덱싱 항목

    await h.run_entry(reindex=True, refetch=False)

    touched = [e for e in h.timeline if e[0] == "db.execute" and "reindex_run_items" in str(e[1])]
    assert touched and str(touched[0][1]).startswith("UPDATE reindex_run_items SET updated_at")


async def test_unreadable_locations_do_not_fail_the_index(run_harness, points) -> None:
    """재리뷰 R-n3 — 위치 파일 읽기 오류(스토리지 순단)는 위치만 버린다. 색인은 ready까지 간다."""
    h = run_harness
    _hwp_parser(h)
    handoff = (await h.run_entry())["handoff"]
    real_stat = ingestion.stat_object_size

    def flaky(bucket: str, key: str) -> int:
        if key.endswith("/locations.json"):
            raise ConnectionError("스토리지 순단")
        return real_stat(bucket, key)

    h.mp.setattr(ingestion, "stat_object_size", flaky)

    assert (await h.run_index(**handoff))["ok"] is True
    assert h.source.status == "ready"
    assert all(not (_LOC_KEYS & p["payload"].keys()) for p in points)


async def test_event_publish_failure_does_not_change_the_flow(run_harness, points) -> None:
    """재리뷰 R-m2 — 커밋 뒤 이벤트 발행 실패는 삼킨다. 점유한 행이 주인 없이 남지 않는다."""
    h = run_harness
    h.parser(lambda data: ParseResult(markdown=MARKDOWN))

    def broken(*a: Any, **k: Any) -> None:
        raise ConnectionError("redis down")

    h.mp.setattr(ingestion, "publish_source_event", broken)

    assert (await h.run())["ok"] is True
    assert h.source.status == "ready"


# ---------------------------------------------------------------------------
# #230 — 재인덱싱 안내는 `warnings` 코드다(`ready` 행의 `error_message`는 비어 있다)
# ---------------------------------------------------------------------------

_KEPT = {"code": "reindex_kept_previous", "params": {"reason": "error"}}
_DEFERRED = {
    "code": "reindex_deferred",
    "params": {"reason": "embedding_transient", "attempt": 1, "max_attempts": 3,
               "retry_in_seconds": 60},
}
_PARSE_WARNING = {"code": "text_truncated", "params": {"omitted": 3}}


def _reindexing(h: RunHarness, warnings: list[dict[str, Any]] | None) -> None:
    h.source.status = "ready"
    h.source.parsed_object_key = f"{h.source.id}/parsed.md"
    h.source.warnings = warnings
    h.raw = MARKDOWN.encode()
    h.open_reindex = (True, False)  # 열린 재인덱싱 항목


async def test_successful_reindex_clears_reindex_notes(run_harness, points) -> None:
    """새 색인이 커밋되면 재인덱싱 안내는 의미가 없다 — 빼고 파싱 경고는 둔다."""
    h = run_harness
    _reindexing(h, [_PARSE_WARNING, _KEPT, _DEFERRED])

    assert (await h.run(reindex=True, refetch=False))["ok"] is True

    assert h.source.status == "ready" and h.source.error_message is None
    assert h.source.warnings == [_PARSE_WARNING]


async def test_claim_drops_the_retry_countdown_note(run_harness, monkeypatch) -> None:
    """재시도가 점유하면 "N초 뒤 다시 시도" 안내를 뺀다 — 임베딩 중에 보이지 않게(리뷰 m2 (b))."""
    h = run_harness
    _reindexing(h, [_PARSE_WARNING, _KEPT, _DEFERRED])
    seen: list[Any] = []

    def embed(texts: list[str]) -> list[list[float]]:
        seen.append(list(h.source.warnings))
        raise RuntimeError("임베딩 실패")

    monkeypatch.setattr(ingestion, "embed_dense", embed)

    await h.run_index(keep_previous=True, claim_from="ready")

    assert seen == [[_PARSE_WARNING, _KEPT]]
    # 실패하면 유지 안내 하나로 바뀐다(옛 유지 안내도 교체한다)
    assert h.source.warnings == [_PARSE_WARNING, _KEPT]
    assert h.source.status == "ready" and h.source.error_message is None


async def test_failure_without_a_usable_model_clears_reindex_notes(run_harness) -> None:
    """이전 색인을 쓸 수 없어 `failed`면 실패 문구는 `error_message`, 재인덱싱 안내는 뺀다."""
    h = run_harness
    _reindexing(h, [_PARSE_WARNING, _DEFERRED])

    async def gone(db: Any, key: str | None) -> Any:
        return None

    h.mp.setattr(ingestion, "resolve_space", gone)

    def boom(text: str) -> int:
        raise RuntimeError("교체 중 실패")

    h.mp.setattr(ingestion, "count_tokens", boom)

    assert (await h.run(reindex=True, refetch=False))["ok"] is False

    assert h.source.status == "failed"
    assert h.source.error_message.startswith("재인덱싱 실패 — 이전 색인의 임베딩 모델을 쓸 수")
    assert h.source.warnings == [_PARSE_WARNING]


async def test_new_source_deferral_keeps_its_note_in_error_message(run_harness) -> None:
    """신규 소스의 미룸은 in-flight 행이라 안내를 `error_message`에 둔다(D5-A)."""
    from biznotes_core.services.embedding_spaces import TransientEmbeddingError

    h = run_harness
    h.parser(lambda data: ParseResult(markdown=MARKDOWN))
    handoff = (await h.run_entry())["handoff"]

    def busy(texts: list[str]) -> list[list[float]]:
        raise TransientEmbeddingError("429 busy")

    h.mp.setattr(ingestion, "embed_dense", busy)

    result = await h.run_index(**handoff)

    assert "retry_in" in result
    assert h.source.status == "pending"
    assert h.source.error_message.startswith("임베딩 지연 — ")
    assert not any(w["code"].startswith("reindex_") for w in h.source.warnings or [])


async def test_backing_off_retry_drops_its_stale_note_only_without_open_items(
    run_harness,
) -> None:
    """리뷰 m2 (c)·재리뷰 R-m3 — 항목이 닫혀 물러나는 미룬 재시도는 자기 안내를 지운다.

    이 실행은 행을 점유하지 않았다. 그래서 같은 문장 안에서 `ready`·읽은 뒤 무변경·열린 항목
    없음을 확인한다 — 열린 항목이 있으면 더 새 run의 안내일 수 있다.
    """
    h = run_harness
    _reindexing(h, [_PARSE_WARNING, _DEFERRED])
    h.source.updated_at = h.row_updated_at
    h.open_reindex = (False, False)  # 그 사이 항목이 닫혔다

    result = await h.run_index(keep_previous=True, claim_from="ready")

    assert result == {"ok": False, "error": "claim_lost"}
    [stmt] = [s for s in h.hb_statements if s.startswith("UPDATE sources SET warnings")]
    assert "sources.status = %(status_1)s" in stmt
    assert "sources.updated_at = %(updated_at_1)s" in stmt
    assert "NOT (EXISTS (SELECT reindex_run_items.id" in stmt



def test_without_codes_keeps_other_warnings_and_odd_shapes() -> None:
    """PR-B 리뷰 B-n2 — 재인덱싱 코드만 빼고, 다른 버전이 쓴 형식은 건드리지 않는다."""
    from biznotes_core.services.ingestion import source_warnings as sw

    items = [_PARSE_WARNING, _KEPT, "not-a-dict", _DEFERRED]
    out = sw.without_codes(items, sw.REINDEX_CODES)

    assert out == [_PARSE_WARNING, "not-a-dict"] and out is not items
    assert sw.without_codes(None, sw.REINDEX_CODES) is None
    odd = {"code": "reindex_deferred"}
    assert sw.without_codes(odd, sw.REINDEX_CODES) is odd
    assert sw.REINDEX_CODES <= sw.WARNING_CODES
    assert not sw.REINDEX_CODES & sw.SECURITY_CODES
