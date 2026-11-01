"""속도 제한의 클라이언트 IP를 ASGI 서버에 맡긴다 (#290).

계획 `docs/plans/rate-limit-client-ip/plan.md` 수락 기준 1~4다. 키 계산(`_client_key`)은 실경로다.
신뢰 프록시 판단은 실제 uvicorn `ProxyHeadersMiddleware`를 앞에 둔 ASGI 앱으로 확인한다. uvicorn은
코어의 직접 의존이 아니라 `mcp`의 전이 의존이다 — 빠지면 import 오류로 드러난다(건너뛰지 않는다).
"""

from typing import Any

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from structlog.testing import capture_logs
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from biznotes_core.core import rate_limit
from biznotes_core.core.rate_limit import _client_key

WARNING = "rate_limit_proxy_headers_ignored"


@pytest.fixture(autouse=True)
def _fresh_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rate_limit, "_proxy_headers_warned", False)


def _request(client: tuple[str, int] | None, *forwarded: str) -> Request:
    headers = [(b"x-forwarded-for", value.encode()) for value in forwarded]
    scope: dict[str, Any] = {"type": "http", "headers": headers, "client": client}
    return Request(scope)


# ── 수락 기준 1: 클라이언트가 보낸 X-Forwarded-For로 키가 바뀌지 않는다 ────


@pytest.mark.parametrize("scope", ["ip", "user"])
@pytest.mark.parametrize(
    "forwarded", [(), ("1.1.1.1",), ("9.9.9.9, 8.8.8.8",), ("1.1.1.1", "2.2.2.2")]
)
def test_direct_clients_cannot_choose_their_key(scope: str, forwarded: tuple[str, ...]) -> None:
    """`scope="user"`도 토큰이 없으면 IP 키로 떨어진다."""
    with capture_logs():
        key = _client_key(_request(("203.0.113.7", 50000), *forwarded), scope)
    assert key == "ip:203.0.113.7"


def test_missing_client_is_unknown() -> None:
    """수락 기준 4 — 유닉스 소켓 등 피어가 없으면 지금처럼 `ip:unknown`이다."""
    assert _client_key(_request(None), "ip") == "ip:unknown"


# ── 수락 기준 2: uvicorn이 신뢰 프록시 경계로 고친 client를 쓴다 ──────────


async def _key_through_proxy(peer: str, headers: list[tuple[str, str]]) -> tuple[str, list]:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        response = PlainTextResponse(_client_key(Request(scope), "ip"))
        await response(scope, receive, send)

    proxied = ProxyHeadersMiddleware(app, trusted_hosts="10.0.0.0/8")
    transport = httpx.ASGITransport(app=proxied, client=(peer, 40000))
    with capture_logs() as logs:
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            res = await client.get("/", headers=headers)
    return res.text, logs


@pytest.mark.parametrize(
    ("headers", "key"),
    [
        # 프록시가 덧붙인 오른쪽 값 — 왼쪽(위조 가능) 값이 아니다
        ([("x-forwarded-for", "1.1.1.1, 198.51.100.9")], "ip:198.51.100.9"),
        ([("x-forwarded-for", "1.1.1.1"), ("x-forwarded-for", "198.51.100.9")], "ip:198.51.100.9"),
        ([("x-forwarded-for", "198.51.100.9:4711")], "ip:198.51.100.9"),
        # IPv6는 /64로 묶인다(#315)
        ([("x-forwarded-for", "[2001:db8::1]:443")], "ip:2001:db8::/64"),
        ([("x-forwarded-for", "2001:db8::2")], "ip:2001:db8::/64"),
    ],
)
async def test_trusted_proxy_headers_pick_the_real_client_without_warning(
    headers: list[tuple[str, str]], key: str
) -> None:
    text, logs = await _key_through_proxy("10.0.0.5", headers)

    assert text == key
    assert [e for e in logs if e["event"] == WARNING] == []  # 적용된 경우에는 경고가 없다


async def test_untrusted_peer_headers_are_ignored_and_warned_once() -> None:
    text, logs = await _key_through_proxy("93.184.216.34", [("x-forwarded-for", "1.1.1.1")])
    again, more = await _key_through_proxy("93.184.216.34", [("x-forwarded-for", "2.2.2.2")])

    assert text == again == "ip:93.184.216.34"
    [warning] = [e for e in logs if e["event"] == WARNING]
    assert warning["log_level"] == "warning" and warning["peer_kind"] == "global"
    assert "1.1.1.1" not in str(warning) and "93.184.216.34" not in str(warning)
    assert [e for e in more if e["event"] == WARNING] == []  # 프로세스당 한 번


# ── 수락 기준 3: 진단 경고의 경계 ────────────────────────────────────────


def test_no_warning_without_forwarded_headers_or_with_only_empty_entries() -> None:
    with capture_logs() as logs:
        _client_key(_request(("10.0.0.5", 1)), "ip")
        _client_key(_request(("10.0.0.5", 1), " , "), "ip")
    assert [e for e in logs if e["event"] == WARNING] == []


def test_missing_client_with_forwarded_headers_is_warned() -> None:
    """유닉스 소켓 배포는 프록시 헤더를 적용할 수 없다 — 모든 요청이 `ip:unknown` 버킷을 쓴다."""
    with capture_logs() as logs:
        assert _client_key(_request(None, "198.51.100.9"), "ip") == "ip:unknown"
    [warning] = [e for e in logs if e["event"] == WARNING]
    assert warning["peer_kind"] == "none"


@pytest.mark.parametrize(
    ("peer", "kind"),
    [("10.0.0.5", "private"), ("127.0.0.1", "loopback"), ("93.184.216.34", "global"),
     ("testclient", "not_ip")],
)  # fmt: skip
def test_warning_logs_only_the_peer_address_kind(peer: str, kind: str) -> None:
    with capture_logs() as logs:
        _client_key(_request((peer, 1), "198.51.100.9"), "ip")
    [warning] = [e for e in logs if e["event"] == WARNING]
    assert warning["peer_kind"] == kind and peer not in str(warning)


# ── #315: IPv6는 /64로 묶는다 (docs/plans/rate-limit-ipv6-prefix.md) ─────


@pytest.mark.parametrize("scope", ["ip", "user"])
def test_addresses_in_the_same_ipv6_64_share_a_bucket(scope: str) -> None:
    """같은 /64 안에서 주소를 바꿔도 키가 같다 — 주소 회전으로 IP당 제한을 피하지 못한다."""
    keys = {
        _client_key(_request((host, 1)), scope)
        for host in ("2001:db8:1:2::1", "2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:DB8:1:2:a::b")
    }
    assert keys == {"ip:2001:db8:1:2::/64"}


def test_different_ipv6_64s_get_different_buckets() -> None:
    first = _client_key(_request(("2001:db8:1:2::1", 1)), "ip")
    second = _client_key(_request(("2001:db8:1:3::1", 1)), "ip")
    assert first != second


@pytest.mark.parametrize(
    ("host", "key"),
    [
        ("203.0.113.7", "ip:203.0.113.7"),  # IPv4는 그대로
        ("::ffff:203.0.113.7", "ip:203.0.113.7"),  # IPv4-mapped는 IPv4로 — /64로 묶지 않는다
        ("64:ff9b::203.0.113.7", "ip:203.0.113.7"),  # NAT64 잘 알려진 접두도 IPv4로(#327 리뷰)
        ("64:ff9b::198.51.100.9", "ip:198.51.100.9"),
        ("fe80::1%eth0", "ip:fe80::/64"),  # 존 ID는 떼고 판정한다
        ("testclient", "ip:testclient"),  # IP가 아니면 그대로
    ],
)
def test_ipv6_grouping_edge_cases(host: str, key: str) -> None:
    assert _client_key(_request((host, 1)), "ip") == key
