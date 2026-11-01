"""소스가 많을 때 계층 요약(배치 reduce)·요약 기반 줄이기·run 생성 상한(#448).

모델 호출과 DB(소스 목록·지문·캐시·청크·저장)는 `test_source_digest.py`와 같은 대역이다. 배치 캐시의
Redis는 사전 대역(`FakeRedis`)이거나 연결이 실패하는 대역이다.
"""

import json
import re
import uuid
from typing import Any

import pytest

from biznotes_core.config import get_settings
from biznotes_core.services import source_digest as sd
from biznotes_core.services import source_digest_backfill
from biznotes_core.services.agents.nodes import digest as digest_node
from biznotes_core.services.agents.nodes.digest import _exec_source_digest
from biznotes_core.services.agents.nodes.llm import _build_messages, _ContextFit
from biznotes_core.services.agents.nodes.retrieval import _refit_source_load, _split_source_load
from biznotes_core.services.gateway.llm import (
    GatewayCallError,
    ResolvedModel,
    estimate_prompt_tokens,
)
from biznotes_core.services.untrusted import is_untrusted_block_sequence
from tests.test_source_digest import FakeLLM, Store, _ctx, _Db, _model, _row, _src

DIGEST = "핵심 주장과 수치 12%를 담은 요약 문장이다. " * 30  # 소스 요약 하나(약 500토큰 남짓)
TITLE_LINE = re.compile(r"^문서 제목: (.+)$", re.M)


def _batch_reply(drop_last: bool = False) -> Any:
    """배치 요약 대역 — 데이터 블록의 `문서 제목:` 줄마다 항목 하나.

    `drop_last`면 마지막 항목을 빠뜨린다."""

    def reply(system: str, user: str) -> str:
        titles = TITLE_LINE.findall(user)
        if drop_last:
            titles = titles[:-1]
        return "\n".join(f"- {t}: 결론과 수치 12%" for t in titles) or "요약"

    return reply


class FakeRedis:
    """배치 캐시 대역 — `MGET`·`SET EX`만."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}

    async def mget(self, keys: list[str]) -> list[bytes | None]:
        return [self.data.get(k) for k in keys]

    async def set(self, key: str, value: str, ex: int) -> bool:
        self.data[key] = value.encode()
        self.ttls[key] = ex
        return True

    async def aclose(self) -> None:
        return None


class BrokenRedis(FakeRedis):
    async def mget(self, keys: list[str]) -> list[bytes | None]:
        raise ConnectionError("redis down")

    async def set(self, key: str, value: str, ex: int) -> bool:
        raise ConnectionError("redis down")


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(sd, "_redis_client", lambda: fake)
    return fake


def _window(monkeypatch: pytest.MonkeyPatch, window: int | None) -> ResolvedModel:
    """1차 모델 해석 대역 — 노드 예산과 배치 묶음 상한이 이 모델 창을 쓴다."""
    model = _model(window)

    async def resolve(db: Any, model_id: Any, modality: str = "llm") -> ResolvedModel:
        return model

    monkeypatch.setattr(sd, "resolve_model", resolve)
    return model


def _settings(monkeypatch: pytest.MonkeyPatch, **update: Any) -> None:
    monkeypatch.setattr(
        digest_node, "get_settings", lambda: get_settings().model_copy(update=update)
    )


def _cached_sources(monkeypatch: pytest.MonkeyPatch, n: int, body: str = DIGEST) -> list[Any]:
    sources = [_src(f"자료-{i:03d}") for i in range(n)]
    store = Store(monkeypatch, sources)
    for s in sources:
        store.cached[s.id] = _row(f"{s.title} {body}")
    return sources


def _items(n: int, body: str = DIGEST) -> list[sd.BatchItem]:
    return [sd.BatchItem(key=f"k{i}", title=f"자료-{i:03d}", body=body) for i in range(n)]


# ── 배치 계획·생성(services) ─────────────────────────────────────────────


async def test_plan_batches_keeps_order_and_shares_the_budget(monkeypatch):
    _window(monkeypatch, None)  # 창을 모르면 설정 묶음 크기를 쓴다
    items = _items(45)

    batches = await sd.plan_batches(None, items, budget_tokens=4500, group_tokens=3000)  # type: ignore[arg-type]

    assert [it for b in batches for it in b.items] == items  # 순서 그대로, 빠짐없이
    assert len(batches) > 1 and all(len(b.items) <= sd.BATCH_MAX_ITEMS for b in batches)
    assert all(b.target_tokens == 100 * len(b.items) for b in batches)  # 4500 / 45 = 100
    for b in batches:  # 묶음 입력이 상한 안이다(항목 하나뿐인 묶음 제외)
        if len(b.items) > 1:
            assert sum(sd.count_tokens(sd._batch_entry(it)) for it in b.items) <= 3000


async def test_batch_digest_restores_titles_the_model_left_out(monkeypatch):
    fake = FakeLLM(_batch_reply(drop_last=True))
    monkeypatch.setattr(sd, "complete_stream_with_fallback", fake)
    batch = sd.DigestBatch(items=tuple(_items(3)), target_tokens=300)

    res = await sd.generate_batch_digest(None, batch)  # type: ignore[arg-type]

    assert len(fake.calls) == 1 and res.calls == 1
    assert all(f"자료-{i:03d}" in res.text for i in range(3))
    assert res.text.count("자료-002") == 1  # 빠진 항목만 덧붙인다
    system, user = fake.calls[0]
    # 지시에는 코어가 정한 값만, 제목은 비신뢰 데이터 블록 안에만 있다
    assert "자료-000" not in system and "문서마다" in system and "300토큰" in system
    assert user.startswith('<untrusted_data kind="source">') and user.count("<untrusted_data") == 1


async def test_batch_digest_empty_answer_is_a_gateway_failure(monkeypatch):
    monkeypatch.setattr(sd, "complete_stream_with_fallback", FakeLLM(lambda s, u: ""))
    batch = sd.DigestBatch(items=tuple(_items(2)), target_tokens=200)
    with pytest.raises(GatewayCallError):
        await sd.generate_batch_digest(None, batch)  # type: ignore[arg-type]


def test_batch_fallback_text_lists_every_title():
    batch = sd.DigestBatch(items=tuple(_items(4)), target_tokens=200)
    text = sd.batch_fallback_text(batch)
    assert [line.split(":", 1)[0] for line in text.splitlines()] == [
        f"- 자료-{i:03d}" for i in range(4)
    ]
    assert sd.count_tokens(text) < 4 * 120  # 항목마다 몫(50토큰) 안팎으로 자른다


def test_batch_cache_key_follows_bodies_model_and_target():
    items = _items(3)
    base = sd.DigestBatch(items=tuple(items), target_tokens=300)
    key = sd.batch_cache_key(base, None)
    same = sd.DigestBatch(items=tuple(_items(3)), target_tokens=300)
    assert key == sd.batch_cache_key(same, None)
    changed = [*items[:2], sd.BatchItem(key="k2", title="자료-002", body="다른 요약")]
    assert key != sd.batch_cache_key(sd.DigestBatch(items=tuple(changed), target_tokens=300), None)
    assert key != sd.batch_cache_key(sd.DigestBatch(items=tuple(items), target_tokens=301), None)
    assert key != sd.batch_cache_key(base, uuid.uuid4())


async def test_batch_cache_round_trip(redis):
    result = sd.DigestResult(text="배치 요약", token_count=3, model=_model())

    assert await sd.store_batch_digest("k1", result, ttl_seconds=600) is True
    hits = await sd.load_batch_cached(["k1", "k2"])

    assert list(hits) == ["k1"] and hits["k1"].text == "배치 요약"
    assert hits["k1"].model_name == "요약 모델"
    assert list(redis.ttls.values()) == [600]
    assert all(k.startswith("source_digest:batch:") for k in redis.data)


async def test_batch_cache_failures_are_misses(monkeypatch):
    monkeypatch.setattr(sd, "_redis_client", lambda: BrokenRedis())
    result = sd.DigestResult(text="배치 요약", token_count=3)

    assert await sd.load_batch_cached(["k1"]) == {}
    assert await sd.store_batch_digest("k1", result, ttl_seconds=600) is False


async def test_corrupt_batch_cache_entry_is_a_miss(redis):
    redis.data["source_digest:batch:k1"] = b"{not json"
    redis.data["source_digest:batch:k2"] = json.dumps({"text": ""}).encode()
    assert await sd.load_batch_cached(["k1", "k2"]) == {}


# ── 노드: 수락 기준 ──────────────────────────────────────────────────────


async def test_500_sources_fit_a_32k_window_and_name_every_source(monkeypatch, redis):
    """수락 1·2 — 소스 500개 + 32k 모델: 창 안에 들고, 500개 모두 `sources`·본문에 남는다."""
    _window(monkeypatch, 32_768)
    sources = _cached_sources(monkeypatch, 500)
    fake = FakeLLM(_batch_reply(drop_last=True))  # 모델이 빠뜨린 제목도 남아야 한다
    monkeypatch.setattr(sd, "complete_stream_with_fallback", fake)

    out = await _exec_source_digest(_ctx(_Db(sources)), {"id": "digest"}, {})

    assert len(out["sources"]) == 500 and out["dropped"] == [] and out["degraded"] == []
    assert sd.count_tokens(out["context"]) <= 32_768 // 2  # 예산 = 창 × 0.5
    assert all(s.title in out["context"] for s in sources)  # 소스 누락 0
    assert is_untrusted_block_sequence(out["context"])
    assert 1 < len(out["blocks"]) == len(fake.calls) < 500
    assert sorted(i for b in out["blocks"] for i in b["source_ids"]) == sorted(
        str(s.id) for s in sources
    )
    assert {s["batch"] for s in out["sources"]} == set(range(1, len(out["blocks"]) + 1))
    assert out["fit"] == "shrink_each"

    # llm 노드(32k, 출력 4,096)에 그대로 들어간다 — 줄이지 않아 모든 제목이 남는다
    prompt = "개요를 만드세요.\n\n{digest.context}"
    state = {"digest": out}
    base = _build_messages("", prompt, state, None)
    fit = _ContextFit("", prompt, state, 4096, _ContextFit.find_refs([prompt], state), base)
    sent = fit(_model(32_768))
    assert estimate_prompt_tokens(sent) + 4096 <= 32_768
    assert all(s.title in sent[-1]["content"] for s in sources)


async def test_second_run_reads_batch_summaries_from_cache(monkeypatch, redis):
    """수락 3 — 소스 요약이 모두 캐시에 있으면 두 번째 실행은 배치 요약도 캐시다(LLM 호출 0)."""
    _window(monkeypatch, 32_768)
    sources = _cached_sources(monkeypatch, 120)
    fake = FakeLLM(_batch_reply())
    monkeypatch.setattr(sd, "complete_stream_with_fallback", fake)

    first = await _exec_source_digest(_ctx(_Db(sources)), {"id": "digest"}, {})
    made = len(fake.calls)
    second = await _exec_source_digest(_ctx(_Db(sources)), {"id": "digest"}, {})

    assert made == len(first["blocks"]) > 1 and len(fake.calls) == made  # 두 번째는 0번
    assert second["context"] == first["context"]
    assert [b["cached"] for b in first["blocks"]] == [False] * made
    assert [b["cached"] for b in second["blocks"]] == [True] * made
    ttl = get_settings().source_digest_batch_cache_ttl_sec
    assert set(redis.ttls.values()) == {ttl}


@pytest.mark.parametrize("window", [None, 1_000_000])
async def test_enough_budget_keeps_todays_output(monkeypatch, redis, window):
    """수락 5 — 예산이 넉넉하거나 창을 모르면 출력이 지금과 같다(배치 없음, LLM 호출 없음)."""
    sources = _cached_sources(monkeypatch, 5)
    fake = FakeLLM(_batch_reply())
    monkeypatch.setattr(sd, "complete_stream_with_fallback", fake)
    baseline = await _exec_source_digest(_ctx(_Db(sources)), {"id": "digest"}, {})

    _window(monkeypatch, window)
    out = await _exec_source_digest(_ctx(_Db(sources)), {"id": "digest"}, {})

    assert out == baseline and "blocks" not in out and fake.calls == []
    assert out["context"].count('<untrusted_data kind="source">') == 5


async def test_explicit_budget_tokens_drives_batching(monkeypatch, redis):
    _window(monkeypatch, None)  # 창을 몰라도 노드 예산이 있으면 그것을 쓴다
    sources = _cached_sources(monkeypatch, 40)
    monkeypatch.setattr(sd, "complete_stream_with_fallback", FakeLLM(_batch_reply()))

    roomy = await _exec_source_digest(_ctx(_Db(sources)), {"id": "d", "budget_tokens": 10**6}, {})
    tight = await _exec_source_digest(_ctx(_Db(sources)), {"id": "d", "budget_tokens": 2000}, {})

    assert "blocks" not in roomy and len(tight["blocks"]) >= 2


async def test_low_shrink_ratio_summarises_instead_of_truncating(monkeypatch, redis):
    """요약 기반 줄이기 — 소스당 몫은 하한 이상이어도 예산/요약 합이 임계 아래면 배치 요약이다."""
    _window(monkeypatch, None)
    sources = _cached_sources(monkeypatch, 4)
    monkeypatch.setattr(sd, "complete_stream_with_fallback", FakeLLM(_batch_reply()))
    total = sum(sd.count_tokens(f"{s.title} {DIGEST}") for s in sources)
    per_floor = get_settings().source_digest_min_tokens_per_source
    assert total * 0.3 / 4 >= per_floor  # 몫 하한은 넘는다

    shrunk = await _exec_source_digest(
        _ctx(_Db(sources)), {"id": "d", "budget_tokens": int(total * 0.3)}, {}
    )
    mild = await _exec_source_digest(
        _ctx(_Db(sources)), {"id": "d", "budget_tokens": int(total * 0.8)}, {}
    )

    assert "blocks" in shrunk and all(s.title in shrunk["context"] for s in sources)
    assert "blocks" not in mild  # 0.8은 llm 노드가 같은 비율로 줄인다(지금과 같다)


async def test_batch_failure_falls_back_to_a_title_list(monkeypatch, redis):
    _window(monkeypatch, 32_768)
    sources = _cached_sources(monkeypatch, 60)
    monkeypatch.setattr(
        sd, "complete_stream_with_fallback", FakeLLM(lambda s, u: GatewayCallError("x"))
    )

    out = await _exec_source_digest(_ctx(_Db(sources)), {"id": "digest"}, {})

    assert all(s.title in out["context"] for s in sources)
    assert {b["fallback"] for b in out["blocks"]} == {"excerpt"}
    assert redis.data == {}  # 실패한 배치는 캐시에 남기지 않는다
    assert out["degraded"] == []  # degraded는 소스 요약 실패만 뜻한다(그대로)


async def test_redis_outage_does_not_fail_the_run(monkeypatch):
    monkeypatch.setattr(sd, "_redis_client", lambda: BrokenRedis())
    _window(monkeypatch, 32_768)
    sources = _cached_sources(monkeypatch, 60)
    fake = FakeLLM(_batch_reply())
    monkeypatch.setattr(sd, "complete_stream_with_fallback", fake)

    out = await _exec_source_digest(_ctx(_Db(sources)), {"id": "digest"}, {})

    assert len(fake.calls) == len(out["blocks"]) > 1  # 캐시 미스로 만들었다
    assert all(s.title in out["context"] for s in sources)


async def test_bad_budget_tokens_fails_before_any_call(monkeypatch):
    from biznotes_core.services.agents.errors import AgentRuntimeError

    fake = FakeLLM()
    monkeypatch.setattr(sd, "complete_stream_with_fallback", fake)
    Store(monkeypatch, [])
    with pytest.raises(AgentRuntimeError, match="budget_tokens"):
        await _exec_source_digest(_ctx(_Db([])), {"id": "d", "budget_tokens": 0}, {})
    assert fake.calls == []


# ── llm 노드 맞춤: 배치 블록 ─────────────────────────────────────────────


async def test_refit_shrinks_batch_blocks_but_keeps_every_source(monkeypatch, redis):
    _window(monkeypatch, 32_768)
    sources = _cached_sources(monkeypatch, 60)
    monkeypatch.setattr(sd, "complete_stream_with_fallback", FakeLLM(_batch_reply()))
    out = await _exec_source_digest(_ctx(_Db(sources)), {"id": "digest"}, {})

    blocks = _split_source_load(out)
    assert blocks is not None and [b["id"] for b, _ in blocks] == [b["id"] for b in out["blocks"]]

    total = sum(len(body) for _, body in blocks)
    refit, cut = _refit_source_load(out, blocks, total // 2)

    assert refit["sources"] == out["sources"]  # 출력 sources는 그대로 전체다
    assert {d["reason"] for d in cut} == {"context_window_partial"}
    assert {d["id"] for d in cut} <= {str(s.id) for s in sources}  # 뺀 것은 소스 단위로 적는다
    assert _split_source_load(refit) is not None


# ── run 안 생성 상한 ─────────────────────────────────────────────────────


async def test_run_generation_cap_defers_extra_sources(monkeypatch):
    sources = [_src(f"자료 {i}", summary="가이드 요약" if i == 4 else None) for i in range(5)]
    store = Store(monkeypatch, sources)
    fake = FakeLLM()
    monkeypatch.setattr(sd, "complete_stream_with_fallback", fake)
    _settings(monkeypatch, source_digest_run_max_generate=2)
    scheduled: list[Any] = []

    async def schedule(notebook_id: Any, *, countdown: int = 0) -> bool:
        scheduled.append(notebook_id)
        return True

    monkeypatch.setattr(source_digest_backfill, "schedule_backfill", schedule)
    ctx = _ctx(_Db(sources))

    out = await _exec_source_digest(ctx, {"id": "digest"}, {})

    assert len(fake.calls) == 2 and store.stored == [sources[0].id, sources[1].id]
    assert [(d["title"], d["reason"], d["fallback"]) for d in out["degraded"]] == [
        ("자료 2", "generation_deferred", "excerpt"),
        ("자료 3", "generation_deferred", "excerpt"),
        ("자료 4", "generation_deferred", "source_guide"),
    ]
    assert len(out["sources"]) == 5 and "가이드 요약" in out["context"]
    assert scheduled == [ctx.notebook_id]  # 넘긴 소스는 백필로 만든다(#454)


async def test_run_generation_cap_skips_refresh_before_missing_digests(monkeypatch):
    """상한 안에서는 캐시가 없는 소스를 먼저 만든다.

    다시 만들기를 넘긴 소스는 캐시를 그대로 쓴다."""
    chosen = uuid.uuid4()
    stale, missing = _src("다른 모델"), _src("캐시 없음")
    store = Store(monkeypatch, [stale, missing])
    store.cached[stale.id] = _row("다른 모델 요약", uuid.uuid4(), "다른 모델")
    fake = FakeLLM()
    monkeypatch.setattr(sd, "complete_stream_with_fallback", fake)
    _settings(monkeypatch, source_digest_run_max_generate=1)
    ctx = _ctx(_Db([stale, missing]), run_model_id=chosen)

    out = await _exec_source_digest(ctx, {"id": "digest", "refresh_other_models": True}, {})

    assert store.stored == [missing.id] and out["degraded"] == []
    assert "다른 모델 요약" in out["context"]


async def test_zero_generation_cap_is_unlimited(monkeypatch):
    sources = [_src(f"자료 {i}") for i in range(6)]
    Store(monkeypatch, sources)
    fake = FakeLLM()
    monkeypatch.setattr(sd, "complete_stream_with_fallback", fake)
    assert get_settings().source_digest_run_max_generate == 0  # 기본값 — 지금과 같다

    out = await _exec_source_digest(_ctx(_Db(sources)), {"id": "digest"}, {})

    assert len(fake.calls) == 6 and out["degraded"] == []
