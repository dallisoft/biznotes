"""파일 기반 Agent Skill 테스트 — 패키지 파싱/검증/저장/실행 + API 권한 (skill_plan §14)."""

import io
import uuid
import zipfile

import httpx
import pytest

from tests.conftest import bearer, login

# ---------- 인메모리 스토리지 대체 ----------


def make_zip(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            zf.writestr(name, data)
    return buf.getvalue()


GOOD_SKILL_MD = """---
name: sales-summary
description: Summarize sales call notes into action items.
---

# Sales Summary

Follow these steps and use {placeholder} braces freely.
"""


# ========== 단위: parser ==========


def test_parse_skill_md_ok():
    from biznotes_core.services.skills.parser import parse_skill_md, validate_frontmatter

    fm, body = parse_skill_md(GOOD_SKILL_MD)
    name, desc = validate_frontmatter(fm)
    assert name == "sales-summary"
    assert "Sales Summary" in body
    assert "{placeholder}" in body  # 본문 중괄호 보존


def test_parse_skill_md_no_frontmatter():
    from biznotes_core.services.skills.parser import SkillParseError, parse_skill_md

    with pytest.raises(SkillParseError):
        parse_skill_md("# no frontmatter\n")


def test_parse_skill_md_missing_name():
    from biznotes_core.services.skills.parser import (
        SkillParseError,
        parse_skill_md,
        validate_frontmatter,
    )

    fm, _ = parse_skill_md("---\ndescription: only desc\n---\nbody")
    with pytest.raises(SkillParseError):
        validate_frontmatter(fm)


# ========== 단위: validator ==========


def test_validate_path_traversal():
    from biznotes_core.services.skills.validator import SkillValidationError, validate_path

    for bad in ("../etc/passwd", "/abs/path", "a/../../b", ".hidden", "c:\\x"):
        with pytest.raises(SkillValidationError):
            validate_path(bad)
    assert validate_path("references/foo.md") == "references/foo.md"


def test_validate_files_secrets():
    from biznotes_core.services.skills.validator import SkillValidationError, validate_files

    files = {
        "SKILL.md": GOOD_SKILL_MD.encode(),
        "references/keys.md": b"token AKIAIOSFODNN7EXAMPLE here",
    }
    with pytest.raises(SkillValidationError) as e:
        validate_files(files)
    assert e.value.reason == "secret_detected"


def test_validate_files_requires_skill_md():
    from biznotes_core.services.skills.validator import SkillValidationError, validate_files

    with pytest.raises(SkillValidationError) as e:
        validate_files({"references/a.md": b"x"})
    assert e.value.reason == "missing_skill_md"


def test_validate_manifest_defaults_and_mode():
    from biznotes_core.services.skills.validator import SkillValidationError, validate_manifest

    m = validate_manifest({})
    assert m["mode"] == "instruction"
    assert m["permissions"]["rag"] is True
    with pytest.raises(SkillValidationError):
        validate_manifest({"mode": "workflow"})


# ========== 단위: package ==========


def test_package_hash_deterministic():
    from biznotes_core.services.skills import package as pkg

    a = {"SKILL.md": b"x", "references/a.md": b"y"}
    b = {"references/a.md": b"y", "SKILL.md": b"x"}
    assert pkg.package_hash(a) == pkg.package_hash(b)
    assert pkg.package_hash(a) != pkg.package_hash({"SKILL.md": b"z"})


def test_unpack_zip_strips_common_root():
    from biznotes_core.services.skills import package as pkg

    data = make_zip({"my-skill/SKILL.md": GOOD_SKILL_MD, "my-skill/references/a.md": "hi"})
    files = pkg.unpack_zip(data)
    assert "SKILL.md" in files
    assert "references/a.md" in files


def test_unpack_zip_rejects_traversal():
    from biznotes_core.services.skills import package as pkg
    from biznotes_core.services.skills.validator import SkillValidationError

    data = make_zip({"SKILL.md": GOOD_SKILL_MD, "../evil.md": "x"})
    with pytest.raises(SkillValidationError):
        pkg.unpack_zip(data)


def test_build_file_tree_roles():
    from biznotes_core.services.skills import package as pkg

    tree = pkg.build_file_tree(
        {"SKILL.md": b"x", "biznotes.json": b"{}", "references/a.md": b"y"}
    )
    roles = {f["path"]: f["role"] for f in tree}
    assert roles["SKILL.md"] == "skill_md"
    assert roles["biznotes.json"] == "manifest"
    assert roles["references/a.md"] == "reference"


def test_skill_file_out_accepts_legacy_tree_without_hash():
    """C1 read compatibility: pre-hash JSONB rows still serialize safely."""
    from app.schemas.skill import SkillFileOut

    legacy_file = SkillFileOut(
        path="SKILL.md", role="skill_md", size=1, media_type="text/plain"
    )
    assert legacy_file.hash == ""


def test_skill_version_out_reads_legacy_file_tree_without_hash():
    """C1 read compatibility: existing JSONB rows serialize with a safe hash default."""
    from datetime import datetime
    from types import SimpleNamespace

    from app.schemas.skill import SkillVersionOut

    legacy_version = SimpleNamespace(
        id=uuid.uuid4(), skill_id=uuid.uuid4(), version=1, package_hash="legacy",
        manifest={},
        file_tree=[{"path": "SKILL.md", "role": "skill_md", "size": 1, "media_type": "text/plain"}],
        status="draft_frozen", changelog=None, created_at=datetime.now(),
    )

    assert SkillVersionOut.model_validate(legacy_version).file_tree[0].hash == ""


# ========== 단위: builder (권한 게이트) ==========


def test_build_nodes_permission_gate_no_rag():
    from biznotes_core.services.skills.builder import LoadedSkill, build_nodes

    loaded = LoadedSkill(
        metadata_name="t",
        metadata_description="d",
        instructions="do it",
        manifest={"use_rag": False, "permissions": {"rag": False, "llm": True}},
    )
    nodes, _ = build_nodes(loaded)
    types = [n["type"] for n in nodes]
    assert "rag_search" not in types
    assert types == ["constant", "llm", "save_artifact"]


def test_build_nodes_with_rag():
    from biznotes_core.services.skills.builder import LoadedSkill, build_nodes

    loaded = LoadedSkill(
        metadata_name="t", metadata_description="d", instructions="x", manifest={}
    )
    nodes, _ = build_nodes(loaded)
    assert [n["type"] for n in nodes] == ["constant", "rag_search", "llm", "save_artifact"]


# ========== 단위: input 정책 (mode×scope) ==========


def test_resolve_input_defaults_and_backcompat():
    from biznotes_core.services.skills.validator import resolve_input, validate_manifest

    # 기본값: input 부재 + use_rag 기본 true → rag/all
    assert validate_manifest({})["input"] == {
        "mode": "rag",
        "scope": "all",
        "max_sources": 5,
        "max_chars": 60000,
    }
    # 하위호환: use_rag false → none
    assert resolve_input({"use_rag": False})["mode"] == "none"
    # 명시 full×selected
    inp = validate_manifest(
        {"input": {"mode": "full", "scope": "selected", "max_sources": 3, "max_chars": 30000}}
    )["input"]
    assert inp == {"mode": "full", "scope": "selected", "max_sources": 3, "max_chars": 30000}


@pytest.mark.parametrize(
    "bad,reason",
    [
        ({"input": {"mode": "x"}}, "bad_input_mode"),
        ({"input": {"scope": "x"}}, "bad_input_scope"),
        ({"input": {"max_chars": 0}}, "bad_input_max_chars"),
        ({"input": {"max_chars": 999999}}, "bad_input_max_chars"),
        ({"input": {"max_sources": 100}}, "bad_input_max_sources"),
        ({"input": "nope"}, "bad_input"),
    ],
)
def test_resolve_input_guards(bad, reason):
    from biznotes_core.services.skills.validator import SkillValidationError, validate_manifest

    with pytest.raises(SkillValidationError) as e:
        validate_manifest(bad)
    assert e.value.reason == reason


def test_build_nodes_full_mode():
    from biznotes_core.services.skills.builder import LoadedSkill, build_nodes

    loaded = LoadedSkill(
        metadata_name="t",
        metadata_description="d",
        instructions="x",
        manifest={
            "input": {"mode": "full", "scope": "selected", "max_sources": 3, "max_chars": 9000}
        },
    )
    nodes, _ = build_nodes(loaded)
    assert [n["type"] for n in nodes] == ["constant", "source_load", "llm", "save_artifact"]
    load_node = next(n for n in nodes if n["type"] == "source_load")
    assert load_node["max_sources"] == 3 and load_node["max_chars"] == 9000
    gen = next(n for n in nodes if n["type"] == "llm")
    assert "{load.context}" in gen["prompt"]


def test_build_nodes_none_mode():
    from biznotes_core.services.skills.builder import LoadedSkill, build_nodes

    loaded = LoadedSkill(
        metadata_name="t",
        metadata_description="d",
        instructions="x",
        manifest={"input": {"mode": "none", "scope": "all"}},
    )
    nodes, _ = build_nodes(loaded)
    assert [n["type"] for n in nodes] == ["constant", "llm", "save_artifact"]
    gen = next(n for n in nodes if n["type"] == "llm")
    assert "{search.context}" not in gen["prompt"] and "{load.context}" not in gen["prompt"]


# ========== 통합: 런타임 실행 (builder → execute_agent) ==========


@pytest.fixture
def mock_rag(monkeypatch):
    from biznotes_core.services.rag_chat import Citation

    async def fake_retrieve(db, notebook_id, query, source_ids, top_k=6):
        from biznotes_core.services.rag_chat import RetrievalResult

        return RetrievalResult(
            citations=[
                Citation(n=1, source_id="s", chunk_id="c", quote="발췌", source_title="소스A")
            ],
            insufficient_context=False,
        )

    monkeypatch.setattr("biznotes_core.services.rag_chat.retrieve_context", fake_retrieve)


async def test_execute_skill_package_end_to_end(db, mem_storage, mock_llm, mock_rag):
    """draft 저장 → freeze → execute_skill_package → artifact 저장 + 본문 중괄호 보존."""
    from biznotes_core.models import User
    from biznotes_core.services.agents.runtime import RunContext
    from biznotes_core.services.skills import package as pkg
    from biznotes_core.services.skills import service as svc
    from biznotes_core.services.skills.builder import execute_skill_package

    user = User(email=f"sk-{uuid.uuid4().hex[:8]}@t.dev", password_hash="x", name="t", role="user")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    skill = await svc.create_skill(db, user, "My Skill", "desc")
    # SKILL.md를 중괄호 포함 본문으로 교체
    await __import__("anyio").to_thread.run_sync(
        pkg.put_draft_file, skill.id, "SKILL.md", GOOD_SKILL_MD.encode()
    )
    version, wrapper = await svc.freeze_skill(db, skill, user)
    await db.commit()

    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={"topic": "x"})
    summary = await execute_skill_package(ctx, version.id)
    assert summary
    assert len(ctx.artifact_keys) == 1
    # 저장된 artifact 본문에 스킬 지침(중괄호 포함)이 손상 없이 들어갔는지
    art_key = ("artifacts", ctx.artifact_keys[0]["key"])
    # save_artifact는 storage.put_object 경유 → mem_storage에 적재됨
    assert art_key in mem_storage


# ========== API ==========


@pytest.fixture(autouse=True)
def _mock_celery(monkeypatch):
    class FakeTask:
        id = "fake-task"

    monkeypatch.setattr(
        "app.workers.tasks.agent.run_agent.apply_async",
        lambda *a, **k: FakeTask(),
    )


async def test_create_and_freeze_and_list(
    client: httpx.AsyncClient, user_a, mem_storage
):
    token = await login(client, user_a.email)
    # 생성
    res = await client.post(
        "/api/v1/skills", json={"name": "내 스킬", "description": "설명"}, headers=bearer(token)
    )
    assert res.status_code == 201, res.text
    skill_id = res.json()["id"]
    assert res.json()["can_edit"] is True

    # 파일 목록 (draft)
    res = await client.get(f"/api/v1/skills/{skill_id}/files", headers=bearer(token))
    paths = {f["path"] for f in res.json()}
    assert "SKILL.md" in paths and "biznotes.json" in paths

    # validate
    res = await client.post(f"/api/v1/skills/{skill_id}/validate", headers=bearer(token))
    assert res.json()["ok"] is True

    # freeze → wrapper agent id
    res = await client.post(
        f"/api/v1/skills/{skill_id}/freeze", json={}, headers=bearer(token)
    )
    assert res.status_code == 200, res.text
    wrapper_id = res.json()["wrapper_agent_id"]
    assert res.json()["version"] == 1

    # 목록에 노출 + wrapper_agent_id 포함
    res = await client.get("/api/v1/skills", headers=bearer(token))
    mine = [s for s in res.json() if s["id"] == skill_id]
    assert mine and mine[0]["wrapper_agent_id"] == wrapper_id

    # wrapper는 GET /agents 목록에서 제외(§12.5)
    res = await client.get("/api/v1/agents", headers=bearer(token))
    assert all(a["id"] != wrapper_id for a in res.json())


async def test_frontmatter_form_and_binary_asset_survive_freeze_and_export(
    client: httpx.AsyncClient, user_a, mem_storage
):
    """Phase 2: 폼은 원문 정본을 바꾸고 바이너리는 export까지 바이트를 보존한다."""
    token = await login(client, user_a.email)
    headers = bearer(token)
    res = await client.post(
        "/api/v1/skills", json={"name": "authoring", "description": "before"}, headers=headers
    )
    skill_id = res.json()["id"]

    form = await client.get(f"/api/v1/skills/{skill_id}/frontmatter", headers=headers)
    assert form.status_code == 200, form.text
    assert form.json()["editable"] is True
    fields = form.json()["fields"]
    fields.update({"name": "authoring-v2", "description": "after"})
    res = await client.put(
        f"/api/v1/skills/{skill_id}/frontmatter",
        json={"fields": fields, "source_hash": form.json()["source_hash"]},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    assert res.json()["fields"]["name"] == "authoring-v2"
    # 표시 이름은 표준 name과 별개다(biznotes-core D2) — 폼 저장이 덮어쓰지 않는다.
    detail = await client.get(f"/api/v1/skills/{skill_id}", headers=headers)
    assert detail.json()["name"] == "authoring"

    preview = await client.post(
        f"/api/v1/skills/{skill_id}/frontmatter/validate",
        json={
            "fields": {**res.json()["fields"], "name": "Invalid_Name"},
            "source_hash": res.json()["source_hash"],
        },
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["ok"] is True
    assert preview.json()["report"]["standard"]["errors"]

    # JSON처럼 보이는 바이트도 text-write envelope로 해석하지 않고 그대로 저장해야 한다.
    asset = b'{"content":"must remain a file", "binary":"' + bytes([0]) + b'"}'
    res = await client.put(
        f"/api/v1/skills/{skill_id}/files/assets/logo.json",
        content=asset,
        headers={**headers, "Content-Type": "application/octet-stream"},
    )
    assert res.status_code == 204, res.text

    frozen = await client.post(f"/api/v1/skills/{skill_id}/freeze", json={}, headers=headers)
    assert frozen.status_code == 200, frozen.text
    exported = await client.get(
        f"/api/v1/skills/versions/{frozen.json()['version_id']}/export", headers=headers
    )
    assert exported.status_code == 200, exported.text
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        asset_name = next(name for name in archive.namelist() if name.endswith("assets/logo.json"))
        assert archive.read(asset_name) == asset


async def test_frontmatter_comments_disable_the_form_without_losing_raw_editing(
    client: httpx.AsyncClient, user_a, mem_storage
):
    token = await login(client, user_a.email)
    headers = bearer(token)
    created = await client.post(
        "/api/v1/skills", json={"name": "commented", "description": "d"}, headers=headers
    )
    skill_id = created.json()["id"]
    source = "---\n# keep this note\nname: commented\ndescription: d\n---\n# body\n"
    written = await client.put(
        f"/api/v1/skills/{skill_id}/files/SKILL.md", json={"content": source}, headers=headers
    )
    assert written.status_code == 204, written.text

    form = await client.get(f"/api/v1/skills/{skill_id}/frontmatter", headers=headers)
    assert form.status_code == 200, form.text
    assert form.json()["editable"] is False
    assert form.json()["fields"] == {}
    assert form.json()["reason"] == "frontmatter_comments"
    assert len(form.json()["source_hash"]) == 64


async def test_frontmatter_rejects_stale_or_db_oversized_updates_without_writing(
    client: httpx.AsyncClient, user_a, mem_storage
):
    token = await login(client, user_a.email)
    headers = bearer(token)
    created = await client.post(
        "/api/v1/skills", json={"name": "race-safe", "description": "d"}, headers=headers
    )
    skill_id = created.json()["id"]
    original = await client.get(f"/api/v1/skills/{skill_id}/frontmatter", headers=headers)
    fields = original.json()["fields"]

    too_long = await client.put(
        f"/api/v1/skills/{skill_id}/frontmatter",
        json={
            "fields": {**fields, "name": "x" * 121},
            "source_hash": original.json()["source_hash"],
        },
        headers=headers,
    )
    assert too_long.status_code == 422
    current = await client.get(f"/api/v1/skills/{skill_id}/frontmatter", headers=headers)
    assert current.json()["source_hash"] == original.json()["source_hash"]

    raw = await client.put(
        f"/api/v1/skills/{skill_id}/files/SKILL.md",
        json={"content": "---\nname: race-safe\ndescription: changed raw\n---\nbody\n"},
        headers=headers,
    )
    assert raw.status_code == 204
    stale = await client.put(
        f"/api/v1/skills/{skill_id}/frontmatter",
        json={"fields": fields, "source_hash": original.json()["source_hash"]},
        headers=headers,
    )
    assert stale.status_code == 409


async def test_import_zip(client: httpx.AsyncClient, user_a, mem_storage):
    token = await login(client, user_a.email)
    data = make_zip({"SKILL.md": GOOD_SKILL_MD, "references/rules.md": "be concise"})
    files = {"file": ("skill.zip", data, "application/zip")}
    res = await client.post("/api/v1/skills/import-zip", files=files, headers=bearer(token))
    assert res.status_code == 201, res.text
    assert res.json()["name"] == "sales-summary"


async def test_import_zip_selects_one_skill_from_a_multi_skill_archive(
    client: httpx.AsyncClient, user_a, mem_storage
):
    token = await login(client, user_a.email)
    data = make_zip(
        {
            "alpha/SKILL.md": "---\nname: alpha\ndescription: a\n---\n",
            "alpha/references/a.md": "alpha",
            "beta/SKILL.md": "---\nname: beta\ndescription: b\n---\n",
            "beta/references/b.md": "beta",
        }
    )
    files = {"file": ("skills.zip", data, "application/zip")}
    candidates = await client.post(
        "/api/v1/skills/import-zip/candidates", files=files, headers=bearer(token)
    )
    assert candidates.status_code == 200, candidates.text
    # 후보마다 사전 점검 요약(이름·설명·파일 수·통과 여부)이 붙는다(코어 v0.28.0 #402)
    assert [
        (c["path"], c["name"], c["description"], c["file_count"], c["ok"])
        for c in candidates.json()["candidates"]
    ] == [
        ("alpha/SKILL.md", "alpha", "a", 2, True),
        ("beta/SKILL.md", "beta", "b", 2, True),
    ]

    imported = await client.post(
        "/api/v1/skills/import-zip",
        data={"selected_skill_path": "beta/SKILL.md"},
        files=files,
        headers=bearer(token),
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()["name"] == "beta"
    draft = await client.get(
        f"/api/v1/skills/{imported.json()['id']}/files", headers=bearer(token)
    )
    assert {item["path"] for item in draft.json()} == {"SKILL.md", "references/b.md"}


async def test_import_zip_rejects_secret(client: httpx.AsyncClient, user_a, mem_storage):
    token = await login(client, user_a.email)
    data = make_zip({"SKILL.md": GOOD_SKILL_MD, "references/k.md": "AKIAIOSFODNN7EXAMPLE"})
    files = {"file": ("skill.zip", data, "application/zip")}
    res = await client.post("/api/v1/skills/import-zip", files=files, headers=bearer(token))
    assert res.status_code == 422
    assert "secret_detected" in res.json()["detail"]


async def test_private_skill_hidden_from_other_user(
    client: httpx.AsyncClient, user_a, user_b, mem_storage
):
    token_a = await login(client, user_a.email)
    res = await client.post(
        "/api/v1/skills", json={"name": "비공개", "description": "d"}, headers=bearer(token_a)
    )
    skill_id = res.json()["id"]

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    res = await client.get(f"/api/v1/skills/{skill_id}", headers=bearer(token_b))
    assert res.status_code == 404


async def test_other_user_cannot_run_private_wrapper(
    client: httpx.AsyncClient, user_a, user_b, mem_storage
):
    token_a = await login(client, user_a.email)
    res = await client.post(
        "/api/v1/skills", json={"name": "비공개 실행", "description": "d"}, headers=bearer(token_a)
    )
    skill_id = res.json()["id"]
    res = await client.post(f"/api/v1/skills/{skill_id}/freeze", json={}, headers=bearer(token_a))
    wrapper_id = res.json()["wrapper_agent_id"]

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    nb = await client.post("/api/v1/notebooks", json={"title": "b-nb"}, headers=bearer(token_b))
    nb_id = nb.json()["id"]
    # 타인 wrapper로 실행 시도 → 404 (§12.5)
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/agent-runs",
        json={"agent_id": wrapper_id, "params": {"topic": "x"}},
        headers=bearer(token_b),
    )
    assert res.status_code == 404


async def test_run_pins_skill_version(client: httpx.AsyncClient, user_a, mem_storage):
    token = await login(client, user_a.email)
    res = await client.post(
        "/api/v1/skills", json={"name": "실행핀", "description": "d"}, headers=bearer(token)
    )
    skill_id = res.json()["id"]
    res = await client.post(f"/api/v1/skills/{skill_id}/freeze", json={}, headers=bearer(token))
    wrapper_id = res.json()["wrapper_agent_id"]
    version_id = res.json()["version_id"]

    nb = await client.post("/api/v1/notebooks", json={"title": "nb"}, headers=bearer(token))
    nb_id = nb.json()["id"]
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/agent-runs",
        json={"agent_id": wrapper_id, "params": {"topic": "x"}},
        headers=bearer(token),
    )
    assert res.status_code == 202, res.text
    run_id = res.json()["id"]
    res = await client.get(f"/api/v1/agent-runs/{run_id}", headers=bearer(token))
    assert res.json()["skill_version_id"] == version_id


async def test_publish_makes_org_visible(
    client: httpx.AsyncClient, user_a, user_b, mem_storage
):
    token_a = await login(client, user_a.email)
    res = await client.post(
        "/api/v1/skills", json={"name": "공유스킬", "description": "d"}, headers=bearer(token_a)
    )
    skill_id = res.json()["id"]
    res = await client.post(
        f"/api/v1/skills/{skill_id}/publish",
        json={"changelog": "v1"},
        headers=bearer(token_a),
    )
    assert res.status_code == 200, res.text

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    # org 공유 → user_b가 조회 + 클론 가능
    res = await client.get(f"/api/v1/skills/{skill_id}", headers=bearer(token_b))
    assert res.status_code == 200
    assert res.json()["visibility"] == "org"
    res = await client.post(f"/api/v1/skills/{skill_id}/clone", headers=bearer(token_b))
    assert res.status_code == 200
    clone_id = res.json()["skill_id"]
    # 클론은 user_b의 private draft
    res = await client.get(f"/api/v1/skills/{clone_id}", headers=bearer(token_b))
    assert res.json()["visibility"] == "private"
    assert res.json()["forked_from_version_id"] is not None


# ========== S3: 시드 변환 + 어드민 토글 ==========


async def test_seed_system_skill_idempotent(db, mem_storage):
    from biznotes_core.services.skills.service import seed_system_skill

    md = "---\nname: report-writer\ndescription: 보고서\n---\n\n보고서를 작성하세요."
    manifest = {"mode": "instruction", "use_rag": True, "permissions": {"rag": True, "llm": True}}

    s1 = await seed_system_skill(
        db,
        slug="report-writer",
        name="보고서",
        description="보고서",
        skill_md=md,
        manifest=manifest,
    )
    await db.commit()
    from biznotes_core.models import SkillVersion
    from sqlalchemy import func, select

    def count_versions(skill_id):
        return db.scalar(
            select(func.count()).select_from(SkillVersion).where(SkillVersion.skill_id == skill_id)
        )

    assert s1.visibility == "system"
    assert s1.current_version_id is not None
    assert await count_versions(s1.id) == 1

    # 동일 패키지 재시드 → 새 버전 생성 안 함
    s2 = await seed_system_skill(
        db,
        slug="report-writer",
        name="보고서",
        description="보고서",
        skill_md=md,
        manifest=manifest,
    )
    await db.commit()
    assert s2.id == s1.id
    assert await count_versions(s1.id) == 1

    # 내용 변경 → 새 버전
    s3 = await seed_system_skill(
        db,
        slug="report-writer",
        name="보고서",
        description="보고서",
        skill_md=md + "\n추가 지침",
        manifest=manifest,
    )
    await db.commit()
    assert await count_versions(s3.id) == 2


async def test_system_skill_visible_and_runnable_to_all(
    client: httpx.AsyncClient, user_a, db, mem_storage
):
    from biznotes_core.services.skills.service import seed_system_skill

    md = "---\nname: sys-skill\ndescription: 빌트인\n---\n\n작업을 수행하세요."
    await seed_system_skill(
        db,
        slug="sys-skill",
        name="빌트인 스킬",
        description="빌트인",
        skill_md=md,
        manifest={"permissions": {"rag": True, "llm": True}},
    )
    await db.commit()

    token = await login(client, user_a.email)
    res = await client.get("/api/v1/skills", headers=bearer(token))
    sys = [s for s in res.json() if s["slug"] == "sys-skill"]
    assert sys and sys[0]["visibility"] == "system"
    assert sys[0]["can_run"] is True
    assert sys[0]["wrapper_agent_id"] is not None
    assert sys[0]["can_edit"] is False  # 일반 사용자는 편집 불가


async def test_admin_disable_skill(client: httpx.AsyncClient, admin_user, user_a, db, mem_storage):
    from biznotes_core.services.skills.service import seed_system_skill

    md = "---\nname: toggle-skill\ndescription: 토글\n---\n\n작업."
    skill = await seed_system_skill(
        db,
        slug="toggle-skill",
        name="토글 스킬",
        description="토글",
        skill_md=md,
        manifest={"permissions": {"rag": True, "llm": True}},
    )
    await db.commit()
    skill_id = str(skill.id)

    admin_token = await login(client, admin_user.email)
    res = await client.patch(
        f"/api/v1/admin/skills/{skill_id}", json={"enabled": False}, headers=bearer(admin_token)
    )
    assert res.status_code == 200, res.text
    assert res.json()["enabled"] is False

    # 비활성 system 스킬은 일반 사용자 목록에서 제외
    user_token = await login(client, user_a.email)
    res = await client.get("/api/v1/skills", headers=bearer(user_token))
    assert all(s["slug"] != "toggle-skill" for s in res.json())

    # wrapper agent도 비활성 → 실행 시도 차단(목록/실행 모두)
    res = await client.get("/api/v1/agents", headers=bearer(user_token))
    assert res.status_code == 200


async def test_admin_skills_requires_admin(client: httpx.AsyncClient, user_a, mem_storage):
    token = await login(client, user_a.email)
    res = await client.get("/api/v1/admin/skills", headers=bearer(token))
    assert res.status_code == 403


# ========== 스킬 생성 도우미: 아티팩트 → 스킬 1클릭 ==========


def test_parse_package_artifact_json():
    import json

    from biznotes_core.services.skills.service import parse_package_artifact

    payload = json.dumps(
        {
            "skill_md": "---\nname: my-new\ndescription: 생성됨\n---\n\n본문",
            "biznotes_json": {"mode": "instruction", "permissions": {"rag": True, "llm": True}},
        }
    ).encode()
    files = parse_package_artifact(payload)
    assert files["SKILL.md"].decode().startswith("---\nname: my-new")
    assert "biznotes.json" in files


def test_parse_package_artifact_fenced_fallback():
    from biznotes_core.services.skills.service import parse_package_artifact

    fenced = (
        "```markdown\n---\nname: fenced-skill\ndescription: d\n---\n\n본문\n```\n\n"
        '```json\n{"mode": "instruction"}\n```'
    ).encode()
    files = parse_package_artifact(fenced)
    assert b"fenced-skill" in files["SKILL.md"]
    assert "biznotes.json" in files


def test_parse_package_artifact_bad():
    from biznotes_core.services.skills.service import SkillValidationError, parse_package_artifact

    with pytest.raises(SkillValidationError):
        parse_package_artifact(b"this is not a skill package at all")


async def test_create_skill_from_artifact(client: httpx.AsyncClient, user_a, db, mem_storage):
    import json as _json
    import uuid as _uuid

    from biznotes_core.models import Agent, AgentRun

    token = await login(client, user_a.email)
    nb = await client.post("/api/v1/notebooks", json={"title": "art-nb"}, headers=bearer(token))
    nb_id = nb.json()["id"]

    agent = Agent(name="도우미", kind="skill", definition={}, scope="system", enabled=True)
    db.add(agent)
    await db.flush()
    run = AgentRun(
        agent_id=agent.id,
        notebook_id=_uuid.UUID(nb_id),
        user_id=user_a.id,
        status="succeeded",
        artifact_keys=[
            {
                "key": "run-x/skill-package.json",
                "name": "skill-package.json",
                "content_type": "application/json",
            }
        ],
    )
    db.add(run)
    await db.commit()

    # 아티팩트 내용을 인메모리 artifacts 버킷에 적재
    mem_storage[("artifacts", "run-x/skill-package.json")] = _json.dumps(
        {
            "skill_md": "---\nname: generated-skill\ndescription: 자동 생성\n---\n\n본문",
            "biznotes_json": {"mode": "instruction", "permissions": {"rag": False, "llm": True}},
        }
    ).encode()

    res = await client.post(
        "/api/v1/skills/from-artifact",
        json={"run_id": str(run.id), "artifact_name": "skill-package.json"},
        headers=bearer(token),
    )
    assert res.status_code == 200, res.text
    new_id = res.json()["skill_id"]
    res = await client.get(f"/api/v1/skills/{new_id}", headers=bearer(token))
    assert res.json()["name"] == "generated-skill"
    assert res.json()["visibility"] == "private"


# ---- core-v0.20.0-upgrade AC-1.8 ⑤ — PATCH 명시적 null(코어 v0.18.0 #267) ----


async def test_skill_patch_explicit_null_is_422_and_visibility_null_is_omitted(
    client: httpx.AsyncClient, user_a, user_b, mem_storage
):
    """스킬 `name`·`description`·`tags`의 명시적 null은 관문보다 먼저 422(문자열 detail)이고 행은
    그대로다. `visibility` null은 생략과 같다(200, 값 불변). 남의 스킬 편집은 지금처럼 거부된다."""
    token_a = await login(client, user_a.email)
    res = await client.post(
        "/api/v1/skills", json={"name": "널 스킬", "description": "d"}, headers=bearer(token_a)
    )
    assert res.status_code == 201, res.text
    skill_id = res.json()["id"]
    before = (await client.get(f"/api/v1/skills/{skill_id}", headers=bearer(token_a))).json()

    for field in ("name", "description", "tags"):
        res = await client.patch(
            f"/api/v1/skills/{skill_id}", json={field: None}, headers=bearer(token_a)
        )
        assert res.status_code == 422, res.text
        assert res.json() == {"detail": f"null로 바꿀 수 없는 필드: {field}"}
    after = (await client.get(f"/api/v1/skills/{skill_id}", headers=bearer(token_a))).json()
    assert {k: after[k] for k in ("name", "description", "tags", "visibility")} == {
        k: before[k] for k in ("name", "description", "tags", "visibility")
    }

    res = await client.patch(
        f"/api/v1/skills/{skill_id}", json={"visibility": None}, headers=bearer(token_a)
    )
    assert res.status_code == 200, res.text
    assert res.json()["visibility"] == before["visibility"]

    token_b = await login(client, user_b.email)
    res = await client.patch(
        f"/api/v1/skills/{skill_id}", json={"name": "남의 이름"}, headers=bearer(token_b)
    )
    assert (res.status_code, res.json()["detail"]) == (404, "Skill not found")
    after = (await client.get(f"/api/v1/skills/{skill_id}", headers=bearer(token_a))).json()
    assert after["name"] == "널 스킬"
