"""llm 노드 `tools`의 부작용 도구 거부 (#70, #49).

llm 노드의 도구 루프(guards=None)에는 HITL 승인 게이트가 없다. 등록된 부작용 도구는 저장
검증 헬퍼와 실행 시점 관문이 같은 규칙으로 거부한다.
계획 `docs/plans/llm-node-side-effect-tools/plan.md`의 수락 기준 1~6·8을 DB 없이 확인한다
(중첩 경로인 수락 기준 7은 PG 스모크로 검증하고 계획 문서에 기록).
"""

import json
import uuid
from typing import Any

import pytest

from biznotes_core.services.agents import tool_registry
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError, ApprovalRequired
from biznotes_core.services.agents.nodes import llm as llm_node
from biznotes_core.services.agents.react import loop
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult

SIDE_EFFECT = ["call_agent", "http_request", "mcp_call", "register_source", "run_skill_script"]
SAFE = ["rag_search", "web_search", "source_load", "run_skill", "activate_skill", "read_skill_file"]


def _ctx(**kw: Any) -> RunContext:
    return RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={}, **kw)  # type: ignore[arg-type]


def _llm(tools: Any) -> dict[str, Any]:
    return {"id": "gen", "type": "llm", "prompt": "질문", "tools": tools}


def _assistant_call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


_DONE = ToolTurnResult(
    content="끝",
    assistant_message={"role": "assistant", "content": "끝"},
    usage={"total_tokens": 10},
)


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """모델 대역 — 첫 턴에 rag_search를 부르고 다음 턴에 끝낸다.

    턴마다 받은 도구 이름을 기록한다."""
    turns: list[list[str]] = []

    async def fake(db: Any, model_id: Any, messages: Any, tools: Any, **kw: Any) -> Any:
        turns.append([t["function"]["name"] for t in tools])
        if len(turns) == 1:
            call = ToolCall(id="c1", name="rag_search", arguments={"query": "q"})
            return ToolTurnResult(
                tool_calls=[call],
                assistant_message=_assistant_call("c1", "rag_search", {"query": "q"}),
            )
        return _DONE

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake)
    return turns


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def start(*a: Any, **kw: Any) -> None:
        return None

    async def finish(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(loop, "_log_node_start", start)
    monkeypatch.setattr(loop, "_log_node_finish", finish)


@pytest.fixture
def specs_calls(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """`build_tool_specs` 호출 기록 — 실제 구현은 그대로 부른다."""
    calls: list[Any] = []
    real = tool_registry.build_tool_specs

    async def spy(ctx: Any, allowed: Any = None) -> Any:
        calls.append(allowed)
        return await real(ctx, allowed)

    monkeypatch.setattr(tool_registry, "build_tool_specs", spy)
    return calls


# ── 수락 기준 5: 저장 검증 헬퍼 ──────────────────────────────────────────


def test_helper_reports_sorted_unique_side_effect_tools_of_llm_nodes() -> None:
    nodes = [
        _llm(["rag_search", "register_source", "http_request"]),
        {**_llm(["http_request", "mcp_call"]), "id": "gen2"},
    ]

    assert tool_registry.find_llm_node_side_effect_tools(nodes) == [
        "http_request",
        "mcp_call",
        "register_source",
    ]


@pytest.mark.parametrize("name", SIDE_EFFECT)
def test_helper_rejects_every_registered_side_effect_tool(name: str) -> None:
    assert tool_registry.find_llm_node_side_effect_tools([_llm([name])]) == [name]


def test_helper_ignores_other_nodes_and_malformed_tools() -> None:
    nodes: list[Any] = [
        # react_agent는 게이트가 있다 — 이 헬퍼의 대상이 아니다
        {"id": "r", "type": "react_agent", "allowed_tools": ["http_request"]},
        {"id": "h", "type": "http", "url": "https://example.com", "tools": ["http_request"]},
        _llm("http_request"),  # 리스트가 아니다 — 실행 경로도 도구 루프를 타지 않는다
        _llm(None),
        _llm([]),
        "not-a-node",
    ]

    assert tool_registry.find_llm_node_side_effect_tools(nodes) == []


def test_helper_allows_safe_mcp_token_unknown_and_non_string_entries() -> None:
    """`mcp` 토큰과 안전 도구, 미등록 이름(`build_tool_specs`가 노출하지 않는다)은 위반이 아니다.
    원소는 str()로 비교한다 — None은 "None"(미등록)이다."""
    nodes = [_llm([*SAFE, "mcp", "typo_tool", None, 42])]

    assert tool_registry.find_llm_node_side_effect_tools(nodes) == []


def test_helper_does_not_depend_on_discovered_mcp_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """리뷰 R2 — 디스커버리가 안전 목록을 채워도 판정이 바뀌지 않는다.

    저장(API 프로세스)과 실행(워커)이 같은 답을 낸다."""
    nodes = [_llm(["mcp_1234abcd_search", "http_request"])]
    before = tool_registry.find_llm_node_side_effect_tools(nodes)

    monkeypatch.setattr(
        tool_registry, "_safe_discovered_mcp_tools", {"mcp_1234abcd_search", "http_request"}
    )

    assert tool_registry.find_llm_node_side_effect_tools(nodes) == before == ["http_request"]


# ── 수락 기준 1·3: 실행 시점 거부 ────────────────────────────────────────


@pytest.mark.parametrize("name", SIDE_EFFECT)
async def test_llm_node_with_side_effect_tool_fails_before_exposing_tools(
    name: str, model: list[list[str]], specs_calls: list[Any]
) -> None:
    with pytest.raises(AgentRuntimeError, match="부작용 도구") as exc:
        await llm_node._exec_llm(_ctx(source_ids=[]), _llm(["rag_search", name]), {})

    assert f"['{name}']" in str(exc.value)
    assert model == []  # 모델을 부르지 않았다
    assert specs_calls == []  # 도구를 노출하지도(MCP 디스커버리·DB 조회) 않았다


async def test_error_lists_all_violations_sorted(model: list[list[str]]) -> None:
    with pytest.raises(AgentRuntimeError) as exc:
        await llm_node._exec_llm(
            _ctx(), _llm(["register_source", "rag_search", "http_request"]), {}
        )

    assert "['http_request', 'register_source']" in str(exc.value)
    assert "react_agent" in str(exc.value)


async def test_register_source_is_never_dispatched_from_llm_node(
    monkeypatch: pytest.MonkeyPatch, model: list[list[str]]
) -> None:
    """#49 — llm 노드의 register_source는 디스패치 전에 막힌다.

    그래서 병렬 분기에서 같은 URL을 동시에 등록할 수 없다."""
    dispatched: list[Any] = []

    async def spy(*a: Any, **kw: Any) -> str:
        dispatched.append(a)
        return "registered"

    monkeypatch.setattr(llm_node, "_dispatch_register_source", spy)
    ctx = _ctx(in_parallel_branch=True)

    with pytest.raises(AgentRuntimeError):
        await llm_node._exec_llm(ctx, _llm(["register_source"]), {})

    assert dispatched == []


async def test_tool_loop_without_guards_requires_explicit_tools(model: list[list[str]]) -> None:
    """리뷰 R3 — guards 없이 allowed=None이면 기본 도구의 call_agent가 게이트 없이 노출된다."""
    with pytest.raises(AgentRuntimeError, match="도구 목록"):
        await loop._exec_llm_with_tools(_ctx(), _llm(None), [], None, None)

    assert model == []


# ── 수락 기준 8: 노출 뒤 이중 방어 ───────────────────────────────────────


def _fake_specs(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> None:
    async def fake(ctx: Any, allowed: Any = None) -> Any:
        async def dispatch(ctx: Any, name: str, args: dict[str, Any]) -> str:
            return "ok"

        specs = [{"type": "function", "function": {"name": n, "parameters": {}}} for n in names]
        return specs, {n: dispatch for n in names}

    monkeypatch.setattr(tool_registry, "build_tool_specs", fake)


async def test_exposed_unregistered_tool_is_rejected(
    monkeypatch: pytest.MonkeyPatch, model: list[list[str]]
) -> None:
    """`build_tool_specs`가 미등록 이름을 노출하게 바뀌어도(분류 누락) llm 경로는
    실행하지 않는다."""
    _fake_specs(monkeypatch, ["rag_search", "new_side_effect_tool"])

    with pytest.raises(AgentRuntimeError, match="new_side_effect_tool"):
        await llm_node._exec_llm(_ctx(), _llm(["rag_search", "new_side_effect_tool"]), {})

    assert model == []


async def test_exposed_registered_side_effect_tool_is_rejected(
    monkeypatch: pytest.MonkeyPatch, model: list[list[str]]
) -> None:
    """사전 검사를 통과했는데 노출 단계가 부작용 도구를 내놓으면(분기 추가 실수) 실행하지 않는다."""
    _fake_specs(monkeypatch, ["rag_search", "http_request"])

    with pytest.raises(AgentRuntimeError, match=r"\['http_request'\]"):
        await llm_node._exec_llm(_ctx(), _llm(["rag_search"]), {})

    assert model == []


async def test_exposed_safe_discovered_mcp_tool_is_allowed(
    monkeypatch: pytest.MonkeyPatch, model: list[list[str]], audit: None
) -> None:
    """`mcp` 토큰이 노출하는 디스커버리 도구는 allowlist를 통과한 read-only 도구다 — 허용한다."""
    _fake_specs(monkeypatch, ["rag_search", "mcp_1234abcd_search"])
    monkeypatch.setattr(tool_registry, "_safe_discovered_mcp_tools", {"mcp_1234abcd_search"})

    out = await llm_node._exec_llm(_ctx(source_ids=[]), _llm(["rag_search", "mcp"]), {})

    assert out["text"] == "끝"
    assert model[0] == ["rag_search", "mcp_1234abcd_search"]


# ── 수락 기준 2·4·6: 바뀌지 않는 경로 ────────────────────────────────────


async def test_safe_tools_and_unknown_names_run_as_before(
    model: list[list[str]], audit: None
) -> None:
    """안전 도구와 미등록 이름(지금처럼 무시)만 있으면 도구 루프가 그대로 돈다."""
    out = await llm_node._exec_llm(
        _ctx(source_ids=[]), _llm(["rag_search", "web_search", "typo_tool"]), {}
    )

    assert out["text"] == "끝"
    assert [t["tool"] for t in out["tool_calls"]] == ["rag_search"]
    assert model[0] == ["rag_search", "web_search"]


async def test_react_agent_side_effect_tool_still_goes_to_the_gate(
    monkeypatch: pytest.MonkeyPatch, audit: None
) -> None:
    """react_agent(guards 있음)는 같은 도구를 거부하지 않고 승인 게이트로 보낸다."""
    call = ToolCall(id="c1", name="http_request", arguments={"url": "https://example.com"})

    async def fake(db: Any, model_id: Any, messages: Any, tools: Any, **kw: Any) -> Any:
        return ToolTurnResult(
            tool_calls=[call],
            assistant_message=_assistant_call("c1", "http_request", call.arguments),
        )

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake)
    node = {"id": "react", "type": "react_agent", "goal": "목표", "allowed_tools": ["http_request"]}

    with pytest.raises(ApprovalRequired):
        await loop.run_react_node(_ctx(), node, {})


async def test_registry_still_exposes_mcp_call_when_called_directly() -> None:
    """레지스트리 수준 하위호환 — 거부는 llm 노드 관문에서만 한다."""
    specs, dispatchers = await tool_registry.build_tool_specs(_ctx(), ["mcp_call"])

    assert [s["function"]["name"] for s in specs] == ["mcp_call"]
    assert "mcp_call" in dispatchers


# ── 수락 기준 8: 분류 누락 회귀 ──────────────────────────────────────────


def test_builtin_spec_keys_match_their_function_names() -> None:
    """키와 spec 이름이 다르면 llm 노드 규칙(키 기준)과 노출 이름이 어긋난다."""
    for key, spec in tool_registry._BUILTIN_SPECS.items():
        assert spec["function"]["name"] == key


async def test_every_statically_exposable_tool_is_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`build_tool_specs`가 정적 분기로 내놓을 수 있는 이름은 전부 `TOOL_SIDE_EFFECT`에 있다 —
    분기를 더하고 분류를 빠뜨리면 llm 노드가 그 도구를 게이트 없이 노출한다(재리뷰 N1)."""
    from biznotes_core.services.skills import sandbox

    def spec(name: str) -> dict[str, Any]:
        return {"type": "function", "function": {"name": name, "parameters": {}}}

    async def dispatch(ctx: Any, name: str, args: dict[str, Any]) -> str:
        return ""

    async def published(ctx: Any) -> Any:
        return spec("run_skill")

    async def runtime_specs(ctx: Any) -> Any:
        return {n: (spec(n), dispatch) for n in tool_registry._SKILL_RUNTIME_TOOLS}

    async def script_spec(ctx: Any) -> Any:
        return spec(tool_registry._SKILL_SCRIPT_TOOL)

    monkeypatch.setattr(tool_registry, "_published_skill_spec", published)
    monkeypatch.setattr(tool_registry, "_skill_runtime_specs", runtime_specs)
    monkeypatch.setattr(sandbox, "run_skill_script_spec", script_spec)

    # 기대 목록을 코드에서 파생하지 않고 적는다 — `_static_tool_names()`와 `TOOL_SIDE_EFFECT`는
    # 같은 원천이라 서로 대조하면 동어반복이다. 도구를 추가하면 이 목록도 함께 고친다
    expected = {
        "rag_search", "source_load", "web_search", "http_request", "mcp_call",
        "register_source", "call_agent", "run_skill", "activate_skill", "read_skill_file",
        "run_skill_script", "artifact_list", "artifact_read",
        "source_list",
    }  # fmt: skip
    _, dispatchers = await tool_registry.build_tool_specs(_ctx(), sorted(expected))

    assert set(dispatchers) == expected
    assert tool_registry._static_tool_names() == expected
    assert set(dispatchers) <= tool_registry.TOOL_SIDE_EFFECT.keys()
