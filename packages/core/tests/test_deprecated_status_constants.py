"""코어는 Starlette가 deprecated로 지정한 HTTP 상태 상수를 쓰지 않는다.

왜 규칙인가:
    `status.HTTP_422_UNPROCESSABLE_ENTITY` 같은 옛 이름은 접근할 때마다
    `DeprecationWarning`을 내 소비 제품의 테스트 로그를 오염시키고(#124),
    Starlette에서 제거되면 그 오류 경로가 `AttributeError`로 바뀐다.
    상수는 요청 처리 중에 평가되므로 import만으로는 드러나지 않는다.

    금지 목록은 설치된 Starlette의 `status.__deprecated__`를 그대로 쓴다.
    새 이름은 starlette 0.48부터 있으므로 의존성 하한도 함께 지킨다(#131).
"""

import ast
import pathlib
import tomllib

import pytest
from packaging.requirements import Requirement
from starlette import status

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = ROOT / "biznotes_core"
DEPRECATED = frozenset(getattr(status, "__deprecated__", {}))


def _py_files() -> list[pathlib.Path]:
    return sorted(p for p in CORE.rglob("*.py") if "__pycache__" not in p.parts)


def test_deprecated_registry_is_known() -> None:
    """레지스트리가 비면 아래 검사가 아무것도 막지 못한다."""
    assert "HTTP_422_UNPROCESSABLE_ENTITY" in DEPRECATED


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: str(p.relative_to(CORE)))
def test_no_deprecated_status_constants(path: pathlib.Path) -> None:
    used = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Attribute):
            names = [node.attr]
        elif isinstance(node, ast.Name):
            names = [node.id]
        elif isinstance(node, ast.ImportFrom):
            names = [a.name for a in node.names]
        else:
            continue
        used += [f"{node.lineno}: {n}" for n in names if n in DEPRECATED]
    assert not used, f"deprecated 상태 상수: {used}"


def test_declared_starlette_floor_has_new_constants() -> None:
    """선언 범위로 새 상수가 없는 starlette(0.47.x 이하)가 설치되면 안 된다."""
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    reqs = [Requirement(d) for d in deps if Requirement(d).name == "starlette"]
    assert reqs, "starlette 하한을 직접 선언해야 한다 — fastapi 하한만으로는 막지 못한다"
    assert not any(r.specifier.contains("0.47.3") for r in reqs)
    assert all(r.specifier.contains("0.48.0") for r in reqs)
