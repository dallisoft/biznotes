"""공유 POST의 하향 금지 모드 `mode: "upgrade_only"` (#300) — 스키마와 최고 권한 계산.

계획 `docs/plans/share-upgrade-only/plan.md` 수락 기준 4·5와 1·2의 계산 부분이다.
`upsert_notebook_share`는 실경로이고 DB 세션만 대역이다(기존 행 반환·추가 기록). 실제 행·잠금·경합은
`tests/test_share_upgrade_only_pg.py`가 본다.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from biznotes_core.api.v1 import notebooks
from biznotes_core.models import NotebookShare
from biznotes_core.schemas.notebook import NotebookShareRequest
from biznotes_core.services.notebook_shares import upsert_notebook_share

NB = uuid.uuid4()
TARGET = uuid.uuid4()


class _Db:
    """같은 그룹의 기존 행 하나(또는 없음)를 돌려준다 — DB 유니크가 둘 이상을 막는다(#278 3단계)."""

    def __init__(self, permission: str | None = None) -> None:
        self.row = (
            NotebookShare(id=uuid.uuid4(), notebook_id=NB, group_id=TARGET, permission=permission)
            if permission is not None
            else None
        )
        self.added: list[Any] = []

    async def execute(self, stmt: Any) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: self.row)

    def add(self, row: Any) -> None:
        self.added.append(row)


async def _upsert(db: _Db, permission: str, *, upgrade_only: bool) -> NotebookShare:
    return await upsert_notebook_share(
        db,  # type: ignore[arg-type]
        NB,
        group_id=TARGET,
        permission=permission,  # type: ignore[arg-type]
        upgrade_only=upgrade_only,
    )


# ── 수락 기준 1·2: 최고 권한 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("existing", "requested", "expected"),
    [
        (None, "read", "read"),  # 새 대상 → 요청 권한
        (None, "write", "write"),
        ("read", "write", "write"),
        ("write", "read", "write"),  # 낮추지 않는다
        ("read", "read", "read"),
    ],
)
async def test_upgrade_only_keeps_the_highest_permission(
    existing: str | None, requested: str, expected: str
) -> None:
    db = _Db(existing)

    row = await _upsert(db, requested, upgrade_only=True)

    assert row.permission == expected
    if existing is not None:
        assert row is db.row and db.added == []  # 처음 공유한 행을 갱신한다
    else:
        assert db.added == [row]


@pytest.mark.parametrize(("existing", "requested"), [("write", "read"), ("read", "write")])
async def test_default_mode_still_sets_the_requested_permission(
    existing: str, requested: str
) -> None:
    row = await _upsert(_Db(existing), requested, upgrade_only=False)
    assert row.permission == requested  # #222 동작 그대로(하향 포함)


# ── 수락 기준 4: 스키마·OpenAPI ───────────────────────────────────────────


def test_mode_defaults_to_set_and_rejects_unknown_values() -> None:
    assert NotebookShareRequest(group_id=uuid.uuid4()).mode == "set"
    assert NotebookShareRequest(group_id=uuid.uuid4(), mode="upgrade_only").mode == "upgrade_only"
    with pytest.raises(ValidationError):
        NotebookShareRequest(group_id=uuid.uuid4(), mode="downgrade")


def test_openapi_shows_mode_as_an_optional_enum() -> None:
    app = FastAPI()
    app.include_router(notebooks.router, prefix="/api/v1")
    schema = app.openapi()["components"]["schemas"]["NotebookShareRequest"]

    assert schema["properties"]["mode"]["enum"] == ["set", "upgrade_only"]
    assert schema["properties"]["mode"]["default"] == "set"
    assert "mode" not in schema.get("required", [])
