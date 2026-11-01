"""스킬 파일·패키지 경계의 방어 — 제품 앱에 조립된 코어 스킬 라우터(계획 §4.5 스킬 행 보강).

- 능동 콘텐츠(svg·html)는 어떤 경로로도 그 타입 inline으로 내리지 않는다(저장형 XSS 방어)
- 내보내기 파일명은 헤더 분할(CR/LF) 없이 안전하게 만든다
- 업로드 크기 상한(ZIP·draft 파일)은 413
- 공개 version이 없는 공유 스킬의 복제는 409, 타인의 실행 산출물로는 스킬을 만들 수 없다(404)

스토리지 경계만 `mem_storage`로 대체한다. 크기 상한은 설정 인스턴스 값만 바꾼다(mock 아님 —
`test_rate_limit.py`의 `active_settings` 선례).
"""

import uuid

import httpx
import pytest

from tests.conftest import bearer, login
from tests.test_skills import make_zip


async def _skill(client: httpx.AsyncClient, token: str, name: str = "파일안전") -> str:
    res = await client.post(
        "/api/v1/skills", json={"name": name, "description": "d"}, headers=bearer(token)
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _put_bytes(client, token, skill_id, path, data, content_type="application/octet-stream"):
    return await client.put(
        f"/api/v1/skills/{skill_id}/files/{path}",
        content=data,
        headers={**bearer(token), "Content-Type": content_type},
    )


@pytest.mark.parametrize(
    ("path", "data", "media_type", "attachment"),
    [
        ("assets/x.svg", b"<svg onload='alert(1)'/>", "text/plain; charset=utf-8", False),
        ("assets/x.html", b"<script>alert(1)</script>", "text/plain; charset=utf-8", False),
        ("assets/x.png", b"\x89PNG\r\n\x1a\n", "image/png", False),
        ("assets/blob.bin", b"\xff\xfe\x00\x81", "application/octet-stream", True),
    ],
)
async def test_draft_file_is_never_served_as_active_content(
    client: httpx.AsyncClient, user_a, mem_storage, path, data, media_type, attachment
):
    token = await login(client, user_a.email)
    skill_id = await _skill(client, token)
    put = await _put_bytes(client, token, skill_id, path, data)
    assert put.status_code == 204, put.text

    res = await client.get(f"/api/v1/skills/{skill_id}/files/{path}", headers=bearer(token))

    assert res.status_code == 200
    assert res.headers["content-type"] == media_type
    assert res.headers["x-content-type-options"] == "nosniff"
    assert ("attachment" in res.headers.get("content-disposition", "")) is attachment
    assert res.content == data


async def test_published_version_files_follow_the_same_rules_for_non_owners(
    client: httpx.AsyncClient, user_a, user_b, mem_storage
):
    token_a = await login(client, user_a.email)
    skill_id = await _skill(client, token_a)
    await _put_bytes(client, token_a, skill_id, "assets/x.svg", b"<svg onload='x'/>")
    version_id = (
        await client.post(f"/api/v1/skills/{skill_id}/freeze", json={}, headers=bearer(token_a))
    ).json()["version_id"]
    await client.post(
        f"/api/v1/skills/{skill_id}/versions/{version_id}/publish", headers=bearer(token_a)
    )

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    listed = await client.get(
        f"/api/v1/skills/versions/{version_id}/files", headers=bearer(token_b)
    )
    # 새 스킬 기본 매니페스트(biznotes.json)도 함께 동결된다
    assert {f["path"] for f in listed.json()} == {"SKILL.md", "biznotes.json", "assets/x.svg"}
    svg = await client.get(
        f"/api/v1/skills/versions/{version_id}/files/assets/x.svg", headers=bearer(token_b)
    )
    assert svg.status_code == 200
    assert svg.headers["content-type"] == "text/plain; charset=utf-8"
    missing = await client.get(
        f"/api/v1/skills/versions/{version_id}/files/nope.md", headers=bearer(token_b)
    )
    assert missing.status_code == 404
    draft_missing = await client.get(
        f"/api/v1/skills/{skill_id}/files/nope.md", headers=bearer(token_a)
    )
    assert draft_missing.status_code in (403, 404)


async def test_export_filename_cannot_split_headers(
    client: httpx.AsyncClient, user_a, mem_storage
):
    """표시 이름의 CR/LF·비ASCII가 Content-Disposition 헤더를 깨거나 새 헤더를 만들지 않는다."""
    token = await login(client, user_a.email)
    skill_id = await _skill(client, token, "보고서\r\nX-Injected: 1")
    version_id = (
        await client.post(f"/api/v1/skills/{skill_id}/freeze", json={}, headers=bearer(token))
    ).json()["version_id"]

    for query in ("", "?layout=flat", "?portable=true"):
        res = await client.get(
            f"/api/v1/skills/versions/{version_id}/export{query}", headers=bearer(token)
        )
        assert res.status_code == 200, (query, res.text)
        disposition = res.headers["content-disposition"]
        assert "\r" not in disposition and "\n" not in disposition
        assert "x-injected" not in {k.lower() for k in res.headers}
        assert disposition.startswith("attachment; filename=")


async def test_upload_limits_return_413(
    client: httpx.AsyncClient, user_a, mem_storage, monkeypatch
):
    from app.core.config import get_settings

    settings = get_settings()
    token = await login(client, user_a.email)
    skill_id = await _skill(client, token)
    archive = make_zip({"SKILL.md": "---\nname: big\ndescription: d\n---\n" + "x" * 4000})
    monkeypatch.setattr(settings, "skill_import_max_upload_bytes", len(archive) - 1)
    monkeypatch.setattr(settings, "skill_import_max_file_bytes", 16)

    for path in ("/api/v1/skills/import-zip", "/api/v1/skills/import-zip/candidates"):
        res = await client.post(
            path, files={"file": ("big.zip", archive, "application/zip")}, headers=bearer(token)
        )
        assert res.status_code == 413, (path, res.text)
    big_file = await _put_bytes(client, token, skill_id, "assets/big.bin", b"\x00" * 64)
    assert big_file.status_code == 413
    bad_json = await client.put(
        f"/api/v1/skills/{skill_id}/files/references/a.md",
        content=b"{not json",
        headers={**bearer(token), "Content-Type": "application/json"},
    )
    assert bad_json.status_code == 422


async def test_clone_of_shared_skill_without_published_version_is_409(
    client: httpx.AsyncClient, user_a, user_b, admin_user, mem_storage
):
    """공유(org)됐지만 공개 version이 철회된 스킬은 보이지만 복제할 내용이 없다 — 409(R2-01)."""
    token_a = await login(client, user_a.email)
    skill_id = await _skill(client, token_a)
    version_id = (
        await client.post(f"/api/v1/skills/{skill_id}/freeze", json={}, headers=bearer(token_a))
    ).json()["version_id"]
    await client.post(
        f"/api/v1/skills/{skill_id}/versions/{version_id}/publish", headers=bearer(token_a)
    )
    client.cookies.clear()
    admin = await login(client, admin_user.email)
    await client.post(
        f"/api/v1/admin/skills/{skill_id}/published-version",
        json={"version_id": None},
        headers=bearer(admin),
    )

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    res = await client.post(f"/api/v1/skills/{skill_id}/clone", headers=bearer(token_b))
    assert res.status_code == 409, res.text


async def test_skill_from_artifact_requires_access_to_the_run(
    client: httpx.AsyncClient, db, user_a, user_b, mem_storage
):
    """`POST /skills/from-artifact` — 타인 run은 404, 없는 run·없는 산출물도 404."""
    from biznotes_core.models import Agent, AgentRun, Notebook

    agent = Agent(
        name=f"creator-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        definition={"nodes": [], "edges": []},
        params_schema={},
        scope="system",
        enabled=True,
    )
    notebook = Notebook(title="artifact-nb", owner_id=user_a.id)
    db.add_all([agent, notebook])
    await db.flush()
    run = AgentRun(
        agent_id=agent.id,
        notebook_id=notebook.id,
        user_id=user_a.id,
        params={},
        status="succeeded",
        artifact_keys=[
            {"key": "k/skill-package.json", "name": "skill-package.json", "content_type": "x"}
        ],
    )
    db.add(run)
    await db.commit()

    token_b = await login(client, user_b.email)
    other = await client.post(
        "/api/v1/skills/from-artifact",
        json={"run_id": str(run.id), "artifact_name": "skill-package.json"},
        headers=bearer(token_b),
    )
    assert other.status_code == 404, other.text

    client.cookies.clear()
    token_a = await login(client, user_a.email)
    no_run = await client.post(
        "/api/v1/skills/from-artifact",
        json={"run_id": str(uuid.uuid4()), "artifact_name": "skill-package.json"},
        headers=bearer(token_a),
    )
    assert no_run.status_code == 404
    no_artifact = await client.post(
        "/api/v1/skills/from-artifact",
        json={"run_id": str(run.id), "artifact_name": "nope.json"},
        headers=bearer(token_a),
    )
    assert no_artifact.status_code == 404
