"""실행 로그 API 테스트 — `GET /agent-runs/{run_id}/logs`."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from tests.conftest import bearer, login


async def _seed_run(db, *, user_id, notebook_id, agent_id, status, dur_s=2.0, created=None):
    from biznotes_core.models import AgentRun

    now = created or datetime.now(UTC)
    started = now
    finished = now + timedelta(seconds=dur_s) if status in ("succeeded", "failed") else None
    run = AgentRun(
        agent_id=agent_id,
        notebook_id=notebook_id,
        user_id=user_id,
        params={},
        status=status,
        created_at=now,
        started_at=started,
        finished_at=finished,
    )
    db.add(run)
    await db.flush()
    return run


async def _seed_log(db, run_id, *, cost, tokens, node_id="n", status="succeeded"):
    from biznotes_core.models import ExecutionLog

    db.add(
        ExecutionLog(
            run_id=run_id,
            node_id=node_id,
            node_type="llm",
            status=status,
            cost_usd=Decimal(str(cost)),
            token_usage={"total_tokens": tokens},
            duration_ms=1500,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
    )


async def _seed_agent(db, owner_id):
    from biznotes_core.models import Agent, Notebook

    nb = Notebook(owner_id=owner_id, title="mon-nb")
    db.add(nb)
    await db.flush()
    agent = Agent(
        name="mon-agent",
        kind="workflow",
        definition={"nodes": [], "edges": []},
        scope="user",
        owner_id=owner_id,
    )
    db.add(agent)
    await db.flush()
    return nb, agent


async def test_agent_run_logs(client: httpx.AsyncClient, db, user_a):
    nb, agent = await _seed_agent(db, user_a.id)
    run = await _seed_run(
        db, user_id=user_a.id, notebook_id=nb.id, agent_id=agent.id, status="succeeded"
    )
    await _seed_log(db, run.id, cost=0.1, tokens=10, node_id="gen")
    await db.commit()

    token = await login(client, user_a.email)
    res = await client.get(f"/api/v1/agent-runs/{run.id}/logs", headers=bearer(token))
    assert res.status_code == 200
    logs = res.json()
    assert len(logs) == 1
    assert logs[0]["node_id"] == "gen"
    assert logs[0]["token_usage"]["total_tokens"] == 10
    assert logs[0]["cost_usd"] == 0.1


async def test_agent_run_logs_forbidden_for_other_user(
    client: httpx.AsyncClient, db, user_a, user_b
):
    nb, agent = await _seed_agent(db, user_b.id)
    run = await _seed_run(
        db, user_id=user_b.id, notebook_id=nb.id, agent_id=agent.id, status="succeeded"
    )
    await db.commit()

    token = await login(client, user_a.email)
    res = await client.get(f"/api/v1/agent-runs/{run.id}/logs", headers=bearer(token))
    assert res.status_code == 403


# ---------- 실행 로그 `error_code` — core-v0.20.0-upgrade AC-2.3(§5.5·D-11) ----------
#
# 실 그래프로 실패 노드를 만든다: 워커 `_run`(제품 워커 실물 — 재검사·상태 전이 포함)이 코어
# `execute_agent`로 상수 노드 → http 노드(`127.0.0.1` — SSRF 사전 검사가 요청 전에 막는다)를 돈다.
# 대체는 이벤트 발행 경계 `agent_task._publish`뿐이다(계획 §9 허용 대체 ②). 실제 요청은
# 나가지 않는다.


async def _run_ssrf_workflow(db, owner, monkeypatch):
    from biznotes_core.models import Agent, AgentRun, Notebook

    import app.workers.tasks.agent as agent_task

    monkeypatch.setattr(agent_task, "_publish", lambda run_id, event: None)
    nb = Notebook(owner_id=owner.id, title="mon-ssrf-nb")
    db.add(nb)
    await db.flush()
    agent = Agent(
        name="mon-ssrf-agent",
        kind="workflow",
        definition={
            "nodes": [
                {"id": "c", "type": "constant", "value": "v"},
                {"id": "h", "type": "http", "url": "http://127.0.0.1:9/", "method": "GET"},
            ],
            "edges": [["c", "h"]],
        },
        params_schema={},
        scope="user",
        owner_id=owner.id,
        enabled=True,
    )
    db.add(agent)
    await db.flush()
    run = AgentRun(
        agent_id=agent.id,
        notebook_id=nb.id,
        user_id=owner.id,
        params={},
        status="queued",
        trigger="manual",
    )
    db.add(run)
    await db.commit()
    result = await agent_task._run(str(run.id))
    await db.refresh(run)
    return run, result


async def test_agent_run_logs_error_code_from_real_graph(
    client: httpx.AsyncClient, db, user_a, monkeypatch
):
    """실패 노드 로그의 `error_code` == `ssrf_blocked`, 성공 노드는 null(키는 있다)."""
    run, result = await _run_ssrf_workflow(db, user_a, monkeypatch)
    assert result["ok"] is False
    assert run.status == "failed"
    assert run.error.startswith("노드 [h] 실행 실패:")  # 그래프 안 노드 실패 — 코어 문구 그대로

    token = await login(client, user_a.email)
    res = await client.get(f"/api/v1/agent-runs/{run.id}/logs", headers=bearer(token))
    assert res.status_code == 200, res.text
    by_node = {row["node_id"]: row for row in res.json()}
    assert set(by_node) == {"c", "h"}
    assert by_node["c"]["status"] == "succeeded"
    assert "error_code" in by_node["c"] and by_node["c"]["error_code"] is None
    assert by_node["h"]["status"] == "failed"
    assert by_node["h"]["error_code"] == "ssrf_blocked"
    assert by_node["h"]["error_message"]


async def test_agent_run_logs_error_code_forbidden_for_unrelated_user(
    client: httpx.AsyncClient, db, user_a, user_b, monkeypatch
):
    """`error_code`가 생겨도 실행 로그 인가는 그대로다 — 무관 사용자 403, 본문에 코드 없음."""
    run, _ = await _run_ssrf_workflow(db, user_a, monkeypatch)

    token = await login(client, user_b.email)
    res = await client.get(f"/api/v1/agent-runs/{run.id}/logs", headers=bearer(token))
    assert res.status_code == 403
    assert "ssrf_blocked" not in res.text
