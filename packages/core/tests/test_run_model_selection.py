"""run 단위 모델 지정(#359 2/2) — 열 계약과 llm·react 노드의 1차 모델 순서.

1차 모델: 노드 `model_id` → `RunContext.run_model_id` → `chat_model_id` → 기본 모델(None).
모델 조회(`resolve_chain`·`complete_with_tools`)만 대역이다. 생성 검증과 실제 DB는
`tests/test_run_model_selection_pg.py`가 본다.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from biznotes_core.models import AgentRun, LLMModel, LLMProvider
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.nodes.llm import _exec_llm
from biznotes_core.services.agents.react import loop
from biznotes_core.services.gateway import circuit
from biznotes_core.services.gateway import llm as gateway
from biznotes_core.services.gateway.llm import ResolvedModel, ToolTurnResult

NODE_MODEL, RUN_MODEL, CHAT_MODEL = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _mock() -> ResolvedModel:
    provider = LLMProvider(id=uuid.uuid4(), name="p", type="mock")
    model = LLMModel(
        id=uuid.uuid4(), provider_id=provider.id, model_name="m", display_name="m",
        capabilities=["llm"],
    )
    return ResolvedModel(model=model, provider=provider)


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """게이트웨이에 1차 모델로 요청된 id들."""
    seen: list[Any] = []

    async def chain(db: Any, model_id: Any, modality: str = "llm") -> list[ResolvedModel]:
        seen.append(model_id)
        return [_mock()]

    async def tools(db: Any, model_id: Any, messages: Any, tools: Any, **kw: Any) -> Any:
        seen.append(model_id)
        done = {"role": "assistant", "content": "끝"}
        return ToolTurnResult(content="끝", assistant_message=done)

    async def closed(_pid: Any) -> bool:
        return False

    async def noop(_pid: Any) -> None:
        return None

    async def log_noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(gateway, "resolve_chain", chain)
    monkeypatch.setattr(gateway, "complete_with_tools", tools)
    monkeypatch.setattr(circuit, "is_open", closed)
    monkeypatch.setattr(circuit, "record_success", noop)
    monkeypatch.setattr(loop, "_log_node_start", log_noop)
    monkeypatch.setattr(loop, "_log_node_finish", log_noop)
    return seen


def _ctx(**kw: Any) -> RunContext:
    return RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={}, **kw)  # type: ignore[arg-type]


CASES = [
    # (노드 model_id, run 모델, 채팅 모델) → 1차 모델
    pytest.param(NODE_MODEL, RUN_MODEL, CHAT_MODEL, NODE_MODEL, id="노드가-이긴다"),
    pytest.param(None, RUN_MODEL, CHAT_MODEL, RUN_MODEL, id="run-모델"),
    pytest.param(None, None, CHAT_MODEL, CHAT_MODEL, id="채팅-모델(D2)"),
    pytest.param(None, None, None, None, id="기본-모델"),
]


@pytest.mark.parametrize(("node_model", "run_model", "chat_model", "expected"), CASES)
async def test_llm_node_primary_model_order(asked, node_model, run_model, chat_model, expected):
    node: dict[str, Any] = {"id": "gen", "type": "llm", "prompt": "요약"}
    if node_model is not None:
        node["model_id"] = str(node_model)

    await _exec_llm(_ctx(run_model_id=run_model, chat_model_id=chat_model), node, {})

    assert asked == [expected]


@pytest.mark.parametrize(("node_model", "run_model", "chat_model", "expected"), CASES)
async def test_react_node_primary_model_order(asked, node_model, run_model, chat_model, expected):
    node: dict[str, Any] = {
        "id": "r", "type": "react_agent", "goal": "목표", "allowed_tools": ["rag_search"]
    }
    if node_model is not None:
        node["model_id"] = str(node_model)

    ctx = _ctx(run_model_id=run_model, chat_model_id=chat_model, source_ids=[])
    await loop.run_react_node(ctx, node, {})

    assert asked[0] == expected


async def test_llm_tool_node_uses_the_run_model(asked, monkeypatch):
    async def no_search(*_a: Any, **_k: Any) -> str:
        return "없음"

    from biznotes_core.services.agents import runtime

    monkeypatch.setattr(runtime, "_dispatch_builtin_tool", no_search)
    node = {"id": "gen", "type": "llm", "prompt": "요약", "tools": ["web_search"]}

    await _exec_llm(_ctx(run_model_id=RUN_MODEL), node, {})

    assert asked and set(asked) == {RUN_MODEL}


def test_child_context_keeps_the_run_model():
    ctx = _ctx(run_model_id=RUN_MODEL, chat_model_id=CHAT_MODEL)
    child = ctx.child(uuid.uuid4(), params={})
    assert (child.run_model_id, child.chat_model_id) == (RUN_MODEL, CHAT_MODEL)


def test_column_is_a_nullable_set_null_fk():
    col = AgentRun.__table__.c.model_id
    assert col.nullable and col.server_default is None
    [fk] = col.foreign_keys
    assert (fk.target_fullname, fk.ondelete, fk.name) == (
        "llm_models.id",
        "SET NULL",
        "fk_agent_runs_model_id",
    )
    ddl = str(CreateTable(AgentRun.__table__).compile(dialect=postgresql.dialect()))
    assert (
        "CONSTRAINT fk_agent_runs_model_id FOREIGN KEY(model_id) "
        "REFERENCES llm_models (id) ON DELETE SET NULL"
    ) in ddl


def test_positional_construction_keeps_the_old_field_order():
    """리뷰 M1 — 새 필드는 키워드 전용이라 위치 인자로 만든 기존 호출이 밀리지 않는다."""
    used = uuid.uuid4()
    ctx = RunContext(uuid.uuid4(), uuid.uuid4(), None, {}, "chat", "", [], CHAT_MODEL, used)  # type: ignore[arg-type]
    assert (ctx.chat_model_id, ctx.chat_used_model_id, ctx.run_model_id) == (CHAT_MODEL, used, None)
    with pytest.raises(TypeError):
        RunContext(uuid.uuid4(), uuid.uuid4(), None, {}, *([None] * 60))  # type: ignore[arg-type,misc]
