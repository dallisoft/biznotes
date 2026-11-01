"""LLM 노드 실행기 및 내장 도구 디스패치(R2-5 기계적 추출, 동작 변경 없음).

runtime.py는 이 모듈의 `_exec_llm`/`_dispatch_builtin_tool`/`_dispatch_register_source`
및 관련 상수(`_BUILTIN_TOOL_SPECS`/`_REGISTER_SOURCE_RUN_LIMIT`)를 재수출한다 —
NODE_EXECUTORS 등록·기존 공개 임포트 계약(R0-6) 하위 호환 유지.

tool-calling 루프(`_exec_llm_with_tools`)는 react/loop.py(R2-4)에서 임포트한다.
"""

import json
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import HTTPException

from biznotes_core.config import get_settings
from biznotes_core.core.ssrf import EgressBlockedError, SSRFBlockedError, ssrf_safe_request
from biznotes_core.models.llm import REASONING_EFFORTS
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError, ToolUnavailableError
from biznotes_core.services.agents.output_truncation import finish_fields, warn_truncated
from biznotes_core.services.agents.react.loop import _exec_llm_with_tools
from biznotes_core.services.agents.templating import render_prompt, template_paths
from biznotes_core.services.gateway.cost import compute_cost
from biznotes_core.services.untrusted import (
    UNTRUSTED_DATA_RULE,
    clean_label,
    format_source_excerpts,
    has_untrusted_block,
    wrap_untrusted,
)

if TYPE_CHECKING:
    from biznotes_core.services.agents.react.loop import _ReactGuards
    from biznotes_core.services.gateway.llm import CallPlan, ResolvedModel
    from biznotes_core.services.rag_chat import Citation, RetrievalResult

logger = structlog.get_logger()

# web_search 결과 중 모델에게 보여 주는 개수와 요약 길이
_WEB_RESULTS_SHOWN = 5
_WEB_SNIPPET_CHARS = 200


# LLM 네이티브 tool-calling — 기존 자산(rag_search/web_search/http/mcp)을 도구로 재노출.
# 모델이 도구를 요청하면 런타임이 실행하고 결과를 주입해 재호출한다 (Phase C).
# (_MAX_TOOL_ITERS는 _exec_llm_with_tools와 함께 react/loop.py로 이동, R2-4.)

_BUILTIN_TOOL_SPECS: dict[str, dict[str, Any]] = {
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
    "mcp_call": {
        "type": "function",
        "function": {
            "name": "mcp_call",
            "description": "등록된 MCP 서버의 도구를 호출한다.",
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
}


# rag_search 도구 `source_ids`(#447)가 이 실행에서 검색할 수 있는 소스와 겹치지 않을 때 — 범위 밖·
# 다른 노트북·없는 id를 가르지 않는다(존재 여부를 흘리지 않는다)
RAG_SEARCH_NO_SCOPE_MESSAGE = (
    "요청한 source_ids 중 이 실행에서 검색할 수 있는 소스가 없어 검색하지 않았습니다 — "
    "source_list로 소스 ID를 확인하세요."
)


async def _tool_source_scope(
    ctx: RunContext, tool: str, args: dict[str, Any]
) -> list[uuid.UUID] | None:
    """도구 인자 `source_ids`(#447)로 좁힌 소스 범위 — rag_search 노드 규칙(#362)을 그대로 쓴다.

    인자를 생략하면 run 범위(`ctx.source_ids`, None이면 노트북 전체)다. 주면 run 범위 ∩ 이 노트북
    소속만 남는다 — 범위를 넓히지 않는다. 템플릿 상태는 비운다(모델 인자로 run 상태를 읽지 않는다).
    """
    from biznotes_core.services.agents.nodes.retrieval import _rag_search_scope

    if args.get("source_ids") is None:
        return ctx.source_ids
    return await _rag_search_scope(ctx, {"id": tool, "source_ids": args["source_ids"]}, {})


# rag_search 도구 결과의 발췌 길이 — 커버리지 옵션(#446)을 주면 발췌 전체가 ranked 최대 결과
# (발췌 30개 × 1000자)를 넘지 않게 발췌마다 같은 길이로 줄인다. 근거표에는 원문이 남는다.
_RAG_TOOL_EXCERPT_CHARS = 1000
_RAG_TOOL_COVERAGE_CHARS = 30 * _RAG_TOOL_EXCERPT_CHARS


def _rag_tool_coverage_options(args: dict[str, Any]) -> dict[str, Any]:
    """rag_search 도구 인자의 커버리지 옵션(#446) — 생략한 키는 넘기지 않는다(지금과 같은 호출)."""
    from biznotes_core.services.rag_chat import coverage_from, per_source_limit_from

    options: dict[str, Any] = {}
    if args.get("coverage") is not None:
        options["coverage"] = coverage_from(args["coverage"])
    if args.get("per_source_limit") is not None:
        options["per_source_limit"] = per_source_limit_from(args["per_source_limit"])
    return options


def _render_rag_coverage(shown: "list[Citation]", result: "RetrievalResult") -> str:
    """커버리지 옵션을 준 rag_search 도구 결과(#446) — 소스 단위 블록 + 반영 소스 수 한 줄."""
    from biznotes_core.services.agents.nodes.retrieval import (
        _join_source_context,
        _source_excerpt_blocks,
    )

    per_excerpt = max(1, min(_RAG_TOOL_EXCERPT_CHARS, _RAG_TOOL_COVERAGE_CHARS // len(shown)))
    parts, _ = _source_excerpt_blocks(shown, per_excerpt)
    text = _join_source_context(parts, [])
    if result.coverage is not None:
        text += (
            f"\n\n[범위 소스 {result.coverage.scope_count}개 중 "
            f"{len(result.coverage.hit_sources)}개에서 발췌를 찾았습니다]"
        )
    return text


async def _dispatch_builtin_tool(ctx: RunContext, name: str, args: dict[str, Any]) -> str:
    """tool-calling 루프가 요청한 내장 도구 1건 실행 → 결과 문자열."""
    if name == "rag_search":
        from biznotes_core.services.agents.nodes.retrieval import _record_run_citations
        from biznotes_core.services.rag_chat import resolve_top_k, retrieve_context

        if ctx.source_ids is not None and not ctx.source_ids:
            return "선택된 소스가 없어 검색하지 않았습니다."
        scope = await _tool_source_scope(ctx, name, args)
        if scope is not None and not scope:
            return RAG_SEARCH_NO_SCOPE_MESSAGE
        query = str(args.get("query", "")) or json.dumps(ctx.params, ensure_ascii=False)
        # 인자가 없으면 어드민 RAG 설정을 따르고 1~30으로 제한한다 — 강제 호출
        # (required_initial_tools)은 query만 넘기므로 이 값이 곧 그 호출의 발췌 수다(#24).
        top_k = await resolve_top_k(ctx.db, args.get("top_k"))
        options = _rag_tool_coverage_options(args)
        result = await retrieve_context(
            ctx.db,
            ctx.notebook_id,
            query,
            source_ids=scope,
            top_k=top_k,
            **options,
        )
        shown = _record_run_citations(ctx, result.citations)
        if not shown:
            return "관련 발췌를 찾지 못했습니다."
        note = (
            "\n\n[주의: 검색 결과의 관련성이 낮습니다 — 근거가 불충분하면 모른다고 답하세요]"
            if result.insufficient_context
            else ""
        )
        # 발췌는 비신뢰 데이터 블록이다(HWP-05). 주의 문구는 코어가 쓴 문장이라 블록 밖에 둔다.
        if not options:
            return format_source_excerpts(shown, _RAG_TOOL_EXCERPT_CHARS) + note
        return _render_rag_coverage(shown, result) + note

    if name in ("artifact_list", "artifact_read"):
        from biznotes_core.services.agents.nodes.artifact_tools import dispatch_artifact_tool

        return await dispatch_artifact_tool(ctx, name, args)

    if name == "source_load":
        return await _dispatch_source_load(ctx, args)

    if name == "source_list":
        from biznotes_core.services.agents.nodes.source_list import dispatch_source_list

        return await dispatch_source_list(ctx, args)

    if name == "web_search":
        from biznotes_core.services.websearch import web_search

        # 호출 1건마다 web_search 항목(상태·결과 수)을, 모델에게 보여 준 결과마다 web
        # 항목을 남긴다(#12) — 앱이 출력 문자열을 비교하지 않고 0건·실패를 판정한다.
        # 두 항목은 await 없이 연달아 추가되므로, web 항목은 항상 자기 호출 항목 바로
        # 뒤에 붙는다(병렬 분기·서브에이전트가 끼어들 수 없다).
        try:
            results = await web_search(str(args.get("query", "")))
        except Exception:
            ctx.tool_evidence.append({"kind": "web_search", "status": "failed", "result_count": 0})
            raise
        shown_web = [
            {
                "url": str(r.get("url") or ""),
                "title": str(r.get("title") or ""),
                # 제공자 어댑터는 요약을 snippet 키로 준다(#11)
                "snippet": str(r.get("snippet") or "")[:_WEB_SNIPPET_CHARS],
            }
            for r in results[:_WEB_RESULTS_SHOWN]
        ]
        ctx.tool_evidence.append(
            {"kind": "web_search", "status": "succeeded", "result_count": len(shown_web)}
        )
        ctx.tool_evidence.extend({"kind": "web", **w, "status": "succeeded"} for w in shown_web)
        if not shown_web:
            return "웹 검색 결과가 없습니다."
        # 결과마다 비신뢰 데이터 블록으로 감싼다(HWP-05) — 제목·URL은 한 줄 라벨로 접는다.
        return "\n".join(
            wrap_untrusted(
                f"- {clean_label(w['title'])}: {clean_label(w['url'])}\n"
                f"  {clean_label(w['snippet'])}",
                "web",
            )
            for w in shown_web
        )

    if name == "http_request":
        from biznotes_core.services.agents.policy import agent_egress_policy

        url = str(args.get("url", ""))
        method = str(args.get("method", "GET")).upper()
        try:
            res = await ssrf_safe_request(
                method,
                url,
                json=args.get("body"),
                allow_private=get_settings().ssrf_allow_private,
                timeout=30,
                egress=agent_egress_policy(),
            )
        except EgressBlockedError as e:
            # 연결 정책 위반(#395)은 사유를 모델에 알린다 — 문구에 URL은 없다
            raise ToolUnavailableError(f"요청 차단됨: {e}") from e
        except SSRFBlockedError as e:
            raise ToolUnavailableError("요청 차단됨: 안전하지 않은 URL입니다.") from e
        return res.text[:8000]

    if name == "mcp_call":
        from biznotes_core.services.agents import mcp_exposure
        from biznotes_core.services.agents.mcp_client import call_tool, caller_meta

        # LLM이 서버·도구·인자를 고르므로 `mcp` 디스커버리와 같은 경계로 막는다(#77·#139 A) —
        # 관리자·시스템 실행은 켜진 서버, 일반 사용자 실행은 공개(`all_users`) 서버의 승인
        # read-only 도구만.
        # 일반 사용자에게는 없음·비활성·admin_only·미승인을 같은 메시지로 거부해 서버 존재가 새지
        # 않는다. `server_id` 형식 오류는 서버 존재와 무관하다.
        audience = mcp_exposure.mcp_audience(ctx.user_role, ctx.user_id)
        try:
            server_id = uuid.UUID(str(args.get("server_id")))
        except ValueError:
            return "MCP server_id가 올바르지 않습니다."
        tool_name = str(args.get("tool_name"))
        server, verdict = await mcp_exposure.check_mcp_call(
            ctx.db, server_id, tool_name, audience=audience
        )
        if server is None or verdict != "ok":
            raise ToolUnavailableError(mcp_exposure.mcp_call_denial_message(verdict, audience))
        arguments = args.get("arguments", {}) or {}
        if audience != "user":
            return await call_tool(server, tool_name, arguments, meta=caller_meta(ctx, server))
        call_id = await mcp_exposure.audit_user_mcp_call(ctx, server, tool_name, arguments)
        try:
            output = await call_tool(server, tool_name, arguments, meta=caller_meta(ctx, server))
        except Exception:
            await mcp_exposure.audit_user_mcp_result(ctx, server, tool_name, "failed", call_id)
            raise
        await mcp_exposure.audit_user_mcp_result(ctx, server, tool_name, "succeeded", call_id)
        return output

    if name == "register_source":
        return await _dispatch_register_source(ctx, args)

    raise ToolUnavailableError(f"알 수 없는 도구: {name}")


# source_load 도구 1회 결과 상한(#389) — 노드 설정 키(`source_load_max_sources`·
# `source_load_max_chars`)로 바꿀 수 있다. 미설정·비정상 값은 기본값, 하드 상한을 넘으면 상한이다.
# 기본값은 지금까지의 고정 클램프(소스 5개·60,000자)와 같다.
# 1회 상한이다 — 더 많은 소스는 `offset`·`source_ids`(#447)로 나눠 읽는다. legacy 스킬
# `input.mode: full`의 노드 상한(`skills/validator.py` `MAX_INPUT_SOURCES` 50개·`MAX_INPUT_CHARS`
# 200,000자)은 한 번에 다 읽는 다른 경로라 개수가 다르다. 길이 상한은 같다.
SOURCE_LOAD_TOOL_DEFAULTS: dict[str, int] = {"max_sources": 5, "max_chars": 60_000}
SOURCE_LOAD_TOOL_HARD_CAPS: dict[str, int] = {"max_sources": 20, "max_chars": 200_000}
# source_load 도구 설정 노드 키 → 상한 이름. 남은 토큰 예산 비율 키는 따로 읽는다
_SOURCE_LOAD_LIMIT_KEYS: dict[str, str] = {
    "source_load_max_sources": "max_sources",
    "source_load_max_chars": "max_chars",
}
# 1회 결과를 react 노드의 남은 토큰 예산 `(token_budget − 사용량) × 비율` 안으로 자른다(#389).
# 0·미설정이면 끈다(기본). 1보다 크면 1이다. llm 노드(예산 없음)에서는 쓰지 않는다
_SOURCE_LOAD_BUDGET_RATIO_KEY = "source_load_budget_ratio"
# 남은 예산에 맞춰 줄인 결과의 맨 앞 키 — 모델과 실행 로그가 잘림을 본다
_SOURCE_LOAD_BUDGET_FIT_KEY = "budget_fit"


def _clamp_source_load_limit(value: Any, key: str) -> int:
    """source_load 도구 상한 설정을 기본값/하드 상한으로 클램프(react 클램프와 같은 규칙)."""
    default = SOURCE_LOAD_TOOL_DEFAULTS[key]
    try:
        n = int(value) if value is not None else default
    except (TypeError, ValueError):
        n = default
    if n <= 0:
        n = default
    return min(n, SOURCE_LOAD_TOOL_HARD_CAPS[key])


def _source_load_budget_ratio(value: Any) -> float:
    """남은 토큰 예산 비율 — 0이면 끔. 비정상 값도 끈다(기본 동작 유지)."""
    try:
        ratio = float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
    if not ratio > 0:  # NaN도 끈다
        return 0.0
    return min(ratio, 1.0)


def _source_load_tool_spec(max_sources: int, max_chars: int) -> dict[str, Any]:
    """설정한 상한을 알리는 source_load 도구 스펙 — 공유 스펙(`_BUILTIN_SPECS`)은 고치지 않는다."""
    from biznotes_core.services.agents.tool_registry import _source_load_spec

    return _source_load_spec(max_sources, max_chars)


def _bind_source_load_tool(
    node: dict[str, Any],
    guards: "_ReactGuards | None",
    tools: list[dict[str, Any]],
    dispatchers: dict[str, Any],
) -> None:
    """노드 설정으로 source_load 도구의 1회 상한·남은 예산 맞춤을 건다(#389).

    설정 키가 하나도 없으면 아무것도 바꾸지 않는다 — 스펙·디스패처(`_dispatch_builtin_tool`
    monkeypatch seam 포함)가 지금과 같다. 설정이 있으면 이 노드의 source_load 디스패처를
    상한·비율을 넘기는 디스패처로 바꾸고, 상한이 기본값과 다르면 스펙의 안내도 바꾼다.
    """
    keys = (*_SOURCE_LOAD_LIMIT_KEYS, _SOURCE_LOAD_BUDGET_RATIO_KEY)
    if "source_load" not in dispatchers or all(node.get(k) is None for k in keys):
        return
    limits = {
        name: _clamp_source_load_limit(node.get(key), name)
        for key, name in _SOURCE_LOAD_LIMIT_KEYS.items()
    }
    ratio = _source_load_budget_ratio(node.get(_SOURCE_LOAD_BUDGET_RATIO_KEY)) if guards else 0.0
    if limits == SOURCE_LOAD_TOOL_DEFAULTS and not ratio:
        return  # 설정했지만 결과가 기본값과 같다(비율 0·비정상 값 등) — 지금 경로 그대로
    if limits != SOURCE_LOAD_TOOL_DEFAULTS:
        spec = _source_load_tool_spec(limits["max_sources"], limits["max_chars"])
        tools[:] = [
            spec if t.get("function", {}).get("name") == "source_load" else t for t in tools
        ]

    async def _dispatch(ctx: RunContext, name: str, args: dict[str, Any]) -> str:
        allowance: int | None = None
        if ratio and guards is not None:
            used = ctx.usage.get("total_tokens", 0) - guards.token_base
            allowance = max(0, int((guards.token_budget - used) * ratio))
        return await _dispatch_source_load(
            ctx,
            args,
            max_sources_cap=limits["max_sources"],
            max_chars_cap=limits["max_chars"],
            token_allowance=allowance,
        )

    dispatchers["source_load"] = _dispatch


async def _dispatch_source_load(
    ctx: RunContext,
    args: dict[str, Any],
    *,
    max_sources_cap: int = SOURCE_LOAD_TOOL_DEFAULTS["max_sources"],
    max_chars_cap: int = SOURCE_LOAD_TOOL_DEFAULTS["max_chars"],
    token_allowance: int | None = None,
) -> str:
    """source_load 도구 1건 — 모델 인자를 상한으로 자르고 원문을 읽는다.

    token_allowance가 있으면(#389 옵션) 결과를 그 토큰 수 안으로 소스 단위로 줄이고, 줄였으면
    결과 맨 앞 `budget_fit`과 실행 로그에 남긴다.
    """
    from biznotes_core.services.agents.nodes.retrieval import _exec_source_load

    max_sources = min(max(int(args.get("max_sources", max_sources_cap)), 1), max_sources_cap)
    max_chars = min(max(int(args.get("max_chars", max_chars_cap)), 1), max_chars_cap)
    # 새 인자(#447)는 줬을 때만 넘긴다 — 생략하면 호출이 지금과 같다
    extra: dict[str, Any] = {}
    if args.get("source_ids") is not None:
        extra["source_ids"] = await _tool_source_scope(ctx, "source_load", args)
    offset = 0
    if args.get("offset") is not None:
        try:
            offset = max(int(args["offset"]), 0)
        except (TypeError, ValueError):
            offset = 0
        extra["offset"] = offset
    loaded = await _exec_source_load(
        ctx, {"max_sources": max_sources, "max_chars": max_chars}, {}, **extra
    )
    loaded = _with_next_offset(loaded, offset, max_sources)
    if token_allowance is None:
        text = _render_source_load(loaded)
    else:
        text, loaded = _fit_source_load_to_tokens(ctx, loaded, token_allowance)
    ctx.tool_evidence.extend(
        {"kind": "source", "source_id": item["id"], "title": item["title"], "mode": "full"}
        for item in loaded["sources"]
    )
    return text


def _with_next_offset(loaded: dict[str, Any], offset: int, max_sources: int) -> dict[str, Any]:
    """개수 상한(`max_sources`)으로 남은 소스가 있으면 이어 읽을 위치를 붙인다(#447).

    남은 소스는 이번 창 뒤에서 `max_sources` 사유로 뺀 소스다. 창 안에서 길이 상한으로 빠진 소스는
    `dropped`에 남으니 `source_ids`로 다시 읽는다. 남은 소스가 없으면 결과는 지금과 같다.
    """
    remaining = sum(1 for d in loaded.get("dropped", []) if d.get("reason") == "max_sources")
    if not remaining:
        return loaded
    next_offset = offset + max_sources
    return {
        **loaded,
        "next_offset": next_offset,
        "remaining": remaining,
        "notice": (
            f"남은 소스 {remaining}개 — offset={next_offset}로 이어 읽거나, "
            "source_list로 고른 source_ids를 넘기세요."
        ),
    }


def _render_source_load(loaded: dict[str, Any], fit: dict[str, Any] | None = None) -> str:
    # `context` 밖의 제목도 외부가 정한 문자열이다(HWP-05) — 모델에 보내는 사본만 한 줄
    # 라벨로 접는다. evidence에는 원문을 남긴다.
    shown_load = {
        **loaded,
        "sources": [{**i, "title": clean_label(i["title"])} for i in loaded["sources"]],
        "dropped": [{**d, "title": clean_label(d["title"])} for d in loaded["dropped"]],
    }
    if fit is not None:
        # 잘린 사실은 결과 머리에 둔다 — 모델이 먼저 읽고, 실행 로그가 출력을 잘라도 남는다
        shown_load = {_SOURCE_LOAD_BUDGET_FIT_KEY: fit, **shown_load}
    return json.dumps(shown_load, ensure_ascii=False)


def _fit_source_load_to_tokens(
    ctx: RunContext, loaded: dict[str, Any], allowance: int
) -> tuple[str, dict[str, Any]]:
    """source_load 결과를 `allowance` 토큰 안으로 소스 단위로 줄인다(#389).

    `_ContextFit`(#357)과 같은 방법이다 — 토큰은 `estimate_prompt_tokens`(cl100k, 보수적)로 세고,
    원문 몫의 비율을 정해 `_refit_source_load`로 다시 담은 뒤 넘치면 더 줄인다. 원문을 모두 빼도
    넘치면 원문 없이 안내만 돌려준다. 반환: (모델에 보낼 결과, evidence를 남길 출력).
    """
    from biznotes_core.services.agents.nodes.retrieval import _refit_source_load, _split_source_load
    from biznotes_core.services.gateway.llm import estimate_prompt_tokens

    def count(text: str) -> int:
        return estimate_prompt_tokens([{"role": "tool", "content": text}])

    text = _render_source_load(loaded)
    full = count(text)
    if full <= allowance:
        return text, loaded
    fit = {
        "reason": "token_budget",
        "max_tokens": allowance,
        "note": (
            "이 노드의 남은 토큰 한도에 맞춰 원문을 줄였습니다 — 뺀·자른 소스는 dropped에 있습니다."
        ),
    }
    blocks = _split_source_load(loaded) or []
    total = sum(len(b) for _, b in blocks)

    def render(scale: float) -> tuple[str, dict[str, Any], list[dict[str, str]]]:
        refit, cut = _refit_source_load(loaded, blocks, int(total * scale), reason="token_budget")
        return _render_source_load(refit, fit), refit, cut

    bare_text, bare, bare_cut = render(0.0)
    fixed = count(bare_text)
    out_text, out, cut, tokens = bare_text, bare, bare_cut, fixed
    if fixed < allowance and blocks:
        scale = min(1.0, (allowance - fixed) / max(full - fixed, 1)) * 0.98
        for _ in range(_FIT_MAX_ROUNDS):
            out_text, out, cut = render(scale)
            tokens = count(out_text)
            if tokens <= allowance or scale == 0.0:
                break
            scale = max(0.0, scale * (allowance - fixed) / max(tokens - fixed, 1) * 0.95)
        if tokens > allowance:
            out_text, out, cut, tokens = bare_text, bare, bare_cut, fixed
    if tokens > allowance:
        # 원문을 모두 빼도 넘친다 — 소스 목록도 싣지 않는다
        out = {"context": "", "sources": [], "dropped": []}
        out_text = json.dumps({_SOURCE_LOAD_BUDGET_FIT_KEY: fit, **out}, ensure_ascii=False)
        tokens = count(out_text)
    logger.info(
        "source_load 도구 결과를 남은 토큰 한도에 맞춤",
        run_id=str(ctx.run_id),
        allowance=allowance,
        prompt_tokens=full,
        fitted_tokens=tokens,
        dropped=[(d["id"], d["reason"]) for d in cut],
    )
    return out_text, out


# run 단위 register_source 호출 상한(phase-22-design.md §2.3, D-15) — 노트북 전체
# 상한(`enqueue.notebook_source_limit`, 기본 1000, #445)과 별개의 run-scoped 카운터.
# 초과 시 도구 에러만 반환하고 run은 계속 진행한다(D-15 권고).
_REGISTER_SOURCE_RUN_LIMIT = 10


async def _dispatch_register_source(ctx: RunContext, args: dict[str, Any]) -> str:
    """Deep Research react_agent 전용 도구 — 웹 URL을 ctx.notebook_id에 소스로 등록.

    notebook_id는 인자로 받지 않고 항상 ctx.notebook_id(run 컨텍스트)만 사용한다
    (F11, IDOR 방지 — LLM이 임의 notebook_id를 지정해 다른 노트북에 등록하지 못하게).
    기존 /notebooks/{id}/sources가 쓰는 가드(소스 상한·SSRF·중복 조회)를
    재사용하되, run 단위 호출 카운터(D-15)는 이 도구 전용으로 신규 추가한다.
    """
    from sqlalchemy import select

    from biznotes_core.core.ssrf import SSRFBlockedError, validate_external_url
    from biznotes_core.models import Notebook, Source, User
    from biznotes_core.services.ingestion.enqueue import (
        ensure_source_capacity,
        ingest_uow,
        stage_ingest_job,
    )
    from biznotes_core.services.notebooks import get_permission

    if ctx.register_source_calls[0] >= _REGISTER_SOURCE_RUN_LIMIT:
        return (
            f"이번 실행에서 소스 등록 상한({_REGISTER_SOURCE_RUN_LIMIT}건)에 도달하여 "
            "추가 등록을 거부했습니다."
        )

    # [R22B-01] create_agent_run은 노트북 read 권한만 요구하므로(agents.py
    # _notebook_readable), read-only 공유 사용자도 이 워크플로우를 실행/승인해 도구를
    # 호출할 수 있다. register_source는 쓰기 동작이므로 여기서 별도로 write 권한을
    # 강제한다. ctx.user_id가 None(시스템 실행)이면 사용자 스코프 제약이 없어 통과.
    if ctx.user_id is not None:
        user = await ctx.db.get(User, ctx.user_id)
        notebook = await ctx.db.get(Notebook, ctx.notebook_id)
        perm = await get_permission(ctx.db, notebook, user) if user and notebook else None
        if perm not in ("owner", "write"):
            return "이 노트북에 소스를 등록할 쓰기 권한이 없습니다."

    url = str(args.get("url", "")).strip()
    if not url:
        return "url이 필요합니다."

    try:
        validate_external_url(url, allow_private=get_settings().ssrf_allow_private)
    except SSRFBlockedError as e:
        return f"요청 차단됨: {e}"

    try:
        async with ingest_uow(ctx.db) as uow:
            await ensure_source_capacity(ctx.db, ctx.notebook_id)

            existing = (
                await ctx.db.execute(
                    select(Source).where(
                        Source.notebook_id == ctx.notebook_id, Source.origin_url == url
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                ctx.register_source_calls[0] += 1
                return f"이미 등록된 소스입니다(source_id={existing.id})."

            title = str(args.get("title") or "") or "(제목 추출 중) " + url[:400]
            source = Source(
                notebook_id=ctx.notebook_id,
                type="web",
                title=title[:500],
                origin_url=url,
                status="pending",
            )
            ctx.db.add(source)
            await ctx.db.flush()
            stage_ingest_job(ctx.db, source, uow)
    except HTTPException as exc:
        if exc.status_code == 422:
            return str(exc.detail)
        raise

    ctx.register_source_calls[0] += 1
    return f"소스를 등록했습니다(source_id={source.id}, url={url})."


# 마스킹(_mask_value/_mask_tool_args/_SECRET_ARG_KEYS)은 logging.py로 이동(R2-3) —
# runtime.py 상단에서 재수출한다.


# ReAct 인루프 가드·예산·HITL 게이트·tool-calling 루프(_ReactGuards/_react_budget_exceeded/
# _aux_log_tool_call/_tool_loop_result/_react_approval_gate/_exec_llm_with_tools 및 관련
# 상수)는 react/loop.py로 이동(R2-4, 기계적 추출) — 여기서는 _exec_llm_with_tools만 임포트.


def _build_messages(
    system_text: str, prompt_text: str, state: dict, tools: Any
) -> list[dict[str, str]]:
    """llm 노드 메시지 조립 — 시스템(있으면)과 사용자 메시지."""
    messages: list[dict[str, str]] = []
    # 프롬프트 렌더 — `{x|untrusted}`는 블록으로 감싸고, 그 밖의 값의 위조 태그는
    # 이스케이프한다(#228)
    system = render_prompt(system_text, state)
    prompt = render_prompt(prompt_text, state)
    # 비신뢰 데이터 블록이 들어가면(템플릿의 `{search.context}` 등, 또는 도구 결과) 블록이
    # 데이터라는 규칙을 시스템 메시지 앞에 둔다(HWP-05). 그 밖의 노드 메시지는 그대로다.
    if has_untrusted_block(system) or has_untrusted_block(prompt) or (
        isinstance(tools, list) and tools
    ):
        system = f"{UNTRUSTED_DATA_RULE}\n\n{system}" if system else UNTRUSTED_DATA_RULE
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return messages


# 노드 재정의 LLM 설정(#429 D4) — llm 노드 결과의 이 키에 노드가 정한 값만 싣고(모델 기본값으로
# 해석한 값은 싣지 않는다), json_validate 재시도가 원 노드 출력에서 물려받는다. 키 → 물려받을 수
# 있는 값인지
LLM_SETTINGS_KEY = "llm_settings"
_LLM_SETTING_CHECKS: dict[str, Callable[[Any], bool]] = {
    "reasoning_effort": lambda value: isinstance(value, str) and value in REASONING_EFFORTS,
    # 출력 하한(#428) — 재시도 노드의 `max_tokens`와의 대조는 json_validate가 한다
    "min_output_tokens": lambda value: (
        isinstance(value, int) and not isinstance(value, bool) and value > 0
    ),
}


def _node_reasoning_effort(node: dict[str, Any]) -> str | None:
    """노드 정의의 사고 양(#429). 허용 밖 값은 모델 호출 전에 정의 오류다(저장 헬퍼와 같은 검사)."""
    from biznotes_core.services.agents.policy import reasoning_effort_errors

    errors = reasoning_effort_errors(node)
    if errors:
        raise AgentRuntimeError(f"노드 [{node.get('id')}] 설정 오류: {'; '.join(errors)}")
    value = node.get("reasoning_effort")
    return value if isinstance(value, str) else None


def _node_min_output_tokens(node: dict[str, Any]) -> int | None:
    """노드 정의의 출력 하한(#428). 잘못된 값은 모델 호출 전에 정의 오류다(저장 헬퍼와 같은
    검사)."""
    from biznotes_core.services.agents.policy import min_output_tokens_errors

    errors = min_output_tokens_errors(node)
    if errors:
        raise AgentRuntimeError(f"노드 [{node.get('id')}] 설정 오류: {'; '.join(errors)}")
    value = node.get("min_output_tokens")
    return value if isinstance(value, int) else None


def _llm_settings(node: dict[str, Any]) -> dict[str, Any]:
    """노드가 정한 LLM 설정(`llm_settings`, #429 D4) — 값이 있는 키만."""
    return {key: node[key] for key in _LLM_SETTING_CHECKS if node.get(key) is not None}


# 출력 하한이 있을 때 평가한 후보별 입력 예산·출력 상한(#428) — llm 노드와 json_validate(재시도)
# 결과의 키
OUTPUT_BUDGET_KEY = "output_budget"

# 모델 컨텍스트 맞춤(#357) — 출력(max_tokens) 말고도 남겨 두는 여유. 토큰 추정(cl100k)과 모델
# 토크나이저·채팅 템플릿의 차이를 덮는다
_FIT_RESERVE_MIN_TOKENS = 256
_FIT_RESERVE_RATIO = 0.05
# 줄인 길이를 다시 재 보는 최대 횟수
_FIT_MAX_ROUNDS = 8


def _fit_reserve(window: int) -> int:
    """출력 말고 남겨 두는 여유(#357) — `max(256, 컨텍스트의 5%)`."""
    return max(_FIT_RESERVE_MIN_TOKENS, int(window * _FIT_RESERVE_RATIO))


def _output_cap(max_tokens: int, candidate: "ResolvedModel") -> int:
    """이 후보에 줄 수 있는 출력 상한의 위 끝(#428) — 노드 `max_tokens`이고, 모델 최대 출력
    토큰(`max_output_tokens`, ③)이 있으면 그 값을 넘지 않는다. 게이트웨이와 같은 규칙이다."""
    from biznotes_core.services.gateway.llm import _model_max_tokens

    return int(_model_max_tokens(candidate, max_tokens))


class _ContextFit:
    """후보 모델의 `context_window`에 맞춰 source_load 원문을 소스 단위로 줄인다(#357).

    게이트웨이 폴백 체인이 후보마다 부른다(`fit_messages`). 예산은
    `context_window − 출력 상한(_output_cap) − 여유`이고, 프롬프트가 넘치면 템플릿이 참조한
    source_load 출력(`{노드.context}`)들을 같은 비율로 줄여 다시 렌더한다. `context_window`가
    없는 모델과 예산 안의 프롬프트는 원래 메시지를 그대로 보낸다 — 큰 모델로 폴백하면 원문을 다
    넣는다.

    출력 하한(`min_output_tokens`, #428)이 있으면 `plan`이 후보마다 호출 계획을 세운다(`plan_call`).
    """

    def __init__(
        self,
        system_text: str,
        prompt_text: str,
        state: dict,
        max_tokens: int,
        refs: dict[str, list[tuple[dict[str, str], str]]],
        base: list[dict[str, str]],
        min_output_tokens: int | None = None,
    ) -> None:
        self.system_text = system_text
        self.prompt_text = prompt_text
        self.state = state
        self.max_tokens = max_tokens
        self.refs = refs
        self.base = base  # 줄이지 않은 메시지
        self.min_output_tokens = min_output_tokens
        # 후보 모델 id → (보낸 메시지, 이번에 뺀 소스)
        self.fitted: dict[uuid.UUID, tuple[list[dict[str, str]], list[dict[str, str]]]] = {}
        # `plan`이 평가한 후보별 출력 예산(#428) — 결과 키 `output_budget`
        self.budgets: list[dict[str, Any]] = []

    @staticmethod
    def find_refs(
        templates: list[str], state: dict
    ) -> dict[str, list[tuple[dict[str, str], str]]]:
        """템플릿이 `{노드.context}`로 참조한 source_load 출력 — 노드 id → (소스, 블록 본문)."""
        from biznotes_core.services.agents.nodes.retrieval import _split_source_load

        refs: dict[str, list[tuple[dict[str, str], str]]] = {}
        for template in templates:
            for path in template_paths(template):
                root, _, rest = path.partition(".")
                if rest != "context" or root in refs:
                    continue
                blocks = _split_source_load(state.get(root))
                if blocks:
                    refs[root] = blocks
        return refs

    def _render(self, scale: float) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        from biznotes_core.services.agents.nodes.retrieval import _refit_source_load

        state = dict(self.state)
        cut: list[dict[str, str]] = []
        for node_id, blocks in self.refs.items():
            limit = int(sum(len(b) for _, b in blocks) * scale)
            state[node_id], dropped = _refit_source_load(state[node_id], blocks, limit)
            cut.extend({**d, "node": node_id} for d in dropped)
        return _build_messages(self.system_text, self.prompt_text, state, None), cut

    def __call__(self, candidate: "ResolvedModel") -> list[dict[str, str]]:
        from biznotes_core.services.gateway.llm import estimate_prompt_tokens

        window = candidate.model.context_window
        if not window or window <= 0:
            return self.base
        # 출력 몫은 이 후보에 실제로 보낼 상한이다 — 모델 최대 출력 토큰으로 줄면 입력을 더 남긴다
        budget = window - _output_cap(self.max_tokens, candidate) - _fit_reserve(window)
        full = estimate_prompt_tokens(self.base)
        if full <= budget:
            return self.base
        return self._shrink(candidate, window, budget, full)

    def plan(self, candidate: "ResolvedModel") -> "CallPlan":
        """출력 하한이 있을 때 이 후보의 호출 계획(#428) — 입력 몫을 먼저 지키고 남는 몫을 준다.

        입력 예산은 `context_window − min_output_tokens − 여유`이고, 그 안으로 원문을 줄인다. 출력
        상한은 `min(_output_cap, context_window − 맞춘 프롬프트 − 여유)`다. 줄일 원문이 없으면
        프롬프트는 그대로 두고 상한만 정한다. `context_window`를 모르면 입력을 맞추지 않고
        `_output_cap`을 쓴다. 상한이 하한보다 작으면 `OUTPUT_FLOOR_UNMET`으로 건너뛴다.
        """
        from biznotes_core.services.gateway.llm import (
            OUTPUT_FLOOR_UNMET,
            CallPlan,
            estimate_prompt_tokens,
        )

        floor = self.min_output_tokens or 0
        cap = _output_cap(self.max_tokens, candidate)
        window = candidate.model.context_window
        messages = self.base
        input_budget: int | None = None
        limit = cap
        if window and window > 0:
            reserve = _fit_reserve(window)
            input_budget = window - floor - reserve
            prompt = estimate_prompt_tokens(self.base)
            if prompt > input_budget and self.refs:
                messages = self._shrink(candidate, window, input_budget, prompt)
                prompt = estimate_prompt_tokens(messages)
            limit = max(0, min(cap, window - prompt - reserve))
        skipped = limit < floor
        budget = {
            "model_id": str(candidate.model.id),
            "display_name": candidate.model.display_name,
            "context_window": window,
            "input_budget": input_budget,
            "max_tokens": limit,
            "skipped": skipped,
        }
        self.budgets.append(budget)
        if limit < self.max_tokens:
            logger.info("llm 출력 상한을 모델 컨텍스트에 맞춤", min_output_tokens=floor, **budget)
        return CallPlan(
            messages=messages,
            max_tokens=limit,
            skip_code=OUTPUT_FLOOR_UNMET if skipped else None,
            budget=budget,
        )

    def _shrink(
        self, candidate: "ResolvedModel", window: int, budget: int, full: int
    ) -> list[dict[str, str]]:
        """원문을 줄여 프롬프트를 `budget` 토큰 안으로 넣는다. `full`은 줄이기 전 토큰 수다."""
        from biznotes_core.services.gateway.llm import estimate_prompt_tokens

        # 원문을 모두 뺀 프롬프트의 크기 — 나머지가 원문 몫이다. 처음 비율은 그 몫으로 정하고,
        # 넘치면 넘친 만큼 더 줄인다
        bare, _ = self._render(0.0)
        fixed = estimate_prompt_tokens(bare)
        scale = max(0.0, (budget - fixed) / max(full - fixed, 1)) * 0.98
        messages, cut = self._render(scale)
        for _ in range(_FIT_MAX_ROUNDS):
            tokens = estimate_prompt_tokens(messages)
            if tokens <= budget or scale == 0.0:
                break
            scale = max(0.0, scale * (budget - fixed) / max(tokens - fixed, 1) * 0.95)
            messages, cut = self._render(scale)
        logger.info(
            "llm 입력을 모델 컨텍스트에 맞춤",
            model=candidate.model.display_name,
            context_window=window,
            budget=budget,
            prompt_tokens=full,
            dropped=len(cut),
        )
        self.fitted[candidate.model.id] = (messages, cut)
        return messages

    def result_for(
        self, candidate: "ResolvedModel | None"
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """그 후보에 보낸 메시지와 뺀 소스. 맞추지 않았으면 원래 메시지와 []."""
        if candidate is None:
            return self.base, []
        return self.fitted.get(candidate.model.id, (self.base, []))


async def _exec_llm(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
    from biznotes_core.services.gateway.llm import StreamResult, complete_stream_with_fallback

    effort = _node_reasoning_effort(node)
    floor = _node_min_output_tokens(node)
    # prompt_ref 지정 시 프롬프트 템플릿의 활성 버전을 가져와 prompt/system을 대체 (Phase F).
    prompt_text = node.get("prompt", "")
    system_text = node.get("system", "")
    raw_ref = node.get("prompt_ref")
    if raw_ref:
        from biznotes_core.services.prompts import resolve_active_prompt

        try:
            ref_id = uuid.UUID(str(raw_ref))
        except ValueError as e:
            raise AgentRuntimeError(f"prompt_ref가 올바르지 않습니다: {raw_ref}") from e
        resolved = await resolve_active_prompt(ctx.db, ref_id)
        if resolved is None:
            raise AgentRuntimeError(f"prompt_ref 템플릿/활성 버전을 찾을 수 없습니다: {raw_ref}")
        prompt_text, ref_system = resolved
        if ref_system:
            system_text = ref_system

    tools = node.get("tools")
    messages = _build_messages(system_text, prompt_text, state, tools)

    # 1차 모델: 노드 model_id → run 모델(#359) → 채팅이 고른 모델 → 기본 모델. 어느 쪽이든 실패하면
    # 폴백 체인을 탄다.
    model_id: uuid.UUID | None = None
    raw_model_id = node.get("model_id") or ctx.run_model_id or ctx.chat_model_id
    if raw_model_id:
        try:
            model_id = uuid.UUID(str(raw_model_id))
        except ValueError as e:
            raise AgentRuntimeError(f"llm model_id가 올바르지 않습니다: {raw_model_id}") from e

    # 도구 사용 옵션 — 모델이 스스로 내장 도구를 호출하는 루프로 위임.
    if isinstance(tools, list) and tools:
        looped = await _exec_llm_with_tools(ctx, node, messages, model_id, [str(t) for t in tools])
        if settings := _llm_settings(node):
            looped[LLM_SETTINGS_KEY] = settings
        return looped

    result = StreamResult()
    parts: list[str] = []
    max_tokens = node.get("max_tokens", 4096)
    # source_load 원문을 참조하면 후보 모델의 컨텍스트에 맞춰 줄인다(#357). 출력 하한이 있으면
    # 후보마다 입력 몫을 먼저 지키고 남는 몫을 출력 상한으로 준다(#428, 참조가 없어도). 둘 다
    # 없으면 게이트웨이 호출은 지금과 같다
    refs = _ContextFit.find_refs([system_text, prompt_text], state)
    fit: _ContextFit | None = None
    extra: dict[str, Any] = {}
    if refs or floor is not None:
        fit = _ContextFit(system_text, prompt_text, state, int(max_tokens), refs, messages, floor)
        if floor is not None:
            extra["plan_call"] = fit.plan
        else:
            extra["fit_messages"] = fit
    if effort is not None:
        extra["reasoning_effort"] = effort  # 없으면 넘기지 않는다 — 지금 호출과 같다
    gen = await complete_stream_with_fallback(
        ctx.db, model_id, messages, result, max_tokens=max_tokens, **extra
    )
    async for delta in gen:
        parts.append(delta)
    ctx.add_tokens(result.usage)  # 공유 usage + per-node node_tokens 양쪽 가산(R5-2)
    dropped: list[dict[str, str]] = []
    if fit is not None:
        messages, dropped = fit.result_for(result.model)
        if dropped:
            logger.info(
                "llm 노드 원문 일부를 컨텍스트 때문에 뺌",
                run_id=str(ctx.run_id),
                node_id=node.get("id"),
                dropped=[(d["node"], d["id"], d["reason"]) for d in dropped],
            )

    # 응답 모델의 cost_per_1k로 노드 비용 가산 (ExecutionLog 영속화는 make_node 델타).
    # add_cost로 run 전체 셀(cost_total)에도 반영 — llm 노드만으로 된 서브에이전트
    # 비용이 부모 react 예산 가드에 보이도록 한다(codex C5).
    if result.model is not None:
        ctx.add_cost(compute_cost(result.usage, result.model.model.cost_per_1k))

    out: dict[str, Any] = {"text": "".join(parts)}
    # 실제로 응답한 모델과 폴백 여부(#359) — 실행 로그(output_data)와 채팅 메타데이터의 정본이다
    if result.model is not None:
        out["model"] = {
            "id": str(result.model.model.id),
            "display_name": result.model.model.display_name,
        }
    out["fallback_used"] = result.fallback_used
    if dropped:
        # 응답한 모델에 맞추느라 뺀 소스(#357) — 실행 로그(output_data)에 남는다
        out["dropped"] = dropped
    if settings := _llm_settings(node):
        out[LLM_SETTINGS_KEY] = settings
    if floor is not None and fit is not None:
        # 평가한 후보별 입력 예산·출력 상한(#428) — 실행 로그(output_data)에 남는다
        out[OUTPUT_BUDGET_KEY] = fit.budgets
    # 종료 사유와 출력 상한 잘림(#422) — 실행 로그에서 「JSON이 깨졌다」의 원인이 상한인지 보인다.
    # 상한은 응답한 후보에 실제로 보낸 값이다(#428)
    sent_max = result.max_tokens_used if result.max_tokens_used is not None else max_tokens
    finish = finish_fields(result.finish_reason, result.usage, sent_max)
    warn_truncated(finish, run_id=str(ctx.run_id), node_id=node.get("id"))
    out.update(finish)
    return out
