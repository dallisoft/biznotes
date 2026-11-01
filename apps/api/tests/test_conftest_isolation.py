"""BL-01 워커별 격리 헬퍼 단위 테스트 (AC-18 a·b·d·e).

대부분은 `conftest.py`의 순수 헬퍼만 다룬다 — 실제 DB/Valkey 접속이 없으므로
xdist 워커 어느 쪽에서 실행되어도 같은 결과여야 한다.

예외는 `test_guard_aborts_a_real_session_before_drop_all` 한 건이다(P5-02). destructive
guard의 **배선**(호출이 `drop_all`보다 앞이라는 사실)은 순수 함수 테스트로 고정할 수
없어서, 서브프로세스로 실제 세션을 띄우고 유지보수 DB(`postgres`)에 프로브 DB의
부재를 조회한다. 프로브 DB 이름은 워커 간·실행 간 고유하므로 병렬에서도 안전하다.
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests.conftest import (
    DEFAULT_REDIS_DB_BASE,
    RSS_UNIT,
    SHARED_TEST_DB,
    TEST_DB_PREFIX,
    db_guard_error,
    derive_redis_db_index,
    derive_test_db_name,
    ensure_once_per_run,
    replace_redis_db_index,
    run_once_per_run,
    worker_env_overrides,
)

# --- (a) DB 이름·Redis 인덱스 파생 ----------------------------------------


def test_db_name_includes_run_uid_and_worker():
    assert derive_test_db_name("abcd1234", "gw3") == "biznotes_test_abcd1234_gw3"


def test_db_name_differs_between_controllers_with_same_worker_id():
    """PR-12: 워커 번호만 쓰면 두 controller가 같은 DB를 공유한다."""
    assert derive_test_db_name("aaaaaaaa", "gw0") != derive_test_db_name("bbbbbbbb", "gw0")


@pytest.mark.parametrize(
    ("worker", "expected"),
    [("gw0", 15), ("gw1", 14), ("gw3", 12), ("gw14", 1)],
)
def test_redis_db_index_counts_down_from_base(worker, expected):
    assert derive_redis_db_index(worker, DEFAULT_REDIS_DB_BASE) == expected


def test_redis_db_index_rejects_index_below_one():
    """AC-18 (d): 인덱스 ≥1 단언 — 워커 15개 이상은 Redis DB 0(앱 기본)을 침범한다."""
    with pytest.raises(ValueError, match="범위를 벗어났"):
        derive_redis_db_index("gw15", DEFAULT_REDIS_DB_BASE)


def test_redis_db_base_offset_separates_controllers():
    """HCI-P2-17: controller 간 Redis 격리는 수동 `PYTEST_REDIS_DB_BASE` offset."""
    first = {derive_redis_db_index(f"gw{n}", "15") for n in range(2)}
    second = {derive_redis_db_index(f"gw{n}", "11") for n in range(2)}
    assert first.isdisjoint(second)


def test_replace_redis_db_index_preserves_host_and_port():
    assert replace_redis_db_index("redis://localhost:26379/15", 12) == "redis://localhost:26379/12"


# --- (a) 명시 대입 vs setdefault -------------------------------------------


def test_worker_env_overrides_assigns_explicitly_under_xdist():
    env = {
        "PYTEST_XDIST_WORKER": "gw2",
        "PYTEST_XDIST_TESTRUNUID": "0123456789abcdef",
        "POSTGRES_DB": "biznotes",  # 외부에서 물려받은 위험한 값
        "VALKEY_URL": "redis://localhost:26379/15",
    }
    overrides = worker_env_overrides(env)
    assert overrides["POSTGRES_DB"] == "biznotes_test_01234567_gw2"
    assert overrides["VALKEY_URL"] == "redis://localhost:26379/13"


def test_worker_env_overrides_is_empty_without_xdist():
    """비-xdist는 기존 `setdefault` 기본값을 그대로 쓴다."""
    assert worker_env_overrides({"POSTGRES_DB": "biznotes_test"}) == {}


def test_worker_env_overrides_honours_redis_db_base():
    env = {
        "PYTEST_XDIST_WORKER": "gw0",
        "PYTEST_XDIST_TESTRUNUID": "deadbeefcafe",
        "PYTEST_REDIS_DB_BASE": "11",
    }
    assert worker_env_overrides(env)["VALKEY_URL"] == "redis://localhost:26379/11"


def test_live_session_uses_isolated_db_name_under_xdist():
    """실행 중인 세션의 설정이 실제로 파생 이름을 쓰는지 확인 (계측이 아닌 실측)."""
    from app.core.config import get_settings

    settings = get_settings()
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker:
        run_uid = os.environ.get("PYTEST_XDIST_TESTRUNUID", "")[:8]
        assert settings.postgres_db == derive_test_db_name(run_uid, worker)
    else:
        assert settings.postgres_db == SHARED_TEST_DB


# --- (b) destructive guard -------------------------------------------------


@pytest.mark.parametrize("name", ["biznotes", "postgres", "biznotes_migrate", "biznote"])
def test_guard_rejects_non_test_database(name):
    error = db_guard_error(name)
    assert error is not None
    assert TEST_DB_PREFIX in error


@pytest.mark.parametrize("name", ["biznotes_test", "biznotes_test_abcd1234_gw0"])
def test_guard_allows_test_databases(name):
    assert db_guard_error(name) is None


GUARD_PROBE_DB = "biznotes_guardprobe"


def _run_guard_probe_session() -> "subprocess.CompletedProcess[str]":
    """비-테스트 DB를 가리킨 채 실제 pytest 세션을 띄운다 (블로킹 — 스레드에서 호출)."""
    api_root = Path(__file__).resolve().parents[1]
    # 부모의 xdist 워커 변수(`PYTEST_XDIST_WORKER` 등)를 물려주면 자식이 워커별 DB
    # 이름으로 덮어써 guard가 발동하지 않는다 — `PYTEST_*`를 전부 제거한다.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["POSTGRES_DB"] = GUARD_PROBE_DB
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            # guard는 세션 픽스처라 어떤 노드를 고르든 그 전에 발동한다. 자기 자신을
            # 다시 실행하지 않도록 이 파일의 다른 테스트 1건만 지정한다.
            "tests/test_conftest_isolation.py::test_rss_unit_matches_platform",
            "-q",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
        ],
        cwd=api_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


async def _probe_db_exists_then_drop() -> bool:
    """프로브 DB의 존재 여부를 확인하고, 있으면 지운다(다음 실행 오염 방지)."""
    from tests.conftest import _connect_maintenance_db

    conn = await _connect_maintenance_db()
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", GUARD_PROBE_DB)
        if exists:
            await conn.execute(f'DROP DATABASE IF EXISTS "{GUARD_PROBE_DB}" WITH (FORCE)')
        return bool(exists)
    finally:
        await conn.close()


async def test_guard_aborts_a_real_session_before_drop_all():
    """P5-02: guard가 `_setup_db`의 `_ensure_database()`·`drop_all`보다 **앞**에 배선돼
    있음을 고정한다.

    위의 단위 테스트는 순수 함수 `db_guard_error`만 덮으므로, `conftest._setup_db`에서
    `pytest.exit(...)` 호출을 지우거나 뒤로 옮겨도 전부 통과한다. 여기서는 실제 pytest
    세션을 서브프로세스로 띄워 **두 가지**를 단언한다.

    1. 종료 코드 1 + 중단 메시지 — guard가 발동했다.
    2. **프로브 DB가 생성되지 않았다** — 이것이 "앞에 있다"의 관측 가능한 증거다.
       guard가 `_ensure_database()` 뒤로 밀리면 그 서브프로세스는 프로브 DB를 만들고
       (배선에 따라 `drop_all`/`create_all`까지 수행한 뒤) 중단하므로 DB가 남는다.
       종료 코드만 보면 이 이동을 잡지 못한다.

    `biznotes_guardprobe`는 **존재하지 않는 비-테스트 DB 이름**이다 — 개발 DB
    (`biznotes`)를 인자로 쓰지 않으므로 배선이 깨져 있더라도 파괴할 대상이 없다.
    """
    assert not await _probe_db_exists_then_drop(), (
        f"{GUARD_PROBE_DB}가 이미 존재한다 — 이전 실행의 잔재이므로 정리 후 재실행하라"
    )

    proc = await asyncio.to_thread(_run_guard_probe_session)

    output = proc.stdout + proc.stderr
    leaked = await _probe_db_exists_then_drop()

    assert proc.returncode == 1, f"returncode={proc.returncode}\n{output}"
    assert "drop_all 전에 중단합니다" in output, output
    assert GUARD_PROBE_DB in output, output
    assert not leaked, f"{GUARD_PROBE_DB}가 생성됐다 — guard가 `_ensure_database()`보다 뒤로 밀렸다"


# --- (e) run당 1회 잠금 헬퍼 ---------------------------------------------


def test_run_once_per_run_executes_action_once_across_threads(tmp_path):
    """AC-18 (e): 스레드 4개 동시 호출 → 실호출 1회."""
    lock_path = tmp_path / "ensure.lock"
    calls: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)
    ran: list[bool] = []

    def action() -> None:
        with lock:
            calls.append(1)

    def worker() -> None:
        barrier.wait()
        ran.append(run_once_per_run(lock_path, "run-key", action))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1
    assert sorted(ran) == [False, False, False, True]


def test_run_once_per_run_runs_again_for_a_new_run(tmp_path):
    """run 키가 다르면 다시 실행 — 잠금 파일이 실행 사이에 남아도 무관."""
    lock_path = tmp_path / "ensure.lock"
    calls: list[str] = []

    assert run_once_per_run(lock_path, "run-a", lambda: calls.append("a")) is True
    assert run_once_per_run(lock_path, "run-a", lambda: calls.append("a")) is False
    assert run_once_per_run(lock_path, "run-b", lambda: calls.append("b")) is True
    assert calls == ["a", "b"]


def test_run_once_per_run_creates_lock_file_content(tmp_path):
    lock_path = tmp_path / "nested" / "ensure.lock"
    run_once_per_run(lock_path, "run-xyz", lambda: None)
    assert lock_path.read_text(encoding="utf-8").strip() == "run-xyz"


def test_ensure_once_per_run_skips_the_lock_without_xdist(tmp_path):
    """P5-01: 비-xdist는 잠금 파일을 만들지 않고 매 run `action`을 실행한다.

    비-xdist에서 잠금 파일이 놓이는 `getbasetemp().parent`는 run 간 영속하는
    `pytest-of-<user>/`다. 거기에 `pid-<pid>`를 run 키로 남기면 OS가 PID를 재사용한
    이후 실행이 "이미 실행한 run"으로 오인돼 `ensure_collection()`을 조용히 영구
    skip한다. 프로세스가 하나뿐이라 직렬화할 대상이 없으므로 잠금 경로 자체를 없앤다.
    """
    lock_path = tmp_path / "ensure.lock"
    calls: list[int] = []

    assert ensure_once_per_run(lock_path, None, lambda: calls.append(1)) is True
    assert ensure_once_per_run(lock_path, "", lambda: calls.append(1)) is True

    assert calls == [1, 1], "비-xdist는 run마다 실행되어야 한다 (영구 skip 금지)"
    assert not lock_path.exists(), "비-xdist는 영속 디렉터리에 상태를 남기지 않는다"


def test_ensure_once_per_run_serialises_under_xdist(tmp_path):
    """xdist(run-uid 있음)는 기존대로 잠금 파일로 run당 1회를 보장한다."""
    lock_path = tmp_path / "ensure.lock"
    calls: list[int] = []

    assert ensure_once_per_run(lock_path, "run-uid-1", lambda: calls.append(1)) is True
    assert ensure_once_per_run(lock_path, "run-uid-1", lambda: calls.append(1)) is False
    assert ensure_once_per_run(lock_path, "run-uid-2", lambda: calls.append(1)) is True

    assert calls == [1, 1]
    assert lock_path.read_text(encoding="utf-8").strip() == "run-uid-2"


# --- RSS 훅 opt-in 성질 (AC-20 b·c) --------------------------------------


def _rss_state_snapshot():
    from tests import conftest as ct

    return (dict(ct._rss_module_delta), list(ct._rss_current_module), list(ct._rss_module_base))


def _rss_state_restore(snapshot):
    from tests import conftest as ct

    delta, current, base = snapshot
    ct._rss_module_delta.clear()
    ct._rss_module_delta.update(delta)
    ct._rss_current_module[:] = current
    ct._rss_module_base[:] = base


def test_rss_hook_is_inert_when_disabled(monkeypatch):
    """계측 장치가 계측 대상을 바꾸지 않도록, 비활성이면 상태를 만들지 않는다."""
    from tests import conftest as ct

    snapshot = _rss_state_snapshot()
    try:
        monkeypatch.setattr(ct, "RSS_REPORT_ENABLED", False)
        ct._rss_module_delta.clear()
        ct._rss_current_module[:] = [None]
        ct.pytest_runtest_logstart("tests/test_probe.py::test_x", None)
        assert ct._rss_current_module[0] is None
        assert ct._rss_module_delta == {}
    finally:
        _rss_state_restore(snapshot)


def test_rss_hook_records_module_when_enabled(monkeypatch):
    from tests import conftest as ct

    snapshot = _rss_state_snapshot()
    try:
        monkeypatch.setattr(ct, "RSS_REPORT_ENABLED", True)
        ct._rss_module_delta.clear()
        ct._rss_current_module[:] = [None]
        ct.pytest_runtest_logstart("tests/test_probe.py::test_x", None)
        assert ct._rss_current_module[0] == "tests/test_probe.py"
        assert ct._rss_module_base[0] > 0
    finally:
        _rss_state_restore(snapshot)


def test_rss_hook_writes_worker_json_without_cleaning_dir(monkeypatch, tmp_path):
    """AC-20 (c): `<worker>.json`에 worker·testrunuid·ru_maxrss·unit. 정리는 하지 않는다."""
    from tests import conftest as ct

    snapshot = _rss_state_snapshot()
    keepsake = tmp_path / "pre-existing.json"
    keepsake.write_text("{}", encoding="utf-8")
    try:
        monkeypatch.setattr(ct, "RSS_REPORT_ENABLED", True)
        monkeypatch.setenv("PYTEST_RSS_REPORT_DIR", str(tmp_path))
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw7")
        monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", "run-uid-probe")
        ct._rss_module_delta.clear()
        ct._rss_current_module[:] = [None]
        ct.pytest_sessionfinish(None, 0)

        payload = json.loads((tmp_path / "gw7.json").read_text(encoding="utf-8"))
        assert payload["worker"] == "gw7"
        assert payload["testrunuid"] == "run-uid-probe"
        assert payload["unit"] == RSS_UNIT
        assert isinstance(payload["ru_maxrss"], int) and payload["ru_maxrss"] > 0
        assert keepsake.exists(), "훅이 디렉터리를 정리하면 호출자의 fresh-dir 단언이 무의미해진다"
    finally:
        _rss_state_restore(snapshot)


def test_rss_hook_writes_nothing_without_report_dir(monkeypatch, tmp_path):
    from tests import conftest as ct

    snapshot = _rss_state_snapshot()
    try:
        monkeypatch.setattr(ct, "RSS_REPORT_ENABLED", True)
        monkeypatch.delenv("PYTEST_RSS_REPORT_DIR", raising=False)
        ct.pytest_sessionfinish(None, 0)
        assert list(Path(tmp_path).iterdir()) == []
    finally:
        _rss_state_restore(snapshot)


def test_rss_unit_matches_platform():
    """macOS는 bytes, Linux는 KiB — 단위 정규화의 근거 (AC-20 a)."""
    import sys

    assert RSS_UNIT == ("bytes" if sys.platform == "darwin" else "KiB")
