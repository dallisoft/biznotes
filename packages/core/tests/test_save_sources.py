"""`save_sources` 노드(#403 PR-A) — DB 없이 보는 계약: 항목 스키마·설정·저장 헬퍼·실행 전 검사.

실제 생성·중복·용량·권한·동시 실행·스케줄 흐름은 `tests/test_save_sources_pg.py`가 본다.
"""

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from biznotes_core.config import CoreSettings
from biznotes_core.schemas.source import SaveSourceItem
from biznotes_core.services.agents import policy
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError
from biznotes_core.services.agents.nodes import save_sources

# ---------- 항목 스키마 ----------


def _item(**kw: Any) -> dict[str, Any]:
    return {"title": "리포트", "external_id": "r-1", "content": "본문", **kw}


@pytest.mark.parametrize(
    "raw",
    [
        _item(),
        _item(mime_type="text/plain"),
        _item(mime_type="text/markdown"),
        _item(external_id="A-z_0.9~:/=+-" * 2),
        _item(external_id="x" * 200),
        _item(title="t" * 500),
        _item(content="c" * 500_000),
        _item(unknown="무시", nested={"a": 1}),  # 모르는 키는 무시한다
        {
            "title": "pdf",
            "external_id": "p-1",
            "url": "https://stock.example.com/a.pdf",
            "mime_type": "application/pdf",
        },
        {"title": "pdf", "external_id": "p-1", "url": "https://stock.example.com/a.pdf"},
    ],
)
def test_item_schema_accepts(raw: dict[str, Any]) -> None:
    item = SaveSourceItem.model_validate(raw)
    assert item.external_id == raw["external_id"]
    assert not hasattr(item, "unknown")


@pytest.mark.parametrize(
    "raw",
    [
        {"external_id": "r-1", "content": "본문"},  # title 없음
        _item(title=""),
        _item(title="t" * 501),
        {"title": "t", "content": "본문"},  # external_id 없음
        _item(external_id=""),
        _item(external_id="x" * 201),
        _item(external_id="공백 있음"),
        _item(external_id="a#b"),
        _item(external_id="a?b"),
        _item(external_id="a\n"),  # 끝 줄바꿈도 글자다
        _item(external_id=12345),  # 문자열만
        {"title": "t", "external_id": "r"},  # url·content 둘 다 없음
        _item(url="https://a.example.com/x.pdf"),  # 둘 다
        _item(content=""),
        _item(content="   \n\t"),
        _item(content="c" * 500_001),
        _item(mime_type="application/pdf"),  # content는 텍스트만
        _item(mime_type="text/html"),
        {"title": "t", "external_id": "r", "url": "http://a.example.com/x.pdf"},  # https만
        {"title": "t", "external_id": "r", "url": "https:///x.pdf"},
        {"title": "t", "external_id": "r", "url": "https://a.example.com/" + "x" * 2000},
        {
            "title": "t",
            "external_id": "r",
            "url": "https://a.example.com/x",
            "mime_type": "text/plain",
        },
        "문자열 항목",
    ],
)
def test_item_schema_rejects(raw: Any) -> None:
    with pytest.raises(ValidationError):
        SaveSourceItem.model_validate(raw)


def test_node_cleans_title_before_validation() -> None:
    """제목은 한 줄로 접고 보이지 않는 글자를 지운 뒤 500자로 자른다 — 그다음 1..500을 본다."""
    item, reason = save_sources._parse_item(_item(title="  첫 줄\n둘째\u200b 줄\x00 " + "x" * 600))
    assert reason is None and item is not None
    assert item.title.startswith("첫 줄 둘째 줄 ")
    assert len(item.title) == 500
    blank, reason = save_sources._parse_item(_item(title="\u200b\n "))
    assert blank is None and reason
    not_dict, reason = save_sources._parse_item(["x"])
    assert not_dict is None and reason


# ---------- 중복 키·고정 코드 ----------


def test_external_ref_format() -> None:
    assert save_sources.mcp_external_ref("naver-research", "a/b:1") == "mcp:naver-research:a/b:1"
    longest = save_sources.mcp_external_ref("n" * 64, "x" * 200)
    assert len(longest) <= 512


def test_failure_codes_and_messages_are_fixed() -> None:
    assert set(save_sources.SAVE_SOURCE_FAILURE_MESSAGES) == {
        "invalid_item",
        "host_not_allowed",
        "ssrf_blocked",
        "fetch_failed",
        "too_large",
        "not_pdf",
        "notebook_full",
        "forbidden",
        "save_failed",  # 리뷰 M3 — 저장 중 예외는 그 항목만
    }
    assert all(isinstance(m, str) and m for m in save_sources.SAVE_SOURCE_FAILURE_MESSAGES.values())


# ---------- 설정 ----------


def test_max_items_setting_default_and_bounds() -> None:
    assert CoreSettings.model_fields["agent_save_sources_max_items"].default == 200
    base = {"jwt_secret": "test-secret"}
    widest = CoreSettings(**base, agent_save_sources_max_items=1000)
    assert widest.agent_save_sources_max_items == 1000
    for bad in (0, 1001):
        with pytest.raises(ValidationError):
            CoreSettings(**base, agent_save_sources_max_items=bad)


# ---------- 저장 헬퍼·실행 판정 ----------


def _node(**kw: Any) -> dict[str, Any]:
    return {
        "id": "save",
        "type": "save_sources",
        "items": "{list.text}",
        "namespace": "naver-research",
        **kw,
    }


def _violations(nodes: list[dict[str, Any]], scope: str, edges: list[list[str]] | None = None):
    return policy.find_direct_node_approval_violations(nodes, edges or [], scope)


@pytest.mark.parametrize(
    ("node", "needle"),
    [
        (_node(namespace=None), "namespace"),
        ({k: v for k, v in _node().items() if k != "namespace"}, "namespace"),
        (_node(namespace="Naver"), "namespace"),  # 소문자만
        (_node(namespace="-lead"), "namespace"),
        (_node(namespace="n" * 65), "namespace"),
        (_node(namespace="{params.ns}"), "namespace"),  # 템플릿으로 바꿀 수 없다
        (_node(namespace=["a"]), "namespace"),
        (_node(items=["a"]), "items"),
        ({k: v for k, v in _node().items() if k != "items"}, "items"),
    ],
)
def test_save_helper_rejects_bad_literal_fields(node: dict[str, Any], needle: str) -> None:
    found = _violations([node], "system")
    assert found and all(f.startswith("save: ") for f in found)
    assert any(needle in f for f in found)


class FakeDb:
    def __init__(self, scope: str | None) -> None:
        self.agent = None if scope is None else SimpleNamespace(scope=scope)

    async def get(self, model: Any, key: Any, **kw: Any) -> Any:
        return self.agent


def _ctx(scope: str | None = "system", **kw: Any) -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=FakeDb(scope),  # type: ignore[arg-type]
        params={},
        agent_id=uuid.uuid4(),
        **kw,
    )


# ---------- 실행 전 검사(쓰기 전에 노드가 실패한다) ----------


@pytest.mark.parametrize(
    ("node", "state", "match"),
    [
        (_node(namespace="{params.ns}"), {}, "namespace"),
        (_node(items=["x"]), {}, "items"),
        (_node(), {"list": {"text": "not json"}}, "JSON"),
        (_node(), {"list": {"text": json.dumps({"items": []})}}, "배열"),
        (_node(), {"list": {"text": ""}}, "JSON"),
        (
            _node(notebook_rule={"title_template": "x {day}"}),
            {"list": {"text": "[]"}},
            "notebook_rule",
        ),
    ],
)
async def test_node_fails_before_any_write(node: dict[str, Any], state: dict, match: str) -> None:
    ctx = _ctx(user_id=uuid.uuid4())
    with pytest.raises(AgentRuntimeError, match=match):
        await save_sources._exec_save_sources(ctx, node, state)


async def test_node_requires_a_run_user() -> None:
    with pytest.raises(AgentRuntimeError, match="실행 사용자"):
        await save_sources._exec_save_sources(_ctx(), _node(), {"list": {"text": "[]"}})


# ---------- 입력 상한·승인 카드 (리뷰 M1·M5) ----------


def test_input_list_has_a_hard_cap() -> None:
    raw = json.dumps([{"title": "t", "external_id": f"x{i}", "content": "c"} for i in range(5001)])
    items, total = save_sources._render_items(_node(), {"list": {"text": raw}})
    assert (len(items), total) == (save_sources.MAX_INPUT_ITEMS, 5001)


def test_approval_card_uses_only_validated_items_and_is_bounded() -> None:
    huge_host = "h" * 3_000_000
    long_host = "l" * 300 + ".example.test"  # URL 2000자 안의 긴 호스트 — 253자로 자른다
    items: list[Any] = [
        {"title": "거대", "external_id": "g", "url": f"https://{huge_host}.test/a.pdf"},
        {"title": "x" * 2_000_000, "external_id": "t", "content": "본문"},  # 제목은 정리 후 500자
        {"title": "긴 호스트", "external_id": "l", "url": f"https://{long_host}/a.pdf"},
        {"external_id": "no-title", "content": "c"},
        *({"title": f"t{i}", "external_id": f"u{i}", "url": f"https://h{i}.test/a.pdf"}
          for i in range(10)),
    ]
    parsed = [save_sources._parse_item(raw) for raw in items]
    card = save_sources._approval_card(_ctx(), _node(), parsed, len(items))

    assert card["count"] == len(items)
    assert huge_host not in json.dumps(card)  # 검증을 통과하지 못한 항목은 싣지 않는다
    assert card["titles"][0] == "x" * 100
    assert card["titles"][1:] == ["긴 호스트", "t0", "t1", "t2"]
    assert card["hosts"][0] == long_host[:253]
    assert len(json.dumps(card, ensure_ascii=False)) <= save_sources._CARD_MAX_CHARS


def test_approval_card_shrinks_lists_to_fit_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(save_sources, "_CARD_MAX_CHARS", 300)
    items = [
        {"title": "t" * 90 + str(i), "external_id": f"u{i}", "url": f"https://{'h' * 200}{i}.test/"}
        for i in range(5)
    ]
    parsed = [save_sources._parse_item(raw) for raw in items]
    card = save_sources._approval_card(_ctx(), _node(), parsed, len(items))
    assert len(json.dumps(card, ensure_ascii=False)) <= 300
    assert card["count"] == 5
