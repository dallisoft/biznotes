"""api 진입점 `scripts/start.sh` — 마이그레이션을 먼저 적용하고 받은 명령으로 바뀐다(E9).

`alembic`을 PATH의 가짜 실행 파일로 바꿔 호출 여부만 본다. DB에 닿지 않는다.
"""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "start.sh"


def _run(tmp_path: Path, flag: str | None, *, alembic_exit: int = 0) -> tuple[int, list[str]]:
    calls = tmp_path / "calls.log"
    fake = tmp_path / "alembic"
    fake.write_text(f'#!/bin/sh\necho "alembic $*" >> "{calls}"\nexit {alembic_exit}\n')
    fake.chmod(0o755)
    env = {"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    if flag is not None:
        env["BIZNOTES_AUTO_MIGRATE"] = flag
    proc = subprocess.run(
        ["sh", str(SCRIPT), "sh", "-c", f'echo "app" >> "{calls}"'],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = calls.read_text().splitlines() if calls.exists() else []
    return proc.returncode, lines


@pytest.mark.parametrize("flag", [None, "true", "1", "yes"])
def test_migrates_before_starting_the_app(tmp_path: Path, flag: str | None) -> None:
    assert _run(tmp_path, flag) == (0, ["alembic upgrade head", "app"])


@pytest.mark.parametrize("flag", ["false", "0", "no"])
def test_skips_migration_when_disabled(tmp_path: Path, flag: str) -> None:
    assert _run(tmp_path, flag) == (0, ["app"])


def test_failed_migration_does_not_start_the_app(tmp_path: Path) -> None:
    code, lines = _run(tmp_path, "true", alembic_exit=3)
    assert code == 3
    assert lines == ["alembic upgrade head"]


def test_unknown_flag_value_is_rejected(tmp_path: Path) -> None:
    code, lines = _run(tmp_path, "maybe")
    assert code == 64
    assert lines == []
