"""에이전트 실행 런타임 — 모든 kind를 노드 파이프라인으로 정규화해 LangGraph로 실행.

노드 타입:
  rag_search   {query, top_k}            → {context, citations}
  source_load  {max_sources, max_chars, fit?} → {context, sources, dropped}
               (full 모드 원본 전문 주입)
               `fit: "shrink_each"`(#447)면 앞에서부터 채우지 않고 모든 소스를 같은 비율로 자른다
               (출력에 `fit` 표식). 생략하면 앞 소스부터 채운다
  llm          {prompt, system?}         → {text}
  json_validate{source, schema, prompt?, system?} → {json, text, truncated}
               (JSON 파싱·스키마 검증, 실패 시 prompt로 1회 재시도, 최종 실패는 예외로 run 종결)
  citation_verify{source, notebook_id?} → {json, text, dropped}
               (flashcards/quiz 항목의 citation 검증 — 마커 매칭·소속 확인 실패 항목 제외,
               question 텍스트 정규화 dedupe. phase-21-design.md §2.3)
  save_artifact{name, content, content_type} → {key}
  http         {url, method, headers?, body?} → {text, status}
  mcp_tool     {server_id, tool_name, arguments} → {text}
  image_generate{prompt, name?, n?, size?|aspect_ratio?, model_id?}
               → {key, keys, names, images, count}
               (이미지 생성 → PNG·JPEG 아티팩트 저장. 비신뢰 블록이 든 프롬프트는 거부, #338)
  agent_call   {agent_id, params?}        → {text} (서브에이전트 호출, kind∈{workflow,skill})
  react_agent  {goal|prompt, allowed_tools?, max_iters?, token_budget?, ...} → {text, tool_calls}
               (자율 ReAct — LLM이 도구를 동적 선택. 예산/fan-out/병렬 가드 강제, Phase 2)
               예산 소진 시 출력에 `_react_budget_hit: True`와 `_react_budget {reason,
               used_tokens, token_budget, used_cost_usd, cost_budget_usd, has_content}`(#385).
               기본값·하드 상한은 `react.loop.REACT_DEFAULTS`·`REACT_HARD_CAPS`
               source_load 도구 1회 상한 `source_load_max_sources`(5, 상한 20)·
               `source_load_max_chars`(60,000, 상한 200,000)와 남은 토큰 예산 비율
               `source_load_budget_ratio`(0=끔, 기본) — 도구를 쓰는 llm 노드도 상한은 읽는다(#389)

공통 내장 도구(rag_search/save_artifact)는 모든 kind에서 노드로 사용 가능.
"""

# runtime.py는 R2 분해 완료 후 재수출 셸이다 — 자체 로직 없이 하위 모듈의 공개 심볼을
# 모아 기존 `from biznotes_core.services.agents.runtime import ...` 임포트 계약(R0-6)을 유지한다.
# json_validate 산출물 스키마 검증기(ctx 무의존 순수 함수)는 validators.py로 이동(R2-2).
# _exec_json_validate는 nodes/validation.py로 이동(R2-5)하며 이 검증기들을 직접
# 임포트한다. 아래는 runtime의 재수출 — 특성화 테스트(R0-6)의 공개 임포트 계약
# (from ...runtime import _validate_mindmap_shape 등)을 하위 호환 유지한다.
# 실행 로깅 부수효과(_truncate_output/_log_node_start/_log_node_finish)와 도구 인자
# 마스킹(_mask_value/_mask_tool_args/_SECRET_ARG_KEYS)은 logging.py로 이동(R2-3). 아래
# 재수출로 실행기·react 루프의 bare 참조가 runtime 네임스페이스에서 해석되고,
# test_react_agent의 runtime._log_node_* monkeypatch seam·tool_registry의 _mask_tool_args
# 임포트·특성화 테스트 임포트 계약(R0-6)이 하위 호환된다.
from biznotes_core.services.agents.context import (
    RunContext,  # noqa: F401  # 재수출 — 하위 호환(R1-2)
)
from biznotes_core.services.agents.errors import (  # noqa: F401  # 재수출 — 하위 호환(R2-1)
    AgentRuntimeError,
    ApprovalRequired,
    SubagentApprovalNotSupportedError,
)

# 그래프 오케스트레이션(kind_to_nodes/_topological_order/execute_agent/NODE_EXECUTORS)과
# 실행 상수(MAX_AGENT_DEPTH/_TOKEN_KEYS/_LOG_INPUT_KEYS)는 graph.py로 이동(R2-6).
# 재수출로 기존 임포트 계약(admin/agents.py·workers·skills/builder·특성화 테스트, R0-6)과
# NODE_EXECUTORS setitem monkeypatch seam(동일 dict 객체 공유)을 하위 호환 유지한다.
from biznotes_core.services.agents.graph import (  # noqa: E402, F401
    _LOG_INPUT_KEYS,
    _TOKEN_KEYS,
    MAX_AGENT_DEPTH,
    NODE_EXECUTORS,
    _topological_order,
    execute_agent,
    kind_to_nodes,
)
from biznotes_core.services.agents.logging import (  # noqa: E402, F401
    _SECRET_ARG_KEYS,
    _log_node_finish,
    _log_node_start,
    _mask_tool_args,
    _mask_value,
    _truncate_output,
)

# 조건/루프/RAG 검색/소스 로드 노드 실행기(evaluate_condition/parse_and_evaluate/
# _exec_condition/_exec_loop/_exec_rag_search/_exec_source_load)는
# nodes/retrieval.py로 이동(R2-5) — 상단에서 재수출.
# LLM 노드 실행기·내장 도구 디스패치(_exec_llm/_dispatch_builtin_tool/
# _dispatch_register_source 및 _BUILTIN_TOOL_SPECS/_REGISTER_SOURCE_RUN_LIMIT)는
# nodes/llm.py로 이동(R2-5) — 상단에서 재수출.
# json_validate/citation_verify 노드 실행기(_exec_json_validate/
# _normalize_question_text/_verify_chunk_membership/_exec_citation_verify)는
# nodes/validation.py로 이동(R2-5) — 상단에서 재수출.
# 서브에이전트 호출/오케스트레이션 노드 실행기(_exec_constant/_exec_save_artifact/
# _exec_http/_exec_mcp_tool/agent_visible_to/dispatch_agent/_run_subagent/
# _exec_agent_call/_exec_sub_workflow/_exec_error_handler/_exec_supervisor)는
# nodes/composite.py로 이동(R2-5) — 상단에서 재수출.
# react_agent 노드(_clamp_react_*/_exec_react_agent/_react_history_messages/
# run_react_node)는 react/loop.py로 이동(R2-4) — 상단에서 재수출한다.
# 서브에이전트 호출/오케스트레이션 노드 실행기(agent_visible_to/dispatch_agent/
# _run_subagent/_exec_agent_call/_exec_sub_workflow/_exec_error_handler/
# _exec_supervisor/_exec_save_artifact/_exec_http/_exec_mcp_tool/_exec_constant)는
# nodes/composite.py로 이동(R2-5, 기계적 추출). NODE_EXECUTORS 등록·
# tool_registry.py의 runtime.dispatch_agent/_run_subagent 호출 시점 조회(seam)·
# 특성화 테스트 임포트 계약(R0-6)을 하위 호환 유지하도록 재수출한다.
from biznotes_core.services.agents.nodes.composite import (  # noqa: E402, F401
    _exec_agent_call,
    _exec_constant,
    _exec_error_handler,
    _exec_http,
    _exec_mcp_tool,
    _exec_save_artifact,
    _exec_sub_workflow,
    _exec_supervisor,
    _run_subagent,
    agent_visible_to,
    dispatch_agent,
    resolve_subagent_skill_version,
    skill_agent_visible_to,
)

# compute_cost는 게이트웨이 경계(gateway/cost.py)로 이동했다(R1-6). 여기서는 하위
# 호환을 위해 재수출만 한다 — admin/usage.py 등 기존 임포트 경로가 그대로 동작한다.
# RunContext는 tool_registry.py와의 양방향 순환 import를 끊기 위해 agents/context.py로
# 분리됐다(R1-2). 여기서는 하위 호환을 위해 재수출만 한다 — 기존
# `from biznotes_core.services.agents.runtime import RunContext` 경로가 그대로 동작한다.
# LLM 노드 실행기·내장 도구 디스패치(_exec_llm/_dispatch_builtin_tool/
# _dispatch_register_source 및 관련 상수)는 nodes/llm.py로 이동(R2-5, 기계적 추출).
# NODE_EXECUTORS 등록·tool_registry.py의 runtime._dispatch_builtin_tool 호출 시점
# 조회(seam)·특성화 테스트 임포트 계약(R0-6)을 하위 호환 유지하도록 재수출한다.
from biznotes_core.services.agents.nodes.llm import (  # noqa: E402, F401
    _BUILTIN_TOOL_SPECS,
    _REGISTER_SOURCE_RUN_LIMIT,
    _dispatch_builtin_tool,
    _dispatch_register_source,
    _exec_llm,
)

# 조건/루프/RAG 검색/소스 로드 노드 실행기(evaluate_condition/parse_and_evaluate/
# _exec_condition/_exec_loop/_exec_rag_search/_exec_source_load)는 nodes/retrieval.py로
# 이동(R2-5, 기계적 추출). NODE_EXECUTORS 등록·특성화 테스트 임포트 계약(R0-6)을
# 하위 호환 유지하도록 재수출한다.
from biznotes_core.services.agents.nodes.retrieval import (  # noqa: E402, F401
    _exec_condition,
    _exec_loop,
    _exec_rag_search,
    _exec_source_load,
    evaluate_condition,
    parse_and_evaluate,
)

# json_validate/citation_verify 노드 실행기는 nodes/validation.py로 이동(R2-5,
# 기계적 추출). NODE_EXECUTORS 등록·특성화 테스트 임포트 계약(R0-6)을 하위 호환
# 유지하도록 재수출한다.
from biznotes_core.services.agents.nodes.validation import (  # noqa: E402, F401
    _exec_citation_verify,
    _exec_json_validate,
    _normalize_question_text,
    _verify_chunk_membership,
)

# ReAct 자율 에이전트 루프·예산·HITL 승인 게이트는 react/loop.py로 이동(R2-4, 기계적
# 추출·동작 변경 없음). NODE_EXECUTORS 등록(_exec_react_agent)·chat.py의 run_react_node
# 임포트·특성화/react 테스트 임포트 계약(R0-6)을 하위 호환 유지하도록 재수출한다.
# HITL 승인/재개 순수 로직은 react/approval.py로 통합(R2b). evaluate_gate·CheckpointKeys·
# restore_run_context 등을 재수출해 특성화·react 테스트의 임포트 계약을 하위 호환한다.
from biznotes_core.services.agents.react.approval import (  # noqa: E402, F401
    CheckpointKeys,
    build_checkpoint,
    evaluate_gate,
    is_consumable,
    restore_run_context,
)
from biznotes_core.services.agents.react.loop import (  # noqa: E402, F401
    _BUDGET_PARTIAL_NOTE,
    _CALL_AGENT_TOOL,
    _MAX_TOOL_ITERS,
    _NODE_INFRA_KEYS,
    _REACT_CONFIG_KEYS,
    _REACT_DEFAULTS,
    _REACT_GLOBAL_SUBAGENT_CAP,
    _REACT_GUARD_PROMPT,
    _REACT_HARD_CAPS,
    _REACT_TOOL_NODE_TYPE,
    _SUBAGENT_CAP_NOTE,
    _aux_log_tool_call,
    _clamp_react_float,
    _clamp_react_int,
    _exec_llm_with_tools,
    _exec_react_agent,
    _react_approval_gate,
    _react_budget_exceeded,
    _react_history_messages,
    _ReactGuards,
    _tool_loop_result,
    run_react_node,
)
from biznotes_core.services.agents.validators import (  # noqa: E402, F401  (개별 검증기·_DIALOGUE_SPEAKERS는 특성화 테스트 임포트 계약용 순수 재수출)
    _JSON_VALIDATE_DICT_SCHEMAS,
    _JSON_VALIDATE_SCHEMAS,
    _MINDMAP_NODE_CAP,
    _count_mindmap_nodes,
    _truncate_mindmap_bfs,
    _validate_citation_shape,
    _validate_flashcards_shape,
    _validate_mindmap_shape,
    _validate_quiz_shape,
)
from biznotes_core.services.gateway.cost import (
    compute_cost,  # noqa: F401  # 재수출 — 하위 호환(R1-6)
)

# (그래프 오케스트레이션·상수는 graph.py로 이동, R2-6 — 상단에서 재수출)
