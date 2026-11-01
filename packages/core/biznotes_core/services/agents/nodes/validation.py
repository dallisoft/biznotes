"""json_validate/citation_verify 노드 실행기(R2-5 기계적 추출, 동작 변경 없음).

runtime.py는 이 모듈의 `_exec_json_validate`/`_exec_citation_verify` 등을 재수출한다
— NODE_EXECUTORS 등록·기존 공개 임포트 계약(R0-6) 하위 호환 유지.

산출물 스키마 검증기(ctx 무의존 순수 함수)는 validators.py(R2-2)에서 임포트한다.
"""

import json
import re
import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError, LLMOutputTruncatedError
from biznotes_core.services.agents.nodes.llm import (
    _LLM_SETTING_CHECKS,
    LLM_SETTINGS_KEY,
    OUTPUT_BUDGET_KEY,
    _exec_llm,
    _node_min_output_tokens,
    _node_reasoning_effort,
)
from biznotes_core.services.agents.output_truncation import truncation_of
from biznotes_core.services.agents.policy import min_output_tokens_errors
from biznotes_core.services.agents.templating import render, resolve_path, template_paths
from biznotes_core.services.agents.validators import (
    _JSON_VALIDATE_DICT_SCHEMAS,
    _JSON_VALIDATE_SCHEMAS,
    _MINDMAP_NODE_CAP,
    _count_mindmap_nodes,
    _prune_slide_deck_images,
    _resolve_mindmap_refs,
    _truncate_mindmap_bfs,
)
from biznotes_core.services.gateway.image import IMAGE_PROMPT_MAX_CHARS
from biznotes_core.services.untrusted import _LESS_THAN

logger = structlog.get_logger()


def _source_truncation(source_expr: str, state: dict) -> dict[str, Any] | None:
    """source가 참조한 노드(뿌리) 출력의 잘림 표시(#422) — 처음 찾은 것."""
    for path in template_paths(source_expr):
        info = truncation_of(state.get(path.split(".", 1)[0]))
        if info is not None:
            return info
    return None


def _inherited_llm_settings(node: dict, source_expr: str, state: dict) -> dict[str, Any]:
    """json_validate 재시도가 물려받을 노드 재정의 LLM 설정(#429 D4).

    source가 참조한 노드(뿌리) 출력의 `llm_settings` 중 처음 찾은 것에서, json_validate 정의에
    **키가 없는** 설정만 물려받는다 — 자기 키가 먼저이고, 명시적 null도 자기 값(물려받지 않음 —
    게이트웨이가 모델 기본값을 쓰고, 그것도 없으면 보내지 않는다)이다.
    물려받을 수 없는 값은 버린다(`_LLM_SETTING_CHECKS`). 실행 입력(`params` 등 예약 키)은 노드
    출력이 아니라 보지 않는다.
    """
    from biznotes_core.services.agents.graph import _STATE_RESERVED_KEYS

    for path in template_paths(source_expr):
        root = path.split(".", 1)[0]
        output = None if root in _STATE_RESERVED_KEYS else state.get(root)
        settings = output.get(LLM_SETTINGS_KEY) if isinstance(output, dict) else None
        if isinstance(settings, dict):
            return {
                key: value
                for key, value in settings.items()
                if key in _LLM_SETTING_CHECKS
                and key not in node
                and _LLM_SETTING_CHECKS[key](value)
            }
    return {}


def _raise_truncated(info: dict[str, Any] | None) -> None:
    """원 출력이 출력 상한에서 잘렸으면 고정 사유로 끝낸다(#422) — 같은 상한으로 다시 불러도 또
    잘린다. 모르는 토큰 수는 문구에서 뺀다."""
    if info is None:
        return
    limit = info.get("max_tokens")
    head = f"출력이 상한(max_tokens={limit})에서" if isinstance(limit, int) else "출력이 상한에서"
    detail = ""
    used = info.get("completion_tokens")
    if isinstance(used, int):
        detail = f"출력 {used:,}토큰"
        reasoning = info.get("reasoning_tokens")
        if isinstance(reasoning, int) and reasoning > 0:
            detail += f", 그중 사고 {reasoning:,}토큰"
        detail = f"({detail})"
    raise LLMOutputTruncatedError(
        f"json_validate: {head} 잘렸습니다{detail}. 상한을 올리거나 입력을 줄이세요."
    )


async def _exec_json_validate(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
    """LLM 텍스트 출력을 JSON으로 파싱·스키마 검증. 실패 시 동일 프롬프트로 1회만 재시도.

    node: {source(필수, 검증할 템플릿 표현식, 예: "{map.text}"), schema(필수, 등록된
    스키마 이름), system?, prompt?(재시도용 — 원 LLM 노드와 동일 내용), model_id?}
    재시도까지 실패하면 AgentRuntimeError로 run을 종결한다(빈 결과를 조용히 반환하지 않음,
    phase-21-design.md §1.3·CR-13).
    source가 참조한 노드 출력이 출력 상한에서 잘렸으면(`output_truncated`, #422) 재시도하지 않고
    `LLMOutputTruncatedError`로 끝낸다. 재시도 출력이 잘려도 같다. 재시도는 이 노드의 `max_tokens`를
    쓴다(원 노드의 상한이 아니다).
    사고 양(`reasoning_effort`, #429)은 이 노드의 키를 먼저 쓰고(null이면 모델 기본값, 그것도
    없으면 보내지 않는다), 키가 없으면 원 llm 노드가 정한 값(출력의 `llm_settings`)을 물려받는다.
    허용 밖 값은 모델 호출 전에 정의 오류다.
    마인드맵 한정: 통과 후 트리 노드 수 상한(D-11, 기본 200)을 넘으면 BFS로 절단하고
    truncated=true를 표시한다.
    slide_deck 한정: 통과 후 선택 필드 `images`의 잘못된 항목을 버리고(재시도·실패 없음)
    버린 수를 반환의 `images_dropped`에 싣는다(#345).
    출력 하한(`min_output_tokens`, #428)은 사고 양과 같은 규칙으로 물려받고, 재시도는 llm 노드와
    같이 후보마다 출력 상한을 정한다. 물려받은 하한이 이 노드의 `max_tokens`보다 크면 물려받지
    않는다. 재시도 결과에 후보별 출력 예산(`output_budget`)이 있으면 이 노드 결과에도 싣는다.
    """
    schema_name = node.get("schema")
    validator = _JSON_VALIDATE_SCHEMAS.get(str(schema_name))
    if validator is None:
        raise AgentRuntimeError(f"json_validate: 알 수 없는 schema: {schema_name}")

    source_expr = node.get("source")
    if not source_expr:
        raise AgentRuntimeError("json_validate 노드에 source가 없습니다")
    _node_reasoning_effort(node)
    _node_min_output_tokens(node)

    def _parse_and_check(raw_text: str) -> tuple[dict[str, Any] | None, str | None]:
        try:
            parsed = json.loads(raw_text)
        except (ValueError, TypeError) as e:
            # JSONDecodeError는 ValueError다. 4300자리를 넘는 정수 리터럴은 ValueError만 낸다 —
            # 원문 오류로 노드가 끝나지 않고 재시도 경로를 탄다(#302 재리뷰 N6)
            return None, f"JSON 파싱 실패: {e}"
        err = validator(parsed)
        if err:
            return None, f"스키마 검증 실패: {err}"
        return parsed, None

    raw = render(str(source_expr), state)
    parsed, error = _parse_and_check(raw)
    # 재시도 호출의 후보별 출력 예산(#428) — 하한이 있는 재시도만 있다. 실행 로그는 이 노드의
    # 최종 결과만 남기므로 옮겨 싣는다
    budget: dict[str, Any] = {}
    if parsed is None:
        _raise_truncated(_source_truncation(str(source_expr), state))

    if parsed is None and node.get("prompt"):
        # 1회 재시도 — 실패 사유를 원 프롬프트에 덧붙여 동일 LLM 설정으로 재호출.
        logger.warning("json_validate 검증 실패, 1회 재시도", schema=schema_name, error=error)
        retry_node = dict(node)
        retry_node.update(_inherited_llm_settings(node, str(source_expr), state))
        if floor_errors := min_output_tokens_errors(retry_node):
            # 자기 값은 위에서 검사했다 — 남은 오류는 물려받은 하한이 이 노드의 상한(`max_tokens`,
            # D4)보다 큰 경우다. 물려받지 않는다
            logger.warning(
                "json_validate 재시도가 원 노드의 출력 하한을 물려받지 않음",
                min_output_tokens=retry_node.pop("min_output_tokens"),
                reason=floor_errors[0],
            )
        # 사유는 LLM 출력(중복 id·speaker 등)을 담을 수 있고 템플릿 원문에 붙는다(#302).
        # 진단 문구라 꺾쇠를 모두 무력화해 지침·비신뢰·스캐폴드 블록 태그를 위조하지 못하게 하고,
        # 프롬프트 렌더에서 `{params.x}` 같은 값이 state로 치환되지 않게 템플릿 문법도 막는다
        reason = str(error).replace("{", "(").replace("}", ")")
        for less_than in _LESS_THAN:  # 태그 탐지기가 태그 시작으로 보는 꺾쇠 변형 모두
            reason = reason.replace(less_than, "&lt;")
        retry_node["prompt"] = (
            str(node["prompt"])
            + f"\n\n(이전 시도 실패 — 반드시 유효한 JSON만 출력하세요. 실패 사유: {reason})"
        )
        retry_result = await _exec_llm(ctx, retry_node, state)
        parsed, error = _parse_and_check(str(retry_result.get("text", "")))
        if parsed is None:
            _raise_truncated(truncation_of(retry_result))
        if OUTPUT_BUDGET_KEY in retry_result:
            budget = {OUTPUT_BUDGET_KEY: retry_result[OUTPUT_BUDGET_KEY]}

    if parsed is None:
        raise AgentRuntimeError(f"json_validate: 스키마 검증 최종 실패({schema_name}): {error}")

    truncated = False
    refs: dict[str, int] = {}
    if schema_name in _JSON_VALIDATE_DICT_SCHEMAS:
        if schema_name == "mindmap":
            root = parsed["root"]
            if _count_mindmap_nodes(root) > _MINDMAP_NODE_CAP:
                root, truncated = _truncate_mindmap_bfs(root, _MINDMAP_NODE_CAP)
                parsed["root"] = root
            # 근거는 절단 뒤에 run 근거표로 해석·검증한다(#303). 건수를 truncated 옆에 남긴다
            resolved, dropped = _resolve_mindmap_refs(root, ctx.tool_evidence)
            refs = {"refs_resolved": resolved, "refs_dropped": dropped}
            parsed.update(refs)
            if dropped:
                logger.info(
                    "json_validate mindmap refs dropped",
                    run_id=str(ctx.run_id),
                    resolved=resolved,
                    dropped=dropped,
                )
        parsed["truncated"] = truncated
    elif schema_name == "slide_deck":
        dropped_reasons = _prune_slide_deck_images(parsed, IMAGE_PROMPT_MAX_CHARS)
        refs = {"images_dropped": len(dropped_reasons)}
        if dropped_reasons:
            logger.info(
                "json_validate slide_deck images dropped",
                run_id=str(ctx.run_id),
                dropped=len(dropped_reasons),
                reasons=dropped_reasons[:5],
            )

    return {
        "json": parsed,
        "text": json.dumps(parsed, ensure_ascii=False),
        "truncated": truncated,
        **refs,
        **budget,
    }


def _normalize_question_text(text: str) -> str:
    """중복 카드/문항 판정용 정규화 — 공백/구두점 제거, 소문자화(phase-21-design.md §2.4)."""
    import re as _re

    return _re.sub(r"[\s\W_]+", "", text).lower()


async def _verify_chunk_membership(
    db: AsyncSession, notebook_id: uuid.UUID, chunk_id: str, source_id: str
) -> bool:
    """citation의 chunk_id(청크 행 id = `SourceChunk.id`)가 실제로 이
    notebook/source 소속인지 조인 검증(D-10, phase-21-design.md §2.3 — IDOR 방지).
    """
    from sqlalchemy import select

    from biznotes_core.models import Source, SourceChunk

    try:
        chunk_uuid = uuid.UUID(chunk_id)
        source_uuid = uuid.UUID(source_id)
    except ValueError:
        return False

    row = await db.execute(
        select(SourceChunk.id)
        .join(Source, Source.id == SourceChunk.source_id)
        .where(
            SourceChunk.id == chunk_uuid,
            SourceChunk.source_id == source_uuid,
            Source.notebook_id == notebook_id,
        )
    )
    return row.scalar_one_or_none() is not None


async def _exec_citation_verify(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
    """flashcards/quiz 항목의 citation을 검증하고 근거 없는/소속 불일치 항목을 제외한다.

    node: {source(필수, json_validate 출력의 배열 템플릿 표현식, 예: "{validate.json}")}

    검증 대상 콘텐츠: flashcards는 answer, quiz는 정답 선택지(choices[answer_index]) —
    citation.quote(청크 원문 발췌)와 실제로 겹치는지 확인해야 하므로, LLM이 채워 넣은
    quote 자신이 아니라 카드/문항이 만들어낸 "정답 콘텐츠"를 검증 대상으로 삼는다.

    안전한 어댑터(F4 위험 방어, phase-21-design.md §2.3):
    - 정답 콘텐츠를 "{content} [1]" 형태로 마커를 문장 **뒤**에 부착(verify_citations_sync의
      _extract_marker_sentences가 마커 직전 문장을 찾는 파싱 규칙에 맞춤).
    - verified를 검증 전 명시적으로 False로 초기화 — 기본값 True에 기대지 않는다.
    - 마커 매칭 실패(문장을 못 찾음)는 명시적으로 제외(continue로 인한 암묵적 통과 금지).
    - chunk_id(청크 행 id = SourceChunk.id) 소속을 notebook_id로 조인 검증
      (D-10) — 타 notebook의 chunk_id는 verified 여부와 무관하게 무조건 제외(IDOR 방지).
    - question 텍스트 정규화 dedupe(CR-13) — 완전 일치만 중복으로 간주.
    """
    from biznotes_core.services.rag_chat import Citation, verify_citations_sync

    source_expr = node.get("source")
    if not source_expr:
        raise AgentRuntimeError("citation_verify 노드에 source가 없습니다")

    # source는 "{node_id.json}" 형태의 단일 경로 표현식 — render()는 문자열만 반환하므로
    # 배열 그대로를 얻기 위해 resolve_path로 직접 상태를 참조한다.
    m = re.fullmatch(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}", str(source_expr))
    if not m:
        raise AgentRuntimeError("citation_verify: source는 {node_id.json} 형태여야 합니다")
    items = resolve_path(state, m.group(1))
    if not isinstance(items, list):
        raise AgentRuntimeError("citation_verify: source가 배열이 아닙니다")

    kept: list[dict[str, Any]] = []
    seen_questions: set[str] = set()
    dropped = 0

    for item in items:
        if not isinstance(item, dict):
            dropped += 1
            continue
        citation = item.get("citation")
        if not isinstance(citation, dict):
            dropped += 1
            continue
        chunk_id = str(citation.get("chunk_id", ""))
        source_id = str(citation.get("source_id", ""))
        quote = str(citation.get("quote", ""))
        source_title = str(citation.get("source_title", ""))
        if not (chunk_id and source_id and quote and source_title):
            dropped += 1
            continue

        # 소속 검증(D-10) — verified 여부와 무관하게 IDOR 방지 우선.
        if not await _verify_chunk_membership(ctx.db, ctx.notebook_id, chunk_id, source_id):
            dropped += 1
            continue

        # 검증 대상 콘텐츠 — flashcards는 answer, quiz는 정답 선택지(choices[answer_index]).
        # citation.quote 자신과 비교하면 항상 통과하는 자기참조가 되므로, LLM이 만든
        # "정답" 콘텐츠가 실제로 quote에 근거하는지를 봐야 한다.
        if "answer_index" in item:
            choices = item.get("choices")
            answer_index = item.get("answer_index")
            if not isinstance(choices, list) or not isinstance(answer_index, int):
                dropped += 1
                continue
            try:
                content = str(choices[answer_index])
            except IndexError:
                dropped += 1
                continue
        else:
            content = str(item.get("answer", ""))
        if not content.strip():
            dropped += 1
            continue

        # 마커는 문장 뒤에 부착 — _extract_marker_sentences가 마커 직전 문장을 찾는
        # 파싱 규칙에 맞춘다(F4 위험 방어).
        synthetic_answer = f"{content} [1]"
        c = Citation(
            n=1,
            source_id=source_id,
            chunk_id=chunk_id,
            quote=quote,
            source_title=source_title,
            verified=False,  # 기본값 True에 기대지 않고 명시적으로 초기화
        )
        result = verify_citations_sync([c], synthetic_answer)
        if not result[0].verified:
            dropped += 1
            continue

        question = str(item.get("question", ""))
        norm = _normalize_question_text(question)
        if norm and norm in seen_questions:
            dropped += 1
            continue
        if norm:
            seen_questions.add(norm)

        kept.append(item)

    return {
        "json": kept,
        "text": json.dumps(kept, ensure_ascii=False),
        "dropped": dropped,
        "kept_count": len(kept),
        "total_count": len(items),
    }
