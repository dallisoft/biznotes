# 신규(#378). 필요: PostgreSQL·Valkey(PG 테스트).
"""인제스트 뒤 소스 요약 미리 만들기 — 실제 DB의 캐시 저장, 다음 run 적중, 생성 중 재인제스트, 귀속.

첫 테스트는 게이트웨이 실경로(mock 프로바이더)와 실제 사용량 원장을 쓴다. 나머지는 모델 호출
(`complete_stream_with_fallback`)만 대역이다. 예약·디바운스·설정 토글은
`tests/test_source_digest_on_ingest.py`가 본다.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import delete, select

from biznotes_core.config import get_settings
from biznotes_core.core.db import get_session_factory
from biznotes_core.models import Notebook, Source, SourceChunk, SourceDigest, User
from biznotes_core.services import source_digest as sd
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.nodes.digest import _exec_source_digest
from biznotes_core.services.gateway.llm import GatewayCallError, StreamResult
from biznotes_core.workers.tasks import source_digest as digest_task

# 청크 하나 약 4,150자(cl100k로 약 4,150토큰) — 둘이면 묶음 2개라 map 2번 + 합치기 1번이다
CHUNK = ("분기 매출은 전년 대비 12% 늘었고 신제품 출시는 3월로 미뤄졌다. " * 120)[:4150]


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """설정을 켜고, 태스크가 워커 엔진 대신 스위트의 세션 팩토리를 쓰게 한다."""
    settings = get_settings().model_copy(update={"source_digest_on_ingest": True})
    monkeypatch.setattr(digest_task, "get_settings", lambda: settings)
    monkeypatch.setattr(digest_task, "get_session_factory", get_session_factory)


class FakeLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False
        self.on_first_call: Callable[[], Awaitable[None]] | None = None

    async def __call__(self, db: Any, model_id: Any, messages: Any, result: StreamResult,
                       **kw: Any) -> Any:
        self.calls += 1
        if self.calls == 1 and self.on_first_call is not None:
            await self.on_first_call()
        if self.fail:
            raise GatewayCallError("LLM 호출 실패")
        result.usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

        async def gen() -> Any:
            yield "요약: 핵심 주장과 수치"

        return gen()


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr(sd, "complete_stream_with_fallback", fake)
    return fake


async def _source(
    db: Any, owner: User, texts: list[str], status: str = "ready"
) -> tuple[Notebook, Source]:
    nb = Notebook(owner_id=owner.id, title="요약")
    db.add(nb)
    await db.flush()
    src = Source(
        notebook_id=nb.id, type="text", title="자료", status=status, chunk_count=len(texts)
    )
    db.add(src)
    await db.flush()
    db.add_all(
        SourceChunk(source_id=src.id, seq=k, text=t)
        for k, t in enumerate(texts)
    )
    await db.commit()
    return nb, src


async def _rows() -> dict[uuid.UUID, SourceDigest]:
    async with get_session_factory()() as s:
        return {r.source_id: r for r in await s.scalars(select(SourceDigest))}


async def _replace_chunks(source_id: uuid.UUID, text: str) -> None:
    """재인제스트 — 다른 세션에서 청크를 지우고 다시 넣어 커밋한다(색인 교체와 같은 모양)."""
    async with get_session_factory()() as s:
        await s.execute(delete(SourceChunk).where(SourceChunk.source_id == source_id))
        s.add(SourceChunk(source_id=source_id, seq=0, text=text))
        await s.commit()


async def test_pregenerated_digest_makes_the_next_run_free(db, user_a, mock_llm, enabled):
    """수락 기준 1 — 미리 만든 요약이 캐시되고, 다음 `source_digest` run의 LLM 호출이 0회다."""
    texts = [f"자료 0 {CHUNK}", f"자료 1 {CHUNK}"]
    nb, src = await _source(db, user_a, texts)

    out = await digest_task._run(str(src.id))

    assert out == {"ok": True, "calls": 3}  # map 2번 + 합치기 1번
    [row] = (await _rows()).values()
    assert (row.source_id, row.content_hash) == (src.id, sd.content_hash_of(texts))
    assert row.model_name == "Mock"  # 미리 만들기는 지금처럼 기본 모델이다(#396)

    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=nb.id, db=db, params={})
    node = await _exec_source_digest(ctx, {"id": "digest"}, {})

    assert [s["cached"] for s in node["sources"]] == [True]
    assert row.digest in node["context"]
    # 같은 내용으로 다시 예약돼도 만들지 않는다
    assert await digest_task._run(str(src.id)) == {"ok": True, "skipped": "cached"}


async def test_reingest_during_generation_keeps_only_the_latest_content(
    db, user_a, llm, enabled
):
    """수락 기준 3 — 만드는 동안 청크가 바뀌면 옛 내용의 요약을 남기지 않는다."""
    _, src = await _source(db, user_a, ["첫 본문"])
    llm.on_first_call = lambda: _replace_chunks(src.id, "바뀐 본문")

    out = await digest_task._run(str(src.id))

    assert out == {"ok": True, "skipped": "superseded", "calls": 1}
    assert await _rows() == {}

    # 재인제스트가 예약한 다음 태스크가 새 내용으로 만든다 — 행은 소스당 하나다
    assert await digest_task._run(str(src.id)) == {"ok": True, "calls": 1}
    [row] = (await _rows()).values()
    assert row.content_hash == sd.content_hash_of(["바뀐 본문"])

    await _replace_chunks(src.id, "세 번째 본문")
    assert await digest_task._run(str(src.id)) == {"ok": True, "calls": 1}
    [row] = (await _rows()).values()
    assert row.content_hash == sd.content_hash_of(["세 번째 본문"])


async def test_a_late_store_never_overwrites_the_newer_digest(
    db, user_a, llm, enabled, monkeypatch
):
    """독립 리뷰 M — 태스크 A가 최종 지문 확인을 통과하고 저장 직전에 멈춘 사이 재인제스트와 태스크
    B가 새 요약을 저장해도, 늦게 저장하는 A가 그것을 옛 요약으로 덮지 않는다."""
    _, src = await _source(db, user_a, ["첫 본문"])
    real_store = sd.store_digest

    async def paused(source_id: uuid.UUID, content_hash: str, result: Any) -> bool:
        monkeypatch.setattr(sd, "store_digest", real_store)
        await _replace_chunks(src.id, "바뀐 본문")
        assert await digest_task._run(str(src.id)) == {"ok": True, "calls": 1}  # B
        return await real_store(source_id, content_hash, result)

    monkeypatch.setattr(sd, "store_digest", paused)

    assert await digest_task._run(str(src.id)) == {"ok": False, "calls": 1}  # A는 쓰지 못한다
    [row] = (await _rows()).values()
    assert row.content_hash == sd.content_hash_of(["바뀐 본문"])


async def test_unready_missing_and_empty_sources_are_skipped(db, user_a, llm, enabled):
    _, busy = await _source(db, user_a, ["본문"], status="embedding")
    _, empty = await _source(db, user_a, [])

    assert await digest_task._run(str(busy.id)) == {"ok": True, "skipped": "not_ready"}
    assert await digest_task._run(str(empty.id)) == {"ok": True, "skipped": "no_content"}
    assert await digest_task._run(str(uuid.uuid4())) == {"ok": True, "skipped": "source_deleted"}
    assert llm.calls == 0


async def test_generation_failure_stores_nothing(db, user_a, llm, enabled):
    _, src = await _source(db, user_a, ["본문"])
    llm.fail = True

    assert await digest_task._run(str(src.id)) == {"ok": False, "error": "model_call_failed"}
    assert await _rows() == {}
