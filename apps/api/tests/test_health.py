import httpx
import pytest

from app.main import app


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_healthz(client: httpx.AsyncClient):
    res = await client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


async def test_security_headers_present(client: httpx.AsyncClient):
    res = await client.get("/healthz")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


async def test_ping(client: httpx.AsyncClient):
    res = await client.get("/api/v1/ping")
    assert res.status_code == 200
    body = res.json()
    assert body["message"] == "pong"


async def test_openapi_schema(client: httpx.AsyncClient):
    res = await client.get("/api/v1/openapi.json")
    assert res.status_code == 200
    schema = res.json()
    assert schema["info"]["title"] == "biznotes API"
    assert "/healthz" in schema["paths"]
