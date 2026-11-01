# 기여 안내 / Contributing

[한국어](#한국어) · [English](#english)

## 한국어

기여를 환영합니다. 버그는 이슈로, 보안 문제는 [SECURITY.md](SECURITY.md)대로 비공개로 알려 주세요. 큰 변경은 먼저 이슈에서 방향을 맞추면 좋습니다. 코드 규칙은 [AGENTS.md](AGENTS.md), 구조는 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)에 있습니다.

### 준비

| 도구 | 버전 |
|---|---|
| Docker (Compose v2) | 스택·이미지 빌드 |
| GNU make | 명령 진입점 |
| [uv](https://docs.astral.sh/uv/) | 0.13 이상. Python 3.12는 uv가 받는다(`.python-version`) |
| Node.js | 22 |
| pnpm | 10.14 (`corepack enable`이면 `package.json`의 `packageManager`를 따른다) |

```bash
uv sync --all-packages   # 코어 + api, dev 그룹 포함(.venv)
pnpm install
make up                  # 테스트가 쓰는 PostgreSQL(25433)·Valkey(26379)도 여기서 뜬다
make seed
```

### 테스트와 게이트

- PR 전에 `make ci`를 돌립니다. GitHub Actions도 같은 Makefile 타깃(`ci-backend`·`ci-frontend`·`ci-contracts`)을 부르고, 여기에 이미지 3개 빌드(docker 잡)와 gitleaks를 더합니다. 이미지까지 로컬에서 보려면 `make ci-full`.
- api 테스트는 병렬(`PYTEST_XDIST_N=4`)로 돕니다. 직렬은 `PYTEST_XDIST_N=0 make test`.
- 코어 테스트는 단일 스위트이고, `*_pg.py`는 실제 PostgreSQL·Valkey를 씁니다. 접속값은 `CORE_TEST_PG_*`·`CORE_TEST_VALKEY_URL`로 바꿀 수 있습니다.
- 스킬 샌드박스 격리 시험은 Linux 커널에서만 돕니다(macOS에서는 skip). 방법은 `packages/core/README.md` 「Linux 시험 이미지」.
- Playwright e2e는 수동입니다: 스택을 띄운 뒤 `cd apps/web && pnpm exec playwright test`, 또는 Actions의 `e2e` 워크플로를 수동 실행.
- UI·워커·인제스트를 바꿨다면 `make up` 실스택에서 해당 흐름을 한 번 돌려 보고 PR에 적어 주세요.

### 변경할 때 함께 넣을 것

- **API 응답**: `pnpm gen:types` → `packages/shared-types` 변경을 같은 PR에.
- **DB 모델**: `cd apps/api && uv run alembic revision --autogenerate -m "<요약>"` 뒤 생성된 파일을 손으로 확인하고 `make migrate-check`.
- **의존성**: `uv add …`(또는 `pnpm add …`) 뒤 `make lock-docker`. dependabot의 uv PR도 브랜치에서 `make lock-docker`를 돌려 커밋해야 contracts 잡이 통과합니다.

### 커밋·PR

- 기능 하나 = 커밋 하나, 커밋마다 `make ci` 초록. 메시지는 `type(scope): 요약` 형식(예: `fix(ingestion): …`).
- PR 템플릿의 세 칸(요약·테스트·마이그레이션 동반)을 채워 주세요.
- 기여한 코드는 이 저장소의 라이선스([Apache-2.0](LICENSE))로 배포되는 데 동의한 것으로 봅니다.
- 공개 저장소 `dallisoft/biznotes`는 유지보수자의 작업 저장소 `main`을 자동으로 따라가는 미러입니다(`.github/workflows/mirror.yml`). PR은 공개 저장소에 열어 주세요 — 유지보수자가 작업 저장소에 옮겨 넣으면 미러에 반영되고, 공개 저장소에서 직접 병합하지는 않습니다.

---

## English

Contributions are welcome. Please file bugs as issues and report security problems privately as described in [SECURITY.md](SECURITY.md). For larger changes, agreeing on direction in an issue first helps. Code rules are in [AGENTS.md](AGENTS.md) and the design in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Code comments and most docs are in Korean; issues and pull requests in English are fine.

### Setup

You need Docker (Compose v2), GNU make, [uv](https://docs.astral.sh/uv/) 0.13+ (it fetches Python 3.12 per `.python-version`), Node.js 22 and pnpm 10.14 (`corepack enable` picks the version from `packageManager`).

```bash
uv sync --all-packages   # core + api with dev groups (.venv)
pnpm install
make up                  # also provides PostgreSQL (25433) and Valkey (26379) for tests
make seed
```

### Tests and gates

- Run `make ci` before opening a pull request. GitHub Actions calls the same Makefile targets (`ci-backend`, `ci-frontend`, `ci-contracts`) plus an image build job and gitleaks; `make ci-full` covers the images locally.
- Core tests are a single suite; files named `*_pg.py` use the real PostgreSQL and Valkey from `make up` (override with `CORE_TEST_PG_*` and `CORE_TEST_VALKEY_URL`).
- The skill sandbox isolation tests run only on a Linux kernel (skipped on macOS); see "Linux 시험 이미지" in `packages/core/README.md`.
- Playwright e2e is manual: start the stack, then `cd apps/web && pnpm exec playwright test`, or dispatch the `e2e` workflow.
- If you change UI, workers or ingestion, exercise the flow once on the `make up` stack and say so in the pull request.

### Changes that must travel together

- **API responses**: include the `pnpm gen:types` output (`packages/shared-types`).
- **DB models**: `cd apps/api && uv run alembic revision --autogenerate -m "<summary>"`, review the file, then `make migrate-check`.
- **Dependencies**: after `uv add …` (or `pnpm add …`) run `make lock-docker`. Dependabot uv PRs also need `make lock-docker` committed on their branch for the contracts job to pass.

### Commits and pull requests

- One change per commit, each with `make ci` green. Messages follow `type(scope): summary`.
- Fill in the three sections of the pull request template (summary, testing, migrations/contracts).
- By contributing you agree that your contribution is licensed under the repository's license ([Apache-2.0](LICENSE)).
- The public repository `dallisoft/biznotes` is a mirror that automatically follows `main` of the maintainers' working repository (`.github/workflows/mirror.yml`). Open pull requests on the public repository; maintainers apply them upstream and the mirror picks them up, so they are not merged on the public repository directly.
