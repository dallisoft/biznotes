"""MCP 도구 스키마의 `$ref` 전개 예산 (#294).

계획 `docs/plans/mcp-ref-expansion-budget/plan.md` 수락 기준 1~6이다. 판정(`mcp_tool_function`)은
실경로다. 충실도는 설치된 LiteLLM의 실제 전개기(`unpack_defs`, Vertex `_build_vertex_schema`)와
대조한다 — 네트워크는 쓰지 않는다. 디스커버리 경로는 `test_mcp_tool_spec`의 `World`(외부 서버·DB
조회·승인 조회 대역)를 쓴다.
"""

import copy
import itertools
import json
import string
import time
import tracemalloc
from typing import Any

import pytest
from conftest import make_settings
from litellm.litellm_core_utils.prompt_templates import common_utils as litellm_common
from litellm.litellm_core_utils.prompt_templates.common_utils import (
    sanitize_input_schema_for_anthropic,
    unpack_defs,
    unpack_legacy_defs,
)
from litellm.llms.vertex_ai.common_utils import _build_vertex_schema
from pydantic import BaseModel
from test_mcp_tool_spec import World

from biznotes_core.config import CoreSettings
from biznotes_core.services.agents import mcp_tool_spec
from biznotes_core.services.agents.mcp_tool_spec import (
    MCP_SCHEMA_MAX_BYTES,
    MCP_SCHEMA_MAX_DEPTH,
    MCP_SCHEMA_MAX_EXPANDED_DEPTH,
    MCPToolSpecRejected,
    _Expansion,
    _legacy_input,
    _simulate_unpack,
    _vertex_input,
    mcp_tool_function,
)

# 운영 기본 예산(바이트) — 크기 예산은 `app_env == "prod"`에서만 거부한다(사용자 결정)
BUDGET = CoreSettings.model_fields["mcp_schema_max_expanded_bytes"].default


@pytest.fixture(autouse=True)
def app_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """기본은 운영(크기 예산 적용)이다. 테스트가 `app_env("dev")`처럼 바꿀 수 있다."""

    def set_env(env: str, **overrides: Any) -> None:
        settings = make_settings(app_env=env, **overrides)
        monkeypatch.setattr(mcp_tool_spec, "get_settings", lambda: settings)

    set_env("prod")
    return set_env


def _fn(schema: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return mcp_tool_function({"name": "t", "description": "d", "input_schema": schema})


def _reason(schema: dict[str, Any]) -> str | None:
    try:
        _fn(schema)
    except MCPToolSpecRejected as e:
        return e.reason
    return None


def fan_out(k: int, d: int) -> dict[str, Any]:
    """정의 하나가 다음 정의를 k번 가리키는 사슬 — 펼치면 k^d배가 된다."""
    defs: dict[str, Any] = {
        f"L{i}": {
            "type": "object",
            "properties": {f"p{j}": {"$ref": f"#/$defs/L{i + 1}"} for j in range(k)},
        }
        for i in range(d)
    }
    defs[f"L{d}"] = {"type": "string", "description": "잎"}
    return {"type": "object", "properties": {"root": {"$ref": "#/$defs/L0"}}, "$defs": defs}


def mutual(n: int) -> dict[str, Any]:
    """정의 n개가 서로를 모두 가리킨다 — 사슬 규칙만으로는 계승 수준으로 커진다."""
    names = [f"M{i}" for i in range(n)]
    defs = {
        a: {"type": "object", "properties": {b: {"$ref": f"#/$defs/{b}"} for b in names if b != a}}
        for a in names
    }
    return {"type": "object", "properties": {"m": {"$ref": "#/$defs/M0"}}, "$defs": defs}


def chain(n: int) -> dict[str, Any]:
    """참조 사슬 — 원문은 얕지만 펼치면 정의마다 두 겹씩 깊어진다."""
    defs: dict[str, Any] = {
        f"D{i}": {"type": "object", "properties": {"x": {"$ref": f"#/$defs/D{i + 1}"}}}
        for i in range(n)
    }
    defs[f"D{n}"] = {"type": "string"}
    return {"type": "object", "properties": {"a": {"$ref": "#/$defs/D0"}}, "$defs": defs}


class Address(BaseModel):
    city: str
    zip_code: str | None = None


class Person(BaseModel):
    name: str
    address: Address
    previous: list[Address] = []


class Query(BaseModel):
    owner: Person
    tags: list[str] = []


class Tree(BaseModel):
    label: str
    children: list["Tree"] = []


LEGACY_DEFINITIONS = {
    "type": "object",
    "properties": {"item": {"$ref": "#/definitions/Item"}},
    "definitions": {"Item": {"type": "object", "properties": {"id": {"type": "integer"}}}},
}
NO_REFS = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}

NORMAL = {
    "pydantic-nested": Query.model_json_schema(),
    "pydantic-recursive": Tree.model_json_schema(),
    "definitions": LEGACY_DEFINITIONS,
    "no-refs": NO_REFS,
    "fan-out-under-budget": fan_out(4, 5),
}


# ── 수락 기준 1: 팬아웃 폭탄 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "schema",
    [fan_out(2, 60), fan_out(4, 8), fan_out(10, 5), mutual(8)],
    ids=["k2-d60", "k4-d8", "k10-d5", "mutual-8"],
)
def test_expansion_bombs_are_rejected_quickly(schema: dict[str, Any]) -> None:
    started = time.perf_counter()

    assert _reason(schema) == "schema_expansion"

    assert time.perf_counter() - started < 1.0  # 예산(1 MiB)만큼만 일한다


# ── 수락 기준 2: 일반 스키마는 그대로 ────────────────────────────────────


@pytest.mark.parametrize("name", list(NORMAL))
def test_normal_schemas_pass_unchanged(name: str) -> None:
    schema = NORMAL[name]
    original = copy.deepcopy(schema)

    _, out = _fn(schema)

    assert out == original  # 판정은 측정 전용이다 — 바이트가 그대로다(#229 수락 기준 1)
    assert schema == original  # 입력도 바꾸지 않는다


# ── 수락 기준 3: LiteLLM이 예외를 내는 모양 ──────────────────────────────

CRASHING = {
    "non-string-ref": {"type": "object", "properties": {"a": {"$ref": 5}}},
    "property-named-ref": {
        "type": "object",
        "properties": {"$ref": {"type": "string"}},
    },
    "non-dict-target": {
        "type": "object",
        "properties": {"a": {"$ref": "#/$defs/T"}},
        "$defs": {"T": True},
    },
    "null-defs": {"type": "object", "properties": {}, "$defs": None},
    "list-definitions": {
        "type": "object",
        "properties": {"a": {"type": "object", "definitions": []}},
    },
}


@pytest.mark.parametrize("name", list(CRASHING))
def test_shapes_that_crash_litellm_are_rejected(name: str) -> None:
    assert _reason(copy.deepcopy(CRASHING[name])) == "schema_unsupported_value"


@pytest.mark.parametrize("name", list(CRASHING))
def test_litellm_vertex_really_crashes_on_those_shapes(name: str) -> None:
    """수락 기준 6(판정 근거) — 같은 모양에서 실제 Vertex 변환이 예외를 낸다."""
    with pytest.raises((AttributeError, TypeError)):
        _build_vertex_schema(copy.deepcopy(CRASHING[name]))


# ── 수락 기준 4: 펼친 깊이 ───────────────────────────────────────────────


def test_deep_expansion_is_rejected_even_when_the_raw_schema_is_shallow() -> None:
    deep = chain(40)  # 원문 깊이는 상한 안이지만 펼치면 80겹을 넘는다
    assert _reason(deep) == "schema_expansion"
    ok = chain((MCP_SCHEMA_MAX_EXPANDED_DEPTH - 4) // 2)
    assert _reason(ok) is None
    assert MCP_SCHEMA_MAX_DEPTH < MCP_SCHEMA_MAX_EXPANDED_DEPTH


# ── 수락 기준 6: 설치된 LiteLLM과 대조 ───────────────────────────────────


@pytest.mark.parametrize("name", list(NORMAL))
def test_accepted_schemas_go_through_the_real_vertex_conversion(name: str) -> None:
    _, out = _fn(NORMAL[name])
    _build_vertex_schema(copy.deepcopy(out))  # 예외 없이 끝난다


def test_accepted_chain_at_the_depth_cap_goes_through_vertex() -> None:
    _, out = _fn(chain((MCP_SCHEMA_MAX_EXPANDED_DEPTH - 4) // 2))
    _build_vertex_schema(copy.deepcopy(out))


def _litellm_rejects(schema: dict[str, Any]) -> bool:
    """LiteLLM 자신의 누적 상한(`max_inlined_bytes`)을 같은 예산으로 건 Vertex·Bedrock 절차."""
    work = copy.deepcopy(schema)
    defs = work.pop("$defs", {})
    try:
        unpack_defs(work, defs, max_inlined_bytes=BUDGET)
    except ValueError:
        return True
    return False


@pytest.mark.parametrize(
    "schema",
    [fan_out(k, d) for k in (2, 3, 4, 6) for d in (3, 5, 6, 7, 8)]
    + [mutual(n) for n in (3, 4, 5, 6, 7)],
)
def test_budget_decision_matches_litellm_accounting(schema: dict[str, Any]) -> None:
    """이식본의 누적 셈이 LiteLLM과 같다 — 같은 예산에서 거부·통과가 갈리지 않는다.

    깊이 상한에 먼저 걸리지 않는 얕은 스키마만 쓴다(사슬 깊이 ≤ 8 → 펼친 깊이 ≤ 20).
    """
    ours = _reason(schema) == "schema_expansion"
    assert ours == _litellm_rejects(schema)


# ── 수락 기준 5: 디스커버리 경로 ─────────────────────────────────────────


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    return World(monkeypatch)


async def test_bomb_tool_is_hidden_and_the_reason_is_logged(world: World) -> None:
    world.tools = [
        {"name": "bomb", "description": "d", "input_schema": fan_out(2, 60)},
        {"name": "fine", "description": "d", "input_schema": Query.model_json_schema()},
    ]

    by_tool, logs, _ = await world.specs()

    assert set(by_tool) == {"fine"}
    reasons = {e["tool"]: e["reason"] for e in logs if "reason" in e}
    assert reasons == {"bomb": "schema_expansion"}


# ── 독립 리뷰 R294-1: LiteLLM의 범위 복사(정의 수 × dict 수) ──────────────


def many_defs(n_defs: int, n_nodes: int) -> dict[str, Any]:
    """`$ref`가 하나도 없는 스키마 — 정의가 많으면 LiteLLM이 dict마다 범위를 복사한다."""
    alphabet = string.ascii_letters + string.digits
    names = ["x" + "".join(p) for p in itertools.product(alphabet, repeat=2)][:n_defs]
    items = {"anyOf": [{"a": 1} for _ in range(n_nodes)]}
    return {
        "type": "object",
        "properties": {"x": {"type": "array", "items": items}},
        "$defs": {n: {} for n in names},
    }


def test_many_definitions_without_refs_are_rejected_cheaply() -> None:
    schema = many_defs(3000, 3200)  # 리뷰 실측: LiteLLM 피크 333 MB
    assert len(json.dumps(schema)) < MCP_SCHEMA_MAX_BYTES  # 원문 상한 안이다
    tracemalloc.start()
    started = time.perf_counter()

    reason = _reason(schema)

    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert reason == "schema_expansion"
    assert elapsed < 1.0 and peak < 20 * 1024 * 1024  # 판정기 자신은 범위를 복사하지 않는다


def test_fan_out_under_the_byte_budget_with_many_definitions_is_rejected() -> None:
    """바이트 예산 안의 팬아웃(추정 70 KB)도 정의가 많으면 범위 복사가 수백만 항목이 된다."""
    schema = fan_out(10, 3)
    assert _reason(schema) is None
    schema["$defs"].update(many_defs(2500, 0)["$defs"])
    assert _reason(schema) == "schema_expansion"


def test_a_few_definitions_keep_working() -> None:
    assert _reason(many_defs(20, 500)) is None


# ── R294-2: 깊이는 펼친 최종 구조로, type 배열 2배를 견디게 ───────────────


def items_chain(d: int) -> dict[str, Any]:
    """`type: ["array", "null"]` + `items` 사슬 — Vertex는 겹마다 `anyOf` 한 겹을 더한다."""
    defs: dict[str, Any] = {
        f"D{i}": {"type": ["array", "null"], "items": {"$ref": f"#/$defs/D{i + 1}"}}
        for i in range(d)
    }
    defs[f"D{d}"] = {"type": "string"}
    return {"type": "object", "properties": {"x": {"$ref": "#/$defs/D0"}}, "$defs": defs}


def props_chain(d: int) -> dict[str, Any]:
    defs: dict[str, Any] = {
        f"D{i}": {"type": ["object", "null"], "properties": {"c": {"$ref": f"#/$defs/D{i + 1}"}}}
        for i in range(d)
    }
    defs[f"D{d}"] = {"type": "string"}
    return {"type": "object", "properties": {"x": {"$ref": "#/$defs/D0"}}, "$defs": defs}


def hidden_under_unresolved_ref(width: int, height: int) -> dict[str, Any]:
    """해석하지 않는 `$ref`(모르는 이름) 옆의 깊은 형제 서브트리 — 전개 순회가 걷지 않는다."""
    hidden: dict[str, Any] = {"type": "string"}
    for _ in range(height):
        hidden = {"type": ["array", "null"], "items": hidden}
    defs: dict[str, Any] = {
        f"D{i}": {"type": "array", "items": {"$ref": f"#/$defs/D{i + 1}"}} for i in range(width)
    }
    defs[f"D{width}"] = {"$ref": "#/$defs/NOPE", "type": ["array", "null"], "items": hidden}
    return {"type": "object", "properties": {"x": {"$ref": "#/$defs/D0"}}, "$defs": defs}


def _vertex_ok(schema: dict[str, Any]) -> bool:
    try:
        _build_vertex_schema(copy.deepcopy(schema))
    except ValueError:
        return False
    return True


@pytest.mark.parametrize("make", [items_chain, props_chain], ids=["items", "properties"])
def test_every_accepted_depth_survives_the_vertex_conversion(make: Any) -> None:
    accepted = []
    for d in range(1, 60):
        try:
            _, out = _fn(make(d))
        except MCPToolSpecRejected as e:
            assert e.reason == "schema_expansion"
            continue
        assert _vertex_ok(out), d  # 받은 스키마는 Vertex 깊이 100 예외를 내지 않는다
        accepted.append(d)
    # 지나치게 막지 않는다 — 사슬 한 겹이 깊이 1~2를 더한다
    assert accepted and max(accepted) >= MCP_SCHEMA_MAX_EXPANDED_DEPTH // 2 - 4
    assert accepted == list(range(1, max(accepted) + 1))


@pytest.mark.parametrize(("width", "height"), [(20, 28), (10, 28), (24, 20), (5, 5)])
def test_hidden_subtrees_count_toward_the_expanded_depth(width: int, height: int) -> None:
    schema = hidden_under_unresolved_ref(width, height)
    try:
        _, out = _fn(schema)
    except MCPToolSpecRejected as e:
        assert e.reason == "schema_expansion"
        return
    assert _vertex_ok(out)


def test_reviewer_hidden_depth_case_is_rejected() -> None:
    """리뷰 실측: 순회 큐 깊이로 재면 상한 안인데 펼친 최종 깊이는 77이다 — 최종 구조로 잰다."""
    assert _reason(hidden_under_unresolved_ref(20, 28)) == "schema_expansion"


# ── R294-3: Anthropic·Fireworks 경로(`unpack_legacy_defs`) ─────────────────


def components_chain(k: int, d: int) -> dict[str, Any]:
    schemas: dict[str, Any] = {
        f"C{i}": {
            "type": "object",
            "properties": {f"p{j}": {"$ref": f"#/components/schemas/C{i + 1}"} for j in range(k)},
        }
        for i in range(d)
    }
    schemas[f"C{d}"] = {"type": "string", "description": "x" * 20}
    return {
        "type": "object",
        "properties": {"root": {"$ref": "#/components/schemas/C0"}},
        "components": {"schemas": schemas},
    }


def mutual_with_empty_definitions() -> dict[str, Any]:
    schema = mutual(3)
    schema["definitions"] = {}  # 빈 dict라도 Anthropic 전개를 켠다(`$defs`는 트리에 남아 걸린다)
    return schema


@pytest.mark.parametrize(
    "schema",
    [components_chain(4, 9), components_chain(4, 8), mutual_with_empty_definitions()],
    ids=["components-k4-d9", "components-k4-d8", "mutual3+definitions"],
)
def test_anthropic_path_bombs_are_rejected(schema: dict[str, Any]) -> None:
    started = time.perf_counter()
    assert _reason(schema) == "schema_expansion"
    assert time.perf_counter() - started < 1.0


def test_same_mutual_refs_without_definitions_stay_accepted() -> None:
    assert _reason(mutual(3)) is None


@pytest.mark.parametrize(
    "schema",
    [components_chain(2, 3), components_chain(3, 4), LEGACY_DEFINITIONS, NO_REFS],
    ids=["components-k2-d3", "components-k3-d4", "definitions", "no-refs"],
)
def test_accepted_schemas_go_through_the_real_anthropic_conversion(
    schema: dict[str, Any],
) -> None:
    _, out = _fn(schema)
    sanitize_input_schema_for_anthropic(copy.deepcopy(out))  # 10 MB 예산 예외 없이 끝난다


# ── R294-4·RR294-2: Vertex type 배열 복제 — 중첩될 때만 폭탄이다 ───────────


def nested_type_array(types: list[str], depth: int) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "string"}
    for _ in range(depth):
        node = {"type": list(types), "items": node, "properties": {"a": {"type": "string"}}}
    return {"type": "object", "properties": {"x": node}}


ANY_JSON = ["string", "number", "integer", "boolean", "object", "array", "null"]


@pytest.mark.parametrize("types", [["array", "array"], ["object", "array"], ["array", "object"]])
def test_nested_multi_container_type_arrays_are_rejected(types: list[str]) -> None:
    assert _reason(nested_type_array(types, 20)) == "schema_expansion"  # 2^20배


@pytest.mark.parametrize(
    ("types", "depth"),
    [(["array", "array"], 1), (["object", "array"], 2), (ANY_JSON, 1), (["array", "null"], 10),
     (["object", "null"], 10), (["string", "integer"], 10)],
)  # fmt: skip
def test_unnested_or_single_container_type_arrays_are_accepted(
    types: list[str], depth: int
) -> None:
    _, out = _fn(nested_type_array(types, depth))
    started = time.perf_counter()
    _build_vertex_schema(copy.deepcopy(out))
    assert time.perf_counter() - started < 1.0


def test_any_json_value_fields_pass() -> None:
    """흔한 「임의 JSON 값」 필드 — Vertex는 0.2 ms에 처리한다(리뷰 실측)."""
    assert _reason({"type": "object", "properties": {"v": {"type": ANY_JSON}}}) is None


# ── R294-6·RR294-3: 현실적인 OpenAPI 유래 스키마와 예산(설정, 운영 기본 1 MiB) ──


def model_dag(layers: int, width: int) -> dict[str, Any]:
    """OpenAPI식 모델 DAG — 모델마다 필드 5개와 다음 층 모델 참조 2개, 순환 없음."""
    defs: dict[str, Any] = {}
    for layer in range(layers):
        for w in range(width):
            props: dict[str, Any] = {
                f"f{i}": {"type": "string", "description": "field"} for i in range(5)
            }
            if layer + 1 < layers:
                props["a"] = {"$ref": f"#/$defs/M{layer + 1}_{w}"}
                props["b"] = {"$ref": f"#/$defs/M{layer + 1}_{(w + 1) % width}"}
            defs[f"M{layer}_{w}"] = {"type": "object", "properties": props}
    roots = {f"root{w}": {"$ref": f"#/$defs/M0_{w}"} for w in range(width)}
    return {"type": "object", "properties": roots, "$defs": defs}


def test_medium_model_dag_fits_the_production_budget() -> None:
    """원문 22 KB·펼치면 약 600 KB — 예산을 1 MiB로 올려 받는다(256 KiB에서는 거짓 양성이었다)."""
    assert _reason(model_dag(8, 8)) is None


def test_large_model_dag_is_rejected_in_production_only(app_env: Any) -> None:
    schema = model_dag(10, 6)  # 펼치면 약 1.8 MB
    assert _reason(schema) == "schema_expansion"
    app_env("dev")
    assert _reason(schema) is None


def test_the_budget_follows_the_setting(app_env: Any) -> None:
    app_env("prod", mcp_schema_max_expanded_bytes=4 * 1024 * 1024)
    assert _reason(model_dag(10, 6)) is None
    app_env("prod", mcp_schema_max_expanded_bytes=64 * 1024)
    assert _reason(model_dag(8, 8)) == "schema_expansion"


def test_openapi_like_shared_definitions_fit_the_budget() -> None:
    money = {"type": "object", "properties": {"amount": {"type": "number"},
             "currency": {"type": "string", "enum": ["KRW", "USD", "EUR"]}}}  # fmt: skip
    address = {"type": "object", "properties": {k: {"type": "string"} for k in
               ("line1", "line2", "city", "zip", "country")}}  # fmt: skip
    line = {"type": "object", "properties": {"price": {"$ref": "#/$defs/Money"},
            "tax": {"$ref": "#/$defs/Money"}, "ship_to": {"$ref": "#/$defs/Address"}}}  # fmt: skip
    schema = {
        "type": "object",
        "properties": {f"line_{i}": {"$ref": "#/$defs/Line"} for i in range(40)},
        "$defs": {"Money": money, "Address": address, "Line": line},
    }
    assert _reason(schema) is None


# ── R294-7: 대상별 추정 순서를 LiteLLM과 대조, 참조되는 대상만 판정 ────────


def _litellm_inline_sequence(
    monkeypatch: pytest.MonkeyPatch, run: Any
) -> list[int]:
    seen: list[int] = []
    real = litellm_common._estimate_json_bytes

    def spy(obj: object) -> int:
        size = real(obj)
        seen.append(size)
        return size

    monkeypatch.setattr(litellm_common, "_estimate_json_bytes", spy)
    run()
    monkeypatch.setattr(litellm_common, "_estimate_json_bytes", real)
    return seen


@pytest.mark.parametrize(
    "schema",
    [fan_out(3, 4), mutual(4), Query.model_json_schema(), Tree.model_json_schema(),
     LEGACY_DEFINITIONS],
    ids=["fan-k3-d4", "mutual-4", "pydantic-nested", "pydantic-recursive", "definitions"],
)  # fmt: skip
def test_vertex_inline_sequence_matches_litellm(
    schema: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    work, defs = _vertex_input(schema)
    expected = _litellm_inline_sequence(
        monkeypatch, lambda: unpack_defs(work, defs, max_inlined_bytes=10**12)
    )
    stats = _Expansion()
    _simulate_unpack(*_vertex_input(schema), stats)
    assert stats.inlines == expected


@pytest.mark.parametrize(
    "schema",
    [components_chain(2, 3), components_chain(3, 3), LEGACY_DEFINITIONS],
    ids=["components-k2-d3", "components-k3-d3", "definitions"],
)
def test_legacy_inline_sequence_matches_litellm(
    schema: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = _litellm_inline_sequence(
        monkeypatch,
        lambda: unpack_legacy_defs(copy.deepcopy(schema), max_inlined_bytes=10**12),
    )
    legacy = _legacy_input(schema)
    assert legacy is not None
    stats = _Expansion()
    _simulate_unpack(*legacy, stats)
    assert stats.inlines == expected


def test_unreferenced_non_dict_definition_is_fine_and_null_targets_stay() -> None:
    unreferenced = {"type": "object", "properties": {"a": {"type": "string"}},
                    "$defs": {"T": True}}  # fmt: skip
    null_target = {"type": "object", "properties": {"a": {"$ref": "#/$defs/T"}},
                   "$defs": {"T": None}}  # fmt: skip
    assert _reason(unreferenced) is None
    assert _reason(null_target) is None
    for schema in (unreferenced, null_target):
        _build_vertex_schema(copy.deepcopy(schema))  # LiteLLM도 정상이다


# ── 운영 밖(dev·staging): 설정 예산 대신 안전 상한(10 MB)만 — 사용자 결정 ──────


@pytest.mark.parametrize("env", ["dev", "staging"])
@pytest.mark.parametrize(
    "schema",
    [mutual(8), fan_out(4, 8), model_dag(10, 6)],
    ids=["mutual-8 (3 MB)", "k4-d8 (5.5 MB)", "dag-10x6 (1.8 MB)"],
)
def test_outside_production_only_the_safety_ceiling_applies(
    app_env: Any, env: str, schema: dict[str, Any]
) -> None:
    app_env(env)
    assert _reason(schema) is None  # 운영 예산(1 MiB)은 넘지만 안전 상한 안이다


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
@pytest.mark.parametrize(
    "schema",
    [fan_out(2, 60), components_chain(4, 10), many_defs(3000, 3200),
     nested_type_array(["array", "array"], 20)],
    ids=["k2-d60", "components-k4-d10 (128 MB)", "many-defs (960만 범위)", "type-array-20"],
)  # fmt: skip
def test_bombs_beyond_the_safety_ceiling_are_rejected_everywhere(
    app_env: Any, env: str, schema: dict[str, Any]
) -> None:
    """리뷰 RR2-294-1 — 상한을 넘는 폭탄은 LiteLLM이 워커 메모리를 고갈시킨다(7초에 1.5 GB)."""
    app_env(env)
    started = time.perf_counter()
    assert _reason(schema) == "schema_expansion"
    assert time.perf_counter() - started < 5.0  # 측정은 상한에서 멈춘다


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_litellm_crash_and_depth_checks_apply_in_every_environment(
    app_env: Any, env: str
) -> None:
    app_env(env)
    assert _reason(copy.deepcopy(CRASHING["non-string-ref"])) == "schema_unsupported_value"
    assert _reason(chain(40)) == "schema_expansion"  # 펼친 깊이(Vertex 깊이 예외 방지)


@pytest.mark.parametrize("env", ["dev", "staging"])
def test_depth_is_still_checked_past_the_production_budget(app_env: Any, env: str) -> None:
    """리뷰 RR2-294-2 — 운영 예산을 넘는 전개도 상한 안이면 끝까지 재서 깊이를 판정한다."""
    app_env(env)
    schema = fan_out(4, 7)  # 펼치면 약 1.4 MB — 운영 예산(1 MiB) 밖, 안전 상한 안
    assert _reason(schema) is None
    schema["$defs"].update(chain(40)["$defs"])
    schema["properties"]["deep"] = {"$ref": "#/$defs/D0"}
    assert _reason(schema) == "schema_expansion"


# ── 재리뷰 2 RR2-294-3: 판정 결과를 프로세스에서 재사용한다 ─────────────────


def test_verdicts_are_cached_and_returned_as_fresh_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    real = mcp_tool_spec._check_expansion

    def counting(schema: dict[str, Any]) -> None:
        calls.append(1)
        real(schema)

    monkeypatch.setattr(mcp_tool_spec, "_check_expansion", counting)
    tool = {"name": "t", "description": "d", "input_schema": Query.model_json_schema()}

    first = mcp_tool_function(tool)
    second = mcp_tool_function(copy.deepcopy(tool))
    second[1]["properties"]["mutated"] = {}  # LiteLLM Vertex는 받은 스펙을 제자리에서 바꾼다

    assert len(calls) == 1
    assert first == mcp_tool_function(tool)  # 캐시가 오염되지 않는다
    assert first[1] is not second[1]


def test_rejections_are_cached_per_budget(app_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    real = mcp_tool_spec._check_expansion

    def counting(schema: dict[str, Any]) -> None:
        calls.append(1)
        real(schema)

    monkeypatch.setattr(mcp_tool_spec, "_check_expansion", counting)
    schema = model_dag(10, 6)

    assert _reason(schema) == "schema_expansion" == _reason(copy.deepcopy(schema))
    app_env("dev")  # 예산이 바뀌면 다시 잰다
    assert _reason(schema) is None
    assert len(calls) == 2


async def test_rediscovering_many_bomb_tools_is_cheap(world: World) -> None:
    world.tools = [
        {"name": f"bomb{i}", "description": "d", "input_schema": fan_out(4 + i % 3, 8)}
        for i in range(20)
    ]
    await world.specs()
    started = time.perf_counter()
    from biznotes_core.services.agents import tool_registry

    tool_registry._reported_mcp_tool_specs.clear()
    by_tool, _, _ = await world.specs()

    assert by_tool == {} and time.perf_counter() - started < 0.5


# ── 재리뷰 RR294-1: 깊은 목록 순환 정의가 판정기 재귀를 넘지 않는다 ─────────


def deep_list_cycle(defs: int, nesting: int) -> dict[str, Any]:
    """순환 정의 — 본문이 목록을 깊게 중첩한 뒤 다음 정의를 가리킨다(리뷰 실측 878 B)."""
    definitions: dict[str, Any] = {}
    for i in range(defs):
        node: Any = [{"$ref": f"#/definitions/D{(i + 1) % defs}"}]
        for _ in range(nesting - 1):
            node = [node]
        definitions[f"D{i}"] = {"x": node}
    return {"type": "object", "properties": {"a": {"$ref": "#/definitions/D0"}},
            "definitions": definitions}  # fmt: skip


@pytest.mark.parametrize("env", ["dev", "prod"])
@pytest.mark.parametrize(("defs", "nesting"), [(8, 28), (17, 28), (30, 28)])
def test_deep_list_cycles_are_rejected_not_recursion_errors(
    app_env: Any, env: str, defs: int, nesting: int
) -> None:
    app_env(env)
    assert _reason(deep_list_cycle(defs, nesting)) == "schema_expansion"


async def test_one_broken_tool_never_breaks_discovery(world: World) -> None:
    world.tools = [
        {"name": "cycle", "description": "d", "input_schema": deep_list_cycle(8, 28)},
        {"name": "fine", "description": "d", "input_schema": Query.model_json_schema()},
    ]

    by_tool, logs, _ = await world.specs()

    assert set(by_tool) == {"fine"}
    assert {e["tool"]: e["reason"] for e in logs if "reason" in e} == {
        "cycle": "schema_expansion"
    }


async def test_unexpected_checker_errors_drop_only_that_tool(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """심층 방어 — 판정기 버그가 나도 그 도구만 빠지고 디스커버리는 계속한다."""
    real = mcp_tool_spec.mcp_tool_function

    def flaky(tool: Any) -> Any:
        if tool["name"] == "boom":
            raise KeyError("판정기 버그")
        return real(tool)

    from biznotes_core.services.agents import tool_registry

    monkeypatch.setattr(tool_registry, "mcp_tool_function", flaky)
    world.tools = [
        {"name": "boom", "description": "d", "input_schema": NO_REFS},
        {"name": "fine", "description": "d", "input_schema": NO_REFS},
    ]

    by_tool, logs, _ = await world.specs()

    assert set(by_tool) == {"fine"}
    assert {e["tool"]: e["reason"] for e in logs if "reason" in e} == {"boom": "spec_error"}
