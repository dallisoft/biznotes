# 신규(#194, docs/plans/embedding-transient-retry/). 필요: PostgreSQL·Valkey(PG 테스트).
"""임베딩 일시 오류의 미룸 — 실제 PostgreSQL에서 상태·재인덱싱 항목·정체 판정을 확인한다.

`_run`(진입)·`_run_index`(색인, #244)·재인덱싱 run 생성(관리 라우터)·`stalled_reindex_items`는
실경로다. 미룸은 이제 색인 단계가 하고 재시도도 색인 단계만 다시 낸다(`retry_kwargs`). 원격 임베딩
모델(게이트웨이 공간), 스토리지(`ingest_objects`), 브로커 발행, 개요 예약만 대역이다. 벡터는
실제 pgvector 행이다. `create_all` 기반이라 Alembic 마이그레이션 검증이 아니다.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import select, update

from biznotes_core.api.deps import require_admin
from biznotes_core.api.v1 import auth
from biznotes_core.api.v1.admin import rag
from biznotes_core.config import get_settings
from biznotes_core.core.db import async_session_factory
from biznotes_core.models import Notebook, ReindexRunItem, Source, User
from biznotes_core.services.embedding_spaces import TransientEmbeddingError
from biznotes_core.services.ingestion import stage_artifacts
from biznotes_core.services.ingestion.reindex import stalled_reindex_items
from biznotes_core.workers.celery_app import celery_app
from biznotes_core.workers.tasks import ingestion, overview

PASSWORD = "pw-test-1234"  # tests/conftest.py 공용 해시의 평문
MARKDOWN = "# 제목\n\n" + "본문 문장입니다. " * 120
PARSED_KEY = "parsed/t.md"
# 색인 단계가 미룰 때 재시도에 못박는 세대(#244 §3.3)
DIGEST = stage_artifacts.parsed_digest(MARKDOWN.encode())


class FakeSpace:
    is_local = False
    key = "gateway:fake-194"
    collection = "chunks_fake_194"
    display_name = "fake"

    def __init__(self) -> None:
        self.fail = True
        self.calls = 0

    def embed(self, texts: list[str], **kw: Any) -> list[list[float]]:
        self.calls += 1
        if self.fail:
            raise TransientEmbeddingError("503 busy")
        return [[1.0, 0.0] for _ in texts]


@pytest.fixture
def space(monkeypatch: pytest.MonkeyPatch, ingest_objects: Any) -> FakeSpace:
    fake = FakeSpace()

    async def active_space(db: Any) -> Any:
        return fake

    monkeypatch.setattr(ingestion, "active_space", active_space)
    ingest_objects.objects[(get_settings().storage_bucket_parsed, PARSED_KEY)] = MARKDOWN.encode()
    monkeypatch.setattr(overview, "schedule_overview_refresh", lambda nid: None)
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: None)
    return fake


@pytest.fixture
async def api(admin_user: User) -> AsyncIterator[Any]:
    app = FastAPI()
    admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
    admin.include_router(rag.router)
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(admin, prefix="/api/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/auth/login", json={"email": admin_user.email, "password": PASSWORD}
        )
        token = res.json()["access_token"]

        async def call(method: str, path: str, **kw: Any) -> httpx.Response:
            client.cookies.clear()
            headers = {"Authorization": f"Bearer {token}"}
            return await client.request(method, f"/api/v1{path}", headers=headers, **kw)

        yield call


async def _source(owner: User, status: str) -> uuid.UUID:
    async with async_session_factory() as s:
        nb = Notebook(owner_id=owner.id, title="미룸")
        s.add(nb)
        await s.flush()
        src = Source(
            notebook_id=nb.id,
            type="text",
            title="t",
            status=status,
            parsed_object_key=PARSED_KEY,
        )
        s.add(src)
        await s.commit()
        return src.id


async def _row(sid: uuid.UUID) -> Source:
    async with async_session_factory() as s:
        row = await s.get(Source, sid)
        assert row is not None
        return row


async def _items(sid: uuid.UUID) -> list[ReindexRunItem]:
    async with async_session_factory() as s:
        rows = await s.scalars(select(ReindexRunItem).where(ReindexRunItem.source_id == sid))
        return list(rows.all())


async def _stalled_ids(cutoff: datetime) -> set[uuid.UUID]:
    """정체 판정만 보고 되돌린다 — 판정이 올린 attempts를 남기지 않는다."""
    async with async_session_factory() as s:
        picked = {item.source_id for item, _ in await stalled_reindex_items(s, cutoff)}
        await s.rollback()
        return picked


async def test_deferred_reindex_keeps_index_and_item_open_then_succeeds(
    api: Any, user_a: User, space: FakeSpace
) -> None:
    sid = await _source(user_a, "ready")
    started = await api("POST", "/admin/rag/reindex", json={"failed_only": False})
    assert started.json()["enqueued"] == 1
    # 오래 멈춘 run처럼 만든다 — 미룸이 없으면 정체로 재발행될 항목이다
    old = datetime.now(UTC) - timedelta(minutes=30)
    async with async_session_factory() as s:
        await s.execute(
            update(ReindexRunItem).where(ReindexRunItem.source_id == sid).values(updated_at=old)
        )
        await s.commit()
    cutoff = datetime.now(UTC) - timedelta(minutes=ingestion.STALLED_THRESHOLD_MINUTES)
    assert await _stalled_ids(cutoff) == {sid}

    before = await _row(sid)
    entry = await ingestion._run(str(sid))  # 모드는 DB(열린 항목)로 정해진다

    # 저장된 parsed.md로 하는 재인덱싱 — 진입은 파싱도 상태 쓰기도 하지 않는다(#244). 색인 단계가
    # 점유할 때까지 ready로 남아 검색된다
    assert entry["handoff"] == {
        "parsed_sha256": None, "keep_previous": True, "claim_from": "ready",
        "transient_attempt": 0, "gate_attempt": 0,
    }
    mid = await _row(sid)
    assert (mid.status, mid.updated_at) == ("ready", before.updated_at)

    result = await ingestion._run_index(str(sid), **entry["handoff"])

    # 첫 미럼이므로 사유 카운터가 실린다(#196) — 다음 메시지가 그것을 이어받는다. 이전 색인을
    # 유지한 재인덱싱은 ready로 미룬다 — 재시도의 점유 조건은 토큰이 아니라 열린 항목이다
    assert result["retry_kwargs"] == {
        "parsed_sha256": DIGEST, "keep_previous": True, "claim_from": "ready",
        "deferred_at": None, "transient_attempt": 1, "gate_attempt": 0,
    }
    row = await _row(sid)
    assert row.status == "ready"
    # ready 행의 안내는 경고 코드다 — error_message는 실패 전용이다(#230)
    assert row.error_message is None
    assert [w["code"] for w in row.warnings or []] == ["reindex_deferred"]
    assert row.warnings[0]["params"]["reason"] == "embedding_transient"
    (item,) = await _items(sid)
    assert item.status == "queued" and item.updated_at > cutoff
    assert await _stalled_ids(cutoff) == set()  # 미루는 동안 정체로 보지 않는다

    space.fail = False
    result = await ingestion._run_index(str(sid), **result["retry_kwargs"])

    assert result["ok"] is True
    row = await _row(sid)
    assert (row.status, row.error_message, row.embedding_model) == ("ready", None, space.key)
    (item,) = await _items(sid)
    assert item.status == "done"


async def test_new_source_defers_to_pending_then_fails_at_limit(
    user_a: User, space: FakeSpace
) -> None:
    # 파싱을 마치고 색인 단계를 기다리는 신규 소스(진입 단계가 chunking을 커밋했다)
    sid = await _source(user_a, "chunking")

    first = await ingestion._run_index(str(sid))

    row = await _row(sid)
    # 미루는 동안 신규 소스는 pending이다(D3-B) — 재시도는 미룰 때의 updated_at(토큰)을 들고
    # pending에서 점유한다
    assert first["retry_kwargs"] == {
        "parsed_sha256": DIGEST, "keep_previous": False, "claim_from": "pending",
        "deferred_at": row.updated_at.isoformat(), "transient_attempt": 1, "gate_attempt": 0,
    }
    assert row.status == "pending" and row.error_message.startswith("임베딩 지연 — ")

    last = await ingestion._run_index(
        str(sid),
        **{**first["retry_kwargs"], "transient_attempt": ingestion.INGEST_TRANSIENT_RETRIES},
    )

    assert "retry_in" not in last
    row = await _row(sid)
    assert row.status == "failed"
    assert row.error_message.startswith("임베딩 서버 일시 오류 — 재시도 한도 초과")


@pytest.mark.parametrize(
    ("message", "error"),
    [
        # 구 버전이 미룬 `ingest_source` 메시지(§3.8) — 진입 단계가 걸러 낸다
        (
            {"stage": "entry", "reindex": True, "refetch": False, "transient_attempt": 1},
            "retry_superseded",
        ),
        # 새 버전이 미룬 `index_source` 메시지 — 점유 문장의 열린 항목 조건이 걸러 낸다
        (
            {
                "stage": "index", "keep_previous": True, "claim_from": "ready",
                "transient_attempt": 1,
            },
            "claim_lost",
        ),
        # 첫 색인 메시지도 재인덱싱이면 열린 항목이 있어야 점유한다
        ({"stage": "index", "keep_previous": True, "claim_from": "ready"}, "claim_lost"),
    ],
    ids=["entry-retry", "index-retry", "index-first"],
)
async def test_superseded_retry_writes_nothing(
    user_a: User, space: FakeSpace, message: dict[str, Any], error: str
) -> None:
    # 미룬 사이 사용자 재시도가 처리를 끝냈다(ready, 열린 항목 없음)
    sid = await _source(user_a, "ready")
    before = await _row(sid)
    kwargs = {k: v for k, v in message.items() if k != "stage"}
    run = ingestion._run if message["stage"] == "entry" else ingestion._run_index

    result = await run(str(sid), **kwargs)

    assert result == {"ok": False, "error": error}
    after = await _row(sid)
    assert (after.status, after.updated_at) == (before.status, before.updated_at)
    assert space.calls == 0
