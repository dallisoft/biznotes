# syntax=docker/dockerfile:1.7
#
# 빌드 컨텍스트는 저장소 루트다(compose가 `context: ..`, `dockerfile: apps/api/Dockerfile`로 빌드한다).
# 코어(packages/core)는 같은 빌드 안의 `core-wheel` 스테이지에서 wheel로 만들어 얹는다.
#
# 런타임 스테이지가 갖추는 것:
#   /opt/biznotes-core/wheels  코어 wheel(core-wheel 스테이지 산출물)
#   /app/.venv               빈 venv (appuser 소유). 제품 lock으로 채운다
#   VIRTUAL_ENV·PATH         위 venv를 가리킨다
#   /usr/local/bin/uv        고정 uv 0.12.17 (빌드 도구 — 마지막에 제거한다)
#   appuser (uid 1000)       런타임 사용자
#   /data (appuser 소유)     로컬 스토리지 루트(BIZNOTES_DATA_DIR) — compose가 data 볼륨을 붙인다
#   PYTHONUNBUFFERED·UV_COMPILE_BYTECODE=1·UV_LINK_MODE=copy·UV_PYTHON_DOWNLOADS=never
#
# ---- 코어 wheel (packages/core) ----
# uv는 태그를 고정한다 — 이 버전이 아래 설치 동작을 좌우한다.
FROM ghcr.io/astral-sh/uv:0.13.6 AS uv

FROM python:3.15-slim AS core-wheel
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /src
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
# 빌드 백엔드를 고정한다 — `uv build`는 uv.lock을 읽지 않으므로, 격리 빌드에 맡기면
# hatchling이 매번 고정 없이 해석된다.
ARG HATCHLING_VERSION=1.32.4
RUN uv pip install --system "hatchling==${HATCHLING_VERSION}"
COPY packages/core/pyproject.toml ./
COPY packages/core/biznotes_core ./biznotes_core
RUN uv build --wheel --no-build-isolation --out-dir /wheels

# ---- 런타임 ----
FROM python:3.15-slim

COPY --from=uv /uv /usr/local/bin/uv

# /data는 로컬 스토리지 백엔드의 루트다(BIZNOTES_DATA_DIR). 이름 있는 볼륨은 처음 붙을 때 이
# 디렉터리의 소유자를 물려받으므로 appuser 소유로 만들어 둔다.
RUN useradd -m -u 1000 appuser \
    && mkdir -p /home/appuser/.cache /data \
    && chown -R appuser:appuser /home/appuser/.cache /data

WORKDIR /app

COPY --from=core-wheel /wheels /opt/biznotes-core/wheels

ENV VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    BIZNOTES_DATA_DIR=/data

# 빈 venv를 appuser 소유로 만들어 둔다 — 아래 설치는 모두 appuser로 한다.
RUN uv venv /app/.venv && chown -R appuser:appuser /app/.venv

WORKDIR /app
# 위에서 /app/.venv를 appuser 소유로 만들어 두었다.
# root로 설치한 뒤 `chown -R`하면 overlay copy-up으로 venv 전체가 추가 레이어에
# 복제된다(V3-06). 그래서 apt 이후의 모든 설치를 appuser로 수행하고 chown을 하지 않는다.
USER appuser
COPY --chown=appuser:appuser apps/api/requirements.docker.txt ./
# --no-cache: 단일 스테이지라 uv 캐시가 레이어에 남으면 이미지가 커진다(AC-21).
# 설치 순서: 제품 lock → 코어 wheel(--no-deps). 빈 venv라 sync가 아닌 install.
#
# `set -eu`로 단계를 나눈다(계획 §10.5 스니펫의 `&& … || {…}` 체인에서 벗어난 점):
# 그 체인은 어느 단계가 깨지든 마지막 `||` 로 떨어져 **전부 "코어 버전 불일치"로
# 보고**한다(설치 실패를 버전 불일치로 오진). 실측으로 확인해 분리했다.
#
# `uv pip check`의 종료 코드는 버리지 않는다(리뷰 P3-02): 0=충돌 없음, 1=충돌 보고,
# 2 이상=uv 자체 실행 실패(venv 손상·권한 등). 실행 실패를 `|| true`로 삼키면
# residual 이 비어 빌드가 **조용히 통과**하므로, 2 이상은 즉시 빌드를 깨뜨린다.
RUN set -eu; \
    uv pip install --no-cache --requirement requirements.docker.txt; \
    uv pip install --no-cache --no-deps /opt/biznotes-core/wheels/biznotes_core-*.whl; \
    check_rc=0; check_out=$(uv pip check 2>&1) || check_rc=$?; \
    printf '%s\n' "$check_out"; \
    [ "$check_rc" -le 1 ] || { echo "uv pip check 실행 실패(rc=$check_rc)" >&2; exit 1; }; \
    residual=$(printf '%s\n' "$check_out" \
      | grep -E '^(The package |.* requires |.* is not installed)' || true); \
    [ -z "$residual" ] || { echo "의존성 충돌: $residual" >&2; exit 1; }; \
    python -c "import importlib.metadata as m; print('biznotes-core', m.version('biznotes-core'))"

COPY --chown=appuser:appuser apps/api/pyproject.toml apps/api/alembic.ini ./
COPY --chown=appuser:appuser apps/api/app ./app
COPY --chown=appuser:appuser apps/api/alembic ./alembic
COPY --chown=appuser:appuser apps/api/scripts ./scripts
# 프로젝트 자신만 얹는다. --no-sources는 [tool.uv.sources]의 워크스페이스 소스를 무시한다.
RUN uv pip install --no-cache --no-deps --no-sources .

# uv는 빌드 도구 — 런타임 경로에서 제거한다(공격면·오용 방지). 바이트는 앞선 레이어에
# 남으므로 크기 절감으로 계산하지 않는다(D-14, HCI-18).
USER root
RUN rm -f /usr/local/bin/uv
USER appuser
EXPOSE 8000

# 진입점이 마이그레이션(BIZNOTES_AUTO_MIGRATE, 기본 켬)을 끝낸 뒤 uvicorn으로 바뀐다(exec → PID 1)
CMD ["sh", "scripts/start.sh", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
