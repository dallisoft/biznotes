# 출처: 원본 제품 저장소 `apps/api/tests/test_gateway_fallback.py` @ 1079bbf
#       (sl-agents 사본의 상위집합)
# #1 이관(docs/plans/opt-in-pg-suite/). 필요: PostgreSQL·Valkey(PG 테스트).
# 본문은 원본 그대로 — 바꾼 것: import 정렬(ruff I001), 장애 시뮬레이션에 연결 오류 원인을
# 단다(#380 — 재시도·서킷은 예외 사슬의 HTTP 응답·전송 오류로 나누고, 원인 없는 예외는 영구
# 오류로 본다).
"""LLM 게이트웨이 폴백 체인 + 서킷 브레이커 테스트.

실패 프로바이더는 complete_stream 몽키패치로 시뮬레이션 (name이 'failing'으로 시작).
Redis는 conftest의 테스트 DB(redis://localhost:26379/15)를 사용.
"""

import uuid

import pytest

from biznotes_core.services.gateway import circuit
from biznotes_core.services.gateway.llm import (
    GatewayError,
    StreamResult,
    complete_stream,
    complete_stream_with_fallback,
    resolve_chain,
)


async def _seed(db, *, name: str, is_default: bool = False, fallback_priority: int | None = None):
    from biznotes_core.models import LLMModel, LLMProvider

    provider = LLMProvider(name=f"{name}-{uuid.uuid4().hex[:6]}", type="mock", enabled=True)
    db.add(provider)
    await db.flush()
    model = LLMModel(
        provider_id=provider.id,
        model_name=f"mock-{name}",
        display_name=name,
        capabilities=["llm"],
        is_default=is_default,
        enabled=True,
        fallback_priority=fallback_priority,
    )
    db.add(model)
    await db.commit()
    return provider, model


@pytest.fixture(autouse=True)
async def _clean_llm(db):
    """이 모듈은 default/fallback 구성을 정밀 검증하므로 기존 모델을 비운다."""
    from sqlalchemy import delete

    from biznotes_core.models import LLMModel, LLMProvider

    await db.execute(delete(LLMModel))
    await db.execute(delete(LLMProvider))
    await db.commit()


@pytest.fixture(autouse=True)
def _fail_by_name(monkeypatch):
    """provider.name이 'failing'으로 시작하면 GatewayError를 던지는 complete_stream 래퍼."""

    async def wrapper(resolved, messages, result, temperature=0.2, max_tokens=2048):
        if resolved.provider.name.startswith("failing"):
            raise GatewayError("simulated provider outage") from ConnectionError("provider down")
        return await complete_stream(resolved, messages, result, temperature, max_tokens)

    monkeypatch.setattr("biznotes_core.services.gateway.llm.complete_stream", wrapper)


MESSAGES = [{"role": "user", "content": "[1] 컨텍스트 본문\n\n질문입니다"}]


async def test_circuit_transitions():
    pid = uuid.uuid4()
    assert await circuit.is_open(pid) is False
    for _ in range(circuit.FAIL_THRESHOLD - 1):
        assert await circuit.record_failure(pid) is False
    assert await circuit.is_open(pid) is False
    assert await circuit.record_failure(pid) is True  # 임계 도달 → open
    assert await circuit.is_open(pid) is True
    status = await circuit.get_status(pid)
    assert status["open"] is True and status["open_remaining_seconds"] > 0
    await circuit.reset(pid)
    assert await circuit.is_open(pid) is False


async def test_circuit_success_resets_counter():
    pid = uuid.uuid4()
    for _ in range(circuit.FAIL_THRESHOLD - 1):
        await circuit.record_failure(pid)
    await circuit.record_success(pid)
    status = await circuit.get_status(pid)
    assert status["recent_failures"] == 0


async def test_resolve_chain_order(db):
    _, primary = await _seed(db, name="primary", is_default=True)
    await _seed(db, name="second", fallback_priority=2)
    await _seed(db, name="first", fallback_priority=1)

    chain = await resolve_chain(db, None)
    names = [c.model.display_name for c in chain]
    assert names[0] == "primary"
    assert names.index("first") < names.index("second")


async def test_fallback_switches_to_secondary(db):
    failing_provider, _ = await _seed(db, name="failing-primary", is_default=True)
    await _seed(db, name="backup", fallback_priority=1)

    result = StreamResult()
    gen = await complete_stream_with_fallback(db, None, MESSAGES, result)
    text = "".join([d async for d in gen])

    assert text  # backup(mock)이 응답
    assert result.fallback_used is True
    assert result.model is not None and result.model.model.display_name == "backup"
    status = await circuit.get_status(failing_provider.id)
    assert status["recent_failures"] >= 1


async def test_no_fallback_on_primary_success(db):
    await _seed(db, name="healthy", is_default=True)
    await _seed(db, name="backup2", fallback_priority=1)

    result = StreamResult()
    gen = await complete_stream_with_fallback(db, None, MESSAGES, result)
    text = "".join([d async for d in gen])

    assert text
    assert result.fallback_used is False
    assert result.model is not None and result.model.model.display_name == "healthy"


async def test_all_candidates_fail(db):
    await _seed(db, name="failing-only", is_default=True)

    result = StreamResult()
    with pytest.raises(GatewayError, match="모든 LLM 후보"):
        await complete_stream_with_fallback(db, None, MESSAGES, result)


async def test_open_circuit_skipped_without_attempt(db):
    failing_provider, _ = await _seed(db, name="failing-open", is_default=True)
    await _seed(db, name="backup3", fallback_priority=1)

    for _ in range(circuit.FAIL_THRESHOLD):
        await circuit.record_failure(failing_provider.id)
    assert await circuit.is_open(failing_provider.id) is True

    result = StreamResult()
    gen = await complete_stream_with_fallback(db, None, MESSAGES, result)
    text = "".join([d async for d in gen])
    assert text
    assert result.model is not None and result.model.model.display_name == "backup3"
    # 차단 중에는 시도 자체가 없어 실패 카운터가 늘지 않음
    status = await circuit.get_status(failing_provider.id)
    assert status["recent_failures"] == 0
