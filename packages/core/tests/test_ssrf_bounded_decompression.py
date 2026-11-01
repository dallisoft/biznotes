"""SSRF 관문 open 모드의 압축 해제 상한(#413) — 원본·해제 뒤 바이트를 모두 `max_bytes`로 묶는다.

네트워크 경계만 바꾼다: 가짜 `getaddrinfo` + 실제 httpx Client에 꽂은 MockTransport(핀 연결 분기).
응답은 실제 전송처럼 스트림으로 준다 — `content=`는 생성 때 본문을 읽고 해제해 버린다. 관문
(`ssrf_safe_fetch`·`ssrf_safe_request`)과 해제는 실제 코드다. 메모리는 tracemalloc 최고치로 잰다
(zlib 출력 버퍼도 잡힌다).
"""

import gzip
import importlib.util
import random
import socket
import sys
import tracemalloc
import types
import zlib
from collections.abc import AsyncIterator, Iterator
from typing import Any

import anyio
import httpx
import pytest
import structlog.testing

from biznotes_core.core import ssrf
from biznotes_core.core.ssrf import EgressBlockedError, EgressPolicy, SSRFBlockedError

REAL_CLIENT = httpx.Client
REAL_ASYNC_CLIENT = httpx.AsyncClient

_DNS = {"a.test": "93.184.216.34", "b.test": "93.184.216.35"}
OPEN = EgressPolicy(fetch_only=False, allowed_hosts=(), max_url_chars=2048, max_query_chars=512)
FETCH_ONLY = EgressPolicy(
    fetch_only=True, allowed_hosts=(), max_url_chars=2048, max_query_chars=512
)

# 할당 최고치의 고정분(Client·zlib 창·로그) — 실측 20~110 KiB. 폭탄을 풀면 수~수십 MiB다
_BASE = 256 << 10
_BLOCK = 1 << 16


def _bound(max_bytes: int) -> int:
    """할당 최고치 상한 — `max_bytes`의 작은 배수 + 고정분(실측 약 2배 + 고정분)."""
    return 4 * max_bytes + _BASE


def _zeros_gzip(size: int) -> bytes:
    """0 `size`바이트의 gzip — 원문을 한 번에 만들지 않는다."""
    c = zlib.compressobj(wbits=16 + zlib.MAX_WBITS)
    block = bytes(1 << 20)
    parts = [c.compress(block) for _ in range(size >> 20)]
    return b"".join(parts) + c.flush()


# 8 MiB 0의 gzip(전송 약 8 KiB) — 한 겹 폭탄
_SINGLE = _zeros_gzip(8 << 20)
# 그것을 다시 gzip(전송 수백 바이트) — 한 겹 풀면 약 8 KiB, 두 겹 풀면 8 MiB
_NESTED = gzip.compress(_SINGLE, mtime=0)
# 8 MiB 0의 zlib 헤더 deflate
_DEFLATE_BOMB = zlib.compress(bytes(8 << 20))
# 정상 본문 — 압축이 잘 되는 글과 잘 안 되는 바이트를 섞는다
_PLAIN = ("<html>" + "안녕 bounded 해제 본문 " * 400 + "</html>").encode() + random.Random(
    413
).randbytes(6000)


def _deflate_raw(data: bytes) -> bytes:
    c = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return c.compress(data) + c.flush()


_ENCODE = {
    "gzip": ("gzip", lambda data: gzip.compress(data, mtime=0)),
    "deflate-zlib": ("deflate", zlib.compress),
    "deflate-raw": ("deflate", _deflate_raw),
}


class _Chunks(httpx.SyncByteStream, httpx.AsyncByteStream):
    """전송 본문을 `size`바이트씩 낸다. `tail`(블록, 반복 수)은 같은 블록을 거듭 낸다.

    꼬리를 미리 만들지 않으니 테스트 쪽 할당이 최고치에 들지 않는다. `served`는 관문이 실제로
    읽어 간 청크 수다.
    """

    def __init__(self, body: bytes, size: int, tail: tuple[bytes, int] | None) -> None:
        self.body = body
        self.size = size
        self.tail = tail
        self.served = 0

    def _chunks(self) -> Iterator[bytes]:
        for i in range(0, len(self.body), self.size):
            self.served += 1
            yield self.body[i : i + self.size]
        if self.tail is not None:
            block, count = self.tail
            for _ in range(count):
                self.served += 1
                yield block

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks():
            yield chunk


class _Net:
    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.routes: dict[str, tuple[str, str | None]] = {}  # "host/path" → (Location, 인코딩)
        self.bodies: dict[str, dict[str, Any]] = {}  # "host/path" → 응답 스펙
        self.streams: list[_Chunks] = []

    def serve(
        self,
        key: str,
        body: bytes = b"",
        *,
        encoding: str | None = None,
        size: int = _BLOCK,
        tail: tuple[bytes, int] | None = None,
        status: int = 200,
    ) -> None:
        self.bodies[key] = {
            "body": body, "encoding": encoding, "size": size, "tail": tail, "status": status
        }


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> _Net:
    state = _Net()

    def handler(request: httpx.Request) -> httpx.Response:
        state.seen.append(request)
        key = f"{request.headers['host']}{request.url.raw_path.decode().split('?')[0]}"
        if key in state.routes:
            location, encoding = state.routes[key]
            headers = {"Location": location}
            if encoding:
                headers["Content-Encoding"] = encoding
            # 리다이렉트 응답의 본문은 관문이 읽지 않는다 — 읽으면 거부·상한에 걸린다
            return httpx.Response(302, headers=headers, stream=_Chunks(_NESTED, 8, None))
        spec = state.bodies.get(key) or {
            "body": b"ok", "encoding": None, "size": _BLOCK, "tail": None, "status": 200
        }
        stream = _Chunks(spec["body"], spec["size"], spec["tail"])
        state.streams.append(stream)
        headers = {"Content-Encoding": spec["encoding"]} if spec["encoding"] else {}
        return httpx.Response(spec["status"], headers=headers, stream=stream)

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


def _get(url: str, *, use_async: bool, method: str = "GET", **kw: Any) -> httpx.Response:
    if use_async:
        return anyio.run(lambda: ssrf.ssrf_safe_request(method, url, **kw))
    assert method == "GET"
    return ssrf.ssrf_safe_fetch(url, **kw)


def _traced_peak(run: Any) -> tuple[Any, int]:
    """`run()`을 돌리는 동안의 파이썬 할당 최고치(바이트)."""
    tracemalloc.start()
    try:
        result = run()
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def _oversize(max_bytes: int) -> str:
    return f"응답이 너무 큽니다(>{max_bytes} bytes)"


ASYNC = pytest.mark.parametrize("use_async", [True, False], ids=["request", "fetch"])


# ---------- 폭탄: 판정 전에 풀리지 않는다 ----------


@ASYNC
@pytest.mark.parametrize("policy", [None, OPEN], ids=["none", "open"])
@pytest.mark.parametrize(
    ("encoding", "body", "max_bytes", "tail"),
    [
        # 두 번 gzip한 본문을 gzip 한 겹으로 — 한 겹만 풀어도 상한(1 KiB)을 넘는다
        ("gzip", _NESTED, 1024, None),
        # 전송 약 8 KiB < 상한 64 KiB < 해제 8 MiB — 해제 상한이 막는다
        ("gzip", _SINGLE, 64 << 10, None),
        ("deflate", _DEFLATE_BOMB, 64 << 10, None),
        # gzip 끝 뒤에 32 MiB — 첫 청크부터 끝 뒤 바이트가 원본 상한에 든다
        ("gzip", gzip.compress(b"hello", mtime=0) + bytes(_BLOCK), 1024, (bytes(_BLOCK), 512)),
    ],
    ids=["nested", "single", "deflate", "trailing"],
)
def test_open_bombs_are_cut_at_the_cap_with_bounded_memory(
    net: _Net,
    use_async: bool,
    policy: EgressPolicy | None,
    encoding: str,
    body: bytes,
    max_bytes: int,
    tail: tuple[bytes, int] | None,
) -> None:
    net.serve("a.test/bomb", body, encoding=encoding, tail=tail)

    def run() -> str:
        with pytest.raises(SSRFBlockedError) as info:
            _get("https://a.test/bomb", use_async=use_async, max_bytes=max_bytes, egress=policy)
        return str(info.value)

    message, peak = _traced_peak(run)
    assert message == _oversize(max_bytes)
    assert peak < _bound(max_bytes)
    assert net.streams[0].served <= 2


@ASYNC
def test_data_after_the_gzip_end_is_not_read(net: _Net, use_async: bool) -> None:
    """상한이 넉넉해도 gzip 끝 뒤 원본은 풀지도 읽지도 않는다 — 첫 멤버 본문만 남는다."""
    net.serve(
        "a.test/tail",
        gzip.compress(b"hello", mtime=0) + bytes(100),
        encoding="gzip",
        tail=(bytes(_BLOCK), 512),
    )

    res, peak = _traced_peak(lambda: _get("https://a.test/tail", use_async=use_async))
    assert res.content == b"hello"
    assert peak < _BASE
    assert net.streams[0].served == 1


# ---------- 1바이트 청크: 메모리는 청크 수가 아니라 바이트 수에 비례 (#413 리뷰) ----------

# 비최종 빈 stored 블록(5바이트) n개 + 최종 빈 블록 — 해제 출력이 0인 정상 raw deflate
_EMPTY_BLOCKS = 13_000
_EMPTY_DEFLATE = b"\x00\x00\x00\xff\xff" * _EMPTY_BLOCKS + b"\x01\x00\x00\xff\xff"
_ZERO_OUTPUT = {
    # gzip 헤더 + 블록 + CRC32(빈 값)=0·ISIZE=0 → 65,023바이트
    "gzip": b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff" + _EMPTY_DEFLATE + bytes(8),
    # zlib 헤더 + 블록 + Adler-32(빈 값)=1
    "deflate": b"\x78\x9c" + _EMPTY_DEFLATE + (1).to_bytes(4, "big"),
}


@ASYNC
@pytest.mark.parametrize("encoding", ["gzip", "deflate", None], ids=["gzip", "deflate", "identity"])
def test_one_byte_chunks_keep_memory_proportional_to_bytes(
    net: _Net, use_async: bool, encoding: str | None
) -> None:
    """원본이 1바이트씩 와도(HTTP/1.1 chunked) 할당 최고치는 `max_bytes`의 작은 배수다.

    청크마다 리스트 항목을 쌓고 `b"".join`하면 항목 수만큼(청크당 수십 바이트) 커진다.
    """
    max_bytes = 1 << 16
    body = _ZERO_OUTPUT[encoding] if encoding else b"x" * max_bytes
    assert len(body) <= max_bytes
    net.serve("a.test/slow", body, encoding=encoding, size=1)

    res, peak = _traced_peak(
        lambda: _get("https://a.test/slow", use_async=use_async, max_bytes=max_bytes)
    )
    assert res.content == (b"" if encoding else body)
    assert peak < _bound(max_bytes)
    assert net.streams[0].served == len(body)


# ---------- 정상 압축 응답: 지금과 같은 본문 ----------


@ASYNC
@pytest.mark.parametrize("size", [1, 7, 1024, 65536])
@pytest.mark.parametrize("kind", ["gzip", "deflate-zlib", "deflate-raw"])
def test_open_decodes_gzip_and_deflate_at_any_chunk_boundary(
    net: _Net, use_async: bool, size: int, kind: str
) -> None:
    encoding, encode = _ENCODE[kind]
    net.serve("a.test/page", encode(_PLAIN), encoding=encoding, size=size)

    res = _get("https://a.test/page?x=1", use_async=use_async, max_bytes=len(_PLAIN))
    assert res.content == _PLAIN
    assert res.status_code == 200
    assert "content-encoding" not in res.headers
    assert int(res.headers["content-length"]) == len(_PLAIN)
    assert str(res.request.url) == "https://a.test/page?x=1"


@ASYNC
@pytest.mark.parametrize("kind", ["gzip", "deflate-zlib", "deflate-raw"])
def test_decoded_cap_is_inclusive(net: _Net, use_async: bool, kind: str) -> None:
    """해제 뒤 정확히 `max_bytes`는 통과, 1바이트 넘으면 같은 문구로 거부한다."""
    encoding, encode = _ENCODE[kind]
    net.serve("a.test/fit", encode(b"y" * 4096), encoding=encoding)
    net.serve("a.test/over", encode(b"y" * 4097), encoding=encoding)

    assert _get("https://a.test/fit", use_async=use_async, max_bytes=4096).content == b"y" * 4096
    with pytest.raises(SSRFBlockedError) as info:
        _get("https://a.test/over", use_async=use_async, max_bytes=4096)
    assert str(info.value) == _oversize(4096)


@ASYNC
@pytest.mark.parametrize("encoding", ["Gzip", " gzip ", "identity, gzip", "gzip, identity"])
def test_gzip_token_spelling_is_normalized(net: _Net, use_async: bool, encoding: str) -> None:
    net.serve("a.test/g", gzip.compress(b"spelled", mtime=0), encoding=encoding)
    assert _get("https://a.test/g", use_async=use_async).content == b"spelled"


@ASYNC
def test_multi_member_gzip_keeps_only_the_first_member(net: _Net, use_async: bool) -> None:
    """httpx와 같다 — 첫 멤버 끝에서 멈춘다."""
    net.serve(
        "a.test/m",
        gzip.compress(b"first", mtime=0) + gzip.compress(b"second", mtime=0),
        encoding="gzip",
    )
    assert _get("https://a.test/m", use_async=use_async).content == b"first"


@ASYNC
def test_truncated_and_corrupt_gzip_keep_the_current_behavior(net: _Net, use_async: bool) -> None:
    """트레일러가 잘린 gzip은 지금처럼 푼 만큼 돌려주고, 깨진 gzip은 `httpx.DecodingError`다."""
    net.serve("a.test/cut", gzip.compress(_PLAIN, mtime=0)[:-8], encoding="gzip", size=7)
    net.serve("a.test/bad", b"\x1f\x8b\x08\x00" + bytes(range(200)), encoding="gzip")

    assert _get("https://a.test/cut", use_async=use_async).content == _PLAIN
    with pytest.raises(httpx.DecodingError):
        _get("https://a.test/bad", use_async=use_async)


# ---------- identity: 상한 의미 그대로 ----------


@ASYNC
@pytest.mark.parametrize("encoding", [None, "identity"])
def test_identity_cap_is_unchanged(net: _Net, use_async: bool, encoding: str | None) -> None:
    net.serve("a.test/fit", b"x" * 1000, encoding=encoding, size=300)
    net.serve("a.test/over", b"x" * 1001, encoding=encoding, size=300)

    assert _get("https://a.test/fit", use_async=use_async, max_bytes=1000).content == b"x" * 1000
    with pytest.raises(SSRFBlockedError) as info:
        _get("https://a.test/over", use_async=use_async, max_bytes=1000)
    assert str(info.value) == _oversize(1000)


# ---------- 그 밖의 인코딩은 거부 ----------

_UNSUPPORTED = [
    ("br", "br"),
    ("zstd", "zstd"),
    ("gzip, gzip", "gzip,gzip"),
    ("x-gzip", "x-gzip"),
    ("compress", "compress"),
    ("x-zz9-unknown", "other"),
    ("deflate, br", "deflate,br"),
]


@ASYNC
@pytest.mark.parametrize(("encoding", "label"), _UNSUPPORTED)
def test_unsupported_encodings_are_rejected_without_decoding(
    net: _Net, monkeypatch: pytest.MonkeyPatch, use_async: bool, encoding: str, label: str
) -> None:
    """본문이 있는 첫 청크에서 `httpx.DecodingError` — zlib 해제기를 만들지도 않는다."""
    inflaters: list[Any] = []
    real = zlib.decompressobj

    def spy(*args: Any, **kwargs: Any) -> Any:
        inflaters.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(zlib, "decompressobj", spy)
    # 중첩 gzip 폭탄(두 겹 풀면 8 MiB)을 여러 청크로 — 거부는 첫 청크에서 끝난다
    net.serve("a.test/enc", _NESTED, encoding=encoding, size=16)

    def run() -> tuple[httpx.DecodingError, list[dict[str, Any]]]:
        with structlog.testing.capture_logs() as logs:
            with pytest.raises(httpx.DecodingError) as info:
                _get("https://a.test/enc", use_async=use_async)
        return info.value, logs

    (err, logs), peak = _traced_peak(run)
    assert str(err) == "지원하지 않는 압축(Content-Encoding) 응답입니다"
    assert peak < _BASE
    assert inflaters == []
    assert net.streams[0].served == 1
    assert [e for e in logs if e["event"] == "ssrf_response_rejected"] == [
        {
            "event": "ssrf_response_rejected",
            "host": "a.test",
            "method": "GET",
            "reason": "unsupported_encoding",
            "encoding": label,
            "log_level": "warning",
        }
    ]


@ASYNC
def test_rejection_log_has_no_header_value_or_query(net: _Net, use_async: bool) -> None:
    net.serve("a.test/p", b"payload", encoding="x-secret-zz9;q=SECRETH")
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(httpx.DecodingError) as info:
            _get("https://a.test/p?token=SECRETQ", use_async=use_async)
    assert [e["encoding"] for e in logs if e["event"] == "ssrf_response_rejected"] == ["other"]
    text = (str(logs) + str(info.value)).lower()
    assert "secret" not in text


@pytest.mark.parametrize(
    ("method", "status", "use_async"),
    [("HEAD", 200, True), ("GET", 204, True), ("GET", 204, False), ("GET", 200, False)],
)
def test_bodyless_responses_pass_with_any_encoding(
    net: _Net, method: str, status: int, use_async: bool
) -> None:
    net.serve("a.test/h", b"", encoding="br", status=status)
    with structlog.testing.capture_logs() as logs:
        res = _get("https://a.test/h", use_async=use_async, method=method)
    assert res.status_code == status
    assert res.content == b""
    assert not [e for e in logs if e["event"] == "ssrf_response_rejected"]


# ---------- 송신 Accept-Encoding ----------


@ASYNC
@pytest.mark.parametrize(
    ("policy", "headers", "expected"),
    [
        (None, None, "gzip, deflate"),
        (None, {"accept-ENCODING": "br, zstd", "X-Keep": "1"}, "gzip, deflate"),
        (OPEN, {"Accept-Encoding": "zstd"}, "gzip, deflate"),
        (FETCH_ONLY, None, "identity"),
    ],
    ids=["none", "none-caller", "open-caller", "fetch_only"],
)
def test_gateway_sends_its_own_accept_encoding_on_every_hop(
    net: _Net,
    use_async: bool,
    policy: EgressPolicy | None,
    headers: dict[str, str] | None,
    expected: str,
) -> None:
    net.routes["a.test/start"] = ("https://b.test/next", None)
    net.routes["b.test/next"] = ("https://a.test/end", None)
    sent = dict(headers) if headers else None

    _get("https://a.test/start", use_async=use_async, headers=sent, egress=policy)

    assert len(net.seen) == 3
    assert [r.headers.get_list("accept-encoding") for r in net.seen] == [[expected]] * 3
    assert sent == headers  # 호출자 dict는 그대로
    if headers and "X-Keep" in headers:
        assert all(r.headers["x-keep"] == "1" for r in net.seen)


async def test_fetch_only_caller_accept_encoding_is_still_rejected(net: _Net) -> None:
    with pytest.raises(EgressBlockedError) as info:
        await ssrf.ssrf_safe_request(
            "GET", "https://a.test/x", headers={"accept-encoding": "gzip"}, egress=FETCH_ONLY
        )
    assert info.value.reason == "header"
    assert net.seen == []


# ---------- 리다이렉트 홉마다 ----------


@ASYNC
@pytest.mark.parametrize("policy", [None, OPEN], ids=["none", "open"])
def test_rules_apply_after_every_redirect_hop(
    net: _Net, use_async: bool, policy: EgressPolicy | None
) -> None:
    """리다이렉트 응답(압축 헤더가 있어도)은 읽지 않고, 마지막 응답에도 같은 규칙이 선다."""
    net.routes["a.test/start"] = ("https://b.test/mid", "br")
    net.routes["b.test/mid"] = ("https://a.test/bomb", "gzip")
    net.serve("a.test/bomb", _SINGLE, encoding="gzip")

    def run() -> str:
        with pytest.raises(SSRFBlockedError) as info:
            _get("https://a.test/start", use_async=use_async, max_bytes=64 << 10, egress=policy)
        return str(info.value)

    message, peak = _traced_peak(run)
    assert message == _oversize(64 << 10)
    assert peak < _bound(64 << 10)

    net.seen.clear()
    net.routes["b.test/mid"] = ("https://a.test/br", None)
    net.serve("a.test/br", b"compressed", encoding="br")
    with pytest.raises(httpx.DecodingError):
        _get("https://a.test/start", use_async=use_async, egress=policy)
    assert [r.headers["accept-encoding"] for r in net.seen] == ["gzip, deflate"] * 3

    net.routes["b.test/mid"] = ("https://a.test/ok", None)
    net.serve("a.test/ok", gzip.compress(b"fine", mtime=0), encoding="gzip", size=3)
    assert _get("https://a.test/start", use_async=use_async, egress=policy).content == b"fine"


# ---------- 호출처: 기존 오류 경로 ----------


def test_parse_web_maps_a_rejected_encoding_to_its_fetch_error(
    net: _Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`parse_web`은 관문 실물을 탄다 — 거부는 기존 「가져오지 못했습니다」 경로로 간다."""
    from biznotes_core.config import get_settings
    from biznotes_core.services.ingestion.parsers import ParseError, parse_web

    monkeypatch.setattr(get_settings(), "ssrf_allow_private", False)
    # trafilatura는 코어 의존성이 아니다(지연 import). 실패 경로는 추출에 닿지 않는다
    if importlib.util.find_spec("trafilatura") is None:
        monkeypatch.setitem(sys.modules, "trafilatura", types.ModuleType("trafilatura"))
    net.serve("a.test/article", b"\x28\xb5\x2f\xfd" + bytes(64), encoding="zstd")

    with pytest.raises(ParseError) as info:
        parse_web("https://a.test/article")
    assert str(info.value) == (
        "URL을 가져오지 못했습니다: https://a.test/article "
        "(DecodingError: 지원하지 않는 압축(Content-Encoding) 응답입니다)"
    )
    assert isinstance(info.value.__cause__, httpx.DecodingError)
    assert net.seen[0].headers["accept-encoding"] == "gzip, deflate"
