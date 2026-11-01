"""워커 non-dumpable(보안 발견 F2) — 같은 UID 자식이 워커 environ의 비밀을 읽지 못한다.

prctl만 가짜 libc로 대신하는 단위 케이스는 전 플랫폼, 실제 커널 판정은 Linux 전용이다.
"""

import errno
import sys
from typing import Any

import pytest
import structlog
from celery import signals

from biznotes_core.core import dumpable
from biznotes_core.workers import celery_app as celery_app_module

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="실제 prctl 판정은 Linux에서만 동작한다"
)


class FakeLibc:
    def __init__(self, rc: int = 0):
        self.rc = rc
        self.calls: list[tuple[int, ...]] = []

    def prctl(self, *args: int) -> int:
        self.calls.append(args)
        return self.rc


@pytest.fixture
def fake_libc(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(rc: int = 0, *, linux: bool = True) -> FakeLibc:
        libc = FakeLibc(rc)
        monkeypatch.setattr(dumpable, "_libc", lambda: libc)
        monkeypatch.setattr(dumpable, "supported", lambda: linux)
        return libc

    return install


# ---------------------------------------------------------------------------
# core.dumpable
# ---------------------------------------------------------------------------


def test_set_non_dumpable_skips_other_platforms(fake_libc) -> None:
    libc = fake_libc(linux=False)
    assert dumpable.set_non_dumpable() is False
    assert libc.calls == []


def test_set_non_dumpable_calls_prctl(fake_libc) -> None:
    libc = fake_libc()
    assert dumpable.set_non_dumpable() is True
    assert libc.calls == [(4, 0, 0, 0, 0)]  # PR_SET_DUMPABLE, SUID_DUMP_DISABLE


def test_set_non_dumpable_raises_on_failure(fake_libc, monkeypatch) -> None:
    fake_libc(rc=-1)
    monkeypatch.setattr(dumpable.ctypes, "get_errno", lambda: errno.EINVAL)
    with pytest.raises(OSError) as exc:
        dumpable.set_non_dumpable()
    assert exc.value.errno == errno.EINVAL


@pytest.mark.parametrize(
    "rc,expected",
    [(0, False), (1, True), (2, True), (-1, True)],
    ids=["disabled", "user", "suid-root", "unknown"],
)
def test_is_dumpable_maps_prctl_result(fake_libc, rc, expected) -> None:
    libc = fake_libc(rc)
    assert dumpable.is_dumpable() is expected
    assert libc.calls == [(3, 0, 0, 0, 0)]  # PR_GET_DUMPABLE


class _NoPrctl:
    """prctl 심볼이 없는 libc(예: macOS)."""


def _libc_unavailable() -> Any:
    raise OSError(errno.ENOENT, "libc")


@pytest.mark.parametrize("libc", [_NoPrctl, _libc_unavailable], ids=["no-symbol", "no-libc"])
def test_is_dumpable_fails_closed_when_undeterminable(monkeypatch, libc) -> None:
    monkeypatch.setattr(dumpable, "_libc", libc)
    assert dumpable.is_dumpable() is True


def test_set_non_dumpable_reports_missing_symbol_as_oserror(monkeypatch) -> None:
    monkeypatch.setattr(dumpable, "supported", lambda: True)
    monkeypatch.setattr(dumpable, "_libc", _NoPrctl)
    with pytest.raises(OSError):
        dumpable.set_non_dumpable()


# ---------------------------------------------------------------------------
# 워커 시그널 배선
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "signal", [signals.worker_init, signals.worker_process_init], ids=lambda s: s.name
)
def test_worker_signals_set_non_dumpable(signal, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        celery_app_module.dumpable, "set_non_dumpable", lambda: calls.append("set") or True
    )
    signal.send(sender=None)
    assert calls == ["set"]


def test_signal_failure_is_logged_and_worker_keeps_starting(monkeypatch) -> None:
    def fail() -> bool:
        raise OSError(errno.EPERM, "denied")

    monkeypatch.setattr(celery_app_module.dumpable, "set_non_dumpable", fail)
    with structlog.testing.capture_logs() as logs:
        signals.worker_process_init.send(sender=None)  # 예외가 새지 않는다
    assert [(e["log_level"], e["errno"]) for e in logs] == [("error", errno.EPERM)]


# ---------------------------------------------------------------------------
# 실제 커널 (Linux)
# ---------------------------------------------------------------------------


@linux_only
def test_real_prctl_roundtrip(non_dumpable) -> None:
    assert dumpable.is_dumpable() is False


@linux_only
def test_process_is_dumpable_by_default() -> None:
    assert dumpable.is_dumpable() is True
