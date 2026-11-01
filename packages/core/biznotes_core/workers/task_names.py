"""Celery 태스크 이름 상수.

태스크 이름은 브로커에 실리는 **계약**이다. 큐에 남은 메시지는 이름으로
라우팅되므로, 이름을 바꾸면 배포 시점에 처리 중이던 메시지가 유실된다.

태스크 모듈은 아직 각 프로젝트의 `app.workers.tasks.*`에 있고 두 제품이 같은
이름을 쓴다. 코어가 태스크를 **import하지 않고 이름으로 발행**하면
서비스→워커 방향 의존이 사라진다 — 그래서 이름만 여기 모은다.

모듈이 코어로 이관되더라도 이 문자열은 그대로 유지해야 한다.
"""

INGEST_SOURCE = "app.workers.tasks.ingestion.ingest_source"
# 인제스트 색인 단계(청킹·임베딩·색인, #244). `ingest_source`가 파싱 뒤 이 이름으로 넘긴다.
# `task_routes`의 `app.workers.tasks.ingestion.*`로 `ingestion` 큐에 간다 — HWP 전용 워커
# (`ingest.hwp`)는 소비하지 않는다. `ingestion`을 소비하는 모든 워커가 이 태스크를 알아야 한다
INDEX_SOURCE = "app.workers.tasks.ingestion.index_source"
REAP_STALLED_SOURCES = "app.workers.tasks.ingestion.reap_stalled_sources"
RUN_AGENT = "app.workers.tasks.agent.run_agent"
REAP_ORPHANED_RUNS = "app.workers.tasks.agent.reap_orphaned_runs"
GENERATE_NOTEBOOK_OVERVIEW = "app.workers.tasks.overview.generate_notebook_overview"
# 소스 요약 미리 만들기(#378, 설정 `source_digest_on_ingest`). `ingestion` 큐로 간다 — 코어 앱의
# `imports`에 모듈이 있어 `ingestion`을 소비하는 워커가 등록한다
GENERATE_SOURCE_DIGEST = "app.workers.tasks.source_digest.generate_source_digest"
# 기존 소스 요약 백필(#454, 인자 `notebook_id` 또는 None=모든 노트북). 같은 모듈이라 `ingestion`
# 큐로 간다. 예약은 `biznotes_core.services.source_digest_backfill`이다(범위마다 중복 발행 방지)
BACKFILL_SOURCE_DIGESTS = "app.workers.tasks.source_digest.backfill_source_digests"

# 스킬 스크립트 샌드박스(표준 계획 D7). 제품 워커가 아니라 코어의 전용 최소 앱
# (`biznotes_core.workers.skill_sandbox.app`)이 구현하므로 코어 모듈 경로를 이름으로 쓴다.
# 큐와 회신 키 접두어도 브로커 계약이다 — 에이전트 워커와 샌드박스 워커가 함께 읽는다.
RUN_SKILL_SCRIPT = "biznotes_core.workers.skill_sandbox.run_skill_script"
SKILL_SANDBOX_QUEUE = "skills.sandbox"
SKILL_SANDBOX_REPLY_PREFIX = "skill-sandbox:reply:"
