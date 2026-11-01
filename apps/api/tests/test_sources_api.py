"""소스 API 테스트 — 스토리지/Celery는 모킹, DB는 실 PG."""

import asyncio
import uuid

import httpx
import pytest
from biznotes_core.config import get_settings
from biznotes_core.models import Source
from fastapi import HTTPException

from tests.conftest import bearer, login, set_source_limit, share_group


@pytest.fixture(autouse=True)
def _mock_external(monkeypatch):
    """스토리지/Celery 외부 의존성 차단."""
    stored: dict[tuple[str, str], bytes] = {}

    def fake_put(bucket, key, data, content_type="application/octet-stream"):
        stored[(bucket, key)] = data

    def fake_get(bucket, key):
        return stored[(bucket, key)]

    def fake_delete(bucket, key):
        stored.pop((bucket, key), None)

    def fake_delete_many(bucket, keys):
        for key in keys:
            stored.pop((bucket, key), None)
        return []

    class FakeObjectStream:
        """코어 `ObjectStream` 대역 — 본문 전문 조회는 스트리밍 경로를 쓴다(코어 #26)."""

        def __init__(self, data: bytes) -> None:
            self._data = data
            self.length = len(data)
            self.closed = False

        def chunks(self):
            yield self._data

        def close(self) -> None:
            self.closed = True

    def fake_open_stream(bucket, key, chunk_size=64 * 1024):
        return FakeObjectStream(stored[(bucket, key)])

    class FakeAsyncResult:
        id = "fake-task-id"

    monkeypatch.setattr("biznotes_core.api.v1.sources.ensure_buckets", lambda: None)
    monkeypatch.setattr("biznotes_core.api.v1.sources.put_object", fake_put)
    monkeypatch.setattr("biznotes_core.api.v1.sources.get_object", fake_get)
    monkeypatch.setattr("biznotes_core.api.v1.sources.open_object_stream", fake_open_stream)
    monkeypatch.setattr("biznotes_core.services.storage.delete_object", fake_delete)
    monkeypatch.setattr("biznotes_core.services.storage.delete_objects", fake_delete_many)
    monkeypatch.setattr(
        "biznotes_core.workers.tasks.ingestion.ingest_source.delay", lambda sid: FakeAsyncResult()
    )
    monkeypatch.setattr(
        "biznotes_core.workers.tasks.ingestion.ingest_source.apply_async",
        lambda *a, **k: FakeAsyncResult(),
    )
    # 코어 v0.22.0 #326 — 소스 삭제의 벡터 정리는 라우터가 `publish_task`로 이름 발행한다
    from biznotes_core.workers.celery_app import celery_app

    monkeypatch.setattr(celery_app, "send_task", lambda name, *a, **k: FakeAsyncResult())
    return stored


async def _make_notebook(client, token) -> str:
    res = await client.post("/api/v1/notebooks", json={"title": "src-nb"}, headers=bearer(token))
    return res.json()["id"]


async def test_create_text_source(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token)

    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources",
        json={"type": "text", "text": "붙여넣은 본문입니다.\n둘째 줄.", "title": "메모"},
        headers=bearer(token),
    )
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["type"] == "text"
    assert body["title"] == "메모"
    assert body["status"] == "pending"

    res = await client.get(f"/api/v1/notebooks/{nb_id}/sources", headers=bearer(token))
    assert len(res.json()) == 1

    # 노트북 source_count 반영
    res = await client.get(f"/api/v1/notebooks/{nb_id}", headers=bearer(token))
    assert res.json()["source_count"] == 1


async def test_create_web_source_requires_valid_url(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token)

    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources",
        json={"type": "web", "url": "ftp://bad"},
        headers=bearer(token),
    )
    assert res.status_code == 422

    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources",
        json={"type": "web", "url": "https://example.com/article"},
        headers=bearer(token),
    )
    assert res.status_code == 202


async def test_upload_file_source(client: httpx.AsyncClient, user_a, _mock_external):
    token = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token)

    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources/upload",
        files={"file": ("notes.md", b"# Hello\n\nworld", "text/markdown")},
        headers=bearer(token),
    )
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["type"] == "file"
    assert body["mime_type"] == "text/markdown"
    # 원본이 스토리지에 저장됨
    assert any(k[0] == "sources" for k in _mock_external)


async def test_upload_rejects_oversized_file_with_413_at_boundary(
    client: httpx.AsyncClient, db, user_a, _mock_external
):
    """업로드 크기 상한 경계 — 상한 이하는 202, 상한 초과는 **413**(422가 아님).

    코어는 본문을 통째 힙에 올리지 않고 유효 상한+1까지만 읽은 뒤 `admit_bytes`가 `too_large`를
    올리고 라우터가 413으로 매핑한다. 코어 v0.26.0(#388)부터 유효 상한은 관리자 재정의
    (`system_settings["source_upload"]`) > env(`SOURCE_MAX_UPLOAD_BYTES`)다. 50MB를 실제로 전송하지
    않고 **재정의 행으로 상한을 줄여** 그 실경로(재정의 조회 → 잘린 read → 관문 → 413)를 탄다.
    관문은 mock으로 우회하지 않는다(§3.3-3). 기본 상한값(50MB) 자체도 함께 고정한다.
    """
    from biznotes_core.config import get_settings
    from biznotes_core.services.ingestion import admission, upload_limits
    from biznotes_core.services.settings_store import set_setting

    assert admission.MAX_UPLOAD_BYTES == 50 * 1024 * 1024  # 계약 상수
    assert get_settings().source_max_upload_bytes == admission.MAX_UPLOAD_BYTES

    limit = 1024
    await set_setting(db, upload_limits.SETTING_KEY, {upload_limits.OVERRIDE_FIELD: limit})
    await db.commit()

    token = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token)

    at_limit = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources/upload",
        files={"file": ("at-limit.md", b"a" * limit, "text/markdown")},
        headers=bearer(token),
    )
    assert at_limit.status_code == 202, at_limit.text

    over_limit = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources/upload",
        files={"file": ("over-limit.md", b"a" * (limit + 1), "text/markdown")},
        headers=bearer(token),
    )
    assert over_limit.status_code == 413, over_limit.text
    assert "초과" in over_limit.json()["detail"]


async def test_upload_duplicate_file_returns_409_and_uses_canonical_name(
    client: httpx.AsyncClient, user_a, _mock_external
):
    token = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token)

    first = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources/upload",
        files={"file": ("folder/e\u0301.md", b"same bytes", "text/markdown")},
        headers=bearer(token),
    )
    assert first.status_code == 202, first.text
    assert first.json()["title"] == "é.md"

    duplicate = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources/upload",
        files={"file": ("é.md", b"same bytes", "text/markdown")},
        headers=bearer(token),
    )
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["detail"]["existing_source_id"] == first.json()["id"]


async def test_upload_source_limit_allows_1000th_and_rejects_1001st(
    client: httpx.AsyncClient, user_a, db, _mock_external
):
    """설정이 없을 때(코어 기본값) 상한은 1000이다 — 배포값 500은 env로 준다(#281)."""
    token = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token)

    from biznotes_core.models import Source

    db.add_all(
        [
            Source(
                notebook_id=uuid.UUID(nb_id),
                type="text",
                title=f"existing-{i}",
                status="ready",
            )
            for i in range(999)
        ]
    )
    await db.commit()

    accepted = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources/upload",
        files={"file": ("last.md", b"last", "text/markdown")},
        headers=bearer(token),
    )
    assert accepted.status_code == 202, accepted.text

    rejected = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources/upload",
        files={"file": ("over.md", b"over", "text/markdown")},
        headers=bearer(token),
    )
    assert rejected.status_code == 422, rejected.text


async def test_upload_source_limit_is_atomic_for_concurrent_requests(
    client: httpx.AsyncClient, user_a, db, _mock_external
):
    token = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token)

    from biznotes_core.models import Source

    db.add_all(
        [
            Source(
                notebook_id=uuid.UUID(nb_id),
                type="text",
                title=f"existing-{i}",
                status="ready",
            )
            for i in range(999)
        ]
    )
    await db.commit()

    responses = await asyncio.gather(
        *(
            client.post(
                f"/api/v1/notebooks/{nb_id}/sources/upload",
                files={"file": (name, body, "text/markdown")},
                headers=bearer(token),
            )
            for name, body in (("concurrent-a.md", b"a"), ("concurrent-b.md", b"b"))
        )
    )

    assert sorted(response.status_code for response in responses) == [202, 422]
    listed = await client.get(f"/api/v1/notebooks/{nb_id}/sources", headers=bearer(token))
    assert len(listed.json()) == 1000


async def test_upload_cleans_raw_object_when_ingest_staging_fails(
    client: httpx.AsyncClient, user_a, _mock_external, monkeypatch
):
    token = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token)

    def boom(*args, **kwargs):
        raise RuntimeError("job staging failed")

    monkeypatch.setattr("biznotes_core.api.v1.sources.stage_ingest_job", boom)
    with pytest.raises(RuntimeError, match="job staging failed"):
        await client.post(
            f"/api/v1/notebooks/{nb_id}/sources/upload",
            files={"file": ("cleanup.md", b"# cleanup", "text/markdown")},
            headers=bearer(token),
        )

    assert _mock_external == {}


def test_file_name_canonicalization_rejects_empty_names():
    from biznotes_core.services.ingestion.enqueue import canonicalize_file_name

    assert canonicalize_file_name("a\\b/ e\u0301.md ") == "é.md"
    with pytest.raises(HTTPException) as exc:
        canonicalize_file_name(" / ")
    assert exc.value.status_code == 422


async def test_upload_unsupported_extension(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources/upload",
        files={"file": ("malware.exe", b"MZ", "application/octet-stream")},
        headers=bearer(token),
    )
    assert res.status_code == 422


async def test_read_only_user_cannot_add_source(client: httpx.AsyncClient, db, user_a, user_b):
    token_a = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token_a)
    group = await share_group(db, user_a, user_b)
    await client.post(
        f"/api/v1/notebooks/{nb_id}/share",
        json={"group_id": str(group.id), "permission": "read"},
        headers=bearer(token_a),
    )

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources",
        json={"type": "text", "text": "x"},
        headers=bearer(token_b),
    )
    assert res.status_code == 403
    # 읽기는 가능
    res = await client.get(f"/api/v1/notebooks/{nb_id}/sources", headers=bearer(token_b))
    assert res.status_code == 200


async def test_delete_source(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources",
        json={"type": "text", "text": "삭제될 텍스트"},
        headers=bearer(token),
    )
    sid = res.json()["id"]

    res = await client.delete(f"/api/v1/sources/{sid}", headers=bearer(token))
    assert res.status_code == 204
    res = await client.get(f"/api/v1/sources/{sid}", headers=bearer(token))
    assert res.status_code == 404


async def _seed_object_keys(db, stored, source_id: str) -> list[tuple[str, str]]:
    """소스 행에 원본·parsed 키를 직접 두고 같은 키를 가짜 저장소에 넣는다.

    인제스트가 막혀 있어 `parsed_object_key`는 저절로 채워지지 않는다.
    """
    settings = get_settings()
    raw = (settings.storage_bucket_sources, f"sources/{source_id}/raw.txt")
    parsed = (settings.storage_bucket_parsed, f"parsed/{source_id}.md")
    source = await db.get(Source, uuid.UUID(source_id))
    source.original_object_key = raw[1]
    source.parsed_object_key = parsed[1]
    await db.commit()
    stored[raw] = b"raw"
    stored[parsed] = b"parsed"
    return [raw, parsed]


async def _make_text_source(client, token, nb_id: str) -> str:
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources",
        json={"type": "text", "text": "오브젝트 정리 대상"},
        headers=bearer(token),
    )
    assert res.status_code == 202, res.text
    return res.json()["id"]


async def test_delete_source_removes_raw_and_parsed_objects(
    client: httpx.AsyncClient, user_a, db, _mock_external
):
    token = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    sid = await _make_text_source(client, token, nb_id)
    refs = await _seed_object_keys(db, _mock_external, sid)

    res = await client.delete(f"/api/v1/sources/{sid}", headers=bearer(token))

    assert res.status_code == 204
    assert [ref for ref in refs if ref in _mock_external] == []


async def test_delete_notebook_removes_source_objects(
    client: httpx.AsyncClient, user_a, db, _mock_external
):
    token = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    sid = await _make_text_source(client, token, nb_id)
    refs = await _seed_object_keys(db, _mock_external, sid)

    res = await client.delete(f"/api/v1/notebooks/{nb_id}", headers=bearer(token))

    assert res.status_code == 204
    assert [ref for ref in refs if ref in _mock_external] == []


async def test_source_detail_includes_preview(client: httpx.AsyncClient, user_a, _mock_external):
    token = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources",
        json={"type": "text", "text": "미리보기 본문"},
        headers=bearer(token),
    )
    sid = res.json()["id"]

    # 파싱 산출물을 직접 저장해 detail preview 확인
    _mock_external[("parsed", f"{sid}/parsed.md")] = "미리보기 본문".encode()
    import uuid as _uuid

    from biznotes_core.core.db import async_session_factory
    from biznotes_core.models import Source

    async with async_session_factory() as db:
        src = await db.get(Source, _uuid.UUID(sid))
        src.parsed_object_key = f"{sid}/parsed.md"
        await db.commit()

    res = await client.get(f"/api/v1/sources/{sid}", headers=bearer(token))
    assert res.status_code == 200
    assert res.json()["parsed_preview"] == "미리보기 본문"


async def test_source_content_permission_matrix(
    client: httpx.AsyncClient, db, user_a, user_b, admin_user, _mock_external
):
    """Parsed source content is hidden from strangers but readable by sharees and admins.

    주의: 코어 `biznotes_core/api/deps.py::_extract_token`은 **httpOnly 쿠키를 Bearer 헤더보다
    우선**한다. `login()`이 응답 쿠키를 `client`에 누적시키므로, 여러 사용자를 오가는 이
    테스트는 매 요청 전 `client.cookies.clear()`로 쿠키를 비워야 Bearer 헤더가 실제
    인증 주체가 된다(비우지 않으면 마지막 로그인 사용자로 요청이 나간다).
    """
    from biznotes_core.models import Source

    owner_token = await login(client, user_a.email)
    notebook_id = await _make_notebook(client, owner_token)
    created = await client.post(
        f"/api/v1/notebooks/{notebook_id}/sources",
        json={"type": "text", "text": "원본", "title": "권한 본문"},
        headers=bearer(owner_token),
    )
    source_id = created.json()["id"]
    source = await db.get(Source, uuid.UUID(source_id))
    source.parsed_object_key = f"{source_id}/parsed.md"
    await db.commit()
    secret = "노출되면 안 되는 파싱 본문"
    _mock_external[("parsed", source.parsed_object_key)] = secret.encode()

    client.cookies.clear()
    unauthenticated = await client.get(f"/api/v1/sources/{source_id}/content")
    assert unauthenticated.status_code == 401

    stranger_token = await login(client, user_b.email)
    client.cookies.clear()
    hidden = await client.get(
        f"/api/v1/sources/{source_id}/content", headers=bearer(stranger_token)
    )
    assert hidden.status_code == 404
    assert secret not in hidden.text

    group = await share_group(db, user_a, user_b)
    shared = await client.post(
        f"/api/v1/notebooks/{notebook_id}/share",
        json={"group_id": str(group.id), "permission": "read"},
        headers=bearer(owner_token),
    )
    assert shared.status_code in (200, 201)
    shared_read = await client.get(
        f"/api/v1/sources/{source_id}/content", headers=bearer(stranger_token)
    )
    assert shared_read.status_code == 200
    assert shared_read.text == secret

    admin_token = await login(client, admin_user.email)
    client.cookies.clear()
    admin_read = await client.get(
        f"/api/v1/sources/{source_id}/content", headers=bearer(admin_token)
    )
    assert admin_read.status_code == 200
    assert admin_read.text == secret


async def test_retry_source_clears_summary_and_keywords(
    client: httpx.AsyncClient, db, user_a
):
    from biznotes_core.models import Source

    token = await login(client, user_a.email)
    notebook_id = await _make_notebook(client, token)
    created = await client.post(
        f"/api/v1/notebooks/{notebook_id}/sources",
        json={"type": "text", "text": "교체 본문", "title": "재시도"},
        headers=bearer(token),
    )
    source_id = uuid.UUID(created.json()["id"])
    source = await db.get(Source, source_id)
    source.status = "failed"
    source.summary = "이전 요약"
    source.keywords = ["이전", "키워드"]
    await db.commit()

    retried = await client.post(
        f"/api/v1/sources/{source_id}/retry", headers=bearer(token)
    )
    assert retried.status_code == 202
    await db.refresh(source)
    assert source.status == "pending"
    assert source.summary is None
    assert source.keywords is None


# --- R3-1e: source·발행 예약 단일 커밋 (결함 α — #163 이후 job 행 없음) ---
# --- HCI-R3B-1(a): 발행 실패 삼킴 ---


async def test_커밋_실패시_source가_남지_않는다(client: httpx.AsyncClient, user_a, monkeypatch):
    """이전에는 source를 커밋한 뒤 job을 따로 커밋해, 그 사이 실패 시 job 없는 source가 남았다."""
    token = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token)

    def boom(db, source, uow):
        raise RuntimeError("job staging failed")

    monkeypatch.setattr("biznotes_core.api.v1.sources.stage_ingest_job", boom)

    with pytest.raises(RuntimeError, match="job staging failed"):
        await client.post(
            f"/api/v1/notebooks/{nb_id}/sources",
            json={"type": "text", "text": "본문", "title": "메모"},
            headers=bearer(token),
        )

    res = await client.get(f"/api/v1/notebooks/{nb_id}/sources", headers=bearer(token))
    assert res.json() == [], "커밋이 실패했으므로 source도 남으면 안 된다 (α 해소)"


async def test_발행_실패해도_202를_반환한다(client: httpx.AsyncClient, user_a, monkeypatch):
    """HCI-R3B-1(a): 500을 내면 사용자 재시도가 중복 소스를 만든다. reaper가 회수한다."""
    token = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token)

    from biznotes_core.workers import task_names
    from biznotes_core.workers.celery_app import celery_app

    attempts: list[str] = []

    def boom(name, args, task_id, **k):
        # 코어는 태스크를 이름으로 발행한다(send_task) — 발행 경계에서 실패를 주입한다.
        # (예전 `ingest_source.apply_async` 패치는 코어가 부르지 않는 경로라 실패가 주입되지
        #  않았다 — BL-58. core-v0.11.0-upgrade AC-1.12에서 발행 경계로 옮겼다)
        assert name == task_names.INGEST_SOURCE
        attempts.append(args[0])
        raise RuntimeError("broker down")

    monkeypatch.setattr(celery_app, "send_task", boom)

    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources",
        json={"type": "text", "text": "본문", "title": "메모"},
        headers=bearer(token),
    )
    assert res.status_code == 202, res.text
    assert res.json()["status"] == "pending"  # reaper 회수 대상
    assert attempts == [res.json()["id"]], "커밋 뒤 발행을 실제로 시도했고 그 실패를 삼켰다"

    res = await client.get(f"/api/v1/notebooks/{nb_id}/sources", headers=bearer(token))
    assert len(res.json()) == 1, "발행은 실패해도 source는 생성된다"


async def test_t7_source_content_rate_limit_returns_429_after_limit(
    client: httpx.AsyncClient, db, _mock_external, monkeypatch
):
    """T-7: 소스 본문 전문 조회는 사용자별 `RATE_LIMIT_SOURCE_CONTENT`/분 — limit회 200, 다음은 429.

    테스트 환경(`APP_ENV=test`)은 리미터를 단락하므로 설정 인스턴스의 `app_env`·
    `rate_limit_enabled`만 바꾼다(`test_rate_limit.py`의 `active_settings` 선례). 리미터 본문과
    Redis 카운터는 실제로 실행된다 — mock이 아니다(계획 AC-14·HCI-23). 한도는 하드코딩하지 않고
    설정에서 읽고, 전용 사용자(새 JWT sub)로 키를 다른 테스트와 분리한 뒤 지운다.
    """
    from biznotes_core.models import Source
    from redis.asyncio import Redis

    from app.core.config import get_settings
    from tests.conftest import _create_user

    settings = get_settings()
    limit = int(settings.rate_limit_source_content)
    assert limit > 0
    reader = await _create_user(db, "ratelimit-reader")
    token = await login(client, reader.email)
    notebook_id = await _make_notebook(client, token)
    created = await client.post(
        f"/api/v1/notebooks/{notebook_id}/sources",
        json={"type": "text", "text": "원본", "title": "한도"},
        headers=bearer(token),
    )
    source_id = created.json()["id"]
    source = await db.get(Source, uuid.UUID(source_id))
    source.parsed_object_key = f"{source_id}/parsed.md"
    await db.commit()
    _mock_external[("parsed", source.parsed_object_key)] = b"body"

    bucket = f"ratelimit:source-content:user:{reader.id}"
    redis = Redis.from_url(settings.redis_url)
    await redis.delete(bucket)
    monkeypatch.setattr(settings, "app_env", "dev")
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    try:
        for i in range(limit):
            ok = await client.get(f"/api/v1/sources/{source_id}/content", headers=bearer(token))
            assert ok.status_code == 200, (i, ok.text)
        blocked = await client.get(
            f"/api/v1/sources/{source_id}/content", headers=bearer(token)
        )
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) >= 1
        assert int(await redis.get(bucket)) == limit + 1
    finally:
        await redis.delete(bucket)
        await redis.aclose()


# ---------- core-v0.11.0-upgrade P1 (직전 계획 v7 승계): 문서 제목·경고, 권한 표 E-3~E-6 ----------


async def _share(
    client: httpx.AsyncClient, db, owner, owner_token: str, nb_id: str, user, permission
):
    group = await share_group(db, owner, user)
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/share",
        json={"group_id": str(group.id), "permission": permission},
        headers=bearer(owner_token),
    )
    assert res.status_code in (200, 201), res.text


async def _text_source(client: httpx.AsyncClient, token: str, nb_id: str) -> str:
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources",
        json={"type": "text", "text": "본문", "title": "메타"},
        headers=bearer(token),
    )
    assert res.status_code == 202, res.text
    return res.json()["id"]


async def test_source_list_exposes_document_title_and_warnings(
    client: httpx.AsyncClient, db, user_a
):
    """E-3a — 목록이 `document_title`·`warnings`를 싣는다(코어 v0.10.7 #100, v7 AC-10 ①).

    NULL → null, [] → [], 형식이 틀린 경고 항목은 그 항목만 버리고 200이다.
    """
    from biznotes_core.models import Source

    token = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    ids = [await _text_source(client, token, nb_id) for _ in range(3)]
    rows = {sid: await db.get(Source, uuid.UUID(sid)) for sid in ids}
    rows[ids[1]].document_title = "보고서 제목"
    rows[ids[1]].warnings = []
    rows[ids[2]].document_title = "경고 있음"
    rows[ids[2]].warnings = [
        {"code": "text_truncated", "params": {"omitted": 3}},
        {"code": 42, "params": "not-a-dict"},  # 다른 버전이 쓴 형식 오류 항목
    ]
    await db.commit()

    res = await client.get(f"/api/v1/notebooks/{nb_id}/sources", headers=bearer(token))

    assert res.status_code == 200, res.text
    by_id = {s["id"]: s for s in res.json()}
    assert (by_id[ids[0]]["document_title"], by_id[ids[0]]["warnings"]) == (None, None)
    assert (by_id[ids[1]]["document_title"], by_id[ids[1]]["warnings"]) == ("보고서 제목", [])
    assert by_id[ids[2]]["warnings"] == [{"code": "text_truncated", "params": {"omitted": 3}}]


async def test_retry_clears_document_title_and_warnings(client: httpx.AsyncClient, db, user_a):
    """E-4 — retry가 두 컬럼을 SQL NULL로 지운다(전체 파싱이 다시 채운다, v7 AC-10 ②)."""
    from biznotes_core.models import Source
    from sqlalchemy import text

    token = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    sid = await _text_source(client, token, nb_id)
    source = await db.get(Source, uuid.UUID(sid))
    source.status = "failed"
    source.document_title = "이전 제목"
    source.warnings = [{"code": "text_truncated", "params": {"omitted": 1}}]
    await db.commit()

    res = await client.post(f"/api/v1/sources/{sid}/retry", headers=bearer(token))

    assert res.status_code == 202, res.text
    assert res.json()["document_title"] is None and res.json()["warnings"] is None
    nulls = (
        await db.execute(
            text(
                "SELECT count(*) FROM sources WHERE id = :id"
                " AND warnings IS NULL AND document_title IS NULL"
            ),
            {"id": uuid.UUID(sid)},
        )
    ).scalar_one()
    assert nulls == 1


async def test_source_list_and_detail_hidden_from_non_member(
    client: httpx.AsyncClient, user_a, user_b
):
    """E-3a·E-3b — 비구성원은 소스 목록·상세 모두 404(존재를 드러내지 않는다)."""
    token_a = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token_a)
    sid = await _text_source(client, token_a, nb_id)

    token_b = await login(client, user_b.email)
    client.cookies.clear()
    listed = await client.get(f"/api/v1/notebooks/{nb_id}/sources", headers=bearer(token_b))
    detail = await client.get(f"/api/v1/sources/{sid}", headers=bearer(token_b))

    assert listed.status_code == 404
    assert detail.status_code == 404


@pytest.mark.parametrize("action", ["retry", "delete", "upload"])
async def test_read_shared_user_cannot_write_sources(
    client: httpx.AsyncClient, db, user_a, user_b, action
):
    """E-4·E-5·E-6 — read 공유 사용자는 retry·삭제·업로드 모두 403, 소스는 그대로다."""
    from biznotes_core.models import Source

    token_a = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token_a)
    sid = await _text_source(client, token_a, nb_id)
    source = await db.get(Source, uuid.UUID(sid))
    source.status = "failed"
    await db.commit()
    await _share(client, db, user_a, token_a, nb_id, user_b, "read")

    token_b = await login(client, user_b.email)
    client.cookies.clear()
    if action == "retry":
        res = await client.post(f"/api/v1/sources/{sid}/retry", headers=bearer(token_b))
    elif action == "delete":
        res = await client.delete(f"/api/v1/sources/{sid}", headers=bearer(token_b))
    else:
        res = await client.post(
            f"/api/v1/notebooks/{nb_id}/sources/upload",
            files={"file": ("memo.txt", b"hello", "text/plain")},
            headers=bearer(token_b),
        )

    assert res.status_code == 403, res.text
    db.expire_all()
    still = await db.get(Source, uuid.UUID(sid))
    assert still is not None and still.status == "failed"
    listed = await client.get(f"/api/v1/notebooks/{nb_id}/sources", headers=bearer(token_a))
    assert len(listed.json()) == 1


async def test_upload_source_limit_follows_setting_500(
    client: httpx.AsyncClient, user_a, db, _mock_external, monkeypatch
):
    """배포값 `MAX_SOURCES_PER_NOTEBOOK=500`(#281, core-v0.30.0-upgrade AC-6) — 500번째는 받고
    501번째는 422다. 공개 조회 `upload-limits`가 같은 값을 돌려준다(웹 `N/500` 표시)."""
    set_source_limit(monkeypatch, 500)
    token = await login(client, user_a.email)
    nb_id = await _make_notebook(client, token)

    from biznotes_core.models import Source

    db.add_all(
        [
            Source(notebook_id=uuid.UUID(nb_id), type="text", title=f"e-{i}", status="ready")
            for i in range(499)
        ]
    )
    await db.commit()

    limits = await client.get("/api/v1/sources/upload-limits", headers=bearer(token))
    assert limits.status_code == 200, limits.text
    assert limits.json()["max_sources_per_notebook"] == 500

    accepted = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources/upload",
        files={"file": ("last.md", b"last", "text/markdown")},
        headers=bearer(token),
    )
    assert accepted.status_code == 202, accepted.text
    rejected = await client.post(
        f"/api/v1/notebooks/{nb_id}/sources/upload",
        files={"file": ("over.md", b"over", "text/markdown")},
        headers=bearer(token),
    )
    assert rejected.status_code == 422, rejected.text
    assert "500" in rejected.text


async def test_notebook_source_limit_admin_override_is_admin_only(
    client: httpx.AsyncClient, user_a, admin_user
):
    """관리자 재정의 `/admin/notebook-source-limit`(코어 #445) — 일반 사용자 403(비인가)."""
    user_token = await login(client, user_a.email)
    for method in ("get", "put"):
        kwargs = {"json": {"max_sources_per_notebook": 10}} if method == "put" else {}
        res = await getattr(client, method)(
            "/api/v1/admin/notebook-source-limit", headers=bearer(user_token), **kwargs
        )
        assert res.status_code == 403, res.text

    admin_token = await login(client, admin_user.email)
    res = await client.get("/api/v1/admin/notebook-source-limit", headers=bearer(admin_token))
    assert res.status_code == 200, res.text
