"""이미지 프롬프트 출처 판정 (#338 D1).

이미지 API에는 system 메시지가 없어 비신뢰 데이터 규칙을 실을 곳이 없다. 그래서 `image_generate`
프롬프트는 출처가 안전한 값만 참조하게 한다. 「안전」은 작성자 상수이거나, 비신뢰 데이터 규칙을 받는
LLM 계열이 만든 값이다. 검색 citations·소스 전문·HTTP·MCP 출력은 태그 없는 원문을 담을 수 있어
렌더 뒤 블록 검사로는 걸러지지 않는다.

- 안전한 뿌리: `chat_message`·`chat_history`·`user`, 이 그래프의 params가 안전하면 `params`,
  `constant`·`llm`·`react_agent` 노드, `source`의 뿌리가 모두 안전한 `json_validate` 노드.
- params의 안전 여부(`RunContext.params_image_safe`): 최상위 run은 참(llm 노드의 지시 루트와 같은
  신뢰, #228). 하위 실행(loop·agent_call·sub_workflow)은 부모가 그 params를 만든 템플릿의 뿌리가
  모두 안전할 때만 참이다. 그 밖의 하위 실행(supervisor·error_handler·react 도구 호출)은 거짓이다.
- 키별 params(`RunContext.params_image_safe_keys`, #390): params 전체가 안전하지 않아도 부모가
  안전하다고 판정한 최상위 키(`params.<키>…`)는 안전하다. loop 공통 `params`가 키마다 넘긴다.
  뿌리 집합에는 `params.<키>`로 들어간다. `{params}` 전체 참조는 params 전체가 안전할 때만 안전하다.
- `|untrusted` 필터가 달린 참조는 안전하지 않다.
"""

from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from biznotes_core.services.agents.errors import AgentRuntimeError
from biznotes_core.services.agents.templating import _VAR_RE

_SAFE_NODE_TYPES = frozenset({"constant", "llm", "react_agent"})
_ALWAYS_SAFE_ROOTS = frozenset({"chat_message", "chat_history", "user"})


def _references(value: Any) -> Iterator[tuple[str, bool]]:
    """값(문자열·목록·dict 안의 템플릿)이 참조하는 (경로, `|untrusted` 여부)."""
    if isinstance(value, str):
        for m in _VAR_RE.finditer(value):
            yield m.group(1), bool(m.group(2))
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _references(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _references(item)


def _path_safe(path: str, safe_roots: frozenset[str]) -> bool:
    """참조 경로의 뿌리가 안전한가. `params.<키>…`는 그 키가 안전해도 참이다(#390)."""
    root, _, rest = path.partition(".")
    if root in safe_roots:
        return True
    return root == "params" and bool(rest) and f"params.{rest.split('.', 1)[0]}" in safe_roots


def is_image_safe(value: Any, safe_roots: frozenset[str]) -> bool:
    """값이 참조하는 뿌리가 모두 안전한가 — 참조가 없으면(작성자 상수) 참."""
    return all(
        not filtered and _path_safe(path, safe_roots) for path, filtered in _references(value)
    )


def loop_items_image_safe(
    items_expr: Any, safe_roots: frozenset[str], *, resolved_path: str | None
) -> bool:
    """`loop`의 `items` 출처 — 루프가 실제로 값을 꺼낸 방식을 따른다.

    - `resolved_path`: `{경로}` 꼴을 공백을 떼고 경로로 읽어 값을 얻었을 때의 그 경로
      (`{ search.citations }`도 같다). 그 뿌리가 안전해야 한다.
    - `None`: 경로로 얻지 못해 `str(items)` 전체를 렌더해 JSON으로 읽었다. 목록·dict 리터럴의
      **키**에 든 템플릿도 치환되므로 문자열 전체를 본다(`'{"heading": "x"}'` 같은 JSON 객체 포함).
    """
    if resolved_path is not None:
        return _path_safe(resolved_path, safe_roots)
    return is_image_safe(str(items_expr), safe_roots)


def image_safe_roots(
    node_map: Mapping[str, dict], *, params_safe: bool, params_safe_keys: Iterable[str] = ()
) -> frozenset[str]:
    """이 그래프에서 이미지 프롬프트가 참조할 수 있는 뿌리 집합(모듈 docstring).

    `params_safe`가 거짓이면 `params_safe_keys`의 키만 `params.<키>`로 넣는다(#390).
    """
    base = set(_ALWAYS_SAFE_ROOTS)
    if params_safe:
        base.add("params")
    else:
        base.update(f"params.{key}" for key in params_safe_keys)
    base_roots = frozenset(base)
    memo: dict[str, bool] = {}

    def safe(node_id: str, seen: frozenset[str]) -> bool:
        if node_id in memo:
            return memo[node_id]
        node = node_map.get(node_id)
        if node is None or node_id in seen:
            return False
        kind = node.get("type")
        if kind in _SAFE_NODE_TYPES:
            ok = True
        elif kind == "json_validate":
            refs = list(_references(node.get("source")))
            ok = bool(refs) and all(
                not filtered
                and (_path_safe(path, base_roots) or safe(path.split(".", 1)[0], seen | {node_id}))
                for path, filtered in refs
            )
        else:
            ok = False
        memo[node_id] = ok
        return ok

    # 점이 든 노드 id는 템플릿 뿌리가 될 수 없다 — 넣지 않아 `params.<키>` 항목과 섞이지 않게 한다
    return base_roots | {n for n in node_map if "." not in n and safe(n, frozenset())}


def check_image_prompt_sources(node: dict, safe_roots: frozenset[str]) -> None:
    """그래프 구성 시점 검사 — 안전하지 않은 뿌리를 참조하는 이미지 프롬프트를 거부한다."""
    for path, filtered in _references(str(node.get("prompt") or "")):
        if filtered:
            raise AgentRuntimeError(
                f"image_generate {node.get('id')}: 프롬프트에 |untrusted 값을 넣을 수 없습니다"
            )
        if not _path_safe(path, safe_roots):
            root = path.split(".", 1)[0]
            raise AgentRuntimeError(
                f"image_generate {node.get('id')}: 프롬프트는 {root}의 값을 직접 쓸 수 없습니다 — "
                "출처가 검색·소스·HTTP·MCP 등 비신뢰 원문일 수 있습니다. llm 노드로 그림 묘사를 "
                "만든 뒤 그 출력을 쓰세요"
            )


def loop_params_image_safe(
    common: Mapping[str, Any], item_var: str, items_safe: bool, safe_roots: frozenset[str]
) -> tuple[bool, frozenset[str]]:
    """loop 본문 params(항목 + 공통 `params`, #390)의 출처 — `(params 전체가 안전한가, 안전한 키)`.

    키마다 판정한다. 항목 키(`item_var`)는 `items`의 출처를, 공통 키는 그 값(렌더 전 템플릿)이
    참조하는 뿌리를 따른다. 항목이 이기므로 겹친 키는 항목 출처다. 모든 키가 안전하면 `(True,
    빈 집합)`이라 공통 params가 없을 때와 같다.
    """
    safe_keys = {item_var} if items_safe else set()
    safe_keys.update(
        key for key, value in common.items() if key != item_var and is_image_safe(value, safe_roots)
    )
    if safe_keys >= {item_var, *common}:
        return True, frozenset()
    return False, frozenset(safe_keys)
