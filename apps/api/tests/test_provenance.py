"""Provenance v1 저장/공개 계약과 방어적 빌더 테스트."""

from app.schemas.provenance import ProvenanceOut
from app.services.provenance import (
    _build_execution_log_item,
    _parse_mcp_exposed_name,
    build_chat_provenance,
    public_evidence_items,
)


def test_stored_and_public_key_whitelists():
    stored = build_chat_provenance(
        tool_calls=[{"tool": "rag_search", "status": "succeeded", "arguments": {"q": "secret"}}],
        tool_evidence=[
            {
                "kind": "skill",
                "name": "검토 스킬",
                "version": 3,
                "status": "succeeded",
                "skill_version_id": "sv-1",
                "package_hash": "hash",
                "agent_id": "agent-1",
                "unexpected": "drop-me",
            }
        ],
        citations=[],
        mode="autonomous",
    )

    assert set(stored) == {
        "v",
        "mode",
        "recorded_at",
        "run_id",
        "artifact_name",
        "content_sha256",
        "items",
    }
    assert set(stored["items"][0]) == {
        "kind",
        "name",
        "version",
        "status",
        "skill_version_id",
        "package_hash",
        "agent_id",
    }
    assert set(stored["items"][1]) == {"kind", "tool", "status"}


def test_public_schema_drops_internal_fields():
    stored = build_chat_provenance(
        tool_calls=[],
        tool_evidence=[
            {
                "kind": "skill",
                "name": "검토 스킬",
                "version": 3,
                "status": "succeeded",
                "skill_version_id": "sv-1",
                "package_hash": "hash",
                "agent_id": "agent-1",
            }
        ],
        citations=[],
        mode="autonomous",
    )
    stored["content_sha256"] = "a" * 64

    public = ProvenanceOut.model_validate(stored).model_dump(mode="json")
    assert "content_sha256" not in public
    assert "skill_version_id" not in public["items"][0]
    assert "package_hash" not in public["items"][0]
    assert "agent_id" not in public["items"][0]
    assert public["items"][0]["version"] == 3

    evidence = public_evidence_items(stored["items"])
    assert not {"skill_version_id", "package_hash", "agent_id"} & set(evidence[0])
    assert evidence[0]["name"] == "검토 스킬"
    assert evidence[0]["version"] == 3


def test_caps_and_truncation():
    stored = build_chat_provenance(
        tool_calls=[],
        tool_evidence=[
            {
                "kind": "source",
                "source_id": f"source-{index}",
                "title": "가" * 250,
                "status": "succeeded",
            }
            for index in range(60)
        ]
        + [
            {
                "kind": "skill",
                "name": "후순위 도착 스킬",
                "version": 1,
                "status": "succeeded",
            },
            {
                "kind": "mcp",
                "server": "archhub",
                "tool": "find_region",
                "status": "failed",
            },
        ],
        citations=[],
        mode="autonomous",
    )

    assert len(stored["items"]) == 50
    assert [item["kind"] for item in stored["items"][:2]] == ["skill", "mcp"]
    assert all(len(item["title"]) == 200 for item in stored["items"][2:])


def test_caps_and_truncation_prioritizes_tool_over_source():
    """실패한 tool 항목은 다수의 source 인용 evidence에 밀려 잘리지 않는다."""
    stored = build_chat_provenance(
        tool_calls=[{"tool": "rag_search", "status": "failed"}],
        tool_evidence=[
            {"kind": "source", "source_id": f"source-{index}", "status": "succeeded"}
            for index in range(60)
        ],
        citations=[],
        mode="autonomous",
    )

    assert len(stored["items"]) == 50
    assert stored["items"][0] == {"kind": "tool", "status": "failed", "tool": "rag_search"}


def test_public_evidence_items_caps_at_fifty():
    from app.services.provenance import public_evidence_items

    evidence = [
        {"kind": "mcp", "server": "archhub", "tool": f"tool-{i}", "status": "succeeded"}
        for i in range(60)
    ]

    assert len(public_evidence_items(evidence)) == 50


def test_failed_mcp_dedup_matches_numbered_suffix_without_enumeration():
    """`mcp_tool_name`이 실제로 부여한 충돌 접미사(`_7`)도 O(1) 재구성으로 매칭된다."""
    from biznotes_core.services.agents.tool_registry import MAX_TOOL_NAME_LEN, mcp_tool_name

    raw_tool = "find.region/" + "x" * 80
    base = mcp_tool_name("deadbeef", raw_tool, {})
    suffix = "_7"
    suffixed = base[: MAX_TOOL_NAME_LEN - len(suffix)] + suffix
    evidence = [{"kind": "mcp", "tool": raw_tool, "status": "failed"}]

    stored = build_chat_provenance(
        tool_calls=[{"tool": suffixed, "status": "failed"}],
        tool_evidence=evidence,
        citations=[],
        mode="autonomous",
    )

    assert stored["items"] == [{"kind": "mcp", "status": "failed", "tool": raw_tool[:200]}]


def test_failed_mcp_dedup_uses_exposed_name_rules_only():
    raw_tool = "find.region/" + "x" * 80
    evidence = [{"kind": "mcp", "tool": raw_tool, "status": "failed"}]
    from biznotes_core.services.agents.tool_registry import mcp_tool_name  # 공개 이름(#152)

    exposed = mcp_tool_name("deadbeef", raw_tool, {})
    stored = build_chat_provenance(
        tool_calls=[
            {"tool": "rag_search", "status": "failed"},
            {"tool": exposed, "status": "failed"},
        ],
        tool_evidence=evidence,
        citations=[],
        mode="autonomous",
    )

    assert stored["items"] == [
        {"kind": "mcp", "status": "failed", "tool": raw_tool[:200]},
        {"kind": "tool", "status": "failed", "tool": "rag_search"},
    ]


# --- AC-9: origin=agent 스냅샷 typed extractor 순수 함수 단위 --------------------------


def test_extract_parses_mcp_tool_name_and_server_tag():
    from biznotes_core.services.agents.tool_registry import mcp_tool_name  # 공개 이름(#152)

    exposed = mcp_tool_name("deadbeef", "find_region", {})
    parsed = _parse_mcp_exposed_name(exposed)
    assert parsed == ("deadbeef", "find_region")


def test_extract_rejects_non_mcp_and_malformed_tool_names():
    assert _parse_mcp_exposed_name("rag_search") is None
    assert _parse_mcp_exposed_name("mcp_short") is None
    assert _parse_mcp_exposed_name("mcp_deadbeefXfind_region") is None  # 구분자 '_' 아님


def test_extract_mcp_tool_call_item_joins_server_name():
    item = _build_execution_log_item(
        "mcp_deadbeef_find_region", "succeeded", {"deadbeef": "archhub"}
    )
    assert item == {
        "kind": "mcp",
        "server": "archhub",
        "tool": "find_region",
        "status": "succeeded",
    }


def test_extract_mcp_tool_call_item_server_name_missing_from_map():
    """서버가 삭제/미매칭이어도 항목 자체는 생략하지 않는다 — server만 비운다."""
    item = _build_execution_log_item("mcp_deadbeef_find_region", "failed", {})
    assert item == {"kind": "mcp", "tool": "find_region", "status": "failed"}


def test_extract_non_mcp_tool_call_item_is_generic_tool_kind():
    item = _build_execution_log_item("rag_search", "succeeded", {})
    assert item == {"kind": "tool", "tool": "rag_search", "status": "succeeded"}


def test_extract_unknown_status_for_legacy_log_without_call_status():
    """`call_status` 키 자체가 없는 과거 ExecutionLog는 unknown으로 판정한다(R2-02)."""
    from app.services.provenance import _item, _status

    data: dict = {}  # input_data에 call_status 키가 없는 과거 행을 흉내
    call_status = _status(data.get("call_status"))
    assert call_status == "unknown"
    item = _item({"kind": "tool", "tool": "legacy_tool", "status": call_status})
    assert item == {"kind": "tool", "tool": "legacy_tool", "status": "unknown"}


def test_builder_never_raises():
    class BrokenString:
        def __str__(self):
            raise RuntimeError("cannot stringify")

    result = build_chat_provenance(
        tool_calls=[None, {"tool": BrokenString(), "status": object()}],
        tool_evidence=[None, {"kind": "mcp", "server": BrokenString()}],
        citations=object(),
        mode=[],  # type: ignore[arg-type]
    )

    assert result["v"] == 1
    assert result["mode"] == "unrecorded"
    assert isinstance(result["items"], list)
