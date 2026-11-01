"""비밀 필드 라우터의 요청 검증 422가 입력값을 되돌려주지 않는다 (#173).

제품처럼 코어 라우터를 `/api/v1`·`/admin` 라우터에 중첩 include한 앱으로 확인한다. 인증·DB·
속도 제한 의존성만 대역이고, 요청 검증과 422 응답은 FastAPI 실경로다.
"""

import uuid
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from biznotes_core.api import deps
from biznotes_core.api.routing import RedactedValidationRoute
from biznotes_core.api.v1 import auth, skills
from biznotes_core.api.v1.admin import llm, users
from biznotes_core.core.db import get_db
from biznotes_core.core.rate_limit import RateLimiter
from biznotes_core.models import User

SECRET = "pw-secret-7f3a"  # 응답 어디에도 나오면 안 되는 표식
LONG = SECRET + "x" * 130  # 스키마 상한(128) 초과
# signed URL처럼 query에 비밀이 든 4096자 초과 URL(#402 리뷰)
LONG_URL = "https://example.test/repo.zip?" + "p=1&" * 1100 + f"sig={SECRET}"


def _app() -> FastAPI:
    admin = APIRouter(prefix="/admin")
    admin.include_router(users.router)
    admin.include_router(llm.router)
    api = APIRouter(prefix="/api/v1")
    api.include_router(auth.router)
    api.include_router(skills.router)
    api.include_router(admin)
    app = FastAPI()
    app.include_router(api)

    me = User(id=uuid.uuid4(), email="admin@test.dev", role="admin", is_active=True)
    app.dependency_overrides[deps.get_current_user] = lambda: me
    app.dependency_overrides[deps.get_current_user_allow_password_change] = lambda: me
    app.dependency_overrides[get_db] = lambda: None
    # 0.141부터 include한 라우트를 펼치지 않는다(`_IncludedRouter`) — 원래 라우터에서 찾는다
    for router in (auth.router, users.router, llm.router, skills.router):
        for route in router.routes:
            for dep in getattr(route, "dependencies", []):
                if isinstance(dep.dependency, RateLimiter):
                    app.dependency_overrides[dep.dependency] = lambda: None
    return app


async def _post(path: str, **kwargs: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        method = kwargs.pop("method", "POST")
        return await client.request(method, f"/api/v1{path}", **kwargs)


CASES = [
    # 상한 초과 — 필드 입력값
    ("/auth/password", {"json": {"current_password": "pw-ok-123", "new_password": LONG}}),
    ("/auth/password", {"json": {"current_password": LONG, "new_password": "pw-ok-1234"}}),
    # 잘못된 타입 — 숫자
    ("/auth/password", {"json": {"current_password": 70313, "new_password": SECRET}}),
    # 필드 누락 — FastAPI는 본문 전체를 input에 싣는다
    ("/auth/password", {"json": {"current_password": SECRET}}),
    # 객체가 아닌 본문 — 원본이 그대로 실린다
    ("/auth/password", {"json": [SECRET]}),
    (
        "/auth/password",
        {
            "content": f'{{"current_password": "{SECRET}"',
            "headers": {"content-type": "application/json"},
        },
    ),
    ("/auth/login", {"json": {"password": SECRET}}),
    ("/auth/login", {"json": {"email": "a@test.dev", "password": LONG}}),
    ("/admin/users", {"json": {"email": "a@test.dev", "name": "n", "password": LONG}}),
    ("/admin/users", {"json": {"name": "n", "password": SECRET}}),
    ("/admin/users", {"json": {"email": "a@test.dev", "name": "n", "password": 70313}}),
    ("/admin/llm/providers", {"json": {"type": "openai", "api_key": SECRET}}),
    # 리뷰 보강(T1) — 문자열·form·text 본문, 중첩 값, path·body 동시 오류
    ("/auth/password", {"json": SECRET}),
    ("/auth/password", {"content": SECRET, "headers": {"content-type": "text/plain"}}),
    ("/auth/login", {"data": {"email": "a@test.dev", "password": SECRET}}),
    ("/auth/password", {"json": {"current_password": {"x": SECRET}, "new_password": [SECRET]}}),
    (
        "/admin/llm/providers/not-a-uuid",
        {"method": "PATCH", "json": {"api_key": [SECRET]}},
    ),
    # 스킬 원격 가져오기(#402 리뷰) — URL query의 서명·토큰
    ("/skills/import-url/candidates", {"json": {"url": LONG_URL}}),
    ("/skills/import-url/candidates", {"json": [SECRET]}),
    ("/skills/import-url", {"json": {"url": LONG_URL}}),
    ("/skills/import-url", {"json": {"url": "https://example.test/a.zip", "sha256": LONG}}),
    ("/skills/import-url", {"json": {"sha256": SECRET}}),
    # 출처 키·대상 스킬(#402 §4) — 필드 누락이면 본문 전체(`source_ref` 포함)가 input에 실린다
    ("/skills/import-url", {"json": {"source_ref": f"github:{SECRET}"}}),
    ("/skills/import-url", {"json": {"url": "https://example.test/a.zip", "source_ref": [SECRET]}}),
    ("/skills/import-zip", {"data": {"source_ref": f"github:{SECRET}", "target_skill_id": "x"}}),
]


@pytest.mark.parametrize(("path", "request_kwargs"), CASES)
async def test_validation_422_carries_no_input(path: str, request_kwargs: dict[str, Any]) -> None:
    res = await _post(path, **request_kwargs)

    assert res.status_code == 422
    assert SECRET not in res.text and "70313" not in res.text
    errors = res.json()["detail"]
    assert errors  # 무엇이 틀렸는지(loc·msg·type)는 남는다
    for error in errors:
        assert set(error) >= {"loc", "msg", "type"}
        assert "input" not in error and "ctx" not in error


def test_secret_routers_use_redacting_route_class() -> None:
    # 위 응답 검증은 중첩 include 뒤에도 이 클래스의 핸들러가 쓰인다는 데 기댄다
    for router in (auth.router, users.router, llm.router, skills.router):
        routes = [r for r in router.routes if isinstance(r, APIRoute)]
        assert routes and all(isinstance(r, RedactedValidationRoute) for r in routes)


async def test_other_routers_keep_default_422() -> None:
    """가리기는 비밀 필드 라우터에만 건다 — 일반 라우터의 422 형식은 그대로다."""
    app = FastAPI()
    plain = APIRouter()

    @plain.post("/echo")
    async def echo(body: dict[str, int]) -> dict[str, int]:
        return body

    app.include_router(plain)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        res = await client.post("/echo", json={"a": "not-int"})
    assert res.status_code == 422
    assert res.json()["detail"][0]["input"] == "not-int"


async def test_redacted_exception_does_not_chain_original() -> None:
    """앱 예외 핸들러·로깅이 받는 예외에도 입력값·본문이 없다(리뷰 m1)."""
    from fastapi.exception_handlers import request_validation_exception_handler
    from fastapi.exceptions import RequestValidationError

    app = _app()
    caught: list[RequestValidationError] = []

    @app.exception_handler(RequestValidationError)
    async def capture(request: Any, exc: RequestValidationError) -> Any:
        caught.append(exc)
        return await request_validation_exception_handler(request, exc)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        res = await client.post("/api/v1/auth/password", json={"current_password": SECRET})

    assert res.status_code == 422 and SECRET not in res.text
    (exc,) = caught
    assert exc.body is None and exc.__cause__ is None and exc.__context__ is None
    assert SECRET not in str(exc) and SECRET not in repr(exc.errors())


async def test_unwrapped_core_router_keeps_input() -> None:
    """비대상 코어 라우터(admin/rag)는 그대로다 — 전역 동작을 바꾸지 않았다(리뷰 T4)."""
    from biznotes_core.api.v1.admin import rag

    app = FastAPI()
    app.include_router(rag.router)
    app.dependency_overrides[deps.get_current_user] = lambda: None
    app.dependency_overrides[get_db] = lambda: None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        res = await client.post("/rag/reindex", json={"notebook_id": "not-a-uuid"})
    assert res.status_code == 422
    assert res.json()["detail"][0]["input"] == "not-a-uuid"
