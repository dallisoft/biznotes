"""스킬 스크립트 샌드박스 전용 최소 Celery 앱 (표준 계획 D7 "실행 환경과 배달").

    SKILL_SANDBOX_BROKER_URL=redis://… SKILL_SANDBOX_STORAGE_ORIGINS=http://api:8000 \\
        celery -A biznotes_core.workers.skill_sandbox.app worker -Q skills.sandbox -c 1 -P prefork

- 코어 `celery_app`을 import하지 않는다 — 그 모듈은 제품 설정·비밀 검증을 끌고 온다. 이 앱은
  표준 라이브러리, Celery, redis와 이 패키지만 쓰고 설정은 환경 변수 두 개로 받는다.
- 태스크는 이 앱에만 등록한다. 다른 큐로 잘못 간 메시지는 제품 워커에서 "unregistered task"다.
- **최대 한 번 실행**: `acks_late=False`, `reject_on_worker_lost=False`. 워커가 죽으면 그 작업은
  잃는다 — 에이전트는 대기 한도 뒤 도구 오류를 받는다. 재배달로 같은 스크립트가 두 번 돌지 않는다.
- **전용 워커 가드**(fail-closed): 소비 큐가 `skills.sandbox` 하나, 동시성 1, root 아님,
  Landlock ABI >= 6, 환경에 DB·스토리지·LLM 자격증명 없음. 하나라도 어기면 기동을 멈추고, 태스크도
  가드 통과 표식이 없으면 실행하지 않는다.
- 회신은 결과 백엔드가 아니라 회신 키(`SKILL_SANDBOX_REPLY_PREFIX` + call_id)에 LPUSH한다.
  에이전트 워커는 태스크 안에서 `AsyncResult.get()`을 부르지 않고 그 키를 BLPOP한다.
"""

import json
import os
import re
import signal
from collections.abc import Mapping
from types import FrameType
from typing import Any

import structlog
from celery import Celery, signals

from biznotes_core.workers import task_names
from biznotes_core.workers.skill_sandbox import job as sandbox_job
from biznotes_core.workers.skill_sandbox import launcher, runner

logger = structlog.get_logger()

REPLY_TTL_SEC = 600

# 이 워커 환경에 있으면 안 되는 자격증명(D7 "자격증명"). 브로커 URL만 허용한다.
# 거부 목록은 넓게 잡는다(리뷰 R4P-10) — 오탐이면 기동이 멈출 뿐이고,
# 미탐이면 비밀이 스크립트 옆에 있다.
_FORBIDDEN_ENV = re.compile(
    r"(POSTGRES|DATABASE|MINIO|S3_|AWS_|GOOGLE|AZURE|OPENAI|ANTHROPIC|GEMINI|LITELLM|"
    r"KEY|SECRET|PASS|TOKEN|CREDENTIAL|BROKER|JWT|DSN)",
    re.IGNORECASE,
)
# 이 앱의 두 변수와, 파이썬 공식 이미지가 싣는 서명 키 지문(비밀 아님)
_ALLOWED_ENV = frozenset(
    {"SKILL_SANDBOX_BROKER_URL", "SKILL_SANDBOX_STORAGE_ORIGINS", "GPG_KEY"}
)

_state: dict[str, Any] = {"guard_ok": False, "dirty": False, "child_hardened": False}


def _broker_url() -> str:
    # import만 하는 도구(테스트·문서 생성)를 위해 기본값을 둔다 — 가드가 값 없음을 거부한다
    return os.environ.get("SKILL_SANDBOX_BROKER_URL", "memory://")


app = Celery("biznotes_core_skill_sandbox", broker=_broker_url())
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    task_acks_late=False,
    task_reject_on_worker_lost=False,
    worker_prefetch_multiplier=1,
    worker_concurrency=1,
    task_default_queue=task_names.SKILL_SANDBOX_QUEUE,
    broker_connection_retry_on_startup=True,
    worker_hijack_root_logger=False,
)


def guard_violations(
    *,
    queues: set[str],
    concurrency: int,
    uid: int,
    abi: int,
    env: Mapping[str, str],
    pool: str = "prefork",
) -> list[str]:
    """전용 워커 조건 위반 목록. 비어 있어야 기동한다."""
    problems: list[str] = []
    if pool != "prefork":
        # solo·threads 풀에서는 풀 자식 하드닝(non-dumpable·subreaper)과 terminate가 없다
        problems.append(f"풀이 prefork가 아니다: {pool}")
    if queues != {task_names.SKILL_SANDBOX_QUEUE}:
        problems.append(
            f"소비 큐가 {task_names.SKILL_SANDBOX_QUEUE} 하나가 아니다: {sorted(queues)}"
        )
    if concurrency != 1:
        problems.append(f"동시성이 1이 아니다: {concurrency}")
    if uid == 0:
        problems.append("root로 실행 중이다")
    if abi < launcher.REQUIRED_LANDLOCK_ABI:
        problems.append(f"Landlock ABI {abi} < {launcher.REQUIRED_LANDLOCK_ABI}")
    if not env.get("SKILL_SANDBOX_BROKER_URL"):
        problems.append("SKILL_SANDBOX_BROKER_URL이 없다")
    try:
        if not sandbox_job.origins_from_env(env):
            problems.append("SKILL_SANDBOX_STORAGE_ORIGINS가 없다")
    except sandbox_job.JobRejected:
        problems.append("SKILL_SANDBOX_STORAGE_ORIGINS 형식이 틀렸다")
    leaked = sorted(k for k in env if k not in _ALLOWED_ENV and _FORBIDDEN_ENV.search(k))
    if leaked:
        problems.append(f"자격증명으로 보이는 환경 변수가 있다: {leaked}")
    return problems


def _is_this_app(candidate: Any) -> bool:
    """Celery 시그널은 프로세스 전역이다 — 이 앱의 워커일 때만 반응한다."""
    return candidate is app


@signals.worker_init.connect
def _guard(sender: Any = None, **_: object) -> None:
    if not _is_this_app(getattr(sender, "app", None)):
        return
    try:
        queues = set(sender.app.amqp.queues.consume_from)
        concurrency = int(sender.concurrency)
        pool_module = str(getattr(sender.pool_cls, "__module__", ""))
        pool = "prefork" if pool_module.endswith(".prefork") else pool_module or "unknown"
    except Exception:  # noqa: BLE001 — 읽지 못하면 전용 워커라고 볼 수 없다
        queues, concurrency, pool = set(), 0, "unknown"
    problems = guard_violations(
        pool=pool,
        queues=queues,
        concurrency=concurrency,
        uid=os.getuid(),
        abi=launcher.DefaultSyscalls().landlock_abi(),
        env=os.environ,
    )
    if problems:
        logger.critical("skill.sandbox.guard_failed", problems=problems)
        # 시그널 수신자의 Exception은 Celery가 삼킨다 — BaseException으로 기동을 멈춘다
        raise SystemExit(1)
    _state["guard_ok"] = True
    logger.info("skill.sandbox.guard_ok", queues=sorted(queues))


@signals.worker_process_init.connect
def _harden_child(**_: object) -> None:
    """풀 자식: non-dumpable(스크립트가 부모 environ·메모리를 못 읽게), subreaper(R4-09)."""
    import ctypes

    from celery import current_app

    if not _is_this_app(current_app._get_current_object()):
        return

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE 0
        logger.critical("skill.sandbox.harden_failed", step="non_dumpable")
        return
    if not runner.set_subreaper():
        logger.critical("skill.sandbox.harden_failed", step="subreaper")
        return
    # 이 표식이 없으면 handle()이 작업을 거부한다
    _state["child_hardened"] = True


class Terminated(BaseException):
    """revoke(terminate=True)의 SIGTERM — 실행 중인 스크립트를 정리하고 끝낸다."""


def _on_sigterm(_signo: int, _frame: FrameType | None) -> None:
    raise Terminated()


def _publish(call_id: str, body: dict[str, Any]) -> None:
    import redis

    key = task_names.SKILL_SANDBOX_REPLY_PREFIX + call_id
    client = redis.Redis.from_url(_broker_url())
    try:
        pipe = client.pipeline()
        pipe.lpush(key, json.dumps(body))
        pipe.expire(key, REPLY_TTL_SEC)
        pipe.execute()
    finally:
        client.close()


def handle(raw: Any) -> tuple[str | None, dict[str, Any]]:
    """작업 한 건을 처리하고 (call_id, 회신 본문)을 돌려준다. call_id가 없으면 회신하지 않는다."""
    call_id = raw.get("call_id") if isinstance(raw, dict) else None
    if not isinstance(call_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", call_id):
        return None, {"status": "rejected", "reason": "bad_call_id"}
    if not _state["guard_ok"]:
        return call_id, {"status": "rejected", "reason": "not_dedicated"}
    if _state["dirty"]:
        return call_id, {"status": "rejected", "reason": "dirty"}
    # 이전 실행의 잔존 프로세스가 0인지 확인한다(수락 기준 33 "다음 run 시작 때 0")
    if not runner.sweep_residue():
        _state["dirty"] = True
        return call_id, {"status": "rejected", "reason": "dirty"}
    if not _state["child_hardened"]:
        return call_id, {"status": "rejected", "reason": "not_hardened"}
    try:
        parsed = sandbox_job.parse_job(raw, sandbox_job.origins_from_env(os.environ))
        body = sandbox_job.execute(parsed)
    except sandbox_job.JobRejected as e:
        return call_id, {"status": "rejected", "reason": e.reason}
    except runner.SandboxError as e:
        return call_id, {"status": "rejected", "reason": e.reason}
    except sandbox_job.WorkspaceLeft:
        _state["dirty"] = True
        return call_id, {"status": "rejected", "reason": "cleanup_failed"}
    except Exception as e:  # noqa: BLE001 — 회신 없이 끝나면 에이전트가 대기 한도를 다 쓴다(R4P-05)
        logger.error("skill.sandbox.internal_error", call_id=call_id, error=type(e).__name__)
        if not runner.sweep_residue():
            _state["dirty"] = True
        return call_id, {"status": "rejected", "reason": "internal"}
    if not body.get("residual_clean", False) or not body.pop("workspace_removed", False):
        _state["dirty"] = True
    return call_id, body


@app.task(
    name=task_names.RUN_SKILL_SCRIPT,
    # 기본값(shared=True)이면 같은 프로세스의 다른 앱(코어 celery_app)에도 등록된다
    shared=False,
    acks_late=False,
    reject_on_worker_lost=False,
    ignore_result=True,
)
def run_skill_script(job: dict[str, Any]) -> None:
    previous = signal.signal(signal.SIGTERM, _on_sigterm)
    call_id: str | None = None
    try:
        call_id, body = handle(job)
    except Terminated:
        # runner의 finally가 스크립트와 자손을 이미 정리했다
        call_id = job.get("call_id") if isinstance(job, dict) else None
        body = {"status": "terminated"}
        if not runner.sweep_residue():
            _state["dirty"] = True
    finally:
        signal.signal(signal.SIGTERM, previous)
    logger.info(
        "skill.sandbox.finished",
        call_id=call_id,
        status=body.get("status"),
        reason=body.get("reason"),
        exit_code=body.get("exit_code"),
    )
    if call_id is not None:
        _publish(call_id, body)
    if _state["dirty"]:
        # 남은 프로세스를 없애지 못했다 — 이 풀 자식을 버리고 새로 띄우게 한다
        logger.critical("skill.sandbox.dirty_worker")
        raise SystemExit(1)
