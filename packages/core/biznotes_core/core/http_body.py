"""응답 본문 상한 읽기(#413·#421) — 원본 전송 바이트와 해제 뒤 바이트를 모두 `max_bytes`로 묶는다.

SSRF 관문(`core/ssrf`)이 쓴다.

- 요청은 `ACCEPT_ENCODING`만 청한다. 호출자가 보낼 헤더의 값을 이것으로 덮는다.
- 본문은 `iter_raw`·`aiter_raw`(원본 전송 바이트)로 읽고 표준 `zlib`로 출력량을 묶어 푼다. httpx
  디코더는 출력 상한 없이 풀어, 상한 판정 전에 수백 바이트가 수백 MiB가 된다.
- `Content-Encoding`이 없거나 `identity`면 그대로, `gzip`·`deflate` 한 겹이면 묶어 푼다. 그 밖
  (br·zstd·중첩·x-gzip·compress·모르는 토큰)은 본문이 있는 첫 청크에서 `httpx.DecodingError`로
  거부하고 경고 로그를 남긴다. 로그 이벤트 이름은 호출자가 정한다.
- 크기 초과 예외도 호출자가 정한다(`on_oversize`).

계층은 `core`다 — 표준·서드파티 라이브러리만 import한다.
"""

import zlib
from collections.abc import Callable
from typing import Any, NoReturn

import httpx
import structlog

__all__ = ["ACCEPT_ENCODING", "CappedDecoder", "read_capped", "read_capped_async"]

# 청하는 응답 인코딩 — 출력량을 묶어 풀 수 있는 것만
ACCEPT_ENCODING = "gzip, deflate"
# 거부 로그에 이름 그대로 남기는 인코딩 토큰. 그 밖은 `other`다(헤더 원문을 남기지 않는다)
_KNOWN_ENCODINGS = frozenset({"gzip", "deflate", "br", "zstd", "x-gzip", "compress"})
_UNSUPPORTED_ENCODING = "지원하지 않는 압축(Content-Encoding) 응답입니다"

logger = structlog.get_logger()


class CappedDecoder:
    """응답 본문 해제기 — 원본 누적과 해제 누적이 `max_bytes`를 넘으면 `on_oversize()`를 올린다.

    방식은 `content_encoding`(응답 `Content-Encoding` 값)으로 고른다. 토큰을 소문자로 바꾸고
    공백·`identity`를 뺀 뒤, 0개면 identity, `gzip`·`deflate` 하나면 묶어 풀기, 그 밖은 거부다.
    빈 문자열을 넘기면 identity다.

    httpx 디코더는 `decompress(data)`를 출력 상한 없이 부른다. 여기서는 `decompress(data, 남은
    예산 + 1)`로 한 번에 낼 출력을 묶는다.

    - `identity`: 그대로 낸다(원본 상한이 곧 해제 상한이다).
    - `gzip`·`deflate`: 출력량을 묶어 푼다. deflate는 zlib 헤더 형식을 먼저 쓰고, 첫 입력에서
      `zlib.error`면 raw로 다시 푼다(httpx와 같다). 첫 입력은 헤더 2바이트를 다 모은 뒤에 넣는다
      — 1바이트 청크면 httpx는 둘째 청크에서 헤더 오류를 만나 raw로 바꾸지 못한다.
    - 거부: 본문이 있는 첫 청크에서 경고 로그(`reject_event`)와 `httpx.DecodingError`(고정 문구).
      본문 없는 응답은 통과한다.

    스트림 끝(`eof`) 뒤 입력은 풀지 않는다 — 다시 `decompress`를 부르면 `unused_data`가 끝없이
    쌓인다. 여러 멤버 gzip이 첫 멤버만 남는 것과 잘린 gzip을 허용하는 것은 httpx와 같다.
    """

    def __init__(
        self,
        content_encoding: str,
        max_bytes: int,
        request: httpx.Request,
        *,
        on_oversize: Callable[[], Exception],
        reject_event: str,
    ) -> None:
        self._coding, self._label = _select(content_encoding)
        self._max_bytes = max_bytes
        self._request = request
        self._on_oversize = on_oversize
        self._reject_event = reject_event
        self._raw = 0
        self._decoded = 0
        self._pending = b""  # deflate 형식을 고르기 전 입력
        # zlib은 `_Decompress`를 공개 타입으로 내지 않는다(`services/skills/package.py`와 같다)
        self._inflater: Any = (
            zlib.decompressobj(16 + zlib.MAX_WBITS) if self._coding == "gzip" else None
        )
        self.eof = False

    def feed(self, raw: bytes) -> bytes:
        """원본 청크 하나를 받아 해제한 바이트를 낸다 — 원본 상한을 먼저 잰다."""
        if not raw or self.eof:
            return b""
        self._raw += len(raw)
        if self._raw > self._max_bytes:
            raise self._on_oversize()
        if self._coding == "identity":
            return raw
        if self._coding == "reject":
            self._reject()
        if self._inflater is None:
            return self._start_deflate(raw, final=False)
        return self._inflate(raw)

    def flush(self) -> bytes:
        out = b""
        if self._pending:  # 2바이트가 안 되게 끝난 deflate
            out = self._start_deflate(b"", final=True)
        if self._inflater is None or self.eof:
            return out
        try:
            # 남은 입력은 `_inflate`가 다 넣었다 — 여기서 나오는 출력도 상한에 넣는다
            tail = self._inflater.flush()
        except zlib.error as e:
            raise httpx.DecodingError(str(e), request=self._request) from e
        self._count(tail)
        return out + tail

    def _start_deflate(self, raw: bytes, *, final: bool) -> bytes:
        self._pending += raw
        if len(self._pending) < 2 and not final:
            return b""
        data, self._pending = self._pending, b""
        self._inflater = zlib.decompressobj()
        try:
            return self._inflate(data, first=True)
        except zlib.error:
            self._decoded = 0
            self._inflater = zlib.decompressobj(-zlib.MAX_WBITS)
            return self._inflate(data)

    def _inflate(self, data: bytes, *, first: bool = False) -> bytes:
        # 한 번의 feed에 쌓이는 조각은 많아야 하나다 — 한도를 다 채운 조각은 `_count`가 초과로
        # 끊고, 덜 채운 조각이면 zlib이 입력을 다 소비해 `unconsumed_tail`이 빈다
        out: list[bytes] = []
        try:
            while data:
                # `max_length`는 양수로 둔다 — 0이면 무제한이다. 예산 + 1까지만 풀어 초과를 안다
                limit = max(1, self._max_bytes - self._decoded + 1)
                piece = self._inflater.decompress(data, limit)
                self._count(piece)
                if piece:
                    out.append(piece)
                if self._inflater.eof:
                    self.eof = True
                    break
                data = self._inflater.unconsumed_tail
        except zlib.error as e:
            if first:
                raise
            raise httpx.DecodingError(str(e), request=self._request) from e
        return b"".join(out)

    def _count(self, piece: bytes) -> None:
        self._decoded += len(piece)
        if self._decoded > self._max_bytes:
            raise self._on_oversize()

    def _reject(self) -> NoReturn:
        method = self._request.method
        logger.warning(
            self._reject_event,
            host=_log_host(self._request.url),
            method=method if _plain_token(method) else "invalid",
            reason="unsupported_encoding",
            encoding=self._label,
        )
        raise httpx.DecodingError(_UNSUPPORTED_ENCODING, request=self._request)


def read_capped(resp: httpx.Response, decoder: CappedDecoder) -> bytes:
    """본문을 원본 전송 바이트(`iter_raw`)로 읽어 해제한 본문을 돌려준다 — 상한은 `decoder`가 잰다.

    해제기가 스트림 끝을 보면 남은 원본은 읽지 않는다. 본문은 `bytearray` 하나에 잇는다 — 청크마다
    리스트 항목을 두면 1바이트 청크(빈 해제 출력 포함)에서 메모리가 바이트 수가 아니라 청크 수에
    비례한다.
    """
    body = bytearray()
    for chunk in resp.iter_raw():
        body += decoder.feed(chunk)
        if decoder.eof:
            break
    body += decoder.flush()
    return bytes(body)


async def read_capped_async(resp: httpx.Response, decoder: CappedDecoder) -> bytes:
    """`read_capped`의 비동기 판(`aiter_raw`)."""
    body = bytearray()
    async for chunk in resp.aiter_raw():
        body += decoder.feed(chunk)
        if decoder.eof:
            break
    body += decoder.flush()
    return bytes(body)


def _select(content_encoding: str) -> tuple[str, str]:
    """`Content-Encoding` 값 → (방식, 거부 로그의 인코딩). 방식은 identity·gzip·deflate·reject다."""
    tokens = [
        t for t in (p.strip().lower() for p in content_encoding.split(",")) if t and t != "identity"
    ]
    if not tokens:
        return "identity", ""
    if len(tokens) == 1 and tokens[0] in ("gzip", "deflate"):
        return tokens[0], ""
    # 로그에는 아는 토큰 이름만 남긴다 — 토큰이 많으면 통째로 `other`
    if len(tokens) > 4:
        return "reject", "other"
    return "reject", ",".join(t if t in _KNOWN_ENCODINGS else "other" for t in tokens)


def _plain_token(text: str) -> bool:
    return text.isascii() and text.isalpha() and len(text) <= 16


def _log_host(url: httpx.URL) -> str | None:
    """로그용 호스트(IDNA ASCII) — 없거나 해석할 수 없으면 None."""
    try:
        return url.raw_host.decode("ascii") or None
    except Exception:  # noqa: BLE001 — 로그 필드 하나 때문에 거부 경로가 깨지지 않게
        return None
