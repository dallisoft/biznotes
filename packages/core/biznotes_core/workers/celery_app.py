
import structlog
from celery import Celery, signals

from biznotes_core.config import assert_prod_secrets, get_settings
from biznotes_core.core import dumpable
from biznotes_core.core.http_logging import install_httpx_log_redaction

logger = structlog.get_logger()

# 워커는 `-l INFO`로 root를 INFO에 둔다 — httpx 요청 로그의 URL을 출처로 줄인다(#408)
install_httpx_log_redaction()

assert_prod_secrets(get_settings())  # prod + 기본 시크릿 → 워커도 기동 거부

celery_app = Celery(
    get_settings().celery_app_name,
    broker=get_settings().redis_url,
    backend=get_settings().redis_url,
)

# task_routes·beat_schedule·imports의 `app.workers.tasks.*`는 그대로 둔다.
# Celery 태스크 이름은 브로커에 실리는 **계약**이다 — 바꾸면 큐에 남은 메시지가
# 라우팅되지 않는다. 태스크 모듈이 코어로 오더라도 이름은 유지해야 한다.
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    # 기동 시 브로커 미준비 상태에서도 재연결 시도 (Celery 6 기본값 변경 대비)
    broker_connection_retry_on_startup=True,
    # 장시간 idle 시 TCP 연결이 조용히 끊겨 컨슈머가 wedge되는 것 방지.
    redis_socket_keepalive=True,
    broker_transport_options={"socket_keepalive": True},
    result_backend_transport_options={"socket_keepalive": True},
    # 워커가 태스크 처리 중 OOM·재시작되어도 태스크를 잃지 않도록 완료 후 ack.
    # 재배달로 ingest_source가 겹쳐도 색인 교체는 소스 단위 잠금으로 직렬화되어 정합하다(#27)
    # — 겹침은 비용(중복 파싱·임베딩)일 뿐이다.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # 큐 분리: K8s 전환 시 워커 Deployment별 독립 스케일링 (DEPLOYMENT.md)
    task_routes={
        "app.workers.tasks.ingestion.*": {"queue": "ingestion"},
        "app.workers.tasks.overview.*": {"queue": "ingestion"},
        "app.workers.tasks.source_digest.*": {"queue": "ingestion"},
        "app.workers.tasks.agent.*": {"queue": "agent"},
        # 스킬 스크립트 샌드박스(D7) — 이 앱에는 등록하지 않는다. 전용 워커만 소비한다
        "biznotes_core.workers.skill_sandbox.*": {"queue": "skills.sandbox"},
    },
    task_default_queue="ingestion",
    # beat 주기 작업(단일 beat 인스턴스 전제)
    beat_schedule={
        # 5분마다 정체된(non-terminal 상태로 오래 멈춘) 소스를 재발행하는 안전망.
        # 메시지 유실·컨슈머 wedge·enqueue 실패 등 모든 유실 시나리오를 자가 치유.
        "reap-stalled-sources": {
            "task": "app.workers.tasks.ingestion.reap_stalled_sources",
            "schedule": 300.0,
        },
        # 5분마다 발행 누락으로 queued에 정체된 agent run을 재발행한다.
        # 커밋은 성공했으나 브로커 발행이 실패한 경우의 유일한 회수 경로(R3-1c/d).
        # `running` 정체(stuck-running)는 별개 문제로 범위 밖(HCI-R3-6).
        "reap-orphaned-runs": {
            "task": "app.workers.tasks.agent.reap_orphaned_runs",
            "schedule": 300.0,
        },
    },
)

celery_app.autodiscover_tasks(["biznotes_core.workers", "app.workers"])
# **import 경로는 실제 모듈 위치**, **태스크 이름은 브로커 계약**이다 — 둘은 다르다.
# 이관된 태스크는 코어 경로로 import하되, `@celery_app.task(name=...)`의 이름은
# `app.workers.tasks.*` 그대로 둔다(바꾸면 큐에 남은 메시지가 라우팅되지 않는다).
#
# `agent`는 아직 프로젝트에 있다. 프로젝트별로 달라질 수 있어 목록을 설정으로 받는다.
celery_app.conf.imports = [
    "biznotes_core.workers.tasks.ingestion",
    "biznotes_core.workers.tasks.overview",
    "biznotes_core.workers.tasks.source_digest",
    *get_settings().celery_extra_task_modules,
]


@signals.worker_init.connect
@signals.worker_process_init.connect
def _deny_same_uid_proc_access(**_: object) -> None:
    """워커 프로세스를 non-dumpable로 둔다 — 자식 파서가 environ의 비밀을 읽지 못하게 한다.

    `worker_init`은 풀을 fork하기 전 마스터에서, `worker_process_init`은 prefork 자식마다
    돈다(fork 상속과 별개로 자식에서도 명시한다). API 프로세스도 이 모듈을 import하므로
    import 시점이 아니라 워커 시그널에 건다. 실패해도 워커는 기동한다.
    """
    try:
        dumpable.set_non_dumpable()
    except OSError as exc:
        logger.error("워커 non-dumpable 설정 실패", errno=exc.errno)


@signals.worker_init.connect
def _warn_unset_agent_http_egress_mode(**_: object) -> None:
    """`AGENT_HTTP_EGRESS_MODE`를 명시하지 않은 워커는 기동 때 1회 경고한다(#395).

    지금 기본은 `open`(외부 HTTP 메서드·본문 제한 없음)이고 다음 MINOR에서 `fetch_only`로 바뀐다.
    """
    settings = get_settings()
    if "agent_http_egress_mode" not in settings.model_fields_set:
        logger.warning(
            "agent_http_egress_mode_unset",
            mode=settings.agent_http_egress_mode,
            hint="AGENT_HTTP_EGRESS_MODE를 명시한다 — 다음 MINOR에서 기본이 fetch_only로 바뀐다",
        )


@celery_app.task(name="app.workers.tasks.ping")
def ping() -> str:
    """Phase 0 워커 동작 확인용."""
    return "pong"
