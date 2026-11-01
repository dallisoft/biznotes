"""스킬 표면 권한 표(계획 §4.5) — 제품 앱에 조립된 코어 스킬 라우터의 소유자·비소유자·admin 관점.

§4.5의 행 중 스킬 쓰기 경로(`PATCH/DELETE /skills/{id}`, 파일 쓰기·삭제, frontmatter, freeze,
version publish, 검토 요청)와 관리자 경로(`/admin/skills/**`)를 엔드포인트마다 비인가 케이스 1건
이상으로 고정한다(AGENTS.md 「보안·데이터」). 전부 제품 FastAPI 앱 실경로이며, 대체하는 것은
스토리지 경계(`mem_storage`)뿐이다.
"""

import uuid

import httpx
import pytest

from tests.conftest import bearer, login
from tests.test_skills import make_zip

_HASH = "0" * 64  # 형식은 맞지만 어떤 draft와도 맞지 않는 source_hash
SKILL_MD = "---\nname: surface-skill\ndescription: 권한 표 검증용\n---\n\n# 본문\n"


async def _owner_skill(client: httpx.AsyncClient, token: str) -> tuple[str, str]:
    created = await client.post(
        "/api/v1/skills", json={"name": "권한표", "description": "d"}, headers=bearer(token)
    )
    assert created.status_code == 201, created.text
    skill_id = created.json()["id"]
    frozen = await client.post(
        f"/api/v1/skills/{skill_id}/freeze", json={"changelog": "v1"}, headers=bearer(token)
    )
    assert frozen.status_code == 200, frozen.text
    return skill_id, frozen.json()["version_id"]


async def _imported_skill(client: httpx.AsyncClient, token: str) -> tuple[str, str]:
    res = await client.post(
        "/api/v1/skills/import-zip",
        files={
            "file": ("p.zip", make_zip({"surface-skill/SKILL.md": SKILL_MD}), "application/zip")
        },
        headers=bearer(token),
    )
    assert res.status_code == 201, res.text
    skill_id = res.json()["id"]
    frozen = await client.post(
        f"/api/v1/skills/{skill_id}/freeze", json={"changelog": "v1"}, headers=bearer(token)
    )
    assert frozen.status_code == 200, frozen.text
    return skill_id, frozen.json()["version_id"]


def _write_calls(skill_id: str, version_id: str) -> list[tuple[str, str, dict]]:
    base = f"/api/v1/skills/{skill_id}"
    frontmatter = {"json": {"fields": {}, "source_hash": _HASH}}
    return [
        ("patch", base, {"json": {"name": "탈취"}}),
        ("delete", base, {}),
        ("put", f"{base}/files/references/x.md", {"json": {"content": "x"}}),
        ("delete", f"{base}/files/references/x.md", {}),
        ("put", f"{base}/frontmatter", frontmatter),
        ("post", f"{base}/frontmatter/validate", frontmatter),
        ("post", f"{base}/validate", {}),
        ("post", f"{base}/freeze", {"json": {}}),
        ("post", f"{base}/versions/{version_id}/publish", {}),
        ("post", f"{base}/versions/{version_id}/review-requests", {"json": {"kind": "expose_org"}}),
        ("get", f"{base}/review-requests", {}),
    ]


async def test_non_owner_cannot_use_any_skill_write_endpoint(
    client: httpx.AsyncClient, user_a, user_b, mem_storage
):
    """§4.5 스킬 쓰기 경로(PATCH/DELETE·파일·frontmatter·freeze·publish·검토 요청) — 비관련 404."""
    token_a = await login(client, user_a.email)
    skill_id, version_id = await _owner_skill(client, token_a)

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    for method, path, kwargs in _write_calls(skill_id, version_id):
        res = await getattr(client, method)(path, headers=bearer(token_b), **kwargs)
        assert res.status_code in (403, 404), (method, path, res.status_code, res.text)

    client.cookies.clear()
    token_a = await login(client, user_a.email)
    still = await client.get(f"/api/v1/skills/{skill_id}", headers=bearer(token_a))
    assert still.status_code == 200
    assert still.json()["name"] == "권한표"  # 비소유자 PATCH가 적용되지 않았다


async def test_owner_write_lifecycle_through_the_product_app(
    client: httpx.AsyncClient, user_a, mem_storage
):
    """소유자 경로 양성 대조 — 파일·frontmatter·검증·재동결·메타 수정·삭제."""
    token = await login(client, user_a.email)
    skill_id, _version_id = await _owner_skill(client, token)

    put = await client.put(
        f"/api/v1/skills/{skill_id}/files/references/rules.md",
        json={"content": "# 규칙"},
        headers=bearer(token),
    )
    assert put.status_code == 204, put.text
    read = await client.get(
        f"/api/v1/skills/{skill_id}/files/references/rules.md", headers=bearer(token)
    )
    assert read.status_code == 200 and "규칙" in read.text
    traversal = await client.put(
        f"/api/v1/skills/{skill_id}/files/..%2Fescape.md",
        json={"content": "x"},
        headers=bearer(token),
    )
    assert traversal.status_code in (404, 422)
    no_delete_skill_md = await client.delete(
        f"/api/v1/skills/{skill_id}/files/SKILL.md", headers=bearer(token)
    )
    assert no_delete_skill_md.status_code == 422

    fm = await client.get(f"/api/v1/skills/{skill_id}/frontmatter", headers=bearer(token))
    assert fm.status_code == 200, fm.text
    fields = dict(fm.json()["fields"])
    fields["description"] = "고친 설명"
    checked = await client.post(
        f"/api/v1/skills/{skill_id}/frontmatter/validate",
        json={"fields": fields, "source_hash": fm.json()["source_hash"]},
        headers=bearer(token),
    )
    assert checked.status_code == 200, checked.text
    written = await client.put(
        f"/api/v1/skills/{skill_id}/frontmatter",
        json={"fields": fields, "source_hash": fm.json()["source_hash"]},
        headers=bearer(token),
    )
    assert written.status_code == 200, written.text
    stale = await client.put(
        f"/api/v1/skills/{skill_id}/frontmatter",
        json={"fields": fields, "source_hash": fm.json()["source_hash"]},
        headers=bearer(token),
    )
    assert stale.status_code == 409  # 낡은 source_hash는 덮어쓰지 않는다

    validated = await client.post(f"/api/v1/skills/{skill_id}/validate", headers=bearer(token))
    assert validated.status_code == 200 and validated.json()["ok"] is True
    refrozen = await client.post(
        f"/api/v1/skills/{skill_id}/freeze", json={"changelog": "v2"}, headers=bearer(token)
    )
    assert refrozen.json()["version"] == 2
    versions = await client.get(f"/api/v1/skills/{skill_id}/versions", headers=bearer(token))
    assert [v["version"] for v in versions.json()] == [2, 1]
    meta = await client.get(
        f"/api/v1/skills/versions/{refrozen.json()['version_id']}/metadata", headers=bearer(token)
    )
    assert meta.status_code == 200, meta.text

    renamed = await client.patch(
        f"/api/v1/skills/{skill_id}", json={"name": "새 이름"}, headers=bearer(token)
    )
    assert renamed.status_code == 200 and renamed.json()["name"] == "새 이름"
    deleted = await client.delete(f"/api/v1/skills/{skill_id}", headers=bearer(token))
    assert deleted.status_code == 204
    gone = await client.get(f"/api/v1/skills/{skill_id}", headers=bearer(token))
    assert gone.status_code == 404


async def test_clone_requires_visibility(
    client: httpx.AsyncClient, user_a, user_b, mem_storage
):
    """복제 — 비공개 스킬은 비소유자에게 404, 공개(org)되면 비소유자가 자기 draft로 복제한다."""
    token_a = await login(client, user_a.email)
    skill_id, version_id = await _owner_skill(client, token_a)

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    hidden = await client.post(f"/api/v1/skills/{skill_id}/clone", headers=bearer(token_b))
    assert hidden.status_code == 404

    client.cookies.clear()
    token_a = await login(client, user_a.email)
    published = await client.post(
        f"/api/v1/skills/{skill_id}/versions/{version_id}/publish", headers=bearer(token_a)
    )
    assert published.status_code == 200, published.text

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    cloned = await client.post(f"/api/v1/skills/{skill_id}/clone", headers=bearer(token_b))
    assert cloned.status_code == 200, cloned.text
    mine = await client.get(f"/api/v1/skills/{cloned.json()['skill_id']}", headers=bearer(token_b))
    assert mine.status_code == 200
    assert mine.json()["owner_id"] == str(user_b.id)
    assert mine.json()["visibility"] == "private"


async def test_owner_visibility_change_on_imported_skill_waits_for_review(
    client: httpx.AsyncClient, user_a, mem_storage
):
    """`PATCH /skills/{id}` visibility(D8 경로 2) — 가져온 스킬의 org 공개는 409 review_pending."""
    token = await login(client, user_a.email)
    skill_id, _version_id = await _imported_skill(client, token)

    res = await client.patch(
        f"/api/v1/skills/{skill_id}",
        json={"visibility": "org", "name": "같이 바꾸려던 이름"},
        headers=bearer(token),
    )

    assert res.status_code == 409, res.text
    assert res.json()["reason"] == "review_pending"
    detail = await client.get(f"/api/v1/skills/{skill_id}", headers=bearer(token))
    assert detail.json()["visibility"] == "private"
    # 원자적 거부 — 같은 요청의 다른 필드도 적용하지 않는다
    assert detail.json()["name"] != "같이 바꾸려던 이름"
    reviews = await client.get(f"/api/v1/skills/{skill_id}/review-requests", headers=bearer(token))
    assert [r["status"] for r in reviews.json()] == ["pending"]


def _admin_calls(skill_id: str, version_id: str, request_id: str) -> list[tuple[str, str, dict]]:
    admin = "/api/v1/admin/skills"
    review = f"{admin}/review-requests/{request_id}"
    return [
        ("get", admin, {}),
        ("patch", f"{admin}/{skill_id}", {"json": {"enabled": False}}),
        ("post", f"{admin}/{skill_id}/published-version", {"json": {"version_id": None}}),
        ("get", f"{admin}/review-requests", {}),
        ("post", f"{review}/approve", {}),
        ("post", f"{review}/reject", {"json": {"note": "x"}}),
        ("post", f"{review}/withdraw", {}),
        ("get", f"{admin}/versions/{version_id}/files", {}),
        ("get", f"{admin}/versions/{version_id}/files/SKILL.md", {}),
    ]


async def test_admin_skill_endpoints_reject_non_admin_even_the_owner(
    client: httpx.AsyncClient, user_a, mem_storage
):
    """§4.5 `/admin/skills/**` — 일반 사용자(그 스킬의 소유자라도)는 모두 403."""
    token = await login(client, user_a.email)
    skill_id, version_id = await _imported_skill(client, token)
    pending = await client.post(
        f"/api/v1/skills/{skill_id}/versions/{version_id}/publish", headers=bearer(token)
    )
    assert pending.status_code == 202
    request_id = pending.json()["request_id"]

    for method, path, kwargs in _admin_calls(skill_id, version_id, request_id):
        res = await getattr(client, method)(path, headers=bearer(token), **kwargs)
        assert res.status_code == 403, (method, path, res.status_code)


async def test_admin_review_lifecycle_reject_then_approve_then_withdraw(
    client: httpx.AsyncClient, db, user_a, user_b, admin_user, mem_storage
):
    """관리자 검토 — 원문 열람(감사 기록) → 거절(사유) → 새 version 승인(공개) → 공개 철회(보류)."""
    from biznotes_core.models import AuditLog
    from sqlalchemy import select

    token_a = await login(client, user_a.email)
    skill_id, v1 = await _imported_skill(client, token_a)
    first = await client.post(
        f"/api/v1/skills/{skill_id}/versions/{v1}/publish", headers=bearer(token_a)
    )
    assert first.status_code == 202

    client.cookies.clear()
    admin = await login(client, admin_user.email)
    files = await client.get(f"/api/v1/admin/skills/versions/{v1}/files", headers=bearer(admin))
    assert [f["path"] for f in files.json()] == ["SKILL.md"]
    original = await client.get(
        f"/api/v1/admin/skills/versions/{v1}/files/SKILL.md", headers=bearer(admin)
    )
    assert original.status_code == 200 and "권한 표 검증용" in original.text
    missing = await client.get(
        f"/api/v1/admin/skills/versions/{v1}/files/nope.md", headers=bearer(admin)
    )
    assert missing.status_code == 404
    reads = (
        await db.execute(select(AuditLog).where(AuditLog.action == "skill.review.read"))
    ).scalars().all()
    assert {r.detail["path"] for r in reads} >= {"SKILL.md"}

    rejected = await client.post(
        f"/api/v1/admin/skills/review-requests/{first.json()['request_id']}/reject",
        json={"note": "출처 불명"},
        headers=bearer(admin),
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    queue = await client.get(
        "/api/v1/admin/skills/review-requests?status=rejected", headers=bearer(admin)
    )
    assert [r["id"] for r in queue.json()] == [first.json()["request_id"]]

    client.cookies.clear()
    token_a = await login(client, user_a.email)
    v2 = (
        await client.post(
            f"/api/v1/skills/{skill_id}/freeze", json={"changelog": "v2"}, headers=bearer(token_a)
        )
    ).json()["version_id"]
    second = await client.post(
        f"/api/v1/skills/{skill_id}/versions/{v2}/publish", headers=bearer(token_a)
    )
    assert second.status_code == 202

    client.cookies.clear()
    admin = await login(client, admin_user.email)
    approved = await client.post(
        f"/api/v1/admin/skills/review-requests/{second.json()['request_id']}/approve",
        headers=bearer(admin),
    )
    assert approved.status_code == 200, approved.text

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    visible = await client.get(f"/api/v1/skills/{skill_id}", headers=bearer(token_b))
    assert visible.status_code == 200
    assert visible.json()["published_version_id"] == v2

    client.cookies.clear()
    admin = await login(client, admin_user.email)
    # 승인 철회(withdraw)는 스크립트 허가 전용이다 — 공개 승인은 공개 version 철회(경로 4)로
    not_scripts = await client.post(
        f"/api/v1/admin/skills/review-requests/{second.json()['request_id']}/withdraw",
        headers=bearer(admin),
    )
    assert not_scripts.status_code == 409, not_scripts.text
    assert not_scripts.json()["detail"].startswith("[not_scripts] ")
    revoked = await client.post(
        f"/api/v1/admin/skills/{skill_id}/published-version",
        json={"version_id": None},
        headers=bearer(admin),
    )
    assert revoked.status_code == 200, revoked.text
    listed = await client.get("/api/v1/admin/skills", headers=bearer(admin))
    row = next(s for s in listed.json() if s["id"] == skill_id)
    assert row["exposure_hold"] is True

    # 철회 뒤에도 org 스킬은 목록·상세에 남지만 내용은 노출 version에서만 나온다(코어
    # `exposure.can_view` — 공개 version이 없으면 version 목록이 비고 파일·내보내기는 404)
    client.cookies.clear()
    token_b = await login(client, user_b.email)
    after = await client.get(f"/api/v1/skills/{skill_id}", headers=bearer(token_b))
    assert after.status_code == 200
    assert after.json()["published_version_id"] is None
    assert after.json()["can_run"] is False
    versions = await client.get(f"/api/v1/skills/{skill_id}/versions", headers=bearer(token_b))
    assert versions.json() == []
    export = await client.get(f"/api/v1/skills/versions/{v2}/export", headers=bearer(token_b))
    assert export.status_code == 404


async def test_admin_promotes_to_system_and_revokes_published_version(
    client: httpx.AsyncClient, user_a, user_b, admin_user, mem_storage
):
    """관리자 승격(D8 경로 3)·공개 version 지정/철회(경로 4)·비활성화."""
    token_a = await login(client, user_a.email)
    skill_id, version_id = await _owner_skill(client, token_a)

    client.cookies.clear()
    admin = await login(client, admin_user.email)
    missing_version = await client.patch(
        f"/api/v1/admin/skills/{skill_id}", json={"visibility": "system"}, headers=bearer(admin)
    )
    assert missing_version.status_code in (409, 422)  # 승격에는 version_id가 필요하다
    promoted = await client.patch(
        f"/api/v1/admin/skills/{skill_id}",
        json={"visibility": "system", "version_id": version_id},
        headers=bearer(admin),
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["visibility"] == "system"
    assert promoted.json()["published_version_id"] == version_id

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    shared = await client.get(f"/api/v1/skills/{skill_id}", headers=bearer(token_b))
    assert shared.status_code == 200

    client.cookies.clear()
    admin = await login(client, admin_user.email)
    revoked = await client.post(
        f"/api/v1/admin/skills/{skill_id}/published-version",
        json={"version_id": None},
        headers=bearer(admin),
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["published_version_id"] is None
    assert revoked.json()["exposure_hold"] is True
    restored = await client.post(
        f"/api/v1/admin/skills/{skill_id}/published-version",
        json={"version_id": version_id},
        headers=bearer(admin),
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["published_version_id"] == version_id
    disabled = await client.patch(
        f"/api/v1/admin/skills/{skill_id}", json={"enabled": False}, headers=bearer(admin)
    )
    assert disabled.status_code == 200 and disabled.json()["enabled"] is False
    unknown = await client.patch(
        f"/api/v1/admin/skills/{uuid.uuid4()}", json={"enabled": False}, headers=bearer(admin)
    )
    assert unknown.status_code == 404


@pytest.mark.parametrize("kind", ["expose_org", "scripts"])
async def test_owner_review_request_is_listed_only_to_owner(
    client: httpx.AsyncClient, user_a, user_b, mem_storage, kind
):
    """`POST/GET /skills/{id}/(versions/{vid}/)review-requests` — 소유자만 만들고 본다."""
    token_a = await login(client, user_a.email)
    skill_id, version_id = await _owner_skill(client, token_a)
    created = await client.post(
        f"/api/v1/skills/{skill_id}/versions/{version_id}/review-requests",
        json={"kind": kind},
        headers=bearer(token_a),
    )
    assert created.status_code == 201, created.text
    assert created.json()["kind"] == kind
    assert created.json()["status"] == "pending"
    assert created.json()["requested_by"] == str(user_a.id)
    listed = await client.get(f"/api/v1/skills/{skill_id}/review-requests", headers=bearer(token_a))
    assert listed.status_code == 200
    assert [r["id"] for r in listed.json()] == [created.json()["id"]]

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    other = await client.get(f"/api/v1/skills/{skill_id}/review-requests", headers=bearer(token_b))
    assert other.status_code == 404


# ---------- 노출 모델의 강등·철회 규칙 (코어 exposure D8 경로 2·3·4, E-02) ----------


async def test_owner_unexposes_and_cannot_touch_system_tier(
    client: httpx.AsyncClient, user_a, user_b, admin_user, mem_storage
):
    """소유자 경로 2 — org → private 내리기, system 승격·강등은 관리자 전용."""
    token_a = await login(client, user_a.email)
    skill_id, version_id = await _owner_skill(client, token_a)
    await client.post(
        f"/api/v1/skills/{skill_id}/versions/{version_id}/publish", headers=bearer(token_a)
    )
    schema = await client.patch(
        f"/api/v1/skills/{skill_id}", json={"visibility": "system"}, headers=bearer(token_a)
    )
    assert schema.status_code == 422  # 소유자 스키마는 private|org만 받는다

    down = await client.patch(
        f"/api/v1/skills/{skill_id}", json={"visibility": "private"}, headers=bearer(token_a)
    )
    assert down.status_code == 200, down.text
    assert down.json()["visibility"] == "private"
    client.cookies.clear()
    token_b = await login(client, user_b.email)
    gone = await client.get(f"/api/v1/skills/{skill_id}", headers=bearer(token_b))
    assert gone.status_code == 404

    # 관리자가 system으로 올린 스킬을 소유자가 org로 내리는 것은 관리자 경로 전용(403 admin_only)
    client.cookies.clear()
    admin = await login(client, admin_user.email)
    await client.patch(
        f"/api/v1/admin/skills/{skill_id}",
        json={"visibility": "system", "version_id": version_id},
        headers=bearer(admin),
    )
    client.cookies.clear()
    token_a = await login(client, user_a.email)
    forbidden = await client.patch(
        f"/api/v1/skills/{skill_id}", json={"visibility": "org"}, headers=bearer(token_a)
    )
    assert forbidden.status_code == 403, forbidden.text
    assert forbidden.json()["detail"].startswith("[admin_only] ")


async def test_owner_private_demotion_ends_admin_system_approval(
    client: httpx.AsyncClient, user_a, admin_user, mem_storage
):
    """E-02 — 관리자가 준 system 승격은 소유자가 private로 내리는 순간 끝난다.

    남아 있으면 `system → private → org` 2단계로 관리자 경로를 우회해 재공개된다.
    """
    token_a = await login(client, user_a.email)
    skill_id, version_id = await _owner_skill(client, token_a)
    client.cookies.clear()
    admin = await login(client, admin_user.email)
    promoted = await client.patch(
        f"/api/v1/admin/skills/{skill_id}",
        json={"visibility": "system", "version_id": version_id},
        headers=bearer(admin),
    )
    assert promoted.json()["visibility"] == "system"

    client.cookies.clear()
    token_a = await login(client, user_a.email)
    down = await client.patch(
        f"/api/v1/skills/{skill_id}", json={"visibility": "private"}, headers=bearer(token_a)
    )
    assert down.status_code == 200, down.text
    up = await client.patch(
        f"/api/v1/skills/{skill_id}", json={"visibility": "org"}, headers=bearer(token_a)
    )
    assert up.status_code == 200, up.text
    assert up.json()["visibility"] == "org"  # 다시 올라가도 system으로 되살아나지 않는다

    client.cookies.clear()
    admin = await login(client, admin_user.email)
    approved = await client.get(
        "/api/v1/admin/skills/review-requests?status=approved", headers=bearer(admin)
    )
    assert all(
        not (r["version_id"] == version_id and r["kind"] == "expose_system")
        for r in approved.json()
    )


async def test_admin_demotions_system_to_org_and_to_private_hold(
    client: httpx.AsyncClient, user_a, user_b, admin_user, mem_storage
):
    """관리자 경로 3 강등 — system → org(승격 승인 철회), → private(보류 + 노출 승인 철회)."""
    token_a = await login(client, user_a.email)
    skill_id, version_id = await _owner_skill(client, token_a)
    client.cookies.clear()
    admin = await login(client, admin_user.email)
    await client.patch(
        f"/api/v1/admin/skills/{skill_id}",
        json={"visibility": "system", "version_id": version_id},
        headers=bearer(admin),
    )

    to_org = await client.patch(
        f"/api/v1/admin/skills/{skill_id}", json={"visibility": "org"}, headers=bearer(admin)
    )
    assert to_org.status_code == 200, to_org.text
    assert to_org.json()["visibility"] == "org"
    same = await client.patch(
        f"/api/v1/admin/skills/{skill_id}", json={"visibility": "org"}, headers=bearer(admin)
    )
    assert same.status_code == 200  # 같은 값은 변화 없음

    to_private = await client.patch(
        f"/api/v1/admin/skills/{skill_id}", json={"visibility": "private"}, headers=bearer(admin)
    )
    assert to_private.status_code == 200, to_private.text
    assert to_private.json()["visibility"] == "private"
    assert to_private.json()["exposure_hold"] is True

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    hidden = await client.get(f"/api/v1/skills/{skill_id}", headers=bearer(token_b))
    assert hidden.status_code == 404


async def test_seed_skill_cannot_be_exposed_to_org(
    client: httpx.AsyncClient, db, admin_user, mem_storage
):
    """시드 스킬(system_seed)은 system 또는 private로만 둔다 — org 강등은 거부된다."""
    from biznotes_core.models import Skill
    from sqlalchemy import select

    from scripts.seed import upsert_skill_creator

    await upsert_skill_creator()
    seed = (await db.execute(select(Skill).where(Skill.slug == "skill-creator"))).scalar_one()
    assert seed.origin == "system_seed"

    admin = await login(client, admin_user.email)
    res = await client.patch(
        f"/api/v1/admin/skills/{seed.id}", json={"visibility": "org"}, headers=bearer(admin)
    )
    assert res.status_code == 409, res.text
    assert res.json()["detail"].startswith("[seed_org_forbidden] ")


async def test_scripts_approval_can_be_withdrawn_once(
    client: httpx.AsyncClient, user_a, admin_user, mem_storage
):
    """스크립트 실행 허가의 승인 철회(R4-12) — approved만 철회되고, 두 번째 철회는 409."""
    token_a = await login(client, user_a.email)
    skill_id, version_id = await _owner_skill(client, token_a)
    created = await client.post(
        f"/api/v1/skills/{skill_id}/versions/{version_id}/review-requests",
        json={"kind": "scripts"},
        headers=bearer(token_a),
    )
    request_id = created.json()["id"]

    client.cookies.clear()
    admin = await login(client, admin_user.email)
    not_yet = await client.post(
        f"/api/v1/admin/skills/review-requests/{request_id}/withdraw", headers=bearer(admin)
    )
    assert not_yet.status_code == 409
    assert not_yet.json()["detail"].startswith("[not_approved] ")
    approved = await client.post(
        f"/api/v1/admin/skills/review-requests/{request_id}/approve", headers=bearer(admin)
    )
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    withdrawn = await client.post(
        f"/api/v1/admin/skills/review-requests/{request_id}/withdraw", headers=bearer(admin)
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["status"] == "withdrawn"
    again = await client.post(
        f"/api/v1/admin/skills/review-requests/{request_id}/withdraw", headers=bearer(admin)
    )
    assert again.status_code == 409
    unknown = await client.post(
        f"/api/v1/admin/skills/review-requests/{uuid.uuid4()}/withdraw", headers=bearer(admin)
    )
    assert unknown.status_code == 404
