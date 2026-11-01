"""web_search 도구 — 요약 전달(#11)과 evidence·결과 수(#12).

제공자 HTTP 응답만 가짜로 두고, 어댑터(`services/websearch.py`)와 도구 디스패처
(`nodes/llm.py`)는 실제 코드를 탄다 — 키 이름이 한쪽에서만 바뀌어도 여기서 깨진다.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from biznotes_core.services import websearch
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.nodes.llm import _dispatch_builtin_tool

SEARXNG_BODY = {
    "results": [
        {
            "title": "에이전트의 어원",
            "url": "https://example.com/agent",
            "content": "agent는 라틴어 agere(행하다)에서 왔다.",
            "engine": "duckduckgo",
        },
        {"title": "URL 없는 항목", "content": "버려진다"},
    ]
}
TAVILY_BODY = {
    "results": [
        {
            "title": "Agentic AI 개요",
            "url": "https://example.org/agentic",
            "content": "스스로 계획하고 도구를 호출하는 AI 시스템을 말한다.",
        }
    ]
}


def _ctx() -> RunContext:
    return RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={})  # type: ignore[arg-type]


@pytest.fixture
def provider(monkeypatch):
    """제공자 설정과 HTTP 응답을 고른다. 호출된 요청을 기록한다."""
    requests: list[httpx.Request] = []
    state: dict[str, Any] = {"status": 200, "body": {"results": []}}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(state["status"], json=state["body"])

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        websearch.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )

    def use(name: str, *, status: int = 200, body: dict[str, Any] | None = None) -> None:
        settings = SimpleNamespace(
            search_provider=name,
            tavily_api_key="test-tavily-key" if name == "tavily" else "",
            searxng_url="http://searxng.test",
        )
        monkeypatch.setattr(websearch, "get_settings", lambda: settings)
        state["status"] = status
        state["body"] = body if body is not None else {"results": []}

    use.requests = requests  # type: ignore[attr-defined]
    return use


@pytest.mark.parametrize(
    ("name", "body", "title", "url", "summary", "host"),
    [
        (
            "searxng",
            SEARXNG_BODY,
            "에이전트의 어원",
            "https://example.com/agent",
            "agent는 라틴어 agere(행하다)에서 왔다.",
            "searxng.test",
        ),
        (
            "tavily",
            TAVILY_BODY,
            "Agentic AI 개요",
            "https://example.org/agentic",
            "스스로 계획하고 도구를 호출하는 AI 시스템을 말한다.",
            "api.tavily.com",
        ),
    ],
)
async def test_summary_reaches_the_model_for_every_provider(
    provider, name, body, title, url, summary, host
):
    provider(name, body=body)
    ctx = _ctx()

    out = await _dispatch_builtin_tool(ctx, "web_search", {"query": "agent 어원"})

    assert provider.requests[-1].url.host == host
    assert f"- {title}: {url}\n  {summary}" in out
    assert ctx.tool_evidence == [
        {"kind": "web_search", "status": "succeeded", "result_count": 1},
        {"kind": "web", "url": url, "title": title, "snippet": summary, "status": "succeeded"},
    ]


async def test_only_results_shown_to_the_model_become_evidence(provider):
    many = {
        "results": [
            {"title": f"t{i}", "url": f"https://e.com/{i}", "content": "요약" * 200}
            for i in range(8)
        ]
    }
    provider("searxng", body=many)
    ctx = _ctx()

    out = await _dispatch_builtin_tool(ctx, "web_search", {"query": "q"})

    web = [e for e in ctx.tool_evidence if e["kind"] == "web"]
    assert ctx.tool_evidence[0] == {"kind": "web_search", "status": "succeeded", "result_count": 5}
    assert [e["url"] for e in web] == [f"https://e.com/{i}" for i in range(5)]
    assert all(len(e["snippet"]) == 200 and e["snippet"] in out for e in web)
    assert "https://e.com/5" not in out


async def test_zero_results_are_a_success_with_count_zero(provider):
    provider("searxng", body={"results": []})
    ctx = _ctx()

    out = await _dispatch_builtin_tool(ctx, "web_search", {"query": "q"})

    assert out == "웹 검색 결과가 없습니다."
    assert ctx.tool_evidence == [{"kind": "web_search", "status": "succeeded", "result_count": 0}]


async def test_missing_summary_is_an_empty_line_not_none(provider):
    blank = {"title": None, "url": "https://e.com", "content": None}
    provider("searxng", body={"results": [blank]})
    ctx = _ctx()

    out = await _dispatch_builtin_tool(ctx, "web_search", {"query": "q"})

    assert "None" not in out
    assert ctx.tool_evidence[1]["snippet"] == ""


async def test_provider_failure_is_recorded_and_still_raised(provider):
    provider("searxng", status=502)
    ctx = _ctx()

    with pytest.raises(websearch.SearchProviderError):
        await _dispatch_builtin_tool(ctx, "web_search", {"query": "q"})

    assert ctx.tool_evidence == [{"kind": "web_search", "status": "failed", "result_count": 0}]
