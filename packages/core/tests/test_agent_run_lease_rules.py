"""agent run lease의 DB 없는 규칙(#153 PR-B) — 설정 검증기, lease 확인 전파, 외부 호출 직전 확인.

lease·fencing·회수의 실제 동작은 `tests/test_agent_run_lease_pg.py`가 본다.
"""

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from biznotes_core.config import CoreSettings
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import RunLeaseLostError
from biznotes_core.services.agents.nodes import composite


def test_settings_reject_heartbeat_slower_than_the_lease() -> None:
    """(F+1)·interval + T < ttl — 갱신이 F번 실패해도 만료 전에 스스로 멈춘다."""
    CoreSettings(agent_run_lease_ttl_sec=120)  # 기본: (2+1)·30 + 10 = 100 < 120
    with pytest.raises(ValidationError, match="lease"):
        CoreSettings(agent_run_lease_ttl_sec=100)
    with pytest.raises(ValidationError, match="lease"):
        CoreSettings(agent_run_heartbeat_max_failed_ticks=3)


def _ctx(**kw: Any) -> RunContext:
    return RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={}, **kw)  # type: ignore[arg-type]


def test_lease_check_reaches_subagents() -> None:
    def check() -> bool:
        return True

    child = _ctx(lease_valid=check).child(uuid.uuid4(), {})
    assert child.lease_valid is check


async def test_direct_node_does_not_call_out_after_losing_the_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """회수된 run에서는 외부 호출을 하지 않는다 — 요청 0회, 정책 오류."""
    sent: list[str] = []

    async def fake_request(method: str, url: str, **kw: Any) -> Any:
        sent.append(url)
        raise AssertionError("호출되면 안 된다")

    async def not_gated(ctx: Any, node: Any) -> bool:
        return False

    monkeypatch.setattr("biznotes_core.core.ssrf.ssrf_safe_request", fake_request)
    monkeypatch.setattr("biznotes_core.core.ssrf.validate_external_url", lambda *a, **k: None)
    monkeypatch.setattr(
        "biznotes_core.services.agents.policy.direct_node_needs_gate", not_gated
    )
    ctx = _ctx(lease_valid=lambda: False)
    with pytest.raises(RunLeaseLostError):
        await composite._exec_http(ctx, {"id": "call", "url": "https://a.example.com/"}, {})
    assert sent == []
