"""앱 버전 조회 `GET /meta/versions` — 사이드바 브랜드 행 아래 표기."""

import tomllib
from pathlib import Path

import httpx

from tests.conftest import bearer, login

PATH = "/api/v1/meta/versions"
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


async def test_versions_report_installed_app_version(client: httpx.AsyncClient, user_a):
    """앱 버전은 pyproject `[project].version`이고, 코어 버전은 노출하지 않는다."""
    token = await login(client, user_a.email)

    res = await client.get(PATH, headers=bearer(token))

    assert res.status_code == 200, res.text
    declared = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    assert res.json() == {"app_version": declared}


async def test_openapi_info_version_follows_app_version(client: httpx.AsyncClient):
    """OpenAPI `info.version`도 같은 앱 버전을 쓴다(따로 적힌 값이 낡지 않게)."""
    res = await client.get("/api/v1/openapi.json")

    declared = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    assert res.json()["info"]["version"] == declared


async def test_versions_require_login(client: httpx.AsyncClient):
    """버전은 로그인 사용자에게만 보인다 — 익명 요청에 버전(지문)을 내주지 않는다."""
    res = await client.get(PATH)

    assert res.status_code == 401, res.text
