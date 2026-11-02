# syntax=docker/dockerfile:1.7
#
# Linux 시험 이미지 biznotes-core-test:py312-node — 스킬 스크립트 샌드박스 격리 시험
# (tests/test_skill_sandbox_isolation.py, 격리 체인 setrlimit → no-new-privileges → Landlock →
# seccomp → close_range → execve)과 tests/ 전체를 Linux에서 돌린다. 발행하지 않는 로컬 전용 이미지다.
#
# 빌드 컨텍스트는 저장소 루트다(다른 이미지와 같다 — 워크스페이스 uv.lock이 루트에 있다).
#   docker build -f packages/core/docker/test.Dockerfile -t biznotes-core-test:py312-node .
#   docker run --rm --security-opt no-new-privileges:true -v "$PWD/packages/core:/src:ro" \
#       biznotes-core-test:py312-node /run.sh -m pytest tests/test_skill_sandbox_isolation.py -q -p no:cacheprovider
#
# 절차·커널 요건은 README "Linux 시험 이미지" 절이 정본이다.
#
# 이 이미지가 제공하는 것
#   /venv                  uv.lock 그대로의 본 의존성 + dev 그룹(pytest·pypdf·python-docx·openpyxl 등).
#                          biznotes_core 자체는 설치하지 않는다 — /run.sh가 마운트한 작업 트리를 `.pth`로 얹는다
#   /usr/local/bin/python3 스크립트용 파이썬(runner의 `.py` 인터프리터 후보). /venv와 같은 3.12다
#   /usr/local/bin/node    스크립트용 node(`.js`·`.mjs` 시험) — 샌드박스 워커 이미지와 같은 node:22
#   /run.sh                docker/test/linux-run.sh — 작업 트리를 복사해 시험 전용 uid(기본 10999)·no-new-privs로 실행한다
#
# root로 남긴다 — /run.sh가 `.pth`를 쓰고 사본을 chown한 뒤 setpriv로 시험 uid로 내려간다.

ARG PYTHON_VERSION=3.12

# 버전은 다른 Dockerfile(base·skill-sandbox)과 같이 올린다.
FROM ghcr.io/astral-sh/uv:0.13.6 AS uv
FROM node:26-slim AS node

FROM python:${PYTHON_VERSION}-slim

COPY --from=uv /uv /usr/local/bin/uv
COPY --from=node /usr/local/bin/node /usr/local/bin/node

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/venv \
    PYTHONUNBUFFERED=1

# 버전은 uv.lock이 고정한다(--frozen: lock을 다시 해석하지 않고, 어긋나면 실패한다).
# 이미지의 파이썬을 쓰도록 --python으로 못 박는다 — lock은 3.15까지 해석한다.
WORKDIR /opt/biznotes-core-test
COPY packages/core/pyproject.toml ./pyproject.toml
COPY uv.lock ./
RUN uv sync --frozen --group dev --no-install-project --python /usr/local/bin/python3 \
    && rm -rf /root/.cache

COPY packages/core/docker/test/linux-run.sh /run.sh

# 빌드 시 smoke — /run.sh가 기대하는 경로와 시험이 쓰는 도구가 있는지 확인한다.
RUN set -eu; \
    chmod 0755 /run.sh; \
    test -d /venv/lib/python3.12/site-packages; \
    command -v setpriv; \
    /usr/local/bin/node --version; \
    /venv/bin/python -c "import pytest, pypdf, docx, openpyxl, sqlalchemy, pydantic; print('deps ok')"; \
    ! /venv/bin/python -c "import biznotes_core" 2>/dev/null

WORKDIR /src
