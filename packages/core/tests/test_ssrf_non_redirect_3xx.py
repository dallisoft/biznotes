"""SSRF 관문은 Location 있는 리다이렉트만 따라가고 그 밖의 3xx는 최종 응답이다 (#424).

네트워크 경계만 바꾼다: 가짜 `getaddrinfo` + 실제 httpx Client에 꽂은 MockTransport(핀 연결·
`allow_private` 분기 모두). 응답은 실제 전송처럼 스트림으로 준다 — `content=`는 생성 때 본문을
읽어 버린다. 조건부 GET 하나는 로컬 HTTP 서버가 실제 304를 보낸다. 관문(`ssrf_safe_fetch`·
`ssrf_safe_request`)과 본문 읽기(상한·해제)는 실제 코드다.
"""

import asyncio
import gzip
import socket
import sys
import types
from typing import Any

import anyio
import httpx
import pytest

from biznotes_core.core import ssrf
from biznotes_core.core.ssrf import EgressBlockedError, EgressPolicy, SSRFBlockedError

REAL_CLIENT = httpx.Client
REAL_ASYNC_CLIENT = httpx.AsyncClient

_DNS = {"a.test": "93.184.216.34", "b.test": "93.184.216.35"}
FETCH_ONLY = EgressPolicy(
    fetch_only=True, allowed_hosts=(), max_url_chars=2048, max_query_chars=512
)
_REDIRECTS = [301, 302, 303, 307, 308]

use_async = pytest.mark.parametrize("use_async", [False, True], ids=["fetch", "request"])
branch = pytest.mark.parametrize("allow_private", [False, True], ids=["pinned", "allow_private"])
mode = pytest.mark.parametrize("egress", [None, FETCH_ONLY], ids=["open", "fetch_only"])


class _Net:
    """받은 요청을 센다. `reply`: "host/path" → (상태, 헤더, 본문). 없는 경로는 200 `final`."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}

    def reply(
        self, key: str, status: int, headers: dict[str, str] | None = None, body: bytes = b""
    ) -> None:
        self.routes[key] = (status, headers or {}, body)

    def paths(self) -> list[str]:
        return [r.url.raw_path.decode() for r in self.seen]


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> _Net:
    state = _Net()

    def handler(request: httpx.Request) -> httpx.Response:
        state.seen.append(request)
        key = f"{request.headers['host']}{request.url.raw_path.decode()}"
        status, headers, body = state.routes.get(key, (200, {}, b"final"))
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
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


async def _call(
    url: str, *, use_async: bool, allow_private: bool, egress: EgressPolicy | None, **kw: Any
) -> httpx.Response:
    method = kw.pop("method", "GET")
    if use_async:
        return await ssrf.ssrf_safe_request(
            method, url, allow_private=allow_private, egress=egress, **kw
        )
    assert method == "GET"  # 동기 관문은 GET만 보낸다
    return ssrf.ssrf_safe_fetch(url, allow_private=allow_private, egress=egress, **kw)


# ---------- 304 Not Modified ----------


@use_async
@branch
@mode
async def test_304_is_the_final_response(net, use_async, allow_private, egress):
    """304는 따라가지 않고 그대로 돌려준다 — 본문 없음, 상태·헤더 보존."""
    net.reply("a.test/page", 304, {"ETag": '"v1"', "Cache-Control": "max-age=60"})
    res = await _call(
        "http://a.test/page",
        use_async=use_async,
        allow_private=allow_private,
        egress=egress,
        headers={"If-None-Match": '"v1"'},
    )
    assert res.status_code == 304
    assert res.content == b""
    assert res.headers["etag"] == '"v1"'
    assert res.headers["cache-control"] == "max-age=60"
    assert str(res.request.url) == "http://a.test/page"  # 핀 주소가 아니라 원 URL
    assert net.paths() == ["/page"]  # 다음 홉이 없다
    assert net.seen[0].headers["if-none-match"] == '"v1"'


@use_async
@branch
@pytest.mark.parametrize(
    ("name", "value"),
    [("If-None-Match", '"v1"'), ("If-Modified-Since", "Wed, 21 Oct 2026 07:28:00 GMT")],
)
async def test_fetch_only_conditional_get_returns_304(net, use_async, allow_private, name, value):
    """받아오기 전용이 허용하는 조건부 헤더로 304를 받으면 거부 없이 돌려준다."""
    net.reply("a.test/page", 304, {"ETag": '"v1"'})
    res = await _call(
        "http://a.test/page",
        use_async=use_async,
        allow_private=allow_private,
        egress=FETCH_ONLY,
        headers={name: value},
    )
    assert res.status_code == 304
    assert res.content == b""
    assert net.seen[0].headers[name] == value
    assert net.seen[0].headers["user-agent"] == ssrf.FETCH_ONLY_USER_AGENT


class _Origin:
    """로컬 HTTP 서버 — `If-None-Match`가 맞으면 Content-Length 없는 실제 304를 보낸다."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            head = await reader.readuntil(b"\r\n\r\n")
            line, *lines = head.decode("latin-1").split("\r\n")
            method = line.split(" ", 1)[0]
            headers = {
                k.strip().lower(): v.strip()
                for k, v in (h.split(":", 1) for h in lines if ":" in h)
            }
            self.requests.append((method, headers))
            if headers.get("if-none-match") == '"v1"':
                writer.write(
                    b'HTTP/1.1 304 Not Modified\r\nETag: "v1"\r\nConnection: close\r\n\r\n'
                )
            else:
                writer.write(
                    b'HTTP/1.1 200 OK\r\nETag: "v1"\r\nContent-Length: 7\r\n'
                    b"Connection: close\r\n\r\nfetched"
                )
            await writer.drain()
            writer.close()

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]


@pytest.fixture
async def origin() -> Any:
    server = _Origin()
    await server.start()
    yield server
    assert server.server is not None
    server.server.close()
    await server.server.wait_closed()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_fetch_only_conditional_request_against_a_real_server(origin, method):
    """실제 서버의 304(본문·Content-Length 없음)도 최종 응답이다 — 비동기 GET·HEAD, 동기 GET."""
    url = f"http://127.0.0.1:{origin.port}/page"
    cond = {"If-None-Match": '"v1"'}
    res = await ssrf.ssrf_safe_request(
        method, url, allow_private=True, egress=FETCH_ONLY, headers=cond
    )
    assert (res.status_code, res.content, res.headers["etag"]) == (304, b"", '"v1"')
    sync_res = await anyio.to_thread.run_sync(
        lambda: ssrf.ssrf_safe_fetch(url, allow_private=True, egress=FETCH_ONLY, headers=cond)
    )
    assert (sync_res.status_code, sync_res.content) == (304, b"")
    assert [m for m, _ in origin.requests] == [method, "GET"]
    assert all(h["if-none-match"] == '"v1"' for _, h in origin.requests)


# ---------- Location 없는 3xx·300 ----------


@use_async
@branch
@mode
@pytest.mark.parametrize("status", [300, *_REDIRECTS])
async def test_3xx_without_location_is_the_final_response(
    net, use_async, allow_private, egress, status
):
    """Location 없는 3xx는 「Location 헤더가 없습니다」로 실패하지 않고 본문까지 읽은 응답이다."""
    net.reply("a.test/page", status, {"Content-Type": "text/html"}, b"<p>choices</p>")
    res = await _call(
        "http://a.test/page", use_async=use_async, allow_private=allow_private, egress=egress
    )
    assert res.status_code == status
    assert res.text == "<p>choices</p>"
    assert res.headers["content-type"] == "text/html"
    assert net.paths() == ["/page"]


@use_async
@branch
@mode
async def test_300_with_location_is_not_followed(net, use_async, allow_private, egress):
    """300 Multiple Choices는 Location이 있어도 리다이렉트가 아니다(`has_redirect_location`)."""
    net.reply("a.test/page", 300, {"Location": "http://b.test/choice"}, b"choices")
    res = await _call(
        "http://a.test/page", use_async=use_async, allow_private=allow_private, egress=egress
    )
    assert (res.status_code, res.text) == (300, "choices")
    assert res.headers["location"] == "http://b.test/choice"
    assert net.paths() == ["/page"]


@use_async
@branch
@mode
@pytest.mark.parametrize("status", [300, 301])
async def test_final_3xx_body_keeps_the_size_cap(net, use_async, allow_private, egress, status):
    """최종 3xx 본문도 다른 최종 응답과 같은 상한(`max_bytes`)으로 읽는다."""
    net.reply("a.test/fit", status, body=b"x" * 100)
    net.reply("a.test/over", status, body=b"x" * 101)
    kw = {"use_async": use_async, "allow_private": allow_private, "egress": egress}
    res = await _call("http://a.test/fit", max_bytes=100, **kw)
    assert (res.status_code, len(res.content)) == (status, 100)
    with pytest.raises(SSRFBlockedError, match=r"^응답이 너무 큽니다\(>100 bytes\)$"):
        await _call("http://a.test/over", max_bytes=100, **kw)


_PLAIN = b"<p>" + b"moved " * 200 + b"</p>"


@use_async
@branch
async def test_final_3xx_body_follows_open_decoding_rules(net, use_async, allow_private):
    """open: gzip 3xx 본문은 관문이 풀고 와이어 인코딩 헤더를 뺀다. br은 거부한다(#413)."""
    net.reply("a.test/gz", 300, {"Content-Encoding": "gzip"}, gzip.compress(_PLAIN))
    net.reply("a.test/br", 300, {"Content-Encoding": "br"}, b"\x0b\x02\x80choices\x03")
    kw = {"use_async": use_async, "allow_private": allow_private, "egress": None}
    res = await _call("http://a.test/gz", **kw)
    assert (res.status_code, res.content) == (300, _PLAIN)
    assert "content-encoding" not in res.headers
    with pytest.raises(httpx.DecodingError):
        await _call("http://a.test/br", **kw)


@use_async
@branch
async def test_final_3xx_compressed_body_is_rejected_in_fetch_only(net, use_async, allow_private):
    """받아오기 전용: 압축된 3xx 응답은 본문을 읽기 전에 `encoded_response`로 거부한다."""
    net.reply("a.test/gz", 301, {"Content-Encoding": "gzip"}, gzip.compress(_PLAIN))
    with pytest.raises(EgressBlockedError) as ei:
        await _call(
            "http://a.test/gz", use_async=use_async, allow_private=allow_private, egress=FETCH_ONLY
        )
    assert ei.value.reason == "encoded_response"


# ---------- Location 있는 리다이렉트는 그대로 따라간다 ----------


@use_async
@branch
@mode
@pytest.mark.parametrize("status", _REDIRECTS)
async def test_redirect_with_location_is_still_followed(
    net, use_async, allow_private, egress, status
):
    """Location 있는 301·302·303·307·308은 따라간다 — open 비동기는 GET 강등, 받아오기 전용 유지."""
    net.reply("a.test/start", status, {"Location": "http://b.test/final"})
    fetch_only = egress is not None
    method = "GET" if not use_async else ("HEAD" if fetch_only else "POST")
    kw: dict[str, Any] = {"json": {"a": 1}} if method == "POST" else {}
    res = await _call(
        "http://a.test/start",
        use_async=use_async,
        allow_private=allow_private,
        egress=egress,
        method=method,
        **kw,
    )
    assert res.status_code == 200
    hops = [(r.method, r.headers["host"], r.url.raw_path.decode()) for r in net.seen]
    followed = method if fetch_only or method == "GET" else "GET"
    assert hops == [(method, "a.test", "/start"), (followed, "b.test", "/final")]
    assert net.seen[1].content == b""  # 강등·유지 모두 다음 홉에 본문이 없다


@use_async
@branch
async def test_redirect_with_empty_location_is_still_rejected(net, use_async, allow_private):
    """Location 헤더가 비어 있으면 리다이렉트 판정은 참이고 `_next_redirect`가 그대로 거부한다."""
    net.reply("a.test/start", 302, {"Location": ""})
    with pytest.raises(SSRFBlockedError, match="Location 헤더가 없습니다"):
        await _call(
            "http://a.test/start", use_async=use_async, allow_private=allow_private, egress=None
        )
    assert net.paths() == ["/start"]


# ---------- 호출처: parse_web ----------


_ARTICLE = b"<html><body><article><p>moved page</p></article></body></html>"


def _stub_trafilatura(monkeypatch) -> list[str]:
    """trafilatura는 코어 의존성이 아니다(지연 import) — 추출만 대역으로 두고 받은 HTML을 센다."""
    extracted: list[str] = []
    mod = types.ModuleType("trafilatura")

    def extract(html: str, **_kw: Any) -> str:
        extracted.append(html)
        return "# moved page"

    mod.extract = extract  # type: ignore[attr-defined]
    mod.extract_metadata = lambda html: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "trafilatura", mod)
    return extracted


@pytest.mark.parametrize(
    ("status", "headers", "body"),
    [
        (304, {"ETag": '"v1"'}, b""),
        (300, {"Content-Type": "text/html"}, _ARTICLE),
        (301, {"Content-Type": "text/html"}, _ARTICLE),
        (302, {"Content-Type": "text/html"}, _ARTICLE),
        (300, {"Location": "http://b.test/choice", "Content-Type": "text/html"}, _ARTICLE),
    ],
    ids=["304", "300", "301-no-location", "302-no-location", "300-with-location"],
)
def test_parse_web_rejects_a_final_3xx_as_an_http_status(net, monkeypatch, status, headers, body):
    """parse_web은 2xx만 소스로 받는다 — 관문을 통과한 3xx는 SSRF 거부가 아니라 HTTP 상태 오류다.

    #424 전에는 관문이 이 응답들을 거부해 실패했다. 3xx 본문(HTML)을 소스로 적재하지 않는다.
    """
    from biznotes_core.services.ingestion.parsers import ParseError, parse_web

    extracted = _stub_trafilatura(monkeypatch)
    net.reply("a.test/article", status, headers, body)
    pattern = rf"^URL을 가져오지 못했습니다: http://a\.test/article \(HTTP {status}\)$"
    with pytest.raises(ParseError, match=pattern):
        parse_web("http://a.test/article")
    assert extracted == []  # 본문이 추출에 닿지 않는다
    assert net.paths() == ["/article"]


@pytest.mark.parametrize("status", [200, 203])
def test_parse_web_still_extracts_a_2xx(net, monkeypatch, status):
    from biznotes_core.services.ingestion.parsers import parse_web

    extracted = _stub_trafilatura(monkeypatch)
    net.reply("a.test/article", status, {"Content-Type": "text/html"}, _ARTICLE)
    result = parse_web("http://a.test/article")
    assert result.markdown == "# moved page"
    assert extracted == [_ARTICLE.decode()]
