# biznotes-core

`packages/core`는 biznotes 백엔드의 코어 라이브러리다. Python 패키지는 `biznotes_core`, 배포 이름은 `biznotes-core`다.
DB 모델·Pydantic 스키마·도메인 서비스·공통 라우터·Celery 앱과 태스크·스킬 스크립트 샌드박스 워커가 여기 있다.

제품 앱은 `apps/api`다. `apps/api`가 이 패키지를 uv 워크스페이스 멤버로 받아(`biznotes-core = { workspace = true }`)
설정 클래스를 등록하고, 라우터를 조립하고, 워커를 띄운다. 코어는 따로 릴리스하거나 태그를 붙이지 않는다 —
`apps/api`와 같은 커밋에서 함께 바뀌고, 의존성은 루트 `uv.lock` 하나가 고정한다. 출처는 루트 `ORIGIN.md`에,
스냅샷에서 뺀 기능과 바꾼 저장소 계층은 `docs/plans/biznotes-extract.md` §3·§9에 있다.

---

## 무엇이 들어 있나

| 경로 | 내용 |
|---|---|
| `biznotes_core/config.py` | 공통 설정 `CoreSettings`와 등록 함수(`set_settings_class`·`get_settings`) |
| `biznotes_core/core/` | DB·보안·암호화·레이트 리밋·SSRF 관문 같은 인프라 기반 |
| `biznotes_core/models/` | DB 스키마 정본(SQLAlchemy, 33개 테이블) |
| `biznotes_core/schemas/` | 공통 Pydantic 요청·응답 스키마 |
| `biznotes_core/services/` | 도메인 로직 |
| `biznotes_core/api/` | 공통 라우터, 인증 의존성(`deps`), 내부 스토리지 엔드포인트 |
| `biznotes_core/workers/` | Celery 앱·태스크·태스크 이름 상수, 스킬 샌드박스 워커(`skill_sandbox/`) |
| `docker/`·`tests/` | 샌드박스·Linux 시험 이미지 레시피와 export한 requirements, 단일 테스트 스위트 |

### `biznotes_core/core/` — 인프라 기반

| 모듈 | 역할 |
|---|---|
| `db` | 비동기 엔진·세션 팩토리(지연 생성, `get_engine()`) |
| `security`·`crypto` | JWT 발급·검증, bcrypt 해시, 비밀번호 규칙 / 비밀값 AES-GCM 암호화(`ENCRYPTION_KEY`) |
| `token_denylist`·`rate_limit`·`concurrency_gate` | 토큰 무효화, 고정 윈도우 분당 상한, 프로바이더 동시 요청 리스 — 모두 Valkey |
| `ssrf`·`http_body` | 외부 fetch의 단일 관문(아래), 응답 본문 상한 읽기와 gzip·deflate 묶음 해제 |
| `http_logging` | httpx 요청 로그의 URL을 출처(`scheme://host[:port]`)로 줄이는 필터 |
| `uow`·`metrics` | Unit of Work와 `after_commit` 콜백 / Prometheus 도메인 메트릭(`/metrics`) |
| `dumpable`·`timezones` | 워커 non-dumpable 설정(Linux) / 표시 시간대 변환 |

**`ssrf`**는 외부 fetch의 유일한 통로다(`ssrf_safe_fetch`). 리다이렉트를 직접 따라가며 홉마다 스킴·호스트·공인 IP를
다시 검증하고, **검증한 IP로 직접 연결하면서 원 호스트를 Host 헤더와 TLS SNI에 유지**한다 — 검증과 연결 사이의
DNS rebinding 창을 닫는다. 다른 출처로 넘어가는 홉에서는 `Authorization`·`Cookie`·`Proxy-Authorization`을 뺀다.
응답은 `Accept-Encoding: gzip, deflate`로 청하고(받아오기 전용은 `identity`) 원본·해제 누적을 모두 `max_bytes`로
묶어 푼다. 그 밖의 인코딩(`br`·`zstd`·중첩 등)은 본문이 있으면 `httpx.DecodingError`로 거부한다.

### `biznotes_core/models/` — DB 스키마 정본

사용자·그룹·노트북·공유·소스·청크·노트·태그·채팅·에이전트·실행·승인·MCP 서버·스킬·LLM·프롬프트·감사 로그·
사용 한도·재인덱싱·시스템 설정의 33개 테이블. 벡터는 청크 행에 있다 — `source_chunks.embedding`(`vector`, 차원
미지정)·`source_chunks.sparse`(`sparsevec`), 타입은 `models/vector_types.py`. Alembic 리비전은
`apps/api/alembic/versions/`에만 있다(pgvector 확장을 포함한 초기 스키마 하나). 모델을 바꾸면 같은 변경에서
`apps/api`에 리비전을 더하고 `make migrate-check`로 모델과 맞는지 확인한다.

### `biznotes_core/services/` — 도메인 로직

| 묶음 | 모듈 |
|---|---|
| 에이전트 실행 `agents/` | `runtime`(진입점)·`graph`(LangGraph DAG)·`context`(RunContext)·`nodes/`(llm·retrieval·composite·image·validation·digest·save_sources·source_list·artifact_tools)·`react/`(자율 루프·HITL 승인 게이트)·`tool_registry`·`mcp_client`·`mcp_exposure`·`policy`·`prompt_provenance`·`templating`·`chat_entry` |
| LLM 게이트웨이 `gateway/` | `llm`(폴백 체인·스트리밍)·`retry`·`circuit`(서킷 브레이커)·`discovery`(모델 가져오기)·`image`·`rerank`·`cost` |
| 인제스천 `ingestion/` | `admission`(업로드 관문)·`parsers`(pypdf·python-docx·python-pptx·openpyxl·trafilatura)·`chunker`·`embedder`(fastembed)·`enqueue`·`reindex`·`source_create`·`source_limits`·`upload_limits`·`source_warnings`·`stage_artifacts` |
| 스킬 `skills/` | `standard`·`package`(ZIP 검증·해시·저장)·`parser`(SKILL.md frontmatter)·`validator`·`builder`·`stages`·`runtime`·`service`·`sandbox`(스크립트 실행 요청)·`files`·`exposure`·`authoring`·`report`·`orphans` |
| 검색·채팅 | `rag_chat`·`vectorstore`(pgvector 하이브리드 검색)·`embedding_spaces`·`rerank`·`websearch`(SearXNG·Tavily) |
| 노트북·소스 | `notebooks`·`notebook_shares`·`notebook_series`·`overview`·`overview_refresh`·`source_guide`·`source_digest`·`object_cleanup` |
| 운영 | `authz`·`audit`·`agent_runs`·`agent_run_lease`·`events`(SSE·Valkey pub/sub)·`usage_guard`(일일 한도)·`rate_limit_settings`·`settings_store`·`llm_admin`·`admin_roster`·`user_passwords`·`prompts`·`task_publish`·`untrusted` |
| 스토리지 | `storage`(로컬 파일 기본)·`storage_s3`(선택) — 아래 「파일 스토리지」 |

런타임 파서 라이브러리는 `apps/api`가 선언하고 코어에서는 dev 그룹(테스트용)이다. 글자 층이 없는 스캔 PDF는 거부한다.

### `biznotes_core/api/`·`biznotes_core/workers/`

- `api/v1/`: `auth`·`notebooks`·`sources`·`search`·`tags`·`favorites`·`prompts`·`groups`·`skills`, `admin/`의 `users`·
  `llm`·`rag`·`skills`·`source_digest`·`audit`·`limits`. `api/v1/artifacts`는 라우터가 아니라 산출물 다운로드 응답
  함수(`artifact_response`)다. `api/internal_storage`는 샌드박스용 서명 URL 엔드포인트다.
- `workers/`: `celery_app`(앱·큐 라우팅·beat 스케줄)·`task_names`(태스크 이름 상수)·`runner`·`parsing_heartbeat`,
  `tasks/`의 `ingestion`·`overview`·`source_digest`, 스킬 스크립트 전용 워커 `skill_sandbox/`.

---

## 사용법

### 의존과 이미지

`apps/api/pyproject.toml`은 코어를 `biznotes-core[s3]`로 선언하고 `{ workspace = true }`로 받으므로, 호스트 개발에서는
코어 수정이 곧바로 반영된다. 이미지는 이 저장소에서 빌드하며 빌드 컨텍스트는 둘 다 저장소 루트다.

| 이미지 | Dockerfile | 코어가 들어가는 길 |
|---|---|---|
| api·worker | `apps/api/Dockerfile` | 같은 빌드의 `core-wheel` 스테이지가 wheel을 만들고, 제품 의존성 설치 뒤 `--no-deps`로 얹는다 |
| 스킬 샌드박스 워커 | `packages/core/docker/skill-sandbox.Dockerfile` | 코어 wheel(`--no-deps`) + `sandbox` 의존성 그룹. 스크립트용 시스템 파이썬에는 `sandbox-scripts` 그룹 |

이미지는 `uv.lock`이 아니라 export한 requirements로 설치한다. `uv.lock`을 바꾸면 루트에서 `make lock-docker`로
`apps/api/requirements.docker.txt`와 `packages/core/docker/requirements.sandbox*.txt`를 다시 만든다(워크스페이스 멤버는
빠진다). `make docker-lock-check`가 드리프트를 잡는다.

### 설정 — `CoreSettings` 상속

필드 선언은 전부 코어에 있고, 제품은 **기본값만** 덮어쓴다. 값은 환경 변수(대소문자 무관)와 `.env`에서 읽는다.

```python
# apps/api/app/core/config.py (발췌)
class Settings(CoreSettings):
    service_name: str = "biznotes"          # advisory lock 네임스페이스 접두사로도 쓰인다
    celery_app_name: str = "biznotes"
    celery_extra_task_modules: list[str] = ["app.workers.tasks.agent"]
    skill_manifest_filename: str = "biznotes.json"
    redis_url: str = Field(default="redis://localhost:26379/0", validation_alias=VALKEY_URL_ALIASES)

set_settings_class(Settings)
```

**등록은 코어 모듈 import보다 먼저** 일어나야 한다. 제품의 `app/__init__.py`가 `from app.core import config`로 먼저
불러 순서를 보장한다. 늦으면 `set_settings_class`가 예외를 던진다 — 조용히 코어 기본값을 쓰는 것보다 낫다.

| 항목 | 내용 |
|---|---|
| Valkey | 필드 이름은 `redis_url`, 환경 변수는 `VALKEY_URL`(별칭 `REDIS_URL`, 둘 다 있으면 `VALKEY_URL`). 클라이언트는 redis-py이고 캐시·Celery 브로커·이벤트 pub/sub이 같이 쓴다. 필드를 다시 선언할 때도 `validation_alias=VALKEY_URL_ALIASES`를 준다 — 빠뜨리면 별칭이 사라진다 |
| 운영 시크릿 | `APP_ENV=prod`에서 `JWT_SECRET`·`ENCRYPTION_KEY`·`POSTGRES_PASSWORD`(s3 백엔드면 `S3_ACCESS_KEY`·`S3_SECRET_KEY`도)가 기본값이면 api와 워커가 기동을 거부한다(`assert_prod_secrets`) |
| 표시 시간대 | `DISPLAY_TIMEZONE`(IANA 이름, 기본 `UTC`, 대소문자까지 시간대 DB와 같아야 한다). 일일 사용 한도의 「오늘」, 관리자 사용자 한도의 `today`, 소스 요약 백필의 일일 예산, `save_sources` 노트북 계열의 기본 시간대가 따른다. API가 주고받는 시각은 계속 시간대가 붙은 ISO 형식이다 |

### 앱 조립 — 제품이 직접 한다

`apps/api/app/main.py`가 `FastAPI()` 생성부터 미들웨어·보안 헤더·예외 핸들러·메트릭·헬스체크까지 조립하고, 코어
라우터를 제품 라우터와 섞어 `include_router`로 얹는다(`app/api/v1/router.py`·`app/api/v1/admin/__init__.py`). 등록
**순서도 제품이 정한다.** FastAPI가 그 순서로 OpenAPI 경로를 나열하므로, 코어가 한 덩어리로 조립하면 생성 타입
(`packages/shared-types`)에 기능과 무관한 diff가 생긴다. `biznotes_core.api.internal_storage.router`는 `/api/v1` 접두사
없이 붙인다.

> **`biznotes_core/app_factory.py`는 쓰이지 않는다.** 부팅 시퀀스를 코어로 모으려던 스캐폴딩이고, 참조하는
> `api/v1/router`·`core/exception_handlers`·`core/health`가 없어 동작하지 않는다. 제품 코드를 이 파일에 맞추지 말 것.

### 워커

코어 `celery_app`이 큐 라우팅과 beat 스케줄을 갖고, 제품에만 있는 태스크 모듈은 `celery_extra_task_modules`로 받는다.
제품은 `app/workers/celery_app.py`를 **shim으로 남긴다**(`from biznotes_core.workers.celery_app import celery_app, ping`) —
워커 진입점이 `app.workers.celery_app`이어야 `app/__init__.py`가 먼저 실행된다.

| 큐 | 태스크 |
|---|---|
| `ingestion`(기본 큐) | `app.workers.tasks.ingestion.*`(`ingest_source`·`index_source`·`reap_stalled_sources`), `overview.*`, `source_digest.*` |
| `agent` | `app.workers.tasks.agent.*`(`run_agent`·`reap_orphaned_runs`, 제품 모듈) |
| `skills.sandbox` | `biznotes_core.workers.skill_sandbox.run_skill_script` — 샌드박스 전용 워커만 소비한다 |

기본 compose는 워커를 **하나** 띄운다 — `celery -A app.workers.celery_app worker -Q ingestion,agent -B …`. beat가 이 워커
안에 있으므로 복제본은 하나만 두고, 큐별로 워커를 나눌 때도 `-B`는 한 곳에만 둔다. beat 작업은 5분마다 정체 소스
재발행(`reap-stalled-sources`)과 발행이 누락된 agent run 재발행(`reap-orphaned-runs`) 둘이다.

- 태스크는 완료 뒤 ack(`task_acks_late`)라 워커가 죽어도 잃지 않는다. 같은 소스가 두 번 처리될 수 있지만 색인 교체는
  잠금으로 직렬화된다(「재인덱싱」). 코어는 태스크 시간 제한을 걸지 않는다 — 필요하면 제품 워커에 `task_time_limit`을 둔다.
- 코어 `celery_app`을 import하면 httpx 요청 로그(INFO `HTTP Request: …`)의 URL이 출처로 줄어든다. 이 모듈을 거치지 않고
  root를 INFO 이하로 두는 프로세스는 `http_logging.install_httpx_log_redaction()`을 부른다. httpcore DEBUG 로그는 필터 밖이다.
- 워커 프로세스는 기동할 때 **non-dumpable**이 된다 — 같은 uid의 자식이 `/proc/<pid>/environ`에서 비밀을 읽지 못하게
  하려는 것이다. 같은 uid의 `py-spy`·`gdb` attach와 코어 덤프도 막히므로 디버깅에는 `CAP_SYS_PTRACE`를 가진 임시
  컨테이너를 쓴다.

### 파일 스토리지

호출부는 `biznotes_core.services.storage`의 모듈 함수(`put_object`·`get_object`·`open_object_stream`·`delete_object`·
`list_objects`·`presigned_url_for` 등)만 쓴다. 백엔드를 바꿔도 호출부는 그대로다.

| 설정(환경 변수) | 기본 | 뜻 |
|---|---|---|
| `STORAGE_BACKEND` | `local` | `local`은 `<데이터 디렉터리>/<버킷>/<키>` 파일, `s3`는 S3 호환 서버 |
| `BIZNOTES_DATA_DIR` | `data` | 로컬 백엔드 루트(상대 경로는 작업 디렉터리 기준). api 이미지는 `/data`이고 api·worker가 같은 볼륨을 본다 |
| `STORAGE_BUCKET_{SOURCES,PARSED,ARTIFACTS,SKILL_PACKAGES}` | `sources`·`parsed`·`artifacts`·`skill-packages` | 버킷 이름(로컬에서는 하위 디렉터리) |
| `S3_ENDPOINT`·`S3_ACCESS_KEY`·`S3_SECRET_KEY`·`S3_SECURE` | `localhost:9000`·빈 값·빈 값·`false` | s3 백엔드 전용 |

로컬 백엔드는 키의 절대 경로·`..`·`.`·빈 성분·역슬래시·NUL을 거부한다(`InvalidObjectKey`). 없는 오브젝트는 두 백엔드
모두 `ObjectNotFound`다. s3 백엔드는 `biznotes-core[s3]` extra(minio Python 클라이언트, `services/storage_s3.py`)가
필요하고, RustFS·SeaweedFS처럼 S3 API를 말하는 서버면 된다. `apps/api`는 extra를 함께 싣는다.

### 스킬 스크립트 샌드박스 워커

번들 스크립트 실행은 `SKILL_SCRIPT_BACKEND=process`일 때만 켜진다(기본 `disabled`, `container`는 미구현). 켜기 전에
전용 워커를 띄운다 — `docker compose --profile skill-scripts up -d worker-skill-sandbox`(`infra/compose.yml`).

- 환경 변수는 `SKILL_SANDBOX_BROKER_URL`·`SKILL_SANDBOX_STORAGE_ORIGINS` 둘뿐이고 코어 `celery_app`과 제품 설정을 불러오지
  않는다. 기동 가드가 소비 큐(`skills.sandbox` 하나)·동시성 1·root 아님·Landlock ABI ≥ 6·자격증명 모양의 환경 변수
  부재를 확인하고, 하나라도 어기면 기동하지 않는다. 작업은 최대 한 번 실행한다(late ack 없음).
- 런타임 기본 seccomp 프로필이 필요하다(k8s는 `seccompProfile: RuntimeDefault`). compose는 read-only 루트·
  `no-new-privileges`·`cap_drop: [ALL]`·tmpfs `/tmp`·internal 네트워크로 띄운다. 스크립트용 파이썬에는 `reportlab`·
  `pypdf`·`python-docx`·`python-pptx`·`openpyxl`과 나눔 글꼴이, `.js`·`.mjs`용으로 node 22가 있다.
- **파일은 서명 URL로만 오간다.** 샌드박스는 데이터 볼륨을 붙이지 않고 자격 증명도 없다. 에이전트 워커가 작업마다
  `presigned_url_for`로 URL을 만들어 보낸다. s3면 서버의 presigned URL이고, local이면 api 내부 엔드포인트
  `/internal/storage/<토큰>`이다 — 토큰은 객체 하나·메서드 하나·만료 시각(PUT은 본문 상한까지)을 HMAC으로 묶는다
  (키는 `JWT_SECRET`에서 파생).
- 내부 엔드포인트는 local일 때만 응답한다(s3면 404). GET은 스킬 패키지·산출물 버킷, PUT은 산출물 버킷만 허용하고,
  토큰이 어긋나면 이유 없이 403, 본문 상한을 넘으면 413이다. 서명할 주소는 `SKILL_SCRIPT_STORAGE_ENDPOINT`다(비우면
  local은 `API_BASE_URL`, s3는 `S3_ENDPOINT`. compose는 `http://api:8000`). 샌드박스의 `SKILL_SANDBOX_STORAGE_ORIGINS`가
  같은 출처여야 한다.
- 격리는 이미지 빌드 중에 잴 수 없다. 배포할 노드에서 `tests/test_skill_sandbox_isolation.py`를 한 번 돌린다(「Linux 시험 이미지」).

### 소스 본문 조회

소스 상세(`GET /api/v1/sources/{id}`)의 `parsed_preview`는 앞 20,000자다. 전문은 `GET /api/v1/sources/{id}/content`
(`text/markdown`, 스트리밍)로 받는다. 권한은 상세 조회와 같고(노트북 읽기 권한, 없으면 404), 파싱 전이거나 오브젝트가
지워졌으면 404, 스토리지 장애는 503이다. 레이트 리밋 키는 `rate_limit_source_content`다.

### 사용 제한 — 관리자 설정

관리 화면 「사용 제한」의 값은 `system_settings` 재정의다. 기본값은 env(설정)이고, 재정의를 지우면 env 값으로 돌아간다.
라우터는 `biznotes_core.api.v1.admin.limits_router`이고 제품이 `/admin` 집계 라우터에 `require_admin`을 걸어 include한다
(경로도 관리자를 다시 확인한다 — 일반 사용자 403). PUT은 부분 갱신이다 — 본문에 없는 키는 그대로, 명시적 `null`은
재정의 삭제다. 범위 밖 값은 422이고, 변경은 감사 로그(`detail = {changes, before, after}`)에 남는다.

| 설정 | API | 저장 키 | 규칙 |
|---|---|---|---|
| 분당 레이트 리밋 | `GET`·`PUT /admin/rate-limits` | `rate_limits` | 키는 `rate_limit_chat`·`rate_limit_source_create`·`rate_limit_source_upload`·`rate_limit_web_search`·`rate_limit_search`·`rate_limit_source_content`·`rate_limit_agent_run`, 재정의 값은 1~100,000. 0 이하(그 리밋 비활성)와 전체 끄기 `RATE_LIMIT_ENABLED`는 env 전용이다. 재정의는 프로세스마다 30초(`RATE_LIMIT_OVERRIDE_TTL_SECONDS`) 캐시한다 — 다른 프로세스는 최대 30초 늦게 반영한다 |
| 일일 사용 한도 기본값 | `GET`·`PUT /admin/usage-defaults` | `usage_defaults` | 필드 `daily_tokens`·`daily_agent_runs`, 값 0~2,147,483,647(0은 막음), null은 기본값 없음. 한도는 **사용자별 값 > 시스템 기본값 > 무제한**이고 관리자 역할은 검사에서 빠진다. 카운터는 Valkey 일일 키다. `GET`·`PUT /admin/users/{id}/limits` 응답에 `effective: {필드: {value, source}}`(`source` = `user`·`default`·`unlimited`)가 붙는다 |
| 소스 업로드 최대 크기 | `GET`·`PUT /admin/source-upload` | `source_upload` | env `SOURCE_MAX_UPLOAD_BYTES`(기본 50 MiB)가 기본값이다. 0 이하이거나 하드 상한 500 MiB(`SOURCE_MAX_UPLOAD_BYTES_CEILING`)를 넘으면 설정 검증이 실패해 기동하지 않는다. 재정의 `max_upload_bytes`는 1~하드 상한. 넘는 업로드는 413(`X-Admission-Code: too_large`)이다 |
| 노트북당 소스 상한 | `GET`·`PUT /admin/notebook-source-limit` | `notebook_source_limit` | env `MAX_SOURCES_PER_NOTEBOOK`(기본 1000)이 기본값이고 0 이하면 기동하지 않는다. 재정의는 1~2,147,483,647. 소스를 더하는 모든 관문(`ensure_source_capacity` 등)이 같은 값(`enqueue.notebook_source_limit`)으로 노트북 advisory lock 아래에서 판정하고, 넘으면 422 「노트북당 소스는 최대 {N}개입니다」다. 낮춰도 기존 소스는 남고 추가만 거부한다 |

인증 사용자는 `GET /sources/upload-limits`(`{max_upload_bytes, max_sources_per_notebook}`)로 추가 전에 거른다 — 판정과
같은 함수로 구한 값이고 캐시가 없다. 운영 프록시의 요청 본문 상한(nginx `client_max_body_size` 등)이 더 작으면 그 값이
실제 상한이므로, 업로드 상한을 올리면 프록시 상한도 함께 올린다.

### 재인덱싱

재인덱싱은 **저장된 파싱 결과(`parsed_object_key`)에서 시작한다.** 원본(웹 URL)을 다시 가져오지 않으므로, 원격이 막혀도
멀쩡한 소스가 실패로 떨어지지 않는다.

```jsonc
POST /api/v1/admin/rag/reindex
{ "notebook_id": null,    // null = 전체
  "failed_only": false,   // 실패한 소스만
  "stale_only": false,    // 현재 임베딩 모델·청킹 설정과 다르게 색인된 ready 소스만
  "refetch": false }      // true면 원본부터 다시 파싱 (원문 갱신·파서 개선 반영)
→ { "enqueued": 12, "skipped": 3, "run_id": "…" }   // skipped = 이미 대기·처리 중
```

진행률은 `GET /admin/rag/reindex`(최근 요청 목록)·`GET /admin/rag/reindex/{run_id}`(`requested`·`queued`·`done`·`failed`·
`removed`·`retrying`·`finished_at` 등)·`GET /admin/rag/index-status`(노트북별 `reindex_pending`·`stale`)로 본다.

- 소스 `status`는 바뀌지 않는다 — 대기 중에도 `ready`로 남아 검색·소스 선택에서 빠지지 않는다. 같은 요청을 다시 보내면
  이미 대기·처리 중인 소스는 `skipped`로 세고 다시 발행하지 않는다.
- run이 15분간 멈추면 `reap_stalled_sources`가 남은 항목을 다시 발행한다. 처리 중인 소스는 건드리지 않고, 항목당
  3회(`MAX_REINDEX_ATTEMPTS`)까지 재발행한 뒤 실패로 닫는다. 다른 소스가 처리되는 중이면 회수를 미루되, 60분(회수 창
  × 4) 넘게 갱신이 없는 run은 흐름과 무관하게 재발행한다.
- 색인을 건드리기 전에 실패하면 **이전 색인을 유지**하고 `ready`로 둔다. 그 안내는 `error_message`(실패 전용)가 아니라
  `sources.warnings` 코드 `reindex_kept_previous`(이전 색인 유지)·`reindex_deferred`(임베딩 일시 오류로 미룸)다.
- 이미 `failed`인 소스는 `{"failed_only": true}`로 파싱 결과에서 복구된다. 파서를 고친 뒤 기존 소스에 반영하려면
  `refetch: true`가 필요하다.

**같은 소스의 인제스천이 겹쳐도 색인은 하나로 남는다.** 색인 교체(청크 행과 그 벡터 열)는 한 트랜잭션이고 소스 단위
advisory lock으로 직렬화된다. 늦게 온 쪽은 최대 5분(`INDEX_LOCK_TIMEOUT`) 기다리고, 넘으면 상태를 쓰지 않고
`index_lock_busy`로 물러난다(reaper가 회수한다). 교체가 실패하면 롤백되어 이전 색인이 남는다. 벡터가 청크 행에 있으므로
소스를 지우면 CASCADE로 함께 사라지고, 따로 회수할 고아 벡터가 없다.

### 임베딩 모델 교체

벡터는 **활성 임베딩 모델**로 만든다. 어드민에 기본 `embedding` 모델이 없으면 로컬 fastembed
(`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`)가 활성 공간이다 — API 키 없이 동작하는 기본값이다.
소스는 자기 벡터를 만든 공간 키를 `sources.embedding_model`에 기록한다(NULL은 로컬 기본 모델).

`embedding` 열은 차원을 고정하지 않는다. 검색은 노트북(필요하면 소스)과 공간으로 먼저 걸러 낸 행에서만 거리를 재므로
차원이 다른 공간이 섞이지 않는다. 인덱스 없는 정확 탐색이고, dense(코사인)·sparse(BM25 내적) 후보를 SQL 안에서 RRF로
합친다(`services/vectorstore.py`). 모델 교체는 재인덱싱이 소스의 청크 행(벡터 열 포함)을 새 모델로 다시 쓰는 것이다.
상태는 `GET /admin/rag/embedding`(`active_key`·`spaces[]`(`key`·`active`·`available`·`source_count`)·`stale_sources`)이다.

질의는 소스가 속한 공간마다 **그 공간의 모델로** 한다. 그래서 교체 도중에도 기존 소스가 검색에서 빠지지 않는다. 단
순서를 지켜야 한다.

1. 기존 모델 **비활성화**(`PATCH /admin/llm/models/{id}` → `enabled: false`)
2. 새 모델 등록(`POST /admin/llm/models`) — 활성 임베딩 모델이 남아 있으면 409
3. `POST /admin/llm/models/{new_id}/set-default`
4. `POST /admin/rag/reindex` `{"stale_only": true}` → `stale_sources`가 0이 될 때까지
5. 기존 모델 삭제

기존 모델을 **먼저 지우면** 그 모델로 만든 벡터를 재인덱싱이 끝날 때까지 질의할 수 없다(`available: false`로 드러난다).

### 채팅 — 메시지 순서·소스 범위·인용 번호·필수 초기 검색

- **메시지 순서는 `chat_messages.seq`(IDENTITY)로 정렬한다.** `created_at`은 트랜잭션 시작 시각이라 한 트랜잭션에 넣은
  질문·답변이 같은 값을 갖고, 트랜잭션이 겹치면 순서가 뒤집히기까지 한다 — `.order_by(ChatMessage.seq)`.
- **소스 범위(`source_ids`)는 세 상태다.** `None`은 노트북 전체, `[]`는 선택한 소스 없음(검색·로드하지 않고
  `insufficient_context=True`), 목록은 그 소스만이다. `retrieve_context`·`rag_search`·`source_load`·`stage_agent_run`이 같은
  규칙을 따른다. `failed` 소스는 청크가 남아 있어도 근거에서 빠진다(재인덱싱 중인 소스는 `ready`이므로 남는다).
- **발췌 수(`top_k`)를 생략하면 어드민 RAG 설정(`rag.top_k`)을 따른다.** react 도구 인자, 워크플로 `rag_search` 노드,
  에이전트 정의, 스킬 매니페스트가 같은 규칙이고 값은 1~30으로 제한된다. 리랭킹을 켜면 후보는 최소한 `top_k`만큼
  가져온다(`rerank_candidates`가 더 작아도 발췌 수가 줄지 않는다).
- **인용 번호는 run 단위다.** 한 run에서 `rag_search`를 여러 번 불러도(스킬 서브에이전트 포함) 같은 청크는 같은 번호, 새
  청크는 다음 번호를 받는다. `ctx.tool_evidence`의 `citation.n`이 모델이 본 `[n]`과 같으므로 재번호 없이 저장하면 된다.
- **웹 검색 근거**: `web_search`는 호출마다 `{kind: "web_search", status, result_count}`, 모델에게 보여 준 결과마다
  `{kind: "web", url, title, snippet}`을 evidence로 남긴다. 결과 항목은 항상 자기 호출 항목 바로 뒤에 붙는다.

**모델이 검색을 건너뛰지 못하게 하려면** react 노드에 `required_initial_tools`를 둔다.

```json
{"type": "react_agent", "goal": "{chat_message}",
 "allowed_tools": ["rag_search"], "required_initial_tools": ["rag_search"]}
```

첫 모델 턴 전에 루프가 발화 원문으로 `rag_search`를 실행하고 결과를 `role="tool"` 메시지로 넣는다. `tool_calls`와
`react_tool_call` 감사 행에 `required_initial: true`가 붙고, 검색이 실패하면 노드가 실패한다. 지원 도구는 `rag_search`뿐이다.

### MCP 호출자 컨텍스트 — `_meta`

코어 실행 경로(react 디스커버리·llm 노드 `tools:["mcp"]`·`mcp_call`·`mcp_tool` 노드)는 `tools/call`의 `params._meta`에 run
컨텍스트를 싣는다. 키는 `services/agents/mcp_client.py`의 공개 상수이고, 바꾸면 이 키를 읽는 MCP 서버도 함께 바꿔야 한다.

| 키 | 값 | 보내는 서버 |
| --- | --- | --- |
| `MCP_META_RUN_ID` (`io.github.dallisoft.biznotes/run_id`) | run 식별자 | 모든 서버 |
| `MCP_META_USER_ID` (`io.github.dallisoft.biznotes/user_id`) | run 실행 사용자 | **stdio만**. 시스템 실행이면 없음 |

사용자별 데이터를 돌려주는 서버는 인자의 사용자 id 대신 `_meta`의 사용자로 조회한다(FastMCP라면
`ctx.request_context.meta`의 `model_extra.get(MCP_META_USER_ID)`). 사용자 키가 없을 때 거부할지는 서버가 정한다.

**신뢰 범위**: 이 값은 코어가 주장하는 값이고 서명하지 않는다. **인가 근거로 쓸 수 있는 것은 코어가 호출마다 띄우는 stdio
자식뿐이다.** 명령이 원격 브리지(`mcp-remote`·`ssh` 등)면 신뢰는 그 브리지의 전송으로 넘어간다. sse·streamable_http는 워커
안의 LLM 제어 egress(`ssrf_allow_private=true`의 `http_request`·`http` 노드)로 위조될 수 있어 사용자 id를 보내지 않는다.
`run_id`는 감사·상관용이며 `AgentRun` 행이 있다고 보장하지 않는다.

### 에이전트 워커 외부 연결 정책

에이전트가 코어 HTTP 관문으로 내보내는 요청(`http_request` 도구, `http` 노드와 이를 펼치는 tool·plugin kind)을 「받아오기
전용」으로 묶을 수 있다.

| 설정 | 기본 | 뜻 |
| --- | --- | --- |
| `AGENT_HTTP_EGRESS_MODE` | `open` | `open`은 제한 없음, `fetch_only`는 아래 규칙 |
| `AGENT_HTTP_ALLOWED_HOSTS` | (빈 값) | 쉼표 구분 허용 호스트(`*.example.com`은 하위 도메인만). 비면 제한 없음. **두 모드 모두** 리다이렉트 홉마다 본다 |
| `AGENT_HTTP_MAX_URL_CHARS`·`AGENT_HTTP_MAX_QUERY_CHARS` | 2048·512 | `fetch_only`의 URL·쿼리 길이 상한(인코딩 기준) |

- `fetch_only`: GET·HEAD만, 요청 본문 없음, 헤더는 `Accept`·`Accept-Language`·`If-None-Match`·`If-Modified-Since`·`Range`만
  (`User-Agent`는 코어 고정값으로 덮는다), URL userinfo 없음, 길이 상한. 위반은 **DNS 질의·네트워크 호출 전에** 거부하고,
  리다이렉트 뒤에도 원 메서드를 유지한다. 응답은 `identity`로 청하고, 압축으로 오면 풀기 전에 거부한다.
- `http` 노드 정의는 `method`를 GET·HEAD로 **명시**해야 한다(생략하면 기본 POST라 거부). `body`, 허용 밖 헤더, 템플릿(`{`)
  헤더 값, 리터럴이 아닌 url 대상(scheme·호스트·포트)은 거부한다. 저장 시에는 `find_direct_node_approval_violations`가 사유를
  더하고, 실행 시에 다시 본다. 순수 헬퍼는 `policy.find_http_egress_violations(nodes, policy=agent_egress_policy())`다.
- `http_request` 도구는 스펙에서 `body`가 빠지고 `method`가 `GET`·`HEAD` enum이 된다. HITL 분류는 두 모드 모두 부작용이다 —
  자동 실행은 `auto_approve_tools.http_request.domains`로 한다.
- 거부는 경고 로그 `agent_http_blocked`(method·호스트·사유만)와 지표 `agent_http_blocked_total{reason}`으로 남는다. 사유:
  `method`·`body`·`header`·`userinfo`·`url_too_long`·`query_too_long`·`host_not_allowed`·`non_literal_target`·`encoded_response`.
- **API와 워커에 같은 값을 둔다.** API는 저장 검증, 워커는 실행 시 거부에 쓴다(API `open` + 워커 `fetch_only`면 저장은 되고
  실행이 거부된다). 워커가 모드를 명시하지 않으면 기동 때 `agent_http_egress_mode_unset`을 한 번 경고한다.
- 게이트웨이(등록 프로바이더)·MCP·`web_search`·웹 소스 인제스트는 정책 대상이 아니다. URL·DNS 라벨로 값을 실어 보내는 채널과
  관문 밖 경로(MCP stdio 자식 등)는 남으므로, 워커 egress는 네트워크 정책·egress 프록시로 허용 목적지에 묶는다.

### 이미지 생성 — `image_generate` 노드

관리자가 `image` 능력 모델을 등록하면(모델 가져오기가 이름·출력 모달리티로 분류한다) 워크플로가 이미지를 run 산출물로
만든다. `image`는 단독 능력이라 채팅에도 쓰는 모델은 이미지용 행을 따로 등록한다. 장당 가격은 `cost_per_1k.per_image`(USD)다.

```json
{"id": "art", "type": "image_generate", "prompt": "{params.slide.heading}: {desc.text}",
 "name": "slide-{params.index}", "n": 1, "aspect_ratio": "16:9"}
```

- 반환은 `{key, keys, names, images: [{key, name, content_type, bytes}], count}`다. 산출물은 PNG·JPEG만 저장하고 확장자는
  바이트에서 정한다. `n`이 2 이상이면 `<name>-1.png`처럼 번호를 붙이고, 이름에 경로 구분자·`..`·제어 문자가 있으면 실패한다.
- 상한(모든 환경): 장수 1~4, `size` `1024x1024`·`1536x1024`·`1024x1536`(또는 `aspect_ratio` `1:1`·`3:2`·`16:9`·`2:3`·`9:16`),
  프롬프트 4000자, 산출물 장당 20 MiB. 넘으면 프로바이더를 부르기 전에 거부한다. 장수 상한은 노드 한 번에만 걸리므로 `loop`
  본문에서는 `loop`의 `max_items`(정수 또는 템플릿)로 묶는다 — 넘는 항목은 잘리고 반환에 `truncated`·`total`이 실린다.
- **비신뢰 경계**: 이미지 API에는 system 메시지가 없다. 그래프 구성 시점에 프롬프트가 출처가 안전한 값만 참조하는지
  본다(`prompt_provenance`) — 검색·소스·HTTP·MCP 출력이나 `|untrusted` 값을 참조하면 어떤 노드도 돌기 전에 run이 실패한다.
  실행 시점에는 렌더 결과가 `untrusted_data` 블록을 담으면 실패한다. 소스 본문은 앞 `llm` 노드가 그림 묘사로 바꿔 쓴다.
- 프로바이더가 URL로 돌려주면 SSRF 관문(공인 IP만, 장당 상한)으로 받는다. 사용량은 `usage["images"]`, 비용은
  `per_image × 장수`다(`per_image`가 없으면 응답 토큰 × `input`/`output` 단가). 사용자별 일일 이미지 한도는 없다.

---

## 지켜야 할 규칙

### 코어는 제품을 import하지 않는다

확장은 `CoreSettings` 상속, 제품의 라우터 조립, 기존 레지스트리(`tool_registry`·`skills`)로만 한다.
`tests/test_boundary.py`가 파일마다 `app` 패키지 import를 검사하고, 코어 안의 계층 방향도 강제한다(같은 계층 안은 허용).

| 계층 | import해도 되는 계층 |
|---|---|
| `config` → `core` → `models` → `schemas` | 각자 앞의 계층들 |
| `workers.task_names` | 표준 라이브러리만 |
| `services` | `config`·`core`·`models`·`schemas`·`task_names`·`celery_app` |
| `workers`·`api` | 위 전부 + `services` |

서비스·라우터는 태스크 모듈을 import하지 않고 이름으로 발행한다(`services/task_publish.py`).

### import 시점에 설정을 읽지 않는다

```python
settings = get_settings()      # ✗ 모듈 최상위
def f(): get_settings().x      # ✓ 사용처
```

제품이 설정 클래스를 등록하기 전에 실행되면 **코어 기본값이 조용히 굳는다.** `events.py`가 import 시점에 pub/sub 연결 풀을
만들었을 때는 발행과 구독이 서로 다른 DB 번호를 봐서 **오류가 아니라 무한 대기**로 나타났다. 모듈 레벨 객체가 꼭 필요하면
`@lru_cache` 팩토리로 지연시킨다(`core/db.py`의 `get_engine()`). `tests/test_no_import_time_settings.py`가 강제한다 — 예외는
`workers/celery_app.py` 하나다(Celery 앱은 모듈 레벨 객체여야 하고, 진입점 shim이 등록 순서를 보장한다).

### Celery 태스크 이름은 브로커 계약이다

```python
@celery_app.task(name="app.workers.tasks.ingestion.ingest_source")   # 이름은 유지
```

모듈이 코어로 와도 **이름 문자열은 그대로 둔다.** 바꾸면 큐에 남은 메시지가 라우팅되지 않는다. 이름 상수는
`workers/task_names.py`에 모여 있다. 반면 **`conf.imports`의 경로는 실제 위치**를 따라야 한다 — 둘을 혼동하면 워커가 태스크를
찾지 못하거나 재시작 루프에 빠진다.

### 제품마다 다른 값은 설정으로 올린다

포트·DB명·Celery 앱 이름·스킬 매니페스트 파일명·advisory lock 네임스페이스(`service_name`)가 그렇다. 코어에 하드코딩하지 않는다.

---

## 개발

명령은 `uv run …`이고 워크스페이스 공용 venv(루트 `.venv`)를 쓴다. 저장소 루트에서 `make up`(PostgreSQL(pgvector)
25433·Valkey 26379를 포함한 개발 스택)을 띄운 뒤 `make core-test`(ruff → mypy → pytest(PG 테스트·경계 규칙 포함) → wheel
빌드 `dist/core`)를 돈다. `make ci`(`ci-backend`)도 `core-test`를 돈다. `packages/core` 안에서 나눠 돌릴 수도 있다.

```bash
cd packages/core
uv run ruff check . && uv run mypy biznotes_core
uv run python -m pytest -q             # 전체(PG·Valkey 필요)
uv run python -m pytest -q -m "not pg" # PG 테스트를 빼고(서버 불필요)
```

`uv run pytest`가 아니라 `uv run python -m pytest`를 쓴다 — 콘솔 스크립트의 shebang이 지금 venv와 어긋나 다른 인터프리터로
도는 일을 막는다.

**테스트는 단일 스위트다.** 대부분은 외부 경계(DB·스토리지·벡터 검색)를 기록형 가짜 객체로 대신하는 단위 테스트다. 파일
이름이 `_pg.py`로 끝나거나 `pytest.mark.pg`가 붙은 테스트는 실제 PostgreSQL(pgvector)과 Valkey를 쓴다. 실행마다
`core_test_<pid>_<난수>` DB를 만들어 `CREATE EXTENSION vector` + `create_all`을 하고(Alembic 검증이 아니다 — 그것은
`make migrate-check`) 끝나면 지우며, 테스트마다 전 테이블을 비운다. 서버에 닿지 않으면 종료 코드 1로 끝난다(「0개 통과」가
초록으로 보이지 않게). 스위트는 개발자 `.env`를 읽지 않고, `BIZNOTES_DATA_DIR`은 임시 디렉터리다.

| 환경 변수 | 기본(`make up`) |
|---|---|
| `CORE_TEST_PG_HOST`·`CORE_TEST_PG_PORT` | `127.0.0.1`·`25433` |
| `CORE_TEST_PG_USER`·`CORE_TEST_PG_PASSWORD` | `biznotes`·`biznotes` (CREATE DATABASE 권한 필요) |
| `CORE_TEST_VALKEY_URL` | `redis://localhost:26379/9` (DB 번호를 그대로 쓰고 FLUSHDB하지 않는다) |

`apps/api`가 의존하는 코어 계약은 코어 테스트로 고정하고, 제품 통합은 `apps/api` 테스트가 본다.

### Linux 시험 이미지 — 샌드박스 격리 실측

스킬 스크립트 샌드박스의 격리 체인(setrlimit → no-new-privileges → Landlock → seccomp → close_range → execve)은 Linux
커널에서만 잴 수 있다. `tests/test_skill_sandbox_isolation.py`는 macOS나 요건이 모자란 커널에서는 **통째로 skip**된다. 아래
이미지로 Linux에서 돌린다. `SKILL_SCRIPT_BACKEND=process`를 켜기 전에 배포할 노드에서 한 번 돌려 본다.

| 파일 | 역할 |
| --- | --- |
| `docker/test.Dockerfile` | 시험 이미지 레시피. Python 3.12(`/venv`, 버전은 `uv.lock`의 본 의존성과 dev 그룹), node 22, uv 0.12.17. 발행하지 않는다 |
| `docker/test/linux-run.sh` | 이미지 안의 `/run.sh`. 마운트한 작업 트리를 복사해 `.pth`로 얹고, 비특권 uid와 no-new-privs로 내려가 `/venv/bin/python "$@"`를 실행한다 |

빌드 컨텍스트는 다른 이미지처럼 저장소 루트다(워크스페이스 `uv.lock`이 루트에 있다). 이미지는 의존성만 담고(`uv.lock`이나
코어 의존성이 바뀌면 다시 만든다), 코드·테스트는 실행할 때 읽기 전용으로 마운트한다.

```bash
# 저장소 루트에서
docker build -f packages/core/docker/test.Dockerfile -t biznotes-core-test:py312-node .

# 1) 커널 요건 확인 — 6 이상이어야 한다(0이면 Landlock 없음, 아래 요건 참고)
docker run --rm --security-opt no-new-privileges:true -v "$PWD/packages/core:/src:ro" biznotes-core-test:py312-node \
    /run.sh -c "from biznotes_core.workers.skill_sandbox import launcher; print(launcher.DefaultSyscalls().landlock_abi())"

# 2) 격리 시험 — 기대 결과: 전부 passed, skip 0
docker run --rm --security-opt no-new-privileges:true -v "$PWD/packages/core:/src:ro" biznotes-core-test:py312-node \
    /run.sh -m pytest tests/test_skill_sandbox_isolation.py -q -p no:cacheprovider -rs
```

`tests/` 전체를 Linux에서 돌리려면 2)의 경로를 `tests/ -m "not pg"`로 바꾼다(컨테이너 안에서는 `make up`의 PG에 닿지 않는다).

요건과 함정:

- **커널**: Linux, Landlock ABI ≥ 6(코드 상수 `REQUIRED_LANDLOCK_ABI`, 커널 6.12 이상)이 켜져 있어야 한다
  (`CONFIG_SECURITY_LANDLOCK=y`이고 부팅 LSM 목록에 `landlock` 포함). 1)이 6 미만이면 시험 전체가 skip된다. Docker
  Desktop(macOS)의 LinuxKit 커널에서 ABI 8을 확인했다.
- **네이티브 아키텍처로 돌린다**: arm64 호스트는 arm64, amd64 호스트는 amd64 이미지로. 다른 아키텍처를 `--platform`으로
  에뮬레이션하면 Landlock·seccomp를 잴 수 없다(ABI가 0으로 보여 skip된다).
- **seccomp는 런타임 기본 프로필 그대로 둔다**. `--privileged`나 별도 프로필로 바꾸지 않는다. 기본 프로필이 Landlock 시스템
  콜을 막는 오래된 Docker면 ABI가 0으로 보인다.
- **uid**: `/run.sh`는 시험 전용 uid 10999로 실행한다. `RLIMIT_NPROC`(시험은 nproc=64)는 컨테이너가 아니라 커널 전체에서
  같은 uid의 스레드를 센다 — 같은 호스트에 uid 1000(api 이미지의 `appuser`)으로 도는 컨테이너가 있을 때 uid 1000으로
  돌리면 node 시험이 스레드를 못 만들어 SIGABRT로 실패한다. 바꿔야 하면 `-e RUN_UID=<uid>`.
