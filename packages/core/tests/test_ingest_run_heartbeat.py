"""워커 `_run` 배선(§5) — parsing heartbeat 래핑·to_thread 4곳·소유권 상실·진입 실패.

`_run` 본문과 `parsing_heartbeat`는 실경로다. DB 세션·스토리지·임베딩·벡터스토어·파서만 스텁이다.
"""

import inspect
import re
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest
from conftest import RunHarness

from biznotes_core.services.ingestion.abort import PARSE_ABORT
from biznotes_core.services.ingestion.parsers import ParseError, ParseResult
from biznotes_core.workers import parsing_heartbeat as hb
from biznotes_core.workers.tasks import ingestion


def _hb_updates(h: RunHarness) -> int:
    return sum(1 for s in h.hb_statements if s.startswith("UPDATE sources SET updated_at=now()"))


async def test_heartbeat_ticks_during_parsing_and_stops_before_chunking(run_harness) -> None:
    h = run_harness

    def parser(data: bytes) -> ParseResult:
        time.sleep(0.1)
        return ParseResult(markdown="# 제목\n\n본문 " * 20)

    h.parser(parser)
    result = await h.run()

    assert result["ok"] is True
    assert _hb_updates(h) >= 3  # 진입 틱 + 주기 틱 ≥ 2
    chunking = h.timeline.index(("db.commit", "chunking"))
    assert all(e[0] != "hb.tick" for e in h.timeline[chunking:])
    # embedding은 색인 단계 점유 커밋 하나다 — 임베딩 전에 읽기 트랜잭션도 끝낸다(#27·#244)
    assert [c["status"] for c in h.committed] == ["parsing", "chunking", "embedding", "ready"]
    assert h.committed[1]["parsed_object_key"] == f"{h.source.id}/parsed.md"
    # 구간 안에서 db가 sources를 쓰지 않는다(자기 heartbeat를 상실로 오판하지 않도록)
    assert h.db_writes_during_parsing() == []


async def test_parser_error_fails_and_stops_heartbeat(run_harness) -> None:
    h = run_harness

    def parser(data: bytes) -> ParseResult:
        time.sleep(0.05)
        raise ParseError("손상된 문서")

    h.parser(parser)
    result = await h.run()

    assert result == {"ok": False, "error": "손상된 문서"}
    assert h.committed[-1] == {
        "status": "failed",
        "error_message": "손상된 문서",
        "parsed_object_key": None,
    }
    count = len(h.hb_statements)
    await anyio.sleep(0.1)
    assert len(h.hb_statements) == count
    assert h.put_calls == []


async def test_sync_storage_calls_run_off_the_event_loop(run_harness) -> None:
    h = run_harness
    h.get_object_delay = 0.2  # 동기 sleep — 루프에서 돌았다면 그동안 틱이 0회
    h.parser(lambda data: ParseResult(markdown="본문"))

    await h.run()

    get = h.get_calls[0]
    ticks_during_get = [t for t in h.tick_times if get["start"] < t < get["end"]]
    assert len(ticks_during_get) >= 2
    assert h.get_calls[0]["thread"] != h.loop_thread
    assert h.put_calls[0]["thread"] != h.loop_thread


async def test_ownership_loss_returns_without_writing(run_harness, monkeypatch) -> None:
    h = run_harness
    handles: list[Any] = []
    real = ingestion.parsing_heartbeat

    @asynccontextmanager
    async def recording(*a: Any, **k: Any):
        async with real(*a, **k) as handle:
            handles.append(handle)
            yield handle

    monkeypatch.setattr(ingestion, "parsing_heartbeat", recording)
    h.hb_response = lambda n: [] if n == 2 else [(h.now,)]  # 첫 주기 틱이 0행
    seen: dict[str, Any] = {}

    finished = threading.Event()

    def parser(data: bytes) -> ParseResult:
        seen["thread"] = threading.get_ident()
        seen["abort"] = PARSE_ABORT.get()
        time.sleep(0.5)  # abort를 보지 않는 파서 — 끝날 때까지 기다려야 한다
        finished.set()
        return ParseResult(markdown="본문")

    h.parser(parser)
    started = time.monotonic()
    result = await h.run()

    # anyio 작업 스레드는 풀에서 재사용돼 유휴 상태로 살아 있으므로 스레드 객체가 아니라
    # "파서 호출이 반환 전에 끝났는가"를 본다(abandon하면 여기서 아직 돌고 있다)
    assert finished.is_set()
    assert result == {"ok": False, "error": "ownership_lost"}
    assert time.monotonic() - started >= 0.45
    assert seen["thread"] != threading.get_ident()
    assert seen["abort"] is handles[0].abort and seen["abort"].is_set()
    assert [c["status"] for c in h.committed] == ["parsing"]  # failed 전이 없음
    assert h.source.error_message is None
    assert h.put_calls == []


async def test_heartbeat_start_failure_skips_parsing(run_harness, monkeypatch) -> None:
    h = run_harness
    calls: list[bytes] = []

    @asynccontextmanager
    async def failing(factory: Any, source_id: Any, **k: Any):
        raise hb.HeartbeatStartFailed(source_id)
        yield  # pragma: no cover

    monkeypatch.setattr(ingestion, "parsing_heartbeat", failing)
    h.parser(lambda data: calls.append(data) or ParseResult(markdown="x"))

    result = await h.run()

    assert result == {"ok": False, "error": "heartbeat_start_failed"}
    assert calls == []
    assert [c["status"] for c in h.committed] == ["parsing"]
    assert h.put_calls == [] and h.get_calls == []


async def test_entry_tick_failures_end_to_end(run_harness, monkeypatch) -> None:
    """실제 heartbeat의 진입 틱이 세 번 실패하면 파서를 부르지 않는다."""
    h = run_harness
    monkeypatch.setattr(hb, "_ENTRY_RETRY_DELAY_SEC", 0.01)
    calls: list[bytes] = []
    h.parser(lambda data: calls.append(data) or ParseResult(markdown="x"))

    def broken(n: int) -> list[Any]:
        raise ConnectionError("db down")

    h.hb_response = broken
    result = await h.run()
    assert result == {"ok": False, "error": "heartbeat_start_failed"}
    assert calls == [] and len(h.hb_statements) == 3


def test_to_thread_calls_do_not_abandon_threads() -> None:
    source = inspect.getsource(ingestion)
    assert "abandon_on_cancel" not in source
    assert source.count("to_thread.run_sync") >= 5
    assert source.count("PARSE_ABORT") >= 2
    assert not re.search(
        r"^\s+(raw = )?(get_object|put_object|parse_file|_parse)\(", source, re.M
    )


@pytest.mark.parametrize("status", ["chunking", "embedding"])
async def test_no_heartbeat_after_parsing(run_harness, status) -> None:
    h = run_harness
    h.parser(lambda data: ParseResult(markdown="본문"))
    await h.run()
    index = h.timeline.index(("db.commit", status))
    assert not any(e[0] == "hb.tick" for e in h.timeline[index:])


async def test_reindex_from_parsed_skips_the_parsing_window(run_harness) -> None:
    """저장된 parsed.md로 재인덱싱하면 parsing 구간 자체가 없다 — heartbeat도 없다.

    진입 단계는 상태를 쓰지 않고(#244 — 점유 전까지 ready로 검색된다) 색인 단계가 `ready`에서
    점유한다. 그래서 이 경로의 `sources` 쓰기는 남의 parsing 구간을 침범하지 않는다. 한 소스를
    두 태스크가 동시에 처리하는 경우는 색인 단계 점유가 하나만 남긴다.
    """
    h = run_harness
    h.source.status = "ready"
    h.source.parsed_object_key = f"{h.source.id}/parsed.md"
    h.raw = "# 저장된 본문\n\n본문".encode()

    h.open_reindex = (True, False)  # 열린 재인덱싱 항목
    result = await h.run(reindex=True, refetch=False)

    assert result["ok"] is True
    assert h.hb_statements == []  # 진입 틱조차 없다
    # 진입 단계의 커밋은 읽기 트랜잭션을 끝낼 뿐이다(상태 ready 그대로)
    assert [c["status"] for c in h.committed] == ["ready", "embedding", "ready"]
    assert h.handoffs[0]["claim_from"] == "ready"
    # 위치 파일(locations.json)은 없다 — 크기 조회에서 "없음"이라 받지 않는다
    assert [c["key"] for c in h.get_calls] == [h.source.parsed_object_key]
    assert h.put_calls == []  # parsed.md를 다시 쓰지 않는다


async def test_reindex_with_refetch_keeps_the_parsing_window(run_harness) -> None:
    h = run_harness
    h.source.parsed_object_key = f"{h.source.id}/parsed.md"
    h.parser(lambda data: ParseResult(markdown="다시 파싱한 본문"))

    h.open_reindex = (True, True)  # 열린 재인덱싱 항목
    result = await h.run(reindex=True, refetch=True)

    assert result["ok"] is True
    assert _hb_updates(h) >= 1
    assert [c["status"] for c in h.committed][:2] == ["parsing", "chunking"]
    assert h.db_writes_during_parsing() == []
