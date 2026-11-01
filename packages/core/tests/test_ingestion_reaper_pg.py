# 출처: 원본 제품 저장소 `apps/api/tests/test_ingestion_reaper.py` @ 1079bbf
#       (sl-agents 사본의 상위집합)
# #1 이관(docs/plans/opt-in-pg-suite/). 필요: PostgreSQL·Valkey(PG 테스트).
# 본문은 원본 그대로 — 바꾼 것: import 정렬(ruff I001).
"""정체된 소스 재발행 안전망(reap_stalled_sources) 테스트.

메시지 유실·컨슈머 wedge·enqueue 실패로 non-terminal 상태에 오래 멈춘 소스를
주기 스위퍼가 다시 ingest 큐로 발행하는지 검증한다. Celery 발행 경계만 모킹한다.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from biznotes_core.models import Notebook, Source
from biznotes_core.workers.tasks.ingestion import STALLED_THRESHOLD_MINUTES, _reap_stalled


@pytest.fixture
def _mock_apply_async(monkeypatch):
    """ingest_source.apply_async 호출 인자를 기록 (실제 Celery/Redis 발행 차단).

    `kwargs`도 보관한다 — 코어 v0.9.0은 `publish_kwargs(hwp_queue_for(...))`로 큐를
    고르므로(HWP 전용 큐 `ingest.hwp`), 큐 라우팅이 계약대로인지 단언해야 한다.
    HWP 플래그가 꺼진 기본 설정에서는 `queue` 키가 아예 없어야 한다(현행 task_routes 유지).
    """
    calls: list[dict] = []
    monkeypatch.setattr(
        "biznotes_core.workers.tasks.ingestion.ingest_source.apply_async",
        lambda *, args, **kwargs: calls.append({"args": args, "kwargs": kwargs}),
    )
    return calls


async def _make_source(db, notebook_id, status, age_minutes) -> uuid.UUID:
    source = Source(notebook_id=notebook_id, type="text", title=f"src-{status}", status=status)
    db.add(source)
    await db.flush()
    sid = source.id
    # updated_at을 과거로 강제 (onupdate를 우회해 명시값으로 설정)
    moment = datetime.now(UTC) - timedelta(minutes=age_minutes)
    await db.execute(update(Source).where(Source.id == sid).values(updated_at=moment))
    await db.commit()
    return sid


async def test_reaper_requeues_only_stalled_nonterminal(db, user_a, _mock_apply_async):
    nb = Notebook(owner_id=user_a.id, title="reaper-nb")
    db.add(nb)
    await db.flush()

    old = STALLED_THRESHOLD_MINUTES + 5
    fresh = STALLED_THRESHOLD_MINUTES - 5

    stalled = await _make_source(db, nb.id, "embedding", old)  # 정체 → 재발행 대상
    fresh_inflight = await _make_source(db, nb.id, "parsing", fresh)  # 처리 중 → 제외
    done = await _make_source(db, nb.id, "ready", old)  # 종료 상태 → 제외
    failed = await _make_source(db, nb.id, "failed", old)  # 종료 상태 → 제외

    result = await _reap_stalled()

    assert result == {"requeued": 1, "reindex_requeued": 0}
    assert [call["args"] for call in _mock_apply_async] == [[str(stalled)]]
    # HWP 플래그 off → publish_kwargs가 빈 dict → 큐 오버라이드 없음(기본 ingestion 큐).
    assert [call["kwargs"] for call in _mock_apply_async] == [{}]

    # 정체 소스는 pending으로 리셋 + updated_at 갱신(다음 스윕 중복 방지)
    await db.commit()  # 워커 세션 커밋분을 보기 위해 트랜잭션 갱신
    refreshed = await db.get(Source, stalled, populate_existing=True)
    assert refreshed.status == "pending"
    assert refreshed.error_message is None

    # 나머지는 그대로
    for sid, expected in ((fresh_inflight, "parsing"), (done, "ready"), (failed, "failed")):
        s = await db.get(Source, sid, populate_existing=True)
        assert s.status == expected


async def test_reaper_noop_when_nothing_stalled(db, user_a, _mock_apply_async):
    nb = Notebook(owner_id=user_a.id, title="reaper-nb2")
    db.add(nb)
    await db.flush()
    await _make_source(db, nb.id, "pending", STALLED_THRESHOLD_MINUTES - 1)

    result = await _reap_stalled()

    assert result == {"requeued": 0, "reindex_requeued": 0}
    assert _mock_apply_async == []
