"""스킬 스크립트 샌드박스 음성 시험 — 실제 launcher·runner로 격리를 건다(표준 계획 수락 기준 33).

Linux + Landlock ABI >= 6에서만 돈다(macOS·구 커널은 skip). 각 시험은 **대조군**과 짝이다:
같은 탐침을 격리 없이 돌리면 성공해야, 격리에서의 실패가 격리 덕분이라고 말할 수 있다.

    docker build -f docker/test.Dockerfile -t biznotes-core-test:py312-node .
    docker run --rm --security-opt no-new-privileges:true -v "$PWD:/src:ro" \\
        biznotes-core-test:py312-node \\
        /run.sh -m pytest tests/test_skill_sandbox_isolation.py -q -p no:cacheprovider

이미지 레시피·`/run.sh`(docker/test/linux-run.sh)·커널 요건은 README "Linux 시험 이미지" 절.
"""

import json
import os
import pathlib
import socket
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import LINUX

from biznotes_core.workers.skill_sandbox import launcher, runner


def _abi() -> int:
    if not LINUX:
        return 0
    return launcher.DefaultSyscalls().landlock_abi()


pytestmark = pytest.mark.skipif(
    _abi() < launcher.REQUIRED_LANDLOCK_ABI,
    reason="Linux + Landlock ABI >= 6에서만 격리를 실측할 수 있다",
)

# 실행기와 같은 후보를 쓴다. GitHub Actions의 Python은 /usr/local 아래에 없을 수 있지만,
# runner가 고른 경로는 Landlock의 SYSTEM_READ_PATHS 아래에 있다.
PY = runner.resolve_interpreter("probe.py")[0]
SECRET = "test-secret-sandbox-parent"


def _uid_tasks() -> int:
    """이 uid가 지금 가진 태스크(스레드) 수.

    RLIMIT_NPROC는 컨테이너·세션과 무관하게 실제 uid 전체의 태스크를 센다. CI 러너처럼 같은 uid로
    다른 프로세스(러너 에이전트 등)가 많이 돌면 고정 상한 64로는 node가 스레드를 만들지 못해 멈춘다
    (같은 uid 스레드 60개로 재현 — wall 시간 초과). 기동 시험의 여유 64를 이 수 위에 둔다.
    """
    if not LINUX:
        return 0
    uid, total = os.getuid(), 0
    for status in pathlib.Path("/proc").glob("[0-9]*/status"):
        try:
            fields = dict(
                line.split(":", 1) for line in status.read_text().splitlines() if ":" in line
            )
            if int(fields["Uid"].split()[0]) == uid:
                total += int(fields["Threads"])
        except (OSError, KeyError, ValueError):
            continue  # 그새 끝난 프로세스
    return total


LIMITS = runner.Limits(
    wall_sec=20, cpu_sec=10, as_bytes=1 << 30, fsize_bytes=16 << 20, nproc=_uid_tasks() + 64
)

PRELUDE = textwrap.dedent(
    """
    import errno, json, os, socket, sys
    def out(**kw):
        print("RESULT " + json.dumps(kw), flush=True)
    def attempt(fn):
        try:
            fn()
            return "ok"
        except OSError as e:
            return errno.errorcode.get(e.errno, str(e.errno))
        except Exception as e:
            return type(e).__name__
    """
)


def _result(stdout: bytes) -> dict[str, Any]:
    for line in stdout.decode(errors="replace").splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT ") :])
    raise AssertionError(f"탐침 결과 없음: {stdout[-2000:]!r}")


@pytest.fixture
def ws() -> Iterator[runner.Workspace]:
    import shutil

    runner.set_subreaper()
    space = runner.prepare_workspace()
    yield space
    shutil.rmtree(space.root, ignore_errors=True)


def sandboxed(
    ws: runner.Workspace,
    body: str,
    *args: str,
    limits: runner.Limits = LIMITS,
    name: str = "scripts/probe.py",
) -> runner.RunResult:
    runner.write_file(ws.skill, name, (PRELUDE + textwrap.dedent(body)).encode())
    return runner.run_script(ws, name, list(args), limits)


def control(body: str, *args: str) -> dict[str, Any]:
    """같은 탐침을 격리 없이 돌린다(대조군)."""
    proc = subprocess.run(
        [PY, "-I", "-c", PRELUDE + textwrap.dedent(body), *args],
        capture_output=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "SECRET_TOKEN": SECRET},
    )
    return _result(proc.stdout)


@pytest.fixture
def outsider() -> Iterator[subprocess.Popen[bytes]]:
    """샌드박스 밖 프로세스 — 신호·ptrace·environ 대상. 환경에 비밀을 둔다."""
    proc = subprocess.Popen(
        [PY, "-c", "import time; time.sleep(120)"], env={"SECRET_TOKEN": SECRET}
    )
    time.sleep(0.2)
    yield proc
    proc.kill()
    proc.wait()


@pytest.fixture
def listener() -> Iterator[int]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    yield srv.getsockname()[1]
    srv.close()


# ---------- 기동(양성 대조) ----------


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("scripts/ok.py", "import sys; print('py-ok', sys.version_info[:2])"),
        ("scripts/ok.sh", "echo sh-ok; pwd; echo $HOME"),
        ("scripts/ok.js", "console.log('node-ok', process.memoryUsage().rss > 0)"),
        ("scripts/ok.mjs", "import os from 'node:os'; console.log('mjs-ok', os.tmpdir())"),
    ],
)
def test_runtimes_start_under_isolation(ws: runner.Workspace, name: str, body: str) -> None:
    runner.write_file(ws.skill, name, body.encode())
    res = runner.run_script(ws, name, [], LIMITS)
    assert res.exit_code == 0, res.stderr
    assert b"-ok" in res.stdout
    assert res.residual_clean


def test_local_ipc_is_allowed(ws: runner.Workspace) -> None:
    """socketpair(AF_UNIX, SOCK_STREAM|SOCK_CLOEXEC)·asyncio.run·multiprocessing.Pipe는 된다."""
    res = sandboxed(
        ws,
        """
        import asyncio, multiprocessing
        def pair():
            a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
            a.sendall(b"x"); assert b.recv(1) == b"x"
        async def main():
            await asyncio.sleep(0)
            return 1
        def pipe():
            r, w = multiprocessing.Pipe()
            w.send(1); assert r.recv() == 1
        out(pair=attempt(pair), asyncio=attempt(lambda: asyncio.run(main())), pipe=attempt(pipe))
        """,
    )
    assert _result(res.stdout) == {"pair": "ok", "asyncio": "ok", "pipe": "ok"}


# ---------- 네트워크·소켓 ----------

NET_PROBE = """
port = int(sys.argv[1])
def tcp():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    s.connect(("127.0.0.1", port))
def udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(b"x", ("127.0.0.1", port))
def dns():
    socket.getaddrinfo("dns-probe.example.com", 80)
def sctp():
    try:
        socket.socket(socket.AF_INET, socket.SOCK_STREAM, 132)
    except OSError as e:
        if e.errno in (errno.EPROTONOSUPPORT, errno.ESOCKTNOSUPPORT):
            return  # 커널에 SCTP가 없으면 대조군에서도 생성 불가 — 거부(EACCES)만 구분한다
        raise
def unix_path():
    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).connect("/tmp/nowhere.sock")
def unix_abstract():
    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).connect("\\0skill-probe")
def netlink():
    socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)
def dgram_pair():
    socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
def inet_pair():
    socket.socketpair(socket.AF_INET, socket.SOCK_STREAM)
out(tcp=attempt(tcp), udp=attempt(udp), dns=attempt(dns), sctp=attempt(sctp),
    unix_path=attempt(unix_path), unix_abstract=attempt(unix_abstract), netlink=attempt(netlink),
    dgram_pair=attempt(dgram_pair), inet_pair=attempt(inet_pair))
"""


def test_new_sockets_are_refused(ws: runner.Workspace, listener: int) -> None:
    got = _result(sandboxed(ws, NET_PROBE, str(listener)).stdout)
    assert got["tcp"] == "EACCES"
    assert got["udp"] == "EACCES"
    assert got["dns"] != "ok"  # 소켓을 못 만들어 이름 풀이가 실패한다(gaierror)
    assert got["sctp"] == "EACCES"
    assert got["unix_path"] == "EACCES"
    assert got["unix_abstract"] == "EACCES"
    assert got["netlink"] == "EACCES"
    assert got["dgram_pair"] == "EACCES"
    assert got["inet_pair"] == "EACCES"

    ctl = control(NET_PROBE, str(listener))
    assert ctl["tcp"] == "ok" and ctl["udp"] == "ok" and ctl["netlink"] == "ok"
    assert ctl["sctp"] == "ok"
    # 대상이 없어 연결은 실패해도 소켓 생성은 된다 — 거부 코드가 아니다
    assert ctl["unix_path"] != "EACCES" and ctl["unix_abstract"] != "EACCES"
    assert ctl["dgram_pair"] == "ok"


PAIR_PROBE = """
a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
def bind_path():
    a.bind(os.path.join(os.getcwd(), "x.sock"))
def bind_abstract():
    b.bind("\\0skill-pair")
out(bind_path=attempt(bind_path), bind_abstract=attempt(bind_abstract),
    listen=attempt(lambda: a.listen(1)), accept=attempt(lambda: a.accept()),
    connect=attempt(lambda: a.connect("\\0skill-pair")))
"""


def test_socketpair_fds_cannot_bind_listen_accept_connect(ws: runner.Workspace) -> None:
    got = _result(sandboxed(ws, PAIR_PROBE).stdout)
    assert got == {
        "bind_path": "EACCES",
        "bind_abstract": "EACCES",
        "listen": "EACCES",
        "accept": "EACCES",
        "connect": "EACCES",
    }
    assert not os.path.exists(os.path.join(ws.work, "x.sock"))


def test_socket_files_cannot_be_created_in_work(ws: runner.Workspace) -> None:
    body = """
    import stat
    out(mknod=attempt(lambda: os.mknod("s.sock", 0o600 | stat.S_IFSOCK)),
        fifo=attempt(lambda: os.mkfifo("f")))
    """
    got = _result(sandboxed(ws, body).stdout)
    assert got["mknod"] == "EACCES"
    assert got["fifo"] == "ok"  # FIFO는 만들 수 있다 — 수집이 거른다(아래 수집 시험)


def test_io_uring_is_enosys(ws: runner.Workspace) -> None:
    body = """
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    params = ctypes.create_string_buffer(120)
    rc = libc.syscall(425, 4, params)
    out(rc=rc, err=errno.errorcode.get(ctypes.get_errno()))
    """
    got = _result(sandboxed(ws, body).stdout)
    assert got == {"rc": -1, "err": "ENOSYS"}


def test_inherited_socket_fd_is_closed_before_exec(listener: int, tmp_path: Any) -> None:
    """부모가 연 TCP 소켓을 물려줘도 execve 전에 닫힌다(D7 "fd 정리", R3-08)."""
    conn = socket.create_connection(("127.0.0.1", listener))
    fd = conn.fileno()
    os.set_inheritable(fd, True)
    body = PRELUDE + f"out(write=attempt(lambda: os.write({fd}, b'leak')))"
    script = tmp_path / "p.py"
    script.write_text(body)
    ws = runner.prepare_workspace(str(tmp_path))
    argv = runner.launcher_argv(ws, LIMITS, [PY, "-I", str(script)])
    # 스크립트 경로를 읽을 수 있게 tmp_path를 읽기 전용으로 더한다
    argv[argv.index("--") : argv.index("--")] = ["--ro", str(tmp_path)]
    try:
        sandboxed_run = subprocess.run(
            argv, pass_fds=(fd,), capture_output=True, timeout=30, env=runner.script_env(ws)
        )
        assert _result(sandboxed_run.stdout) == {"write": "EBADF"}
        plain = subprocess.run(
            [PY, "-I", str(script)], pass_fds=(fd,), capture_output=True, timeout=30
        )
        assert _result(plain.stdout) == {"write": "ok"}  # 대조군: 닫지 않으면 쓸 수 있다
    finally:
        conn.close()


# ---------- 다른 프로세스 ----------

PROC_PROBE = """
import ctypes, signal
pid = int(sys.argv[1])
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
def sys_rc(rc):
    if rc < 0:
        raise OSError(ctypes.get_errno(), "")
def ptrace():
    # PTRACE_SEIZE — ATTACH와 같은 권한 검사를 하되 대상을 멈추지 않는다
    sys_rc(libc.ptrace(0x4206, pid, 0, 0))
    libc.ptrace(17, pid, 0, 0)  # PTRACE_DETACH
class IoVec(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("len", ctypes.c_size_t)]
def vm_read():
    buf = ctypes.create_string_buffer(8)
    local = IoVec(ctypes.addressof(buf), 8)
    remote = IoVec(0x400000, 8)
    rc = libc.process_vm_readv(pid, ctypes.byref(local), 1, ctypes.byref(remote), 1, 0)
    if rc < 0 and ctypes.get_errno() == errno.EFAULT:
        return  # 권한은 통과했고 주소만 틀렸다
    sys_rc(rc)
def getfd():
    pidfd = libc.syscall(434, pid, 0)  # pidfd_open
    sys_rc(pidfd)
    sys_rc(libc.syscall(438, pidfd, 0, 0))  # pidfd_getfd(fd 0)
def environ():
    with open(f"/proc/{pid}/environ", "rb") as fp:
        assert b"SECRET_TOKEN" in fp.read()
def mem():
    open(f"/proc/{pid}/mem", "rb").close()
out(ptrace=attempt(ptrace), vm_read=attempt(vm_read), getfd=attempt(getfd),
    environ=attempt(environ), mem=attempt(mem), signal=attempt(lambda: os.kill(pid, 0)))
"""


def _yama_ptrace_scope() -> int:
    """Yama `ptrace_scope`(없으면 0).

    1 이상이면 커널이 자손이 아닌 프로세스에 대한 attach 모드 접근을 uid와 무관하게 막는다 —
    Ubuntu 기본값이고 GitHub Actions 러너도 1이다.
    """
    try:
        with open("/proc/sys/kernel/yama/ptrace_scope", encoding="ascii") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def test_other_processes_are_out_of_reach(
    ws: runner.Workspace, outsider: "subprocess.Popen[bytes]"
) -> None:
    got = _result(sandboxed(ws, PROC_PROBE, str(outsider.pid)).stdout)
    assert got == {
        "ptrace": "EPERM",
        "vm_read": "EPERM",
        "getfd": "EPERM",
        "environ": "EACCES",
        "mem": "EACCES",
        "signal": "EPERM",
    }
    assert outsider.poll() is None
    ctl = control(PROC_PROBE, str(outsider.pid))
    # 대조군: 같은 uid면 된다. 다만 대조군에서도 막히는 키는 격리의 효과로 구분할 수 없어 뺀다.
    # pidfd_getfd는 Docker 기본 seccomp가 CAP_SYS_PTRACE 없이 막는다. Yama ptrace_scope ≥ 1이면
    # ptrace·process_vm_readv·/proc/<pid>/mem(attach 모드)도 대조군(outsider의 형제)에서 막힌다 —
    # 그런 호스트에서는 environ·signal만 대조하고, 셋은 ptrace_scope 0인 환경(Linux 시험 이미지)이
    # 증명한다
    unprovable = {"getfd"} | ({"ptrace", "vm_read", "mem"} if _yama_ptrace_scope() >= 1 else set())
    assert {k: v for k, v in ctl.items() if k not in unprovable} == {
        k: "ok" for k in got if k not in unprovable
    }


def test_parent_is_out_of_reach(ws: runner.Workspace) -> None:
    """부모(워커 = 이 테스트 프로세스)를 향한 ptrace·environ·신호도 막힌다."""
    os.environ["SECRET_TOKEN"] = SECRET
    try:
        got = _result(sandboxed(ws, PROC_PROBE, str(os.getpid())).stdout)
    finally:
        del os.environ["SECRET_TOKEN"]
    assert got["ptrace"] == "EPERM" and got["environ"] == "EACCES" and got["signal"] == "EPERM"


def test_parent_environment_is_not_exposed(ws: runner.Workspace) -> None:
    os.environ["SECRET_TOKEN"] = SECRET
    try:
        got = _result(sandboxed(ws, "out(keys=sorted(os.environ))").stdout)
    finally:
        del os.environ["SECRET_TOKEN"]
    assert set(got["keys"]) <= set(launcher.ENV_KEYS) | {"LC_CTYPE"}


@pytest.mark.parametrize(("tz", "label"), [("Asia/Seoul", "KST"), (None, "UTC")])
def test_worker_tz_reaches_the_script(
    ws: runner.Workspace, monkeypatch: pytest.MonkeyPatch, tz: str | None, label: str
) -> None:
    """워커에 준 `TZ`가 스크립트의 시간대가 된다(#352). 시간대 DB는 `/usr` 아래라 읽힌다."""
    if tz is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", tz)
    got = _result(sandboxed(ws, "import time; out(tz=time.strftime('%Z'))").stdout)
    assert got == {"tz": label}


# ---------- 파일시스템 ----------


def test_writes_outside_work_are_refused(ws: runner.Workspace) -> None:
    runner.write_file(ws.inputs, "in.txt", b"input")
    body = f"""
    def w(path):
        with open(path, "w") as fp:
            fp.write("x")
    out(package=attempt(lambda: w({os.path.join(ws.skill, "scripts", "probe.py")!r})),
        package_new=attempt(lambda: w({os.path.join(ws.skill, "new.txt")!r})),
        inputs=attempt(lambda: w({os.path.join(ws.inputs, "in.txt")!r})),
        tmp=attempt(lambda: w("/tmp/escape.txt")),
        root=attempt(lambda: w({os.path.join(ws.root, "x")!r})),
        read_input=attempt(lambda: open({os.path.join(ws.inputs, "in.txt")!r}).read()),
        work=attempt(lambda: w("ok.txt")))
    """
    got = _result(sandboxed(ws, body).stdout)
    assert got == {
        "package": "EACCES",
        "package_new": "EACCES",
        "inputs": "EACCES",
        "tmp": "EACCES",
        "root": "EACCES",
        "read_input": "ok",
        "work": "ok",
    }


# ---------- 산출물 수집 공격 (R3-06) ----------


def test_output_collection_ignores_links_fifos_and_hardlinks(ws: runner.Workspace) -> None:
    body = """
    os.symlink("/proc/self/environ", "outputs/env.txt")
    os.symlink("/etc/passwd", "outputs/passwd.txt")
    with open("secret.txt", "w") as fp:
        fp.write("inner")
    os.link("secret.txt", "outputs/hard.txt")
    os.mkfifo("outputs/fifo.txt")
    os.mkdir("outputs/sub")
    with open("outputs/good.txt", "w") as fp:
        fp.write("good")
    out(done=True)
    """
    os.environ["SECRET_TOKEN"] = SECRET
    try:
        started = time.monotonic()
        res = sandboxed(ws, body)
    finally:
        del os.environ["SECRET_TOKEN"]
    assert res.exit_code == 0, res.stderr
    assert time.monotonic() - started < 10  # FIFO로 부모가 멈추지 않는다
    assert [o.name for o in res.outputs] == ["good.txt"]
    assert dict(res.skipped) == {
        "env.txt": "not_regular",
        "passwd.txt": "not_regular",
        "hard.txt": "hardlink",
        "fifo.txt": "not_regular",
        "sub": "not_regular",
    }
    assert all(SECRET.encode() not in o.data for o in res.outputs)


def test_outputs_dir_replaced_by_link_is_refused(ws: runner.Workspace) -> None:
    body = """
    import shutil
    shutil.rmtree("outputs")
    os.symlink("/proc/self", "outputs")
    out(done=True)
    """
    res = sandboxed(ws, body)
    assert res.outputs == []
    assert res.skipped == [("outputs", "not_a_directory")]


def test_escaped_descendants_are_killed_before_collection(ws: runner.Workspace) -> None:
    """setsid·이중 fork로 빠져나간 자손이 수집 전에 죽는다(R4-09). 다음 run 시작 때 0이다."""
    body = """
    import time
    if os.fork() == 0:
        os.setsid()
        if os.fork() == 0:
            # 수집 중에 outputs를 바꾸려는 자손
            while True:
                try:
                    with open("outputs/race.txt", "w") as fp:
                        fp.write("evil")
                    os.replace("outputs/race.txt", "outputs/result.txt")
                except OSError:
                    pass
        os._exit(0)
    with open("outputs/result.txt", "w") as fp:
        fp.write("clean")
    time.sleep(0.3)
    out(done=True)
    """
    res = sandboxed(ws, body)
    assert res.residual_clean
    assert runner.sandbox_residue() == set()
    before = {o.name: o.data for o in res.outputs}
    time.sleep(0.3)
    again, _ = runner.collect_outputs(ws, LIMITS)
    assert {o.name: o.data for o in again} == before  # 수집 뒤에 바뀌지 않는다


# ---------- 자원 ----------


def test_fork_bomb_is_bounded_and_cleaned(ws: runner.Workspace) -> None:
    body = """
    while True:
        try:
            os.fork()
        except OSError:
            pass
    """
    limits = runner.Limits(wall_sec=3, cpu_sec=3, as_bytes=1 << 30, fsize_bytes=1 << 20, nproc=32)
    res = sandboxed(ws, body, limits=limits)
    assert res.timed_out or res.exit_code != 0
    assert res.residual_clean
    assert runner.sandbox_residue() == set()


def test_memory_limit(ws: runner.Workspace) -> None:
    got = _result(sandboxed(ws, "out(alloc=attempt(lambda: bytearray(2 << 30)))").stdout)
    assert got == {"alloc": "MemoryError"}


def test_wall_clock_timeout_kills_the_script(ws: runner.Workspace) -> None:
    limits = runner.Limits(wall_sec=1.5, cpu_sec=30, as_bytes=1 << 30, fsize_bytes=1 << 20)
    started = time.monotonic()
    res = sandboxed(ws, "import time\ntime.sleep(60)", limits=limits)
    assert res.timed_out
    assert time.monotonic() - started < 10
    assert runner.sandbox_residue() == set()


def test_cpu_limit(ws: runner.Workspace) -> None:
    limits = runner.Limits(wall_sec=20, cpu_sec=1, as_bytes=1 << 30, fsize_bytes=1 << 20)
    res = sandboxed(ws, "while True: pass", limits=limits)
    assert not res.timed_out
    assert res.exit_code is not None and res.exit_code < 0  # SIGXCPU·SIGKILL


def test_output_flood_is_cut(ws: runner.Workspace) -> None:
    limits = runner.Limits(
        wall_sec=20, cpu_sec=10, as_bytes=1 << 30, fsize_bytes=1 << 20, stream_max_bytes=4096
    )
    res = sandboxed(ws, "while True: sys.stdout.write('x' * 65536)", limits=limits)
    assert res.output_overflow
    assert len(res.stdout) == 4096
    assert runner.sandbox_residue() == set()


def test_file_size_limit(ws: runner.Workspace) -> None:
    body = """
    def big():
        with open("big.bin", "wb") as fp:
            fp.write(b"x" * (32 << 20))
    import signal
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    out(write=attempt(big))
    """
    assert _result(sandboxed(ws, body).stdout) == {"write": "EFBIG"}


def test_script_cannot_escape_through_launcher_stage_failure(tmp_path: Any) -> None:
    """launcher 인자가 틀리면 execve 없이 125다 — 실프로세스 확인."""
    marker = tmp_path / "ran"
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            runner.LAUNCHER_MODULE,
            "--cpu",
            "1",
            "--",
            "/bin/sh",
            "-c",
            f"touch {marker}",
        ],
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == launcher.EXIT_LAUNCHER_FAILED
    assert not marker.exists()


def _live_sandbox_threads() -> set[str]:
    """스레드 단위로 본 살아 있는 샌드박스 태스크 — 탐지 함수와 독립적인 기준."""
    me = runner._status_fields("self", "/proc")
    found: set[str] = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except OSError:
            continue
        for tid in tids:
            fields = runner._status_fields(f"{pid}/task/{tid}", "/proc")
            if runner._is_sandbox_task(fields, me.get("Uid", "").split()[:1], runner._filters(me)):
                found.add(f"{pid}/{tid}")
    return found


def test_zombie_leader_with_live_threads_is_still_swept(ws: runner.Workspace) -> None:
    """리더 스레드만 `SYS_exit`로 끝내 좀비로 보이게 해도 나머지 스레드까지 죽는다(리뷰 R4P-01)."""
    body = """
    import ctypes, platform, threading, time
    SYS_EXIT = 93 if platform.machine() in ("aarch64", "arm64") else 60
    if os.fork() == 0:
        os.setsid()
        def loop():
            for i in range(600):
                try:
                    with open("outputs/late.txt", "w") as fp:
                        fp.write(str(i))
                except OSError:
                    pass
                time.sleep(0.05)
        threading.Thread(target=loop).start()
        time.sleep(0.1)
        ctypes.CDLL(None).syscall(SYS_EXIT, 0)
    time.sleep(0.5)
    out(done=True)
    """
    res = sandboxed(ws, body)
    assert res.residual_clean
    assert _live_sandbox_threads() == set()
    before = {o.name: o.data for o in res.outputs}
    time.sleep(0.3)
    again, _ = runner.collect_outputs(ws, LIMITS)
    assert {o.name: o.data for o in again} == before


def test_workspace_locked_by_chmod_is_still_removed(ws: runner.Workspace) -> None:
    """Landlock은 chmod를 다루지 않는다 — 스크립트가 잠근 작업공간도 지운다(리뷰 R4P-02)."""
    body = f"""
    os.makedirs("deep/er", exist_ok=True)
    os.chmod("deep", 0)
    os.chmod({ws.tmp!r}, 0)
    os.chmod({ws.skill!r}, 0)
    out(root=attempt(lambda: os.chmod({ws.root!r}, 0)))
    """
    res = sandboxed(ws, body)
    assert _result(res.stdout) == {"root": "ok"}  # 막을 수 없다 — 정리가 되돌린다
    assert runner.remove_workspace(ws) is True
    assert not os.path.lexists(ws.root)


def test_unprintable_output_names_are_skipped(ws: runner.Workspace) -> None:
    body = """
    fd = os.open(b"outputs/\\xff\\xfe.txt", os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, b"x"); os.close(fd)
    with open("outputs/\\u202etxt.exe", "w") as fp:
        fp.write("y")
    with open("outputs/보고서.txt", "w") as fp:
        fp.write("z")
    out(done=True)
    """
    res = sandboxed(ws, body)
    assert [o.name for o in res.outputs] == ["보고서.txt"]
    assert sorted(r for _, r in res.skipped) == ["bad_name", "bad_name"]
