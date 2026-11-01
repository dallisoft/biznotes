"""마인드맵 산출물 계약(mindmap-agent-skill §8.1·AC-1.1·AC-1.11).

- `mindmap-shape`: 웹 관용 파서(`apps/web/src/lib/mindmap/parse.ts`)와 코어
  `_validate_mindmap_shape`가 같은 공유 fixture(`apps/web/src/lib/__fixtures__/mindmap/`)에
  같은 판정을 내린다. 웹 쪽은 `parse.test.ts`가 읽는다. BFS 절단도 코어 결과를 fixture로 고정한다.
- `mindmap-artifact-binding`(P4 — plan §10 r5): 시드 시스템 스킬 `mindmap`의 산출물
  선언과 웹 스튜디오 레지스트리 리터럴(스킬 slug·산출물 이름)이 같다. 시드 사본
  (`scripts/seed_skills/mindmap/`)은 저장소 정본 `skills/mindmap/`과 바이트 동일하다(D-21).
  시드는 멱등이고 워크플로우 「마인드맵」을 은퇴시키며, 스킬 시드가 실패하면 은퇴하지 않는다(D-22).
- 권한: 비멤버는 비공개 노트북의 실행 생성·목록·산출물에 모두 404,
  공유자(read·write)·admin은 200.
"""

import copy
import json
import re
import uuid
from pathlib import Path

import httpx
import pytest
from biznotes_core.core.db import async_session_factory
from biznotes_core.models import Agent, AgentRun, Notebook, Skill, SkillVersion
from biznotes_core.services.agents.validators import (
    _MINDMAP_NODE_CAP,
    _count_mindmap_nodes,
    _truncate_mindmap_bfs,
    _validate_mindmap_shape,
)
from biznotes_core.services.skills.validator import SkillValidationError
from sqlalchemy import func, select

from tests.conftest import bearer, login, share_with

API_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = API_DIR.parents[1]
WEB_SRC = API_DIR.parent / "web/src"
FIXTURES = WEB_SRC / "lib/__fixtures__/mindmap"
STUDIO_REGISTRY = WEB_SRC / "lib/studio-registry.ts"
ARTIFACT_VIEWERS = WEB_SRC / "lib/artifact-viewers.ts"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _names(prefix: str) -> list[str]:
    return sorted(p.name for p in FIXTURES.glob(f"{prefix}-*.json"))


# ---------- mindmap-shape ----------


def test_fixture_counts():
    assert len(_names("valid")) >= 5
    assert len(_names("invalid")) >= 6
    assert len(_names("fenced")) >= 1


@pytest.mark.parametrize("name", _names("valid"))
def test_core_verdicts_valid(name: str):
    assert _validate_mindmap_shape(_load(name)) is None


@pytest.mark.parametrize("name", _names("invalid"))
def test_core_verdicts_invalid(name: str):
    assert _validate_mindmap_shape(_load(name)) is not None


@pytest.mark.parametrize("name", _names("fenced"))
def test_fenced_bodies_need_lenient_parse(name: str):
    """펜스·설명 문장이 붙은 본문은 코어 json.loads로는 실패한다 — 웹 관용 파서가 필요한
    근거(core-01 전)."""
    body = _load(name)["body"]
    with pytest.raises(json.JSONDecodeError):
        json.loads(body)


def test_truncation_matches_core():
    doc = _load("truncate-input.json")
    expected = _load("truncate-expected.json")
    root = copy.deepcopy(doc["root"])
    assert _count_mindmap_nodes(root) > _MINDMAP_NODE_CAP
    out, truncated = _truncate_mindmap_bfs(root, _MINDMAP_NODE_CAP)
    assert out == expected["root"]
    assert truncated is expected["truncated"] is True


# ---------- mindmap-artifact-binding ----------


SKILL_FILES = ("SKILL.md", "biznotes.json")


def test_seed_copy_matches_repo_package():
    from scripts.seed import MINDMAP_SKILL_DIR

    assert MINDMAP_SKILL_DIR == API_DIR / "scripts/seed_skills/mindmap"
    assert sorted(p.name for p in MINDMAP_SKILL_DIR.iterdir()) == sorted(SKILL_FILES)
    for name in SKILL_FILES:
        assert (MINDMAP_SKILL_DIR / name).read_bytes() == (
            REPO_ROOT / "skills/mindmap" / name
        ).read_bytes(), name


def _seed_manifest() -> dict:
    from scripts.seed import MINDMAP_SKILL_DIR

    return json.loads((MINDMAP_SKILL_DIR / "biznotes.json").read_text(encoding="utf-8"))


def test_seed_binding():
    from scripts.seed import SEED_AGENTS

    assert _seed_manifest()["artifact"] == {
        "name": "mindmap.json",
        "content_type": "application/json",
        "schema": "mindmap",
    }
    # 은퇴한 워크플로우 정의는 시드에 없다 — upsert_agents가 다시 켜지 않는다
    assert [s for s in SEED_AGENTS if s["name"] == "마인드맵"] == []


def _ts_const(path: Path, name: str) -> str:
    m = re.search(rf'export const {name} = "([^"]+)";', path.read_text(encoding="utf-8"))
    assert m, f"{path.name}에 {name} 리터럴이 없다"
    return m.group(1)


def test_web_binding_literals():
    from scripts.seed import MINDMAP_SKILL_SLUG

    assert (
        _ts_const(ARTIFACT_VIEWERS, "MINDMAP_ARTIFACT_NAME") == _seed_manifest()["artifact"]["name"]
    )
    assert _ts_const(STUDIO_REGISTRY, "MINDMAP_SKILL_SLUG") == MINDMAP_SKILL_SLUG
    # 시드 스킬은 seed_system_skill이 visibility=system으로 만든다
    assert _ts_const(STUDIO_REGISTRY, "STUDIO_SKILL_VISIBILITY") == "system"


# ---------- 시드(P4 — AC-4.1·AC-4.3) ----------


async def _legacy_mindmap_workflow(db) -> Agent:
    """은퇴 전 dev·운영 DB에 남아 있는 시드 워크플로우 행."""
    agent = Agent(
        name="마인드맵",
        kind="workflow",
        icon="🧠",
        definition={"nodes": [], "edges": []},
        params_schema={"type": "object"},
        scope="system",
        source_kind="legacy",
        enabled=True,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


async def _mindmap_skill_state() -> tuple[Skill | None, int]:
    async with async_session_factory() as s:
        skill = (
            await s.execute(
                select(Skill).where(Skill.slug == "mindmap", Skill.origin == "system_seed")
            )
        ).scalar_one_or_none()
        count = 0
        if skill is not None:
            count = (
                await s.execute(
                    select(func.count())
                    .select_from(SkillVersion)
                    .where(SkillVersion.skill_id == skill.id)
                )
            ).scalar_one()
        return skill, count


async def _enabled(agent_id) -> bool:
    async with async_session_factory() as s:
        return (await s.get(Agent, agent_id)).enabled


async def test_seed_registers_skill_and_retires_workflow(db, monkeypatch):
    from scripts.seed import upsert_agents, upsert_mindmap_skill

    monkeypatch.setattr("biznotes_core.services.storage.ensure_buckets", lambda: None)
    legacy = await _legacy_mindmap_workflow(db)

    await upsert_mindmap_skill()
    skill, versions = await _mindmap_skill_state()
    assert skill is not None
    assert (skill.visibility, skill.name, skill.enabled) == ("system", "마인드맵", True)
    assert skill.published_version_id is not None
    assert versions == 1
    assert await _enabled(legacy.id) is False

    # 멱등 — 같은 패키지는 새 버전을 만들지 않고, 에이전트 시드를 다시 돌려도 워크플로우는 꺼진 채다
    await upsert_agents()
    await upsert_mindmap_skill()
    assert (await _mindmap_skill_state())[1] == 1
    assert await _enabled(legacy.id) is False

    async with async_session_factory() as s:
        version = await s.get(SkillVersion, skill.published_version_id)
        wrapper = (
            await s.execute(
                select(Agent).where(
                    Agent.skill_version_id == version.id, Agent.source_kind == "skill_package"
                )
            )
        ).scalar_one()
    assert wrapper.enabled is True


async def test_seed_keeps_workflow_when_admin_disabled_skill(db, monkeypatch, capsys):
    """롤백 → 재적용 왕복(P4 리뷰 F-1): 관리자가 스킬을 끄고 워크플로우가 되살아난 뒤 다시 시드해도
    관리자 차단은 풀지 않고, 실행할 산출 주체가 없어지지 않게 워크플로우를 끄지 않는다."""
    from scripts.seed import upsert_mindmap_skill

    monkeypatch.setattr("biznotes_core.services.storage.ensure_buckets", lambda: None)
    legacy = await _legacy_mindmap_workflow(db)
    await upsert_mindmap_skill()
    assert await _enabled(legacy.id) is False

    # 롤백 런북: 관리자가 스킬을 비활성화하고, 되돌린 시드가 워크플로우를 다시 켠다
    async with async_session_factory() as s:
        (await s.get(Skill, (await _mindmap_skill_state())[0].id)).enabled = False
        (await s.get(Agent, legacy.id)).enabled = True
        await s.commit()

    capsys.readouterr()
    await upsert_mindmap_skill()
    assert "WARNING: system skill mindmap is not runnable" in capsys.readouterr().out
    skill, versions = await _mindmap_skill_state()
    assert skill.enabled is False  # 관리자 차단 보존
    assert versions == 1
    assert await _enabled(legacy.id) is True


async def test_seed_failure_keeps_workflow(db, monkeypatch, tmp_path):
    from scripts.seed import MINDMAP_SKILL_DIR, upsert_mindmap_skill

    monkeypatch.setattr("biznotes_core.services.storage.ensure_buckets", lambda: None)
    legacy = await _legacy_mindmap_workflow(db)
    bad = json.loads((MINDMAP_SKILL_DIR / "biznotes.json").read_text(encoding="utf-8"))
    bad["artifact"]["schema"] = "no-such-schema"
    (tmp_path / "biznotes.json").write_text(json.dumps(bad), encoding="utf-8")
    (tmp_path / "SKILL.md").write_bytes((MINDMAP_SKILL_DIR / "SKILL.md").read_bytes())

    with pytest.raises(SkillValidationError) as exc:
        await upsert_mindmap_skill(tmp_path)
    assert exc.value.reason == "bad_artifact_schema"
    assert await _enabled(legacy.id) is True
    assert (await _mindmap_skill_state())[0] is None


# ---------- 권한(§8.2, AC-1.11) ----------


async def _private_notebook(db, owner) -> Notebook:
    nb = Notebook(owner_id=owner.id, title=f"mm-{uuid.uuid4().hex[:6]}")
    db.add(nb)
    await db.commit()
    await db.refresh(nb)
    return nb


async def _mindmap_agent(db) -> Agent:
    agent = Agent(
        name=f"마인드맵-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        icon="🧠",
        definition={"nodes": [], "edges": []},
        params_schema={"type": "object", "properties": {"topic": {"type": "string"}}},
        scope="system",
        enabled=True,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


async def _mindmap_run(db, nb, user, agent) -> AgentRun:
    run = AgentRun(
        agent_id=agent.id,
        notebook_id=nb.id,
        user_id=user.id,
        params={"topic": "x"},
        status="succeeded",
        progress=100,
        trigger="manual",
        artifact_keys=[
            {
                "key": f"{uuid.uuid4()}/mindmap.json",
                "name": "mindmap.json",
                "content_type": "application/json",
            }
        ],
        token_usage={},
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def test_non_member_create_run_404(client: httpx.AsyncClient, db, user_a, user_b):
    nb = await _private_notebook(db, user_a)
    agent = await _mindmap_agent(db)
    token_b = await login(client, user_b.email)
    res = await client.post(
        f"/api/v1/notebooks/{nb.id}/agent-runs",
        json={"agent_id": str(agent.id), "params": {"topic": "x"}},
        headers=bearer(token_b),
    )
    assert res.status_code == 404


async def test_non_member_artifact_404(client: httpx.AsyncClient, db, user_a, user_b, mem_storage):
    nb = await _private_notebook(db, user_a)
    agent = await _mindmap_agent(db)
    run = await _mindmap_run(db, nb, user_a, agent)
    from app.core.config import get_settings

    mem_storage[(get_settings().storage_bucket_artifacts, run.artifact_keys[0]["key"])] = (
        b'{"root":{}}'
    )
    token_b = await login(client, user_b.email)
    res = await client.get(
        f"/api/v1/agent-runs/{run.id}/artifacts/mindmap.json", headers=bearer(token_b)
    )
    assert res.status_code == 404

    # 양성 대조 — 소유자는 본문을 받는다(attachment + nosniff)
    client.cookies.clear()
    token_a = await login(client, user_a.email)
    res = await client.get(
        f"/api/v1/agent-runs/{run.id}/artifacts/mindmap.json", headers=bearer(token_a)
    )
    assert res.status_code == 200
    assert res.content == b'{"root":{}}'


async def test_non_member_list_runs_404(client: httpx.AsyncClient, db, user_a, user_b):
    nb = await _private_notebook(db, user_a)
    agent = await _mindmap_agent(db)
    await _mindmap_run(db, nb, user_a, agent)
    token_b = await login(client, user_b.email)
    res = await client.get(f"/api/v1/notebooks/{nb.id}/agent-runs", headers=bearer(token_b))
    assert res.status_code == 404


@pytest.mark.parametrize("permission", ["read", "write"])
async def test_list_runs_shared_200(client: httpx.AsyncClient, db, user_a, user_b, permission):
    nb = await _private_notebook(db, user_a)
    agent = await _mindmap_agent(db)
    run = await _mindmap_run(db, nb, user_a, agent)
    await share_with(db, nb.id, user_b, permission)
    await db.commit()
    token_b = await login(client, user_b.email)
    res = await client.get(f"/api/v1/notebooks/{nb.id}/agent-runs", headers=bearer(token_b))
    assert res.status_code == 200
    assert [r["id"] for r in res.json()] == [str(run.id)]


async def test_list_runs_admin_200(client: httpx.AsyncClient, db, user_a, admin_user):
    nb = await _private_notebook(db, user_a)
    agent = await _mindmap_agent(db)
    run = await _mindmap_run(db, nb, user_a, agent)
    token = await login(client, admin_user.email)
    res = await client.get(f"/api/v1/notebooks/{nb.id}/agent-runs", headers=bearer(token))
    assert res.status_code == 200
    assert [r["id"] for r in res.json()] == [str(run.id)]
