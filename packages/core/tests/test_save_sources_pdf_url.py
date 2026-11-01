"""`save_sources` URL(PDF) 항목(#403 PR-B) — DB 없이 보는 규칙: 허용 호스트 저장 검사·교집합·카드.

실제 받아 오기(관문·리다이렉트·크기·매직)와 생성·인제스트는
`tests/test_save_sources_pdf_url_pg.py`가 본다.
"""

import uuid
from typing import Any

import pytest

from biznotes_core.core.ssrf import host_matches
from biznotes_core.services.agents import policy
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError
from biznotes_core.services.agents.nodes import save_sources


def _node(**kw: Any) -> dict[str, Any]:
    return {
        "id": "save",
        "type": "save_sources",
        "items": "{list.text}",
        "namespace": "ns",
        "requires_approval": False,
        **kw,
    }


@pytest.mark.parametrize(
    "hosts",
    [["stock.pstatic.net"], ["*.pstatic.net", "cdn.example.com"], ["주식.한국"], ["93.184.216.34"]],
)
def test_allowed_hosts_literals_are_accepted(hosts: list[str]) -> None:
    found = policy.find_direct_node_approval_violations([_node(allowed_hosts=hosts)], [], "system")
    assert found == []


@pytest.mark.parametrize(
    "hosts",
    [
        [],  # 비어 있으면 안 된다 — url 항목이 없으면 키를 뺀다
        "stock.pstatic.net",  # 목록이어야 한다
        ["{params.host}"],  # 템플릿으로 바꿀 수 없다
        ["*.{params.domain}"],
        ["https://stock.pstatic.net"],
        ["stock.pstatic.net:443"],
        ["stock.pstatic.net/path"],
        ["user@stock.pstatic.net"],
        ["*"],
        ["*.*.pstatic.net"],
        ["a.*.net"],
        [".pstatic.net"],
        ["pstatic..net"],
        ["pstatic.net."],
        ["a b"],
        [3],
    ],
)
def test_allowed_hosts_must_be_non_empty_literal_hosts(hosts: Any) -> None:
    found = policy.find_direct_node_approval_violations([_node(allowed_hosts=hosts)], [], "system")
    assert found and all(f.startswith("save: ") and "allowed_hosts" in f for f in found)


async def test_runtime_rejects_bad_allowed_hosts_before_any_write() -> None:
    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=None,  # type: ignore[arg-type] — 쓰기 전에 실패한다
        params={},
        user_id=uuid.uuid4(),
    )
    with pytest.raises(AgentRuntimeError, match="allowed_hosts"):
        await save_sources._exec_save_sources(
            ctx, _node(allowed_hosts=["{params.h}"]), {"list": {"text": "[]"}}
        )


@pytest.mark.parametrize(
    ("node_hosts", "global_hosts", "expected"),
    [
        (["cdn.example.test"], (), ("cdn.example.test",)),  # 전역 목록이 없으면 노드 목록
        (["*.example.test"], ("cdn.example.test", "other.test"), ("cdn.example.test",)),
        (["cdn.example.test"], ("*.example.test",), ("cdn.example.test",)),
        (["*.example.test"], ("*.cdn.example.test",), ("*.cdn.example.test",)),
        (["*.cdn.example.test"], ("*.example.test",), ("*.cdn.example.test",)),
        (["*.example.test"], ("*.example.test",), ("*.example.test",)),
        (["CDN.Example.Test"], ("cdn.example.test",), ("cdn.example.test",)),
        (["example.test"], ("*.example.test",), ()),  # `*.`는 그 도메인 자체를 덮지 않는다
        (["*.example.test"], ("*.other.test", "evil.test"), ()),
        (["cdn.example.test"], ("cdn.example.test.evil.net",), ()),
    ],
)
def test_effective_hosts_are_the_intersection(
    node_hosts: list[str], global_hosts: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    met = save_sources._effective_hosts(node_hosts, global_hosts)
    assert met == expected
    # 교집합 패턴이 허용하는 호스트는 두 목록 모두가 허용한다
    for host in ("cdn.example.test", "a.cdn.example.test", "example.test", "evil.test"):
        if host_matches(host, met):
            assert host_matches(host, node_hosts)
            assert not global_hosts or host_matches(host, global_hosts)


def test_approval_card_lists_hosts_of_the_first_five_valid_items() -> None:
    items: list[Any] = [
        {"title": "a", "external_id": "a", "url": "https://one.example.test/a.pdf?token=s"},
        "문자열",  # 검증을 통과하지 못한 항목은 싣지 않고 세지도 않는다
        {"title": "b", "external_id": "b", "url": "https://two.example.test/b.pdf"},
        {"title": "c", "external_id": "c", "content": "본문"},
        {"title": "d", "external_id": "d", "url": "https://one.example.test/d.pdf"},
        {"title": "e", "external_id": "e", "url": "https://three.example.test/e.pdf"},
        {"title": "f", "external_id": "f", "url": "https://six.example.test/f.pdf"},  # 6번째
    ]
    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=None,  # type: ignore[arg-type] — 카드는 DB를 쓰지 않는다
        params={},
    )
    parsed = [save_sources._parse_item(raw) for raw in items]
    card = save_sources._approval_card(ctx, _node(), parsed, len(items))
    assert card["titles"] == ["a", "b", "c", "d", "e"]
    assert card["hosts"] == ["one.example.test", "two.example.test", "three.example.test"]
    assert "token" not in str(card)  # URL 경로·쿼리는 싣지 않는다
