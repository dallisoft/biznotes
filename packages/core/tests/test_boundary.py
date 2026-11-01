"""코어 경계 규칙 검증.

코어가 프로젝트(`app.*`)를 import하기 시작하면 분리의 의미가 사라진다.
이 테스트는 그 역류를 커밋 시점에 잡는다. 코어 안의 계층 import 방향(ARCHITECTURE.md §2)도
여기서 강제한다(#321).
"""

import ast
import pathlib
import sys

import pytest

CORE = pathlib.Path(__file__).resolve().parent.parent / "biznotes_core"


def _py_files() -> list[pathlib.Path]:
    return sorted(p for p in CORE.rglob("*.py") if "__pycache__" not in p.parts)


def _imported_roots(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: str(p.relative_to(CORE)))
def test_core_does_not_import_project(path: pathlib.Path) -> None:
    """코어는 `app` 패키지를 import하지 않는다."""
    assert "app" not in _imported_roots(path), (
        f"{path.relative_to(CORE)} 가 프로젝트 패키지 `app`을 import한다 — "
        "코어는 프로젝트에 의존할 수 없다. 확장이 필요하면 extra_routers나 "
        "CoreSettings 상속을 쓴다."
    )


def test_core_has_files() -> None:
    """파일 수집이 실패해 위 테스트가 공회전하는 상황을 막는다."""
    assert len(_py_files()) > 20


# ── 계층 import 방향 (ARCHITECTURE.md §2, #321) ────────────────────────────────
# 계층 이름 → import해도 되는 다른 계층. 같은 계층 안의 import는 항상 허용한다.
# `celery_app`은 `send_task`로 이름 발행만 하는 서비스·라우터에 허용한다. 태스크 모듈
# (`workers.tasks.*` 등 그 밖의 `workers`)은 서비스·라우터에서 금지다 — 워커가 서비스를 부르므로
# 반대 방향 import는 순환을 만든다. `schemas`→`models`는 열거값·모델 상수 정본을 쓰기 위해 허용한다.
LAYER_ALLOWED: dict[str, frozenset[str]] = {
    "config": frozenset(),
    "core": frozenset({"config"}),
    "models": frozenset({"config", "core"}),
    "schemas": frozenset({"config", "core", "models"}),
    "task_names": frozenset(),
    "services": frozenset({"config", "core", "models", "schemas", "task_names", "celery_app"}),
    "workers": frozenset(
        {"config", "core", "models", "schemas", "services", "task_names", "celery_app"}
    ),
    "api": frozenset(
        {"config", "core", "models", "schemas", "services", "task_names", "celery_app"}
    ),
}
# 조립 루트 — 모든 계층을 조립하므로 출발점 검사에서 뺀다(아직 동작하지 않는 스캐폴딩이다)
COMPOSITION_ROOTS = frozenset({"biznotes_core.app_factory"})


def _module_name(path: pathlib.Path) -> str:
    parts = list(path.relative_to(CORE.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_core_module(name: str) -> bool:
    rel = pathlib.Path(*name.split(".")[1:]) if "." in name else pathlib.Path()
    return (CORE / rel).with_suffix(".py").is_file() or (CORE / rel / "__init__.py").is_file()


def _layer(name: str) -> str | None:
    """모듈 이름의 계층. 코어 밖이거나 루트 패키지면 None."""
    parts = name.split(".")
    if parts[0] != "biznotes_core" or len(parts) == 1:
        return None
    if parts[1] == "workers" and len(parts) > 2 and parts[2] in ("task_names", "celery_app"):
        return parts[2]
    return parts[1]


def _source_layer(name: str) -> str | None:
    """출발 파일의 계층 — `celery_app.py` 자체는 워커 계층이다."""
    layer = _layer(name)
    return "workers" if layer == "celery_app" else layer


def _dynamic_import_target(node: ast.Call) -> str | None:
    """문자열 상수로 쓴 `importlib.import_module("…")`·`__import__("…")`의 대상."""
    func = node.func
    is_dynamic = (isinstance(func, ast.Name) and func.id in ("__import__", "import_module")) or (
        isinstance(func, ast.Attribute) and func.attr == "import_module"
    )
    if not is_dynamic or not node.args:
        return None
    first = node.args[0]
    return first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None


def _targets(module: str, is_package: bool, tree: ast.AST) -> list[tuple[int, str]]:
    """파일의 모든 import 대상(함수 안 지연 import, 문자열 상수 동적 import 포함).

    `from pkg import sub`는 sub로 푼다."""
    package = module if is_package else module.rpartition(".")[0]
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            for alias in node.names:
                sub = f"{mod}.{alias.name}"
                out.append((node.lineno, sub if _is_core_module(sub) else mod))
        elif isinstance(node, ast.Call) and (target := _dynamic_import_target(node)):
            out.append((node.lineno, target))
    return out


def _layer_violations(module: str, source: str, *, is_package: bool = False) -> list[str]:
    src = _source_layer(module)
    if src is None or module in COMPOSITION_ROOTS:
        return []
    allowed = LAYER_ALLOWED.get(src)
    if allowed is None:
        return [f"{module}: 계층이 정해지지 않은 모듈(LAYER_ALLOWED에 없음)"]
    tree = ast.parse(source)
    found: list[str] = []
    for lineno, target in _targets(module, is_package, tree):
        if src == "task_names":
            root = target.split(".")[0]
            if root not in sys.stdlib_module_names and root != "__future__":
                found.append(f"{lineno}: task_names → {target} (표준 라이브러리만 허용)")
            continue
        dst = _layer(target)
        if dst is None or dst == src or dst in allowed:
            continue
        found.append(f"{lineno}: {src} → {target}")
    return found


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: str(p.relative_to(CORE)))
def test_layer_import_direction(path: pathlib.Path) -> None:
    """계층 간 import는 ARCHITECTURE.md §2 표가 허용한 방향만 쓴다."""
    module = _module_name(path)
    violations = _layer_violations(
        module, path.read_text(), is_package=path.name == "__init__.py"
    )
    assert not violations, (
        f"{path.relative_to(CORE)} 의 계층 위반:\n  "
        + "\n  ".join(violations)
        + "\n태스크는 import하지 말고 `task_names` 이름으로 `celery_app.send_task`를 부른다. "
        "표를 바꿔야 하면 docs/ARCHITECTURE.md §2와 LAYER_ALLOWED를 함께 고친다."
    )


def test_every_core_module_has_a_known_layer() -> None:
    """새 최상위 모듈·패키지가 표 밖에 생기면 검사가 조용히 빠진다 — 여기서 잡는다."""
    unknown = {
        _module_name(p)
        for p in _py_files()
        if _module_name(p) != "biznotes_core"
        and _module_name(p) not in COMPOSITION_ROOTS
        and _source_layer(_module_name(p)) not in LAYER_ALLOWED
    }
    assert not unknown, f"계층이 정해지지 않은 모듈: {sorted(unknown)}"


@pytest.mark.parametrize(
    ("module", "source"),
    [
        # 라우터가 태스크 함수를 직접 import (#321 이전의 실제 위반 형태, 함수 안 지연 import)
        (
            "biznotes_core.api.v1.sources",
            "def f():\n"
            "    from biznotes_core.workers.tasks.ingestion import cleanup_source_vectors\n",
        ),
        # 서비스가 태스크 모듈을 import
        ("biznotes_core.services.x", "from biznotes_core.workers.tasks import overview\n"),
        # 어떤 계층도 api를 import하지 않는다
        ("biznotes_core.services.x", "import biznotes_core.api.v1.notebooks\n"),
        ("biznotes_core.workers.tasks.x", "from biznotes_core.api.v1 import notebooks\n"),
        # 스키마가 서비스를, 모델이 스키마를 import
        ("biznotes_core.schemas.x", "from biznotes_core.services import storage\n"),
        ("biznotes_core.models.x", "from biznotes_core.schemas.enums import Modality\n"),
        # core는 config만
        ("biznotes_core.core.x", "from biznotes_core.models import User\n"),
        # task_names는 표준 라이브러리만(코어·서드파티 모두 금지)
        ("biznotes_core.workers.task_names", "import redis\n"),
        ("biznotes_core.workers.task_names", "from biznotes_core.config import get_settings\n"),
        # 상대 import도 푼다
        ("biznotes_core.services.x", "from ..workers.tasks import overview\n"),
        # 문자열 상수 동적 import도 잡는다(#321 리뷰)
        (
            "biznotes_core.services.x",
            "import importlib\n"
            "m = importlib.import_module('biznotes_core.workers.tasks.overview')\n",
        ),
        ("biznotes_core.services.x", "m = __import__('biznotes_core.workers.tasks.overview')\n"),
        (
            "biznotes_core.workers.task_names",
            "import importlib\nr = importlib.import_module('redis')\n",
        ),
    ],
)
def test_layer_checker_rejects_violations(module: str, source: str) -> None:
    """검사기가 공회전하지 않는다 — 표에 없는 방향은 위반으로 잡는다."""
    assert _layer_violations(module, source)


@pytest.mark.parametrize(
    ("module", "source"),
    [
        ("biznotes_core.services.x", "from biznotes_core.workers.celery_app import celery_app\n"),
        ("biznotes_core.services.x", "from biznotes_core.workers import task_names\n"),
        ("biznotes_core.api.v1.x", "from biznotes_core.workers.task_names import INGEST_SOURCE\n"),
        ("biznotes_core.schemas.x", "from biznotes_core.models import LLMModel\n"),
        (
            "biznotes_core.workers.tasks.x",
            "from biznotes_core.services.storage import put_object\n",
        ),
        ("biznotes_core.workers.task_names", "from typing import Final\n"),
        (
            "biznotes_core.app_factory",
            "from biznotes_core.api.v1.router import build_core_router\n",
        ),
    ],
)
def test_layer_checker_allows_table_directions(module: str, source: str) -> None:
    assert not _layer_violations(module, source)
