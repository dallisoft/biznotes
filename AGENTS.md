# AGENTS.md

이 저장소에서 일하는 사람과 코딩 에이전트를 위한 한 쪽 안내다. 구조는 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), 개발 환경은 [CONTRIBUTING.md](CONTRIBUTING.md)에 있다.

## 지도

| 경로 | 내용 |
|---|---|
| `packages/core/biznotes_core/` | 백엔드 코어(모델·스키마·서비스·공통 라우터·워커). 패키지 설명은 `packages/core/README.md` |
| `apps/api/` | FastAPI 제품 앱(설정 상속·라우터 조립·제품 라우터), Alembic, 시드 스크립트 |
| `apps/web/` | Next.js 프론트엔드. Vitest 단위 테스트, `e2e/`는 Playwright(수동) |
| `packages/shared-types/` | api OpenAPI에서 생성한 TS 타입 — 손으로 고치지 않는다 |
| `infra/` | compose(기본 + 개발 오버레이), SearXNG 설정, smoke |

## 명령

```bash
make up && make seed          # 스택 기동(PG 25433·Valkey 26379·api 28000·web 23000) + 시드
make ci                       # PR 전 필수 — ci-backend + ci-frontend + ci-contracts (스택이 떠 있어야 한다)
make core-test                # 코어만: ruff·mypy·pytest(PG 포함)·경계 규칙
cd apps/api && uv run python -m pytest tests/test_x.py   # api 테스트 하나
pnpm --filter web test        # 웹 단위 테스트
pnpm gen:types                # API 응답을 바꾼 뒤 — shared-types 재생성
make lock-docker              # uv.lock을 바꾼 뒤 — Docker requirements 재생성
make migrate-check            # 모델을 바꾼 뒤 — 새 DB에 upgrade + alembic check
```

## 규칙

1. **범위**: 요청한 것만 바꾼다. 무관한 정리는 따로 커밋한다. 기능 하나 = 커밋 하나, 커밋마다 `make ci`가 초록이어야 한다.
2. **계층**: 코어는 `app.*`를 import하지 않고, 코어 안의 import 방향은 [ARCHITECTURE.md §2](docs/ARCHITECTURE.md#코어-계층-규칙) 표를 따른다(`packages/core/tests/test_boundary.py`가 막는다). 서비스·라우터는 태스크 모듈을 import하지 않고 `workers/task_names.py`의 이름으로 발행한다.
3. **설정**: import 시점에 설정을 읽지 않는다(`get_settings()`는 함수 안에서). 배포마다 다른 값은 하드코딩하지 않고 설정으로 올린다.
4. **계약은 같은 커밋에**: API 응답을 바꾸면 `pnpm gen:types` 결과를, 모델을 바꾸면 `apps/api/alembic/versions/`의 새 리비전(autogenerate 뒤 손으로 확인)을, 의존성을 바꾸면 `uv.lock`과 `make lock-docker` 결과를 함께 넣는다. Celery 태스크 이름은 브로커에 남는 계약이라 바꾸지 않는다(바꿔야 하면 옛 이름을 함께 등록).
5. **테스트**: 동작을 바꾸면 테스트를 더하거나 고친다. 검증 대상 자체를 mock으로 바꾸지 않는다. 코어의 `*_pg.py`는 실제 PostgreSQL(pgvector)·Valkey를 쓴다. 커버리지 하한(`apps/api/pyproject.toml` `fail_under`, `apps/web/vitest.config.ts` `thresholds`)은 내리지 않는다.
6. **비밀**: `.env`·키 파일을 열거나 커밋하지 않는다. 테스트·픽스처에는 누가 봐도 가짜인 값만 쓴다(CI가 gitleaks를 돈다).
7. **개발 스택의 함정**: dev compose는 `apps/api/app`·`apps/api/alembic`만 마운트한다. api는 핫리로드되지만 worker는 `make restart s=worker`, 의존성·Dockerfile·웹 변경은 `make rebuild s=<서비스>`가 필요하다.
8. **언어**: 코드 주석·문서는 한국어로 쓴다(README·CONTRIBUTING은 한/영). 커밋 메시지는 `type(scope): 요약`(`feat`·`fix`·`refactor`·`docs`·`test`·`build`·`ci`·`chore`) 뒤 한국어 본문.
9. **원본**: 원본 저장소에서 수정을 옮겨 오면 `ORIGIN.md` 표에 기록한다. 주석의 코어 버전(`코어 v0.18.0` 등)·`#123`·`biznotes-core#123` 같은 번호, 「표준 계획 D8」·`HCI-`·`docs/plans/…` 참조는 대개 원본 저장소의 것이라 이 저장소에서는 찾을 수 없다.
