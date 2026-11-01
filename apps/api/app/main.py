import logging

import structlog
from biznotes_core.api import internal_storage
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis
from sqlalchemy import text

from app.api.v1.meta import APP_VERSION
from app.api.v1.router import api_router
from app.core.config import assert_prod_secrets, find_insecure_defaults, get_settings
from app.core.openapi_sse import add_sse_event_schemas
from app.schemas.skill_install import SkillInstallOfferEvent

settings = get_settings()
assert_prod_secrets(settings)  # prod + 기본 시크릿 → 기동 거부

# 구조화 로깅: prod=JSON, 그 외=콘솔
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if settings.app_env == "prod"
        else structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
)
logger = structlog.get_logger()

app = FastAPI(
    title="biznotes API",
    version=APP_VERSION,
    openapi_url="/api/v1/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",  # API 응답은 프레임 임베드 불필요
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # 웹 CSP enforce 전환(§3c)과 일관성 — API 응답(JSON/docs)은 프레임 임베드 차단만 필요.
    # frame-ancestors는 X-Frame-Options의 현대적 등가물이며 Swagger UI 동작에 영향 없음.
    "Content-Security-Policy": "frame-ancestors 'none'",
}


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


# dev/staging에서 기본 시크릿 사용 중이면 경고 (prod는 assert_prod_secrets가 기동 거부)
if settings.app_env not in ("prod", "test"):
    _insecure = find_insecure_defaults(settings)
    if _insecure:
        logger.warning("기본 시크릿 사용 중 — prod 전 교체 필요", fields=_insecure)

from app.core.exception_handlers import register_exception_handlers  # noqa: E402

register_exception_handlers(app)  # 도메인 예외 → HTTP 매핑 단일화 (R1-1)

app.include_router(api_router, prefix="/api/v1")
# 로컬 스토리지 서명 URL(`/internal/storage/<토큰>`) — 스킬 샌드박스 워커가 파일을 받고 올린다.
# 접두사 없이 붙이고 OpenAPI에 싣지 않는다. s3 백엔드면 404다
app.include_router(internal_storage.router)
# 채팅 SSE 이벤트 페이로드(설치 제안 `skill_install_offer`, #259)를 OpenAPI 컴포넌트로 내보낸다
add_sse_event_schemas(app, [SkillInstallOfferEvent])

# Prometheus 메트릭 (/metrics) — API 레이턴시/요청수
from prometheus_fastapi_instrumentator import Instrumentator  # noqa: E402

Instrumentator(excluded_handlers=["/healthz", "/readyz", "/metrics"]).instrument(app).expose(
    app, endpoint="/metrics", include_in_schema=False
)


@app.get("/healthz", tags=["health"])
async def healthz() -> dict[str, str]:
    """Liveness — 프로세스 생존 여부만 확인."""
    return {"status": "ok"}


@app.get("/readyz", tags=["health"])
async def readyz() -> dict[str, str]:
    """Readiness — DB/Redis 연결 확인 (Compose healthcheck / K8s probe 공용)."""
    from biznotes_core.core.db import engine

    checks: dict[str, str] = {}
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    checks["postgres"] = "ok"

    redis = Redis.from_url(settings.redis_url)
    try:
        await redis.ping()
        checks["redis"] = "ok"
    finally:
        await redis.aclose()

    return {"status": "ok", **checks}
