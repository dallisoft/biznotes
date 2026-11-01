"""에이전트 실행·워크플로우 정의 정책의 서비스 계층 관문."""

import re
from collections import deque
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, status

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from biznotes_core.core.ssrf import EgressPolicy
    from biznotes_core.models import Agent, User
    from biznotes_core.services.agents.context import RunContext

_STRING_OUTPUT_NODE_TYPES = frozenset({"llm", "react_agent", "json_validate"})

# `mcp_tool` 노드를 실행할 수 있는 정의의 scope(#77). 관리자가 관리하는 정의만 신뢰한다 —
# 실행 시점 관문(`assert_mcp_tool_node_allowed`)과 저장 시점 헬퍼(`find_forbidden_mcp_tool_nodes`)가
# 이 상수 하나를 공유해 저장과 실행의 판정이 갈리지 않는다.
_MCP_TRUSTED_AGENT_SCOPES = frozenset({"system"})


async def assert_can_run_skill_wrapper(
    db: "AsyncSession", user: "User", agent: "Agent"
) -> None:
    """wrapper Agent의 연결 Skill 실행 권한 검사 — 불가 시 404(존재 은닉, §13.2).

    이전에는 `api/v1/agents.py`의 헬퍼에 위임했다. 서비스가 라우터를 import하는
    계층 위반이라 구현을 이쪽으로 내렸다 — 라우터가 서비스를 쓰는 것이 맞는 방향이다.
    """
    from biznotes_core.models import Skill, SkillVersion
    from biznotes_core.services.skills.service import can_run

    if agent.skill_version_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    version = await db.get(SkillVersion, agent.skill_version_id)
    skill = await db.get(Skill, version.skill_id) if version else None
    if skill is None or not can_run(skill, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")


async def assert_agent_execution_allowed(
    db: "AsyncSession", user: "User", agent: "Agent"
) -> None:
    """수동·채팅 진입이 공유하는 실행 인가 OR 식을 강제한다."""
    from biznotes_core.services.authz import can_run_agent, can_run_owner_draft

    owner_draft = can_run_owner_draft(user, agent)
    if not agent.enabled and not owner_draft:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    if agent.source_kind == "skill_package":
        await assert_can_run_skill_wrapper(db, user, agent)
    elif not (can_run_agent(user, agent) or owner_draft):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")


def chat_output_reference_error(definition: dict[str, Any]) -> str | None:
    """명시된 chat_output의 형식·참조 유효성 오류를 반환한다.

    chat_output이 없는 일반 workflow는 유효하다. capability의 fallback 성립 여부는
    `is_chat_callable`이 별도로 판정한다.
    """
    ref = definition.get("chat_output")
    if ref is None:
        return None
    if not isinstance(ref, dict):
        return "chat_output은 객체여야 합니다"

    node_id = ref.get("node_id")
    if not isinstance(node_id, str) or not node_id:
        return "chat_output.node_id가 필요합니다"
    if ref.get("output_key", "text") != "text":
        return "chat_output.output_key는 text만 허용됩니다"

    nodes = definition.get("nodes")
    edges = definition.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return "workflow nodes/edges 형식이 올바르지 않습니다"

    node_by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if isinstance(node, dict):
            candidate_id = node.get("id")
            if isinstance(candidate_id, str):
                node_by_id[candidate_id] = node
    target = node_by_id.get(node_id)
    if target is None:
        return f"chat_output 노드를 찾을 수 없습니다: {node_id}"
    if target.get("type") not in _STRING_OUTPUT_NODE_TYPES:
        return "chat_output은 llm, react_agent, json_validate 노드만 참조할 수 있습니다"

    incoming = {candidate_id: 0 for candidate_id in node_by_id}
    outgoing: dict[str, list[str]] = {candidate_id: [] for candidate_id in node_by_id}
    for edge in edges:
        if not isinstance(edge, list) or len(edge) != 2:
            return "workflow edge는 [source, target] 형식이어야 합니다"
        raw_source, raw_destination = edge
        if not isinstance(raw_source, str) or not isinstance(raw_destination, str):
            return "workflow edge 노드 ID는 문자열이어야 합니다"
        source = raw_source
        destination = raw_destination
        if source not in node_by_id or destination not in node_by_id:
            return "workflow edge가 존재하지 않는 노드를 참조합니다"
        outgoing[source].append(destination)
        incoming[destination] += 1

    roots = [candidate_id for candidate_id, count in incoming.items() if count == 0]
    reachable: set[str] = set()
    queue = deque(roots)
    while queue:
        current = queue.popleft()
        if current in reachable:
            continue
        reachable.add(current)
        queue.extend(outgoing[current])
    if node_id not in reachable:
        return f"chat_output 노드가 시작 노드에서 도달 불가능합니다: {node_id}"
    return None


def validate_chat_output_reference(definition: dict[str, Any]) -> None:
    error = chat_output_reference_error(definition)
    if error is not None:
        raise ValueError(error)


def agent_audience_error(definition: dict[str, Any]) -> str | None:
    """정의 최상위 `audience`의 저장 시점 오류를 돌려준다(#436 D1). 제품 저장 422용이다.

    키가 없거나 null이면 None이다. `"admin"`(`ADMIN_AUDIENCE`) 밖의 값(빈 문자열·다른 문자열·bool·
    숫자 등)은 오류다. 런타임은 그런 값도 관리자 전용으로 보므로(닫힌 쪽) 저장에서 막아 의도를
    드러낸다. scope는 보지 않는다 — user scope에 붙은 키는 런타임이 무시한다. 문구에 입력값을
    넣지 않는다. FastAPI에 의존하지 않으므로 HTTPException은 호출자가 낸다.
    """
    from biznotes_core.services.authz import ADMIN_AUDIENCE

    if not isinstance(definition, dict):
        return None
    value = definition.get("audience")
    if value is None or value == ADMIN_AUDIENCE:
        return None
    return f'audience는 "{ADMIN_AUDIENCE}"(관리자 전용)만 둘 수 있습니다'


async def assert_mcp_tool_node_allowed(ctx: "RunContext") -> None:
    """`mcp_tool` 노드를 이 ctx에서 실행해도 되는지 확인한다. 안 되면 `AgentRuntimeError`(#77).

    MCPServer는 `owner_id` 없는 전역 리소스(관리자 env 자격증명)이고 이 노드에는 승인 게이트가
    없다. 그래서 **정의의 작성 주체**를 신뢰 기준으로 삼는다 — 실행 중인 정의(`ctx.agent_id`)가
    `_MCP_TRUSTED_AGENT_SCOPES`(system, 관리자 관리)일 때만 허용하고, 실행자가 누구든(관리자가
    사용자 정의를 실행해도) 그 밖의 정의는 거부한다. `child()`가 `agent_id`를 호출된 정의로 바꾸므로
    서브에이전트·loop 본문은 호출된 정의 기준이다.

    귀속할 정의가 없는 ctx(`agent_id is None`)만 실행 권한(admin 또는 사용자 없는 시스템 실행)으로
    판정한다. `agent_id`가 있는데 `Agent` 행이 없으면(삭제 경합) 실행자와 무관하게 거부한다 —
    폴백으로 통과시키면 관리자 실행 중에 혼동된 대리인 보호가 풀린다.
    """
    from biznotes_core.models import Agent
    from biznotes_core.services.agents.errors import AgentRuntimeError
    from biznotes_core.services.agents.tool_registry import _is_admin_audience

    if ctx.agent_id is None:
        if _is_admin_audience(ctx.user_role, ctx.user_id):
            return
    else:
        agent = await ctx.db.get(Agent, ctx.agent_id)
        scope = getattr(agent, "scope", None)
        if isinstance(scope, str) and scope in _MCP_TRUSTED_AGENT_SCOPES:
            return
    raise AgentRuntimeError(
        "mcp_tool 노드는 관리자가 관리하는(system) 워크플로우에서만 실행할 수 있습니다 — "
        "전역 MCP 서버(관리자 자격증명)의 도구는 사용자 소유 워크플로우에서 호출할 수 없습니다. "
        "필요하면 관리자가 system 워크플로우로 등록하세요."
    )


def find_forbidden_mcp_tool_nodes(
    nodes: list[dict[str, Any]], agent_scope: str | None
) -> list[tuple[str, str]]:
    """`agent_scope`로 저장할 때 거부할 MCP 노드를 `(node_id, kind)` 정렬 목록으로 돌려준다(#77).

    `nodes`는 `kind_to_nodes` 결과다(`kind="mcp"`도 `mcp_tool` 노드로 정규화된다). system이면
    빈 목록이다. kind는 `"mcp_tool"`(노드 타입이 `mcp_tool`)과 `"mcp_call"`(react_agent의
    `allowed_tools`에 `mcp_call`)이다. `mcp_call` 판정은 실행 규칙(실행자·allowlist)이 아니라 저장
    시점 UX 보조다 — system 정의에 두어도 저장은 통과하지만 admin·시스템 run에서만 동작한다.
    node_id는 `str(id)`이고 id가 없으면 빈 문자열이다(노드 id는 제약이 없는 문자열이라 이어 붙이면
    모호하다). FastAPI에 의존하지 않으므로 HTTPException은 호출자가 낸다.
    """
    if isinstance(agent_scope, str) and agent_scope in _MCP_TRUSTED_AGENT_SCOPES:
        return []
    found: set[tuple[str, str]] = set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        raw_id = node.get("id")
        node_id = "" if raw_id is None else str(raw_id)
        node_type = node.get("type")
        if node_type == "mcp_tool":
            found.add((node_id, "mcp_tool"))
        elif node_type == "react_agent":
            tools = node.get("allowed_tools")
            if isinstance(tools, list) and "mcp_call" in {str(t) for t in tools}:
                found.add((node_id, "mcp_call"))
    return sorted(found)


# 직접 부작용 노드(#153) — 렌더링한 요청을 그대로 외부로 보낸다. 기본은 HITL 승인 대상이고,
# 관리자가 관리하는(system) 정의만 `requires_approval: false`로 끌 수 있다. `save_sources`(#403)는
# 렌더링한 목록으로 노트북에 소스를 만든다 — 같은 규칙(승인·fan-out 금지)을 따른다.
DIRECT_SIDE_EFFECT_NODE_TYPES = frozenset({"http", "mcp_tool", "save_sources"})
_DIRECT_NODE_UNGATED_SCOPES = frozenset({"system"})

# `save_sources` 중복 키의 namespace(#403) — 노드 리터럴이다(렌더링하지 않는다)
_SAVE_SOURCES_NAMESPACE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
# `save_sources` allowed_hosts 항목 — 호스트 이름 또는 `*.` 접미사 패턴. scheme·포트·경로·
# userinfo·템플릿 글자를 받지 않는다
_SAVE_SOURCES_HOST_RE = re.compile(r"(?:\*\.)?[^\s/\\:@?#{}\[\]*%.][^\s/\\:@?#{}\[\]*%]*")


def save_sources_config_errors(node: dict[str, Any]) -> list[str]:
    """`save_sources` 노드의 리터럴 설정 오류 목록(#403). 저장 헬퍼와 실행기가 같이 쓴다.

    - `namespace`: `[a-z0-9][a-z0-9._-]{0,63}` 리터럴. 템플릿으로 바꿀 수 없다(중복 키가 된다).
    - `items`: 템플릿 문자열 하나.
    - `allowed_hosts`(선택): url 항목을 받아 올 호스트(`host_matches` 패턴) 리터럴 목록. 두면 비어
      있으면 안 된다. 없으면 url 항목은 모두 `host_not_allowed`다.
    - `notebook_rule`(선택): 리터럴 규칙(`notebook_series.notebook_rule_errors`). 없으면 대상은
      run의 노트북이다.
    """
    from biznotes_core.services.notebook_series import notebook_rule_errors

    errors: list[str] = []
    namespace = node.get("namespace")
    if not isinstance(namespace, str) or not _SAVE_SOURCES_NAMESPACE_RE.fullmatch(namespace):
        errors.append(
            "save_sources namespace는 [a-z0-9][a-z0-9._-]{0,63} 형식의 리터럴이어야 합니다"
        )
    if not isinstance(node.get("items"), str):
        errors.append("save_sources items는 템플릿 문자열 하나여야 합니다")
    if "allowed_hosts" in node:
        hosts = node["allowed_hosts"]
        if not isinstance(hosts, list) or not hosts:
            errors.append("save_sources allowed_hosts는 비어 있지 않은 호스트 목록이어야 합니다")
        elif not all(
            isinstance(h, str)
            and len(h) <= 253
            and _SAVE_SOURCES_HOST_RE.fullmatch(h)
            and not h.endswith(".")
            and ".." not in h
            for h in hosts
        ):
            errors.append(
                "save_sources allowed_hosts에는 호스트 이름이나 *.example.com 형식의 리터럴만 "
                "둘 수 있습니다(scheme·포트·경로·템플릿 없음)"
            )
    if "notebook_rule" in node:
        errors.extend(f"save_sources {e}" for e in notebook_rule_errors(node["notebook_rule"]))
    return errors


def url_target_is_literal(url: Any) -> bool:
    """렌더링 전 url의 대상(scheme·userinfo·host·port)이 리터럴인가(#153 A-1, 재리뷰 R2).

    scheme이 리터럴 `http`·`https`이고 netloc(userinfo·host·port 전체)이 비어 있지 않으며
    템플릿 참조(`{`)가 없어야 한다. 그러면 authority는 리터럴 `/`·`?`·`#`나 문자열 끝에서
    닫혀 path·query 템플릿으로 host를 바꿀 수 없다(`https://api.example.com{params.p}`에
    `@evil.com`을 넣는 우회 차단). 해석할 수 없는 url(`http://[bad` 등 `ValueError`)은 리터럴이
    아니다(fail closed) — 저장 헬퍼가 예외 대신 사유를 낸다.
    """
    from urllib.parse import urlsplit

    if not isinstance(url, str) or "{" in url.split("://", 1)[0]:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc) and "{" not in parts.netloc


async def direct_node_requires_approval(ctx: "RunContext", node: dict[str, Any]) -> bool:
    """정의 설정만으로 직접 노드가 승인을 받아야 하는가(#153 A-1). 판정할 수 없으면 True(게이트).

    `requires_approval`이 리터럴 `False`이고, 실행 중인 정의(`ctx.agent_id`)를 DB에서 다시 읽어
    scope가 system이며, http 노드면 url 대상이 리터럴일 때만 False다. 문자열 `"false"`·`0`,
    user·group 정의, 정의 없는 ctx(관리자 실행 포함)는 모두 게이트한다 — #77의 관리자 fallback을
    이 판정에는 쓰지 않는다. 거부가 아니라 게이트인 것은 검증기가 생기기 전에 저장된 정의를
    망가뜨리지 않기 위해서다. MCP 승인 행까지 본 최종 판정은 `direct_node_needs_gate`다.
    """
    from biznotes_core.models import Agent

    if node.get("requires_approval", True) is not False or ctx.agent_id is None:
        return True
    if node.get("type") == "http" and not url_target_is_literal(node.get("url")):
        return True
    agent = await ctx.db.get(Agent, ctx.agent_id, populate_existing=True)
    scope = getattr(agent, "scope", None)
    return not (isinstance(scope, str) and scope in _DIRECT_NODE_UNGATED_SCOPES)


async def _mcp_tool_read_only(ctx: "RunContext", server_id: Any, tool_name: str) -> bool | None:
    """`MCPServerTool` 승인 행의 read_only. 행이 없으면 None."""
    from sqlalchemy import select

    from biznotes_core.models import MCPServerTool

    result = await ctx.db.execute(
        select(MCPServerTool.read_only).where(
            MCPServerTool.server_id == server_id, MCPServerTool.tool_name == tool_name
        )
    )
    value = result.scalar_one_or_none()
    return None if value is None else bool(value)


async def direct_node_needs_gate(ctx: "RunContext", node: dict[str, Any]) -> bool:
    """직접 노드 실행 전 HITL 게이트가 필요한가 — 실행기와 fan-out 사전 차단이 공유한다(재리뷰 R1).

    정의 판정(`direct_node_requires_approval`)에 더해 `mcp_tool`은 승인 행이 read-only일 때만
    게이트를 건너뛴다(`read_only=False`·행 없음은 게이트). 판정 중 오류(DB 오류·잘못된
    server_id)는 게이트로 본다.
    """
    import uuid

    try:
        if await direct_node_requires_approval(ctx, node):
            return True
        if node.get("type") == "mcp_tool":
            server_id = uuid.UUID(str(node.get("server_id")))
            return await _mcp_tool_read_only(ctx, server_id, str(node.get("tool_name"))) is not True
        return False
    except Exception:  # noqa: BLE001 — 판정할 수 없으면 보수적으로 게이트한다
        return True


def find_direct_node_approval_violations(
    nodes: list[dict[str, Any]], edges: list[list[str]], agent_scope: str | None
) -> list[str]:
    """`agent_scope`로 저장할 때 거부할 직접 노드 승인 설정을 `"<node_id>: <사유>"` 목록으로 낸다.

    `nodes`·`edges`는 `kind_to_nodes` 결과다(tool·plugin·mcp kind도 노드로 정규화된다)(#153 A-1).
    - `requires_approval`이 bool이 아니다.
    - system이 아닌 정의가 `requires_approval: false`를 둔다.
    - `requires_approval: false`인 http 노드의 url 대상(scheme·userinfo·host·port)이
      리터럴이 아니다.
    - 승인이 필요한 직접 노드가 병렬(fan-out) 그래프에 있다 — 형제가 취소되며 승인이 반복된다(A-4).
    - `save_sources`의 리터럴 설정이 잘못됐다(`save_sources_config_errors`, #403).
    - 받아오기 전용 모드면 http 노드가 그 규칙을 어긴다(#395, `find_http_egress_violations`를 호출
      시점 설정으로 부른다). open 모드에서는 이 사유가 없다.
    FastAPI에 의존하지 않으므로 HTTPException은 호출자가 낸다.
    """
    from biznotes_core.services.agents.graph import _has_fan_out

    trusted = isinstance(agent_scope, str) and agent_scope in _DIRECT_NODE_UNGATED_SCOPES
    node_map = {n["id"]: n for n in nodes if isinstance(n, dict) and "id" in n}
    fan_out = _has_fan_out(node_map, edges)
    found: set[str] = set()
    for node in node_map.values():
        if node.get("type") not in DIRECT_SIDE_EFFECT_NODE_TYPES:
            continue
        node_id = str(node.get("id"))
        if node.get("type") == "save_sources":
            found.update(f"{node_id}: {e}" for e in save_sources_config_errors(node))
        value = node.get("requires_approval", True)
        if not isinstance(value, bool):
            found.add(f"{node_id}: requires_approval은 true/false여야 합니다")
            continue
        if value is False and not trusted:
            found.add(f"{node_id}: requires_approval=false는 system 정의에서만 둘 수 있습니다")
        if value is False and node.get("type") == "http" and not url_target_is_literal(
            node.get("url")
        ):
            found.add(
                f"{node_id}: 승인 없는 http 노드의 url 대상(scheme·호스트·포트)은 "
                "리터럴이어야 합니다"
            )
        if fan_out and (value is not False or not trusted):
            found.add(f"{node_id}: 승인이 필요한 노드는 병렬(fan-out) 그래프에 둘 수 없습니다")
    found.update(find_http_egress_violations(list(node_map.values()), policy=agent_egress_policy()))
    return sorted(found)


def agent_egress_policy() -> "EgressPolicy":
    """호출 시점 설정으로 에이전트 외부 HTTP 연결 정책을 만든다(#395).

    `core.ssrf`는 설정을 읽지 않으므로 서비스 계층이 만든다. `http_request` 도구·`http` 노드의
    실행과 저장 검증이 이 함수 하나를 쓴다 — 모드는 API·워커 각자의 설정이다.
    """
    from biznotes_core.config import get_settings
    from biznotes_core.core.ssrf import EgressPolicy

    settings = get_settings()
    return EgressPolicy(
        fetch_only=settings.agent_http_egress_mode == "fetch_only",
        allowed_hosts=tuple(settings.agent_http_allowed_host_list),
        max_url_chars=settings.agent_http_max_url_chars,
        max_query_chars=settings.agent_http_max_query_chars,
    )


# 받아오기 전용 모드에서 http 노드 정의가 어긴 규칙(#395) — 사유 코드는 `core.ssrf`와 같다
_HTTP_EGRESS_RULES: dict[str, str] = {
    "method": "받아오기 전용 모드의 http 노드는 method를 GET 또는 HEAD로 명시해야 합니다"
    "(기본값은 POST)",
    "body": "받아오기 전용 모드의 http 노드에는 body를 둘 수 없습니다",
    "header": "받아오기 전용 모드의 http 노드 헤더는 Accept·Accept-Language·If-None-Match·"
    "If-Modified-Since·Range·User-Agent만, 템플릿·제어 문자 없는 문자열 값으로 둘 수 있습니다",
    "non_literal_target": "받아오기 전용 모드의 http 노드 url 대상(scheme·호스트·포트)은 "
    "리터럴이어야 합니다",
    "userinfo": "받아오기 전용 모드의 http 노드 url에는 사용자 정보(userinfo)를 둘 수 없습니다",
}


def _http_node_egress_reasons(node: dict[str, Any], policy: "EgressPolicy") -> list[str]:
    """http 노드 정의(렌더링 전)가 받아오기 전용 규칙을 어긴 사유 코드. open이면 빈 목록이다.

    저장 검증(`find_http_egress_violations`)과 실행 시 사전 검사(`_exec_http`)가 공유한다. 경로·
    쿼리 템플릿은 허용한다 — 렌더링한 길이는 실행 시 `check_egress`가 본다.
    """
    from urllib.parse import urlsplit

    from biznotes_core.core.ssrf import FETCH_ONLY_HEADERS, FETCH_ONLY_METHODS

    if not policy.fetch_only:
        return []
    reasons: list[str] = []
    method = node.get("method")
    if not isinstance(method, str) or method.upper() not in FETCH_ONLY_METHODS:
        reasons.append("method")
    if node.get("body") is not None:
        reasons.append("body")
    headers = node.get("headers")
    if headers is not None and not (
        isinstance(headers, dict)
        and all(
            isinstance(name, str)
            and name.lower() in FETCH_ONLY_HEADERS
            and isinstance(value, str)
            and "{" not in value
            and not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name + value)
            for name, value in headers.items()
        )
    ):
        reasons.append("header")
    url = node.get("url")
    if not url_target_is_literal(url):
        reasons.append("non_literal_target")
    elif "@" in urlsplit(str(url)).netloc:
        reasons.append("userinfo")
    return reasons


def find_http_egress_violations(
    nodes: list[dict[str, Any]], *, policy: "EgressPolicy"
) -> list[str]:
    """받아오기 전용 정책을 어긴 http 노드 정의를 `"<node_id>: <사유>"` 목록으로 낸다(#395).

    `nodes`는 `kind_to_nodes` 결과다(tool·plugin kind도 http 노드로 정규화된다). `policy`가 open이면
    빈 목록이다. 거부하는 정의: method가 GET·HEAD가 아님(생략하면 기본 POST라 거부), body 있음,
    허용 밖 헤더 이름·템플릿(`{`) 헤더 값, url 대상(scheme·호스트·포트)이 리터럴이 아님, url
    userinfo. FastAPI에 의존하지 않는다 — 422는 호출자가 낸다.
    """
    found: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") != "http":
            continue
        node_id = str(node.get("id"))
        for reason in _http_node_egress_reasons(node, policy):
            found.add(f"{node_id}: {_HTTP_EGRESS_RULES[reason]}")
    return sorted(found)


async def assert_mcp_tool_approved(ctx: "RunContext", server_id: Any, tool_name: str) -> None:
    """`mcp_tool` 노드의 도구가 `MCPServerTool`에 승인돼 있는지 확인한다(#153 A-5).

    행이 없으면 `AgentRuntimeError`(fail closed). 게이트 여부는 `direct_node_needs_gate`가 정한다.
    """
    from biznotes_core.services.agents.errors import AgentRuntimeError

    if await _mcp_tool_read_only(ctx, server_id, tool_name) is None:
        raise AgentRuntimeError(
            f"MCP 도구 '{tool_name}'은 관리자가 승인한 도구가 아닙니다(mcp_server_tools)"
        )


# 사고 양(`reasoning_effort`, #429)을 읽는 노드 타입 — json_validate는 재시도 호출에 쓴다
_REASONING_EFFORT_NODE_TYPES = frozenset({"llm", "react_agent", "json_validate"})


def reasoning_effort_errors(node: dict[str, Any]) -> list[str]:
    """노드 하나의 `reasoning_effort` 설정 오류 목록(#429). 저장 헬퍼와 실행기가 같이 쓴다.

    값은 `REASONING_EFFORTS`(`low`·`medium`·`high`) 리터럴이어야 한다 — 템플릿(`{params.x}`)·다른
    effort 값(`minimal`·`xhigh` 등)·대소문자 변형은 받지 않는다. 키가 없거나 null이면 미설정이다
    (모델 기본값, 없으면 프로바이더 기본). 노드 타입은 보지 않는다 — 타입별 대상은
    `find_reasoning_effort_violations`가 정한다.
    """
    from biznotes_core.models.llm import REASONING_EFFORTS

    value = node.get("reasoning_effort")
    if value is None or (isinstance(value, str) and value in REASONING_EFFORTS):
        return []
    return [f"reasoning_effort는 {'·'.join(REASONING_EFFORTS)} 중 하나의 리터럴이어야 합니다"]


def find_reasoning_effort_violations(nodes: list[dict[str, Any]]) -> list[str]:
    """`reasoning_effort`가 허용 값이 아닌 노드를 `"<node_id>: <사유>"` 정렬 목록으로 낸다(#429).

    `nodes`는 `kind_to_nodes` 결과다. 이 키를 읽는 노드(llm·react_agent·json_validate)만 본다.
    실행기도 모델 호출 전에 같은 검사(`reasoning_effort_errors`)로 실패한다(검증기 전에 저장된
    정의). FastAPI에 의존하지 않으므로 HTTPException은 호출자가 낸다.
    """
    found: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") not in _REASONING_EFFORT_NODE_TYPES:
            continue
        found.update(f"{node.get('id')}: {e}" for e in reasoning_effort_errors(node))
    return sorted(found)


# 출력 하한(`min_output_tokens`, #428)을 읽는 노드 타입 — llm은 스트림 경로만, json_validate는
# 재시도 호출에 쓴다
_MIN_OUTPUT_TOKENS_NODE_TYPES = frozenset({"llm", "json_validate"})
# llm·json_validate 실행기의 `max_tokens` 기본값(`nodes/llm.py` `_exec_llm`)
_LLM_DEFAULT_MAX_TOKENS = 4096


def min_output_tokens_errors(node: dict[str, Any]) -> list[str]:
    """노드 하나의 `min_output_tokens` 설정 오류 목록(#428). 저장 헬퍼와 실행기가 같이 쓴다.

    값은 양의 정수 리터럴이고 노드의 `max_tokens`(없으면 기본 4096) 이하여야 한다. 하한은 스트림
    경로만 지키므로 도구를 쓰는 llm 노드(`tools`)에는 둘 수 없다. 키가 없거나 null이면 미설정이다
    (지금 동작). 노드 타입은 보지 않는다 — 타입별 대상은 `find_min_output_tokens_violations`가
    정한다.
    """
    value = node.get("min_output_tokens")
    if value is None:
        return []
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return ["min_output_tokens는 양의 정수여야 합니다"]
    tools = node.get("tools")
    if isinstance(tools, list) and tools:
        return ["min_output_tokens는 도구(tools)를 쓰지 않는 llm 노드에만 둘 수 있습니다"]
    max_tokens = node.get("max_tokens", _LLM_DEFAULT_MAX_TOKENS)
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
        return ["min_output_tokens를 쓰려면 max_tokens가 정수여야 합니다"]
    if value > max_tokens:
        return [f"min_output_tokens는 max_tokens({max_tokens}) 이하여야 합니다"]
    return []


def find_min_output_tokens_violations(nodes: list[dict[str, Any]]) -> list[str]:
    """`min_output_tokens`가 잘못된 노드를 `"<node_id>: <사유>"` 정렬 목록으로 낸다(#428).

    `nodes`는 `kind_to_nodes` 결과다. 이 키를 읽는 노드(llm·json_validate)만 본다. 실행기도 모델
    호출 전에 같은 검사(`min_output_tokens_errors`)로 실패한다(검증기 전에 저장된 정의). FastAPI에
    의존하지 않으므로 HTTPException은 호출자가 낸다.
    """
    found: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") not in _MIN_OUTPUT_TOKENS_NODE_TYPES:
            continue
        found.update(f"{node.get('id')}: {e}" for e in min_output_tokens_errors(node))
    return sorted(found)


# 워크플로우 정의(사용자가 저장하는 JSON)에 둘 수 없는 노드 키(#91). `_resume`은 워커가 소비한
# 승인의 checkpoint를 react 노드에 싣는 내부 키다. 다른 `_` 접두 키는 막지 않는다 —
# `_skill_preactivate`는 의도적으로 허용한 내부 키이고, 에디터가 저장하는 `_` 키가 있을 수 있다.
_RESERVED_NODE_KEYS = frozenset({"_resume"})


def find_reserved_node_keys(nodes: list[dict[str, Any]]) -> list[str]:
    """예약 키(`_resume`)를 가진 노드를 `"<node_id>: <key>"` 정렬 목록으로 낸다(#91).

    `nodes`는 `kind_to_nodes` 결과다(tool·plugin kind의 `**http` 전개로 들어온 키도 잡힌다).
    위반이 없으면 빈 목록이다. dict가 아닌 노드는 무시한다. 표시용 문자열이므로 파싱하지 않는다.
    FastAPI에 의존하지 않으므로 HTTPException은 호출자가 낸다.
    """
    found: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for key in _RESERVED_NODE_KEYS & node.keys():
            found.add(f"{node.get('id')}: {key}")
    return sorted(found)


__all__ = [
    "DIRECT_SIDE_EFFECT_NODE_TYPES",
    "agent_audience_error",
    "agent_egress_policy",
    "assert_agent_execution_allowed",
    "assert_can_run_skill_wrapper",
    "assert_mcp_tool_approved",
    "assert_mcp_tool_node_allowed",
    "chat_output_reference_error",
    "direct_node_needs_gate",
    "direct_node_requires_approval",
    "find_direct_node_approval_violations",
    "find_http_egress_violations",
    "find_min_output_tokens_violations",
    "find_reasoning_effort_violations",
    "url_target_is_literal",
    "find_forbidden_mcp_tool_nodes",
    "find_reserved_node_keys",
    "min_output_tokens_errors",
    "reasoning_effort_errors",
    "save_sources_config_errors",
    "validate_chat_output_reference",
]
