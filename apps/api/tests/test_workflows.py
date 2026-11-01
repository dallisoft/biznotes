"""워크플로우 비주얼 에디터 — agent_call 런타임 + 사용자 워크플로우 API 테스트."""

import json
import uuid
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from biznotes_core.services.agents.runtime import (
    MAX_AGENT_DEPTH,
    AgentRuntimeError,
    RunContext,
    _exec_agent_call,
    _exec_citation_verify,
    _exec_json_validate,
    agent_visible_to,
    execute_agent,
)

from app.schemas.agent import WorkflowCreate, WorkflowOut
from tests.conftest import bearer, login

# ---------- 런타임 의존 모킹 (test_agents 패턴 재사용) ----------


async def _make_workflow_agent(db, owner_id, *, scope="user", enabled=True, definition=None):
    from biznotes_core.models import Agent

    agent = Agent(
        name=f"wf-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        icon="⚙️",
        definition=definition
        or {
            "nodes": [
                {"id": "gen", "type": "llm", "prompt": "sub: {params.x}"},
                {
                    "id": "save",
                    "type": "save_artifact",
                    "name": "sub.md",
                    "content": "{gen.text}",
                    "content_type": "text/markdown",
                },
            ],
            "edges": [["gen", "save"]],
        },
        params_schema={},
        scope=scope,
        owner_id=owner_id,
        enabled=enabled,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


# ---------- agent_call 런타임 ----------


def test_agent_visible_to_rules():
    class A:
        def __init__(self, enabled, scope, owner_id):
            self.enabled = enabled
            self.scope = scope
            self.owner_id = owner_id

    uid = uuid.uuid4()
    assert agent_visible_to(A(True, "system", None), uid) is True
    assert agent_visible_to(A(False, "system", None), uid) is False  # disabled
    assert agent_visible_to(A(True, "user", uid), uid) is True  # owner
    assert agent_visible_to(A(True, "user", uuid.uuid4()), uid) is False  # 타인 소유
    assert agent_visible_to(A(True, "user", uuid.uuid4()), uid, "admin") is True  # admin


def test_workflow_schema_preserves_node_specific_properties():
    """C1 정본은 DAG 외곽만 좁히며 노드별 런타임 속성은 보존한다."""
    body = WorkflowCreate(
        definition={
            "nodes": [{"id": "llm", "type": "llm", "prompt": "{params.topic}"}],
            "edges": [],
        }
    )

    assert body.definition.model_dump() == {
        "nodes": [{"id": "llm", "type": "llm", "prompt": "{params.topic}"}],
        "edges": [],
    }


def test_workflow_out_reads_legacy_jsonb_without_edges():
    """C1 read compatibility: old JSONB definition may omit defaulted outer keys."""
    legacy_agent = SimpleNamespace(
        id=uuid.uuid4(), name="legacy", description="", icon="⚙️", kind="workflow",
        definition={"nodes": [{"id": "llm", "type": "llm", "prompt": "x"}]},
        params_schema={}, scope="user", owner_id=None, enabled=False, version=1,
        created_at=datetime.now(), updated_at=datetime.now(),
    )

    assert WorkflowOut.model_validate(legacy_agent).definition.edges == []


async def test_agent_call_executes_subagent(db, user_a, _mock_runtime_deps, mock_llm):
    target = await _make_workflow_agent(db, user_a.id, scope="system")
    caller_def = {
        "nodes": [
            {
                "id": "call",
                "type": "agent_call",
                "agent_id": str(target.id),
                "params": {"x": "{params.topic}"},
            }
        ],
        "edges": [],
    }
    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={"topic": "에스프레소"},
        user_id=user_a.id,
        agent_id=uuid.uuid4(),  # caller 자신
    )
    summary = await execute_agent(ctx, "workflow", caller_def)
    assert summary  # 서브에이전트 llm 출력 요약
    # 서브에이전트 아티팩트가 부모 ctx에 누적
    assert any(a["name"] == "sub.md" for a in ctx.artifact_keys)


async def test_agent_call_self_reference_blocked(db, user_a, _mock_runtime_deps, mock_llm):
    target = await _make_workflow_agent(db, user_a.id, scope="system")
    caller_def = {
        "nodes": [{"id": "call", "type": "agent_call", "agent_id": str(target.id)}],
        "edges": [],
    }
    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=user_a.id,
        agent_id=target.id,  # 자기 자신을 호출
    )
    with pytest.raises(AgentRuntimeError, match="순환"):
        await execute_agent(ctx, "workflow", caller_def)


async def test_agent_call_depth_limit(db, user_a, _mock_runtime_deps, mock_llm):
    target = await _make_workflow_agent(db, user_a.id, scope="system")
    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=user_a.id,
        agent_id=uuid.uuid4(),
        depth=MAX_AGENT_DEPTH,
    )
    with pytest.raises(AgentRuntimeError, match="한도"):
        await _exec_agent_call(ctx, {"type": "agent_call", "agent_id": str(target.id)}, {})


async def test_agent_call_invisible_target_blocked(db, user_a, user_b):
    # user_b 소유 private(user scope) 워크플로우는 user_a가 호출 불가
    target = await _make_workflow_agent(db, user_b.id, scope="user")
    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=user_a.id,
        agent_id=uuid.uuid4(),
    )
    with pytest.raises(AgentRuntimeError, match="권한"):
        await _exec_agent_call(ctx, {"type": "agent_call", "agent_id": str(target.id)}, {})


# ---------- 워크플로우 API ----------

_SIMPLE_DEF = {
    "nodes": [{"id": "gen", "type": "llm", "prompt": "{params.topic}"}],
    "edges": [],
}


async def test_workflow_crud_lifecycle(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)

    # 생성 → draft(enabled=false), owner, scope=user, version=1
    res = await client.post(
        "/api/v1/workflows",
        json={"name": "내 워크플로우", "definition": _SIMPLE_DEF},
        headers=bearer(token),
    )
    assert res.status_code == 201, res.text
    wf = res.json()
    assert wf["enabled"] is False
    assert wf["scope"] == "user"
    assert wf["version"] == 1
    assert wf["can_edit"] is True
    wf_id = wf["id"]

    # 목록에 노출
    res = await client.get("/api/v1/workflows", headers=bearer(token))
    assert any(w["id"] == wf_id for w in res.json())

    # 저장(PATCH) → version++
    res = await client.patch(
        f"/api/v1/workflows/{wf_id}",
        json={"name": "수정됨"},
        headers=bearer(token),
    )
    assert res.status_code == 200
    assert res.json()["version"] == 2
    assert res.json()["name"] == "수정됨"

    # 게시 → enabled=true
    res = await client.post(f"/api/v1/workflows/{wf_id}/publish", headers=bearer(token))
    assert res.status_code == 200
    assert res.json()["enabled"] is True

    # 게시 취소
    res = await client.post(f"/api/v1/workflows/{wf_id}/unpublish", headers=bearer(token))
    assert res.json()["enabled"] is False

    # 삭제
    res = await client.delete(f"/api/v1/workflows/{wf_id}", headers=bearer(token))
    assert res.status_code == 204


async def test_workflow_invalid_definition_rejected(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)
    # 사이클 정의 → 422
    res = await client.post(
        "/api/v1/workflows",
        json={
            "name": "cycle",
            "definition": {
                "nodes": [
                    {"id": "a", "type": "llm", "prompt": "x"},
                    {"id": "b", "type": "llm", "prompt": "y"},
                ],
                "edges": [["a", "b"], ["b", "a"]],
            },
        },
        headers=bearer(token),
    )
    assert res.status_code == 422


async def test_workflow_ownership_enforced(client: httpx.AsyncClient, user_a, user_b):
    token_a = await login(client, user_a.email)
    res = await client.post(
        "/api/v1/workflows",
        json={"name": "private", "definition": _SIMPLE_DEF},
        headers=bearer(token_a),
    )
    wf_id = res.json()["id"]

    # user_b는 user_a의 draft 워크플로우를 볼 수 없음 (404)
    client.cookies.clear()
    token_b = await login(client, user_b.email)
    res = await client.get(f"/api/v1/workflows/{wf_id}", headers=bearer(token_b))
    assert res.status_code == 404


async def test_workflow_duplicate(client: httpx.AsyncClient, user_a, user_b, db):
    # user_b가 만든 system 워크플로우를 user_a가 복제
    from biznotes_core.models import Agent

    src = Agent(
        name="공용 워크플로우",
        kind="workflow",
        definition=_SIMPLE_DEF,
        params_schema={},
        scope="system",
        enabled=True,
    )
    db.add(src)
    await db.commit()
    await db.refresh(src)

    token_a = await login(client, user_a.email)
    res = await client.post(f"/api/v1/workflows/{src.id}/duplicate", headers=bearer(token_a))
    assert res.status_code == 200, res.text
    clone = res.json()
    assert clone["id"] != str(src.id)
    assert clone["scope"] == "user"
    assert clone["enabled"] is False
    assert "복사본" in clone["name"]


async def test_workflow_agent_call_validation(client: httpx.AsyncClient, user_a, db):
    token = await login(client, user_a.email)
    target = await _make_workflow_agent(db, user_a.id, scope="system")

    # 유효한 agent_call → 201
    res = await client.post(
        "/api/v1/workflows",
        json={
            "name": "caller",
            "definition": {
                "nodes": [{"id": "call", "type": "agent_call", "agent_id": str(target.id)}],
                "edges": [],
            },
        },
        headers=bearer(token),
    )
    assert res.status_code == 201, res.text

    # 존재하지 않는 대상 → 422
    res = await client.post(
        "/api/v1/workflows",
        json={
            "name": "bad-caller",
            "definition": {
                "nodes": [{"id": "call", "type": "agent_call", "agent_id": str(uuid.uuid4())}],
                "edges": [],
            },
        },
        headers=bearer(token),
    )
    assert res.status_code == 422


async def test_workflow_callable_list(client: httpx.AsyncClient, user_a, db):
    token = await login(client, user_a.email)
    target = await _make_workflow_agent(db, user_a.id, scope="system")
    res = await client.get("/api/v1/workflows/callable", headers=bearer(token))
    assert res.status_code == 200
    assert any(a["id"] == str(target.id) for a in res.json())


async def test_workflow_condition_execution(db, user_a, _mock_runtime_deps):
    # 조건 노드를 활용한 워크플로우 정의
    # params.score > 80 이면 high.md를 저장하고, 그렇지 않으면 low.md를 저장하는 분기 흐름
    wf_definition = {
        "nodes": [
            {
                "id": "cond",
                "type": "condition",
                "left": "{params.score}",
                "op": ">",
                "right": "80",
                "true_node": "high_score",
                "false_node": "low_score",
            },
            {
                "id": "high_score",
                "type": "save_artifact",
                "name": "high.md",
                "content": "Excellent score: {params.score}",
            },
            {
                "id": "low_score",
                "type": "save_artifact",
                "name": "low.md",
                "content": "Try again score: {params.score}",
            },
        ],
        "edges": [["cond", "high_score"], ["cond", "low_score"]],
    }

    # 케이스 1: score가 90일 때 (참 분기 실행)
    ctx_high = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={"score": "90"},
        user_id=user_a.id,
    )
    await execute_agent(ctx_high, "workflow", wf_definition)

    # high.md는 있고, low.md는 없어야 함
    assert any(a["name"] == "high.md" for a in ctx_high.artifact_keys)
    assert not any(a["name"] == "low.md" for a in ctx_high.artifact_keys)

    # 케이스 2: score가 50일 때 (거짓 분기 실행)
    ctx_low = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={"score": "50"},
        user_id=user_a.id,
    )
    await execute_agent(ctx_low, "workflow", wf_definition)

    # low.md는 있고, high.md는 없어야 함
    assert any(a["name"] == "low.md" for a in ctx_low.artifact_keys)
    assert not any(a["name"] == "high.md" for a in ctx_low.artifact_keys)


async def test_workflow_parallel_execution(db, user_a, _mock_runtime_deps):
    # start -> branch_a -> merge
    # start -> branch_b -> merge
    wf_definition = {
        "nodes": [
            {
                "id": "start",
                "type": "constant",
                "value": {"value": "start_val"},
            },
            {
                "id": "branch_a",
                "type": "constant",
                "value": {"value": "a_val"},
            },
            {
                "id": "branch_b",
                "type": "constant",
                "value": {"value": "b_val"},
            },
            {
                "id": "merge",
                "type": "save_artifact",
                "name": "merged.md",
                "content": "{branch_a.value} + {branch_b.value}",
            },
        ],
        "edges": [
            ["start", "branch_a"],
            ["start", "branch_b"],
            ["branch_a", "merge"],
            ["branch_b", "merge"],
        ],
    }

    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=user_a.id,
    )
    await execute_agent(ctx, "workflow", wf_definition)

    # merged.md가 성공적으로 쓰였고, 병합된 값이 정상적이어야 함
    assert any(a["name"] == "merged.md" for a in ctx.artifact_keys)


async def test_workflow_retry_and_error_handling(db, user_a, _mock_runtime_deps):
    # 케이스 1: 실패하고 continue_on_error=False인 경우 -> 에러로 중단됨
    bad_wf_1 = {
        "nodes": [
            {
                "id": "bad_call",
                "type": "agent_call",
                "agent_id": str(uuid.uuid4()),  # 존재하지 않는 ID로 무조건 실패
                "retry_policy": {"max_retries": 1, "delay": 0.01},
                "continue_on_error": False,
            }
        ],
        "edges": [],
    }

    ctx1 = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=user_a.id,
    )
    with pytest.raises(AgentRuntimeError, match="실패"):
        await execute_agent(ctx1, "workflow", bad_wf_1)

    # 케이스 2: 실패하고 continue_on_error=True인 경우 -> 에러 전파 방지되어 계속 실행
    bad_wf_2 = {
        "nodes": [
            {
                "id": "bad_call",
                "type": "agent_call",
                "agent_id": str(uuid.uuid4()),
                "retry_policy": {"max_retries": 1, "delay": 0.01},
                "continue_on_error": True,
            },
            {
                "id": "check_error",
                "type": "condition",
                "left": "{bad_call.failed}",
                "op": "==",
                "right": "True",
                "true_node": "handle_error",
                "false_node": "do_nothing",
            },
            {
                "id": "handle_error",
                "type": "save_artifact",
                "name": "error_log.txt",
                "content": "Node failed as expected: {bad_call.error}",
            },
            {
                "id": "do_nothing",
                "type": "constant",
                "value": "ok",
            },
        ],
        "edges": [
            ["bad_call", "check_error"],
            ["check_error", "handle_error"],
            ["check_error", "do_nothing"],
        ],
    }

    ctx2 = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=user_a.id,
    )
    # 중단 없이 실행 완료되어야 함
    await execute_agent(ctx2, "workflow", bad_wf_2)

    # handle_error 가 정상 작동하여 error_log.txt가 저장되었어야 함
    assert any(a["name"] == "error_log.txt" for a in ctx2.artifact_keys)


async def test_workflow_loop_execution(db, user_a, _mock_runtime_deps):
    # 1. 루프 안에서 호출할 대상 에이전트(스킬) 생성
    # 입력받은 params.item 이름을 따서 아티팩트를 저장하는 에이전트
    loop_body_agent = await _make_workflow_agent(
        db,
        user_a.id,
        scope="system",
        definition={
            "nodes": [
                {
                    "id": "saver",
                    "type": "save_artifact",
                    "name": "{params.item}.md",
                    "content": "Content for {params.item}",
                }
            ],
            "edges": [],
        },
    )

    # 2. 루프 노드를 포함하는 마스터 워크플로우 정의
    wf_definition = {
        "nodes": [
            {
                "id": "loop_node",
                "type": "loop",
                "items": "{params.fruits}",
                "item_var": "item",
                "loop_body_agent_id": str(loop_body_agent.id),
                "parallel": True,
            }
        ],
        "edges": [],
    }

    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={"fruits": ["apple", "banana", "cherry"]},
        user_id=user_a.id,
    )

    await execute_agent(ctx, "workflow", wf_definition)

    # 루프를 돌면서 apple.md, banana.md, cherry.md가 모두 아티팩트로 저장되었는지 검증
    assert any(a["name"] == "apple.md" for a in ctx.artifact_keys)
    assert any(a["name"] == "banana.md" for a in ctx.artifact_keys)
    assert any(a["name"] == "cherry.md" for a in ctx.artifact_keys)


async def _make_run(db, user_a, definition, *, kind="workflow"):
    """ExecutionLog FK를 만족하는 실제 Notebook/Agent/AgentRun 생성."""
    from biznotes_core.models import Agent, AgentRun, Notebook

    notebook = Notebook(owner_id=user_a.id, title="log-test-nb")
    db.add(notebook)
    await db.flush()
    agent = Agent(
        name="log-test-workflow",
        kind=kind,
        definition=definition,
        params_schema={},
        scope="user",
        owner_id=user_a.id,
    )
    db.add(agent)
    await db.flush()
    run = AgentRun(
        agent_id=agent.id,
        notebook_id=notebook.id,
        user_id=user_a.id,
        params={},
        status="running",
    )
    db.add(run)
    await db.commit()
    return notebook, agent, run


async def test_workflow_execution_logs_recording(db, user_a, _mock_runtime_deps):
    from biznotes_core.models import ExecutionLog
    from sqlalchemy import select

    definition = {
        "nodes": [
            {"id": "node_1", "type": "constant", "value": {"value": "initial"}},
            {
                "id": "node_2",
                "type": "save_artifact",
                "name": "log_output.txt",
                "content": "{node_1.value}",
            },
        ],
        "edges": [["node_1", "node_2"]],
    }
    notebook, agent, run = await _make_run(db, user_a, definition)

    ctx = RunContext(
        run_id=run.id,
        notebook_id=notebook.id,
        db=db,
        params={},
        user_id=user_a.id,
    )
    await execute_agent(ctx, "workflow", definition)

    # execution_logs 는 격리 세션에서 커밋되므로 새 트랜잭션으로 조회
    await db.commit()
    logs = list(
        (
            await db.execute(
                select(ExecutionLog)
                .where(ExecutionLog.run_id == run.id)
                .order_by(ExecutionLog.started_at)
            )
        )
        .scalars()
        .all()
    )

    assert len(logs) == 2

    log1 = next(log for log in logs if log.node_id == "node_1")
    assert log1.node_type == "constant"
    assert log1.status == "succeeded"
    assert log1.started_at is not None and log1.finished_at is not None
    assert log1.started_at <= log1.finished_at
    assert log1.duration_ms is not None and log1.duration_ms >= 0

    log2 = next(log for log in logs if log.node_id == "node_2")
    assert log2.node_type == "save_artifact"
    assert log2.status == "succeeded"
    # content(템플릿 본문)는 로그에서 제외, name은 보존
    assert log2.input_data == {"name": "log_output.txt"}
    assert log2.output_data == {"key": f"{run.id}/log_output.txt"}


async def test_workflow_execution_logs_token_and_cost(db, user_a, _mock_runtime_deps):
    """llm 노드 실행 시 token_usage / cost_usd 가 노드 단위로 영속화되는지 검증."""
    from biznotes_core.models import ExecutionLog, LLMModel, LLMProvider
    from sqlalchemy import select

    # cost_per_1k 가 설정된 mock 프로바이더 — 결정적 비용 계산
    provider = LLMProvider(name=f"mock-cost-{uuid.uuid4().hex[:6]}", type="mock", enabled=True)
    db.add(provider)
    await db.flush()
    model = LLMModel(
        provider_id=provider.id,
        model_name="mock-cost",
        display_name="Mock Cost",
        capabilities=["llm"],
        is_default=False,
        enabled=True,
        cost_per_1k={"input": 1.0, "output": 2.0},
    )
    db.add(model)
    await db.commit()

    # model_id로 모델 고정 — 전체 스위트의 다른 default 모델에 영향받지 않도록.
    definition = {
        "nodes": [
            {
                "id": "ctx",
                "type": "constant",
                "value": {"context": "[1] 테스트 컨텍스트 문장입니다."},
            },
            {
                "id": "gen",
                "type": "llm",
                "model_id": str(model.id),
                "prompt": "다음을 요약하라: {ctx.context}",
            },
        ],
        "edges": [["ctx", "gen"]],
    }
    notebook, agent, run = await _make_run(db, user_a, definition)

    ctx = RunContext(run_id=run.id, notebook_id=notebook.id, db=db, params={}, user_id=user_a.id)
    await execute_agent(ctx, "workflow", definition)
    await db.commit()

    logs = list(
        (await db.execute(select(ExecutionLog).where(ExecutionLog.run_id == run.id)))
        .scalars()
        .all()
    )
    gen_log = next(log for log in logs if log.node_id == "gen")
    # mock 스트림은 usage(prompt/completion/total)를 채운다
    assert gen_log.token_usage is not None
    assert gen_log.token_usage["total_tokens"] > 0
    # cost = prompt/1k * 1.0 + completion/1k * 2.0 > 0
    assert gen_log.cost_usd is not None
    assert float(gen_log.cost_usd) > 0

    # constant 노드는 토큰을 발생시키지 않음
    ctx_log = next(log for log in logs if log.node_id == "ctx")
    assert ctx_log.token_usage is None
    assert ctx_log.cost_usd is None


# ---------- Phase B: sub_workflow / error_handler 노드 ----------


async def test_sub_workflow_node_input_output_mapping(db, user_a, _mock_runtime_deps):
    """sub_workflow 노드 — input_map으로 부모 상태를 대상 params에 매핑, output_key 노출."""
    sub = await _make_workflow_agent(
        db,
        user_a.id,
        scope="system",
        definition={
            "nodes": [
                {
                    "id": "save",
                    "type": "save_artifact",
                    "name": "sub-{params.tag}.txt",
                    "content": "tag={params.tag}",
                }
            ],
            "edges": [],
        },
    )

    caller_def = {
        "nodes": [
            {
                "id": "sw",
                "type": "sub_workflow",
                "workflow_id": str(sub.id),
                "input_map": {"tag": "{params.label}"},
                "output_key": "sub_out",
            }
        ],
        "edges": [],
    }
    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={"label": "X"},
        user_id=user_a.id,
        agent_id=uuid.uuid4(),
    )
    summary = await execute_agent(ctx, "workflow", caller_def)
    # 서브워크플로우가 input_map의 tag=X로 아티팩트를 저장
    assert any(a["name"] == "sub-X.txt" for a in ctx.artifact_keys)
    assert summary  # 서브워크플로우 요약


async def test_error_handler_recovers_with_fallback(db, user_a, _mock_runtime_deps):
    """보호 대상이 실패하면 fallback 에이전트로 복구 → recovered, fallback 산출물 생성."""
    # 보호 대상: 존재하지 않는 에이전트를 호출해 무조건 실패하는 워크플로우
    protected = await _make_workflow_agent(
        db,
        user_a.id,
        scope="system",
        definition={
            "nodes": [{"id": "bad", "type": "agent_call", "agent_id": str(uuid.uuid4())}],
            "edges": [],
        },
    )
    # 폴백: 아티팩트를 저장하는 워크플로우 (LLM 불필요, 결정적)
    fallback = await _make_workflow_agent(
        db,
        user_a.id,
        scope="system",
        definition={
            "nodes": [
                {"id": "c", "type": "constant", "value": {"value": "recovered"}},
                {
                    "id": "s",
                    "type": "save_artifact",
                    "name": "fallback.txt",
                    "content": "{c.value}",
                },
            ],
            "edges": [["c", "s"]],
        },
    )

    caller_def = {
        "nodes": [
            {
                "id": "eh",
                "type": "error_handler",
                "protected_agent_id": str(protected.id),
                "fallback_agent_id": str(fallback.id),
                "max_retries": 1,
                "backoff": 0.01,
            }
        ],
        "edges": [],
    }
    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=user_a.id,
        agent_id=uuid.uuid4(),
    )
    await execute_agent(ctx, "workflow", caller_def)
    # 폴백이 실행되어 산출물이 생성됨
    assert any(a["name"] == "fallback.txt" for a in ctx.artifact_keys)


async def test_error_handler_raises_without_fallback(db, user_a, _mock_runtime_deps):
    """보호 대상이 실패하고 폴백이 없으면 에러로 전파."""
    protected = await _make_workflow_agent(
        db,
        user_a.id,
        scope="system",
        definition={
            "nodes": [{"id": "bad", "type": "agent_call", "agent_id": str(uuid.uuid4())}],
            "edges": [],
        },
    )
    caller_def = {
        "nodes": [
            {
                "id": "eh",
                "type": "error_handler",
                "protected_agent_id": str(protected.id),
                "max_retries": 1,
                "backoff": 0.01,
            }
        ],
        "edges": [],
    }
    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=user_a.id,
        agent_id=uuid.uuid4(),
    )
    with pytest.raises(AgentRuntimeError, match="실행 실패"):
        await execute_agent(ctx, "workflow", caller_def)


async def test_llm_tool_calling_loop(db, user_a, _mock_runtime_deps):
    """llm 노드 tools 옵션 — 모델이 도구를 호출 → 실행 → 결과 주입 → 최종 텍스트."""
    from biznotes_core.models import LLMModel, LLMProvider

    provider = LLMProvider(name=f"mock-tool-{uuid.uuid4().hex[:6]}", type="mock", enabled=True)
    db.add(provider)
    await db.flush()
    model = LLMModel(
        provider_id=provider.id,
        model_name="mock-tool",
        display_name="Mock Tool",
        capabilities=["llm"],
        is_default=False,
        enabled=True,
    )
    db.add(model)
    await db.commit()

    definition = {
        "nodes": [
            {
                "id": "gen",
                "type": "llm",
                "model_id": str(model.id),
                "prompt": "노트북에서 커피 추출에 대해 조사해줘",
                "tools": ["rag_search"],
            },
            {
                "id": "save",
                "type": "save_artifact",
                "name": "answer.md",
                "content": "{gen.text}",
            },
        ],
        "edges": [["gen", "save"]],
    }
    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=user_a.id,
    )
    summary = await execute_agent(ctx, "workflow", definition)
    # mock 루프: 1턴 rag_search 호출 → (검색 결과 없음) → 2턴 도구 결과 종합 응답
    assert "도구 결과를 종합" in summary
    # 도구 실행 결과(검색 결과 없음 메시지)가 최종 응답에 반영됨
    assert "관련 발췌를 찾지 못했습니다" in summary
    assert any(a["name"] == "answer.md" for a in ctx.artifact_keys)


async def test_complete_with_tools_mock_turn(db):
    """게이트웨이 complete_with_tools — mock 프로바이더 결정적 턴 동작 단위 검증."""
    from biznotes_core.models import LLMModel, LLMProvider
    from biznotes_core.services.gateway.llm import complete_with_tools

    provider = LLMProvider(name=f"mock-ct-{uuid.uuid4().hex[:6]}", type="mock", enabled=True)
    db.add(provider)
    await db.flush()
    model = LLMModel(
        provider_id=provider.id,
        model_name="mock-ct",
        display_name="Mock CT",
        capabilities=["llm"],
        is_default=False,
        enabled=True,
    )
    db.add(model)
    await db.commit()

    tools = [
        {
            "type": "function",
            "function": {
                "name": "rag_search",
                "description": "검색",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
        }
    ]
    # 1턴: 도구 결과가 없으므로 도구 호출을 반환
    turn1 = await complete_with_tools(db, model.id, [{"role": "user", "content": "질문"}], tools)
    assert turn1.tool_calls and turn1.tool_calls[0].name == "rag_search"
    assert turn1.content == ""

    # 2턴: 도구 결과가 대화에 있으면 최종 텍스트 반환
    msgs = [
        {"role": "user", "content": "질문"},
        turn1.assistant_message,
        {
            "role": "tool",
            "tool_call_id": turn1.tool_calls[0].id,
            "name": "rag_search",
            "content": "검색결과 본문",
        },
    ]
    turn2 = await complete_with_tools(db, model.id, msgs, tools)
    assert not turn2.tool_calls
    assert "검색결과 본문" in turn2.content


async def test_error_handler_validation_rejects_invisible_target(
    client: httpx.AsyncClient, user_a, user_b, db
):
    """error_handler protected_agent_id 가 호출 불가 대상이면 422."""
    invisible = await _make_workflow_agent(db, user_b.id, scope="user")
    token = await login(client, user_a.email)
    res = await client.post(
        "/api/v1/workflows",
        json={
            "name": "eh-wf",
            "definition": {
                "nodes": [
                    {
                        "id": "eh",
                        "type": "error_handler",
                        "protected_agent_id": str(invisible.id),
                    }
                ],
                "edges": [],
            },
        },
        headers=bearer(token),
    )
    assert res.status_code == 422, res.text


# ---------- Phase F: supervisor 노드 ----------


async def test_supervisor_fanout_combines_results(db, user_a, _mock_runtime_deps):
    """supervisor — 여러 워커에 팬아웃 후 결과를 라벨과 함께 결합."""
    w1 = await _make_workflow_agent(
        db,
        user_a.id,
        scope="system",
        definition={
            "nodes": [{"id": "s", "type": "save_artifact", "name": "w1.txt", "content": "one"}],
            "edges": [],
        },
    )
    w2 = await _make_workflow_agent(
        db,
        user_a.id,
        scope="system",
        definition={
            "nodes": [{"id": "s", "type": "save_artifact", "name": "w2.txt", "content": "two"}],
            "edges": [],
        },
    )
    caller_def = {
        "nodes": [
            {
                "id": "sup",
                "type": "supervisor",
                "parallel": True,
                "workers": [
                    {"agent_id": str(w1.id), "label": "워커A"},
                    {"agent_id": str(w2.id), "label": "워커B"},
                ],
            }
        ],
        "edges": [],
    }
    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=user_a.id,
        agent_id=uuid.uuid4(),
    )
    summary = await execute_agent(ctx, "workflow", caller_def)
    # 두 워커 모두 실행되어 산출물 생성
    assert any(a["name"] == "w1.txt" for a in ctx.artifact_keys)
    assert any(a["name"] == "w2.txt" for a in ctx.artifact_keys)
    # 결합 출력에 워커 라벨 포함
    assert "워커A" in summary and "워커B" in summary


async def test_supervisor_with_synthesis(db, user_a, _mock_runtime_deps):
    """supervisor + synthesis_prompt — 워커 결과를 LLM으로 종합."""
    from biznotes_core.models import LLMModel, LLMProvider

    provider = LLMProvider(name=f"mock-sup-{uuid.uuid4().hex[:6]}", type="mock", enabled=True)
    db.add(provider)
    await db.flush()
    model = LLMModel(
        provider_id=provider.id,
        model_name="mock-sup",
        display_name="Mock Sup",
        capabilities=["llm"],
        is_default=False,
        enabled=True,
    )
    db.add(model)
    await db.commit()

    worker = await _make_workflow_agent(
        db,
        user_a.id,
        scope="system",
        definition={
            "nodes": [{"id": "s", "type": "save_artifact", "name": "w.txt", "content": "x"}],
            "edges": [],
        },
    )
    caller_def = {
        "nodes": [
            {
                "id": "sup",
                "type": "supervisor",
                "model_id": str(model.id),
                "synthesis_prompt": "[1] 종합대상\n{results}",
                "workers": [{"agent_id": str(worker.id), "label": "W"}],
            }
        ],
        "edges": [],
    }
    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=user_a.id,
        agent_id=uuid.uuid4(),
    )
    summary = await execute_agent(ctx, "workflow", caller_def)
    # mock LLM이 종합 프롬프트의 [1] 블록을 추출해 응답 — 종합이 수행됨
    assert "종합대상" in summary


async def test_supervisor_validation_rejects_invisible_worker(
    client: httpx.AsyncClient, user_a, user_b, db
):
    invisible = await _make_workflow_agent(db, user_b.id, scope="user")
    token = await login(client, user_a.email)
    res = await client.post(
        "/api/v1/workflows",
        json={
            "name": "sup-wf",
            "definition": {
                "nodes": [
                    {
                        "id": "sup",
                        "type": "supervisor",
                        "workers": [{"agent_id": str(invisible.id)}],
                    }
                ],
                "edges": [],
            },
        },
        headers=bearer(token),
    )
    assert res.status_code == 422, res.text


# ---------- json_validate 노드 (Phase 21a, phase-21-design.md §1.3) ----------

_VALID_MINDMAP = (
    '{"version": 1, "root": {"id": "root", "label": "주제", '
    '"children": [{"id": "n1", "label": "하위1", '
    '"source_ref": {"source_id": "s1", "chunk_id": "c1"}, "children": []}]}}'
)


async def test_json_validate_passes_valid_mindmap(db, user_a, _mock_runtime_deps):
    wf = {
        "nodes": [
            {"id": "raw", "type": "constant", "value": {"text": _VALID_MINDMAP}},
            {
                "id": "validate",
                "type": "json_validate",
                "schema": "mindmap",
                "source": "{raw.text}",
            },
            {
                "id": "save",
                "type": "save_artifact",
                "name": "mindmap.json",
                "content": "{validate.text}",
                "content_type": "application/json",
            },
        ],
        "edges": [["raw", "validate"], ["validate", "save"]],
    }
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, user_id=user_a.id
    )
    await execute_agent(ctx, "workflow", wf)
    assert any(a["name"] == "mindmap.json" for a in ctx.artifact_keys)


async def test_json_validate_unknown_schema_raises(db, user_a, _mock_runtime_deps):
    wf = {
        "nodes": [
            {"id": "raw", "type": "constant", "value": {"text": _VALID_MINDMAP}},
            {
                "id": "validate",
                "type": "json_validate",
                "schema": "not-a-real-schema",
                "source": "{raw.text}",
            },
        ],
        "edges": [["raw", "validate"]],
    }
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, user_id=user_a.id
    )
    with pytest.raises(AgentRuntimeError, match="알 수 없는 schema"):
        await execute_agent(ctx, "workflow", wf)


async def test_json_validate_parse_failure_without_retry_prompt_fails_run(
    db, user_a, _mock_runtime_deps
):
    # prompt 미지정 → 재시도 불가, 파싱 실패 즉시 run 종결(§1.3, CR-13: 빈 결과를 조용히
    # 반환하지 않음).
    wf = {
        "nodes": [
            {"id": "raw", "type": "constant", "value": {"text": "이것은 JSON이 아닙니다"}},
            {
                "id": "validate",
                "type": "json_validate",
                "schema": "mindmap",
                "source": "{raw.text}",
            },
        ],
        "edges": [["raw", "validate"]],
    }
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, user_id=user_a.id
    )
    with pytest.raises(AgentRuntimeError, match="스키마 검증 최종 실패"):
        await execute_agent(ctx, "workflow", wf)


async def test_json_validate_schema_failure_missing_label(db, user_a, _mock_runtime_deps):
    bad = '{"root": {"id": "root", "children": []}}'  # label 누락
    wf = {
        "nodes": [
            {"id": "raw", "type": "constant", "value": {"text": bad}},
            {
                "id": "validate",
                "type": "json_validate",
                "schema": "mindmap",
                "source": "{raw.text}",
            },
        ],
        "edges": [["raw", "validate"]],
    }
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, user_id=user_a.id
    )
    with pytest.raises(AgentRuntimeError, match="스키마 검증 최종 실패"):
        await execute_agent(ctx, "workflow", wf)


async def test_json_validate_rejects_duplicate_node_ids(db, user_a, _mock_runtime_deps):
    # RN21A-02(codex 리뷰) — 중복 id는 MindmapViewer의 react-flow 노드 키 렌더를 깨뜨리므로
    # 스키마 검증 단계에서 명시적으로 거부한다.
    dup = (
        '{"root": {"id": "root", "label": "주제", "children": ['
        '{"id": "dup", "label": "a", "children": []}, '
        '{"id": "dup", "label": "b", "children": []}]}}'
    )
    wf = {
        "nodes": [
            {"id": "raw", "type": "constant", "value": {"text": dup}},
            {
                "id": "validate",
                "type": "json_validate",
                "schema": "mindmap",
                "source": "{raw.text}",
            },
        ],
        "edges": [["raw", "validate"]],
    }
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, user_id=user_a.id
    )
    with pytest.raises(AgentRuntimeError, match="중복됩니다"):
        await execute_agent(ctx, "workflow", wf)


async def test_json_validate_rejects_missing_node_id(db, user_a, _mock_runtime_deps):
    missing_id = '{"root": {"label": "주제", "children": []}}'
    wf = {
        "nodes": [
            {"id": "raw", "type": "constant", "value": {"text": missing_id}},
            {
                "id": "validate",
                "type": "json_validate",
                "schema": "mindmap",
                "source": "{raw.text}",
            },
        ],
        "edges": [["raw", "validate"]],
    }
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, user_id=user_a.id
    )
    with pytest.raises(AgentRuntimeError, match="스키마 검증 최종 실패"):
        await execute_agent(ctx, "workflow", wf)


async def test_json_validate_handles_deeply_nested_tree_without_recursion_error(db, user_a):
    # RN21A-01(codex 리뷰) — 반복(스택) 순회이므로 병적으로 깊은 트리(재귀였다면
    # RecursionError를 유발할 깊이)도 절단 로직까지 정상 도달해야 한다.
    depth = 3000
    node = {"id": f"n{depth}", "label": "leaf", "children": []}
    for i in range(depth - 1, -1, -1):
        node = {"id": f"n{i}", "label": f"level-{i}", "children": [node]}
    deep_tree = json.dumps({"root": node})
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, user_id=user_a.id
    )
    node_def = {"schema": "mindmap", "source": "{raw.text}"}
    state = {"raw": {"text": deep_tree}}
    out = await _exec_json_validate(ctx, node_def, state)
    assert out["truncated"] is True


async def test_json_validate_retries_once_then_succeeds(
    db, user_a, _mock_runtime_deps, monkeypatch
):
    """1차 파싱 실패(constant의 잘못된 source) → prompt로 1회 재시도 LLM 호출 → 성공 시 저장됨."""
    from biznotes_core.services.agents import runtime as runtime_mod
    from biznotes_core.services.agents.nodes import llm as nodes_llm
    from biznotes_core.services.agents.nodes import validation as nodes_validation

    calls = {"n": 0}

    async def fake_exec_llm(ctx, node, state):
        calls["n"] += 1
        return {"text": _VALID_MINDMAP}

    # _exec_json_validate(nodes/validation.py, R2-5 이동)가 자기 모듈 네임스페이스에서
    # _exec_llm을 직접 임포트해 참조하므로, runtime 재수출만 패치하면 재시도 호출이
    # 패치를 우회한다 — nodes.llm/nodes.validation 양쪽을 함께 패치해야 seam이 보존된다.
    monkeypatch.setattr(runtime_mod, "_exec_llm", fake_exec_llm)
    monkeypatch.setattr(nodes_llm, "_exec_llm", fake_exec_llm)
    monkeypatch.setattr(nodes_validation, "_exec_llm", fake_exec_llm)

    wf = {
        "nodes": [
            {"id": "raw", "type": "constant", "value": {"text": "이것은 JSON이 아닙니다"}},
            {
                "id": "validate",
                "type": "json_validate",
                "schema": "mindmap",
                "source": "{raw.text}",
                "prompt": "재시도용 프롬프트",
            },
            {
                "id": "save",
                "type": "save_artifact",
                "name": "mindmap.json",
                "content": "{validate.text}",
                "content_type": "application/json",
            },
        ],
        "edges": [["raw", "validate"], ["validate", "save"]],
    }
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, user_id=user_a.id
    )
    await execute_agent(ctx, "workflow", wf)
    assert calls["n"] == 1  # 재시도 LLM 호출 정확히 1회
    assert any(a["name"] == "mindmap.json" for a in ctx.artifact_keys)


async def test_json_validate_retry_fails_twice_raises(db, user_a, _mock_runtime_deps, monkeypatch):
    from biznotes_core.services.agents import runtime as runtime_mod
    from biznotes_core.services.agents.nodes import llm as nodes_llm
    from biznotes_core.services.agents.nodes import validation as nodes_validation

    async def always_bad_llm(ctx, node, state):
        return {"text": "여전히 JSON이 아닙니다"}

    # seam 보존 사유는 test_json_validate_retries_once_then_succeeds 참조.
    monkeypatch.setattr(runtime_mod, "_exec_llm", always_bad_llm)
    monkeypatch.setattr(nodes_llm, "_exec_llm", always_bad_llm)
    monkeypatch.setattr(nodes_validation, "_exec_llm", always_bad_llm)

    wf = {
        "nodes": [
            {"id": "raw", "type": "constant", "value": {"text": "placeholder"}},
            {
                "id": "validate",
                "type": "json_validate",
                "schema": "mindmap",
                "source": "{raw.text}",
                "prompt": "재시도용 프롬프트",
            },
        ],
        "edges": [["raw", "validate"]],
    }
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, user_id=user_a.id
    )
    with pytest.raises(AgentRuntimeError, match="스키마 검증 최종 실패"):
        await execute_agent(ctx, "workflow", wf)


async def test_json_validate_truncates_oversized_mindmap(db, user_a):
    # 201개 노드(root + 200 children) — 상한(200) 초과 시 BFS 절단 + truncated=true(D-11).
    children = [{"id": f"n{i}", "label": f"leaf-{i}", "children": []} for i in range(201)]
    oversized = json.dumps({"root": {"id": "root", "label": "root", "children": children}})
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, user_id=user_a.id
    )
    node = {"schema": "mindmap", "source": "{raw.text}"}
    state = {"raw": {"text": oversized}}
    out = await _exec_json_validate(ctx, node, state)
    assert out["truncated"] is True
    assert len(out["json"]["root"]["children"]) < 201


# ---------- json_validate: flashcards/quiz 스키마 (Phase 21b, phase-21-design.md §2) ----------

_VALID_CITATION = (
    '{"chunk_id": "__CHUNK__", "quote": "녹차는 70~80도의 물에서 2분간 우린다.", '
    '"source_id": "__SOURCE__", "source_title": "녹차 가이드"}'
)


def _valid_flashcards_json(chunk_id: str, source_id: str) -> str:
    citation = _VALID_CITATION.replace("__CHUNK__", chunk_id).replace("__SOURCE__", source_id)
    return (
        f'[{{"id": "c1", "question": "녹차 우림 온도는?", "answer": "70~80도", '
        f'"citation": {citation}}}]'
    )


def _valid_quiz_json(chunk_id: str, source_id: str) -> str:
    citation = _VALID_CITATION.replace("__CHUNK__", chunk_id).replace("__SOURCE__", source_id)
    return (
        '[{"id": "q1", "question": "녹차 우림 온도는?", '
        '"choices": ["60도", "70~80도", "90도", "100도"], "answer_index": 1, '
        f'"citation": {citation}}}]'
    )


async def test_json_validate_passes_valid_flashcards(db, user_a, _mock_runtime_deps):
    raw = _valid_flashcards_json(str(uuid.uuid4()), str(uuid.uuid4()))
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, user_id=user_a.id
    )
    node = {"schema": "flashcards", "source": "{raw.text}"}
    state = {"raw": {"text": raw}}
    out = await _exec_json_validate(ctx, node, state)
    assert out["json"] == json.loads(raw)
    # flashcards/quiz는 배열 스키마라 mindmap과 달리 truncated 메타를 얹지 않는다.
    assert "truncated" not in out["json"]


async def test_json_validate_passes_valid_quiz(db, user_a, _mock_runtime_deps):
    raw = _valid_quiz_json(str(uuid.uuid4()), str(uuid.uuid4()))
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, user_id=user_a.id
    )
    node = {"schema": "quiz", "source": "{raw.text}"}
    state = {"raw": {"text": raw}}
    out = await _exec_json_validate(ctx, node, state)
    assert out["json"] == json.loads(raw)


async def test_json_validate_rejects_flashcards_missing_citation_field(
    db, user_a, _mock_runtime_deps
):
    bad = (
        '[{"id": "c1", "question": "q", "answer": "a", '
        '"citation": {"chunk_id": "x", "quote": "q", "source_id": "s"}}]'  # source_title 누락
    )
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, user_id=user_a.id
    )
    node = {"schema": "flashcards", "source": "{raw.text}"}
    state = {"raw": {"text": bad}}
    with pytest.raises(AgentRuntimeError, match="스키마 검증 최종 실패"):
        await _exec_json_validate(ctx, node, state)


async def test_json_validate_rejects_quiz_wrong_choice_count(db, user_a, _mock_runtime_deps):
    bad = (
        '[{"id": "q1", "question": "q", "choices": ["a", "b", "c"], "answer_index": 0, '
        f'"citation": {_VALID_CITATION.replace("__CHUNK__", "x").replace("__SOURCE__", "s")}}}]'
    )
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, user_id=user_a.id
    )
    node = {"schema": "quiz", "source": "{raw.text}"}
    state = {"raw": {"text": bad}}
    with pytest.raises(AgentRuntimeError, match="스키마 검증 최종 실패"):
        await _exec_json_validate(ctx, node, state)


async def test_json_validate_rejects_quiz_answer_index_out_of_range(db, user_a, _mock_runtime_deps):
    bad = (
        '[{"id": "q1", "question": "q", "choices": ["a", "b", "c", "d"], "answer_index": 4, '
        f'"citation": {_VALID_CITATION.replace("__CHUNK__", "x").replace("__SOURCE__", "s")}}}]'
    )
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, user_id=user_a.id
    )
    node = {"schema": "quiz", "source": "{raw.text}"}
    state = {"raw": {"text": bad}}
    with pytest.raises(AgentRuntimeError, match="스키마 검증 최종 실패"):
        await _exec_json_validate(ctx, node, state)


async def test_json_validate_rejects_flashcards_duplicate_ids(db, user_a, _mock_runtime_deps):
    citation = _VALID_CITATION.replace("__CHUNK__", "x").replace("__SOURCE__", "s")
    bad = (
        f'[{{"id": "dup", "question": "q1", "answer": "a1", "citation": {citation}}}, '
        f'{{"id": "dup", "question": "q2", "answer": "a2", "citation": {citation}}}]'
    )
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, user_id=user_a.id
    )
    node = {"schema": "flashcards", "source": "{raw.text}"}
    state = {"raw": {"text": bad}}
    with pytest.raises(AgentRuntimeError, match="중복됩니다"):
        await _exec_json_validate(ctx, node, state)


# ---------- json_validate dialogue 스키마 (Phase 21c, phase-21-design.md §3.2) ----------


# ---------- citation_verify 노드 (Phase 21b, phase-21-design.md §2.3) ----------


async def _ready_source_with_chunk(
    db, user, *, quote: str = "녹차는 70~80도의 물에서 2분간 우린다."
):
    from biznotes_core.models import Notebook, Source, SourceChunk

    nb = Notebook(owner_id=user.id, title="citation-verify-nb")
    db.add(nb)
    await db.flush()
    source = Source(notebook_id=nb.id, type="text", title="녹차 가이드", status="ready")
    db.add(source)
    await db.flush()
    chunk = SourceChunk(source_id=source.id, seq=0, text=quote, meta={})
    db.add(chunk)
    await db.commit()
    return nb, source, chunk.id


def _flashcard_item(
    chunk_id: str, source_id: str, quote: str, question: str = "질문", answer: str | None = None
) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "question": question,
        # answer는 검증 대상 콘텐츠 — 기본값은 quote 자체(어휘 겹침 100%로 항상 매칭 성공).
        "answer": answer if answer is not None else quote,
        "citation": {
            "chunk_id": chunk_id,
            "quote": quote,
            "source_id": source_id,
            "source_title": "녹차 가이드",
        },
    }


async def test_citation_verify_keeps_valid_matching_citation(db, user_a):
    quote = "녹차는 70~80도의 물에서 2분간 우린다."
    nb, source, chunk_id = await _ready_source_with_chunk(db, user_a, quote=quote)
    item = _flashcard_item(str(chunk_id), str(source.id), quote)
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=nb.id, db=db, params={}, user_id=user_a.id)
    out = await _exec_citation_verify(
        ctx, {"source": "{validate.json}"}, {"validate": {"json": [item]}}
    )
    assert out["kept_count"] == 1
    assert out["dropped"] == 0
    assert out["json"][0]["id"] == item["id"]


async def test_citation_verify_drops_marker_mismatch(db, user_a):
    """F4 위험 방어 회귀 — answer가 청크 quote와 무관하면(어휘/임베딩 모두 불일치)
    verified가 암묵적으로 True로 남지 않고 명시적으로 제외되어야 한다."""
    quote = "녹차는 70~80도의 물에서 2분간 우린다."
    nb, source, chunk_id = await _ready_source_with_chunk(db, user_a, quote=quote)
    item = _flashcard_item(
        str(chunk_id), str(source.id), quote, answer="완전히 무관한 발췌 내용입니다 xyz"
    )
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=nb.id, db=db, params={}, user_id=user_a.id)
    out = await _exec_citation_verify(
        ctx, {"source": "{validate.json}"}, {"validate": {"json": [item]}}
    )
    assert out["kept_count"] == 0
    assert out["dropped"] == 1


async def test_citation_verify_rejects_foreign_notebook_chunk_id(db, user_a):
    """D-10 — 타 notebook 소속 chunk_id는 verified 여부와 무관하게 무조건 제외(IDOR 방지)."""
    quote = "녹차는 70~80도의 물에서 2분간 우린다."
    _other_nb, source, chunk_id = await _ready_source_with_chunk(db, user_a, quote=quote)
    # citation_verify를 호출하는 run의 notebook_id는 이 소스가 속한 notebook과 다르다.
    item = _flashcard_item(str(chunk_id), str(source.id), quote)
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, user_id=user_a.id
    )
    out = await _exec_citation_verify(
        ctx, {"source": "{validate.json}"}, {"validate": {"json": [item]}}
    )
    assert out["kept_count"] == 0
    assert out["dropped"] == 1


async def test_citation_verify_rejects_source_id_chunk_mismatch(db, user_a):
    """chunk_id는 실제 존재하지만 citation의 source_id가 다른 소스를 가리키면 거부."""
    quote = "녹차는 70~80도의 물에서 2분간 우린다."
    nb, source, chunk_id = await _ready_source_with_chunk(db, user_a, quote=quote)
    item = _flashcard_item(str(chunk_id), str(uuid.uuid4()), quote)  # 존재하지 않는 source_id
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=nb.id, db=db, params={}, user_id=user_a.id)
    out = await _exec_citation_verify(
        ctx, {"source": "{validate.json}"}, {"validate": {"json": [item]}}
    )
    assert out["kept_count"] == 0
    assert out["dropped"] == 1


async def test_citation_verify_dedupes_normalized_duplicate_questions(db, user_a):
    quote = "녹차는 70~80도의 물에서 2분간 우린다."
    nb, source, chunk_id = await _ready_source_with_chunk(db, user_a, quote=quote)
    item1 = _flashcard_item(str(chunk_id), str(source.id), quote, question="녹차 우림 온도는?")
    item2 = _flashcard_item(
        str(chunk_id), str(source.id), quote, question="녹차   우림  온도는??"
    )  # 공백/구두점만 다른 중복
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=nb.id, db=db, params={}, user_id=user_a.id)
    out = await _exec_citation_verify(
        ctx, {"source": "{validate.json}"}, {"validate": {"json": [item1, item2]}}
    )
    assert out["kept_count"] == 1
    assert out["dropped"] == 1


async def test_citation_verify_missing_citation_field_dropped(db, user_a):
    nb, source, chunk_id = await _ready_source_with_chunk(db, user_a)
    item = {"id": "c1", "question": "q", "answer": "a"}  # citation 필드 자체가 없음
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=nb.id, db=db, params={}, user_id=user_a.id)
    out = await _exec_citation_verify(
        ctx, {"source": "{validate.json}"}, {"validate": {"json": [item]}}
    )
    assert out["kept_count"] == 0
    assert out["dropped"] == 1


async def test_citation_verify_quiz_uses_answer_choice_as_content(db, user_a):
    """퀴즈 항목은 answer 필드가 없다 — choices[answer_index]를 검증 대상 콘텐츠로 삼는다."""
    quote = "녹차는 70~80도의 물에서 2분간 우린다."
    nb, source, chunk_id = await _ready_source_with_chunk(db, user_a, quote=quote)
    item = {
        "id": "q1",
        "question": "녹차 우림 온도는?",
        "choices": ["60도", quote, "90도", "100도"],
        "answer_index": 1,
        "citation": {
            "chunk_id": str(chunk_id),
            "quote": quote,
            "source_id": str(source.id),
            "source_title": "녹차 가이드",
        },
    }
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=nb.id, db=db, params={}, user_id=user_a.id)
    out = await _exec_citation_verify(
        ctx, {"source": "{validate.json}"}, {"validate": {"json": [item]}}
    )
    assert out["kept_count"] == 1
    assert out["dropped"] == 0


async def test_citation_verify_quiz_wrong_choice_dropped(db, user_a):
    """정답 선택지가 청크 quote와 무관하면(오답을 정답으로 표기하는 등) 검증에서 제외."""
    quote = "녹차는 70~80도의 물에서 2분간 우린다."
    nb, source, chunk_id = await _ready_source_with_chunk(db, user_a, quote=quote)
    item = {
        "id": "q1",
        "question": "녹차 우림 온도는?",
        "choices": ["완전히 무관한 오답 텍스트", "70~80도", "90도", "100도"],
        "answer_index": 0,
        "citation": {
            "chunk_id": str(chunk_id),
            "quote": quote,
            "source_id": str(source.id),
            "source_title": "녹차 가이드",
        },
    }
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=nb.id, db=db, params={}, user_id=user_a.id)
    out = await _exec_citation_verify(
        ctx, {"source": "{validate.json}"}, {"validate": {"json": [item]}}
    )
    assert out["kept_count"] == 0
    assert out["dropped"] == 1


# ---------- 저장 시점 정책 배선 (계획 AC-25·AC-29·AC-31, §4.5 T-9~T-21) ----------
# 코어 헬퍼(find_llm_node_side_effect_tools · find_forbidden_mcp_tool_nodes ·
# find_reserved_node_keys)는 스스로 호출되지 않는다 — 제품 저장 경로의 배선만이 422를 낸다.
# 아래 테스트는 전부 제품 FastAPI 앱 실경로이며 검증 함수를 mock하지 않는다.

_SIDE_EFFECT_SAFE_TOOLS = [
    "rag_search",
    "web_search",
    "source_load",
    "run_skill",
    "activate_skill",
    "read_skill_file",
    "mcp",  # 디스커버리 토큰 — 코어 D2 보존 대상
    "not_a_registered_tool",  # 미등록 이름 — build_tool_specs가 경고 후 무시(거부 대상 아님)
]


def _llm_def(tools) -> dict:
    return {"nodes": [{"id": "g", "type": "llm", "prompt": "x", "tools": tools}], "edges": []}


async def _post_workflow(client: httpx.AsyncClient, token: str, definition: dict):
    return await client.post(
        "/api/v1/workflows",
        json={"name": f"wf-{uuid.uuid4().hex[:6]}", "definition": definition},
        headers=bearer(token),
    )


async def test_t9_create_rejects_llm_node_side_effect_tool(client: httpx.AsyncClient, user_a):
    """T-9: llm 노드 tools의 mcp_call → 422 + 위반 도구명."""
    token = await login(client, user_a.email)
    res = await _post_workflow(client, token, _llm_def(["rag_search", "mcp_call"]))
    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert "llm 노드 tools" in detail and "mcp_call" in detail
    assert "rag_search" not in detail.split("옮기세요")[-1]  # 허용 도구는 위반 목록에 없다


@pytest.mark.parametrize(
    "tool", ["call_agent", "http_request", "mcp_call", "register_source", "run_skill_script"]
)
async def test_t9_each_side_effect_tool_is_rejected(client: httpx.AsyncClient, user_a, tool):
    """T-9 보강: 코어 #70의 부작용 도구 5종이 각각 거부된다."""
    token = await login(client, user_a.email)
    res = await _post_workflow(client, token, _llm_def([tool]))
    assert res.status_code == 422, res.text
    assert tool in res.json()["detail"]


async def test_t9b_params_only_patch_revalidates_stored_definition(
    client: httpx.AsyncClient, user_a, db
):
    """T-9b: 저장 검증을 거치지 않은 기존 정의(DB 직접 삽입)에 params_schema만 PATCH → 422."""
    stored = await _make_workflow_agent(
        db, user_a.id, enabled=False, definition=_llm_def(["http_request"])
    )
    token = await login(client, user_a.email)
    res = await client.patch(
        f"/api/v1/workflows/{stored.id}",
        json={"params_schema": {"type": "object", "properties": {}}},
        headers=bearer(token),
    )
    assert res.status_code == 422, res.text
    assert "http_request" in res.json()["detail"]


async def test_t11_non_side_effect_tools_are_accepted(client: httpx.AsyncClient, user_a):
    """T-11(비하강): 읽기 전용 도구·mcp 디스커버리 토큰·미등록 이름은 거부하지 않는다 → 201."""
    token = await login(client, user_a.email)
    res = await _post_workflow(client, token, _llm_def(_SIDE_EFFECT_SAFE_TOOLS))
    assert res.status_code == 201, res.text


@pytest.mark.parametrize(
    "node",
    [
        # llm이 아닌 노드의 tools는 이 규칙의 대상이 아니다
        {"id": "x", "type": "constant", "value": "v", "tools": ["mcp_call"]},
        # tools가 리스트가 아니면 무시
        {"id": "g", "type": "llm", "prompt": "x", "tools": "mcp_call"},
        # 빈 리스트
        {"id": "g", "type": "llm", "prompt": "x", "tools": []},
    ],
)
async def test_t12_rule_ignores_non_llm_non_list_and_empty(
    client: httpx.AsyncClient, user_a, node
):
    """T-12: 비-llm 노드·비리스트·빈 리스트는 무시 → 201."""
    token = await login(client, user_a.email)
    res = await _post_workflow(client, token, {"nodes": [node], "edges": []})
    assert res.status_code == 201, res.text


async def test_t14_create_rejects_mcp_tool_node_in_user_scope(client: httpx.AsyncClient, user_a):
    """T-14: 사용자 경로(scope "user")의 mcp_tool 노드 → 422 + detail에 노드 id·kind."""
    token = await login(client, user_a.email)
    definition = {
        "nodes": [
            {"id": "m", "type": "mcp_tool", "server_id": str(uuid.uuid4()), "tool_name": "t"}
        ],
        "edges": [],
    }
    res = await _post_workflow(client, token, definition)
    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert "m (mcp_tool)" in detail
    assert "scope user" in detail


async def test_t14_admin_on_user_router_is_still_user_scope(client: httpx.AsyncClient, admin_user):
    """T-14 보강: 신뢰 기준은 실행자 role이 아니라 정의 scope — admin도 /workflows는 "user"."""
    token = await login(client, admin_user.email)
    definition = {"nodes": [{"id": "m", "type": "mcp_tool", "server_id": "s"}], "edges": []}
    res = await _post_workflow(client, token, definition)
    assert res.status_code == 422, res.text


async def test_t15_create_rejects_react_mcp_call(client: httpx.AsyncClient, user_a):
    """T-15: react_agent allowed_tools의 mcp_call도 같은 경계 → 422."""
    token = await login(client, user_a.email)
    definition = {
        "nodes": [
            {"id": "r", "type": "react_agent", "goal": "x", "allowed_tools": ["mcp_call"]}
        ],
        "edges": [],
    }
    res = await _post_workflow(client, token, definition)
    assert res.status_code == 422, res.text
    assert "r (mcp_call)" in res.json()["detail"]


async def test_t16_params_only_patch_rejects_stored_mcp_tool(client: httpx.AsyncClient, user_a, db):
    """T-16: DB에 직접 넣은 mcp_tool 정의(user scope)의 params_schema만 바꾸는 PATCH → 422."""
    stored = await _make_workflow_agent(
        db,
        user_a.id,
        enabled=False,
        definition={"nodes": [{"id": "m", "type": "mcp_tool", "server_id": "s"}], "edges": []},
    )
    token = await login(client, user_a.email)
    res = await client.patch(
        f"/api/v1/workflows/{stored.id}",
        json={"params_schema": {"type": "object", "properties": {}}},
        headers=bearer(token),
    )
    assert res.status_code == 422, res.text
    assert "m (mcp_tool)" in res.json()["detail"]


async def test_publish_revalidates_stored_definition(client: httpx.AsyncClient, user_a, db):
    """발행(`:publish`)도 저장 정의를 agent.scope로 재검증한다 — 위반 draft는 게시되지 않는다."""
    stored = await _make_workflow_agent(
        db,
        user_a.id,
        enabled=False,
        definition={
            "nodes": [{"id": "r", "type": "react_agent", "goal": "x", "_resume": {"forged": 1}}],
            "edges": [],
        },
    )
    token = await login(client, user_a.email)
    res = await client.post(f"/api/v1/workflows/{stored.id}/publish", headers=bearer(token))
    assert res.status_code == 422, res.text
    await db.refresh(stored)
    assert stored.enabled is False


async def test_t19_create_rejects_reserved_resume_key(client: httpx.AsyncClient, user_a):
    """T-19: react_agent 노드의 `_resume`(위조 승인 재개) → 422 + "r: _resume"."""
    token = await login(client, user_a.email)
    definition = {
        "nodes": [
            {
                "id": "r",
                "type": "react_agent",
                "goal": "x",
                "_resume": {"react_approval_status": "approved"},
            }
        ],
        "edges": [],
    }
    res = await _post_workflow(client, token, definition)
    assert res.status_code == 422, res.text
    assert "r: _resume" in res.json()["detail"]


async def test_t21_other_underscore_keys_are_allowed(client: httpx.AsyncClient, user_a):
    """T-21(비하강): 코어가 의도적으로 허용하는 `_skill_preactivate`는 통과 → 201."""
    token = await login(client, user_a.email)
    definition = {
        "nodes": [
            {"id": "r", "type": "react_agent", "goal": "x", "_skill_preactivate": ["s"]}
        ],
        "edges": [],
    }
    res = await _post_workflow(client, token, definition)
    assert res.status_code == 201, res.text


async def test_violations_are_reported_together_in_one_422(client: httpx.AsyncClient, user_a):
    """세 헬퍼의 위반을 한 번의 422에 모두 담는다(작성자가 한 번에 고친다 — 코어 후속 R10)."""
    token = await login(client, user_a.email)
    definition = {
        "nodes": [
            {"id": "g", "type": "llm", "prompt": "x", "tools": ["mcp_call"]},
            {"id": "m", "type": "mcp_tool", "server_id": "s"},
            {"id": "r", "type": "react_agent", "goal": "x", "_resume": {}},
        ],
        "edges": [],
    }
    res = await _post_workflow(client, token, definition)
    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert "llm 노드 tools" in detail
    assert "m (mcp_tool)" in detail
    assert "r: _resume" in detail


async def test_duplicate_rejects_copy_that_user_scope_cannot_hold(
    client: httpx.AsyncClient, user_a, db
):
    """P0-14: 복제 사본은 scope "user"로 저장된다 — system 원본의 mcp_tool은 사본에 둘 수 없다."""
    from biznotes_core.models import Agent

    src = Agent(
        name="시스템 MCP 워크플로우",
        kind="workflow",
        definition={"nodes": [{"id": "m", "type": "mcp_tool", "server_id": "s"}], "edges": []},
        params_schema={},
        scope="system",
        enabled=True,
    )
    db.add(src)
    await db.commit()
    await db.refresh(src)

    token = await login(client, user_a.email)
    res = await client.post(f"/api/v1/workflows/{src.id}/duplicate", headers=bearer(token))
    assert res.status_code == 422, res.text
    assert "m (mcp_tool)" in res.json()["detail"]
    mine = await client.get("/api/v1/workflows", headers=bearer(token))
    assert all(w["owner_id"] != str(user_a.id) for w in mine.json())  # 사본이 저장되지 않았다


# ---------- 직접 노드 승인 설정 저장 검증 (코어 #153 A, AC-2.1·E-B2·D-26) ----------
#
# 저장 경로(`_validate_structure`)가 `find_direct_node_approval_violations(nodes, edges,
# agent_scope)`를 실제로 부른다 — 코어 판정 함수를 대체하지 않고 라우터 → 검증기 → 코어 규칙을
# 그대로 지난다. 런타임 게이트(`direct_node_needs_gate`)와 같은 규칙이다.

_LITERAL_URL = "https://api.example.com/v1/items?q={params.q}"
_TEMPLATE_HOST_URL = "https://{params.host}/v1/items"


def _http_node(node_id: str = "h", **extra) -> dict:
    return {"id": node_id, "type": "http", "url": _LITERAL_URL, "method": "GET", **extra}


def _fan_out_definition(**http_extra) -> dict:
    """a → (h, b) 병렬 — 게이트가 필요한 http 노드가 fan-out 그래프에 있다."""
    return {
        "nodes": [
            {"id": "a", "type": "constant", "value": "x"},
            _http_node(**http_extra),
            {"id": "b", "type": "constant", "value": "y"},
        ],
        "edges": [["a", "h"], ["a", "b"]],
    }


async def _system_workflow(db, definition: dict):
    from biznotes_core.models import Agent

    agent = Agent(
        name=f"sys-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        definition=definition,
        params_schema={},
        scope="system",
        enabled=True,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("user_false", "requires_approval=false는 system 정의에서만"),
        ("string_false", "requires_approval은 true/false여야"),
        ("fan_out_gated", "병렬(fan-out) 그래프에 둘 수 없습니다"),
        ("system_template_host_false", "url 대상(scheme·호스트·포트)은 리터럴이어야"),
        ("duplicate_system_false", "requires_approval=false는 system 정의에서만"),
    ],
    ids=["user_false", "string_false", "fan_out_gated", "system_template_host", "duplicate"],
)
async def test_direct_node_violation_rejected_on_save(
    client: httpx.AsyncClient, user_a, admin_user, db, case, expected
):
    """사용자 경로 저장(생성·수정·복제)이 직접 노드 위반을 기존 422 problems에 합친다.

    - user_false: 사용자 정의(scope "user")의 `requires_approval: false` http 노드
    - string_false: 문자열 `"false"` — bool이 아니면 거부(런타임은 게이트로 취급)
    - fan_out_gated: 승인이 필요한(기본) http 노드가 병렬 그래프에 있다
    - system_template_host_false: admin이 이 라우터로 system 정의를 고치되 url host가 템플릿
    - duplicate_system_false: system 원본(false, 리터럴)을 복제하면 사본은 scope "user"(D-26)
    """
    if case == "user_false":
        token = await login(client, user_a.email)
        res = await _post_workflow(
            client, token, {"nodes": [_http_node(requires_approval=False)], "edges": []}
        )
    elif case == "string_false":
        token = await login(client, user_a.email)
        res = await _post_workflow(
            client, token, {"nodes": [_http_node(requires_approval="false")], "edges": []}
        )
    elif case == "fan_out_gated":
        token = await login(client, user_a.email)
        res = await _post_workflow(client, token, _fan_out_definition())
    elif case == "system_template_host_false":
        stored = await _system_workflow(
            db, {"nodes": [_http_node(requires_approval=False)], "edges": []}
        )
        token = await login(client, admin_user.email)
        res = await client.patch(
            f"/api/v1/workflows/{stored.id}",
            json={
                "definition": {
                    "nodes": [
                        {
                            "id": "h",
                            "type": "http",
                            "url": _TEMPLATE_HOST_URL,
                            "method": "GET",
                            "requires_approval": False,
                        }
                    ],
                    "edges": [],
                }
            },
            headers=bearer(token),
        )
    else:  # duplicate_system_false
        src = await _system_workflow(
            db, {"nodes": [_http_node(requires_approval=False)], "edges": []}
        )
        token = await login(client, user_a.email)
        res = await client.post(f"/api/v1/workflows/{src.id}/duplicate", headers=bearer(token))

    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert "직접 노드 승인 설정을 저장할 수 없습니다" in detail
    assert "h: " in detail and expected in detail

    if case == "duplicate_system_false":
        mine = await client.get("/api/v1/workflows", headers=bearer(token))
        assert all(w["owner_id"] != str(user_a.id) for w in mine.json())  # 사본 미저장
    if case == "system_template_host_false":
        await db.refresh(stored)
        assert stored.definition["nodes"][0]["url"] == _LITERAL_URL  # 거부된 수정은 반영 안 됨


async def test_direct_node_violations_join_existing_problems(client: httpx.AsyncClient, user_a):
    """직접 노드 위반은 기존 헬퍼 위반과 같은 한 번의 422에 담긴다(작성자가 한 번에 고친다)."""
    token = await login(client, user_a.email)
    definition = {
        "nodes": [
            {"id": "g", "type": "llm", "prompt": "x", "tools": ["mcp_call"]},
            _http_node(requires_approval=False),
        ],
        "edges": [],
    }
    res = await _post_workflow(client, token, definition)
    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert "llm 노드 tools" in detail
    assert "h: requires_approval=false는 system 정의에서만" in detail


async def test_direct_node_valid_definitions_pass(
    client: httpx.AsyncClient, user_a, admin_user, db
):
    """대조: 위반이 없는 정의는 그대로 저장된다.

    ① 사용자 정의의 http 노드(승인 기본값, 직렬) → 201 — 런타임에 승인 대기로 멈출 뿐 저장은 된다
    ② `requires_approval: true` 명시 → 201
    ③ admin이 system 정의를 system·리터럴 url·false로 수정 → 200
    ④ system 정의를 발행(publish) — 저장 정의 재검증 통과 → 200
    """
    token = await login(client, user_a.email)
    res = await _post_workflow(client, token, {"nodes": [_http_node()], "edges": []})
    assert res.status_code == 201, res.text
    res = await _post_workflow(
        client, token, {"nodes": [_http_node(requires_approval=True)], "edges": []}
    )
    assert res.status_code == 201, res.text

    stored = await _system_workflow(db, {"nodes": [_http_node()], "edges": []})
    admin_token = await login(client, admin_user.email)
    res = await client.patch(
        f"/api/v1/workflows/{stored.id}",
        json={"definition": {"nodes": [_http_node(requires_approval=False)], "edges": []}},
        headers=bearer(admin_token),
    )
    assert res.status_code == 200, res.text
    assert res.json()["definition"]["nodes"][0]["requires_approval"] is False
    res = await client.post(f"/api/v1/workflows/{stored.id}/publish", headers=bearer(admin_token))
    assert res.status_code == 200, res.text


async def test_direct_node_other_users_workflow_patch_is_404(
    client: httpx.AsyncClient, user_a, user_b
):
    """비인가 관점(E-B2): 타인의 워크플로우 수정은 검증 이전에 404 — 위반 여부를 드러내지 않는다."""
    token_a = await login(client, user_a.email)
    res = await _post_workflow(client, token_a, {"nodes": [_http_node()], "edges": []})
    assert res.status_code == 201, res.text
    wf_id = res.json()["id"]

    token_b = await login(client, user_b.email)
    res = await client.patch(
        f"/api/v1/workflows/{wf_id}",
        json={"definition": {"nodes": [_http_node(requires_approval=False)], "edges": []}},
        headers=bearer(token_b),
    )
    assert res.status_code == 404, res.text
    assert "직접 노드" not in res.text
