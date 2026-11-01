"""`sources` write-site 가드 — biznotes-core product-guide §6 (계획 hwp-ingest-enable Phase H3).

코어의 parsing heartbeat는 `sources.updated_at`을 **소유권 토큰**으로 쓴다. 워커가 처리 중인
행(`parsing`·`chunking`·`embedding`)을 제품 코드가 쓰면 워커가 소유권을 잃고 작업을 버린다.

대장 `docs/plans/hwp-ingest-enable/source-writers-{app,scripts}.allow.tsv`에서 verdict가
`owner`·`legit_transfer`·`guarded`인 앵커(`<file>::<qualname>::<kind>::<target>`)마다
이 파일에 **문자열 리터럴이 정확히 한 번** 나온다. 코어 게이트
`source_writers.py --check-tests`가 그 1:1 대응을 검사한다. 각 테스트는 앵커의 target
컬럼을 실경로(라우터·시드 함수)로 호출해 단언한다. 대체한 것은 외부 경계(스토리지·벡터스토어·
임베딩 모델·Celery 발행)뿐이다.

재발행 여부는 **Celery 발행 경계(`celery_app.send_task`)**로 센다. 코어 v0.11.0(#163)부터
`stage_ingest_job`은 `ingestion_jobs` 행을 쓰지 않으므로 그 행 수로는 0 == 0 공허 통과가 된다
(core-v0.11.0-upgrade AC-1.12).
"""

import uuid

import httpx
import pytest
from biznotes_core.models import Source
from biznotes_core.workers import task_names
from biznotes_core.workers.celery_app import celery_app
from sqlalchemy import select

from tests.conftest import bearer, login

#: 워커가 소유권을 쥔 단계 — 가이드 §6이 "쓰지 말라"고 한 상태들.
HEARTBEAT_OWNED = ("parsing", "chunking", "embedding")


def _column(anchor: str) -> str:
    return anchor.rsplit("::", 1)[1]


class _Published:
    """`celery_app.send_task` 스파이 — 인제스트 발행(이름·인자·task_id·큐)을 기록한다."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send_task(self, name, args=None, task_id=None, **kwargs):
        self.calls.append({"name": name, "args": list(args or []), "task_id": task_id, **kwargs})

    def for_source(self, source_id: uuid.UUID) -> list[dict]:
        return [
            c
            for c in self.calls
            if c["name"] == task_names.INGEST_SOURCE and c["args"] == [str(source_id)]
        ]


@pytest.fixture
def published(monkeypatch) -> _Published:
    spy = _Published()
    monkeypatch.setattr(celery_app, "send_task", spy.send_task)
    return spy


async def _reload(db, source_id: uuid.UUID) -> Source:
    db.expire_all()
    return (await db.execute(select(Source).where(Source.id == source_id))).scalar_one()


# ---------- app/api/v1/notes.py convert_to_source — legit_transfer ----------


@pytest.fixture
def _notes_storage(monkeypatch):
    stored: dict[tuple[str, str], bytes] = {}

    def fake_put(bucket, key, data, content_type="application/octet-stream"):
        stored[(bucket, key)] = data

    monkeypatch.setattr("app.api.v1.notes.ensure_buckets", lambda: None)
    monkeypatch.setattr("app.api.v1.notes.put_object", fake_put)
    monkeypatch.setattr(
        "biznotes_core.workers.tasks.ingestion.ingest_source.apply_async",
        lambda *a, **k: type("R", (), {"id": "fake"})(),
    )
    return stored


CONVERT_ANCHOR = "app/api/v1/notes.py::convert_to_source::attr_assign::original_object_key"


@pytest.mark.parametrize("status", HEARTBEAT_OWNED)
async def test_convert_to_source_writes_only_the_new_row(
    status, client: httpx.AsyncClient, user_a, db, _notes_storage, published
):
    """`original_object_key`는 같은 트랜잭션에서 방금 만든 `pending` 행(발행 전)에만 쓴다.
    같은 노트북에서 처리 중인 다른 소스는 건드리지 않는다.
    """
    token = await login(client, user_a.email)
    res = await client.post("/api/v1/notebooks", json={"title": "nb"}, headers=bearer(token))
    nb_id = uuid.UUID(res.json()["id"])
    busy = Source(notebook_id=nb_id, type="text", title="처리 중", status=status)
    db.add(busy)
    await db.commit()
    busy_id = busy.id
    busy_before = await _reload(db, busy_id)
    busy_updated_at, busy_key = busy_before.updated_at, busy_before.original_object_key

    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/notes",
        json={"title": "메모", "content": "본문"},
        headers=bearer(token),
    )
    assert res.status_code == 201, res.text
    res = await client.post(
        f"/api/v1/notes/{res.json()['id']}/convert-to-source", headers=bearer(token)
    )
    assert res.status_code == 202, res.text
    new_id = uuid.UUID(res.json()["id"])

    assert new_id != busy_id
    new = await _reload(db, new_id)
    assert getattr(new, _column(CONVERT_ANCHOR)) == f"{new_id}/raw.txt"
    assert new.status == "pending"
    assert len(published.for_source(new_id)) == 1  # 새 행만 발행
    assert published.for_source(busy_id) == []

    busy_after = await _reload(db, busy_id)
    assert busy_after.status == status
    assert getattr(busy_after, _column(CONVERT_ANCHOR)) == busy_key
    assert busy_after.updated_at == busy_updated_at
