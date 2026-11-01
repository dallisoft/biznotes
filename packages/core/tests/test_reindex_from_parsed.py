"""재인덱싱은 저장된 파싱 결과에서 시작한다 — 발행 계약과 모드 판정 (#6).

모드(재인덱싱·원본 재수집)는 브로커 메시지가 아니라 DB의 재인덱싱 항목에서 읽는다.
그래서 메시지는 예전과 똑같고, 배포 중 구 워커가 새 메시지를 받아도 깨지지 않는다.
워커의 실제 분기(파싱 결과 재사용·이전 색인 유지·부분 교체 롤백)는 DB 세션이 필요해
소비 제품 통합 검증 몫이다.
"""

import inspect
from typing import Any

import pytest

from biznotes_core.services.ingestion import enqueue
from biznotes_core.workers.celery_app import celery_app
from biznotes_core.workers.task_names import INGEST_SOURCE
from biznotes_core.workers.tasks import ingestion

_ABSENT = object()  # kwargs 인자를 넘기지 않았음을 구분한다


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_send_task(name: str, args: Any = None, kwargs: Any = _ABSENT, **opts: Any) -> None:
        calls.append({"name": name, "args": args, "kwargs": kwargs, **opts})

    monkeypatch.setattr(celery_app, "send_task", fake_send_task)
    return calls


def test_publish_message_is_unchanged(sent):
    """발행 메시지는 인자 1개 그대로다 — 구 워커도 그대로 소비한다."""
    enqueue._publish("sid", "tid")

    assert sent == [{"name": INGEST_SOURCE, "args": ["sid"], "kwargs": _ABSENT, "task_id": "tid"}]
    # 발행부에 모드 인자가 없다 — 모드는 DB에서 읽는다
    assert set(inspect.signature(enqueue.stage_ingest_job).parameters) == {"db", "source", "uow"}


@pytest.mark.parametrize(
    "status,open_item,expected",
    [
        # 열린 재인덱싱 항목이 있으면 재인덱싱이다
        ("ready", (True, False), (True, False)),
        ("failed", (True, False), (True, False)),
        # 요청 중 하나라도 원본 재수집이면 원본부터 처리한다
        ("ready", (True, True), (True, True)),
        # 재시도·정체 회수는 pending으로 바꾼 뒤 발행한다 — 원본부터 처리한다
        ("pending", (True, False), (False, False)),
        # 열린 항목이 없으면 평범한 인제스트
        ("pending", (False, False), (False, False)),
        ("ready", (False, False), (False, False)),
    ],
)
def test_mode_is_derived_from_open_reindex_item(status, open_item, expected):
    assert ingestion._effective_mode(status, open_item, None, None) == expected


def test_explicit_mode_overrides_db():
    """운영·테스트에서 인자로 모드를 강제할 수 있다."""
    assert ingestion._effective_mode("pending", (False, False), True, True) == (True, True)
    assert ingestion._effective_mode("ready", (True, True), False, False) == (False, False)


def test_worker_accepts_old_and_new_messages(monkeypatch):
    seen: list[tuple[str, Any, Any]] = []

    async def fake_run(source_id: str, reindex: Any = None, refetch: Any = None) -> dict:
        seen.append((source_id, reindex, refetch))
        return {"ok": True}

    monkeypatch.setattr(ingestion, "_run", fake_run)
    monkeypatch.setattr(ingestion, "ensure_buckets", lambda: None)
    monkeypatch.setattr(ingestion, "run_async", lambda coro: __import__("asyncio").run(coro))

    ingestion.ingest_source.run("a")  # 통상 메시지: 인자 1개 → 모드는 워커가 판단
    ingestion.ingest_source.run("b", reindex=True, refetch=True)  # 명시 강제

    assert seen == [("a", None, None), ("b", True, True)]
