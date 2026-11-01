"""SSRF 관문 단위 테스트 — 코어가 소유한다.

구현이 `biznotes_core.core.ssrf`로 옮겨졌으므로 검증도 코어 리포에 둔다.
프로젝트 리포에는 API 레벨 통합 테스트(`test_security.py`)만 남는다.
"""

import inspect
import socket
from types import SimpleNamespace

import httpx
import pytest
import structlog.testing

from biznotes_core.core.ssrf import SSRFBlockedError, validate_external_url

REAL_HTTPX_CLIENT = httpx.Client
REAL_HTTPX_ASYNC_CLIENT = httpx.AsyncClient

# ---------- SSRF ----------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/admin",
        "http://localhost/x",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/router",
        "http://169.254.169.254/latest/meta-data",  # 클라우드 메타데이터
        "ftp://example.com/file",
        "file:///etc/passwd",
    ],
)
def test_ssrf_blocked(url: str):
    with pytest.raises(SSRFBlockedError):
        validate_external_url(url)


def test_ssrf_allows_public():
    validate_external_url("https://en.wikipedia.org/wiki/Espresso")


def test_ssrf_allow_private_flag():
    validate_external_url("http://localhost:9999/x", allow_private=True)


# ---------- R5-1: fetch 경로 단일 관문 (매 홉 재검증) ----------


def _fake_getaddrinfo(mapping: dict[str, str | list[str]]):
    """host → ip 매핑으로 socket.getaddrinfo를 대체하는 스텁."""

    def _inner(host, *args, **kwargs):
        value = mapping.get(host)
        if value is None:
            raise socket.gaierror(f"unknown host {host}")
        ips = value if isinstance(value, list) else [value]
        return [(0, 0, 0, "", (ip, 0)) for ip in ips]

    return _inner


def _make_resp(spec: dict, url: str) -> httpx.Response:
    """홉 스펙 → httpx.Response(스트리밍 본문). spec: status/location/body."""
    body = spec.get("body", b"")
    if isinstance(body, str):
        body = body.encode()
    headers = {}
    if "location" in spec:
        headers["location"] = spec["location"]
    return httpx.Response(
        spec["status"],
        headers=headers,
        stream=httpx.ByteStream(body),
        request=httpx.Request(spec.get("method", "GET"), url),
    )


def _install_fake_http(monkeypatch, hops: list[dict], *, is_async: bool = False) -> dict:
    """홉 스펙 리스트를 순서대로 반환하는 httpx.Client/AsyncClient stub 설치.

    각 홉은 client.stream(...) 컨텍스트가 하나씩 소비한다.
    """
    from biznotes_core.core import ssrf

    seq = iter(hops)
    state = {"clients": [], "streams": [], "client_exits": 0, "stream_exits": 0}

    class _Stream:
        def __init__(self, method, url, **kwargs):
            spec = next(seq)
            state["streams"].append({"method": method, "url": url, **kwargs})
            self._exception = spec.get("exception")
            self._resp = _make_resp(spec, url)

        def _trace(self):
            stream = state["streams"][-1]
            callback = stream.get("extensions", {}).get("trace")
            if callback is None:
                return
            host = httpx.URL(stream["url"]).host
            port = httpx.URL(stream["url"]).port or (
                443 if httpx.URL(stream["url"]).scheme == "https" else 80
            )
            callback("connection.connect_tcp.started", {"host": host, "port": port})
            if httpx.URL(stream["url"]).scheme == "https":
                callback(
                    "connection.start_tls.started",
                    {"server_hostname": stream["extensions"]["sni_hostname"]},
                )

        async def _atrace(self):
            stream = state["streams"][-1]
            callback = stream.get("extensions", {}).get("trace")
            if callback is None:
                return
            host = httpx.URL(stream["url"]).host
            port = httpx.URL(stream["url"]).port or (
                443 if httpx.URL(stream["url"]).scheme == "https" else 80
            )
            await callback("connection.connect_tcp.started", {"host": host, "port": port})
            if httpx.URL(stream["url"]).scheme == "https":
                await callback(
                    "connection.start_tls.started",
                    {"server_hostname": stream["extensions"]["sni_hostname"]},
                )

        def __enter__(self):
            self._trace()
            if self._exception is not None:
                raise self._exception
            return self._resp

        def __exit__(self, *a):
            state["stream_exits"] += 1
            return False

        async def __aenter__(self):
            await self._atrace()
            if self._exception is not None:
                raise self._exception
            return self._resp

        async def __aexit__(self, *a):
            state["stream_exits"] += 1
            return False

    class _Client:
        def __init__(self, *a, **k):
            state["clients"].append({"args": a, "kwargs": k})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            state["client_exits"] += 1
            return False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            state["client_exits"] += 1
            return False

        def stream(self, method, url, **kwargs):
            return _Stream(method, url, **kwargs)

    monkeypatch.setattr(ssrf.httpx, "AsyncClient" if is_async else "Client", _Client)
    return state


def test_ssrf_fetch_blocks_redirect_to_private(monkeypatch):
    """수락 기준(CR-R5-05): 공인 URL → 사설 IP 리다이렉트가 fetch 관문에서 차단됨."""
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        _fake_getaddrinfo({"public.test": "93.184.216.34", "internal.test": "10.0.0.9"}),
    )
    _install_fake_http(monkeypatch, [{"status": 302, "location": "http://internal.test/secret"}])

    with pytest.raises(ssrf.SSRFBlockedError):
        ssrf.ssrf_safe_fetch("http://public.test/page")


def test_ssrf_fetch_blocks_relative_redirect_to_private(monkeypatch):
    """상대 경로 리다이렉트도 매 홉 재검증 대상 — 같은 호스트가 rebinding되면 차단."""
    from biznotes_core.core import ssrf

    # 첫 홉: public.test → 공인, 상대 리다이렉트 /next. 재검증 시 사설로 재해석.
    resolved = {"public.test": "93.184.216.34"}
    monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo(resolved))
    _install_fake_http(monkeypatch, [{"status": 302, "location": "/next"}])

    # 두 번째 홉 검증 직전에 public.test를 사설로 rebinding.
    orig = ssrf._resolve_public_ips
    calls = {"n": 0}

    def _rebind(host):
        calls["n"] += 1
        if calls["n"] >= 2:
            resolved["public.test"] = "10.1.2.3"
        return orig(host)

    monkeypatch.setattr(ssrf, "_resolve_public_ips", _rebind)

    with pytest.raises(ssrf.SSRFBlockedError):
        ssrf.ssrf_safe_fetch("http://public.test/page")


def test_ssrf_fetch_multi_hop_public_ok(monkeypatch):
    """공인→공인 다중 홉 리다이렉트는 최종 응답 반환."""
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        _fake_getaddrinfo({"a.test": "93.184.216.34", "b.test": "93.184.216.35"}),
    )
    _install_fake_http(
        monkeypatch,
        [
            {"status": 302, "location": "http://b.test/step2"},
            {"status": 200, "body": "final"},
        ],
    )
    resp = ssrf.ssrf_safe_fetch("http://a.test/step1")
    assert resp.status_code == 200
    assert resp.text == "final"


def test_ssrf_fetch_redirect_without_location(monkeypatch):
    from biznotes_core.core import ssrf

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"a.test": "93.184.216.34"}))
    # 빈 Location — 헤더가 아예 없는 3xx는 최종 응답이다(#424, test_ssrf_non_redirect_3xx)
    _install_fake_http(monkeypatch, [{"status": 302, "location": ""}])
    with pytest.raises(ssrf.SSRFBlockedError, match="Location"):
        ssrf.ssrf_safe_fetch("http://a.test/x")


def test_ssrf_fetch_redirect_loop_exhausted(monkeypatch):
    from biznotes_core.core import ssrf

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"a.test": "93.184.216.34"}))
    # 계속 자기 자신으로 리다이렉트 → 홉 소진.
    _install_fake_http(
        monkeypatch, [{"status": 302, "location": "http://a.test/loop"}] * 10
    )
    with pytest.raises(ssrf.SSRFBlockedError, match="리다이렉트가 너무 많"):
        ssrf.ssrf_safe_fetch("http://a.test/loop")


def test_ssrf_fetch_size_cap_streaming(monkeypatch):
    """Content-Length 없이도 스트리밍 누적 바이트가 상한을 넘으면 차단."""
    from biznotes_core.core import ssrf

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"a.test": "93.184.216.34"}))
    _install_fake_http(monkeypatch, [{"status": 200, "body": b"x" * 5000}])
    with pytest.raises(ssrf.SSRFBlockedError, match="너무 큽"):
        ssrf.ssrf_safe_fetch("http://a.test/big", max_bytes=1000)


def test_ssrf_fetch_allow_private_bypass(monkeypatch):
    """allow_private=True면 사설 IP 검증을 건너뛴다(개발 전용)."""
    from biznotes_core.core import ssrf

    # 검증을 타면 실패할 사설 IP지만 allow_private로 통과.
    monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"local.test": "10.0.0.1"}))
    _install_fake_http(monkeypatch, [{"status": 200, "body": "ok"}])
    resp = ssrf.ssrf_safe_fetch("http://local.test/x", allow_private=True)
    assert resp.text == "ok"


def test_ssrf_fetch_allows_public_no_redirect(monkeypatch):
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"public.test": "93.184.216.34"})
    )
    _install_fake_http(monkeypatch, [{"status": 200, "body": "hello"}])
    resp = ssrf.ssrf_safe_fetch("http://public.test/page")
    assert resp.status_code == 200
    assert resp.text == "hello"


def test_ssrf_fetch_rejects_bad_scheme():
    from biznotes_core.core import ssrf

    with pytest.raises(ssrf.SSRFBlockedError):
        ssrf.ssrf_safe_fetch("file:///etc/passwd")


async def test_ssrf_request_blocks_redirect_to_private(monkeypatch):
    """비동기 관문(agent http 노드)도 리다이렉트→사설을 차단."""
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        _fake_getaddrinfo({"public.test": "93.184.216.34", "internal.test": "127.0.0.1"}),
    )
    _install_fake_http(
        monkeypatch, [{"status": 307, "location": "http://internal.test/x"}], is_async=True
    )
    with pytest.raises(ssrf.SSRFBlockedError):
        await ssrf.ssrf_safe_request("POST", "http://public.test/api")


async def test_ssrf_request_public_ok(monkeypatch):
    from biznotes_core.core import ssrf

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"a.test": "93.184.216.34"}))
    _install_fake_http(monkeypatch, [{"status": 200, "body": "pong"}], is_async=True)
    resp = await ssrf.ssrf_safe_request("POST", "http://a.test/api", json={"q": 1})
    assert resp.status_code == 200
    assert resp.text == "pong"


async def test_ssrf_request_redirect_downgrades_post_to_get(monkeypatch):
    """계약(R5-1-03): 307 POST 리다이렉트는 다음 홉에서 GET·무body로 강등된다."""
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        _fake_getaddrinfo({"a.test": "93.184.216.34", "b.test": "93.184.216.35"}),
    )

    captured: list[dict] = []
    seq = iter(
        [
            {"status": 307, "location": "http://b.test/next"},
            {"status": 200, "body": "done"},
        ]
    )

    class _Stream:
        def __init__(self, method, url, **kwargs):
            captured.append({"method": method, "url": url, "json": kwargs.get("json")})
            spec = next(seq)
            self._resp = _make_resp(spec, url)

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *a):
            return False

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, **kwargs):
            return _Stream(method, url, **kwargs)

    monkeypatch.setattr(ssrf.httpx, "AsyncClient", _Client)

    resp = await ssrf.ssrf_safe_request("POST", "http://a.test/api", json={"q": 1})
    assert resp.text == "done"
    # 첫 홉: 원 메서드·body 유지. 둘째 홉: GET·body 폐기.
    assert captured[0]["method"] == "POST" and captured[0]["json"] == {"q": 1}
    assert captured[1]["method"] == "GET" and captured[1]["json"] is None


@pytest.mark.parametrize(
    ("ip", "blocked"),
    [
        ("::ffff:127.0.0.1", True),
        ("::ffff:169.254.169.254", True),
        ("::ffff:10.0.0.1", True),
        ("::ffff:8.8.8.8", False),
    ],
)
def test_ssrf_ipv4_mapped_ipv6_is_classified_by_mapped_address(ip, blocked):
    from biznotes_core.core import ssrf

    assert ssrf._is_private(ip) is blocked


def test_ssrf_mixed_public_a_and_private_aaaa_is_blocked(monkeypatch):
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        _fake_getaddrinfo({"mixed.test": ["93.184.216.34", "fd00::1"]}),
    )
    # 문구는 호스트까지만 — 해석 IP는 로그에만 남는다(#42)
    with pytest.raises(ssrf.SSRFBlockedError, match="mixed.test"):
        ssrf.ssrf_safe_fetch("https://mixed.test/")


@pytest.mark.parametrize(
    ("ips", "leaked"),
    [
        (["10.0.0.9"], "10.0.0.9"),
        (["fd00::1"], "fd00::1"),
        (["::ffff:169.254.169.254"], "169.254.169.254"),
        (["93.184.216.34", "127.0.0.1"], "127.0.0.1"),
    ],
)
def test_private_address_block_message_hides_the_resolved_ip(monkeypatch, ips, leaked):
    """#42 — 내부 DNS 해석 결과는 사용자에게 돌아가는 문구에 들어가지 않는다.

    이 문구는 소스 `error_message`·API 422 응답·에이전트 실행 결과로 가공 없이 나간다.
    """
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"attacker.test": ips})
    )

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(ssrf.SSRFBlockedError) as exc:
            ssrf.validate_external_url("https://attacker.test/x")

    message = str(exc.value)
    assert leaked not in message
    assert "attacker.test" in message
    # 진단용 IP는 서버 로그에만 남는다
    blocked = [e for e in logs if e["event"] == "ssrf_private_address_blocked"]
    assert [(e["log_level"], e["host"]) for e in blocked] == [("warning", "attacker.test")]
    assert leaked in blocked[0]["resolved_ip"]


def test_private_address_block_message_is_the_same_on_the_fetch_path(monkeypatch):
    """#42 — fetch 관문(매 홉 재검증)도 같은 문구다: 노출 경로가 한 문구를 공유한다."""
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"attacker.test": "10.1.2.3"})
    )

    with pytest.raises(ssrf.SSRFBlockedError) as exc:
        ssrf.ssrf_safe_fetch("https://attacker.test/x")

    assert str(exc.value) == "내부망 주소로의 요청이 차단되었습니다: attacker.test"


async def test_private_address_block_message_is_the_same_on_the_async_path(monkeypatch):
    """#42 — 에이전트 http 노드가 쓰는 비동기 관문도 같은 문구여야 한다."""
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"attacker.test": "10.1.2.3"})
    )

    with pytest.raises(ssrf.SSRFBlockedError) as exc:
        await ssrf.ssrf_safe_request("POST", "https://attacker.test/x", json={"a": 1})

    assert str(exc.value) == "내부망 주소로의 요청이 차단되었습니다: attacker.test"


def test_public_address_does_not_log_a_block_warning(monkeypatch):
    """#42 — 차단 경고는 차단할 때만 남는다(정상 fetch마다 warning이 쌓이면 안 된다)."""
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"public.test": "93.184.216.34"})
    )

    with structlog.testing.capture_logs() as logs:
        ssrf.validate_external_url("https://public.test/x")

    assert [e for e in logs if e["event"] == "ssrf_private_address_blocked"] == []


@pytest.mark.parametrize(
    ("url", "resolved_ip", "expected_url", "expected_host"),
    [
        (
            "https://example.test/path?q=1",
            "93.184.216.34",
            "https://93.184.216.34/path?q=1",
            "example.test",
        ),
        (
            "https://example.test:444/path",
            "2001:4860:4860::8888",
            "https://[2001:4860:4860::8888]:444/path",
            "example.test:444",
        ),
        (
            "https://[2001:4860:4860:0:0:0:0:8888]:444/path",
            "2001:4860:4860::8844",
            "https://[2001:4860:4860::8844]:444/path",
            # Host는 원 URL 표기를 그대로 보존(비핀 httpx 요청과 동일한 authority).
            # 핀 URL의 IP는 getaddrinfo 정규(압축) 표기다.
            "[2001:4860:4860:0:0:0:0:8888]:444",
        ),
    ],
)
def test_ssrf_fetch_pins_connection_and_preserves_host_sni_and_original_request(
    monkeypatch, url, resolved_ip, expected_url, expected_host
):
    from biznotes_core.core import ssrf

    original_host = httpx.URL(url).host
    monkeypatch.setattr(
        ssrf.socket, "getaddrinfo", _fake_getaddrinfo({original_host: resolved_ip})
    )
    state = _install_fake_http(monkeypatch, [{"status": 200, "body": "ok"}])

    response = ssrf.ssrf_safe_fetch(url, headers={"X-Test": "kept"})

    stream = state["streams"][0]
    assert str(stream["url"]) == expected_url
    # 관문이 open 모드 Accept-Encoding을 직접 싣는다(#413)
    assert stream["headers"] == {
        "X-Test": "kept", "Accept-Encoding": "gzip, deflate", "Host": expected_host
    }
    assert stream["extensions"]["sni_hostname"] == original_host
    assert callable(stream["extensions"]["trace"])
    assert not inspect.iscoroutinefunction(stream["extensions"]["trace"])
    assert "verify" not in state["clients"][0]["kwargs"]
    assert state["clients"][0]["kwargs"]["follow_redirects"] is False
    assert state["clients"][0]["kwargs"]["trust_env"] is False
    assert state["client_exits"] == 1
    assert state["stream_exits"] == 1
    assert str(response.request.url) == str(httpx.URL(url))


async def test_ssrf_request_pins_async_connection_and_preserves_original_request(monkeypatch):
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"api.test": "93.184.216.34"})
    )
    state = _install_fake_http(
        monkeypatch, [{"status": 200, "body": "ok"}], is_async=True
    )

    response = await ssrf.ssrf_safe_request(
        "POST", "https://api.test:444/v1", headers={"X-Test": "kept"}, json={"x": 1}
    )

    stream = state["streams"][0]
    assert str(stream["url"]) == "https://93.184.216.34:444/v1"
    assert stream["headers"] == {
        "X-Test": "kept", "Accept-Encoding": "gzip, deflate", "Host": "api.test:444"
    }
    assert stream["json"] == {"x": 1}
    assert stream["extensions"]["sni_hostname"] == "api.test"
    assert inspect.iscoroutinefunction(stream["extensions"]["trace"])
    assert "verify" not in state["clients"][0]["kwargs"]
    assert state["clients"][0]["kwargs"]["trust_env"] is False
    assert state["client_exits"] == 1
    assert str(response.request.url) == "https://api.test:444/v1"


def test_ssrf_fetch_uses_verified_ip_after_dns_rebinding(monkeypatch):
    """(f) 검증 직후 DNS가 사설로 바뀌어도 연결 대상은 검증 IP — 관문은 재해석하지 않는다.

    DNS 경계만 가짜로 둔다: 첫 해석(검증)은 공인, 그 뒤 해석은 루프백을 돌려준다.
    호스트명으로 연결하는 구현이면 stream URL 호스트가 `rebind.test`가 되어 실패한다.
    """
    from biznotes_core.core import ssrf

    calls: list[str] = []

    def rebinding_getaddrinfo(host, *args, **kwargs):
        calls.append(host)
        ip = "93.184.216.34" if len(calls) == 1 else "127.0.0.1"
        return [(0, 0, 0, "", (ip, 0))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", rebinding_getaddrinfo)
    state = _install_fake_http(monkeypatch, [{"status": 200, "body": "ok"}])

    response = ssrf.ssrf_safe_fetch("http://rebind.test/x")

    assert response.text == "ok"
    assert calls == ["rebind.test"]  # 홉당 1회 해석 — 연결 직전 재해석 없음
    assert httpx.URL(state["streams"][0]["url"]).host == "93.184.216.34"
    assert state["streams"][0]["headers"]["Host"] == "rebind.test"


def test_ssrf_redirect_same_ip_uses_new_clients_and_separate_host_sni(monkeypatch):
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        _fake_getaddrinfo({"a.test": "93.184.216.34", "b.test": "93.184.216.34"}),
    )
    state = _install_fake_http(
        monkeypatch,
        [
            {"status": 302, "location": "https://b.test/final"},
            {"status": 200, "body": "ok"},
        ],
    )

    response = ssrf.ssrf_safe_fetch("https://a.test/start")

    assert response.text == "ok"
    assert len(state["clients"]) == 2
    assert state["client_exits"] == 2
    assert [s["headers"]["Host"] for s in state["streams"]] == ["a.test", "b.test"]
    assert [s["extensions"]["sni_hostname"] for s in state["streams"]] == [
        "a.test",
        "b.test",
    ]


def test_ssrf_fetch_falls_back_over_verified_ips_with_shared_timeout(monkeypatch):
    from biznotes_core.core import ssrf

    request = httpx.Request("GET", "https://fallback.test/")
    first_error = httpx.ConnectTimeout("first unavailable", request=request)
    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        _fake_getaddrinfo({"fallback.test": ["93.184.216.34", "93.184.216.35"]}),
    )
    ticks = iter([100.0, 100.1, 100.4])
    monkeypatch.setattr(ssrf.time, "monotonic", lambda: next(ticks))
    state = _install_fake_http(
        monkeypatch,
        [
            {"status": 599, "exception": first_error},
            {"status": 200, "body": "ok"},
        ],
    )

    response = ssrf.ssrf_safe_fetch("https://fallback.test/", timeout=2.0)

    assert response.text == "ok"
    assert [httpx.URL(s["url"]).host for s in state["streams"]] == [
        "93.184.216.34",
        "93.184.216.35",
    ]
    assert len(state["clients"]) == 2
    assert state["client_exits"] == 2
    timeouts = [c["kwargs"]["timeout"] for c in state["clients"]]
    # read/write/pool = 남은 홉 예산, connect = 남은 검증 IP 수로 나눈 몫(최소 1.0s) — P3-03
    assert [t.read for t in timeouts] == [pytest.approx(1.9), pytest.approx(1.6)]
    assert [t.connect for t in timeouts] == [pytest.approx(1.0), pytest.approx(1.6)]


def test_ssrf_fetch_reraises_last_connection_error(monkeypatch):
    from biznotes_core.core import ssrf

    request = httpx.Request("GET", "https://fallback.test/")
    first_error = httpx.ConnectError("first unavailable", request=request)
    last_error = httpx.ConnectTimeout("last unavailable", request=request)
    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        _fake_getaddrinfo({"fallback.test": ["93.184.216.34", "93.184.216.35"]}),
    )
    state = _install_fake_http(
        monkeypatch,
        [
            {"status": 599, "exception": first_error},
            {"status": 599, "exception": last_error},
        ],
    )

    with pytest.raises(httpx.ConnectTimeout, match="last unavailable") as exc_info:
        ssrf.ssrf_safe_fetch("https://fallback.test/")

    assert exc_info.value is last_error
    assert len(state["clients"]) == 2
    assert state["client_exits"] == 2


def test_ssrf_fetch_does_not_fallback_on_http_error_response(monkeypatch):
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        _fake_getaddrinfo({"errors.test": ["93.184.216.34", "93.184.216.35"]}),
    )
    state = _install_fake_http(monkeypatch, [{"status": 503, "body": "unavailable"}])

    response = ssrf.ssrf_safe_fetch("https://errors.test/")

    assert response.status_code == 503
    assert len(state["clients"]) == 1


def test_ssrf_fetch_redirect_starts_new_verified_ip_set(monkeypatch):
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        _fake_getaddrinfo(
            {
                "a.test": ["93.184.216.34", "93.184.216.35"],
                "b.test": ["93.184.216.36", "93.184.216.37"],
            }
        ),
    )
    state = _install_fake_http(
        monkeypatch,
        [
            {"status": 302, "location": "https://b.test/final"},
            {"status": 200, "body": "ok"},
        ],
    )

    response = ssrf.ssrf_safe_fetch("https://a.test/start")

    assert response.text == "ok"
    assert [httpx.URL(s["url"]).host for s in state["streams"]] == [
        "93.184.216.34",
        "93.184.216.36",
    ]


def test_ssrf_fetch_timeout_budget_is_per_hop_and_shared_across_ip_attempts(monkeypatch):
    """기존 계약(홉마다 timeout)을 유지한다 — 한 홉 안의 검증 IP 시도만 예산을 공유하고
    리다이렉트 후 새 홉은 새 예산으로 시작한다(D-14)."""
    from biznotes_core.core import ssrf

    request = httpx.Request("GET", "https://a.test/")
    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        _fake_getaddrinfo(
            {"a.test": ["93.184.216.34", "93.184.216.35"], "b.test": "93.184.216.36"}
        ),
    )
    # hop1 시작 100.0 → 시도1 100.5(잔여 1.5, ConnectTimeout) → 시도2 101.5(잔여 0.5, 302)
    # hop2 시작 101.6 → 시도 101.7(잔여 1.9 — 홉 단위 새 예산)
    ticks = iter([100.0, 100.5, 101.5, 101.6, 101.7])
    monkeypatch.setattr(ssrf.time, "monotonic", lambda: next(ticks))
    state = _install_fake_http(
        monkeypatch,
        [
            {"status": 599, "exception": httpx.ConnectTimeout("slow", request=request)},
            {"status": 302, "location": "https://b.test/final"},
            {"status": 200, "body": "ok"},
        ],
    )

    response = ssrf.ssrf_safe_fetch("https://a.test/", timeout=2.0)

    assert response.text == "ok"
    timeouts = [c["kwargs"]["timeout"] for c in state["clients"]]
    assert [t.read for t in timeouts] == [
        pytest.approx(1.5),
        pytest.approx(0.5),
        pytest.approx(1.9),
    ]
    assert [t.connect for t in timeouts] == [
        pytest.approx(1.0),
        pytest.approx(0.5),
        pytest.approx(1.9),
    ]


def test_ssrf_fetch_exhausted_ip_budget_raises_connect_timeout(monkeypatch):
    """앞선 실제 오류가 없는 상태에서 예산이 소진되면 기존대로 예산 소진 ConnectTimeout."""
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        _fake_getaddrinfo({"slow.test": ["93.184.216.34", "93.184.216.35"]}),
    )
    ticks = iter([100.0, 101.5])  # 예산 1.0 → 첫 시도 시점에 이미 잔여 -0.5
    monkeypatch.setattr(ssrf.time, "monotonic", lambda: next(ticks))
    state = _install_fake_http(monkeypatch, [{"status": 200, "body": "ok"}])

    with pytest.raises(httpx.ConnectTimeout, match="예산이 소진") as exc_info:
        ssrf.ssrf_safe_fetch("https://slow.test/", timeout=1.0)

    assert exc_info.value.__cause__ is None
    assert state["clients"] == []  # 예산이 없으면 Client를 만들지 않는다


def test_ssrf_fetch_budget_exhaustion_preserves_first_connection_error(monkeypatch):
    """(P3-15) 예산 소진이 앞선 IP의 실제 실패 원인을 덮지 않는다.

    첫 IP가 실제 `ConnectError`로 실패한 뒤 예산이 소진되면, 호출자에게 전달되는 예외는
    **실제 원인**이고 예산 소진 예외는 `__cause__`로 남는다.
    """
    from biznotes_core.core import ssrf

    request = httpx.Request("GET", "https://slow.test/")
    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        _fake_getaddrinfo({"slow.test": ["93.184.216.34", "93.184.216.35"]}),
    )
    ticks = iter([100.0, 100.5, 101.2])  # 예산 1.0 → 두 번째 시도 시점 잔여 -0.2
    monkeypatch.setattr(ssrf.time, "monotonic", lambda: next(ticks))
    state = _install_fake_http(
        monkeypatch,
        [{"status": 599, "exception": httpx.ConnectError("refused", request=request)}],
    )

    with pytest.raises(httpx.ConnectError, match="refused") as exc_info:
        ssrf.ssrf_safe_fetch("https://slow.test/", timeout=1.0)

    cause = exc_info.value.__cause__
    assert isinstance(cause, httpx.ConnectTimeout)
    assert "예산이 소진" in str(cause)  # 예산 소진은 맥락으로 보존
    assert len(state["clients"]) == 1
    assert len(state["streams"]) == 1


async def test_ssrf_request_budget_exhaustion_preserves_first_connection_error(monkeypatch):
    """(P3-15) 비동기 경로도 동일 — 실제 원인 전달 + 예산 소진은 `__cause__`."""
    from biznotes_core.core import ssrf

    request = httpx.Request("POST", "https://slow.test/")
    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        _fake_getaddrinfo({"slow.test": ["93.184.216.34", "93.184.216.35"]}),
    )
    ticks = iter([100.0, 100.5, 101.2])
    # 비동기 경로에서는 `ssrf.time` 모듈 참조만 교체한다 — `time.monotonic` 자체를 바꾸면
    # 이벤트 루프가 같은 가짜 시계를 소비해 테스트가 아니라 루프가 먼저 멈춘다.
    monkeypatch.setattr(ssrf, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    _install_fake_http(
        monkeypatch,
        [{"status": 599, "exception": httpx.ConnectError("refused", request=request)}],
        is_async=True,
    )

    with pytest.raises(httpx.ConnectError, match="refused") as exc_info:
        await ssrf.ssrf_safe_request("POST", "https://slow.test/", timeout=1.0)

    assert isinstance(exc_info.value.__cause__, httpx.ConnectTimeout)
    assert "예산이 소진" in str(exc_info.value.__cause__)


def test_ssrf_fetch_relative_redirect_joins_original_url_not_pinned_ip(monkeypatch):
    """(e) 상대 Location은 핀된 IP URL이 아니라 원 URL 기준으로 결합 — 다음 홉도 원
    호스트로 재검증·재핀되고 Host/반환 request URL은 원 호스트를 유지한다."""
    from biznotes_core.core import ssrf

    calls: list[str] = []

    def resolver(host, *args, **kwargs):
        calls.append(host)
        return [(0, 0, 0, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", resolver)
    state = _install_fake_http(
        monkeypatch,
        [{"status": 302, "location": "/next?x=1"}, {"status": 200, "body": "ok"}],
    )

    response = ssrf.ssrf_safe_fetch("https://a.test:8443/start")

    assert response.text == "ok"
    assert calls == ["a.test", "a.test"]
    assert [str(s["url"]) for s in state["streams"]] == [
        "https://93.184.216.34:8443/start",
        "https://93.184.216.34:8443/next?x=1",
    ]
    assert [s["headers"]["Host"] for s in state["streams"]] == ["a.test:8443", "a.test:8443"]
    assert str(response.request.url) == "https://a.test:8443/next?x=1"


def test_ssrf_is_private_uses_explicit_ipv4_mapped_recursion(monkeypatch):
    """(P3-09) `_is_private`의 `ipv4_mapped` 명시 재귀가 제거되면 실패한다.

    호스트·컨테이너 Python은 `ipaddress`가 IPv4-mapped 판정을 이미 위임하므로(7-11),
    파라미터 테스트만으로는 재귀 제거를 탐지할 수 없다. 여기서는 IPv6 쪽 분류 속성을 전부
    False로 고정해 **위임이 가려줄 수 없는 조건**을 만든 뒤, 매핑된 IPv4로 재귀해 판정하는지
    본다(재귀가 없으면 `::ffff:127.0.0.1`이 공인으로 통과해 실패).
    """
    import ipaddress

    from biznotes_core.core import ssrf

    for prop in (
        "is_private",
        "is_loopback",
        "is_link_local",
        "is_multicast",
        "is_reserved",
        "is_unspecified",
    ):
        monkeypatch.setattr(ipaddress.IPv6Address, prop, property(lambda self: False))

    assert ssrf._is_private("::ffff:127.0.0.1") is True
    assert ssrf._is_private("::ffff:169.254.169.254") is True
    assert ssrf._is_private("::ffff:10.0.0.1") is True
    assert ssrf._is_private("::ffff:8.8.8.8") is False  # 매핑된 공인 IPv4는 계속 허용


def test_ssrf_fetch_pins_idn_host_with_idna_ascii_host_and_sni(monkeypatch):
    """(P3-02) IDN 호스트는 검증·Host·SNI가 모두 IDNA ASCII(punycode) 표기.

    유니코드 호스트를 Host/SNI에 쓰면 요청 생성 단계에서 `UnicodeEncodeError`가 나고,
    DNS 검증도 `xn--` 표기로 해석해야 Host/SNI와 대상이 어긋나지 않는다.
    """
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"xn--bcher-kva.test": "93.184.216.34"})
    )
    state = _install_fake_http(monkeypatch, [{"status": 200, "body": "ok"}])

    response = ssrf.ssrf_safe_fetch("https://bücher.test:444/p")

    stream = state["streams"][0]
    assert str(stream["url"]) == "https://93.184.216.34:444/p"
    assert stream["headers"]["Host"] == "xn--bcher-kva.test:444"
    assert stream["extensions"]["sni_hostname"] == "xn--bcher-kva.test"
    assert response.text == "ok"
    assert str(response.request.url) == str(httpx.URL("https://bücher.test:444/p"))


def test_ssrf_pinned_debug_log_uses_idna_ascii_host_for_idn_url(monkeypatch):
    """(P3-14) 로그 `host`는 검증·Host·SNI와 같은 IDNA ASCII 표기다.

    게이트 판정 2)가 `tls_server_hostname != host`를 sni_mismatch로 보므로 표기가 갈리면
    IDN 호스트에서 정상 동작에도 false red가 난다. 원 유니코드 표기는 별도 키로만 남는다.
    """
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"xn--bcher-kva.test": "93.184.216.34"})
    )
    _install_fake_http(monkeypatch, [{"status": 200, "body": "ok"}])
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        ssrf.logger, "debug", lambda event, **fields: captured.append((event, fields))
    )

    ssrf.ssrf_safe_fetch("https://bücher.test/p")

    assert captured == [
        (
            "ssrf_pinned_request_attempt",
            {
                "host": "xn--bcher-kva.test",
                "unicode_host": "bücher.test",
                "pinned_ip": "93.184.216.34",
                "connect_host": "93.184.216.34",
                "connect_port": 443,
                "tls_server_hostname": "xn--bcher-kva.test",
            },
        )
    ]
    # 게이트 대조가 성립: host == tls_server_hostname
    assert captured[0][1]["host"] == captured[0][1]["tls_server_hostname"]


async def test_ssrf_request_pins_idn_host_with_idna_ascii_host_and_sni(monkeypatch):
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"xn--bcher-kva.test": "93.184.216.34"})
    )
    state = _install_fake_http(monkeypatch, [{"status": 200, "body": "ok"}], is_async=True)

    response = await ssrf.ssrf_safe_request("POST", "https://bücher.test/api", json={"q": 1})

    stream = state["streams"][0]
    assert str(stream["url"]) == "https://93.184.216.34/api"
    assert stream["headers"]["Host"] == "xn--bcher-kva.test"
    assert stream["extensions"]["sni_hostname"] == "xn--bcher-kva.test"
    assert response.text == "ok"


def test_ssrf_fetch_connect_timeout_slice_leaves_budget_for_next_ip(monkeypatch):
    """(P3-03) 첫 IP가 블랙홀(SYN 무응답)이어도 다음 검증 IP를 시도한다.

    가짜 시계는 각 시도가 **자기 connect 상한을 전부 소진**하도록 전진한다. 홉 예산 전체를
    첫 시도의 connect로 주면(수정 전) 두 번째 IP는 예산 소진으로 연결조차 못 한다.
    """
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        _fake_getaddrinfo({"blackhole.test": ["93.184.216.34", "93.184.216.35"]}),
    )
    clock = {"now": 100.0}
    monkeypatch.setattr(ssrf.time, "monotonic", lambda: clock["now"])
    attempts: list[object] = []

    class _Stream:
        def __init__(self, client, url):
            self._client = client
            self._url = url

        def __enter__(self):
            if httpx.URL(self._url).host == "93.184.216.34":
                cfg = self._client.timeout_cfg
                clock["now"] += getattr(cfg, "connect", cfg)  # connect 상한을 전부 소진
                raise httpx.ConnectTimeout(
                    "blackhole", request=httpx.Request("GET", self._url)
                )
            return _make_resp({"status": 200, "body": "ok"}, self._url)

        def __exit__(self, *a):
            return False

    class _Client:
        def __init__(self, *args, **kwargs):
            self.timeout_cfg = kwargs["timeout"]
            attempts.append(kwargs["timeout"])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, url, **kwargs):
            return _Stream(self, url)

    monkeypatch.setattr(ssrf.httpx, "Client", _Client)

    response = ssrf.ssrf_safe_fetch("https://blackhole.test/", timeout=2.0)

    assert response.text == "ok"  # 두 번째 검증 IP로 성공
    assert len(attempts) == 2
    assert [t.connect for t in attempts] == [pytest.approx(1.0), pytest.approx(1.0)]
    assert [t.read for t in attempts] == [pytest.approx(2.0), pytest.approx(1.0)]


def test_ssrf_resolve_deduplicates_ips_preserving_order(monkeypatch):
    """(P3-04) socktype 힌트가 없어 같은 주소가 여러 항목으로 와도 한 번만 시도한다."""
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        _fake_getaddrinfo({"dup.test": ["93.184.216.34", "93.184.216.34", "93.184.216.35"]}),
    )

    assert ssrf._resolve_public_ips("dup.test") == ["93.184.216.34", "93.184.216.35"]

    request = httpx.Request("GET", "https://dup.test/")
    state = _install_fake_http(
        monkeypatch,
        [
            {"status": 599, "exception": httpx.ConnectError("down", request=request)},
            {"status": 200, "body": "ok"},
        ],
    )

    response = ssrf.ssrf_safe_fetch("https://dup.test/")

    assert response.text == "ok"
    assert [httpx.URL(s["url"]).host for s in state["streams"]] == [
        "93.184.216.34",
        "93.184.216.35",
    ]


def test_ssrf_fetch_replaces_caller_supplied_host_header(monkeypatch):
    """(P3-05) 호출자가 준 host 헤더는 대소문자 무관 제거 — Host가 둘 전송되지 않는다."""
    from biznotes_core.core import ssrf

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"a.test": "93.184.216.34"}))
    state = _install_fake_http(monkeypatch, [{"status": 200, "body": "ok"}])

    ssrf.ssrf_safe_fetch(
        "https://a.test/x", headers={"host": "evil.test", "HOST": "evil2.test", "X-Test": "kept"}
    )

    sent = state["streams"][0]["headers"]
    assert sent == {"X-Test": "kept", "Accept-Encoding": "gzip, deflate", "Host": "a.test"}
    assert [k for k in sent if k.lower() == "host"] == ["Host"]


def test_ssrf_fetch_rejects_url_without_host():
    """기존 가드 유지: 호스트 없는 URL은 해석·연결 전에 차단(`_validate_hop`)."""
    from biznotes_core.core import ssrf

    with pytest.raises(ssrf.SSRFBlockedError, match="호스트가 없는 URL"):
        ssrf.ssrf_safe_fetch("http:///no-host")


async def test_ssrf_request_size_cap_streaming_async(monkeypatch):
    """기존 가드 유지: 비동기 핀 경로도 스트리밍 누적 바이트가 상한을 넘으면 차단."""
    from biznotes_core.core import ssrf

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"a.test": "93.184.216.34"}))
    state = _install_fake_http(
        monkeypatch, [{"status": 200, "body": b"x" * 5000}], is_async=True
    )
    with pytest.raises(ssrf.SSRFBlockedError, match="너무 큽"):
        await ssrf.ssrf_safe_request("GET", "https://a.test/big", max_bytes=1000)
    assert state["client_exits"] == 1  # 차단 시에도 Client는 닫힌다


# ── 압축 응답(Content-Encoding) — 관문이 이미 해제한 본문을 재해제하지 않는다 ──────
#
# 네트워크 경계만 가짜: DNS(getaddrinfo) + transport(실제 httpx Client/AsyncClient에
# MockTransport 주입). 응답은 실제 압축 바이트 + 해당 content-encoding 헤더를 스트림으로
# 돌려주므로 관문 해제(`_read_capped*`) → `_capped_response` 경로를 그대로 탄다.
# (라이브 결함: example.com `content-encoding: br` → `httpx.DecodingError: brotli: decoder failed`)
# br은 #413부터 풀지 않고 거부한다(`tests/test_ssrf_bounded_decompression.py`).

_PLAINTEXT = ("<html><body>" + "안녕 pinned example " * 300 + "</body></html>").encode()


def _compress(encoding: str, data: bytes) -> bytes:
    if encoding == "gzip":
        import gzip

        return gzip.compress(data)
    # br은 해제하지 않는다 — 헤더만 붙인 임의 바이트로 충분하다
    return b"\x8b\x02\x80" + data[:64]


def _install_encoded_transport(
    monkeypatch, *, encoding: str, body: bytes, is_async: bool = False
) -> list[httpx.Request]:
    from biznotes_core.core import ssrf

    wire = _compress(encoding, body)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={
                "Content-Encoding": encoding,
                "Content-Type": "text/html; charset=utf-8",
                "X-Kept": "1",
            },
            # 실제 전송처럼 스트림으로 준다 — `content=`는 생성 때 본문을 읽고 해제해 버린다
            stream=httpx.ByteStream(wire),
        )

    base = REAL_HTTPX_ASYNC_CLIENT if is_async else REAL_HTTPX_CLIENT

    class _Boundary(base):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(ssrf.httpx, "AsyncClient" if is_async else "Client", _Boundary)
    return requests


def _assert_decoded_response(response: httpx.Response, original_url: str) -> None:
    assert response.content == _PLAINTEXT
    assert response.text == _PLAINTEXT.decode()
    response.read()  # 다시 읽어도 재해제 시도·예외 없음
    assert "content-encoding" not in response.headers
    assert "transfer-encoding" not in response.headers
    assert int(response.headers["content-length"]) == len(response.content)
    assert response.headers["x-kept"] == "1"  # 그 밖의 헤더는 보존
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert str(response.request.url) == original_url


@pytest.mark.parametrize("encoding", ["gzip", "br"])
def test_ssrf_fetch_compressed_response_is_not_decoded_twice(monkeypatch, encoding):
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"enc.test": "93.184.216.34"})
    )
    requests = _install_encoded_transport(monkeypatch, encoding=encoding, body=_PLAINTEXT)

    if encoding == "br":  # #413: 출력량을 묶어 풀 수 없는 인코딩은 거부한다
        with pytest.raises(httpx.DecodingError):
            ssrf.ssrf_safe_fetch("https://enc.test/page?x=1")
    else:
        response = ssrf.ssrf_safe_fetch("https://enc.test/page?x=1")
        _assert_decoded_response(response, "https://enc.test/page?x=1")
    assert [str(r.url) for r in requests] == ["https://93.184.216.34/page?x=1"]
    assert requests[0].headers["host"] == "enc.test"


@pytest.mark.parametrize("encoding", ["gzip", "br"])
async def test_ssrf_request_compressed_response_is_not_decoded_twice(monkeypatch, encoding):
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"enc.test": "93.184.216.34"})
    )
    requests = _install_encoded_transport(
        monkeypatch, encoding=encoding, body=_PLAINTEXT, is_async=True
    )

    if encoding == "br":  # #413: 출력량을 묶어 풀 수 없는 인코딩은 거부한다
        with pytest.raises(httpx.DecodingError):
            await ssrf.ssrf_safe_request("GET", "https://enc.test/api?y=2")
    else:
        response = await ssrf.ssrf_safe_request("GET", "https://enc.test/api?y=2")
        _assert_decoded_response(response, "https://enc.test/api?y=2")
    assert [str(r.url) for r in requests] == ["https://93.184.216.34/api?y=2"]


async def test_ssrf_size_cap_applies_to_decoded_bytes(monkeypatch):
    """상한은 해제된 본문에도 걸린다 — 압축 전송량이 작아도 해제 크기가 max_bytes를 넘으면
    sync/async 모두 차단하고, 판정 전에 해제 크기만큼 할당하지 않는다(#413)."""
    import tracemalloc

    from biznotes_core.core import ssrf

    body = b"x" * 500_000  # 압축 시 수백 바이트 → 해제 시 500,000 bytes
    assert len(_compress("gzip", body)) < 1000
    monkeypatch.setattr(
        ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"enc.test": "93.184.216.34"})
    )

    # 압축(대역 설치)은 재는 구간 밖에서 한다 — 압축기 상태만 수백 KiB다
    _install_encoded_transport(monkeypatch, encoding="gzip", body=body)
    tracemalloc.start()
    try:
        with pytest.raises(ssrf.SSRFBlockedError, match="너무 큽"):
            ssrf.ssrf_safe_fetch("https://enc.test/bomb", max_bytes=1000)
        sync_peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    _install_encoded_transport(monkeypatch, encoding="gzip", body=body, is_async=True)
    tracemalloc.start()
    try:
        with pytest.raises(ssrf.SSRFBlockedError, match="너무 큽"):
            await ssrf.ssrf_safe_request("GET", "https://enc.test/bomb", max_bytes=1000)
        async_peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert sync_peak < len(body) // 2
    assert async_peak < len(body) // 2


def test_capped_response_drops_wire_encoding_headers_only():
    """`_capped_response` 단위: 전송 인코딩 헤더 3종(대소문자 무관)만 제거하고 나머지·
    상태·원 요청 URL은 보존, Content-Length는 확정 본문 길이로 재설정."""
    from biznotes_core.core import ssrf

    wire = httpx.Response(
        203,
        headers=[
            ("Content-Encoding", "gzip"),
            ("Transfer-Encoding", "chunked"),
            ("Content-Length", "12"),
            ("Set-Cookie", "a=1"),
            ("Set-Cookie", "b=2"),
            ("X-Kept", "1"),
        ],
    )

    out = ssrf._capped_response(wire, b"decoded body", "POST", "https://orig.test/p")

    assert out.status_code == 203
    assert out.content == b"decoded body"
    assert "content-encoding" not in out.headers
    assert "transfer-encoding" not in out.headers
    assert out.headers["content-length"] == str(len(b"decoded body"))
    assert out.headers.get_list("set-cookie") == ["a=1", "b=2"]
    assert out.headers["x-kept"] == "1"
    assert out.request.method == "POST"
    assert str(out.request.url) == "https://orig.test/p"


def test_ssrf_fetch_allow_private_does_not_pin(monkeypatch):
    from biznotes_core.core import ssrf

    state = _install_fake_http(monkeypatch, [{"status": 200, "body": "ok"}])
    response = ssrf.ssrf_safe_fetch("http://127.0.0.1:8080/x", allow_private=True)

    assert response.text == "ok"
    assert str(state["streams"][0]["url"]) == "http://127.0.0.1:8080/x"
    assert "extensions" not in state["streams"][0]
    assert "Host" not in (state["streams"][0].get("headers") or {})
    assert state["clients"][0]["kwargs"]["trust_env"] is False


async def test_ssrf_request_redirect_same_ip_new_clients_downgrades_and_splits_host_sni(
    monkeypatch,
):
    """(e)(k) 비동기: a.test→b.test(같은 IP) — 시도마다 새 AsyncClient를 열고 닫으며
    Host/SNI가 호스트별로 분리되고, 리다이렉트 홉은 GET·무body로 강등된다."""
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        _fake_getaddrinfo({"a.test": "93.184.216.34", "b.test": "93.184.216.34"}),
    )
    state = _install_fake_http(
        monkeypatch,
        [{"status": 307, "location": "https://b.test/final"}, {"status": 200, "body": "ok"}],
        is_async=True,
    )

    response = await ssrf.ssrf_safe_request("POST", "https://a.test/start", json={"q": 1})

    assert response.text == "ok"
    assert len(state["clients"]) == 2
    assert state["client_exits"] == 2
    assert [s["method"] for s in state["streams"]] == ["POST", "GET"]
    assert [s["json"] for s in state["streams"]] == [{"q": 1}, None]
    assert [s["headers"]["Host"] for s in state["streams"]] == ["a.test", "b.test"]
    assert [s["extensions"]["sni_hostname"] for s in state["streams"]] == ["a.test", "b.test"]
    assert response.request.method == "GET"
    assert str(response.request.url) == "https://b.test/final"


async def test_ssrf_request_falls_back_over_verified_ips_async(monkeypatch):
    """(l) 비동기: 첫 검증 IP ConnectError → 다음 검증 IP(IPv6 포함)로 새 Client 재시도.
    같은 홉 안의 재시도는 원 메서드·body를 유지한다(연결 전 실패라 중복 전송 없음)."""
    from biznotes_core.core import ssrf

    request = httpx.Request("POST", "https://fallback.test/")
    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        _fake_getaddrinfo({"fallback.test": ["93.184.216.34", "2001:4860:4860::8888"]}),
    )
    state = _install_fake_http(
        monkeypatch,
        [
            {"status": 599, "exception": httpx.ConnectError("down", request=request)},
            {"status": 200, "body": "ok"},
        ],
        is_async=True,
    )

    response = await ssrf.ssrf_safe_request("POST", "https://fallback.test/", json={"a": 1})

    assert response.text == "ok"
    assert [str(s["url"]) for s in state["streams"]] == [
        "https://93.184.216.34/",
        "https://[2001:4860:4860::8888]/",
    ]
    assert [s["headers"]["Host"] for s in state["streams"]] == ["fallback.test"] * 2
    assert [s["json"] for s in state["streams"]] == [{"a": 1}, {"a": 1}]
    assert len(state["clients"]) == 2
    assert state["client_exits"] == 2


def test_ssrf_gateway_ignores_environment_proxies(monkeypatch):
    from biznotes_core.core import ssrf

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy.invalid:1080")
    monkeypatch.setenv("NO_PROXY", "example.test")
    monkeypatch.setattr(
        ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"example.test": "93.184.216.34"})
    )
    state = _install_fake_http(monkeypatch, [{"status": 200, "body": "ok"}])

    ssrf.ssrf_safe_fetch("https://example.test/")

    assert state["clients"][0]["kwargs"]["trust_env"] is False
    with REAL_HTTPX_CLIENT(trust_env=False) as real_client:
        assert real_client._mounts == {}
    # 대조군: 같은 환경에서 trust_env=True면 프록시 transport가 실제로 선택된다 — 위
    # 단언이 "환경 변수가 애초에 효과 없음"으로 통과하는 false green이 아님을 보인다.
    # (ALL_PROXY=socks5는 선택 의존성 socksio를 요구하므로 대조군에서만 제거)
    monkeypatch.delenv("ALL_PROXY")
    with REAL_HTTPX_CLIENT(trust_env=True) as env_client:
        assert env_client._mounts


async def test_ssrf_trace_callbacks_record_only_connection_target_and_sni():
    from biznotes_core.core import ssrf

    sync_record: dict[str, object] = {}
    sync_trace = ssrf._pin_trace(sync_record)
    sync_trace("connection.connect_tcp.started", {"host": "93.184.216.34", "port": 443})
    sync_trace("connection.start_tls.started", {"server_hostname": "example.com"})
    sync_trace("http11.receive_response_body.started", {"secret": "ignored"})
    assert sync_record == {
        "connect_host": "93.184.216.34",
        "connect_port": 443,
        "tls_server_hostname": "example.com",
    }
    assert not inspect.iscoroutinefunction(sync_trace)

    async_record: dict[str, object] = {}
    async_trace = ssrf._pin_atrace(async_record)
    await async_trace(
        "connection.connect_tcp.started", {"host": "93.184.216.35", "port": 8443}
    )
    await async_trace("connection.start_tls.started", {"server_hostname": "api.example.com"})
    await async_trace("http11.receive_response_body.started", {"secret": "ignored"})
    assert async_record == {
        "connect_host": "93.184.216.35",
        "connect_port": 8443,
        "tls_server_hostname": "api.example.com",
    }
    assert inspect.iscoroutinefunction(async_trace)


def test_ssrf_pinned_debug_log_contains_transport_observation_without_payload(monkeypatch):
    from biznotes_core.core import ssrf

    monkeypatch.setattr(
        ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"example.test": "93.184.216.34"})
    )
    _install_fake_http(monkeypatch, [{"status": 200, "body": "classified response body"}])
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        ssrf.logger, "debug", lambda event, **fields: captured.append((event, fields))
    )

    ssrf.ssrf_safe_fetch(
        "https://example.test/private?token=classified",
        headers={"Authorization": "Bearer classified"},
    )

    assert captured == [
        (
            "ssrf_pinned_request_attempt",
            {
                "host": "example.test",
                "pinned_ip": "93.184.216.34",
                "connect_host": "93.184.216.34",
                "connect_port": 443,
                "tls_server_hostname": "example.test",
            },
        )
    ]
    assert "classified" not in repr(captured)


# ── 교차 출처 리다이렉트의 자격증명 헤더 제거(#176) ────────────────────────────────
#
# 네트워크 경계만 가짜: DNS(getaddrinfo) + 실제 httpx Client/AsyncClient에 MockTransport.
# 핀 경로는 transport가 IP를 보므로 홉은 Host 헤더로 구분한다.

_CREDS = {"Authorization": "Bearer test-token", "Cookie": "sid=test", "proxy-authorization": "x"}
_DNS = {
    "a.test": "93.184.216.34",
    "b.test": "93.184.216.35",
    "twin.test": "93.184.216.34",  # a.test와 같은 IP — 다른 출처
    "xn--bcher-kva.test": "93.184.216.36",
}


def _install_redirect_transport(
    monkeypatch, routes: dict[str, str], *, is_async: bool
) -> list[httpx.Request]:
    """`routes`: "host[:port]/path" → 다음 URL(302). 없는 경로는 200."""
    from biznotes_core.core import ssrf

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = f"{request.headers['host']}{request.url.raw_path.decode()}"
        if key in routes:
            return httpx.Response(302, headers={"Location": routes[key]})
        # 실제 전송처럼 스트림으로 준다 — `content=`는 생성 때 본문을 읽어 버린다
        return httpx.Response(200, stream=httpx.ByteStream(b"ok"))

    base = REAL_HTTPX_ASYNC_CLIENT if is_async else REAL_HTTPX_CLIENT

    class _Boundary(base):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(ssrf.httpx, "AsyncClient" if is_async else "Client", _Boundary)
    monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo(_DNS))
    return seen


async def _follow(
    monkeypatch, url: str, routes: dict[str, str], *, use_async: bool, allow_private=False
) -> list[httpx.Request]:
    from biznotes_core.core import ssrf

    seen = _install_redirect_transport(monkeypatch, routes, is_async=use_async)
    headers = {**_CREDS, "X-Keep": "1"}
    if use_async:
        res = await ssrf.ssrf_safe_request(
            "POST", url, headers=headers, json={"a": 1}, allow_private=allow_private
        )
    else:
        res = ssrf.ssrf_safe_fetch(url, headers=headers, allow_private=allow_private)
    assert res.status_code == 200
    assert headers == {**_CREDS, "X-Keep": "1"}  # 호출자 dict는 그대로
    return seen


def _creds_of(request: httpx.Request) -> set[str]:
    return {k for k in ("authorization", "cookie", "proxy-authorization") if k in request.headers}


_ALL = {"authorization", "cookie", "proxy-authorization"}


@pytest.mark.parametrize("use_async", [False, True], ids=["fetch", "request"])
@pytest.mark.parametrize("allow_private", [False, True], ids=["pinned", "allow_private"])
async def test_cross_origin_redirect_strips_credentials(monkeypatch, use_async, allow_private):
    seen = await _follow(
        monkeypatch,
        "https://a.test/start",
        {"a.test/start": "https://b.test/landing"},
        use_async=use_async,
        allow_private=allow_private,
    )
    assert [r.headers["host"] for r in seen] == ["a.test", "b.test"]
    assert _creds_of(seen[0]) == _ALL
    assert _creds_of(seen[1]) == set()
    assert seen[1].headers["x-keep"] == "1"  # 다른 헤더는 남는다


@pytest.mark.parametrize("use_async", [False, True], ids=["fetch", "request"])
@pytest.mark.parametrize(
    "location",
    ["/next", "https://A.TEST:443/next", "//a.test/next"],
    ids=["relative", "explicit-default-port", "scheme-relative"],
)
async def test_same_origin_redirect_keeps_credentials(monkeypatch, use_async, location):
    seen = await _follow(
        monkeypatch, "https://a.test/start", {"a.test/start": location}, use_async=use_async
    )
    assert len(seen) == 2
    assert all(_creds_of(r) == _ALL for r in seen)


@pytest.mark.parametrize("use_async", [False, True], ids=["fetch", "request"])
@pytest.mark.parametrize(
    ("start", "target"),
    [
        ("https://a.test/start", "https://twin.test/x"),  # 같은 IP, 다른 호스트
        ("https://a.test/start", "https://a.test:8443/x"),  # 포트만 다름
        ("https://a.test/start", "http://a.test/x"),  # https → http 다운그레이드
        ("http://a.test/start", "https://a.test/x"),  # http → https 업그레이드도 교차 출처
    ],
    ids=["same-ip", "port", "downgrade", "upgrade"],
)
async def test_origin_change_variants_strip(monkeypatch, use_async, start, target):
    key = start.split("://", 1)[1]
    seen = await _follow(monkeypatch, start, {key: target}, use_async=use_async)
    assert len(seen) == 2
    assert _creds_of(seen[0]) == _ALL
    assert _creds_of(seen[1]) == set()


@pytest.mark.parametrize("use_async", [False, True], ids=["fetch", "request"])
async def test_stripping_is_sticky_when_redirected_back(monkeypatch, use_async):
    seen = await _follow(
        monkeypatch,
        "https://a.test/1",
        {"a.test/1": "https://b.test/2", "b.test/2": "https://a.test/3"},
        use_async=use_async,
    )
    assert [r.headers["host"] for r in seen] == ["a.test", "b.test", "a.test"]
    assert [_creds_of(r) for r in seen] == [_ALL, set(), set()]


@pytest.mark.parametrize("use_async", [False, True], ids=["fetch", "request"])
async def test_mixed_case_credential_keys_are_stripped(monkeypatch, use_async):
    from biznotes_core.core import ssrf

    seen = _install_redirect_transport(
        monkeypatch, {"a.test/start": "https://b.test/x"}, is_async=use_async
    )
    headers = {"AUTHORIZATION": "Bearer t", "cOOkie": "s=1", "Proxy-Authorization": "p"}
    if use_async:
        await ssrf.ssrf_safe_request("GET", "https://a.test/start", headers=headers)
    else:
        ssrf.ssrf_safe_fetch("https://a.test/start", headers=headers)
    assert _creds_of(seen[0]) == _ALL
    assert _creds_of(seen[1]) == set()


@pytest.mark.parametrize("use_async", [False, True], ids=["fetch", "request"])
async def test_idn_host_spellings_are_same_origin(monkeypatch, use_async):
    seen = await _follow(
        monkeypatch,
        "https://bücher.test/start",
        {"xn--bcher-kva.test/start": "https://xn--bcher-kva.test/next"},
        use_async=use_async,
    )
    assert len(seen) == 2
    assert all(_creds_of(r) == _ALL for r in seen)


# 리뷰(docs/plans/ssrf-redirect-credentials/reviews/plan-review.md) 보강


@pytest.mark.parametrize("use_async", [False, True], ids=["fetch", "request"])
@pytest.mark.parametrize("url", ["ftp://x.test/", "gopher://a.test/", "file:///etc/passwd"])
async def test_bad_scheme_error_contract_is_unchanged(monkeypatch, use_async, url):
    """출처 계산은 홉 검증 뒤다 — 비 http(s) 스킴은 종전처럼 SSRFBlockedError(M-1)."""
    from biznotes_core.core import ssrf

    _install_redirect_transport(monkeypatch, {}, is_async=use_async)
    with pytest.raises(ssrf.SSRFBlockedError, match="허용되지 않는 스킴"):
        if use_async:
            await ssrf.ssrf_safe_request("GET", url, headers=_CREDS)
        else:
            ssrf.ssrf_safe_fetch(url, headers=_CREDS)


@pytest.mark.parametrize("use_async", [False, True], ids=["fetch", "request"])
async def test_redirect_to_bad_scheme_keeps_error_contract(monkeypatch, use_async):
    from biznotes_core.core import ssrf

    _install_redirect_transport(monkeypatch, {"a.test/x": "ftp://b.test/y"}, is_async=use_async)
    with pytest.raises(ssrf.SSRFBlockedError, match="허용되지 않는 스킴"):
        if use_async:
            await ssrf.ssrf_safe_request("GET", "https://a.test/x", headers=_CREDS)
        else:
            ssrf.ssrf_safe_fetch("https://a.test/x", headers=_CREDS)


@pytest.mark.parametrize("use_async", [False, True], ids=["fetch", "request"])
@pytest.mark.parametrize(
    ("location", "expect_creds"), [("https://b.test/y", False), ("/y", True)]
)
async def test_ip_retry_within_hop_uses_same_headers(
    monkeypatch, use_async, location, expect_creds
):
    """한 홉의 IP 재시도(D-14)는 같은 헤더를 쓴다 — 교차 출처면 두 시도 모두 없다."""
    from biznotes_core.core import ssrf

    seen: list[httpx.Request] = []
    dead = {"93.184.216.40"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host in dead:
            raise httpx.ConnectError("refused", request=request)
        if request.url.raw_path == b"/x":
            return httpx.Response(302, headers={"Location": location})
        return httpx.Response(200, stream=httpx.ByteStream(b"ok"))  # 실제 전송처럼 스트림

    base = REAL_HTTPX_ASYNC_CLIENT if use_async else REAL_HTTPX_CLIENT

    class _Boundary(base):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(ssrf.httpx, "AsyncClient" if use_async else "Client", _Boundary)
    target = ["93.184.216.40", "93.184.216.41"]
    dns = {"a.test": target if expect_creds else "93.184.216.34", "b.test": target}
    monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo(dns))
    if use_async:
        await ssrf.ssrf_safe_request("GET", "https://a.test/x", headers=_CREDS)
    else:
        ssrf.ssrf_safe_fetch("https://a.test/x", headers=_CREDS)

    second_hop = [r for r in seen if r.url.raw_path == b"/y"]
    assert [r.url.host for r in second_hop] == target  # 첫 IP 실패 뒤 둘째 IP로 재시도
    assert all(_creds_of(r) == (_ALL if expect_creds else set()) for r in second_hop)


@pytest.mark.parametrize("use_async", [False, True], ids=["fetch", "request"])
async def test_uppercase_scheme_with_explicit_default_port_is_same_origin(monkeypatch, use_async):
    """호출자가 준 초기 URL은 정규화되지 않을 수 있다 — 기본 포트를 채워 비교한다(m-1)."""
    seen = await _follow(
        monkeypatch, "HTTPS://A.TEST:443/x", {"a.test:443/x": "/y"}, use_async=use_async
    )
    assert len(seen) == 2
    assert all(_creds_of(r) == _ALL for r in seen)


@pytest.mark.parametrize("use_async", [False, True], ids=["fetch", "request"])
async def test_userinfo_basic_auth_does_not_cross_origin(monkeypatch, use_async):
    """URL userinfo가 만드는 Basic 인증은 교차 출처 홉으로 넘어가지 않는다(m-2, httpx join)."""
    from biznotes_core.core import ssrf

    seen = _install_redirect_transport(
        monkeypatch, {"a.test/x": "https://b.test/y"}, is_async=use_async
    )
    if use_async:
        await ssrf.ssrf_safe_request("GET", "https://u:p@a.test/x")
    else:
        ssrf.ssrf_safe_fetch("https://u:p@a.test/x")
    assert seen[0].headers["authorization"].startswith("Basic ")
    assert "authorization" not in seen[1].headers


async def test_strip_log_carries_host_only(monkeypatch):
    seen_logs = []
    with structlog.testing.capture_logs() as logs:
        await _follow(
            monkeypatch, "https://a.test/s", {"a.test/s": "https://b.test/l"}, use_async=False
        )
        seen_logs = [e for e in logs if e["event"] == "ssrf_redirect_credentials_stripped"]
    assert seen_logs == [{"event": "ssrf_redirect_credentials_stripped", "host": "b.test",
                          "log_level": "debug"}]
    assert not any(v in str(logs) for v in _CREDS.values())
