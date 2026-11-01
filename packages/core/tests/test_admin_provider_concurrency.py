"""어드민 프로바이더 API의 임베딩 동시성 상한 계약(#196).

계획 `docs/plans/embedding-concurrency-limit/plan.md`의 AC7을 **라우터 수준**에서 확인한다.
`tests/test_provider_concurrency_schema.py`는 스키마·메타데이터만 본다. 여기서는 라우터가
**상한을 실수로 지우지 않는지**와 감사 기록까지 단언한다 — 스키마 단위로는 이 경로가
검증되지 않는다(구현 리뷰 m11).

DB·감사 기록은 기록형 대역이다. 라우터 본문은 실경로다.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from biznotes_core.api.v1.admin import llm as admin_llm
from biznotes_core.models import LLMProvider

ADMIN = SimpleNamespace(id=uuid.uuid4())


class _DB:
    """한 행만 돌려주는 가짜 세션 — 라우터 본문은 실경로다."""

    def __init__(self, provider: LLMProvider | None) -> None:
        self._provider = provider

    async def get(self, cls: Any, ident: Any) -> Any:
        return self._provider

    async def execute(self, stmt: Any) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    async def commit(self) -> None:
        return None

    async def refresh(self, obj: Any) -> None:
        return None

    def add(self, obj: Any) -> None:
        obj.id = uuid.uuid4()


@pytest.fixture
def provider() -> LLMProvider:
    return LLMProvider(
        id=uuid.uuid4(),
        name="p",
        type="ollama",
        base_url=None,
        api_key_encrypted=None,
        enabled=True,
        embedding_max_concurrency=4,
    )


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def record(
        db: Any, user_id: Any, action: str, resource: Any = None, detail: Any = None
    ) -> None:
        # 라우터는 5개를 **위치 인자**로 넘긴다 — 시그니처가 어긋나면 조용히 실패한다
        seen.append({"action": action, "resource": resource, "detail": detail})

    monkeypatch.setattr(admin_llm, "record", record)
    return seen


async def _patch(
    provider: LLMProvider,
    audit: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> _DB:
    db = _DB(provider)
    monkeypatch.setattr(admin_llm.LLMProvider, "commit", lambda self: None, raising=False)
    return db


# ── AC7: 상한 갱신 계약 ────────────────────────────────────────────────────


async def test_patch_without_the_key_keeps_the_limit(
    provider: LLMProvider, audit: list[dict[str, Any]]
) -> None:
    """키를 안 보내면 **바꾸지 않는다** — 다른 필드의 규칙과 같다."""
    from biznotes_core.schemas.llm import ProviderUpdate

    out = await admin_llm.update_provider(
        provider.id, ProviderUpdate(enabled=False), _DB(provider), ADMIN  # type: ignore[arg-type]
    )

    assert provider.embedding_max_concurrency == 4, "키가 없는데 상한이 지워졌다"
    assert out.embedding_max_concurrency == 4
    assert audit[0]["detail"] == {}  # 바뀌지 않았으면 상한은 기록하지 않는다


async def test_patch_sets_the_limit_and_records_it(
    provider: LLMProvider, audit: list[dict[str, Any]]
) -> None:
    from biznotes_core.schemas.llm import ProviderUpdate

    out = await admin_llm.update_provider(
        provider.id, ProviderUpdate(embedding_max_concurrency=8), _DB(provider), ADMIN  # type: ignore[arg-type]
    )

    assert provider.embedding_max_concurrency == 8
    assert out.embedding_max_concurrency == 8
    # 상한 변경은 추적돼야 한다 — 운영자가 왜 느려졌는지 알 수 있어야 한다
    assert audit[0]["detail"] == {"embedding_max_concurrency": 8}


async def test_patch_with_explicit_null_releases_the_limit(
    provider: LLMProvider, audit: list[dict[str, Any]]
) -> None:
    """명시적 None은 "제한 해제"다 — 0이나 "없음"으로 읽히면 안 된다."""
    from biznotes_core.schemas.llm import ProviderUpdate

    out = await admin_llm.update_provider(
        provider.id, ProviderUpdate(embedding_max_concurrency=None), _DB(provider), ADMIN  # type: ignore[arg-type]
    )

    assert provider.embedding_max_concurrency is None
    assert out.embedding_max_concurrency is None
    assert audit[0]["detail"] == {"embedding_max_concurrency": None}


async def test_create_records_the_limit_in_the_audit(
    audit: list[dict[str, Any]]
) -> None:
    """생성도 상한을 감사 detail에 남긴다."""
    from biznotes_core.schemas.llm import ProviderCreate

    created: list[LLMProvider] = []

    db = _DB(None)
    added: list[Any] = []
    _add = db.add

    def _track(obj: Any) -> None:
        added.append(obj)
        _add(obj)

    db.add = _track  # type: ignore[method-assign]
    out = await admin_llm.create_provider(
        ProviderCreate(name="new", type="ollama", embedding_max_concurrency=2), db, ADMIN  # type: ignore[arg-type]
    )

    assert out.embedding_max_concurrency == 2
    assert audit[0]["action"] == "llm.provider_create"
    assert audit[0]["detail"]["embedding_max_concurrency"] == 2
    assert created == []
    assert len(added) == 1


async def test_out_never_reports_a_limit_it_was_not_given(
    provider: LLMProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider.embedding_max_concurrency = None
    from biznotes_core.schemas.llm import provider_to_out

    assert provider_to_out(provider).embedding_max_concurrency is None

# ── #207: 게이트 상태 조회가 이벤트 루프를 막지 않는다 ──────────────────────


async def test_embedding_gate_status_runs_off_the_event_loop(
    provider: LLMProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """동기 Redis 조회가 느려도 같은 루프의 다른 코루틴은 계속 돈다."""
    import threading
    import time

    import anyio

    from biznotes_core.core import concurrency_gate

    loop_thread = threading.get_ident()
    seen_threads: list[int] = []

    def slow_status(provider_id: uuid.UUID) -> concurrency_gate.GateStatus:
        seen_threads.append(threading.get_ident())
        time.sleep(0.3)  # 느린 Redis 왕복의 대역
        return concurrency_gate.GateStatus(in_use=2, earliest_expiry_in=1.5, last_error=None)

    monkeypatch.setattr(concurrency_gate, "status", slow_status)
    ticks = 0
    out: Any = None

    async def ticker() -> None:
        nonlocal ticks
        while out is None:
            ticks += 1
            await anyio.sleep(0.01)

    async def call() -> None:
        nonlocal out
        out = await admin_llm.provider_embedding_gate(provider.id, _DB(provider), ADMIN)  # type: ignore[arg-type]

    async with anyio.create_task_group() as tg:
        tg.start_soon(ticker)
        tg.start_soon(call)

    assert seen_threads and seen_threads[0] != loop_thread, "조회가 이벤트 루프 스레드에서 돌았다"
    # 루프가 막혔다면 조회가 끝날 때까지 ticker가 한 번만 돈다
    assert ticks >= 5
    assert (out.limit, out.in_use, out.earliest_expiry_in) == (4, 2, 1.5)


async def test_embedding_gate_redis_failure_is_still_503(
    provider: LLMProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    from biznotes_core.core import concurrency_gate

    def broken(provider_id: uuid.UUID) -> concurrency_gate.GateStatus:
        raise RuntimeError("임베딩 게이트 상태를 읽지 못했습니다: down")

    monkeypatch.setattr(concurrency_gate, "status", broken)
    with pytest.raises(HTTPException) as exc:
        await admin_llm.provider_embedding_gate(provider.id, _DB(provider), ADMIN)  # type: ignore[arg-type]
    assert exc.value.status_code == 503


async def test_embedding_gate_without_limit_skips_redis(
    provider: LLMProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biznotes_core.core import concurrency_gate

    def must_not_call(provider_id: uuid.UUID) -> concurrency_gate.GateStatus:
        raise AssertionError("상한이 없으면 Redis를 만지지 않는다")

    monkeypatch.setattr(concurrency_gate, "status", must_not_call)
    provider.embedding_max_concurrency = None
    out = await admin_llm.provider_embedding_gate(provider.id, _DB(provider), ADMIN)  # type: ignore[arg-type]
    assert out.limit is None and out.in_use == 0
