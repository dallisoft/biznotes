"""스킬 샌드박스 전용 워커 — 앱 설정·가드·작업 검증·배달 의미 (표준 계획 D7, 수락 기준 35).

스크립트 실행 자체(`runner.run_script`)는 Linux 격리 시험(`test_skill_sandbox_isolation.py`)이
실측한다. 여기서는 그 앞뒤 — 작업 검증, 마감, 내려받기 무결성, 올리기, 회신 — 를 플랫폼과
무관하게 본다.
"""

import ast
import hashlib
import http.server
import json
import pathlib
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from biznotes_core.workers import task_names
from biznotes_core.workers.skill_sandbox import app as sandbox_app
from biznotes_core.workers.skill_sandbox import job as sandbox_job
from biznotes_core.workers.skill_sandbox import runner

CORE = pathlib.Path(__file__).resolve().parent.parent / "biznotes_core"
ORIGIN = "http://rustfs:9000"
ORIGINS = frozenset({"http://rustfs:9000"})
SCRIPT = b"print('hi')\n"


def remote(path: str, data: bytes, origin: str = ORIGIN) -> dict[str, Any]:
    return {
        "path": path,
        "url": f"{origin}/skill-packages/v/{path}?sig",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def make_job(**over: Any) -> dict[str, Any]:
    job = {
        "call_id": str(uuid.uuid4()),
        "start_by": time.time() + 60,
        "script": "scripts/a.py",
        "args": ["--x", "1"],
        "files": [remote("SKILL.md", b"---\n"), remote("scripts/a.py", SCRIPT)],
        "inputs": [],
        "output_slots": [f"{ORIGIN}/artifacts/r/scripts/c/{i}?sig" for i in range(3)],
        "limits": {
            "wall_sec": 10,
            "cpu_sec": 10,
            "as_bytes": 1 << 30,
            "fsize_bytes": 1 << 20,
            "outputs_max_bytes": 1 << 20,
        },
    }
    job.update(over)
    return job


# ---------- 앱 설정: 전용 최소 앱, 최대 한 번 ----------


def test_app_delivers_at_most_once_one_at_a_time() -> None:
    conf = sandbox_app.app.conf
    assert conf.task_acks_late is False
    assert conf.task_reject_on_worker_lost is False
    assert conf.worker_prefetch_multiplier == 1
    assert conf.worker_concurrency == 1
    assert conf.task_ignore_result is True
    assert conf.task_default_queue == "skills.sandbox"
    task = sandbox_app.app.tasks[task_names.RUN_SKILL_SCRIPT]
    assert task.acks_late is False and task.reject_on_worker_lost is False


def test_task_is_registered_only_in_the_sandbox_app() -> None:
    """코어·제품 앱에는 없다 — 잘못 라우팅되면 "unregistered task"로 실패한다."""
    from biznotes_core.workers.celery_app import celery_app

    assert task_names.RUN_SKILL_SCRIPT not in celery_app.tasks
    routes = celery_app.conf.task_routes
    assert routes["biznotes_core.workers.skill_sandbox.*"] == {
        "queue": task_names.SKILL_SANDBOX_QUEUE
    }
    # 샌드박스 워커 프로세스처럼 새 인터프리터에서 앱만 불러온다 — 코어 설정·앱을 끌고 오지 않는다
    probe = (
        "import json, sys\n"
        "import biznotes_core.workers.skill_sandbox.app as a\n"
        "tasks = sorted(n for n in a.app.tasks if not n.startswith('celery.'))\n"
        "mods = sorted(m for m in sys.modules if m.startswith('biznotes_core'))\n"
        "print(json.dumps({'tasks': tasks, 'mods': mods}))\n"
    )
    out = json.loads(
        subprocess.run([sys.executable, "-c", probe], capture_output=True, check=True).stdout
    )
    assert out["tasks"] == [task_names.RUN_SKILL_SCRIPT]
    assert set(out["mods"]) == {
        "biznotes_core",
        "biznotes_core.workers",
        "biznotes_core.workers.task_names",
        "biznotes_core.workers.skill_sandbox",
        "biznotes_core.workers.skill_sandbox.app",
        "biznotes_core.workers.skill_sandbox.job",
        "biznotes_core.workers.skill_sandbox.launcher",
        "biznotes_core.workers.skill_sandbox.runner",
    }


def test_signal_receivers_ignore_other_apps() -> None:
    """다른 앱의 워커 기동에는 가드가 반응하지 않는다(시그널은 프로세스 전역이다)."""
    from types import SimpleNamespace

    sandbox_app._guard(sender=SimpleNamespace(app=object()))  # SystemExit이 나지 않는다


def test_sandbox_package_imports_no_core_services_models_or_config() -> None:
    """DB·모델·서비스·설정 import 금지(D15). 표준 라이브러리·Celery·redis·structlog만."""
    allowed_core = {
        "biznotes_core.workers.task_names",
        "biznotes_core.workers",
        "biznotes_core.workers.skill_sandbox",
    }
    third_party = {"celery", "redis", "structlog"}
    for path in sorted((CORE / "workers" / "skill_sandbox").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
            for name in names:
                root = name.split(".")[0]
                if root == "biznotes_core":
                    assert name in allowed_core or name.startswith(
                        "biznotes_core.workers.skill_sandbox"
                    ), f"{path.name}: {name}"
                else:
                    assert root in sys.stdlib_module_names or root in third_party, (
                        f"{path.name}: {name}"
                    )


def test_agent_side_does_not_import_the_sandbox_worker() -> None:
    tree = ast.parse((CORE / "services" / "skills" / "sandbox.py").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any("skill_sandbox" in name for name in imported)
    assert "subprocess" not in imported  # 에이전트 워커에서 스크립트를 띄우지 않는다


# ---------- 전용 워커 가드 ----------

GOOD_ENV = {
    "SKILL_SANDBOX_BROKER_URL": "redis://broker:6379/0",
    "SKILL_SANDBOX_STORAGE_ORIGINS": ORIGIN,
    "PATH": "/usr/bin",
}


def violations(**over: Any) -> list[str]:
    kw: dict[str, Any] = {
        "queues": {"skills.sandbox"},
        "concurrency": 1,
        "uid": 1000,
        "abi": 8,
        "env": GOOD_ENV,
    }
    kw.update(over)
    return sandbox_app.guard_violations(**kw)


def test_guard_accepts_a_dedicated_worker() -> None:
    assert violations() == []
    # 파이썬 공식 이미지의 서명 키 지문은 비밀이 아니다
    assert violations(env={**GOOD_ENV, "GPG_KEY": "7169605F62C751356D054A26A821E680E5FA6305"}) == []
    # 스크립트에 넘기는 시간대(#352)
    assert violations(env={**GOOD_ENV, "TZ": "Asia/Seoul"}) == []


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"queues": {"skills.sandbox", "agent"}}, "소비 큐"),
        ({"queues": {"ingestion"}}, "소비 큐"),
        ({"concurrency": 2}, "동시성"),
        ({"uid": 0}, "root"),
        ({"abi": 5}, "Landlock"),
        ({"env": {**GOOD_ENV, "POSTGRES_PASSWORD": "x"}}, "자격증명"),
        ({"env": {**GOOD_ENV, "MINIO_SECRET_KEY": "x"}}, "자격증명"),
        ({"env": {**GOOD_ENV, "OPENAI_API_KEY": "x"}}, "자격증명"),
        ({"env": {**GOOD_ENV, "DATABASE_URL": "x"}}, "자격증명"),
        ({"env": {"SKILL_SANDBOX_STORAGE_ORIGINS": ORIGIN}}, "BROKER_URL"),
        ({"env": {"SKILL_SANDBOX_BROKER_URL": "redis://b"}}, "STORAGE_ORIGINS"),
        ({"env": {**GOOD_ENV, "SKILL_SANDBOX_STORAGE_ORIGINS": "ftp://x"}}, "형식"),
        ({"env": {**GOOD_ENV, "DB_PASS": "x"}}, "자격증명"),
        ({"env": {**GOOD_ENV, "GOOGLE_APPLICATION_CREDENTIALS": "/k.json"}}, "자격증명"),
        ({"env": {**GOOD_ENV, "CELERY_BROKER_URL": "redis://:pw@b"}}, "자격증명"),
        ({"env": {**GOOD_ENV, "ENCRYPTION_KEY": "x"}}, "자격증명"),
        ({"pool": "solo"}, "prefork"),
    ],
)
def test_guard_rejects(over: dict[str, Any], needle: str) -> None:
    assert any(needle in p for p in violations(**over))


@pytest.fixture
def guarded(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    state = {"guard_ok": True, "dirty": False, "child_hardened": True}
    monkeypatch.setattr(sandbox_app, "_state", state)
    monkeypatch.setenv("SKILL_SANDBOX_STORAGE_ORIGINS", ORIGIN)
    monkeypatch.setattr(runner, "sweep_residue", lambda **_: True)
    yield state


def test_task_refuses_without_the_guard_mark(
    guarded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    guarded["guard_ok"] = False
    monkeypatch.setattr(sandbox_job, "execute", _must_not_run)
    call_id, body = sandbox_app.handle(make_job())
    assert body == {"status": "rejected", "reason": "not_dedicated"}
    assert call_id is not None


def test_dirty_worker_refuses_further_work(
    guarded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_job, "execute", _must_not_run)
    monkeypatch.setattr(runner, "sweep_residue", lambda **_: False)
    assert sandbox_app.handle(make_job())[1] == {"status": "rejected", "reason": "dirty"}
    assert guarded["dirty"] is True
    monkeypatch.setattr(runner, "sweep_residue", lambda **_: True)
    assert sandbox_app.handle(make_job())[1] == {"status": "rejected", "reason": "dirty"}


def test_bad_call_id_gets_no_reply(guarded: dict[str, Any]) -> None:
    assert sandbox_app.handle(make_job(call_id="../../x"))[0] is None
    assert sandbox_app.handle("not a dict")[0] is None


def _must_not_run(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("실행하면 안 된다")


# ---------- 작업 검증 ----------


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        (
            {"files": [remote("scripts/a.py", SCRIPT, origin="http://evil:9000")]},
            "url_origin_not_allowed",
        ),
        (
            {"files": [remote("scripts/a.py", SCRIPT, origin="http://rustfs:9001")]},
            "url_origin_not_allowed",
        ),
        ({"output_slots": ["http://169.254.169.254/latest?x"]}, "url_origin_not_allowed"),
        ({"output_slots": ["file:///etc/passwd"]}, "bad_url"),
        ({"script": "scripts/../SKILL.md"}, "bad_path"),
        ({"script": "/etc/passwd"}, "bad_path"),
        ({"script": "SKILL.md"}, "script_not_in_package"),
        ({"script": "scripts/missing.py"}, "script_not_in_package"),
        (
            {"files": [remote("scripts/a.py", SCRIPT), remote("scripts/a.py", SCRIPT)]},
            "duplicate_path",
        ),
        ({"files": [{**remote("scripts/a.py", SCRIPT), "sha256": "x"}]}, "bad_digest"),
        ({"files": [{**remote("scripts/a.py", SCRIPT), "size": -1}]}, "bad_size"),
        ({"args": "--x 1"}, "bad_args"),
        ({"args": ["a\x00b"]}, "bad_args"),
        ({"args": ["x"] * 65}, "bad_args"),
        ({"output_slots": [f"{ORIGIN}/a/{i}" for i in range(51)]}, "bad_outputs"),
        ({"limits": {"wall_sec": 10_000}}, "bad_limits"),
        ({"call_id": "x"}, "bad_call_id"),
        ({"start_by": "soon"}, "bad_deadline"),
        ({"inputs": [{**remote(".hidden", b"x")}]}, "bad_input_name"),
    ],
)
def test_parse_rejects_forged_jobs(over: dict[str, Any], reason: str) -> None:
    with pytest.raises(sandbox_job.JobRejected) as err:
        sandbox_job.parse_job(make_job(**over), ORIGINS)
    assert err.value.reason == reason


def test_parse_clamps_nothing_silently_and_keeps_argv() -> None:
    parsed = sandbox_job.parse_job(make_job(), ORIGINS)
    assert parsed.args == ["--x", "1"]
    assert parsed.limits.outputs_max_files == 3
    assert parsed.limits.wall_sec == 10


def test_origin_normalization() -> None:
    assert sandbox_job.origins_from_env(
        {"SKILL_SANDBOX_STORAGE_ORIGINS": "http://RustFS:9000/, https://s3.x"}
    ) == {
        "http://rustfs:9000",
        "https://s3.x:443",
    }


# ---------- 실행 흐름(실행기는 가짜) ----------


class Transfers:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.gets: list[str] = []
        self.puts: dict[str, bytes] = {}

    def get(self, url: str, limit: int) -> bytes:
        self.gets.append(url)
        for path, data in self.files.items():
            if f"/{path}?" in url:
                return data[: limit + 1]
        raise OSError("404")

    def put(self, url: str, data: bytes) -> None:
        self.puts[url] = data


def test_expired_job_is_not_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "run_script", _must_not_run)
    monkeypatch.setattr(runner, "prepare_workspace", _must_not_run)
    parsed = sandbox_job.parse_job(make_job(start_by=time.time() - 1), ORIGINS)
    with pytest.raises(sandbox_job.JobRejected) as err:
        sandbox_job.execute(parsed, get=_must_not_run, put=_must_not_run)
    assert err.value.reason == "expired"


def test_deadline_passing_during_download_skips_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setattr(runner, "run_script", _must_not_run)
    clock = iter([0.0, 100.0])
    parsed = sandbox_job.parse_job(make_job(start_by=50.0), ORIGINS)
    t = Transfers({"SKILL.md": b"---\n", "scripts/a.py": SCRIPT})
    with pytest.raises(sandbox_job.JobRejected) as err:
        sandbox_job.execute(
            parsed, get=t.get, put=t.put, now=lambda: next(clock), work_root=str(tmp_path)
        )
    assert err.value.reason == "expired"
    assert list(tmp_path.iterdir()) == []  # 작업공간을 지웠다


def test_digest_mismatch_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setattr(runner, "run_script", _must_not_run)
    parsed = sandbox_job.parse_job(make_job(), ORIGINS)
    t = Transfers({"SKILL.md": b"---\n", "scripts/a.py": b"print('tampered')\n"})
    with pytest.raises(sandbox_job.JobRejected) as err:
        sandbox_job.execute(parsed, get=t.get, put=t.put, work_root=str(tmp_path))
    assert err.value.reason == "digest_mismatch"


def test_completed_job_uploads_outputs_to_slots_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(
        ws: runner.Workspace, script: str, args: list[str], limits: runner.Limits
    ) -> runner.RunResult:
        seen["script"] = (pathlib.Path(ws.skill) / script).read_bytes()
        seen["args"] = args
        return runner.RunResult(
            exit_code=0,
            launcher_failed=False,
            timed_out=False,
            output_overflow=False,
            stdout=b"ok\n" + b"x" * (sandbox_job.REPLY_STREAM_BYTES + 10),
            stderr=b"",
            outputs=[runner.Output("a.txt", b"A"), runner.Output("b.png", b"BB")],
            skipped=[("env.txt", "not_regular")],
        )

    monkeypatch.setattr(runner, "run_script", fake_run)
    job = make_job()
    parsed = sandbox_job.parse_job(job, ORIGINS)
    t = Transfers({"SKILL.md": b"---\n", "scripts/a.py": SCRIPT})
    body = sandbox_job.execute(parsed, get=t.get, put=t.put, work_root=str(tmp_path))
    assert seen == {"script": SCRIPT, "args": ["--x", "1"]}
    assert t.puts == {job["output_slots"][0]: b"A", job["output_slots"][1]: b"BB"}
    assert [(o["slot"], o["name"], o["size"]) for o in body["outputs"]] == [
        (0, "a.txt", 1),
        (1, "b.png", 2),
    ]
    assert body["skipped"] == [{"name": "env.txt", "reason": "not_regular"}]
    assert len(body["stdout"]) == sandbox_job.REPLY_STREAM_BYTES
    assert body["status"] == "completed"
    assert list(tmp_path.iterdir()) == []


# ---------- HTTP: 리다이렉트·프록시를 따르지 않는다 ----------


@pytest.fixture
def redirecting_server() -> Iterator[str]:
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            hits.append(self.path)
            if self.path.startswith("/redirect"):
                self.send_response(302)
                self.send_header("Location", "/secret")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"body")

        def log_message(self, *_: Any) -> None:
            return None

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    assert "/secret" not in hits


def test_http_get_does_not_follow_redirects(
    redirecting_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import urllib.error

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")  # 프록시를 쓰면 연결이 실패한다
    assert sandbox_job.http_get(redirecting_server + "/ok", 10) == b"body"
    with pytest.raises(urllib.error.HTTPError):
        sandbox_job.http_get(redirecting_server + "/redirect", 10)
    with pytest.raises(sandbox_job.JobRejected):
        sandbox_job.http_get(redirecting_server + "/ok", 2)


# ---------- 회신 ----------


def test_reply_goes_to_the_call_key_with_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    import redis

    ops: list[tuple[str, Any]] = []

    class Pipe:
        def lpush(self, key: str, value: str) -> None:
            ops.append(("lpush", (key, value)))

        def expire(self, key: str, ttl: int) -> None:
            ops.append(("expire", (key, ttl)))

        def execute(self) -> None:
            ops.append(("execute", None))

    class Client:
        def pipeline(self) -> Pipe:
            return Pipe()

        def close(self) -> None:
            ops.append(("close", None))

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda _u: Client()))
    call_id = str(uuid.uuid4())
    sandbox_app._publish(call_id, {"status": "completed"})
    key = task_names.SKILL_SANDBOX_REPLY_PREFIX + call_id
    assert ops == [
        ("lpush", (key, '{"status": "completed"}')),
        ("expire", (key, sandbox_app.REPLY_TTL_SEC)),
        ("execute", None),
        ("close", None),
    ]


def test_unhardened_child_refuses_work(
    guarded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    guarded["child_hardened"] = False
    monkeypatch.setattr(sandbox_job, "execute", _must_not_run)
    assert sandbox_app.handle(make_job())[1] == {"status": "rejected", "reason": "not_hardened"}


def test_unexpected_error_still_replies(
    guarded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """회신 없이 끝나면 에이전트가 대기 한도를 다 쓴다(리뷰 R4P-05)."""

    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sandbox_job, "execute", boom)
    call_id, body = sandbox_app.handle(make_job())
    assert call_id is not None and body == {"status": "rejected", "reason": "internal"}
    assert guarded["dirty"] is False  # 잔존 프로세스가 없으면 워커는 계속 쓴다


def test_workspace_left_behind_marks_the_worker_dirty(
    guarded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def left(*_a: Any, **_k: Any) -> Any:
        raise sandbox_job.WorkspaceLeft("/tmp/skillrun-x")

    monkeypatch.setattr(sandbox_job, "execute", left)
    assert sandbox_app.handle(make_job())[1] == {"status": "rejected", "reason": "cleanup_failed"}
    assert guarded["dirty"] is True


def test_completed_job_that_left_its_workspace_marks_dirty(
    guarded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sandbox_job,
        "execute",
        lambda *_a, **_k: {
            "status": "completed",
            "residual_clean": True,
            "workspace_removed": False,
        },
    )
    _, body = sandbox_app.handle(make_job())
    assert body["status"] == "completed" and "workspace_removed" not in body
    assert guarded["dirty"] is True


@pytest.mark.parametrize(
    "path", ["scripts/보고서.py", "scripts/fill (v2)+@.py", "references/표.md"]
)
def test_unicode_package_paths_are_accepted(path: str) -> None:
    """패키지 검증과 같은 경로 규칙 — 한글·기호 파일명이 있어도 스크립트를 실행한다(리뷰 R4P-04)."""
    job = make_job(
        files=[remote("scripts/a.py", SCRIPT), remote(path, b"x")],
    )
    parsed = sandbox_job.parse_job(job, ORIGINS)
    assert path in {f.path for f in parsed.files}


@pytest.mark.parametrize("path", ["scripts/a\x01.py", "scripts\\a.py", "scripts/\u202ea.py"])
def test_unprintable_package_paths_are_rejected(path: str) -> None:
    with pytest.raises(sandbox_job.JobRejected) as err:
        sandbox_job.parse_job(
            make_job(files=[remote("scripts/a.py", SCRIPT), remote(path, b"x")]), ORIGINS
        )
    assert err.value.reason == "bad_path"
