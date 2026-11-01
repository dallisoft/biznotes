"""Redis 기반 단순 레이트 리밋 (고정 윈도우).

scope:
- "ip": 비로그인 엔드포인트(로그인 등) — ASGI 서버가 정한 클라이언트 주소(`request.client`)
- "user": 인증 엔드포인트 — JWT sub 기준 (프록시 뒤 IP 공유 환경 대응),
  토큰 없거나 무효하면 IP로 폴백 (401은 인증 의존성이 처리)

프록시 헤더(`X-Forwarded-For`)는 코어가 읽지 않는다(#290). 신뢰 프록시 경계는 ASGI 서버가 정한다 —
uvicorn은 직전 피어가 `--forwarded-allow-ips`(`FORWARDED_ALLOW_IPS`)에 있을 때만 그 헤더로
`request.client`를 고친다. 헤더 첫 값을 그대로 믿으면 클라이언트가 값을 바꿔 가며 제한을 피한다.

IPv6 주소는 /64로 묶어 버킷을 정한다(#315). 클라이언트는 보통 /64 하나를 통째로 받으므로, 주소마다
버킷을 나누면 그 안에서 주소를 바꿔 IP당 제한을 피한다. IPv4와 IPv4-mapped IPv6는 주소 그대로다.

관리자 재정의(#386): `limit_setting` 한도는 재정의 공급자(`set_override_provider`)가 준 값이 있으면
그 값을, 없으면 설정(env) 값을 쓴다. 공급자 결과는 프로세스 안에
`RATE_LIMIT_OVERRIDE_TTL_SECONDS`(30초) 동안 캐시한다 — 요청마다 DB를 읽지 않는다. 그래서 다른
프로세스의 변경은 최대 그만큼 늦게 반영된다. 이 모듈은 `config`만 import한다(ARCHITECTURE.md §2) —
DB에서 값을 읽는 공급자는 서비스 계층(`services.rate_limit_settings`)이 등록한다.
"""

import ipaddress
import time
from collections.abc import Awaitable, Callable, Mapping

import structlog
from fastapi import HTTPException, Request, status
from redis.asyncio import Redis

from biznotes_core.config import get_settings

logger = structlog.get_logger()

# 프록시 헤더가 적용되지 않은 배포를 알리는 경고는 프로세스에서 한 번만 남긴다
_proxy_headers_warned = False

# IPv6 클라이언트를 묶는 접두 길이(#315) — 보통 가구·단말 하나가 /64 하나를 쓴다
_IPV6_BUCKET_PREFIX = 64
# 잘 알려진 NAT64 접두(RFC 6052). 하위 32비트가 IPv4 클라이언트라 IPv4처럼 주소마다 버킷을 둔다 —
# /64로 묶으면 SIIT-DC 등에서 모든 IPv4 클라이언트가 버킷 하나를 쓴다
_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")

# 관리자 재정의 캐시 주기(초, #386) — 다른 프로세스에서 바꾼 값은 최대 이만큼 늦게 반영된다
RATE_LIMIT_OVERRIDE_TTL_SECONDS = 30.0

#: 재정의 공급자 — `limit_setting` 이름 → 분당 한도. 없는 키는 설정(env) 값을 쓴다
RateLimitOverrideProvider = Callable[[], Awaitable[Mapping[str, int]]]

_override_provider: RateLimitOverrideProvider | None = None
_override_cache: dict[str, int] = {}
_override_expires_at = 0.0  # time.monotonic 기준. 0이면 다음 요청이 다시 읽는다


def set_override_provider(provider: RateLimitOverrideProvider | None) -> None:
    """`limit_setting` 한도의 재정의 공급자를 등록한다(None이면 해제). 캐시도 비운다."""
    global _override_provider
    _override_provider = provider
    invalidate_overrides()


def invalidate_overrides() -> None:
    """캐시를 버린다 — 이 프로세스의 다음 요청이 공급자를 다시 부른다(변경 직후 호출)."""
    global _override_cache, _override_expires_at
    _override_cache = {}
    _override_expires_at = 0.0


async def _current_overrides() -> Mapping[str, int]:
    """캐시된 재정의 값. 공급자 실패는 요청을 막지 않는다 — 직전 값(처음이면 env)을 쓴다."""
    global _override_cache, _override_expires_at
    if _override_provider is None:
        return {}
    now = time.monotonic()
    if now < _override_expires_at:
        return _override_cache
    # 읽는 동안 들어온 요청은 직전 값을 쓰게 주기를 먼저 연장한다(동시 요청이 함께 읽지 않게)
    _override_expires_at = now + RATE_LIMIT_OVERRIDE_TTL_SECONDS
    try:
        loaded = await _override_provider()
    except Exception as e:  # noqa: BLE001 — DB 장애가 모든 요청을 막지 않게
        logger.warning("rate_limit_override_load_failed", error=type(e).__name__)
        return _override_cache
    _override_cache = dict(loaded)
    return _override_cache


def _forwarded_hosts(request: Request) -> list[str]:
    """`X-Forwarded-For` 모든 줄의 호스트 — uvicorn과 같은 규칙(괄호 IPv6, 콜론 하나면 host:port).

    빈 항목은 뺀다(uvicorn도 빈 값으로는 client를 바꾸지 않는다).
    """
    hosts: list[str] = []
    for line in request.headers.getlist("x-forwarded-for"):
        for raw in line.split(","):
            entry = raw.strip()
            if not entry:
                continue
            if entry.startswith("["):
                end = entry.find("]")
                hosts.append(entry[1:end] if end > 0 else entry)
            elif entry.count(":") == 1:
                hosts.append(entry.split(":", 1)[0])
            else:
                hosts.append(entry)
    return hosts


def _address_kind(host: str | None) -> str:
    """로그용 주소 분류 — 사용자 IP일 수 있어 주소 자체는 남기지 않는다."""
    if host is None:
        return "none"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "not_ip"
    if address.is_loopback:
        return "loopback"
    return "private" if address.is_private else "global"


def _warn_if_proxy_headers_ignored(request: Request, host: str | None) -> None:
    """`X-Forwarded-For`가 있는데 ASGI 서버가 적용하지 않았으면 한 번 경고한다.

    uvicorn이 적용하면 `request.client`는 헤더 항목 중 하나가 된다. 항목 어디에도 없으면(또는
    client가 없으면 — 유닉스 소켓 배포) 프록시 헤더가 꺼져 있거나 신뢰 목록 밖이다. 그러면 모든
    사용자가 프록시 주소 하나의 IP 버킷을 함께 쓴다. 부분 신뢰(CDN → 프록시에서 프록시만 신뢰)는
    client가 헤더 항목이라 탐지하지 못한다.
    """
    global _proxy_headers_warned
    if _proxy_headers_warned:
        return
    hosts = _forwarded_hosts(request)
    if not hosts or host in hosts:
        return
    _proxy_headers_warned = True
    logger.warning(
        "rate_limit_proxy_headers_ignored",
        peer_kind=_address_kind(host),
        hint=(
            "X-Forwarded-For가 있지만 ASGI 서버가 적용하지 않았다 — 프록시 뒤라면 uvicorn "
            "FORWARDED_ALLOW_IPS(--forwarded-allow-ips)에 프록시 주소를 넣는다. 아니면 IP 속도 "
            "제한이 프록시 주소 하나로 모인다"
        ),
    )


def _client_ip(request: Request) -> str:
    host = request.client.host if request.client else None
    _warn_if_proxy_headers_ignored(request, host)
    return host or "unknown"


def _ip_bucket(host: str) -> str:
    """IP 범위 버킷 이름 — IPv6는 /64 네트워크, IPv4-mapped·NAT64(64:ff9b::/96)는 IPv4.

    그 밖의 값(IP가 아닌 값 등)은 그대로다.
    """
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])  # 존 ID(`fe80::1%eth0`)는 뗀다
    except ValueError:
        return host
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        if address in _NAT64_WELL_KNOWN:
            return str(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
        network = ipaddress.IPv6Network((address, _IPV6_BUCKET_PREFIX), strict=False)
        return network.compressed
    return str(address)


def _client_key(request: Request, scope: str) -> str:
    if scope == "user":
        token = request.cookies.get("access_token")
        if not token:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth.removeprefix("Bearer ")
        if token:
            from biznotes_core.core.security import decode_token

            try:
                sub = decode_token(token, "access").get("sub")
                if sub:
                    return f"user:{sub}"
            except Exception:
                pass  # 무효 토큰은 IP 기준 — 401은 인증 의존성이 처리
    return f"ip:{_ip_bucket(_client_ip(request))}"


class RateLimiter:
    """엔드포인트별 의존성: RateLimiter("login", limit=10, window_seconds=60).

    limit_setting 지정 시 관리자 재정의(#386, 캐시 주기 안에 반영)가 있으면 그 값을, 없으면
    settings의 해당 필드를 매 요청 시 읽는다(환경변수로 한도 조정, 0 이하 = 해당 엔드포인트 리밋
    비활성 — 비활성은 env에서만 정할 수 있다).
    """

    def __init__(
        self,
        key: str,
        limit: int = 60,
        window_seconds: int = 60,
        scope: str = "ip",
        limit_setting: str | None = None,
    ):
        self.key = key
        self.limit = limit
        self.window = window_seconds
        self.scope = scope
        self.limit_setting = limit_setting

    def _effective_limit(self) -> int:
        if self.limit_setting:
            return int(getattr(get_settings(), self.limit_setting, self.limit))
        return self.limit

    async def _resolve_limit(self) -> int:
        if self.limit_setting:
            override = (await _current_overrides()).get(self.limit_setting)
            if override is not None:
                return override
        return self._effective_limit()

    async def __call__(self, request: Request) -> None:
        settings = get_settings()
        if settings.app_env == "test" or not settings.rate_limit_enabled:
            return
        limit = await self._resolve_limit()
        if limit <= 0:
            return
        redis = Redis.from_url(settings.redis_url)
        try:
            bucket = f"ratelimit:{self.key}:{_client_key(request, self.scope)}"
            count = await redis.incr(bucket)
            if count == 1:
                await redis.expire(bucket, self.window)
            if count > limit:
                ttl = await redis.ttl(bucket)
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    "요청이 너무 많습니다. 잠시 후 다시 시도하세요.",
                    headers={"Retry-After": str(max(ttl, 1))},
                )
        finally:
            await redis.aclose()
