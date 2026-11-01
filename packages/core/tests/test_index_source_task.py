"""Celery 태스크 본문 — `ingest_source`의 색인 단계 배치와 `index_source`의 재시도 (#194·#196·#244).

`_run`·`_run_index`는 대역이다. 단계 본문은 `test_ingest_transient_retry.py`와
`test_ingest_stage_tasks.py`가 본다. 여기서는 브로커 메시지의 모양(args·kwargs·큐)을 고정한다.
발행은 `celery_app.send_task`만 기록형으로 바꾼다 — `apply_async`의 인자 검사(`typing`)와
라우터(`task_routes`)는 실경로다.
"""

import asyncio
import inspect
import uuid
from datetime import datetime
from typing import Any

import pytest
from celery.exceptions import MaxRetriesExceededError, Retry

from biznotes_core.workers import task_names
from biznotes_core.workers.celery_app import celery_app
from biznotes_core.workers.tasks import ingestion

DIGEST = "0" * 64
DEFERRED_AT = "2026-10-16T00:00:01+00:00"
# 진입 단계의 핸드오프 — 미룸 토큰은 싣지 않는다
HANDOFF = {
    "parsed_sha256": DIGEST,
    "keep_previous": False,
    "claim_from": "chunking",
    "transient_attempt": 0,
    "gate_attempt": 0,
}
# 색인 단계의 미룸 — 미룬 상태와 그때의 updated_at(ISO 문자열)을 싣는다
RETRY_KWARGS = {
    "parsed_sha256": DIGEST,
    "keep_previous": False,
    "claim_from": "pending",
    "deferred_at": DEFERRED_AT,
    "transient_attempt": 1,
    "gate_attempt": 0,
}
# `_index_kwargs_of`가 정규화한 값 — 토큰은 datetime, 빠진 키는 기본값
DEFAULTS = {
    "parsed_sha256": None,
    "keep_previous": False,
    "claim_from": "chunking",
    "deferred_at": None,
    "transient_attempt": 0,
    "gate_attempt": 0,
}
HANDOFF_NORMALIZED = {**DEFAULTS, **HANDOFF}
RETRY_NORMALIZED = {**RETRY_KWARGS, "deferred_at": datetime.fromisoformat(DEFERRED_AT)}
# 재시도 상한은 두 사유 예산의 합이다(#196) — 한 실행은 사유를 정확히 하나 만든다
MAX_RETRIES = ingestion.INGEST_TRANSIENT_RETRIES + ingestion.INGEST_GATE_DEFERRALS
DEFERRED = {"ok": False, "error": "busy", "retry_in": 61, "retry_kwargs": RETRY_KWARGS}


class _Retry(Exception):
    pass


@pytest.fixture
def loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """태스크 본문이 부르는 버킷 확인과 프로세스 공용 루프를 테스트용으로 바꾼다."""
    monkeypatch.setattr(ingestion, "ensure_buckets", lambda: None)
    monkeypatch.setattr(ingestion, "run_async", lambda coro: asyncio.run(coro))


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """브로커 발행 기록 — 실제 `apply_async`가 `send_task`에 넘긴 이름·인자·옵션."""
    out: list[dict[str, Any]] = []

    def send_task(name: str, args: Any = None, kwargs: Any = None, **options: Any) -> None:
        out.append({"name": name, "args": args, "kwargs": kwargs, "options": options})

    monkeypatch.setattr(celery_app, "send_task", send_task)
    return out


def _broken_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    def send_task(*a: Any, **k: Any) -> None:
        raise ConnectionError("broker down")

    monkeypatch.setattr(celery_app, "send_task", send_task)


def _routed_queue(message: dict[str, Any]) -> str:
    """Celery `send_task`가 하듯 실제 라우터로 목적지 큐를 정한다."""
    options = dict(message["options"])
    task_type = options.pop("task_type", None)
    route = celery_app.amqp.router.route(
        options, message["name"], message["args"], message["kwargs"], task_type
    )
    return route["queue"].name


def _assert_routed_to_ingestion(message: dict[str, Any]) -> None:
    # 목적지를 지정하지 않는다 — 라우터가 `ingestion`으로 정한다
    for name in ("queue", "routing_key", "exchange"):
        assert message["options"].get(name) is None, f"{name}을 지정해 발행했다"
    assert _routed_queue(message) == "ingestion"


def _entry(monkeypatch: pytest.MonkeyPatch, result: Any) -> list[dict[str, Any]]:
    """`_run` 대역 — 받은 인자를 기록하고 `result`의 사본을 돌려준다."""
    seen: list[dict[str, Any]] = []

    async def fake_run(source_id: str, **kw: Any) -> Any:
        seen.append({"source_id": source_id, **kw})
        if not isinstance(result, dict):
            return result
        return {k: dict(v) if isinstance(v, dict) else v for k, v in result.items()}

    monkeypatch.setattr(ingestion, "_run", fake_run)
    return seen


def _index(monkeypatch: pytest.MonkeyPatch, *results: dict[str, Any]) -> list[dict[str, Any]]:
    """`_run_index` 대역 — 받은 인자를 기록하고 `results`를 차례로(마지막은 반복) 돌려준다."""
    seen: list[dict[str, Any]] = []

    async def fake_run_index(source_id: str, **kw: Any) -> dict[str, Any]:
        seen.append({"source_id": source_id, **kw})
        out = results[min(len(seen), len(results)) - 1] if results else {"ok": True}
        return {k: dict(v) if isinstance(v, dict) else v for k, v in out.items()}

    monkeypatch.setattr(ingestion, "_run_index", fake_run_index)
    return seen


def _forbid_index(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_index(source_id: str, **kw: Any) -> dict[str, Any]:
        raise AssertionError("핸드오프 없이 색인 단계를 돌렸다")

    monkeypatch.setattr(ingestion, "_run_index", fake_run_index)


def _forbid_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    def retry(**kw: Any) -> Exception:
        raise AssertionError(f"진입 태스크가 재시도를 걸었다: {kw}")

    monkeypatch.setattr(ingestion.ingest_source, "retry", retry)


# ── 라우팅: 색인 태스크는 일반 인제스트 큐로 간다(#244 수락 기준 1) ─────────


def test_index_task_routes_to_the_ingestion_queue() -> None:
    # 큐 인자 없이 내면 `task_routes`가 정한다
    route = celery_app.amqp.router.route({}, task_names.INDEX_SOURCE)

    assert route["queue"].name == "ingestion"


# ── ingest_source: 같은 태스크에서 색인한다(D6) ───────────────


def test_general_worker_indexes_inline(
    monkeypatch: pytest.MonkeyPatch,
    loop: None,
    sent: list[dict[str, Any]],
) -> None:
    _entry(monkeypatch, {"ok": True, "stage": "parsed", "handoff": HANDOFF})
    seen = _index(monkeypatch, {"ok": True, "chunks": 3})
    _forbid_retry(monkeypatch)
    sid = str(uuid.uuid4())

    result = ingestion.ingest_source.run(sid)

    assert result == {"ok": True, "chunks": 3}  # 색인 단계의 결과다
    # 핸드오프도 정규화를 거친다
    assert seen == [{"source_id": sid, **HANDOFF_NORMALIZED}]
    assert sent == []


def test_general_worker_publishes_deferred_index_task_with_countdown(
    monkeypatch: pytest.MonkeyPatch, loop: None, sent: list[dict[str, Any]]
) -> None:
    _entry(monkeypatch, {"ok": True, "stage": "parsed", "handoff": HANDOFF})
    _index(monkeypatch, DEFERRED)
    _forbid_retry(monkeypatch)  # 진입 태스크를 다시 내지 않는다 — 파싱을 되풀이하지 않는다
    sid = str(uuid.uuid4())

    result = ingestion.ingest_source.run(sid)

    assert result == {"ok": False, "error": "busy", "retry_in": 61}
    [message] = sent
    assert message["name"] == task_names.INDEX_SOURCE
    assert message["args"] == [sid]
    assert message["kwargs"] == RETRY_KWARGS
    assert message["options"]["countdown"] == 61
    _assert_routed_to_ingestion(message)


def test_general_worker_swallows_deferred_publish_errors(
    monkeypatch: pytest.MonkeyPatch, loop: None
) -> None:
    # 되돌린 행(pending·재인덱싱 ready)은 reaper·재인덱싱 회수가 가져간다
    _entry(monkeypatch, {"ok": True, "stage": "parsed", "handoff": HANDOFF})
    _index(monkeypatch, DEFERRED)
    _forbid_retry(monkeypatch)
    _broken_broker(monkeypatch)

    result = ingestion.ingest_source.run(str(uuid.uuid4()))

    assert result == {"ok": False, "error": "busy", "retry_in": 61}


@pytest.mark.parametrize(
    "result",
    [
        {"ok": False, "error": "retry_superseded"},
        {"ok": False, "error": "in_flight"},
        {"ok": False, "error": "already_parsed"},
        {"ok": False, "error": "손상된 문서"},
        {"ok": True},  # 제품의 `_run` 대역 — 핸드오프 없이 끝난다
    ],
)
def test_ingest_source_does_nothing_more_without_handoff(
    monkeypatch: pytest.MonkeyPatch,
    loop: None,
    sent: list[dict[str, Any]],
    result: dict[str, Any],
) -> None:
    _entry(monkeypatch, result)
    _forbid_index(monkeypatch)
    _forbid_retry(monkeypatch)

    assert ingestion.ingest_source.run(str(uuid.uuid4())) == result
    assert sent == []


def test_ingest_source_retry_kwargs_bind_to_task_signature(
    monkeypatch: pytest.MonkeyPatch, loop: None, sent: list[dict[str, Any]]
) -> None:
    """구 버전이 미룬 재시도 메시지가 큐에 남아 있다(#196, #244 수락 기준 5).

    두 카운터 인자가 기본값으로 있어야 인자 없는 레거시 메시지도 소비된다. 구
    `self.retry(kwargs=...)`가 넘긴 dict는 그대로 다음 메시지의 kwarg다.
    """
    seen = _entry(monkeypatch, {"ok": True})

    params = inspect.signature(ingestion.ingest_source.run).parameters
    for name in ("transient_attempt", "gate_attempt"):
        assert name in params, f"{name} 가 시그니처에 없다 — 재시도 메시지를 못 받는다"
        assert params[name].default == 0, f"{name} 에 기본값이 없다 — 레거시 메시지가 깨진다"

    sid = str(uuid.uuid4())
    task = ingestion.ingest_source
    legacy_retry = {"reindex": True, "refetch": False, "transient_attempt": 1, "gate_attempt": 0}
    task.push_request(args=[sid], kwargs={}, retries=0)
    try:
        assert task.run(sid) == {"ok": True}
        assert task.run(sid, gate_attempt=3) == {"ok": True}
        assert task.run(sid, transient_attempt=2) == {"ok": True}
        assert task.run(sid, **legacy_retry) == {"ok": True}
    finally:
        task.pop_request()
    # 첫 시도는 차수를 넘기지 않는다(기존 `_run` 대역 호환)
    assert [{k: v for k, v in s.items() if k != "source_id"} for s in seen] == [
        {"reindex": None, "refetch": None},
        {"reindex": None, "refetch": None, "gate_attempt": 3},
        {"reindex": None, "refetch": None, "transient_attempt": 2},
        {"reindex": True, "refetch": False, "transient_attempt": 1},
    ]
    assert sent == []


# ── index_source: Celery 재시도 ───────────────────────────────────────────


def test_index_source_schedules_retry_without_source_id_kwarg(
    monkeypatch: pytest.MonkeyPatch, loop: None
) -> None:
    seen = _index(monkeypatch, DEFERRED, {"ok": True})
    retries: list[dict[str, Any]] = []

    def retry(**kw: Any) -> Exception:
        retries.append(kw)
        return _Retry()

    monkeypatch.setattr(ingestion.index_source, "retry", retry)

    sid = str(uuid.uuid4())
    with pytest.raises(_Retry):
        ingestion.index_source.run(sid)

    assert retries == [{
        "countdown": 61,
        "max_retries": MAX_RETRIES,
        "kwargs": RETRY_KWARGS,
    }]
    # 첫 시도(핸드오프 없는 reaper 재개 메시지)는 기본값으로 부른다
    assert seen[0] == {"source_id": sid, **DEFAULTS}

    # 재시도 메시지 = 원 args + 위 kwargs. 차수·토큰은 kwargs로 실려 온다(AC15)
    ingestion.index_source.push_request(retries=1)
    try:
        assert ingestion.index_source.run(sid, **retries[0]["kwargs"]) == {"ok": True}
    finally:
        ingestion.index_source.pop_request()
    assert seen[1] == {"source_id": sid, **RETRY_NORMALIZED}


def test_index_source_real_retry_keeps_args_and_ceiling(
    monkeypatch: pytest.MonkeyPatch, loop: None
) -> None:
    """실제 `Task.retry` — 원 args·배달 큐를 유지하고, 두 예산의 합까지만 다시 낸다."""
    _index(monkeypatch, DEFERRED)
    sid = str(uuid.uuid4())
    task = ingestion.index_source

    def run(retries: int) -> None:
        task.push_request(
            args=[sid], kwargs={}, retries=retries, is_eager=True, called_directly=False,
            delivery_info={"routing_key": "ingestion"},
        )
        try:
            task.run(sid)
        finally:
            task.pop_request()

    with pytest.raises(Retry) as caught:
        run(MAX_RETRIES - 1)  # 마지막 미룸도 다시 낸다
    sig = caught.value.sig
    assert tuple(sig.args) == (sid,)
    assert sig.kwargs == RETRY_KWARGS
    assert sig.options["countdown"] == 61
    assert sig.options["routing_key"] == "ingestion"

    with pytest.raises(MaxRetriesExceededError):
        run(MAX_RETRIES)


def test_index_source_kwargs_bind_to_task_signature(
    monkeypatch: pytest.MonkeyPatch, loop: None
) -> None:
    """모든 인자가 기본값이 있는 kwargs다 — 이후 인자를 더해도 큐에 남은 메시지가 깨지지 않는다."""
    seen = _index(monkeypatch)
    params = inspect.signature(ingestion.index_source.run).parameters
    assert list(params) == ["source_id", *DEFAULTS]
    for name, default in DEFAULTS.items():
        assert params[name].default == default, f"{name} 의 기본값이 바뀌었다"

    sid = str(uuid.uuid4())
    task = ingestion.index_source
    task.push_request(args=[sid], kwargs={}, retries=0)
    try:
        assert task.run(sid) == {"ok": True}  # reaper 재개 메시지: 인자 없음
        assert task.run(sid, **HANDOFF) == {"ok": True}  # 전용 워커의 핸드오프
        assert task.run(sid, **RETRY_KWARGS) == {"ok": True}  # 미룬 재시도
    finally:
        task.pop_request()
    assert seen == [
        {"source_id": sid, **DEFAULTS},
        {"source_id": sid, **HANDOFF_NORMALIZED},
        {"source_id": sid, **RETRY_NORMALIZED},
    ]


def test_celery_retry_signature_keeps_args_and_calls_task(
    monkeypatch: pytest.MonkeyPatch, loop: None
) -> None:
    """`Task.retry`가 쓰는 `signature_from_request`로 실제 재시도 서명을 만들어 호출한다."""
    seen = _index(monkeypatch)
    sid = str(uuid.uuid4())
    task = ingestion.index_source
    task.push_request(args=[sid], kwargs=HANDOFF, retries=0)
    try:
        sig = task.signature_from_request(kwargs=RETRY_KWARGS)
    finally:
        task.pop_request()

    assert tuple(sig.args) == (sid,)
    assert sig.kwargs == RETRY_KWARGS
    assert task.run(*sig.args, **sig.kwargs) == {"ok": True}
    assert seen == [{"source_id": sid, **RETRY_NORMALIZED}]


# ── index_source: 비신뢰 메시지 값 정규화(`_index_kwargs_of`) ───────────────


@pytest.mark.parametrize(
    "name,value,expected",
    [
        # 세대 해시 — 소문자 hex 64자만
        ("parsed_sha256", "0123456789abcdef" * 4, "0123456789abcdef" * 4),
        ("parsed_sha256", "", None),
        ("parsed_sha256", "0" * 63, None),
        ("parsed_sha256", "0" * 65, None),
        ("parsed_sha256", "A" * 64, None),
        ("parsed_sha256", "g" * 64, None),
        ("parsed_sha256", "0" * 64 + "\n", None),
        ("parsed_sha256", 123, None),
        ("parsed_sha256", ["0" * 64], None),
        # 점유 출발 상태 — 진행 상태에서 점유하면 살아 있는 실행을 덮는다
        ("claim_from", "chunking", "chunking"),
        ("claim_from", "ready", "ready"),
        ("claim_from", "failed", "failed"),
        ("claim_from", "pending", "pending"),
        ("claim_from", "parsing", "chunking"),
        ("claim_from", "embedding", "chunking"),
        ("claim_from", "", "chunking"),
        ("claim_from", None, "chunking"),
        # 미룸 토큰 — 시간대가 있는 ISO 시각만
        ("deferred_at", DEFERRED_AT, datetime.fromisoformat(DEFERRED_AT)),
        ("deferred_at", "2026-10-16T00:00:01", None),  # naive
        ("deferred_at", "yesterday", None),
        ("deferred_at", "", None),
        ("deferred_at", 1_700_000_000, None),
        # 차수 — 0 이상 정수만
        ("transient_attempt", 3, 3),
        ("transient_attempt", -1, 0),
        ("transient_attempt", "2", 0),
        ("transient_attempt", 1.5, 0),
        ("transient_attempt", True, 0),
        ("transient_attempt", None, 0),
        ("gate_attempt", 5, 5),
        ("gate_attempt", -3, 0),
        ("gate_attempt", "5", 0),
        # 이전 색인 유지 — JSON true만
        ("keep_previous", True, True),
        ("keep_previous", "yes", False),
        ("keep_previous", "true", False),
        ("keep_previous", 1, False),
        ("keep_previous", None, False),
    ],
)
def test_index_source_normalizes_untrusted_kwargs(
    monkeypatch: pytest.MonkeyPatch, loop: None, name: str, value: Any, expected: Any
) -> None:
    # 핸드오프는 비신뢰 파서가 도는 전용 워커가 낸다 — 어긋난 값은 기본값으로 본다
    seen = _index(monkeypatch)

    assert ingestion.index_source.run(str(uuid.uuid4()), **{name: value}) == {"ok": True}

    assert seen[0][name] == expected
    others = {k: v for k, v in seen[0].items() if k not in ("source_id", name)}
    assert others == {k: v for k, v in DEFAULTS.items() if k != name}
