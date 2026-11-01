"""`tool_registry` deprecated 별칭 제거 (#210, #139 B·#152 3단계).

옛 MCP 승인 상수·함수(#139 B, v0.10.13부터 deprecated)와 소비 제품이 쓰던 비공개 이름 별칭
(#152, v0.10.15부터 deprecated)을 지웠다. 승인 판정의 정본은 DB(`MCPServerTool`)와
`mcp_exposure.approved_tools`·`mcp_exposure_for`·`mcp_audience`다.
"""

import pathlib
import re

import pytest

import biznotes_core
from biznotes_core.services.agents import tool_registry

# #139 B — 옛 MCP 승인 표면
MCP_APPROVAL_NAMES = (
    "MCP_READ_ONLY_TOOLS",
    "mcp_server_allowlisted",
    "mcp_tool_allowlisted",
    "mcp_discovery_allowed_for",
)
# #152 — 비공개 이름 별칭
PRIVATE_ALIAS_NAMES = (
    "_MCP_TOOL_PREFIX",
    "_MAX_TOOL_NAME_LEN",
    "_CACHE_MISS",
    "_SAFE_DISCOVERED_MCP_TOOLS",
    "_mcp_server_fingerprint",
    "_cached_mcp_tool_list",
    "_store_mcp_tool_list",
    "_mcp_tool_name",
    "_skill_manifest_is_read_only",
    "_exposed_skill_label",
)
# 별칭을 받치던 내부 보조물
INTERNAL_NAMES = (
    "_LEGACY_MCP_READ_ONLY_TOOLS",
    "_MCP_ALLOWLIST_DEPRECATION",
    "_PRIVATE_CONSTANT_ALIASES",
    "_PRIVATE_ALIAS_DEPRECATION",
    "_warn_private_alias",
)
REMOVED = MCP_APPROVAL_NAMES + PRIVATE_ALIAS_NAMES + INTERNAL_NAMES


@pytest.mark.parametrize("name", REMOVED)
def test_removed_name_is_gone(name: str) -> None:
    assert name not in vars(tool_registry)
    with pytest.raises(AttributeError):
        getattr(tool_registry, name)
    with pytest.raises(ImportError):
        exec(f"from biznotes_core.services.agents.tool_registry import {name}", {})


def test_module_getattr_hook_is_gone() -> None:
    """PEP 562 훅이 없어야 모르는 이름이 기본 `AttributeError`로 떨어진다."""
    assert "__getattr__" not in vars(tool_registry)
    with pytest.raises(AttributeError):
        tool_registry.MCP_READ_ONLY_TOOL  # noqa: B018


def test_runtime_safe_set_and_admin_audience_stay() -> None:
    """런타임이 쓰는 내부 상태는 남는다 — 별칭만 지웠다."""
    assert isinstance(tool_registry._safe_discovered_mcp_tools, set)
    assert tool_registry._is_admin_audience("admin", None)


def test_core_source_never_mentions_the_removed_names() -> None:
    """코어 어디에도 지운 이름이 식별자·문자열로 남지 않는다(#168 선례)."""
    pattern = re.compile(r"(?<![\w])(" + "|".join(map(re.escape, REMOVED)) + r")(?![\w])")
    root = pathlib.Path(biznotes_core.__file__).parent
    hits = [
        f"{path.relative_to(root)}:{no}: {line.strip()}"
        for path in root.rglob("*.py")
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert hits == []
