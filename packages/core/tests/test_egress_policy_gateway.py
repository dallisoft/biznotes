"""외부 연결 정책(`EgressPolicy`, #395) — 코어 관문의 판정·리다이렉트·로그·지표.

네트워크 경계만 바꾼다. 요청 수는 **로컬 HTTP 서버**(allow_private 분기)나 실제 httpx Client에
꽂은 MockTransport(핀 연결 분기)가 세고, DNS 질의 수는 가짜 `getaddrinfo`가 센다. 판정
(`check_egress`)과 관문(`ssrf_safe_request`·`ssrf_safe_fetch`) 본문은 실제 코드다.
"""

import asyncio
import gzip
import socket
import time
import tracemalloc
from typing import Any

import anyio
import httpx
import pytest
import structlog.testing
from prometheus_client import REGISTRY

from biznotes_core.core import ssrf
from biznotes_core.core.ssrf import (
    FETCH_ONLY_USER_AGENT,
    EgressBlockedError,
    EgressPolicy,
    SSRFBlockedError,
    check_egress,
    host_matches,
)

REAL_CLIENT = httpx.Client
REAL_ASYNC_CLIENT = httpx.AsyncClient


def _policy(
    *, fetch_only: bool = True, hosts: tuple[str, ...] = (), url: int = 2048, query: int = 512
) -> EgressPolicy:
    return EgressPolicy(
        fetch_only=fetch_only, allowed_hosts=hosts, max_url_chars=url, max_query_chars=query
    )


FETCH_ONLY = _policy()
OPEN = _policy(fetch_only=False)


def _blocked_count(reason: str) -> float:
    return REGISTRY.get_sample_value("agent_http_blocked_total", {"reason": reason}) or 0.0


# ---------- 순수 판정 ----------


@pytest.mark.parametrize(
    ("method", "url", "headers", "has_body", "reason"),
    [
        ("POST", "https://a.test/x", None, False, "method"),
        ("PUT", "https://a.test/x", None, False, "method"),
        ("PATCH", "https://a.test/x", None, False, "method"),
        ("DELETE", "https://a.test/x", None, False, "method"),
        ("OPTIONS", "https://a.test/x", None, False, "method"),
        ("GET", "https://a.test/x", None, True, "body"),
        ("GET", "https://a.test/x", {"Authorization": "Bearer t"}, False, "header"),
        ("GET", "https://a.test/x", {"Cookie": "sid=1"}, False, "header"),
        ("GET", "https://a.test/x", {"X-Api-Key": "k"}, False, "header"),
        ("GET", "https://a.test/x", {"Host": "b.test"}, False, "header"),
        ("GET", "https://u:p@a.test/x", None, False, "userinfo"),
        ("GET", "https://u@a.test/x", None, False, "userinfo"),
    ],
)
def test_fetch_only_rejects(
    method: str, url: str, headers: dict[str, str] | None, has_body: bool, reason: str
) -> None:
    with pytest.raises(EgressBlockedError) as info:
        check_egress(method, url, headers, has_body, FETCH_ONLY)
    assert info.value.reason == reason
    # 기존 catch(SSRFBlockedError)가 그대로 막는다
    assert isinstance(info.value, SSRFBlockedError)
    # 문구에 URL·헤더 값이 없다
    assert "a.test" not in str(info.value)
    assert not any(v in str(info.value) for v in ("Bearer t", "sid=1", "u:p"))


@pytest.mark.parametrize("method", ["GET", "HEAD", "get", "head"])
def test_fetch_only_allows_get_head_with_allowed_headers(method: str) -> None:
    headers = {
        "Accept": "text/html",
        "accept-language": "ko",
        "If-None-Match": '"e"',
        "If-Modified-Since": "Wed, 21 Oct 2026 07:28:00 GMT",
        "Range": "bytes=0-99",
        "User-Agent": "anything",
    }
    check_egress(method, "https://a.test/x?q=1", headers, False, FETCH_ONLY)
    check_egress(method, "https://a.test/x", None, False, FETCH_ONLY)
    check_egress(method, "https://a.test/x", {}, False, FETCH_ONLY)


def test_lengths_are_measured_in_encoded_form() -> None:
    """한글 1자는 인코딩하면 9자(`%ED%95%9C`)다 — 원문 길이가 아니라 보내는 길이로 잰다."""
    policy = _policy(url=60, query=20)
    check_egress("GET", "https://a.test/?q=" + "a" * 18, None, False, policy)  # 쿼리 20자
    with pytest.raises(EgressBlockedError) as info:
        check_egress("GET", "https://a.test/?q=" + "a" * 19, None, False, policy)
    assert info.value.reason == "query_too_long"
    # 원문 4자(q=한한)지만 인코딩하면 20자다
    with pytest.raises(EgressBlockedError) as info:
        check_egress("GET", "https://a.test/?q=한한한", None, False, policy)
    assert info.value.reason == "query_too_long"
    check_egress("GET", "https://a.test/?q=한한", None, False, policy)
    # 경로도 인코딩한 길이 — `https://a.test/` 15자 + 한글 5자 × 9 = 60
    check_egress("GET", "https://a.test/" + "한" * 5, None, False, policy)
    with pytest.raises(EgressBlockedError) as info:
        check_egress("GET", "https://a.test/" + "한" * 5 + "a", None, False, policy)
    assert info.value.reason == "url_too_long"


def test_open_policy_without_hosts_checks_nothing() -> None:
    check_egress("POST", "https://u:p@a.test/" + "x" * 10_000, {"X-Any": "1"}, True, OPEN)


def test_allowlist_applies_in_both_modes() -> None:
    for fetch_only in (True, False):
        policy = _policy(fetch_only=fetch_only, hosts=("api.example.com", "*.docs.test"))
        check_egress("GET", "https://api.example.com/x", None, False, policy)
        check_egress("GET", "https://a.docs.test/x", None, False, policy)
        for url in ("https://docs.test/x", "https://evil.test/x", "https://api.example.com.evil/x"):
            with pytest.raises(EgressBlockedError) as info:
                check_egress("GET", url, None, False, policy)
            assert info.value.reason == "host_not_allowed"
    # open 모드는 목록만 본다 — 메서드·본문·헤더는 그대로 보낸다
    open_listed = _policy(fetch_only=False, hosts=("api.example.com",))
    check_egress("POST", "https://api.example.com/x", {"X-Any": "1"}, True, open_listed)


def test_unparseable_url_fails_closed() -> None:
    with pytest.raises(SSRFBlockedError):
        check_egress("GET", "https://exa mple.test:x/", None, False, FETCH_ONLY)


@pytest.mark.parametrize(
    ("host", "patterns", "expected"),
    [
        # tool_registry._domain_allowed의 기존 규칙(tests/test_hitl_approval_pg.py)과 같다
        ("api.internal.corp", ["*.internal.corp"], True),
        ("a.b.internal.corp", ["*.internal.corp"], True),
        ("evil-internal.corp", ["*.internal.corp"], False),
        ("internal.corp.attacker.com", ["*.internal.corp"], False),
        ("internal.corp", ["*.internal.corp"], False),
        ("api.example.com", ["api.example.com"], True),
        ("api.example.com.evil.com", ["api.example.com"], False),
        # 대소문자·후행 점
        ("API.Example.COM.", ["api.example.com"], True),
        ("api.example.com", ["API.EXAMPLE.COM"], True),
        # IDNA ASCII로 비교한다 — 유니코드·punycode 어느 쪽 표기든 같은 호스트
        ("bücher.test", ["xn--bcher-kva.test"], True),
        ("xn--bcher-kva.test", ["bücher.test"], True),
        ("shop.bücher.test", ["*.xn--bcher-kva.test"], True),
        ("bucher.test", ["bücher.test"], False),
        ("a.test", [], False),
    ],
)
def test_host_matches(host: str, patterns: list[str], expected: bool) -> None:
    assert host_matches(host, patterns) is expected


def test_domain_allowed_uses_the_same_matcher() -> None:
    from biznotes_core.services.agents.tool_registry import _domain_allowed

    assert _domain_allowed("shop.bücher.test", ["*.xn--bcher-kva.test"]) is True
    assert _domain_allowed("evil-internal.corp", ["*.internal.corp"]) is False


# ---------- 네트워크 경계: 로컬 HTTP 서버 ----------


class Origin:
    """로컬 HTTP 서버 — 받은 요청(method·path·헤더·본문)을 센다.

    `routes`: path → 302 Location. `encoded`: path → (Content-Encoding, 전송 본문).
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.routes: dict[str, str] = {}
        self.encoded: dict[str, tuple[str, bytes]] = {}
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
            elif path in self.encoded:
                encoding, payload = self.encoded[path]
                status, extra = "200 OK", f"Content-Encoding: {encoding}\r\n"
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
async def origin() -> Any:
    server = Origin()
    await server.start()
    yield server
    assert server.server is not None
    server.server.close()
    await server.server.wait_closed()


@pytest.fixture
def dns_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`getaddrinfo` 호출을 센다(실제 해석은 그대로). 거부 경로는 0건이어야 한다."""
    calls: list[str] = []
    real = socket.getaddrinfo

    def counting(host: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(str(host))
        return real(host, *args, **kwargs)

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", counting)
    return calls


async def _request(method: str, url: str, policy: EgressPolicy | None, **kw: Any) -> Any:
    return await ssrf.ssrf_safe_request(method, url, allow_private=True, egress=policy, **kw)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_fetch_only_get_head_follow_redirect_keeping_method(
    origin: Origin, method: str
) -> None:
    """302 뒤에도 원 메서드(HEAD는 HEAD)를 유지하고, 매 홉 User-Agent는 코어 고정값이다."""
    origin.routes["/start"] = origin.url("/final", host="localhost")
    res = await _request(
        method,
        origin.url("/start"),
        FETCH_ONLY,
        headers={"Accept": "text/plain", "User-Agent": "leak-me"},
    )
    assert res.status_code == 200
    assert res.text == ("fetched" if method == "GET" else "")
    assert [(r["method"], r["path"]) for r in origin.requests] == [
        (method, "/start"),
        (method, "/final"),
    ]
    for r in origin.requests:
        assert r["headers"]["user-agent"] == FETCH_ONLY_USER_AGENT
        assert r["headers"]["accept"] == "text/plain"
        assert r["body"] == b""


async def test_fetch_only_sync_fetch_follows_redirect(origin: Origin) -> None:
    origin.routes["/start"] = "/final"
    res = await anyio.to_thread.run_sync(
        lambda: ssrf.ssrf_safe_fetch(origin.url("/start"), allow_private=True, egress=FETCH_ONLY)
    )
    assert res.text == "fetched"
    assert [r["path"] for r in origin.requests] == ["/start", "/final"]
    assert all(r["headers"]["user-agent"] == FETCH_ONLY_USER_AGENT for r in origin.requests)


@pytest.mark.parametrize(
    ("method", "path", "kw", "reason"),
    [
        ("POST", "/x", {}, "method"),
        ("PUT", "/x", {}, "method"),
        ("PATCH", "/x", {}, "method"),
        ("DELETE", "/x", {}, "method"),
        ("GET", "/x", {"json": {"data": "secret-body"}}, "body"),
        ("GET", "/x", {"headers": {"Authorization": "Bearer secret-token"}}, "header"),
        ("GET", "/x", {"headers": {"Cookie": "sid=secret-cookie"}}, "header"),
        ("GET", "/x", {"headers": {"X-Exfil": "secret-header"}}, "header"),
        ("GET", "/x?d=" + "s" * 600, {}, "query_too_long"),
        ("GET", "/" + "s" * 2100, {}, "url_too_long"),
    ],
)
async def test_violations_send_nothing(
    origin: Origin,
    dns_calls: list[str],
    method: str,
    path: str,
    kw: dict[str, Any],
    reason: str,
) -> None:
    before = _blocked_count(reason)
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(EgressBlockedError) as info:
            await _request(method, origin.url(path), FETCH_ONLY, **kw)
    assert info.value.reason == reason
    assert origin.requests == []
    assert dns_calls == []
    # 경고 로그 1건 — method·호스트·사유만
    blocked = [e for e in logs if e["event"] == "agent_http_blocked"]
    assert blocked == [
        {
            "event": "agent_http_blocked",
            "log_level": "warning",
            "method": method,
            "host": "127.0.0.1",
            "reason": reason,
        }
    ]
    assert "secret" not in repr(logs) and "sss" not in repr(logs)
    assert "secret" not in str(info.value) and "127.0.0.1" not in str(info.value)
    assert _blocked_count(reason) == before + 1


async def test_userinfo_and_allowlist_send_nothing(origin: Origin, dns_calls: list[str]) -> None:
    with pytest.raises(EgressBlockedError) as info:
        await _request("GET", f"http://u:p@127.0.0.1:{origin.port}/x", FETCH_ONLY)
    assert info.value.reason == "userinfo"
    with pytest.raises(EgressBlockedError) as info:
        await _request("GET", origin.url(), _policy(hosts=("api.example.com",)))
    assert info.value.reason == "host_not_allowed"
    assert origin.requests == []
    assert dns_calls == []


async def test_sync_fetch_violations_send_nothing(origin: Origin, dns_calls: list[str]) -> None:
    cases = [
        ({"headers": {"Authorization": "Bearer t"}}, origin.url(), "header"),
        ({}, f"http://u:p@127.0.0.1:{origin.port}/x", "userinfo"),
        ({}, origin.url("/?q=" + "a" * 600), "query_too_long"),
    ]
    for kw, url, reason in cases:
        with pytest.raises(EgressBlockedError) as info:
            await anyio.to_thread.run_sync(
                lambda u=url, k=kw: ssrf.ssrf_safe_fetch(
                    u, allow_private=True, egress=FETCH_ONLY, **k
                )
            )
        assert info.value.reason == reason
    assert origin.requests == []
    assert dns_calls == []


@pytest.mark.parametrize("fetch_only", [True, False], ids=["fetch_only", "open"])
async def test_redirect_outside_allowlist_is_blocked(origin: Origin, fetch_only: bool) -> None:
    """허용 호스트에서 시작해도 다음 홉이 목록 밖이면 보내지 않는다(오픈 리다이렉트 우회)."""
    origin.routes["/start"] = origin.url("/final", host="localhost")
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(EgressBlockedError) as info:
            await _request(
                "GET", origin.url("/start"), _policy(fetch_only=fetch_only, hosts=("127.0.0.1",))
            )
    assert info.value.reason == "host_not_allowed"
    assert [r["path"] for r in origin.requests] == ["/start"]
    assert [(e["host"], e["reason"]) for e in logs if e["event"] == "agent_http_blocked"] == [
        ("localhost", "host_not_allowed")
    ]


@pytest.mark.parametrize(
    ("location", "reason"),
    [
        ("http://u:p@127.0.0.1:{port}/final", "userinfo"),
        ("/final?d=" + "x" * 600, "query_too_long"),
    ],
)
async def test_redirect_hop_is_rechecked(origin: Origin, location: str, reason: str) -> None:
    origin.routes["/start"] = location.format(port=origin.port)
    with pytest.raises(EgressBlockedError) as info:
        await _request("GET", origin.url("/start"), FETCH_ONLY)
    assert info.value.reason == reason
    assert [r["path"] for r in origin.requests] == ["/start"]


async def test_open_policy_keeps_the_current_behavior(origin: Origin) -> None:
    """규칙 없는 정책은 `egress=None`과 같다 — 본문·임의 헤더를 보내고 302 뒤 GET으로 강등한다."""
    for policy in (None, OPEN):
        origin.requests.clear()
        origin.routes["/start"] = "/final"
        res = await _request(
            "POST",
            origin.url("/start"),
            policy,
            json={"a": 1},
            headers={"X-Keep": "1", "User-Agent": "caller-ua"},
        )
        assert res.status_code == 200
        assert [(r["method"], r["path"]) for r in origin.requests] == [
            ("POST", "/start"),
            ("GET", "/final"),
        ]
        assert origin.requests[0]["body"] == b'{"a":1}'
        assert origin.requests[1]["body"] == b""
        assert all(r["headers"]["x-keep"] == "1" for r in origin.requests)
        assert all(r["headers"]["user-agent"] == "caller-ua" for r in origin.requests)


async def test_open_with_allowlist_sends_post_to_listed_host(origin: Origin) -> None:
    res = await _request(
        "POST", origin.url("/hook"), _policy(fetch_only=False, hosts=("127.0.0.1",)), json={}
    )
    assert res.status_code == 200
    assert [r["method"] for r in origin.requests] == ["POST"]


# ---------- 네트워크 경계: 핀 연결 분기(MockTransport + 가짜 DNS) ----------

_DNS = {"a.test": "93.184.216.34", "b.test": "93.184.216.35"}


class _Pinned:
    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.dns: list[str] = []
        self.routes: dict[str, str] = {}
        # "host/path" → (Content-Encoding, 전송 본문)
        self.encoded: dict[str, tuple[str, bytes]] = {}


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> _Pinned:
    state = _Pinned()

    def handler(request: httpx.Request) -> httpx.Response:
        state.seen.append(request)
        key = f"{request.headers['host']}{request.url.raw_path.decode()}"
        if key in state.routes:
            return httpx.Response(302, headers={"Location": state.routes[key]})
        # 실제 전송처럼 스트림으로 준다 — `content=`는 생성 때 본문을 읽고 해제해 버린다
        if key in state.encoded:
            encoding, body = state.encoded[key]
            return httpx.Response(
                200, headers={"Content-Encoding": encoding}, stream=httpx.ByteStream(body)
            )
        return httpx.Response(200, stream=httpx.ByteStream(b"ok"))

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        state.dns.append(str(host))
        ip = _DNS.get(str(host))
        if ip is None:
            raise socket.gaierror(f"unknown host {host}")
        return [(0, 0, 0, "", (ip, 0))]

    class _Sync(REAL_CLIENT):  # type: ignore[misc,valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    class _Async(REAL_ASYNC_CLIENT):  # type: ignore[misc,valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(ssrf.httpx, "Client", _Sync)
    monkeypatch.setattr(ssrf.httpx, "AsyncClient", _Async)
    monkeypatch.setattr(ssrf.socket, "getaddrinfo", getaddrinfo)
    return state


@pytest.mark.parametrize("use_async", [True, False], ids=["request", "fetch"])
async def test_pinned_violation_has_no_dns_and_no_request(pinned: _Pinned, use_async: bool) -> None:
    for url, kw in (
        ("https://a.test/x", {"headers": {"Cookie": "c"}}),
        ("https://u:p@a.test/x", {}),
        ("https://b.test/x", {}),
    ):
        policy = _policy(hosts=("a.test",))
        with pytest.raises(EgressBlockedError):
            if use_async:
                await ssrf.ssrf_safe_request("GET", url, egress=policy, **kw)
            else:
                ssrf.ssrf_safe_fetch(url, egress=policy, **kw)
    assert pinned.dns == []
    assert pinned.seen == []


async def test_pinned_cross_host_redirect_keeps_rules(pinned: _Pinned) -> None:
    """302로 다른 호스트로 가도 메서드(HEAD)·헤더 규칙을 지키고, 홉마다 DNS 전에 판정한다."""
    pinned.routes["a.test/start"] = "https://b.test/next"
    res = await ssrf.ssrf_safe_request(
        "HEAD", "https://a.test/start", headers={"Accept": "*/*"}, egress=FETCH_ONLY
    )
    assert res.status_code == 200
    assert [(r.method, r.headers["host"]) for r in pinned.seen] == [
        ("HEAD", "a.test"),
        ("HEAD", "b.test"),
    ]
    for r in pinned.seen:
        assert r.headers["user-agent"] == FETCH_ONLY_USER_AGENT
        assert r.headers["accept"] == "*/*"
        assert r.content == b""
    assert pinned.dns == ["a.test", "b.test"]


async def test_pinned_redirect_outside_allowlist_stops_before_dns(pinned: _Pinned) -> None:
    pinned.routes["a.test/start"] = "https://b.test/next"
    with pytest.raises(EgressBlockedError) as info:
        await ssrf.ssrf_safe_request(
            "GET", "https://a.test/start", egress=_policy(hosts=("a.test",))
        )
    assert info.value.reason == "host_not_allowed"
    assert [r.headers["host"] for r in pinned.seen] == ["a.test"]
    assert pinned.dns == ["a.test"]  # b.test는 질의하지 않았다


# ---------- 리뷰 반영: IDNA 사전 DNS·헤더 제어 문자 ----------


def test_validate_external_url_queries_the_gateway_ascii_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """사전 DNS도 관문과 같은 IDNA ASCII 호스트를 묻는다 — `faß.de`는 `xn--fa-hia.de`다.

    Python 내장 idna 코덱(IDNA2003)에 유니코드를 넘기면 `fass.de`(다른 호스트)를 묻는다.
    가짜 DNS는 실제 `socket.getaddrinfo`처럼 받은 이름을 idna 코덱으로 바꿔 기록한다.
    """
    queried: list[str] = []

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        queried.append(host.encode("idna").decode("ascii") if isinstance(host, str) else host)
        return [(0, 0, 0, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", getaddrinfo)
    ssrf.validate_external_url("https://faß.de/x")
    assert queried == ["xn--fa-hia.de"]
    # 인코딩할 수 없는 호스트는 묻지 않고 거부한다(fail closed)
    for allow_private in (False, True):
        with pytest.raises(SSRFBlockedError):
            ssrf.validate_external_url("https://☃☃.com/x", allow_private=allow_private)
    assert queried == ["xn--fa-hia.de"]


@pytest.mark.parametrize(
    "headers",
    [
        {"Accept": "text/html\r\nX-Exfil: secret-v"},
        {"Accept": "text/html\nsecret-v"},
        {"Accept": "secret-v\x00"},
        {"Accept": "secret-v\x7f"},
        {"Accept-Language": "ko\tsecret-v"},
        {"Accept\r\nX-Exfil": "secret-v"},
        {"Accept": 1},
    ],
)
async def test_fetch_only_rejects_control_characters_in_headers(
    origin: Origin, dns_calls: list[str], headers: dict[str, Any]
) -> None:
    """제어 문자는 httpx에 닿기 전에 `header`로 거부한다 — 값이 담긴 httpx 예외 문구가 없다."""
    with pytest.raises(EgressBlockedError) as info:
        check_egress("GET", "https://a.test/x", headers, False, FETCH_ONLY)
    assert info.value.reason == "header"
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(EgressBlockedError) as info:
            await _request("GET", origin.url(), FETCH_ONLY, headers=headers)
    assert info.value.reason == "header"
    assert origin.requests == []
    assert dns_calls == []
    assert "secret" not in repr(logs) and "secret" not in str(info.value)
    # open 모드는 판정하지 않는다(지금 동작)
    check_egress("GET", "https://a.test/x", headers, False, OPEN)



# ---------- #403 리뷰발 보강: 압축 응답·비동기 DNS ----------

# 8 MiB 0을 두 번 gzip — 전송 본문은 수백 바이트지만 해제하면 8 MiB다
_BOMB_PLAIN = 8 << 20
_BOMB = gzip.compress(gzip.compress(b"\0" * _BOMB_PLAIN))


def _traced_peak(run: Any) -> tuple[Any, int]:
    """`run()`을 돌리는 동안의 파이썬 할당 최고치(바이트)."""
    tracemalloc.start()
    try:
        result = run()
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("use_async", [True, False], ids=["request", "fetch"])
async def test_fetch_only_asks_for_identity_on_every_hop(origin: Origin, use_async: bool) -> None:
    """관문이 `Accept-Encoding: identity`를 직접 싣는다 — 호출자 값은 허용 헤더가 아니다."""
    origin.routes["/start"] = "/final"
    if use_async:
        await _request("GET", origin.url("/start"), FETCH_ONLY)
    else:
        await anyio.to_thread.run_sync(
            lambda: ssrf.ssrf_safe_fetch(
                origin.url("/start"), allow_private=True, egress=FETCH_ONLY
            )
        )
    assert [r["headers"]["accept-encoding"] for r in origin.requests] == ["identity", "identity"]
    with pytest.raises(EgressBlockedError) as info:
        await _request("GET", origin.url(), FETCH_ONLY, headers={"Accept-Encoding": "gzip"})
    assert info.value.reason == "header"


@pytest.mark.parametrize("use_async", [True, False], ids=["request", "fetch"])
@pytest.mark.parametrize("encoding", ["gzip, gzip", "gzip", "br", "deflate"])
async def test_fetch_only_rejects_encoded_response_before_decoding(
    origin: Origin, use_async: bool, encoding: str
) -> None:
    """압축 응답은 본문을 읽기(해제하기) 전에 거부한다 — 상한 1 KiB인데 8 MiB로 풀리지 않는다."""
    origin.encoded["/bomb"] = (encoding, _BOMB)
    before = _blocked_count("encoded_response")

    def fetch() -> Any:
        return ssrf.ssrf_safe_fetch(
            origin.url("/bomb"), allow_private=True, egress=FETCH_ONLY, max_bytes=1024
        )

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(EgressBlockedError) as info:
            if use_async:
                await _request("GET", origin.url("/bomb"), FETCH_ONLY, max_bytes=1024)
            else:
                await anyio.to_thread.run_sync(fetch)
    assert info.value.reason == "encoded_response"
    assert [(e["host"], e["reason"]) for e in logs if e["event"] == "agent_http_blocked"] == [
        ("127.0.0.1", "encoded_response")
    ]
    assert _blocked_count("encoded_response") == before + 1


@pytest.mark.parametrize("use_async", [True, False], ids=["request", "fetch"])
def test_pinned_encoded_response_is_rejected_within_the_cap(
    pinned: _Pinned, use_async: bool
) -> None:
    """핀 분기도 같다. 해제하지 않으므로 할당 최고치가 해제 크기(8 MiB)에 한참 못 미친다."""
    pinned.encoded["a.test/bomb"] = ("gzip, gzip", _BOMB)

    def run() -> Any:
        with pytest.raises(EgressBlockedError) as info:
            if use_async:
                anyio.run(
                    lambda: ssrf.ssrf_safe_request(
                        "GET", "https://a.test/bomb", egress=FETCH_ONLY, max_bytes=1024
                    )
                )
            else:
                ssrf.ssrf_safe_fetch("https://a.test/bomb", egress=FETCH_ONLY, max_bytes=1024)
        return info.value.reason

    reason, peak = _traced_peak(run)
    assert reason == "encoded_response"
    assert peak < 1 << 20
    assert pinned.seen[0].headers["accept-encoding"] == "identity"


@pytest.mark.parametrize("use_async", [True, False], ids=["request", "fetch"])
def test_fetch_only_caps_raw_bytes_and_keeps_identity_responses(
    pinned: _Pinned, use_async: bool
) -> None:
    pinned.encoded["a.test/plain"] = ("identity", b"x" * 1000)
    pinned.encoded["a.test/big"] = ("identity", b"x" * 1025)

    def get(path: str) -> Any:
        url = f"https://a.test/{path}"
        if use_async:
            return anyio.run(
                lambda: ssrf.ssrf_safe_request("GET", url, egress=FETCH_ONLY, max_bytes=1024)
            )
        return ssrf.ssrf_safe_fetch(url, egress=FETCH_ONLY, max_bytes=1024)

    assert get("plain").content == b"x" * 1000
    assert get("x").text == "ok"  # Content-Encoding 없음
    with pytest.raises(SSRFBlockedError, match="너무 큽니다"):
        get("big")


@pytest.mark.parametrize("policy", [None, OPEN], ids=["none", "open"])
def test_open_mode_still_negotiates_and_decodes_compression(
    pinned: _Pinned, policy: EgressPolicy | None
) -> None:
    """open은 관문이 고정한 `gzip, deflate`를 보내고 gzip 응답을 풀어 준다(#413)."""
    pinned.encoded["a.test/gz"] = ("gzip", gzip.compress(b"hello"))
    res = ssrf.ssrf_safe_fetch("https://a.test/gz", egress=policy)
    assert res.text == "hello"
    assert pinned.seen[0].headers["accept-encoding"] == "gzip, deflate"
    res = anyio.run(lambda: ssrf.ssrf_safe_request("GET", "https://a.test/gz", egress=policy))
    assert res.text == "hello"


@pytest.mark.parametrize("policy", [None, FETCH_ONLY], ids=["none", "fetch_only"])
async def test_async_hop_dns_is_cancellable(
    pinned: _Pinned, monkeypatch: pytest.MonkeyPatch, policy: EgressPolicy | None
) -> None:
    """느린 DNS 동안에도 이벤트 루프가 돈다 — 호출측 `fail_after`가 제한 시간 안에 취소한다."""

    def slow_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        time.sleep(1.0)
        return [(0, 0, 0, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", slow_getaddrinfo)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        with anyio.fail_after(0.05):
            await ssrf.ssrf_safe_request("GET", "https://a.test/x", egress=policy)
    assert time.monotonic() - started < 0.5
    assert pinned.seen == []
