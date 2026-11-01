"""소스가 많아 소스당 몫이 작을 때 노트북 개요 입력을 배치 reduce로 줄인다(#450).

`generate_overview`와 #448 배치 함수(`plan_batches`·`generate_batch_digest`·배치 캐시)는 실경로다.
LLM 스트림(개요·배치 요약)·DB 세션·배치 캐시의 Redis·1차 모델 해석만 대역이다. 실 Redis 왕복은
`tests/test_source_digest_batch_pg.py`(#448)가 본다.
"""

import json
import re
import uuid
from typing import Any

import pytest

from biznotes_core.config import get_settings
from biznotes_core.services import overview
from biznotes_core.services import source_digest as sd
from biznotes_core.services.gateway.llm import GatewayCallError
from tests.test_overview_all_sources import _DB, _block_body, _capture_llm, _ready, _source
from tests.test_source_digest import FakeLLM
from tests.test_source_digest_batch import BrokenRedis, FakeRedis, _batch_reply, _window

USER_TAIL = "\n\n위 소스를 요약하고 질문을 제안하세요."
# #451의 개요 지시 — 소스가 적은 노트북은 글자 하나까지 같아야 한다
OVERVIEW_PROMPT_451 = (
    "당신은 노트북 소스를 요약하는 어시스턴트입니다.\n"
    "아래 untrusted_data 블록의 [n] 줄은 노트북에 등록된 소스마다 하나씩 있고,\n"
    "각 줄은 그 소스의 요약(없으면 앞부분 발췌)입니다.\n"
    "일부 소스만이 아니라 모든 줄을 고르게 반영하세요.\n"
    + overview.UNTRUSTED_DATA_RULE
    + "\n\n"
    "다음 JSON 형식으로만 답하세요 (다른 텍스트 금지):\n"
    '{"summary": "<소스 전체를 아우르는 3~5문장 한국어 요약>", '
    '"questions": ["<소스 내용으로 답할 수 있는 한국어 질문>", "..."], '
    '"title_suggestions": ["<소스 내용을 대표하는 간결한 노트북 제목 후보(10자 내외)>", "..."]}\n\n'
    "질문은 3~5개, 서로 다른 주제를 다루도록 작성하세요.\n"
    "제목 후보는 1~3개, 서로 다른 관점으로 작성하세요."
)
SUMMARY = "주제 가나다라 " * 70  # 소스 가이드 요약 하나 — 하한(150토큰)보다 길다


def _many(n: int, body: str = SUMMARY) -> list[Any]:
    # 제목이 서로의 부분 문자열이 아니게 자릿수를 맞춘다(배치 요약의 빠진 제목 검사)
    return [_source(i, summary=f"S{i:03d} {body}", title=f"자료-{i:03d}") for i in range(n)]


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(sd, "_redis_client", lambda: fake)
    return fake


@pytest.fixture
def batch_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    _window(monkeypatch, None)  # 묶음 상한은 설정 `source_digest_group_tokens`
    fake = FakeLLM(_batch_reply())
    monkeypatch.setattr(sd, "complete_stream_with_fallback", fake)
    return fake


def _settings(monkeypatch: pytest.MonkeyPatch, **update: Any) -> None:
    monkeypatch.setattr(overview, "get_settings", lambda: get_settings().model_copy(update=update))


# ── 수락 기준 1: 소스 500개에서 소스당 입력이 기준 길이 이상 남는다 ───────


async def test_500_sources_reduce_in_batches_keeping_each_source_above_the_minimum(
    monkeypatch: pytest.MonkeyPatch, redis: FakeRedis, batch_llm: FakeLLM
) -> None:
    sources = _many(500)
    _ready(monkeypatch, sources)
    seen = _capture_llm(monkeypatch, "{}")
    minimum = get_settings().source_digest_min_tokens_per_source

    result = await overview.generate_overview(_DB(), uuid.uuid4())  # type: ignore[arg-type]

    assert result is not None and result.source_count == 500
    # 소스마다 본문 전체가 배치 요약 입력에 한 번 들어간다 — 기준(소스당 하한 토큰) 이상
    batch_inputs = [user for _, user in batch_llm.calls]
    assert len(batch_inputs) > 1
    for s in sources:
        holders = [u for u in batch_inputs if f"문서 제목: {s.title}\n\n{s.summary}" in u]
        assert len(holders) == 1, s.title
    assert sd.count_tokens(sources[0].summary) >= minimum
    # #451처럼 한 줄씩 담았다면 소스당 몫은 하한보다 훨씬 작다
    assert overview.MAX_CONTEXT_CHARS / 500 < minimum
    # 개요 호출은 1번, 묶음마다 한 줄이고 모든 소스 제목이 남는다(누락 0)
    assert len(seen) == 1
    system, user = seen[0][0]["content"], seen[0][1]["content"]
    assert system == overview.OVERVIEW_BATCH_PROMPT
    body = _block_body(user)
    lines = body.split("\n")
    assert len(lines) == len(batch_inputs)
    assert [re.match(r"\[(\d+)\] ", line).group(1) for line in lines] == [  # type: ignore[union-attr]
        str(n) for n in range(1, len(lines) + 1)
    ]
    assert all(s.title in body for s in sources)
    assert len(body) <= overview.MAX_CONTEXT_CHARS
    # 배치 요약은 캐시에 남는다
    assert len(redis.data) == len(batch_inputs)


async def test_second_overview_reuses_batch_summaries_from_cache(
    monkeypatch: pytest.MonkeyPatch, redis: FakeRedis, batch_llm: FakeLLM
) -> None:
    _ready(monkeypatch, _many(300))
    seen = _capture_llm(monkeypatch, "{}")

    await overview.generate_overview(_DB(), uuid.uuid4())  # type: ignore[arg-type]
    first = len(batch_llm.calls)
    assert len(redis.data) == first
    await overview.generate_overview(_DB(), uuid.uuid4())  # type: ignore[arg-type]

    assert first > 1 and len(batch_llm.calls) == first  # 두 번째는 배치 호출 0
    assert len(seen) == 2 and seen[0][1]["content"] == seen[1][1]["content"]


async def test_appended_source_regenerates_only_the_last_batch(
    monkeypatch: pytest.MonkeyPatch, redis: FakeRedis, batch_llm: FakeLLM
) -> None:
    sources = _many(301)
    _capture_llm(monkeypatch, "{}")
    _ready(monkeypatch, sources[:300])
    await overview.generate_overview(_DB(), uuid.uuid4())  # type: ignore[arg-type]
    first = len(batch_llm.calls)

    _ready(monkeypatch, sources)
    await overview.generate_overview(_DB(), uuid.uuid4())  # type: ignore[arg-type]

    # 소스마다 몫이 그대로라 앞 묶음들은 캐시에서 읽고, 새 소스가 든 마지막 묶음만 다시 만든다
    assert first > 1
    assert len(batch_llm.calls) == first + 1
    assert f"문서 제목: {sources[300].title}" in batch_llm.calls[-1][1]


# ── Redis·배치 실패: 개요는 만들어지고 소스는 빠지지 않는다 ──────────────


async def test_redis_outage_is_a_cache_miss(
    monkeypatch: pytest.MonkeyPatch, batch_llm: FakeLLM
) -> None:
    monkeypatch.setattr(sd, "_redis_client", lambda: BrokenRedis())
    sources = _many(300)
    _ready(monkeypatch, sources)
    seen = _capture_llm(monkeypatch, "{}")

    result = await overview.generate_overview(_DB(), uuid.uuid4())  # type: ignore[arg-type]

    assert result is not None and len(batch_llm.calls) > 1 and len(seen) == 1
    body = _block_body(seen[0][1]["content"])
    assert all(s.title in body for s in sources)


async def test_failed_batch_summary_falls_back_to_a_title_list(
    monkeypatch: pytest.MonkeyPatch, redis: FakeRedis
) -> None:
    _window(monkeypatch, None)
    failing = FakeLLM(lambda s, u: GatewayCallError("down"))
    monkeypatch.setattr(sd, "complete_stream_with_fallback", failing)
    sources = _many(300)
    _ready(monkeypatch, sources)
    seen = _capture_llm(monkeypatch, "{}")

    result = await overview.generate_overview(_DB(), uuid.uuid4())  # type: ignore[arg-type]

    assert result is not None and len(seen) == 1
    body = _block_body(seen[0][1]["content"])
    assert all(f"- {s.title}" in body for s in sources)
    assert redis.data == {}  # 실패한 묶음은 캐시하지 않는다


# ── 수락 기준 2·하위 호환: 예산 안이면 #451과 같다(LLM 1회) ──────────────


async def test_small_notebook_overview_matches_issue_451(
    monkeypatch: pytest.MonkeyPatch, batch_llm: FakeLLM
) -> None:
    def no_redis() -> Any:
        raise AssertionError("예산 안에서는 배치 캐시를 읽지 않는다")

    monkeypatch.setattr(sd, "_redis_client", no_redis)
    sources = [_source(i, summary=f"요약 {i}", title=f"제목{i}") for i in range(3)]
    _ready(monkeypatch, sources)
    raw = json.dumps({"summary": "전체 요약", "questions": ["q1"]}, ensure_ascii=False)
    seen = _capture_llm(monkeypatch, raw)

    result = await overview.generate_overview(_DB(), uuid.uuid4())  # type: ignore[arg-type]

    assert batch_llm.calls == [] and len(seen) == 1
    assert seen[0] == [
        {"role": "system", "content": OVERVIEW_PROMPT_451},
        {
            "role": "user",
            "content": '<untrusted_data kind="source">\n[1] 요약 0\n[2] 요약 1\n[3] 요약 2\n'
            "</untrusted_data>" + USER_TAIL,
        },
    ]
    assert result is not None
    assert (result.summary, result.suggested_questions, result.source_count) == (
        "전체 요약",
        ["q1"],
        3,
    )


async def test_over_budget_with_enough_share_per_source_keeps_one_call(
    monkeypatch: pytest.MonkeyPatch, batch_llm: FakeLLM
) -> None:
    # 줄 합계는 예산을 넘지만 소스당 몫(약 600자)이 하한보다 크다 — #451처럼 고르게 자른다
    sources = _many(40, body="가나다라 " * 250)
    _ready(monkeypatch, sources)
    seen = _capture_llm(monkeypatch, "{}")

    await overview.generate_overview(_DB(), uuid.uuid4())  # type: ignore[arg-type]

    assert batch_llm.calls == [] and len(seen) == 1
    assert seen[0][0]["content"] == overview.OVERVIEW_PROMPT
    body = _block_body(seen[0][1]["content"])
    assert len(body.split("\n")) == 40 and len(body) <= overview.MAX_CONTEXT_CHARS


async def test_zero_minimum_share_disables_batch_reduce(
    monkeypatch: pytest.MonkeyPatch, batch_llm: FakeLLM
) -> None:
    _settings(monkeypatch, source_digest_min_tokens_per_source=0)
    _ready(monkeypatch, _many(500))
    seen = _capture_llm(monkeypatch, "{}")

    await overview.generate_overview(_DB(), uuid.uuid4())  # type: ignore[arg-type]

    assert batch_llm.calls == [] and len(seen) == 1
    assert len(_block_body(seen[0][1]["content"]).split("\n")) == 500


# ── 배치 입력 본문 상한 ──────────────────────────────────────────────────


async def test_batch_item_bodies_are_capped_at_one_source_digest(
    monkeypatch: pytest.MonkeyPatch, redis: FakeRedis, batch_llm: FakeLLM
) -> None:
    # 첫 청크·digest가 길어도 배치 입력에는 소스 요약 하나 분량(`source_digest_target_tokens`)만
    _settings(monkeypatch, source_digest_target_tokens=200)
    sources = _many(300, body="가나다라 " * 600)
    _ready(monkeypatch, sources)
    _capture_llm(monkeypatch, "{}")

    await overview.generate_overview(_DB(), uuid.uuid4())  # type: ignore[arg-type]

    entries = [
        entry.split("\n\n", 1)[1]
        for _, user in batch_llm.calls
        for entry in _block_body(user).split("\n\n---\n\n")
    ]
    assert len(entries) == 300
    assert all(e.startswith("S") and 150 <= sd.count_tokens(e) <= 210 for e in entries)


# ── 추천 질문 대체 문구: 앞 5개가 아니라 모든 소스에서 고르게 ─────────────


def test_fallback_questions_spread_over_all_sources() -> None:
    titles = [f"자료-{i:03d}" for i in range(500)]

    _, questions, _ = overview._parse_overview_response("그냥 문장", titles)

    assert questions == [
        f"{t}의 핵심 내용은 무엇인가요?"
        for t in ["자료-000", "자료-125", "자료-250", "자료-374", "자료-499"]
    ]


@pytest.mark.parametrize("count", [1, 3, 5])
def test_fallback_questions_for_five_or_fewer_sources_are_unchanged(count: int) -> None:
    titles = [f"제목{i}" for i in range(count)]

    _, questions, _ = overview._parse_overview_response("그냥 문장", titles)

    assert questions == [f"{t}의 핵심 내용은 무엇인가요?" for t in titles]
