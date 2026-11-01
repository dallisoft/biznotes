"""스킬 스키마 계약 — 표준 계획 D13(수락 기준 13의 메타데이터 부분)과 legacy 기준선(15).

두 제품의 Alembic 리비전이 이 정의를 따라간다. 여기서는 PostgreSQL DDL로 컴파일한 결과를
고정한다. 실제 리비전 upgrade와 CHECK 대조는 제품 쪽 검증(임시 PostgreSQL)이 맡는다 —
SQLite `create_all()`은 그 증거가 아니다(AGENTS.md §7.4).
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from biznotes_core.models import Base
from biznotes_core.models import skill as skill_models
from biznotes_core.services.skills import package as pkg
from biznotes_core.services.skills import report as rpt
from biznotes_core.services.skills import service as svc
from biznotes_core.services.skills import standard as std

FIXTURES = Path(__file__).parent / "fixtures" / "skills"


def _ddl(table: str) -> str:
    return str(CreateTable(Base.metadata.tables[table]).compile(dialect=postgresql.dialect()))


def _checks(table: str) -> dict[str, str]:
    return {
        str(c.name): str(c.sqltext)
        for c in Base.metadata.tables[table].constraints
        if isinstance(c, CheckConstraint)
    }


def test_exactly_five_new_check_constraints() -> None:
    checks = {
        **_checks("skills"),
        **_checks("skill_versions"),
        **_checks("skill_review_requests"),
    }
    assert checks == {
        "ck_skills_origin": "origin IN ('authored', 'imported', 'system_seed')",
        "ck_skill_versions_validation_profile": (
            "validation_profile IN ('legacy_v1', 'standard_v1')"
        ),
        "ck_skill_versions_runtime_profile": (
            "runtime_profile IN ('legacy_instruction_v1', 'standard_agent_v1')"
        ),
        "ck_skill_review_requests_kind": "kind IN ('expose_org', 'expose_system', 'scripts')",
        "ck_skill_review_requests_status": (
            "status IN ('pending', 'approved', 'rejected', 'withdrawn', 'cancelled')"
        ),
    }


def test_runtime_check_drops_the_abandoned_v2_profile() -> None:
    """S-HCI-7: sl dev DB의 브랜치 CHECK(`standard_instruction_v2`)는 리비전이 재생성한다."""
    assert "standard_instruction_v2" not in _checks("skill_versions")[
        "ck_skill_versions_runtime_profile"
    ]


@pytest.mark.parametrize(
    ("table", "fragment"),
    [
        ("skills", "origin VARCHAR(16) DEFAULT 'authored' NOT NULL"),
        ("skills", "origin_detail JSONB DEFAULT '{}'::jsonb NOT NULL"),
        ("skills", "published_version_id UUID,"),
        ("skills", "exposure_hold BOOLEAN DEFAULT false NOT NULL"),
        ("skill_versions", "validation_profile VARCHAR(32) DEFAULT 'legacy_v1' NOT NULL"),
        (
            "skill_versions",
            "runtime_profile VARCHAR(32) DEFAULT 'legacy_instruction_v1' NOT NULL",
        ),
        ("skill_versions", "standard_metadata JSONB DEFAULT '{}'::jsonb NOT NULL"),
    ],
)
def test_new_columns_have_server_defaults(table: str, fragment: str) -> None:
    """추가 전용이고 `server_default`가 있다 — 구 앱의 INSERT도 성공해야 한다."""
    assert fragment in _ddl(table)


def test_published_pointer_has_no_foreign_key() -> None:
    """순환 FK를 피한다 — 소속 검사는 `set_published_version`이 한다."""
    column = Base.metadata.tables["skills"].c.published_version_id
    assert not column.foreign_keys


def test_review_request_table_shape() -> None:
    table = Base.metadata.tables["skill_review_requests"]
    fks = {fk.parent.name: (fk.column.table.name, fk.ondelete) for fk in table.foreign_keys}
    assert fks == {
        "version_id": ("skill_versions", "CASCADE"),
        "requested_by": ("users", "SET NULL"),
        "decided_by": ("users", "SET NULL"),
    }
    pending = next(ix for ix in table.indexes if ix.name == "uq_skill_review_requests_pending")
    ddl = str(CreateIndex(pending).compile(dialect=postgresql.dialect()))
    assert ddl == (
        "CREATE UNIQUE INDEX uq_skill_review_requests_pending ON skill_review_requests "
        "(version_id, kind) WHERE status = 'pending'"
    )


def test_profile_constants_agree_across_modules() -> None:
    assert std.VALIDATION_PROFILES == skill_models.SKILL_VALIDATION_PROFILES


# ---------- legacy 기준선 (수락 기준 15, sl-HCI-2: 자동 개명 없음) ----------

LEGACY = json.loads((FIXTURES / "legacy-golden.json").read_text("utf-8"))


@pytest.mark.parametrize(
    "row", LEGACY["slug_derivation"], ids=[r["display_name"] for r in LEGACY["slug_derivation"]]
)
def test_slug_derivation_is_unchanged(row: dict) -> None:
    assert svc.slugify(row["display_name"]) == row["slug"]


@pytest.mark.parametrize("key", ["manifest_package", "no_manifest_package"])
def test_legacy_package_hash_is_unchanged(key: str) -> None:
    entry = LEGACY[key]
    root = FIXTURES / entry["path"]
    files = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    assert pkg.package_hash(files) == entry["package_hash"]
    assert ("slagents.json" in files) is entry["has_manifest"]


def test_legacy_manifest_package_keeps_the_legacy_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """매니페스트가 있는 기존 패키지는 플래그와 무관하게 legacy 실행 프로필이다."""
    from conftest import make_settings

    settings = make_settings(
        skill_manifest_filename="slagents.json", skill_standard_runtime_enabled=True
    )
    for module in (rpt, pkg):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr("biznotes_core.services.skills.validator.get_settings", lambda: settings)
    monkeypatch.setattr("biznotes_core.services.skills.parser.get_settings", lambda: settings)
    root = FIXTURES / LEGACY["manifest_package"]["path"]
    files = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    evaluation = rpt.evaluate_package(files)
    assert evaluation.runtime_profile == "legacy_instruction_v1"


@pytest.mark.parametrize("key", ["manifest_package", "no_manifest_package"])
def test_legacy_execution_graph_is_unchanged(key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """수락 기준 15: 기존 스킬의 실행 그래프가 Phase 1 전후로 같다.

    freeze는 이제 `report.evaluate_package`의 파싱 결과로 노드를 만든다(`loaded_from_evaluation`).
    기존 경로(`load_skill_files`)와 같은 그래프가 나와야 legacy 스킬의 실행이 바뀌지 않는다.
    """
    from conftest import make_settings

    from biznotes_core.services.skills import builder

    settings = make_settings(skill_manifest_filename="slagents.json")
    for module in (rpt, pkg, builder):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr("biznotes_core.services.skills.validator.get_settings", lambda: settings)
    monkeypatch.setattr("biznotes_core.services.skills.parser.get_settings", lambda: settings)

    root = FIXTURES / LEGACY[key]["path"]
    files = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    legacy_loaded = builder.load_skill_files(files)
    phase1_loaded = builder.loaded_from_evaluation(rpt.evaluate_package(files), files)

    assert phase1_loaded == legacy_loaded
    assert builder.build_nodes(phase1_loaded) == builder.build_nodes(legacy_loaded)
