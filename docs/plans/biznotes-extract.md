# biznotes 추출 계획 — agents-core·rapid-notes의 핵심으로 만드는 가벼운 오픈소스 모노리포

- 전제(owner, 2026-11-01): **agents-core와 rapid-notes는 그대로 개발을 이어간다.** 아카이브·rename·이슈 이관 없음. biznotes는 두 저장소의 **핵심만 가져온 별개의 가벼운 오픈소스 모노리포**다.
- 상태: **결정 완료(2026-11-01, §3)**. **Phase 1 완료(2026-11-01, 브랜치 `phase-1-baseline`)**, **Phase 2a 완료(브랜치 `phase-2a-slim`)**, **Phase 2b 완료(2026-11-01, 브랜치 `phase-2b-storage`)**, **Phase 3 완료(2026-11-01, 브랜치 `phase-3-hygiene`, PR #4)**, **Phase 4 완료(2026-11-01, `v0.1.0` 공개)** — 구현 기록은 §5 각 Phase 끝. Phase 1~4의 브랜치·PR(#1~#5)은 비공개 `dalli/biznotes-history`에 있다.
- 기준 커밋: agents-core `v0.31.0`(`5671bfa`, main) · rapid-notes `origin/dev` `415824f4`(v0.8.0). 2026-11-01 실측.
- 저장소(owner, 2026-11-02): 공개 저장소는 **`https://github.com/dallisoft/biznotes`** — 작업 저장소 **`dalli/biznotes`**(비공개)의 `main`·태그를 따라가는 미러다(`.github/workflows/mirror.yml`). 작업 이력은 `dalli/biznotes-history`(비공개, 옛 `dalli/biznotes`의 이름을 바꾼 것).
- 제품 이름(owner, 2026-11-01): **biznotes**. Phase 1의 작업 이름 freenotes는 모두 biznotes로 바꿨다. 원본에서 온 `rapid-notes` 이름은 Phase 4 리브랜딩에서 biznotes로 바꾼다.

## 0. 한 줄 요약

두 저장소를 기준 커밋에서 **스냅샷(이력 없음)** 으로 떠서 한 모노리포 레이아웃에 놓고, 먼저 **전체가 그대로 도는 기준선**을 만든다(Phase 1). 그다음 §3 범위대로 **빼면서 가볍게** 만들고 저장소 계층을 PostgreSQL(pgvector)·Valkey·로컬 파일로 바꾼 뒤(Phase 2), 공개 위생을 갖추고(Phase 3) 이름을 바꿔 Apache-2.0으로 공개한다(Phase 4). 최종 compose는 **postgres · valkey · searxng · api · worker · worker-skill-sandbox · web 7개**, 이미지는 api·web·skill-sandbox 3개다. 원본과는 일회성 추출 관계이며 자동 동기화는 두지 않는다.

## 1. 목표 · 비목표

**목표**
- 노트북 → 소스 인제스트 → RAG 채팅/노트 → 에이전트·스킬(스크립트 포함)·워크플로 실행 → 스튜디오 산출물(마인드맵·슬라이드·이미지) → 관리자 설정의 **제품 핵심 루프**가 `make up` 한 번으로 도는 자급자족 저장소.
- 상태 저장소를 **PostgreSQL과 Valkey 둘**로 줄인다. 벡터는 pgvector, 파일은 로컬 디스크(선택적으로 S3 호환).
- 무거운 것을 뺀다: OCR(docling·opencv), HWP(rhwp), PDF 내보내기(weasyprint), 오디오(ffmpeg·yt-dlp), 외부 계정 커넥터, OIDC, 관측 스택, 운영 인프라(Helm·GKE·Caddy), 사내 거버넌스(레인·ratchet·plan-gates·미러).
- 오픈소스로 공개 가능한 상태: Apache-2.0, 비밀·내부 참조 0, 서드파티 고지, README·CONTRIBUTING 한/영, **공개 저장소라 무료인 GitHub Actions CI를 실제로 켠다**.
- 가벼운 거버넌스: 1쪽 AGENTS.md와 CI 필수 체크.

**비목표**
- 원본 저장소 변경. agents-core·rapid-notes에는 커밋하지 않는다.
- 양방향·자동 동기화. 원본의 수정을 받을 길은 수동 cherry-pick뿐이다.
- 새 기능. 추출·경량화·저장소 교체·공개 준비만 한다.
- 코어 패키지 내부 계층(config→core→models→schemas→services→workers→api)과 경계 테스트 변경.
- 주석·docstring 번역. Valkey까지 없애는 PostgreSQL 단일화(v0.2 후보).

## 2. 현재 상태 (실측, 범위 판단 근거)

**코드 크기(LOC)**

| 영역 | 크기 | 비고 |
|---|---|---|
| `agents_core/services` | 39,439 | agents 13,471(nodes 4,826·react 1,853·tool_registry 1,419·graph 793·mcp_* 1,283) · skills 7,370 · ingestion 5,916(hwp* 5파일) · gateway 2,809 · connectors 1,750 |
| `agents_core/api` · `workers` | 7,093 · 5,111 | 라우터 16 + admin 8, Celery 태스크 17 |
| `agents_core/core`·`models`·`schemas` | 2,472 · 1,787 · 1,943 | 모델 22모듈 |
| `apps/api/app` | 8,284 | api 4,039 · services 2,331 · workers 823 |
| `apps/web/src` | 89,203 | components 41,934 · app 24,942 · lib 14,224 |
| 테스트 | 코어 199 + PG 88 · api 151 · web vitest + Playwright | Alembic 리비전 **54개** |

**저장소 계층 실측(교체 범위)**

| 계층 | 현재 | 코드 접점 | 교체 난도 |
|---|---|---|---|
| 캐시·브로커·pubsub | Redis 7 | 코어 20파일·제품 4파일. 용도: Celery 브로커, rate_limit, token_denylist, concurrency_gate, circuit breaker, usage_guard, events SSE pubsub, notebooks 캐시, digest/overview 락, github_install | **Valkey는 프로토콜 호환 — 코드 변경 0.** PostgreSQL 단일화는 24파일 재작성(v0.2) |
| 벡터 | Qdrant(컬렉션 `chunks_v1`·모델별 `chunks_m_*`, dense 384 + sparse bm25, RRF 융합, payload 필터 `notebook_id`·`source_id`) | `vectorstore.py`(354, 공개 함수 11) · `embedding_spaces.py` 컬렉션 관리 · `workers/tasks/ingestion.py` · conftest의 `:memory:` 대역 | **청크 본문은 이미 PG `source_chunks`(text·seq·meta)에 있고 `vector_id`만 Qdrant를 가리킨다.** 컬럼 추가 + 쿼리 재작성 |
| 파일 | MinIO(버킷 4: sources·parsed·artifacts·skill_packages) | `storage.py`(204, 공개 함수 8) 경유 19파일. 웹은 S3를 직접 보지 않는다. `presigned_get_url`은 사용처가 0이지만, **샌드박스 작업은 `presigned_url_for`로 서명한 URL**(스킬 파일·입력 GET, 출력 슬롯 PUT)로 파일을 주고받는다(Phase 1에서 확인 — `services/skills/sandbox.py` `_presign`) | 호출부 함수는 시그니처 유지로 수정 0. 샌드박스용 서명 URL은 로컬 백엔드에서 대체 발급처가 필요하다 |

**의존성 크기(코어 venv 607 MB 중)**: litellm 128 · onnxruntime 75(fastembed) · grpc 38(qdrant_client 전용) · numpy 24 · cryptography 13 · tokenizers 9.5(fastembed) · redis·qdrant_client 각 5 · celery 3 · langgraph 3 · minio 1. 제품 venv는 여기에 docling·opencv·weasyprint가 더해진다.

**공개 시 걸러야 할 것(추적 파일)**: `.gstack/` 27, `docs/` 세션 로그 1,132, 내부 참조 `<개인 계정>` 9파일·`<회사>` 2·`pkg.dev`/`PROJECT_ID` 5·`nip.io` 2, `tests/fixtures/skills/oracle-src`(Apache-2.0 사본), LICENSE **양쪽 모두 없음**.

## 3. 결정 (owner 확정 2026-11-01)

| # | 결정 | 확정 | 귀결 |
|---|---|---|---|
| E1 | 핵심 범위 | 아래 표 | — |
| E2 | 이력 | **스냅샷, 이력 없음** | `ORIGIN.md`에 기준 커밋 기록. 공개 전 이력 검수 불필요 |
| E3 | 원본과의 관계 | **일회성 추출 후 독립** | 수정은 수동 cherry-pick. Phase 4에서 `agents_core` → `biznotes_core` 리네임 |
| E4 | 라이선스 | **Apache-2.0** | `LICENSE`·`NOTICE`, 각 패키지 `license` 필드 |
| E5 | 저장소 계층 | **Valkey · PostgreSQL+pgvector · 로컬 파일(+선택 S3 호환)** | 설계는 아래 |
| E6 | 언어 | **주석 한국어 유지 + README·CONTRIBUTING 한/영 병기** | 번역 비용 0 |
| E7 | 거버넌스 | **1쪽 AGENTS.md + CI 필수 체크** | 레인·독립 리뷰·ratchet·pr-check·미러 전부 제외 |
| E8 | Alembic | **초기 리비전 1개로 squash** | pgvector 확장·컬럼 포함. 원본 DB는 붙이지 않는다 |
| E9 | 프로세스 | **worker 1개(`-Q ingestion,agent`) + api 기동 시 `alembic upgrade head`** | 분리는 env로 가능. 자동 마이그레이션은 끄는 플래그 제공 |
| E10 | 로컬 임베딩 | **fastembed 기본 유지** | 키 없는 퀵스타트. 리랭커는 기본 꺼짐 |
| E11 | PostgreSQL 단일화 | **v0.1 보류, v0.2 후보** | 상태 저장소 2개 |

### E1 범위 (확정)

판단 기준: ① 핵심 루프에 필요한가 ② 시스템 의존성·외부 바이너리·외부 계정을 끌어오는가 ③ 셀프호스트 1인 운영자가 쓰는가. 제외는 **코드·모델·리비전·라우터·웹·테스트·의존성을 함께** 뺀다.

**포함**

| 영역 | 포함 |
|---|---|
| 코어 기반 | `config`, `core/*`(db·security·crypto·rate_limit·ssrf·uow·http_*·token_denylist·usage_scope·concurrency_gate), `models/`·`schemas/` 중 포함 기능의 것 |
| 인증·관리 | 로컬 로그인(JWT·bcrypt), 강제 비밀번호 변경, `admin/users`, `admin/llm`(텍스트·임베딩·이미지 모델), `admin/rag`(임베딩 모델·리랭크·재색인), `admin/limits`, `admin/skills`, 감사 로그 |
| 노트북·소스 | notebooks, sources(업로드·URL·텍스트), tags, favorites, 인제스트(pypdf·docx·pptx·xlsx·trafilatura, chunker, embedder, enqueue, reindex, stage_artifacts, 상한·경고), 소스 요약·가이드·오버뷰 |
| RAG | rag_chat, vectorstore(**pgvector**), rerank(기본 꺼짐), embedding_spaces, search 라우터, chat·notes, 인용, **노트 출처(provenance)** |
| 에이전트·스킬 | agents/{react, graph, nodes, tool_registry, policy, context, runtime, validators, chat_entry, notebook_artifacts, templating, logging, errors}, agent_runs·lease, MCP 클라이언트, **워크플로 편집기**(workflows 라우터·웹), skills/{standard, package, parser, validator, builder, stages, runtime, service, report, files, **sandbox**}, **스킬 샌드박스 워커**(`workers/skill_sandbox`, 전용 이미지), **GitHub 스킬 설치**(skill_installs·skill_install_chat·github_install), 제품 `services/skills`·`run_models`·`definition_policy` |
| LLM 게이트웨이 | gateway/{llm, retry, circuit, cost, discovery, rerank, **image**}(litellm), **이미지 생성**(nodes/image·image_prompts) |
| 웹 검색 | websearch 서비스·에이전트 툴 + **SearXNG compose 서비스**(기본 켜짐, `infra/searxng/settings.yml`) |
| 워커 | celery_app·runner·task_names, tasks/{ingestion, overview, source_digest}, 제품 tasks/agent — **프로세스 1개** + 샌드박스 워커 1개 |
| 웹 | shell·layout·ui·theme, login·change-password, **대시보드**, notebooks·sources·chat·notes·agents·skills·workflows, **마인드맵·슬라이드 덱 스튜디오**, **그룹·노트북 공유**, **프롬프트 라이브러리**, admin(users·llm·rag·limits·skills·audit) |
| 저장소 | `packages/shared-types`, `apps/web/e2e` 중 포함 기능 스펙(CI 수동) |
| 샘플 | 샘플 스킬(핵심 요약·액션 아이템·마인드맵·current-datetime 스크립트 스킬), 시드(admin·demo) |

**제외**

| 영역 | 제외 | 이유 |
|---|---|---|
| HWP/HWPX | `ingestion/{hwp*, rhwp_launcher}`, `worker-hwp`, rhwp 스테이지(api·샌드박스 이미지 모두), fonts-noto-cjk, `nodes/hwp_tools` | 한국 전용 포맷 + 외부 바이너리 |
| OCR | `docling[rapidocr]`·`opencv`, `parsers.py` OCR 경로, 모델 베이크 | 가장 무거운 의존성 |
| 오디오·유튜브 | `gateway/audio`, `nodes/audio`, `stt_fallback`, yt-dlp·youtube-transcript-api·ffmpeg | 시스템 패키지 + 외부 서비스 약관 |
| PDF 내보내기 | `notebook_export*`, weasyprint·markdown, pango 패키지 | 시스템 패키지 5종 |
| 외부 계정 커넥터 | `services/connectors/*`, `user_connection`, `connections` 라우터·웹 | OAuth 앱 등록 부담 |
| OIDC/Keycloak | `oidc.py`, keycloak | 로컬 인증만 |
| 관측·추적 | langfuse(`gateway/tracing`), prometheus·grafana, `monitoring` 라우터·웹 | `/metrics` 엔드포인트만 남긴다 |
| 자동화 | automation·cron·event_triggers·unattended_runs·agent_schedule·webhooks·beat·admin/automation·웹 automation | v0.1은 수동 실행만 |
| MCP 노출 | mcp_exposure(513) | 외부 노출 표면 |
| 사용량 원장·차트 | usage_ledger·report·usage_event, admin/usage, 웹 usage, 대시보드의 사용량 위젯, recharts | 한도(`usage_guard`·admin/limits)는 포함 |
| 캡처 | capture 라우터·웹 capture/guide | 브라우저 확장 의존 |
| 인트로 | 웹 intro | README가 대신 |
| 운영 인프라 | Helm·GKE·cloudbuild·compose.prod/rollback·caddy·backup·audit-schema-drift·smoke·probe·**minio·qdrant·redis 서비스** | compose 7서비스 |
| 거버넌스 | `scripts/plan-gates`, `pr-check.sh`, `mirror_*`, ratchet 스크립트, `check_container_timezone`, `check_env_contract`, `compare_freeze`, `core_v0_11_0_reconcile`, 레인·HCI 문서, `.gstack`, `docs/` 전부 | 사내 절차 |
| 특화 앱 | `apps/mcp-naver-research`, `vacation_mcp_server`, `demo_mcp_server`, vacation 모델, `rag_eval*` | 데모·국내 증권 |
| 스킬 운영 도구 | skills/{cli, evaluation, authoring, orphans, exposure}, `skills_emergency_private.py`, `oracle-src` 사본 | 저작·평가·긴급 도구 |
| 스토리지 잔재 | `presigned_get_url`, `object_cleanup`의 버킷 순회(로컬 FS 버전으로 축소) | 사용처 없음 |

### E5 저장소 계층 설계

**Valkey** — `valkey/valkey:8-alpine`. redis-py 클라이언트·Celery 브로커 URL·모든 명령이 그대로 동작한다. 할 일: compose 서비스명·이미지, 설정 기본값 `VALKEY_URL`(+`REDIS_URL` 별칭), 문서. **0.25일.**

**PostgreSQL + pgvector** — 이미지 `pgvector/pgvector:pg16`.
- 스키마: `source_chunks`에 `embedding vector`(차원 미지정)·`sparse sparsevec`(차원 미지정) 컬럼 추가, `vector_id` 삭제. `notebook_id`는 `sources` 조인.
- **차원을 고정하지 않는 이유**: 원본은 모델마다 컬렉션을 두어 전환·재색인을 지원한다. pgvector HNSW는 차원 고정이 필요해 같은 설계를 옮기면 모델별 테이블이 된다. v0.1은 **인덱스 없는 정확 탐색** — 검색은 항상 노트북 단위로 필터되어 후보가 수천 행이고, 모델 전환은 컬럼 재생성(reindex 태스크)으로 끝난다. 데이터가 커지면 활성 공간의 차원을 고정하고 HNSW를 더한다(장부에 임계값).
- 하이브리드: dense `<=>`(코사인), sparse `sparsevec` 내적, RRF 융합을 SQL(두 CTE + `row_number`)로. `hybrid_search_groups`는 `DISTINCT ON (source_id)`.
- 코드: `vectorstore.py` 공개 함수 11개를 같은 시그니처로 SQLAlchemy(asyncpg) 구현으로 교체. `embedding_spaces.py`의 컬렉션 관리 제거. `ingestion.py` upsert·`reindex.py`·`admin/rag` 수정. `qdrant_client`·`grpc` 제거(43 MB).
- 테스트: **`tests`/`tests_pg` 이분법을 없애고 코어 테스트도 PG 필수**로 통일. CI 서비스 컨테이너 `pgvector/pgvector:pg16`.
- **2일.**

**파일 스토리지** — `storage.py`의 공개 함수 8개 뒤에 백엔드 2종.
- `local`(기본): `BIZNOTES_DATA_DIR/<bucket>/<key>`. api·worker·**샌드박스 워커**가 같은 볼륨을 마운트한다. 키에 `..`·절대경로를 거부한다(경로 탈출 테스트).
- `s3`(선택): 현재 minio SDK 코드 그대로 — `biznotes-core[s3]` extra. README에 RustFS(Apache-2.0, MinIO 호환)·SeaweedFS(Apache-2.0) 예시. Garage는 AGPL이라 예시에서 뺀다.
- **샌드박스 워커와의 상호작용**(Phase 1에서 바로잡음): 샌드박스는 볼륨을 보지 않고 **서명 URL로만** 파일을 받고 올린다. 네트워크는 브로커·스토리지 출처뿐이다. 로컬 백엔드에서는 **api가 짧은 수명의 HMAC 서명 URL을 발급하고 내부 GET/PUT 엔드포인트로 서빙**한다(권고). `presigned_url_for`의 로컬 구현이 `http://api:8000/internal/storage/<토큰>`을 돌려주게 하면 샌드박스 launcher·작업 형식은 바뀌지 않는다. 샌드박스 네트워크에서 닿는 대상만 minio에서 api(내부 경로)로 바뀐다. 대안인 볼륨 마운트 + Landlock 경로 제한은 작업 형식을 바꿔야 해서 택하지 않는다. 내부 엔드포인트는 토큰의 버킷·키·메서드·만료를 검증하고, 경로 탈출·크기 상한 테스트를 둔다. **+0.5일.**
- `presigned_get_url` 삭제(사용처 0). 호출부 19파일 중 제외 기능 4파일(audio·hwp_tools·notebook_export·orphans)을 빼면 15파일이며 시그니처가 같아 수정 0.
- **1.5일.**

### 줄인 것 · 남긴 것 (검토 결과)

| 후보 | 판정 |
|---|---|
| 워커 프로세스 통합(E9) | **채택** — compose −1 |
| migrate 서비스 제거(E9) | **채택** — api 기동 시 자동, `BIZNOTES_AUTO_MIGRATE=0`으로 끈다 |
| Qdrant 제거로 grpc·qdrant_client 소멸(−43 MB) | **채택** |
| 사용량 차트 제외로 recharts 소멸 | **채택** |
| `tests`/`tests_pg` 통합 | **채택** |
| `requires-python = ">=3.12,<3.13"` 고정 | **채택** |
| fastembed·onnxruntime 선택 extra(E10) | **보류**(기본 유지) |
| 리랭커 cross-encoder | **기본 꺼짐** |
| litellm(128 MB) 대체 | **유지** |
| Valkey 제거(E11) | **v0.2 후보** |
| Celery → arq 등 | **제외** |

확정 범위 적용 뒤: compose **7서비스**(postgres·valkey·searxng·api·worker·worker-skill-sandbox·web), 이미지 3(api·web·skill-sandbox), api 이미지 apt 패키지 0, 샌드박스 이미지는 fonts-nanum·node만(rhwp 제거), 상태 저장소 2 + 데이터 볼륨 1. 코어 런타임 의존성에서 qdrant_client·grpc·minio(선택 extra)·docling·opencv·weasyprint·markdown·yt-dlp·youtube-transcript-api·langfuse 제거.

## 4. 목표 구조

```
biznotes/
├── README.md · LICENSE(Apache-2.0) · NOTICE · CONTRIBUTING.md · SECURITY.md · ORIGIN.md
├── AGENTS.md                    1쪽
├── Makefile                     up/down/logs/migrate/seed/test/ci/build/lock-docker/gen-types
├── pyproject.toml               uv 워크스페이스 루트. members = [packages/core, apps/api]
├── uv.lock
├── package.json · pnpm-workspace.yaml(apps/web, packages/shared-types) · pnpm-lock.yaml
├── .dockerignore · .gitignore · .editorconfig
├── .github/workflows/ci.yml     backend(pgvector·valkey 서비스 컨테이너)·frontend·contracts·docker-build(3이미지)
├── apps/
│   ├── api/                     alembic/versions/0001_initial.py 1개(pgvector 확장·컬럼 포함)
│   └── web/
├── packages/
│   ├── core/                    biznotes_core/ · tests/(PG 필수, 단일 스위트) · docker/skill-sandbox.Dockerfile · docker/test/ · pyproject.toml
│   └── shared-types/
├── infra/
│   ├── compose.yml              postgres(pgvector/pgvector:pg16) · valkey · searxng · api · worker · worker-skill-sandbox · web — 볼륨: pgdata · valkeydata · data
│   ├── compose.dev.yml · searxng/settings.yml · .env.example
└── docs/ARCHITECTURE.md · docs/plans/biznotes-extract.md
```

## 5. 구현 단계

### Phase 1 — 기준선: 전체 스냅샷을 모노리포 레이아웃에서 돌린다 (1.5일)

저장소 계층은 **아직 원본 그대로**(redis·qdrant·minio)다 — 교체는 Phase 2b.

1. 스냅샷: `git -C ../agents-core archive v0.31.0 | tar -x -C packages/core`, `git -C ../rapid-notes archive 415824f4 apps packages infra Makefile package.json pnpm-workspace.yaml pnpm-lock.yaml .gitignore skills | tar -x`. `docs/`·`.github/`·`.gstack/`·`scripts/`는 가져오지 않는다. `ORIGIN.md`에 기준 커밋·날짜·원본 주소.
2. `git init` → 첫 커밋. 원격은 `dalli/biznotes`(비공개)다.
3. uv 워크스페이스: 루트 `pyproject.toml`(가상, `override-dependencies` 이동, members), `apps/api`의 git 핀 → `{ workspace = true }`, `allow-direct-references` 삭제, `uv lock` 단일화(공유 126개 중 69개 버전 상이 — 제품 lock 우선), `requirements.docker.txt`는 `uv export --no-dev --no-emit-workspace`.
4. Docker: `apps/api/Dockerfile` 루트 컨텍스트 + `core-wheel` 스테이지 + `python:3.12-slim` 직접 FROM, 루트 `.dockerignore`, compose의 api·worker 컨텍스트 `..`. 샌드박스 이미지도 루트 컨텍스트로 빌드(`packages/core/docker/skill-sandbox.Dockerfile`, 루트 lock에서 `uv export --package agents-core --only-group sandbox`), compose의 `worker-skill-sandbox`를 `<개인 레지스트리>/…` 핀 대신 `build:`로.
5. 핀·거버넌스 잔재 제거: `check-core-pin.sh`, `test_core_pin_*`·`test_dockerfile_core_pin.py`, Makefile의 `UV_NO_SOURCES`·`CORE_PIN_STRICT`·`core-pin-check`·`ci-dev`·ratchet·plan-gates·mirror·helm·env-contract·mcp-naver 타깃, `meta.py`의 `core_version`.
6. Makefile `ci` = `ruff·mypy·pytest(api·core)·boundary·migrate-check·web lint·typecheck·vitest·gen:types 드리프트`.

**수락 기준**
- [x] `make up && make migrate && make seed` 뒤 로그인·소스 업로드·질의·스크립트 실행이 된다. 스크립트는 LLM 없이 샌드박스 작업을 직접 보내 확인했다(아래 기록). LLM이 도구를 부르는 에이전트 경로는 미검증.
- [x] `make ci` 통과. 커버리지·vitest 임계값은 원본 값 그대로.
- [x] `packages/core/tests/test_boundary.py` 통과(`make core-test`에 포함).
- [x] 코드·설정에서 `github.com/dalli/agents-core`·`<개인 레지스트리>/agents-core` 참조 0건. 서술 문서(코어 README 릴리스 노트, 스킬 README 검증 기록)에는 남아 있다 — Phase 3 문서 재작성에서 정리.

#### Phase 1 구현 기록 (2026-11-01)

브랜치 `phase-1-baseline`. `main`은 스냅샷 커밋(`0145ac3`) 하나다. 두 브랜치 모두 `origin`(`dalli/biznotes`, 비공개)에 올렸다.

**계획과 달라진 점**
1. **rapid-notes dev 스택과의 격리를 추가했다.** 같은 호스트에서 rapid-notes 스택이 돌고 있었고, 스냅샷은 같은 compose 프로젝트명·로컬 이미지 태그(`rapid-notes/api:latest`)·호스트 포트·테스트 DB 접두를 물려받았다. 그대로 띄우면 rapid-notes 컨테이너를 재생성하고 이미지 태그를 덮어쓴다. compose 프로젝트명 `biznotes`, 이미지 `biznotes/*`, 호스트 포트 1xxxx → 2xxxx(PG 25433·Redis 26379·Qdrant 26333·MinIO 29000·API 28000·Web 23000·SearXNG 28080)로 바꿨다.
2. **코어 `docs/`·`.github/`를 스냅샷 첫 커밋부터 넣지 않았다.** 첫 커밋에 들어가면 나중에 지워도 공개 이력에 남는다. 그래서 Phase 4에 이력 전체 검사 단계를 더했다.
3. **샌드박스 이미지 의존성 export를 호스트로 옮겼다.** `make lock-docker`가 `packages/core/docker/requirements.sandbox*.txt`를 만들고 `docker-lock-check`가 드리프트를 막는다. 이미지 안 uv(0.12.17)가 호스트 uv(0.13.6)의 lock을 읽지 않아도 된다.
4. **`tests` 패키지 이름 충돌.** 한 venv에서 api editable 설치가 `apps/api` 전체를 경로에 올려 api의 `tests`(일반 패키지)가 코어의 `tests`(네임스페이스)를 가렸다. api에 hatchling `dev-mode-exact = true`를 두고 그 런타임 `editables`를 dev 그룹에 넣었다. 코어 `tests/`에 `__init__.py`를 두는 방법은 코어 테스트의 `from conftest import`를 깨뜨려 택하지 않았다.
5. **원본 `docs/`를 읽던 테스트를 정리했다.** api: env 계약 게이트 테스트 파일(17건), 롤백 가드 SQL 테스트 파일(1건), webhook 테스트의 가드 대조 3줄. 코어: rhwp 가이드 대조 파일 1개, write-site 대장 대조 함수 1개. 웹: 인트로 문구 대조 파일 1개.
6. **`GET /meta/versions`의 `core_version`을 뺐다.** 웹 사이드바 표기·shared-types 동반.
7. **샌드박스 작업이 MinIO 서명 URL을 쓴다는 점을 확인했다.** §3 E5 파일 스토리지 설계와 Phase 2b를 고쳤다(api 내부 엔드포인트가 서명 URL 발급·서빙).

**검증 결과**
| 항목 | 결과 |
|---|---|
| `make ci` | 통과(5분 46초). api ruff·mypy, pytest 2,016 passed·2 skipped, 커버리지 73.84%(하한 70.0) · 코어 ruff·mypy, pytest 6,906 passed·190 skipped·1 xfailed, wheel 빌드 · migrate-check(clean DB에 54 리비전, `alembic check` 드리프트 없음) · web lint·typecheck, vitest 189파일 2,658 passed, 구문 커버리지 91.12% · shared-types·Docker requirements 드리프트 없음 |
| api 이미지 | 루트 컨텍스트 3.69 MB, 4분 33초, 12.3 GB(원본 rapid-notes/api 12.4 GB). 이미지 안 코어 wheel `agents-core 0.31.0` |
| web·skill-sandbox 이미지 | web 366 MB. skill-sandbox 827 MB, 빌드 시점 smoke 통과(python·node·bash, 한글 PDF·오피스, uid 10001 HWPX→HWP 변환, 워커 모듈 누수 없음) |
| `make up && make migrate && make seed` | 11개 서비스 기동, 54 리비전 적용, 시드(관리자·데모·샘플 에이전트·샘플 스킬) |
| `make smoke` | readyz → 로그인 → 노트북 → 텍스트 소스 → ready → 검색 1건 이상: SMOKE OK |
| 파일 업로드 | docx 업로드 202 → 인제스트 `ready` → 검색 1위가 업로드 본문 |
| 스크립트 실행 | 샌드박스 워커(`--profile skill-scripts`)에 코어 작업 형식 그대로 `scripts/now.py`(current-datetime)를 보냄 → `completed`, exit 0, UTC 시각 JSON |
| 웹 | `GET /`·`/login` 200 |
| rapid-notes 스택 | 작업 내내 무중단(컨테이너 재생성 없음) |

**미검증**
- LLM을 쓰는 채팅 답변과 에이전트가 `run_skill_script`를 부르는 경로. 새 DB에 LLM 프로바이더가 없고, rapid-notes `.env`의 키를 가져다 쓰지 않았다.
- Playwright e2e(원본도 수동·야간 전용).

**후속**
- 워크스페이스 lock은 제품 lock을 우선하므로 샌드박스 스크립트용 라이브러리가 원본 코어 이미지보다 낮다: pillow 12.3.0→12.2.0, pypdf 6.19.0→6.13.2, lxml 6.1.3→6.1.1, charset-normalizer 3.5.1→3.4.7. api 이미지는 원본과 같다. 기준선에서는 올리지 않았다 — Phase 3 dependabot 도입 전에 `uv lock --upgrade-package pillow --upgrade-package pypdf`를 별도 커밋으로.
- 호스트 개발 Python은 3.14(원본 rapid-notes와 같음), 이미지는 3.12다. Phase 2a의 `requires-python <3.13` 고정 때 맞춘다.

### Phase 2a — 경량화: 확정 범위대로 빼면서 간다 (2.5~4일)

원칙: **기능 하나 = 커밋 하나**, 매 커밋 `make ci` 초록. 빼는 순서는 의존 방향의 바깥부터(웹 → 라우터 → 서비스·태스크 → 모델·스키마 → 의존성 → compose). §9 **제거 장부**에 "무엇을·어디서·왜·되살리려면 원본 어느 경로".

1. 운영·거버넌스·특화 앱 제거. 의존성 없는 삭제라 먼저.
2. 무거운 모달리티 제거: HWP(api·샌드박스 이미지의 rhwp 스테이지 포함) → OCR → 오디오·유튜브 → PDF 내보내기 → 커넥터 → OIDC → 관측. 단계마다 `pyproject`·Dockerfile apt·RUN을 같이 지우고 이미지 크기를 장부에.
3. 기능 제외: 자동화·beat → MCP 노출 → 사용량 원장·차트(대시보드 위젯 포함) → 캡처 → 인트로.
4. 워커 통합·migrate 제거(E9), `requires-python` 고정.
5. 테스트: 제외 기능 테스트 삭제, 픽스처 정리. Playwright 스펙은 포함 기능 것만.

**Phase 2a 착수 시 바로잡은 것(2026-11-01)**
- **「MCP 노출」 제외 항목은 실체가 없었다.** `services/agents/mcp_exposure.py`는 스킬을 MCP 서버로 내보내는 기능이 아니라, 외부 MCP 서버 도구를 에이전트에 노출할지 판정하고 승인을 관리하는 **MCP 클라이언트의 일부**다. 스킬을 MCP 서버로 내보내는 코드는 저장소에 없다. 포함 기능(MCP 클라이언트)이므로 남긴다.
- **스킬 운영 도구 중 `exposure`·`authoring`·`orphans`는 남긴다.** 각각 스킬 공개 범위·검토 승인, 스킬 저작 검증, 패키지 고아 정리로 스킬 본체가 import한다. 아무도 import하지 않는 `cli`·`evaluation`만 뺐다.
- **데모 MCP 서버(`scripts/demo_mcp_server.py`)는 남긴다.** 포함 기능인 MCP 클라이언트의 시드 샘플 3개(단어 세기·대문자 변환·텍스트 뒤집기)가 이 서버를 쓴다. 외부 의존성이 없는 stdio 서버라 무게가 없다.
- **beat는 없애지 않고 통합 워커에 내장한다(`celery worker -B`).** 정체 소스 재발행·고아 run 회수·벡터 고아 정리가 beat 주기 작업이다. 자동화 스케줄 평가와 사용량 원장 정리, PDF 내보내기 회수만 빠진다.
- **Alembic squash를 Phase 2a 끝으로 당긴다.** 제외 기능의 모델을 지우면 리비전과 어긋나 `alembic check`가 실패한다. 기능 코드를 먼저 빼고, 마지막 커밋에서 모델 삭제와 초기 리비전 1개 squash를 함께 한다. Phase 2b는 pgvector 변경을 그 초기 리비전에 반영한다.

**Phase 2a 구현 기록 (2026-11-01, 브랜치 `phase-2a-slim`, 커밋 16개)**

순서는 계획과 같되 두 가지를 바꿨다. 유튜브 제거 뒤 남은 조각(영상 검색 분류·문구)을 별도 커밋으로 정리했고, 남은 표·열 제거와 Alembic squash를 한 커밋으로 묶었다(제거 리비전만 따로 두면 곧 지울 마이그레이션 테스트 18개가 head 핀으로 실패한다). 커밋마다 `make ci`가 초록이었다. 각 기능의 경로·이유·되살릴 원본은 §9 장부에 있다.

| 항목 | 결과 |
|---|---|
| 커밋 | 운영·거버넌스·특화 앱 → HWP → OCR → 오디오·유튜브 → PDF 내보내기 → 커넥터 → 유튜브 잔여 → OIDC → 관측 → 자동화 → 사용량 원장 → 캡처 → 인트로 → E9·Python 3.12 → 표·열 제거·squash → e2e 정리 |
| 코드 줄 수(.py·.ts·.tsx, Phase 1 → 2a) | 코어 58,517 → 46,721 · 코어 테스트 98,294 → 79,262 · api 앱 8,399 → 7,495 · api 테스트 53,190 → 39,354 · 웹 88,932 → 76,231 · e2e 8,045 → 6,497 |
| 저장소 diff | 595파일, +10,846 / −96,362 |
| `make ci` | api 1,580 passed(커버리지 75.73%, 하한 70.0) · 코어 5,702 passed·32 skipped·1 xfailed · migrate-check(리비전 1개, `alembic check` 깨끗) · 웹 170파일 2,254 · 계약·Docker lock 드리프트 없음. 호스트 Python 3.12.11 |
| 이미지 | api 12.3 GB → 1.25 GB, skill-sandbox 827 MB → 624 MB, web 366 MB → 363 MB |
| 기본 기동 서비스 | 11개 → 8개(데이터 서비스 postgres·redis·minio·qdrant·searxng + api·worker·web). 빠진 것: worker-ingestion·worker-agent·worker-hwp·beat. 선택 프로파일은 `skill-scripts` 샌드박스만 남았다 |
| 실스택 확인 | 새 DB에서 api가 `0001`을 적용하고 healthy, worker가 `ingestion`·`agent` 큐와 beat로 기동, `make seed`, `make smoke` OK, web `/login` 200 |

후속 정리 후보(이번 범위 밖):
- 청크 위치(locations) 파이프라인과 파싱 중단 토큰 `abort.py`는 생산자가 없다(HWP·OCR이 쓰던 것).
- 스킬 매니페스트 권한 키 `tts`가 스킬 스키마에 남아 있다(`permissions.tts`). 스킬 패키지 형식이라 Phase 3 문서 정리 때 판단한다.
- 코어 README의 릴리스 이력 표에는 뺀 기능 언급이 그대로다. 모듈 개요만 고쳤다.
- 샌드박스 스크립트 라이브러리(pillow·pypdf 등)가 원본 코어 이미지보다 낮다(Phase 1 기록). Phase 3 dependabot 전에 올린다.
- `phase-2a-slim`은 아직 원격에 올리지 않았다.

### Phase 2b — 저장소 계층 교체 (4.5일, E5)

순서는 위험이 작은 것부터. 각 단계 끝에 Phase 1의 수동 smoke를 반복한다.

1. **Valkey**(0.25일): compose 이미지·서비스명, `VALKEY_URL`(+`REDIS_URL` 별칭), 문서. 코드 변경 0 — `make ci`와 smoke로 증명.
2. **파일 스토리지**(2일): `storage.py`에 `StorageBackend` 프로토콜 + `LocalStorage`(기본)·`S3Storage`(선택 extra). 설정 `STORAGE_BACKEND=local|s3`, `BIZNOTES_DATA_DIR`. compose에서 minio 제거, `data` 볼륨을 api·worker에 마운트(샌드박스는 마운트하지 않는다). 샌드박스용 서명 URL을 api 내부 엔드포인트로 발급·서빙(HMAC 토큰, 샌드박스 네트워크에 api 추가). 테스트: 로컬 백엔드 단위 테스트(put/get/stream/stat/delete·경로 탈출 거부) + 내부 엔드포인트 토큰 검증(만료·메서드·버킷·경로 탈출) 테스트 + 기존 대역을 tmp_path 백엔드로 교체.
3. **pgvector**(2일): 초기 리비전에 `CREATE EXTENSION vector` + `source_chunks` 컬럼. `vectorstore.py` 재구현(공개 함수 11, 같은 시그니처), `embedding_spaces.py` 컬렉션 관리 제거, ingestion upsert·reindex·`admin/rag` 수정. 코어 테스트를 PG 필수 단일 스위트로 통합. CI 서비스 컨테이너 `pgvector/pgvector:pg16`. `qdrant_client` 제거 후 `uv pip check`.
4. **Alembic squash(E8)**: `versions/` 비우고 `alembic revision --autogenerate -m initial` 1개. `make migrate-check`. api 엔트리포인트에 자동 마이그레이션(E9).
5. 모델·스키마 정리 뒤 `pnpm gen:types`, 의존성 최종 정리(`uv lock`·`requirements.docker.txt`·`pnpm why`), compose 7서비스, `.env.example` 재작성.

#### Phase 2b 구현 기록 (2026-11-01, 브랜치 `phase-2b-storage`)

커밋은 단계마다 하나다: Valkey → 로컬 파일 스토리지(샌드박스 서명 URL 포함) → pgvector(코어 테스트 단일화 포함) → compose·`.env.example`·커버리지 재잠금·이 기록. 커밋마다 `make ci`가 초록이었다. Alembic squash(4번)는 Phase 2a 끝에 이미 했고, 이번에는 초기 리비전 `0001`을 고쳤다(pgvector 확장·열, 열 이름 변경).

**계획과 달라진 점**
1. **`minio_*` 이름을 지금 걷어냈다.** Phase 4 수락 기준(`git grep minio`가 ORIGIN·NOTICE·S3 문서뿐)을 미리 맞췄다. 설정 `minio_bucket_*` → `storage_bucket_*`, `minio_endpoint`·키·`secure` → `s3_*`, 열 `sources.minio_object_key` → `original_object_key`. S3 자격 증명은 `storage_backend=s3`일 때만 prod 기본값 검사를 받는다(`S3_SECRET_FIELDS`) — 기본(로컬)은 자격 증명이 없다.
2. **minio 클라이언트는 코어 `s3` extra지만 api 이미지에는 싣는다.** api가 `agents-core[s3]`에 의존한다(1 MB). 이미지 그대로 `STORAGE_BACKEND=s3`로 바꿀 수 있게 하려는 것이다. 코어만 쓰는 쪽은 extra 없이 가볍다.
3. **샌드박스 서명 URL**: 토큰은 JWT 키에서 파생한 HMAC(용도별 키)으로 메서드·버킷·키·만료(PUT은 본문 상한 = `skill_script_outputs_max_bytes`)를 묶는다. 내부 엔드포인트는 로컬 백엔드일 때만 응답하고, 버킷을 메서드마다 고정했다(GET: 스킬 패키지·산출물, PUT: 산출물). s3에서는 샌드박스 출처가 api가 아니라 S3 서버라 compose에 `SKILL_SANDBOX_STORAGE_ORIGINS`를 따로 두었다(처음에는 서명 주소 변수를 재사용해 s3에서 형식이 맞지 않았다 — RustFS 확인 중에 발견).
4. **pgvector 값 변환은 패키지 없이** `models/vector_types.py`의 `UserDefinedType` 두 개로 한다. asyncpg는 코덱이 없는 타입을 텍스트로 주고받는다(실측).
5. **`vector_id`를 없앴다 — 인용 `chunk_id`는 청크 행 id다.** #190 회귀 테스트(“행 PK가 아니라 point id로 대조”)는 “행 id로 대조, 어느 행도 아니거나 다른 소스·노트북 청크면 거부”로 바꿨다.
6. **BM25 토큰 id 접기.** fastembed BM25 id는 `abs(mmh3)`(2³¹ 미만)이고 `sparsevec` 최대 차원은 10억이다. 문서·질의 모두 `id % 10억`으로 접는다(겹치면 가중치를 더한다).
7. **RRF는 Qdrant 눈금 그대로**(`Σ 1/(순위+2)`, 후보 `limit×3`, float8 합). `numeric`으로 더하면 1/6+1/6이 1/3보다 미세하게 커져 컷오프 동률이 달라졌다(대조 600건 중 1건) — float8로 고쳤다. 소스별 묶음은 후보를 자르지 않는다(Qdrant 인메모리와 같다).
8. **벡터 정합 장치를 걷어냈다.** 색인 교체가 한 트랜잭션이 되어 Qdrant 스냅샷·보상 삭제, `cleanup_source_vectors` 태스크, 고아 벡터 회수(beat `reconcile-vector-orphans`, `vector_reconcile_enabled`)가 필요 없다. 소스 삭제는 CASCADE다. 게이트웨이 공간은 색인한 소스가 없으면 질의 임베딩을 건너뛴다(옛 「컬렉션 없음」 분기와 같다).
9. **코어 테스트 단일화**: `tests_pg/` 73개를 `tests/`로 옮기고 이름이 `_pg.py`로 끝나면 자동으로 `pg` 표시한다(같은 이름 3개는 `_pg`를 붙였다). PG 테스트가 처음 쓸 때 실행 전용 DB를 만든다 — 기본 접속은 `make up`의 25433·26379(`CORE_TEST_PG_*`·`CORE_TEST_VALKEY_URL`). `make core-pg-test`는 없앴다. **그동안 게이트 밖이라 Phase 2a 뒤 낡은 PG 테스트가 있었다**(지운 함수 패치, `uuid` 대역에 `UUID` 없음 2건) — 함께 고쳤다. 코어 게이트 pytest 시간은 2분 30초 → 4분 15초~4분 50초.
10. **개발 DB는 새 볼륨(`pgdata`)으로 시작했다.** 이미지가 alpine → Debian(pgvector)이고 `0001`을 고쳤기 때문이다. 옛 볼륨(`biznotes_pg_data`·`biznotes_minio_data`·`biznotes_qdrant_data`·`biznotes_redis_data`)은 지우지 않았다 — 필요 없으면 `docker volume rm`.
11. **API `BIZNOTES_AUTO_MIGRATE`를 compose에서 넘긴다**(Phase 2a는 이미지 기본값에만 기댔다).

**검증 결과**
| 항목 | 결과 |
|---|---|
| `make ci` | 통과. api 1,581 passed(커버리지 75.73%) · 코어 6,628 passed·32 skipped·1 xfailed(PG 테스트 포함) · migrate-check(리비전 1개, `alembic check` 깨끗) · web 170파일 2,254 · 계약·Docker lock 드리프트 없음 |
| Qdrant 대조 | 옮기기 전 Qdrant(인메모리, 원 `vectorstore.py`)와 pgvector 구현에 같은 합성 데이터(노트북 3·소스 9·청크 ~40, 2³¹ 근처 토큰 포함)를 넣고 질의 1,000개 × 단일·다중 노트북·소스별 묶음 = 3,000건 비교: 순서·점수 차이 0 |
| 검색 지연(정확 탐색) | 노트북 하나, 384차원 + BM25(60토큰), Docker Desktop: 1만 청크 hybrid 30ms·묶음(소스 50 × 1) 39ms, 10만 청크 334ms·626ms. 처음 쓴 SQL은 CTE끼리 다시 조인해 1만 청크 묶음이 2.9초였다(중첩 루프) — 결과 행을 기본 키로 가져오게 고쳤다 |
| 이미지 | api 1.25 GB → 1.22 GB(qdrant-client·grpcio 제거). apt 패키지 = 베이스(python:3.12-slim) 87개 그대로. 금지 패키지(docling·opencv·weasyprint·yt-dlp·langfuse·qdrant-client·grpcio·rhwp·youtube-transcript-api) 0개. skill-sandbox 624 MB, rhwp 없음. web 363 MB |
| 기동 | 기본 서비스 6개(postgres·valkey·searxng·api·worker·web) + 선택 1개(worker-skill-sandbox) = 7. 이미지가 있으면 `make up` 15초(healthy까지). api 이미지 무캐시 빌드 27초(베이스 이미지 받아 둔 상태) |
| DB | 새 `pgdata`에서 api가 `0001` 적용, `\dx` → `vector 0.8.7`, `source_chunks.embedding vector`·`sparse sparsevec` |
| 로컬 스토리지 | `make smoke` OK. docx 업로드 → 워커 파싱 → `/data/sources`·`/data/parsed` → 본문 스트리밍 → 검색 1위. 노트북 삭제 뒤 `/data` 파일 0개 |
| 샌드박스 | `--profile skill-scripts`로 띄운 샌드박스가 api 서명 URL로 스킬 파일·입력을 받고(GET) 산출물을 올림(PUT) — exit 0, `/data/artifacts`에 저장. 샌드박스 컨테이너에 `/data` 없음, `postgres` 이름 해석 안 됨(네트워크 분리), 닿는 곳은 api·valkey뿐 |
| S3(RustFS, 수동) | `rustfs/rustfs:latest`를 compose 네트워크에 붙이고 api·worker를 `STORAGE_BACKEND=s3`로: docx 업로드 → 파싱 → 본문 → 검색 통과, 객체가 `sources`·`parsed` 버킷에 생기고 노트북 삭제 뒤 0개. 샌드박스도 RustFS presigned URL로 받고 올림(`SKILL_SCRIPT_STORAGE_ENDPOINT`·`SKILL_SANDBOX_STORAGE_ORIGINS`) |
| pgvector 실경로 | 업로드 → 384차원 벡터 행 → 노트북 검색(점수 1.0 = 두 목록 1위)·전체 시맨틱 검색 → 소스 삭제 뒤 청크 0행 |
| 수동 smoke | GitHub 스킬 설치(`anthropics/skills`, 제안 → `POST /skill-installs/github` 200, 패키지가 `/data/skill-packages`에), 웹 검색(SearXNG, 결과 20건), 워크플로 실행(시드 「[샘플] MCP 대문자 변환」 → succeeded, 산출물 내려받기, 노트북 삭제와 함께 정리) |
| 커버리지 재잠금 | api `fail_under` 70.0 → 75.0(실측 75.73). web statements·branches·functions·lines 87.6·84.7·83.6·88.8 → 90.5·87.4·87.5·91.8(실측 90.67·87.53·87.62·91.94) |

**미검증**
- LLM이 필요한 경로: 채팅 답변, 마인드맵·슬라이드 생성, 이미지 생성, 브라우저의 워크플로 편집기 화면. 새 DB에 LLM 프로바이더가 없고 키를 쓰지 않았다(Phase 1과 같다). 워크플로 런타임 자체는 MCP 샘플 에이전트로 확인했다.
- 처음부터(베이스 이미지까지 받는) 세 이미지 빌드를 합친 기동 시간 — api 무캐시 빌드와 warm 기동만 쟀다.

**후속**
- 코어 README·스크립트 AGENTS.md 등 서술 문서의 Redis·MinIO·Qdrant 언급은 Phase 3 문서 재작성에서 정리한다(코드 주석은 이번에 고쳤다).
- 활성 공간의 차원을 고정한 HNSW는 10만 청크 묶음 600ms대가 문제가 될 때(v0.2 후보 그대로).

**Phase 2 수락 기준**
- [x] compose 서비스 7개(postgres·valkey·searxng·api·worker·worker-skill-sandbox·web). `make up`이 노트북 PC에서 3분 안에 뜬다(측정값 장부 기록). — warm 15초, api 무캐시 빌드 27초
- [x] api 이미지에 apt 패키지 0, `docling`·`opencv`·`weasyprint`·`yt-dlp`·`langfuse`·`qdrant_client`·`grpc`·`rhwp` 없음(`uv pip list`). 샌드박스 이미지에 `rhwp` 없음.
- [x] `alembic/versions/` 파일 1개, `make migrate-check` 통과, `\dx`에 `vector`.
- [x] 하이브리드 검색 회귀: 원본 코어의 검색 테스트(RRF 순서·소스별 그룹·노트북 필터·삭제 반영)가 pgvector 구현에서 같은 결과로 통과한다. — Qdrant 대조 3,000건 + `test_vectorstore_pg.py`
- [x] 파일 스토리지: 업로드 → 파싱(워커) → 다운로드, 스킬 패키지 업로드 → 샌드박스 스크립트 실행 → 산출물 저장이 로컬 볼륨에서 되고, 샌드박스 격리 테스트(Linux)가 data 경로 차단을 증명한다. `STORAGE_BACKEND=s3`로 RustFS 컨테이너에 대해서도 1회 통과(수동, 장부 기록). — 격리는 볼륨 미마운트·네트워크 분리를 실컨테이너로 확인, 엔드포인트의 토큰·버킷·경로 탈출은 테스트
- [~] 워크플로 편집기·마인드맵·슬라이드·이미지 생성·GitHub 스킬 설치·웹 검색 각 1회 수동 smoke. — GitHub 스킬 설치·웹 검색·워크플로 실행은 통과, LLM이 필요한 마인드맵·슬라이드·이미지 생성·편집기 화면은 미검증(위)
- [x] `make ci` 통과. 커버리지 임계값은 삭제한 코드 비율만큼 **재측정해 다시 잠근다**(장부에 전후 값).

### Phase 3 — 공개 위생 (1일)

1. `LICENSE`(Apache-2.0), `NOTICE`(서드파티 고지: skills-ref 사본을 남겼다면 Apache-2.0, fonts-nanum OFL, 외부 스키마 사본 출처), 각 패키지 `license` 필드.
2. 비밀·내부 참조 스캔: `gitleaks detect --no-git` + `git grep -i`(`<개인 계정>`, `<회사>`, `pkg.dev`, `PROJECT_ID`, `nip.io`, 사내 호스트명, 개인 이메일, 시드 실명). 결과 0을 장부에.
3. 문서: `README.md`(한/영, 3문단·스크린샷 1장·`make up` 퀵스타트·아키텍처 그림·출처·Landlock 커널 요건), `CONTRIBUTING.md`(한/영), `SECURITY.md`, `docs/ARCHITECTURE.md`, `AGENTS.md` 1쪽.
4. GitHub Actions `ci.yml` PR·push 트리거: backend(pgvector·valkey 서비스 컨테이너)·frontend·contracts·docker(3이미지). `dependabot.yml`. e2e는 `workflow_dispatch`.
5. 이슈 템플릿 2종, PR 템플릿 3칸(요약·테스트·마이그레이션 동반).

**수락 기준**: `gitleaks` 0건, 내부 참조 0건, 새 clone에서 README만 보고 `make up`(다른 머신 1회), CI 4잡 초록(비공개 `dalli/biznotes`에서 먼저 — 비공개 저장소의 Actions는 무료 분량이 정해져 있으므로 `workflow_dispatch`로 1회 확인 후 PR 트리거를 켠다).

#### Phase 3 구현 기록 (2026-11-01, 브랜치 `phase-3-hygiene`)

커밋은 라이선스 → 내부 참조 정리 → archhub 분기 제거 → 의존성 상향 → CI → 시크릿 가드 수정 → 문서 → 이 기록 순이다.

**계획과 달라진 점**
1. **스캔에서 범위 밖 특화 코드 두 가지가 더 나왔다.** 샘플 스킬 `use-change-review`(E1 샘플 목록에 없음, 실제 고객 보고서 파일명·건물 주소 포함)와 채팅의 `archhub` MCP 선행 조회(한 배포용 분기가 모든 채팅 프롬프트에 건물정보 도구 규칙을 넣었다). 둘 다 지우고 §9 장부에 적었다. 스킬은 첫 스냅샷 커밋부터 이력에 있으므로 Phase 4 이력 검사에서 걸린다.
2. **보안 결함을 고쳤다.** prod 기동 가드(`assert_prod_secrets`)는 설정 클래스 기본값과 비교하는데, compose 기본값(`dev-jwt-secret`·`dev-encryption-key-32-bytes-pad!`)과 `.env.example`(`change-me`)이 코어 클래스 기본값과 달라 공개된 값으로 `APP_ENV=prod`가 떴다. 제품 `Settings` 기본값을 compose 값과 맞추고(개발 스택의 암호화 데이터 영향 없음) compose·`.env.example`·가드 기본값 대조 테스트를 더했다.
3. **MCP `_meta` 키 이름공간을 바꿨다**: `io.github.<회사>.agents-core/` → `io.github.dalli.biznotes/`. 공개 계약이지만 아직 외부 소비자가 없다.
4. **디렉터리별 AGENTS.md 22개를 지웠다.** 원본 거버넌스(레인·보호 파일)와 낡은 저장소 서술이라 고쳐 쓰는 대신 루트 1쪽 AGENTS.md와 ARCHITECTURE.md로 합쳤다(E7). 코어 README는 1,535줄 → 476줄.
5. **Makefile `ci`를 `ci-backend`·`ci-frontend`·`ci-contracts`로 나눠 CI 잡이 같은 타깃을 부른다.** gitleaks는 contracts 잡에 넣었다(`.gitleaks.toml`, 테스트 코드 경로 허용). 액션은 공개 2주 이상 지난 릴리스의 커밋 SHA로 고정했다.
6. **`workflow_dispatch` 먼저 돌리기는 할 수 없었다.** GitHub는 기본 브랜치에 없는 워크플로를 수동 실행하지 못한다. PR #4를 열어 `pull_request` 트리거로 확인했다. `e2e.yml`은 main에 들어간 뒤에야 수동 실행할 수 있어 미검증이다.
9. **스킬 샌드박스 격리 시험이 CI에서 처음으로 실제로 돈다.** GitHub 러너(ubuntu-24.04)는 Landlock ABI ≥ 6이라 skip되지 않는다(macOS 개발 환경에서는 통째로 skip). 러너에서 드러난 시험의 환경 가정 두 가지를 고쳤다: ① 대조군(샌드박스 밖 같은 uid)이 다른 프로세스를 ptrace할 수 있다는 가정 — Yama `ptrace_scope ≥ 1`(Ubuntu 기본)이면 그 세 키를 대조에서 뺀다. ② 고정 `RLIMIT_NPROC`=64 — 같은 uid의 러너 에이전트 스레드까지 세므로 node가 스레드를 못 만들어 20초 시간 초과(같은 uid 스레드 60개로 재현). 상한을 「현재 uid 태스크 수 + 64」로 둔다. 같은 uid 스레드 0·60·200개에서 29 passed.
7. **pillow·pypdf와 함께 lxml·charset-normalizer도 올렸다**(Phase 1 후속 4개 모두). pypdf는 원본 코어 이미지(6.19.0)보다 높은 6.21.4다.
8. **코어 Linux 시험 이미지(`packages/core/docker/test.Dockerfile`)를 루트 컨텍스트로 바꿨다.** 루트 `uv.lock`을 못 찾아 헤더의 빌드 명령이 실패했다. 격리 시험 29 passed(Docker Desktop, Landlock ABI 8).

**검증 결과**
| 항목 | 결과 |
|---|---|
| gitleaks | `v8.28.0` dir 모드, 추적 파일 전체: 0건(`.gitleaks.toml` 없이 돌린 1차 13건은 모두 테스트의 가짜 값 — `sk-test-…`, AWS 예시 키 형식(`AKIA` + 16자), Python 공식 GPG 지문 등) |
| 내부 참조 | `git grep -i` `<개인 계정>`·`<회사>`·`pkg.dev`·`PROJECT_ID`·`nip.io`·사내 MCP 호스트·`@gmail.com`·`dalli/agents-core`·`dalli/rapid-notes`: 0건(이 계획 문서 제외). 이메일은 `example.com`·`test.dev`·시드 `rapidnotes.dev`뿐, 사설 IP는 테스트 밖 0건, 시드에 실명 없음 |
| 라이선스 | api 런타임 152개·샌드박스 30개·web prod 542개 중 GPL·AGPL 전용 0개. 비허용형은 MPL-2.0(certifi·orjson·tqdm, dompurify 선택), tld(MPL-1.1/GPL/LGPL 3중), `@img/sharp-libvips-*`(LGPL-3.0, 동적 링크), caniuse-lite(CC-BY-4.0) — `NOTICE`에 고지. fastembed 메타데이터의 「Other/Proprietary」 분류는 실제 Apache-2.0(동봉 LICENSE 확인) |
| `make ci` | 통과(7분 10초). api 1,577 passed(커버리지 75.70%, 하한 75.0) · 코어 6,628 passed·32 skipped·1 xfailed · migrate-check · web 170파일 2,254(구문 90.67%) · 계약·Docker lock 드리프트 없음 |
| 새 clone | 같은 머신, 브랜치를 새로 clone해 다른 compose 프로젝트(`COMPOSE_PROJECT_NAME=biznotes-fresh`, 새 볼륨)로 README 순서대로: 무캐시 빌드(api·web, 베이스 이미지는 받아 둔 상태) 1분 40초 → `make up` 14초 만에 api·web 응답 → `make seed` → `make smoke` OK(빈 모델 캐시에서 임베딩 모델 다운로드 포함) → web `/login` 200. 끝나고 프로젝트·볼륨을 지웠다 |
| GitHub Actions | PR #4. 1차: frontend·contracts·docker 3개 초록, backend는 격리 시험 1건 실패(위 9-①). 2차: 다른 격리 시험 1건 실패(위 9-②), 나머지 초록 — api 1,577 passed(커버리지 75.71%)·코어 6,658 passed·1 skipped(러너에서는 Linux 전용 시험까지 돈다). 3차: backend(api 1,577 passed·코어 6,659 passed)·frontend·docker 초록, contracts의 gitleaks가 이 기록에 옮겨 적은 테스트용 AWS 예시 키 문자열을 잡았다 — 문구를 고쳤다(CI의 gitleaks가 문서까지 지키는 것을 확인한 셈이다). 4차는 그 수정 커밋이다. 잡 시간(2차, 캐시 있음): backend 17분, frontend 10분, contracts 1분, docker 각 30초 안팎(1차 무캐시: api 1.5분·web 4분·skill-sandbox 1.5분) |

**미검증**
- 다른 머신에서 README만 보고 `make up`(수락 기준). 같은 머신의 새 clone·새 compose 프로젝트·무캐시 빌드로 대신했다.
- `e2e.yml` 수동 실행(위 6).

**후속(Phase 4)**
- 스크린샷(`docs/images/screenshot.png`)에 「Rapid Notes v0.8.0」이 보인다 — 리브랜딩 뒤 다시 찍는다.
- 공개 직후 저장소 설정에서 **Private vulnerability reporting**을 켠다(SECURITY.md가 이 경로를 안내한다).
- `NOTICE` 저작권자는 「The biznotes Authors」로 두었다 — owner가 개인·법인 이름을 원하면 바꾼다.
- 주석의 원본 참조(`HCI-` 135파일, `docs/plans/…` 120파일, `AGENTS.md §…` 9파일, `#123` 이슈 번호)는 그대로다 — 비공개 정보는 아니지만 공개 저장소에서 찾을 수 없는 참조다. AGENTS.md 규칙 9에 안내했다.
- SearXNG `settings.yml`의 `secret_key` 값과 compose의 `searxng/searxng:latest` 핀이 남았다.
- 관리 「사용 제한」의 업로드 상한 경고가 지운 GKE ingress(`proxy-body-size`)를 안내한다 — 일반 리버스 프록시 문구로 바꿀 후보.

### Phase 4 — 리브랜딩 · 공개 (1일)

1. 이름: `biznotes`, `biznotes-api`, `@biznotes/shared-types`, 이미지 `biznotes/*`, DB·사용자 `biznotes`, 스킬 매니페스트 `biznotes.json`, 웹 브랜드 문구. 남은 `rapid-notes` 문자열 전부.
2. 패키지 리네임: `agents_core` → `biznotes_core`, `agents-core` → `biznotes-core`. `sed` + `git mv` 한 커밋. 경계 테스트 경로·coverage `source`·Dockerfile `COPY`·샌드박스 워커 모듈 경로 동반.
3. 버전 `0.1.0`, 태그 `v0.1.0`.
4. **공개 전 이력 검사**: 첫 스냅샷 커밋부터 모든 커밋이 공개된다. `gitleaks detect`(git 모드, 전체 이력)와 `git log -p` 대상 내부 참조 grep을 돌린다. 걸리는 것이 있으면 최종 트리로 orphan 커밋 1개를 만들어 이력을 단일화한 뒤 공개한다(작업 이력은 비공개 사본에 남긴다).
5. `dalli/biznotes`(이미 있음, 비공개)에서 CI 초록 확인 → `gh repo edit dalli/biznotes --visibility public --accept-visibility-change-consequences`. 브랜치 보호(`main`: CI 필수·PR 필수). 브랜치 모델 `main` ← feat/* 단일. 4번에서 이력을 orphan 커밋으로 단일화했다면 공개 전에 `main`을 강제 갱신하고 작업 브랜치를 지운다.
6. 원본 README에 링크 1줄(owner 판단).

**수락 기준**: 공개 저장소 CI 초록, `v0.1.0` 릴리스, `git grep -il "rapid.notes\|rapidnotes\|agents_core\|agents-core\|qdrant\|minio"` 결과가 `ORIGIN.md`·`NOTICE`·S3 extra 문서뿐.

#### Phase 4 구현 기록 (2026-11-01, 브랜치 `phase-4-rebrand` → 공개 저장소 orphan 커밋 `efa8022`)

`phase-3-hygiene`(PR #4) 위에 쌓았다. 커밋은 코어 패키지 리네임 → 제품 이름 → 버전 0.1.0 → SearXNG → 업로드 안내 → 낡은 저장소 언급 → 스크린샷 → 이 기록 순이다.

**계획과 달라진 점**
1. **브로커·저장 계약 몇 개가 이름을 따라 바뀌었다.** 샌드박스 태스크 이름 `RUN_SKILL_SCRIPT`는 모듈 경로라 `biznotes_core.workers.skill_sandbox.run_skill_script`가 된다. AGENTS.md 규칙 4는 태스크 이름을 바꾸지 않거나 옛 이름을 함께 등록하라고 하지만, 공개 전이라 옛 이름 메시지가 남은 배포가 없어 별칭을 두지 않았다. 같은 이유로 스킬 매니페스트 파일명(`biznotes.json`, 생성 도우미 출력 키 `biznotes_json`), advisory lock 네임스페이스(`service_name`), Celery 앱 이름, 스토리지 서명 HMAC 문맥 문자열, fetch User-Agent, 시드 계정 도메인(`biznotes.dev`)도 바꿨다 — v0.1.0부터는 계약이다.
2. **원본을 가리키는 서술은 이름을 바꾸지 않았다.** 코어 테스트의 출처 주석·Alembic 첫 리비전·compose 프로젝트명 주석·sl-agents 개명 이력은 「원본 제품 저장소」로 고쳤다. 기계 치환으로 생긴 `biznotes-core 표준 계획 D8`·`biznotes-core#218`·「코어 v0.18.0」 같은 원본 참조는 AGENTS.md 규칙 9에 안내했다. README·AGENTS.md는 원본 이름 대신 ORIGIN.md를 가리킨다.
3. **수락 기준의 grep은 문자 그대로는 맞출 수 없어 예외를 정했다.** 남은 파일은 모두 다음 중 하나다: 원본 이름(ORIGIN.md·NOTICE·이 문서), S3 extra가 실제로 쓰는 minio 클라이언트(`storage_s3.py`·`storage.py`·설정·pyproject·코어 README·ARCHITECTURE·Docker requirements·uv.lock, 그 클라이언트를 흉내 내는 테스트 대역 7파일), 샌드박스 env 차단 목록의 `MINIO_`(워커와 그 시험), fastembed 모델 ID `Qdrant/bm25`, pgvector 점수가 옮기기 전 Qdrant RRF와 같다는 설계 근거(vectorstore·test_vectorstore_pg·test_ingest_index_swap). 그 밖의 낡은 MinIO·Qdrant 언급(테스트 docstring·대역 이름·오류 문자열, 약 50파일)은 「스토리지」·`rustfs` 등으로 고쳤다.
4. **Phase 3 후속 셋을 함께 처리했다.** SearXNG를 날짜 태그(`2026.8.29-d226b78bc`, 쓰던 판과 같은 다이제스트)로 고정하고 `secret_key`를 `SEARXNG_SECRET` 환경 변수로 뺐다. 관리 「사용 제한」의 GKE ingress 100MB 경고는 문턱 없는 리버스 프록시 일반 안내로 바꿨다(§9 장부). 스크린샷은 같은 장면을 Playwright로 다시 찍었다.
5. **덜 알려진 함정 둘.** ruff는 한글을 폭 2로 세므로 「MinIO」→「스토리지」처럼 글자 수가 줄어도 줄이 100자를 넘을 수 있다(몇 줄을 다시 감쌌다). `skills/*.zip`은 안의 SKILL.md와 바이트까지 대조되므로 문구를 바꾸면 zip도 다시 만든다 — `current-datetime.zip`을 빠뜨려 api 테스트 1건이 실패했고 커밋을 고쳤다. `pnpm install`은 importer 이름 외에 무관한 `debug` peer 해석까지 바꿔, lock은 그 한 줄만 손으로 고쳤다(`--frozen-lockfile` 통과).
6. **개발 스택.** 기존 `biznotes` 프로젝트의 볼륨은 `rapidnotes` 역할로 초기화돼 새 기본값으로 붙지 않는다. 볼륨은 지우지 않고 멈춰 두었으며, 검증은 새 프로젝트(`COMPOSE_PROJECT_NAME=biznotes-v01`, 새 볼륨)로 했다.

**이력 검사(4단계) — 공개 전에 orphan 커밋으로 단일화해야 한다**
| 검사 | 결과 |
|---|---|
| gitleaks git 모드 | `v8.28.0`, `--log-opts=--all`(44커밋), `.gitleaks.toml` 적용: 1건 — `9d7b823`에서 이 문서에 옮겨 적은 테스트용 AWS 예시 키(가짜 값, `c1e2650`에서 지움) |
| 추가된 줄 grep(`git log --all -p`) | 첫 스냅샷 `0145ac3`: `PROJECT_ID`·`pkg.dev`·`nip.io`(cloudbuild·Helm GKE 값), `<개인 계정>` 11파일, `<회사>` 5, `dalli/agents-core` 11·`dalli/rapid-notes` 2, archhub 25, `use-change-review` 스킬(고객 보고서 파일명·건물 주소). 그 뒤 커밋은 이 문서의 검색어 목록뿐 |

**검증 결과**
| 항목 | 결과 |
|---|---|
| `make ci` | 커밋 1(코어 리네임)·2(제품 이름) 각각 통과 — 2는 zip을 고친 뒤 재실행. HEAD(커밋 7) 통과 7분 8초: api 1,577 passed(커버리지 75.70%, 하한 75.0) · 코어 6,628 passed·32 skipped·1 xfailed · migrate-check · web 170파일 2,253(구문 90.67% — GKE 경고 시험 2건을 안내 시험 1건으로 바꿈) · 계약·Docker lock 드리프트 없음. 커밋 3~6은 ruff·mypy와 손댄 시험 파일만 따로 돌렸다(버전·SearXNG·웹 시험 12건·정리 772건) |
| 새 compose 프로젝트 | `biznotes-v01`(새 볼륨): `make up` 2분 → `make seed` → `make smoke` OK. 웹 제목·로그인 문구 「biznotes」, 사이드바 「biznotes v0.1.0」, `/api/v1/ping` `biznotes-api`, 시드 스킬 5개의 매니페스트가 `biznotes.json`이고 `legacy_instruction_v1`로 인식됨, SearXNG JSON 검색 20건 |
| 스킬 샌드박스 | 이미지 빌드(설치 확인 단계 포함) 통과, 워커가 `biznotes_core.workers.skill_sandbox.run_skill_script`를 등록하고 ready(Landlock 확인 통과, Docker Desktop) |
| 수락 기준 grep | 위 3의 예외만 남는다 |

**미검증**
- 샌드박스에서 스크립트 스킬을 실제로 실행하는 왕복(태스크 등록까지만 확인). Linux 격리 시험은 GitHub Actions backend 잡에서 돈다.
- `e2e.yml` 수동 실행.
- 이력 저장소의 PR #5 CI는 6잡 모두 checkout에서 실패했다 — 실행 도중 저장소 이름을 바꿔 옛 이름을 찾지 못했다. 같은 트리를 공개 저장소 `main`에서 다시 돌려 초록을 확인했다(아래).

**공개 절차(owner 결정·승인 2026-11-01)**
- **같은 저장소를 공개로 돌리지 않았다.** GitHub는 PR마다 `refs/pull/N/head`를 지울 수 없게 남기고, PR #1의 head가 첫 스냅샷 커밋을 가리킨다. `main`만 orphan으로 바꿔 공개하면 그 참조로 옛 이력을 받을 수 있다. 그래서 계획(5)의 「같은 저장소 공개 전환」 대신 다음 순서로 했다.
  1. 옛 `dalli/biznotes`를 `dalli/biznotes-history`로 이름을 바꿨다(비공개 — 전체 이력·브랜치·PR #1~#5·CI 기록 보존, PR #4·#5는 안내 댓글을 달고 닫음). 시크릿·웹훅·배포 키는 없었다.
  2. 이 문서의 개인·회사 식별자를 `<개인 계정>`·`<회사>`로 가렸다(문서는 공개 트리에 둔다).
  3. 새 `dalli/biznotes`(비공개)를 만들고 `phase-4-rebrand` 최종 트리로 orphan 커밋 `efa8022` 하나를 push했다. 작성자·태거는 GitHub noreply 주소다(공개 위생 기준 「개인 이메일 0건」).
  4. `main` push CI 6잡 초록 → 태그 `v0.1.0`(주석 태그)·GitHub 릴리스 → 공개 전환 → `main` 브랜치 보호 → Private vulnerability reporting.

| 항목 | 결과 |
|---|---|
| 공개 저장소 CI(`main` push, 캐시 없음) | 6잡 초록. backend 16분(api 1,577 passed·커버리지 75.70%, 코어 6,659 passed·1 skipped — Linux 러너라 샌드박스 격리 시험이 새 모듈 경로로 돈다), frontend 9.5분, contracts 1분(gitleaks 포함), docker api 2분·web 4분·skill-sandbox 1.5분 |
| 공개 직전 미러 clone 검사 | 뿌리 커밋은 `efa8022` 하나(나머지는 Dependabot 브랜치·PR 참조). gitleaks git 모드(`--all`, `.gitleaks.toml`) 0건. 내부 참조 grep 일치는 이 문서의 검색어·제거 장부 서술뿐 |
| 릴리스 | `dalli/biznotes`의 `v0.1.0`(한/영 노트) — 공개 미러 전환 뒤에는 <https://github.com/dallisoft/biznotes/releases/tag/v0.1.0> |
| 브랜치 보호(`main`) | PR 필수(승인 0명 — 1인 유지보수), 필수 체크 6개(backend·frontend·contracts·docker 3), 관리자에게도 적용, 강제 push·삭제 금지. 최신 브랜치 요구(strict)는 끔 |
| Private vulnerability reporting | 켜짐(SECURITY.md가 안내하는 경로) |

**남은 owner 작업**
- 원본 README 링크(6) — 원본 저장소 변경은 비목표라 owner가 한다.
- `NOTICE` 저작권자(「The biznotes Authors」)를 개인·법인 이름으로 바꿀지.
- Dependabot이 공개 직후 PR 12개를 열었다(next 16·mcp 2·react-resizable-panels 4·jsdom 30·`@vitest/coverage-v8` 5·pytest-randomly 5 같은 주 버전 포함). 주 버전은 따로 검토한다.
- 이 저장소에서 커밋할 때 작성자 이메일이 공개된다 — 로컬 `user.email`을 noreply 주소로 둘지.
- 기존 개발 볼륨(`biznotes_pgdata` 등)은 `rapidnotes` 역할이다. `make clean` 뒤 `make up && make seed`로 새로 띄운다(데이터 삭제).
- 테스트의 예시 MCP 서버 이름 `archhub`(약 15파일)는 그대로다 — 엔드포인트는 `example.com`·`https://x`.

**공개 미러로 전환(owner, 2026-11-02)**

`dalli/biznotes`는 공개한 지 반나절 만에 다시 비공개로 돌렸다(그동안 fork·star·watcher 0). 공개 저장소는 `dallisoft/biznotes`이고, 작업 저장소의 `main`·태그를 그대로 따라가는 미러다.
- 동기화: 작업 저장소의 `mirror` 워크플로가 `main` push·`v*` 태그 push 때 미러로 민다. 인증은 미러의 쓰기 권한 배포 키(작업 저장소 시크릿 `MIRROR_DEPLOY_KEY`)이고, 강제 push는 하지 않는다. 미러에서는 저장소 이름 조건으로 돌지 않는다. 미러의 `ci`는 공개 저장소에서 그대로 돈다.
- 공개 저장소를 가리키는 주소(README clone, SECURITY.md·이슈 템플릿의 취약점 신고 링크)와 MCP `_meta` 키 이름공간(`io.github.dallisoft.biznotes/`)을 바꿨다. 외부 PR은 공개 저장소에 받고 유지보수자가 작업 저장소로 옮긴다(CONTRIBUTING).
- 브랜치 보호: 무료 플랜의 비공개 저장소에는 브랜치 보호가 없어 작업 저장소의 「PR 필수·CI 필수」는 강제되지 않는다(관례로 지킨다. 강제하려면 GitHub Pro). 미러 `main`은 강제 push·삭제만 막는다 — PR·체크를 요구하면 배포 키의 push가 거부된다. Private vulnerability reporting은 미러에서 켠다.
- PR #13을 병합한 커밋 `ce1f3b9`의 커미터는 GitHub 계정 이메일이다(웹 병합이 계정 이메일을 쓴다). owner가 그대로 미러하기로 했다.
- 미러에도 `dependabot.yml`이 실려 Dependabot이 미러에서도 PR을 열 수 있다(첫 동기화 직후 #1·#2). 의존성 갱신은 작업 저장소의 PR로 받는다.
- 미러의 이슈는 누구나 열고, PR은 협업자만 연다(저장소 설정 `pull_request_creation_policy=collaborators_only`, owner 2026-11-02). CONTRIBUTING(한/영)도 「코드 기여는 이슈에서 방향을 맞춘 뒤 협업자 초대」로 안내한다.

### Phase 5 — 이후 운영 규칙 (작업 없음)

- 원본에서 가져올 수정은 **수동 cherry-pick**. `ORIGIN.md` 표에 원본 PR과 biznotes 커밋. 경로·이름·저장소 계층이 달라 diff를 손으로 옮긴다 — 작게·자주.
- 제거 장부 항목은 "원본 경로 + 기준 커밋"으로 되살린다.
- v0.2 후보: PostgreSQL 단일화(E11), fastembed extra(E10), HNSW 인덱스(활성 공간 차원 고정), 자동화·스케줄 복원.

## 6. 롤백 · 호환성

- Phase 4 공개 전까지 로컬(또는 비공개) 저장소. 어느 단계든 버리고 다시 시작해도 원본에 영향이 없다.
- 공개 뒤 되돌릴 것은 "공개 상태"뿐. 비밀 유출 시 비공개 전환 + 키 회전 — Phase 3 스캔이 그 전에 막는다.
- **원본 DB·Qdrant·MinIO 데이터를 biznotes에 붙이지 않는다.** 스키마(squash·pgvector 컬럼)·스토리지 경로가 다르다. 이전 도구는 비목표.

## 7. 위험

| 위험 | 영향 | 완화 |
|---|---|---|
| 제거가 포함 기능을 조용히 깨뜨린다 | 런타임 오류 | 기능 단위 커밋 + `make ci` + smoke 반복. 조용한 무시 대신 명시적 "지원 안 함" |
| 샌드박스 + 로컬 파일 스토리지의 격리 구멍 | 스크립트가 다른 사용자 파일을 읽음 | 샌드박스에 볼륨을 마운트하지 않는다. 파일은 작업마다 발급한 단일 객체·단일 메서드·짧은 만료의 HMAC 토큰으로만 api 내부 엔드포인트에서 주고받는다. 토큰 위조·만료·경로 탈출 테스트 |
| Landlock ABI 6 요건이 사용자 호스트에서 안 맞음 | 샌드박스 워커 기동 거부 | README 요건 명시 + 샌드박스 없이도 나머지가 돌도록 `--profile skill-scripts`로 분리(원본 방식 유지) |
| pgvector 정확 탐색의 성능 | 노트북당 청크 수만 개에서 검색 지연 | 노트북 필터가 선행된다. 장부에 1만·10만 청크 측정값. 임계 초과 시 HNSW(차원 고정) |
| 하이브리드 융합 결과가 Qdrant RRF와 미세하게 다름 | 테스트 기대값 차이 | 순위 동률 처리를 명시(점수→chunk_id 정렬). 기대값은 순서가 아니라 집합·상위 k로 |
| 로컬 파일 스토리지의 다중 노드 | 수평 확장 불가 | 셀프호스트 단일 노드가 목표. 확장은 `s3` 백엔드 |
| 단일 lock 해석 충돌(69개 상이) | Phase 1 지연 | 제품 lock 우선. Phase 2에서 무거운 의존성이 빠지며 충돌면이 준다 |
| 비공개 이력·문서가 주석·픽스처에 남아 공개됨 | 정보 유출 | 스냅샷 + gitleaks + grep + 시드·픽스처 점검. 첫 스냅샷 커밋부터 공개되므로 Phase 4에서 **이력 전체**를 검사하고, 걸리면 orphan 커밋으로 단일화 |
| 서드파티 라이선스 누락 | 공개 뒤 문제 | `NOTICE` + `pip-licenses`·`pnpm licenses` 결과를 장부에 |
| 커버리지 재설정이 "하향"으로 보임 | 기준 혼선 | biznotes는 Phase 2 끝 실측으로 새 기준선. 장부에 전후 값 |

## 8. 비용 · 순서

| 단계 | 내용 | 예상 |
|---|---|---|
| 1 | 전체 스냅샷 기준선(워크스페이스·Docker 3이미지·거버넌스 잔재 제거) | 1.5일 |
| 2a | 경량화(확정 범위) + 워커·migrate 통합 | 2.5~4일 |
| 2b | Valkey → 로컬 스토리지(샌드박스 서명 URL 포함) → pgvector → Alembic squash | 4.5일 |
| 3 | 공개 위생·문서·CI | 1일 |
| 4 | 리브랜딩·리네임·공개 | 1일 |

총 **10.5~12일**. Phase 2b는 2a와 독립이라 순서를 바꿔도 된다(2a 먼저가 싸다 — 제외 기능의 스토리지 호출부까지 고치지 않아도 된다).

## 9. 제거 장부 (Phase 2에서 채운다)

| 기능 | 뺀 것(경로) | 이유 | 되살릴 때 원본 경로 @ 기준 커밋 | 이미지·기동 시간 변화 |
|---|---|---|---|---|
| 운영 인프라 | `infra/{helm,gke-scaler,caddy}/`, `cloudbuild.yaml`, `compose.prod.yml`·`compose.rollback.yml`, `env-contract.yaml`, `infra/scripts/{backup,audit-schema-drift,probe-api-image}.sh`, Makefile `audit-drift` | 셀프호스트 compose만 둔다. `smoke.sh`는 개발 검증용으로 남긴다 | rapid-notes `infra/…` @`415824f4` | — |
| 거버넌스·정합 스크립트 | `apps/api/scripts/{check_container_timezone,check_env_contract,compare_freeze,core_v0_11_0_reconcile}.py`와 테스트 | 원본의 게이트·일회성 데이터 정합 | rapid-notes `apps/api/scripts/…` @`415824f4` | — |
| 네이버 리서치 MCP | `apps/mcp-naver-research/`, compose 서비스·`naver_research` 네트워크, 시드 「종목 리포트 수집」, 워크플로 테스트, 워크스페이스 exclude | 국내 증권 특화 앱 | rapid-notes `apps/mcp-naver-research/`, `scripts/seed.py` naver 구간 @`415824f4` | — |
| 휴가 예제 | `scripts/vacation_mcp_server.py`, `scripts/data/sme-employment-rules.md`, 시드(휴가 프로필·사용 내역·MCP 서버·「내 연차 잔여일 확인」·취업규칙 노트북)와 관련 테스트 부분 | 사내 HR 예제. 모델 `EmployeeVacation*`는 Alembic squash 때 뺀다 | rapid-notes `scripts/seed.py` vacation 구간 @`415824f4` | — |
| RAG 평가 도구 | `scripts/rag_eval*.py`·`*.json`, `test_rag_eval_grounding.py`, 관리자 RAG 화면의 안내 문구 | 개발용 측정 도구 | rapid-notes `apps/api/scripts/rag_eval*` @`415824f4` | — |
| 스킬 운영 도구 | 코어 `services/skills/{cli,evaluation}.py`, `scripts/skills_emergency_private.py`, `tests/fixtures/skills/oracle-src/`(skills-ref 0.1.0 사본)와 CLI·평가 테스트 | 저작 CLI·평가 하네스·긴급 비공개 도구. 오라클 사본은 해시 증거용이며 테스트는 `golden-corpus.json`만 읽는다 | agents-core `agents_core/services/skills/{cli,evaluation}.py` @`5671bfa` | — |
| HWP/HWPX | 코어 `ingestion/{hwp,hwp_markdown,hwp_route,hwp_sanitize,hwp_structure,rhwp_launcher}.py`, `nodes/hwp_tools.py`, 업로드 관문의 HWP·HWPX 분기(ZIP 구조 검사 포함 — 관문 379→139줄), 파서 등록, 인제스트의 전용 워커 핸드오프·구조 사본 업로드·HWP 큐 라우팅, 워커 소비 큐 기록 시그널, `hwp_*`·`hwpx_*`·`rhwp_bin` 설정과 PDF 상한 검증기, rhwp 메트릭·이벤트 이름, 에이전트 도구 `hwp_outline`·`hwp_fields`, compose `worker-hwp`·`hwp_isolated`·`HWP_*` env, api 이미지 rhwp 스테이지·`fonts-noto-cjk`·모델 베이크(`/opt/models`), 샌드박스 이미지 rhwp·`fonts-noto-cjk`·HWP 스모크, 웹 업로드 허용 확장자·워크플로 HWP 도구 옵션·관리자 RAG 위치 안내, e2e `hwp-upload`, HWP 전용 테스트 13파일 | 한국 전용 포맷 + 외부 바이너리 + 전용 격리 워커. 기본값도 꺼져 있었다. 남긴 것: 경고 코드 상수(웹 라벨과 짝), 청크 위치(locations) 파이프라인과 파싱 중단 토큰(`abort.py`) — 지금은 생산자가 없어 후속 정리 후보 | agents-core `agents_core/services/ingestion/hwp*.py`·`rhwp_launcher.py` @`5671bfa`, rapid-notes `apps/api/Dockerfile` rhwp 스테이지 @`415824f4` | 아래 PDF 내보내기 행에 묶어 잰다 |
| OCR(Docling) | 파서의 Docling·RapidOCR 경로(`parse_pdf_ocr`·`ocr_pdf`·변환기 캐시)와 빈 쪽 신호, 인제스트의 OCR 재시도·`source.ocr_used` 기록, 경고의 OCR 뒤 보안 신호 필터, 응답 스키마 `ocr_used`와 웹 「OCR 처리됨」 배지, api 의존성 `docling[rapidocr]`·`opencv-python-headless`와 루트 opencv override, 이미지 빌드의 NVIDIA wheel 예외, OCR 테스트(코어·api `test_ingestion_ocr.py`, 파서 OCR 4건) | 가장 무거운 의존성. 함께 빠진 런타임 패키지 69개(torch·torchvision·transformers·NVIDIA CUDA 라이브러리 포함). 글자 층이 없는 PDF는 이제 「스캔(이미지) PDF는 지원하지 않습니다」로 거부한다. 일부 쪽만 빈 PDF는 글이 있는 쪽만 쓴다. DB 컬럼 `sources.ocr_used`는 Alembic squash 때 뺀다 | agents-core `agents_core/services/ingestion/parsers.py` OCR 구간·`workers/tasks/ingestion.py` `_maybe_ocr_retry` @`5671bfa` | 아래 PDF 내보내기 행에 묶어 잰다 |
| 오디오·유튜브 | 코어 `gateway/audio.py`(TTS·STT 호출), `nodes/audio.py`(워크플로 노드 `tts`·`merge_audio`), `ingestion/stt_fallback.py`, 파서의 유튜브 자막(`parse_youtube`·`extract_youtube_id`), 대화 대본 스키마 `dialogue`, 소스 종류 `youtube`(모델 튜플·응답 열거형·생성 스키마), api 의존성 `youtube-transcript-api`·`yt-dlp`, 이미지 `ffmpeg`, 시드의 「오디오 오버뷰」·대본 프롬프트·모의 TTS·STT 모델, 웹 스튜디오 오디오 카드·워크플로 `tts` 노드·유튜브 소스 종류와 감지, 관련 테스트(코어 TTS 게이트웨이·PG 오디오, api `test_audio.py` 등) | 시스템 패키지(ffmpeg) + 외부 서비스 약관(유튜브). 유튜브 URL은 이제 일반 웹 URL로 들어간다. 남긴 것: 모델 종류 `tts`·`stt`와 사용자 일일 TTS·STT 한도(DB 부분 유일 인덱스·컬럼과 묶여 있어 Alembic squash 때 뺀다), 범용 산출물 오디오 뷰어, 스킬 매니페스트의 `tts` 권한 키(읽는 곳이 없다 — 매니페스트 호환용) | agents-core `agents_core/services/{gateway,agents/nodes}/audio.py`·`ingestion/stt_fallback.py` @`5671bfa`, rapid-notes `scripts/seed.py` 오디오 오버뷰 @`415824f4` | 아래 PDF 내보내기 행에 묶어 잰다 |
| PDF 내보내기 | 코어 `services/notebook_export.py`·`api/v1/notebook_exports.py`·`workers/tasks/notebook_export.py`·`schemas/notebook_export.py`, 태스크 이름 `RUN_EXPORT`·`REAP_ORPHANED_EXPORTS`와 라우팅·beat 「reap-orphaned-exports」, 이벤트 채널, 제품 라우터 등록, api 의존성 `weasyprint`·`markdown`, 이미지의 Pango·cairo·GDK-PixBuf apt 패키지(이로써 api 이미지 apt 설치 단계가 사라졌다), 웹 노트북 헤더 「내보내기」 메뉴·다이얼로그·훅, 관련 테스트 | 시스템 패키지 5종. 비소유자의 노트북 헤더에는 더보기 메뉴가 없어졌다(유일한 항목이 내보내기였다). 남긴 것: 모델 `NotebookExport`와 열거형 `ExportFormat`·`ExportStatus`, 객체 정리의 내보내기 참조 — Alembic squash 때 뺀다 | agents-core `agents_core/services/notebook_export.py` 등 @`5671bfa`, rapid-notes `apps/web/src/components/notebooks/export-notebook-dialog.tsx` @`415824f4` | 모달리티 4묶음(HWP·OCR·오디오·PDF) 뒤 api 이미지 12.3 GB → 1.25 GB, skill-sandbox 827 MB → 625 MB, web 366 MB 그대로 |
| 외부 계정 커넥터 | 코어 `services/connectors/{base,google,microsoft,mail,attachments,tokens,state,errors}.py`, `schemas/connection.py`, 인제스트의 원격 받기(`_fetch_connector`)와 관문의 커넥터 전용 함수(`canonical_mime`·`direct_ingest_mimes`), 중복 키 조회의 `connection_id` 조건, 소스 종류 `gmail`·`gdrive`·`outlook`·`onedrive`(모델 튜플·응답 열거형), 열거형 `ConnectionProvider`·`ConnectionStatus`·`RemoteItemKind`, 설정 `google_connector_*`·`ms_connector_*`·`connector_redirect_base_url`·`connector_max_download_bytes`·`rate_limit_connector`(관리자 레이트 리밋 키 포함), 제품 `connections` 라우터와 `ConnectionTokenError` 409 매핑, compose·`.env.example`의 커넥터 env, 웹 `/connections`·`/connections/guide`·소스 추가 「연결된 계정」 탭·빈 대화 화면의 제공자 카드·사용자 메뉴와 명령 팔레트 항목·감사 라벨 `connection.*`, e2e `connector-attachments`, 커넥터 테스트(코어 4·PG 1·api 2파일) | OAuth 앱 등록 부담. 남긴 것: 웹훅 서명 검증 `connectors/{github,slack}.py`(자동화와 함께 뺀다), 모델 `UserConnection`과 `sources.connection_id` 열(Alembic squash 때 뺀다) | agents-core `agents_core/services/connectors/` @`5671bfa`, rapid-notes `apps/api/app/api/v1/connections.py`·`apps/web/src/app/(app)/connections/` @`415824f4` | 런타임 의존성 변화 없음(httpx만 썼다) |
| OIDC/Keycloak | 코어 `api/v1/oidc.py`(상태·로그인·콜백·JIT 가입)와 `oidc_*` 설정, 제품 라우터 등록, compose `keycloak` 서비스(`sso` 프로파일)와 api의 `OIDC_*` env, `.env.example` SSO 구간, 웹 로그인 화면의 「○○로 로그인」 버튼과 상태 조회, 테스트(코어 `test_oidc_email_verified.py`, api `test_search_oidc.py`의 OIDC 구간 — 나머지는 `test_search.py`로 이름을 바꿨다) | 로컬 계정 인증만 둔다 | agents-core `agents_core/api/v1/oidc.py` @`5671bfa`, rapid-notes `infra/compose.yml` keycloak·`apps/web/src/app/login/page.tsx` @`415824f4` | 선택 프로파일 서비스 1개 감소, 런타임 의존성 변화 없음 |
| 관측·추적 | 코어 `services/gateway/tracing.py`(Langfuse)와 호출부 5곳(채팅·개요·소스 가이드·llm 노드·이미지), `langfuse_*` 설정, api 의존성 `langfuse`(런타임 패키지 3개 감소: langfuse·backoff·wrapt), compose `langfuse`·`prometheus`·`grafana` 서비스(`observability` 프로파일)와 볼륨·api의 `LANGFUSE_*` env, `infra/{prometheus,grafana}/`, `.env.example` 관측성 구간, 제품 `monitoring` 라우터의 요약·비용 추이(`/monitoring/summary`·`/monitoring/cost-trend`), 웹 「실행 기록」 화면(`/monitoring`)·주 메뉴·명령 팔레트 항목, 테스트(api `test_tracing.py`, 요약·추이 테스트와 다른 테스트의 요약 단언, 코어 테스트 13파일의 트레이싱 무력화 패치) | 셀프호스트 기본 구성에서 외부 관측 스택을 뺀다. 남긴 것: `/metrics`(Prometheus 형식 노출)와 앱 메트릭, 노드별 실행 로그 `GET /agent-runs/{id}/logs`(실행 상세 화면이 쓴다 — `run_logs.py`로 옮겼다) | agents-core `agents_core/services/gateway/tracing.py` @`5671bfa`, rapid-notes `apps/api/app/api/v1/monitoring.py`·`apps/web/src/app/(app)/monitoring/`·`infra/{prometheus,grafana}/` @`415824f4` | 선택 프로파일 서비스 3개 감소 |
| 자동화(스케줄·웹훅·내부 이벤트) | 코어 `api/v1/automations.py`(`/me/agent-*`)·`api/v1/admin/automation.py`, `schemas/automation.py`, `services/{automation,cron,event_triggers,unattended_runs}.py`, `services/connectors/`(웹훅 서명 검증 `github`·`slack` — 패키지째), `workers/tasks/scheduler.py`와 태스크 이름 `TICK_SCHEDULES`·라우팅·beat 「tick-agent-schedules」, 설정 `agent_automation_user_max_*`·`agent_schedule_user_min_interval_minutes`, 제품 `webhooks` 라우터·노트북 스케줄 배지 `GET /notebooks/{id}/agent-schedules`·노트 생성의 `note_created` 이벤트 발화·`CronError` 422 매핑, 웹 「자동화」(`/automation`)·「관리 › 자동화」·에이전트 패널 스케줄·자동 중지 배지·`cron-describe`·`automation-labels`·스케줄 시간대 선택과 시간대 지정 포맷터·감사 라벨 `agent_schedule.*`·`agent_webhook.*`, e2e `admin-automation`·`my-automation`·`schedule-timezone`, 테스트(코어 8·PG 7·api 6파일과 다른 테스트의 스케줄 틱·웹훅 구간) | 무인 실행 경로를 통째로 뺀다 — 실행은 사용자가 직접(수동·채팅) 시작한다. 남긴 것: 모델 `AgentSchedule`·`AgentWebhook`과 `TRIGGER_SOURCES`(Alembic squash 때 뺀다), 워커의 trigger별 노트북 권한 재검사(모르는 trigger는 쓰기 이상 — fail-closed), beat(정체 소스·고아 run·벡터 고아 회수) | agents-core `agents_core/services/{automation,cron,event_triggers,unattended_runs}.py`·`api/v1/automations.py` @`5671bfa`, rapid-notes `apps/api/app/api/v1/webhooks.py`·`apps/web/src/app/(app)/automation/` @`415824f4` | 런타임 의존성 변화 없음 |
| 사용량 원장·차트 | 코어 `services/{usage_ledger,usage_report}.py`, `core/usage_scope.py`, `schemas/usage.py`, `api/v1/usage.py`(`/me/usage/daily`), 관리자 `/admin/usage`·`/usage/daily`·`/usage/breakdown`, 보존 정리 태스크 `workers/tasks/usage.py`와 태스크 이름 `PURGE_USAGE_EVENTS`·라우팅·beat, 설정 `usage_events_retention_days`, 게이트웨이(LLM 스트림·도구 턴·이미지·리랭크·임베딩)의 호출별 원장 기록과 `_tool_turn`의 `scope` 인자, 검색·소스·RAG·인제스트·개요·소스 요약·에이전트 그래프·채팅의 귀속 범위와 대기열 비우기, 워커 러너의 원장 flush, 웹 「사용량」(`/usage`)·「관리 › 사용량」·관리 개요 「이번 달 토큰」 카드·사용자 메뉴 항목, `recharts`·`react-is`, e2e `usage-page`·`admin-usage-chart`·`admin-usage-breakdown`, 테스트(코어 6·PG 4·api 2파일과 다른 테스트의 원장 단언·대역) | 셀프호스트 기본값에서 원장 쓰기와 차트를 뺀다. 남긴 것: 일일 한도(`usage_guard` — Redis 카운터, 원장과 무관)와 관리 「사용 제한」, 감사 로그 `GET /admin/audit-logs`(`admin/audit.py`로 옮겼다), 모델 `UsageEvent`(Alembic squash 때 뺀다) | agents-core `agents_core/services/usage_ledger.py`·`usage_report.py`·`core/usage_scope.py`·`api/v1/admin/usage.py` @`5671bfa`, rapid-notes `apps/web/src/app/(app)/usage/`·`admin/usage/`·`components/usage/` @`415824f4` | 웹 의존성 `recharts`·`react-is` 제거 |
| 퀵 캡처 | 코어 `api/v1/capture.py`(`POST /capture`)와 스키마 `CaptureCreate`·`CaptureOut`, 제품 라우터 등록, 웹 `/capture`·`/capture/guide`(북마클릿 안내)·`use-capture`, 사용자 메뉴 「퀵 캡처 설정」, PWA `manifest.json`의 `share_target`, 테스트(api `test_capture_api.py`, PG 소스 상한의 캡처 구간, 화면 랜드마크의 캡처 구간) | 북마클릿·공유 대상 진입점은 소스 추가 다이얼로그로 대신한다 | agents-core `agents_core/api/v1/capture.py` @`5671bfa`, rapid-notes `apps/web/src/app/(app)/capture/` @`415824f4` | — |
| 인트로 | 웹 공개 소개 페이지 `app/intro/`와 `components/intro/`(문구 레코드·섹션·제품 일러스트·`intro-rules.test-kit`), 로그인 화면 「소개 보기」 링크, 사용자 메뉴 「소개」 항목, 화면 대비 테스트의 소개 색 쌍 I1~I5와 픽스처 2건, e2e `intro.spec.ts` | README가 대신한다. 남긴 것: 사용자 메뉴는 「비밀번호 변경」만 남고, 항목이 없으면(구 API) 드로어 「계정」 섹션을 그리지 않는다 | rapid-notes `apps/web/src/app/intro/`·`apps/web/src/components/intro/` @`415824f4` | — |
| 워커·beat·migrate 서비스 통합(E9) | compose `worker-ingestion`·`worker-agent`·`beat`·`migrate` 서비스 → 통합 `worker`(`-Q ingestion,agent -B`, api healthy 뒤 기동) 하나. api는 `scripts/start.sh`가 `alembic upgrade head`를 먼저 돌린다(`BIZNOTES_AUTO_MIGRATE`, 기본 켬 — 끄면 따로 적용). `make migrate`는 `exec api alembic upgrade head`로 바꿨다. `requires-python = ">=3.12,<3.13"`(세 프로젝트)와 `.python-version` 3.12 — 호스트 개발 Python을 이미지와 맞춘다 | 셀프호스트 기본 구성을 api·worker·web + 데이터 서비스로 줄인다. 남긴 것: beat 주기 작업(정체 소스 재발행·고아 run 회수·벡터 고아 정리), 샌드박스 워커(`skill-scripts` 프로파일) | rapid-notes `infra/compose.yml` 워커·beat·migrate @`415824f4` | 워커 계열 기본 서비스 3개(worker-ingestion·worker-agent·beat) → 1개, 일회성 migrate 서비스 제거. lock이 3.12 wheel만 남겨 `uv.lock` 1,763줄·Docker requirements 해시 1,792줄 감소, 패키지 목록은 같다 |
| 남은 표·열과 Alembic squash | 표 `employee_vacation_profiles`·`employee_vacation_usages`·`user_connections`·`notebook_exports`·`agent_schedules`·`agent_webhooks`·`usage_events`, 열 `sources.connection_id`·`sources.ocr_used`·`usage_limits.daily_tts_chars`·`daily_stt_minutes`, 능력 `tts`·`stt`(모델 튜플·응답 열거형·`uq_llm_models_default_{tts,stt}`·웹 능력 목록·관리 LLM 필터), `TRIGGER_SOURCES`를 `("manual", "chat")`로 줄여 `models/agent.py`로 옮김, 열거형 `ExportFormat`·`ExportStatus`, 일일 한도 TTS·STT(가드·관리자 사용자 한도·「사용 제한」 화면), 디스커버리의 음성 모델 능력 추론(이제 목록에서 뺀다), 객체 정리·스킬 wrapper 삭제의 내보내기·스케줄·웹훅 참조, 코어 `models/skill_backfill.py`(옛 리비전 전용 SQL)와 그 해시 고정 테스트. Alembic: rapid-notes 54개 리비전 + 제거 리비전 → `0001_biznotes_initial_schema` 하나, `tests/test_migration_core_v0_*.py` 18개 | 새 제품이라 옛 배포의 스키마를 이어받을 필요가 없다. 검증: 옛 체인 head DB와 squash DB의 열 330·제약 101·인덱스 92를 비교했고, 다른 점은 의도한 3가지다 — 옛 리비전이 백필용으로 둔 server default 16개(모델에는 없다), `prompt_*` 시각 열의 `now()` 기본값(모델대로), `user_groups` 기본 키 이름(`uq_user_group` → 모델 기본 `user_groups_pkey`). autogenerate가 빠뜨린 `use_alter` 외래 키 `fk_chat_messages_run_id`는 손으로 더했다. `alembic check` 깨끗, downgrade base·재upgrade 확인 | rapid-notes `apps/api/alembic/versions/`·`apps/api/tests/test_migration_core_v0_*.py` @`415824f4` | 리비전 파일 55개 → 1개 |
| Redis → Valkey | compose `redis` 서비스(`redis:7-alpine`)·`redis_data` 볼륨 → `valkey`(`valkey/valkey:8-alpine`)·`valkey_data`. 환경 변수는 `VALKEY_URL`(설정 필드 `redis_url`, `REDIS_URL` 별칭) | 공개 저장소의 기본 구성을 BSD-3 라이선스로 둔다(Redis는 7.4부터 RSALv2/SSPLv1, 8.0부터 AGPLv3 선택이다). 프로토콜 호환이라 코드 변경 없다 | rapid-notes `infra/compose.yml` redis @`415824f4` | — |
| MinIO | compose `minio` 서비스·`minio_data` 볼륨·`MINIO_*` env, 코어 런타임 의존성 `minio`(→ `s3` extra), `storage.get_minio`·`presigned_get_url`(사용처 없음), 설정 `minio_*`(→ `storage_bucket_*`·`s3_*`), 열 `sources.minio_object_key`(→ `original_object_key`), 소스 본문 조회의 `S3Error` 분기(→ `ObjectNotFound`) | 셀프호스트 기본은 로컬 파일(`/data` 볼륨). S3 호환 서버는 `STORAGE_BACKEND=s3`로 그대로 쓴다(`storage_s3.py` — 옛 코드) | agents-core `agents_core/services/storage.py` @`5671bfa` | compose 서비스 1개 감소 |
| Qdrant | compose `qdrant` 서비스·`qdrant_data` 볼륨·`QDRANT_URL`, 의존성 `qdrant-client`(+`grpcio`·`portalocker`), Qdrant `vectorstore.py`(컬렉션·payload 인덱스·스크롤), `embedding_spaces`의 컬렉션 관리(`collection_for_key`·`EmbeddingSpace.collection`), 색인 교체의 스냅샷·포인트 보상 삭제, 태스크 `cleanup_source_vectors`·`reconcile_vector_orphans`와 beat 「reconcile-vector-orphans」, 설정 `qdrant_url`·`vector_reconcile_enabled`, 열 `source_chunks.vector_id`, `scripts/reindex_qdrant.py`·`make reindex`, 테스트(고아 회수 `test_vector_orphan_reconcile.py`, Qdrant 내부를 보던 대역) | 벡터를 청크 행(pgvector)에 둔다 — 색인 교체가 한 트랜잭션이라 고아·보상이 없다. 재색인은 관리 「RAG 재인덱싱」으로 | agents-core `agents_core/services/vectorstore.py`·`workers/tasks/ingestion.py` 회수 구간 @`5671bfa`, rapid-notes `apps/api/scripts/reindex_qdrant.py` @`415824f4` | compose 서비스 1개 감소, api 이미지 1.25 → 1.22 GB |
| opt-in PG 스위트 | 디렉터리 `packages/core/tests_pg/`(→ `tests/*_pg.py`), `make core-pg-test`, `CORE_TEST_REDIS_URL`(→ `CORE_TEST_VALKEY_URL`), 인메모리 Qdrant 픽스처 | 코어 테스트를 PG 필수 단일 스위트로 — `make ci`가 PG 테스트도 돈다 | agents-core `tests_pg/` @`5671bfa` | 코어 pytest 2분 30초 → 4분 15초~4분 50초 |
| 샘플 스킬 use-change-review | `skills/use-change-review/`(SKILL.md·`rapidnotes.json`·references 5개)·`.README.md`·`.zip` | 건축물 용도변경 검토 보고서 스킬. E1 샘플 목록(요약·액션 아이템·마인드맵·current-datetime)에 없고, 실제 고객 보고서 파일명·건물 주소를 예시로 담고 있었다(Phase 3 스캔에서 발견) | rapid-notes `skills/use-change-review/` @`415824f4` | — |
| 채팅의 archhub 선행 조회 | 제품 `api/v1/chat.py`의 `_explicit_archhub_lookup`(관리자 질의에 "mcp"·"archhub"가 있으면 archhub 서버 `find_region`·`building_profile`을 먼저 호출), 답변 뒤 조회 결과 덧붙이기, 모든 채팅 시스템 프롬프트의 건물정보 도구 선택 규칙, 테스트 4건(`test_chat_api.py`) | 한 배포(건축물 대장 MCP)를 위한 분기. E1 「특화 앱」과 같은 기준. 외부 MCP 도구는 에이전트·워크플로의 MCP 클라이언트로 그대로 쓴다 | rapid-notes `apps/api/app/api/v1/chat.py` @`415824f4` | — |
| 디렉터리별 AGENTS.md | `apps/**/AGENTS.md`·`packages/AGENTS.md`·`packages/core/AGENTS.md`·`packages/shared-types/AGENTS.md` 22개 | 원본 거버넌스(레인·보호 파일·에스컬레이션)와 Qdrant·MinIO·Redis·`tests_pg`·`UV_NO_SOURCES` 서술이 낡았다. E7대로 루트 1쪽 AGENTS.md와 docs/ARCHITECTURE.md로 합쳤다 | rapid-notes 각 경로 @`415824f4`, agents-core `AGENTS.md` @`5671bfa` | — |
| 업로드 상한 GKE 경고 | 웹 `INGRESS_PROXY_BODY_LIMIT_MB`(100)와 관리 「사용 제한」의 `proxy-body-size`(100m) 경고 | Helm·GKE 배포를 뺐다. 문턱 없는 리버스 프록시 일반 안내로 바꿨다(Phase 4) | rapid-notes `apps/web/src/lib/core-limits.ts`·`components/admin/limits/source-upload-section.tsx` @`415824f4` | — |
