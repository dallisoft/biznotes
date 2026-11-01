"""SSRF 방지 — 외부 URL 검증 및 안전 fetch (웹 소스, 에이전트 http 노드).

내부망/링크로컬/루프백 IP로의 요청을 차단한다.

두 계층:
- `validate_external_url`: 등록 시점 조기 차단(UX). 호스트명 DNS 해석 후 사설 IP 거부.
- `ssrf_safe_fetch`: **fetch 시점 단일 관문**(R5-1). 리다이렉트를 수동 추적하며
  **매 홉 스킴·호스트·공인 IP를 재검증**한다. 등록 시점 검증만으로는 리다이렉트를
  통한 우회가 가능하므로 실제 원격 fetch는 반드시 이 관문을 통과해야 한다.
  Location 있는 301·302·303·307·308만 따라가고 그 밖의 3xx(304 등)는 최종 응답이다(#424).

  검증한 공인 IP로 직접 연결하고 원 호스트는 Host 헤더와 TLS SNI에 유지하므로
  검증 직후 연결 직전 사이의 DNS rebinding 창을 닫는다. 관문 Client는 환경 프록시를
  무시한다(`trust_env=False`) — 관문 경유 outbound의 프록시는 지원하지 않는다.

  리다이렉트가 다른 출처(scheme·host·port)로 넘어가면 호출자 헤더에서 `Authorization`·
  `Cookie`·`Proxy-Authorization`을 빼고, 그 뒤 홉에서는 되살리지 않는다(#176). 다른
  이름의 자격증명 헤더(`X-API-Key` 등)는 빼지 않는다. 출처는 기본 포트를 채워 비교하고,
  표기가 다르면(후행 점, IPv6 비정규 표기 등) 같은 호스트라도 교차 출처로 본다.

외부 연결 정책(`EgressPolicy`, #395): 두 관문에 `egress=`를 넘기면 **첫 DNS 전과 매 홉**
`check_egress`로 판정한다. 받아오기 전용(`fetch_only`)은 GET·HEAD, 본문 없음, 허용 헤더만,
userinfo 없음, URL·쿼리 길이 상한이고 리다이렉트 뒤에도 원 메서드를 유지한다. 응답은
`Accept-Encoding: identity`로 청하고 압축 응답은 본문을 읽기 전에 거부한다. 허용 호스트
목록은 모드와 무관하게 매 홉 본다. `egress=None`(기본)이면 open과 같다. 이 모듈은
설정을 읽지 않는다 — 정책은 호출자(서비스 계층)가 만든다.

open 모드 응답 압축(#413): 관문이 `Accept-Encoding: gzip, deflate`를 고정으로 보내고(호출자 값은
덮는다), 본문을 원본 전송 바이트로 읽어 표준 `zlib`로 출력량을 묶으며 푼다. 원본 누적과 해제 누적
모두 `max_bytes`를 넘으면 크기 초과다. 그 밖의 인코딩(br·zstd·중첩·x-gzip·compress·모르는 토큰)은
본문이 있으면 `httpx.DecodingError`로 거부한다. 해제기는 `core/http_body`다
(#421).
"""

import functools
import ipaddress
import socket
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import anyio
import httpx
import structlog

from biznotes_core.core.http_body import (
    ACCEPT_ENCODING,
    CappedDecoder,
    read_capped,
    read_capped_async,
)
from biznotes_core.core.metrics import agent_http_blocked_total

# 리다이렉트 최대 추적 홉 수 — 무한 리다이렉트 루프 방지.
_MAX_REDIRECTS = 5
# fetch 응답 본문 상한(bytes) — 대용량 응답으로 워커 메모리 고갈 방지.
_MAX_RESPONSE_BYTES = 25 * 1024 * 1024
# 시도별 connect 상한의 하한(초) — 검증 IP가 많아도 각 시도에 최소한의 연결 시간은 준다.
_MIN_CONNECT_TIMEOUT = 1.0
# 교차 출처 리다이렉트에서 빼는 자격증명 헤더(소문자, #176)
_CREDENTIAL_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
# open 모드가 청하는 응답 인코딩(#413) — 관문이 출력량을 묶어 풀 수 있는 것만
_OPEN_ACCEPT_ENCODING = ACCEPT_ENCODING

logger = structlog.get_logger()


class SSRFBlockedError(Exception):
    # 노드 실패 코드(#216). 메시지는 #42로 이미 고정 문구다
    node_error_code = "ssrf_blocked"


# 받아오기 전용 모드(#395)에서 보낼 수 있는 메서드와 헤더 이름(소문자). `User-Agent`는 받되
# 값을 `FETCH_ONLY_USER_AGENT`로 덮는다 — 값에 데이터를 실어 보내지 못하게
FETCH_ONLY_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})
FETCH_ONLY_HEADERS: frozenset[str] = frozenset(
    {"accept", "accept-language", "if-none-match", "if-modified-since", "range", "user-agent"}
)
FETCH_ONLY_USER_AGENT = "biznotes-core-fetch/1"

# 거부 사유 코드 → 문구. 문구에는 URL·헤더 값을 넣지 않는다
# (실행 로그·재시도 경고가 `str(e)`를 남긴다)
_EGRESS_REASONS: dict[str, str] = {
    "method": "GET·HEAD만 보낼 수 있습니다",
    "body": "요청 본문을 보낼 수 없습니다",
    "header": "허용하지 않는 요청 헤더입니다",
    "userinfo": "URL에 사용자 정보(userinfo)를 넣을 수 없습니다",
    "url_too_long": "URL이 길이 상한을 넘습니다",
    "query_too_long": "쿼리가 길이 상한을 넘습니다",
    "host_not_allowed": "허용 목록에 없는 호스트입니다",
    "non_literal_target": "url 대상(scheme·호스트·포트)이 리터럴이 아닙니다",
    "encoded_response": "압축(Content-Encoding)된 응답은 받지 않습니다",
}


class EgressBlockedError(SSRFBlockedError):
    """외부 연결 정책(`EgressPolicy`) 위반 — `reason`은 `_EGRESS_REASONS`의 사유 코드다.

    `SSRFBlockedError` 하위라 기존 catch가 그대로 막는다(fail closed). 사유를 전하려면 먼저 잡는다.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        detail = _EGRESS_REASONS.get(reason, "외부 연결 정책 위반입니다")
        super().__init__(f"외부 HTTP 요청이 연결 정책으로 차단되었습니다({reason}): {detail}")

    def record(self, method: str, url: str) -> None:
        """거부 1건을 경고 로그 `agent_http_blocked`와 지표에 남긴다 — method·호스트·사유만.

        쿼리·경로·헤더 값은 남기지 않는다. 형식이 이상한 method는 값 대신 `invalid`로 적는다.
        """
        method_text = str(method)
        logger.warning(
            "agent_http_blocked",
            method=method_text.upper() if _plain_token(method_text) else "invalid",
            host=_log_host(url),
            reason=self.reason,
        )
        agent_http_blocked_total.labels(reason=self.reason).inc()


@dataclass(frozen=True)
class EgressPolicy:
    """에이전트 외부 HTTP 연결 정책(#395). 호출 시점 설정으로 서비스 계층이 만든다.

    - `fetch_only`: GET·HEAD, 본문 없음, `FETCH_ONLY_HEADERS`만, userinfo 없음, 인코딩한
      URL·쿼리가 상한 이내. 거짓이면 이 규칙들을 보지 않는다(지금 동작).
    - `allowed_hosts`: 비어 있지 않으면 모드와 무관하게 맞는 호스트만(`host_matches`).
    """

    fetch_only: bool
    allowed_hosts: tuple[str, ...]
    max_url_chars: int
    max_query_chars: int


def _host_key(name: str) -> str:
    """비교용 호스트 표기 — 소문자 IDNA ASCII. 인코딩할 수 없으면 소문자 원문이다."""
    lowered = name.lower()
    if lowered.isascii():
        return lowered
    try:
        raw = httpx.URL("http://placeholder/").copy_with(host=lowered).raw_host
    except (httpx.InvalidURL, ValueError):
        return lowered
    return raw.decode("ascii").lower()


def host_matches(host: str, patterns: Iterable[str]) -> bool:
    """호스트가 패턴 중 하나와 맞는가 — 양쪽 모두 소문자 IDNA ASCII로 비교한다.

    `*.example.com`은 선행 점을 포함한 접미사(`.example.com`)로만 맞는다 — `example.com` 자체,
    `evil-example.com`, `example.com.evil.net`은 맞지 않는다. 그 밖의 패턴은 정확 일치다.
    호스트의 후행 점은 떼고 비교한다(패턴은 그대로).
    """
    key = _host_key(host.rstrip("."))
    for raw_pattern in patterns:
        pattern = str(raw_pattern)
        if pattern.startswith("*."):
            suffix = "." + _host_key(pattern[2:])
            if key != suffix.lstrip(".") and key.endswith(suffix):
                return True
        elif key == _host_key(pattern):
            return True
    return False


def check_egress(
    method: str,
    url: str,
    headers: Mapping[str, str] | None,
    has_body: bool,
    policy: EgressPolicy,
) -> None:
    """외부 연결 정책 판정(#395) — 위반이면 `EgressBlockedError`. DNS·네트워크·로그가 없다.

    `fetch_only`면 메서드 → 본문 → 헤더 → userinfo → URL 길이 → 쿼리 길이 순으로 보고,
    두 모드 모두 끝으로 허용 호스트를 본다. 길이는 인코딩한 형태(`httpx.URL`)로 잰다 — 한글
    1자는 9자다. 규칙이 하나도 없는 정책(open·목록 없음)은 아무것도 보지 않는다. 판정해야
    하는데 URL을 해석할 수 없으면 `SSRFBlockedError`다(fail closed).
    """
    if not policy.fetch_only and not policy.allowed_hosts:
        return
    if policy.fetch_only:
        if str(method).upper() not in FETCH_ONLY_METHODS:
            raise EgressBlockedError("method")
        if has_body:
            raise EgressBlockedError("body")
        if headers is not None and (
            not isinstance(headers, Mapping)
            or any(not _fetch_only_header(name, value) for name, value in headers.items())
        ):
            raise EgressBlockedError("header")
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError) as e:
        raise SSRFBlockedError("URL을 해석할 수 없습니다") from e
    if policy.fetch_only:
        if parsed.userinfo:
            raise EgressBlockedError("userinfo")
        if len(str(parsed)) > policy.max_url_chars:
            raise EgressBlockedError("url_too_long")
        if len(parsed.query) > policy.max_query_chars:
            raise EgressBlockedError("query_too_long")
    if policy.allowed_hosts:
        host = parsed.raw_host.decode("ascii")
        if not host or not host_matches(host, policy.allowed_hosts):
            raise EgressBlockedError("host_not_allowed")


def _fetch_only_header(name: Any, value: Any) -> bool:
    """받아오기 전용에서 보낼 수 있는 헤더인가 — 허용 이름, 문자열 값, 제어 문자 없음.

    제어 문자(C0·DEL)를 httpx에 넘기면 값이 담긴 예외 문구(`Illegal header value b'…'`)가
    실행 로그·재시도 경고로 흐른다. 그 전에 `header`로 거부한다.
    """
    return (
        isinstance(name, str)
        and isinstance(value, str)
        and name.lower() in FETCH_ONLY_HEADERS
        and not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name + value)
    )


def _enforce_egress(
    method: str,
    url: str,
    headers: Mapping[str, str] | None,
    has_body: bool,
    policy: EgressPolicy,
) -> None:
    """관문 안의 판정 — 거부는 로그·지표에 남기고 그대로 올린다."""
    try:
        check_egress(method, url, headers, has_body, policy)
    except EgressBlockedError as e:
        e.record(method, url)
        raise


def _egress_headers(
    headers: dict[str, str] | None, egress: EgressPolicy | None
) -> dict[str, str]:
    """관문이 보낼 호출자 헤더 — 호출자 `Accept-Encoding`(대소문자 무관)은 모드와 무관하게 덮는다.

    open(`egress=None` 포함)은 관문이 묶어 풀 수 있는 `gzip, deflate`만 청한다(#413). 받아오기
    전용은 `User-Agent`를 코어 고정값으로 덮고 `identity`만 청한다. 다른 헤더는 그대로 둔다 —
    허용 밖 헤더를 조용히 빼지 않고 판정(`check_egress`)이 거부한다. 받아오기 전용에서
    `Accept-Encoding`은 허용 헤더가 아니라 호출자 값은 판정에서 이미 거부된다.
    """
    fetch_only = egress is not None and egress.fetch_only
    dropped = ("user-agent", "accept-encoding") if fetch_only else ("accept-encoding",)
    kept = {k: v for k, v in (headers or {}).items() if k.lower() not in dropped}
    if fetch_only:
        kept["User-Agent"] = FETCH_ONLY_USER_AGENT
        kept["Accept-Encoding"] = "identity"
    else:
        kept["Accept-Encoding"] = _OPEN_ACCEPT_ENCODING
    return kept


def _oversize(max_bytes: int) -> SSRFBlockedError:
    # 호출처가 문구 접두(`응답이 너무 큽니다`)에 기댄다 — 원본·해제 뒤 초과 모두 같은 문구다
    return SSRFBlockedError(f"응답이 너무 큽니다(>{max_bytes} bytes)")


def _body_decoder(
    resp: httpx.Response, egress: EgressPolicy | None, method: str, url: str, max_bytes: int
) -> CappedDecoder:
    """응답 본문 해제기를 고른다 — 본문을 읽기 전에 부른다.

    받아오기 전용은 identity만 받는다 — 압축 응답은 읽기 전에 `encoded_response`로 거부한다.
    open은 `Content-Encoding` 토큰(소문자, 공백·`identity` 제외)이 없으면 그대로, `gzip`·
    `deflate` 하나면 출력량을 묶어 풀고, 그 밖(br·zstd·중첩·x-gzip·compress·모르는 토큰)은
    거부한다(#413). 해제기는 공용 `http_body.CappedDecoder`다 — 크기 초과는 `_oversize`, 거부
    로그는 `ssrf_response_rejected`다.
    """
    header = resp.headers.get("content-encoding", "")
    if egress is not None and egress.fetch_only:
        encoding = header.strip().lower()
        if encoding and encoding != "identity":
            err = EgressBlockedError("encoded_response")
            err.record(method, url)
            raise err
        header = ""  # identity
    return CappedDecoder(
        header,
        max_bytes,
        httpx.Request(method, url),
        on_oversize=functools.partial(_oversize, max_bytes),
        reject_event="ssrf_response_rejected",
    )


def _plain_token(text: str) -> bool:
    return text.isascii() and text.isalpha() and len(text) <= 16


def _log_host(url: Any) -> str | None:
    """로그용 호스트(IDNA ASCII) — 해석할 수 없으면 None."""
    try:
        return _ascii_host(str(url))
    except Exception:  # noqa: BLE001 — 로그 필드 하나 때문에 거부 경로가 깨지지 않게
        return None


def _is_private(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_private(str(ip.ipv4_mapped))
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_public_ips(host: str) -> list[str]:
    """호스트를 해석하고 모든 결과 IP가 공인 IP인지 검증한 뒤 IP 목록 반환.

    하나라도 사설/내부 IP면 SSRFBlockedError. 호출부는 반환된 IP 전건을 순서대로
    직접 연결 대상으로 사용한다.

    **예외 문구에 해석 IP를 넣지 않는다**(#42) — 문구는 사용자에게 그대로 돌아간다.
    진단이 필요한 IP는 서버 로그(warning)에만 남는다.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise SSRFBlockedError(f"호스트를 해석할 수 없습니다: {host}") from e

    ips: list[str] = []
    for info in infos:
        ip_str = str(info[4][0])
        if _is_private(ip_str):
            # 해석 IP는 **로그에만** 남긴다(#42) — 이 예외 문구는 소스 `error_message`·API 422
            # 응답·에이전트 실행 결과로 가공 없이 사용자에게 돌아간다. 공격자가 자기 DNS 이름을
            # 등록해 내부 DNS의 해석 결과(어떤 사설 IP인지)를 읽어 내는 정찰을 막는다.
            logger.warning("ssrf_private_address_blocked", host=host, resolved_ip=ip_str)
            raise SSRFBlockedError(f"내부망 주소로의 요청이 차단되었습니다: {host}")
        ips.append(ip_str)
    if not ips:
        raise SSRFBlockedError(f"호스트를 해석할 수 없습니다: {host}")
    # socktype 힌트가 없어 한 주소가 여러 항목(STREAM/DGRAM 등)으로 오므로 **전건 검증 뒤**
    # 순서를 보존해 중복을 제거한다 — 같은 IP를 여러 번 재시도·로깅하지 않기 위함.
    return list(dict.fromkeys(ips))


def validate_external_url(url: str, allow_private: bool = False) -> None:
    """http/https 스킴 + 공인 IP만 허용. 위반 시 SSRFBlockedError.

    등록 시점 조기 차단용. 실제 fetch는 `ssrf_safe_fetch`가 매 홉 재검증한다 —
    등록과 fetch 사이의 DNS 재해석/리다이렉트 우회는 이 함수만으로 막지 못한다.

    allow_private: 개발 환경에서 내부 테스트용 (APP_ENV=dev에서만 사용 권장).

    해석하는 호스트는 관문(`_validate_hop`)과 같은 IDNA ASCII 표기다 — 유니코드를 그대로
    `getaddrinfo`에 넘기면 내장 idna 코덱(IDNA2003)이 다른 호스트(`faß.de` → `fass.de`)를 묻는다.
    인코딩할 수 없는 호스트는 묻지 않고 거부한다.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SSRFBlockedError(f"허용되지 않는 스킴: {parsed.scheme}")
    if not parsed.hostname:
        raise SSRFBlockedError("호스트가 없는 URL입니다")
    host = _ascii_host(url)

    if allow_private:
        return

    _resolve_public_ips(host)


def ssrf_safe_fetch(
    url: str,
    *,
    allow_private: bool = False,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
    max_bytes: int = _MAX_RESPONSE_BYTES,
    egress: EgressPolicy | None = None,
) -> httpx.Response:
    """SSRF 안전 fetch — fetch 경로 단일 관문(R5-1).

    리다이렉트 자동 추적을 끄고 **수동으로 추적하며 매 홉마다** (1) 스킴·호스트
    검증 (2) 호스트 DNS 해석 후 모든 결과 IP가 공인 IP인지 확인한 뒤, 검증한 IP로
    직접 연결한다.
    이로써 등록 시점 검증을 통과한 URL이 리다이렉트로 사설 IP에 도달하는 우회를
    차단한다(CR-R5-05 수락 기준).

    원 호스트는 Host 헤더와 TLS SNI/인증서 검증 대상으로 유지한다. 각 IP 시도마다
    새 Client를 만들어 연결 풀과 SNI를 재사용하지 않으며 환경 프록시는 무시한다.
    따라서 관문 경유 outbound는 프록시를 지원하지 않는다.

    본문은 **스트리밍으로 읽으며 원본 전송 바이트나 해제 뒤 바이트가 max_bytes를 초과하면
    중단**한다(Content-Length 유무·정확성에 의존하지 않음). 응답 압축은 모듈 docstring(#413).

    교차 출처 리다이렉트에서는 자격증명 헤더를 뺀다(모듈 docstring, #176).

    `egress`가 있으면 매 홉 DNS 전에 `check_egress`로 판정한다(#395, 모듈 docstring).

    Returns: 최종 성공 응답(httpx.Response, 본문 읽힘 완료).
    Raises: SSRFBlockedError(검증 실패·크기 초과, 정책 위반은 하위 `EgressBlockedError`),
        httpx.HTTPError(네트워크 오류, 손상되거나 지원하지 않는 압축은 `httpx.DecodingError`).
    """
    current = url
    credentials = _RedirectCredentials(_egress_headers(headers, egress))
    for _hop in range(_MAX_REDIRECTS + 1):
        if egress is not None:
            _enforce_egress("GET", current, headers, False, egress)
        # timeout 예산은 홉 단위(기존 계약: 홉마다 timeout)이며, 한 홉 안의 검증 IP
        # 순차 시도(D-14)가 이 예산을 공유한다.
        deadline = time.monotonic() + timeout
        ips = _validate_hop(current, allow_private=allow_private)
        hop_headers = credentials.for_hop(current)
        if allow_private:
            with httpx.Client(
                timeout=_remaining_timeout(deadline, "GET", current),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                with client.stream("GET", current, headers=hop_headers) as resp:
                    if resp.has_redirect_location:
                        current = _next_redirect(current, resp)
                        continue
                    decoder = _body_decoder(resp, egress, "GET", current, max_bytes)
                    return _capped_response(
                        resp, _read_capped(resp, max_bytes, decoder), "GET", current
                    )

        next_url: str | None = None
        last_connect_error: httpx.ConnectError | httpx.ConnectTimeout | None = None
        for idx, ip in enumerate(ips):
            # 예산 계산은 아래 except 밖에서 — 예산 소진이 앞선 실제 오류를 덮지 않게(P3-15).
            attempt_timeout = _attempt_budget(
                deadline, "GET", current, len(ips) - idx, last_connect_error
            )
            pinned_url, host_header, sni_host = _pin(current, ip)
            request_headers = _pinned_headers(hop_headers, host_header)
            trace_record: dict[str, object] = {}
            try:
                # 보안 불변식: 매 홉·매 IP 시도마다 새 Client를 만들고 닫아 연결/SNI를
                # 절대 재사용하지 않는다. 이 Client를 IP 루프 밖으로 올리면 안 된다.
                with httpx.Client(
                    timeout=attempt_timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    with client.stream(
                        "GET",
                        pinned_url,
                        headers=request_headers,
                        extensions={"sni_hostname": sni_host, "trace": _pin_trace(trace_record)},
                    ) as resp:
                        if resp.has_redirect_location:
                            next_url = _next_redirect(current, resp)
                        else:
                            decoder = _body_decoder(resp, egress, "GET", current, max_bytes)
                            return _capped_response(
                                resp, _read_capped(resp, max_bytes, decoder), "GET", current
                            )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_connect_error = exc
                continue
            finally:
                _log_pin_attempt(current, ip, trace_record)
            break

        if next_url is not None:
            current = next_url
            continue
        if last_connect_error is not None:
            raise last_connect_error

    raise SSRFBlockedError(f"리다이렉트가 너무 많습니다(>{_MAX_REDIRECTS})")


async def ssrf_safe_request(
    method: str,
    url: str,
    *,
    allow_private: bool = False,
    timeout: float = 30.0,  # noqa: ASYNC109 — httpx 클라이언트 타임아웃으로 전달(asyncio.timeout 아님)
    headers: dict[str, str] | None = None,
    json: object | None = None,
    max_bytes: int = _MAX_RESPONSE_BYTES,
    egress: EgressPolicy | None = None,
) -> httpx.Response:
    """SSRF 안전 요청(비동기) — agent http 노드 등 임의 메서드 fetch 관문(R5-1).

    `ssrf_safe_fetch`의 비동기·임의 메서드 판. 리다이렉트를 수동 추적하며 매 홉
    스킴·호스트 검증 + 공인 IP 확인 후 검증 IP로 직접 연결한다. pinned-IP와
    프록시 비지원 계약은 `ssrf_safe_fetch` docstring과 동일하다.

    **리다이렉트 시 메서드는 GET으로 강등하고 요청 body를 버린다**(303류 안전화 +
    body 재전송 방지). `_exec_http`의 계약 변경은 호출부 주석 참조. 본문은 스트리밍
    으로 읽으며 max_bytes 초과 시 중단한다. 교차 출처 리다이렉트에서는 자격증명 헤더를
    뺀다(모듈 docstring, #176).

    `egress`가 있으면 매 홉 DNS 전에 `check_egress`로 판정한다(#395). 받아오기 전용이면
    리다이렉트 뒤에도 원 메서드(GET·HEAD)를 유지한다 — 강등은 `egress`가 없거나 open일 때만이다.

    홉마다의 DNS 해석은 워커 스레드에서 돈다 — 느린 DNS 동안에도 이벤트 루프가 돌아 호출측
    제한 시간(`anyio.fail_after`)·heartbeat가 동작하고, 취소되면 기다리지 않고 바로 돌아간다.
    """
    current = url
    keep_method = egress is not None and egress.fetch_only
    credentials = _RedirectCredentials(_egress_headers(headers, egress))
    for _hop in range(_MAX_REDIRECTS + 1):
        if egress is not None:
            _enforce_egress(method, current, headers, json is not None, egress)
        # timeout 예산은 홉 단위이며 한 홉 안의 검증 IP 순차 시도가 공유한다.
        deadline = time.monotonic() + timeout
        ips = await anyio.to_thread.run_sync(
            functools.partial(_validate_hop, current, allow_private=allow_private),
            abandon_on_cancel=True,
        )
        hop_headers = credentials.for_hop(current)
        if allow_private:
            async with httpx.AsyncClient(
                timeout=_remaining_timeout(deadline, method, current),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(method, current, headers=hop_headers, json=json) as resp:
                    if resp.has_redirect_location:
                        current = _next_redirect(current, resp)
                        method = method if keep_method else "GET"
                        json = None
                        continue
                    decoder = _body_decoder(resp, egress, method, current, max_bytes)
                    return _capped_response(
                        resp, await _read_capped_async(resp, max_bytes, decoder), method, current
                    )

        next_url = None
        last_connect_error: httpx.ConnectError | httpx.ConnectTimeout | None = None
        for idx, ip in enumerate(ips):
            # 예산 계산은 아래 except 밖에서 — 예산 소진이 앞선 실제 오류를 덮지 않게(P3-15).
            attempt_timeout = _attempt_budget(
                deadline, method, current, len(ips) - idx, last_connect_error
            )
            pinned_url, host_header, sni_host = _pin(current, ip)
            request_headers = _pinned_headers(hop_headers, host_header)
            trace_record: dict[str, object] = {}
            try:
                # 보안 불변식: 매 홉·매 IP 시도마다 새 Client를 만들고 닫아 연결/SNI를
                # 절대 재사용하지 않는다. 이 Client를 IP 루프 밖으로 올리면 안 된다.
                async with httpx.AsyncClient(
                    timeout=attempt_timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    async with client.stream(
                        method,
                        pinned_url,
                        headers=request_headers,
                        json=json,
                        extensions={
                            "sni_hostname": sni_host,
                            "trace": _pin_atrace(trace_record),
                        },
                    ) as resp:
                        if resp.has_redirect_location:
                            next_url = _next_redirect(current, resp)
                        else:
                            decoder = _body_decoder(resp, egress, method, current, max_bytes)
                            return _capped_response(
                                resp,
                                await _read_capped_async(resp, max_bytes, decoder),
                                method,
                                current,
                            )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_connect_error = exc
                continue
            finally:
                _log_pin_attempt(current, ip, trace_record)
            break

        if next_url is not None:
            current = next_url
            method = method if keep_method else "GET"
            json = None
            continue
        if last_connect_error is not None:
            raise last_connect_error

    raise SSRFBlockedError(f"리다이렉트가 너무 많습니다(>{_MAX_REDIRECTS})")


def _ascii_host(url: str) -> str:
    """URL 호스트를 IDNA ASCII(punycode) 표기로 돌려준다.

    IDN 호스트(`bücher.test`)를 유니코드 그대로 Host 헤더·SNI에 쓰면 요청 생성 단계에서
    `UnicodeEncodeError`가 난다(헤더·SNI는 ASCII). httpx가 URL 파싱 때 만들어 둔
    `raw_host`(IDNA 인코딩 결과)를 써서 **검증·Host·SNI가 모두 같은 ASCII 표기**를
    쓰도록 통일한다. IP 리터럴·ASCII 호스트는 원 표기가 그대로 유지된다.
    """
    try:
        raw = httpx.URL(url).raw_host
    except httpx.InvalidURL as e:
        raise SSRFBlockedError(f"URL을 해석할 수 없습니다: {e}") from e
    if not raw:
        raise SSRFBlockedError("호스트가 없는 URL입니다")
    return raw.decode("ascii")


def _validate_hop(url: str, *, allow_private: bool) -> list[str]:
    """리다이렉트 한 홉의 스킴·호스트를 검증하고 연결할 공인 IP 목록 반환."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SSRFBlockedError(f"허용되지 않는 스킴: {parsed.scheme}")
    if not parsed.hostname:
        raise SSRFBlockedError("호스트가 없는 URL입니다")
    host = _ascii_host(url)
    if allow_private:
        return []
    return _resolve_public_ips(host)


def _pin(url: str, ip: str) -> tuple[httpx.URL, str, str]:
    """검증 IP 연결 URL과 원 호스트 기반 Host/SNI 값을 만든다.

    호스트 표기는 `_ascii_host`(IDNA ASCII)를 쓴다 — `_validate_hop`이 해석·검증한 것과
    같은 표기여야 Host·SNI·검증 대상이 어긋나지 않는다.
    """
    original = httpx.URL(url)
    host = _ascii_host(url)
    host_header = f"[{host}]" if ":" in host else host
    if original.port is not None:
        host_header = f"{host_header}:{original.port}"
    return original.copy_with(host=ip), host_header, host


def _origin(url: str) -> tuple[str, str, int]:
    """출처(scheme, ASCII host, port) — 기본 포트를 채워 `https://a`와 `https://a:443`을 같게 본다.

    `_validate_hop`을 통과한 논리 URL에만 쓴다(핀 IP가 아니다 — 같은 IP의 다른 호스트는 다른 출처).
    """
    u = httpx.URL(url)
    return u.scheme, _ascii_host(url).lower(), u.port or _DEFAULT_PORTS[u.scheme]


class _RedirectCredentials:
    """홉마다 보낼 호출자 헤더 — 출처가 바뀌면 자격증명 헤더를 빼고 되살리지 않는다(#176).

    httpx 기본 리다이렉트 추적의 `Authorization` 제거를 수동 추적에서 대신한다. 한 번 빼면
    A→B→A의 세 번째 홉에도 싣지 않는다. 호출자 dict는 바꾸지 않는다.
    """

    def __init__(self, headers: dict[str, str] | None) -> None:
        self._headers = headers
        self._origin: tuple[str, str, int] | None = None
        self._stripped = False

    def for_hop(self, url: str) -> dict[str, str] | None:
        origin = _origin(url)
        if self._origin is not None and origin != self._origin and not self._stripped:
            self._stripped = True
            logger.debug("ssrf_redirect_credentials_stripped", host=origin[1])
        self._origin = origin
        if not self._stripped or not self._headers:
            return self._headers
        return {k: v for k, v in self._headers.items() if k.lower() not in _CREDENTIAL_HEADERS}


def _pinned_headers(headers: dict[str, str] | None, host_header: str) -> dict[str, str]:
    """호출자 헤더에 핀 연결용 Host를 씌운다.

    호출자가 준 `host`(대소문자 무관)는 **제거**한 뒤 `Host`를 넣는다 — 그대로 병합하면
    Host 헤더가 둘 전송되어 모호한 요청이 된다(RFC 9112). 연결 대상은 핀 IP라 SSRF
    우회는 아니지만 원 서버 동작이 달라진다.
    """
    merged = {k: v for k, v in (headers or {}).items() if k.lower() != "host"}
    merged["Host"] = host_header
    return merged


def _pin_trace(
    record: dict[str, object],
) -> Callable[[str, dict[str, object]], None]:
    """sync httpcore trace에서 실제 TCP 연결 대상과 TLS SNI를 기록."""

    def trace(event_name: str, info: dict[str, object]) -> None:
        if event_name == "connection.connect_tcp.started":
            record["connect_host"] = info["host"]
            record["connect_port"] = info["port"]
        elif event_name == "connection.start_tls.started":
            record["tls_server_hostname"] = info["server_hostname"]

    return trace


def _pin_atrace(
    record: dict[str, object],
) -> Callable[[str, dict[str, object]], Awaitable[None]]:
    """async httpcore trace에서 실제 TCP 연결 대상과 TLS SNI를 기록."""

    async def trace(event_name: str, info: dict[str, object]) -> None:
        if event_name == "connection.connect_tcp.started":
            record["connect_host"] = info["host"]
            record["connect_port"] = info["port"]
        elif event_name == "connection.start_tls.started":
            record["tls_server_hostname"] = info["server_hostname"]

    return trace


def _attempt_timeout(remaining: float, ips_left: int) -> httpx.Timeout:
    """한 홉의 남은 예산으로 이번 시도의 timeout을 만든다 — connect만 시도별로 자른다.

    read/write/pool은 남은 홉 예산 전체를 쓰지만, **connect 상한은 남은 검증 IP 수로 나눈
    몫**(최소 `_MIN_CONNECT_TIMEOUT`)이다. 첫 IP가 블랙홀(SYN 무응답)이면 connect가 홉
    예산을 전부 소진해 다음 검증 IP를 시도조차 못 하므로, D-14·PR-04의 가용성 fallback이
    `ConnectError`(빠른 실패)에서만 동작하게 된다. 시도별로 잘라야 `ConnectTimeout`에서도
    다음 IP로 넘어갈 예산이 남는다. 홉 전체 예산(`_remaining_timeout`)은 그대로 상한이다.
    """
    share = remaining / ips_left if ips_left > 0 else remaining
    connect = min(remaining, max(share, _MIN_CONNECT_TIMEOUT))
    return httpx.Timeout(remaining, connect=connect)


def _attempt_budget(
    deadline: float,
    method: str,
    url: str,
    ips_left: int,
    last_connect_error: "httpx.ConnectError | httpx.ConnectTimeout | None",
) -> httpx.Timeout:
    """이번 시도의 timeout을 만들고, 홉 예산이 소진됐으면 적절한 오류로 중단한다.

    예산 소진 예외를 IP 루프의 `except` **밖에서** 던지기 위한 분리다 — 루프 안에서 던지면
    `last_connect_error`가 "예산 소진"으로 덮여 앞선 IP의 실제 실패 원인(예:
    `ConnectError: Connection refused`)이 호출자·로그에서 사라진다(P3-15).

    - 앞선 시도의 실제 연결 오류가 있으면 **그 오류를 재발생**시키고 예산 소진 예외는
      `__cause__`로 남긴다(원인은 실제 실패, 맥락은 예산 소진).
    - 실제 오류가 없으면 기존과 동일하게 예산 소진 `ConnectTimeout`을 그대로 던진다
      (예산 의미·예외 타입 불변).
    """
    try:
        remaining = _remaining_timeout(deadline, method, url)
    except httpx.ConnectTimeout as budget_exc:
        if last_connect_error is not None:
            raise last_connect_error from budget_exc
        raise
    return _attempt_timeout(remaining, ips_left)


def _remaining_timeout(deadline: float, method: str, url: str) -> float:
    """한 홉 안의 모든 검증 IP 시도가 공유하는 남은 timeout 예산(D-14)."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise httpx.ConnectTimeout(
            "SSRF 관문 연결 시간 예산이 소진되었습니다",
            request=httpx.Request(method, url),
        )
    return remaining


def _log_pin_attempt(url: str, pinned_ip: str, record: dict[str, object]) -> None:
    """핀 연결 시도 1건을 debug로 남긴다(본문·헤더 금지).

    `host`는 검증·`Host` 헤더·SNI와 **같은 IDNA ASCII 표기**다 — phase-3 게이트가
    `tls_server_hostname`과 대조하는 값이라 표기가 갈리면 IDN 호스트에서 오탐이 난다
    (P3-14). 원 유니코드 표기는 의미가 다른 별도 키(`unicode_host`)로만 남긴다.
    """
    original = httpx.URL(url)
    try:
        host = _ascii_host(url)
    except SSRFBlockedError:  # 여기까지 온 URL은 이미 검증을 통과했다 — 방어적 폴백
        host = original.host
    fields: dict[str, object] = {
        "host": host,
        "pinned_ip": pinned_ip,
        "connect_host": record.get("connect_host"),
        "connect_port": record.get("connect_port"),
    }
    if original.host != host:
        fields["unicode_host"] = original.host
    if original.scheme == "https":
        fields["tls_server_hostname"] = record.get("tls_server_hostname")
    logger.debug("ssrf_pinned_request_attempt", **fields)


def _next_redirect(current: str, resp: httpx.Response) -> str:
    """Location 헤더를 절대 URL로 정규화(상대·스킴상대 리다이렉트 지원)."""
    location = resp.headers.get("location")
    if not location:
        raise SSRFBlockedError("리다이렉트 응답에 Location 헤더가 없습니다")
    return str(httpx.URL(current).join(location))


def _read_capped(resp: httpx.Response, max_bytes: int, decoder: CappedDecoder) -> bytes:
    """본문을 원본 전송 바이트(`iter_raw`)로 읽으며 max_bytes 초과 시 중단. 해제한 본문 반환.

    공용 `http_body.read_capped`의 얇은 래퍼다(#421). 원본·해제 누적은 `decoder`가 잰다 —
    `_body_decoder`가 같은 `max_bytes`로 만들었다.
    """
    return read_capped(resp, decoder)


async def _read_capped_async(
    resp: httpx.Response, max_bytes: int, decoder: CappedDecoder
) -> bytes:
    return await read_capped_async(resp, decoder)  # `_read_capped`


# 전송 계층 인코딩을 기술하는 헤더 — `_capped_response`가 해제된 본문으로 응답을
# 재구성할 때 제거한다(아래 docstring).
_WIRE_ENCODING_HEADERS = ("content-encoding", "content-length", "transfer-encoding")


def _capped_response(
    resp: httpx.Response, content: bytes, method: str, original_url: str
) -> httpx.Response:
    """읽은 본문으로 새 httpx.Response를 구성 — private `_content` 변경 회피(R5-1 nit).

    스트림 응답의 상태·헤더를 보존하되 본문은 상한 검사를 통과한 bytes로 확정한다.

    `content`는 관문 해제기(`http_body.CappedDecoder`)가 이미 **해제(decode)한** 본문이다. 원
    `Content-Encoding`(gzip 등)을 그대로 두면 httpx가 새 응답을 읽을 때 한 번 더
    해제를 시도해 `httpx.DecodingError`로 실패하므로, 와이어 인코딩을 기술하는
    `Content-Encoding`·`Content-Length`·`Transfer-Encoding`만 제거한다. `Content-Length`는
    httpx가 확정 본문 길이로 다시 채운다. 그 밖의 헤더(중복 헤더 포함)와 상태는 그대로
    보존한다. 응답 상한은 원본 전송 바이트와 해제 뒤 바이트 모두에 적용됐다(`_read_capped*`).
    """
    headers = httpx.Headers(resp.headers)
    for name in _WIRE_ENCODING_HEADERS:
        if name in headers:
            del headers[name]
    return httpx.Response(
        resp.status_code,
        headers=headers,
        content=content,
        request=httpx.Request(method, original_url),
    )
