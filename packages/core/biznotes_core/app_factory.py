"""FastAPI 앱 조립 팩토리 — **아직 사용되지 않는 스캐폴딩**.

.. warning::
   현재 이 모듈은 동작하지 않는다. 아래 세 모듈이 코어에 아직 없다.
   ``biznotes_core.api.v1.router`` · ``biznotes_core.core.exception_handlers`` ·
   ``biznotes_core.core.health``

   두 제품의 ``app/main.py``가 부팅 시퀀스를 각자 복제하고 있고, 그것을 코어로
   모으려는 의도로 Phase 1b에서 만들었으나 배선하지 못했다. 정리할 때 위 모듈을
   함께 이관해야 한다. **제품 코드를 이 파일에 맞추지 말 것.**


코어가 미들웨어·보안 헤더·예외 핸들러·메트릭·헬스체크·코어 라우터를 모두 조립하고,
프로젝트는 `extra_routers`로 자기 고유 라우터만 얹는다. 프로젝트의 main.py가
코어 부팅 순서를 복제하지 않게 하는 것이 이 팩토리의 존재 이유다 — 복제하면
부팅 시퀀스가 다시 두 갈래로 갈라진다.
"""

import logging
from collections.abc import Sequence

import structlog
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from biznotes_core.config import CoreSettings
from biznotes_core.core.http_logging import install_httpx_log_redaction

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": "frame-ancestors 'none'",
}


def configure_logging(settings: CoreSettings) -> None:
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
    install_httpx_log_redaction()


def create_app(
    settings: CoreSettings,
    *,
    title: str,
    version: str = "0.1.0",
    extra_routers: Sequence[APIRouter] = (),
) -> FastAPI:
    """코어 조립 + 프로젝트 라우터 장착."""
    from biznotes_core.api.v1.router import build_core_router
    from biznotes_core.core.exception_handlers import register_exception_handlers
    from biznotes_core.core.health import build_health_router

    configure_logging(settings)

    app = FastAPI(title=title, version=version, openapi_url="/api/v1/openapi.json")
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _security_headers(request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    register_exception_handlers(app)

    api_router = build_core_router(settings)
    for r in extra_routers:
        api_router.include_router(r)
    app.include_router(api_router, prefix="/api/v1")

    app.include_router(build_health_router(settings))

    from prometheus_fastapi_instrumentator import Instrumentator

    Instrumentator(
        excluded_handlers=["/healthz", "/readyz", "/metrics"]
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

    return app
