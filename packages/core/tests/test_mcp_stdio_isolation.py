"""stdio MCP 자식 프로세스 격리 (#139 A, docs/plans/mcp-exposure/ 단계 A 1항).

실제 stdio 자식(탐침 서버)을 띄워, 워커 환경변수(가짜 비밀)를 물려받지 않고 관리자가 등록한
`env_encrypted`만 받으며, 워커 cwd가 아닌 빈 임시 디렉터리에서 시작하는지 본다.
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from biznotes_core.core.crypto import encrypt
from biznotes_core.services.agents import mcp_client

PROBE = Path(__file__).parent / "fixtures" / "mcp_isolation" / "probe_server.py"
WORKER_SECRETS = {
    "DATABASE_URL": "postgresql://worker:test-password@db/prod",
    "ENCRYPTION_KEY": "test-secret-encryption-key",
    "OPENAI_API_KEY": "sk-test-not-real",
}


def _server(env: dict[str, str] | None) -> SimpleNamespace:
    return SimpleNamespace(
        transport="stdio",
        endpoint_or_command=f"{sys.executable} {PROBE}",
        env_encrypted=encrypt(json.dumps(env)) if env is not None else None,
    )


@pytest.fixture
def worker_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """워커에 비밀 환경변수와 `.env` 파일이 있는 cwd를 꾸민다."""
    for key, value in WORKER_SECRETS.items():
        monkeypatch.setenv(key, value)
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


async def _probe(server: SimpleNamespace) -> dict:
    return json.loads(await mcp_client.call_tool(server, "probe", {}))


async def test_child_does_not_inherit_worker_secrets(worker_env: Path) -> None:
    seen = await _probe(_server({"PROBE_API_TOKEN": "registered-token"}))

    assert not set(WORKER_SECRETS) & set(seen["env_keys"])
    assert seen["api_token"] == "registered-token"  # 관리자가 등록한 env는 간다
    assert "PATH" in seen["env_keys"]  # SDK 안전 기본 집합


def _check_workdir(child_cwd: str, worker_cwd: Path) -> None:
    assert Path(child_cwd).resolve() != worker_cwd.resolve()
    assert not Path(child_cwd).exists()  # 세션이 끝나면 지운다
    assert Path(os.getcwd()).resolve() == worker_cwd.resolve()  # 워커 cwd는 그대로


async def test_child_starts_in_an_empty_temporary_directory(worker_env: Path) -> None:
    seen = await _probe(_server(None))

    assert seen["cwd_entries"] == []  # 워커 cwd의 `.env`를 상대 경로로 읽을 수 없다
    _check_workdir(seen["cwd"], worker_env)


def test_server_env_returns_only_the_registered_values(worker_env: Path) -> None:
    assert mcp_client._server_env(_server(None)) is None  # type: ignore[arg-type]
    registered = mcp_client._server_env(_server({"A": "1"}))  # type: ignore[arg-type]
    assert registered == {"A": "1"}
