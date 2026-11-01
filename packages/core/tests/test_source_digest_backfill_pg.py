# 신규(#454). 필요: PostgreSQL·Valkey(PG 테스트).
"""기존 소스 요약 백필 — 실제 DB의 대상 조회·캐시 저장·재개, 실 Redis의 진행률·하루 장부, 귀속.

모델 호출은 게이트웨이 실경로(mock 프로바이더)와 실제 사용량 원장을 쓴다. 하루 장부 키는 테스트마다
고유 접두어로 바꾼다(스위트 Redis는 비우지 않는다). 재개·상한·예약의 세부 분기는
`tests/test_source_digest_backfill.py`가 본다.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from biznotes_core.config import get_settings
from biznotes_core.core.db import get_session_factory
from biznotes_core.models import Notebook, Source, SourceChunk, SourceDigest, User
from biznotes_core.services import source_digest as sd
from biznotes_core.services import source_digest_backfill as backfill
from biznotes_core.workers.tasks import source_digest as digest_task

BASE = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """태스크가 스위트의 세션 팩토리를 쓰고, 하루 장부를 이 테스트만의 키에 쌓게 한다."""
    monkeypatch.setattr(digest_task, "get_session_factory", get_session_factory)
    monkeypatch.setattr(
        backfill, "SPEND_KEY", f"test:{uuid.uuid4().hex}:" + backfill.SPEND_KEY
    )


def _settings(monkeypatch: pytest.MonkeyPatch, **update: Any) -> None:
    settings = get_settings().model_copy(update=update)
    monkeypatch.setattr(digest_task, "get_settings", lambda: settings)


async def _notebook(db: Any, owner: User, specs: list[tuple[str, str, list[str]]]) -> Any:
    """노트북과 소스(제목, 상태, 청크) — 한 트랜잭션이라 `created_at`을 명시해 순서를 고정한다.

    커밋 뒤 ORM 속성을 다시 읽지 않도록 (노트북 id, [소스 id])를 돌려준다.
    """
    nb = Notebook(owner_id=owner.id, title="백필")
    db.add(nb)
    await db.flush()
    nb_id = nb.id
    ids: list[uuid.UUID] = []
    for k, (title, status, texts) in enumerate(specs):
        src = Source(
            notebook_id=nb_id, type="text", title=title, status=status,
            chunk_count=len(texts), created_at=BASE + timedelta(seconds=k),
        )
        db.add(src)
        await db.flush()
        ids.append(src.id)
        db.add_all(
            SourceChunk(source_id=src.id, seq=i, text=t)
            for i, t in enumerate(texts)
        )
    await db.commit()
    return nb_id, ids


async def _digests() -> dict[uuid.UUID, str]:
    async with get_session_factory()() as s:
        return {r.source_id: r.content_hash for r in await s.scalars(select(SourceDigest))}


async def test_backfill_stops_at_the_daily_cap_and_resumes(
    db, user_a, user_b, mock_llm, isolated, monkeypatch
):
    """수락 기준 1·2 — 상한에 닿으면 멈추고 진행률에 남기며, 다시 실행하면 남은 소스만 만든다."""
    nb, (s0, s1, busy, s2) = await _notebook(
        db,
        user_a,
        [
            ("자료 0", "ready", ["본문 0"]),
            ("자료 1", "ready", ["본문 1"]),
            ("처리 중", "embedding", ["본문 x"]),
            ("자료 2", "ready", ["본문 2"]),
        ],
    )
    _, (outside,) = await _notebook(db, user_b, [("다른 노트북", "ready", ["본문 y"])])
    db.add(
        SourceDigest(
            source_id=s0, content_hash=sd.content_hash_of(["본문 0"]), digest="이미 있는 요약",
            token_count=3, prompt_version=sd.PROMPT_VERSION,
        )
    )
    await db.commit()
    # 한 번에 하나씩 — 상한에 닿는 소스가 순서대로 정해진다
    _settings(
        monkeypatch,
        source_digest_backfill_concurrency=1,
        source_digest_backfill_daily_max_sources=1,
    )

    first = await digest_task._backfill(str(nb))

    assert (first["status"], first["stop_reason"]) == ("stopped", "daily_source_cap")
    assert (first["total"], first["generated"], first["skipped"]) == (3, 1, 1)
    assert set(await _digests()) == {s0, s1}
    progress = await backfill.read_progress(nb)
    assert progress is not None and progress.status == "stopped"

    _settings(monkeypatch, source_digest_backfill_daily_max_sources=0)  # 무제한
    second = await digest_task._backfill(str(nb))

    assert (second["status"], second["generated"], second["skipped"]) == ("done", 1, 2)
    digests = await _digests()
    assert set(digests) == {s0, s1, s2}  # 처리 중·다른 노트북 소스는 만들지 않는다
    assert digests[s2] == sd.content_hash_of(["본문 2"])
    assert busy not in digests and outside not in digests
    client = backfill.backfill_redis()
    try:
        assert await client.get(backfill.PENDING_KEY.format(scope=str(nb))) is None
        assert int(await client.hget(backfill._spend_key(), "sources")) == 2
    finally:
        await client.aclose()


async def test_target_pages_follow_created_at_then_id_across_notebooks(db, user_a, user_b):
    owner_b = user_b.id
    nb_a, ids_a = await _notebook(
        db, user_a, [(f"A{k}", "ready", [f"a{k}"]) for k in range(3)]
    )
    nb_b, ids_b = await _notebook(
        db, user_b, [(f"B{k}", "ready", [f"b{k}"]) for k in range(2)] + [("실패", "failed", [])]
    )

    async with get_session_factory()() as s:
        assert await digest_task._count_targets(s, nb_a) == 3
        assert await digest_task._count_targets(s, None) == 5  # ready만 센다
        first = await digest_task._target_page(s, None, None)
        rest = await digest_task._target_page(s, None, (first[1].created_at, first[1].id))
        only_b = await digest_task._target_page(s, nb_b, None)

    # 두 노트북의 k번째 소스는 `created_at`이 같다 — id가 순서를 정한다
    expected = [
        i
        for _, i in sorted(
            [(BASE + timedelta(seconds=k), i) for k, i in enumerate(ids_a)]
            + [(BASE + timedelta(seconds=k), i) for k, i in enumerate(ids_b[:2])]
        )
    ]
    assert [t.id for t in first] == expected
    assert [t.id for t in rest] == expected[2:]
    assert [(t.id, t.owner_id) for t in only_b] == [(i, owner_b) for i in ids_b[:2]]
