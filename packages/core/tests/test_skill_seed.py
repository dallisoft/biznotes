"""seed_system_skill이 제품 설정의 매니페스트 파일명으로 시드 패키지를 만든다(#68).

biznotes는 `skill_manifest_filename="biznotes.json"`이다. 시드가 이름을
`slagents.json`으로 고정하면 `builder.load_skill_files`가 매니페스트를 찾지 못해
`{}`로 읽는다(params_schema·system 유실).
"""

import contextlib
import uuid
from typing import Any

import pytest
from conftest import make_settings

from biznotes_core.services.skills import builder, service
from biznotes_core.services.skills import package as pkg

MANIFEST = {
    "schema_version": "1.0",
    "mode": "instruction",
    "system": "시드 시스템 프롬프트",
    "params_schema": {
        "type": "object",
        "properties": {"topic": {"type": "string"}},
        "required": ["topic"],
    },
}


class _Result:
    def scalar_one_or_none(self) -> None:
        return None


class _FakeDB:
    """시드 대상 스킬이 아직 없는 DB — 새 스킬 생성 경로만 탄다."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    async def execute(self, _stmt: Any) -> _Result:
        return _Result()

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def begin_nested(self) -> contextlib.nullcontext[None]:
        # 새 시드 INSERT는 savepoint 안에서 flush한다(#412) — 대역에서는 아무 일도 없다
        return contextlib.nullcontext()

    async def flush(self) -> None:
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()


@pytest.fixture
def seeded(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    settings = make_settings(skill_manifest_filename="biznotes.json")
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    monkeypatch.setattr(builder, "get_settings", lambda: settings)

    captured: dict[str, Any] = {}

    def fake_save_draft(skill_id: uuid.UUID, files: dict[str, bytes]) -> None:
        captured["files"] = files

    async def fake_materialize(db: Any, skill: Any, **kw: Any) -> tuple[Any, Any]:
        captured["materialized"] = kw
        return object(), object()

    async def fake_publish_seed(db: Any, skill: Any, version: Any) -> None:
        captured["published"] = (skill, version)

    monkeypatch.setattr(pkg, "save_draft", fake_save_draft)
    monkeypatch.setattr(service, "_materialize_version", fake_materialize)
    monkeypatch.setattr(service.exposure, "publish_seed_version", fake_publish_seed)
    return captured


async def test_seed_uses_configured_manifest_filename(seeded: dict[str, Any]) -> None:
    await service.seed_system_skill(
        _FakeDB(),  # type: ignore[arg-type]
        slug="report-writer",
        name="보고서 작성",
        description="보고서를 쓴다",
        skill_md="---\nname: report-writer\ndescription: 보고서를 쓴다\n---\n\n# 보고서",
        manifest=MANIFEST,
    )

    files = seeded["files"]
    assert set(files) == {"SKILL.md", "biznotes.json"}
    # 읽는 쪽(builder)이 같은 설정으로 매니페스트를 그대로 되찾는다 — 고정 이름이면 {}가 된다
    loaded = builder.load_skill_files(files)
    assert loaded.manifest == MANIFEST
    assert loaded.manifest["params_schema"]["required"] == ["topic"]
    assert seeded["materialized"]["changelog"] == "seed"
    # 새 시드 스킬은 출처를 명시해서 만든다(server_default에 기대지 않는다, R3-16)
    skill, _ = seeded["published"]
    assert skill.origin == "system_seed"
    assert skill.visibility == "system"


def test_hardcoded_filename_loses_manifest_under_biznotes_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """음성 대조: 수정 전 시드 패키지 모양(`slagents.json`)은 biznotes 설정에서
    매니페스트를 잃는다."""
    monkeypatch.setattr(
        builder, "get_settings", lambda: make_settings(skill_manifest_filename="biznotes.json")
    )
    import json

    files = {
        "SKILL.md": b"---\nname: report-writer\ndescription: d\n---\n\nbody",
        "slagents.json": json.dumps(MANIFEST).encode("utf-8"),
    }
    assert builder.load_skill_files(files).manifest == {}
