"""시드 스크립트(`scripts/seed.py`)의 사용자 생성 — 코어 v0.11.0 #165 대응.

core-v0.11.0-upgrade AC-1.11.

시드는 코어의 비밀번호 단일 쓰기 지점 `set_password(…, must_change=False)`를 쓴다. 그래서
① 토큰 무효화 정본 `password_changed_at`이 기록되고 ② 시드 계정은 강제 변경 대상이 아니다
(기본 비밀번호가 저장소에 공개돼 있어 플래그를 켜면 공격자가 먼저 바꿔 선점한다 — HCI-9).

시드 함수는 실경로 그대로 부른다(대체 없음). DB는 conftest의 격리 테스트 DB다.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from biznotes_core.core.db import async_session_factory
from biznotes_core.core.security import verify_password
from biznotes_core.models import User
from sqlalchemy import select

from tests.conftest import bearer, login

API_DIR = Path(__file__).resolve().parents[1]
SCRIPT_MODULES = sorted(p.stem for p in (API_DIR / "scripts").glob("*.py") if p.stem != "__init__")


def _fresh_interpreter(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=API_DIR,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize("module", SCRIPT_MODULES)
def test_script_entrypoint_registers_settings_before_core_import(module):
    """새 인터프리터에서 `scripts.<x>`만 import해도 제품 설정 클래스가 코어보다 먼저 등록된다.

    `python -m scripts.<x>`는 `app`을 거치지 않고 시작한다. 하위 모듈이 `biznotes_core`를 먼저
    import해 코어가 기본 설정으로 `get_settings()`를 캐시하면, 뒤늦은 `app` import가
    `set_settings_class`에서 RuntimeError를 낸다(BL-177 — `make seed`가 28a5c51부터 실패, 첫 수정은
    `core_v0_11_0_reconcile`을 새로 깨뜨렸다). `scripts/__init__.py`가 등록을 앞세운다.
    pytest 프로세스는 conftest가 `app`을 먼저 import해 이 순서를 가리므로 별도 프로세스로 본다.
    """
    result = _fresh_interpreter(f"import scripts.{module}; print('ok')")
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("ok")


def test_seed_skill_creator_body_in_fresh_interpreter():
    """`make seed`가 멈추던 지점 — `skill_creator_body()`의 지연 설정 import(BL-177)."""
    code = "import scripts.seed as seed; seed.skill_creator_body(); print('ok')"
    result = _fresh_interpreter(code)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("ok")


async def _user(email: str) -> User:
    async with async_session_factory() as s:
        return (await s.execute(select(User).where(User.email == email))).scalar_one()


async def test_seed_users_not_forced_to_change_password(client: httpx.AsyncClient):
    from scripts.seed import upsert_user

    email = f"seed-{uuid.uuid4().hex[:8]}@test.dev"
    password = "seed-pass-1234"

    await upsert_user(email, password, "시드", "admin")

    user = await _user(email)
    assert user.must_change_password is False
    assert user.password_changed_at is not None  # set_password가 pwv 정본을 남겼다
    assert verify_password(password, user.password_hash)
    assert user.role == "admin"

    # 시드 계정은 로그인 직후 바로 앱을 쓴다 — 강제 변경 게이트(403)에 걸리지 않는다
    token = await login(client, email, password)
    me = await client.get("/api/v1/auth/me", headers=bearer(token))
    assert me.status_code == 200 and me.json()["must_change_password"] is False
    assert (await client.get("/api/v1/notebooks", headers=bearer(token))).status_code == 200

    # 멱등 — 이미 있으면 비밀번호·시각을 바꾸지 않는다
    changed_at = user.password_changed_at
    await upsert_user(email, "other-pass-5678", "시드", "admin")
    again = await _user(email)
    assert again.password_changed_at == changed_at
    assert verify_password(password, again.password_hash)


# ---------- MCP 서버·샘플 정의 최종 상태 — AC-2.4 · HCI-5·HCI-6·HCI-7 ① ----------


async def _seed_mcp_and_samples() -> None:
    from scripts.seed import (
        ADMIN_EMAIL,
        upsert_agents,
        upsert_deep_research_agent,
        upsert_mcp_demo,
        upsert_sample_agents,
        upsert_user,
    )

    await upsert_user(ADMIN_EMAIL, "seed-admin-pass-1", "Admin", "admin")
    await upsert_agents()
    await upsert_mcp_demo()
    await upsert_deep_research_agent()
    await upsert_sample_agents()


async def test_seed_mcp_servers_absolute_and_approved():
    """demo stdio 서버: 절대 경로 명령, env 없음, 승인 3개 read_only."""
    from biznotes_core.models import AuditLog, MCPServer, MCPServerTool

    from scripts.seed import ADMIN_EMAIL

    await _seed_mcp_and_samples()

    async with async_session_factory() as s:
        servers = {sv.name: sv for sv in (await s.execute(select(MCPServer))).scalars()}
        tools = (await s.execute(select(MCPServerTool))).scalars().all()
        admin = (await s.execute(select(User).where(User.email == ADMIN_EMAIL))).scalar_one()
        audits = (
            (await s.execute(select(AuditLog).where(AuditLog.action == "mcp.tools_approve")))
            .scalars()
            .all()
        )

    assert servers["demo"].endpoint_or_command == (
        "/app/.venv/bin/python /app/scripts/demo_mcp_server.py"
    )
    assert servers["demo"].env_encrypted is None
    by_server = {
        name: {t.tool_name: t.read_only for t in tools if t.server_id == sv.id}
        for name, sv in servers.items()
    }
    assert by_server == {
        "demo": {"word_count": True, "echo_upper": True, "reverse_text": True},
    }
    assert {t.approved_by for t in tools} == {admin.id}  # actor = 시드 관리자(HCI-5)
    assert sorted(a.resource for a in audits) == ["demo"]

    # 멱등 — 다시 돌려도 승인은 그대로(승인 행 재작성·감사 추가 없음)
    await _seed_mcp_and_samples()
    async with async_session_factory() as s:
        assert len((await s.execute(select(MCPServerTool))).scalars().all()) == 3
        audit_count = len(
            (await s.execute(select(AuditLog).where(AuditLog.action == "mcp.tools_approve")))
            .scalars()
            .all()
        )
        assert audit_count == 1


async def test_seed_demo_server_keeps_operator_command_and_approvals():
    """기존 demo 서버: 운영자가 바꾼 명령·승인은 다시 시드해도 보존한다."""
    from datetime import UTC, datetime

    from biznotes_core.models import MCPServer, MCPServerTool

    from scripts.seed import upsert_mcp_demo

    await upsert_mcp_demo()
    async with async_session_factory() as s:
        demo = (await s.execute(select(MCPServer).where(MCPServer.name == "demo"))).scalar_one()
        assert demo.endpoint_or_command == "/app/.venv/bin/python /app/scripts/demo_mcp_server.py"
        # 운영자가 바꾼 명령·승인 1개만 둔 상태
        demo.endpoint_or_command = "/opt/custom/demo-mcp"
        await s.execute(MCPServerTool.__table__.delete())
        s.add(
            MCPServerTool(
                server_id=demo.id,
                tool_name="word_count",
                read_only=False,
                approved_at=datetime.now(UTC),
            )
        )
        await s.commit()
    await upsert_mcp_demo()
    async with async_session_factory() as s:
        demo = (await s.execute(select(MCPServer).where(MCPServer.name == "demo"))).scalar_one()
        assert demo.endpoint_or_command == "/opt/custom/demo-mcp"
        rows = (await s.execute(select(MCPServerTool))).scalars().all()
        assert [(r.tool_name, r.read_only) for r in rows] == [("word_count", False)]


async def test_seed_sample_definitions_direct_node_policy():
    """샘플 정의의 `requires_approval: false` 대상이 HCI-7 목록과 정확히 같고, 시드한 system
    정의가 모두 저장 검증(코어 `find_direct_node_approval_violations`)을 통과한다.
    Webhook은 게이트 유지."""
    from biznotes_core.models import Agent
    from biznotes_core.services.agents.policy import find_direct_node_approval_violations
    from biznotes_core.services.agents.runtime import kind_to_nodes

    from scripts.seed import DIRECT_NODE_UNGATED_AGENTS

    await _seed_mcp_and_samples()

    async with async_session_factory() as s:
        agents = (
            (await s.execute(select(Agent).where(Agent.scope == "system"))).scalars().all()
        )
    ungated: set[tuple[str, str]] = set()
    by_name = {}
    for agent in agents:
        by_name[agent.name] = agent
        nodes, edges = kind_to_nodes(agent.kind, agent.definition)
        assert find_direct_node_approval_violations(nodes, edges, "system") == [], agent.name
        for node in nodes:
            assert node.get("requires_approval", True) in (True, False), agent.name
            if node.get("requires_approval") is False:
                ungated.add((agent.name, node["id"]))

    expected = {(name, node_id or "call") for name, node_id in DIRECT_NODE_UNGATED_AGENTS.items()}
    assert ungated == expected
    assert len(expected) == 9  # 리터럴 host GET 5 · demo MCP 4

    webhook = by_name["[샘플] Webhook 전송"]
    assert "requires_approval" not in webhook.definition["http"]
    # 외부로 내보내는 POST 샘플은 받아오기 전용 배포(D4)에서 실행되지 않으므로 꺼 둔다. 나머지
    # 샘플은 켠다. 이미 켜진 채 남은 행(이전 시드)도 다시 시드하면 꺼진다
    samples = {name: a for name, a in by_name.items() if name.startswith("[샘플]")}
    assert [name for name, a in samples.items() if not a.enabled] == ["[샘플] Webhook 전송"]
    async with async_session_factory() as s:
        row = await s.get(Agent, webhook.id)
        assert row is not None
        row.enabled = True
        await s.commit()
    await _seed_mcp_and_samples()
    async with async_session_factory() as s:
        row = await s.get(Agent, webhook.id)
        assert row is not None and row.enabled is False


async def test_seed_second_run_keeps_system_definitions_identical():
    """`make seed` 재실행 멱등(core-v0.20.0-upgrade AC-2.5) — 시드가 시스템 정의를 다시 덮어써도
    id·kind·정의가 같고 행이 늘지 않는다. 시드의 에이전트 정의 쓰기 함수 전부
    (`_seed_mcp_and_samples`)를 두 번 부른다. `|untrusted` 필터 자리(4곳)도 그대로다
    (`test_seed_untrusted_filters.py`)."""
    import json

    from biznotes_core.models import Agent

    async def snapshot() -> dict[str, tuple]:
        async with async_session_factory() as s:
            rows = (await s.execute(select(Agent).where(Agent.scope == "system"))).scalars().all()
        assert len({a.name for a in rows}) == len(rows)
        return {a.name: (a.id, a.kind, a.definition, a.enabled) for a in rows}

    await _seed_mcp_and_samples()
    first = await snapshot()
    await _seed_mcp_and_samples()
    second = await snapshot()

    assert first, "시드한 시스템 정의가 없다 — 비교가 공허하다"
    assert second == first
    texts = [json.dumps(v[2], ensure_ascii=False) for v in second.values()]
    assert sum(t.count("|untrusted}") for t in texts) == 4


async def test_seed_pins_spec_hash_from_live_list_tools(monkeypatch, capsys):
    """코어 v0.21.0 #293 — 시드 승인은 지금 `list_tools`가 준 스펙으로 고정한다(dev 서버). 목록에
    없는 도구는 고정되지 않고(노출 안 됨) 재승인 안내를 출력한다(core-v0.22.0-upgrade AC-5)."""
    from biznotes_core.models import MCPServer, MCPServerTool
    from biznotes_core.services.agents.mcp_tool_spec import mcp_tool_spec_hash

    def _listed(name: str) -> dict:
        return {"name": name, "description": f"{name} 도구", "input_schema": {"type": "object"}}

    served = {
        "demo": [_listed("word_count"), _listed("echo_upper")],  # reverse_text 없음
    }

    async def fake_list_tools(server):
        return served[server.name]

    monkeypatch.setattr("biznotes_core.services.agents.mcp_client.list_tools", fake_list_tools)
    await _seed_mcp_and_samples()

    async with async_session_factory() as s:
        names = {sv.id: sv.name for sv in (await s.execute(select(MCPServer))).scalars()}
        pins = {
            (names[t.server_id], t.tool_name): t.approved_spec_hash
            for t in (await s.execute(select(MCPServerTool))).scalars()
        }
    assert pins == {
        ("demo", "word_count"): mcp_tool_spec_hash(served["demo"][0]),
        ("demo", "echo_upper"): mcp_tool_spec_hash(served["demo"][1]),
        ("demo", "reverse_text"): None,
    }
    out = capsys.readouterr().out
    assert "warn: demo 스펙 미고정 도구는 노출되지 않습니다(reverse_text)" in out
