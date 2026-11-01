"""도구 레지스트리 — LLM에 노출할 도구 스펙을 런타임에 동적 생성.

ReAct 에이전트 도입(REACT_AGENT_PLAN.md) Phase 1. 기존 `_BUILTIN_TOOL_SPECS`
하드코딩을 대체해, 호출 컨텍스트와 `allowed` 화이트리스트에 따라 노출 도구를
동적으로 구성한다. 자율 에이전트가 서브에이전트(`call_agent`)·MCP 도구를
발견·선택하려면 이 동적 노출이 필수다(계획 D2).

**기본 안전값(T4·Q2)**: `allowed=None`이면 `rag_search`·`call_agent`만 노출한다.
부작용/외부 호출 도구(http_request·web_search·MCP/stdio)는 **명시 opt-in**
(`allowed`에 이름이 있을 때)만 노출된다. 멀티테넌트 MCP 격리는 인간 승인 전까지
보류(Q6)이므로 MCP 디스커버리는 opt-in 경로에서만 동작한다.

디스패치는 런타임 모듈의 `_dispatch_builtin_tool`/`_run_subagent`로 위임한다
(가드·관측 seam 유지). 본 모듈은 **스펙 합성 + 디스패처 바인딩**만 담당한다.
"""

import asyncio
import copy
import hashlib
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Container
from enum import Enum
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import structlog

from biznotes_core.services.agents.mcp_tool_spec import (
    MCP_TOOL_TEXT_LIMIT,
    MCPToolSpecRejected,
    _spec_hash,  # 디스커버리와 `mcp_tool_spec_hash`가 공유하는 해시 정의(정화를 두 번 돌지 않는다)
    mcp_tool_function,
)

if TYPE_CHECKING:
    # RunContext는 context.py로 분리됐다(R1-2) — runtime.py를 거치지 않아 순환의 한 축 해소.
    from biznotes_core.services.agents.context import RunContext

logger = structlog.get_logger()

# 디스패처 시그니처 — (ctx, name, args) → 결과 문자열.
Dispatcher = Callable[["RunContext", str, dict[str, Any]], Awaitable[str]]

# 기본 노출 도구(allowed=None) — 부작용 없는 RAG + 가드된 서브에이전트 호출만(T4·Q2).
DEFAULT_TOOLS: tuple[str, ...] = ("rag_search", "call_agent")

# opt-in 가능한 내장 도구(명시 allowed에 있을 때만 노출).
# mcp_call: LLM이 server_id/tool_name을 직접 지정하는 기존 단일 MCP 도구(하위호환).
#   자율 디스커버리(Q6 보류)와 별개 — 워크플로우 작성자가 명시 opt-in해야 노출됨.
# register_source: Deep Research 프리셋 전용(phase-22-design.md §2.1) — 웹 소스를
#   노트북에 등록. HITL 자동승인 대상에서 항상 배제된다(_OPTIN_BUILTIN 소속과 무관하게
#   auto_approve_tools 저장 검증(admin/agents.py·workflows.py)이 별도로 강제).
# artifact_list·artifact_read: 같은 노트북의 다른 run 산출물을 읽는다(#133). 읽기 전용이지만
#   다른 세션의 내용을 맥락으로 끌어오므로 명시 opt-in(또는 채팅 팔레트)으로만 노출한다.
_OPTIN_BUILTIN: frozenset[str] = frozenset(
    {"web_search", "http_request", "mcp_call", "register_source", "artifact_list", "artifact_read"}
)

# auto_approve_tools 정책에 절대 포함될 수 없는 도구 이름(phase-22-design.md §2.3,
# D-14) — 워크플로우 정의 저장 시 admin/agents.py::_validate_definition과
# workflows.py::_validate_structure 양쪽이 이 집합을 검사해 거부한다. 런타임
# _react_approval_gate도 이 집합에 한해 auto_approve_match 결과와 무관하게 항상
# 게이트를 강제하는 이중 방어를 둔다(저장 검증 우회 경로 대비).
# `run_skill_script`는 표준 계획 D7(번들 스크립트 실행 L2) — 자동 승인할 수 없다.
_ALWAYS_GATED_TOOLS: frozenset[str] = frozenset({"register_source", "run_skill_script"})

# 도구별 부작용 여부(HITL 게이트 대상 판정, phase-19-design.md §3.1) — 정적 분류.
#
# [R2b/CR19-07] canonical bool은 **정적 spec 옆의 이 선언 하나**에 산다. 과거에는
# `_BUILTIN_SPECS`(스펙)와 `TOOL_SIDE_EFFECT`(분류)가 손으로 유지되는 병렬 dict라
# 신규 도구 추가 시 두 곳을 동기화해야 했다(CR19-07). 이제 `derive_side_effect_map()`이
# 노출 정적 도구 집합(_BUILTIN_SPECS ∪ _CALL_AGENT_SPEC)과 이 선언을 교차 검증해
# `TOOL_SIDE_EFFECT`를 **파생**한다 — spec에 도구를 추가하고 여기 bool을 빠뜨리면
# import 시점에 즉시 실패한다(누락 drift 차단). MCP 디스커버리 도구(mcp_<8hex>_<tool>)는
# 이름 패턴으로 식별 불가하므로 이 맵에 없으면(get 기본값) side_effect=True(안전 기본값).
_SIDE_EFFECT_DECL: dict[str, bool] = {
    "rag_search": False,
    "source_load": False,
    # #447 — run 범위 소스의 목록을 읽기만 한다
    "source_list": False,
    "run_skill": False,
    "call_agent": True,
    "web_search": False,
    "http_request": True,
    "mcp_call": True,
    "register_source": True,
    # 표준 스킬 런타임(D6) — 스킬 지침·파일을 읽기만 한다
    "activate_skill": False,
    "read_skill_file": False,
    # 번들 스크립트 실행(D7) — 격리 환경에서 코드를 실행하고 산출물을 만든다
    "run_skill_script": True,
    # #133 — 열람할 수 있는 run의 산출물을 읽기만 한다
    "artifact_list": False,
    "artifact_read": False,
}


def _static_tool_names() -> set[str]:
    """노출 정적 도구 이름 집합 — spec의 단일 출처(_BUILTIN_SPECS ∪ _CALL_AGENT_SPEC).

    이 함수가 spec을 도구 이름의 canonical 출처로 삼는다 — side_effect 선언은 이
    집합을 반드시 덮어야 한다(derive_side_effect_map이 강제).
    """
    names = {spec["function"]["name"] for spec in _BUILTIN_SPECS.values()}
    names.add(_CALL_AGENT_SPEC["function"]["name"])
    names.add(_RUN_SKILL_SPEC["function"]["name"])
    names.update(_SKILL_RUNTIME_TOOLS)
    names.add(_SKILL_SCRIPT_TOOL)
    return names


def derive_side_effect_map() -> dict[str, bool]:
    """정적 spec 집합에서 side_effect 분류 맵을 파생(CR19-07·CR-R2B4-01).

    노출 정적 도구가 `_SIDE_EFFECT_DECL`에 **모두 선언돼 있어야** 하며(누락 시 즉시
    실패), 선언에만 있고 spec에 없는 유령 항목도 거부한다 — 이로써 "spec에 도구를
    추가했는데 분류를 빠뜨리는" drift가 구조적으로 불가능해진다.
    """
    static = _static_tool_names()
    missing = static - _SIDE_EFFECT_DECL.keys()
    if missing:
        raise RuntimeError(f"side_effect 분류 누락(spec에 있으나 선언 없음): {sorted(missing)}")
    extra = _SIDE_EFFECT_DECL.keys() - static
    if extra:
        raise RuntimeError(f"side_effect 유령 선언(선언에 있으나 spec 없음): {sorted(extra)}")
    return {name: _SIDE_EFFECT_DECL[name] for name in static}


def is_always_gated_tool(tool_name: str) -> bool:
    """auto_approve_tools 정책 매칭과 무관하게 항상 HITL 게이트를 강제해야 하는 도구인지."""
    return tool_name in _ALWAYS_GATED_TOOLS


def find_always_gated_auto_approve_violations(nodes: list[dict[str, Any]]) -> list[str]:
    """definition의 react_agent 노드들 중 auto_approve_tools에 상시 게이트 도구
    (register_source 등)를 포함한 것이 있으면 그 도구 이름을 정렬된 목록으로 반환한다
    (phase-22-design.md §2.3, D-14). 위반이 없으면 빈 목록.

    admin/agents.py::_validate_definition과 workflows.py::_validate_structure
    양쪽의 저장 시 검증이 이 함수를 공유해 로직 중복을 피한다(R22B-05). 이 모듈은
    FastAPI에 의존하지 않으므로 HTTPException은 호출자가 직접 발생시킨다.
    """
    blocked: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") != "react_agent":
            continue
        policy = node.get("auto_approve_tools")
        if not isinstance(policy, dict):
            continue
        blocked.update(name for name in policy if is_always_gated_tool(name))
    return sorted(blocked)


def _llm_node_side_effect_tools(names: list[Any]) -> list[str]:
    """llm 노드 도구 루프(승인 게이트 없음)에 둘 수 없는 도구 — **등록된** 부작용 도구(#70).

    `is_side_effect_tool`과 달리 프로세스 전역 디스커버리 상태를 보지 않는 순수 규칙이다 —
    저장 검증(API 프로세스)과 실행 시점(워커)이 같은 답을 낸다. 미등록 이름은
    `build_tool_specs`가 노출하지 않으므로(경고 후 무시) 대상이 아니다. 원소는 `str()`로 비교한다.
    """
    return sorted({str(n) for n in names if TOOL_SIDE_EFFECT.get(str(n)) is True})


def find_llm_node_side_effect_tools(nodes: list[dict[str, Any]]) -> list[str]:
    """definition의 llm 노드 `tools`에 든 부작용 도구를 정렬된 목록으로 반환한다(#70).

    llm 노드의 도구 루프에는 HITL 승인 게이트가 없다 — 부작용 도구는 게이트가 있는
    react_agent에서만 쓸 수 있다. 제품의 저장 검증(workflows.py::_validate_structure,
    admin/agents.py::_validate_definition)이 이 함수로 거부하고, 실행 시점에는
    `_exec_llm_with_tools`가 같은 규칙으로 노드를 실패시킨다. 위반이 없으면 빈 목록.
    이 모듈은 FastAPI에 의존하지 않으므로 HTTPException은 호출자가 직접 발생시킨다.
    """
    blocked: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") != "llm":
            continue
        tools = node.get("tools")
        if not isinstance(tools, list):
            continue
        blocked.update(_llm_node_side_effect_tools(tools))
    return sorted(blocked)


def is_side_effect_tool(tool_name: str) -> bool:
    """도구가 HITL 게이트 대상(부작용)인지 판정. 미등록 도구는 보수적으로 True."""
    # `mcp` 자체는 discovery 제어 토큰이며 실제 서버 도구가 아니다. 동적 MCP
    # capability는 discovery가 실제 allowlist를 통과시킨 이름만 안전하다고 등록한다;
    # 임의의 mcp_<tag>_<tool>은 여전히 보수적으로 side_effect=True다.
    if tool_name == "mcp":
        return False
    if tool_name in TOOL_SIDE_EFFECT:
        return TOOL_SIDE_EFFECT[tool_name]
    # 안전 집합은 디스커버리 이름(`mcp_<tag>_<tool>`)에만 쓴다 — 집합에 내장 도구 이름이 들어가도
    # (`register_source`·`mcp_call` 등) 게이트가 꺼지지 않는다(#152)
    return not (
        tool_name.startswith(MCP_TOOL_PREFIX) and tool_name in _safe_discovered_mcp_tools
    )


# [R5-4] 채팅 자율 모드가 노출하는 **명시적 chat-safe allowlist**.
#
# ⚠️ 이 집합을 `is_side_effect_tool == False`로 파생하지 말 것(HCI-R5-4, blocking):
# `web_search`는 side_effect=False지만 노출 시 채팅이 노트북 한정 RAG → **외부 웹
# 검색까지 확장**된다(외부 네트워크·비-노트북 데이터 노출). `call_agent`(side_effect
# =True)는 enqueue_agent_run을 우회해 일일 agent_runs 한도·감사 추적을 우회한다
# (codex D7, 인간 승인 2026-06-26). 두 경계를 모두 지키려면 side-effect 필터가 아닌
# **명시 열거**가 유일하게 안전하다. 도구 추가는 인간 명시 승인 필요.
#
# `activate_skill`·`read_skill_file`은 표준 계획 S-HCI-10(인간 승인 2026-09-22)으로 더했다.
# 둘 다 부작용이 없고, 카탈로그는 본인 스킬과 관리자가 공개한 스킬만 담는다.
# `artifact_list`·`artifact_read`는 #133(인간 승인 2026-10-07)으로 더했다. 둘 다 부작용이 없고,
# 경계는 run 열람 규칙(타인 채팅 run 제외, 관리자 예외)이다. 단 채팅이 아닌 실행에서는
# 채팅 run을 아예 빼고, 노트북 권한을 호출마다 다시 확인한다. 결과는 비신뢰 블록이다.
# `source_list`는 #447(owner 이슈 요청)로 더했다. 부작용이 없고, 범위는 `source_load`와 같은 run
# 범위(선택 소스 ∩ 노트북, ready)다. 제목·요약은 비신뢰 블록이다.
CHAT_SAFE_TOOLS: tuple[str, ...] = (
    "rag_search",
    "source_load",
    "source_list",
    "mcp",
    "run_skill",
    "activate_skill",
    "read_skill_file",
    "artifact_list",
    "artifact_read",
)

def _is_admin_audience(user_role: str, user_id: uuid.UUID | None) -> bool:
    """관리자·사용자 없는 시스템 실행이면 참 — MCP 노출 기준 `admin`(#139 A)."""
    return user_role == "admin" or user_id is None


def assert_no_side_effect_tools(tool_names: tuple[str, ...]) -> None:
    """allowlist에 부작용 도구가 혼입되지 않았는지 검사하는 보조 가드(R5-4).

    allowlist의 **출처가 아니라** 방어선이다 — 실수로 부작용 도구(register_source
    등)가 chat-safe 목록에 들어가면 즉시 실패시킨다. 단 `web_search`처럼
    side_effect=False지만 채팅에 부적합한 도구는 이 가드가 잡지 못하므로(그것은
    CHAT_SAFE_TOOLS 명시 열거의 책임), 이 함수는 이중 방어의 하위 계층이다.
    """
    offenders = [t for t in tool_names if is_side_effect_tool(t)]
    if offenders:
        raise ValueError(f"chat-safe allowlist에 부작용 도구 혼입: {offenders}")


_SENSITIVE_QUERY_KEY_RE = re.compile(r"key|token|secret|password|auth", re.IGNORECASE)
_APPROVAL_SUMMARY_VALUE_MAX_LEN = 500


def sanitize_for_approval_card(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """승인 카드/감사 로그 노출용 마스킹 — `_mask_tool_args`보다 공격적(CDX-D7).

    `_mask_tool_args`(logging.py)는 인자 **키** 기준으로만 마스킹해 URL 쿼리
    파라미터(`?api_key=...`)·userinfo(`user:pass@host`)·body 내부 값은 그대로
    남는다. 승인 카드/`agent_run_approvals.arguments_summary`는 이 함수의
    출력만 사용한다(phase-19-design.md §2.6).
    """
    # [R2b/R2B-02] `_mask_tool_args`는 R2-3에서 logging.py로 이동했다. runtime을 거쳐
    # 임포트하면 approval.build_checkpoint→sanitize_for_approval_card→runtime→
    # RunContext→SQLAlchemy 전이 임포트가 생겨 approval의 "DB 무의존" 경계가 호출 시점에
    # 깨진다. logging에서 직접 임포트해 이 사슬을 끊는다(순수성 유지).
    from biznotes_core.services.agents.logging import _mask_tool_args

    out: dict[str, Any] = dict(arguments) if isinstance(arguments, dict) else {}

    if tool_name == "http_request" and "url" in out:
        parsed = urlparse(str(out["url"]))
        safe_netloc = parsed.hostname or ""
        if parsed.port:
            safe_netloc = f"{safe_netloc}:{parsed.port}"
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        redacted_query = urlencode(
            [
                (k, "***" if _SENSITIVE_QUERY_KEY_RE.search(k) else v)
                for k, v in query_pairs
            ]
        )
        out["url"] = urlunparse(parsed._replace(netloc=safe_netloc, query=redacted_query))

    # body/headers를 포함한 모든 dict 값은 _mask_tool_args가 재귀적으로 순회하며
    # 키 기반 마스킹(_SECRET_ARG_KEYS: authorization|token|key|password|secret|
    # credential)을 적용한다 — 별도 사전 마스킹은 이 결과로 즉시 덮어써지는
    # 죽은 코드였다(재사용/단순화 리뷰 지적).
    out = _mask_tool_args(out)
    # 스칼라 값만 길이 상한 적용 — body/headers처럼 이미 처리된 dict는 구조를 보존한다.
    return {
        k: (v if isinstance(v, dict | list) else str(v)[:_APPROVAL_SUMMARY_VALUE_MAX_LEN])
        for k, v in out.items()
    }

# MCP 디스커버리 상한 — opt-in 경로에서만 사용.
MCP_DISCOVERY_TIMEOUT_SECONDS = 8.0  # 서버 1대 list_tools 타임아웃(초)
MCP_TOOL_PREFIX: Final = "mcp_"  # 디스커버리된 MCP 도구 이름 접두사(내장 도구와 충돌 방지)
# 현재 프로세스에서 allowlist를 통과해 실제로 노출된 동적 MCP 함수명만 기록한다. 디스커버리만
# 추가한다 — 공개 등록 함수는 두지 않는다(#152).
# 카탈로그는 `discovered_mcp_tool_side_effect`를 쓴다.
_safe_discovered_mcp_tools: set[str] = set()
# OpenAI 호환 tool-calling 함수명 길이 상한(초과 시 모델 실행 전 실패).
MAX_TOOL_NAME_LEN: Final = 64

# `list_tools` 결과의 프로세스 단위 캐시(#147). 일반 사용자 채팅은 매 턴 디스커버리를 돈다
# (`CHAT_SAFE_TOOLS`에 `mcp`) — 공개 서버마다 연결·stdio 기동을 반복하지 않게 결과만 재사용한다.
# 승인·공개 범위 판정은 캐시하지 않는다(run마다 DB). 워커 프로세스마다 따로 생기므로 서버가
# 도구 목록을 바꾸면 최대 TTL만큼 늦게 반영된다. 서버 행이 바뀌면(설정 수정·헬스체크 기록으로
# `updated_at` 갱신) 지문이 달라져 즉시 무효가 된다. 승인 변경은 서버 행을 고치지 않으므로, 서버에
# 새로 생긴 도구는 헬스체크(행 갱신)나 TTL 만료 뒤에 노출된다. 만료 직후 동시에 도는 run들의
# 조회는 합치지 않는다(이벤트 루프 사이에 Future를 공유할 수 없다) — 각자 한 번씩 조회한다.
_MCP_TOOL_LIST_TTL_SECONDS = 60.0
# 실패(타임아웃·오류)는 짧게 음성 캐시한다 — 응답 없는 서버가 매 턴 타임아웃을 더하지 않게 한다.
# 그동안 그 서버는 `failed_approved_servers`에 실패로 집계된다.
_MCP_TOOL_LIST_FAILURE_TTL_SECONDS = 30.0
# server_id → (지문, 만료 시각(monotonic), 도구 목록 | None=실패)
_mcp_tool_list_cache: dict[Any, tuple[tuple[Any, ...], float, list[dict[str, Any]] | None]] = {}
_mcp_tool_list_lock = threading.Lock()


class McpToolListMiss(Enum):
    """`get_cached_mcp_tool_list`의 "캐시 없음" 표식. `None`(디스커버리 실패 음성 캐시)과 다르다."""

    MISS = "miss"


MCP_TOOL_LIST_MISS: Final = McpToolListMiss.MISS

def _source_load_spec(max_sources: int, max_chars: int) -> dict[str, Any]:
    """`source_load` 도구 스펙 — 공유 스펙(기본 상한)과 노드 상한 스펙(#389)이 같은 모양을 쓴다."""
    return {
        "type": "function",
        "function": {
            "name": "source_load",
            "description": (
                "현재 노트북에서 허용된 소스의 파싱 원문을 읽는다. 채팅에서 선택한 소스가 "
                f"있으면 그 안에서만 읽으며 최대 {max_sources}개/{max_chars}자다. source_ids로 "
                "읽을 소스를 고르고(source_list로 확인), 생략하면 앞에서부터 읽는다 — 결과의 "
                "next_offset을 offset으로 넘겨 이어 읽는다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "max_sources": {"type": "integer", "maximum": max_sources},
                    "max_chars": {"type": "integer", "maximum": max_chars},
                    # #447 — `rag_search` 노드 `source_ids`(#362)와 같은 규칙으로 좁힌다
                    "source_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "읽을 소스 ID 목록(선택, source_list로 확인)",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "건너뛸 소스 수(기본 0) — 이전 결과의 next_offset",
                    },
                },
            },
        },
    }


# 내장 도구 스펙(레지스트리 단일 출처 — 런타임 _BUILTIN_TOOL_SPECS의 후신).
_BUILTIN_SPECS: dict[str, dict[str, Any]] = {
    "rag_search": {
        "type": "function",
        "function": {
            "name": "rag_search",
            "description": "노트북 소스를 의미 검색해 관련 발췌(컨텍스트)를 가져온다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "검색어"},
                    "top_k": {"type": "integer", "description": "가져올 발췌 수(기본: RAG 설정값)"},
                    # #447 — 노드 `source_ids`(#362)와 같은 규칙. 허용된 소스 안에서만 좁힌다
                    "source_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "검색할 소스 ID 목록(선택, source_list로 확인). "
                            "생략하면 허용된 소스 전체"
                        ),
                    },
                    "per_source_limit": {
                        "type": "integer",
                        "description": (
                            "소스당 최대 발췌 수(1~5). per_source면 소스마다 가져올 수(기본 1)"
                        ),
                    },
                    "coverage": {
                        "type": "string",
                        "enum": ["ranked", "per_source"],
                        "description": (
                            "ranked: 관련도 상위(기본). per_source: 범위의 소스마다 발췌를 보장"
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    "web_search": {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "외부 웹을 검색해 결과 목록(제목/URL/요약)을 가져온다.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "검색어"}},
                "required": ["query"],
            },
        },
    },
    "http_request": {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": "외부 HTTP API를 호출해 응답 본문을 가져온다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {"type": "string", "description": "GET/POST 등(기본 GET)"},
                    "body": {"type": "object", "description": "JSON 본문(선택)"},
                },
                "required": ["url"],
            },
        },
    },
    # 하위호환: 기존 워크플로우의 `tools: ["mcp_call"]`를 그대로 지원(C1).
    # LLM이 server_id/tool_name을 직접 지정 — 자율 디스커버리(mcp opt-in)와 별개.
    "mcp_call": {
        "type": "function",
        "function": {
            "name": "mcp_call",
            "description": "등록된 MCP 서버의 도구를 호출한다(server_id/tool_name 지정).",
            "parameters": {
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "tool_name": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "required": ["server_id", "tool_name"],
            },
        },
    },
    # Deep Research 프리셋 전용(phase-22-design.md §2.1) — notebook_id는 인자로
    # 받지 않는다(F11, IDOR 방지). 실행은 항상 ctx.notebook_id(run 컨텍스트)를 쓴다.
    "register_source": {
        "type": "function",
        "function": {
            "name": "register_source",
            "description": (
                "리서치 중 발견한 웹 URL을 현재 노트북에 소스로 등록한다. "
                "등록 후 별도로 rag_search로 내용을 확인할 수 있다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "등록할 웹 페이지 URL"},
                    "title": {"type": "string", "description": "제목 힌트(선택)"},
                },
                "required": ["url"],
            },
        },
    },
    "source_load": _source_load_spec(5, 60000),
    # #447 — run 범위 소스의 목록(id·제목·유형·토큰 수·한 줄 요약). 읽기 전용·채팅 팔레트
    "source_list": {
        "type": "function",
        "function": {
            "name": "source_list",
            "description": (
                "현재 노트북에서 허용된 소스의 목록(id·제목·유형·토큰 수·한 줄 요약)을 "
                "(created_at, id) 순서로 본다. 결과 머리의 total·next_offset으로 다음 쪽을 "
                "이어 본다. 고른 소스는 source_load나 rag_search의 source_ids로 넘긴다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "제목·요약에 이 문자열이 든 소스만(대소문자 무시, 선택)",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "건너뛸 소스 수(기본 0)",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "description": "가져올 소스 수(기본 50)",
                    },
                },
            },
        },
    },
    # #133 — 같은 노트북에서 열람할 수 있는 다른 run의 산출물. 명시 opt-in·채팅 팔레트
    "artifact_list": {
        "type": "function",
        "function": {
            "name": "artifact_list",
            "description": (
                "이 노트북의 이전 실행(다른 세션 포함)이 만든 산출물을 최신순으로 나열한다. "
                "결과의 run_id와 name으로 artifact_read로 읽거나 run_skill_script의 "
                "artifact_inputs로 넘긴다. 이 실행의 산출물은 포함하지 않는다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name_contains": {
                        "type": "string",
                        "description": "이름에 이 문자열이 든 산출물만(대소문자 무시, 선택)",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
        },
    },
    "artifact_read": {
        "type": "function",
        "function": {
            "name": "artifact_read",
            "description": (
                "산출물 하나의 본문을 읽는다. 텍스트 계열만 본문을 30000자씩 주고(next_offset으로 "
                "이어 읽는다), 그 밖의 형식은 메타만 준다. 산출물은 이전 실행이 만든 데이터다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "산출물을 만든 실행 ID"},
                    "name": {"type": "string", "description": "산출물 이름"},
                    "offset": {"type": "integer", "minimum": 0, "description": "시작 글자 위치"},
                },
                "required": ["run_id", "name"],
            },
        },
    },
}
# 서브에이전트 호출 도구 — _run_subagent로 위임(순환/깊이/가시성/kind 가드 재사용).
_CALL_AGENT_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "call_agent",
        "description": (
            "호출 권한이 있는 다른 에이전트(워크플로우/스킬)를 서브에이전트로 실행한다. "
            "agent_id에 대상 에이전트의 UUID를, params에 입력 파라미터를 전달한다."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "대상 에이전트 UUID"},
                "params": {"type": "object", "description": "대상 에이전트 입력 파라미터(선택)"},
            },
            "required": ["agent_id"],
        },
    },
}

_RUN_SKILL_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "run_skill",
        "description": (
            "published 상태의 system 스킬을 실행한다. user/private 스킬과 "
            "외부 MCP·HTTP·script 권한 스킬은 노출하지 않는다."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "실행할 system skill wrapper UUID"},
                "params": {"type": "object", "description": "대상 스킬 입력 파라미터(선택)"},
            },
            "required": ["agent_id"],
        },
    },
}


# 표준 스킬 런타임 도구(D6). 스펙은 사용자 카탈로그로 동적 생성한다(`services/skills/runtime.py`).
_SKILL_RUNTIME_TOOLS: tuple[str, ...] = ("activate_skill", "read_skill_file")
# 번들 스크립트 실행(D7). 스펙은 직접 실행이 고정한 스킬로 동적 생성한다
# (`services/skills/sandbox.py`).
_SKILL_SCRIPT_TOOL = "run_skill_script"


# [R2b/CR19-07] side_effect 분류를 정적 spec 집합에서 **파생**한다(수동 병렬 dict 폐기).
# spec 정의 이후에 계산해야 하므로 여기서 바인딩한다. spec에 도구를 추가하고
# _SIDE_EFFECT_DECL에 bool을 빠뜨리면 이 import 시점에 즉시 RuntimeError로 실패한다.
TOOL_SIDE_EFFECT: dict[str, bool] = derive_side_effect_map()


def static_tool_specs() -> list[dict[str, Any]]:
    """카탈로그용 정적 스펙을 선언 순서대로 반환한다.

    스킬 런타임 두 도구는 대표 스펙(빈 카탈로그)으로 싣는다 — 실제 enum은 실행 사용자마다 다르다.
    """
    from biznotes_core.services.skills.runtime import READ_SKILL_FILE_SPEC

    activate = {
        "type": "function",
        "function": {
            "name": "activate_skill",
            "description": "사용 가능한 표준 스킬을 활성화해 지침(SKILL.md 본문)을 받는다.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "스킬 name"}},
                "required": ["name"],
            },
        },
    }
    script = {
        "type": "function",
        "function": {
            "name": _SKILL_SCRIPT_TOOL,
            "description": "스크립트 실행이 승인된 스킬의 번들 스크립트를 격리 환경에서 실행한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "스킬 name"},
                    "script": {"type": "string", "description": "스크립트 경로"},
                    "args": {"type": "array", "items": {"type": "string"}},
                    "inputs": {"type": "array", "items": {"type": "string"}},
                    "artifact_inputs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "run_id": {"type": "string"},
                                "name": {"type": "string"},
                                "path": {"type": "string"},
                            },
                            "required": ["run_id", "name"],
                        },
                    },
                },
                "required": ["name", "script"],
            },
        },
    }
    specs = [
        *_BUILTIN_SPECS.values(),
        _CALL_AGENT_SPEC,
        _RUN_SKILL_SPEC,
        activate,
        READ_SKILL_FILE_SPEC,
        script,
    ]
    return copy.deepcopy(specs)


def _builtin_dispatcher() -> Dispatcher:
    """내장 도구 디스패치 — 런타임 모듈의 `_dispatch_builtin_tool`로 위임.

    런타임 함수를 **호출 시점에 조회**하여 monkeypatch seam(테스트 스파이)을 보존한다.
    """
    async def _dispatch(ctx: "RunContext", name: str, args: dict[str, Any]) -> str:
        from biznotes_core.services.agents import runtime

        return await runtime._dispatch_builtin_tool(ctx, name, args)

    return _dispatch


def _call_agent_dispatcher() -> Dispatcher:
    """call_agent 디스패치 — `_run_subagent`로 위임(가드 일원화, 계획 D3)."""
    async def _dispatch(ctx: "RunContext", name: str, args: dict[str, Any]) -> str:
        from biznotes_core.services.agents import runtime

        params = args.get("params")
        return await runtime._run_subagent(
            ctx, args.get("agent_id"), params if isinstance(params, dict) else {}
        )

    return _dispatch


def _run_skill_dispatcher() -> Dispatcher:
    """공개된 system skill만 _run_subagent 가드 경유로 실행한다.

    실행·판정·evidence는 모두 **노출 version**(`Skill.published_version_id`) 기준이다.
    소유자 호출도 노출 version을 쓴다 — `run_skill`은 공유 카탈로그이기 때문이다(R5-04).
    """

    async def _dispatch(ctx: "RunContext", name: str, args: dict[str, Any]) -> str:
        import uuid

        from biznotes_core.models import Agent
        from biznotes_core.services.agents import runtime

        try:
            target_id = uuid.UUID(str(args.get("agent_id")))
        except (TypeError, ValueError) as e:
            raise RuntimeError("system skill agent_id가 올바르지 않습니다") from e
        query = published_system_skill_query().where(Agent.id == target_id)
        row = (await ctx.db.execute(query)).first()
        if row is None:
            # 스킬 신원 자체를 특정할 수 없어 name/version 없는 빈 evidence보다 생략이 낫다.
            raise RuntimeError("published system skill이 아니거나 실행할 수 없습니다")
        target, skill, version = row
        label, _ = exposed_skill_label(version, target)
        evidence = {
            "kind": "skill",
            "name": label,
            "version": version.version,
            "skill_version_id": str(version.id),
            "package_hash": version.package_hash,
            "agent_id": str(target.id),
        }
        if not skill_manifest_is_read_only(version.manifest):
            ctx.tool_evidence.append({**evidence, "status": "failed"})
            raise RuntimeError("published system skill이 아니거나 실행할 수 없습니다")
        params = args.get("params")
        try:
            result = await runtime._run_subagent(
                ctx,
                str(target.id),
                params if isinstance(params, dict) else {},
                version_id=version.id,
            )
        except Exception:
            ctx.tool_evidence.append({**evidence, "status": "failed"})
            raise
        ctx.tool_evidence.append({**evidence, "status": "succeeded"})
        return result

    return _dispatch


def skill_manifest_is_read_only(manifest: Any) -> bool:
    """`run_skill` 카탈로그·런타임의 판정 — 외부 MCP/HTTP/script 권한을 선언한 스킬은 거부한다."""
    if not isinstance(manifest, dict):
        return False
    permissions = manifest.get("permissions", {})
    if not isinstance(permissions, dict):
        return False
    return not any(bool(permissions.get(name, False)) for name in ("mcp", "http", "scripts"))


def published_system_skill_query() -> Any:
    """`run_skill` 카탈로그·런타임·evidence가 공유하는 노출 조건(표준 계획 D8).

    행은 `(wrapper Agent, Skill, 노출 SkillVersion)`이다. 정본은 `Skill.published_version_id`
    이고, wrapper는 실행 핸들로만 쓴다 — wrapper의 scope·enabled·version을 보지 않는다.
    SkillVersion 별칭이 두 개다: wrapper 조회용과 노출 version 매니페스트용.
    `standard_agent_v1` version은 표준 런타임 전용이라 제외한다(D6).
    """
    from sqlalchemy import select
    from sqlalchemy.orm import aliased

    from biznotes_core.models import Agent, Skill, SkillVersion
    from biznotes_core.models.skill import SKILL_RUNTIME_LEGACY

    wrapper_version = aliased(SkillVersion, name="wrapper_version")
    exposed = aliased(SkillVersion, name="exposed_version")
    return (
        select(Agent, Skill, exposed)
        .join(wrapper_version, Agent.skill_version_id == wrapper_version.id)
        .join(Skill, wrapper_version.skill_id == Skill.id)
        .join(exposed, Skill.published_version_id == exposed.id)
        .where(
            Agent.source_kind == "skill_package",
            Agent.kind == "skill",
            Skill.visibility == "system",
            Skill.enabled.is_(True),
            exposed.skill_id == Skill.id,
            exposed.runtime_profile == SKILL_RUNTIME_LEGACY,
        )
    )


def exposed_skill_label(version: Any, wrapper: Any) -> tuple[str, str]:
    """LLM에 보일 이름·설명 — 노출 version의 frontmatter에서 읽는다(R3-02).

    Phase 1 이전에 동결된 version은 frontmatter를 저장하지 않았으므로 wrapper 값으로
    대신한다(릴리스 노트의 한계).
    """
    frontmatter = getattr(version, "standard_metadata", None) or {}
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    return (
        name.strip() if isinstance(name, str) and name.strip() else str(wrapper.name),
        (
            description.strip()
            if isinstance(description, str) and description.strip()
            else str(wrapper.description or "")
        ),
    )


def published_system_skill_predicates() -> tuple[Any, ...]:
    """카탈로그와 런타임이 공유하던 published system skill 노출 조건.

    **deprecated**(표준 계획 D8, R4-04): `skills.status`와 wrapper version을 보므로 공개
    포인터와 어긋난다. `published_system_skill_query()`로 옮기고, `0.13.0` 이상에서 두
    제품의 이관을 확인한 뒤 제거한다. 동작은 바꾸지 않는다.
    """
    import warnings

    from biznotes_core.models import Agent, Skill, SkillVersion

    warnings.warn(
        "published_system_skill_predicates는 deprecated입니다. "
        "published_system_skill_query를 쓰세요(0.13.0 이상에서 제거 예정).",
        DeprecationWarning,
        stacklevel=2,
    )
    return (
        Agent.kind == "skill",
        Agent.scope == "system",
        Agent.enabled.is_(True),
        Skill.visibility == "system",
        Skill.status == "published",
        Skill.enabled.is_(True),
        SkillVersion.status == "published",
    )


def _tool_list_fingerprint(server: Any) -> tuple[Any, ...] | None:
    """캐시 키의 서버 지문. `updated_at`이 없으면(저장 전 행) 캐시하지 않는다(None).

    `updated_at`에 더해 연결 설정을 직접 넣는다 — ORM을 거치지 않은 수정으로 `updated_at`이
    그대로여도 다른 서버 설정의 결과를 재사용하지 않는다. env는 암호문 해시만 둔다.
    """
    updated_at = getattr(server, "updated_at", None)
    if updated_at is None:
        return None
    env = server.env_encrypted
    env_digest = hashlib.sha256(env.encode()).hexdigest() if env else None
    return (updated_at, server.transport, server.endpoint_or_command, env_digest)


def _cache_get(server_id: Any, fingerprint: tuple[Any, ...] | None) -> Any:
    """유효한 캐시 항목의 사본(실패면 None), 없으면 `MCP_TOOL_LIST_MISS`."""
    if fingerprint is None:
        return MCP_TOOL_LIST_MISS
    with _mcp_tool_list_lock:
        entry = _mcp_tool_list_cache.get(server_id)
    if entry is None or entry[0] != fingerprint or entry[1] <= time.monotonic():
        return MCP_TOOL_LIST_MISS
    # 스펙의 parameters로 그대로 들어가므로 run 사이에 변경이 새지 않게 사본을 준다
    return copy.deepcopy(entry[2])


def _cache_put(
    server_id: Any, fingerprint: tuple[Any, ...] | None, tools: list[dict[str, Any]] | None
) -> None:
    if fingerprint is None:
        return
    ttl = _MCP_TOOL_LIST_TTL_SECONDS if tools is not None else _MCP_TOOL_LIST_FAILURE_TTL_SECONDS
    now = time.monotonic()
    with _mcp_tool_list_lock:
        # 지워진 서버의 항목이 쌓이지 않게 만료 항목을 함께 정리한다
        for key in [k for k, v in _mcp_tool_list_cache.items() if v[1] <= now]:
            del _mcp_tool_list_cache[key]
        _mcp_tool_list_cache[server_id] = (fingerprint, now + ttl, copy.deepcopy(tools))


McpToolListCacheKey = tuple[Any, tuple[Any, ...] | None]


def mcp_tool_list_cache_key(server: Any) -> McpToolListCacheKey:
    """`list_tools` 캐시가 이 서버 설정을 구분하는 키 — 항상 hashable이고 None이 아니다.

    서버 id를 포함하므로 설정이 같은 두 서버도 키가 다르다. 그 밖의 구성은 계약이 아니다. 소비
    제품이 동시 조회를 합치는(single-flight) 키로 쓰고, 조회 시작 때 한 번 계산해 get/store에
    넘긴다 — `await list_tools` 뒤에 서버 속성을 다시 읽지 않는다(#152). 저장 전 행(`updated_at`
    없음)도 키는 있지만 캐시되지 않는다.
    """
    return (server.id, _tool_list_fingerprint(server))


def _resolve_cache_key(server: Any, key: McpToolListCacheKey | None) -> McpToolListCacheKey:
    if key is None:
        return mcp_tool_list_cache_key(server)
    if key[0] != server.id:  # 다른 서버의 캐시 항목을 읽거나 덮어쓰지 않게 한다
        raise ValueError("mcp_tool_list_cache_key는 같은 서버로 계산한 값이어야 합니다")
    return key


def get_cached_mcp_tool_list(
    server: Any, *, key: McpToolListCacheKey | None = None
) -> list[dict[str, Any]] | None | McpToolListMiss:
    """런타임 디스커버리와 같은 프로세스 캐시를 읽는다(#147·#152).

    도구 목록의 사본, 최근 실패면 None, 없거나 만료·설정 변경이면 `MCP_TOOL_LIST_MISS`.
    `key`는 같은 `server`로 `mcp_tool_list_cache_key`를 미리 계산한 값이다(없으면 지금 계산한다).
    """
    server_id, fingerprint = _resolve_cache_key(server, key)
    result: list[dict[str, Any]] | None | McpToolListMiss = _cache_get(server_id, fingerprint)
    return result


def store_mcp_tool_list(
    server: Any,
    tools: list[dict[str, Any]] | None,
    *,
    key: McpToolListCacheKey | None = None,
) -> None:
    """조회 결과(None이면 실패)를 캐시한다. 성공은 60초, 실패는 30초. 저장 전 행은 무시한다.

    `key`는 조회를 시작할 때 같은 `server`로 계산한 값을 넘긴다 — 그 설정의 결과로 저장한다.
    """
    server_id, fingerprint = _resolve_cache_key(server, key)
    _cache_put(server_id, fingerprint, tools)


def _clear_mcp_tool_list_cache() -> None:
    """프로세스 캐시, 스펙 판정 캐시, 정화·거부 로그 기록을 비운다(테스트 격리용)."""
    from biznotes_core.services.agents.mcp_tool_spec import _clear_spec_verdicts

    with _mcp_tool_list_lock:
        _mcp_tool_list_cache.clear()
    _reported_mcp_tool_specs.clear()
    _clear_spec_verdicts()


async def _discover_mcp_tools(
    ctx: "RunContext",
) -> tuple[list[dict[str, Any]], dict[str, Callable]]:
    """등록·활성 MCP 서버의 도구를 병렬 디스커버리해 스펙으로 합성(opt-in 전용).

    F3 반영: `enabled=False` 사전 제외 + `asyncio.gather` 병렬 + 서버별 타임아웃 상한.
    실패 서버는 조용히 제외하지 않고 `logger.warning` 후 스킵(루프를 죽이지 않음).
    run 단위 캐시는 ctx._mcp_tool_cache에 보관(동일 run 재호출 시 재사용).
    `list_tools` 결과는 run을 넘어 프로세스 캐시(#147)로 재사용한다 — 서브에이전트·다음 턴도
    서버에 다시 붙지 않는다. 서버 목록·승인 조회는 매번 DB에서 한다. 관리자 헬스체크처럼 최신
    목록이 필요한 경로는 이 함수가 아니라 `mcp_client.list_tools`를 직접 부른다.
    """
    from sqlalchemy import select

    from biznotes_core.models import MCPServer
    from biznotes_core.services.agents import mcp_client

    cache = getattr(ctx, "_mcp_tool_cache", None)
    if cache is not None:
        return cache

    from biznotes_core.services.agents.mcp_exposure import approved_tool_pins, mcp_audience

    conditions: list[Any] = [MCPServer.enabled.is_(True)]
    if mcp_audience(ctx.user_role, ctx.user_id) == "user":
        # 일반 사용자는 공개(`all_users`) 서버만 본다 — admin_only 서버는 조회·list_tools·사유
        # 근거 어디에도 들어가지 않는다(#139 A, 리뷰 A-2)
        conditions.append(MCPServer.exposure == "all_users")
    rows = await ctx.db.execute(select(MCPServer).where(*conditions))
    servers = list(rows.scalars().all())
    # 승인 도구는 DB가 정본이다(#139 B). 승인 행이 없는 서버는 list_tools 전에 거른다 — stdio
    # 프로세스를 띄우지 않는다. 승인 조회 오류는 삼키지 않는다(run 트랜잭션 보존). 승인은 승인 시점
    # 스펙 해시와 함께 읽는다 — 고정되지 않은 승인은 빠진다(#293)
    approved = await approved_tool_pins(ctx.db, [s.id for s in servers])
    targets = [s for s in servers if approved.get(s.id)]

    async def _list_one(server: MCPServer) -> tuple[MCPServer, list[dict[str, Any]] | None]:
        key = mcp_tool_list_cache_key(server)  # 조회 시작 시점의 설정으로 조회·저장한다
        cached = get_cached_mcp_tool_list(server, key=key)
        if cached is not MCP_TOOL_LIST_MISS:
            return server, cached
        try:
            tools = await asyncio.wait_for(
                mcp_client.list_tools(server), timeout=MCP_DISCOVERY_TIMEOUT_SECONDS
            )
        except Exception as e:  # noqa: BLE001 — 한 서버 실패가 디스커버리 전체를 막지 않음
            logger.warning(
                "MCP 도구 디스커버리 실패 — 스킵",
                server_id=str(server.id),
                error=str(e),
            )
            store_mcp_tool_list(server, None, key=key)
            return server, None
        store_mcp_tool_list(server, tools, key=key)  # 저장은 사본이다
        return server, tools

    results = await asyncio.gather(*(_list_one(s) for s in targets))
    # 노출 0개의 사유를 가르는 근거(#139 C) — 서버가 없는지, 승인 서버가 응답하지 않았는지
    ctx._mcp_discovery_outcome = {  # type: ignore[attr-defined]
        "servers": len(servers),
        "approved_servers": len(targets),
        "failed_approved_servers": sum(1 for _, tools in results if tools is None),
    }

    specs: list[dict[str, Any]] = []
    dispatchers: dict[str, Callable] = {}
    # 노출 이름은 승인 도구 순서대로 잡는다. 스펙이 거부된 도구도 이름은 예약해, 남은 도구의 이름이
    # 거부 여부와 상관없이 같게 한다(이전 턴의 호출 이름·카탈로그 `mcp_tool_name`과 일치)
    names: set[str] = set()
    for server, tools in results:
        if not tools:
            continue
        server_tag = mcp_server_tag(server.id)
        for tool in tools:
            name = str(tool.get("name", ""))
            if name not in approved[server.id]:
                continue
            exposed = mcp_tool_name(server_tag, tool["name"], names)
            names.add(exposed)
            # 외부 서버가 쓴 설명·스키마를 정화한다(#229)
            try:
                description, parameters = mcp_tool_function(tool)
            except MCPToolSpecRejected as e:
                _report_mcp_tool_spec(server.id, name, e.reason)
                continue
            except Exception:  # noqa: BLE001 — 판정기의 예상 밖 오류도 그 도구만 뺀다(#294)
                _report_mcp_tool_spec(server.id, name, "spec_error")
                continue
            # 승인 뒤 서버가 설명·스키마를 바꿨으면 다시 승인할 때까지 노출하지 않는다(#293)
            if _spec_hash(description, parameters) != approved[server.id][name]:
                _report_mcp_tool_spec(server.id, name, "spec_changed")
                continue
            raw_description = str(tool.get("description") or "")[:MCP_TOOL_TEXT_LIMIT]
            raw_schema = tool.get("input_schema") or parameters  # 비었으면 기본 스키마다
            if description != raw_description or parameters != raw_schema:
                _report_mcp_tool_spec(server.id, name, "sanitized")
            specs.append(
                {
                    "type": "function",
                    "function": {
                        "name": exposed,
                        "description": description,
                        "parameters": parameters,
                    },
                }
            )
            _safe_discovered_mcp_tools.add(exposed)
            dispatchers[exposed] = _make_mcp_dispatcher(server.id, tool["name"])

    result = (specs, dispatchers)
    ctx._mcp_tool_cache = result  # type: ignore[attr-defined]
    return result


# 같은 (서버, 도구, 사유)의 정화·거부 로그는 프로세스에서 한 번만 남긴다 — 일반 사용자 채팅 턴마다
# 디스커버리가 돌아 같은 로그가 쌓이지 않게 한다. 상한을 넘으면 비우고 다시 센다.
_reported_mcp_tool_specs: set[tuple[str, str, str]] = set()
_REPORTED_MCP_TOOL_SPECS_LIMIT = 1024


def _report_mcp_tool_spec(server_id: uuid.UUID, tool: str, reason: str) -> None:
    """정화(사유 `sanitized`)는 info, 거부(`MCPToolSpecRejectReason`)와 승인 뒤 변경(`spec_changed`,
    #293)은 warning. 모두 같은 중복 제거를 탄다. 원문은 싣지 않는다(도구 이름은 승인 정규식을 통과한
    값이다)."""
    key = (str(server_id), tool, reason)
    if key in _reported_mcp_tool_specs:
        return
    if len(_reported_mcp_tool_specs) >= _REPORTED_MCP_TOOL_SPECS_LIMIT:
        _reported_mcp_tool_specs.clear()
    _reported_mcp_tool_specs.add(key)
    if reason == "sanitized":
        logger.info("MCP 도구 설명·스키마 정화", server_id=key[0], tool=tool)
    elif reason == "spec_changed":
        logger.warning(
            "MCP 도구 스펙이 승인 뒤 바뀜 — 다시 승인할 때까지 노출하지 않음",
            server_id=key[0],
            tool=tool,
            reason=reason,
        )
    else:
        logger.warning(
            "MCP 도구 스펙 거부 — 노출하지 않음", server_id=key[0], tool=tool, reason=reason
        )


def mcp_server_tag(server_id: uuid.UUID | str) -> str:
    """노출 함수명에 쓰는 서버 식별자 — uuid 앞 8 hex만 쓴다(함수명 64자 상한 회피, C2)."""
    return str(server_id).replace("-", "")[:8]


def mcp_tool_name(server_tag: str, tool_name: str, taken: Container[str]) -> str:
    """디스커버리된 MCP 도구의 노출 함수명을 OpenAI 호환 형식으로 안정 생성(C2·C3).

    형식 `mcp_<8hex>_<tool>` — (1) `[a-zA-Z0-9_-]` 외 문자를 `_`로 정규화(C3:
    `github.search`·`filesystem/read_file` 등이 함수명 패턴을 위반하지 않도록),
    (2) 64자 초과 시 절단(C2). 절단/정규화 충돌로 동명이 생기면 숫자 접미사로
    유일화한다(디스패처는 노출 이름으로 바인딩, 원본 tool_name은 클로저가 보존).
    `taken`은 이미 쓴 노출 이름이다(포함 여부만 본다). 런타임은 승인 도구만 차례로 넣는다.

    형식은 공개 계약이다(소비 제품이 역파싱한다, #152): `MCP_TOOL_PREFIX` + 서버 태그 + `_` +
    `[^a-zA-Z0-9_-]`를 `_`로 바꾼 도구 이름, `MAX_TOOL_NAME_LEN`(64)자 절단, 충돌하면
    `base[:64 - len(suffix)] + suffix`(`suffix`는 `_1`…`_999`).
    """
    safe_tool = re.sub(r"[^a-zA-Z0-9_-]", "_", tool_name)
    base = f"{MCP_TOOL_PREFIX}{server_tag}_{safe_tool}"
    if len(base) > MAX_TOOL_NAME_LEN:
        base = base[:MAX_TOOL_NAME_LEN]
    if base not in taken:
        return base
    # 충돌 — 접미사 자리를 확보하며 유일화.
    for i in range(1, 1000):
        suffix = f"_{i}"
        candidate = base[: MAX_TOOL_NAME_LEN - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
    return base  # 사실상 도달 불가(서버당 도구 수 한정)


def discovered_mcp_tool_side_effect(*, approved: bool) -> bool:
    """디스커버리된 MCP 도구의 부작용 표시 — 승인(read-only) 도구만 False다(#152).

    런타임은 승인 도구만 노출하고 안전 집합에 등록하므로 `is_side_effect_tool(노출 이름)`과 같은
    답이다. 카탈로그는 프로세스 상태(안전 집합)를 바꾸지 않고 이 규칙으로 표시한다.
    """
    return not approved


def _make_mcp_dispatcher(server_id: Any, tool_name: str) -> Dispatcher:
    """특정 MCP 서버/도구로 바인딩된 디스패처 — call_tool로 위임."""
    async def _dispatch(ctx: "RunContext", name: str, args: dict[str, Any]) -> str:
        from biznotes_core.services.agents import mcp_exposure
        from biznotes_core.services.agents.errors import ToolUnavailableError
        from biznotes_core.services.agents.mcp_client import call_tool, caller_meta

        # 호출 시점 재확인(#139 A-4) — 활성·공개 범위·승인을 run 캐시가 아니라 DB에서 다시 본다
        audience = mcp_exposure.mcp_audience(ctx.user_role, ctx.user_id)
        server, verdict = await mcp_exposure.check_mcp_call(
            ctx.db, server_id, tool_name, audience=audience
        )
        server_name = str(server.name) if server is not None and server.name else None

        def append_failed() -> None:
            ctx.tool_evidence.append(
                {
                    "kind": "mcp",
                    "server": server_name,
                    "tool": tool_name,
                    "status": "failed",
                }
            )

        # 실패도 evidence에 남긴다 — 성공만 기록하면 "도구를 시도했는가"를
        # 감사 로그에서 재구성할 수 없다. 오류는 구조화 타입으로 올려
        # react 루프가 사유를 모델에 그대로 전달하게 한다.
        if server is None or verdict != "ok":
            append_failed()
            raise ToolUnavailableError(mcp_exposure.mcp_call_denial_message(verdict, audience))
        call_id = None
        if audience == "user":
            try:
                call_id = await mcp_exposure.audit_user_mcp_call(ctx, server, tool_name, args)
            except ToolUnavailableError:
                append_failed()  # 감사 실패로 호출하지 않았어도 시도는 evidence에 남긴다
                raise
        try:
            result = await call_tool(server, tool_name, args or {}, meta=caller_meta(ctx, server))
        except Exception:
            append_failed()
            if call_id is not None:
                await mcp_exposure.audit_user_mcp_result(ctx, server, tool_name, "failed", call_id)
            raise
        ctx.tool_evidence.append(
            {"kind": "mcp", "server": server.name, "tool": tool_name, "status": "succeeded"}
        )
        if call_id is not None:
            await mcp_exposure.audit_user_mcp_result(ctx, server, tool_name, "succeeded", call_id)
        return result

    return _dispatch


def _http_request_spec() -> dict[str, Any]:
    """이 실행의 연결 정책에 맞는 `http_request` 스펙(#395).

    open이면 공유 스펙 그대로다. 받아오기 전용이면 body가 없고 method가 GET·HEAD enum인 **새
    dict**다 — 공유 `_BUILTIN_SPECS`는 고치지 않는다. 실행 시 거부는 관문(`check_egress`)이 한다.
    """
    from biznotes_core.services.agents.policy import agent_egress_policy

    if not agent_egress_policy().fetch_only:
        return _BUILTIN_SPECS["http_request"]
    return {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": "외부 URL에서 내용을 받아온다(GET·HEAD만, 요청 본문·헤더 없음).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {
                        "type": "string",
                        "enum": ["GET", "HEAD"],
                        "description": "GET 또는 HEAD(기본 GET)",
                    },
                },
                "required": ["url"],
            },
        },
    }


async def build_tool_specs(
    ctx: "RunContext", allowed: list[str] | None = None
) -> tuple[list[dict[str, Any]], dict[str, Dispatcher]]:
    """노출 도구 스펙 + 이름→디스패처 매핑을 런타임에 동적 생성.

    Args:
        ctx: 실행 컨텍스트(MCP 디스커버리·서브에이전트 가드에 사용).
        allowed: 노출 허용 도구 이름 화이트리스트. None이면 DEFAULT_TOOLS
            (`rag_search`·`call_agent`)만 노출(T4·Q2 기본 안전값).
            "mcp"가 포함되면 MCP 도구를 opt-in 디스커버리(Q6 승인 전까지 호출자 책임).

    Returns:
        (specs, dispatchers): specs는 allowed 순서를 보존(첫 도구 결정성 — mock 의존).
            dispatchers는 도구 이름 → async 디스패처.
    """
    names = list(DEFAULT_TOOLS) if allowed is None else list(allowed)

    specs: list[dict[str, Any]] = []
    dispatchers: dict[str, Dispatcher] = {}
    builtin_dispatch = _builtin_dispatcher()
    want_mcp = False
    skill_specs: dict[str, tuple[dict[str, Any], Dispatcher]] | None = None

    for name in names:
        if name in _BUILTIN_SPECS:
            # rag_search는 항상, web_search/http_request는 명시 opt-in일 때만.
            if name in _OPTIN_BUILTIN and allowed is None:
                continue
            if name == "mcp_call" and not await _mcp_call_exposed(ctx):
                # `mcp` 디스커버리와 같은 경계(#77·#139 A) — 일반 사용자에게는 켜진 공개 서버에
                # 승인 도구가 있을 때만 노출한다. 디스패처도 호출 시점에 다시 확인한다.
                logger.warning(
                    "mcp_call 노출 거부 — 이 실행에 공개된 MCP 도구가 없음",
                    user_id=str(ctx.user_id) if ctx.user_id else None,
                    user_role=ctx.user_role,
                )
                continue
            specs.append(_http_request_spec() if name == "http_request" else _BUILTIN_SPECS[name])
            dispatchers[name] = builtin_dispatch
        elif name == "call_agent":
            specs.append(_CALL_AGENT_SPEC)
            dispatchers[name] = _call_agent_dispatcher()
        elif name == "run_skill":
            skill_spec = await _published_skill_spec(ctx)
            if skill_spec is not None:
                specs.append(skill_spec)
                dispatchers[name] = _run_skill_dispatcher()
        elif name in _SKILL_RUNTIME_TOOLS:
            if skill_specs is None:
                skill_specs = await _skill_runtime_specs(ctx)
            if name in skill_specs:
                spec, dispatcher = skill_specs[name]
                specs.append(spec)
                dispatchers[name] = dispatcher
        elif name == _SKILL_SCRIPT_TOOL:
            from biznotes_core.services.skills import sandbox as skill_sandbox

            script_spec = await skill_sandbox.run_skill_script_spec(ctx)
            if script_spec is not None:
                specs.append(script_spec)
                dispatchers[name] = skill_sandbox.dispatch_run_skill_script
        elif name == "mcp":
            want_mcp = True
        else:
            logger.warning("알 수 없는 도구 이름 — 무시", tool=name)

    # MCP는 명시 opt-in("mcp")일 때만 디스커버리한다. Q6(#139 A): 관리자·시스템 실행은 켜진 모든
    # 서버, 일반 사용자 실행은 관리자가 공개(`all_users`)한 서버만 본다. 어느 쪽이든 승인된
    # read-only 도구만 노출한다. MCPServer는 관리자 자격증명을 담는 전역 리소스다.
    if want_mcp:
        mcp_specs, mcp_dispatchers = await _discover_mcp_tools(ctx)
        specs.extend(mcp_specs)
        dispatchers.update(mcp_dispatchers)

    return specs, dispatchers


async def _skill_runtime_specs(
    ctx: "RunContext",
) -> dict[str, tuple[dict[str, Any], Dispatcher]]:
    """스킬 도구 두 개의 스펙·디스패처. 쓸 수 있는 스킬이 없으면 둘 다 없다(D6-1).

    카탈로그는 한 번만 계산한다. 활성화 디스패처는 호출할 때 권한을 다시 확인한다.
    """
    from biznotes_core.services.skills import runtime as skill_runtime

    activate = await skill_runtime.activate_skill_spec(ctx)
    if activate is None:
        return {}
    return {
        "activate_skill": (activate, skill_runtime.dispatch_activate_skill),
        "read_skill_file": (
            copy.deepcopy(skill_runtime.READ_SKILL_FILE_SPEC),
            skill_runtime.dispatch_read_skill_file,
        ),
    }


async def _mcp_call_exposed(ctx: "RunContext") -> bool:
    """`mcp_call` 스펙을 노출하는가 — 관리자·시스템은 항상, 일반 사용자는 켜진 `all_users` 서버에
    승인 read-only 도구가 하나라도 있을 때(디스커버리와 같은 기준, #139 A)."""
    from biznotes_core.services.agents import mcp_exposure

    if _is_admin_audience(ctx.user_role, ctx.user_id):
        return True
    return await mcp_exposure.user_mcp_tools_available(ctx.db)


async def _published_skill_spec(ctx: "RunContext") -> dict[str, Any] | None:
    """공개된 system skill wrapper를 capability enum으로 생성한다(노출 version 기준)."""
    entries: list[tuple[str, str]] = []
    for wrapper, _skill, version in (await ctx.db.execute(published_system_skill_query())).all():
        if not skill_manifest_is_read_only(version.manifest):
            continue
        label, _ = exposed_skill_label(version, wrapper)
        entries.append((label, str(wrapper.id)))
    if not entries:
        return None
    entries.sort()
    choices = [agent_id for _, agent_id in entries]
    labels = ", ".join(f"{label} ({agent_id})" for label, agent_id in entries[:20])
    spec: dict[str, Any] = {"type": "function", "function": dict(_RUN_SKILL_SPEC["function"])}
    spec["function"]["description"] += f" 사용 가능한 스킬: {labels}"
    spec["function"]["parameters"] = dict(spec["function"]["parameters"])
    spec["function"]["parameters"]["properties"] = dict(
        spec["function"]["parameters"]["properties"]
    )
    spec["function"]["parameters"]["properties"]["agent_id"] = {
        "type": "string",
        "enum": choices,
        "description": "published system skill wrapper UUID",
    }
    return spec


def _domain_allowed(host: str, allowlist: list[str]) -> bool:
    """와일드카드 서브도메인 매칭 — 선행 점 포함 접미사 비교(SEC-D3 반영).

    `evil-internal.corp`(점 없이 이어붙은 문자열)나 `internal.corp.attacker.com`
    같은 우회를 막기 위해 `*.internal.corp`는 `.internal.corp` 접미사로만
    매칭한다(정확 매칭이 아닌 한 호스트 == 접미사 자체는 허용하지 않음).
    판정은 연결 정책의 허용 호스트(#395)와 같은 `core.ssrf.host_matches`다.
    """
    from biznotes_core.core.ssrf import host_matches

    return host_matches(host, allowlist)


def auto_approve_match(
    tool_name: str, arguments: dict[str, Any], policy: dict[str, Any] | None
) -> bool:
    """`auto_approve_tools` 정책과 도구 호출 인자를 매칭(CR-03·SEC-D3 반영).

    매칭 실패(정책에 도구 키 없음, 조건 불일치)는 항상 False(승인 게이트로 폴백).
    http_request는 이 함수가 True를 반환해도 **SSRF 검증(`validate_external_url`)을
    별도로 통과해야 최종 자동 승인**된다 — 이 함수는 allowlist 판정만 담당한다.
    """
    if not policy:
        return False
    rule = policy.get(tool_name)
    if not isinstance(rule, dict):
        return False

    if tool_name == "http_request":
        url = str(arguments.get("url", ""))
        method = str(arguments.get("method", "GET")).upper()
        domains = rule.get("domains")
        methods = rule.get("methods")
        if not isinstance(domains, list) or not domains:
            return False
        host = urlparse(url).hostname or ""
        if not _domain_allowed(host, domains):
            return False
        if isinstance(methods, list) and methods and method not in [
            str(m).upper() for m in methods
        ]:
            return False
        return True

    if tool_name == "call_agent":
        agent_ids = rule.get("agent_ids")
        if not isinstance(agent_ids, list) or not agent_ids:
            return False
        return str(arguments.get("agent_id", "")) in {str(a) for a in agent_ids}

    return False
