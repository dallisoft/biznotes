"""LLM 출력이 출력 상한(`max_tokens`)에서 잘렸다는 표시(#422).

게이트웨이는 공급자의 종료 사유(`finish_reason`)를 그대로 싣고, 잘림은 LiteLLM이 정규화한
`length`만 본다(Anthropic `max_tokens`·Gemini `MAX_TOKENS`도 `length`로 온다). llm·react 노드
결과(곧 실행 로그 output_data)에 싣는 키:

- `finish_reason`: 본문을 만든 호출의 종료 사유. 값이 없거나 본문이 안내 문구면 싣지 않는다.
- `output_truncated`: 잘렸을 때만 `{max_tokens, completion_tokens, reasoning_tokens}`. 공급자가
  주지 않은 토큰 수는 뺀다. `truncated`는 json_validate가 마인드맵 절단 뜻으로 쓰므로 피한다.
"""

from typing import Any

import structlog

logger = structlog.get_logger()

#: 출력 상한에서 잘린 응답의 종료 사유(LiteLLM 정규화 값)
TRUNCATED_FINISH_REASON = "length"
#: 노드 결과의 잘림 표시 키
OUTPUT_TRUNCATED_KEY = "output_truncated"


def finish_fields(
    finish_reason: str | None, usage: dict[str, int], max_tokens: Any
) -> dict[str, Any]:
    """노드 결과에 더할 키 — 종료 사유가 있으면 `finish_reason`, 잘렸으면 `output_truncated`."""
    if finish_reason is None:
        return {}
    fields: dict[str, Any] = {"finish_reason": finish_reason}
    if finish_reason != TRUNCATED_FINISH_REASON:
        return fields
    info: dict[str, int] = {}
    if isinstance(max_tokens, int):
        info["max_tokens"] = max_tokens
    completion = usage.get("completion_tokens")
    if isinstance(completion, int):
        info["completion_tokens"] = completion
    reasoning = usage.get("reasoning_tokens")
    if isinstance(reasoning, int) and reasoning > 0:
        info["reasoning_tokens"] = reasoning
    fields[OUTPUT_TRUNCATED_KEY] = info
    return fields


def truncation_of(output: Any) -> dict[str, Any] | None:
    """노드 출력의 잘림 표시. 잘리지 않았거나 노드 출력이 아니면 None이다."""
    info = output.get(OUTPUT_TRUNCATED_KEY) if isinstance(output, dict) else None
    return info if isinstance(info, dict) else None


def warn_truncated(fields: dict[str, Any], **context: Any) -> None:
    """잘렸으면 경고 로그를 남긴다 — run은 계속된다(조용히 잘린 답을 내지 않게)."""
    info = truncation_of(fields)
    if info is not None:
        logger.warning("llm 출력이 출력 상한에서 잘림", **context, **info)
