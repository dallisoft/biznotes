"""노트북당 소스 상한 설정(#445) — env 기본값·관리자 재정의·실효 값·진입점 판정·공개 조회.

대역 세션은 상한 판정에 드는 만큼만 흉내 낸다: advisory lock·개수 조회(`execute`), 관리자
재정의 행 조회(`get`). 라우터·도구 진입점과 실제 잠금·관리자 API는
`tests/test_notebook_source_limit_pg.py`가 본다.
"""

import uuid
import warnings
from typing import Any

import pytest
from conftest import make_settings
from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from biznotes_core.api.v1 import sources
from biznotes_core.api.v1.admin import limits_router
from biznotes_core.config import CoreSettings
from biznotes_core.models import Source, SystemSetting
from biznotes_core.schemas.limits import (
    NOTEBOOK_SOURCE_LIMIT_OVERRIDE_MAX,
    NotebookSourceLimitUpdate,
)
from biznotes_core.services.ingestion import enqueue, source_create, source_limits


class _EnvOnly(CoreSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")


class _Result:
    def __init__(self, count: int) -> None:
        self.count = count

    def scalar_one(self) -> int:
        return self.count

    def scalars(self) -> Any:
        return iter(())  # 같은 이름·크기의 기존 파일 없음


class CapacityDB:
    """상한 판정이 쓰는 만큼만 — 잠금·개수는 `execute`, 관리자 재정의는 `get`이다."""

    def __init__(self, count: int, override: dict[str, Any] | None = None) -> None:
        self.count = count
        self.override = override
        self.statements: list[str] = []
        self.added: list[Any] = []

    async def get(self, model: Any, key: Any, **kw: Any) -> Any:
        if model is SystemSetting and key == source_limits.SETTING_KEY:
            if self.override is not None:
                return SystemSetting(key=key, value=self.override)
        return None

    async def execute(self, stmt: Any, params: Any = None) -> _Result:
        self.statements.append(str(stmt))
        return _Result(self.count)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            obj.id = obj.id or uuid.uuid4()


class _Uow:
    def __init__(self) -> None:
        self.after: list[Any] = []

    def after_commit(self, fn: Any) -> None:
        self.after.append(fn)


@pytest.fixture
def env_limit(monkeypatch: pytest.MonkeyPatch) -> Any:
    """`enqueue`가 읽는 설정의 `max_sources_per_notebook`을 바꾼다."""

    def set_limit(value: int | None) -> None:
        settings = (
            make_settings() if value is None else make_settings(max_sources_per_notebook=value)
        )
        monkeypatch.setattr(enqueue, "get_settings", lambda: settings)

    return set_limit


# ── 설정 ────────────────────────────────────────────────────────────────────


def test_default_is_unchanged_1000() -> None:
    assert CoreSettings.model_fields["max_sources_per_notebook"].default == 1000


def test_env_value_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_SOURCES_PER_NOTEBOOK", "500")
    assert _EnvOnly().max_sources_per_notebook == 500


@pytest.mark.parametrize("value", [0, -1])
def test_out_of_range_env_refuses_to_start(monkeypatch: pytest.MonkeyPatch, value: int) -> None:
    monkeypatch.setenv("MAX_SOURCES_PER_NOTEBOOK", str(value))
    with pytest.raises(ValidationError, match="max_sources_per_notebook"):
        _EnvOnly()


@pytest.mark.parametrize("value", [0, -1, NOTEBOOK_SOURCE_LIMIT_OVERRIDE_MAX + 1])
def test_admin_update_schema_rejects_out_of_range(value: int) -> None:
    with pytest.raises(ValidationError):
        NotebookSourceLimitUpdate(max_sources_per_notebook=value)


def test_admin_update_schema_requires_field_and_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        NotebookSourceLimitUpdate.model_validate({})
    with pytest.raises(ValidationError):
        NotebookSourceLimitUpdate.model_validate({"max_sources_per_notebook": 5, "x": 1})
    assert NotebookSourceLimitUpdate(max_sources_per_notebook=None).max_sources_per_notebook is None


# ── 실효 값: 관리자 재정의 > env ─────────────────────────────────────────────


async def test_effective_value_defaults_to_env(env_limit: Any) -> None:
    env_limit(None)
    assert await enqueue.notebook_source_limit(CapacityDB(0)) == 1000
    env_limit(500)
    assert await enqueue.notebook_source_limit(CapacityDB(0)) == 500


async def test_admin_override_wins_over_env(env_limit: Any) -> None:
    env_limit(500)
    db = CapacityDB(0, {"max_sources_per_notebook": 3})
    assert await enqueue.notebook_source_limit(db) == 3
    assert await source_limits.get_max_sources_override(db) == 3


@pytest.mark.parametrize("stored", [{}, {"max_sources_per_notebook": None}])
async def test_absent_override_falls_back_to_env(env_limit: Any, stored: dict[str, Any]) -> None:
    env_limit(500)
    db = CapacityDB(0, stored)
    assert await source_limits.get_max_sources_override(db) is None
    assert await enqueue.notebook_source_limit(db) == 500


@pytest.mark.parametrize("bad", [0, -3, "7", True, 1.5, NOTEBOOK_SOURCE_LIMIT_OVERRIDE_MAX + 1])
async def test_out_of_range_stored_override_falls_back_to_env(env_limit: Any, bad: Any) -> None:
    env_limit(500)
    assert (
        await enqueue.notebook_source_limit(CapacityDB(0, {"max_sources_per_notebook": bad})) == 500
    )


def test_legacy_constant_is_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    """폐기 별칭(#445)은 v0.31.0에서 지웠다(#453) — 그 이름을 바꾸던 호출자는 속성 오류를 만난다.

    상한은 설정 `max_sources_per_notebook`과 관리자 재정의로만 바꾼다.
    """
    assert not hasattr(enqueue, "MAX_SOURCES_PER_NOTEBOOK")
    with pytest.raises(AttributeError):
        monkeypatch.setattr(enqueue, "MAX_SOURCES_PER_NOTEBOOK", 2)


async def test_limit_does_not_warn(env_limit: Any) -> None:
    env_limit(500)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert await enqueue.notebook_source_limit(CapacityDB(0)) == 500


# ── 진입점 공통 관문: env 500이면 501번째를 거부 ─────────────────────────────


async def test_ensure_source_capacity_rejects_501st(env_limit: Any) -> None:
    """URL·capture·`register_source`·제품 노트·커넥터 가져오기가 이 관문을 쓴다."""
    env_limit(500)
    notebook_id = uuid.uuid4()
    await enqueue.ensure_source_capacity(CapacityDB(499), notebook_id)

    db = CapacityDB(500)
    with pytest.raises(HTTPException) as exc:
        await enqueue.ensure_source_capacity(db, notebook_id)
    assert exc.value.status_code == 422
    assert exc.value.detail == "노트북당 소스는 최대 500개입니다"
    assert "pg_advisory_xact_lock" in db.statements[0]  # 잠근 뒤에 센다


async def test_ensure_source_capacity_counts_the_whole_batch(env_limit: Any) -> None:
    env_limit(500)
    await enqueue.ensure_source_capacity(CapacityDB(498), uuid.uuid4(), additional=2)
    with pytest.raises(HTTPException):
        await enqueue.ensure_source_capacity(CapacityDB(499), uuid.uuid4(), additional=2)


async def test_file_capacity_rejects_501st_upload(env_limit: Any) -> None:
    env_limit(500)
    notebook_id = uuid.uuid4()
    name = await enqueue.ensure_file_source_capacity_and_uniqueness(
        CapacityDB(499), notebook_id, "a.pdf", 10
    )
    assert name == "a.pdf"
    with pytest.raises(HTTPException) as exc:
        await enqueue.ensure_file_source_capacity_and_uniqueness(
            CapacityDB(500), notebook_id, "a.pdf", 10
        )
    assert (exc.value.status_code, exc.value.detail) == (422, "노트북당 소스는 최대 500개입니다")


async def test_admit_source_is_full_at_limit(env_limit: Any) -> None:
    """`save_sources`·`notebook_series`의 판정 — 꽉 차면 `full`이고 행을 넣지 않는다."""
    env_limit(500)
    notebook_id = uuid.uuid4()
    db = CapacityDB(500)
    full = await source_create.admit_source(
        db,
        _Uow(),
        Source(notebook_id=notebook_id, type="text", title="새"),
    )
    assert (full.outcome, full.notebook_id) == ("full", notebook_id)
    assert db.added == []

    db = CapacityDB(499)
    uow = _Uow()
    created = await source_create.admit_source(
        db,
        uow,
        Source(notebook_id=notebook_id, type="text", title="새"),
    )
    assert created.outcome == "created"
    assert len(db.added) == 1 and len(uow.after) == 1


async def test_override_applies_to_judgment(env_limit: Any) -> None:
    env_limit(500)
    db = CapacityDB(3, {"max_sources_per_notebook": 3})
    with pytest.raises(HTTPException) as exc:
        await enqueue.ensure_source_capacity(db, uuid.uuid4())
    assert exc.value.detail == "노트북당 소스는 최대 3개입니다"


async def test_notebook_over_limit_keeps_rows_and_rejects_additions(env_limit: Any) -> None:
    """상한을 낮춰도 기존 소스는 그대로다 — 추가만 거부한다."""
    env_limit(2)
    db = CapacityDB(5)
    with pytest.raises(HTTPException):
        await enqueue.ensure_source_capacity(db, uuid.uuid4())
    full = await source_create.admit_source(
        db,
        _Uow(),
        Source(notebook_id=uuid.uuid4(), type="text", title="새"),
    )
    assert full.outcome == "full"
    assert db.added == []
    assert not any("DELETE" in s.upper() or "UPDATE" in s.upper() for s in db.statements)


# ── 공개 조회 = 판정 값 ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("env_value", "override", "expected"),
    [(None, None, 1000), (500, None, 500), (500, {"max_sources_per_notebook": 7}, 7)],
    ids=["default", "env", "override"],
)
async def test_public_limits_expose_effective_value(
    env_limit: Any,
    monkeypatch: pytest.MonkeyPatch,
    env_value: int | None,
    override: dict[str, Any] | None,
    expected: int,
) -> None:
    env_limit(env_value)
    monkeypatch.setattr(sources, "get_settings", lambda: make_settings())
    db = CapacityDB(expected, override)
    public = await sources.get_source_upload_limits(user=object(), db=db)
    assert public.max_sources_per_notebook == expected

    with pytest.raises(HTTPException) as exc:
        await enqueue.ensure_source_capacity(db, uuid.uuid4())
    assert exc.value.detail == f"노트북당 소스는 최대 {expected}개입니다"


# ── OpenAPI 반영 ─────────────────────────────────────────────────────────────


def test_openapi_exposes_public_value_and_admin_routes() -> None:
    app = FastAPI()
    admin = APIRouter(prefix="/admin")
    admin.include_router(limits_router)
    app.include_router(sources.router, prefix="/api/v1")
    app.include_router(admin, prefix="/api/v1")
    spec = app.openapi()

    schemas = spec["components"]["schemas"]
    public = schemas["SourceUploadLimitsOut"]
    assert set(public["required"]) == {"max_upload_bytes", "max_sources_per_notebook"}

    path = spec["paths"]["/api/v1/admin/notebook-source-limit"]
    assert set(path) == {"get", "put"}
    ref = path["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.rsplit("/", 1)[-1] == "NotebookSourceLimitOut"
    assert set(schemas["NotebookSourceLimitOut"]["required"]) == {
        "max_sources_per_notebook",
        "env_max_sources_per_notebook",
        "override",
    }
    body = schemas["NotebookSourceLimitUpdate"]
    assert body["required"] == ["max_sources_per_notebook"]
    assert body["additionalProperties"] is False
