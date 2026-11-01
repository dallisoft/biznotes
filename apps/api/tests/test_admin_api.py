"""어드민 API 테스트 — RBAC, 사용자/그룹/LLM/RAG/에이전트 관리, 감사 로그."""

import asyncio
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from biznotes_core.api.v1.admin import llm as llm_api
from biznotes_core.config import get_settings
from biznotes_core.core.db import async_session_factory
from biznotes_core.models import AuditLog, LLMModel, LLMProvider, Notebook, Source, User
from biznotes_core.schemas.llm import ModelCreate, ModelImport
from biznotes_core.services.audit import record as record_audit
from fastapi import HTTPException
from sqlalchemy import func, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _create_user, bearer, login


@pytest.fixture(autouse=True)
def _mock_celery(monkeypatch):
    class FakeTask:
        id = "fake"

    monkeypatch.setattr(
        "biznotes_core.workers.tasks.ingestion.ingest_source.delay",
        lambda sid: FakeTask(),
    )
    monkeypatch.setattr(
        "biznotes_core.workers.tasks.ingestion.ingest_source.apply_async",
        lambda *a, **k: FakeTask(),
    )


async def test_admin_endpoints_require_admin_role(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)
    res = await client.get("/api/v1/admin/users", headers=bearer(token))
    assert res.status_code == 403


async def test_user_crud_and_audit(client: httpx.AsyncClient, admin_user):
    token = await login(client, admin_user.email)

    # 생성
    email = f"managed-{uuid.uuid4().hex[:6]}@test.dev"
    res = await client.post(
        "/api/v1/admin/users",
        json={"email": email, "name": "Managed", "password": "password123", "role": "user"},
        headers=bearer(token),
    )
    assert res.status_code == 201, res.text
    uid = res.json()["id"]
    # 코어 v0.11.0 #165 — 관리자가 정한 비밀번호는 다음 로그인 때 본인이 바꾼다
    assert res.json()["must_change_password"] is True

    # 비밀번호 하한 위반 → 입력값 없는 dict detail의 422(#165, core-v0.11.0-upgrade A-17)
    res = await client.post(
        "/api/v1/admin/users",
        json={"email": f"short-{email}", "name": "S", "password": "short1", "role": "user"},
        headers=bearer(token),
    )
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "invalid_new_password"
    assert res.json()["detail"]["reason"] == "too_short"
    assert "short1" not in res.text

    # 중복 → 409
    res = await client.post(
        "/api/v1/admin/users",
        json={"email": email, "name": "Dup", "password": "password123"},
        headers=bearer(token),
    )
    assert res.status_code == 409

    # 비활성화
    res = await client.patch(
        f"/api/v1/admin/users/{uid}", json={"is_active": False}, headers=bearer(token)
    )
    assert res.json()["is_active"] is False

    # 비활성 사용자는 로그인 불가
    res = await client.post("/api/v1/auth/login", json={"email": email, "password": "password123"})
    assert res.status_code == 403

    # 비밀번호 초기화
    res = await client.post(f"/api/v1/admin/users/{uid}/password-reset", headers=bearer(token))
    assert res.status_code == 200
    assert len(res.json()["temporary_password"]) >= 8
    users = (await client.get("/api/v1/admin/users", headers=bearer(token))).json()
    assert next(u for u in users if u["id"] == uid)["must_change_password"] is True

    # 자기 자신 삭제 차단
    res = await client.delete(f"/api/v1/admin/users/{admin_user.id}", headers=bearer(token))
    assert res.status_code == 422

    # 감사 로그에 기록됨
    res = await client.get("/api/v1/admin/audit-logs", headers=bearer(token))
    actions = [log["action"] for log in res.json()]
    assert "user.create" in actions
    assert "user.password_reset" in actions


async def _audit_count(db, *emails: str) -> int:
    """이 테스트의 사용자 행을 대상으로 한 감사 로그 수 — 다른 xdist 워커의 기록은 세지 않는다."""
    stmt = select(func.count()).select_from(AuditLog).where(AuditLog.resource.in_(emails))
    return (await db.execute(stmt)).scalar_one()


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"is_active": False, "name": "x"}, "자기 계정은 비활성화할 수 없습니다"),
        ({"is_active": None, "name": "x"}, "자기 계정은 비활성화할 수 없습니다"),
        ({"role": "user", "name": "x"}, "자기 자신의 관리자 권한은 해제할 수 없습니다"),
    ],
)
async def test_admin_cannot_deactivate_or_demote_self(
    client: httpx.AsyncClient, db, admin_user, body, detail
):
    """코어 v0.15.1 #219(core-v0.16.0 AC-1.3 ⑤) — 관리자 자기 잠금은 API에서도 422 고정
    문구로 막힌다.

    같은 요청의 다른 필드(`name`)도 적용되지 않고 감사 로그도 남지 않는다. 명시적 null도 거부된다.
    """
    token = await login(client, admin_user.email)
    before = await _audit_count(db, admin_user.email)

    res = await client.patch(
        f"/api/v1/admin/users/{admin_user.id}", json=body, headers=bearer(token)
    )

    assert res.status_code == 422, res.text
    assert res.json() == {"detail": detail}
    me = await db.get(User, admin_user.id, populate_existing=True)
    assert (me.is_active, me.role, me.name) == (True, "admin", "admin")
    assert await _audit_count(db, admin_user.email) == before


@pytest.mark.parametrize(("body", "field"), [({"name": None}, "name"), ({"role": None}, "role")])
async def test_admin_patch_other_user_explicit_null_is_422(
    client: httpx.AsyncClient, db, admin_user, user_a, body, field
):
    """코어 v0.17.1 #242(core-v0.17.1-upgrade AC-1.7 ④) — 다른 사용자 대상 명시적 null은 대상
    조회 전에 422(str)다. 이전에는 커밋 때 500이었고 오류 상세에 대상 행이 실렸다.

    행·감사 로그는 바뀌지 않는다. 없는 사용자 id에도 같은 422다(조회 전 판정).
    """
    token = await login(client, admin_user.email)
    before = await _audit_count(db, user_a.email)
    expected = {"detail": f"null로 바꿀 수 없는 필드: {field}"}

    res = await client.patch(f"/api/v1/admin/users/{user_a.id}", json=body, headers=bearer(token))

    assert res.status_code == 422, res.text
    assert res.json() == expected
    target = await db.get(User, user_a.id, populate_existing=True)
    assert (target.name, target.role, target.is_active) == ("user-a", "user", True)
    assert await _audit_count(db, user_a.email) == before

    missing = await client.patch(
        f"/api/v1/admin/users/{uuid.uuid4()}", json=body, headers=bearer(token)
    )
    assert missing.status_code == 422, missing.text
    assert missing.json() == expected


async def test_admin_can_rename_self_and_deactivate_others(
    client: httpx.AsyncClient, db, admin_user, user_a
):
    """자기 보호는 잠금 필드만 막는다 — 자기 `name` 변경과 다른 사용자 비활성화는 그대로
    200(감사 기록)."""
    token = await login(client, admin_user.email)
    before = await _audit_count(db, admin_user.email, user_a.email)

    res = await client.patch(
        f"/api/v1/admin/users/{admin_user.id}",
        json={"name": "관리자 새 이름", "is_active": True, "role": "admin"},
        headers=bearer(token),
    )
    assert res.status_code == 200, res.text
    assert (res.json()["name"], res.json()["is_active"], res.json()["role"]) == (
        "관리자 새 이름",
        True,
        "admin",
    )

    res = await client.patch(
        f"/api/v1/admin/users/{user_a.id}", json={"is_active": False}, headers=bearer(token)
    )
    assert res.status_code == 200, res.text
    assert res.json()["is_active"] is False
    assert await _audit_count(db, admin_user.email, user_a.email) == before + 2


async def test_non_admin_cannot_patch_users(client: httpx.AsyncClient, db, admin_user, user_a):
    """비인가 관점(§3.4-5): 일반 사용자의 `PATCH /admin/users/{id}`는 403 — 대상 행·감사 로그 불변.

    남의 계정 비활성화와 자기 권한 상승 둘 다 막힌다.
    """
    token = await login(client, user_a.email)
    before = await _audit_count(db, admin_user.email, user_a.email)

    res = await client.patch(
        f"/api/v1/admin/users/{admin_user.id}", json={"is_active": False}, headers=bearer(token)
    )
    assert res.status_code == 403, res.text
    res = await client.patch(
        f"/api/v1/admin/users/{user_a.id}", json={"role": "admin"}, headers=bearer(token)
    )
    assert res.status_code == 403, res.text

    target = await db.get(User, admin_user.id, populate_existing=True)
    assert (target.is_active, target.role) == (True, "admin")
    me = await db.get(User, user_a.id, populate_existing=True)
    assert (me.is_active, me.role) == (True, "user")
    assert await _audit_count(db, admin_user.email, user_a.email) == before


async def test_user_delete_cleans_owned_source_objects(
    client: httpx.AsyncClient, db, admin_user, user_a, deleted_objects
):
    notebook = Notebook(owner_id=user_a.id, title="삭제될 사용자 노트북")
    db.add(notebook)
    await db.flush()
    source = Source(
        notebook_id=notebook.id,
        type="text",
        title="s",
        original_object_key=f"{uuid.uuid4()}/raw.txt",
        parsed_object_key=f"{uuid.uuid4()}/parsed.md",
    )
    db.add(source)
    await db.commit()
    token = await login(client, admin_user.email)

    res = await client.delete(f"/api/v1/admin/users/{user_a.id}", headers=bearer(token))

    assert res.status_code == 204, res.text
    settings = get_settings()
    deleted = {(bucket, key) for bucket, keys in deleted_objects for key in keys}
    assert (settings.storage_bucket_sources, source.original_object_key) in deleted
    assert (settings.storage_bucket_parsed, source.parsed_object_key) in deleted


async def test_group_membership_flow(client: httpx.AsyncClient, admin_user, user_a):
    token = await login(client, admin_user.email)

    res = await client.post(
        "/api/v1/admin/groups",
        json={"name": f"team-{uuid.uuid4().hex[:6]}", "description": "테스트 팀"},
        headers=bearer(token),
    )
    assert res.status_code == 201
    gid = res.json()["id"]

    res = await client.post(
        f"/api/v1/admin/groups/{gid}/members",
        json={"user_id": str(user_a.id)},
        headers=bearer(token),
    )
    assert res.status_code == 201

    res = await client.get(f"/api/v1/admin/groups/{gid}/members", headers=bearer(token))
    assert any(m["user_id"] == str(user_a.id) for m in res.json())

    res = await client.delete(
        f"/api/v1/admin/groups/{gid}/members/{user_a.id}", headers=bearer(token)
    )
    assert res.status_code == 204


async def test_llm_provider_model_management(client: httpx.AsyncClient, admin_user):
    token = await login(client, admin_user.email)

    # 프로바이더 등록 (API 키 암호화 저장)
    res = await client.post(
        "/api/v1/admin/llm/providers",
        json={
            "name": f"prov-{uuid.uuid4().hex[:6]}",
            "type": "openai",
            "api_key": "sk-secret",
        },
        headers=bearer(token),
    )
    assert res.status_code == 201
    body = res.json()
    assert body["has_api_key"] is True
    pid = body["id"]

    # 모델 등록 + 사용자 노출 확인
    res = await client.post(
        "/api/v1/admin/llm/models",
        json={
            "provider_id": pid,
            "model_name": "gpt-test",
            "display_name": "GPT Test",
            "capabilities": ["llm"],
        },
        headers=bearer(token),
    )
    assert res.status_code == 201
    mid = res.json()["id"]

    res = await client.get("/api/v1/models", headers=bearer(token))
    assert any(m["id"] == mid for m in res.json())

    # default 지정
    res = await client.post(f"/api/v1/admin/llm/models/{mid}/set-default", headers=bearer(token))
    assert res.json()["is_default"] is True

    # disable → 사용자 목록에서 제외
    res = await client.patch(
        f"/api/v1/admin/llm/models/{mid}", json={"enabled": False}, headers=bearer(token)
    )
    res = await client.get("/api/v1/models", headers=bearer(token))
    assert all(m["id"] != mid for m in res.json())


async def test_provider_embedding_max_concurrency_set_keep_clear(
    client: httpx.AsyncClient, admin_user
) -> None:
    token = await login(client, admin_user.email)
    res = await client.post(
        "/api/v1/admin/llm/providers",
        json={
            "name": f"gate-{uuid.uuid4().hex[:6]}",
            "type": "openai",
            "api_key": "k",
            "embedding_max_concurrency": 3,
        },
        headers=bearer(token),
    )
    assert res.status_code == 201, res.text
    assert res.json()["embedding_max_concurrency"] == 3
    pid = uuid.UUID(res.json()["id"])

    async def stored() -> int | None:
        async with async_session_factory() as db:
            provider = await db.get(LLMProvider, pid)
            assert provider is not None
            return provider.embedding_max_concurrency

    assert await stored() == 3

    res = await client.patch(
        f"/api/v1/admin/llm/providers/{pid}", json={"enabled": False}, headers=bearer(token)
    )
    assert res.status_code == 200, res.text
    assert res.json()["embedding_max_concurrency"] == 3
    assert await stored() == 3

    res = await client.patch(
        f"/api/v1/admin/llm/providers/{pid}",
        json={"embedding_max_concurrency": None},
        headers=bearer(token),
    )
    assert res.status_code == 200, res.text
    assert res.json()["embedding_max_concurrency"] is None
    assert await stored() is None


@pytest.mark.parametrize("limit", [0, 65])
async def test_provider_embedding_max_concurrency_out_of_range_is_422(
    client: httpx.AsyncClient, admin_user, limit: int
) -> None:
    token = await login(client, admin_user.email)
    res = await client.post(
        "/api/v1/admin/llm/providers",
        json={
            "name": f"gate-{uuid.uuid4().hex[:6]}",
            "type": "openai",
            "embedding_max_concurrency": limit,
        },
        headers=bearer(token),
    )
    assert res.status_code == 422, res.text

    res = await client.post(
        "/api/v1/admin/llm/providers",
        json={"name": f"gate-{uuid.uuid4().hex[:6]}", "type": "openai"},
        headers=bearer(token),
    )
    pid = res.json()["id"]
    res = await client.patch(
        f"/api/v1/admin/llm/providers/{pid}",
        json={"embedding_max_concurrency": limit},
        headers=bearer(token),
    )
    assert res.status_code == 422, res.text


async def test_provider_embedding_gate_status(client: httpx.AsyncClient, admin_user) -> None:
    # 새 provider_id라 Redis의 embgate:*:<id> 키가 비어 있다 — in_use 0이 전제다
    token = await login(client, admin_user.email)
    res = await client.post(
        "/api/v1/admin/llm/providers",
        json={"name": f"gate-{uuid.uuid4().hex[:6]}", "type": "openai"},
        headers=bearer(token),
    )
    pid = res.json()["id"]

    res = await client.get(
        f"/api/v1/admin/llm/providers/{pid}/embedding-gate", headers=bearer(token)
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["limit"] is None
    assert body["in_use"] == 0
    assert body["earliest_expiry_in"] is None

    await client.patch(
        f"/api/v1/admin/llm/providers/{pid}",
        json={"embedding_max_concurrency": 2},
        headers=bearer(token),
    )
    res = await client.get(
        f"/api/v1/admin/llm/providers/{pid}/embedding-gate", headers=bearer(token)
    )
    assert res.status_code == 200, res.text
    assert res.json()["limit"] == 2
    assert res.json()["in_use"] == 0

    res = await client.get(
        f"/api/v1/admin/llm/providers/{uuid.uuid4()}/embedding-gate", headers=bearer(token)
    )
    assert res.status_code == 404


async def test_embedding_model_singleton(client: httpx.AsyncClient, admin_user):
    token = await login(client, admin_user.email)
    res = await client.post(
        "/api/v1/admin/llm/providers",
        json={"name": f"emb-{uuid.uuid4().hex[:6]}", "type": "openai", "api_key": "k"},
        headers=bearer(token),
    )
    pid = res.json()["id"]

    res = await client.post(
        "/api/v1/admin/llm/models",
        json={
            "provider_id": pid,
            "model_name": "embed-1",
            "display_name": "Embed 1",
            "capabilities": ["embedding"],
        },
        headers=bearer(token),
    )
    assert res.status_code == 201
    assert res.json()["is_default"] is True

    # 두 번째 임베딩 모델 등록 차단 (전역 1개 강제)
    res = await client.post(
        "/api/v1/admin/llm/models",
        json={
            "provider_id": pid,
            "model_name": "embed-2",
            "display_name": "Embed 2",
            "capabilities": ["embedding"],
        },
        headers=bearer(token),
    )
    assert res.status_code == 409
    assert "재인덱싱" in res.json()["detail"]


async def test_multi_capability_routing_and_default_uniqueness(
    client: httpx.AsyncClient, admin_user
):
    token = await login(client, admin_user.email)
    pid = (
        await client.post(
            "/api/v1/admin/llm/providers",
            json={"name": f"mc-{uuid.uuid4().hex[:6]}", "type": "openai", "api_key": "k"},
            headers=bearer(token),
        )
    ).json()["id"]

    # 다중 능력(llm + vlm) 모델
    res = await client.post(
        "/api/v1/admin/llm/models",
        json={
            "provider_id": pid,
            "model_name": f"omni-{uuid.uuid4().hex[:6]}",
            "display_name": "Omni",
            "capabilities": ["llm", "vlm"],
        },
        headers=bearer(token),
    )
    assert res.status_code == 201
    omni = res.json()
    assert set(omni["capabilities"]) == {"llm", "vlm"}

    # llm 능력을 가지므로 사용자 LLM 목록(modality=llm 라우팅)에 노출
    user_models = (await client.get("/api/v1/models", headers=bearer(token))).json()
    assert any(m["id"] == omni["id"] for m in user_models)

    # llm 전용 모델 추가 후 default 지정 → 능력(llm) 겹치는 omni의 default 해제
    txt = (
        await client.post(
            "/api/v1/admin/llm/models",
            json={
                "provider_id": pid,
                "model_name": f"txt-{uuid.uuid4().hex[:6]}",
                "display_name": "Txt",
                "capabilities": ["llm"],
            },
            headers=bearer(token),
        )
    ).json()
    await client.post(
        f"/api/v1/admin/llm/models/{omni['id']}/set-default", headers=bearer(token)
    )
    await client.post(
        f"/api/v1/admin/llm/models/{txt['id']}/set-default", headers=bearer(token)
    )
    models = {
        m["id"]: m
        for m in (await client.get("/api/v1/admin/llm/models", headers=bearer(token))).json()
    }
    assert models[txt["id"]]["is_default"] is True
    assert models[omni["id"]]["is_default"] is False  # llm 능력 겹쳐 해제됨


async def test_rag_settings_roundtrip(client: httpx.AsyncClient, admin_user):
    token = await login(client, admin_user.email)

    res = await client.get("/api/v1/admin/rag/settings", headers=bearer(token))
    assert res.status_code == 200
    assert res.json()["chunk_size"] == 800  # 기본값

    res = await client.put(
        "/api/v1/admin/rag/settings",
        json={"chunk_size": 1000, "chunk_overlap": 150, "top_k": 8},
        headers=bearer(token),
    )
    assert res.json()["chunk_size"] == 1000

    res = await client.get("/api/v1/admin/rag/settings", headers=bearer(token))
    body = res.json()
    # 리랭킹 등 신규 필드가 추가될 수 있으므로 부분 일치로 검증
    assert {k: body[k] for k in ("chunk_size", "chunk_overlap", "top_k")} == {
        "chunk_size": 1000,
        "chunk_overlap": 150,
        "top_k": 8,
    }

    # 범위 검증
    res = await client.put(
        "/api/v1/admin/rag/settings",
        json={"chunk_size": 10, "chunk_overlap": 0, "top_k": 1},
        headers=bearer(token),
    )
    assert res.status_code == 422


async def test_rag_reranker_settings_contract(client: httpx.AsyncClient, db, admin_user):
    """core-v0.15.0 AC-2.3: 리랭커 설정 키(#209) — 기본값·원값 보존·형식/범위 검증."""
    from biznotes_core.models import SystemSetting

    token = await login(client, admin_user.email)

    res = await client.get("/api/v1/admin/rag/settings", headers=bearer(token))
    assert res.status_code == 200
    defaults = res.json()
    assert defaults["rerank_timeout_seconds"] == 30
    assert defaults["rerank_model"] == "BAAI/bge-reranker-base"

    # 접두사 없는 옛 값은 입력 그대로 저장한다 — 구 코어로 롤백해도 읽힌다(계획 D-4)
    legacy = {**defaults, "rerank_model": "jinaai/jina-reranker-v1-turbo-en"}
    res = await client.put("/api/v1/admin/rag/settings", json=legacy, headers=bearer(token))
    assert res.status_code == 200
    assert res.json()["rerank_model"] == "jinaai/jina-reranker-v1-turbo-en"
    row = await db.get(SystemSetting, "rag")
    assert row is not None
    assert row.value["rerank_model"] == "jinaai/jina-reranker-v1-turbo-en"

    res = await client.put(
        "/api/v1/admin/rag/settings",
        json={**defaults, "rerank_model": "gateway:not-a-uuid:x"},
        headers=bearer(token),
    )
    assert res.status_code == 422

    for timeout, expected in ((4, 422), (181, 422), (30, 200)):
        res = await client.put(
            "/api/v1/admin/rag/settings",
            json={**defaults, "rerank_timeout_seconds": timeout},
            headers=bearer(token),
        )
        assert res.status_code == expected, (timeout, res.text)
    assert res.json()["rerank_timeout_seconds"] == 30


async def test_rag_settings_put_rejects_non_admin(client: httpx.AsyncClient, db, user_a):
    """리뷰 P2-05·계획 §6: 비관리자의 유효한 RAG 설정 PUT은 403이고 설정 행을 만들지 않는다."""
    from biznotes_core.models import SystemSetting

    token = await login(client, user_a.email)
    body = {
        "chunk_size": 800,
        "chunk_overlap": 100,
        "top_k": 6,
        "rerank_enabled": True,
        "rerank_model": f"gateway:{uuid.uuid4()}:forbidden",
        "rerank_timeout_seconds": 45,
    }

    res = await client.put("/api/v1/admin/rag/settings", json=body, headers=bearer(token))

    assert res.status_code == 403
    assert await db.get(SystemSetting, "rag") is None


async def test_rag_settings_and_index_status_reject_non_admin(
    client: httpx.AsyncClient, db, user_a
):
    """BL-148: 일반 사용자는 RAG 설정 조회·변경과 인덱스 상태 조회에서 403을 받는다.

    권한 검사는 `admin_router`의 `require_admin` 의존성 실경로로 검증한다(mock 없음).
    """
    from biznotes_core.models import SystemSetting

    token = await login(client, user_a.email)

    res = await client.get("/api/v1/admin/rag/settings", headers=bearer(token))
    assert res.status_code == 403, res.text

    res = await client.put(
        "/api/v1/admin/rag/settings",
        json={"chunk_size": 1000, "chunk_overlap": 150, "top_k": 8},
        headers=bearer(token),
    )
    assert res.status_code == 403, res.text
    assert await db.get(SystemSetting, "rag") is None

    res = await client.get("/api/v1/admin/rag/index-status", headers=bearer(token))
    assert res.status_code == 403, res.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/admin/rag/settings"),
        ("PUT", "/api/v1/admin/rag/settings"),
        ("GET", "/api/v1/admin/rag/index-status"),
        ("GET", "/api/v1/admin/users"),
    ],
)
async def test_admin_endpoints_reject_unauthenticated(
    client: httpx.AsyncClient, db, method: str, path: str
):
    """BL-148: 토큰 없는 어드민 API 요청은 401이고 RAG 설정 행을 만들지 않는다."""
    from biznotes_core.models import SystemSetting

    body = {"chunk_size": 1000, "chunk_overlap": 150, "top_k": 8} if method == "PUT" else None
    res = await client.request(method, path, json=body)

    assert res.status_code == 401, res.text
    assert await db.get(SystemSetting, "rag") is None


async def test_provider_connection_test_rejects_non_admin_before_probe(
    client: httpx.AsyncClient, db, user_a, admin_user, monkeypatch
):
    """리뷰 P2-05·계획 §6: 비관리자 연결 테스트는 403이고 프로바이더 호출 경계에 닿지 않는다.

    라우트 본문은 그대로 돌고, 외부 호출 경계(`probe_connection`)만 관찰 스파이로 둔다.
    관리자 대조군이 같은 경계에 닿는 것으로 스파이 위치가 맞음을 확인한다.
    """
    provider = LLMProvider(
        name=f"probe-{uuid.uuid4().hex[:6]}",
        type="custom",
        base_url="http://probe.invalid/v1",
        enabled=True,
    )
    db.add(provider)
    await db.flush()
    db.add(
        LLMModel(
            provider_id=provider.id,
            model_name="openai/probe-chat",
            display_name="Probe Chat",
            capabilities=["llm"],
            is_default=False,
            enabled=True,
        )
    )
    await db.commit()
    calls: list[str] = []

    async def spy_probe(resolved, capability):
        calls.append(capability)
        return {"ok": True, "detail": "spy"}

    monkeypatch.setattr("biznotes_core.services.gateway.llm.probe_connection", spy_probe)
    path = f"/api/v1/admin/llm/providers/{provider.id}/test"

    user_token = await login(client, user_a.email)
    forbidden = await client.post(path, headers=bearer(user_token))
    assert forbidden.status_code == 403
    assert calls == []

    admin_token = await login(client, admin_user.email)
    allowed = await client.post(path, headers=bearer(admin_token))
    assert allowed.status_code == 200
    assert calls == ["llm"]


async def test_rag_reranker_status_marks_unsupported_provider_unavailable(
    client: httpx.AsyncClient, admin_user
):
    """core-v0.15.0 AC-2.3: mock 프로바이더 리랭커는 리랭크를 못 하므로 옵션 `available:false`."""
    token = await login(client, admin_user.email)
    provider = await client.post(
        "/api/v1/admin/llm/providers",
        json={"name": f"mock-rr-{uuid.uuid4().hex[:6]}", "type": "mock"},
        headers=bearer(token),
    )
    assert provider.status_code == 201
    model = await client.post(
        "/api/v1/admin/llm/models",
        json={
            "provider_id": provider.json()["id"],
            "model_name": "mock-reranker",
            "display_name": "Mock Reranker",
            "capabilities": ["reranker"],
        },
        headers=bearer(token),
    )
    assert model.status_code == 201

    res = await client.get("/api/v1/admin/rag/reranker", headers=bearer(token))

    assert res.status_code == 200
    body = res.json()
    options = {option["key"]: option for option in body["options"]}
    gateway_key = f"gateway:{model.json()['id']}:mock-reranker"
    assert options[gateway_key]["available"] is False
    assert options[gateway_key]["display_name"] == "Mock Reranker"
    assert options["fastembed:BAAI/bge-reranker-base"]["available"] is True
    # 저장값(기본 무접두)은 그대로 로컬 기본으로 해석된다 — 등록만으로 활성 리랭커가 바뀌지 않는다
    assert body["saved_key"] == "BAAI/bge-reranker-base"
    assert body["active_key"] == "fastembed:BAAI/bge-reranker-base"
    assert body["error"] is None


async def test_reranker_capability_is_exclusive_and_first_becomes_default(
    client: httpx.AsyncClient, admin_user
):
    """core-v0.15.0 AC-2.5: `reranker`는 단독 능력(혼합 422), 첫 리랭커만 자동 기본값."""
    token = await login(client, admin_user.email)
    provider = await client.post(
        "/api/v1/admin/llm/providers",
        json={"name": f"mock-rr-{uuid.uuid4().hex[:6]}", "type": "mock"},
        headers=bearer(token),
    )
    provider_id = provider.json()["id"]

    def body(name: str, capabilities: list[str]) -> dict[str, object]:
        return {
            "provider_id": provider_id,
            "model_name": name,
            "display_name": name,
            "capabilities": capabilities,
        }

    mixed = await client.post(
        "/api/v1/admin/llm/models", json=body("mixed", ["llm", "reranker"]), headers=bearer(token)
    )
    assert mixed.status_code == 422

    first = await client.post(
        "/api/v1/admin/llm/models", json=body("rr-1", ["reranker"]), headers=bearer(token)
    )
    second = await client.post(
        "/api/v1/admin/llm/models", json=body("rr-2", ["reranker"]), headers=bearer(token)
    )
    assert (first.status_code, second.status_code) == (201, 201)
    assert first.json()["is_default"] is True
    assert second.json()["is_default"] is False

    patched = await client.patch(
        f"/api/v1/admin/llm/models/{second.json()['id']}",
        json={"capabilities": ["reranker", "llm"]},
        headers=bearer(token),
    )
    assert patched.status_code == 422


async def test_agent_admin_crud_with_validation(client: httpx.AsyncClient, admin_user):
    token = await login(client, admin_user.email)

    # 잘못된 definition (순환) → 422
    res = await client.post(
        "/api/v1/admin/agents",
        json={
            "name": "bad",
            "kind": "workflow",
            "definition": {
                "nodes": [
                    {"id": "a", "type": "llm", "prompt": "x"},
                    {"id": "b", "type": "llm", "prompt": "y"},
                ],
                "edges": [["a", "b"], ["b", "a"]],
            },
        },
        headers=bearer(token),
    )
    assert res.status_code == 422

    # 정상 생성 → 사용자 목록에 즉시 노출
    res = await client.post(
        "/api/v1/admin/agents",
        json={
            "name": f"새 스킬 {uuid.uuid4().hex[:6]}",
            "kind": "skill",
            "definition": {"prompt_template": "요약: {search.context}"},
            "params_schema": {
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"],
            },
        },
        headers=bearer(token),
    )
    assert res.status_code == 201, res.text
    aid = res.json()["id"]

    res = await client.get("/api/v1/agents?kind=skill", headers=bearer(token))
    assert any(a["id"] == aid for a in res.json())

    # disable → 사용자 목록에서 제외 + 버전 증가
    res = await client.patch(
        f"/api/v1/admin/agents/{aid}", json={"enabled": False}, headers=bearer(token)
    )
    assert res.json()["version"] == 2
    res = await client.get("/api/v1/agents?kind=skill", headers=bearer(token))
    assert all(a["id"] != aid for a in res.json())


async def test_reindex_trigger(client: httpx.AsyncClient, admin_user):
    token = await login(client, admin_user.email)
    res = await client.post(
        "/api/v1/admin/rag/reindex", json={"failed_only": True}, headers=bearer(token)
    )
    assert res.status_code == 200
    assert "enqueued" in res.json()


async def test_new_rag_get_endpoints_require_admin(
    client: httpx.AsyncClient, user_a, admin_user
):
    user_token = await login(client, user_a.email)
    unknown_run = uuid.uuid4()
    paths = (
        "/api/v1/admin/rag/embedding",
        "/api/v1/admin/rag/reindex",
        f"/api/v1/admin/rag/reindex/{unknown_run}",
        "/api/v1/admin/rag/reranker",  # core-v0.15.0 #209 — 코어 라우트는 admin_router가 보호
    )
    for path in paths:
        forbidden = await client.get(path, headers=bearer(user_token))
        assert forbidden.status_code == 403, path

    admin_token = await login(client, admin_user.email)
    assert (
        await client.get("/api/v1/admin/rag/embedding", headers=bearer(admin_token))
    ).status_code == 200
    assert (
        await client.get("/api/v1/admin/rag/reranker", headers=bearer(admin_token))
    ).status_code == 200
    assert (
        await client.get("/api/v1/admin/rag/reindex", headers=bearer(admin_token))
    ).status_code == 200
    assert (
        await client.get(
            f"/api/v1/admin/rag/reindex/{unknown_run}", headers=bearer(admin_token)
        )
    ).status_code == 404


async def test_reindex_lifecycle_persists_deduplicates_and_completes(
    client: httpx.AsyncClient, db, admin_user, monkeypatch
):
    from biznotes_core.models import Notebook, Source
    from biznotes_core.services.ingestion.reindex import close_reindex_items
    from biznotes_core.workers.celery_app import celery_app

    notebook = Notebook(owner_id=admin_user.id, title="reindex-lifecycle")
    db.add(notebook)
    await db.flush()
    source = Source(
        notebook_id=notebook.id,
        type="text",
        title="ready-source",
        status="ready",
        chunk_count=1,
    )
    db.add(source)
    await db.commit()

    published: list[tuple[str, list[str]]] = []

    def record_publish(task_name, *, args, **kwargs):
        published.append((task_name, args))

    monkeypatch.setattr(celery_app, "send_task", record_publish)
    token = await login(client, admin_user.email)
    request = {"notebook_id": str(notebook.id)}

    started = await client.post(
        "/api/v1/admin/rag/reindex", json=request, headers=bearer(token)
    )
    assert started.status_code == 200
    assert started.json()["enqueued"] == 1
    assert started.json()["skipped"] == 0
    run_id = started.json()["run_id"]
    assert run_id is not None
    assert len(published) == 1
    assert published[0][1] == [str(source.id)]

    duplicate = await client.post(
        "/api/v1/admin/rag/reindex", json=request, headers=bearer(token)
    )
    assert duplicate.status_code == 200
    assert duplicate.json() == {"enqueued": 0, "skipped": 1, "run_id": None}
    assert len(published) == 1

    await close_reindex_items(db, source.id, ok=True)
    completed = await client.get(
        f"/api/v1/admin/rag/reindex/{run_id}", headers=bearer(token)
    )
    assert completed.status_code == 200
    assert completed.json()["requested"] == 1
    assert completed.json()["queued"] == 0
    assert completed.json()["done"] == 1
    assert completed.json()["failed"] == 0
    assert completed.json()["finished_at"] is not None


async def test_model_cost_per_1k_create_update_and_serialize(
    client: httpx.AsyncClient, admin_user
):
    """[phase-20-design.md CDX20-D2] cost_per_1k가 ModelCreate/Update/Out에 실제로
    직렬화/역직렬화되는지 — 어드민 모델 API 스키마에 필드가 없던 문제의 회귀 테스트."""
    token = await login(client, admin_user.email)
    pid = (
        await client.post(
            "/api/v1/admin/llm/providers",
            json={"name": f"cost-{uuid.uuid4().hex[:6]}", "type": "openai", "api_key": "k"},
            headers=bearer(token),
        )
    ).json()["id"]

    # 등록 시 cost_per_1k 지정
    res = await client.post(
        "/api/v1/admin/llm/models",
        json={
            "provider_id": pid,
            "model_name": f"priced-{uuid.uuid4().hex[:6]}",
            "display_name": "Priced",
            "capabilities": ["llm"],
            "cost_per_1k": {"input": 0.001, "output": 0.002},
        },
        headers=bearer(token),
    )
    assert res.status_code == 201, res.text
    model = res.json()
    assert model["cost_per_1k"] == {"input": 0.001, "output": 0.002}
    mid = model["id"]

    # 미지정 등록 시 빈 dict 기본값
    res2 = await client.post(
        "/api/v1/admin/llm/models",
        json={
            "provider_id": pid,
            "model_name": f"unpriced-{uuid.uuid4().hex[:6]}",
            "display_name": "Unpriced",
            "capabilities": ["llm"],
        },
        headers=bearer(token),
    )
    assert res2.json()["cost_per_1k"] == {}

    # 수정으로 단가 변경
    res3 = await client.patch(
        f"/api/v1/admin/llm/models/{mid}",
        json={"cost_per_1k": {"input": 0.005, "output": 0.01}},
        headers=bearer(token),
    )
    assert res3.json()["cost_per_1k"] == {"input": 0.005, "output": 0.01}

    # 목록 조회에도 반영
    listed = {
        m["id"]: m
        for m in (await client.get("/api/v1/admin/llm/models", headers=bearer(token))).json()
    }
    assert listed[mid]["cost_per_1k"] == {"input": 0.005, "output": 0.01}

    # 알 수 없는 키는 거부
    res4 = await client.patch(
        f"/api/v1/admin/llm/models/{mid}",
        json={"cost_per_1k": {"bogus": 1.0}},
        headers=bearer(token),
    )
    assert res4.status_code == 422


async def test_provider_model_sync_check(client: httpx.AsyncClient, admin_user):
    """[phase-20-design.md §2.3] 동기화 확인 — mock 프로바이더는 discovery 미지원이라
    502를 반환해야 한다(자동 삭제 없음 원칙과 별개로, discovery 자체의 에러 경로 확인)."""
    token = await login(client, admin_user.email)
    pid = (
        await client.post(
            "/api/v1/admin/llm/providers",
            json={"name": f"sync-{uuid.uuid4().hex[:6]}", "type": "mock"},
            headers=bearer(token),
        )
    ).json()["id"]
    res = await client.get(
        f"/api/v1/admin/llm/providers/{pid}/models/sync-check", headers=bearer(token)
    )
    assert res.status_code == 502


async def test_provider_model_sync_check_still_available_flags(
    client: httpx.AsyncClient, admin_user, monkeypatch
):
    """[CDX20B-05] discover_provider_models가 반환하는 목록과 등록된 모델을 실제로 대조해
    still_available true/false가 올바르게 갈리는지 — 사라진 모델도 삭제되지 않는지 확인."""
    token = await login(client, admin_user.email)
    pid = (
        await client.post(
            "/api/v1/admin/llm/providers",
            json={"name": f"sync-real-{uuid.uuid4().hex[:6]}", "type": "openai", "api_key": "k"},
            headers=bearer(token),
        )
    ).json()["id"]

    still_here = (
        await client.post(
            "/api/v1/admin/llm/models",
            json={
                "provider_id": pid,
                "model_name": "gpt-still-here",
                "display_name": "Still Here",
                "capabilities": ["llm"],
            },
            headers=bearer(token),
        )
    ).json()
    gone = (
        await client.post(
            "/api/v1/admin/llm/models",
            json={
                "provider_id": pid,
                "model_name": "gpt-gone",
                "display_name": "Gone",
                "capabilities": ["llm"],
            },
            headers=bearer(token),
        )
    ).json()

    async def _fake_discover(provider):
        return [
            {
                "model_name": "gpt-still-here",
                "raw_id": "gpt-still-here",
                "display_name": "gpt-still-here",
                "capabilities": ["llm"],
            }
        ]

    monkeypatch.setattr(
        "biznotes_core.services.gateway.discovery.discover_provider_models", _fake_discover
    )

    res = await client.get(
        f"/api/v1/admin/llm/providers/{pid}/models/sync-check", headers=bearer(token)
    )
    assert res.status_code == 200
    by_id = {m["id"]: m for m in res.json()}
    assert by_id[still_here["id"]]["still_available"] is True
    assert by_id[gone["id"]]["still_available"] is False

    # 자동 삭제되지 않음 — 목록 조회에서 여전히 존재
    listed_ids = {
        m["id"]
        for m in (await client.get("/api/v1/admin/llm/models", headers=bearer(token))).json()
    }
    assert gone["id"] in listed_ids


async def test_tool_policies_lists_side_effect_and_auto_approve_scan(
    db, client: httpx.AsyncClient, admin_user, user_a
):
    """[phase-19-design.md §6, 19c] 도구별 side_effect 정적 분류 + workflow agent의
    react_agent 노드가 설정한 auto_approve_tools를 스캔해 집계한다."""
    from biznotes_core.models import Agent

    target_id = str(uuid.uuid4())
    agent = Agent(
        name=f"policy-scan-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        icon="🤖",
        definition={
            "nodes": [
                {
                    "id": "r",
                    "type": "react_agent",
                    "goal": "x",
                    "auto_approve_tools": {
                        "http_request": {"domains": ["api.example.com"], "methods": ["GET"]},
                        "call_agent": {"agent_ids": [target_id]},
                    },
                }
            ],
            "edges": [],
        },
        params_schema={},
        scope="system",
        owner_id=user_a.id,
        enabled=True,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)

    token = await login(client, admin_user.email)
    res = await client.get("/api/v1/admin/agents/tool-policies", headers=bearer(token))
    assert res.status_code == 200
    body = {p["tool_name"]: p for p in res.json()}

    # 정적 side_effect 분류(tool_registry.TOOL_SIDE_EFFECT와 일치).
    assert body["rag_search"]["side_effect"] is False
    assert body["http_request"]["side_effect"] is True
    assert body["call_agent"]["side_effect"] is True

    # http_request 자동승인 정책이 이 워크플로우로부터 집계되어야 한다.
    http_refs = body["http_request"]["auto_approved_by"]
    assert any(
        r["agent_id"] == str(agent.id) and r["node_id"] == "r"
        and r["policy"]["domains"] == ["api.example.com"]
        for r in http_refs
    )
    call_agent_refs = body["call_agent"]["auto_approved_by"]
    assert any(
        r["agent_id"] == str(agent.id) and r["policy"]["agent_ids"] == [target_id]
        for r in call_agent_refs
    )
    # 정책이 없는 도구는 빈 리스트.
    assert body["rag_search"]["auto_approved_by"] == []


async def test_tool_policies_requires_admin_role(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)
    res = await client.get("/api/v1/admin/agents/tool-policies", headers=bearer(token))
    assert res.status_code == 403


async def test_tool_policies_includes_unregistered_mcp_tools(
    db, client: httpx.AsyncClient, admin_user, user_a
):
    """[코드 리뷰 지적] TOOL_SIDE_EFFECT 정적 맵에 없는 도구명(예: MCP 자율
    디스커버리 도구 mcp_<8hex>_<tool>)에도 auto_approve_tools 정책이 걸려
    있으면, 조용히 누락되지 않고 안전 기본값(side_effect=True)으로 목록에
    포함되어야 한다(tool_registry.is_side_effect_tool 기본값과 일치)."""
    from biznotes_core.models import Agent

    mcp_tool_name = "mcp_1a2b3c4d_search_repo"
    agent = Agent(
        name=f"policy-scan-mcp-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        icon="🤖",
        definition={
            "nodes": [
                {
                    "id": "r",
                    "type": "react_agent",
                    "goal": "x",
                    "auto_approve_tools": {mcp_tool_name: {"anything": True}},
                }
            ],
            "edges": [],
        },
        params_schema={},
        scope="system",
        owner_id=user_a.id,
        enabled=True,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)

    token = await login(client, admin_user.email)
    res = await client.get("/api/v1/admin/agents/tool-policies", headers=bearer(token))
    assert res.status_code == 200
    body = {p["tool_name"]: p for p in res.json()}

    assert mcp_tool_name in body
    assert body[mcp_tool_name]["side_effect"] is True
    refs = body[mcp_tool_name]["auto_approved_by"]
    assert any(
        r["agent_id"] == str(agent.id) and r["node_id"] == "r" and r["policy"] == {"anything": True}
        for r in refs
    )


# --- R3-3/R33CR-03: /admin/llm/providers/{id}/models/import HTTP 경로 ---


async def _make_provider(client: httpx.AsyncClient, token: str, name: str) -> str:
    res = await client.post(
        "/api/v1/admin/llm/providers",
        json={"name": name, "type": "mock", "enabled": True},
        headers=bearer(token),
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def test_model_import_없는_프로바이더는_404(client: httpx.AsyncClient, admin_user):
    token = await login(client, admin_user.email)
    item = {
        "model_name": "m",
        "display_name": "M",
        "capabilities": ["llm"],
        "context_window": 8192,
    }
    res = await client.post(
        f"/api/v1/admin/llm/providers/{uuid.uuid4()}/models/import",
        json={"items": [item]},
        headers=bearer(token),
    )
    assert res.status_code == 404


async def test_model_import_빈_목록은_422(client: httpx.AsyncClient, admin_user):
    """items min_length=1 — Pydantic 검증이 404 조회보다 먼저 걸린다."""
    token = await login(client, admin_user.email)
    res = await client.post(
        f"/api/v1/admin/llm/providers/{uuid.uuid4()}/models/import",
        json={"items": []},
        headers=bearer(token),
    )
    assert res.status_code == 422


async def test_model_import_중복은_건너뛰고_DB에_반영된다(client: httpx.AsyncClient, admin_user):
    token = await login(client, admin_user.email)
    pid = await _make_provider(client, token, "imp-dup")

    item = {
        "model_name": "gpt-x",
        "display_name": "X",
        "capabilities": ["llm"],
        "context_window": 8192,
    }
    first = await client.post(
        f"/api/v1/admin/llm/providers/{pid}/models/import",
        json={"items": [item]},
        headers=bearer(token),
    )
    assert first.status_code == 200, first.text
    assert first.json() == {"created": 1, "skipped": []}

    # 같은 model_name 재등록 → skip
    second = await client.post(
        f"/api/v1/admin/llm/providers/{pid}/models/import",
        json={"items": [item]},
        headers=bearer(token),
    )
    assert second.json() == {"created": 0, "skipped": ["gpt-x"]}

    # 커밋 후 실제로 조회된다
    listed = await client.get("/api/v1/admin/llm/models", headers=bearer(token))
    assert [m["model_name"] for m in listed.json() if m["model_name"] == "gpt-x"] == ["gpt-x"]


async def test_model_import_한_배치에_임베딩은_하나만(client: httpx.AsyncClient, admin_user):
    """서비스 규칙이 HTTP 경로를 통해서도 적용되는지 (라우터 파싱 + 커밋 포함)."""
    token = await login(client, admin_user.email)
    pid = await _make_provider(client, token, "imp-emb")

    def emb(name: str) -> dict:
        return {
            "model_name": name,
            "display_name": name,
            "capabilities": ["embedding"],
            "context_window": 512,
        }

    res = await client.post(
        f"/api/v1/admin/llm/providers/{pid}/models/import",
        json={"items": [emb("emb-a"), emb("emb-b")]},
        headers=bearer(token),
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"created": 1, "skipped": ["emb-b"]}

    listed = await client.get("/api/v1/admin/llm/models", headers=bearer(token))
    embs = [m for m in listed.json() if "embedding" in m["capabilities"]]
    assert len(embs) == 1
    assert embs[0]["is_default"] is True, "임베딩은 등록 즉시 default"


# --- backlog-p2 Phase 2: 능력별 default DB 보장 ---


class _RecordBarrier:
    """두 라우터 호출이 commit 직전까지 진행하도록 감사 기록 지점에서 동기화한다."""

    def __init__(self) -> None:
        self.arrivals = 0
        self.ready = asyncio.Event()

    async def __call__(self, *args, **kwargs) -> None:
        await record_audit(*args, **kwargs)
        self.arrivals += 1
        if self.arrivals == 2:
            self.ready.set()
        await asyncio.wait_for(self.ready.wait(), timeout=5)


def _embedding_create(provider_id: uuid.UUID, name: str) -> ModelCreate:
    return ModelCreate(
        provider_id=provider_id,
        model_name=name,
        display_name=name,
        capabilities=["embedding"],
    )


def _embedding_import(name: str) -> ModelImport:
    return ModelImport(
        items=[
            {
                "model_name": name,
                "display_name": name,
                "capabilities": ["embedding"],
                "context_window": 512,
            }
        ]
    )


async def _run_embedding_write(
    kind: str,
    session: AsyncSession,
    provider_id: uuid.UUID,
    admin_user,
    name: str,
) -> int:
    try:
        if kind == "create":
            await llm_api.create_model(_embedding_create(provider_id, name), session, admin_user)
        else:
            await llm_api.import_models(provider_id, _embedding_import(name), session, admin_user)
    except HTTPException as exc:
        return exc.status_code
    return 200


async def _run_set_default(session: AsyncSession, model_id: uuid.UUID, admin_user) -> int:
    try:
        await llm_api.set_default_model(model_id, session, admin_user)
    except HTTPException as exc:
        return exc.status_code
    return 200


async def _assert_loser_session_reusable(
    sessions: tuple[AsyncSession, AsyncSession], statuses: list[int]
) -> None:
    loser = sessions[statuses.index(409)]
    assert (await loser.execute(select(literal(1)))).scalar_one() == 1


async def test_set_default_동시_경합은_한_행만_남기고_패자_세션을_복구한다(
    db, admin_user, monkeypatch
) -> None:
    provider = LLMProvider(name=f"race-set-{uuid.uuid4().hex[:6]}", type="mock")
    db.add(provider)
    await db.flush()
    first = LLMModel(
        provider_id=provider.id,
        model_name="first",
        display_name="First",
        capabilities=["llm"],
    )
    second = LLMModel(
        provider_id=provider.id,
        model_name="second",
        display_name="Second",
        capabilities=["llm"],
    )
    db.add_all([first, second])
    await db.commit()

    monkeypatch.setattr(llm_api, "record", _RecordBarrier())
    async with async_session_factory() as session_a, async_session_factory() as session_b:
        statuses = list(
            await asyncio.gather(
                _run_set_default(session_a, first.id, admin_user),
                _run_set_default(session_b, second.id, admin_user),
            )
        )
        assert sorted(statuses) == [200, 409]
        await _assert_loser_session_reusable((session_a, session_b), statuses)

    async with async_session_factory() as verify:
        defaults = (
            (
                await verify.execute(
                    select(LLMModel).where(
                        LLMModel.is_default.is_(True),
                        LLMModel.capabilities.overlap(["llm"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(defaults) == 1


@pytest.mark.parametrize(
    ("left", "right"),
    [("create", "create"), ("import", "import"), ("create", "import")],
)
async def test_embedding_등록_동시_경합은_409와_재사용_가능한_세션을_보장한다(
    db, admin_user, monkeypatch, left: str, right: str
) -> None:
    provider = LLMProvider(name=f"race-emb-{uuid.uuid4().hex[:6]}", type="mock")
    db.add(provider)
    await db.commit()

    monkeypatch.setattr(llm_api, "record", _RecordBarrier())
    async with async_session_factory() as session_a, async_session_factory() as session_b:
        statuses = list(
            await asyncio.gather(
                _run_embedding_write(left, session_a, provider.id, admin_user, "embedding-a"),
                _run_embedding_write(right, session_b, provider.id, admin_user, "embedding-b"),
            )
        )
        assert sorted(statuses) == [200, 409]
        await _assert_loser_session_reusable((session_a, session_b), statuses)

    async with async_session_factory() as verify:
        defaults = (
            (
                await verify.execute(
                    select(LLMModel).where(
                        LLMModel.is_default.is_(True),
                        LLMModel.capabilities.overlap(["embedding"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(defaults) == 1


async def test_commit_helper는_default_충돌만_rollback후_409로_바꾼다() -> None:
    unique_error = IntegrityError("statement", {}, Exception("uq_llm_models_default_llm"))
    unique_db = AsyncMock(spec=AsyncSession)
    unique_db.commit.side_effect = unique_error

    with pytest.raises(HTTPException) as caught:
        await llm_api._commit_or_409(unique_db)

    assert caught.value.status_code == 409
    unique_db.rollback.assert_awaited_once()

    foreign_error = IntegrityError("statement", {}, Exception("provider_id_fkey"))
    foreign_db = AsyncMock(spec=AsyncSession)
    foreign_db.commit.side_effect = foreign_error
    with pytest.raises(IntegrityError) as reraised:
        await llm_api._commit_or_409(foreign_db)
    assert reraised.value is foreign_error
    foreign_db.rollback.assert_awaited_once()


async def test_commit_helper는_실제_FK_integrity_error를_재발생하고_세션을_복구한다() -> None:
    async with async_session_factory() as session:
        session.add(
            LLMModel(
                provider_id=uuid.uuid4(),
                model_name="orphan",
                display_name="Orphan",
                capabilities=["llm"],
            )
        )
        with pytest.raises(IntegrityError):
            await llm_api._commit_or_409(session)
        assert (await session.execute(select(literal(1)))).scalar_one() == 1


async def _create_http_model(
    client: httpx.AsyncClient,
    token: str,
    provider_id: str,
    name: str,
    capabilities: list[str],
) -> dict:
    response = await client.post(
        "/api/v1/admin/llm/models",
        json={
            "provider_id": provider_id,
            "model_name": name,
            "display_name": name,
            "capabilities": capabilities,
        },
        headers=bearer(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_default_모델_capabilities_충돌은_409이고_행은_변하지_않는다(
    client: httpx.AsyncClient, admin_user
) -> None:
    token = await login(client, admin_user.email)
    provider_id = await _make_provider(client, token, f"patch-conflict-{uuid.uuid4().hex[:6]}")
    llm = await _create_http_model(client, token, provider_id, "patch-llm", ["llm"])
    vlm = await _create_http_model(client, token, provider_id, "patch-vlm", ["vlm"])
    await client.post(f"/api/v1/admin/llm/models/{llm['id']}/set-default", headers=bearer(token))
    await client.post(f"/api/v1/admin/llm/models/{vlm['id']}/set-default", headers=bearer(token))

    response = await client.patch(
        f"/api/v1/admin/llm/models/{llm['id']}",
        json={"capabilities": ["llm", "vlm"]},
        headers=bearer(token),
    )
    assert response.status_code == 409

    models = {
        model["id"]: model
        for model in (await client.get("/api/v1/admin/llm/models", headers=bearer(token))).json()
    }
    assert models[llm["id"]]["capabilities"] == ["llm"]


async def test_non_default_모델은_default와_겹치는_capabilities로_수정할_수_있다(
    client: httpx.AsyncClient, admin_user
) -> None:
    token = await login(client, admin_user.email)
    provider_id = await _make_provider(client, token, f"patch-nondefault-{uuid.uuid4().hex[:6]}")
    default = await _create_http_model(client, token, provider_id, "default-llm", ["llm"])
    await client.post(
        f"/api/v1/admin/llm/models/{default['id']}/set-default", headers=bearer(token)
    )
    candidate = await _create_http_model(client, token, provider_id, "candidate", ["vlm"])

    response = await client.patch(
        f"/api/v1/admin/llm/models/{candidate['id']}",
        json={"capabilities": ["vlm", "llm"]},
        headers=bearer(token),
    )

    assert response.status_code == 200, response.text
    assert set(response.json()["capabilities"]) == {"vlm", "llm"}
    assert response.json()["is_default"] is False


async def test_non_default_모델에_embedding_추가는_409이고_행은_변하지_않는다(
    client: httpx.AsyncClient, admin_user
) -> None:
    token = await login(client, admin_user.email)
    provider_id = await _make_provider(client, token, f"patch-embedding-{uuid.uuid4().hex[:6]}")
    await _create_http_model(client, token, provider_id, "existing-embedding", ["embedding"])
    candidate = await _create_http_model(client, token, provider_id, "candidate-vlm", ["vlm"])

    response = await client.patch(
        f"/api/v1/admin/llm/models/{candidate['id']}",
        json={"capabilities": ["vlm", "embedding"]},
        headers=bearer(token),
    )

    assert response.status_code == 409
    assert response.json()["detail"].startswith("임베딩 모델은 전역 1개입니다")
    models = {
        model["id"]: model
        for model in (await client.get("/api/v1/admin/llm/models", headers=bearer(token))).json()
    }
    assert models[candidate["id"]]["capabilities"] == ["vlm"]
    assert models[candidate["id"]]["is_default"] is False


async def test_default_모델에서_능력_제거는_해당_default_슬롯을_해제한다(
    client: httpx.AsyncClient, admin_user
) -> None:
    token = await login(client, admin_user.email)
    provider_id = await _make_provider(client, token, f"patch-release-{uuid.uuid4().hex[:6]}")
    omni = await _create_http_model(client, token, provider_id, "release-omni", ["llm", "vlm"])
    vlm = await _create_http_model(client, token, provider_id, "release-vlm", ["vlm"])
    await client.post(f"/api/v1/admin/llm/models/{omni['id']}/set-default", headers=bearer(token))

    response = await client.patch(
        f"/api/v1/admin/llm/models/{omni['id']}",
        json={"capabilities": ["llm"]},
        headers=bearer(token),
    )
    assert response.status_code == 200, response.text

    released = await client.post(
        f"/api/v1/admin/llm/models/{vlm['id']}/set-default", headers=bearer(token)
    )
    assert released.status_code == 200, released.text
    defaults = [
        model
        for model in (await client.get("/api/v1/admin/llm/models", headers=bearer(token))).json()
        if model["is_default"]
    ]
    assert {tuple(model["capabilities"]) for model in defaults} == {("llm",), ("vlm",)}


async def test_변경된_LLM_관리_엔드포인트는_비관리자를_403으로_거부한다(
    client: httpx.AsyncClient, user_a
) -> None:
    token = await login(client, user_a.email)
    provider_id = str(uuid.uuid4())
    model_id = str(uuid.uuid4())
    cases = [
        (
            "POST",
            "/api/v1/admin/llm/models",
            {
                "provider_id": provider_id,
                "model_name": "forbidden",
                "display_name": "Forbidden",
                "capabilities": ["llm"],
            },
        ),
        (
            "POST",
            f"/api/v1/admin/llm/providers/{provider_id}/models/import",
            {
                "items": [
                    {
                        "model_name": "forbidden",
                        "display_name": "Forbidden",
                        "capabilities": ["llm"],
                    }
                ]
            },
        ),
        ("PATCH", f"/api/v1/admin/llm/models/{model_id}", {"capabilities": ["llm"]}),
        ("POST", f"/api/v1/admin/llm/models/{model_id}/set-default", None),
        (
            "POST",
            "/api/v1/admin/llm/providers",
            {"name": "forbidden", "type": "openai", "embedding_max_concurrency": 2},
        ),
        (
            "PATCH",
            f"/api/v1/admin/llm/providers/{provider_id}",
            {"embedding_max_concurrency": 2},
        ),
        ("GET", f"/api/v1/admin/llm/providers/{provider_id}/embedding-gate", None),
    ]

    for method, path, body in cases:
        response = await client.request(method, path, json=body, headers=bearer(token))
        assert response.status_code == 403, (method, path, response.text)


# ---------- 어드민 저장 경로의 정책 배선 (계획 AC-25·AC-29·AC-31, §4.5) ----------

_MCP_NODE_DEF = {"nodes": [{"id": "m", "type": "mcp_tool", "server_id": "s"}], "edges": []}


async def _admin_workflow(client: httpx.AsyncClient, token: str, scope: str = "system") -> str:
    res = await client.post(
        "/api/v1/admin/agents",
        json={
            "name": f"adm-{uuid.uuid4().hex[:6]}",
            "kind": "workflow",
            "scope": scope,
            "definition": {"nodes": [{"id": "g", "type": "llm", "prompt": "x"}], "edges": []},
        },
        headers=bearer(token),
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def test_t10_admin_patch_rejects_llm_node_side_effect_tool(
    client: httpx.AsyncClient, admin_user
):
    """T-10: PATCH /admin/agents/{id}(workflow 정의 수정)의 llm 노드 mcp_call → 422."""
    token = await login(client, admin_user.email)
    agent_id = await _admin_workflow(client, token)
    res = await client.patch(
        f"/api/v1/admin/agents/{agent_id}",
        json={
            "definition": {
                "nodes": [{"id": "g", "type": "llm", "prompt": "x", "tools": ["mcp_call"]}],
                "edges": [],
            }
        },
        headers=bearer(token),
    )
    assert res.status_code == 422, res.text
    assert "mcp_call" in res.json()["detail"]


async def test_t11_admin_patch_accepts_non_side_effect_tools(
    client: httpx.AsyncClient, admin_user
):
    """T-11(비하강): 어드민 경로도 읽기 전용 도구·mcp 토큰·미등록 이름은 통과 → 200."""
    token = await login(client, admin_user.email)
    agent_id = await _admin_workflow(client, token)
    tools = [
        "rag_search",
        "web_search",
        "source_load",
        "run_skill",
        "activate_skill",
        "read_skill_file",
        "mcp",
        "not_a_registered_tool",
    ]
    res = await client.patch(
        f"/api/v1/admin/agents/{agent_id}",
        json={
            "definition": {
                "nodes": [{"id": "g", "type": "llm", "prompt": "x", "tools": tools}],
                "edges": [],
            }
        },
        headers=bearer(token),
    )
    assert res.status_code == 200, res.text


async def test_t12_admin_patch_ignores_non_llm_and_non_list_tools(
    client: httpx.AsyncClient, admin_user
):
    """T-12(어드민): 비-llm 노드의 tools·비리스트 tools·빈 리스트는 무시 → 200."""
    token = await login(client, admin_user.email)
    agent_id = await _admin_workflow(client, token)
    res = await client.patch(
        f"/api/v1/admin/agents/{agent_id}",
        json={
            "definition": {
                "nodes": [
                    {"id": "c", "type": "constant", "value": "v", "tools": ["mcp_call"]},
                    {"id": "g", "type": "llm", "prompt": "x", "tools": "http_request"},
                    {"id": "h", "type": "llm", "prompt": "y", "tools": []},
                ],
                "edges": [],
            }
        },
        headers=bearer(token),
    )
    assert res.status_code == 200, res.text


async def test_t17_admin_system_scope_keeps_mcp_tool(client: httpx.AsyncClient, admin_user):
    """T-17(비하강): 신뢰 scope(system) 정의의 mcp_tool 노드는 통과 → 200.

    제품 `kind="mcp"` 에이전트도 `kind_to_nodes`가 mcp_tool 노드로 정규화하므로 같은 경로다.
    """
    token = await login(client, admin_user.email)
    agent_id = await _admin_workflow(client, token, scope="system")
    res = await client.patch(
        f"/api/v1/admin/agents/{agent_id}",
        json={
            "definition": {
                "nodes": [
                    {"id": "m", "type": "mcp_tool", "server_id": "s", "tool_name": "t"},
                    {"id": "r", "type": "react_agent", "goal": "x", "allowed_tools": ["mcp_call"]},
                ],
                "edges": [["m", "r"]],
            }
        },
        headers=bearer(token),
    )
    assert res.status_code == 200, res.text

    created = await client.post(
        "/api/v1/admin/agents",
        json={
            "name": f"mcp-{uuid.uuid4().hex[:6]}",
            "kind": "mcp",
            "definition": {"server_id": str(uuid.uuid4()), "tool_name": "search"},
        },
        headers=bearer(token),
    )
    assert created.status_code == 201, created.text  # scope 기본값 system


@pytest.mark.parametrize("scope", ["group", "user"])
async def test_t17b_admin_untrusted_scope_rejects_mcp_tool(
    client: httpx.AsyncClient, admin_user, scope
):
    """T-17b: 어드민이라도 정의 scope가 신뢰 집합(system) 밖이면 mcp_tool은 422."""
    token = await login(client, admin_user.email)
    res = await client.post(
        "/api/v1/admin/agents",
        json={
            "name": f"mcp-{uuid.uuid4().hex[:6]}",
            "kind": "workflow",
            "scope": scope,
            "definition": _MCP_NODE_DEF,
        },
        headers=bearer(token),
    )
    assert res.status_code == 422, res.text
    assert f"scope {scope}" in res.json()["detail"]

    mcp_kind = await client.post(
        "/api/v1/admin/agents",
        json={
            "name": f"mcp-{uuid.uuid4().hex[:6]}",
            "kind": "mcp",
            "scope": scope,
            "definition": {"server_id": str(uuid.uuid4()), "tool_name": "search"},
        },
        headers=bearer(token),
    )
    assert mcp_kind.status_code == 422, mcp_kind.text  # kind=mcp → mcp_tool 노드로 정규화


async def test_t18_non_admin_cannot_create_admin_agent(client: httpx.AsyncClient, user_a):
    """T-18: 일반 사용자 POST /admin/agents → 403(require_admin 라우터 의존성)."""
    token = await login(client, user_a.email)
    res = await client.post(
        "/api/v1/admin/agents",
        json={
            "name": "x",
            "kind": "workflow",
            "definition": _MCP_NODE_DEF,
        },
        headers=bearer(token),
    )
    assert res.status_code == 403


async def test_t20_admin_patch_rejects_reserved_resume_key(client: httpx.AsyncClient, admin_user):
    """T-20: 어드민도 `_resume`을 저장할 수 없다 → 422(워커만 런타임 사본에 주입한다)."""
    token = await login(client, admin_user.email)
    agent_id = await _admin_workflow(client, token)
    res = await client.patch(
        f"/api/v1/admin/agents/{agent_id}",
        json={
            "definition": {
                "nodes": [{"id": "r", "type": "react_agent", "goal": "x", "_resume": {}}],
                "edges": [],
            }
        },
        headers=bearer(token),
    )
    assert res.status_code == 422, res.text
    assert "r: _resume" in res.json()["detail"]


async def test_t20_tool_kind_http_resume_is_rejected(client: httpx.AsyncClient, admin_user):
    """AC-31 (2): tool·plugin kind의 `definition.http._resume`도 `**http` 전개 뒤 헬퍼가 잡는다."""
    token = await login(client, admin_user.email)
    res = await client.post(
        "/api/v1/admin/agents",
        json={
            "name": f"tool-{uuid.uuid4().hex[:6]}",
            "kind": "tool",
            "definition": {"http": {"url": "https://example.com", "_resume": {}}},
        },
        headers=bearer(token),
    )
    assert res.status_code == 422, res.text
    assert "call: _resume" in res.json()["detail"]


async def test_t21_admin_allows_skill_preactivate(client: httpx.AsyncClient, admin_user):
    """T-21(비하강, 어드민): `_skill_preactivate`는 통과 → 200."""
    token = await login(client, admin_user.email)
    agent_id = await _admin_workflow(client, token)
    res = await client.patch(
        f"/api/v1/admin/agents/{agent_id}",
        json={
            "definition": {
                "nodes": [
                    {"id": "r", "type": "react_agent", "goal": "x", "_skill_preactivate": ["s"]}
                ],
                "edges": [],
            }
        },
        headers=bearer(token),
    )
    assert res.status_code == 200, res.text


async def test_t13_non_admin_cannot_trigger_reindex(client: httpx.AsyncClient, user_a):
    """T-13: 일반 사용자 POST /admin/rag/reindex → 403(쓰기 경로의 비인가 케이스)."""
    token = await login(client, user_a.email)
    res = await client.post(
        "/api/v1/admin/rag/reindex", json={"failed_only": True}, headers=bearer(token)
    )
    assert res.status_code == 403


async def test_admin_save_rejects_missing_or_wrong_kind_subagent_target(
    client: httpx.AsyncClient, admin_user
):
    """P0-26: 어드민 저장 경로도 agent_call 대상의 존재·kind·자기참조를 검사한다(422)."""
    token = await login(client, admin_user.email)
    missing = await client.post(
        "/api/v1/admin/agents",
        json={
            "name": f"caller-{uuid.uuid4().hex[:6]}",
            "kind": "workflow",
            "definition": {
                "nodes": [{"id": "c", "type": "agent_call", "agent_id": str(uuid.uuid4())}],
                "edges": [],
            },
        },
        headers=bearer(token),
    )
    assert missing.status_code == 422, missing.text
    assert "호출할 수 없습니다" in missing.json()["detail"]

    tool = await client.post(
        "/api/v1/admin/agents",
        json={
            "name": f"tool-{uuid.uuid4().hex[:6]}",
            "kind": "tool",
            "definition": {"http": {"url": "https://example.com"}},
        },
        headers=bearer(token),
    )
    assert tool.status_code == 201, tool.text
    wrong_kind = await client.post(
        "/api/v1/admin/agents",
        json={
            "name": f"caller-{uuid.uuid4().hex[:6]}",
            "kind": "workflow",
            "definition": {
                "nodes": [{"id": "c", "type": "agent_call", "agent_id": tool.json()["id"]}],
                "edges": [],
            },
        },
        headers=bearer(token),
    )
    assert wrong_kind.status_code == 422, wrong_kind.text

    target_id = await _admin_workflow(client, token)
    ok = await client.post(
        "/api/v1/admin/agents",
        json={
            "name": f"caller-{uuid.uuid4().hex[:6]}",
            "kind": "workflow",
            "definition": {
                "nodes": [{"id": "c", "type": "agent_call", "agent_id": target_id}],
                "edges": [],
            },
        },
        headers=bearer(token),
    )
    assert ok.status_code == 201, ok.text

    self_ref = await client.patch(
        f"/api/v1/admin/agents/{ok.json()['id']}",
        json={
            "definition": {
                "nodes": [{"id": "c", "type": "agent_call", "agent_id": ok.json()["id"]}],
                "edges": [],
            }
        },
        headers=bearer(token),
    )
    assert self_ref.status_code == 422, self_ref.text


async def test_mcp_health_check_requires_admin(client: httpx.AsyncClient, db, user_a, monkeypatch):
    """권한 표 E-B3(core-v0.11.0-upgrade) — 일반 사용자의 MCP 헬스체크는 403, 서버에 닿지 않는다.

    코어 v0.10.14 #147·#146으로 응답 키(`title`·`annotations`)가 늘었을 뿐 권한은 그대로다.
    """
    from biznotes_core.models import MCPServer

    server = MCPServer(
        name=f"hc-{uuid.uuid4().hex[:6]}",
        transport="streamable_http",
        endpoint_or_command="https://mcp.example/hc",
        enabled=True,
    )
    db.add(server)
    await db.commit()
    probes: list[str] = []

    async def fake_list(s):
        probes.append(s.name)
        return []

    monkeypatch.setattr("biznotes_core.services.agents.mcp_client.list_tools", fake_list)
    token = await login(client, user_a.email)

    res = await client.post(
        f"/api/v1/admin/mcp-servers/{server.id}/health-check", headers=bearer(token)
    )

    assert res.status_code == 403
    assert probes == []


# ---- core-v0.20.0-upgrade AC-1.8 ⑤⑥⑩ⓐ ----


async def _mock_model(client: httpx.AsyncClient, token: str, name: str) -> dict:
    provider_id = await _make_provider(client, token, f"{name}-{uuid.uuid4().hex[:6]}")
    res = await client.post(
        "/api/v1/admin/llm/models",
        json={
            "provider_id": provider_id,
            "model_name": name,
            "display_name": name,
            "capabilities": ["llm"],
            "context_window": 8192,
        },
        headers=bearer(token),
    )
    assert res.status_code == 201, res.text
    return res.json()


async def test_admin_model_patch_explicit_null(client: httpx.AsyncClient, db, admin_user):
    """관리자 모델 `display_name`·`enabled`·`capabilities`의 명시적 null은 422(문자열 detail)·행
    불변이고, `context_window` null은 해제(200)다(코어 v0.18.0 #267, AC-1.8 ⑤)."""
    token = await login(client, admin_user.email)
    model = await _mock_model(client, token, "null-model")
    url = f"/api/v1/admin/llm/models/{model['id']}"

    for field in ("display_name", "enabled", "capabilities"):
        res = await client.patch(url, json={field: None}, headers=bearer(token))
        assert res.status_code == 422, res.text
        assert res.json() == {"detail": f"null로 바꿀 수 없는 필드: {field}"}
    row = await db.get(LLMModel, uuid.UUID(model["id"]), populate_existing=True)
    assert (row.display_name, row.enabled, row.capabilities, row.context_window) == (
        "null-model",
        True,
        ["llm"],
        8192,
    )

    res = await client.patch(url, json={"context_window": None}, headers=bearer(token))
    assert res.status_code == 200, res.text
    assert res.json()["context_window"] is None


async def test_admin_model_unknown_capability_is_literal_error_422(
    client: httpx.AsyncClient, db, admin_user, record_property
):
    """미지 능력은 생성·수정·가져오기 모두 항목별 `literal_error` 422이고 행이 생기거나 바뀌지
    않는다. 정상 6값은 저장·응답 왕복이 같다(코어 v0.18.0 #218, AC-1.8 ⑥). 422 본문에는 입력값이
    실리지 않는다(관리자 라우터의 `RedactedValidationRoute`)."""
    token = await login(client, admin_user.email)
    model = await _mock_model(client, token, "cap-model")
    provider_id = model["provider_id"]

    created = await client.post(
        "/api/v1/admin/llm/models",
        json={
            "provider_id": provider_id,
            "model_name": "cap-video",
            "display_name": "cap-video",
            "capabilities": ["llm", "video"],
        },
        headers=bearer(token),
    )
    patched = await client.patch(
        f"/api/v1/admin/llm/models/{model['id']}",
        json={"capabilities": ["llm", "video"]},
        headers=bearer(token),
    )
    imported = await client.post(
        f"/api/v1/admin/llm/providers/{provider_id}/models/import",
        json={
            "items": [{"model_name": "cap-import", "display_name": "I", "capabilities": ["video"]}]
        },
        headers=bearer(token),
    )
    record_property("capabilities_422_create_body", created.text)
    for res in (created, patched, imported):
        assert res.status_code == 422, res.text
        detail = res.json()["detail"]
        assert detail and {item["type"] for item in detail} == {"literal_error"}
        assert "video" not in res.text  # 입력값 비노출

    names = set(
        await db.scalars(
            select(LLMModel.model_name).where(LLMModel.provider_id == uuid.UUID(provider_id))
        )
    )
    assert names == {"cap-model"}
    row = await db.get(LLMModel, uuid.UUID(model["id"]), populate_existing=True)
    assert row.capabilities == ["llm"]

    for modality in ("llm", "vlm", "embedding", "reranker", "image"):
        res = await client.patch(
            f"/api/v1/admin/llm/models/{model['id']}",
            json={"capabilities": [modality]},
            headers=bearer(token),
        )
        assert res.status_code == 200, res.text
        assert res.json()["capabilities"] == [modality]
        listed = await client.get("/api/v1/admin/llm/models", headers=bearer(token))
        assert next(m for m in listed.json() if m["id"] == model["id"])["capabilities"] == [
            modality
        ]


async def test_admin_roster_sequential_deactivate_then_delete(
    client: httpx.AsyncClient, db, admin_user
):
    """명부 잠금 경로가 제품 DB·연결에서 동작한다(코어 v0.18.0 #241·v0.19.0 #276, AC-1.8 ⑩ⓐ).

    활성 관리자 A·B 중 A가 B를 비활성화 → 200·`user.update` 감사 1행, 이어서 B 삭제 → 204·
    `user.delete` 감사 1행. 활성 관리자는 A 하나로 남는다. 경합은 `tests/test_admin_roster_race.py`.
    """
    other = await _create_user(db, "admin-b", role="admin")
    token = await login(client, admin_user.email)

    res = await client.patch(
        f"/api/v1/admin/users/{other.id}", json={"is_active": False}, headers=bearer(token)
    )
    assert res.status_code == 200, res.text
    assert res.json()["is_active"] is False
    res = await client.delete(f"/api/v1/admin/users/{other.id}", headers=bearer(token))
    assert res.status_code == 204, res.text

    actions = list(
        await db.scalars(
            select(AuditLog.action).where(AuditLog.resource == other.email).order_by(AuditLog.id)
        )
    )
    assert sorted(actions) == ["user.delete", "user.update"]
    assert await db.get(User, other.id, populate_existing=True) is None
    active_admins = await db.scalar(
        select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True))
    )
    assert active_admins == 1
