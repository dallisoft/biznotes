"""llm 노드 실행 로그의 실제 사용 모델·폴백 여부와, 실패 때 시도한 모델(#359).

워크플로우를 `execute_agent`로 돌리고 노드 로그 기록(`_log_node_finish`)을 엿본다. 게이트웨이는
실경로이고 모델 조회(`resolve_chain`·`resolve_model`)와 서킷의 Redis만 대역이다. 응답은 mock
프로바이더 실경로, 후보 실패는 `complete_stream` 앞에서 낸다.
"""

import uuid
from typing import Any

import pytest

from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.services.agents import graph
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError
from biznotes_core.services.agents.node_errors import describe_node_failure
from biznotes_core.services.gateway import circuit
from biznotes_core.services.gateway import llm as gateway
from biznotes_core.services.gateway.llm import (
    GatewayCallError,
    GatewayContextLengthError,
    ResolvedModel,
)


def _model(name: str) -> ResolvedModel:
    provider = LLMProvider(id=uuid.uuid4(), name=f"p-{name}", type="mock")
    model = LLMModel(
        id=uuid.uuid4(),
        provider_id=provider.id,
        model_name=f"mock-{name}",
        display_name=name,
        capabilities=["llm"],
    )
    return ResolvedModel(model=model, provider=provider)


def _meta(m: ResolvedModel) -> dict[str, str]:
    return {"id": str(m.model.id), "display_name": m.model.display_name}


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """체인·후보별 실패·열린 서킷을 정하고, 노드 로그를 모은다."""
    state: dict[str, Any] = {"chain": [], "fail": {}, "open": [], "logs": {}}
    real = gateway.complete_stream

    async def chain(db: Any, model_id: Any, modality: str = "llm") -> list[ResolvedModel]:
        return state["chain"]

    async def resolve(db: Any, model_id: Any, modality: str = "llm") -> ResolvedModel:
        return state["chain"][0]

    async def stream(candidate: ResolvedModel, *a: Any, **k: Any) -> Any:
        error = state["fail"].get(candidate.model.display_name)
        if error is not None:
            raise error
        return await real(candidate, *a, **k)

    async def is_open(pid: Any) -> bool:
        return any(m.provider.id == pid for m in state["open"])

    async def noop(_pid: Any) -> None:
        return None

    async def failure(_pid: Any) -> bool:
        return False

    async def log_start(run_id: Any, node_id: str, *a: Any, **k: Any) -> str:
        return node_id

    async def log_finish(log_id: str, **kw: Any) -> bool:
        state["logs"][log_id] = kw
        return True

    monkeypatch.setattr(gateway, "resolve_chain", chain)
    monkeypatch.setattr(gateway, "resolve_model", resolve)
    monkeypatch.setattr(gateway, "complete_stream", stream)
    monkeypatch.setattr(circuit, "is_open", is_open)
    monkeypatch.setattr(circuit, "record_success", noop)
    monkeypatch.setattr(circuit, "record_failure", failure)
    monkeypatch.setattr(graph, "_log_node_start", log_start)
    monkeypatch.setattr(graph, "_log_node_finish", log_finish)
    return state


def _ctx() -> RunContext:
    return RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={})  # type: ignore[arg-type]


async def _execute(node: dict[str, Any], ctx: RunContext | None = None) -> RunContext:
    ctx = ctx or _ctx()
    await graph.execute_agent(ctx, "workflow", {"nodes": [node], "edges": []})
    return ctx


LLM = {"id": "gen", "type": "llm", "prompt": "요약해 주세요"}


async def test_log_carries_the_answering_model(run):
    default = _model("gpt-oss-120b")
    run["chain"] = [default]

    await _execute(LLM)

    out = run["logs"]["gen"]["output_data"]
    assert (out["model"], out["fallback_used"]) == (_meta(default), False)
    assert out["text"]


async def test_log_shows_the_fallback_model(run):
    primary, backup = _model("gemini-3.8-flash"), _model("gpt-oss-120b")
    run["chain"] = [primary, backup]
    run["fail"] = {"gemini-3.8-flash": GatewayCallError("LLM 호출 실패: 500")}

    ctx = await _execute(LLM)

    out = run["logs"]["gen"]["output_data"]
    assert (out["model"], out["fallback_used"]) == (_meta(backup), True)
    # 채팅 출력 메타데이터도 실제 응답 모델이다(react 노드와 같은 규칙)
    assert (ctx.chat_used_model_id, ctx.chat_used_model_name, ctx.chat_fallback_used) == (
        backup.model.id, "gpt-oss-120b", True
    )


async def test_failed_log_lists_every_attempted_model(run):
    a, b, c = _model("a"), _model("b"), _model("c")
    run["chain"] = [a, b, c]
    run["open"] = [b]
    run["fail"] = {
        "a": GatewayContextLengthError("컨텍스트 초과"),
        "c": GatewayCallError("LLM 호출 실패: 공급자 원문 secret-provider"),
    }

    with pytest.raises(AgentRuntimeError):
        await _execute(LLM)

    log = run["logs"]["gen"]
    assert (log["status"], log["error_code"]) == ("failed", "model_call_failed")
    assert log["output_data"] == {
        "model_attempts": [
            {**_meta(a), "error_code": "context_length_exceeded"},
            {**_meta(b), "error_code": "circuit_open"},
            {**_meta(c), "error_code": "model_call_failed"},
        ]
    }
    assert "secret-provider" not in str(log["output_data"])


async def test_failed_context_only_run_names_the_model(run):
    small = _model("gpt-oss-120b")
    run["chain"] = [small]
    run["fail"] = {"gpt-oss-120b": GatewayContextLengthError("컨텍스트 초과")}

    with pytest.raises(AgentRuntimeError):
        await _execute(LLM)

    log = run["logs"]["gen"]
    assert log["error_code"] == "context_length_exceeded"
    assert log["output_data"] == {
        "model_attempts": [{**_meta(small), "error_code": "context_length_exceeded"}]
    }


async def test_tool_llm_node_logs_its_model(run):
    model = _model("tool-model")
    run["chain"] = [model]

    async def no_search(*_a: Any, **_k: Any) -> str:
        return "검색 결과 없음"

    from biznotes_core.services.agents import runtime

    original = runtime._dispatch_builtin_tool
    runtime._dispatch_builtin_tool = no_search  # type: ignore[assignment]
    try:
        await _execute({**LLM, "tools": ["web_search"]})
    finally:
        runtime._dispatch_builtin_tool = original  # type: ignore[assignment]

    out = run["logs"]["gen"]["output_data"]
    assert (out["model"], out["fallback_used"]) == (_meta(model), False)


def test_node_failure_output_comes_from_the_cause_chain():
    err = GatewayCallError("x")
    err.model_attempts = [{"id": "m", "display_name": "모델", "error_code": "model_call_failed"}]
    try:
        try:
            raise err
        except GatewayCallError as inner:
            raise AgentRuntimeError("노드 [gen] 실행 실패: 모델 호출에 실패했습니다") from inner
    except AgentRuntimeError as outer:
        failure = describe_node_failure(outer)
    assert failure.output == {"model_attempts": err.model_attempts}
    # 시도 목록이 없으면 출력도 없다
    assert describe_node_failure(GatewayCallError("y")).output is None
    assert describe_node_failure(RuntimeError("z")).output is None


def test_product_exceptions_can_attach_log_output():
    class ProductError(Exception):
        node_log_output = {"quota": "daily"}

    class NotADict(Exception):
        node_log_output = ["x"]

    assert describe_node_failure(ProductError()).output == {"quota": "daily"}
    assert describe_node_failure(NotADict()).output is None
