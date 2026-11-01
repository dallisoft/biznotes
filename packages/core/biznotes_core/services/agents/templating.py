"""에이전트 정의 내 템플릿 치환 — {params.x}, {node_id.key} 점 표기 경로.

`{경로|untrusted}` 필터(#228)는 LLM 프롬프트 렌더(`render_prompt`)에서만 값을
`untrusted_data` 블록으로 감싼다. 그 밖의 렌더(`render`·`render_value` — HTTP·MCP 인자·
검색어·하위 실행 params 등)는 필터를 인식하되 원값을 넣는다. 다른 `{x|y}`는 치환하지 않는다.
코어가 만드는 템플릿(스킬 스캐폴드 등)에는 필터를 달지 않는다 — 기존 프롬프트를 바꾸지 않는다.
"""

import json
import re
from typing import Any

from biznotes_core.services.agents.react.skill_guard import escape_skill_tags
from biznotes_core.services.untrusted import (
    UNTRUSTED_TAG,
    escape_untrusted_tags,
    is_untrusted_block_sequence,
    wrap_untrusted,
)

_VAR_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)(\|untrusted)?\}")
# 작성자가 템플릿에 손으로 쓴 블록 — 그 안의 값은 이미 블록 안이라 감싸지 않고 이스케이프만 한다
_AUTHOR_OPEN_RE = re.compile(rf'<{UNTRUSTED_TAG} kind="(?:source|web|tool)">')
_AUTHOR_CLOSE = f"</{UNTRUSTED_TAG}>"
# 사용자·호출자가 넣는 예약 키 — 채팅 요청처럼 지시 쪽 값일 수 있어 통째로 감싸지 않는다(#228 재리뷰
# m1). 웹훅·이벤트 params처럼 비신뢰 값이면 작성자가 필터를 단다
_INSTRUCTION_ROOTS = frozenset({"params", "chat_message", "chat_history", "user"})


# 구조화 값 참조(#361) — `json_compose` 템플릿과 정의 `output`이 쓴다. 점 표기에 목록 첨자(`[0]`)와
# 펼침(`[*]`)을 더한다. 문자열 전체가 참조 하나일 때만 값 그대로 넣는다
_JSON_REF_RE = re.compile(
    r"\{\s*([A-Za-z_]\w*(?:\[(?:\*|\d+)\])*(?:\.[A-Za-z_]\w*(?:\[(?:\*|\d+)\])*)*)\s*\}"
)
_JSON_STEP_RE = re.compile(r"\.?([A-Za-z_]\w*)|\[(\*|\d+)\]")


def whole_value_ref(value: Any) -> str | None:
    """문자열 전체가 값 참조 하나(`{a.b[*].c}`, 앞뒤 공백 허용)면 그 경로, 아니면 None."""
    if not isinstance(value, str):
        return None
    m = _JSON_REF_RE.fullmatch(value.strip())
    return m.group(1) if m else None


def resolve_value_path(state: dict[str, Any], path: str) -> Any:
    """`a.b[0].c`·`a.items[*].output` 경로의 값. 없으면 None이다.

    `[*]`는 목록의 원소마다 나머지 경로를 풀어 목록을 만든다(원소에 없으면 그 자리는 None).
    """
    steps = [(m.group(1), m.group(2)) for m in _JSON_STEP_RE.finditer(path)]

    def walk(cur: Any, i: int) -> Any:
        if i == len(steps):
            return cur
        key, index = steps[i]
        if key is not None:
            return walk(cur[key], i + 1) if isinstance(cur, dict) and key in cur else None
        if not isinstance(cur, list):
            return None
        if index == "*":
            return [walk(x, i + 1) for x in cur]
        n = int(index)
        return walk(cur[n], i + 1) if n < len(cur) else None

    return walk(state, 0)


def resolve_path(state: dict[str, Any], path: str) -> Any:
    cur: Any = state
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def template_paths(template: str) -> list[str]:
    """템플릿이 참조하는 점 표기 경로들(등장 순서). `render`와 같은 문법으로 찾는다.

    `{경로|untrusted}`도 찾고, 필터를 뺀 경로를 돌려준다.
    """
    return [m.group(1) for m in _VAR_RE.finditer(template)]


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def render(template: str, state: dict[str, Any]) -> str:
    return _VAR_RE.sub(lambda m: _text(resolve_path(state, m.group(1))), template)


def render_prompt(template: str, state: dict[str, Any]) -> str:
    """LLM 메시지가 될 템플릿 렌더 — 비신뢰 원시 출력과 지시를 가른다(#228, HWP-15).

    값마다 먼저 스킬 지침 태그를 이스케이프한다(도구 결과·대화 이력과 같다). 그다음:

    - `{경로|untrusted}`: 값을 `kind="tool"` 블록으로 감싼다(본문의 태그는 이스케이프된다). 빈 값은
      빈 문자열이다. 이미 블록인 값도 다시 감싼다 — 값이 `kind`·인용 머리를 고르지 못하게 한다.
    - 필터 없는 `{경로}`:
      - 생산자 블록으로만 된 값(`{search.context}`·`{load.context}`)은 그대로 둔다.
      - 블록 태그(정규형·변형, JSON 안 포함)를 하나라도 품은 값은 통째로 감싼다 — 짝 없는 태그가
        작성자의 지시를 블록 안으로 삼키거나 작성자의 블록을 닫지 못하고, 하위 실행 결과·
        supervisor `{results}`가 품은 비신뢰 데이터도 블록 밖으로 나오지 않는다.
      - 다만 사용자·호출자 입력 예약 키(`params`·`chat_message`·`chat_history`·`user`)는 지시일 수
        있어 감싸지 않고 태그만 이스케이프한다.
      - 태그가 없는 값은 지금처럼 그대로 넣는다.
    - 작성자가 템플릿에 손으로 쓴 블록 안의 값은 필터와 상관없이 태그만 이스케이프한다 — 이미
      블록 안이고, 감싸면 중첩된 닫는 태그가 작성자 블록의 끝처럼 읽힐 수 있다.
    """

    spans = _author_block_spans(template)

    def _sub(m: re.Match[str]) -> str:
        text = escape_skill_tags(_text(resolve_path(state, m.group(1))))
        if not text:
            return text
        if any(start <= m.start() < end for start, end in spans):
            # 작성자 블록 안 — 감싸면 안쪽 닫는 태그가 작성자 블록을 일찍 닫은 것처럼 보인다
            return escape_untrusted_tags(text)
        if m.group(2):
            return wrap_untrusted(text, "tool")
        if is_untrusted_block_sequence(text) or escape_untrusted_tags(text) == text:
            return text
        if m.group(1).split(".", 1)[0] in _INSTRUCTION_ROOTS:
            return escape_untrusted_tags(text)
        return wrap_untrusted(text, "tool")

    return _VAR_RE.sub(_sub, template)


def _author_block_spans(template: str) -> list[tuple[int, int]]:
    """템플릿 원문에서 작성자가 쓴 정규형 블록의 본문 구간들. 닫는 태그가 없으면 끝까지다."""
    spans: list[tuple[int, int]] = []
    pos = 0
    while (opened := _AUTHOR_OPEN_RE.search(template, pos)) is not None:
        close = template.find(_AUTHOR_CLOSE, opened.end())
        end = len(template) if close < 0 else close
        spans.append((opened.end(), end))
        pos = end + len(_AUTHOR_CLOSE)
    return spans


def render_value(value: Any, state: dict[str, Any]) -> Any:
    """dict/list 내부의 문자열 템플릿을 재귀 치환."""
    if isinstance(value, str):
        return render(value, state)
    if isinstance(value, dict):
        return {k: render_value(v, state) for k, v in value.items()}
    if isinstance(value, list):
        return [render_value(v, state) for v in value]
    return value
