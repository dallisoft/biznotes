"""run event 발행 Lua 인자 배선(#154 B) — 실제 Redis 없이 표준 게이트에서 잠근다.

실 Redis의 EXPIRE·PERSIST 동작은 `tests/test_run_event_sequence_retention_pg.py`가 본다.
"""

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from biznotes_core.schemas.enums import TERMINAL_RUN_STATUSES
from biznotes_core.schemas.run_events import ProgressEventInput, StatusEventInput
from biznotes_core.services import events


class FakeRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def eval(self, script: str, key_count: int, *keys_and_args: Any) -> Any:
        self.calls.append(keys_and_args)
        payload = json.loads(keys_and_args[2])
        payload["event_id"] = len(self.calls)
        return [payload["event_id"], json.dumps(payload)]

    def close(self) -> None:
        return None


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    client = FakeRedis()
    monkeypatch.setattr(events.redis_sync.Redis, "from_url", lambda *a, **k: client)
    monkeypatch.setattr(
        events,
        "get_settings",
        lambda: SimpleNamespace(redis_url="redis://x", run_event_sequence_retention_sec=123),
    )
    return client


@pytest.mark.parametrize("status", sorted(TERMINAL_RUN_STATUSES))
def test_terminal_status_passes_the_retention(fake: FakeRedis, status: str) -> None:
    events.publish_run_event(uuid.uuid4(), StatusEventInput(type="status", status=status))
    assert len(fake.calls[0]) == 4 and fake.calls[0][3] == 123


@pytest.mark.parametrize(
    "event",
    [
        StatusEventInput(type="status", status="running"),
        StatusEventInput(type="status", status="awaiting_approval"),
        ProgressEventInput(type="progress", node="n", progress=1),
    ],
)
def test_other_events_keep_the_old_argument_shape(fake: FakeRedis, event: Any) -> None:
    """비terminal은 인자 모양이 예전과 같다(키 2개 + 페이로드).

    소비 제품 테스트의 고정 인자 가짜 eval과도 맞는다."""
    events.publish_run_event(uuid.uuid4(), event)
    assert len(fake.calls[0]) == 3


def test_terminal_set_is_the_single_source() -> None:
    assert TERMINAL_RUN_STATUSES == {"succeeded", "failed", "cancelled"}
    assert "PERSIST" in events._ALLOCATE_AND_PUBLISH_LUA
    assert events._ALLOCATE_AND_PUBLISH_LUA.count("EXPIRE") == 2  # 정상·낡은 세대 두 분기
