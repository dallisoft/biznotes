# 출처: 원본 제품 저장소 `apps/api/tests/test_tool_calling_characterization.py` @ 1079bbf
#       (sl-agents 사본의 상위집합)
# #1 이관(docs/plans/opt-in-pg-suite/). 필요: PostgreSQL·Valkey(PG 테스트).
# 본문은 원본 그대로 — 바꾼 것: import 정렬(ruff I001).
"""Phase 0 — 기존 `llm` 노드 tool-calling 경로의 회귀 안전망(특성화 테스트).

ReAct 에이전트 도입(REACT_AGENT_PLAN.md) Phase 1에서 `_exec_llm_with_tools`를
`tool_registry` 경유로 일반화 리팩터한다. 그 전에 **현재 동작을 골든으로 고정**해
리팩터 전후 동일성을 입증하기 위한 테스트다(계획 §3 Phase 0, F4·F8 반영).

고정 대상(계획 Phase 0 수락기준):
  - 게이트웨이(`complete_with_tools`)에 전달되는 **메시지 시퀀스**
  - 런타임이 디스패치한 **tool_calls(이름·인자)** 트레이스
  - `ctx.usage` **토큰 누적** 결과

mock 프로바이더(`_mock_tool_turn`)를 사용하므로 외부 키 없이 결정적이다.
이 테스트가 깨지면 = tool-calling의 관측 가능한 동작이 바뀌었다는 신호이며,
Phase 1 리팩터에서는 깨지지 않아야 한다(동일 디스패치/메시지 트레이스).
"""

import copy
import uuid

import pytest

from biznotes_core.services.agents import runtime
from biznotes_core.services.agents.runtime import RunContext, execute_agent


@pytest.fixture
async def _tool_mock_model(db):
    """tool-calling 경로용 mock LLM 모델 — model_id 고정으로 결정성 확보."""
    from biznotes_core.models import LLMModel, LLMProvider

    provider = LLMProvider(name=f"mock-char-{uuid.uuid4().hex[:6]}", type="mock", enabled=True)
    db.add(provider)
    await db.flush()
    model = LLMModel(
        provider_id=provider.id,
        model_name="mock-char",
        display_name="Mock Characterization",
        capabilities=["llm"],
        is_default=False,
        enabled=True,
    )
    db.add(model)
    await db.commit()
    await db.refresh(model)
    return model


@pytest.fixture
def _trace_tool_calling(monkeypatch):
    """tool-calling 경계를 가로채 골든 트레이스를 수집.

    - `complete_with_tools` 호출마다 전달된 messages의 (role, has_tool_calls,
      content 요약) 스냅샷을 기록 → 메시지 시퀀스 고정.
    - `_dispatch_builtin_tool` 호출마다 (name, args)를 기록 → 디스패치 트레이스 고정.
    원본 구현은 그대로 호출한다(동작 변경 없이 관찰만).
    """
    from biznotes_core.services.gateway import llm as gateway_llm

    real_complete = gateway_llm.complete_with_tools
    real_dispatch = runtime._dispatch_builtin_tool

    trace: dict[str, list] = {"turns": [], "dispatches": []}

    async def spy_complete(db, model_id, messages, tools, **kwargs):
        # messages는 루프가 in-place로 키우므로 호출 시점 스냅샷을 깊은 복사로 보존.
        snapshot = []
        for m in copy.deepcopy(messages):
            content = m.get("content")
            snapshot.append(
                {
                    "role": m.get("role"),
                    "has_tool_calls": bool(m.get("tool_calls")),
                    # 결과 귀속(tool_call_id ↔ assistant tool_calls[].id) 등식 단언용.
                    "tool_call_ids": [
                        call["id"] if isinstance(call, dict) else getattr(call, "id", None)
                        for call in (m.get("tool_calls") or [])
                    ],
                    "name": m.get("name"),
                    "tool_call_id": m.get("tool_call_id"),
                    # 본문은 길이만 비교 대상 — 결정적 mock에서 안정적.
                    "content_kind": (
                        "none" if content is None else f"str({len(str(content))})"
                    ),
                }
            )
        trace["turns"].append(
            {"tool_names": [t["function"]["name"] for t in tools], "messages": snapshot}
        )
        # 런타임이 _exec_llm_with_tools에서 전달하는 인자(complete_with_tools는
        # complete_with_tools(ctx.db, model_id, messages, tools, max_tokens=...))를
        # 원본 그대로 위임한다.
        return await real_complete(db, model_id, messages, tools, **kwargs)

    async def spy_dispatch(ctx, name, args):
        trace["dispatches"].append({"name": name, "args": copy.deepcopy(args)})
        return await real_dispatch(ctx, name, args)

    monkeypatch.setattr(gateway_llm, "complete_with_tools", spy_complete)
    # _exec_llm_with_tools는 모듈 네임스페이스에서 complete_with_tools를 import하므로
    # 런타임 모듈 쪽 참조도 함께 패치(런타임이 `from ... import complete_with_tools`를
    # 함수 내부에서 수행하므로 gateway_llm 패치로 충분하지만, 디스패치는 직접 패치).
    monkeypatch.setattr(runtime, "_dispatch_builtin_tool", spy_dispatch)
    return trace


async def test_llm_tools_rag_search_golden_trace(
    db, user_a, _mock_runtime_deps, _tool_mock_model, _trace_tool_calling
):
    """`tools=["rag_search"]` 경로의 메시지 시퀀스·디스패치·usage 골든 고정.

    mock 루프 동작(현재 코드 기준):
      턴1: role:tool 없음 → 첫 도구(rag_search)를 1회 호출
      디스패치: rag_search(query=<프롬프트 앞 120자>) → 결과 없음 → 안내 문자열
      턴2: role:tool 결과가 대화에 있음 → 도구 결과를 종합한 최종 텍스트
    """
    model = _tool_mock_model
    trace = _trace_tool_calling

    definition = {
        "nodes": [
            {
                "id": "gen",
                "type": "llm",
                "model_id": str(model.id),
                "prompt": "노트북에서 커피 추출에 대해 조사해줘",
                "tools": ["rag_search"],
            },
        ],
        "edges": [],
    }
    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=user_a.id,
    )
    summary = await execute_agent(ctx, "workflow", definition)

    # ---- 골든 1: complete_with_tools 호출(턴) 횟수와 노출 도구 ----
    assert len(trace["turns"]) == 2, "mock 루프는 정확히 2턴(도구호출→마무리)이어야 한다"
    assert trace["turns"][0]["tool_names"] == ["rag_search"]
    assert trace["turns"][1]["tool_names"] == ["rag_search"]

    # ---- 골든 2: 턴별 메시지 시퀀스(역할 순서) ----
    # 턴1: 비신뢰 데이터 규칙(system) + user 프롬프트. 도구를 쓰는 llm 노드는 도구 결과가
    # 데이터 블록으로 들어오므로 규칙을 시스템 메시지로 받는다(HWP-05, 계획 source-boundary D4).
    turn1_roles = [m["role"] for m in trace["turns"][0]["messages"]]
    assert turn1_roles == ["system", "user"]
    assert trace["turns"][0]["messages"][1]["has_tool_calls"] is False

    # 턴2: system → user → assistant(tool_calls 포함) → tool 결과 순으로 누적.
    turn2 = trace["turns"][1]["messages"][1:]
    turn2_roles = [m["role"] for m in trace["turns"][1]["messages"]]
    assert turn2_roles == ["system", "user", "assistant", "tool"]
    # assistant 메시지는 tool_calls를 담고 content는 없음(mock).
    assert turn2[1]["has_tool_calls"] is True
    assert turn2[1]["content_kind"] == "none"
    # tool 메시지는 tool_call_id로만 도구 결과를 귀속시킨다.
    # 코어 v0.9.0(#29)에서 role="tool" 메시지의 `name` 필드가 의도적으로 제거됐다 —
    # OpenAI `ChatCompletionToolMessageParam`의 허용 필드가 아니라서 엄격히 검증하는
    # 호환 게이트웨이가 요청 전체를 400으로 거절했기 때문이다
    # (biznotes_core/services/agents/react/loop.py `_apply_resolved_pending_call` 주석).
    # 기능 퇴행이 아니라 계약 변경이므로 골든을 새 계약으로 갱신한다.
    assert turn2[2]["role"] == "tool"
    assert turn2[2]["name"] is None
    # 귀속은 직전 assistant 메시지의 tool_calls[0].id와 **정확히 일치**해야 한다
    # (`name` 제거 후 유일한 결과 연결 수단이므로 truthy 검사로는 부족하다).
    assert len(turn2[1]["tool_call_ids"]) == 1
    assert turn2[2]["tool_call_id"] == turn2[1]["tool_call_ids"][0]
    assert turn2[2]["tool_call_id"]

    # ---- 골든 3: 디스패치 트레이스(도구 1회, rag_search, query 인자) ----
    assert len(trace["dispatches"]) == 1
    assert trace["dispatches"][0]["name"] == "rag_search"
    assert "query" in trace["dispatches"][0]["args"]
    # mock이 user 프롬프트 앞 120자를 query로 사용.
    assert trace["dispatches"][0]["args"]["query"].startswith("노트북에서 커피 추출")

    # ---- 골든 4: usage 토큰 누적(두 턴의 합) ----
    # mock_tool_turn은 매 턴 total_tokens > 0을 반환하고 _exec_llm_with_tools가
    # ctx.usage에 키별로 합산한다. 누적 후 0보다 커야 하고 키가 보존돼야 한다.
    assert ctx.usage["total_tokens"] > 0
    assert ctx.usage["prompt_tokens"] > 0
    assert ctx.usage["completion_tokens"] > 0
    # total = prompt + completion 불변식(각 턴이 만족 → 합도 만족).
    assert (
        ctx.usage["total_tokens"]
        == ctx.usage["prompt_tokens"] + ctx.usage["completion_tokens"]
    )

    # ---- 골든 5: 최종 텍스트가 도구 결과를 종합 ----
    assert "도구 결과를 종합" in summary
    # rag_search 결과 없음 → 디스패치가 안내 문자열을 반환하고 최종 응답에 반영.
    assert "관련 발췌를 찾지 못했습니다" in summary


async def test_llm_tools_node_output_shape_golden(
    db, user_a, _mock_runtime_deps, _tool_mock_model
):
    """`_exec_llm_with_tools` 반환 형태(노드 output) 골든 고정.

    노드는 {"text": <최종텍스트>, "tool_calls": [{"tool", "arguments"}, ...]}를
    state에 남긴다. Phase 1 리팩터 후에도 이 출력 형태가 동일해야 한다.
    """
    model = _tool_mock_model

    from biznotes_core.services.agents.nodes import llm as nodes_llm

    captured: dict[str, object] = {}
    real_exec = runtime._exec_llm_with_tools

    async def capture(ctx, node, messages, model_id, allowed):
        out = await real_exec(ctx, node, messages, model_id, allowed)
        captured["out"] = copy.deepcopy(out)
        captured["allowed"] = list(allowed)
        return out

    runtime_attr = runtime._exec_llm_with_tools
    nodes_llm_attr = nodes_llm._exec_llm_with_tools
    try:
        runtime._exec_llm_with_tools = capture  # type: ignore[assignment]
        # _exec_llm(nodes/llm.py, R2-5 이동)이 자기 모듈 네임스페이스에서
        # _exec_llm_with_tools를 직접 임포트해 참조하므로, runtime 재수출만 패치하면
        # 이 경로가 패치를 우회한다 — nodes.llm도 함께 패치해야 seam이 보존된다.
        nodes_llm._exec_llm_with_tools = capture  # type: ignore[assignment]
        ctx = RunContext(
            run_id=uuid.uuid4(),
            notebook_id=uuid.uuid4(),
            db=db,
            params={},
            user_id=user_a.id,
        )
        await execute_agent(
            ctx,
            "workflow",
            {
                "nodes": [
                    {
                        "id": "gen",
                        "type": "llm",
                        "model_id": str(model.id),
                        "prompt": "조사 요청",
                        "tools": ["rag_search"],
                    }
                ],
                "edges": [],
            },
        )
    finally:
        runtime._exec_llm_with_tools = runtime_attr  # type: ignore[assignment]
        nodes_llm._exec_llm_with_tools = nodes_llm_attr  # type: ignore[assignment]

    out = captured["out"]
    assert isinstance(out, dict)
    # 응답 모델·폴백 여부도 싣는다(#359) — 실행 로그에서 노드마다 모델을 본다. 최종 답의 종료
    # 사유도 싣는다(#422) — 잘리지 않았으니 `output_truncated`는 없다
    assert set(out.keys()) == {"text", "tool_calls", "model", "fallback_used", "finish_reason"}
    assert out["finish_reason"] == "stop"
    assert out["model"] == {"id": str(model.id), "display_name": model.display_name}
    assert out["fallback_used"] is False
    assert isinstance(out["text"], str) and out["text"]
    # tool_calls 로그는 {"tool", "arguments"} 형태의 항목 리스트.
    assert isinstance(out["tool_calls"], list) and len(out["tool_calls"]) == 1
    assert set(out["tool_calls"][0].keys()) == {"tool", "arguments"}
    assert out["tool_calls"][0]["tool"] == "rag_search"
    # allowed 도구 목록이 노드 tools를 그대로 반영.
    assert captured["allowed"] == ["rag_search"]


async def test_builtin_tool_specs_baseline(db):
    """`_BUILTIN_TOOL_SPECS`의 현재 구성을 골든으로 고정(Phase 1 레지스트리화 기준선).

    Phase 1에서 tool_registry가 이 스펙들을 동적 생성하도록 옮긴다.
    현재 하드코딩된 도구 집합·스키마 형태를 고정해 회귀를 감지한다.
    """
    specs = runtime._BUILTIN_TOOL_SPECS
    # 현재 내장 도구 집합(하드코딩).
    assert set(specs.keys()) == {"rag_search", "web_search", "http_request", "mcp_call"}
    for name, spec in specs.items():
        assert spec["type"] == "function"
        fn = spec["function"]
        assert fn["name"] == name
        assert "description" in fn
        assert fn["parameters"]["type"] == "object"
    # rag_search는 query를 required로 가진다(가장 기본 도구).
    assert specs["rag_search"]["function"]["parameters"]["required"] == ["query"]
    # mcp_call은 server_id·tool_name을 required로 가진다.
    assert specs["mcp_call"]["function"]["parameters"]["required"] == [
        "server_id",
        "tool_name",
    ]
