"""샌드박스 작업 한 건 — 검증·내려받기·실행·올리기·회신 본문 (표준 계획 D7 "실행 환경과 배달").

작업(`job`)은 에이전트 워커가 만든다(`services/skills/sandbox.py`). 이 워커는 DB·스토리지·LLM
자격증명이 없으므로 파일은 모두 짧은 수명의 presigned URL로 주고받는다. 브로커로 온 작업도
믿지 않는다 — 모양·크기·URL 출처·한도를 모두 다시 확인하고, 한도는 이 모듈의 상한으로 자른다.

HTTP는 표준 라이브러리로 하고, 리다이렉트와 환경 프록시를 따르지 않는다. URL의 출처(scheme·
host·port)는 `SKILL_SANDBOX_STORAGE_ORIGINS`에 있어야 한다 — 위조된 작업으로 이 워커를 내부망
요청 대리인으로 쓰지 못하게 하기 위해서다.
"""

import hashlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from biznotes_core.workers.skill_sandbox import runner

# 작업이 요구해도 넘지 않는 상한
MAX_WALL_SEC = 600
MAX_AS_BYTES = 4 << 30
MAX_FSIZE_BYTES = 256 << 20
MAX_FILES = 200
MAX_PACKAGE_BYTES = 64 << 20
MAX_INPUTS = 20
MAX_INPUT_BYTES = 64 << 20
MAX_OUTPUT_SLOTS = 50
MAX_OUTPUTS_BYTES = 64 << 20
MAX_ARGS = 64
MAX_ARG_CHARS = 8192
# 회신에 싣는 스트림 상한(모델에게 주는 30K자는 에이전트 쪽이 다시 자른다)
REPLY_STREAM_BYTES = 64 * 1024
HTTP_TIMEOUT_SEC = 30

_INPUT_NAME = re.compile(r"^[^/\x00-\x1f\x7f]{1,128}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class WorkspaceLeft(Exception):
    """작업공간을 지우지 못했다 — 이전 사용자의 파일이 남는다. 워커를 dirty로 표시한다(R4P-02)."""


class JobRejected(Exception):
    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        super().__init__(message or reason)


@dataclass(frozen=True)
class RemoteFile:
    path: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Job:
    call_id: str
    start_by: float
    script: str
    args: list[str]
    files: list[RemoteFile]
    inputs: list[RemoteFile]
    output_slots: list[str]
    limits: runner.Limits


# ---------- 검증 ----------


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise JobRejected("bad_url")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{parts.scheme}://{parts.hostname.lower()}:{port}"


def normalize_origin(origin: str) -> str:
    return _origin(origin.strip().rstrip("/") + "/")


def _url(value: Any, origins: frozenset[str]) -> str:
    if not isinstance(value, str) or len(value) > 8192:
        raise JobRejected("bad_url")
    if _origin(value) not in origins:
        raise JobRejected("url_origin_not_allowed")
    return value


def _rel_path(value: Any) -> str:
    """패키지 경로 규칙 — 패키지 검증(`validate_path`)처럼 유니코드 파일명을 받는다(리뷰 R4P-04).

    절대 경로·빈 성분·`.`·`..`·역슬래시·출력할 수 없는 문자는 거부한다.
    """
    if not isinstance(value, str) or not 0 < len(value) <= 512:
        raise JobRejected("bad_path")
    parts = value.split("/")
    if (
        value.startswith("/")
        or "\\" in value
        or not value.isprintable()
        or any(p in ("", ".", "..") for p in parts)
    ):
        raise JobRejected("bad_path")
    return value


def _int(value: Any, low: int, high: int, reason: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise JobRejected(reason)
    return value


def _remote_files(
    items: Any,
    origins: frozenset[str],
    *,
    max_count: int,
    max_total: int,
    name_check: Callable[[Any], str],
) -> list[RemoteFile]:
    if not isinstance(items, list) or len(items) > max_count:
        raise JobRejected("bad_files")
    out: list[RemoteFile] = []
    seen: set[str] = set()
    total = 0
    for item in items:
        if not isinstance(item, dict):
            raise JobRejected("bad_files")
        path = name_check(item.get("path"))
        if path in seen:
            raise JobRejected("duplicate_path")
        seen.add(path)
        size = _int(item.get("size"), 0, max_total, "bad_size")
        total += size
        if total > max_total:
            raise JobRejected("too_large")
        digest = item.get("sha256")
        if not isinstance(digest, str) or not _HEX64.match(digest):
            raise JobRejected("bad_digest")
        out.append(RemoteFile(path, _url(item.get("url"), origins), size, digest))
    return out


def _input_name(value: Any) -> str:
    if not isinstance(value, str) or not _INPUT_NAME.match(value) or value.startswith("."):
        raise JobRejected("bad_input_name")
    return value


def parse_job(raw: Any, origins: frozenset[str]) -> Job:
    """브로커로 온 작업을 검증한다. 모양이 틀리면 `JobRejected`다."""
    if not isinstance(raw, dict):
        raise JobRejected("bad_job")
    call_id = raw.get("call_id")
    if not isinstance(call_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", call_id):
        raise JobRejected("bad_call_id")
    start_by = raw.get("start_by")
    if isinstance(start_by, bool) or not isinstance(start_by, (int, float)):
        raise JobRejected("bad_deadline")
    files = _remote_files(
        raw.get("files"),
        origins,
        max_count=MAX_FILES,
        max_total=MAX_PACKAGE_BYTES,
        name_check=_rel_path,
    )
    script = _rel_path(raw.get("script"))
    if not script.startswith("scripts/") or script not in {f.path for f in files}:
        raise JobRejected("script_not_in_package")
    args = raw.get("args")
    if not isinstance(args, list) or len(args) > MAX_ARGS:
        raise JobRejected("bad_args")
    for arg in args:
        if not isinstance(arg, str) or len(arg) > MAX_ARG_CHARS or "\x00" in arg:
            raise JobRejected("bad_args")
    inputs = _remote_files(
        raw.get("inputs", []),
        origins,
        max_count=MAX_INPUTS,
        max_total=MAX_INPUT_BYTES,
        name_check=_input_name,
    )
    slots = raw.get("output_slots")
    if not isinstance(slots, list) or len(slots) > MAX_OUTPUT_SLOTS:
        raise JobRejected("bad_outputs")
    output_slots = [_url(u, origins) for u in slots]
    lim = raw.get("limits")
    if not isinstance(lim, dict):
        raise JobRejected("bad_limits")
    limits = runner.Limits(
        wall_sec=_int(lim.get("wall_sec"), 1, MAX_WALL_SEC, "bad_limits"),
        cpu_sec=_int(lim.get("cpu_sec"), 1, MAX_WALL_SEC, "bad_limits"),
        as_bytes=_int(lim.get("as_bytes"), 64 << 20, MAX_AS_BYTES, "bad_limits"),
        fsize_bytes=_int(lim.get("fsize_bytes"), 1, MAX_FSIZE_BYTES, "bad_limits"),
        outputs_max_files=len(output_slots),
        outputs_max_bytes=_int(lim.get("outputs_max_bytes"), 0, MAX_OUTPUTS_BYTES, "bad_limits"),
    )
    return Job(call_id, float(start_by), script, list(args), files, inputs, output_slots, limits)


# ---------- HTTP ----------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_: Any, **__: Any) -> None:
        return None  # 리다이렉트를 따르지 않는다 — 3xx는 오류로 올라온다


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def http_get(url: str, limit: int) -> bytes:
    with _opener().open(urllib.request.Request(url, method="GET"), timeout=HTTP_TIMEOUT_SEC) as res:
        data = res.read(limit + 1)
    if len(data) > limit:
        raise JobRejected("download_too_large")
    return bytes(data)


def http_put(url: str, data: bytes) -> None:
    req = urllib.request.Request(url, data=data, method="PUT")
    req.add_header("Content-Type", "application/octet-stream")
    with _opener().open(req, timeout=HTTP_TIMEOUT_SEC) as res:
        res.read()


# ---------- 실행 ----------


Getter = Callable[[str, int], bytes]
Putter = Callable[[str, bytes], None]


def _fetch(ws_dir: str, files: Sequence[RemoteFile], get: Getter) -> None:
    for f in files:
        try:
            data = get(f.url, f.size)
        except JobRejected:
            raise
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise JobRejected("download_failed", type(e).__name__) from e
        if len(data) != f.size or hashlib.sha256(data).hexdigest() != f.sha256:
            raise JobRejected("digest_mismatch")
        runner.write_file(ws_dir, f.path, data, mode=0o400)


def _text(data: bytes) -> str:
    return data[:REPLY_STREAM_BYTES].decode("utf-8", errors="replace")


def execute(
    job: Job,
    *,
    get: Getter = http_get,
    put: Putter = http_put,
    now: Callable[[], float] = time.time,
    work_root: str | None = None,
) -> dict[str, Any]:
    """검증한 작업을 실행하고 회신 본문을 만든다. 마감이 지났으면 실행하지 않는다."""
    if now() > job.start_by:
        raise JobRejected("expired")
    ws = runner.prepare_workspace(work_root)
    try:
        _fetch(ws.skill, job.files, get)
        _fetch(ws.inputs, job.inputs, get)
        # 마감은 내려받기 뒤에 다시 본다 — 에이전트가 이미 포기했을 수 있다
        if now() > job.start_by:
            raise JobRejected("expired")
        result = runner.run_script(ws, job.script, job.args, job.limits)
        uploaded: list[dict[str, Any]] = []
        for slot, out in enumerate(result.outputs[: len(job.output_slots)]):
            try:
                put(job.output_slots[slot], out.data)
            except (urllib.error.URLError, OSError, ValueError):
                result.skipped.append((out.name, "upload_failed"))
                continue
            uploaded.append(
                {
                    "slot": slot,
                    "name": out.name,
                    "size": len(out.data),
                    "sha256": hashlib.sha256(out.data).hexdigest(),
                }
            )
        body = {
            "status": "completed",
            "exit_code": result.exit_code,
            "launcher_failed": result.launcher_failed,
            "timed_out": result.timed_out,
            "output_overflow": result.output_overflow,
            "stdout": _text(result.stdout),
            "stderr": _text(result.stderr),
            "outputs": uploaded,
            "skipped": [{"name": n, "reason": r} for n, r in result.skipped],
            "residual_clean": result.residual_clean,
        }
    except BaseException:
        if not runner.remove_workspace(ws):
            raise WorkspaceLeft(ws.root) from None
        raise
    body["workspace_removed"] = runner.remove_workspace(ws)
    return body


def origins_from_env(env: Mapping[str, str]) -> frozenset[str]:
    raw = env.get("SKILL_SANDBOX_STORAGE_ORIGINS", "")
    return frozenset(normalize_origin(o) for o in raw.split(",") if o.strip())
