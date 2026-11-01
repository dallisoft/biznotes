# syntax=docker/dockerfile:1.7
#
# 스킬 스크립트 샌드박스 워커 이미지 (docs/plans/agent-skills-standard/plan.md D7, S-HCI-1).
#
#   docker build -f packages/core/docker/skill-sandbox.Dockerfile -t biznotes/skill-sandbox:local .
#
# 빌드 컨텍스트는 저장소 루트다(compose `worker-skill-sandbox`가 이 파일로 빌드한다).
# 워커·스크립트 의존성 목록은 루트 uv.lock에서 호스트가 export한 파일이다 —
# requirements.sandbox.txt·requirements.sandbox-scripts.txt(`make lock-docker`, 드리프트는 `make docker-lock-check`).
#
# 이 이미지가 제공하는 것
#   /opt/sandbox          워커 가상환경 — 코어 wheel(--no-deps) + `sandbox` 의존성 그룹만
#   /usr/local/bin/python3 스크립트용 파이썬(워커 venv와 분리). 표준 라이브러리에 더해
#                         `sandbox-scripts` 의존성 그룹(reportlab·pypdf·python-docx·python-pptx·
#                         openpyxl)을 uv.lock 해시로 얹는다(#127)
#   /usr/local/bin/node   스크립트용 node(.js·.mjs) — 서드파티 패키지 없음
#   한글 폰트             /usr/share/fonts/truetype/nanum/ (fonts-nanum, TrueType — ReportLab용)
#   sandbox (uid 10001)   실행 사용자 — root로 돌면 워커 가드가 기동을 거부한다
#
# 의도적으로 넣지 않는 것
#   - 제품 코드·설정. 워커는 `biznotes_core.workers.skill_sandbox`만 불러온다.
#   - DB·스토리지·LLM 자격증명. 환경 변수는 브로커 URL과 저장소 출처 두 개뿐이다.
#   - 위 목록 밖의 스크립트용 서드파티 패키지. L2는 런타임 설치를 하지 않는다 — 필요하면 이
#     이미지를 FROM으로 받아 시스템 파이썬에 얹는다(워커 venv가 아니라).
#     기본 제공 목록의 정본은 docs/SKILL_AUTHORING.md다.
#
# 실행 요건(compose): read-only 루트, no-new-privileges, 쓰기 가능한 /tmp(tmpfs),
# 브로커·스토리지 출처만 닿는 네트워크, 동시성 1. seccomp은 런타임 기본 프로필을 그대로 둔다 —
# launcher가 그 위에 소켓 필터를 더한다. Landlock ABI 6 이상 커널이 필요하다.

ARG PYTHON_VERSION=3.12

FROM ghcr.io/astral-sh/uv:0.12.17 AS uv
FROM node:22-slim AS node

FROM python:${PYTHON_VERSION}-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /src
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
ARG HATCHLING_VERSION=1.32.4
RUN uv pip install --system "hatchling==${HATCHLING_VERSION}"
COPY packages/core/pyproject.toml ./
COPY packages/core/biznotes_core ./biznotes_core
COPY packages/core/docker/requirements.sandbox.txt /wheels/sandbox.txt
COPY packages/core/docker/requirements.sandbox-scripts.txt /wheels/scripts.txt
RUN uv build --wheel --no-build-isolation --out-dir /wheels

FROM python:${PYTHON_VERSION}-slim
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=builder /wheels /tmp/wheels
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-nanum \
    && rm -rf /var/lib/apt/lists/*
RUN uv pip install --python /usr/local/bin/python3 --system --require-hashes \
        --requirement /tmp/wheels/scripts.txt \
    && uv venv /opt/sandbox \
    && uv pip install --python /opt/sandbox/bin/python --requirement /tmp/wheels/sandbox.txt \
    && uv pip install --python /opt/sandbox/bin/python --no-deps /tmp/wheels/biznotes_core-*.whl \
    && rm -rf /tmp/wheels /root/.cache \
    && rm -f /usr/local/bin/uv \
    && useradd --uid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin sandbox

# 빌드 시 smoke — 빌드된 이미지는 항상 이 검사를 통과한 것이다.
# - 워커 앱을 새 인터프리터에서 불러와 코어 설정·서비스를 끌고 오지 않는지(D15)
# - launcher가 표준 라이브러리만으로 불리는지, 스크립트용 python3·bash·node가 있는지
# - 스크립트 파이썬이 나눔 폰트로 한글 PDF를, 그리고 한글 docx·pptx·xlsx를 만들고 다시 읽는지,
#   워커 venv에는 그 라이브러리가 없는지
RUN /opt/sandbox/bin/python - <<'PY'
import importlib.metadata as m
import json
import subprocess
import sys

installed = m.version("biznotes-core")  # 코어 wheel이 워커 venv에 설치됐는지
probe = (
    "import json, sys\n"
    "import biznotes_core.workers.skill_sandbox.app as a\n"
    "print(json.dumps({'tasks': sorted(n for n in a.app.tasks if not n.startswith('celery.')),"
    " 'mods': sorted(m for m in sys.modules if m.startswith('biznotes_core'))}))\n"
)
out = json.loads(subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True,
                                text=True).stdout)
if out["tasks"] != ["biznotes_core.workers.skill_sandbox.run_skill_script"]:
    raise SystemExit(f"등록 태스크가 다르다: {out['tasks']}")
leaked = [n for n in out["mods"] if not n.startswith(("biznotes_core.workers.skill_sandbox",
                                                        "biznotes_core.workers.task_names"))
          and n not in ("biznotes_core", "biznotes_core.workers")]
if leaked:
    raise SystemExit(f"샌드박스 앱이 코어의 다른 모듈을 불러온다: {leaked}")
for cmd in (["/usr/local/bin/python3", "-I", "-c", "print(1)"], ["/bin/bash", "-c", "true"],
            ["/usr/local/bin/node", "--version"]):
    subprocess.run(cmd, check=True, capture_output=True)
subprocess.run([sys.executable, "-I", "-c", "import biznotes_core.workers.skill_sandbox.launcher"],
               check=True)
pdf_probe = (
    "import io, os, sys\n"
    "from reportlab.pdfbase import pdfmetrics\n"
    "from reportlab.pdfbase.ttfonts import TTFont\n"
    "from reportlab.pdfgen import canvas\n"
    "from pypdf import PdfReader\n"
    "pdfmetrics.registerFont(TTFont('Nanum', '/usr/share/fonts/truetype/nanum/NanumGothic.ttf'))\n"
    "buf = io.BytesIO(); c = canvas.Canvas(buf); c.setFont('Nanum', 14)\n"
    "c.drawString(72, 720, '한글 PDF 확인'); c.save()\n"
    "text = PdfReader(io.BytesIO(buf.getvalue())).pages[0].extract_text()\n"
    "sys.exit(0 if text.strip() == '한글 PDF 확인' else f'추출 불일치: {text!r}')\n"
)
subprocess.run(["/usr/local/bin/python3", "-I", "-c", pdf_probe], check=True)
office_probe = (
    "import io\n"
    "from docx import Document\n"
    "from openpyxl import Workbook, load_workbook\n"
    "from pptx import Presentation\n"
    "d = Document(); d.add_paragraph('한글 문서'); b = io.BytesIO(); d.save(b)\n"
    "assert Document(io.BytesIO(b.getvalue())).paragraphs[0].text == '한글 문서'\n"
    "p = Presentation(); s = p.slides.add_slide(p.slide_layouts[0]); s.shapes.title.text = '한글 슬라이드'\n"
    "b = io.BytesIO(); p.save(b)\n"
    "assert Presentation(io.BytesIO(b.getvalue())).slides[0].shapes.title.text == '한글 슬라이드'\n"
    "w = Workbook(); w.active['A1'] = '한글 셀'; b = io.BytesIO(); w.save(b)\n"
    "assert load_workbook(io.BytesIO(b.getvalue())).active['A1'].value == '한글 셀'\n"
)
subprocess.run(["/usr/local/bin/python3", "-I", "-c", office_probe], check=True)
venv_leak = subprocess.run(
    [sys.executable, "-I", "-c", "import importlib.util as u; print([n for n in "
     "('reportlab', 'pypdf', 'PIL', 'docx', 'pptx', 'openpyxl', 'lxml') if u.find_spec(n)])"],
    check=True, capture_output=True, text=True).stdout.strip()
if venv_leak != "[]":
    raise SystemExit(f"워커 venv에 스크립트용 라이브러리가 섞였다: {venv_leak}")
print(f"smoke ok: biznotes_core {installed}, sandbox app modules {len(out['mods'])}")
PY

ARG PYTHON_VERSION
LABEL org.opencontainers.image.title="skill-sandbox" \
      org.opencontainers.image.description="스킬 번들 스크립트 샌드박스 전용 워커" \
      org.opencontainers.image.base.name="python:${PYTHON_VERSION}-slim" \
      io.biznotes-core.python.version="${PYTHON_VERSION}"

ENV PATH="/opt/sandbox/bin:$PATH" PYTHONUNBUFFERED=1
USER sandbox
WORKDIR /tmp
# celery가 PID 1이어야 한다(셸 래퍼 금지) — 풀 자식이 subreaper로 스크립트 자손을 회수한다
CMD ["celery", "-A", "biznotes_core.workers.skill_sandbox.app", "worker", \
     "-Q", "skills.sandbox", "--concurrency=1", "--pool=prefork", "-l", "INFO"]
