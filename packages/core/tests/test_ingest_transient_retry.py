"""인제스트의 임베딩 일시 오류 미룸·재시도 진입 가드·임베딩 heartbeat (#194·#196·#244).

계획 `docs/plans/embedding-transient-retry/plan.md`의 수락 기준 5·7·8과
`docs/plans/ingest-stage-tasks/plan.md`의 수락 기준 2·5를 `RunHarness`로 확인한다.
미룸은 색인 단계(`_run_index`)가 하고, 재시도는 **색인 단계만** 다시 돈다 — 파서를 부르지 않고
위치는 locations.json에서 다시 읽는다. 재시도는 `h.run_index(**retry_kwargs)`로 재현한다.
미룬 상태(`claim_from`)와 `pending`의 미룸 토큰(`deferred_at`)이 재시도의 점유 조건이다.
`_run`·`_run_index` 본문은 실경로이고, 임베딩 공간(원격 모델)과 DB 세션만 기록형 가짜다.
Celery 태스크 본문(`ingest_source`·`index_source`)은 `test_index_source_task.py`가 본다.
실제 PostgreSQL에서의 상태·재인덱싱 항목은 `tests/test_ingest_transient_retry_pg.py`가 본다.
"""

import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from conftest import RunHarness

from biznotes_core.services.embedding_spaces import TransientEmbeddingError
from biznotes_core.services.ingestion import stage_artifacts
from biznotes_core.services.ingestion.chunker import chunk_markdown
from biznotes_core.services.ingestion.parsers import Location, ParseResult
from biznotes_core.workers.tasks import ingestion

MARKDOWN = "# 제목\n\n" + "본문 문장입니다. " * 200
DIGEST = stage_artifacts.parsed_digest(MARKDOWN.encode())
# HWP처럼 위치가 있는 파싱 결과 — 앞 절반은 1쪽 본문, 뒤 절반은 2쪽의 표
_HALF = len(MARKDOWN) // 2
LOCATIONS = [
    Location(0, _HALF, page=1),
    Location(_HALF, len(MARKDOWN), page=2, table_index=0),
]


class FakeSpace:
    """게이트웨이 공간 대역 — `fail_on`번째 호출부터 일시 오류를 낸다."""

    is_local = False
    key = "gateway:fake"
    display_name = "fake"

    def __init__(
        self,
        fail_on: int | None = None,
        retry_after: float | None = None,
        reason: str = "transient",
    ) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.fail_on = fail_on
        self.retry_after = retry_after
        self.reason = reason

    def embed(self, texts: list[str], **kw: Any) -> list[list[float]]:
        self.calls.append((list(texts), kw))
        if self.fail_on is not None and len(self.calls) >= self.fail_on:
            raise TransientEmbeddingError(
                "429 busy", retry_after=self.retry_after, reason=self.reason
            )
        return [[0.0] for _ in texts]


@pytest.fixture
def h(run_harness: RunHarness) -> RunHarness:
    run_harness.parser(lambda data: ParseResult(markdown=MARKDOWN))
    return run_harness


def _use(h: RunHarness, space: FakeSpace, **rag: Any) -> None:
    async def active_space(db: Any) -> Any:
        return space

    async def get_setting(db: Any, name: str) -> dict[str, Any]:
        return dict(rag)

    from biznotes_core.services import settings_store

    h.mp.setattr(ingestion, "active_space", active_space)
    h.mp.setattr(settings_store, "get_setting", get_setting)


def _count_parses(h: RunHarness, locations: list[Location] | None = None) -> list[bytes]:
    """파서 호출을 센다 — `locations`를 주면 HWP처럼 위치를 함께 낸다."""
    calls: list[bytes] = []

    def parse(data: bytes) -> ParseResult:
        calls.append(data)
        return ParseResult(markdown=MARKDOWN, locations=list(locations or []))

    h.parser(parse)
    return calls


def _deferred(h: RunHarness, status: str = "pending") -> dict[str, Any]:
    """미룬 뒤의 행과 그 재시도 kwargs(차수 제외) — parsed.md는 저장돼 있다.

    신규 소스는 `pending`과 미룰 때의 `updated_at`(토큰), 이전 색인을 유지한 재인덱싱은
    열린 항목과 함께 `ready`다.
    """
    h.source.status = status
    h.source.parsed_object_key = f"{h.source.id}/parsed.md"
    h.objects[h.source.parsed_object_key] = MARKDOWN.encode()
    if status == "ready":
        h.open_reindex = (True, False)
    return {
        "parsed_sha256": DIGEST,
        "keep_previous": status == "ready",
        "claim_from": status,
        "deferred_at": h.row_updated_at.isoformat() if status == "pending" else None,
    }


def _is_embedding_tick(sql: str) -> bool:
    return sql.startswith("UPDATE sources SET updated_at") and sql.endswith("RETURNING sources.id")


def _on_embedding_tick(h: RunHarness, respond: Any) -> None:
    """임베딩 heartbeat에만 `respond`로 답한다 — 파싱 heartbeat는 하네스 기본값 그대로."""
    default = h.hb_response

    def response(n: int) -> list[tuple[Any, ...]]:
        if _is_embedding_tick(h.hb_statements[-1]):
            return respond(n)
        return default(n)

    h.hb_response = response


def _touched_reindex_items(h: RunHarness) -> bool:
    return any(
        kind == "db.execute" and value.startswith("UPDATE reindex_run_items")
        for kind, value in h.timeline
    )


def _updates(h: RunHarness) -> list[tuple[str, bool]]:
    """조건부 상태 전이 — (도착 상태, 맞았는가). 점유는 `embedding`으로의 전이다."""
    return [value for kind, value in h.timeline if kind == "db.update"]


def _claims(h: RunHarness) -> list[bool]:
    return [ok for to, ok in _updates(h) if to == "embedding"]


def _page_and_table(chunks: list[Any]) -> list[tuple[Any, Any]]:
    """색인 교체가 넣은 청크 행의 위치 — 검색 payload는 이 행의 meta다."""
    return [(c.meta.get("page"), c.meta.get("table_index")) for c in chunks]


def _expected_page_and_table(chunk_size: int) -> list[tuple[Any, Any]]:
    """첫 파싱의 위치로 청킹했을 때의 page·table_index."""
    chunks = chunk_markdown(MARKDOWN, max_tokens=chunk_size, overlap=0, locations=LOCATIONS)
    return [(c.meta.get("page"), c.meta.get("table_index")) for c in chunks]


# ── 수락 기준 5: 상한 전에는 실패를 쓰지 않고 미룬다 ─────────────────────


async def test_new_source_defers_with_index_stage_retry_kwargs(h: RunHarness) -> None:
    _use(h, FakeSpace(fail_on=1, retry_after=None))

    result = await h.run()

    assert 48 <= result["retry_in"] <= 72  # 60초 ±20%
    # 재시도는 색인 단계만 다시 낸다 — 파싱 인자(reindex·refetch) 대신 이 세대를 못박고,
    # 미룬 상태와 그때의 updated_at(토큰)에서만 점유한다
    assert result["retry_kwargs"] == {
        "parsed_sha256": DIGEST, "keep_previous": False, "claim_from": "pending",
        "deferred_at": h.row_updated_at.isoformat(),
        "transient_attempt": 1, "gate_attempt": 0,
    }
    assert h.handoffs == [{
        "parsed_sha256": DIGEST, "keep_previous": False, "claim_from": "chunking",
        "transient_attempt": 0, "gate_attempt": 0,
    }]
    # 브로커 메시지는 JSON이다(`task_serializer`) — 토큰도 문자열로 실린다
    assert json.loads(json.dumps(result["retry_kwargs"])) == result["retry_kwargs"]
    assert h.source.parsed_object_key
    assert h.source.status == "pending"
    assert h.source.error_message.startswith("임베딩 지연 — 임베딩 서버 일시 오류, ")
    assert "(1/3)" in h.source.error_message
    assert h.committed[-1]["status"] == "pending"
    assert h.closed_reindex == []  # 재인덱싱 항목은 열어 둔다
    assert _touched_reindex_items(h)
    assert h.indexed == []


async def test_retry_reruns_only_the_index_stage(h: RunHarness) -> None:
    parses = _count_parses(h)
    space = FakeSpace(fail_on=1)
    _use(h, space)
    deferred = await h.run()
    space.fail_on = None
    h.get_calls.clear()

    result = await h.run_index(**deferred["retry_kwargs"])

    assert result["ok"] is True
    assert len(parses) == 1, "재시도가 파서를 다시 불렀다"
    # 원본을 다시 가져오지 않는다 — 저장된 parsed.md만 읽는다
    assert h.source.original_object_key not in [c["key"] for c in h.get_calls]
    # 두 번째 점유는 미룰 때 써 둔 pending에서 — 모든 쓰기가 조건부 전이다
    assert _updates(h) == [
        ("parsing", True), ("chunking", True),
        ("embedding", True), ("pending", True),
        ("embedding", True), ("ready", True),
    ]
    assert h.source.status == "ready" and h.source.error_message is None
    assert h.closed_reindex == [(str(h.source.id), True, None)]
    assert [c.text for c in h.indexed[0]] == space.calls[0][0]


@pytest.mark.parametrize(
    "status,run_kw,deferred_status",
    [
        pytest.param("pending", {}, "pending", id="new-source"),
        # 원본부터 다시 파싱한 재인덱싱 — 이전 색인을 유지한 채 ready로 미룬다
        pytest.param("ready", {"reindex": True, "refetch": True}, "ready", id="refetch-reindex"),
    ],
)
async def test_hwp_like_retry_keeps_locations_without_parsing(
    h: RunHarness, status: str, run_kw: dict[str, Any], deferred_status: str
) -> None:
    """위치가 있는 파싱 결과(HWP)의 재시도 — 파서 0회, 위치는 첫 파싱 그대로(#244 수락 기준 2).

    예전에는 parsed.md만으로 page·table_index를 되살릴 수 없어 원본부터 다시 파싱했다.
    이제는 첫 파싱이 남긴 locations.json을 색인 단계가 다시 읽는다.
    """
    parses = _count_parses(h, LOCATIONS)
    h.source.status = status
    if run_kw:
        h.source.parsed_object_key = f"{h.source.id}/parsed.md"
        h.open_reindex = (True, True)
    space = FakeSpace(fail_on=1)
    _use(h, space, chunk_size=100, chunk_overlap=0, embedding_batch_size=1000)
    deferred = await h.run(**run_kw)
    assert deferred["retry_kwargs"]["parsed_sha256"] == DIGEST
    assert deferred["retry_kwargs"]["claim_from"] == deferred_status
    assert h.source.status == deferred_status
    assert f"{h.source.id}/locations.json" in h.objects
    space.fail_on = None

    result = await h.run_index(**deferred["retry_kwargs"])

    assert result["ok"] is True
    assert len(parses) == 1, "재시도가 파서(rhwp)를 다시 불렀다"
    assert h.source.status == "ready"
    [points] = h.indexed
    assert _page_and_table(points) == _expected_page_and_table(100)
    # 두 쪽과 표가 모두 실린다 — 위치 없이 청킹했다면 전부 (None, None)이다
    assert {page for page, _ in _page_and_table(points)} == {1, 2}
    assert 0 in {table for _, table in _page_and_table(points)}
    # 첫 시도가 임베딩하려던 청크와 같다
    assert [c.text for c in points] == space.calls[0][0]


async def test_reindex_of_ready_source_keeps_previous_index(h: RunHarness) -> None:
    parses = _count_parses(h)
    h.source.status = "ready"
    h.source.parsed_object_key = "parsed/k.md"
    h.raw = MARKDOWN.encode()
    h.open_reindex = (True, False)
    _use(h, FakeSpace(fail_on=1, retry_after=120))

    result = await h.run(reindex=True, refetch=False)

    # 저장된 parsed.md를 그대로 쓰고, 점유 전까지 ready로 둔다 — 첫 쓰기가 점유다
    assert h.handoffs[0]["parsed_sha256"] is None
    assert h.handoffs[0]["claim_from"] == "ready"
    assert parses == []
    assert _updates(h)[0] == ("embedding", True)
    # 재시도는 읽은 parsed.md의 세대로 못박는다. ready 미룸은 토큰 대신 열린 항목을 본다
    assert result["retry_kwargs"] == {
        "parsed_sha256": DIGEST, "keep_previous": True, "claim_from": "ready",
        "deferred_at": None, "transient_attempt": 1, "gate_attempt": 0,
    }
    assert result["retry_in"] == 120  # Retry-After가 백오프(48~72초)보다 크면 그 값을 쓴다
    assert h.source.status == "ready"
    # ready 행의 안내는 경고 코드다 — error_message는 실패 전용이다(#230)
    assert h.source.error_message is None
    assert [w for w in h.source.warnings if w["code"] == "reindex_deferred"] == [
        {
            "code": "reindex_deferred",
            "params": {
                "reason": "embedding_transient",
                "attempt": 1,
                "max_attempts": 3,
                "retry_in_seconds": 120,
            },
        }
    ]
    assert h.closed_reindex == []
    assert _touched_reindex_items(h)


async def test_redeferral_keeps_generation_and_previous_index(h: RunHarness) -> None:
    # 재인덱싱 재시도가 또 미룬다 — 세대·이전 색인 유지가 다음 메시지에도 실린다
    kw = _deferred(h, "ready")
    _use(h, FakeSpace(fail_on=1))

    result = await h.run_index(**kw, transient_attempt=1)

    assert result["retry_kwargs"] == {
        "parsed_sha256": DIGEST, "keep_previous": True, "claim_from": "ready",
        "deferred_at": None, "transient_attempt": 2, "gate_attempt": 0,
    }
    assert h.source.status == "ready"
    assert h.source.error_message is None
    # 다시 미루면 안내를 교체한다 — 하나만 남고 차수가 오른다(#230)
    deferred = [w for w in h.source.warnings if w["code"] == "reindex_deferred"]
    assert [(w["params"]["attempt"], w["params"]["max_attempts"]) for w in deferred] == [(2, 3)]
    assert h.closed_reindex == []


async def test_redeferral_of_new_source_issues_a_fresh_token(h: RunHarness) -> None:
    kw = _deferred(h)
    _use(h, FakeSpace(fail_on=1))

    result = await h.run_index(**kw, transient_attempt=1)

    # 미룰 때마다 updated_at이 바뀐다 — 다음 재시도는 새 토큰으로만 점유한다
    assert result["retry_kwargs"]["deferred_at"] == h.row_updated_at.isoformat()
    assert result["retry_kwargs"]["deferred_at"] != kw["deferred_at"]
    assert result["retry_kwargs"]["transient_attempt"] == 2
    assert h.source.status == "pending"


async def test_countdown_grows_and_is_capped(h: RunHarness) -> None:
    kw = _deferred(h)
    _use(h, FakeSpace(fail_on=1, retry_after=10_000))

    result = await h.run_index(**kw, transient_attempt=2)

    assert result["retry_in"] == 600
    assert "(3/3)" in h.source.error_message


async def test_retry_limit_writes_failure_and_closes_items(h: RunHarness) -> None:
    kw = _deferred(h)
    _use(h, FakeSpace(fail_on=1))

    result = await h.run_index(**kw, transient_attempt=ingestion.INGEST_TRANSIENT_RETRIES)

    assert "retry_in" not in result
    assert _updates(h) == [("embedding", True), ("failed", True)]
    assert h.source.status == "failed"
    assert h.source.error_message.startswith("임베딩 서버 일시 오류 — 재시도 한도 초과: ")
    assert h.closed_reindex == [(str(h.source.id), False, h.source.error_message)]


async def test_retry_limit_on_reindex_keeps_previous_index(h: RunHarness) -> None:
    kw = _deferred(h, "ready")  # 미룬 재인덱싱 — 열린 항목이 있어 점유한다
    _use(h, FakeSpace(fail_on=1))

    await h.run_index(**kw, transient_attempt=ingestion.INGEST_TRANSIENT_RETRIES)

    assert h.source.status == "ready"
    assert h.source.error_message is None
    # 미룸 안내는 빠지고 유지 안내가 남는다 — 상세 오류는 재인덱싱 항목에 있다(#230)
    assert [w["code"] for w in h.source.warnings] == ["reindex_kept_previous"]
    assert h.source.warnings[0]["params"] == {"reason": "embedding_transient"}
    assert h.closed_reindex[0][2].startswith("임베딩 서버 일시 오류 — 재시도 한도 초과")
    assert len(h.closed_reindex) == 1


# ── #196: 동시성 상한 미럼은 네트워크 재시도 예산을 벌지 않는다 ────────────


async def test_gate_deferral_uses_its_own_budget_and_counter(h: RunHarness) -> None:
    _use(h, FakeSpace(fail_on=1, reason="gate"))

    result = await h.run()

    # 네트워크 상한(3)이 아니라 게이트 상한(10)을 분모로 쓴다 — 섞으면 (12/3)처럼 찍힌다
    assert result["retry_kwargs"]["gate_attempt"] == 1
    assert result["retry_kwargs"]["transient_attempt"] == 0
    # 게이트 미룸도 같은 점유 조건(미룬 상태 + 토큰)을 싣는다
    assert result["retry_kwargs"]["claim_from"] == "pending"
    assert result["retry_kwargs"]["deferred_at"] == h.row_updated_at.isoformat()
    assert h.source.status == "pending"
    assert "(1/10)" in h.source.error_message
    assert "동시성 상한 대기" in h.source.error_message


async def test_gate_deferral_ignores_transient_counter(h: RunHarness) -> None:
    # 네트워크 오류를 이미 3회 겪었어도 게이트 미럼은 계속된다 — 상한에 걸린 것을
    # Provider 장애로 세면 그 장애로 생긴 유예까지 소스의 실패로 닫힌다
    kw = _deferred(h)
    _use(h, FakeSpace(fail_on=1, reason="gate"))

    result = await h.run_index(**kw, transient_attempt=ingestion.INGEST_TRANSIENT_RETRIES)

    assert result["retry_kwargs"]["gate_attempt"] == 1
    assert h.source.status == "pending"


async def test_gate_budget_exhaustion_writes_failure(h: RunHarness) -> None:
    # 게이트 예산을 다 쓰면 그때는 닫힌다 — "어떤 값에서도 실패하지 않는다"는 보장이 아니다
    kw = _deferred(h)
    _use(h, FakeSpace(fail_on=1, reason="gate"))

    result = await h.run_index(**kw, gate_attempt=ingestion.INGEST_GATE_DEFERRALS)

    assert "retry_in" not in result
    assert h.source.status == "failed"
    assert "재시도 한도 초과" in h.source.error_message


async def test_transient_ignores_gate_counter(h: RunHarness) -> None:
    kw = _deferred(h)
    _use(h, FakeSpace(fail_on=1))

    result = await h.run_index(**kw, gate_attempt=ingestion.INGEST_GATE_DEFERRALS)

    assert result["retry_kwargs"]["transient_attempt"] == 1
    # 게이트 카운터는 **이겨서 0이 아니라 그대로 실린다** — 0으로 리셋되면 상한이 다시 풀린다
    assert result["retry_kwargs"]["gate_attempt"] == ingestion.INGEST_GATE_DEFERRALS
    assert h.source.status == "pending"


async def test_network_deferral_keeps_the_gate_counter(h: RunHarness) -> None:
    """네트워크 오류 미럼이 게이트 카운터를 지우면 안 된다(#196 리뷰 M1).

    한쪽만 실어 보내면 다른 쪽이 0으로 돌아가 상한이 다시 풀리고, 두 카운터가 각자 자기
    한도까지 버티지 못한다. 매 미룸마다 **둘 다** 실려야 한다.
    """
    kw = _deferred(h)
    _use(h, FakeSpace(fail_on=1))

    result = await h.run_index(**kw, transient_attempt=1, gate_attempt=2)

    assert result["retry_kwargs"]["transient_attempt"] == 2
    assert result["retry_kwargs"]["gate_attempt"] == 2, "게이트 카운터가 리셋됐다"


async def test_gate_deferral_keeps_the_network_counter(h: RunHarness) -> None:
    kw = _deferred(h)
    _use(h, FakeSpace(fail_on=1, reason="gate"))

    result = await h.run_index(**kw, transient_attempt=2, gate_attempt=5)

    assert result["retry_kwargs"]["gate_attempt"] == 6
    assert result["retry_kwargs"]["transient_attempt"] == 2, "네트워크 카운터가 리셋됐다"


async def test_defer_backs_off_when_another_run_holds_the_lock(h: RunHarness) -> None:
    # 다른 실행이 교체 중이면 지금처럼 상태를 쓰지 않는다 — 그 실행이 결과를 쓴다
    h.try_lock_result = False
    _use(h, FakeSpace(fail_on=1))

    result = await h.run()

    assert "retry_in" not in result
    assert h.source.status == "embedding"
    assert not _touched_reindex_items(h)


# ── 수락 기준 7: 재시도 진입 가드(색인 단계 점유) ─────────────────────────


@pytest.mark.parametrize(
    "deferred,now,error",
    [
        ("pending", "pending", None),
        ("ready", "ready", None),
        # 신규 소스 재시도인데 그 사이 색인을 얻어 ready다 — 열린 항목이 있어도 물러난다
        ("pending", "ready", "claim_lost"),
        ("ready", "pending", "claim_lost"),  # 사용자 재시도가 원본부터 처리 중
        ("pending", "failed", "claim_lost"),
        ("ready", "failed", "claim_lost"),
        ("pending", "embedding", "claim_lost"),  # 다른 실행이 처리 중
        ("pending", "chunking", "claim_lost"),  # reaper가 다시 파싱해 넘겼다
    ],
)
async def test_retry_guard(h: RunHarness, deferred: str, now: str, error: str | None) -> None:
    kw = _deferred(h, deferred)
    h.source.status = now  # 미룬 뒤 다른 실행이 행을 바꿨다
    space = FakeSpace()
    _use(h, space)

    result = await h.run_index(**kw, transient_attempt=1)

    if error is None:
        assert result["ok"] is True
        assert h.source.status == "ready"
    else:
        assert result == {"ok": False, "error": error}
        assert h.committed == []
        assert space.calls == [] and h.indexed == []
        assert _updates(h) == []  # 점유 문장도 내지 않는다
        assert not _touched_reindex_items(h) and h.closed_reindex == []


async def test_stale_deferral_token_loses_the_claim(h: RunHarness) -> None:
    # 미룬 뒤 reaper가 pending 행을 되돌려 재발행했다(updated_at 갱신) —
    # 낡은 재시도는 그 행을 가로채지 않는다
    kw = _deferred(h)
    h.row_updated_at += timedelta(minutes=15)
    space = FakeSpace()
    _use(h, space)

    result = await h.run_index(**kw, transient_attempt=1)

    assert result == {"ok": False, "error": "claim_lost"}
    assert _updates(h) == [("embedding", False)]
    assert h.committed == [] and space.calls == [] and h.indexed == []
    assert h.source.status == "pending"


async def test_pending_retry_without_token_is_unclaimable(h: RunHarness) -> None:
    # 토큰이 없는 pending은 사용자 재시도·reaper의 것과 구별할 수 없다 — 점유 문장도 내지 않는다
    kw = {**_deferred(h), "deferred_at": None}
    space = FakeSpace()
    _use(h, space)

    result = await h.run_index(**kw, transient_attempt=1)

    assert result == {"ok": False, "error": "claim_lost"}
    assert _updates(h) == []
    assert h.committed == [] and space.calls == []


@pytest.mark.parametrize("item_open", [True, False])
async def test_ready_deferral_retry_requires_an_open_reindex_item(
    h: RunHarness, item_open: bool
) -> None:
    # 열린 항목은 점유 문장 안에서 본다(EXISTS) — 읽은 뒤 닫혀도 낡은 재시도가 점유하지 않는다
    kw = _deferred(h, "ready")
    h.open_reindex = (item_open, False)
    space = FakeSpace()
    _use(h, space)

    result = await h.run_index(**kw, transient_attempt=1)

    # 점유 문장은 id를, 그 밖의 전이는 updated_at을 돌려준다
    [claim_sql] = [
        sql for kind, sql in h.timeline
        if kind == "db.execute" and sql.startswith("UPDATE sources SET status")
        and sql.endswith("RETURNING sources.id")
    ]
    assert "EXISTS" in claim_sql and "reindex_run_items" in claim_sql
    assert _claims(h) == [item_open]
    if item_open:
        assert result["ok"] is True and h.source.status == "ready"
    else:
        assert result == {"ok": False, "error": "claim_lost"}
        assert h.committed == [] and space.calls == []
        assert h.source.status == "ready"  # 이전 색인 그대로


async def test_retry_claim_race_writes_nothing(h: RunHarness) -> None:
    # 가드는 통과했는데 점유 직전에 다른 실행(reaper 재발행 등)이 행을 가져갔다
    kw = _deferred(h)
    h.claim_result = False
    space = FakeSpace()
    _use(h, space)

    result = await h.run_index(**kw, transient_attempt=1)

    assert result == {"ok": False, "error": "claim_lost"}
    assert _updates(h) == [("embedding", False)]
    assert h.committed == [] and space.calls == []
    assert h.source.status == "pending"


async def test_retry_indexes_the_current_parsed_md(h: RunHarness) -> None:
    # 점유가 파싱을 막으므로 점유 뒤 읽은 parsed.md가 정본이다 —
    # 메시지 해시가 달라도 그것을 색인한다
    kw = _deferred(h)
    current = "# 새 판\n\n바뀐 본문"
    h.objects[h.source.parsed_object_key] = current.encode()
    _use(h, FakeSpace())

    result = await h.run_index(**kw, transient_attempt=1)

    assert result["ok"] is True
    assert [c.text for c in h.indexed[0]] == [
        c.text for c in chunk_markdown(current, max_tokens=800, overlap=100)
    ]


# ── 점유 전후 실패: 점유 전에는 쓰지 않고, 점유 뒤에는 embedding에서만 쓴다 ──


@pytest.mark.parametrize("broken", ["get_setting", "active_space"])
async def test_pre_claim_read_failure_writes_nothing(h: RunHarness, broken: str) -> None:
    kw = _deferred(h)
    _use(h, FakeSpace())

    async def fail(*a: Any, **k: Any) -> Any:
        raise RuntimeError("db down")

    from biznotes_core.services import settings_store

    h.mp.setattr(settings_store if broken == "get_setting" else ingestion, broken, fail)
    before = h.row_updated_at

    result = await h.run_index(**kw, transient_attempt=1)

    assert result == {"ok": False, "error": "db down"}
    assert _updates(h) == [] and h.committed == []
    # 행과 토큰이 그대로다 — 다음 회수가 같은 조건으로 점유할 수 있다
    assert h.source.status == "pending" and h.row_updated_at == before


@pytest.mark.parametrize("taken", [False, True])
async def test_post_claim_failure_writes_only_from_embedding(h: RunHarness, taken: bool) -> None:
    kw = _deferred(h)
    space = FakeSpace()

    def embed(texts: list[str], **kw: Any) -> list[list[float]]:
        if taken:
            h.source.status = "chunking"  # reaper가 회수해 색인 단계부터 재발행했다
        raise RuntimeError("boom")

    space.embed = embed  # type: ignore[method-assign]
    _use(h, space)

    result = await h.run_index(**kw, transient_attempt=1)

    assert result == {"ok": False, "error": "boom"}
    assert _updates(h) == [("embedding", True), ("failed", not taken)]
    if taken:
        # 남의 행에 실패를 쓰지 않고 항목도 닫지 않는다 — 가져간 실행이 끝낸다
        assert h.committed[-1]["status"] == "embedding"
        assert h.source.status == "chunking" and h.closed_reindex == []
    else:
        assert h.committed[-1] == {
            "status": "failed",
            "error_message": "내부 오류: boom",
            "parsed_object_key": h.source.parsed_object_key,
        }
        assert h.closed_reindex == [(str(h.source.id), False, "내부 오류: boom")]


# ── #244 수락 기준 5: 구 버전이 미룬 `ingest_source` 재시도 메시지 ───────────


@pytest.mark.parametrize(
    "status,open_reindex,claim_from",
    [
        # 구 버전이 미룬 신규 소스 — 토큰이 없어 행을 chunking으로 가져와 넘긴다
        ("pending", (False, False), "chunking"),
        # 이전 색인을 유지한 재인덱싱 — 상태를 쓰지 않고 넘긴다
        ("ready", (True, False), "ready"),
        ("ready", (False, False), None),  # 그 사이 항목이 닫혔다
        ("failed", (False, False), None),
        ("embedding", (False, False), None),  # 다른 실행이 처리 중
    ],
)
async def test_legacy_retry_message_guard_and_handoff(
    h: RunHarness, status: str, open_reindex: tuple[bool, bool], claim_from: str | None
) -> None:
    """구 재시도 kwargs(`reindex`·`refetch`·카운터)는 진입 단계가 `_retry_still_wanted`로 본다."""
    parses = _count_parses(h)
    h.source.status = status
    # 구 버전의 미룸은 언제나 안내를 남겼다 — 진입 단계는 이것으로 자기가 미룬 `pending`을 가린다
    h.source.error_message = "임베딩 지연 — 임베딩 서버 일시 오류, 60초 뒤 다시 시도합니다(1/3)"
    h.source.parsed_object_key = "parsed/k.md"
    h.raw = MARKDOWN.encode()
    h.open_reindex = open_reindex

    result = await h.run_entry(reindex=True, refetch=False, transient_attempt=1, gate_attempt=2)

    if claim_from is None:
        assert result == {"ok": False, "error": "retry_superseded"}
        assert h.committed == [] and _updates(h) == []
        return
    # 파싱 없이 색인 단계로 — 구 메시지의 카운터를 그대로 넘긴다
    assert result["handoff"] == {
        "parsed_sha256": None, "keep_previous": status == "ready", "claim_from": claim_from,
        "transient_attempt": 1, "gate_attempt": 2,
    }
    assert parses == []
    assert h.source.status == claim_from
    assert _updates(h) == ([] if status == "ready" else [("chunking", True)])


@pytest.mark.parametrize(
    "run_kw,parsed",
    [
        # 구 버전: 위치가 없는 신규 소스는 저장된 parsed.md에서 이어 갔다
        pytest.param({"reindex": True, "refetch": False}, 0, id="from-parsed"),
        # 구 버전: 위치가 있는 신규 소스는 다시 파싱했다 — 이번 한 번은 파서가 돈다
        pytest.param({"reindex": False, "refetch": False}, 1, id="reparse"),
        # 구 버전: parsed.md 없이 원본부터 파싱한 재인덱싱(실패 소스)
        pytest.param({"reindex": True, "refetch": True}, 1, id="refetch"),
    ],
)
async def test_legacy_new_source_retry_message_reaches_ready(
    h: RunHarness, run_kw: dict[str, Any], parsed: int
) -> None:
    """구 버전이 신규 소스를 `pending`으로 미룬 메시지(#244 수락 기준 5, 계획 §3.8).

    `_retry_still_wanted`를 통과하면 kwargs의 모드대로 처리하고, 이어 받은 카운터로 색인
    단계가 `chunking`에서 점유해 ready까지 간다.
    """
    _deferred(h)
    # 구 버전의 미룸 안내(`_defer_transient`) — 사용자 재시도의 `pending`과 가르는 신호다
    h.source.error_message = "임베딩 지연 — 임베딩 서버 일시 오류, 60초 뒤 다시 시도합니다(1/3)"
    parses = _count_parses(h)
    _use(h, FakeSpace())

    result = await h.run(**run_kw, transient_attempt=1, gate_attempt=4)

    assert result["ok"] is True
    assert h.source.status == "ready"
    assert len(parses) == parsed
    [handoff] = h.handoffs
    assert handoff["claim_from"] == "chunking"
    assert (handoff["transient_attempt"], handoff["gate_attempt"]) == (1, 4)
    assert _claims(h) == [True]


async def test_legacy_reindex_retry_message_reaches_ready(h: RunHarness) -> None:
    # 구 버전이 이전 색인을 유지한 채 미룬 재인덱싱 — 열린 항목이 있어 ready에서 점유한다
    _deferred(h, "ready")
    parses = _count_parses(h)
    _use(h, FakeSpace())

    result = await h.run(reindex=True, refetch=False, transient_attempt=1)

    assert result["ok"] is True
    assert parses == []
    assert _updates(h) == [("embedding", True), ("ready", True)]
    assert h.closed_reindex == [(str(h.source.id), True, None)]


async def test_legacy_reparse_retry_forwards_counters(h: RunHarness) -> None:
    # 구 버전이 위치 때문에 정한 재파싱(`reindex=False`) — 위치 파일도 새로 남긴다
    parses = _count_parses(h, LOCATIONS)
    h.source.parsed_object_key = f"{h.source.id}/parsed.md"

    result = await h.run_entry(reindex=False, refetch=False, transient_attempt=2)

    assert len(parses) == 1
    assert result["handoff"] == {
        "parsed_sha256": DIGEST, "keep_previous": False, "claim_from": "chunking",
        "transient_attempt": 2, "gate_attempt": 0,
    }
    assert f"{h.source.id}/locations.json" in h.objects


async def test_legacy_new_source_retry_does_not_touch_ready_source(h: RunHarness) -> None:
    # 신규 소스 재시도(reindex=False)인데 그 사이 색인을 얻어 ready다 — 열린 항목이 있어도 물러난다
    parses = _count_parses(h)
    h.source.status = "ready"
    h.open_reindex = (True, False)

    result = await h.run_entry(reindex=False, refetch=False, transient_attempt=1)

    assert result == {"ok": False, "error": "retry_superseded"}
    assert h.committed == [] and parses == []


# ── 수락 기준 8: 배치 크기와 임베딩 heartbeat ────────────────────────────


async def test_batches_follow_settings_and_heartbeat_between_batches(h: RunHarness) -> None:
    space = FakeSpace()
    _use(h, space, chunk_size=100, chunk_overlap=0, embedding_batch_size=3,
         embedding_timeout_seconds=30)

    result = await h.run()

    assert result["ok"] is True
    sizes = [len(texts) for texts, _ in space.calls]
    assert len(sizes) >= 2 and all(n == 3 for n in sizes[:-1]) and sizes[-1] <= 3
    assert sum(sizes) == result["chunks"]
    # 모든 배치가 한 시도의 대기 예산을 나눠 쓴다
    budgets = {id(kw["retry_budget"]) for _, kw in space.calls}
    assert len(budgets) == 1
    assert all(kw["timeout"] == 30.0 and kw["batch_size"] == 3 for _, kw in space.calls)
    # 배치 사이마다 별도 세션으로 heartbeat 한 번
    ticks = [s for s in h.hb_statements if _is_embedding_tick(s)]
    assert len(ticks) == len(sizes) - 1


async def test_heartbeat_zero_rows_backs_off_without_writes(h: RunHarness) -> None:
    _on_embedding_tick(h, lambda n: [])  # 다른 실행이 행을 가져갔다
    space = FakeSpace()
    _use(h, space, chunk_size=100, chunk_overlap=0, embedding_batch_size=1)

    result = await h.run()

    assert result == {"ok": False, "error": "embedding_superseded"}
    assert len(space.calls) == 1
    assert h.committed[-1]["status"] == "embedding"  # 그 뒤로 상태를 쓰지 않았다
    assert h.closed_reindex == []
    assert h.indexed == []


async def test_heartbeat_db_error_does_not_stop_embedding(h: RunHarness) -> None:
    def broken(n: int) -> list[tuple[Any, ...]]:
        raise RuntimeError("db down")

    _on_embedding_tick(h, broken)
    space = FakeSpace()
    _use(h, space, chunk_size=100, chunk_overlap=0, embedding_batch_size=1)

    result = await h.run()

    assert result["ok"] is True
    assert h.source.status == "ready"


async def test_local_space_keeps_single_embed_dense_call(h: RunHarness) -> None:
    # 로컬 공간은 제품 대역(`lambda texts`)과 같은 호출 형태를 유지한다
    calls: list[int] = []
    def embed_dense(texts: list[str]) -> list[list[float]]:
        calls.append(len(texts))
        return [[0.0] for _ in texts]

    h.mp.setattr(ingestion, "embed_dense", embed_dense)

    result = await h.run()

    assert result["ok"] is True
    assert calls == [result["chunks"]]
    assert not any(_is_embedding_tick(s) for s in h.hb_statements)


# ── 재리뷰 경계 케이스 ────────────────────────────────────────────────────


async def test_reindex_without_parsed_md_keeps_locations_on_retry(h: RunHarness) -> None:
    # parsed.md가 없어 원본부터 파싱한 재인덱싱(실패 소스 등) — 재시도는 위치 파일을 다시 읽는다
    h.source.status = "failed"
    h.source.parsed_object_key = None
    h.open_reindex = (True, False)
    parses = _count_parses(h, LOCATIONS)
    space = FakeSpace(fail_on=1)
    _use(h, space, chunk_size=100, chunk_overlap=0)

    deferred = await h.run(reindex=True, refetch=False)

    # failed 소스는 유지할 이전 색인이 없다 — 신규 소스처럼 pending으로 미룬다
    assert deferred["retry_kwargs"] == {
        "parsed_sha256": DIGEST, "keep_previous": False, "claim_from": "pending",
        "deferred_at": h.row_updated_at.isoformat(),
        "transient_attempt": 1, "gate_attempt": 0,
    }
    assert h.source.status == "pending"
    space.fail_on = None

    result = await h.run_index(**deferred["retry_kwargs"])

    assert result["ok"] is True
    assert len(parses) == 1
    assert _page_and_table(h.indexed[0]) == _expected_page_and_table(100)


async def test_transient_after_row_taken_over_neither_writes_nor_defers(h: RunHarness) -> None:
    space = FakeSpace(fail_on=1)
    original = space.embed

    def embed(texts: list[str], **kw: Any) -> list[list[float]]:
        h.source.status = "pending"  # reaper가 회수해 재발행했다
        return original(texts, **kw)

    space.embed = embed  # type: ignore[method-assign]
    _use(h, space)
    # 실제 세션처럼 롤백이 행 객체를 만료시킨다 — 그 뒤에 읽으면 실패한다(재리뷰 R2-m1)
    h.expire_on_rollback = True

    result = await h.run()

    assert "retry_in" not in result
    assert h.rolled_back_state is not None and h.rolled_back_state["status"] == "pending"
    assert h.committed[-1]["status"] == "embedding"  # 이 실행은 그 뒤로 쓰지 않았다
    assert not _touched_reindex_items(h)
    assert h.closed_reindex == []


async def test_transient_after_another_run_swapped_restores_ready(h: RunHarness) -> None:
    # 시작 뒤 다른 실행이 교체를 커밋했다(#50) — 미루지 않고 그 색인을 정본으로 둔다
    space = FakeSpace(fail_on=1)
    original = space.embed

    def embed(texts: list[str], **kw: Any) -> list[list[float]]:
        h.chunk_ids = [uuid.uuid4()]
        return original(texts, **kw)

    space.embed = embed  # type: ignore[method-assign]
    _use(h, space)

    result = await h.run()

    assert "retry_in" not in result
    assert h.committed[-1]["status"] == "ready"
    assert not _touched_reindex_items(h)
