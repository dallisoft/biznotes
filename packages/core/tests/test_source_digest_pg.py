# 신규(#360). 필요: PostgreSQL·Valkey(PG 테스트).
"""소스 요약 캐시 — 실제 DB의 지문·캐시 적중·재인제스트 무효화·비인가·삭제.

모델 호출(`complete_stream_with_fallback`)만 대역이다. `create_all` 기반이라 Alembic 마이그레이션
검증이 아니다(테이블이 제품 리비전과 같은 모양인지만 본다).
"""

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import delete, select, text

from biznotes_core.core.db import get_session_factory
from biznotes_core.models import (
    LLMModel,
    LLMProvider,
    Notebook,
    Source,
    SourceChunk,
    SourceDigest,
    User,
)
from biznotes_core.services import source_digest as sd
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.nodes.digest import _exec_source_digest
from biznotes_core.services.agents.nodes.llm import _build_messages, _ContextFit
from biznotes_core.services.gateway.llm import (
    GatewayCallError,
    ResolvedModel,
    StreamResult,
    estimate_prompt_tokens,
)

# 소스 하나 약 8,300자(cl100k로 약 8,300토큰) — 36개면 약 30만 토큰이다
CHUNK = ("분기 매출은 전년 대비 12% 늘었고 신제품 출시는 3월로 미뤄졌다. " * 120)[:4150]
DIGEST = ("요약: 핵심 주장, 수치 12%, 3월 출시 연기, 절 구성. " * 20)[:450]


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_titles: set[str] = set()

    async def __call__(self, db: Any, model_id: Any, messages: Any, result: StreamResult,
                       **kw: Any) -> Any:
        system, user = messages[0]["content"], messages[1]["content"]
        self.calls.append(system)
        await asyncio.sleep(0)
        if any(f"문서 제목: {t}\n" in user for t in self.fail_titles):
            raise GatewayCallError("LLM 호출 실패")
        result.usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

        async def gen() -> Any:
            yield DIGEST

        return gen()


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr(sd, "complete_stream_with_fallback", fake)
    return fake


async def _notebook(
    db: Any, owner: User, count: int, chunks: int = 2
) -> tuple[Notebook, list[Source]]:
    nb = Notebook(owner_id=owner.id, title="요약")
    db.add(nb)
    await db.flush()
    # 한 트랜잭션이라 created_at이 같다 — 노드는 (created_at, id) 순이므로(#391) id를 오름차순으로
    # 줘 「자료 1」부터 나오게 한다
    ids = sorted(uuid.uuid4() for _ in range(count))
    sources = [
        Source(
            id=ids[i],
            notebook_id=nb.id,
            type="text",
            title=f"자료 {i + 1}",
            status="ready",
            chunk_count=chunks,
        )
        for i in range(count)
    ]
    db.add_all(sources)
    await db.flush()
    db.add_all(
        SourceChunk(source_id=s.id, seq=k, text=f"{s.title} {k} {CHUNK}")
        for s in sources
        for k in range(chunks)
    )
    await db.commit()
    return nb, sources


def _ctx(db: Any, nb: Notebook, source_ids: list[uuid.UUID] | None = None) -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(), notebook_id=nb.id, db=db, params={}, source_ids=source_ids
    )


async def _digest(db: Any, nb: Notebook, **kw: Any) -> dict[str, Any]:
    return await _exec_source_digest(_ctx(db, nb, **kw), {"id": "digest"}, {})


async def _rows() -> dict[uuid.UUID, SourceDigest]:
    async with get_session_factory()() as s:
        return {r.source_id: r for r in await s.scalars(select(SourceDigest))}


async def test_all_36_sources_fit_a_32k_model_and_the_second_run_is_free(db, user_a, llm):
    nb, sources = await _notebook(db, user_a, 36)

    out = await _digest(db, nb)

    assert [s["title"] for s in out["sources"]] == [s.title for s in sources]
    assert out["context"].count('<untrusted_data kind="source">') == 36
    assert out["degraded"] == [] and not any(s["cached"] for s in out["sources"])
    assert len(llm.calls) == 36 * 3  # 소스마다 묶음 2개 + 합치기 1번(map-reduce)
    stored = await _rows()
    assert set(stored) == {s.id for s in sources}
    assert {r.prompt_version for r in stored.values()} == {sd.PROMPT_VERSION}

    # 뒤의 llm 노드 프롬프트가 32k 모델의 출력 몫(8192)을 남기고 들어간다
    prompt = "다음 요약으로 발표 개요를 만드세요.\n\n{digest.context}"
    state = {"digest": out}
    base = _build_messages("", prompt, state, None)
    provider = LLMProvider(id=uuid.uuid4(), name="p", type="mock")
    small = ResolvedModel(
        model=LLMModel(
            id=uuid.uuid4(), provider_id=provider.id, model_name="gpt-oss-120b",
            display_name="gpt-oss-120b", capabilities=["llm"], context_window=32_768,
        ),
        provider=provider,
    )

    fit = _ContextFit("", prompt, state, 8192, _ContextFit.find_refs([prompt], state), base)
    sent = fit(small)
    assert estimate_prompt_tokens(sent) + 8192 <= 32_768
    assert all(f"## {s.title}\n" in sent[-1]["content"] for s in sources)

    # 같은 소스로 두 번째 run — 요약 LLM 호출 0회
    again = await _digest(db, nb)
    assert len(llm.calls) == 36 * 3
    assert all(s["cached"] for s in again["sources"])
    assert again["context"] == out["context"]


async def test_reingested_source_is_summarised_again(db, user_a, llm):
    nb, (a, b) = await _notebook(db, user_a, 2, chunks=1)
    await _digest(db, nb)
    first = await _rows()
    calls = len(llm.calls)

    # 재인제스트 — 청크를 지우고 다른 내용으로 다시 넣는다(색인 교체와 같은 모양)
    await db.execute(delete(SourceChunk).where(SourceChunk.source_id == b.id))
    db.add(SourceChunk(source_id=b.id, seq=0, text="완전히 바뀐 본문"))
    await db.commit()

    out = await _digest(db, nb)

    assert len(llm.calls) == calls + 1  # b만 다시 만든다
    assert [s["cached"] for s in out["sources"]] == [True, False]
    after = await _rows()
    assert after[a.id].content_hash == first[a.id].content_hash
    assert after[b.id].content_hash == sd.content_hash_of(["완전히 바뀐 본문"])
    assert after[b.id].content_hash != first[b.id].content_hash


async def test_pg_fingerprint_matches_the_python_one(db, user_a):
    nb, (src,) = await _notebook(db, user_a, 1, chunks=3)
    texts = await sd.chunk_texts(db, src.id)
    assert (await sd.content_hashes(db, [src.id]))[src.id] == sd.content_hash_of(texts)


async def test_failed_source_gets_a_fallback_and_is_retried_next_run(db, user_a, llm):
    nb, (a, b) = await _notebook(db, user_a, 2, chunks=1)
    llm.fail_titles = {"자료 2"}

    out = await _digest(db, nb)

    assert [s["title"] for s in out["sources"]] == ["자료 1", "자료 2"]
    [deg] = out["degraded"]
    assert (deg["title"], deg["reason"], deg["fallback"]) == (
        "자료 2", "generation_failed", "excerpt"
    )
    assert "자료 2 0 분기 매출" in out["context"]  # 앞부분 발췌
    assert set(await _rows()) == {a.id}  # 실패한 소스는 캐시하지 않는다

    llm.fail_titles = set()
    again = await _digest(db, nb)
    assert again["degraded"] == [] and set(await _rows()) == {a.id, b.id}


async def test_other_notebook_digests_are_never_read(db, user_a, user_b, llm):
    nb, (own,) = await _notebook(db, user_a, 1, chunks=1)
    other_nb, (foreign,) = await _notebook(db, user_b, 1, chunks=1)
    await _digest(db, other_nb)  # 다른 노트북에도 캐시가 있다
    assert foreign.id in await _rows()

    # run 범위에 다른 노트북 소스 id를 넣어도 그 소스·요약은 나오지 않는다
    out = await _digest(db, nb, source_ids=[own.id, foreign.id])
    assert [s["id"] for s in out["sources"]] == [str(own.id)]
    assert "자료 1" in out["context"] and out["context"].count("<untrusted_data") == 1
    # 노트북 전체 범위도 같다
    whole = await _digest(db, nb)
    assert [s["id"] for s in whole["sources"]] == [str(own.id)]


async def test_deleting_the_source_deletes_its_digest(db, user_a, llm):
    nb, (src,) = await _notebook(db, user_a, 1, chunks=1)
    await _digest(db, nb)
    assert src.id in await _rows()

    await db.execute(text("DELETE FROM sources WHERE id = :id"), {"id": src.id})
    await db.commit()

    assert await _rows() == {}


async def test_store_is_an_upsert(db, user_a):
    nb, (src,) = await _notebook(db, user_a, 1, chunks=1)
    current = sd.content_hash_of(await sd.chunk_texts(db, src.id))
    one = sd.DigestResult(text="첫 요약", token_count=3)
    two = sd.DigestResult(text="둘째 요약", token_count=4)
    assert await sd.store_digest(src.id, current, one)
    assert await sd.store_digest(src.id, current, two)
    row = (await _rows())[src.id]
    assert (row.content_hash, row.digest, row.token_count) == (current, "둘째 요약", 4)
    # 지금 청크와 다른 지문(만드는 사이 재인제스트된 내용)은 쓰지 않는다 — 있던 행도 그대로다
    # (#378 독립 리뷰 M)
    assert not await sd.store_digest(src.id, sd.content_hash_of(["옛 본문"]), one)
    assert (await _rows())[src.id].digest == "둘째 요약"
    # 지운 소스에는 저장하지 못해도 예외를 올리지 않는다
    assert not await sd.store_digest(uuid.uuid4(), "h", one)


async def test_table_matches_the_revision_shape(db):
    rows = (
        await db.execute(
            text(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'source_digests' ORDER BY ordinal_position"
            )
        )
    ).all()
    assert {r[0]: (r[1], r[2]) for r in rows} == {
        "source_id": ("uuid", "NO"),
        "content_hash": ("character varying", "NO"),
        "digest": ("text", "NO"),
        "token_count": ("integer", "NO"),
        "model_id": ("uuid", "YES"),
        "model_name": ("character varying", "YES"),
        "prompt_version": ("character varying", "NO"),
        "created_at": ("timestamp with time zone", "NO"),
        "updated_at": ("timestamp with time zone", "NO"),
    }


# ── 생성 모델 = run에서 고른 모델(#396) — 실제 게이트웨이(mock 프로바이더) ──────────


class GatewaySpy:
    """실제 `complete_stream_with_fallback`을 부르고 넘긴 1차 모델을 기록한다."""

    def __init__(self, real: Any) -> None:
        self.real = real
        self.model_ids: list[Any] = []

    async def __call__(self, db: Any, model_id: Any, *a: Any, **kw: Any) -> Any:
        self.model_ids.append(model_id)
        return await self.real(db, model_id, *a, **kw)


async def _two_models(db: Any) -> tuple[LLMModel, LLMModel]:
    """기본 mock 모델과 run에서 고를 mock 모델."""
    provider = LLMProvider(name=f"mock-{uuid.uuid4().hex[:6]}", type="mock", enabled=True)
    db.add(provider)
    await db.flush()
    default, chosen = (
        LLMModel(provider_id=provider.id, model_name=name, display_name=name,
                 capabilities=["llm"], is_default=is_default, enabled=True)
        for name, is_default in (("기본 모델", True), ("고른 모델", False))
    )
    db.add_all([default, chosen])
    await db.commit()
    return default, chosen


async def test_run_model_makes_the_digest_and_the_cache_is_shared(db, user_a, monkeypatch):
    default, chosen = await _two_models(db)
    spy = GatewaySpy(sd.complete_stream_with_fallback)
    monkeypatch.setattr(sd, "complete_stream_with_fallback", spy)
    nb, (a, b) = await _notebook(db, user_a, 2, chunks=1)

    def ctx(model: LLMModel) -> RunContext:
        return RunContext(run_id=uuid.uuid4(), notebook_id=nb.id, db=db, params={},
                          run_model_id=model.id)

    out = await _exec_source_digest(ctx(chosen), {"id": "digest"}, {})

    assert spy.model_ids and set(spy.model_ids) == {chosen.id}
    rows = await _rows()
    assert {r.model_id for r in rows.values()} == {chosen.id}  # 실제로 만든 모델
    assert [(s["model_id"], s["model_name"]) for s in out["sources"]] == [
        (str(chosen.id), "고른 모델")
    ] * 2

    # 다른 모델을 고른 run — 캐시를 그대로 쓰고 모델을 부르지 않는다
    calls = len(spy.model_ids)
    shared = await _exec_source_digest(ctx(default), {"id": "digest"}, {})
    assert len(spy.model_ids) == calls
    assert [(s["cached"], s["model_name"]) for s in shared["sources"]] == [(True, "고른 모델")] * 2

    # 다시 만들기 — b만 고른 모델이 아니게 해 두면 b만 다시 만들어 덮는다
    async with get_session_factory()() as s:
        await s.execute(
            SourceDigest.__table__.update()
            .where(SourceDigest.source_id == b.id)
            .values(model_id=default.id, model_name="기본 모델")
        )
        await s.commit()
    refreshed = await _exec_source_digest(
        ctx(chosen), {"id": "digest", "refresh_other_models": "{params.refresh}"},
        {"params": {"refresh": True}},
    )
    assert spy.model_ids[calls:] == [chosen.id]
    assert [s["cached"] for s in refreshed["sources"]] == [True, False]
    after = await _rows()
    assert (after[a.id].model_id, after[b.id].model_id) == (chosen.id, chosen.id)

    # 모델을 지우면 행의 model_id는 NULL이다 — 다시 만들기 대상이 된다
    await db.execute(delete(LLMModel).where(LLMModel.id == default.id))
    await db.commit()
    async with get_session_factory()() as s:
        await s.execute(
            SourceDigest.__table__.update()
            .where(SourceDigest.source_id == a.id)
            .values(model_id=None)
        )
        await s.commit()
    calls = len(spy.model_ids)
    await _exec_source_digest(ctx(chosen), {"id": "digest", "refresh_other_models": True}, {})
    assert spy.model_ids[calls:] == [chosen.id]
    assert (await _rows())[a.id].model_id == chosen.id
