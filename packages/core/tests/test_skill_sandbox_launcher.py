"""스킬 샌드박스 launcher — 단계 순서·fail-closed(수락 기준 34)와 seccomp 필터 판정.

(a) 시스템 호출 어댑터만 가짜인 in-process `run()` — launcher 본문은 실경로다.
(b) seccomp BPF는 작은 해석기로 돌린다. arm64 개발 환경에서는 x86_64 커널에 필터를 걸 수 없으므로
    x32·비네이티브 아키텍처 판정은 이 해석기가 정본 검증이다(aarch64 필터는 실커널 시험도 있다).
"""

import ast
import pathlib
import struct
from collections.abc import Mapping, Sequence

import pytest

from biznotes_core.workers.skill_sandbox import launcher
from biznotes_core.workers.skill_sandbox.launcher import (
    AUDIT_ARCH_AARCH64,
    AUDIT_ARCH_X86_64,
    EXIT_LAUNCHER_FAILED,
    REQUIRED_LANDLOCK_ABI,
    run,
    socket_filter,
)

CORE = pathlib.Path(__file__).resolve().parent.parent / "biznotes_core"
LIMITS = [
    "--as",
    "1073741824",
    "--cpu",
    "10",
    "--fsize",
    "1048576",
    "--nofile",
    "64",
    "--nproc",
    "32",
]
ARGV = [*LIMITS, "--ro", "/usr", "--rw", "/tmp/w", "--", "/usr/bin/python3", "-I", "/s/x.py"]


class FakeSyscalls:
    def __init__(self, *, fail: str | None = None, abi: int = 8) -> None:
        self.fail = fail
        self.abi = abi
        self.calls: list[tuple] = []

    def _step(self, name: str, *args: object) -> None:
        self.calls.append((name, *args))
        if self.fail == name:
            raise OSError(1, "fake")

    def set_pdeathsig(self) -> None:
        self._step("set_pdeathsig")

    def setrlimit(self, resource_id: int, limits: tuple[int, int]) -> None:
        self._step("setrlimit", resource_id, limits)

    def no_new_privs(self) -> None:
        self._step("no_new_privs")

    def landlock_abi(self) -> int:
        self._step("landlock_abi")
        return self.abi

    def landlock_restrict(self, ro: Sequence[str], rw: Sequence[str]) -> None:
        self._step("landlock_restrict", list(ro), list(rw))

    def seccomp_filter(self) -> None:
        self._step("seccomp_filter")

    def close_fds(self) -> None:
        self._step("close_fds")

    def execve(self, path: str, argv: Sequence[str], env: Mapping[str, str]) -> None:
        self._step("execve", path, list(argv), dict(env))

    def names(self) -> list[str]:
        out: list[str] = []
        for call in self.calls:
            if not out or out[-1] != call[0]:
                out.append(call[0])
        return out


ORDER = [
    "set_pdeathsig",
    "setrlimit",
    "no_new_privs",
    "landlock_abi",
    "landlock_restrict",
    "seccomp_filter",
    "close_fds",
    "execve",
]


def test_stage_order_is_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_TOKEN", "test-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    for key in ("HOME", "LANG", "TMPDIR", "TZ"):
        monkeypatch.delenv(key, raising=False)
    fake = FakeSyscalls()
    assert run(ARGV, syscalls=fake) == EXIT_LAUNCHER_FAILED  # 가짜 execve는 반환한다
    assert fake.names() == ORDER
    execve = fake.calls[-1]
    assert execve[1] == "/usr/bin/python3"
    assert execve[2] == ["/usr/bin/python3", "-I", "/s/x.py"]
    assert execve[3] == {"PATH": "/usr/bin"}  # 허용 키 밖의 환경은 넘기지 않는다


def test_tz_reaches_the_script(monkeypatch: pytest.MonkeyPatch) -> None:
    """워커의 `TZ`는 스크립트에 넘긴다(#352). 비밀이 아니다."""
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("TZ", "Asia/Seoul")
    monkeypatch.setenv("SECRET_TOKEN", "test-secret")
    for key in ("HOME", "LANG", "TMPDIR"):
        monkeypatch.delenv(key, raising=False)
    fake = FakeSyscalls()
    run(ARGV, syscalls=fake)
    assert fake.calls[-1][3] == {"PATH": "/usr/bin", "TZ": "Asia/Seoul"}


@pytest.mark.parametrize("stage", ORDER[:-1])
def test_any_stage_failure_skips_the_rest_and_never_execs(stage: str) -> None:
    fake = FakeSyscalls(fail=stage)
    assert run(ARGV, syscalls=fake) == EXIT_LAUNCHER_FAILED
    names = fake.names()
    assert names[-1] == stage
    assert "execve" not in names


@pytest.mark.parametrize("abi", [0, 1, 4, 5])
def test_landlock_below_required_abi_never_execs(abi: int) -> None:
    fake = FakeSyscalls(abi=abi)
    assert run(ARGV, syscalls=fake) == EXIT_LAUNCHER_FAILED
    assert fake.names()[-1] == "landlock_abi"
    assert "landlock_restrict" not in fake.names() and "execve" not in fake.names()


def test_required_abi_is_a_code_constant() -> None:
    assert REQUIRED_LANDLOCK_ABI == 6
    assert "--abi" not in (CORE / "workers" / "skill_sandbox" / "launcher.py").read_text()


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--", "/bin/sh"],
        [*LIMITS, "--rw", "/tmp/w", "--"],  # 대상 없음
        [*LIMITS, "--", "/bin/sh"],  # 쓰기 디렉터리 없음
        [*LIMITS, "--rw", "tmp/w", "--", "/bin/sh"],  # 상대 경로
        [*LIMITS, "--rw", "/tmp/w", "--", "sh"],  # 상대 대상
        [*LIMITS[:-2], "--rw", "/tmp/w", "--", "/bin/sh"],  # 한도 누락
        [*LIMITS, "--nproc", "3", "--rw", "/tmp/w", "--", "/bin/sh"],  # 중복
        ["--as", "-1", *LIMITS[2:], "--rw", "/tmp/w", "--", "/bin/sh"],
        [*LIMITS, "--netns", "--rw", "/tmp/w", "--", "/bin/sh"],  # 모르는 옵션
    ],
)
def test_bad_arguments_never_touch_the_kernel(argv: list[str]) -> None:
    fake = FakeSyscalls()
    assert run(argv, syscalls=fake) == EXIT_LAUNCHER_FAILED
    assert fake.calls == []


def test_launcher_imports_only_stdlib() -> None:
    tree = ast.parse((CORE / "workers" / "skill_sandbox" / "launcher.py").read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    import sys

    assert roots <= set(sys.stdlib_module_names)


# ---------- seccomp BPF 해석기 ----------

_RET_KILL = 0x80000000
_ERRNO = 0x00050000
_ALLOW = 0x7FFF0000


def bpf_eval(
    prog: Sequence[tuple[int, int, int, int]], arch: int, nr: int, args: Sequence[int] = ()
) -> int:
    data = struct.pack("=iIQ6Q", nr, arch, 0, *[*args, 0, 0, 0, 0, 0, 0][:6])
    acc = 0
    pc = 0
    while True:
        code, jt, jf, k = prog[pc]
        if code == 0x20:  # LD W ABS
            acc = struct.unpack_from("=I", data, k)[0]
            pc += 1
        elif code == 0x54:  # ALU AND K
            acc &= k
            pc += 1
        elif code == 0x15:  # JEQ K
            pc += 1 + (jt if acc == k else jf)
        elif code == 0x35:  # JGE K
            pc += 1 + (jt if acc >= k else jf)
        elif code == 0x06:  # RET K
            return k
        else:
            raise AssertionError(f"모르는 opcode {code:#x}")


X86 = launcher._SOCKET_SYSCALLS["x86_64"]
ARM = launcher._SOCKET_SYSCALLS["aarch64"]
SOCK_CLOEXEC = 0o2000000
SOCK_NONBLOCK = 0o4000


@pytest.mark.parametrize(
    ("arch", "audit", "nums"),
    [
        ("x86_64", AUDIT_ARCH_X86_64, X86),
        ("aarch64", AUDIT_ARCH_AARCH64, ARM),
    ],
)
def test_filter_denies_socket_creation_and_endpoints(
    arch: str, audit: int, nums: dict[str, int]
) -> None:
    prog = socket_filter(arch)
    for name in ("socket", "bind", "listen", "connect", "accept", "accept4"):
        assert bpf_eval(prog, audit, nums[name], [2, 1, 0]) == _ERRNO | 13, name
    for nr in (425, 426, 427):
        assert bpf_eval(prog, audit, nr) == _ERRNO | 38
    # 일반 syscall(read=0/63)은 통과
    assert bpf_eval(prog, audit, 0 if arch == "x86_64" else 63) == _ALLOW


@pytest.mark.parametrize(
    ("arch", "audit", "nums"),
    [
        ("x86_64", AUDIT_ARCH_X86_64, X86),
        ("aarch64", AUDIT_ARCH_AARCH64, ARM),
    ],
)
def test_filter_allows_only_unix_stream_socketpair(
    arch: str, audit: int, nums: dict[str, int]
) -> None:
    prog = socket_filter(arch)
    sp = nums["socketpair"]
    assert bpf_eval(prog, audit, sp, [1, 1]) == _ALLOW
    assert bpf_eval(prog, audit, sp, [1, 1 | SOCK_CLOEXEC]) == _ALLOW
    assert bpf_eval(prog, audit, sp, [1, 1 | SOCK_CLOEXEC | SOCK_NONBLOCK]) == _ALLOW
    assert bpf_eval(prog, audit, sp, [1, 2]) == _ERRNO | 13  # SOCK_DGRAM
    assert bpf_eval(prog, audit, sp, [1, 5]) == _ERRNO | 13  # SOCK_SEQPACKET
    assert bpf_eval(prog, audit, sp, [2, 1]) == _ERRNO | 13  # AF_INET
    assert bpf_eval(prog, audit, sp, [1, 1 | 0x100000]) == _ERRNO | 13  # 모르는 type 비트


def test_filter_kills_foreign_arch_and_x32() -> None:
    x86 = socket_filter("x86_64")
    assert bpf_eval(x86, 0x40000003, 1) == _RET_KILL  # AUDIT_ARCH_I386 (int 0x80)
    assert bpf_eval(x86, AUDIT_ARCH_X86_64, 0x40000000 | 41) == _RET_KILL  # x32 socket
    assert bpf_eval(x86, AUDIT_ARCH_X86_64, 0x40000000) == _RET_KILL  # x32 read
    arm = socket_filter("aarch64")
    assert bpf_eval(arm, 0x40000028, 281) == _RET_KILL  # AUDIT_ARCH_ARM (compat)
    assert bpf_eval(arm, AUDIT_ARCH_X86_64, 0) == _RET_KILL


def test_filter_jumps_stay_in_range() -> None:
    for arch in ("x86_64", "aarch64"):
        prog = socket_filter(arch)
        for pc, (code, jt, jf, _k) in enumerate(prog):
            if code in (0x15, 0x35):
                assert pc + 1 + jt < len(prog) and pc + 1 + jf < len(prog)
        assert prog[-1][0] == 0x06
