"""샌드박스 워커 부모 쪽 실행기 — 작업공간·실행·정리·산출물 수집 (표준 계획 D7).

이 모듈은 **샌드박스 워커 안에서만** 돈다. 표준 라이브러리만 쓴다. 스크립트는 `launcher`를 거쳐
격리된 뒤에만 실행된다 — 이 모듈이 스크립트를 직접 `execve`하지 않는다.

작업공간(run마다 `mkdtemp`)::

    <root>/skill/     패키지 파일(읽기 전용)
    <root>/inputs/    입력 파일(읽기 전용)
    <root>/work/      쓰기 가능, 스크립트의 cwd·HOME
    <root>/work/tmp/  TMPDIR
    <root>/work/outputs/  산출물로 수집한다

계획은 입력을 `/work/inputs`에 두지만, Landlock 규칙은 합집합이라 쓰기 디렉터리 아래를 읽기
전용으로 되돌릴 수 없다. 그래서 `inputs/`를 `work/` 옆에 둔다.

정리(D7 "자손 정리", R4-09)는 **seccomp 필터 수**로 샌드박스 프로세스를 가린다. launcher가 건
필터는 풀 수 없고 상속되므로, 같은 uid이면서 필터 수가 이 프로세스보다 많은 프로세스는 모두
스크립트 쪽이다. `setsid`·이중 fork로 프로세스 그룹을 벗어난 자손도 이렇게 찾는다.
"""

import os
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from biznotes_core.workers.skill_sandbox import launcher

LAUNCHER_MODULE = "biznotes_core.workers.skill_sandbox.launcher"

# 스크립트가 읽고 실행할 수 있는 시스템 경로. 스파이크 실측: python·bash·node 모두 `/etc`가
# 필요하고(node는 OpenSSL 설정), node의 `process.memoryUsage()`는 `/proc/self`를 읽는다.
# 다른 프로세스의 `/proc/<pid>/environ`·`mem`은 Landlock ptrace 제한이 막는다(R3-17).
SYSTEM_READ_PATHS: tuple[str, ...] = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/proc")
DEV_RW_PATHS: tuple[str, ...] = ("/dev/null",)
DEV_RO_PATHS: tuple[str, ...] = ("/dev/urandom", "/dev/zero")

# 확장자 → 인터프리터 후보(앞에서부터 있는 것)와 앞 인자. 런타임 설치는 하지 않는다(L2).
INTERPRETERS: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    ".py": (("/usr/local/bin/python3", "/usr/bin/python3"), ("-I",)),
    ".sh": (("/bin/bash", "/usr/bin/bash"), ("--noprofile", "--norc")),
    ".js": (("/usr/local/bin/node", "/usr/bin/node"), ()),
    ".mjs": (("/usr/local/bin/node", "/usr/bin/node"), ()),
}

_SWEEP_ROUNDS = 50
_SWEEP_PAUSE_SEC = 0.02
_EXIT_GRACE_SEC = 0.5
_PR_SET_CHILD_SUBREAPER = 36


class SandboxError(RuntimeError):
    """실행 전 준비 실패. `reason`으로 분류한다."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True)
class Limits:
    wall_sec: float
    cpu_sec: int
    as_bytes: int
    fsize_bytes: int
    nofile: int = 256
    nproc: int = 64
    stream_max_bytes: int = 1024 * 1024  # stdout·stderr 각각
    outputs_max_files: int = 20
    outputs_max_bytes: int = 20 * 1024 * 1024  # 합계


@dataclass(frozen=True)
class Output:
    name: str
    data: bytes


@dataclass
class RunResult:
    exit_code: int | None
    launcher_failed: bool
    timed_out: bool
    output_overflow: bool
    stdout: bytes
    stderr: bytes
    outputs: list[Output] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    residual_clean: bool = True


@dataclass(frozen=True)
class Workspace:
    root: str
    skill: str
    inputs: str
    work: str
    outputs: str
    tmp: str


# ---------- 작업공간 ----------


def prepare_workspace(parent: str | None = None) -> Workspace:
    root = os.path.realpath(tempfile.mkdtemp(prefix="skillrun-", dir=parent))
    ws = Workspace(
        root=root,
        skill=os.path.join(root, "skill"),
        inputs=os.path.join(root, "inputs"),
        work=os.path.join(root, "work"),
        outputs=os.path.join(root, "work", "outputs"),
        tmp=os.path.join(root, "work", "tmp"),
    )
    for path in (ws.skill, ws.inputs, ws.work, ws.outputs, ws.tmp):
        os.mkdir(path, 0o700)
    return ws


def write_file(base: str, rel: str, data: bytes, *, mode: int = 0o600) -> None:
    """`base` 아래 `rel`에 새 파일을 쓴다 — 링크를 따라가지 않고 기존 파일을 덮지 않는다.

    경로 성분마다 디렉터리 fd 기준으로 `O_NOFOLLOW`로 연다(D7 "입력은 `O_EXCL|O_NOFOLLOW`").
    """
    parts = rel.split("/")
    if not rel or rel.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise SandboxError("bad_path", f"허용하지 않는 경로입니다: {rel}")
    dirfd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=dirfd)
            except FileExistsError:
                pass
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dirfd)
            os.close(dirfd)
            dirfd = nxt
        fd = os.open(
            parts[-1],
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=dirfd,
        )
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
        finally:
            os.close(fd)
    finally:
        os.close(dirfd)


def resolve_interpreter(script_rel: str) -> list[str]:
    _, ext = os.path.splitext(script_rel)
    entry = INTERPRETERS.get(ext.lower())
    if entry is None:
        raise SandboxError("bad_extension", f"실행할 수 없는 확장자입니다: {ext or '(없음)'}")
    candidates, prefix = entry
    for path in candidates:
        if os.access(path, os.X_OK):
            return [path, *prefix]
    raise SandboxError("no_interpreter", f"이 워커에 {ext} 인터프리터가 없습니다")


def launcher_argv(ws: Workspace, limits: Limits, target: Sequence[str]) -> list[str]:
    argv = [sys.executable, "-I", "-m", LAUNCHER_MODULE]
    argv += ["--as", str(limits.as_bytes), "--cpu", str(limits.cpu_sec)]
    argv += ["--fsize", str(limits.fsize_bytes), "--nofile", str(limits.nofile)]
    argv += ["--nproc", str(limits.nproc)]
    for path in (*SYSTEM_READ_PATHS, *DEV_RO_PATHS, ws.skill, ws.inputs):
        if os.path.exists(path):
            argv += ["--ro", os.path.realpath(path)]
    for path in (ws.work, *DEV_RW_PATHS):
        argv += ["--rw", os.path.realpath(path)]
    return [*argv, "--", *target]


def script_env(ws: Workspace) -> dict[str, str]:
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": ws.work,
        "LANG": "C.UTF-8",
        "TMPDIR": ws.tmp,
    }
    # 워커에 준 시간대를 넘긴다(#352) — 스크립트가 만드는 문서의 날짜·시각이 따른다
    if tz := os.environ.get("TZ"):
        env["TZ"] = tz
    return env


# ---------- 잔존 프로세스 ----------


def set_subreaper() -> bool:
    """`setsid`로 빠져나간 고아가 init이 아니라 이 프로세스에 입양되게 한다(R4-09)."""
    import ctypes

    return ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0


def _status_fields(pid: int | str, proc_root: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    try:
        with open(os.path.join(proc_root, str(pid), "status"), encoding="utf-8") as fp:
            for line in fp:
                key, _, value = line.partition(":")
                fields[key] = value.strip()
    except OSError:
        pass
    return fields


def _filters(fields: Mapping[str, str]) -> int:
    try:
        return int(fields.get("Seccomp_filters", "0"))
    except ValueError:
        return 0


def _is_sandbox_task(fields: Mapping[str, str], my_uid: list[str], my_filters: int) -> bool:
    if not fields or fields.get("State", "").startswith(("Z", "X")):
        return False
    return fields.get("Uid", "").split()[:1] == my_uid and _filters(fields) > my_filters


def sandbox_residue(proc_root: str = "/proc") -> set[int]:
    """같은 uid이면서 이 프로세스보다 seccomp 필터가 많은 살아 있는 스레드 그룹 — 스크립트 쪽이다.

    **스레드 단위로** 본다(리뷰 R4P-01). 리더 스레드만 `SYS_exit`로 끝내면 `/proc/<tgid>/status`는
    좀비(`Z`)로 보이지만 나머지 스레드는 살아 있다. 살아 있는 스레드가 하나라도 있으면 그 tgid를
    돌려준다 — tgid에 보낸 SIGKILL은 좀비 리더여도 그룹 전체를 죽인다.
    """
    me = _status_fields("self", proc_root)
    my_uid = me.get("Uid", "").split()[:1]
    my_filters = _filters(me)
    found: set[int] = set()
    try:
        names = os.listdir(proc_root)
    except OSError:
        return found
    for name in names:
        if not name.isdigit():
            continue
        try:
            tids = os.listdir(os.path.join(proc_root, name, "task"))
        except OSError:
            tids = [name]
        for tid in tids:
            fields = _status_fields(f"{name}/task/{tid}", proc_root)
            if _is_sandbox_task(fields, my_uid, my_filters):
                found.add(int(name))
                break
    return found


def _kill_if_still_sandbox(pid: int, sig: int) -> None:
    """pid를 다시 확인한 뒤 pidfd로 신호를 보낸다 — 목록 조회와 kill 사이의 pid 재사용을 막는다."""
    # Linux 전용 API라 다른 플랫폼의 타입 스텁에 없다
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or pidfd_send_signal is None:
        os.kill(pid, sig)
        return
    fd = pidfd_open(pid)
    try:
        # pidfd를 연 뒤에도 같은 프로세스가 샌드박스 쪽인지 본다. 재사용된 pid라면 여기서 빠진다
        if pid not in sandbox_residue_of(pid):
            return
        pidfd_send_signal(fd, sig)
    finally:
        os.close(fd)


def sandbox_residue_of(pid: int, proc_root: str = "/proc") -> set[int]:
    me = _status_fields("self", proc_root)
    my_uid = me.get("Uid", "").split()[:1]
    my_filters = _filters(me)
    try:
        tids = os.listdir(os.path.join(proc_root, str(pid), "task"))
    except OSError:
        return set()
    for tid in tids:
        if _is_sandbox_task(_status_fields(f"{pid}/task/{tid}", proc_root), my_uid, my_filters):
            return {pid}
    return set()


def _reap_children() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def sweep_residue(
    *,
    rounds: int = _SWEEP_ROUNDS,
    residue: Callable[[], set[int]] = sandbox_residue,
    kill: Callable[[int, int], None] = _kill_if_still_sandbox,
) -> bool:
    """샌드박스 프로세스를 모두 죽이고 입양된 좀비를 회수한다. 0개를 확인하면 True다."""
    for _ in range(rounds):
        targets = residue()
        if not targets:
            _reap_children()
            return True
        for pid in targets:
            try:
                kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(_SWEEP_PAUSE_SEC)
        _reap_children()
    return not residue()


# ---------- 실행 ----------


def _drain(
    proc: "subprocess.Popen[bytes]", limits: Limits, deadline: float
) -> tuple[bytes, bytes, bool, bool]:
    """stdout·stderr를 상한까지 읽는다. (stdout, stderr, 시간 초과, 출력 초과)."""
    assert proc.stdout is not None and proc.stderr is not None
    bufs = {proc.stdout.fileno(): bytearray(), proc.stderr.fileno(): bytearray()}
    sel = selectors.DefaultSelector()
    for stream in (proc.stdout, proc.stderr):
        os.set_blocking(stream.fileno(), False)
        sel.register(stream.fileno(), selectors.EVENT_READ)
    timed_out = overflow = False
    grace_until: float | None = None
    try:
        while sel.get_map():
            now = time.monotonic()
            if grace_until is None and proc.poll() is not None:
                # 본 프로세스가 끝났다 — 파이프를 쥔 자손은 곧 정리하므로 오래 기다리지 않는다
                grace_until = now + _EXIT_GRACE_SEC
            if grace_until is not None and now >= grace_until:
                break
            remaining = deadline - now
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in sel.select(timeout=min(remaining, 0.1)):
                fd = int(key.fd)
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    sel.unregister(fd)
                    continue
                buf = bufs[fd]
                room = limits.stream_max_bytes - len(buf)
                buf += chunk[: max(room, 0)]
                if len(chunk) > room:
                    overflow = True
            if overflow:
                break
    finally:
        sel.close()
    return (
        bytes(bufs[proc.stdout.fileno()]),
        bytes(bufs[proc.stderr.fileno()]),
        timed_out,
        overflow,
    )


def run_script(
    ws: Workspace,
    script_rel: str,
    args: Sequence[str],
    limits: Limits,
) -> RunResult:
    """격리된 스크립트 하나를 실행하고, 자손을 모두 정리한 뒤 산출물을 수집한다.

    잔존 프로세스를 0으로 확인하지 못하면 산출물을 수집하지 않는다(`residual_clean=False`).
    """
    interpreter = resolve_interpreter(script_rel)
    script_path = os.path.join(ws.skill, script_rel)
    argv = launcher_argv(ws, limits, [*interpreter, script_path, *args])
    started = time.monotonic()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=ws.work,
        env=script_env(ws),
        close_fds=True,
        start_new_session=True,
    )
    try:
        stdout, stderr, timed_out, overflow = _drain(proc, limits, started + limits.wall_sec)
        if timed_out or overflow:
            _kill_group(proc.pid)
        try:
            exit_code: int | None = proc.wait(
                timeout=max(0.1, started + limits.wall_sec - time.monotonic())
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc.pid)
            exit_code = proc.wait()
    finally:
        _kill_group(proc.pid)
        clean = sweep_residue()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    result = RunResult(
        exit_code=exit_code,
        launcher_failed=exit_code == launcher.EXIT_LAUNCHER_FAILED,
        timed_out=timed_out,
        output_overflow=overflow,
        stdout=stdout,
        stderr=stderr,
        residual_clean=clean,
    )
    if clean:
        result.outputs, result.skipped = collect_outputs(ws, limits)
    return result


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# ---------- 산출물 수집 (D7 "산출물 수집", R3-06) ----------


def safe_output_name(name: str) -> bool:
    """산출물 이름 규칙 — 에이전트 쪽 등록 규칙과 같다(리뷰 R4P-07).

    출력 가능한 문자만(서로게이트로 들어온 비 UTF-8 바이트·U+202E 같은 서식 문자 거부),
    숨김·경로 구분자 없음.
    """
    return (
        0 < len(name) <= 128
        and not name.startswith(".")
        and "/" not in name
        and "\\" not in name
        and name.isprintable()
    )


def remove_workspace(ws: Workspace) -> bool:
    """작업공간을 지운다. 스크립트가 `chmod 0`으로 막아 두었을 수 있다 — 권한을 되돌리고 지운다.

    Landlock은 chmod를 다루지 않는다(리뷰 R4P-02). 호출 전에 스크립트 프로세스는 모두 죽어 있다.
    지우지 못하면 False — 호출자가 워커를 dirty로 표시한다.
    """
    import shutil

    def _restore(path: str) -> None:
        try:
            if not os.path.islink(path):
                os.chmod(path, 0o700)
        except OSError:
            pass

    if os.path.lexists(ws.root):
        _restore(ws.root)
        # 위에서 아래로 — 부모 디렉터리를 먼저 열어야 자식이 보인다
        for dirpath, dirnames, _files in os.walk(ws.root):
            for d in dirnames:
                _restore(os.path.join(dirpath, d))
        shutil.rmtree(ws.root, ignore_errors=True)
    return not os.path.lexists(ws.root)


def collect_outputs(ws: Workspace, limits: Limits) -> tuple[list[Output], list[tuple[str, str]]]:
    """`work/outputs/`의 일반 파일만 모은다. 링크·FIFO·하드링크·하위 디렉터리는 건너뛴다.

    - `outputs`는 작업 디렉터리 fd 기준 `O_DIRECTORY|O_NOFOLLOW`로 연다(자체가 링크면 거부).
    - 파일은 `O_NOFOLLOW|O_NONBLOCK`으로 열고 `fstat`으로 `S_ISREG`와 `st_nlink == 1`을 본다.
    """
    outputs: list[Output] = []
    skipped: list[tuple[str, str]] = []
    try:
        workfd = os.open(ws.work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return outputs, [("", "work_unavailable")]
    try:
        try:
            outfd = os.open("outputs", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=workfd)
        except OSError:
            return outputs, [("outputs", "not_a_directory")]
        try:
            names = sorted(os.listdir(outfd))
            total = 0
            for name in names:
                if not safe_output_name(name):
                    skipped.append((name, "bad_name"))
                    continue
                if len(outputs) >= limits.outputs_max_files:
                    skipped.append((name, "too_many_files"))
                    continue
                data, reason = _read_regular(outfd, name, limits.outputs_max_bytes - total)
                if data is None:
                    skipped.append((name, reason))
                    continue
                total += len(data)
                outputs.append(Output(name=name, data=data))
        finally:
            os.close(outfd)
    finally:
        os.close(workfd)
    return outputs, skipped


def _read_regular(dirfd: int, name: str, room: int) -> tuple[bytes | None, str]:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dirfd)
    except OSError:
        return None, "not_regular"  # 링크(ELOOP) 등
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "not_regular"
        if st.st_nlink != 1:
            return None, "hardlink"
        if st.st_size > room:
            return None, "too_large"
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            size += len(chunk)
            if size > room:
                return None, "too_large"
            chunks.append(chunk)
        return b"".join(chunks), ""
    finally:
        os.close(fd)
