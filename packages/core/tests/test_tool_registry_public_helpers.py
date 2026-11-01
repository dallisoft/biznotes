"""`tool_registry` 공개 헬퍼 (#152). 비공개 이름 별칭은 #210에서 제거했다.

소비 제품 카탈로그(`GET /tools`)가 런타임과 같은 규칙을 공개 API로 쓰게 한다. 계약 테스트는
런타임 디스커버리(`_discover_mcp_tools`)·`run_skill` 판정과 공개 함수의 답이 같음을 잠근다.
"""

import uuid
import warnings
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import structlog.testing

from biznotes_core.services.agents import tool_registry
from biznotes_core.services.agents.react import approval, loop
from biznotes_core.services.skills.builder import load_skill_files
from tests.test_mcp_tool_list_cache import World, _server

SKILL_MD = b"---\nname: demo\ndescription: d\n---\nbody"


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    return World(monkeypatch)


# ---------- 계약: 공개 함수 == 런타임 ----------


async def test_public_name_and_side_effect_rules_match_runtime_discovery(world: World) -> None:
    """수락 기준 3 — 노출 이름·부작용 표시·서버 태그가 런타임 디스커버리와 같다.

    충돌 접미사(정규화로 같은 이름이 되는 도구)와 미승인 도구를 함께 둔다.
    """
    kosis = _server("kosis")
    world.add(kosis, ["a.b", "a/b", "meta", "hidden"], approved=["a.b", "a/b", "meta"])

    ctx = world.ctx()
    with structlog.testing.capture_logs():
        _, dispatchers = await tool_registry.build_tool_specs(ctx, ["mcp"])

    tag = tool_registry.mcp_server_tag(kosis.id)
    taken: dict[str, bool] = {}
    expected = []
    for name in ["a.b", "a/b", "meta"]:  # 런타임은 승인 도구만 차례로 이름을 예약한다
        exposed = tool_registry.mcp_tool_name(tag, name, taken)
        taken[exposed] = True
        expected.append(exposed)
    runtime_names = [n for n in dispatchers if n.startswith(tool_registry.MCP_TOOL_PREFIX)]
    assert runtime_names == expected
    assert expected[1].endswith("_1")  # 접미사 경로를 실제로 탔다
    for name in runtime_names:
        assert tool_registry.is_side_effect_tool(name) is False
        assert tool_registry.discovered_mcp_tool_side_effect(approved=True) is False
    # 미승인 도구는 노출되지 않고, 런타임 판정도 부작용이다
    hidden = tool_registry.mcp_tool_name(tag, "hidden", taken)
    assert hidden not in dispatchers
    assert tool_registry.is_side_effect_tool(hidden) is True
    assert tool_registry.discovered_mcp_tool_side_effect(approved=False) is True


async def test_public_cache_is_the_runtime_cache(world: World) -> None:
    """수락 기준 3 — 공개 get/store가 런타임 디스커버리와 같은 캐시를 읽고 쓴다."""
    kosis = _server("kosis")
    world.add(kosis, ["search"])
    assert tool_registry.get_cached_mcp_tool_list(kosis) is tool_registry.MCP_TOOL_LIST_MISS

    await world.discover()
    cached = tool_registry.get_cached_mcp_tool_list(kosis)
    assert isinstance(cached, list)
    assert [t["name"] for t in cached] == ["search"]

    # 카탈로그가 저장한 결과를 런타임이 쓴다 — 서버에 다시 붙지 않는다
    other = _server("other")
    world.add(other, ["live"])
    tool_registry.store_mcp_tool_list(
        other, [{"name": "stored", "input_schema": {"type": "object"}}]
    )
    world.approved[other.id] = frozenset({"stored"})
    _, names = await world.discover()
    assert "stored" in names
    assert world.calls == ["kosis"]

    # 실패 음성 캐시는 None이다(MISS와 다르다)
    tool_registry.store_mcp_tool_list(kosis, None)
    world.now += 61  # 이전 성공 항목은 만료, 방금 실패 항목은 30초 유효
    tool_registry.store_mcp_tool_list(kosis, None)
    assert tool_registry.get_cached_mcp_tool_list(kosis) is None


def test_cache_key_tracks_server_settings() -> None:
    """단일 비행 키 계약 — 항상 hashable·None 아님, 서버 id 포함, 설정이 바뀌면 달라진다."""
    server = _server("kosis")
    key = tool_registry.mcp_tool_list_cache_key(server)
    assert key is not None and key == tool_registry.mcp_tool_list_cache_key(server)
    hash(key)
    # 설정(지문)이 같은 두 서버도 키가 다르다 — 다른 서버의 진행 중 조회를 받지 않는다
    twin = _server("kosis")
    twin.endpoint_or_command = server.endpoint_or_command
    fingerprint = tool_registry._tool_list_fingerprint
    assert fingerprint(twin) == fingerprint(server)  # 전제: 설정(지문)이 같다
    assert tool_registry.mcp_tool_list_cache_key(twin) != key
    with pytest.raises(ValueError):  # 다른 서버의 키는 받지 않는다
        tool_registry.get_cached_mcp_tool_list(twin, key=key)
    with pytest.raises(ValueError):
        tool_registry.store_mcp_tool_list(twin, [], key=key)
    server.updated_at = server.updated_at + timedelta(seconds=1)
    assert tool_registry.mcp_tool_list_cache_key(server) != key
    server.updated_at = None  # 저장 전 행은 키는 있지만 캐시하지 않는다
    unsaved = tool_registry.mcp_tool_list_cache_key(server)
    assert unsaved is not None
    hash(unsaved)
    tool_registry.store_mcp_tool_list(server, [{"name": "x"}])
    assert tool_registry.get_cached_mcp_tool_list(server) is tool_registry.MCP_TOOL_LIST_MISS


def test_store_uses_the_key_computed_when_the_lookup_started() -> None:
    """조회 중 서버 설정이 바뀌어도 시작 시점 설정의 결과로 저장한다(재계산하지 않는다)."""
    server = _server("kosis")
    key = tool_registry.mcp_tool_list_cache_key(server)
    server.updated_at = server.updated_at + timedelta(seconds=1)  # 조회 중 설정 변경
    tool_registry.store_mcp_tool_list(server, [{"name": "old"}], key=key)
    assert tool_registry.get_cached_mcp_tool_list(server, key=key) == [{"name": "old"}]
    # 새 설정으로는 적중하지 않는다
    assert tool_registry.get_cached_mcp_tool_list(server) is tool_registry.MCP_TOOL_LIST_MISS


def test_name_format_contract() -> None:
    """biznotes가 역파싱하는 이름 규칙(정규화·64자 절단·접미사)을 잠근다."""
    tag = "abcd1234"
    normalized = tool_registry.mcp_tool_name(tag, "github.search/x", set())
    assert normalized == "mcp_abcd1234_github_search_x"
    long_name = "t" * 80
    base = tool_registry.mcp_tool_name(tag, long_name, set())
    assert len(base) == tool_registry.MAX_TOOL_NAME_LEN
    suffixed = tool_registry.mcp_tool_name(tag, long_name, {base})
    assert suffixed == base[: tool_registry.MAX_TOOL_NAME_LEN - 2] + "_1"
    assert tool_registry.mcp_tool_name(tag, long_name, {base, suffixed}).endswith("_2")


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        ({"permissions": {}}, True),
        ({"permissions": {"mcp": True}}, False),
        ({"permissions": {"http": True}}, False),
        ({"permissions": {"scripts": True}}, False),
        ({"permissions": "all"}, False),
        (None, False),
    ],
)
def test_skill_read_only_rule(manifest: Any, expected: bool) -> None:
    assert tool_registry.skill_manifest_is_read_only(manifest) is expected


def test_skill_label_prefers_frontmatter() -> None:
    wrapper = SimpleNamespace(name="wrapper", description="wd")
    version = SimpleNamespace(standard_metadata={"name": " pdf ", "description": ""})
    assert tool_registry.exposed_skill_label(version, wrapper) == ("pdf", "wd")
    old = SimpleNamespace(standard_metadata=None)
    assert tool_registry.exposed_skill_label(old, wrapper) == ("wrapper", "wd")


# ---------- 런타임 방어 ----------


@pytest.mark.parametrize("name", ["register_source", "http_request", "mcp_call", "call_agent"])
def test_safe_set_cannot_ungate_builtin_tools(
    world: World, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """수락 기준 4(음성 대조) — 안전 집합에 내장 도구 이름이 들어가도 게이트가 걸린다."""
    monkeypatch.setattr(tool_registry, "_safe_discovered_mcp_tools", {name})
    assert tool_registry.is_side_effect_tool(name) is True
    guards = loop._ReactGuards(
        token_budget=1000, cost_budget_usd=1.0, max_subagent_calls=1, aux_node_id="agent"
    )
    checkpoint = approval.evaluate_gate(
        world.ctx(),
        guards,
        name,
        {},
        {"id": "agent", "type": "react_agent"},
        messages=[],
        tool_log=[],
        aux_idx=0,
        iters_done=0,
        subagent_calls_in_node=0,
        remaining_calls=[],
    )
    assert checkpoint is not None


def test_safe_set_still_applies_to_discovered_names(monkeypatch: pytest.MonkeyPatch) -> None:
    name = "mcp_1234abcd_search"
    monkeypatch.setattr(tool_registry, "_safe_discovered_mcp_tools", set())
    assert tool_registry.is_side_effect_tool(name) is True
    tool_registry._safe_discovered_mcp_tools.add(name)
    assert tool_registry.is_side_effect_tool(name) is False


# ---------- 런타임 경로는 DeprecationWarning을 내지 않는다 ----------


async def test_runtime_paths_do_not_use_deprecated_names(world: World) -> None:
    """수락 기준 6 — 디스커버리·캐시 경로가 deprecated 이름을 거치지 않는다."""
    kosis = _server("kosis")
    world.add(kosis, ["search"])
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        await world.discover()
        await world.discover()  # 캐시 적중 경로
        tool_registry.is_side_effect_tool("mcp_x_y")


# ---------- load_skill_files 경고 (B) ----------


def test_missing_manifest_warns_once_and_keeps_empty_manifest() -> None:
    files = {"SKILL.md": SKILL_MD, "references/a.md": b"a"}
    with structlog.testing.capture_logs() as logs:
        loaded = load_skill_files(files)
    assert loaded.manifest == {}
    assert loaded.references == {"references/a.md": "a"}
    events = [e for e in logs if e["event"] == "skill.manifest.missing"]
    assert len(events) == 1
    assert events[0]["files"] == ["SKILL.md", "references/a.md"]
    assert events[0]["file_count"] == 2
    assert events[0]["manifest_filename"]


def test_undecodable_reference_warns_and_is_skipped() -> None:
    from biznotes_core.config import get_settings

    manifest = get_settings().skill_manifest_filename
    files = {
        "SKILL.md": SKILL_MD,
        manifest: b"{}",
        "references/ok.md": b"ok",
        "references/bad.bin": b"\xff\xfe\x00",
    }
    with structlog.testing.capture_logs() as logs:
        loaded = load_skill_files(files)
    assert loaded.references == {"references/ok.md": "ok"}
    assert [(e["event"], e.get("path")) for e in logs] == [
        ("skill.reference.undecodable", "references/bad.bin")
    ]


def test_uuid_tag_matches_runtime_rule() -> None:
    sid = uuid.UUID("12345678-9abc-def0-1234-56789abcdef0")
    assert tool_registry.mcp_server_tag(sid) == "12345678"
    assert tool_registry.mcp_server_tag(str(sid)) == "12345678"
