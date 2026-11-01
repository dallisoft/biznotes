"""스킬 응답의 `chat_entry_reason` 계약 (#135, docs/plans/skill-chat-entry-reason/).

값 집합은 `ChatEntryReason`이 정본이다. `schemas`는 `services`를 import할 수 없어 응답용 Literal
(`ChatEntryReasonValue`)과의 1:1을 여기서 고정한다. 라우터의 판정 적재는
`tests/test_skill_chat_entry_reason_pg.py`가 실제 PostgreSQL로 검증한다.
"""

from typing import get_args

from fastapi import FastAPI

from biznotes_core.api.v1 import skills as skills_api
from biznotes_core.api.v1.admin import skills as admin_skills_api
from biznotes_core.schemas.enums import ChatEntryReasonValue
from biznotes_core.schemas.skill import AdminSkillOut, SkillImportOut, SkillOut
from biznotes_core.services.agents.chat_entry import ChatEntryReason

# #135 이전 `SkillOut`의 응답 키 — 추가만 허용한다(AGENTS.md §5.1)
_KEYS_BEFORE = {
    "id",
    "owner_id",
    "name",
    "description",
    "slug",
    "tags",
    "visibility",
    "status",
    "current_version_id",
    "forked_from_version_id",
    "clone_count",
    "run_count",
    "enabled",
    "created_at",
    "updated_at",
    "published_version_id",
    "origin",
    "wrapper_agent_id",
    "params_schema",
    "input",
    "can_edit",
    "can_run",
}


def test_literal_matches_the_canonical_enum() -> None:
    values = get_args(ChatEntryReasonValue)
    assert len(values) == len(set(values))
    assert set(values) == {reason.value for reason in ChatEntryReason}


def test_field_is_added_only_and_defaults_to_none() -> None:
    assert set(SkillOut.model_fields) == _KEYS_BEFORE | {"chat_entry_reason"}
    for cls in (SkillOut, AdminSkillOut, SkillImportOut):
        field = cls.model_fields["chat_entry_reason"]
        assert field.default is None
        assert not field.is_required()


def test_openapi_exposes_the_value_set_as_an_enum() -> None:
    app = FastAPI()
    app.include_router(skills_api.router)
    app.include_router(admin_skills_api.router, prefix="/admin")
    schemas = app.openapi()["components"]["schemas"]
    expected = sorted(reason.value for reason in ChatEntryReason)
    for name in ("SkillOut", "AdminSkillOut", "SkillImportOut"):
        prop = schemas[name]["properties"]["chat_entry_reason"]
        enums = [s for s in prop["anyOf"] if "enum" in s]
        assert len(enums) == 1, name
        assert sorted(enums[0]["enum"]) == expected, name
        assert {"type": "null"} in prop["anyOf"], name
        assert "chat_entry_reason" not in schemas[name].get("required", [])
