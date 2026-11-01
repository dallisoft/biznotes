"""직접 부작용 노드(`http`·`mcp_tool`) 승인 — DB 없이 보는 판정 규칙 (#153 PR-A).

실제 승인·재개 흐름(호출 수 실측, 소비 CAS, 만료 sweep)은
`tests/test_direct_node_approval_pg.py`가 본다.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from biznotes_core.services.agents import graph, policy
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError, UntrustedResumeError
from biznotes_core.services.agents.nodes.composite import _direct_request_hash
from biznotes_core.services.agents.react import approval
from biznotes_core.services.agents.tool_registry import sanitize_for_approval_card

K = approval.CheckpointKeys


class FakeDb:
    def __init__(self, scope: str | None) -> None:
        self.agent = None if scope is None else SimpleNamespace(scope=scope)
        self.kwargs: list[dict[str, Any]] = []

    async def get(self, model: Any, key: Any, **kw: Any) -> Any:
        self.kwargs.append(kw)
        return self.agent


def _ctx(scope: str | None, *, agent: bool = True) -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=FakeDb(scope),  # type: ignore[arg-type]
        params={},
        agent_id=uuid.uuid4() if agent else None,
    )


def _http(**kw: Any) -> dict[str, Any]:
    return {"id": "call", "type": "http", "url": "https://api.example.com/hook", **kw}


# ---------- URL 대상 리터럴 규칙 (재리뷰 R2) ----------


@pytest.mark.parametrize(
    ("url", "literal"),
    [
        ("https://api.example.com/v1/{params.path}?q={params.q}", True),
        ("http://api.example.com:8443/x", True),
        ("https://api.example.com{params.p}", False),  # `@evil.com`으로 host를 바꾼다
        ("https://api.example.com:{params.port}/", False),
        ("https://{params.host}/x", False),
        ("https://user:{params.pw}@api.example.com/", False),
        ("{params.base}/x", False),
        ("{params.scheme}://api.example.com/", False),
        ("ftp://api.example.com/", False),
        ("https:///path-only", False),
        (None, False),
    ],
)
def test_url_target_rule(url: Any, literal: bool) -> None:
    assert policy.url_target_is_literal(url) is literal


# ---------- 런타임 판정 (A-1) ----------


@pytest.mark.parametrize(
    ("node", "scope", "agent", "gated"),
    [
        (_http(requires_approval=False), "system", True, False),
        (_http(), "system", True, True),  # 기본은 승인 필요
        (_http(requires_approval="false"), "system", True, True),  # 리터럴 False만
        (_http(requires_approval=0), "system", True, True),
        (_http(requires_approval=False), "user", True, True),
        (_http(requires_approval=False), "group", True, True),
        (_http(requires_approval=False), None, True, True),  # 정의 행 없음(삭제 경합)
        # 정의 없는 ctx — 관리자 fallback 없음
        (_http(requires_approval=False), "system", False, True),
        # 검증기를 거치지 않은 정의의 host 템플릿도 실행 시점에 게이트한다(재리뷰 R2)
        (
            _http(requires_approval=False, url="https://api.example.com{params.p}"),
            "system",
            True,
            True,
        ),
    ],
)
async def test_runtime_rule(node: dict[str, Any], scope: Any, agent: bool, gated: bool) -> None:
    ctx = _ctx(scope, agent=agent)
    assert await policy.direct_node_requires_approval(ctx, node) is gated


async def test_runtime_rule_rereads_the_definition_scope() -> None:
    ctx = _ctx("system")
    await policy.direct_node_requires_approval(ctx, _http(requires_approval=False))
    assert ctx.db.kwargs == [{"populate_existing": True}]


async def test_gate_decision_fails_closed_on_errors() -> None:
    """판정 중 오류(잘못된 server_id 등)는 게이트로 본다(재리뷰 R1)."""
    node = {
        "id": "m",
        "type": "mcp_tool",
        "server_id": "not-a-uuid",
        "tool_name": "t",
        "requires_approval": False,
    }
    assert await policy.direct_node_needs_gate(_ctx("system"), node) is True


# ---------- 저장 헬퍼 ----------


def _violations(nodes: list[dict[str, Any]], scope: str, edges: list[list[str]] | None = None):
    return policy.find_direct_node_approval_violations(nodes, edges or [], scope)


def test_save_helper_rejects_bad_settings() -> None:
    assert _violations([_http(requires_approval="no")], "system") == [
        "call: requires_approval은 true/false여야 합니다"
    ]
    assert _violations([_http(requires_approval=False)], "user") == [
        "call: requires_approval=false는 system 정의에서만 둘 수 있습니다"
    ]
    for url in ("https://api.example.com{params.p}", "{params.base}/x", "https://a.com:{p}/"):
        found = _violations([_http(requires_approval=False, url=url)], "system")
        assert found and "리터럴" in found[0], url
    assert _violations([_http(requires_approval=False)], "system") == []
    assert _violations([_http()], "user") == []  # 승인 필요(기본)는 어디서나 저장할 수 있다


def test_save_helper_rejects_gated_nodes_in_fan_out() -> None:
    """수락 기준 3 — 승인이 필요한 직접 노드는 병렬(fan-out) 그래프에 둘 수 없다."""
    nodes = [
        {"id": "start", "type": "constant", "value": {}},
        _http(id="a"),
        _http(id="b", requires_approval=False),
    ]
    edges = [["start", "a"], ["start", "b"]]
    assert _violations(nodes, "system", edges) == [
        "a: 승인이 필요한 노드는 병렬(fan-out) 그래프에 둘 수 없습니다"
    ]
    assert _violations([*nodes[:2], _http(id="b")], "system", edges) == [
        "a: 승인이 필요한 노드는 병렬(fan-out) 그래프에 둘 수 없습니다",
        "b: 승인이 필요한 노드는 병렬(fan-out) 그래프에 둘 수 없습니다",
    ]
    # 순차 경로면 허용한다
    assert _violations(nodes, "system", [["start", "a"], ["a", "b"]]) == []


def test_kind_nodes_carry_the_setting() -> None:
    """수락 기준 4 — mcp kind도 `requires_approval`을 노드로 전달한다(tool·plugin은 http 블록)."""
    mcp_nodes, _ = graph.kind_to_nodes(
        "mcp", {"server_id": str(uuid.uuid4()), "tool_name": "t", "requires_approval": False}
    )
    assert mcp_nodes[0]["requires_approval"] is False
    default_nodes, _ = graph.kind_to_nodes(
        "mcp", {"server_id": str(uuid.uuid4()), "tool_name": "t"}
    )
    assert "requires_approval" not in default_nodes[0]
    tool_nodes, _ = graph.kind_to_nodes(
        "tool", {"http": {"url": "https://a.example.com/", "requires_approval": False}}
    )
    assert tool_nodes[0]["requires_approval"] is False


# ---------- 승인 카드·해시 (A-3) ----------


def test_card_masks_secrets_in_the_request() -> None:
    """수락 기준 6 — userinfo·쿼리 비밀·Authorization·Cookie가 카드에서 가려진다."""
    card = sanitize_for_approval_card(
        "http_request",
        {
            "method": "POST",
            "url": "https://user:pw@api.example.com/x?api_key=s3cret&page=2",
            "headers": {"Authorization": "Bearer t", "Cookie": "sid=1", "X-Trace": "ok"},
            "body": {"password": "p", "text": "hello"},
        },
    )
    assert card["url"] == "https://api.example.com/x?api_key=%2A%2A%2A&page=2"
    assert card["headers"]["Authorization"] != "Bearer t"
    assert card["headers"]["Cookie"] != "sid=1"
    assert card["headers"]["X-Trace"] == "ok"
    assert card["body"]["password"] != "p" and card["body"]["text"] == "hello"


def test_request_hash_is_canonical_and_fails_closed() -> None:
    node = _http()
    a = _direct_request_hash(node, {"url": "u", "headers": {"b": 1, "a": 2}})
    b = _direct_request_hash(node, {"headers": {"a": 2, "b": 1}, "url": "u"})
    assert a == b
    assert a != _direct_request_hash(node, {"url": "u2", "headers": {"b": 1, "a": 2}})
    assert a != _direct_request_hash(
        {**node, "id": "other"}, {"url": "u", "headers": {"b": 1, "a": 2}}
    )
    with pytest.raises(AgentRuntimeError, match="직렬화"):
        _direct_request_hash(node, {"body": float("nan")})
    with pytest.raises(AgentRuntimeError, match="직렬화"):
        _direct_request_hash(node, {"body": object()})


# ---------- 앵커 재개 (A-2) ----------


def _anchored(ctx: RunContext, checkpoint: dict[str, Any]) -> RunContext:
    approval.restore_run_context(ctx, checkpoint)
    return ctx


def test_direct_resume_is_anchor_only_and_single_use() -> None:
    ctx = _ctx("system")
    checkpoint = approval.build_direct_node_checkpoint(
        ctx, "call", tool_name="http:POST", arguments_summary={}, request_hash="h"
    )
    checkpoint[K.REACT_APPROVAL_STATUS] = "approved"
    resumed = _anchored(_ctx("system"), checkpoint)

    assert approval.claim_direct_resume(resumed, "other") is None  # 다른 노드는 새 게이트
    assert approval.claim_direct_resume(resumed, "call") is checkpoint
    assert approval.claim_direct_resume(resumed, "call") is None  # 한 번만


def test_react_checkpoint_cannot_resume_a_direct_node() -> None:
    """앵커가 이 노드를 가리키지만 직접 노드 형식이 아니면(react·위조) 거부한다."""
    react_like = {K.REACT_NODE_ID: "call", K.REACT_APPROVAL_STATUS: "approved"}
    resumed = _anchored(_ctx("system"), react_like)
    with pytest.raises(UntrustedResumeError):
        approval.claim_direct_resume(resumed, "call")


def test_checkpoint_carries_the_run_context_snapshot() -> None:
    """수락 기준 2(단위) — 직접 노드 체크포인트도 react와 같은 RunContext 스냅샷을 싣는다."""
    ctx = _ctx("system")
    ctx.cost_total[0] = 1.25
    ctx.artifact_keys.append({"key": "k"})
    ctx.register_source_calls[0] = 2
    checkpoint = approval.build_direct_node_checkpoint(
        ctx, "call", tool_name="http:POST", arguments_summary={}, request_hash="h"
    )
    restored = _anchored(_ctx("system"), {**checkpoint, K.REACT_APPROVAL_STATUS: "approved"})
    assert restored.cost_total[0] == 1.25
    assert restored.artifact_keys == [{"key": "k"}]
    assert restored.register_source_calls[0] == 2
    assert set(approval.snapshot_run_context(ctx)) <= set(checkpoint)


def test_gate_inside_a_fan_out_graph_is_a_policy_error() -> None:
    """사전 차단 뒤 판정이 바뀌어 fan-out 안에서 게이트하려 하면 정책 오류로 끝난다(재리뷰 R1)."""
    from biznotes_core.services.agents.errors import (
        GatedNodeInFanOutError,
        SubagentApprovalNotSupportedError,
    )
    from biznotes_core.services.agents.nodes.composite import _direct_node_gate

    ctx = _ctx("system")
    ctx.in_fan_out_graph = True
    with pytest.raises(GatedNodeInFanOutError) as caught:
        _direct_node_gate(
            ctx, _http(), request={"url": "u"}, tool_name="http:POST", arguments_summary={}
        )
    # error_handler·재시도가 흡수하지 않는 계열이다
    assert isinstance(caught.value, SubagentApprovalNotSupportedError)


# ---------- save_sources (#403) — 같은 직접 노드 규칙 ----------


def _save(**kw: Any) -> dict[str, Any]:
    return {"id": "save", "type": "save_sources", "items": "{list.text}", "namespace": "ns", **kw}


def test_save_sources_is_a_direct_side_effect_node() -> None:
    assert policy.DIRECT_SIDE_EFFECT_NODE_TYPES >= {"http", "mcp_tool", "save_sources"}


@pytest.mark.parametrize(
    ("node", "scope", "agent", "gated"),
    [
        (_save(requires_approval=False), "system", True, False),
        (_save(), "system", True, True),  # 기본은 승인 필요
        (_save(requires_approval="false"), "system", True, True),
        (_save(requires_approval=False), "user", True, True),
        (_save(requires_approval=False), "group", True, True),
        (_save(requires_approval=False), None, True, True),  # 정의 행 없음
        (_save(requires_approval=False), "system", False, True),  # 정의 없는 ctx
    ],
)
async def test_save_sources_runtime_rule(
    node: dict[str, Any], scope: Any, agent: bool, gated: bool
) -> None:
    assert await policy.direct_node_needs_gate(_ctx(scope, agent=agent), node) is gated


def test_save_sources_save_helper_rules() -> None:
    assert _violations([_save(requires_approval=False)], "system") == []
    assert _violations([_save()], "user") == []
    assert _violations([_save(requires_approval=False)], "user") == [
        "save: requires_approval=false는 system 정의에서만 둘 수 있습니다"
    ]
    assert _violations([_save(requires_approval="no")], "system") == [
        "save: requires_approval은 true/false여야 합니다"
    ]
    nodes = [{"id": "start", "type": "constant", "value": {}}, _save(id="a"), _http(id="b")]
    assert _violations(nodes, "system", [["start", "a"], ["start", "b"]]) == [
        "a: 승인이 필요한 노드는 병렬(fan-out) 그래프에 둘 수 없습니다",
        "b: 승인이 필요한 노드는 병렬(fan-out) 그래프에 둘 수 없습니다",
    ]
