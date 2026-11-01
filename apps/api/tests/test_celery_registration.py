"""Celery 워커가 실제로 기동 가능한지 — import 경로와 태스크 이름을 함께 잠근다.

왜 필요한가:
    태스크 모듈을 코어로 이관하면서 `celery_app.conf.imports`를 갱신하지 않아
    **워커 3개가 재시작 루프에 빠진 적이 있다**
    (`ModuleNotFoundError: No module named 'app.workers.tasks.ingestion'`).

    전체 테스트 스위트는 통과했다. 테스트는 태스크 함수를 직접 import하지
    워커 부팅 경로(`conf.imports`)를 지나지 않기 때문이다. 배포해야 드러났다.

    두 축을 구분해 검증한다.
      - **import 경로**는 실제 모듈 위치를 따라야 한다 (워커 부팅).
      - **태스크 이름**은 브로커 계약이라 위치가 바뀌어도 유지되어야 한다
        (큐에 남은 메시지가 라우팅된다).
"""

import importlib
import shlex
from pathlib import Path

import pytest
import yaml
from biznotes_core.workers import task_names
from biznotes_core.workers.celery_app import celery_app

COMPOSE = Path(__file__).resolve().parents[3] / "infra/compose.yml"

#: 이 이름들은 브로커에 실린다 — 모듈이 어디로 가든 바뀌면 안 된다.
CONTRACT_TASK_NAMES = {
    task_names.INGEST_SOURCE,
    # 코어 v0.16.0 #244 — 인제스트 색인 단계. 미룬 재시도·reaper 재개가 이 이름으로 낸다
    task_names.INDEX_SOURCE,
    task_names.REAP_STALLED_SOURCES,
    task_names.RUN_AGENT,
    task_names.REAP_ORPHANED_RUNS,
    task_names.GENERATE_NOTEBOOK_OVERVIEW,
}


@pytest.mark.parametrize("module", list(celery_app.conf.imports))
def test_configured_task_module_is_importable(module: str) -> None:
    """워커는 기동 시 `conf.imports`를 전부 import한다 — 하나라도 없으면 죽는다."""
    importlib.import_module(module)


def test_all_contract_task_names_are_registered() -> None:
    """워커가 실제로 등록하는 이름과 계약 상수가 일치한다."""
    for module in celery_app.conf.imports:
        importlib.import_module(module)
    registered = set(celery_app.tasks)
    missing = CONTRACT_TASK_NAMES - registered
    assert not missing, (
        f"계약 태스크가 등록되지 않았다 — 워커가 이 메시지를 라우팅하지 못한다: {sorted(missing)}"
    )


def test_beat_and_route_targets_are_registered() -> None:
    """beat 스케줄과 큐 라우팅이 실재하지 않는 태스크를 가리키지 않는다."""
    for module in celery_app.conf.imports:
        importlib.import_module(module)
    registered = set(celery_app.tasks)
    beat_targets = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    missing = beat_targets - registered
    assert not missing, f"beat가 없는 태스크를 가리킨다: {sorted(missing)}"


def test_vector_orphan_reconcile_is_gone() -> None:
    """청크 벡터는 행과 같은 트랜잭션에 있어(pgvector) 고아가 생기지 않는다 — 회수 beat가 없다."""
    for module in celery_app.conf.imports:
        importlib.import_module(module)
    assert "reconcile-vector-orphans" not in celery_app.conf.beat_schedule
    assert "app.workers.tasks.ingestion.reconcile_vector_orphans" not in set(celery_app.tasks)


def _routed_queue(name: str) -> str:
    """큐 인자 없이 발행했을 때 Celery 라우터(`task_routes`)가 정하는 큐 이름."""
    queue = celery_app.amqp.router.route({}, name)["queue"]
    return getattr(queue, "name", queue)


def _compose_worker_queues(service: str) -> list[str]:
    """`infra/compose.yml` 워커 command의 `-Q` 값(쉼표 구분)."""
    command = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"][service]["command"]
    argv = shlex.split(command) if isinstance(command, str) else list(command)
    values = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg in ("-Q", "--queues")]
    assert len(values) == 1, f"{service} command의 -Q가 정확히 1개가 아니다: {argv}"
    return values[0].split(",")


@pytest.mark.parametrize("name", [task_names.INDEX_SOURCE, task_names.INGEST_SOURCE])
def test_ingest_tasks_route_to_ingestion_queue(name: str) -> None:
    """코어 v0.16.0 #244 — 큐 인자 없이 낸 두 인제스트 태스크는 `ingestion`으로 간다."""
    assert _routed_queue(name) == "ingestion"


def test_compose_workers_consume_ingest_routes() -> None:
    """계약 드리프트 게이트(core-v0.16.0 AC-1.3 ①) — 라우팅 큐에 소비자가 있다.

    통합 `worker`가 `ingestion`을 소비하지 않으면 `index_source`가 쌓이기만 한다.
    """
    assert "ingestion" in _compose_worker_queues("worker")
    assert _routed_queue(task_names.INDEX_SOURCE) in _compose_worker_queues("worker")


def test_compose_worker_consumes_agent_queue_and_embeds_beat() -> None:
    """통합 워커 하나가 agent 큐도 소비하고 beat(-B)를 품는다 — 별도 beat 서비스가 없다(E9)."""
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    assert _routed_queue(task_names.RUN_AGENT) in _compose_worker_queues("worker")
    argv = shlex.split(services["worker"]["command"])
    assert "-B" in argv or "--beat" in argv
    assert not {"beat", "migrate", "worker-ingestion", "worker-agent"} & set(services)
