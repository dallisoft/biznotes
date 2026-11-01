"""코어 설정 — 필드 선언은 여기 한 곳, 프로젝트 식별자는 서브클래스가 덮어쓴다.

두 제품은 같은 필드 집합을 쓰되 포트·DB명·베이스 URL만 다르다. 그래서 필드
*선언*은 전부 `CoreSettings`에 두고, 프로젝트는 상속해 **기본값만** 바꾼다.

    class Settings(CoreSettings):
        postgres_db: str = "biznotes"
        api_base_url: str = "http://localhost:28000"

    set_settings_class(Settings)

`get_settings()`는 등록된 서브클래스를 인스턴스화한다. 코어 모듈 다수가 import
시점에 `get_settings()`를 호출하므로 등록이 그보다 늦으면 조용히 코어 기본값을
쓰게 된다 — `set_settings_class`가 그 경우 예외를 던져 실패를 드러낸다.
"""

from functools import lru_cache
from typing import Any, Literal
from zoneinfo import available_timezones

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 소스 업로드 최대 크기의 하드 상한(바이트, #388) — env(`SOURCE_MAX_UPLOAD_BYTES`)와 관리자
#: 재정의 모두 이 값을 넘지 못한다
SOURCE_MAX_UPLOAD_BYTES_CEILING = 500 * 1024 * 1024


@lru_cache(maxsize=1)
def _iana_names() -> frozenset[str]:
    # Debian 시간대 DB에는 호스트 설정을 가리키는 `localtime` 링크가 있다 — IANA 이름이 아니다
    return frozenset(available_timezones() - {"localtime"})


def is_timezone_name(name: str) -> bool:
    """IANA 시간대 이름인지(#352) — `UTC`이거나 시간대 DB에 **정확히** 있는 이름이다.

    `ZoneInfo(name)`이 열리는지로 판정하지 않는다. 대소문자를 가리지 않는 파일 시스템(macOS)에서는
    `asia/seoul`도 열리지만 Linux에서는 실패한다. `UTC`는 시간대 DB(tzdata) 없이도 쓴다.
    """
    return name == "UTC" or name in _iana_names()


#: `redis_url`의 환경 변수 이름 — `VALKEY_URL`이 먼저다. 기본값만 바꾸려고 필드를 다시 선언하는
#: 하위 클래스도 이 값을 `validation_alias`로 줘야 한다(다시 선언하면 필드 정보가 통째로 바뀐다)
VALKEY_URL_ALIASES = AliasChoices("valkey_url", "redis_url")


class CoreSettings(BaseSettings):
    """앱 설정 — 전부 환경변수로 주입 (12-Factor, DEPLOYMENT.md §3 계약)."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    app_env: str = "dev"  # dev | staging | prod
    # 제품 식별자. PostgreSQL advisory lock 네임스페이스 접두사로도 쓰이므로
    # 값을 바꾸면 기존 락과 네임스페이스가 갈라진다.
    service_name: str = "biznotes-core"
    api_base_url: str = "http://localhost:8000"
    web_base_url: str = "http://localhost:3000"
    cors_origins: str = "http://localhost:3000"
    # 사용자에게 보이는 날짜 경계·시각의 기준 시간대(IANA 이름, #352) — 일일 사용량
    # 한도의 「오늘」, 관리자 사용량의 「이번 달」, 노트북 내보내기 시각.
    # `UTC`가 아니면 시간대 DB가 필요하다
    display_timezone: str = "UTC"

    # PostgreSQL
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "agentscore"
    postgres_user: str = "agentscore"
    postgres_password: str = "agentscore"
    # `core.db` 엔진(`get_engine`)의 연결 풀(#288) — 기본값은 SQLAlchemy 기본값과 같다. 프로세스마다
    # 하나이고 API 요청 세션과 워커의 lease heartbeat·실행 로그 세션이 쓴다. 워커 러너 엔진은
    # `workers.runner`가 따로 정한다(5 + 5).
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_pool_timeout: float = Field(default=30.0, gt=0)  # 초

    # Valkey(Redis 프로토콜 호환) — 캐시·Celery 브로커·이벤트 pubsub. 환경 변수는 `VALKEY_URL`이고
    # `REDIS_URL`도 별칭으로 받는다(둘 다 있으면 `VALKEY_URL`). 필드 이름은 호출부가 많아 그대로
    # 둔다
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        validation_alias=VALKEY_URL_ALIASES,
    )

    # 오브젝트 스토리지(`services/storage.py`). `local`은 `data_dir/<버킷>/<키>` 파일이고
    # api·worker가 같은 경로(볼륨)를 봐야 한다. `s3`는 S3 호환 서버이고 `biznotes-core[s3]`
    # (minio 클라이언트)가 필요하다
    storage_backend: Literal["local", "s3"] = "local"
    # 환경 변수는 `BIZNOTES_DATA_DIR`. 상대 경로는 프로세스 작업 디렉터리 기준이다
    data_dir: str = Field(
        default="data", validation_alias=AliasChoices("biznotes_data_dir", "data_dir")
    )
    storage_bucket_sources: str = "sources"
    storage_bucket_parsed: str = "parsed"
    storage_bucket_artifacts: str = "artifacts"
    storage_bucket_skill_packages: str = "skill-packages"  # 파일 기반 스킬 패키지 (drafts/versions)
    # S3 호환 백엔드 — `host:port`. 자격 증명 기본값은 비어 있다(s3일 때 prod는 기동을 거부한다)
    s3_endpoint: str = "localhost:9000"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_secure: bool = False

    # 아직 코어로 오지 않은 프로젝트 태스크 모듈 — celery worker가 import해야
    # 태스크가 등록된다. 이관이 끝나면 빈 목록이 된다.
    celery_extra_task_modules: list[str] = []

    # 스킬 패키지 매니페스트 파일명 — 제품마다 다르다.
    # 값을 바꾸면 이미 저장된 패키지의 매니페스트를 찾지 못한다 — 원본의 다른 제품(sl-agents)은
    # 개명할 때 일회성 이관 스크립트(scripts/rename_skill_manifest.py)를 따로 두었다.
    skill_manifest_filename: str = "skill.json"

    # Agent Skills 표준 (docs/plans/agent-skills-standard/plan.md D14)
    # 켜면 매니페스트 없는 표준 패키지의 새 freeze가 standard_agent_v1이 되고, 채팅·에이전트의
    # 스킬 카탈로그와 직접 실행이 표준 런타임을 쓴다(D6). 끄면 카탈로그가 비고 표준 version의
    # 실행은 명시적 오류다.
    skill_standard_runtime_enabled: bool = False
    skill_yaml_max_depth: int = Field(default=32, gt=0, le=256)  # frontmatter 중첩 상한(D1)
    skill_import_max_upload_bytes: int = Field(default=10 * 1024 * 1024, gt=0)  # 업로드 ZIP
    skill_import_max_total_bytes: int = Field(default=10 * 1024 * 1024, gt=0)  # 압축 해제 합계
    skill_import_max_files: int = Field(default=100, gt=0)
    skill_import_max_file_bytes: int = Field(default=1024 * 1024, gt=0)
    # ZIP 항목의 **신고** 압축률 상한(#119). 합성 표본 10건 실측: 정상 마크다운 37배 ·
    # 반복 로그 331배 · 반복 코드 357배 · 반복 JSON 335배는 통과하고, 1MiB 근-전영
    # 0바이트 파일(1015배)만 거부된다 — 의도된 거부에 해당한다.
    # 정확히 신고된 정상 DEFLATED 항목의 이론 최대는 ~1032:1이라 1000은 그 바로 아래다.
    # 신고 `compress_size`는 위조될 수 있어 이 검사는 방어선이지 유일한 방어선은 아니다
    # (신고 크기를 축소한 폭탄은 여기서 안 걸리고, 방식 allowlist·bounded read가 막는다).
    skill_import_max_compression_ratio: int = Field(default=1000, gt=0)
    # archive 전체 항목 수 상한(#402, 디렉터리 항목 포함). 폴더를 고르면 패키지 한도는 고른
    # 서브트리에만 걸리지만, 이 상한·압축률·압축 방식·업로드 크기는 선택과 무관하게 archive
    # 전체에 건다.
    skill_import_max_archive_entries: int = Field(default=2000, gt=0)
    # 표준 런타임(D6) — 카탈로그 항목 상한, run당 활성화 횟수, 파일 읽기 한도
    skill_catalog_max_entries: int = Field(default=50, gt=0, le=500)
    skill_max_activations_per_run: int = Field(default=5, gt=0, le=50)
    skill_read_max_bytes: int = Field(default=256 * 1024, gt=0)
    skill_read_run_budget_bytes: int = Field(default=2 * 1024 * 1024, gt=0)
    # 표준 스킬 직접 실행의 최종 답변 산출물 이름(D6-6)
    skill_result_artifact_name: str = "result.md"
    # 다단계 스킬 실행(#202) — `SKILL.md`의 `metadata.stages`.
    # 총액을 단계 수로 나눠 각 `react_agent`에 준다 — 단계가 늘면 단계당 예산이 줄어든다.
    skill_stage_total_cost_usd: float = Field(default=2.00, gt=0, le=20.0)
    skill_stage_total_tokens: int = Field(default=400_000, gt=0, le=2_000_000)
    # `metadata.stages` 배열 상한 — 단계 하나가 react_agent+save_artifact 한 쌍이라
    # 그래프 크기와 총 예산이 함께 이 한도로 묶인다
    skill_max_stages: int = Field(default=8, gt=0, le=20)
    # 번들 스크립트 실행 L2(D7, Phase 4). `disabled`면 `run_skill_script`를 노출하지 않는다.
    # `process`는 전용 샌드박스 워커(`biznotes_core.workers.skill_sandbox.app`)로 보낸다.
    # `container`는 예약 값이다(아직 구현 없음 — 고르면 도구 오류).
    skill_script_backend: Literal["disabled", "process", "container"] = "disabled"
    skill_script_timeout_sec: int = Field(default=60, gt=0, le=600)  # 스크립트 벽시계·CPU
    # 발행부터 회신까지 기다리는 한도. 메시지 `expires`는 이 값에서 실행 시간을 뺀 값이다
    skill_script_wait_sec: int = Field(default=150, gt=0, le=1800)
    skill_script_memory_bytes: int = Field(default=1 << 30, ge=64 << 20, le=4 << 30)  # RLIMIT_AS
    skill_script_file_size_bytes: int = Field(default=64 << 20, gt=0, le=256 << 20)  # RLIMIT_FSIZE
    skill_script_max_outputs: int = Field(default=20, ge=0, le=50)
    skill_script_outputs_max_bytes: int = Field(default=20 << 20, ge=0, le=64 << 20)
    skill_script_output_chars: int = Field(default=30000, gt=0, le=200000)  # 모델에게 주는 출력
    # 샌드박스가 저장소에 닿는 주소 — 서명 URL을 이 주소로 만든다(R2-18). `local`이면 api 기준 URL
    # (`/internal/storage/<토큰>`, 비우면 `api_base_url`), `s3`면 서명할 `host:port`(비우면
    # `s3_endpoint`)다. compose는 `http://api:8000`이다
    skill_script_storage_endpoint: str = ""
    skill_script_storage_region: str = "us-east-1"  # s3 서명 region — 버킷 위치 조회 요청을 없앤다
    # `artifact_read`가 산출물 원본에서 읽는 최대 바이트(#133). 넘으면 앞부분만 읽고 표시한다
    artifact_read_max_bytes: int = Field(default=2 << 20, gt=0, le=64 << 20)

    # Celery 앱 이름 — 제품마다 다르다. 브로커에 노출되는 식별자다.
    celery_app_name: str = "biznotes_core"

    # 로컬 LLM 호스트 — 도커에서 호스트의 ollama/lmstudio에 닿기 위한 호스트명
    # (Docker Desktop: host.docker.internal / 호스트 직접 실행: localhost)
    llm_local_host: str = "host.docker.internal"

    # 검색
    search_provider: str = "searxng"  # searxng | tavily
    searxng_url: str = "http://localhost:8080"
    tavily_api_key: str = ""


    # 보안
    ssrf_allow_private: bool = False  # true면 내부망 URL 허용 (개발 전용)
    # 에이전트 외부 HTTP 연결 정책(#395) — `http_request` 도구·`http` 노드(tool·plugin kind 포함).
    # `fetch_only`면 GET·HEAD, 본문 없음, 허용 헤더만, userinfo 없음, URL·쿼리 길이 상한이다.
    # 기본 `open`은 지금 동작이다(다음 MINOR에서 기본이 `fetch_only`로 바뀐다). API와 워커에
    # 같은 값을 둔다 — API는 저장 검증, 워커는 실행 시 거부에 쓴다
    agent_http_egress_mode: Literal["open", "fetch_only"] = "open"
    # 쉼표로 구분한 허용 호스트. `*.example.com`은 하위 도메인만 맞는다. 비면 제한이 없다.
    # 모드와 무관하게 리다이렉트 홉마다 본다
    agent_http_allowed_hosts: str = ""
    # 받아오기 전용의 길이 상한 — 인코딩한 형태로 잰다(한글 1자 = 9자)
    agent_http_max_url_chars: int = Field(default=2048, gt=0, le=16_384)
    agent_http_max_query_chars: int = Field(default=512, gt=0, le=16_384)
    jwt_secret: str = "change-me-in-prod"
    jwt_access_expire_minutes: int = 15
    jwt_refresh_expire_days: int = 14
    encryption_key: str = "change-me-32-bytes-key-for-aesgcm"
    # bcrypt 해시·검증을 이벤트 루프 밖 스레드에서 도는 동시 실행 상한(#289). 비인증 로그인과 인증된
    # 비밀번호 쓰기(본인 변경·관리자 생성·재설정)에 각각 적용한다 — 로그인 폭주가 비밀번호
    # 변경을 막지 않게. bcrypt는 GIL을 놓아 스레드마다 코어 하나를 쓰므로 vCPU−1 이하를 권한다.
    password_hash_concurrency: int = Field(default=4, ge=1)
    # 외부 MCP 도구 스키마를 프로바이더 변환이 펼칠 때의 크기 예산(바이트, #294). 운영
    # (`app_env=prod`)에서 넘는 도구를 노출하지 않는다. dev·staging은 이 값 대신 안전 상한
    # (`mcp_tool_spec.MCP_SCHEMA_EXPANSION_CEILING_BYTES`, 10 MB)만 쓴다. 운영도 안전 상한을 넘지
    # 않는다. 1 MiB는 Bedrock에서 십수만 토큰으로 펼쳐질 수 있다 — 컨텍스트가 작으면 낮춘다.
    mcp_schema_max_expanded_bytes: int = Field(default=1024 * 1024, gt=0)

    # 레이트 리밋 (per-key 분당 요청 수, 0 이하 = 해당 리밋 비활성)
    rate_limit_enabled: bool = True
    rate_limit_chat: int = 30  # 채팅 메시지 전송 — LLM 비용 직결
    rate_limit_source_create: int = 30  # URL/텍스트 소스 등록
    rate_limit_source_upload: int = 1000  # 파일 업로드(인증 사용자별)
    rate_limit_web_search: int = 30  # 웹 검색 (외부 프로바이더 호출)
    rate_limit_search: int = 120  # 전체/청크 검색
    rate_limit_source_content: int = 60  # 소스 파싱 본문 전문 조회(인증 사용자별)
    rate_limit_agent_run: int = 10  # 에이전트 실행 — LLM 비용 직결

    # 소스 업로드 최대 크기(바이트, #388). 0 이하·하드 상한(`SOURCE_MAX_UPLOAD_BYTES_CEILING`,
    # 500 MiB) 초과면 기동하지 않는다. 관리자 재정의(`PUT /admin/source-upload`)가 우선한다.
    source_max_upload_bytes: int = Field(
        default=50 * 1024 * 1024, gt=0, le=SOURCE_MAX_UPLOAD_BYTES_CEILING
    )
    # 노트북당 소스 상한(#445). 0 이하면 기동하지 않는다. 관리자 재정의
    # (`PUT /admin/notebook-source-limit`)가 우선한다. 낮춰도 기존 소스는 남고 추가만 거부한다
    max_sources_per_notebook: int = Field(default=1000, ge=1)
    # parsing 구간 heartbeat — (F+1)·interval + T + S + M ≤ 900 에서 유도한 상한(§5-7)
    parsing_heartbeat_sec: int = Field(default=30, gt=0, le=120)
    parsing_heartbeat_tick_timeout_sec: int = Field(default=10, gt=0, le=30)
    # agent-run SSE 구독 준비(SUBSCRIBE + ACK) 상한. Pub/Sub 풀은 read timeout이 없어
    # 응답 없는 서버 앞에서 무기한 대기하므로 이 단계만 따로 끊는다(#154 C).
    run_event_subscribe_timeout_sec: float = Field(default=5.0, gt=0, le=60)
    # terminal status를 발행한 run의 event sequence 키 보존 기간(#154 B). 만료 뒤 재접속은
    # snapshot만 받는다(`read_run_event_cursor_for_snapshot`).
    run_event_sequence_retention_sec: int = Field(default=86_400, gt=0, le=30 * 86_400)
    # agent run lease(#153 B) — (F+1)·interval + T < ttl(검증기). 워커가 F틱 연속 갱신에 실패해도
    # 그 전에 스스로 멈추고, 회수(ttl 만료)보다 늦게 쓰지 않는다
    agent_run_lease_ttl_sec: int = Field(default=120, gt=0, le=3600)
    agent_run_heartbeat_interval_sec: int = Field(default=30, gt=0, le=600)
    agent_run_heartbeat_tick_timeout_sec: int = Field(default=10, gt=0, le=60)
    agent_run_heartbeat_max_failed_ticks: int = Field(default=2, ge=0, le=10)
    # pending도 소비되지 않은 해결 승인도 없이 이만큼 머문 awaiting_approval은 회수한다
    agent_run_awaiting_stale_sec: int = Field(default=1800, gt=0, le=7 * 86_400)
    # 에이전트용 소스 요약(#360) — 목표 길이, 한 번에 요약하는 청크 묶음 크기(cl100k 토큰),
    # `source_digest` 노드가 동시에 만드는 소스 수(노드 `concurrency`의 상한·기본값)
    source_digest_target_tokens: int = Field(default=500, ge=100, le=4000)
    source_digest_group_tokens: int = Field(default=6000, ge=1000, le=200_000)
    source_digest_max_concurrency: int = Field(default=4, ge=1, le=16)
    # 인제스트가 끝난 소스(ready)의 요약을 미리 만든다(#378) — 첫 `source_digest` run이 생성을
    # 기다리지 않게. 켜면 소스마다 인제스천 큐에서 LLM 호출(map-reduce)이 돈다(노트북 소유자에게
    # 귀속). 꺼져 있으면 노드가 처음 쓸 때 만든다
    source_digest_on_ingest: bool = False
    # 소스가 많을 때 계층 요약(배치 reduce, #448). 담을 예산은 노드 `budget_tokens`, 없으면
    # 1차 모델 `context_window` × `source_digest_budget_window_ratio`다(창을 모르면 예산이 없어
    # 지금처럼 소스마다 담는다). 요약 합이 예산을 넘고 「예산 / 소스 수」가
    # `source_digest_min_tokens_per_source`보다 작거나 「예산 / 요약 합」이
    # `source_digest_shrink_summary_ratio`보다 작으면 글자 자르기 대신 배치 요약을 담는다. 배치
    # 요약은 Redis에 `source_digest_batch_cache_ttl_sec` 동안 캐시한다(휘발성 — 축출되면 다시
    # 만든다)
    source_digest_budget_window_ratio: float = Field(default=0.5, gt=0, le=1)
    source_digest_min_tokens_per_source: int = Field(default=150, ge=0, le=4000)
    source_digest_shrink_summary_ratio: float = Field(default=0.5, ge=0, le=1)
    source_digest_batch_cache_ttl_sec: int = Field(default=30 * 86_400, ge=60, le=365 * 86_400)
    # 한 `source_digest` run이 만드는 소스 요약 수 상한(#448). 0이면 무제한(기본, 지금과 같다).
    # 넘는 소스는 대체 본문(소스 가이드 요약·앞부분 발췌)을 넣고 `degraded`
    # (`generation_deferred`)에 남긴다
    source_digest_run_max_generate: int = Field(default=0, ge=0, le=100_000)
    # 기존 소스 요약 백필 태스크(#454) — 동시에 만드는 소스 수, 하루(표시 시간대 기준) 백필 전체의
    # 비용 상한(USD)·생성 소스 수 상한. 0이면 무제한이다. 상한에 닿으면 백필이 멈추고 진행률에
    # 이유를 남긴다(다시 실행하면 이어서 진행한다). 비용을 모르는 모델(단가 없음)은 0으로 쌓이므로
    # 그때는 개수 상한으로 막는다
    source_digest_backfill_concurrency: int = Field(default=2, ge=1, le=16)
    source_digest_backfill_daily_cost_usd: float = Field(default=0.0, ge=0, le=1_000_000)
    source_digest_backfill_daily_max_sources: int = Field(default=0, ge=0, le=10_000_000)
    # loop 항목 하나가 `results[i].output`으로 돌려줄 구조화 결과의 상한(JSON UTF-8 바이트, #361).
    # 넘으면 그 항목이 실패한다(`on_item_error`를 따른다)
    agent_loop_item_output_max_bytes: int = Field(default=256 * 1024, gt=0, le=16 << 20)
    # `save_sources` 노드가 한 실행에서 처리하는 항목 수(#403). 넘는 항목은 다음 실행으로
    # 넘긴다(중복 키로 이어진다)
    agent_save_sources_max_items: int = Field(default=200, ge=1, le=1000)

    @field_validator("display_timezone")
    @classmethod
    def _check_display_timezone(cls, value: str) -> str:
        if not is_timezone_name(value):
            raise ValueError(f"display_timezone은 IANA 시간대 이름이어야 합니다: {value!r}")
        return value

    @model_validator(mode="after")
    def _check_skill_script_wait(self) -> "CoreSettings":
        """대기 한도 > 실행 한도 + 전송 여유 30초 — 그 차이가 메시지 `expires`다(D7 "배달")."""
        if self.skill_script_wait_sec <= self.skill_script_timeout_sec + 30:
            raise ValueError(
                "skill_script_wait_sec는 skill_script_timeout_sec + 30보다 커야 합니다 "
                f"({self.skill_script_wait_sec} <= {self.skill_script_timeout_sec} + 30)"
            )
        return self

    @model_validator(mode="after")
    def _check_agent_run_lease(self) -> "CoreSettings":
        """(F+1)·interval + T < ttl — 갱신이 F번 실패해도 lease가 만료되기 전에 스스로 멈춘다."""
        f = self.agent_run_heartbeat_max_failed_ticks
        budget = (f + 1) * self.agent_run_heartbeat_interval_sec
        budget += self.agent_run_heartbeat_tick_timeout_sec
        if budget >= self.agent_run_lease_ttl_sec:
            raise ValueError(
                "agent run lease 설정이 맞지 않습니다: "
                f"(F+1)·interval + T = {budget} >= ttl {self.agent_run_lease_ttl_sec}"
            )
        return self

    def check_password_policy(self, plain: str, /, **context: Any) -> None:
        """제품 비밀번호 정책 훅(#174) — 코어 기본은 통과다.

        제품의 설정 하위 클래스가 덮어써서, 금지 값(공개 시드 기본 비밀번호 등)이면
        `core.security.PasswordPolicyError(reason, message)`를 던진다. `set_password`를 거치는
        모든 경로(본인 변경·관리자 생성·재설정·제품 CLI·시드)에 적용되고, 위반이면 아무것도
        저장되지 않는다. 라우트는 `422 invalid_new_password`로 응답한다. 설정 클래스를 등록하지
        않은 프로세스(제품 패키지를 import하지 않는 스크립트 등)에서는 코어 기본(통과)이 돈다.

        - **오버라이드는 `**context`를 받아야 한다** — 코어가 문맥 키를 더해도 깨지지 않게.
          키는 빠질 수 있으니 `context.get(...)`으로 읽는다. `set_password`가 넘기는 키:
          `email`(대상 사용자), `must_change`(True면 관리자가 정해 전달한 비밀번호 — 생성·재설정.
          재설정 임시 값은 `secrets.token_urlsafe(9)` 12자다). `**context`가 없는 오버라이드는
          모든 비밀번호 쓰기를 `TypeError`로 실패시킨다(제품 mypy가 잡는다).
        - 코어 길이 규칙(8자 이상·UTF-8 72바이트 이하)을 통과한 값만 받는다.
        - `reason`은 `^[a-z][a-z0-9_]{0,63}$`이고 코어 값(`too_short`·`too_long_bytes`·
          `same_as_current`·`policy`)과 겹치지 않아야 한다 — 어기면 `policy`로 바뀐다(거부는 유지).
        - `message`는 응답 `detail`로 나간다 — 입력값을 넣지 않는다(넣으면 일반 문구로 바뀐다).
        - 반환값은 None이어야 한다. 다른 값을 돌려주면 거부로 본다(fail closed).
        - 순수하고 빠른 검사여야 한다. 다른 예외는 그대로 올라간다(통과로 바꾸지 않는다).
        """

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def agent_http_allowed_host_list(self) -> list[str]:
        return [h.strip() for h in self.agent_http_allowed_hosts.split(",") if h.strip()]


#: prod에서 기본값 그대로 기동하면 안 되는 시크릿 필드.
SECRET_FIELDS: tuple[str, ...] = (
    "jwt_secret",
    "encryption_key",
    "postgres_password",
    "s3_access_key",
    "s3_secret_key",
)
#: `storage_backend=s3`일 때만 보는 시크릿 필드 — 로컬 스토리지는 자격 증명이 없다
S3_SECRET_FIELDS: frozenset[str] = frozenset({"s3_access_key", "s3_secret_key"})


def insecure_defaults_for(cls: type[CoreSettings]) -> dict[str, str]:
    """해당 설정 클래스의 시크릿 필드 기본값 (필드명 → 기본값).

    하드코딩하지 않고 클래스에서 파생시킨다. `postgres_password` 기본값은
    프로젝트마다 다르므로, 파생시켜야 서브클래스에서 자동으로 맞는다.
    """
    return {f: cls.model_fields[f].default for f in SECRET_FIELDS}


def find_insecure_defaults(settings: CoreSettings) -> list[str]:
    """기본값 그대로인 시크릿 필드 목록. S3 자격 증명은 s3 백엔드일 때만 본다."""
    skip = frozenset() if settings.storage_backend == "s3" else S3_SECRET_FIELDS
    return [
        f
        for f, default in insecure_defaults_for(type(settings)).items()
        if f not in skip and getattr(settings, f) == default
    ]


def assert_prod_secrets(settings: CoreSettings) -> None:
    """prod에서 기본 시크릿이면 기동 거부 (DEPLOYMENT.md §3 시크릿 발급 참조).

    dev/test/staging은 통과 — 호출부에서 경고 로그만 남긴다.
    """
    if settings.app_env != "prod":
        return
    insecure = find_insecure_defaults(settings)
    if insecure:
        raise RuntimeError(
            "프로덕션에서 기본 시크릿으로 기동할 수 없습니다. "
            f"다음 환경변수를 설정하세요: {', '.join(f.upper() for f in insecure)}"
        )


_settings_cls: type[CoreSettings] = CoreSettings


def set_settings_class(cls: type[CoreSettings]) -> None:
    """`get_settings()`가 인스턴스화할 설정 클래스를 등록한다.

    코어 모듈 다수가 import 시점에 `get_settings()`를 호출하므로, 등록은 그보다
    먼저 일어나야 한다. 이미 인스턴스가 캐시된 뒤의 등록은 그 모듈들이 코어
    기본값을 쥔 채 동작한다는 뜻이라 예외를 던진다 — 조용히 틀리는 것보다 낫다.
    """
    global _settings_cls
    if cls is _settings_cls:
        return
    if get_settings.cache_info().currsize:
        raise RuntimeError(
            f"get_settings()가 이미 호출된 뒤에는 설정 클래스를 {cls.__name__}으로 "
            "바꿀 수 없습니다. 프로젝트의 app/__init__.py에서 app.core.config를 "
            "먼저 import해 등록 순서를 보장하세요."
        )
    _settings_cls = cls


def reset_settings(cls: type[CoreSettings] | None = None) -> None:
    """캐시된 설정 인스턴스를 버린다 (테스트용)."""
    global _settings_cls
    get_settings.cache_clear()
    if cls is not None:
        _settings_cls = cls


@lru_cache
def get_settings() -> CoreSettings:
    return _settings_cls()
