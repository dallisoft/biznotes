"""에이전트 외부 HTTP 「받아오기 전용」 모드(#395) — `http` 노드·`http_request` 도구·저장 검증.

요청 수는 로컬 HTTP 서버가, DNS 질의 수는 가짜 `getaddrinfo`가 센다. 모델(`complete_with_tools`)과
실행 로그 기록만 대역이고 노드 실행기·도구 디스패처·관문은 실제 코드다. 실제 그래프 실행(kind
tool·plugin)은 `tests/test_agent_fetch_only_egress_pg.py`가 본다.
"""

import ast
import asyncio
import json
import pathlib
import socket
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import structlog.testing
from pydantic import ValidationError

from biznotes_core.config import CoreSettings, get_settings
from biznotes_core.core import ssrf
from biznotes_core.core.ssrf import FETCH_ONLY_USER_AGENT, EgressPolicy
from biznotes_core.services.agents import graph, policy
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError, ToolUnavailableError
from biznotes_core.services.agents.nodes import composite
from biznotes_core.services.agents.nodes import llm as llm_node
from biznotes_core.services.agents.react import loop
from biznotes_core.services.agents.tool_registry import (
    _BUILTIN_SPECS,
    TOOL_SIDE_EFFECT,
    build_tool_specs,
)
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---------- 경계 ----------


class Origin:
    """로컬 HTTP 서버 — 받은 요청을 센다. `routes`: path → 302 Location."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.routes: dict[str, str] = {}
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            head = await reader.readuntil(b"\r\n\r\n")
            line, *lines = head.decode("latin-1").split("\r\n")
            method, path, _ = line.split(" ", 2)
            headers = {
                k.strip().lower(): v.strip()
                for k, v in (h.split(":", 1) for h in lines if ":" in h)
            }
            length = int(headers.get("content-length") or 0)
            body = await reader.readexactly(length) if length else b""
            self.requests.append({"method": method, "path": path, "headers": headers, "body": body})
            if path in self.routes:
                status, extra, payload = "302 Found", f"Location: {self.routes[path]}\r\n", b""
            else:
                status, extra, payload = "200 OK", "Content-Type: text/plain\r\n", b"fetched"
            writer.write(
                f"HTTP/1.1 {status}\r\n{extra}Content-Length: {len(payload)}\r\n"
                "Connection: close\r\n\r\n".encode()
                + (b"" if method == "HEAD" else payload)
            )
            await writer.drain()
            writer.close()

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    def url(self, path: str = "/page", host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}{path}"


@pytest.fixture
async def origin(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(get_settings(), "ssrf_allow_private", True)  # 로컬 서버만 허용
    server = Origin()
    await server.start()
    yield server
    assert server.server is not None
    server.server.close()
    await server.server.wait_closed()


@pytest.fixture
def dns_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real = socket.getaddrinfo

    def counting(host: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(str(host))
        return real(host, *args, **kwargs)

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", counting)
    return calls


@pytest.fixture
def fetch_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "agent_http_egress_mode", "fetch_only")


class FakeDb:
    """정의 scope 조회(`direct_node_requires_approval`)만 흉내 낸다."""

    def __init__(self, scope: str) -> None:
        self.agent = SimpleNamespace(scope=scope)

    async def get(self, model: Any, key: Any, **kw: Any) -> Any:
        return self.agent


def _ctx(scope: str = "system", **kw: Any) -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=FakeDb(scope),  # type: ignore[arg-type]
        params={},
        agent_id=uuid.uuid4(),
        **kw,
    )


def _http(url: str, **kw: Any) -> dict[str, Any]:
    return {"id": "call", "type": "http", "url": url, **kw}


# ---------- 설정·정책 ----------


def test_settings_defaults_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = CoreSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.agent_http_egress_mode == "open"
    assert settings.agent_http_allowed_host_list == []
    assert (settings.agent_http_max_url_chars, settings.agent_http_max_query_chars) == (2048, 512)
    assert "agent_http_egress_mode" not in settings.model_fields_set
    with pytest.raises(ValidationError):
        CoreSettings(_env_file=None, agent_http_egress_mode="closed")  # type: ignore[call-arg, arg-type]
    with pytest.raises(ValidationError):
        CoreSettings(_env_file=None, agent_http_max_url_chars=0)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        CoreSettings(_env_file=None, agent_http_max_query_chars=100_000)  # type: ignore[call-arg]
    monkeypatch.setenv("AGENT_HTTP_EGRESS_MODE", "fetch_only")
    monkeypatch.setenv("AGENT_HTTP_ALLOWED_HOSTS", " api.example.com, *.docs.test ,,")
    from_env = CoreSettings(_env_file=None)  # type: ignore[call-arg]
    assert from_env.agent_http_egress_mode == "fetch_only"
    assert "agent_http_egress_mode" in from_env.model_fields_set
    assert from_env.agent_http_allowed_host_list == ["api.example.com", "*.docs.test"]


def test_agent_egress_policy_reads_settings_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    assert policy.agent_egress_policy() == EgressPolicy(
        fetch_only=False, allowed_hosts=(), max_url_chars=2048, max_query_chars=512
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_http_egress_mode", "fetch_only")
    monkeypatch.setattr(settings, "agent_http_allowed_hosts", "a.test,*.b.test")
    monkeypatch.setattr(settings, "agent_http_max_url_chars", 100)
    monkeypatch.setattr(settings, "agent_http_max_query_chars", 10)
    assert policy.agent_egress_policy() == EgressPolicy(
        fetch_only=True, allowed_hosts=("a.test", "*.b.test"), max_url_chars=100, max_query_chars=10
    )


def test_worker_warns_once_when_mode_is_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from biznotes_core.workers import celery_app

    def warnings(settings: CoreSettings) -> list[dict[str, Any]]:
        monkeypatch.setattr(celery_app, "get_settings", lambda: settings)
        with structlog.testing.capture_logs() as logs:
            celery_app._warn_unset_agent_http_egress_mode()
        return [e for e in logs if e["event"] == "agent_http_egress_mode_unset"]

    unset = warnings(CoreSettings(_env_file=None))  # type: ignore[call-arg]
    assert [(e["log_level"], e["mode"]) for e in unset] == [("warning", "open")]
    assert warnings(CoreSettings(_env_file=None, agent_http_egress_mode="open")) == []  # type: ignore[call-arg]
    assert warnings(CoreSettings(_env_file=None, agent_http_egress_mode="fetch_only")) == []  # type: ignore[call-arg]


# ---------- 저장 검증 ----------

FETCH_ONLY = EgressPolicy(
    fetch_only=True, allowed_hosts=(), max_url_chars=2048, max_query_chars=512
)
OPEN = EgressPolicy(fetch_only=False, allowed_hosts=(), max_url_chars=2048, max_query_chars=512)
GOOD = _http(
    "https://api.example.com/v1/{params.path}?q={params.q}",
    method="get",
    headers={"Accept": "application/json", "If-None-Match": '"v1"'},
)


@pytest.mark.parametrize(
    ("node", "reason"),
    [
        (_http("https://api.example.com/x"), "method"),  # 생략하면 기본 POST
        (_http("https://api.example.com/x", method="POST"), "method"),
        (_http("https://api.example.com/x", method="DELETE"), "method"),
        (_http("https://api.example.com/x", method="GET", body={"q": 1}), "body"),
        (_http("https://api.example.com/x", method="GET", body=""), "body"),
        (_http("https://api.example.com/x", method="GET", headers={"X-Api-Key": "k"}), "header"),
        (
            _http("https://api.example.com/x", method="GET", headers={"Accept": "{params.a}"}),
            "header",
        ),
        (_http("https://api.example.com/x", method="GET", headers="Accept: */*"), "header"),
        (_http("https://{params.host}/x", method="GET"), "non_literal_target"),
        (_http("https://api.example.com{params.p}", method="GET"), "non_literal_target"),
        (_http("{params.url}", method="GET"), "non_literal_target"),
        (_http("https://u:p@api.example.com/x", method="GET"), "userinfo"),
    ],
)
def test_save_helper_reports_each_violation(node: dict[str, Any], reason: str) -> None:
    assert policy._http_node_egress_reasons(node, FETCH_ONLY) == [reason]
    (message,) = policy.find_http_egress_violations([node], policy=FETCH_ONLY)
    assert message == f"call: {policy._HTTP_EGRESS_RULES[reason]}"
    # open이면 사유가 없다
    assert policy.find_http_egress_violations([node], policy=OPEN) == []


def test_save_helper_accepts_fetch_definitions() -> None:
    assert policy.find_http_egress_violations([GOOD], policy=FETCH_ONLY) == []
    head = _http("https://api.example.com/x", method="HEAD", headers={"User-Agent": "x"})
    assert policy.find_http_egress_violations([head], policy=FETCH_ONLY) == []
    # 다른 노드 유형은 보지 않는다
    other = {"id": "m", "type": "mcp_tool", "method": "POST", "body": {}}
    assert policy.find_http_egress_violations([other, "junk"], policy=FETCH_ONLY) == []  # type: ignore[list-item]


@pytest.mark.parametrize("kind", ["tool", "plugin"])
def test_save_helper_covers_tool_and_plugin_kinds(kind: str) -> None:
    nodes, _ = graph.kind_to_nodes(kind, {"http": {"url": "https://api.example.com/hook"}})
    assert policy.find_http_egress_violations(nodes, policy=FETCH_ONLY) == [
        f"call: {policy._HTTP_EGRESS_RULES['method']}"
    ]


def test_direct_node_violations_add_egress_reasons_only_in_fetch_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """기존 저장 헬퍼(D3)가 호출 시점 설정으로 사유를 더한다 — 제품 저장 경로가 그대로 422다."""
    nodes, edges = graph.kind_to_nodes(
        "tool", {"http": {"url": "https://api.example.com/hook", "body": {"a": 1}}}
    )
    assert policy.find_direct_node_approval_violations(nodes, edges, "user") == []
    monkeypatch.setattr(get_settings(), "agent_http_egress_mode", "fetch_only")
    assert policy.find_direct_node_approval_violations(nodes, edges, "user") == sorted(
        f"call: {policy._HTTP_EGRESS_RULES[reason]}" for reason in ("method", "body")
    )


# ---------- http 노드 실행 ----------


@pytest.mark.parametrize(
    ("node_kw", "reason"),
    [
        ({}, "method"),  # 기본 POST
        ({"method": "PUT"}, "method"),
        ({"method": "GET", "body": {"note": "secret-body"}}, "body"),
        ({"method": "GET", "headers": {"Authorization": "Bearer secret-token"}}, "header"),
        ({"method": "GET", "headers": {"Accept": "{params.secret}"}}, "header"),
    ],
)
async def test_http_node_definition_violation_sends_nothing(
    fetch_only: None,
    origin: Origin,
    dns_calls: list[str],
    node_kw: dict[str, Any],
    reason: str,
) -> None:
    """저장 검증을 우회한 정의도 실행 때 막는다 — DNS·요청·승인 카드가 0건이다.

    user 정의라 통과했다면 승인 게이트를 탔을 것이다.
    """
    ctx = _ctx("user")
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(AgentRuntimeError, match=rf"\({reason}\)") as info:
            await composite._exec_http(ctx, _http(origin.url("/hook?k=secret-q"), **node_kw), {})
    assert origin.requests == []
    assert dns_calls == []
    assert ctx.pending_approval is None
    assert [(e["method"], e["reason"]) for e in logs if e["event"] == "agent_http_blocked"] == [
        (node_kw.get("method", "POST"), reason)
    ]
    assert "secret" not in repr(logs) and "secret" not in str(info.value)


async def test_http_node_non_literal_target_is_rejected(
    fetch_only: None, origin: Origin, dns_calls: list[str]
) -> None:
    node = _http("http://{params.host}/x", method="GET")
    with pytest.raises(AgentRuntimeError, match="non_literal_target"):
        await composite._exec_http(
            _ctx("user"), node, {"params": {"host": f"127.0.0.1:{origin.port}"}}
        )
    assert origin.requests == []
    assert dns_calls == []


@pytest.mark.parametrize(
    ("setting", "value", "params", "reason"),
    [
        ("agent_http_max_query_chars", 20, {"q": "한" * 3}, "query_too_long"),
        ("agent_http_max_url_chars", 25, {"q": "x"}, "url_too_long"),
        ("agent_http_allowed_hosts", "api.example.com", {"q": "x"}, "host_not_allowed"),
    ],
)
async def test_http_node_rendered_request_is_checked_before_dns(
    fetch_only: None,
    origin: Origin,
    dns_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    value: Any,
    params: dict[str, Any],
    reason: str,
) -> None:
    """경로·쿼리 템플릿은 허용하되 렌더링한 요청을 DNS 사전 검사·게이트보다 먼저 본다."""
    monkeypatch.setattr(get_settings(), setting, value)
    ctx = _ctx("user")
    node = _http(origin.url("/search?q={params.q}"), method="GET")
    with pytest.raises(AgentRuntimeError, match=reason):
        await composite._exec_http(ctx, node, {"params": params})
    assert origin.requests == []
    assert dns_calls == []
    assert ctx.pending_approval is None


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_http_node_fetch_follows_redirect(
    fetch_only: None, origin: Origin, method: str
) -> None:
    origin.routes["/start"] = "/final"
    node = _http(
        origin.url("/start"),
        method=method,
        headers={"Accept": "text/plain"},
        requires_approval=False,
    )
    out = await composite._exec_http(_ctx("system"), node, {})
    assert out == {"text": "fetched" if method == "GET" else "", "status": 200}
    assert [(r["method"], r["path"]) for r in origin.requests] == [
        (method, "/start"),
        (method, "/final"),
    ]
    assert all(r["headers"]["user-agent"] == FETCH_ONLY_USER_AGENT for r in origin.requests)


async def test_http_node_redirect_outside_allowlist_is_blocked(
    fetch_only: None, origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "agent_http_allowed_hosts", "127.0.0.1")
    origin.routes["/start"] = origin.url("/final", host="localhost")
    node = _http(origin.url("/start"), method="GET", requires_approval=False)
    with pytest.raises(AgentRuntimeError, match="host_not_allowed"):
        await composite._exec_http(_ctx("system"), node, {})
    assert [r["path"] for r in origin.requests] == ["/start"]


@pytest.mark.parametrize("kind", ["tool", "plugin"])
async def test_http_node_from_tool_and_plugin_kind_is_rejected(
    fetch_only: None, origin: Origin, kind: str
) -> None:
    nodes, _ = graph.kind_to_nodes(kind, {"http": {"url": origin.url("/hook")}})
    with pytest.raises(AgentRuntimeError, match=r"\(method\)"):
        await composite._exec_http(_ctx("user"), nodes[0], {})
    assert origin.requests == []


async def test_http_node_open_mode_is_unchanged(origin: Origin) -> None:
    """open(기본)은 기본 POST·본문·임의 헤더를 보내고 302 뒤에는 GET으로 강등한다."""
    origin.routes["/hook"] = "/after"
    node = _http(
        origin.url("/hook"),
        headers={"X-Token": "{params.t}"},
        body={"q": "{params.q}"},
        requires_approval=False,
    )
    out = await composite._exec_http(_ctx("system"), node, {"params": {"t": "tok", "q": "질문"}})
    assert out["status"] == 200
    assert [(r["method"], r["path"]) for r in origin.requests] == [
        ("POST", "/hook"),
        ("GET", "/after"),
    ]
    assert json.loads(origin.requests[0]["body"]) == {"q": "질문"}
    assert origin.requests[0]["headers"]["x-token"] == "tok"


# ---------- http_request 도구 ----------


def test_http_request_stays_a_side_effect_tool() -> None:
    """D2 — 모드와 무관하게 HITL 대상이다(경로·쿼리 잔여 채널, 정적 분류 일치)."""
    assert TOOL_SIDE_EFFECT["http_request"] is True


async def test_fetch_only_spec_is_a_new_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    specs, _ = await build_tool_specs(ctx, ["http_request"])
    assert specs[0] is _BUILTIN_SPECS["http_request"]  # open은 공유 스펙 그대로

    monkeypatch.setattr(get_settings(), "agent_http_egress_mode", "fetch_only")
    specs, dispatchers = await build_tool_specs(ctx, ["http_request", "web_search"])
    spec = specs[0]
    assert spec is not _BUILTIN_SPECS["http_request"]
    assert spec["function"]["name"] == "http_request"
    assert spec["function"]["description"].startswith("외부 URL에서 내용을 받아온다")
    params = spec["function"]["parameters"]
    assert set(params["properties"]) == {"url", "method"}
    assert params["properties"]["method"]["enum"] == ["GET", "HEAD"]
    assert params["required"] == ["url"]
    assert set(dispatchers) == {"http_request", "web_search"}
    assert specs[1] is _BUILTIN_SPECS["web_search"]
    # 공유 스펙은 바뀌지 않는다
    shared = _BUILTIN_SPECS["http_request"]["function"]["parameters"]["properties"]
    assert set(shared) == {"url", "method", "body"}
    assert "enum" not in shared["method"]


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ({"method": "POST"}, "method"),
        ({"method": "DELETE"}, "method"),
        ({"method": "GET", "body": {"doc": "secret-body"}}, "body"),
    ],
)
async def test_http_request_violation_sends_nothing(
    fetch_only: None, origin: Origin, dns_calls: list[str], args: dict[str, Any], reason: str
) -> None:
    with pytest.raises(ToolUnavailableError, match=rf"\({reason}\)") as info:
        await llm_node._dispatch_builtin_tool(
            _ctx(), "http_request", {"url": origin.url("/x?d=secret-q"), **args}
        )
    assert origin.requests == []
    assert dns_calls == []
    assert "secret" not in str(info.value) and "127.0.0.1" not in str(info.value)


@pytest.mark.parametrize("method", ["GET", "HEAD", "get"])
async def test_http_request_fetches(fetch_only: None, origin: Origin, method: str) -> None:
    origin.routes["/start"] = "/final"
    out = await llm_node._dispatch_builtin_tool(
        _ctx(), "http_request", {"url": origin.url("/start"), "method": method}
    )
    assert out == ("fetched" if method.upper() == "GET" else "")
    assert [(r["method"], r["path"]) for r in origin.requests] == [
        (method.upper(), "/start"),
        (method.upper(), "/final"),
    ]


async def test_http_request_open_mode_is_unchanged(origin: Origin) -> None:
    out = await llm_node._dispatch_builtin_tool(
        _ctx(), "http_request", {"url": origin.url("/x"), "method": "POST", "body": {"a": 1}}
    )
    assert out == "fetched"
    assert [r["method"] for r in origin.requests] == ["POST"]
    assert json.loads(origin.requests[0]["body"]) == {"a": 1}


# ---------- react 루프의 도구 실패 경고 ----------


class _Model:
    def __init__(self, *calls: tuple[str, dict[str, Any]]) -> None:
        self.calls = list(calls)
        self.tool_results: list[str] = []

    async def __call__(
        self, db: Any, model_id: Any, messages: list[dict[str, Any]], tools: Any, **kw: Any
    ) -> ToolTurnResult:
        self.tool_results = [m["content"] for m in messages if m["role"] == "tool"]
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if not self.calls:
            return ToolTurnResult(
                content="끝", assistant_message={"role": "assistant", "content": "끝"}, usage=usage
            )
        name, args = self.calls.pop(0)
        call_id = uuid.uuid4().hex[:9]
        return ToolTurnResult(
            tool_calls=[ToolCall(id=call_id, name=name, arguments=args)],
            assistant_message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            },
            usage=usage,
        )


@pytest.fixture
def no_audit_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    async def start(*a: Any, **k: Any) -> str:
        return "log"

    async def finish(*a: Any, **k: Any) -> bool:
        return True

    monkeypatch.setattr(loop, "_log_node_start", start)
    monkeypatch.setattr(loop, "_log_node_finish", finish)


async def test_react_failure_warning_keeps_only_the_origin(
    fetch_only: None, origin: Origin, no_audit_rows: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """도구 실패 경고의 url은 `scheme://host`만 남는다 — 경로·쿼리 값이 서버 로그로 새지 않는다."""
    url = origin.url("/upload/secret-path?d=secret-q")
    args = {
        "url": url,
        "method": "POST",
        # 스펙 밖 인자도 모델이 넘길 수 있다 — 값이 로그에 남으면 안 된다
        "headers": {"X-Exfil": "secret-h"},
        "body": {"d": "secret-b"},
        "note": "secret-x",
    }
    model = _Model(("http_request", args))
    monkeypatch.setattr(gateway_llm, "complete_with_tools", model)
    node = {
        "id": "react",
        "type": "react_agent",
        "goal": "자료를 받아 와라",
        "allowed_tools": ["http_request"],
        "auto_approve_tools": {"http_request": {"domains": ["127.0.0.1"]}},
    }
    with structlog.testing.capture_logs() as logs:
        await loop.run_react_node(_ctx(), node)
    (warning,) = [e for e in logs if e["event"] == "도구 실행 실패"]
    assert warning["args"] == {"url": "http://127.0.0.1", "method": "POST"}
    assert "(method)" in warning["error"]
    assert "secret" not in repr(logs)
    assert origin.requests == []
    # 모델에는 사유가 전달된다
    assert "(method)" in model.tool_results[0]


async def test_resumed_failure_warning_keeps_only_the_origin() -> None:
    async def failing(ctx: RunContext, name: str, args: dict[str, Any]) -> str:
        raise ToolUnavailableError("요청 차단됨")

    with structlog.testing.capture_logs() as logs:
        await loop._apply_resolved_pending_call(
            _ctx(),
            None,
            pending_call={
                "id": "c1",
                "name": "http_request",
                "arguments": {"url": "https://u:p@api.example.com:8443/a?token=secret-q"},
            },
            approval_status="approved",
            messages=[],
            tool_log=[],
            aux_idx=0,
            dispatchers={"http_request": failing},
        )
    (warning,) = [e for e in logs if e["event"] == "도구 실행 실패(승인 후 재개)"]
    assert warning["args"] == {"url": "https://api.example.com"}
    assert "secret" not in repr(logs) and "u:p" not in repr(logs)


def test_failure_log_args_keeps_only_method_and_origin_for_http_request() -> None:
    args = {
        "url": "https://a.test/x?q=secret",
        "method": "get",
        "headers": {"Accept": "secret-h"},
        "body": "secret-b",
        "extra": ["secret-x"],
    }
    assert loop._failure_log_args("http_request", args) == {
        "url": "https://a.test",
        "method": "GET",
    }
    weird = {"url": "https://a.test/", "method": "GET /secret HTTP/1.1"}
    assert loop._failure_log_args("http_request", weird)["method"] == "invalid"
    assert loop._failure_log_args("http_request", {"body": "secret"}) == {}


def test_failure_log_args_leaves_other_tools() -> None:
    args = {"url": "https://a.test/x?q=1", "api_key": "k"}
    assert loop._failure_log_args("web_search", args) == {
        "url": "https://a.test/x?q=1",
        "api_key": "***",
    }
    assert loop._failure_log_args("http_request", {"url": "http://[bad"})["url"] == "invalid"


# ---------- 모드의 영향을 받지 않는 경로 ----------


def _calls_with_egress() -> set[str]:
    """`egress=`를 넘겨 관문을 부르는 모듈 — 연결 정책의 적용 대상 전부."""
    found: set[str] = set()
    for path in (ROOT / "biznotes_core").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name | ast.Attribute)
                and (node.func.id if isinstance(node.func, ast.Name) else node.func.attr)
                in ("ssrf_safe_request", "ssrf_safe_fetch")
                and any(k.arg == "egress" for k in node.keywords)
            ):
                found.add(path.relative_to(ROOT).as_posix())
    return found


def test_only_agent_http_paths_pass_an_egress_policy() -> None:
    """LLM·임베딩·이미지 게이트웨이, MCP, web_search, parse_web은 정책을 넘기지 않는다."""
    assert _calls_with_egress() == {
        "biznotes_core/services/agents/nodes/composite.py",
        "biznotes_core/services/agents/nodes/llm.py",
        "biznotes_core/services/agents/nodes/save_sources.py",  # URL(PDF) 항목(#403)
    }
    readers = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "biznotes_core").rglob("*.py")
        if "agent_http_" in path.read_text(encoding="utf-8")
        or "agent_egress_policy" in path.read_text(encoding="utf-8")
    }
    assert readers == {
        "biznotes_core/config.py",
        "biznotes_core/core/metrics.py",  # 지표 이름(agent_http_blocked_total)
        "biznotes_core/core/ssrf.py",  # 지표 import
        "biznotes_core/services/agents/policy.py",
        "biznotes_core/services/agents/tool_registry.py",
        "biznotes_core/services/agents/nodes/composite.py",
        "biznotes_core/services/agents/nodes/llm.py",
        "biznotes_core/services/agents/nodes/save_sources.py",
        "biznotes_core/workers/celery_app.py",
    }


def test_parse_web_ignores_the_mode(fetch_only: None, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util
    import sys
    import types

    import httpx

    from biznotes_core.services.ingestion.parsers import ParseError, parse_web

    # trafilatura는 코어 의존성이 아니다(지연 import) — 실패 경로는 추출에 닿지 않는다
    if importlib.util.find_spec("trafilatura") is None:
        monkeypatch.setitem(sys.modules, "trafilatura", types.ModuleType("trafilatura"))
    monkeypatch.setattr(get_settings(), "agent_http_allowed_hosts", "nothing.invalid")
    seen: list[dict[str, Any]] = []

    def fake_fetch(url: str, **kw: Any) -> Any:
        seen.append(kw)
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr("biznotes_core.core.ssrf.ssrf_safe_fetch", fake_fetch)
    with pytest.raises(ParseError):
        parse_web("https://example.com/a")
    assert len(seen) == 1 and "egress" not in seen[0]


# ---------- 리뷰 반영: IDNA 사전 DNS·헤더 제어 문자·해석 불가 url ----------


class _Pinned:
    """핀 연결 분기 경계 — 가짜 DNS(실제 소켓처럼 idna 코덱으로 바꾼 이름 기록) + MockTransport."""

    def __init__(self) -> None:
        self.queried: list[str] = []
        self.seen: list[Any] = []


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> _Pinned:
    import httpx

    state = _Pinned()
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        state.seen.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b"idn-ok"))  # 실제 전송처럼 스트림

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.encode("idna").decode("ascii") if isinstance(host, str) else str(host)
        state.queried.append(name)
        return [(0, 0, 0, "", ("93.184.216.34", 0))]

    class _Async(real_client):  # type: ignore[misc,valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(get_settings(), "ssrf_allow_private", False)
    monkeypatch.setattr(ssrf.httpx, "AsyncClient", _Async)
    monkeypatch.setattr(ssrf.socket, "getaddrinfo", getaddrinfo)
    return state


@pytest.mark.parametrize("mode", ["open", "fetch_only"])
async def test_http_node_idn_host_is_resolved_as_the_gateway_pins(
    pinned: _Pinned, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """사전 DNS·관문 모두 `xn--fa-hia.de`만 묻는다 — 허용 목록 밖 `fass.de`는 묻지 않는다."""
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_http_egress_mode", mode)
    monkeypatch.setattr(settings, "agent_http_allowed_hosts", "xn--fa-hia.de")
    node = _http("https://faß.de/x", method="GET", requires_approval=False)
    out = await composite._exec_http(_ctx("system"), node, {})
    assert out == {"text": "idn-ok", "status": 200}
    assert pinned.queried == ["xn--fa-hia.de", "xn--fa-hia.de"]  # 사전 검사 + 관문 홉
    assert [r.headers["host"] for r in pinned.seen] == ["xn--fa-hia.de"]


async def test_http_node_control_character_header_sends_nothing(
    fetch_only: None, origin: Origin, dns_calls: list[str]
) -> None:
    """리터럴 헤더 값의 CRLF는 저장 헬퍼와 실행 시 모두 `header`다.

    요청·DNS가 0건이고 값은 로그·예외 어디에도 없다.
    """
    node = _http(
        origin.url("/x"),
        method="GET",
        headers={"Accept": "text/html\r\nX-Exfil: secret-v"},
        requires_approval=False,
    )
    assert policy._http_node_egress_reasons(node, FETCH_ONLY) == ["header"]
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(AgentRuntimeError, match=r"\(header\)") as info:
            await composite._exec_http(_ctx("system"), node, {})
    assert origin.requests == []
    assert dns_calls == []
    assert "secret" not in repr(logs) and "secret" not in str(info.value)


def test_unparseable_url_is_not_a_literal_target() -> None:
    """`urlsplit`의 ValueError(`Invalid IPv6 URL`)는 리터럴 아님이다 — 헬퍼는 예외 대신 사유다."""
    assert policy.url_target_is_literal("http://[bad") is False
    node = _http("http://[bad/x", method="GET")
    assert policy.find_http_egress_violations([node], policy=FETCH_ONLY) == [
        f"call: {policy._HTTP_EGRESS_RULES['non_literal_target']}"
    ]


@pytest.mark.parametrize("mode", ["open", "fetch_only"])
def test_direct_node_helper_survives_unparseable_url(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setattr(get_settings(), "agent_http_egress_mode", mode)
    node = _http("http://[bad/x", method="GET", requires_approval=False)
    found = policy.find_direct_node_approval_violations([node], [], "system")
    assert any("리터럴이어야" in reason for reason in found)
    if mode == "fetch_only":
        assert f"call: {policy._HTTP_EGRESS_RULES['non_literal_target']}" in found
