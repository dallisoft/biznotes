"""인제스트 2단계(파싱 → 색인) 계약 특성 — 코어 v0.16.0 #244(core-v0.16.0-upgrade AC-1.3 ⑦·⑨).

코어 워커 본문(`_run`·`_run_index`)과 실 라우터(`POST /api/v1/admin/rag/reindex`)를 그대로 쓰고
외부 경계만 대체한다: 스토리지(키 → 바이트 dict 저장소), 임베딩(`embed_dense`·`embed_sparse` —
벡터는 청크 행에 실제로 쓴다), Celery 발행(`celery_app.send_task`·개요 예약),
상태 이벤트의 Valkey 발행(`publish_source_event` — 기록용 대역, `_publish_status`·전이 본문은
그대로).

- ⑦ 끝난 행(`ready`·`failed`)에 온 일반 인제스트 메시지는 낡은 중복이다 — 아무것도 쓰지 않는다.
  다시 인제스트하려면 먼저 `pending`으로 바꾼다(제품 재발행 경로 `connections.py`·사용자 재시도가
  `pending`을 먼저 쓰는 이유).
- ⑨ 상태 전이 순서 — 이전 색인이 있는 재인덱싱은 색인 단계가 점유할 때까지 `ready`로 남고
  (`chunking`을 거치지 않는다), 신규 소스는 `parsing → chunking → embedding → ready`다. 라이브 L-4는
  샘플링이라 중간 상태를 놓칠 수 있어 순서의 정본은 이 테스트다(계획 D-9).
"""

import uuid

import httpx
import pytest
from biznotes_core.models import Notebook, ReindexRunItem, Source
from biznotes_core.services.ingestion.embedder import SparseVector
from biznotes_core.workers.celery_app import celery_app
from biznotes_core.workers.tasks.ingestion import _run, _run_index
from sqlalchemy import select, update

from tests.conftest import bearer, login

TEXT = "# 단계 분리 표본\n\n첫째 문단입니다.\n\n둘째 문단입니다."


class _NoSuchKey(Exception):
    """S3Error의 `code`만 흉내 낸다 — 코어 `_read_capped`가 이 값으로 「없음」을 판정한다."""

    code = "NoSuchKey"


@pytest.fixture
def stage(monkeypatch) -> dict:
    """외부 경계 대역. `store`는 스토리지, `puts`는 `put_object` 기록, `events`는
    (source_id, status)."""
    store: dict[str, bytes] = {}
    puts: list[str] = []
    events: list[tuple[str, str]] = []

    def _get(bucket, key):
        if key not in store:
            raise _NoSuchKey(key)
        return store[key]

    def _put(bucket, key, data, *a, **k):
        puts.append(key)
        store[key] = data

    target = "biznotes_core.workers.tasks.ingestion"
    monkeypatch.setattr(f"{target}.get_object", _get)
    monkeypatch.setattr(f"{target}.stat_object_size", lambda bucket, key: len(_get(bucket, key)))
    monkeypatch.setattr(f"{target}.put_object", _put)
    monkeypatch.setattr(f"{target}.embed_dense", lambda texts: [[1.0] for _ in texts])
    monkeypatch.setattr(
        f"{target}.embed_sparse", lambda texts: [SparseVector([], []) for _ in texts]
    )
    monkeypatch.setattr(
        f"{target}.publish_source_event",
        lambda source_id, event: events.append((source_id, event["status"])),
    )
    monkeypatch.setattr(
        "biznotes_core.workers.tasks.overview.schedule_overview_refresh",
        lambda notebook_id, **kwargs: None,
    )
    return {"store": store, "puts": puts, "events": events}


@pytest.fixture
def sent(monkeypatch) -> list[dict]:
    """재인덱싱 라우터의 발행 경계(`celery_app.send_task`) 기록 — 실제 브로커 발행 차단."""
    calls: list[dict] = []

    def send_task(name, args=None, **kwargs):
        calls.append({"name": name, "args": list(args or [])})

    monkeypatch.setattr(celery_app, "send_task", send_task)
    return calls


def _statuses(stage: dict, sid: uuid.UUID) -> list[str]:
    """이 사례 소스의 상태 이벤트만 — 다른 소스의 이벤트는 세지 않는다."""
    return [status for source_id, status in stage["events"] if source_id == str(sid)]


async def _reload(db, sid: uuid.UUID) -> Source:
    db.expire_all()
    return (await db.execute(select(Source).where(Source.id == sid))).scalar_one()


async def _text_source(db, owner, stage: dict, **values) -> uuid.UUID:
    """사례마다 별도 노트북·소스. 원본은 스토리지 대역에 둔다."""
    nb = Notebook(owner_id=owner.id, title=f"stage-{uuid.uuid4().hex[:6]}")
    db.add(nb)
    await db.flush()
    key = f"originals/{uuid.uuid4()}.txt"
    stage["store"][key] = TEXT.encode()
    source = Source(
        notebook_id=nb.id,
        type="text",
        title="단계 분리 표본",
        original_object_key=key,
        **{"status": "pending", **values},
    )
    db.add(source)
    await db.commit()
    return source.id


async def _ready_source(db, owner, stage: dict) -> uuid.UUID:
    """실제 두 단계를 거쳐 `ready`가 된 소스(저장된 parsed.md 있음) — 재인덱싱 사례의 출발점."""
    sid = await _text_source(db, owner, stage)
    parsed = await _run(str(sid))
    assert parsed["ok"] is True, parsed
    indexed = await _run_index(str(sid), **parsed["handoff"])
    assert indexed["ok"] is True, indexed
    source = await _reload(db, sid)
    assert source.status == "ready"
    assert source.parsed_object_key is not None
    return sid


async def _item_status(db, run_id: str, sid: uuid.UUID) -> str:
    db.expire_all()
    stmt = select(ReindexRunItem.status).where(
        ReindexRunItem.run_id == uuid.UUID(run_id), ReindexRunItem.source_id == sid
    )
    return (await db.execute(stmt)).scalar_one()


async def _start_reindex(client, token: str, sent, notebook_id: uuid.UUID, refetch: bool) -> str:
    """실 라우터로 그 노트북 한정 재인덱싱을 연다 — 발행은 대역이 기록만 한다."""
    res = await client.post(
        "/api/v1/admin/rag/reindex",
        json={"notebook_id": str(notebook_id), "refetch": refetch},
        headers=bearer(token),
    )
    assert res.status_code == 200, res.text
    assert (res.json()["enqueued"], res.json()["skipped"]) == (1, 0)
    assert len(sent) == 1
    return res.json()["run_id"]


# ---------------------------------------------------------------------------
# ⑦ 끝난 행 규칙
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error_message"), [("ready", None), ("failed", "이전 실행의 실패 사유")]
)
async def test_plain_ingest_on_finished_row_is_stale_and_writes_nothing(
    db, user_a, stage, status, error_message
):
    """끝난 행에 온 일반 메시지(재인덱싱 아님)는 `stale_message`로 물러나고 아무것도 쓰지 않는다."""
    sid = await _text_source(db, user_a, stage, status=status, error_message=error_message)
    before = await _reload(db, sid)
    snapshot = (before.status, before.updated_at, before.error_message)

    result = await _run(str(sid))

    assert result == {"ok": False, "error": "stale_message"}
    after = await _reload(db, sid)
    assert (after.status, after.updated_at, after.error_message) == snapshot
    assert stage["puts"] == []
    assert _statuses(stage, sid) == []

    # 제품 재발행 경로처럼 먼저 pending으로 바꾸면 처음부터 다시 파싱한다
    await db.execute(
        update(Source).where(Source.id == sid).values(status="pending", error_message=None)
    )
    await db.commit()

    result = await _run(str(sid))

    assert result["ok"] is True, result
    assert result["handoff"]["claim_from"] == "chunking"
    assert (await _reload(db, sid)).status == "chunking"
    assert f"{sid}/parsed.md" in stage["puts"]


# ---------------------------------------------------------------------------
# ⑨ 상태 전이 순서
# ---------------------------------------------------------------------------


async def test_reindex_from_stored_parse_stays_ready_until_index_claims(
    client: httpx.AsyncClient, db, admin_user, stage, sent
):
    """ⓐ `ready` 소스·`refetch: false` — 파싱 단계는 상태를 쓰지 않고, 색인 단계가 `ready`에서
    점유한다."""
    # 로그인을 먼저 한다 — 뒤의 `expire_all()`이 픽스처 객체(admin_user)를 만료시킨다
    token = await login(client, admin_user.email)
    sid = await _ready_source(db, admin_user, stage)
    source = await _reload(db, sid)
    run_id = await _start_reindex(client, token, sent, source.notebook_id, refetch=False)
    assert sent[0]["args"] == [str(sid)]

    stage["events"].clear()
    result = await _run(str(sid))

    assert result["ok"] is True, result
    assert _statuses(stage, sid) == []
    assert (await _reload(db, sid)).status == "ready"
    assert result["handoff"]["claim_from"] == "ready"
    assert result["handoff"]["keep_previous"] is True
    assert await _item_status(db, run_id, sid) == "queued"

    stage["events"].clear()
    indexed = await _run_index(str(sid), **result["handoff"])

    assert indexed["ok"] is True, indexed
    assert _statuses(stage, sid) == ["embedding", "ready"]
    final = await _reload(db, sid)
    assert final.status == "ready"
    assert final.error_message is None
    assert await _item_status(db, run_id, sid) == "done"


async def test_reindex_with_refetch_returns_to_ready_after_parsing(
    client: httpx.AsyncClient, db, admin_user, stage, sent
):
    """ⓑ `ready` 소스·`refetch: true` — 원본부터 다시 파싱하고 `chunking`이 아니라 `ready`로
    되돌린다."""
    # 로그인을 먼저 한다 — 뒤의 `expire_all()`이 픽스처 객체(admin_user)를 만료시킨다
    token = await login(client, admin_user.email)
    sid = await _ready_source(db, admin_user, stage)
    source = await _reload(db, sid)
    run_id = await _start_reindex(client, token, sent, source.notebook_id, refetch=True)

    stage["events"].clear()
    result = await _run(str(sid))

    assert result["ok"] is True, result
    assert _statuses(stage, sid) == ["parsing", "ready"]
    assert (await _reload(db, sid)).status == "ready"
    assert result["handoff"]["claim_from"] == "ready"
    assert await _item_status(db, run_id, sid) == "queued"

    stage["events"].clear()
    indexed = await _run_index(str(sid), **result["handoff"])

    assert indexed["ok"] is True, indexed
    assert _statuses(stage, sid) == ["embedding", "ready"]
    final = await _reload(db, sid)
    assert final.status == "ready"
    assert final.error_message is None
    assert await _item_status(db, run_id, sid) == "done"


async def test_new_source_goes_through_chunking_handoff(db, user_a, stage):
    """ⓒ 신규 `pending` 소스(재인덱싱 아님) — `parsing → chunking` 뒤 색인 단계가
    `embedding → ready`."""
    sid = await _text_source(db, user_a, stage)

    stage["events"].clear()
    result = await _run(str(sid))

    assert result["ok"] is True, result
    assert _statuses(stage, sid) == ["parsing", "chunking"]
    assert (await _reload(db, sid)).status == "chunking"
    assert result["handoff"]["claim_from"] == "chunking"

    stage["events"].clear()
    indexed = await _run_index(str(sid), **result["handoff"])

    assert indexed["ok"] is True, indexed
    assert _statuses(stage, sid) == ["embedding", "ready"]
    final = await _reload(db, sid)
    assert final.status == "ready"
    assert final.error_message is None
