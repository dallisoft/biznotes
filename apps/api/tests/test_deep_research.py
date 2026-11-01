"""Phase 22b — Deep Research 프리셋 단위/통합 테스트.

phase-22-design.md §2 참조. `register_source` 도구(F11 IDOR 방지, run 단위 상한,
D-15), auto_approve_tools 상시 게이트 강제(D-14), `deep_research_agent` 시드
등록(F15)을 검증한다. 기존 HITL 게이트 동작 자체는 test_hitl_approval.py가
다루므로 여기서는 register_source 전용 동작만 다룬다.
"""

import uuid

import pytest
from biznotes_core.models import Agent, Source
from biznotes_core.services.agents import runtime
from biznotes_core.services.agents.react import approval
from biznotes_core.services.agents.runtime import ApprovalRequired, RunContext, execute_agent
from biznotes_core.services.agents.tool_registry import is_always_gated_tool, is_side_effect_tool
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult
from sqlalchemy import select

from tests.conftest import set_source_limit, share_with


def _ctx(db, notebook_id, user_id=None):
    return RunContext(
        run_id=uuid.uuid4(), notebook_id=notebook_id, db=db, params={}, user_id=user_id,
    )


def _tool_turn(name: str, args: dict) -> ToolTurnResult:
    import json

    call = ToolCall(id="c", name=name, arguments=args)
    assistant = {
        "role": "assistant", "content": "",
        "tool_calls": [
            {
                "id": "c", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }
    return ToolTurnResult(
        content="", tool_calls=[call], assistant_message=assistant,
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, model=None,
    )


def _final_turn(text: str = "끝") -> ToolTurnResult:
    return ToolTurnResult(
        content=text, tool_calls=[], assistant_message={"role": "assistant", "content": text},
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, model=None,
    )


def _patch_completer(monkeypatch, script):
    calls = {"n": 0}

    async def fake(db, model_id, messages, tools, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        return script(i)

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake)
    return calls


async def _make_notebook(db, owner_id):
    from biznotes_core.models import Notebook

    nb = Notebook(owner_id=owner_id, title="dr-notebook")
    db.add(nb)
    await db.commit()
    await db.refresh(nb)
    return nb


# ── tool_registry 정책 분류 ───────────────────────────────────────────────


def test_register_source_is_side_effect_and_always_gated():
    assert is_side_effect_tool("register_source") is True
    assert is_always_gated_tool("register_source") is True
    assert is_always_gated_tool("call_agent") is False


# ── _dispatch_register_source 동작 ───────────────────────────────────────


@pytest.fixture(autouse=True)
def _mock_ingest(monkeypatch):
    class FakeAsyncResult:
        id = "fake-task-id"

    monkeypatch.setattr(
        "biznotes_core.workers.tasks.ingestion.ingest_source.delay", lambda sid: FakeAsyncResult()
    )
    monkeypatch.setattr(
        "biznotes_core.workers.tasks.ingestion.ingest_source.apply_async",
        lambda *a, **k: FakeAsyncResult(),
    )


async def test_register_source_creates_source_in_ctx_notebook(db, user_a, monkeypatch):
    """notebook_id 인자를 주더라도 무시하고 ctx.notebook_id만 사용한다(F11, IDOR 방지)."""
    from biznotes_core.services.agents.runtime import _dispatch_register_source

    nb = await _make_notebook(db, user_a.id)
    other_nb = await _make_notebook(db, user_a.id)

    ctx = _ctx(db, nb.id, user_a.id)
    result = await _dispatch_register_source(
        ctx, {"url": "https://example.com/a", "notebook_id": str(other_nb.id)}
    )
    assert "등록했습니다" in result

    rows = (await db.execute(select(Source).where(Source.notebook_id == nb.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].origin_url == "https://example.com/a"

    other_rows = (
        await db.execute(select(Source).where(Source.notebook_id == other_nb.id))
    ).scalars().all()
    assert len(other_rows) == 0, "notebook_id 인자를 신뢰해 다른 노트북에 등록하면 안 된다"


async def test_register_source_rejects_without_write_permission(db, user_a, user_b):
    """[R22B-01] read 권한만 있는 공유 사용자는 register_source로 소스를 등록할 수 없다.

    create_agent_run은 노트북 read 권한만 요구하므로(agents.py _notebook_readable),
    이 검사가 없으면 read-only 공유 사용자가 Deep Research를 실행/승인해 타인
    노트북에 쓰기 작업을 수행할 수 있다."""
    from biznotes_core.services.agents.runtime import _dispatch_register_source

    nb = await _make_notebook(db, user_a.id)
    await share_with(db, nb.id, user_b, "read")
    await db.commit()

    ctx = _ctx(db, nb.id, user_b.id)
    result = await _dispatch_register_source(ctx, {"url": "https://example.com/readonly"})
    assert "권한이 없습니다" in result

    rows = (await db.execute(select(Source).where(Source.notebook_id == nb.id))).scalars().all()
    assert len(rows) == 0


async def test_register_source_allows_write_shared_user(db, user_a, user_b):
    from biznotes_core.services.agents.runtime import _dispatch_register_source

    nb = await _make_notebook(db, user_a.id)
    await share_with(db, nb.id, user_b, "write")
    await db.commit()

    ctx = _ctx(db, nb.id, user_b.id)
    result = await _dispatch_register_source(ctx, {"url": "https://example.com/writer"})
    assert "등록했습니다" in result


async def test_register_source_system_run_bypasses_permission_check(db, user_a):
    """ctx.user_id가 None(시스템 실행)이면 사용자 스코프 권한 검사가 적용되지 않는다."""
    from biznotes_core.services.agents.runtime import _dispatch_register_source

    nb = await _make_notebook(db, user_a.id)
    ctx = _ctx(db, nb.id, user_id=None)
    result = await _dispatch_register_source(ctx, {"url": "https://example.com/system"})
    assert "등록했습니다" in result


async def test_register_source_rejects_ssrf(db, user_a):
    from biznotes_core.services.agents.runtime import _dispatch_register_source

    nb = await _make_notebook(db, user_a.id)
    ctx = _ctx(db, nb.id, user_a.id)
    result = await _dispatch_register_source(
        ctx, {"url": "http://169.254.169.254/latest/meta-data"}
    )
    assert "차단" in result

    rows = (await db.execute(select(Source).where(Source.notebook_id == nb.id))).scalars().all()
    assert len(rows) == 0


async def test_register_source_duplicate_url_reports_existing(db, user_a):
    from biznotes_core.services.agents.runtime import _dispatch_register_source

    nb = await _make_notebook(db, user_a.id)
    ctx = _ctx(db, nb.id, user_a.id)
    first = await _dispatch_register_source(ctx, {"url": "https://example.com/dup"})
    assert "등록했습니다" in first

    second = await _dispatch_register_source(ctx, {"url": "https://example.com/dup"})
    assert "이미 등록된 소스" in second

    rows = (await db.execute(select(Source).where(Source.notebook_id == nb.id))).scalars().all()
    assert len(rows) == 1


async def test_register_source_run_scoped_limit_blocks_after_threshold(db, user_a, monkeypatch):
    from biznotes_core.services.agents import runtime as runtime_mod
    from biznotes_core.services.agents.nodes import llm as nodes_llm

    # _dispatch_register_source(nodes/llm.py, R2-5 이동)가 자기 모듈 네임스페이스에서
    # _REGISTER_SOURCE_RUN_LIMIT을 참조하므로, runtime 재수출만 패치하면 실제 판정에
    # 반영되지 않는다 — nodes.llm도 함께 패치해야 seam이 보존된다.
    monkeypatch.setattr(runtime_mod, "_REGISTER_SOURCE_RUN_LIMIT", 2)
    monkeypatch.setattr(nodes_llm, "_REGISTER_SOURCE_RUN_LIMIT", 2)
    nb = await _make_notebook(db, user_a.id)
    ctx = _ctx(db, nb.id, user_a.id)

    r1 = await runtime_mod._dispatch_register_source(ctx, {"url": "https://example.com/1"})
    r2 = await runtime_mod._dispatch_register_source(ctx, {"url": "https://example.com/2"})
    r3 = await runtime_mod._dispatch_register_source(ctx, {"url": "https://example.com/3"})

    assert "등록했습니다" in r1
    assert "등록했습니다" in r2
    assert "상한" in r3

    rows = (await db.execute(select(Source).where(Source.notebook_id == nb.id))).scalars().all()
    assert len(rows) == 2, "상한 초과 후에는 소스가 추가 생성되지 않아야 한다"


async def test_register_source_notebook_source_limit_enforced(db, user_a, monkeypatch):
    set_source_limit(monkeypatch, 0)
    nb = await _make_notebook(db, user_a.id)
    ctx = _ctx(db, nb.id, user_a.id)
    from biznotes_core.services.agents.runtime import _dispatch_register_source

    result = await _dispatch_register_source(ctx, {"url": "https://example.com/x"})
    assert "최대" in result


# ── HITL 상시 게이트 강제 ─────────────────────────────────────────────────


async def test_register_source_always_gated_even_with_matching_auto_approve_policy(
    db, user_a, mock_llm, monkeypatch
):
    """auto_approve_tools에 register_source를 억지로 넣어도(예: 검증 우회 데이터)
    런타임이 항상 게이트를 강제한다(D-14 이중 방어)."""
    from biznotes_core.services.agents.runtime import _exec_react_agent

    nb = await _make_notebook(db, user_a.id)
    _patch_completer(
        monkeypatch, lambda i: _tool_turn("register_source", {"url": "https://example.com/a"})
    )

    ctx = _ctx(db, nb.id, user_a.id)
    with pytest.raises(ApprovalRequired) as exc_info:
        await _exec_react_agent(
            ctx,
            {
                "id": "r", "type": "react_agent", "goal": "리서치",
                "allowed_tools": ["register_source"],
                # 저장 검증을 우회한 것처럼 억지로 넣은 정책 — 매치되더라도 게이트되어야 함.
                "auto_approve_tools": {"register_source": {}},
            },
            {},
        )
    checkpoint = exc_info.value.checkpoint
    assert checkpoint["tool_name"] == "register_source"


async def test_register_source_gated_without_policy(db, user_a, mock_llm, monkeypatch):
    from biznotes_core.services.agents.runtime import _exec_react_agent

    nb = await _make_notebook(db, user_a.id)
    _patch_completer(
        monkeypatch, lambda i: _tool_turn("register_source", {"url": "https://example.com/a"})
    )

    ctx = _ctx(db, nb.id, user_a.id)
    with pytest.raises(ApprovalRequired):
        await _exec_react_agent(
            ctx,
            {
                "id": "r", "type": "react_agent", "goal": "리서치",
                "allowed_tools": ["register_source"],
            },
            {},
        )


async def test_register_source_approved_resume_executes_once(db, user_a, mock_llm, monkeypatch):
    nb = await _make_notebook(db, user_a.id)

    def script(i):
        if i == 0:
            return _tool_turn("register_source", {"url": "https://example.com/resume"})
        return _final_turn("완료")

    _patch_completer(monkeypatch, script)

    definition = {
        "nodes": [
            {
                "id": "r", "type": "react_agent", "goal": "리서치",
                "allowed_tools": ["register_source"],
            }
        ],
        "edges": [],
    }
    ctx1 = _ctx(db, nb.id, user_a.id)
    summary1 = await execute_agent(ctx1, "workflow", definition)
    assert summary1 == ""
    checkpoint = dict(ctx1.pending_approval)
    checkpoint["react_approval_status"] = "approved"

    ctx2 = _ctx(db, nb.id, user_a.id)
    definition_resume = {
        "nodes": [
            {
                "id": "r", "type": "react_agent", "goal": "리서치",
                "allowed_tools": ["register_source"], "_resume": checkpoint,
            }
        ],
        "edges": [],
    }
    # v0.10.6(#91): `_resume`은 워커가 소비한 checkpoint의 앵커가 있어야 재개로 인정된다 —
    # 워커와 같은 순서로 restore_run_context가 신뢰 표식을 먼저 찍는다(agent.py:258-265).
    approval.restore_run_context(ctx2, checkpoint)
    summary2 = await execute_agent(
        ctx2, "workflow", definition_resume, resume_state=checkpoint["graph_state"]
    )
    assert summary2 == "완료"


async def test_checkpoint_carries_register_source_calls_for_worker_restoration(
    db, user_a, mock_llm, monkeypatch
):
    """[R22B-03] 체크포인트에 ctx_register_source_calls가 실려야 worker(agent.py)가
    재개 시 RunContext.register_source_calls를 복원할 수 있다 — 없으면 매 승인마다
    카운터가 0으로 리셋되어 D-15의 run당 상한이 무력화된다."""
    from biznotes_core.services.agents import runtime as runtime_mod
    from biznotes_core.services.agents.nodes import llm as nodes_llm

    # seam 보존 사유는 test_register_source_run_scoped_limit_blocks_after_threshold 참조.
    monkeypatch.setattr(runtime_mod, "_REGISTER_SOURCE_RUN_LIMIT", 1)
    monkeypatch.setattr(nodes_llm, "_REGISTER_SOURCE_RUN_LIMIT", 1)
    nb = await _make_notebook(db, user_a.id)
    _patch_completer(
        monkeypatch, lambda i: _tool_turn("register_source", {"url": "https://example.com/first"})
    )

    ctx1 = _ctx(db, nb.id, user_a.id)
    ctx1.register_source_calls[0] = 1  # 이전 턴에서 이미 1건 등록된 상태를 시뮬레이션
    with pytest.raises(ApprovalRequired) as exc_info:
        await runtime_mod._exec_react_agent(
            ctx1,
            {
                "id": "r", "type": "react_agent", "goal": "리서치",
                "allowed_tools": ["register_source"],
            },
            {},
        )
    checkpoint = exc_info.value.checkpoint
    assert checkpoint["ctx_register_source_calls"] == 1

    # worker(agent.py)가 하는 것처럼 재개 시 카운터를 체크포인트 값으로 복원.
    checkpoint = dict(checkpoint)
    checkpoint["react_approval_status"] = "approved"
    ctx2 = _ctx(db, nb.id, user_a.id)
    ctx2.register_source_calls[0] = checkpoint["ctx_register_source_calls"]

    definition_resume = {
        "nodes": [
            {
                "id": "r", "type": "react_agent", "goal": "리서치",
                "allowed_tools": ["register_source"], "_resume": checkpoint,
            }
        ],
        "edges": [],
    }
    # v0.10.6(#91): `_resume`은 워커가 소비한 checkpoint의 앵커가 있어야 재개로 인정된다 —
    # 워커와 같은 순서로 restore_run_context가 신뢰 표식을 먼저 찍는다(agent.py:258-265).
    approval.restore_run_context(ctx2, checkpoint)
    await execute_agent(
        ctx2, "workflow", definition_resume, resume_state=checkpoint["graph_state"]
    )
    # 복원된 카운터가 이미 상한(1)에 도달한 상태이므로, 승인된 이 호출조차 실행되지
    # 않고 상한 초과 메시지로 거부되어야 한다 — 리셋됐다면(버그) 카운터가 0에서
    # 시작해 이 호출이 통과하며 소스가 1건 생성됐을 것이다.
    rows = (await db.execute(select(Source).where(Source.notebook_id == nb.id))).scalars().all()
    assert len(rows) == 0, "복원된 카운터가 리셋되지 않고 상한을 계속 강제해야 한다"


# ── 저장 시 검증(admin/agents.py·workflows.py) ───────────────────────────


def test_validate_definition_rejects_register_source_in_auto_approve_tools():
    from fastapi import HTTPException

    from app.api.v1.admin.agents import _validate_definition

    definition = {
        "nodes": [
            {
                "id": "r", "type": "react_agent", "goal": "x",
                "auto_approve_tools": {"register_source": {}},
            }
        ],
        "edges": [],
    }
    with pytest.raises(HTTPException) as exc_info:
        _validate_definition("workflow", definition)
    assert exc_info.value.status_code == 422
    assert "register_source" in str(exc_info.value.detail)


def test_validate_definition_allows_other_auto_approve_tools():
    from app.api.v1.admin.agents import _validate_definition

    definition = {
        "nodes": [
            {
                "id": "r", "type": "react_agent", "goal": "x",
                "auto_approve_tools": {"call_agent": {"agent_ids": [str(uuid.uuid4())]}},
            }
        ],
        "edges": [],
    }
    _validate_definition("workflow", definition)  # 예외 없이 통과해야 함


def test_workflows_validate_structure_rejects_register_source_in_auto_approve_tools():
    from fastapi import HTTPException

    from app.api.v1.workflows import _validate_structure

    definition = {
        "nodes": [
            {
                "id": "r", "type": "react_agent", "goal": "x",
                "auto_approve_tools": {"register_source": {"anything": True}},
            }
        ],
        "edges": [],
    }
    with pytest.raises(HTTPException) as exc_info:
        _validate_structure(definition, {})
    assert exc_info.value.status_code == 422


# ── deep_research_agent 시드 ──────────────────────────────────────────────


async def test_seed_deep_research_agent_is_single_react_agent_workflow(db):
    from scripts.seed import DEEP_RESEARCH_AGENT_NAME, upsert_deep_research_agent

    await upsert_deep_research_agent()

    agent = (
        await db.execute(select(Agent).where(Agent.name == DEEP_RESEARCH_AGENT_NAME))
    ).scalar_one()
    assert agent.kind == "workflow"
    assert agent.enabled is True
    nodes = agent.definition["nodes"]
    assert len(nodes) == 1
    node = nodes[0]
    assert node["type"] == "react_agent"
    assert set(node["allowed_tools"]) == {"rag_search", "web_search", "register_source"}
    assert "register_source" not in (node.get("auto_approve_tools") or {})
    assert node["max_iters"] < runtime._REACT_DEFAULTS["max_iters"] + 3  # 보수적 설정값

    # 멱등성 — 재실행 시 동일 이름으로 중복 생성되지 않음.
    await upsert_deep_research_agent()
    count = (
        await db.execute(select(Agent).where(Agent.name == DEEP_RESEARCH_AGENT_NAME))
    ).scalars().all()
    assert len(count) == 1
