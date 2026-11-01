# 아키텍처

biznotes의 구성 요소, 코드 배치, 주요 흐름을 적는다. 설치·운영은 [README](../README.md), 개발 절차는 [CONTRIBUTING](../CONTRIBUTING.md)을 본다.

## 1. 실행 구성

`infra/compose.yml` 하나가 기본 6개 서비스와 선택 1개를 띄운다. 이미지는 저장소 루트를 빌드 컨텍스트로 쓰는 3개(api·web·skill-sandbox)이고, worker는 api 이미지를 명령만 바꿔 쓴다.

| 서비스 | 이미지 | 하는 일 | 네트워크 | 볼륨 |
|---|---|---|---|---|
| postgres | `pgvector/pgvector:pg16` | 관계 데이터 전부 + 청크 벡터(pgvector) | backend | `pgdata` |
| valkey | `valkey/valkey:8-alpine` | Celery 브로커, SSE pub/sub, 레이트 리밋·토큰 거부 목록·동시성 게이트·일일 한도 카운터, 캐시·락 | backend, skill_isolated | `valkey_data` |
| searxng | `searxng/searxng`(날짜 태그 고정) | 웹 검색(에이전트 도구·검색 API) | backend | `searxng_data` |
| api | `biznotes/api` | FastAPI. 기동 시 `alembic upgrade head`(`BIZNOTES_AUTO_MIGRATE`) 뒤 uvicorn | backend, skill_isolated | `data`, `model_cache` |
| worker | `biznotes/api` | Celery 워커 1개가 `ingestion`·`agent` 큐를 함께 소비하고 beat(주기 작업)를 내장한다 | backend | `data`, `model_cache` |
| web | `biznotes/web` | Next.js(standalone). 브라우저는 api를 직접 부르고(`NEXT_PUBLIC_API_BASE_URL`), 서버 렌더링은 `API_INTERNAL_URL`로 부른다 | backend | — |
| worker-skill-sandbox | `biznotes/skill-sandbox` | `skill-scripts` 프로파일. 스킬 스크립트만 실행하는 격리 워커(`skills.sandbox` 큐) | skill_isolated(내부 전용) | 없음 |

`skill_isolated`는 `internal: true` 네트워크라 샌드박스는 외부 인터넷과 postgres에 닿지 않고 valkey(작업 수신)와 api(파일 주고받기)만 본다. 개발 오버레이 `infra/compose.dev.yml`은 호스트 포트(web 23000·api 28000·searxng 28080·PG 25433·Valkey 루프백 26379)와 api 코드 핫리로드를 더한다.

상태는 PostgreSQL·Valkey 두 저장소와 `data` 볼륨(파일)에만 있다. `model_cache`는 fastembed·Hugging Face 모델 캐시라 지워도 다시 받는다.

## 2. 코드 배치

```
packages/core/biznotes_core/   백엔드 코어 라이브러리 — 모델·스키마·서비스·공통 라우터·워커
apps/api/app/                제품 앱 — 설정 상속, 라우터 조립, 제품 전용 라우터·서비스·태스크
apps/api/alembic/            마이그레이션(초기 리비전 0001 하나에서 시작)
apps/web/                    Next.js 프론트엔드
packages/shared-types/       api OpenAPI에서 생성한 TypeScript 타입(웹이 소비)
infra/                       compose, SearXNG 설정, smoke 스크립트
skills/                      샘플 스킬 패키지(SKILL.md 형식)
```

- 코어(`biznotes_core`)는 개별 라우터·서비스·태스크를 내보내고, **조립은 제품이 한다**. `apps/api/app/api/v1/router.py`가 코어 라우터와 제품 라우터(chat·notes·agents·workflows 등)를 한 API로 묶는다.
- 제품 설정 `apps/api/app/core/config.py`의 `Settings`가 코어 `CoreSettings`를 상속해 제품 값(DB 이름, 큐에 더할 태스크 모듈 등)을 정한다. 코어는 import 시점에 설정을 읽지 않는다.
- 웹과 api의 계약은 백엔드 Pydantic 스키마가 정본이다. 응답을 바꾸면 `pnpm gen:types`로 `packages/shared-types`를 다시 만들고, CI(contracts)가 드리프트를 막는다.

### 코어 계층 규칙

코어는 제품 패키지(`app.*`)를 import하지 않는다. 계층 간 import 방향은 아래 표를 따르고(같은 계층 안은 자유), `packages/core/tests/test_boundary.py`가 함수 안 지연 import까지 검사한다. 표를 바꾸면 그 테스트의 `LAYER_ALLOWED`도 함께 고친다.

| 계층 | import 허용 대상 |
|---|---|
| `config` | 표준·서드파티 라이브러리 |
| `core/` | `config` |
| `models/` | `config`, `core` |
| `schemas/` | `config`, `core`, `models` |
| `workers/task_names.py` | 표준 라이브러리만 |
| `services/` | `config`, `core`, `models`, `schemas`, `workers.task_names`, `workers.celery_app` |
| `workers/`(그 외) | `config`, `core`, `models`, `schemas`, `services` |
| `api/v1/` | `config`, `core`, `models`, `schemas`, `services`, `workers.task_names`, `workers.celery_app` |

서비스·라우터는 태스크 모듈을 import하지 않고 `task_names`의 이름으로 발행한다. Celery 태스크 이름은 브로커에 남는 계약이라 바꿀 때는 옛 이름을 함께 등록해 큐를 비운 뒤 지운다.

## 3. 주요 흐름

### 소스 인제스트

1. 업로드(PDF·DOCX·PPTX·XLSX·TXT·MD), URL, 붙여 넣은 텍스트가 api에 들어오면 관문이 형식·크기·노트북당 상한을 검사하고, 원본을 저장소 `sources` 버킷에 두고 `sources` 행을 만든 뒤 `ingestion` 큐에 작업을 넣는다.
2. worker가 파싱한다(pypdf·python-docx·python-pptx·openpyxl, 웹 페이지는 trafilatura). 글자 층이 없는 PDF(스캔본)는 지원하지 않는다고 거부한다. 파싱 결과는 `parsed` 버킷에 둔다.
3. 청커가 본문을 나누고, 임베더(fastembed)가 청크마다 dense 벡터(`paraphrase-multilingual-MiniLM-L12-v2`, 384차원)와 sparse 벡터(BM25)를 만든다. 둘 다 `source_chunks` 행의 `embedding vector`·`sparse sparsevec` 열에 들어간다 — 색인 교체가 한 트랜잭션이다.
4. 소스가 `ready`가 되면 진행 이벤트가 Valkey pub/sub → SSE로 웹에 간다. 설정이 켜져 있으면 소스 요약(digest)도 만든다.

소스·노트북을 지우면 청크는 CASCADE로 지워지고 원본·파싱 파일도 저장소에서 지운다. beat가 오래 멈춘 인제스트를 다시 발행하고 고아 실행을 회수한다.

### 검색과 채팅

- `services/vectorstore.py`의 하이브리드 검색은 노트북(·선택 소스)으로 먼저 거른 뒤 dense 코사인 거리(`<=>`)와 sparse 내적을 각각 순위 매기고 RRF(`Σ 1/(순위+2)`)로 합친다. 인덱스 없이 정확 탐색한다(노트북 단위라 후보가 작다). 리랭커(cross-encoder)는 기본 꺼짐이다.
- 임베딩 모델은 관리 「RAG」에서 바꾼다. 바꾸면 재색인 태스크가 청크의 벡터 열을 다시 채운다.
- 채팅은 ReAct 에이전트 한 노드로 돈다. 허용 도구는 부작용 없는 목록(`CHAT_SAFE_TOOLS`: `rag_search`·`source_load`·`source_list`, 승인된 MCP 도구, 스킬 실행 등)뿐이고, 답은 SSE로 흘려보낸다. 소스 발췌를 근거로 쓰면 `[n]` 인용을 달고, 인용은 청크 행 id로 소스 위치에 연결된다. 1회 토큰·비용·반복 한도와 사용자 일일 한도는 관리 「사용 제한」 값을 쓴다.

### 에이전트·워크플로·스킬

- 워크플로(에이전트 정의)는 노드 그래프(LangGraph)다. 노드 종류는 LLM·검색·ReAct·이미지 생성·검증·산출물 저장 등이고, 웹의 워크플로 편집기가 이 정의를 만든다. 실행(`agent_runs`)은 `agent` 큐에서 돌고, 노드별 로그와 진행 이벤트를 남긴다. 사람 승인(HITL)이 필요한 도구 호출은 실행을 멈추고 승인을 기다린다.
- 외부 도구는 MCP 클라이언트(stdio·SSE·streamable HTTP)로 붙인다. 관리자가 서버를 등록하고 도구 스펙 해시를 고정해 승인한 read-only 도구만 노출된다.
- 스킬은 `SKILL.md`(frontmatter + 지침) 패키지다. 업로드·GitHub 설치 → 검증 → 버전 고정(freeze) → 공개 검토를 거친다. 표준 런타임(`SKILL_STANDARD_RUNTIME_ENABLED`)에서는 채팅 에이전트가 스킬을 골라 쓴다.
- 이미지 생성(슬라이드 삽화 등)과 마인드맵·슬라이드 덱 스튜디오는 이 런타임 위의 워크플로·스킬이다. 산출물은 `artifacts` 버킷에 저장된다.

### 스킬 스크립트 샌드박스

스킬에 든 스크립트(`run_skill_script`)는 실행한 사용자가 매번 승인하고, 관리자가 버전마다 스크립트를 검토 승인해야 돈다.

1. api/worker가 작업(스크립트·입력·출력 슬롯)을 `skills.sandbox` 큐에 넣는다. 파일은 경로가 아니라 **api가 발급한 서명 URL**로 건넨다 — 토큰은 메서드·버킷·키·만료(PUT은 크기 상한)를 묶은 HMAC이다.
2. 샌드박스 워커는 기동 때 자신이 전용 워커인지(소비 큐가 `skills.sandbox` 하나, 비루트 uid, Landlock ABI ≥ 6 등) 검사하고 하나라도 어긋나면 기동을 멈춘다.
3. 작업마다 자식 프로세스를 `setrlimit → no-new-privileges → Landlock → seccomp → close_range → execve` 순서로 가둔 뒤 실행한다. 파일은 서명 URL로 받고(GET) 산출물을 올린다(PUT). api의 내부 엔드포인트는 로컬 저장소일 때만 응답하고 토큰·버킷·경로 탈출을 검사한다.
4. S3 백엔드에서는 서명 URL이 S3 서버를 가리키므로 그 서버를 `skill_isolated` 네트워크에 붙이고 `SKILL_SANDBOX_STORAGE_ORIGINS`를 맞춘다.

### 파일 저장소

`services/storage.py`의 공개 함수 뒤에 백엔드 두 가지가 있다. `local`(기본)은 `BIZNOTES_DATA_DIR/<버킷>/<키>` 파일이고(컨테이너에서는 `/data`), 키에 `..`·절대 경로를 거부한다. `s3`는 S3 호환 서버를 `minio` 클라이언트로 쓴다(`biznotes-core[s3]` extra, api 이미지에 포함). 버킷은 `sources`·`parsed`·`artifacts`·`skill-packages` 넷이다.

## 4. 보안 경계

- **인증**: 로컬 계정(bcrypt) + JWT. 로그아웃 토큰은 Valkey 거부 목록에 넣고, 비밀번호를 바꾸면 이전 토큰이 무효가 된다. 관리자가 만든 임시 비밀번호는 첫 로그인 때 변경을 강제한다.
- **비밀 값**: LLM API 키 등은 `ENCRYPTION_KEY`로 AES-GCM 암호화해 저장한다. `APP_ENV=prod`에서 시크릿이 저장소에 공개된 기본값이면 api·worker가 기동을 거부한다.
- **외부 요청**: URL 소스·웹 가져오기·에이전트 HTTP 도구는 모두 SSRF 관문(`core/ssrf.py`)을 지난다. 리다이렉트마다 공인 IP를 재검증하고, 검증한 IP로 직접 연결해 DNS 재바인딩 틈을 닫으며, 응답 크기를 압축 해제 뒤까지 묶는다. 에이전트의 외부 연결은 기본 `fetch_only`다(`AGENT_HTTP_EGRESS_MODE`).
- **권한**: 노트북은 소유자·공유(사용자·그룹) 기준으로 검사하고, 관리 API는 관리자만 부른다. 감사 로그가 관리 동작과 MCP 호출을 남긴다. 레이트 리밋은 로그인·채팅·업로드 등 키마다 분당 상한을 둔다.
