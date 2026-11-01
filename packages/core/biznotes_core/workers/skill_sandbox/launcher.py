"""스킬 스크립트 실행 직전 격리를 거는 launcher — `python -I -m` 으로만 실행한다(표준 계획 D7).

    python -I -m biznotes_core.workers.skill_sandbox.launcher \\
        --as <bytes> --cpu <sec> --fsize <bytes> --nofile <n> --nproc <n> \\
        [--ro <path>]... [--rw <path>]... -- <인터프리터 절대경로> <인자...>

단계 순서는 고정이다(D7 "적용 순서", R3-07):
`PR_SET_PDEATHSIG` → `setrlimit` → `PR_SET_NO_NEW_PRIVS` → Landlock → seccomp → fd 정리 → `execve`.
어느 단계든 실패하면 **뒤 단계를 실행하지 않고** exit 125다. `execve`는 0회다(수락 기준 34).

- **Landlock**: 요구 ABI는 코드 상수 `REQUIRED_LANDLOCK_ABI`(6)다. 설정으로 낮출 수 없다(R2-17).
  파일 규칙은 `--ro`(읽기·실행)와 `--rw`(쓰기, 소켓·장치 파일 생성 제외)로만 연다.
  TCP bind·connect는 handled로 두고 규칙을 주지 않아 모두 막는다.
  신호·abstract unix socket은 scope로 막는다.
  부모를 향한 ptrace·`process_vm_readv`·`pidfd_getfd`와 다른 프로세스의 `/proc/<pid>/environ`은
  Landlock의 ptrace 제한(도메인 밖 프로세스)이 막는다.
- **seccomp**: 런타임 기본 프로필 **위에** 소켓 계열만 좁히는 필터를 더한다(`socket_filter`).
  전체 허용 목록이 아니다.
- **fd 정리**: `close_range(3, ~0U)` — 워커 부모에게서 물려받은 소켓(브로커 등)을 끊는다.
- **환경**: `ENV_KEYS`만 남긴다. 부모도 이미 비워서 넘기지만 여기서 한 번 더 거른다.

표준 라이브러리만 import하고 import 부작용이 없다. 시스템 호출 어댑터는 in-process 테스트가
`run()`을 직접 부를 때만 바꿀 수 있다 — argv로 어댑터를 고르는 옵션은 두지 않는다.
"""

import os
import platform
import signal
import struct
import sys
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:  # 런타임에는 필요할 때만 import한다(모듈 import 부작용 없음)
    import ctypes

EXIT_LAUNCHER_FAILED = 125

# 스크립트에 넘기는 환경 변수(D7 "환경"). `TZ`는 워커의 시간대다(#352, 비밀 아님)
ENV_KEYS = ("PATH", "HOME", "LANG", "TMPDIR", "TZ")

# Landlock 요구 ABI — 설정이 아니라 코드 상수다(R2-17).
# 6은 신호·abstract unix socket scope가 있는 첫 ABI다
REQUIRED_LANDLOCK_ABI = 6

_INT_OPTIONS = {
    "--as": "as",
    "--cpu": "cpu",
    "--fsize": "fsize",
    "--nofile": "nofile",
    "--nproc": "nproc",
}

_PR_SET_PDEATHSIG = 1
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2

# Landlock — x86_64·aarch64 공통 syscall 번호
_SYS_CREATE_RULESET, _SYS_ADD_RULE, _SYS_RESTRICT_SELF = 444, 445, 446
_SYS_CLOSE_RANGE = 436
_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1

# 파일 접근 비트(ABI 1~5)
_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_MAKE_CHAR = 1 << 6
_FS_MAKE_SOCK = 1 << 9
_FS_MAKE_BLOCK = 1 << 11
_FS_TRUNCATE = 1 << 14
_FS_ALL = (1 << 16) - 1  # ABI 5까지의 전 비트(REFER·TRUNCATE·IOCTL_DEV 포함)
_FS_READ_EXEC = _FS_EXECUTE | _FS_READ_FILE | _FS_READ_DIR
# 쓰기 디렉터리: 소켓 파일(R4-08)·장치 파일은 만들 수 없다
_FS_WRITE_DIR = _FS_ALL & ~(_FS_MAKE_SOCK | _FS_MAKE_CHAR | _FS_MAKE_BLOCK)
# 디렉터리가 아닌 규칙 대상에 의미가 있는 비트
_FS_FILE_BITS = _FS_EXECUTE | _FS_WRITE_FILE | _FS_READ_FILE | _FS_TRUNCATE | (1 << 15)
# 네트워크 bind·connect TCP / scope: abstract unix socket·신호
_NET_ALL = 0b11
_SCOPE_ALL = 0b11
# `O_PATH`는 Linux 전용이라 다른 플랫폼의 타입 스텁에 없다
_O_PATH = getattr(os, "O_PATH", 0o10000000)


# ---------- seccomp 소켓 필터 ----------

AUDIT_ARCH_X86_64 = 0xC000003E
AUDIT_ARCH_AARCH64 = 0xC00000B7
_X32_SYSCALL_BIT = 0x40000000

# 아키텍처별 syscall 번호
_SOCKET_SYSCALLS: dict[str, dict[str, int]] = {
    "x86_64": {
        "socket": 41,
        "connect": 42,
        "accept": 43,
        "bind": 49,
        "listen": 50,
        "socketpair": 53,
        "accept4": 288,
    },
    "aarch64": {
        "socket": 198,
        "socketpair": 199,
        "bind": 200,
        "listen": 201,
        "accept": 202,
        "connect": 203,
        "accept4": 242,
    },
}
_IO_URING_SYSCALLS = (425, 426, 427)  # setup·enter·register — 두 아키텍처 공통
_AUDIT_ARCH = {"x86_64": AUDIT_ARCH_X86_64, "aarch64": AUDIT_ARCH_AARCH64}

_AF_UNIX = 1
_SOCK_STREAM = 1
_SOCK_NONBLOCK = 0o4000
_SOCK_CLOEXEC = 0o2000000
_EACCES = 13
_ENOSYS = 38

_SECCOMP_RET_KILL_PROCESS = 0x80000000
_SECCOMP_RET_ERRNO = 0x00050000
_SECCOMP_RET_ALLOW = 0x7FFF0000

# BPF opcode
_LD_W_ABS = 0x20
_JEQ_K = 0x15
_JGE_K = 0x35
_AND_K = 0x54
_RET_K = 0x06

# seccomp_data 오프셋: nr(0), arch(4), args[i]의 하위 32비트(16 + 8i, 리틀 엔디언)
_OFF_NR, _OFF_ARCH = 0, 4


def _arg_lo(i: int) -> int:
    return 16 + 8 * i


def native_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    raise OSError(f"지원하지 않는 아키텍처: {machine}")


def socket_filter(arch: str) -> list[tuple[int, int, int, int]]:
    """소켓 계열만 좁히는 seccomp BPF 프로그램 — (code, jt, jf, k) 목록(D7 "소켓·네트워크").

    - 아키텍처 불일치·x32 비트 → KILL_PROCESS
    - `socket`·`bind`·`listen`·`connect`·`accept`·`accept4` → EACCES
    - `socketpair`는 `AF_UNIX` + `SOCK_STREAM`(CLOEXEC·NONBLOCK 비트 무시)만 허용, 나머지 EACCES
    - `io_uring_*` → ENOSYS(대체 경로로 가도록 errno)
    - 그 밖은 허용(런타임 기본 프로필이 따로 걸려 있다)
    """
    nums = _SOCKET_SYSCALLS[arch]
    denied = [nums[n] for n in ("socket", "bind", "listen", "connect", "accept", "accept4")]
    # 라벨 기반으로 적고 마지막에 상대 점프로 푼다
    prog: list[tuple[object, object, object, int]] = [
        (_LD_W_ABS, 0, 0, _OFF_ARCH),
        (_JEQ_K, None, "kill", _AUDIT_ARCH[arch]),
        (_LD_W_ABS, 0, 0, _OFF_NR),
    ]
    if arch == "x86_64":
        # x32 ABI는 AUDIT_ARCH_X86_64와 같은 arch 값이라 번호의 비트로 가른다
        prog.append((_JGE_K, "kill", None, _X32_SYSCALL_BIT))
    prog += [(_JEQ_K, "eacces", None, nr) for nr in denied]
    prog += [(_JEQ_K, "enosys", None, nr) for nr in _IO_URING_SYSCALLS]
    prog += [
        (_JEQ_K, "socketpair", None, nums["socketpair"]),
        (_RET_K, 0, 0, _SECCOMP_RET_ALLOW),
        ("socketpair", 0, 0, 0),
        (_LD_W_ABS, 0, 0, _arg_lo(0)),
        (_JEQ_K, None, "eacces", _AF_UNIX),
        (_LD_W_ABS, 0, 0, _arg_lo(1)),
        (_AND_K, 0, 0, 0xFFFFFFFF & ~(_SOCK_CLOEXEC | _SOCK_NONBLOCK)),
        (_JEQ_K, "allow", "eacces", _SOCK_STREAM),
        ("eacces", 0, 0, 0),
        (_RET_K, 0, 0, _SECCOMP_RET_ERRNO | _EACCES),
        ("enosys", 0, 0, 0),
        (_RET_K, 0, 0, _SECCOMP_RET_ERRNO | _ENOSYS),
        ("kill", 0, 0, 0),
        (_RET_K, 0, 0, _SECCOMP_RET_KILL_PROCESS),
        ("allow", 0, 0, 0),
        (_RET_K, 0, 0, _SECCOMP_RET_ALLOW),
    ]
    labels: dict[str, int] = {}
    body: list[tuple[object, object, object, int]] = []
    for ins in prog:
        if isinstance(ins[0], str):
            labels[ins[0]] = len(body)
        else:
            body.append(ins)
    out: list[tuple[int, int, int, int]] = []
    for pc, (code, jt, jf, k) in enumerate(body):

        def rel(target: object, pc: int = pc) -> int:
            if target is None:
                return 0
            if isinstance(target, int):
                return target
            offset = labels[str(target)] - pc - 1
            if not 0 <= offset <= 255:
                raise ValueError("BPF 점프 범위 초과")
            return offset

        assert isinstance(code, int)
        out.append((code, rel(jt), rel(jf), k))
    return out


def pack_filter(prog: Sequence[tuple[int, int, int, int]]) -> bytes:
    return b"".join(struct.pack("=HBBI", *ins) for ins in prog)


# ---------- 시스템 호출 어댑터 ----------


class Syscalls(Protocol):
    def set_pdeathsig(self) -> None: ...

    def setrlimit(self, resource_id: int, limits: tuple[int, int]) -> None: ...

    def no_new_privs(self) -> None: ...

    def landlock_abi(self) -> int: ...

    def landlock_restrict(self, ro: Sequence[str], rw: Sequence[str]) -> None: ...

    def seccomp_filter(self) -> None: ...

    def close_fds(self) -> None: ...

    def execve(self, path: str, argv: Sequence[str], env: Mapping[str, str]) -> None: ...


def _libc() -> "ctypes.CDLL":
    import ctypes

    # 링크된 libc 심볼을 쓴다 — find_library는 ldconfig 서브프로세스를 띄운다
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def _errno() -> OSError:
    import ctypes

    err = ctypes.get_errno()
    return OSError(err, os.strerror(err))


class DefaultSyscalls:
    def set_pdeathsig(self) -> None:
        # 워커 부모가 죽으면 이 프로세스도 죽는다(execve 뒤에도 유지된다)
        if _libc().prctl(_PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0) != 0:
            raise _errno()

    def setrlimit(self, resource_id: int, limits: tuple[int, int]) -> None:
        import resource

        resource.setrlimit(resource_id, limits)

    def no_new_privs(self) -> None:
        if _libc().prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise _errno()

    def landlock_abi(self) -> int:
        """커널이 주는 Landlock ABI. 0은 "없음"이다(ENOSYS·EOPNOTSUPP·seccomp의 EPERM)."""
        import ctypes

        rc = _libc().syscall(
            _SYS_CREATE_RULESET,
            None,
            ctypes.c_size_t(0),
            ctypes.c_uint32(_CREATE_RULESET_VERSION),
        )
        return int(rc) if rc >= 0 else 0

    def landlock_restrict(self, ro: Sequence[str], rw: Sequence[str]) -> None:
        """ABI 6 기준 도메인 — 파일·TCP·scope를 모두 handled로 두고 인자 경로만 연다."""
        import ctypes

        class _RulesetAttr(ctypes.Structure):
            _fields_ = [
                ("handled_access_fs", ctypes.c_uint64),
                ("handled_access_net", ctypes.c_uint64),
                ("scoped", ctypes.c_uint64),
            ]

        class _PathBeneathAttr(ctypes.Structure):
            _pack_ = 1
            _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

        libc = _libc()
        attr = _RulesetAttr(_FS_ALL, _NET_ALL, _SCOPE_ALL)
        fd = libc.syscall(
            _SYS_CREATE_RULESET,
            ctypes.byref(attr),
            ctypes.c_size_t(ctypes.sizeof(attr)),
            ctypes.c_uint32(0),
        )
        if fd < 0:
            raise _errno()
        try:
            for path, access in [
                *((p, _FS_READ_EXEC) for p in ro),
                *((p, _FS_WRITE_DIR) for p in rw),
            ]:
                # O_PATH로 열어 경로 안의 심볼릭 링크를 따라간다(usrmerge의 /lib → usr/lib 포함)
                pfd = os.open(path, _O_PATH | os.O_CLOEXEC)
                try:
                    allowed = access if os.path.isdir(path) else access & _FS_FILE_BITS
                    rule = _PathBeneathAttr(allowed, pfd)
                    rc = libc.syscall(
                        _SYS_ADD_RULE,
                        ctypes.c_int(fd),
                        ctypes.c_int(_RULE_PATH_BENEATH),
                        ctypes.byref(rule),
                        ctypes.c_uint32(0),
                    )
                finally:
                    os.close(pfd)
                if rc != 0:
                    raise _errno()
            if libc.syscall(_SYS_RESTRICT_SELF, ctypes.c_int(fd), ctypes.c_uint32(0)) != 0:
                raise _errno()
        finally:
            os.close(fd)

    def seccomp_filter(self) -> None:
        import ctypes

        raw = pack_filter(socket_filter(native_arch()))
        buf = ctypes.create_string_buffer(raw, len(raw))

        class _SockFprog(ctypes.Structure):
            _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

        prog = _SockFprog(len(raw) // 8, ctypes.addressof(buf))
        if _libc().prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(prog), 0, 0) != 0:
            raise _errno()

    def close_fds(self) -> None:
        import ctypes

        rc = _libc().syscall(
            _SYS_CLOSE_RANGE, ctypes.c_uint(3), ctypes.c_uint(0xFFFFFFFF), ctypes.c_uint(0)
        )
        if rc != 0:
            raise _errno()

    def execve(self, path: str, argv: Sequence[str], env: Mapping[str, str]) -> None:
        os.execve(path, list(argv), dict(env))


class _Args(NamedTuple):
    limits: dict[str, int]
    ro: list[str]
    rw: list[str]
    target: str
    target_args: list[str]


def _parse(argv: Sequence[str]) -> _Args | None:
    limits: dict[str, int] = {}
    ro: list[str] = []
    rw: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            rest = list(argv[i + 1 :])
            if not rest or len(limits) != len(_INT_OPTIONS) or not rw:
                return None
            return _Args(limits, ro, rw, rest[0], rest[1:])
        if arg in ("--ro", "--rw"):
            if i + 1 >= len(argv):
                return None
            path = argv[i + 1]
            # 경로는 부모가 realpath로 계산해 넘긴다 — 상대경로는 우리 버그다
            if not os.path.isabs(path):
                return None
            (ro if arg == "--ro" else rw).append(path)
            i += 2
            continue
        key = _INT_OPTIONS.get(arg)
        if key is None or key in limits or i + 1 >= len(argv):
            return None
        try:
            value = int(argv[i + 1])
        except ValueError:
            return None
        if value < 0:
            return None
        limits[key] = value
        i += 2
    return None


def run(argv: Sequence[str], *, syscalls: Syscalls) -> int:
    """인자를 검사하고 격리를 건 뒤 exec한다. exec에 성공하면 반환하지 않는다.

    Landlock ABI가 `REQUIRED_LANDLOCK_ABI` 미만이면 `execve` 없이 exit 125다(fail-closed).
    """
    parsed = _parse(argv)
    if parsed is None or not os.path.isabs(parsed.target):
        return EXIT_LAUNCHER_FAILED
    try:
        import resource

        syscalls.set_pdeathsig()
        for res, value in (
            (resource.RLIMIT_AS, parsed.limits["as"]),
            (resource.RLIMIT_CPU, parsed.limits["cpu"]),
            (resource.RLIMIT_FSIZE, parsed.limits["fsize"]),
            (resource.RLIMIT_NOFILE, parsed.limits["nofile"]),
            (resource.RLIMIT_NPROC, parsed.limits["nproc"]),
            (resource.RLIMIT_CORE, 0),
        ):
            syscalls.setrlimit(res, (value, value))
        syscalls.no_new_privs()
        if syscalls.landlock_abi() < REQUIRED_LANDLOCK_ABI:
            return EXIT_LAUNCHER_FAILED
        syscalls.landlock_restrict(parsed.ro, parsed.rw)
        syscalls.seccomp_filter()
        syscalls.close_fds()
        env = {k: os.environ[k] for k in ENV_KEYS if k in os.environ}
        syscalls.execve(parsed.target, [parsed.target, *parsed.target_args], env)
    except Exception:  # noqa: BLE001 — 어떤 실패도 스크립트 종료 코드와 섞지 않는다
        return EXIT_LAUNCHER_FAILED
    # 실제 execve는 반환하지 않는다 — 가짜 어댑터에서만 여기 온다
    return EXIT_LAUNCHER_FAILED


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:], syscalls=DefaultSyscalls()))
