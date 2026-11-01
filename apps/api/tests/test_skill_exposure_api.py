"""스킬 노출 모델 API 조립 — biznotes-core 표준 계획 Phase 1(D8·D12).

라우터·스키마는 코어가 소유하고 규칙 자체는 코어 테스트가 고정한다. 여기서는 **제품 앱에
조립된 상태**(인증·관리자 가드·예외 핸들러·라우터 순서)에서 관문이 실제로 동작하는지 본다.
"""

from collections.abc import Iterator

import httpx
import pytest

from tests.conftest import bearer, login
from tests.test_skills import make_zip

SKILL_MD = "---\nname: shared-skill\ndescription: 공유용 스킬\n---\n\n# 본문\n"


async def _create_skill(client: httpx.AsyncClient, token: str, name: str = "공유스킬") -> str:
    res = await client.post(
        "/api/v1/skills", json={"name": name, "description": "d"}, headers=bearer(token)
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _freeze(client: httpx.AsyncClient, token: str, skill_id: str) -> str:
    res = await client.post(
        f"/api/v1/skills/{skill_id}/freeze", json={"changelog": "v1"}, headers=bearer(token)
    )
    assert res.status_code == 200, res.text
    return res.json()["version_id"]


async def _import_zip(client: httpx.AsyncClient, token: str) -> tuple[str, dict]:
    res = await client.post(
        "/api/v1/skills/import-zip",
        files={
            "file": ("pack.zip", make_zip({"shared-skill/SKILL.md": SKILL_MD}), "application/zip")
        },
        headers=bearer(token),
    )
    assert res.status_code == 201, res.text
    return res.json()["id"], res.json()["import_report"]


async def test_import_reports_four_layers(client: httpx.AsyncClient, user_a, mem_storage) -> None:
    token = await login(client, user_a.email)

    _skill_id, report = await _import_zip(client, token)

    assert set(report) >= {"standard", "portability", "policy", "capability", "root_name"}
    assert report["standard"]["profile"] == "standard_v1"
    assert report["root_name"] == "shared-skill"


async def test_authored_version_publish_then_non_owner_sees_it(
    client: httpx.AsyncClient, user_a, user_b, mem_storage
) -> None:
    token_a = await login(client, user_a.email)
    skill_id = await _create_skill(client, token_a)
    version_id = await _freeze(client, token_a, skill_id)

    published = await client.post(
        f"/api/v1/skills/{skill_id}/versions/{version_id}/publish", headers=bearer(token_a)
    )

    assert published.status_code == 200
    assert published.json()["status"] == "published"
    client.cookies.clear()
    token_b = await login(client, user_b.email)
    detail = await client.get(f"/api/v1/skills/{skill_id}", headers=bearer(token_b))
    assert detail.json()["published_version_id"] == version_id
    assert detail.json()["can_run"] is True


async def test_imported_publish_waits_for_admin_review(
    client: httpx.AsyncClient, user_a, user_b, admin_user, mem_storage
) -> None:
    """S-HCI-5: 가져온 스킬의 org 공개는 관리자 승인을 거친다(제품 앱에서도 동일)."""
    token_a = await login(client, user_a.email)
    skill_id, _ = await _import_zip(client, token_a)
    version_id = await _freeze(client, token_a, skill_id)

    pending = await client.post(
        f"/api/v1/skills/{skill_id}/versions/{version_id}/publish", headers=bearer(token_a)
    )
    assert pending.status_code == 202
    request_id = pending.json()["request_id"]

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    before = await client.get(f"/api/v1/skills/{skill_id}", headers=bearer(token_b))
    assert before.status_code == 404  # 아직 비공개다

    client.cookies.clear()
    admin_token = await login(client, admin_user.email)
    queue = await client.get("/api/v1/admin/skills/review-requests", headers=bearer(admin_token))
    assert [item["id"] for item in queue.json()] == [request_id]
    approved = await client.post(
        f"/api/v1/admin/skills/review-requests/{request_id}/approve",
        headers=bearer(admin_token),
    )
    assert approved.status_code == 200

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    after = await client.get(f"/api/v1/skills/{skill_id}", headers=bearer(token_b))
    assert after.status_code == 200
    assert after.json()["published_version_id"] == version_id


async def test_review_queue_requires_admin(
    client: httpx.AsyncClient, user_a, mem_storage
) -> None:
    token = await login(client, user_a.email)

    res = await client.get("/api/v1/admin/skills/review-requests", headers=bearer(token))

    assert res.status_code == 403


async def test_export_round_trips_through_the_product_app(
    client: httpx.AsyncClient, user_a, mem_storage
) -> None:
    token = await login(client, user_a.email)
    skill_id, _ = await _import_zip(client, token)
    version_id = await _freeze(client, token, skill_id)

    res = await client.get(f"/api/v1/skills/versions/{version_id}/export", headers=bearer(token))

    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    assert res.headers["content-disposition"].startswith("attachment; filename=")
    assert res.headers["x-skill-validation-profile"] == "standard_v1"
    reimported = await client.post(
        "/api/v1/skills/import-zip",
        files={"file": ("again.zip", res.content, "application/zip")},
        headers=bearer(token),
    )
    assert reimported.status_code == 201


@pytest.mark.parametrize("method", ["patch", "delete"])
async def test_admin_agents_crud_rejects_skill_wrappers(
    client: httpx.AsyncClient, user_a, admin_user, mem_storage, method: str
) -> None:
    """R3-02: 스킬 wrapper는 실행 핸들이다 — agents 경로로 바꾸면 관문을 우회한다."""
    token = await login(client, user_a.email)
    skill_id = await _create_skill(client, token, "래퍼스킬")
    await _freeze(client, token, skill_id)
    detail = await client.get(f"/api/v1/skills/{skill_id}", headers=bearer(token))
    wrapper_id = detail.json()["wrapper_agent_id"]
    assert wrapper_id is not None

    client.cookies.clear()
    admin_token = await login(client, admin_user.email)
    if method == "patch":
        res = await client.patch(
            f"/api/v1/admin/agents/{wrapper_id}",
            json={"enabled": False},
            headers=bearer(admin_token),
        )
    else:
        res = await client.delete(
            f"/api/v1/admin/agents/{wrapper_id}", headers=bearer(admin_token)
        )

    assert res.status_code == 409


async def test_workflow_save_rejects_an_unexposed_skill_target(
    client: httpx.AsyncClient, user_a, user_b, mem_storage
) -> None:
    """R5-03: 저장 검사와 실행 검사가 같은 규칙을 쓴다(비소유자 org 스킬은 호출 불가)."""
    token_a = await login(client, user_a.email)
    skill_id = await _create_skill(client, token_a, "워크플로대상")
    version_id = await _freeze(client, token_a, skill_id)
    published = await client.post(
        f"/api/v1/skills/{skill_id}/versions/{version_id}/publish", headers=bearer(token_a)
    )
    assert published.status_code == 200, published.text  # 선행 공개가 실제로 일어났다
    detail = await client.get(f"/api/v1/skills/{skill_id}", headers=bearer(token_a))
    wrapper_id = detail.json()["wrapper_agent_id"]
    body = {
        "name": "호출 워크플로",
        "description": "",
        "definition": {
            "nodes": [{"id": "call", "type": "agent_call", "agent_id": wrapper_id}],
            "edges": [],
        },
    }

    # 양성 대조: 소유자는 같은 정의를 저장할 수 있다 — 422가 정의 자체의 결함이 아님을 고정
    own = await client.post("/api/v1/workflows", json=body, headers=bearer(token_a))
    assert own.status_code == 201, own.text

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    res = await client.post("/api/v1/workflows", json=body, headers=bearer(token_b))

    assert res.status_code == 422
    # 422의 원인이 노출 관문임을 고정(P0-15) — 다른 검증 실패로는 통과하지 않는다
    assert "대상 에이전트를 호출할 수 없습니다" in res.json()["detail"]
    assert wrapper_id in res.json()["detail"]


# ---------- 코어 v0.10.6 표면의 비인가 관점 (계획 §4.5 T-1~T-6, AC-14) ----------


async def _private_frozen_skill(
    client: httpx.AsyncClient, token: str, name: str = "비공개대상"
) -> tuple[str, str, str]:
    skill_id = await _create_skill(client, token, name)
    version_id = await _freeze(client, token, skill_id)
    detail = await client.get(f"/api/v1/skills/{skill_id}", headers=bearer(token))
    return skill_id, version_id, detail.json()["wrapper_agent_id"]


async def test_t1_non_owner_cannot_read_private_skill_or_its_files(
    client: httpx.AsyncClient, user_a, user_b, mem_storage
) -> None:
    """T-1: 비소유자는 private 스킬의 상세·version·파일·frontmatter를 404로만 본다(본문 비노출)."""
    token_a = await login(client, user_a.email)
    skill_id, _vid, _wid = await _private_frozen_skill(client, token_a, "비밀레시피")

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    for path in ("", "/versions", "/files", "/files/SKILL.md", "/frontmatter"):
        res = await client.get(f"/api/v1/skills/{skill_id}{path}", headers=bearer(token_b))
        assert res.status_code == 404, (path, res.text)
        assert "비밀레시피" not in res.text


@pytest.fixture
def standard_runtime_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """카탈로그는 표준 런타임이 켜져야 항목을 낸다 — 설정 오버라이드(mock 아님)로 켠다."""
    from biznotes_core.config import reset_settings

    monkeypatch.setenv("SKILL_STANDARD_RUNTIME_ENABLED", "true")
    reset_settings()
    yield
    monkeypatch.delenv("SKILL_STANDARD_RUNTIME_ENABLED")
    reset_settings()


async def test_t2_catalog_excludes_other_users_private_skill(
    client: httpx.AsyncClient, user_a, user_b, mem_storage, standard_runtime_on
) -> None:
    """T-2: 비소유자의 /skills/catalog·/skills 목록에 남의 private 스킬이 없다(소유자는 본다)."""
    token_a = await login(client, user_a.email)
    skill_id, _vid, _wid = await _private_frozen_skill(client, token_a)
    own = await client.get("/api/v1/skills/catalog", headers=bearer(token_a))
    assert own.status_code == 200
    assert skill_id in own.text

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    for path in ("/api/v1/skills/catalog", "/api/v1/skills"):
        res = await client.get(path, headers=bearer(token_b))
        assert res.status_code == 200, res.text
        assert skill_id not in res.text


async def test_t2_imported_private_skill_stays_out_of_others_catalogs_with_runtime_on(
    client: httpx.AsyncClient, user_a, user_b, admin_user, mem_storage, standard_runtime_on
) -> None:
    """#65(BL-93) — 런타임을 켠 상태에서 **가져온** private 스킬도 남의 카탈로그·목록에 없다.

    #259가 dev·stg에서 표준 런타임을 켜고 채팅 설치(가져오기)를 연다. admin도 승격 없이 본인 ·
    system · 승인된 org 항목만 카탈로그에 받는다(코어 `SkillCatalog.for_user`).
    """
    token_a = await login(client, user_a.email)
    skill_id, _report = await _import_zip(client, token_a)
    await _freeze(client, token_a, skill_id)
    own = await client.get("/api/v1/skills/catalog", headers=bearer(token_a))
    assert own.status_code == 200, own.text
    assert own.json()["enabled"] is True
    assert [(e["skill_id"], e["source"]) for e in own.json()["entries"]] == [(skill_id, "own")]

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    for path in ("/api/v1/skills/catalog", "/api/v1/skills"):
        res = await client.get(path, headers=bearer(token_b))
        assert res.status_code == 200, res.text
        assert skill_id not in res.text, path

    client.cookies.clear()
    token_admin = await login(client, admin_user.email)
    catalog = await client.get("/api/v1/skills/catalog", headers=bearer(token_admin))
    assert catalog.status_code == 200, catalog.text
    assert skill_id not in catalog.text


async def test_t3_non_owner_cannot_export_private_version(
    client: httpx.AsyncClient, user_a, user_b, mem_storage
) -> None:
    """T-3: 비소유자의 private version export·metadata·파일 → 404."""
    token_a = await login(client, user_a.email)
    _sid, version_id, _wid = await _private_frozen_skill(client, token_a)

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    for path in ("export", "metadata", "files"):
        res = await client.get(
            f"/api/v1/skills/versions/{version_id}/{path}", headers=bearer(token_b)
        )
        assert res.status_code == 404, (path, res.text)


async def test_t4_import_url_requires_auth_and_blocks_private_addresses(
    client: httpx.AsyncClient, user_a, mem_storage, db
) -> None:
    """T-4: 무인증 POST /skills/import-url → 401, 인증 사용자의 사설 주소 → 422 ssrf_blocked.

    fetch는 코어 SSRF 관문(`biznotes_core.core.ssrf`)만 쓴다 — 관문을 mock하지 않고 실제로 막는다.
    """
    from biznotes_core.models import Skill
    from sqlalchemy import func, select

    anon = await client.post(
        "/api/v1/skills/import-url", json={"url": "https://example.com/skill.zip"}
    )
    assert anon.status_code == 401

    token = await login(client, user_a.email)
    for url in ("http://127.0.0.1/skill.zip", "http://169.254.169.254/latest/meta-data"):
        res = await client.post(
            "/api/v1/skills/import-url", json={"url": url}, headers=bearer(token)
        )
        assert res.status_code == 422, (url, res.text)
        assert res.json()["detail"].startswith("[ssrf_blocked] ")
    count = (await db.execute(select(func.count()).select_from(Skill))).scalar_one()
    assert count == 0  # 막힌 요청은 스킬을 만들지 않는다


async def test_import_url_candidates_requires_auth_and_blocks_private_addresses(
    client: httpx.AsyncClient, user_a, mem_storage, db
) -> None:
    """URL archive 후보 조회(코어 v0.28.0 #402, core-v0.28.0-upgrade AC-10)도 무인증 401이고
    사설 주소는 422 `ssrf_blocked`다 — `import-url`과 같은 관문을 mock 없이 지난다. 조회는
    저장하지 않는다."""
    from biznotes_core.models import Skill
    from sqlalchemy import func, select

    path = "/api/v1/skills/import-url/candidates"
    anon = await client.post(path, json={"url": "https://example.com/skill.zip"})
    assert anon.status_code == 401

    token = await login(client, user_a.email)
    for url in ("http://127.0.0.1/skill.zip", "http://169.254.169.254/latest/meta-data"):
        res = await client.post(path, json={"url": url}, headers=bearer(token))
        assert res.status_code == 422, (url, res.text)
        assert res.json()["detail"].startswith("[ssrf_blocked] ")
    count = (await db.execute(select(func.count()).select_from(Skill))).scalar_one()
    assert count == 0


async def test_t5_non_admin_cannot_set_published_version(
    client: httpx.AsyncClient, user_a, mem_storage
) -> None:
    """T-5: 일반 사용자(소유자라도) POST /admin/skills/{id}/published-version → 403."""
    token = await login(client, user_a.email)
    skill_id, version_id, _wid = await _private_frozen_skill(client, token)
    for version in (version_id, None):
        res = await client.post(
            f"/api/v1/admin/skills/{skill_id}/published-version",
            json={"version_id": version},
            headers=bearer(token),
        )
        assert res.status_code == 403


async def test_callable_list_hides_wrapper_whose_skill_is_not_exposed(
    client: httpx.AsyncClient, user_a, user_b, mem_storage, db
) -> None:
    """P0-09: callable 목록은 저장 검사와 같은 노출 모델로 거른다.

    wrapper의 scope·enabled는 freeze 때만 갱신된다 — wrapper가 system으로 보이더라도
    스킬이 노출되지 않았으면 목록에 없어야 한다(목록이 제시한 대상이 저장 시 422가 되지 않게).
    """
    import uuid

    from biznotes_core.models import Agent

    token_a = await login(client, user_a.email)
    _sid, _vid, wrapper_id = await _private_frozen_skill(client, token_a)
    wrapper = await db.get(Agent, uuid.UUID(wrapper_id))
    assert wrapper is not None
    wrapper.scope = "system"  # 동기화되지 않은 wrapper 메타데이터(공개 철회 뒤의 잔재)
    await db.commit()

    own = await client.get("/api/v1/workflows/callable", headers=bearer(token_a))
    assert wrapper_id in {a["id"] for a in own.json()}  # 소유자는 최신 freeze를 실행할 수 있다

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    other = await client.get("/api/v1/workflows/callable", headers=bearer(token_b))
    assert other.status_code == 200
    assert wrapper_id not in {a["id"] for a in other.json()}


# ---------- 431da1d 이식분 — 코어가 대체한 수정의 회귀 (계획 HCI-10, preflight §6.4) ----------


@pytest.mark.parametrize(
    "skill_md",
    [
        pytest.param("frontmatter가 없는 스킬", id="no-frontmatter"),
        pytest.param("---\nname: [unclosed\n---\n\nbody", id="bad-yaml"),
    ],
)
async def test_import_zip_parse_error_is_422(
    client: httpx.AsyncClient, user_a, mem_storage, skill_md
) -> None:
    """frontmatter 파싱 오류(SkillParseError)는 500이 아니라 422다(코어 import-zip 경로)."""
    token = await login(client, user_a.email)
    files = {"file": ("skill.zip", make_zip({"SKILL.md": skill_md}), "application/zip")}
    res = await client.post("/api/v1/skills/import-zip", files=files, headers=bearer(token))
    assert res.status_code == 422
    assert res.json()["detail"].startswith("[parse_error] ")


async def test_import_zip_audit_records_skill_id(
    client: httpx.AsyncClient, user_a, mem_storage, db
) -> None:
    """가져오기 감사 로그가 스킬 id를 남긴다 — 표시명은 바뀔 수 있어 id로 추적한다."""
    from biznotes_core.models import AuditLog
    from sqlalchemy import select

    token = await login(client, user_a.email)
    skill_id, _report = await _import_zip(client, token)

    rows = (
        await db.execute(select(AuditLog).where(AuditLog.action == "skill.import"))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].detail == {"source": "zip", "skill_id": skill_id}


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://[::1]/s.zip", "내부망 주소"),
        ("http://localhost/s.zip", "내부망 주소"),
        ("http://10.0.0.5/s.zip", "내부망 주소"),
        ("http://192.168.0.1:8080/s.zip", "내부망 주소"),
        ("http://0.0.0.0/s.zip", "내부망 주소"),
        ("http://2130706433/s.zip", "내부망 주소"),  # 정수 표기 127.0.0.1
        ("http://[fd00::1]/s.zip", "내부망 주소"),  # IPv6 ULA
        ("http://user:pw@127.0.0.1/s.zip", "내부망 주소"),  # 자격증명 표기로 호스트를 숨겨도 막는다
        ("ftp://example.com/s.zip", "허용되지 않는 스킴"),
        ("file:///etc/passwd", "허용되지 않는 스킴"),
    ],
)
async def test_t4_import_url_ssrf_gate_blocks_every_internal_form(
    client: httpx.AsyncClient, user_a, mem_storage, url, reason
) -> None:
    """T-4 보강: 표기를 바꾼 내부 주소·비허용 스킴도 fetch 전에 막는다(네트워크 없음)."""
    token = await login(client, user_a.email)
    res = await client.post("/api/v1/skills/import-url", json={"url": url}, headers=bearer(token))
    assert res.status_code == 422, res.text
    assert res.json()["detail"].startswith("[ssrf_blocked] ")
    assert reason in res.json()["detail"]


async def test_t4_import_url_rejects_malformed_digest_before_fetch(
    client: httpx.AsyncClient, user_a, mem_storage
) -> None:
    token = await login(client, user_a.email)
    res = await client.post(
        "/api/v1/skills/import-url",
        json={"url": "http://127.0.0.1/s.zip", "sha256": "zz"},
        headers=bearer(token),
    )
    assert res.status_code == 422
    assert res.json()["detail"].startswith("[bad_digest] ")


# ---------- core-v0.11.0-upgrade P1 (직전 계획 v7 승계): E-7 스킬 삭제·E-8a 가져오기 ----------


async def _wrappers(db, version_id: str) -> list:
    import uuid

    from biznotes_core.models import Agent
    from sqlalchemy import select

    db.expire_all()
    return list(
        (
            await db.execute(
                select(Agent).where(
                    Agent.source_kind == "skill_package",
                    Agent.skill_version_id == uuid.UUID(version_id),
                )
            )
        ).scalars()
    )


async def test_delete_skill_removes_unreferenced_wrapper(
    client: httpx.AsyncClient, user_a, mem_storage, db
) -> None:
    """E-7 — 스킬 삭제가 참조 없는 wrapper를 지우고 audit detail에 `wrappers_deleted`(코어 #121)."""
    from biznotes_core.models import Agent, AuditLog
    from sqlalchemy import select

    token = await login(client, user_a.email)
    skill_id = await _create_skill(client, token)
    version_id = await _freeze(client, token, skill_id)
    wrappers = await _wrappers(db, version_id)
    assert len(wrappers) == 1
    wrapper_id = wrappers[0].id

    res = await client.delete(f"/api/v1/skills/{skill_id}", headers=bearer(token))

    assert res.status_code == 204, res.text
    db.expire_all()
    assert await db.get(Agent, wrapper_id) is None
    audit = (
        await db.execute(select(AuditLog).where(AuditLog.action == "skill.delete"))
    ).scalar_one()
    assert audit.detail["wrappers_deleted"] == [str(wrapper_id)]
    assert audit.detail["wrappers_disabled"] == []


@pytest.mark.parametrize("who", ["user", "admin"])
async def test_delete_skill_by_non_owner_is_404_and_keeps_wrapper(
    client: httpx.AsyncClient, user_a, user_b, admin_user, mem_storage, db, who
) -> None:
    """E-7 ①② — 스킬 소유자만 지운다(`can_edit`). 비소유 일반 사용자·비소유 admin 모두 404,
    스킬·wrapper 무변경(admin 승격 없음 — v7 A-43)."""
    other_email = user_b.email if who == "user" else admin_user.email  # expire_all 전에 잡는다
    owner_token = await login(client, user_a.email)
    skill_id = await _create_skill(client, owner_token)
    version_id = await _freeze(client, owner_token, skill_id)
    before = [(w.id, w.enabled) for w in await _wrappers(db, version_id)]
    assert len(before) == 1

    other_token = await login(client, other_email)
    client.cookies.clear()
    res = await client.delete(f"/api/v1/skills/{skill_id}", headers=bearer(other_token))

    assert res.status_code == 404
    assert [(w.id, w.enabled) for w in await _wrappers(db, version_id)] == before
    client.cookies.clear()
    still = await client.get(f"/api/v1/skills/{skill_id}", headers=bearer(owner_token))
    assert still.status_code == 200


async def test_import_zip_bomb_is_422_with_reason(
    client: httpx.AsyncClient, user_a, mem_storage
) -> None:
    """E-8a — 신고 압축률 1000 초과 항목(1 MiB 0바이트, 약 1015배)은 422 `[bomb_suspected]`
    (코어 v0.10.10 #125, v7 AC-10 ④)."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb/SKILL.md", SKILL_MD.replace("shared-skill", "bomb"))
        zf.writestr("bomb/assets/zeros.bin", b"\0" * (1 << 20))
    token = await login(client, user_a.email)

    res = await client.post(
        "/api/v1/skills/import-zip",
        files={"file": ("bomb.zip", buf.getvalue(), "application/zip")},
        headers=bearer(token),
    )

    assert res.status_code == 422, res.text
    assert res.json()["detail"].startswith("[bomb_suspected]")


async def test_import_zip_requires_auth(client: httpx.AsyncClient, mem_storage) -> None:
    """E-8a — 비인증 401(v7 A-44: 기존 테스트 없음)."""
    client.cookies.clear()
    res = await client.post(
        "/api/v1/skills/import-zip",
        files={"file": ("pack.zip", make_zip({"s/SKILL.md": SKILL_MD}), "application/zip")},
    )
    assert res.status_code == 401
