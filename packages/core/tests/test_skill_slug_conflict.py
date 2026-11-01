"""공개 slug 충돌을 409로 바꾸는 판정(#412) — DB 없이 고정하는 부분.

차례로 공개·경합·생성 때 피하기는 부분 인덱스가 필요해 `tests/test_skill_slug_conflict_pg.py`가
PostgreSQL에서 검증한다. 여기서는 바꿀 위반의 범위(제약명·SQLSTATE)와 사용자 문구를 고정한다.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from biznotes_core.models import Skill
from biznotes_core.services.skills import exposure


class _PgError(Exception):
    """asyncpg 어댑터 오류 모양 — sqlstate와 원본(__cause__)의 constraint_name."""

    def __init__(self, sqlstate: str, constraint_name: str | None):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate
        if constraint_name is not None:
            cause = Exception(constraint_name)
            cause.constraint_name = constraint_name  # type: ignore[attr-defined]
            self.__cause__ = cause


def _integrity(sqlstate: str, constraint_name: str | None) -> IntegrityError:
    return IntegrityError("UPDATE skills", {}, _PgError(sqlstate, constraint_name))


def test_converted_constraints_are_the_global_exposed_slug_indexes() -> None:
    """바꾸는 제약명이 모델의 visibility별 전역 slug 인덱스와 같다 — 이름이 바뀌면 여기서 깨진다."""
    exposed = {
        index.name
        for index in Skill.__table__.indexes
        if index.unique
        and [c.name for c in index.columns] == ["slug"]
        and "visibility" in str(index.dialect_options["postgresql"]["where"])
    }
    assert exposed == {"uq_skills_org_slug", "uq_skills_system_slug"}
    assert exposure._EXPOSED_SLUG_INDEXES == exposed


@pytest.mark.parametrize("constraint_name", ["uq_skills_org_slug", "uq_skills_system_slug"])
def test_exposed_slug_violation_becomes_slug_conflict(constraint_name: str) -> None:
    converted = exposure._slug_conflict_from(_integrity("23505", constraint_name))

    assert isinstance(converted, exposure.ExposureError)
    assert (converted.reason, converted.status_code) == ("slug_conflict", 409)
    message = str(converted)
    assert message == "같은 slug의 공개 스킬이 이미 있습니다 — 복제해 다른 이름으로 공개하세요"
    assert "어드민" not in message


@pytest.mark.parametrize(
    ("sqlstate", "constraint_name"),
    [
        ("23505", "uq_skills_owner_slug"),  # 소유자 범위 — 공개 충돌이 아니다
        ("23503", "uq_skills_org_slug"),  # 이름만 같고 SQLSTATE가 다름
        ("23505", None),  # 원본 제약명을 읽을 수 없음 — 추측하지 않는다
    ],
)
def test_other_integrity_errors_are_not_converted(
    sqlstate: str, constraint_name: str | None
) -> None:
    assert exposure._slug_conflict_from(_integrity(sqlstate, constraint_name)) is None
