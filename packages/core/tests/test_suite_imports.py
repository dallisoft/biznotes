"""코어 테스트가 제품 코드에 기대지 않는지 확인한다 (#1 D8).

코어 테스트 전체가 제품 패키지(`app`)·제품 스크립트(`scripts`)를 import하지 않아야 코어만으로
돈다. PG 테스트(`*_pg.py`)는 소비 제품에서 옮긴 코어 동작 테스트라 `tests.*`도
금지한다 — 워크스페이스 venv에서 그 이름은 제품 테스트 하네스(apps/api/tests)일 수 있다.
다른 코어 테스트는 같은 디렉터리의 도우미를 `tests.*`로 쓴다. 함수 안의 import도 잡도록
모든 Import·ImportFrom 노드를 본다.
"""

import ast
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
FORBIDDEN_ROOTS = {"app", "scripts"}
PG_FORBIDDEN_ROOTS = FORBIDDEN_ROOTS | {"tests"}


def _roots(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name.split(".")[0]) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, node.module.split(".")[0]))
    return found


FILES = sorted(p for p in TESTS.rglob("*.py") if "fixtures" not in p.parts)


def test_pg_tests_exist() -> None:
    assert any(p.name.endswith("_pg.py") for p in FILES), "PG 테스트(*_pg.py)가 사라졌다"


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(TESTS)))
def test_no_product_imports(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    forbidden = PG_FORBIDDEN_ROOTS if path.name.endswith("_pg.py") else FORBIDDEN_ROOTS
    bad = [(line, root) for line, root in _roots(tree) if root in forbidden]
    assert not bad, f"{path.name}: 제품 코드 import {bad}"
