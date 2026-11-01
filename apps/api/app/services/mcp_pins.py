"""MCP 도구 승인 스펙 고정 판정 — 헬스체크·승인 CLI·시드 공용(코어 v0.21.0 #293).

코어는 승인 행의 `approved_spec_hash`가 현재 스펙의 `mcp_tool_spec_hash`와 같을 때만 도구를
노출한다. 해시가 없는 행(업그레이드 전 승인 포함)은 노출되지 않는다. 판정 규칙은 코어
`tool_pin_states` docstring과 같고, 해시 계산은 복제하지 않는다 — 코어 `mcp_tool_spec_hash`가
정본이다.
"""

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from biznotes_core.services.agents.mcp_tool_spec import MCPToolSpecRejected, mcp_tool_spec_hash

# approved = 노출됨 · unpinned = 승인했지만 해시 없음(재승인 필요) · changed = 승인 뒤 스펙 바뀜
# · not_approved = read-only 승인 행 없음(`:rw` 승인도 노출하지 않으므로 여기에 든다)
PinState = Literal["approved", "unpinned", "changed", "not_approved"]


def spec_hash_or_none(tool: Mapping[str, Any]) -> str | None:
    """`list_tools` 항목의 현재 스펙 해시. 정화가 거부하는 도구는 고정할 수 없어 `None`."""
    try:
        return mcp_tool_spec_hash(tool)
    except MCPToolSpecRejected:
        return None


def pin_state(name: str, current_hash: str | None, pins: Mapping[str, str | None]) -> PinState:
    """`pins`는 코어 `tool_pin_states`의 서버 하나 몫(read-only 승인 이름 → 고정 해시|None)."""
    if name not in pins:
        return "not_approved"
    pinned = pins[name]
    if pinned is None:
        return "unpinned"
    return "approved" if pinned == current_hash else "changed"


def current_spec_hashes(
    tools: Iterable[Mapping[str, Any]], names: Iterable[str]
) -> dict[str, str]:
    """`list_tools` 결과에서 `names`에 든 도구의 현재 해시만 고른다(없거나 거부된 도구는 뺀다)."""
    wanted = set(names)
    hashes: dict[str, str] = {}
    for tool in tools:
        name = str(tool.get("name") or "")
        if name in wanted:
            spec_hash = spec_hash_or_none(tool)
            if spec_hash is not None:
                hashes[name] = spec_hash
    return hashes
