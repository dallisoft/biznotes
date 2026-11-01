"""기준선(`1079bbf..c7e682e`) 추적표 공백 행의 실경로 테스트 — 백엔드 (계획 AC-21 (a)).

- #18 `app.schemas.agent`의 코어 입력 계약 재노출 + 본 모듈 공개 이름 보존(P0-22)
- #19 `app.schemas.skill`의 코어 스킬 스키마 재노출
- #25 시드 `upsert_skill_creator()`가 런타임 플래그에 맞는 본문을 실제로 저장
"""

from collections.abc import Iterator

import pytest
from biznotes_core.config import reset_settings


def test_agent_schema_reexports_core_input_contract_and_keeps_module_names() -> None:
    """#18·P0-22: 재노출은 코어 객체 그 자체이고, 본 모듈의 스키마도 공개 이름으로 남는다."""
    import biznotes_core.schemas.agent as core_agent

    import app.schemas.agent as product_agent

    for name in ("AgentInput", "JsonSchemaProperty", "ParamsSchema"):
        assert getattr(product_agent, name) is getattr(core_agent, name)
    # `__all__`로 공개 이름을 셋으로 좁히지 않는다 — `import *`가 본 모듈 스키마를 잃지 않는다
    assert not hasattr(product_agent, "__all__")
    namespace: dict[str, object] = {}
    exec("from app.schemas.agent import *", namespace)
    for name in ("AgentOut", "AgentRunOut", "WorkflowOut", "ParamsSchema"):
        assert name in namespace
    # 재노출 계약이 직렬화에서도 같은 모양을 쓴다(코어 ParamsSchema → 제품 WorkflowCreate)
    body = product_agent.WorkflowCreate(
        name="n",
        definition={"nodes": [], "edges": []},
        params_schema={"type": "object", "properties": {"topic": {"type": "string"}}},
    )
    assert isinstance(body.params_schema, core_agent.ParamsSchema)
    assert body.params_schema.model_dump()["properties"]["topic"]["type"] == "string"


def test_skill_schema_reexports_are_the_core_schemas() -> None:
    """#19: `app.schemas.skill.__all__`의 모든 이름이 코어 스키마 모듈의 같은 객체다."""
    import biznotes_core.schemas.skill as core_skill

    import app.schemas.skill as product_skill

    assert len(product_skill.__all__) == len(set(product_skill.__all__)) == 27
    for name in product_skill.__all__:
        assert getattr(product_skill, name) is getattr(core_skill, name), name


@pytest.fixture
def runtime_flag(monkeypatch: pytest.MonkeyPatch, request) -> Iterator[bool]:
    enabled: bool = request.param
    monkeypatch.setenv("SKILL_STANDARD_RUNTIME_ENABLED", "true" if enabled else "false")
    reset_settings()
    yield enabled
    monkeypatch.delenv("SKILL_STANDARD_RUNTIME_ENABLED")
    reset_settings()


@pytest.mark.parametrize("runtime_flag", [True, False], indirect=True)
async def test_upsert_skill_creator_stores_the_body_for_the_runtime_flag(
    db, mem_storage, runtime_flag: bool
) -> None:
    """#25: 시드가 플래그별 본문(`skill_creator_body()`)을 system 스킬의 SKILL.md로 저장한다."""
    from biznotes_core.models import Skill, SkillVersion
    from sqlalchemy import select

    from scripts.seed import (
        SKILL_CREATOR_BODY,
        SKILL_CREATOR_STANDARD_BODY,
        upsert_skill_creator,
    )

    await upsert_skill_creator()

    skill = (
        await db.execute(select(Skill).where(Skill.slug == "skill-creator"))
    ).scalar_one()
    assert skill.visibility == "system"
    assert skill.owner_id is None
    version = await db.get(SkillVersion, skill.current_version_id)
    assert version is not None
    stored = [
        data.decode()
        for (_bucket, key), data in mem_storage.items()
        if key.startswith(version.package_uri) and key.endswith("SKILL.md")
    ]
    assert len(stored) == 1
    expected = SKILL_CREATOR_STANDARD_BODY if runtime_flag else SKILL_CREATOR_BODY
    other = SKILL_CREATOR_BODY if runtime_flag else SKILL_CREATOR_STANDARD_BODY
    assert stored[0].startswith("---\nname: skill-creator\n")
    assert stored[0].endswith(expected)
    assert other not in stored[0]
