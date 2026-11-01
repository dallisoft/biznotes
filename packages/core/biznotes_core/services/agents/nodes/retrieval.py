"""조건/루프/RAG 검색/소스 로드 노드 실행기(R2-5 기계적 추출, 동작 변경 없음).

runtime.py는 이 모듈의 `evaluate_condition`/`parse_and_evaluate`/`_exec_condition`/
`_exec_loop`/`_exec_rag_search`/`_exec_source_load`를 재수출한다 — NODE_EXECUTORS
등록·기존 공개 임포트 계약(R0-6) 하위 호환 유지.

`_exec_loop`는 서브에이전트 dispatch를 위해 nodes/composite.py의
`dispatch_agent`/`skill_agent_visible_to`를 최상단에서 임포트한다(composite.py는 이
모듈을 역참조하지 않으므로 순환 없음).
"""

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import structlog

from biznotes_core.config import get_settings
from biznotes_core.services.agents.context import RunContext, run_parallel
from biznotes_core.services.agents.errors import (
    AgentRuntimeError,
    ApprovalRequired,
    DirectNodeRejectedError,
    DirectNodeRequestChangedError,
    RunLeaseLostError,
    SubagentApprovalNotSupportedError,
    UntrustedResumeError,
)
from biznotes_core.services.agents.node_errors import describe_node_failure
from biznotes_core.services.agents.nodes.composite import (
    _reject_subagent_approval,
    dispatch_agent,
    resolve_subagent_skill_version,
    skill_agent_visible_to,
)
from biznotes_core.services.agents.prompt_provenance import (
    loop_items_image_safe,
    loop_params_image_safe,
)
from biznotes_core.services.agents.templating import render, render_value
from biznotes_core.services.untrusted import (
    BLOCK_SEPARATOR,
    DROPPED_SOURCES_NOTICE,
    clean_label,
    format_source_excerpts,
    neutralize_citation_heads,
    wrap_untrusted,
)
from biznotes_core.services.usage_guard import UsageLimitExceeded

if TYPE_CHECKING:
    from biznotes_core.services.rag_chat import Citation

logger = structlog.get_logger()

# rag_search 노드 `source_ids`(#362)로 받는 id 수 상한 — 스케줄 소스 핀 상한과 같다
_RAG_SEARCH_SOURCE_IDS_LIMIT = 500

# source_load 출력을 줄이는 방식(`fit`) — 앞에서부터 채우지 않고 모든 소스를 같은 비율로 자른다.
# source_load 노드 옵션(#447)이자 출력 표식이다(`source_digest` 출력도 같은 표식, #360)
SHRINK_EACH = "shrink_each"

# loop `on_item_error: "skip"`(#361)도 건너뛰지 않는 실패 — 항목이 아니라 run·정의의 문제다
# (사용량 한도는 남은 항목도 모두 걸린다)
_LOOP_UNSKIPPABLE: tuple[type[BaseException], ...] = (
    UsageLimitExceeded,
    ApprovalRequired,
    SubagentApprovalNotSupportedError,
    UntrustedResumeError,
    DirectNodeRejectedError,
    DirectNodeRequestChangedError,
    RunLeaseLostError,
)


def evaluate_condition(left_str: str, op: str, right_str: str) -> bool:
    """좌변, 우변 문자열을 연산자에 따라 비교 평가합니다."""

    def parse_val(s: str) -> Any:
        s = s.strip()
        # 쿼트 제거
        if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
            return s[1:-1]
        # 숫자 변환 시도
        try:
            if "." in s:
                return float(s)
            return int(s)
        except ValueError:
            pass
        # 불리언 변환
        if s.lower() == "true":
            return True
        if s.lower() == "false":
            return False
        if s.lower() in ("none", "null"):
            return None
        return s

    left = parse_val(left_str)
    right = parse_val(right_str)

    if op == "==":
        return left == right
    elif op == "!=":
        return left != right
    elif op == ">":
        try:
            return left > right
        except Exception:
            return False
    elif op == "<":
        try:
            return left < right
        except Exception:
            return False
    elif op == ">=":
        try:
            return left >= right
        except Exception:
            return False
    elif op == "<=":
        try:
            return left <= right
        except Exception:
            return False
    elif op in ("in", "contains"):
        try:
            return str(right) in str(left)
        except Exception:
            return False
    return False


def parse_and_evaluate(expression_str: str) -> bool:
    """단일 식 문자열을 파싱하여 참/거짓을 평가합니다."""
    operators = [">=", "<=", "==", "!=", ">", "<", " in "]
    for op in operators:
        if op in expression_str:
            parts = expression_str.split(op, 1)
            return evaluate_condition(parts[0], op.strip(), parts[1])

    val = expression_str.strip()
    if val.lower() in ("false", "0", "none", "null", ""):
        return False
    return True


async def _exec_condition(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
    """조건 노드 평가: 조건식의 참/거짓 여부를 평가하여 반환합니다."""
    left_expr = node.get("left")
    op = node.get("op")
    right_expr = node.get("right")

    if left_expr is not None and op is not None and right_expr is not None:
        left_val = render(str(left_expr), state)
        right_val = render(str(right_expr), state)
        result = evaluate_condition(left_val, op, right_val)
    else:
        expr = node.get("expression", "")
        rendered_expr = render(str(expr), state)
        result = parse_and_evaluate(rendered_expr)

    return {
        "result": result,
        "true_node": node.get("true_node"),
        "false_node": node.get("false_node"),
    }


def _loop_max_items(node: dict, state: dict) -> int | None:
    """loop `max_items`(#342) — 정수 또는 템플릿 문자열(렌더 후 10진 정수), 0 이상. 생략은 None.

    루프 안 `image_generate`처럼 항목당 비용이 드는 본문의 횟수를 LLM이 쓴 목록 길이가 정하지
    않게 묶는다. 잘못된 값은 본문을 부르기 전에 `AgentRuntimeError`로 실패한다.

    렌더 결과의 소수부가 0뿐이면(`"4.0"`) 정수로 읽는다(#346). params JSON Schema
    `{"type": "integer"}`는 수 `4.0`을 정수로 받는데, 렌더하면 `"4.0"`이 되기 때문이다.
    """
    raw = node.get("max_items")
    if raw is None:
        return None
    if isinstance(raw, int) and not isinstance(raw, bool):
        value = raw
    elif isinstance(raw, str):
        rendered = render(raw, state).strip()
        if not re.fullmatch(r"[0-9]+(?:\.0+)?", rendered):
            raise AgentRuntimeError(f"loop max_items가 0 이상의 정수가 아닙니다: {rendered!r}")
        value = int(rendered.partition(".")[0])
    else:
        raise AgentRuntimeError(f"loop max_items는 정수 또는 템플릿 문자열이어야 합니다: {raw!r}")
    if value < 0:
        raise AgentRuntimeError(f"loop max_items가 0 이상의 정수가 아닙니다: {value}")
    return value


async def _exec_loop(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
    """루프 노드: 컬렉션 데이터의 각 아이템을 반복 순회하며 지정된 하위 에이전트를 호출합니다.

    `max_items`가 있으면 앞에서부터 그 수만큼만 돌고 `truncated`·`total`(원래 항목 수)을 함께
    반환한다(#342). 생략하면 모든 항목을 돌고 `results`만 반환한다.

    본문 워크플로우가 `output`(값 참조)을 선언하면 항목마다 그 값을 자르지 않고
    `results[i].output`에 담는다(#361). 항목당 상한은 `agent_loop_item_output_max_bytes`이고
    넘으면 그 항목이 실패한다. 선언하지 않은 본문은 지금처럼 `summary`(500자)만 담는다.

    `on_item_error`(#361): `"fail"`(기본)은 항목 하나가 실패하면 노드가 실패한다. `"skip"`은
    실패한 항목을 결과에서 빼고 `failed: [{index, error_code, error}]`를 남긴다(오류 문구는 실행
    로그와 같은 고정 문구). 승인 관련 차단·lease 상실처럼 run 단위 실패는 건너뛰지 않는다. 순차
    실행은 항목들이 노드 세션을 나눠 쓰므로, 한 항목이 세션을 망가뜨리면(DB 오류) 뒤 항목도
    실패해 건너뛰어진다.

    `params`(#390, 선택 dict): 부모 상태로 한 번 렌더한 값(`render_value` — `{params.format}`
    등)을 본문 params에 `item_var`와 함께 넘긴다(순차·병렬 공통). 키가 겹치면 항목이 이긴다.
    생략·`null`·`{}`이면 지금처럼 항목 하나만 넘긴다. dict가 아니면 본문을 부르기 전에 실패한다.
    이미지 프롬프트 출처는 키마다 판정한다 — 공통 키는 그 템플릿이 참조하는 뿌리가 안전할 때만
    본문에서 안전하다(`loop_params_image_safe`).

    본문 에이전트 안에서 승인이 필요한 도구 호출이 나오면 `SubagentApprovalNotSupportedError`로
    실패한다(순차·병렬 공통). 서브에이전트 내부 HITL 승인은 지원하지 않는다.
    """
    from biznotes_core.models import Agent
    from biznotes_core.services.agents.templating import resolve_path

    raw_agent_id = node.get("loop_body_agent_id")
    if not raw_agent_id:
        raise AgentRuntimeError("loop 노드에 loop_body_agent_id가 없습니다")
    try:
        target_id = uuid.UUID(str(raw_agent_id))
    except ValueError as e:
        raise AgentRuntimeError(
            f"loop loop_body_agent_id가 올바르지 않습니다: {raw_agent_id}"
        ) from e

    target = await ctx.db.get(Agent, target_id)
    if target is None or not await skill_agent_visible_to(
        ctx.db, target, ctx.user_id, ctx.user_role
    ):
        raise AgentRuntimeError("루프 대상 에이전트를 찾을 수 없거나 호출 권한이 없습니다")
    # 스킬 wrapper는 루프 전에 실행 version을 한 번 정해 모든 항목이 같은 version을
    # 실행한다(D8, R4-03). wrapper가 가리키는 최신 freeze를 실행하지 않는다.
    version_id = None
    if getattr(target, "source_kind", None) == "skill_package":
        version_id = await resolve_subagent_skill_version(
            ctx.db, target, ctx.user_id, ctx.user_role
        )
    if target.kind not in ("workflow", "skill"):
        raise AgentRuntimeError(
            f"루프로 호출할 수 없는 kind: {target.kind} (workflow/skill만 가능)"
        )
    on_item_error = node.get("on_item_error", "fail")
    if on_item_error not in ("fail", "skip"):
        raise AgentRuntimeError(
            f"loop on_item_error는 fail 또는 skip이어야 합니다: {on_item_error!r}"
        )
    # 구조화 결과는 정의를 직접 실행하는 워크플로우 본문만 선언할 수 있다(스킬 패키지는 아니다)
    declares_output = (
        version_id is None
        and getattr(target, "skill_version_id", None) is None
        and target.kind == "workflow"
        and isinstance(target.definition, dict)
        and "output" in target.definition
    )
    output_limit = get_settings().agent_loop_item_output_max_bytes

    # items 꺼내기
    items_expr = node.get("items", "")
    items = None
    resolved_path: str | None = None
    if isinstance(items_expr, str) and items_expr.startswith("{") and items_expr.endswith("}"):
        path = items_expr[1:-1].strip()
        items = resolve_path(state, path)
        if items is not None:
            resolved_path = path

    if items is None:
        # fallback: render 후 json loads 시도
        rendered = render(str(items_expr), state)
        try:
            items = json.loads(rendered)
        except Exception:
            items = []

    if not isinstance(items, list):
        if items:
            items = [items]
        else:
            items = []

    max_items = _loop_max_items(node, state)
    total = len(items)
    if max_items is not None:
        items = items[:max_items]

    item_var = node.get("item_var", "item")
    parallel = node.get("parallel", False)

    # 공통 params(#390) — 덱 수준 값(형식·언어 등)을 항목마다 본문에 함께 넘긴다
    common_tpl = node.get("params")
    if common_tpl is None:
        common_tpl = {}
    if not isinstance(common_tpl, dict):
        raise AgentRuntimeError(f"loop params는 객체여야 합니다: {type(common_tpl).__name__}")
    common: dict[str, Any] = render_value(common_tpl, state)

    # 자식 params(항목·공통 키)의 출처 — 이미지 프롬프트가 쓸 수 있는지 키마다 자식에 넘긴다
    # (#338 D1, #390)
    items_safe = loop_items_image_safe(
        items_expr, ctx.image_safe_roots, resolved_path=resolved_path
    )
    params_safe, safe_keys = loop_params_image_safe(
        common_tpl, item_var, items_safe, ctx.image_safe_roots
    )

    async def _run_one(item_ctx: RunContext, item: Any) -> dict[str, Any]:
        sub_ctx = item_ctx.child(
            target_id,
            params={**common, item_var: item},  # 키가 겹치면 항목이 이긴다
            params_image_safe=params_safe,
            params_image_safe_keys=safe_keys,
        )
        summary = (
            await dispatch_agent(sub_ctx, target, version_id=version_id)
            if version_id is not None
            else await dispatch_agent(sub_ctx, target)
        )
        # 본문 안의 승인 요청은 부모로 전달되지 않는다. `_run_subagent`와 같은 가드로 명확히
        # 실패시킨다 — 거르지 않으면 승인 요청이 사라진 채 run이 정상 완료된다(#80).
        _reject_subagent_approval(sub_ctx, target_id)
        result: dict[str, Any] = {"item": item, "summary": summary}
        if declares_output:
            output = sub_ctx.structured_output
            size = len(json.dumps(output, ensure_ascii=False, default=str).encode("utf-8"))
            if size > output_limit:
                raise AgentRuntimeError(
                    f"loop 항목 출력이 상한을 넘었습니다({size} > {output_limit}바이트)"
                )
            result["output"] = output
        return result

    failed: list[dict[str, Any]] = []

    async def _run_item(item_ctx: RunContext, indexed: tuple[int, Any]) -> dict[str, Any] | None:
        index, item = indexed
        if on_item_error != "skip":
            return await _run_one(item_ctx, item)
        try:
            return await _run_one(item_ctx, item)
        except _LOOP_UNSKIPPABLE:
            raise
        except Exception as e:
            failure = describe_node_failure(e)
            logger.warning(
                "loop 항목 실패 — 건너뜀",
                run_id=str(ctx.run_id),
                index=index,
                error_code=failure.code,
                error=failure.raw if failure.raw is not None else failure.message,
            )
            failed.append({"index": index, "error_code": failure.code, "error": failure.message})
            return None

    indexed = list(enumerate(items))
    if parallel:
        # 항목마다 분기 세션을 쓴다(#28). 항목 ctx는 in_parallel_branch=True다(T5/F1).
        ran = await run_parallel(ctx, indexed, _run_item)
    else:
        ran = [await _run_item(ctx, pair) for pair in indexed]
    results = [r for r in ran if r is not None]

    out: dict[str, Any] = {"results": results}
    if on_item_error == "skip":
        out["failed"] = sorted(failed, key=lambda f: f["index"])
    if max_items is not None:
        out.update(truncated=total > max_items, total=total)
    return out


def _record_run_citations(ctx: RunContext, citations: "list[Citation]") -> "list[Citation]":
    """검색 발췌에 run 단위 인용 번호를 매기고, 새 발췌를 근거로 남긴다(#9).

    `retrieve_context`는 호출마다 [1]부터 번호를 매긴다. 그대로 보여 주면 한 run에서
    두 번 검색했을 때 서로 다른 청크가 같은 [1]이 된다. 이미 근거로 남긴 청크는 그
    번호를 다시 쓰고, 새 청크는 이어서 번호를 매겨 `ctx.tool_evidence`에 추가한다 —
    `citation.n`은 run 전체에서 유일하다.

    번호표는 `ctx.tool_evidence`에서 읽는다. react `rag_search` 도구와 워크플로우
    `rag_search` 노드(published 스킬이 쓰는 경로)가 같은 표를 쓰고, 서브에이전트는 같은
    셀을 공유하며, HITL 재개는 체크포인트가 evidence를 복원하므로 번호가 이어진다.
    병렬 분기(parallel loop·fan-out)도 같은 목록을 공유하고, 이 함수는 표를 읽고 덧붙이는 사이에
    await가 없어 분기끼리 끼어들지 못한다 — 번호는 병렬에서도 유일하다(#361, 테스트로 고정).

    반환: 보여 줄 발췌 — 같은 호출 안의 중복 청크는 한 번만.
    """
    numbered: dict[str, int] = {}
    for e in ctx.tool_evidence:
        cit = e.get("citation") if e.get("kind") == "source" else None
        if isinstance(cit, dict) and isinstance(cit.get("n"), int):
            numbered[str(cit.get("chunk_id"))] = cit["n"]
    next_n = max(numbered.values(), default=0) + 1

    shown: list[Citation] = []
    for c in citations:
        if any(s.chunk_id == c.chunk_id for s in shown):
            continue
        if c.chunk_id in numbered:
            c.n = numbered[c.chunk_id]
        else:
            c.n = numbered[c.chunk_id] = next_n
            next_n += 1
            ctx.tool_evidence.append(
                {
                    "kind": "source",
                    "source_id": str(c.source_id),
                    "title": c.source_title,
                    "citation": c.to_dict(),
                }
            )
        shown.append(c)
    return shown


def _requested_source_ids(node: dict, state: dict) -> list[uuid.UUID] | None:
    """rag_search 노드가 요청한 소스 범위(#362). None은 좁히지 않는다는 뜻이다.

    `source_ids`는 목록(원소마다 템플릿 가능) 또는 템플릿 문자열이다. `{경로}` 하나뿐인 문자열은
    그 값을 그대로 쓰고, 그 밖의 문자열은 렌더한다. 문자열 값은 JSON 배열이면 배열로, 아니면 id
    하나로 읽는다. 생략·`null`·값이 없는 경로(빈 렌더)는 None이다 — 지금과 같다. 형식이 틀린 id는
    빼고 로그에 수만 남긴다(LLM이 쓴 개요 값일 수 있다). 중복은 빼고 순서를 지킨다.
    """
    from biznotes_core.services.agents.templating import resolve_path

    raw = node.get("source_ids")
    value: Any = raw
    if isinstance(raw, str):
        whole = re.fullmatch(r"\{([\w.]+)\}", raw.strip())
        value = resolve_path(state, whole.group(1)) if whole is not None else render(raw, state)
    elif isinstance(raw, list):
        value = [render(v, state) if isinstance(v, str) else v for v in raw]
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("["):
            try:
                value = json.loads(value)
            except ValueError:
                pass
    if value is None or value == "":
        return None
    items = value if isinstance(value, list) else [value]

    ids: list[uuid.UUID] = []
    invalid = 0
    for item in items:
        try:
            ids.append(uuid.UUID(str(item).strip()))
        except ValueError:
            invalid += 1
    unique = list(dict.fromkeys(ids))
    over = max(len(unique) - _RAG_SEARCH_SOURCE_IDS_LIMIT, 0)
    if invalid or over:
        logger.warning(
            "rag_search source_ids 일부를 무시", node_id=node.get("id"), invalid=invalid, over=over
        )
    return unique[:_RAG_SEARCH_SOURCE_IDS_LIMIT]


async def _rag_search_scope(ctx: RunContext, node: dict, state: dict) -> list[uuid.UUID] | None:
    """rag_search가 검색할 소스 범위 — 노드 `source_ids` ∩ run 범위(#362). 범위를 넓히지 않는다.

    run 범위는 `ctx.source_ids`(없으면 노트북 전체)다. 노드가 범위를 정하지 않으면 run 범위를
    그대로 돌려준다. 정했으면 run 범위 안이면서 이 노트북 소속인 소스만 남긴다(다른 노트북의
    id는 존재 여부와 상관없이 빠진다). 교집합이 비면 `[]` — 검색하지 않는다(#13).
    """
    from sqlalchemy import select

    from biznotes_core.models import Source

    requested = _requested_source_ids(node, state)
    if requested is None:
        return ctx.source_ids
    if ctx.source_ids is not None:
        allowed = set(ctx.source_ids)
        requested = [s for s in requested if s in allowed]
    if not requested:
        return []
    rows = await ctx.db.execute(
        select(Source.id).where(Source.notebook_id == ctx.notebook_id, Source.id.in_(requested))
    )
    owned = set(rows.scalars().all())
    return [s for s in requested if s in owned]


async def _exec_rag_search(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
    from biznotes_core.services.rag_chat import retrieve_context

    query = render(node.get("query", ""), state) or json.dumps(ctx.params, ensure_ascii=False)
    options = _rag_coverage_options(node)
    # top_k 생략(None)은 그대로 넘긴다 — `retrieve_context`가 어드민 RAG 설정으로 해석한다
    # (#45). 여기서 미리 해석하면 검색을 건너뛰는 경우(source_ids=[])에도 설정을 읽는다.
    result = await retrieve_context(
        ctx.db,
        ctx.notebook_id,
        query,
        source_ids=await _rag_search_scope(ctx, node, state),
        top_k=node.get("top_k"),
        **options,
    )
    citations = _record_run_citations(ctx, result.citations)
    if not options:
        # 발췌는 비신뢰 데이터 블록이다(HWP-05) — 템플릿 `{search.context}`로 프롬프트에 들어간다.
        context = format_source_excerpts(citations, 1500)
        return {
            "context": context,
            "citations": [c.to_dict() for c in citations],
            "insufficient_context": result.insufficient_context,
        }
    # 커버리지 옵션(#446) — source_load와 같은 소스 단위 블록이라 llm 노드가 모델 예산에 맞출 때
    # (#357) `fit: "shrink_each"`로 소스를 빼지 않고 소스마다 같은 비율로 줄인다
    assert result.coverage is not None
    parts, sources = _source_excerpt_blocks(citations, 1500)
    return {
        "context": _join_source_context(parts, []),
        "citations": [c.to_dict() for c in citations],
        "insufficient_context": result.insufficient_context,
        "sources": sources,
        "dropped": [],
        "fit": "shrink_each",
        **result.coverage.to_dict(),
    }


def _rag_coverage_options(node: dict) -> dict[str, Any]:
    """rag_search 노드의 커버리지 옵션(#446) — 생략한 키는 넘기지 않는다(지금과 같은 호출)."""
    from biznotes_core.services.rag_chat import coverage_from, per_source_limit_from

    options: dict[str, Any] = {}
    try:
        if node.get("coverage") is not None:
            options["coverage"] = coverage_from(node["coverage"])
        if node.get("per_source_limit") is not None:
            options["per_source_limit"] = per_source_limit_from(node["per_source_limit"])
    except (TypeError, ValueError) as e:
        raise AgentRuntimeError(
            f"rag_search coverage·per_source_limit가 올바르지 않습니다: {e}"
        ) from e
    return options


def _source_excerpt_blocks(
    citations: "list[Citation]", max_chars: int
) -> tuple[list[str], list[dict[str, str]]]:
    """검색 발췌를 소스 단위 블록으로 묶는다(#446) — (감싼 블록들, 블록 순서의 소스).

    블록은 `## 제목` 아래에 그 소스의 발췌를 `[n]` 머리 줄과 함께 근거 순서로 담는다. 소스 순서는
    처음 나온 순서다. 발췌 본문은 `max_chars`로 자르고 줄 머리 `[n]`을 무력화한다(HWP-05).
    """
    by_source: dict[str, list[Citation]] = {}
    for c in citations:
        by_source.setdefault(str(c.source_id), []).append(c)
    parts: list[str] = []
    sources: list[dict[str, str]] = []
    for sid, group in by_source.items():
        title = group[0].source_title
        excerpts = "\n\n".join(
            f"[{c.n}]\n{neutralize_citation_heads(c.quote[:max_chars])}" for c in group
        )
        parts.append(wrap_untrusted(f"## {clean_label(title)}\n\n{excerpts}", "source"))
        sources.append({"id": sid, "title": title})
    return parts, sources


async def _exec_source_load(
    ctx: RunContext,
    node: dict,
    state: dict,
    *,
    source_ids: list[uuid.UUID] | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """full 모드 — 소스 원본 전문(파싱 마크다운)을 소스별 헤더로 연결해 주입.

    대상: ctx.source_ids(scope:selected) 또는 노트 전체(scope:all). []이면 읽지 않는다(#13).
    순서: `(created_at, id)` 오름차순 — `context` 블록·`sources`·`dropped`와 `max_sources`로
    남길 소스가 이 순서를 따른다. `created_at`이 같아도 행을 고쳐 순서가 바뀌지 않는다(#391).
    상한: max_sources(소스 수) / max_chars(전체 길이) — 초과 시 절단하고 드롭 사실을 남김.
    `fit: "shrink_each"`(#447)면 앞에서부터 채우지 않고 담은 소스 모두를 같은 비율로 자른다 —
    출력에도 `fit`을 남겨 모델 컨텍스트에 맞출 때(#357) 같은 규칙을 쓴다. 생략하면 지금 동작이다.
    원본 = Source.parsed_object_key(파싱 마크다운). 누락 시 SourceChunk(seq) 재조립 폴백.

    `source_ids`·`offset`은 source_load 도구(#447)만 넘긴다(노드 정의 키가 아니다).
    `source_ids`는 `_rag_search_scope`로 이미 run 범위 ∩ 노트북으로 좁힌 목록이고, None이면
    ctx.source_ids다. `offset`은 정렬한 대상에서 건너뛸 수다 — 건너뛴 소스는 `dropped`에 없다.
    """
    import anyio
    from sqlalchemy import select

    from biznotes_core.models import Source, SourceChunk
    from biznotes_core.services.storage import get_object

    max_sources = int(node.get("max_sources", 5))
    max_chars = int(node.get("max_chars", 60_000))
    scope = ctx.source_ids if source_ids is None else source_ids

    q = select(Source).where(Source.notebook_id == ctx.notebook_id, Source.status == "ready")
    if scope is not None:
        q = q.where(Source.id.in_(scope))
    q = q.order_by(Source.created_at, Source.id)
    sources = list((await ctx.db.execute(q)).scalars().all())[offset:]

    dropped: list[dict[str, str]] = []
    selected = sources[:max_sources]
    for extra in sources[max_sources:]:
        dropped.append({"id": str(extra.id), "title": extra.title, "reason": "max_sources"})

    async def _read_parsed(src: Source) -> str | None:
        if src.parsed_object_key:
            try:
                data = await anyio.to_thread.run_sync(
                    get_object, get_settings().storage_bucket_parsed, src.parsed_object_key
                )
                return data.decode("utf-8", errors="replace")
            except Exception:
                logger.warning("파싱 원본 로드 실패", source_id=str(src.id))
        # 폴백: 청크 seq 순 재조립(오버랩 중복 가능 — 파싱본 없을 때만)
        rows = await ctx.db.execute(
            select(SourceChunk.text)
            .where(SourceChunk.source_id == src.id)
            .order_by(SourceChunk.seq)
        )
        chunks = [t for (t,) in rows.all()]
        return "\n\n".join(chunks) if chunks else None

    if node.get("fit") == SHRINK_EACH:
        return await _load_shrink_each(ctx, selected, dropped, max_chars, _read_parsed)

    parts: list[str] = []
    loaded: list[dict[str, str]] = []
    total = 0
    for src in selected:
        body = await _read_parsed(src)
        if not body:
            dropped.append({"id": str(src.id), "title": src.title, "reason": "no_content"})
            continue
        remaining = max_chars - total
        if remaining <= 0:
            dropped.append({"id": str(src.id), "title": src.title, "reason": "max_chars"})
            continue
        # 본문 줄 머리의 `[n]`이 run 단위 인용 번호를 흉내 내지 못하게 한다(HWP-05)
        block = f"## {clean_label(src.title)}\n\n{neutralize_citation_heads(body)}".strip()
        if len(block) > remaining:
            block = block[:remaining]
            dropped.append({"id": str(src.id), "title": src.title, "reason": "max_chars_partial"})
        # 원문은 비신뢰 데이터 블록이다(HWP-05). 절단 뒤에 감싸 닫는 태그가 잘리지 않는다.
        parts.append(wrap_untrusted(block, "source"))
        total += len(block) + 2
        loaded.append({"id": str(src.id), "title": src.title})

    if dropped:
        logger.info(
            "source_load 절단", run_id=str(ctx.run_id), loaded=len(loaded), dropped=len(dropped)
        )
    return {"context": _join_source_context(parts, dropped), "sources": loaded, "dropped": dropped}


async def _load_shrink_each(
    ctx: RunContext,
    selected: list[Any],
    dropped: list[dict[str, str]],
    max_chars: int,
    read: Callable[[Any], Awaitable[str | None]],
) -> dict[str, Any]:
    """`fit: "shrink_each"` source_load(#447) — 담을 소스 모두를 같은 비율로 `max_chars`에 맞춘다.

    `_refit_source_load`의 shrink_each와 같은 규칙이다(블록 길이 합 기준, 구분 문자는 세지 않는다).
    원문이 없는 소스는 `no_content`로 뺀다. 자른 소스는 `max_chars_partial`로 남긴다.
    """
    blocks: list[tuple[Any, str]] = []
    for src in selected:
        body = await read(src)
        if not body:
            dropped.append({"id": str(src.id), "title": src.title, "reason": "no_content"})
            continue
        # 본문 줄 머리의 `[n]`이 run 단위 인용 번호를 흉내 내지 못하게 한다(HWP-05)
        blocks.append(
            (src, f"## {clean_label(src.title)}\n\n{neutralize_citation_heads(body)}".strip())
        )
    ratio = max(0.0, min(1.0, max_chars / max(sum(len(b) for _, b in blocks), 1)))
    parts: list[str] = []
    loaded: list[dict[str, str]] = []
    for src, block in blocks:
        keep = int(len(block) * ratio)
        if keep < len(block):
            block = block[:keep]
            dropped.append({"id": str(src.id), "title": src.title, "reason": "max_chars_partial"})
        # 원문은 비신뢰 데이터 블록이다(HWP-05). 절단 뒤에 감싸 닫는 태그가 잘리지 않는다.
        parts.append(wrap_untrusted(block, "source"))
        loaded.append({"id": str(src.id), "title": src.title})
    if dropped:
        logger.info(
            "source_load 절단",
            run_id=str(ctx.run_id),
            loaded=len(loaded),
            dropped=len(dropped),
            fit=SHRINK_EACH,
        )
    return {
        "context": _join_source_context(parts, dropped),
        "sources": loaded,
        "dropped": dropped,
        "fit": SHRINK_EACH,
    }


# source_load가 소스 블록 사이에 두는 구분
_SOURCE_JOIN = f"\n\n{BLOCK_SEPARATOR}\n\n"
_SOURCE_OPEN = '<untrusted_data kind="source">\n'
_SOURCE_CLOSE = "\n</untrusted_data>"


def _dropped_notice(dropped: list[dict[str, str]]) -> str:
    # 제목 목록은 외부가 정한 문자열이라 데이터 블록에 넣는다(HWP-05). 한 소스는 한 번만 적는다 —
    # 다시 맞출 때(#357) 이미 잘린 소스를 마저 빼면 사유가 둘이다
    titles: dict[str, str] = {}
    for d in dropped:
        titles.setdefault(str(d.get("id")), d["title"])
    names = ", ".join(clean_label(t) for t in titles.values())
    return f"\n\n{DROPPED_SOURCES_NOTICE}\n" + wrap_untrusted(names, "source")


def _join_source_context(parts: list[str], dropped: list[dict[str, str]]) -> str:
    """감싼 소스 블록들을 잇고, 뺀 소스가 있으면 생략 안내와 제목 목록 블록을 붙인다."""
    context = _SOURCE_JOIN.join(parts)
    return context + _dropped_notice(dropped) if dropped else context


def _split_source_load(output: Any) -> list[tuple[dict[str, str], str]] | None:
    """source_load 출력의 `context`를 (소스, 블록 본문) 목록으로 되돌린다(#357). 아니면 None.

    `_exec_source_load`가 만든 모양과 정확히 같을 때만 돌려준다 — 담은 소스(`sources`)마다
    정규형 블록 하나가 구분으로 이어지고, 뺀 소스가 있으면 끝에 생략 안내가 붙는다. 블록 본문은
    이미 이스케이프돼 있어 첫 닫는 태그가 그 블록의 끝이다. 다른 값(작성자가 만든 문자열, 다른
    노드 출력)은 None이라 손대지 않는다.

    `source_digest`의 배치 요약 출력(#448)은 블록이 소스가 아니라 배치 단위라 `blocks`(블록마다
    `{id, title, source_ids}`)를 `sources` 대신 블록 목록으로 쓴다.
    """
    if not isinstance(output, dict):
        return None
    context, sources, dropped = output.get("context"), output.get("sources"), output.get("dropped")
    if not (isinstance(context, str) and isinstance(sources, list) and isinstance(dropped, list)):
        return None
    if output.get("fit") == "shrink_each" and "blocks" in output:
        sources = output["blocks"]
        if not isinstance(sources, list):
            return None
    if not all(isinstance(x, dict) and isinstance(x.get("title"), str) for x in sources + dropped):
        return None
    if dropped:
        notice = _dropped_notice(dropped)
        if not context.endswith(notice):
            return None
        context = context[: -len(notice)]
    blocks: list[tuple[dict[str, str], str]] = []
    pos = 0
    for i, src in enumerate(sources):
        if i and not context.startswith(_SOURCE_JOIN, pos):
            return None
        pos += len(_SOURCE_JOIN) if i else 0
        if not context.startswith(_SOURCE_OPEN, pos):
            return None
        close = context.find(_SOURCE_CLOSE, pos + len(_SOURCE_OPEN))
        if close < 0:
            return None
        body = context[pos + len(_SOURCE_OPEN) : close]
        blocks.append(({"id": str(src.get("id")), "title": src["title"]}, body))
        pos = close + len(_SOURCE_CLOSE)
    return blocks if pos == len(context) else None


def _block_sources(output: dict[str, Any], block: dict[str, str]) -> list[dict[str, str]]:
    """블록에 든 소스(`{id, title}`) — 배치 요약(`blocks`, #448)이면 그 배치의 소스들, 아니면 블록
    자신이다."""
    batches = output.get("blocks")
    if not isinstance(batches, list):
        return [block]
    ids = next(
        (b.get("source_ids") or [] for b in batches if str(b.get("id")) == block["id"]), []
    )
    titles = {str(s.get("id")): s.get("title", "") for s in output["sources"]}
    return [{"id": str(i), "title": titles.get(str(i), "")} for i in ids]


def _refit_source_load(
    output: dict[str, Any],
    blocks: list[tuple[dict[str, str], str]],
    max_chars: int,
    *,
    reason: str = "context_window",
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """source_load 출력을 소스 단위로 `max_chars` 안에 다시 담는다(#357, 모델 컨텍스트 맞춤).

    `_exec_source_load`의 `max_chars`와 같은 규칙이다 — 앞 소스부터 담고, 남는 길이가 없으면 뒤
    소스를 빼고(`context_window`), 넘치는 마지막 소스는 잘라 담는다(`context_window_partial`).
    출력에 `fit: "shrink_each"`가 있으면(`source_digest` #360, source_load 노드 옵션 #447) 소스를
    빼지 않고 모든 소스를 같은 비율로 자른다. 블록 본문은 이미 이스케이프돼 있고, 자른 뒤 다시
    감싸므로 닫는 태그가 지켜진다.
    `reason`은 뺀·자른 소스에 남길 사유의 머리다(`{reason}`·`{reason}_partial`) — source_load
    도구를 남은 토큰 예산에 맞출 때(#389)는 `token_budget`이다.
    배치 요약 출력(`blocks`, #448)은 `sources`를 그대로 두고, 자른 배치에 든 소스를 소스마다 적는다.
    반환: (새 출력, 이번에 뺀·자른 소스).
    """
    parts: list[str] = []
    loaded: list[dict[str, str]] = []
    cut: list[dict[str, str]] = []
    total = 0
    if output.get("fit") == SHRINK_EACH:
        ratio = min(1.0, max_chars / max(sum(len(b) for _, b in blocks), 1))
        for src, block in blocks:
            keep = int(len(block) * ratio)
            if keep < len(block):
                block = block[:keep]
                partial = f"{reason}_partial"
                cut.extend({**s, "reason": partial} for s in _block_sources(output, src))
            parts.append(wrap_untrusted(block, "source"))
            loaded.append(src)
        blocks = []
        if "blocks" in output:
            loaded = output["sources"]
    for src, block in blocks:
        remaining = max_chars - total
        if remaining <= 0:
            cut.append({**src, "reason": reason})
            continue
        if len(block) > remaining:
            block = block[:remaining]
            cut.append({**src, "reason": f"{reason}_partial"})
        parts.append(wrap_untrusted(block, "source"))
        total += len(block) + 2
        loaded.append(src)
    dropped = [*output["dropped"], *cut]
    refit = {
        **output,
        "context": _join_source_context(parts, dropped),
        "sources": loaded,
        "dropped": dropped,
    }
    return refit, cut
