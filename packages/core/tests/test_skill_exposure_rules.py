"""노출 모델의 순수 판정 — 표준 계획 D8(수락 기준 8·9·10의 규칙 부분).

DB 경로(잠금·요청 전이·백필)는 코어 테스트가 PostgreSQL을 띄우지 않으므로
`docs/plans/agent-skills-standard/smoke/`의 임시 PostgreSQL 스모크로 검증한다.
여기서는 그 경로들이 공유하는 판정 함수와 공개 계약(경고·쿼리 모양)을 고정한다.
"""

import pathlib
import re
import uuid
import warnings
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from biznotes_core.models import Skill
from biznotes_core.services.agents.nodes.composite import agent_visible_to
from biznotes_core.services.agents.tool_registry import (
    published_system_skill_predicates,
    published_system_skill_query,
)
from biznotes_core.services.skills import exposure

OWNER = uuid.uuid4()
OTHER = uuid.uuid4()
CURRENT = uuid.uuid4()
PUBLISHED = uuid.uuid4()


def skill(**overrides: object) -> Skill:
    fields: dict[str, object] = {
        "owner_id": OWNER,
        "visibility": "org",
        "enabled": True,
        "current_version_id": CURRENT,
        "published_version_id": PUBLISHED,
        "origin": "authored",
        "exposure_hold": False,
    }
    fields.update(overrides)
    return Skill(**fields)


# ---------- 노출 version과 실행 version ----------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, PUBLISHED),
        ({"visibility": "system"}, PUBLISHED),
        ({"visibility": "private"}, None),
        ({"enabled": False}, None),
        ({"published_version_id": None}, None),
    ],
    ids=["org", "system", "private", "disabled", "no-pointer"],
)
def test_exposed_version_requires_enabled_exposed_visibility_and_pointer(
    overrides: dict, expected: uuid.UUID | None
) -> None:
    """R3-01: visibility·enabled가 노출 조건에 들어 있다."""
    assert exposure.exposed_version_id(skill(**overrides)) == expected


def test_owner_runs_the_latest_freeze_and_others_run_the_published_version() -> None:
    """수락 기준 9: 소유자가 새로 freeze해도 비소유자는 이전 공개 version을 본다."""
    target = skill()
    assert exposure.resolve_run_version_id(target, OWNER) == CURRENT
    assert exposure.resolve_run_version_id(target, OTHER) == PUBLISHED
    assert exposure.resolve_run_version_id(target, None) == PUBLISHED


def test_disabled_skill_is_not_runnable_even_for_the_owner() -> None:
    target = skill(enabled=False)
    assert exposure.resolve_run_version_id(target, OWNER) is None
    assert exposure.visible_version_id(target, OWNER) == CURRENT  # 소유자는 계속 본다


def test_private_skill_is_invisible_to_non_owners_even_with_a_pointer() -> None:
    """'private이면서 포인터 있음' — 이전 공개 version은 보존되지만 노출되지 않는다."""
    target = skill(visibility="private")
    assert exposure.can_view(target, OTHER) is False
    assert exposure.can_view(target, OWNER) is True
    assert exposure.resolve_run_version_id(target, OTHER) is None


# ---------- 관문 규칙표 (S-HCI-5·13·14) ----------


@pytest.mark.parametrize(
    ("origin", "hold", "target", "expected"),
    [
        ("authored", False, "org", None),
        ("authored", True, "org", "expose_org"),
        ("imported", False, "org", "expose_org"),
        ("authored", False, "system", "expose_system"),
        ("imported", False, "system", "expose_system"),
        ("system_seed", False, "system", "expose_system"),
    ],
    ids=[
        "authored-org",
        "authored-org-held",
        "imported-org",
        "authored-system",
        "imported-system",
        "seed-system-outside-seed-path",
    ],
)
def test_gate_rule_table(origin: str, hold: bool, target: str, expected: str | None) -> None:
    assert (
        exposure.required_review_kind(skill(origin=origin, exposure_hold=hold), target)
        == expected
    )


def test_seed_skill_cannot_become_org() -> None:
    with pytest.raises(exposure.ExposureError) as exc:
        exposure.required_review_kind(skill(origin="system_seed"), "org")
    assert exc.value.reason == "seed_org_forbidden"


def test_private_is_not_a_publish_target() -> None:
    with pytest.raises(exposure.ExposureError) as exc:
        exposure.required_review_kind(skill(), "private")
    assert exc.value.status_code == 422


def test_system_approval_covers_org_but_not_the_reverse() -> None:
    assert set(exposure.accepted_kinds("expose_org")) == {"expose_org", "expose_system"}
    assert exposure.accepted_kinds("expose_system") == ("expose_system",)


@pytest.mark.parametrize(
    ("current", "expected"),
    [("private", "org"), ("org", "org"), ("system", "system")],
)
def test_publish_target_never_demotes(current: str, expected: str) -> None:
    """R3-11: version publish의 목표는 max(현재, org)다."""
    assert exposure.target_visibility_for_publish(skill(visibility=current)) == expected


# ---------- 검토 승인의 stale 판정 (R5-01) ----------


@pytest.mark.parametrize(
    ("request_version", "pointer_version", "current", "target", "stale"),
    [
        (2, 1, "private", "org", False),  # private에서 만든 org 요청 — stale이 아니다
        (3, None, "private", "org", False),  # 첫 공개(imported)
        (1, 2, "org", "org", True),  # (a) 더 새 공개 version이 있다
        (2, 2, "system", "org", True),  # (b) 현재가 목표보다 높다 — 승인하면 강등
        (2, 2, "org", "system", False),  # 승격 요청
    ],
    ids=["private-to-org", "first-publish", "older-version", "would-demote", "promotion"],
)
def test_stale_request(
    request_version: int, pointer_version: int | None, current: str, target: str, stale: bool
) -> None:
    assert (
        exposure.is_stale_request(
            request_version=request_version,
            pointer_version=pointer_version,
            current_visibility=current,
            target_visibility=target,
        )
        is stale
    )


# ---------- 공개 계약: deprecation과 쿼리 모양 ----------


def _wrapper(**overrides: object) -> SimpleNamespace:
    fields = {"enabled": True, "scope": "user", "owner_id": OWNER, "source_kind": "skill_package"}
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_agent_visible_to_warns_for_skill_wrappers_without_changing_the_result() -> None:
    """R5-03: 구 함수는 동작을 유지하고 경고만 낸다(두 제품의 워크플로우 저장을 깨지 않음)."""
    with pytest.warns(DeprecationWarning, match="skill_agent_visible_to"):
        assert agent_visible_to(_wrapper(scope="system"), OTHER) is True
    with pytest.warns(DeprecationWarning):
        assert agent_visible_to(_wrapper(), OTHER) is False


def test_agent_visible_to_is_silent_for_ordinary_agents_and_fakes() -> None:
    """sl `test_workflows.py`의 가짜 객체에는 source_kind가 없다 — 경고도 오류도 없다."""
    fake = SimpleNamespace(enabled=True, scope="system", owner_id=None)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert agent_visible_to(fake, OTHER) is True
        assert agent_visible_to(_wrapper(source_kind="workflow"), OWNER) is True


def test_published_system_skill_predicates_is_deprecated() -> None:
    with pytest.warns(DeprecationWarning, match="published_system_skill_query"):
        published_system_skill_predicates()


def test_run_skill_exposure_uses_the_published_pointer_not_status() -> None:
    """R3-03: `run_skill`은 `skills.status`가 아니라 공개 포인터로 조인한다."""
    sql = str(published_system_skill_query().compile(dialect=postgresql.dialect()))
    joins_and_filters = sql.split("\nFROM ", 1)[1]
    assert "skills.published_version_id = exposed_version.id" in joins_and_filters
    assert "skills.status" not in joins_and_filters
    assert "agents.scope" not in joins_and_filters  # wrapper 필드로 판정하지 않는다
    assert "agents.enabled" not in joins_and_filters
    assert "exposed_version.runtime_profile" in joins_and_filters  # standard_agent_v1 제외(D6)


def test_published_pointer_has_a_single_writer() -> None:
    """D8 불변식의 기계 가드 — 포인터 **행**을 쓰는 코드는 공개 관문뿐이다(리뷰 E-04).

    FK도 트리거도 없는 앱 레벨 포인터라, 다른 모듈이 대입하면 관문을 조용히 우회한다.
    응답 DTO 필드(`out.published_version_id`)와 리비전 백필 SQL 문자열은 행을 직접 쓰는
    코드가 아니므로 제외한다 — 백필은 관문이 없던 과거 데이터를 한 번 메우는 경로다.
    """
    core = pathlib.Path(__file__).resolve().parent.parent / "biznotes_core"
    assignment = re.compile(r"(?<![=!<>])\bpublished_version_id\s*=(?!=)")
    writers: set[str] = set()
    for path in core.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not assignment.search(stripped):
                continue
            if stripped.startswith(("out.", "item.", "SET ")):
                continue  # 응답 DTO 필드와 백필 SQL 문자열
            writers.add(str(path.relative_to(core)))

    assert writers == {"services/skills/exposure.py"}, (
        f"공개 포인터를 관문 밖에서 대입한다 — set_published_version만 바꿔야 한다: {writers}"
    )
