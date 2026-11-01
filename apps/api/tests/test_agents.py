"""에이전트 런타임/API 테스트 — 외부 의존(스토리지/임베딩/LLM)은 모킹."""

import uuid

import httpx
import pytest
from biznotes_core.services.agents.runtime import RunContext, execute_agent, kind_to_nodes
from biznotes_core.services.agents.templating import render, render_value
from biznotes_core.services.rag_chat import Citation

from tests.conftest import bearer, login

# ---------- 템플릿 ----------


def test_render_dotted_path():
    state = {"params": {"topic": "커피"}, "search": {"context": "본문"}}
    assert render("주제: {params.topic} / {search.context}", state) == "주제: 커피 / 본문"
    assert render("{missing.path}", state) == ""


def test_render_value_recursive():
    state = {"params": {"q": "검색어"}}
    out = render_value({"a": "{params.q}", "b": ["{params.q}", 1]}, state)
    assert out == {"a": "검색어", "b": ["검색어", 1]}


# ---------- kind 정규화 ----------


def test_kind_to_nodes_skill():
    nodes, edges = kind_to_nodes(
        "skill",
        {"prompt_template": "p {search.context}", "artifact_name": "out.md"},
    )
    types = [n["type"] for n in nodes]
    assert types == ["rag_search", "llm", "save_artifact"]
    assert ["search", "generate"] in edges


def test_kind_to_nodes_skill_no_rag():
    nodes, _ = kind_to_nodes("skill", {"prompt_template": "p", "use_rag": False})
    assert [n["type"] for n in nodes] == ["llm", "save_artifact"]


def test_kind_to_nodes_workflow_passthrough():
    definition = {"nodes": [{"id": "a", "type": "llm", "prompt": "x"}], "edges": []}
    nodes, edges = kind_to_nodes("workflow", definition)
    assert nodes == definition["nodes"]


# ---------- 런타임 실행 (LangGraph) ----------


@pytest.fixture
def _mock_runtime_deps(monkeypatch):
    artifacts: dict[str, bytes] = {}

    async def fake_retrieve(db, notebook_id, query, source_ids, top_k=6):
        from biznotes_core.services.rag_chat import RetrievalResult

        return RetrievalResult(
            citations=[
                Citation(
                    n=1,
                    source_id="s",
                    chunk_id="c",
                    quote=f"'{query}' 관련 발췌",
                    source_title="소스A",
                )
            ],
            insufficient_context=False,
        )

    monkeypatch.setattr("biznotes_core.services.rag_chat.retrieve_context", fake_retrieve)

    def fake_put(bucket, key, data, content_type="text/plain"):
        artifacts[key] = data

    monkeypatch.setattr("biznotes_core.services.storage.ensure_buckets", lambda: None)
    monkeypatch.setattr("biznotes_core.services.storage.put_object", fake_put)

    return artifacts


async def test_execute_skill_agent(db, _mock_runtime_deps, mock_llm):
    progress_events = []

    async def on_progress(node_id, done, total):
        progress_events.append((node_id, done, total))

    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={"topic": "에스프레소"},
        on_progress=on_progress,
    )
    summary = await execute_agent(
        ctx,
        "skill",
        {
            "rag_query": "{params.topic}",
            "prompt_template": "'{params.topic}' 보고서. 소스:\n{search.context}",
            "artifact_name": "report.md",
        },
    )
    assert summary  # llm 출력 요약
    assert len(ctx.artifact_keys) == 1
    assert ctx.artifact_keys[0]["name"] == "report.md"
    assert ctx.usage.get("total_tokens", 0) > 0
    # 진행률: 3개 노드
    assert [p[1] for p in progress_events] == [1, 2, 3]
    # 아티팩트 내용에 mock 응답 포함
    key = ctx.artifact_keys[0]["key"]
    content = _mock_runtime_deps[key].decode()
    assert "에스프레소" in content
    # 회귀 방지: 첫 노드 이후에도 {params.*} 치환이 유지되어야 한다 (LangGraph 상태 소실 버그)
    assert "'에스프레소' 보고서" in content


async def test_execute_workflow_agent(db, _mock_runtime_deps, mock_llm):
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={"topic": "커피"})
    summary = await execute_agent(
        ctx,
        "workflow",
        {
            "nodes": [
                {"id": "search", "type": "rag_search", "query": "{params.topic}"},
                {"id": "gen", "type": "llm", "prompt": "요약: {search.context}"},
                {
                    "id": "save",
                    "type": "save_artifact",
                    "name": "out.json",
                    "content": "{gen.text}",
                    "content_type": "application/json",
                },
            ],
            "edges": [["search", "gen"], ["gen", "save"]],
        },
    )
    assert summary
    assert ctx.artifact_keys[0]["name"] == "out.json"


async def test_workflow_cycle_detected(db, _mock_runtime_deps, mock_llm):
    from biznotes_core.services.agents.runtime import AgentRuntimeError

    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={})
    with pytest.raises(AgentRuntimeError, match="순환"):
        await execute_agent(
            ctx,
            "workflow",
            {
                "nodes": [
                    {"id": "a", "type": "llm", "prompt": "x"},
                    {"id": "b", "type": "llm", "prompt": "y"},
                ],
                "edges": [["a", "b"], ["b", "a"]],
            },
        )


# ---------- 입력 소스: source_load(full) + rag×selected ----------


async def _make_notebook_with_sources(db, owner_id, parsed):
    """parsed = [(title, body|None)] — body None이면 parsed_object_key 미설정(드롭/폴백 대상)."""
    from biznotes_core.models import Notebook, Source

    nb = Notebook(owner_id=owner_id, title="src-nb")
    db.add(nb)
    await db.flush()
    sources = []
    for i, (title, body) in enumerate(parsed):
        s = Source(
            notebook_id=nb.id,
            type="text",
            title=title,
            status="ready",
            parsed_object_key=f"parsed/{nb.id}/{i}.md" if body is not None else None,
        )
        db.add(s)
        sources.append(s)
    await db.commit()
    for s in sources:
        await db.refresh(s)
    return nb, sources


async def test_source_load_full_mode(db, user_a, monkeypatch):
    from biznotes_core.services.agents.runtime import RunContext, _exec_source_load

    nb, sources = await _make_notebook_with_sources(
        db, user_a.id, [("문서A", "A" * 100), ("문서B", "B" * 100)]
    )
    blobs = {f"parsed/{nb.id}/0.md": b"A" * 100, f"parsed/{nb.id}/1.md": b"B" * 100}
    monkeypatch.setattr("biznotes_core.services.storage.get_object", lambda bucket, key: blobs[key])

    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=nb.id, db=db, params={})
    out = await _exec_source_load(ctx, {"max_sources": 5, "max_chars": 60000}, {})
    assert "## 문서A" in out["context"] and "## 문서B" in out["context"]
    assert {s["title"] for s in out["sources"]} == {"문서A", "문서B"}
    assert out["dropped"] == []


async def test_source_load_truncates_and_reports(db, user_a, monkeypatch):
    from biznotes_core.services.agents.runtime import RunContext, _exec_source_load

    nb, _ = await _make_notebook_with_sources(
        db, user_a.id, [("문서A", "A" * 500), ("문서B", "B" * 500), ("문서C", "C" * 500)]
    )
    blobs = {
        f"parsed/{nb.id}/0.md": b"A" * 500,
        f"parsed/{nb.id}/1.md": b"B" * 500,
        f"parsed/{nb.id}/2.md": b"C" * 500,
    }
    monkeypatch.setattr("biznotes_core.services.storage.get_object", lambda bucket, key: blobs[key])

    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=nb.id, db=db, params={})
    # max_sources=2 → 1개 드롭, max_chars 작게 → 절단
    out = await _exec_source_load(ctx, {"max_sources": 2, "max_chars": 120}, {})
    reasons = {d["reason"] for d in out["dropped"]}
    assert "max_sources" in reasons  # 무음 절단 금지
    assert "⚠️" in out["context"]  # 컨텍스트 말미 드롭 표기
    assert len(out["context"]) < 400  # max_chars 절단 적용


async def test_source_load_scope_selected(db, user_a, monkeypatch):
    from biznotes_core.services.agents.runtime import RunContext, _exec_source_load

    nb, sources = await _make_notebook_with_sources(
        db, user_a.id, [("문서A", "A" * 50), ("문서B", "B" * 50)]
    )
    blobs = {f"parsed/{nb.id}/0.md": b"A" * 50, f"parsed/{nb.id}/1.md": b"B" * 50}
    monkeypatch.setattr("biznotes_core.services.storage.get_object", lambda bucket, key: blobs[key])

    # 문서A만 스코프
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=nb.id, db=db, params={}, source_ids=[sources[0].id]
    )
    out = await _exec_source_load(ctx, {"max_sources": 5, "max_chars": 60000}, {})
    assert [s["title"] for s in out["sources"]] == ["문서A"]
    assert "## 문서B" not in out["context"]


async def test_rag_search_passes_source_ids(db, monkeypatch):
    from biznotes_core.services.agents.runtime import RunContext, _exec_rag_search

    captured = {}

    async def fake_retrieve(db, notebook_id, query, source_ids, top_k=6):
        from biznotes_core.services.rag_chat import RetrievalResult

        captured["source_ids"] = source_ids
        return RetrievalResult(citations=[], insufficient_context=True)

    monkeypatch.setattr("biznotes_core.services.rag_chat.retrieve_context", fake_retrieve)
    sid = uuid.uuid4()
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, source_ids=[sid]
    )
    await _exec_rag_search(ctx, {"query": "q"}, {})
    assert captured["source_ids"] == [sid]


# ---------- API ----------


@pytest.fixture
async def seeded_agent(db):
    from biznotes_core.models import Agent

    agent = Agent(
        name=f"테스트 에이전트 {uuid.uuid4().hex[:6]}",
        kind="skill",
        icon="🧪",
        definition={"prompt_template": "p", "use_rag": False},
        params_schema={
            "type": "object",
            "properties": {"topic": {"type": "string", "minLength": 1}},
            "required": ["topic"],
        },
        scope="system",
        enabled=True,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


@pytest.fixture(autouse=True)
def _mock_celery(monkeypatch):
    class FakeTask:
        id = "fake-task"

    monkeypatch.setattr(
        "app.workers.tasks.agent.run_agent.apply_async",
        lambda *a, **k: FakeTask(),
    )


async def test_list_agents(client: httpx.AsyncClient, user_a, seeded_agent):
    token = await login(client, user_a.email)
    res = await client.get("/api/v1/agents?kind=skill", headers=bearer(token))
    assert res.status_code == 200
    assert any(a["id"] == str(seeded_agent.id) for a in res.json())


async def test_create_run_validates_params(client: httpx.AsyncClient, user_a, seeded_agent):
    token = await login(client, user_a.email)
    nb = await client.post("/api/v1/notebooks", json={"title": "agent-nb"}, headers=bearer(token))
    nb_id = nb.json()["id"]

    # required 누락 → 422
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/agent-runs",
        json={"agent_id": str(seeded_agent.id), "params": {}},
        headers=bearer(token),
    )
    assert res.status_code == 422
    assert "topic" in res.json()["detail"]

    # 정상 → 202 queued
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/agent-runs",
        json={"agent_id": str(seeded_agent.id), "params": {"topic": "주제"}},
        headers=bearer(token),
    )
    assert res.status_code == 202
    run = res.json()
    assert run["status"] == "queued"
    assert run["agent_name"] == seeded_agent.name

    # 목록 조회
    res = await client.get(f"/api/v1/notebooks/{nb_id}/agent-runs", headers=bearer(token))
    assert len(res.json()) == 1


async def test_create_run_persists_source_ids(
    client: httpx.AsyncClient, user_a, seeded_agent
):
    token = await login(client, user_a.email)
    nb = await client.post("/api/v1/notebooks", json={"title": "scope-nb"}, headers=bearer(token))
    nb_id = nb.json()["id"]
    sid = str(uuid.uuid4())

    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/agent-runs",
        json={"agent_id": str(seeded_agent.id), "params": {"topic": "주제"}, "source_ids": [sid]},
        headers=bearer(token),
    )
    assert res.status_code == 202
    run = res.json()
    assert run["input_source_ids"] == [sid]

    # 단건 조회에서도 스코프 핀 보존(영속 라운드트립)
    detail = await client.get(f"/api/v1/agent-runs/{run['id']}", headers=bearer(token))
    assert detail.json()["input_source_ids"] == [sid]


async def test_cancel_run(client: httpx.AsyncClient, user_a, seeded_agent):
    token = await login(client, user_a.email)
    nb = await client.post("/api/v1/notebooks", json={"title": "cancel-nb"}, headers=bearer(token))
    nb_id = nb.json()["id"]
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/agent-runs",
        json={"agent_id": str(seeded_agent.id), "params": {"topic": "x"}},
        headers=bearer(token),
    )
    run_id = res.json()["id"]

    res = await client.post(f"/api/v1/agent-runs/{run_id}/cancel", headers=bearer(token))
    assert res.status_code == 200
    assert res.json()["status"] == "cancelled"

    # 이미 종료된 실행 재취소 → 409
    res = await client.post(f"/api/v1/agent-runs/{run_id}/cancel", headers=bearer(token))
    assert res.status_code == 409


async def test_run_invisible_to_other_user(client: httpx.AsyncClient, user_a, user_b, seeded_agent):
    token_a = await login(client, user_a.email)
    nb = await client.post("/api/v1/notebooks", json={"title": "priv-nb"}, headers=bearer(token_a))
    nb_id = nb.json()["id"]
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/agent-runs",
        json={"agent_id": str(seeded_agent.id), "params": {"topic": "x"}},
        headers=bearer(token_a),
    )
    run_id = res.json()["id"]

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    res = await client.get(f"/api/v1/agent-runs/{run_id}", headers=bearer(token_b))
    assert res.status_code == 404


# ---------- 비-스킬 에이전트 목록·수동 실행 인가 (계획 AC-34, P0-01 / BL-103) ----------


async def _published_user_workflow(db, owner_id):
    from biznotes_core.models import Agent

    agent = Agent(
        name=f"남의 워크플로 {uuid.uuid4().hex[:6]}",
        kind="workflow",
        icon="⚙️",
        definition={
            "nodes": [
                {"id": "r", "type": "react_agent", "goal": "x", "cost_budget_usd": 3.5}
            ],
            "edges": [],
        },
        params_schema={"type": "object", "properties": {"secret_param": {"type": "string"}}},
        scope="user",
        owner_id=owner_id,
        enabled=True,  # 게시됨
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


async def test_t26_list_agents_hides_other_users_published_workflow(
    client: httpx.AsyncClient, db, user_a, user_b, admin_user, seeded_agent
):
    """T-26: user_b의 `GET /agents`에 user_a의 게시 user-scope 워크플로가 없다(admin·소유자는 본다).

    코어 `authz.can_list_agent` = (system ∨ 소유자 ∨ admin) ∧ enabled ∧ wrapper 아님.
    """
    wf = await _published_user_workflow(db, user_a.id)

    token_b = await login(client, user_b.email)
    res = await client.get("/api/v1/agents", headers=bearer(token_b))
    assert res.status_code == 200
    ids = {a["id"] for a in res.json()}
    assert str(wf.id) not in ids
    assert str(seeded_agent.id) in ids  # system 정의는 계속 보인다
    assert "secret_param" not in res.text  # 남의 params_schema가 새지 않는다

    for viewer in (user_a, admin_user):
        client.cookies.clear()
        token = await login(client, viewer.email)
        res = await client.get("/api/v1/agents?kind=workflow", headers=bearer(token))
        assert str(wf.id) in {a["id"] for a in res.json()}, viewer.role


async def test_t27_other_user_cannot_run_published_user_workflow(
    client: httpx.AsyncClient, db, user_a, user_b
):
    """T-27: user_b가 user_a의 게시 워크플로로 실행 요청 → 404, run·사용량 차감 없음.

    인가는 코어 `policy.assert_agent_execution_allowed` 단일 관문이다(mock하지 않는다).
    """
    from biznotes_core.models import AgentRun
    from biznotes_core.services.usage_guard import get_today_usage
    from sqlalchemy import func, select

    wf = await _published_user_workflow(db, user_a.id)
    token_b = await login(client, user_b.email)
    nb = await client.post("/api/v1/notebooks", json={"title": "b-nb"}, headers=bearer(token_b))
    before = (await get_today_usage(user_b.id))["agent_runs"]

    res = await client.post(
        f"/api/v1/notebooks/{nb.json()['id']}/agent-runs",
        json={"agent_id": str(wf.id), "params": {}},
        headers=bearer(token_b),
    )

    assert res.status_code == 404, res.text
    count_runs = select(func.count()).select_from(AgentRun).where(AgentRun.agent_id == wf.id)
    runs = (await db.execute(count_runs)).scalar_one()
    assert runs == 0
    assert (await get_today_usage(user_b.id))["agent_runs"] == before

    # 같은 정의를 소유자는 실행할 수 있다(양성 대조 — 404가 정의 결함이 아님)
    client.cookies.clear()
    token_a = await login(client, user_a.email)
    nb_a = await client.post("/api/v1/notebooks", json={"title": "a-nb"}, headers=bearer(token_a))
    own = await client.post(
        f"/api/v1/notebooks/{nb_a.json()['id']}/agent-runs",
        json={"agent_id": str(wf.id), "params": {}},
        headers=bearer(token_a),
    )
    assert own.status_code == 202, own.text


async def test_owner_can_test_run_own_draft_workflow(client: httpx.AsyncClient, db, user_a, user_b):
    """단일 관문이 소유자 draft 테스트 실행(`can_run_owner_draft`)은 유지한다 — 타인은 404."""
    wf = await _published_user_workflow(db, user_a.id)
    wf.enabled = False
    await db.commit()

    token_a = await login(client, user_a.email)
    nb_a = await client.post("/api/v1/notebooks", json={"title": "a-nb"}, headers=bearer(token_a))
    own = await client.post(
        f"/api/v1/notebooks/{nb_a.json()['id']}/agent-runs",
        json={"agent_id": str(wf.id), "params": {}},
        headers=bearer(token_a),
    )
    assert own.status_code == 202, own.text

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    nb_b = await client.post("/api/v1/notebooks", json={"title": "b-nb"}, headers=bearer(token_b))
    other = await client.post(
        f"/api/v1/notebooks/{nb_b.json()['id']}/agent-runs",
        json={"agent_id": str(wf.id), "params": {}},
        headers=bearer(token_b),
    )
    assert other.status_code == 404
