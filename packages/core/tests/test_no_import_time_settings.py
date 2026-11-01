"""코어 모듈은 import 시점에 설정을 스냅샷하지 않는다.

왜 규칙인가:
    프로젝트는 `app/__init__.py`에서 `CoreSettings` 서브클래스를 등록한다.
    코어 모듈이 import 시점에 `get_settings()`를 호출하면 그 등록보다 먼저
    실행될 수 있고, 그러면 **코어 기본값이 굳는다.**

    이 실패는 조용하다. 실제로 두 번 겪었다.
    - `services/events.py`가 import 시점에 Redis 풀을 만들어, 발행은 프로젝트
      DB로 구독은 코어 기본 DB(0)로 갔다. 오류가 아니라 **무한 대기**였다.
    - `core/db.py`가 엔진을 import 시점에 만들어 DB URL이 코어 기본값으로 굳었다.

    사용처에서 `get_settings()`를 부르면 된다 — `lru_cache`라 비용이 없다.
    모듈 레벨 객체가 꼭 필요하면 `@lru_cache` 팩토리로 지연시킨다.
"""

import ast
import pathlib

import pytest

CORE = pathlib.Path(__file__).resolve().parent.parent / "biznotes_core"

#: 워커 진입점이 `app.workers.celery_app` shim을 거치므로 등록 순서가 보장된다.
#: Celery 앱은 모듈 레벨 객체여야 해서 지연시킬 수 없다.
ALLOWED = {"workers/celery_app.py"}


def _py_files() -> list[pathlib.Path]:
    return sorted(
        p
        for p in CORE.rglob("*.py")
        if "__pycache__" not in p.parts
        and str(p.relative_to(CORE)) not in ALLOWED
    )


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: str(p.relative_to(CORE)))
def test_no_settings_snapshot_at_import(path: pathlib.Path) -> None:
    tree = ast.parse(path.read_text())
    offenders = [
        f"line {node.lineno}: {ast.unparse(node)[:80]}"
        for node in tree.body  # 모듈 최상위만 — 함수 안 호출은 정상이다
        if isinstance(node, ast.Assign | ast.AnnAssign)
        and "get_settings()" in ast.unparse(node)
    ]
    assert not offenders, (
        f"{path.relative_to(CORE)} 가 import 시점에 설정을 읽는다 — "
        "프로젝트 등록보다 먼저 실행되면 코어 기본값이 조용히 굳는다:\n  "
        + "\n  ".join(offenders)
    )


def test_allowlist_entries_exist() -> None:
    """허용 목록이 낡아 규칙이 헐거워지는 것을 막는다."""
    for rel in ALLOWED:
        assert (CORE / rel).exists(), f"허용 목록의 {rel} 가 사라졌다 — 목록을 정리하세요"
