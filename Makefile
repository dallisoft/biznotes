# biznotes 개발 환경 (Docker Compose dev 오버레이)
#
# 사용: make help

COMPOSE := docker compose -f infra/compose.yml -f infra/compose.dev.yml

# make logs s=api 처럼 특정 서비스 지정 (생략 시 전체)
s ?=

.PHONY: help build up down restart rebuild logs ps migrate seed smoke test ci ci-backend ci-frontend ci-contracts clean migrate-check types-check lock-docker docker-lock-check core-test ci-full

# 테스트/마이그레이션 검증이 사용하는 호스트 PG 포트 (compose.dev.yml 매핑과 동일)
POSTGRES_PORT ?= 25433

# api pytest 병렬 워커 수. `PYTEST_XDIST_N=0`이면 직렬로 돈다.
PYTEST_XDIST_N ?= 4
PYTEST_ARGS = $(if $(filter 0,$(PYTEST_XDIST_N)),,-n $(PYTEST_XDIST_N) --dist loadfile)

# `uv run pytest`가 아니라 `uv run python -m pytest`를 쓴다: 콘솔 스크립트(`.venv/bin/pytest`)는
# 생성 시점에 박힌 shebang의 인터프리터로 실행되므로, 그 경로가 현재 venv와 어긋나면
# `pytest-xdist`가 없는 환경에서 돌아 `-n`이 인자 오류가 된다. `python -m`은 지금 실행 중인
# 인터프리터를 그대로 쓴다.
PYTEST := uv run python -m pytest

# 코어(packages/core) 테스트는 공용 워크스페이스 venv에서 돈다(pytest-randomly는 코어 pyproject addopts가 끈다).
CORE_PYTEST := uv run python -m pytest -q

help: ## 타깃 목록
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

build: ## 이미지 빌드 (의존성 추가 시 필수 — dev 볼륨 마운트는 의존성을 반영하지 못함)
	$(COMPOSE) build $(s)

up: ## 스택 기동 (빌드 포함)
	$(COMPOSE) up -d --build
	$(COMPOSE) ps

down: ## 스택 중지 (볼륨 유지)
	$(COMPOSE) down

restart: ## 서비스 재시작 (예: make restart s=api)
	$(COMPOSE) restart $(s)

rebuild: ## 이미지 재빌드 + 컨테이너 재생성 (의존성 변경 후 크래시 루프 방지)
	$(COMPOSE) build $(s)
	$(COMPOSE) up -d --force-recreate $(s)

logs: ## 로그 팔로우 (예: make logs s=worker)
	$(COMPOSE) logs -f --tail=100 $(s)

ps: ## 컨테이너 상태
	$(COMPOSE) ps

migrate: ## Alembic 마이그레이션 수동 적용 — 평소에는 api가 기동하며 적용한다(BIZNOTES_AUTO_MIGRATE)
	$(COMPOSE) exec api alembic upgrade head

seed: ## 시드 데이터 (admin/demo 계정, 멱등)
	$(COMPOSE) exec api python -m scripts.seed

smoke: ## 라이브 스택 스모크 테스트 (readyz→로그인→소스→질의)
	API_URL=http://localhost:28000 ./infra/scripts/smoke.sh

test: ## 백엔드 + 프론트 테스트 (호스트 uv/pnpm, 컨테이너 PG 25433·Valkey 26379 필요. 직렬은 PYTEST_XDIST_N=0)
	cd apps/api && $(PYTEST) $(PYTEST_ARGS)
	cd packages/core && $(CORE_PYTEST)
	pnpm --filter web test

migrate-check: ## clean DB에 alembic upgrade head + check (모델↔마이그레이션 드리프트 게이트)
	cd apps/api && POSTGRES_PORT=$(POSTGRES_PORT) uv run python -m scripts.create_clean_db biznotes_migrate
	cd apps/api && POSTGRES_PORT=$(POSTGRES_PORT) POSTGRES_DB=biznotes_migrate uv run alembic upgrade head
	cd apps/api && POSTGRES_PORT=$(POSTGRES_PORT) POSTGRES_DB=biznotes_migrate uv run alembic check

# Docker 이미지는 uv.lock이 아니라 아래 export 결과로 설치한다.
#   api           apps/api/requirements.docker.txt — 워크스페이스 멤버(코어·api 자신)는 빼고, 코어는 이미지 안에서 wheel로 얹는다
#   skill-sandbox packages/core/docker/requirements.sandbox*.txt — 코어의 sandbox·sandbox-scripts 의존성 그룹
lock-docker: ## Docker용 requirements 재생성 (uv.lock 변경 시 필수 — 이미지가 이걸 쓴다)
	uv export --frozen --package biznotes-api --no-dev --no-emit-workspace -o apps/api/requirements.docker.txt
	uv export --frozen --package biznotes-core --only-group sandbox --no-hashes --no-emit-project --no-emit-workspace -o packages/core/docker/requirements.sandbox.txt
	uv export --frozen --package biznotes-core --only-group sandbox-scripts --no-emit-project --no-emit-workspace -o packages/core/docker/requirements.sandbox-scripts.txt

docker-lock-check: ## Docker requirements 드리프트 게이트 — 재생성 후 diff가 남으면 실패
	$(MAKE) lock-docker
	git diff --exit-code -- apps/api/requirements.docker.txt packages/core/docker/requirements.sandbox.txt packages/core/docker/requirements.sandbox-scripts.txt \
		|| { echo "❌ Docker requirements 드리프트 — 'make lock-docker' 결과를 커밋하세요"; exit 1; }

ci-full: ## make ci + 이미지 빌드(api·web·skill-sandbox) — GitHub Actions 4잡과 같은 범위
	$(MAKE) ci
	$(MAKE) build
	$(COMPOSE) --profile skill-scripts build worker-skill-sandbox

types-check: ## OpenAPI 계약 드리프트 게이트 — 재생성 후 diff가 남으면 실패
	pnpm gen:types
	git diff --exit-code -- packages/shared-types \
		|| { echo "❌ shared-types 드리프트 — 'pnpm gen:types' 결과를 커밋하세요"; exit 1; }

# 코어 스위트는 단일이다 — `*_pg.py`는 실제 PostgreSQL(pgvector)·Valkey를 쓴다(기본은 make up의
# 25433·26379, 바꾸려면 CORE_TEST_PG_*·CORE_TEST_VALKEY_URL — packages/core/tests/conftest.py).
core-test: ## 코어(packages/core) 게이트 — ruff·mypy·pytest(PG 테스트·경계 규칙 포함)·wheel 빌드
	cd packages/core && uv run ruff check . && uv run mypy biznotes_core && $(CORE_PYTEST) && uv build --wheel --out-dir ../../dist/core

# GitHub Actions(.github/workflows/ci.yml)의 backend·frontend·contracts 잡이 아래 타깃을 그대로 부른다.
# docker 잡(이미지 3개 빌드)은 로컬에서 `make ci-full`로 같은 범위를 돈다.
ci-backend: export UV_LOCKED := 1
ci-backend: ## 백엔드 게이트 — api ruff·mypy·pytest --cov + 코어 게이트 + migrate-check (PG 25433·Valkey 26379 필요)
	cd apps/api && uv run ruff check . && uv run mypy app && $(PYTEST) --cov $(PYTEST_ARGS)
	$(MAKE) core-test
	$(MAKE) migrate-check

ci-frontend: ## 프론트 게이트 — web lint·typecheck·vitest 커버리지
	pnpm --filter web lint
	pnpm --filter web typecheck
	pnpm --filter web test:coverage

ci-contracts: export UV_LOCKED := 1
ci-contracts: ## 계약 게이트 — shared-types(OpenAPI)·Docker requirements 드리프트
	$(MAKE) types-check
	$(MAKE) docker-lock-check

ci: export UV_LOCKED := 1
ci: ## CI 게이트 로컬 실행 (ci-backend + ci-frontend + ci-contracts)
	@git diff --quiet -- uv.lock || { echo "❌ uv.lock이 인덱스와 다릅니다 — 게이트 실행 전 lock 오염"; git diff -- uv.lock; exit 1; }
	$(MAKE) ci-backend
	$(MAKE) ci-frontend
	$(MAKE) ci-contracts
	@git diff --quiet -- uv.lock || { echo "❌ 게이트가 uv.lock을 재작성했습니다"; git diff -- uv.lock; exit 1; }

clean: ## 전체 삭제 — 컨테이너 + 볼륨(DB·파일(data) 데이터 소실 주의)
	@printf "모든 데이터 볼륨이 삭제됩니다. 계속하려면 yes 입력: " && read ans && [ "$$ans" = "yes" ]
	$(COMPOSE) down -v
