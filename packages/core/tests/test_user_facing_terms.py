"""사용자에게 닿는 코어 문구는 제품 화면 이름(「어드민」)을 쓰지 않는다 (#301).

제품은 코어 문구를 가공 없이 보여 준다(소스 `error_message`, mock 응답 안내 등). 관리자 화면의
이름은 제품마다 다르므로(biznotes는 「관리」) 코어는 역할 이름 「관리자」를 쓴다.
독스트링·주석은 화면에 나가지 않으므로 대상이 아니다.
"""

import ast
import pathlib

CORE = pathlib.Path(__file__).resolve().parent.parent / "biznotes_core"


def _docstring_ids(tree: ast.AST) -> set[int]:
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, owners) or not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            ids.add(id(first.value))
    return ids


def test_string_literals_do_not_use_admin_screen_term():
    hits = []
    for path in sorted(CORE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstring_ids(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and "어드민" in node.value
            ):
                hits.append(f"{path.relative_to(CORE)}:{node.lineno}")
    assert hits == []

