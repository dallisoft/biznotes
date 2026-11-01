"""외부 MCP 도구 스펙 정화 — description·input_schema가 LLM에 들어가는 경계(#229, HWP-16).

MCP 서버가 준 도구 설명·입력 스키마는 외부인이 쓴 글이고, 관리자가 승인한 뒤에도 서버가
언제든 바꿀 수 있다. tool spec에 넣기 전에 보이지 않는 글자(bidi·폭 없는 문자·태그 문자·
변형 선택자·제어 문자)를 지우고 위조 `untrusted_data` 태그를 이스케이프한다. 숨긴 글자는 길이
예산을 쓰지 않는다.

스키마는 두 부류로 나눈다.

- 주석 문자열(`description`·`title`·`$comment`·`markdownDescription`·`deprecationMessage`·
  `x-*`의 문자열 값, `enumDescriptions`·`markdownEnumDescriptions`의 문자열 항목): 설명과 같이
  정화하고 자른다.
- 구조 문자열(모든 키, 그 밖의 문자열 값 — `enum`·`required`·`const`·`pattern`·`default` 등):
  고쳐 쓰면 모델이 서버가 모르는 값을 보내므로 고치지 않는다. 지울 글자나 위조 태그가 있으면
  도구를 노출하지 않는다(`MCPToolSpecRejected`). `enum`·`const`·`default`·`examples` 아래는 데이터
  값이라 키 이름이 주석과 같아도 구조 문자열로 본다.

모양(dict가 아닌 스키마, JSON이 아닌 값, 유한하지 않은 수)·깊이·크기 상한을 벗어나도 노출하지
않는다. NaN·Infinity는 MCP SDK가 받아들이지만 LLM 요청을 만들 때 실패해 run 전체를 막는다.

LiteLLM이 프로바이더 요청을 만들며 스키마를 펼치는 비용도 미리 잰다(#294). 스펙은 모델을 정하기 전에
만들어지므로 경로를 가리지 않는다.
- `$ref` 전개: Gemini(Vertex)·Bedrock은 최상위 `$defs`를, Anthropic·Fireworks는 `definitions`·
  `components.schemas`를 상한 없이(또는 10 MB까지) 펼친다. 참조가 서로를 여러 번 가리키면 작은
  스키마도 결과가 기하급수로 커진다. 같은 절차를 정화 결과의 사본에 예산을 걸고 돌린다.
- 범위 복사: 같은 절차가 dict마다 정의 전체를 복사한다. `$ref`가 없어도 정의가 많으면 메모리가
  커진다.
- 깊이: 펼친 뒤 깊이가 깊으면 Vertex 변환이 예외를 낸다(`type` 배열은 한 겹을 더한다).
- `type` 배열: Vertex는 배열의 object·array 항목마다 하위 스키마를 복사한다. 그런 항목이 둘 이상이면
  겹마다 두 배가 된다.
넘으면 노출하지 않는다(`schema_expansion`). 그 절차가 예외를 낼 모양(문자열이 아닌 `$ref`, 참조되는
dict 아닌 정의, 매핑이 아닌 `$defs`·`definitions`)도 노출하지 않는다 — 그런 도구 하나가 같은 모델의
모든 채팅 턴을 실패시킨다.
감싸기는 하지 않는다 — 도구 설명은 원래 사용법을 알려 주는 글이다. 1차 방어는 관리자 승인과
read-only allowlist다. 입력은 바꾸지 않는다(프로세스 캐시 원문 보존). DB·모델 무의존 순수 모듈이다.
"""

import copy
import hashlib
import json
import math
from collections import OrderedDict, deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from biznotes_core.config import get_settings
from biznotes_core.services.untrusted import escape_untrusted_tags, strip_invisible

MCP_TOOL_TEXT_LIMIT = 1000  # description·주석 문자열 하나의 상한(정화 뒤)
MCP_SCHEMA_MAX_DEPTH = 32  # 최상위 스키마가 1, dict·list 한 겹마다 +1
MCP_SCHEMA_MAX_BYTES = 64 * 1024  # 정화한 스키마의 JSON(UTF-8) 크기
# 펼친 결과의 깊이 상한(#294, 위 깊이와 같은 셈) — 모든 환경에서 지킨다. Vertex 변환은 깊이 100에서
# 예외를 내고 `type` 배열이 겹마다 한 겹을 더하므로 2배를 견디게 잡는다(2×48 < 100). 복사 전에
# 판정하므로 판정기의 재귀 깊은 복사도 이 깊이로 묶인다.
MCP_SCHEMA_MAX_EXPANDED_DEPTH = 48
# Anthropic·Fireworks 모드의 복사 깊이 상한 — 그 경로에는 깊이 예외가 없어 재귀 안전만 본다
MCP_SCHEMA_RECURSION_SAFE_DEPTH = 128
# 모든 환경의 안전 상한(사용자 결정) — dev·staging도 이 값을 넘는 전개는 거부한다. 넘는 폭탄은
# LiteLLM이 워커 메모리를 고갈시킨다(사슬 k=2·d=60 6 KB가 7초에 1.5 GB, 리뷰 실측). 바이트는
# LiteLLM이 Anthropic 경로에 쓰는 `DEFS_MAX_INLINED_BYTES`와 같은 값이다. 운영은 설정
# `MCP_SCHEMA_MAX_EXPANDED_BYTES`(더 작으면 그 값)를 쓴다.
MCP_SCHEMA_EXPANSION_CEILING_BYTES = 10_000_000
# LiteLLM이 dict마다 복사하는 범위(정의 dict) 항목 수 상한 — 모든 환경. 정의 3,000개 × dict
# 3,200개(960만 항목)에서 LiteLLM 피크가 333 MB였다(리뷰 실측) — 400만이면 140 MB 안팎이다. 운영
# 기본 예산(1 MiB)으로 받는 OpenAPI 유래 모델 DAG(정의 64개, 약 190만 항목)는 넉넉히 들어온다.
MCP_SCHEMA_MAX_SCOPE_ENTRIES = 4_000_000

MCPToolSpecRejectReason = Literal[
    "schema_not_object",
    "schema_too_deep",
    "schema_too_large",
    "schema_structural_text",
    "schema_unsupported_value",
    "schema_expansion",
]

_ANNOTATION_KEYS = frozenset(
    {"description", "title", "$comment", "markdownDescription", "deprecationMessage"}
)
_ANNOTATION_LIST_KEYS = frozenset({"enumDescriptions", "markdownEnumDescriptions"})
# 데이터 값이 오는 키 — 아래 하위 트리의 문자열은 모두 구조 문자열이다
_DATA_KEYS = frozenset({"enum", "const", "default", "examples"})
# 이름 지도 — 값 dict의 키는 키워드가 아니라 속성·정의 이름이다(이름이 `default`인 속성 등)
_NAME_MAP_KEYS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)


class MCPToolSpecRejected(ValueError):
    """노출하면 안 되는 도구 스펙. `reason`은 고정 코드다(원문을 싣지 않는다)."""

    def __init__(self, reason: MCPToolSpecRejectReason) -> None:
        super().__init__(reason)
        self.reason: MCPToolSpecRejectReason = reason


def _clean_text(text: str) -> str:
    return escape_untrusted_tags(strip_invisible(text))[:MCP_TOOL_TEXT_LIMIT]


class _Walker:
    """스키마를 새로 만들며 순회한다. 글자 수를 누적해 크기 상한을 넘으면 일찍 끝낸다."""

    def __init__(self) -> None:
        self.chars = 0

    def _count(self, text: str) -> None:
        # 글자 수 ≤ UTF-8 바이트 수라, 글자 수가 상한을 넘으면 직렬화 크기도 넘는다
        self.chars += len(text)
        if self.chars > MCP_SCHEMA_MAX_BYTES:
            raise MCPToolSpecRejected("schema_too_large")

    def structural(self, text: str) -> str:
        self._count(text)
        if strip_invisible(text) != text or escape_untrusted_tags(text) != text:
            raise MCPToolSpecRejected("schema_structural_text")
        return text

    def annotation(self, text: str) -> str:
        self._count(text)
        return _clean_text(text)

    def names(self, mapping: dict[Any, Any], depth: int) -> dict[str, Any]:
        """이름 지도 — 키는 이름(구조 문자열), 값은 각각 스키마다."""
        if depth > MCP_SCHEMA_MAX_DEPTH:
            raise MCPToolSpecRejected("schema_too_deep")
        out: dict[str, Any] = {}
        for name, schema in mapping.items():
            if not isinstance(name, str):
                raise MCPToolSpecRejected("schema_unsupported_value")
            out[self.structural(name)] = self.node(schema, depth + 1)
        return out

    def node(self, value: Any, depth: int, data: bool = False) -> Any:
        if isinstance(value, dict | list) and depth > MCP_SCHEMA_MAX_DEPTH:
            raise MCPToolSpecRejected("schema_too_deep")
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise MCPToolSpecRejected("schema_unsupported_value")
                self.structural(key)
                if data:
                    out[key] = self.node(item, depth + 1, data=True)
                elif isinstance(item, str) and (key in _ANNOTATION_KEYS or key.startswith("x-")):
                    out[key] = self.annotation(item)
                elif key in _NAME_MAP_KEYS and isinstance(item, dict):
                    out[key] = self.names(item, depth + 1)
                elif key in _ANNOTATION_LIST_KEYS and isinstance(item, list):
                    out[key] = [
                        self.annotation(i) if isinstance(i, str) else self.node(i, depth + 2)
                        for i in item
                    ]
                else:
                    # 키가 주석 이름이어도 값이 dict·list면(예: 이름이 `title`인 속성) 스키마로 본다
                    out[key] = self.node(item, depth + 1, data=key in _DATA_KEYS)
            return out
        if isinstance(value, list):
            return [self.node(item, depth + 1, data=data) for item in value]
        if isinstance(value, str):
            return self.structural(value)
        if isinstance(value, float) and not math.isfinite(value):
            raise MCPToolSpecRejected("schema_unsupported_value")
        if value is None or isinstance(value, bool | int | float):
            return value
        raise MCPToolSpecRejected("schema_unsupported_value")


def _measure(obj: object) -> tuple[int, int]:
    """(LiteLLM `_estimate_json_bytes`와 같은 추정 바이트, 가장 깊은 dict·list 깊이).

    한 번의 반복 순회로 잰다(재귀 없음).
    """
    total = 0
    deepest = 0
    stack: list[tuple[object, int]] = [(obj, 1)]
    while stack:
        x, depth = stack.pop()
        if isinstance(x, dict):
            deepest = max(deepest, depth)
            total += 2
            for k, v in x.items():
                total += len(str(k)) + 4
                stack.append((v, depth + 1))
        elif isinstance(x, list):
            deepest = max(deepest, depth)
            total += 2 + max(0, len(x) - 1)
            stack.extend((v, depth + 1) for v in x)
        elif isinstance(x, str):
            total += len(x) + 2
        elif isinstance(x, bool):
            total += 4 if x else 5
        elif x is None:
            total += 4
        else:
            total += 24
    return total, deepest


def _estimate_json_bytes(obj: object) -> int:
    """LiteLLM `_estimate_json_bytes`와 같은 셈 — 직렬화하지 않고 JSON 크기를 어림한다."""
    return _measure(obj)[0]


def _container_depth(value: object, depth: int) -> int:
    """`value`가 `depth`에 있을 때 그 아래 가장 깊은 dict·list의 깊이."""
    deepest = _measure(value)[1]
    return depth + deepest - 1 if deepest else 0


@dataclass(frozen=True)
class _Budget:
    """크기 예산(사용자 결정, #294) — 넘으면 거부한다.

    - 바이트(인라인·`type` 배열 복제): 운영(`app_env == "prod"`)은 설정
      `MCP_SCHEMA_MAX_EXPANDED_BYTES`, dev·staging은 안전 상한 `MCP_SCHEMA_EXPANSION_CEILING_BYTES`.
      운영도 안전 상한을 넘지 않는다.
    - 범위 복사 항목: 모든 환경에서 `MCP_SCHEMA_MAX_SCOPE_ENTRIES`.
    상한 안이면 측정이 끝까지 돌므로 깊이·예외 형태 판정은 환경과 상관없이 끝까지 한다.
    """

    inlined_bytes: int
    scope_entries: int


def _budget() -> _Budget:
    settings = get_settings()
    inlined = MCP_SCHEMA_EXPANSION_CEILING_BYTES
    if settings.app_env == "prod":
        inlined = min(inlined, settings.mcp_schema_max_expanded_bytes)
    return _Budget(inlined, MCP_SCHEMA_MAX_SCOPE_ENTRIES)


class _OverBudget(Exception):
    """크기 예산을 넘었다 — `_check_expansion`이 `schema_expansion`으로 거부한다."""


class _Expansion:
    """LiteLLM `unpack_defs` 이식본 한 번의 측정값 — 누적 인라인 바이트, 범위 복사 항목 수.

    `inlines`는 대상별 추정 바이트를 복사 순서대로 담는다(충실도 테스트가 LiteLLM과 대조한다).
    """

    def __init__(self, budget: _Budget | None = None) -> None:
        self.budget = budget
        self.inlined = 0
        self.scope_entries = 0
        self.inlines: list[int] = []


def _unsupported() -> MCPToolSpecRejected:
    return MCPToolSpecRejected("schema_unsupported_value")


def _scope(outer: dict[str, Any], holder: dict[str, Any], stats: _Expansion) -> dict[str, Any]:
    """`{**outer, **holder["$defs"], **holder["definitions"]}` — LiteLLM이 dict마다 만드는 범위.

    LiteLLM은 매번 복사하므로 그 항목 수를 센다. 이식본은 자기 정의가 있을 때만 복사해 판정 비용이
    입력 크기에 비례한다(만든 뒤 바꾸지 않으므로 의미가 같다). 매핑이 아닌 값은 LiteLLM의 `{**x}`가
    `TypeError`를 내는 모양이다.
    """
    own: list[dict[str, Any]] = []
    for key in ("$defs", "definitions"):
        if key in holder:
            value = holder[key]
            if not isinstance(value, dict):
                raise _unsupported()
            own.append(value)
    stats.scope_entries += len(outer) + sum(len(v) for v in own)
    if stats.budget is not None and stats.scope_entries > stats.budget.scope_entries:
        raise _OverBudget
    if not own:
        return outer
    merged = dict(outer)
    for value in own:
        merged.update(value)
    return merged


def _simulate_unpack(
    work: dict[str, Any],
    defs: dict[str, Any],
    stats: _Expansion,
    max_depth: int = MCP_SCHEMA_MAX_EXPANDED_DEPTH,
) -> None:
    """LiteLLM 1.105.1 `unpack_defs(work, defs)`의 이식본(`litellm_core_utils/prompt_templates/
    common_utils.py`). `work`를 제자리에서 펼친다.

    - 너비 우선이고, `$ref`의 마지막 `/` 조각이 이름이다.
    - 범위는 바깥·노드·대상의 `$defs`·`definitions`를 병합한 것이다.
    - 지금 경로의 사슬에 있는 이름과 모르는(`None`) 이름은 그대로 두고 더 걷지 않는다.
    - 대상의 깊은 복사로 `$ref` dict를 통째로 바꾸고 그 사본을 다시 걷는다. 제자리에서 바꾸므로
      앞서 펼친 정의를 나중에 복사하면 펼친 채로 복사된다.
    복사 전에 대상의 추정 바이트를 누적해 예산을 넘으면 `_OverBudget`이다 — 폭탄도 예산만큼만
    일한다. 복사 결과의 깊이가 상한을 넘으면 복사하지 않고 거부한다(재귀 깊은 복사가 Python 재귀
    한도에 닿지 않게, 리뷰 RR294-1). LiteLLM이 예외를 낼 지점(문자열이 아닌 `$ref`, dict가 아닌
    대상, 매핑이 아닌 정의)은 명시적으로 판정한다.
    """
    root_scope = _scope(defs, work, stats)
    queue: deque[tuple[Any, Any, Any, dict[str, Any], frozenset[str], int]] = deque(
        [(work, None, None, root_scope, frozenset(), 1)]
    )
    while queue:
        node, parent, key, scope, chain, depth = queue.popleft()
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"]
                if not isinstance(ref, str):
                    raise _unsupported()  # LiteLLM: `node["$ref"].split` → AttributeError
                name = ref.split("/")[-1]
                if name in chain:
                    continue
                target = scope.get(name)
                if target is None:
                    continue
                if not isinstance(target, dict):
                    raise _unsupported()  # LiteLLM: `target_schema.get` → AttributeError
                size, target_depth = _measure(target)
                stats.inlines.append(size)
                stats.inlined += size
                if stats.budget is not None and stats.inlined > stats.budget.inlined_bytes:
                    raise _OverBudget
                if depth + target_depth - 1 > max_depth:
                    raise MCPToolSpecRejected("schema_expansion")
                child_scope = _scope(scope, target, stats)
                resolved = copy.deepcopy(target)
                if parent is not None:
                    parent[key] = resolved
                else:
                    work.clear()
                    work.update(resolved)
                    resolved = work
                queue.append((resolved, parent, key, child_scope, chain | {name}, depth))
            else:
                current = _scope(scope, node, stats)
                for k, v in node.items():
                    queue.append((v, node, k, current, chain, depth + 1))
        elif isinstance(node, list):
            for i, item in enumerate(node):
                queue.append((item, node, i, scope, chain, depth + 1))


# Vertex `_convert_schema_types`가 `type` 배열의 object·array 항목마다 복사하는 필드
_TYPE_SPECIFIC_FIELDS = frozenset(
    {"properties", "required", "additionalProperties", "items",
     "minItems", "maxItems", "minProperties", "maxProperties"}
)  # fmt: skip


def _check_type_array_copies(work: dict[str, Any], limit: int) -> None:
    """Vertex `type` 배열 변환 뒤의 추정 크기가 `limit`을 넘으면 `_OverBudget`(리뷰 RR294-2).

    `type` 배열에 object·array 항목이 k개(2 이상)이고 type 전용 필드가 있는 노드는 그 필드를 k번
    복사한다. 그런 노드가 중첩되면 배수가 곱해진다(683 B → 5.8 MB, 리뷰 실측). 노드마다 조상
    배수의 곱을 실어 크기를 더하므로, 중첩 없는 `["object", "array"]`나 「임의 JSON 값」 type
    목록은 통과한다. 데이터 문맥(`enum`·`const`·`default`·`examples`) 아래는 변환하지 않는다.
    상수배 근사다 — 실제 Vertex 출력은 `anyOf`·`nullable` 등이 더해져 추정의 약 2배까지 된다(리뷰
    실측). 지수 오차는 없다.
    """
    total = 0
    stack: list[tuple[Any, int]] = [(work, 1)]
    while stack:
        node, factor = stack.pop()
        if isinstance(node, dict):
            total += 2 * factor
            types = node.get("type")
            copies = 1
            if isinstance(types, list) and len(types) > 1 and _TYPE_SPECIFIC_FIELDS & node.keys():
                copies = max(1, sum(1 for t in types if t in ("object", "array")))
            for k, v in node.items():
                total += (len(str(k)) + 4) * factor
                if k in _DATA_KEYS:
                    total += _estimate_json_bytes(v) * factor
                    continue
                stack.append((v, factor * copies if k in _TYPE_SPECIFIC_FIELDS else factor))
        elif isinstance(node, list):
            total += (2 + max(0, len(node) - 1)) * factor
            stack.extend((v, factor) for v in node)
        else:
            total += _estimate_json_bytes(node) * factor
        if total > limit:
            raise _OverBudget


def _has_legacy_defs(schema: dict[str, Any]) -> bool:
    """LiteLLM `_has_legacy_defs` — Anthropic·Fireworks 경로가 전개를 하는 조건."""
    components = schema.get("components")
    return "definitions" in schema or (
        isinstance(components, dict) and isinstance(components.get("schemas"), dict)
    )


def _check_expansion(schema: dict[str, Any]) -> None:
    """LiteLLM이 프로바이더 요청을 만들며 하는 전개를 예산을 걸고 흉내 낸다(#294). 입력은 그대로다.

    1. Vertex·Bedrock: 최상위 `$defs`를 꺼내 `unpack_defs`를 부른다. 펼친 결과의 깊이와 `type` 배열
       복사 크기도 본다.
    2. Anthropic·Fireworks(`unpack_legacy_defs`): `definitions` 키나 `components.schemas`가 있으면
       둘을 꺼내 정의로 쓴다(충돌하면 `definitions`가 이긴다). `$defs`는 트리에 남긴 채 범위에
       넣는다.
    크기 예산(`_Budget`)을 넘으면 거부한다. 레거시 모드의 복사 깊이는 재귀 안전 상한만 본다.
    """
    budget = _budget()
    try:
        work, defs = _vertex_input(schema)
        _simulate_unpack(work, defs, _Expansion(budget))
        if _container_depth(work, 1) > MCP_SCHEMA_MAX_EXPANDED_DEPTH:
            raise MCPToolSpecRejected("schema_expansion")
        _check_type_array_copies(work, budget.inlined_bytes)
        legacy = _legacy_input(schema)
        if legacy is not None:
            _simulate_unpack(*legacy, _Expansion(budget), MCP_SCHEMA_RECURSION_SAFE_DEPTH)
    except _OverBudget:
        raise MCPToolSpecRejected("schema_expansion") from None


def _vertex_input(schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Vertex·Bedrock이 `unpack_defs`에 넘기는 값(사본) — 최상위 `$defs`를 꺼낸다."""
    work = copy.deepcopy(schema)
    defs = work.pop("$defs", {})
    if not isinstance(defs, dict):
        raise _unsupported()
    return work, defs


def _legacy_input(schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """`unpack_legacy_defs`가 `unpack_defs`에 넘기는 값의 사본. 전개하지 않는 스키마면 `None`."""
    if not _has_legacy_defs(schema):
        return None
    work = copy.deepcopy(schema)
    components = work.pop("components", {})
    if not isinstance(components, dict):
        raise _unsupported()
    legacy = components.get("schemas") or {}
    definitions = work.pop("definitions", None) or {}
    if not isinstance(legacy, dict) or not isinstance(definitions, dict):
        raise _unsupported()
    return work, {**legacy, **definitions}


# 판정 결과 캐시(리뷰 RR2-294-3) — 디스커버리는 매 run 도구마다 판정한다. 같은 원문·예산이면 결과가
# 같으므로 프로세스에서 한 번만 잰다. 값은 (설명, 스키마) 또는 거부 사유다. 꺼낼 때 스키마를 깊은
# 복사한다(LiteLLM Vertex 변환이 받은 스펙을 제자리에서 바꾼다).
_SPEC_VERDICTS: "OrderedDict[str, tuple[str, dict[str, Any]] | str]" = OrderedDict()
_SPEC_VERDICTS_LIMIT = 512


def _verdict_key(tool: Mapping[str, Any]) -> str | None:
    budget = _budget()
    try:
        raw = json.dumps(
            [tool.get("description"), tool.get("input_schema"), budget.inlined_bytes],
            sort_keys=True,
            ensure_ascii=False,
        )
    except (TypeError, ValueError):
        return None  # JSON이 아닌 값 — 캐시하지 않고 매번 판정한다
    return hashlib.sha256(raw.encode()).hexdigest()


def _clear_spec_verdicts() -> None:
    _SPEC_VERDICTS.clear()


def mcp_tool_function(tool: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """`list_tools` 항목 하나 → 정화한 `(description, parameters)`.

    `input_schema`가 비어 있으면(None·`{}` 등) 빈 객체 스키마다. 노출하면 안 되면
    `MCPToolSpecRejected`. 런타임 tool spec과 같은 결과를 보여 줄 카탈로그도 이 함수를 쓴다.
    같은 원문·예산의 판정은 프로세스에서 한 번만 하고 결과를 재사용한다(새 객체를 돌려준다).
    """
    key = _verdict_key(tool)
    if key is not None and key in _SPEC_VERDICTS:
        _SPEC_VERDICTS.move_to_end(key)
        cached = _SPEC_VERDICTS[key]
        if isinstance(cached, str):
            raise MCPToolSpecRejected(cached)  # type: ignore[arg-type]
        return cached[0], copy.deepcopy(cached[1])
    try:
        description, schema = _sanitized_function(tool)
    except MCPToolSpecRejected as e:
        _remember(key, e.reason)
        raise
    _remember(key, (description, copy.deepcopy(schema)))
    return description, schema


def _remember(key: str | None, verdict: "tuple[str, dict[str, Any]] | str") -> None:
    if key is None:
        return
    if len(_SPEC_VERDICTS) >= _SPEC_VERDICTS_LIMIT:
        _SPEC_VERDICTS.popitem(last=False)
    _SPEC_VERDICTS[key] = verdict


def _sanitized_function(tool: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """`mcp_tool_function`의 판정 본체(캐시 없음)."""
    description = _clean_text(str(tool.get("description") or ""))
    raw = tool.get("input_schema")
    if not raw:
        return description, {"type": "object", "properties": {}}
    if not isinstance(raw, dict):
        raise MCPToolSpecRejected("schema_not_object")
    try:
        schema = _Walker().node(raw, 1)
        size = len(json.dumps(schema, ensure_ascii=False, allow_nan=False).encode())
    except (ValueError, TypeError, RecursionError) as e:
        # 예상 밖의 값(아주 큰 정수 등)도 도구 하나만 빼고 디스커버리는 계속한다
        if isinstance(e, MCPToolSpecRejected):
            raise
        raise MCPToolSpecRejected("schema_unsupported_value") from e
    if size > MCP_SCHEMA_MAX_BYTES:
        raise MCPToolSpecRejected("schema_too_large")
    try:
        _check_expansion(schema)
    except RecursionError:
        # 마지막 방어선 — 깊이 판정이 복사를 묶지만, 판정기의 예상 밖 재귀도 도구 하나만 뺀다
        raise MCPToolSpecRejected("schema_expansion") from None
    return description, schema


def _spec_hash(description: str, parameters: dict[str, Any]) -> str:
    """정화한 `(description, parameters)`의 정규 JSON sha256 hex(#293)."""
    payload = json.dumps(
        {"description": description, "input_schema": parameters},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def mcp_tool_spec_hash(tool: Mapping[str, Any]) -> str:
    """`list_tools` 항목의 고정 해시 — 승인 시점 스펙 고정과 승인 뒤 변경 탐지에 쓴다(#293).

    LLM이 실제로 읽는 정화 결과(`mcp_tool_function`)의 설명·입력 스키마를 정규 JSON(키 정렬,
    `(",", ":")` 구분자, `ensure_ascii=False`, UTF-8)으로 직렬화해 sha256 hex로 만든다. 정상 도구는
    원문과 바이트가 같고, 보이지 않는 글자만 바뀐 변경은 같은 해시다. 이름·title·annotations는
    LLM 스펙에 실리지 않아 넣지 않는다. 정화가 거부하면 `MCPToolSpecRejected`.

    항목 형태는 코어 `mcp_client.list_tools`가 돌려주는 것(`input_schema`)이다. MCP SDK 원시 형태
    (`inputSchema`)를 넘기면 빈 스키마로 해시돼 영구히 어긋나므로 `ValueError`로 거부한다.

    정상 도구의 `mcp_tool_function` 출력이나 이 정규형을 바꾸는 변경은 그 도구들의 고정을 모두
    어긋나게 한다 — 비호환(재승인 필요)으로 릴리스 노트에 적는다.
    """
    if "inputSchema" in tool and "input_schema" not in tool:
        raise ValueError("list_tools 항목은 input_schema 키를 쓴다(inputSchema가 아니다)")
    return _spec_hash(*mcp_tool_function(tool))


__all__ = [
    "MCP_SCHEMA_MAX_BYTES",
    "MCP_SCHEMA_MAX_DEPTH",
    "MCP_SCHEMA_EXPANSION_CEILING_BYTES",
    "MCP_SCHEMA_MAX_EXPANDED_DEPTH",
    "MCP_SCHEMA_MAX_SCOPE_ENTRIES",
    "MCP_SCHEMA_RECURSION_SAFE_DEPTH",
    "MCP_TOOL_TEXT_LIMIT",
    "MCPToolSpecRejectReason",
    "MCPToolSpecRejected",
    "mcp_tool_function",
    "mcp_tool_spec_hash",
]
