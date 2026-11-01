"""슬라이드 자료 방안 ④ 런타임(#218, core-v0.25.0-upgrade PR-6).

시드 정의를 코어 그래프로 실제로 돌린다. 대역은 외부 경계뿐이다 — LLM 게이트웨이(워크플로우
llm 노드·소스 요약 생성), RAG 검색, 객체 저장(`tests/slide_deck_fakes.py`, conftest
`_mock_runtime_deps`).
노트북·소스·청크는 테스트 DB의 실제 행이고 소스 요약 캐시(`source_digests`)도 실제로 쓴다.

- 개요: 고른 소스 전부가 소스 요약으로 개요 프롬프트에 들어가고(고르지 않은 소스는 빠진다), 저장한
  개요에 `sources`·`degraded`가 실린다. 두 번째 run은 요약을 다시 만들지 않는다.
- 요약 모델(#239, 코어 #396): run에서 고른 모델로 요약을 만들고, 다른 모델이 만든 캐시는 그대로
  쓴다. `refresh_digest`를 켜면 다른 모델의 캐시만 다시 만든다. `sources`마다 모델·캐시 여부가
  실린다.
- 덱: 장마다 그 장의 `source_ids`로만 검색한다(run 범위 밖 id는 빠진다). 한 장이 실패하면 빼고
  `coverage.failed`에 남긴다. 장 JSON은 잘리지 않고 덱으로 조립된다. 형식·언어는 loop 공통
  params(코어 #390)로 덱 run 설정을 장마다 받는다(core-v0.26.0-upgrade PR-6). 그 전에 복제한
  사용자 덱(옛 loop — 공통 params 없음)은 장 항목의 형식·언어로 물러난다.
- 검토 경로: 의견이 없으면 개요를 다시 쓰지 않고, 의견이 있으면 개요 llm이 검토한 개요와 의견으로
  고친다.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from biznotes_core.models import (
    Agent,
    LLMModel,
    LLMProvider,
    Notebook,
    Source,
    SourceChunk,
    SourceDigest,
)
from biznotes_core.services.agents.runtime import AgentRuntimeError, RunContext, execute_agent
from sqlalchemy import select, update

from scripts.seed import (
    _SLIDE_OUTLINE_MAX_TOKENS,
    _SLIDE_WRITE_MAX_TOKENS,
    SEED_AGENTS,
    SLIDE_DECK_AGENT_NAME,
    SLIDE_IMAGE_AGENT_NAME,
    SLIDE_OUTLINE_AGENT_NAME,
    SLIDE_WRITE_AGENT_NAME,
    link_loop_bodies,
)
from tests.slide_deck_fakes import ITEM_RE, SlideLLM, SlideSearch, outline_doc, outline_slide

# 웹 「계속」 요청과 같은 바이트(웹 flow.test.ts가 같은 파일로 continueRequest 출력을 대조한다)
LEGACY_PRESENTER = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "web/src/lib/__fixtures__/slide-deck/legacy-presenter-continue.json"
    ).read_text(encoding="utf-8")
)


def _spec(name: str) -> dict:
    [spec] = [s for s in SEED_AGENTS if s["name"] == name]
    return spec


@pytest.fixture
def llm(monkeypatch) -> SlideLLM:
    fake = SlideLLM()
    monkeypatch.setattr(
        "biznotes_core.services.gateway.llm.complete_stream_with_fallback", fake.stream
    )
    monkeypatch.setattr(
        "biznotes_core.services.source_digest.complete_stream_with_fallback", fake.digest_stream
    )
    return fake


@pytest.fixture
def search(monkeypatch, _mock_runtime_deps) -> SlideSearch:
    fake = SlideSearch()
    monkeypatch.setattr("biznotes_core.services.rag_chat.retrieve_context", fake.retrieve)
    return fake


async def _notebook(db, user, titles: list[str], *, empty: tuple[str, ...] = ()) -> tuple:
    """ready 소스(청크 2개씩)를 가진 노트북. `empty` 제목의 소스는 청크가 없다.

    소스 순서는 코어 `source_digest`의 정렬 키인 `created_at` 하나로만 정해진다. 한 트랜잭션에서
    넣으면 `created_at`(서버 기본값 `now()` = 트랜잭션 시각)이 모두 같아 순서가 힙 위치에 달린다 —
    같은 DB의 다른 테스트가 지우고 고친 행 자리에 들어가면 순서가 바뀐다(통합 실행 `-n 4`에서 순서
    단언이 한 번 실패한 원인). 그래서 업로드처럼 시각을 1초씩 벌려 넣고, 첫 소스를 고쳐 힙 위치를
    맨 뒤로 옮긴다 — 순서가 힙 위치에 기대면 이 헬퍼를 쓰는 순서 단언이 매번 실패한다.
    """
    nb = Notebook(owner_id=user.id, title=f"slides-{uuid.uuid4().hex[:6]}")
    db.add(nb)
    await db.flush()
    sources = []
    base = datetime.now(UTC) - timedelta(hours=1)
    for i, title in enumerate(titles):
        src = Source(
            notebook_id=nb.id,
            type="text",
            title=title,
            status="ready",
            created_at=base + timedelta(seconds=i),
        )
        db.add(src)
        await db.flush()
        if title not in empty:
            for seq in range(2):
                db.add(
                    SourceChunk(
                        source_id=src.id,
                        seq=seq,
                        text=f"{title} 본문 {seq} {uuid.uuid4().hex}",
                        meta={},
                    )
                )
        sources.append(src)
    await db.commit()
    if sources:
        await db.execute(update(Source).where(Source.id == sources[0].id).values(summary=None))
        await db.commit()
    return nb, sources


async def _bodies(db) -> dict[str, str]:
    """시드처럼 loop 본문 두 개를 시스템 에이전트로 만든다 — 이름 → id."""
    ids = {}
    for name in (SLIDE_WRITE_AGENT_NAME, SLIDE_IMAGE_AGENT_NAME):
        agent = Agent(scope="system", enabled=True, **_spec(name))
        agent.name = f"{name}-{uuid.uuid4().hex[:6]}"
        db.add(agent)
        await db.flush()
        ids[name] = str(agent.id)
    await db.commit()
    return ids


async def _deck(db, *, user_copy: bool = False) -> dict:
    """시드 덱 정의(본문 연결). `user_copy`면 loop 공통 params(#390) 전에 복제한 사용자 덱처럼
    `slides` loop에서 `params`를 뺀다 — 복제본은 정의를 그대로 복사하고 `make seed`는 system 행만
    고친다(`duplicate_workflow`)."""
    deck = _spec(SLIDE_DECK_AGENT_NAME)["definition"]
    linked = link_loop_bodies(SLIDE_DECK_AGENT_NAME, deck, await _bodies(db))
    if user_copy:
        [loop] = [n for n in linked["nodes"] if n["id"] == "slides"]
        del loop["params"]
    return linked


def _format_rule(value: str) -> str:
    """장 작성 프롬프트의 형식 지시 — 공통 params 값, 비면 장 항목으로 물러난다."""
    return f"형식: 「{value}」(비어 있으면 장 항목의 format) — presenter면 발표자 슬라이드"


def _language_rule(value: str) -> str:
    return f"언어: 「{value}」(비어 있으면 장 항목의 language, 그것도 없으면 장 항목의 title과"


def _ctx(db, nb, source_ids, *, run_model_id: uuid.UUID | None = None, **params) -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(),
        notebook_id=nb.id,
        db=db,
        source_ids=source_ids,
        run_model_id=run_model_id,
        params={
            "format": "detailed",
            "length": "short",
            "language": "한국어",
            "description": "",
            "image_count": 0,
            **params,
        },
    )


def _saved(artifacts: dict, ctx: RunContext, name: str) -> dict:
    [ref] = [a for a in ctx.artifact_keys if a["name"] == name]
    return json.loads(artifacts[ref["key"]])


# ---------- 개요(소스 요약 → 개요) ----------


async def test_outline_reads_every_selected_source_digest(
    db, user_a, llm, search, _mock_runtime_deps
):
    """고른 소스 전부가 개요 입력에 들어간다(누락 0). 고르지 않은 소스는 들어가지 않는다."""
    titles = [f"소스 {n:02d}" for n in range(1, 26)]  # 옛 source_load 상한(20개)보다 많다
    nb, sources = await _notebook(db, user_a, [*titles, "고르지 않은 소스"])
    picked = sources[:-1]
    ctx = _ctx(db, nb, [s.id for s in picked])
    await execute_agent(ctx, "workflow", _spec(SLIDE_OUTLINE_AGENT_NAME)["definition"])

    assert sorted(llm.digest_calls) == sorted(titles)  # 고른 소스마다 요약을 만들었다
    [call] = llm.calls_of("outline")
    assert call["max_tokens"] == _SLIDE_OUTLINE_MAX_TOKENS
    for src in picked:
        assert str(src.id) in call["prompt"]  # 소스 목록(id) — 장에 배정할 수 있다
        assert f"요약 — {src.title}" in call["prompt"]  # 소스 요약 본문
    assert "고르지 않은 소스" not in call["prompt"]

    saved = _saved(_mock_runtime_deps, ctx, "slide-outline.json")
    assert [s["id"] for s in saved["sources"]] == [str(s.id) for s in picked]
    assert saved["degraded"] == []
    assert saved["slides"] == llm.outline["slides"]  # 새 키(layout·query·source_ids)는 그대로
    assert (saved["title"], saved["format"]) == ("커피 입문", "detailed")


async def test_second_outline_run_reuses_cached_digests(
    db, user_a, llm, search, _mock_runtime_deps
):
    """같은 소스로 두 번째 run을 돌리면 소스 요약을 다시 만들지 않는다(#218 수락 기준)."""
    nb, sources = await _notebook(db, user_a, ["원두", "추출"])
    definition = _spec(SLIDE_OUTLINE_AGENT_NAME)["definition"]
    await execute_agent(_ctx(db, nb, [s.id for s in sources]), "workflow", definition)
    assert len(llm.digest_calls) == 2

    ctx = _ctx(db, nb, [s.id for s in sources])
    await execute_agent(ctx, "workflow", definition)
    assert len(llm.digest_calls) == 2  # 캐시 적중 — 요약 LLM 호출 0회
    saved = _saved(_mock_runtime_deps, ctx, "slide-outline.json")
    assert [s["cached"] for s in saved["sources"]] == [True, True]
    assert "요약 — 원두" in llm.calls_of("outline")[-1]["prompt"]


async def test_outline_lists_degraded_sources(db, user_a, llm, search, _mock_runtime_deps):
    """요약 대신 대체 본문으로 들어간 소스는 빼지 않고 `degraded`로 남긴다."""
    nb, sources = await _notebook(db, user_a, ["원두", "빈 소스"], empty=("빈 소스",))
    ctx = _ctx(db, nb, [s.id for s in sources])
    await execute_agent(ctx, "workflow", _spec(SLIDE_OUTLINE_AGENT_NAME)["definition"])

    saved = _saved(_mock_runtime_deps, ctx, "slide-outline.json")
    assert len(saved["sources"]) == 2
    [degraded] = saved["degraded"]
    assert (degraded["id"], degraded["reason"]) == (str(sources[1].id), "no_content")
    assert "빈 소스" in llm.calls_of("outline")[0]["prompt"]


# ---------- 요약 모델(#239, 코어 #396) ----------


async def _digest_models(db, llm: SlideLLM) -> SimpleNamespace:
    """요약을 만들 모델 둘(실제 행) — 대역이 이 모델이 응답한 것으로 결과에 싣는다.

    대역에는 행이 아니라 값만 넘긴다 — 캐시 저장은 별도 세션이라 테스트 세션의 만료된 행을 읽지
    못한다.
    """
    provider = LLMProvider(name=f"mock-{uuid.uuid4().hex[:6]}", type="mock", enabled=True)
    db.add(provider)
    await db.flush()
    rows = {}
    for key, name in (("other", "다른 Mock"), ("chosen", "고른 Mock")):
        model = LLMModel(
            provider_id=provider.id,
            model_name=f"mock-{uuid.uuid4().hex[:6]}",
            display_name=name,
            capabilities=["llm"],
            enabled=True,
        )
        db.add(model)
        await db.flush()
        rows[key] = SimpleNamespace(id=model.id, display_name=name, cost_per_1k={})
        llm.models[model.id] = rows[key]
    await db.commit()
    return SimpleNamespace(**rows)


def _source_models(saved: dict) -> list[tuple[str, bool]]:
    return [(s["model_name"], s["cached"]) for s in saved["sources"]]


@pytest.mark.parametrize("refresh", [{}, {"refresh_digest": False}], ids=["missing", "false"])
async def test_digest_uses_run_model_and_shares_cache_across_models(
    db, user_a, llm, search, _mock_runtime_deps, refresh
):
    """run에서 고른 모델로 요약을 만든다. 다른 모델이 만든 캐시는 모델을 부르지 않고 그대로 쓰고,
    결과의 소스마다 요약을 만든 모델과 캐시 여부가 실린다(#239 AC-2). 다시 만들기를 켜지 않으면
    (키 없음·false) 다시 만들지 않는다."""
    models = await _digest_models(db, llm)
    nb, sources = await _notebook(db, user_a, ["원두", "추출", "로스팅"])
    definition = _spec(SLIDE_OUTLINE_AGENT_NAME)["definition"]
    first = _ctx(db, nb, [s.id for s in sources[:2]], run_model_id=models.other.id)
    await execute_agent(first, "workflow", definition)
    assert llm.digest_models == [models.other.id, models.other.id]

    ctx = _ctx(db, nb, [s.id for s in sources], run_model_id=models.chosen.id, **refresh)
    await execute_agent(ctx, "workflow", definition)

    assert llm.digest_calls[2:] == ["로스팅"]  # 캐시가 없는 소스만 만든다
    assert llm.digest_models[2:] == [models.chosen.id]
    saved = _saved(_mock_runtime_deps, ctx, "slide-outline.json")
    assert _source_models(saved) == [
        ("다른 Mock", True),
        ("다른 Mock", True),
        ("고른 Mock", False),
    ]
    assert [s["model_id"] for s in saved["sources"]] == [
        str(models.other.id),
        str(models.other.id),
        str(models.chosen.id),
    ]


async def test_refresh_digest_regenerates_only_other_model_caches(
    db, user_a, llm, search, _mock_runtime_deps
):
    """`refresh_digest`를 켜면 다른 모델이 만든 캐시만 고른 모델로 다시 만들어 덮는다. 고른 모델이
    만든 캐시는 그대로 쓴다(#239 AC-3)."""
    models = await _digest_models(db, llm)
    nb, sources = await _notebook(db, user_a, ["원두", "추출", "로스팅"])
    definition = _spec(SLIDE_OUTLINE_AGENT_NAME)["definition"]
    other = _ctx(db, nb, [s.id for s in sources[:2]], run_model_id=models.other.id)
    await execute_agent(other, "workflow", definition)
    chosen = _ctx(db, nb, [sources[2].id], run_model_id=models.chosen.id)
    await execute_agent(chosen, "workflow", definition)
    assert len(llm.digest_calls) == 3

    ctx = _ctx(db, nb, [s.id for s in sources], run_model_id=models.chosen.id, refresh_digest=True)
    await execute_agent(ctx, "workflow", definition)

    assert sorted(llm.digest_calls[3:]) == ["원두", "추출"]
    assert llm.digest_models[3:] == [models.chosen.id, models.chosen.id]
    saved = _saved(_mock_runtime_deps, ctx, "slide-outline.json")
    assert _source_models(saved) == [
        ("고른 Mock", False),
        ("고른 Mock", False),
        ("고른 Mock", True),
    ]
    rows = await db.execute(
        select(SourceDigest.model_id).where(SourceDigest.source_id.in_([s.id for s in sources]))
    )
    assert set(rows.scalars().all()) == {models.chosen.id}


# ---------- 덱(개요 → 장별 검색·작성 → 조립) ----------


async def test_deck_writes_each_slide_from_its_own_sources(
    db, user_a, llm, search, _mock_runtime_deps
):
    nb, sources = await _notebook(db, user_a, ["원두", "추출", "로스팅", "다른 run의 소스"])
    a, b, c, outside = (str(s.id) for s in sources)
    llm.outline = outline_doc(
        [
            outline_slide(1, [a], layout="title"),
            outline_slide(2, [a, b]),
            # run 범위 밖 id(같은 노트북의 고르지 않은 소스)는 검색 범위에서 빠진다(#362)
            outline_slide(3, [c, outside]),
            outline_slide(4, [b], layout="closing"),
        ]
    )
    ctx = _ctx(db, nb, [s.id for s in sources[:3]])
    await execute_agent(ctx, "workflow", await _deck(db))

    assert len(llm.calls_of("outline")) == 1
    writes = llm.calls_of("write")
    assert len(writes) == 4
    assert {w["max_tokens"] for w in writes} == {_SLIDE_WRITE_MAX_TOKENS}
    scopes = sorted((call["query"], sorted(map(str, call["source_ids"]))) for call in search.calls)
    assert scopes == [
        ("검색어 1", [a]),
        ("검색어 2", sorted([a, b])),
        ("검색어 3", [c]),
        ("검색어 4", [b]),
    ]
    # 장 작성 프롬프트에 그 장의 발췌가 들어간다
    assert all("발췌" in w["prompt"] for w in writes)

    deck = _saved(_mock_runtime_deps, ctx, "slide-deck.json")
    assert deck["title"] == "커피 입문"
    assert deck["format"] == "detailed"
    assert [s["heading"] for s in deck["slides"]] == ["장 1", "장 2", "장 3", "장 4"]
    assert [s["layout"] for s in deck["slides"]] == ["title", "bullets", "bullets", "closing"]
    coverage = deck["coverage"]
    assert [s["id"] for s in coverage["sources"]] == [a, b, c]
    assert coverage["degraded"] == []
    assert coverage["assigned"] == [[a], [a, b], [c, outside], [b]]
    assert [sorted(ids) for ids in coverage["cited"]] == [[a], sorted([a, b]), [c], [b]]
    assert coverage["outline"] == ["장 1", "장 2", "장 3", "장 4"]
    assert coverage["failed"] == []


@pytest.mark.parametrize(
    "copied",
    [
        {},  # 새 개요 — 장에 형식·언어가 없다
        {"format": "detailed", "language": "한국어"},  # 방안 ④ 개요 — 개요 LLM이 옮겨 쓴 값
    ],
)
async def test_deck_loop_passes_run_format_and_language_to_every_slide(
    db, user_a, llm, search, _mock_runtime_deps, copied
):
    """장 작성 본문은 형식·언어를 loop 공통 params로 덱 run 설정에서 받는다(코어 #390). 장 항목에
    없어도, 장 항목의 값이 run 설정과 달라도 run 설정을 쓴다."""
    nb, sources = await _notebook(db, user_a, ["원두"])
    a = str(sources[0].id)
    llm.outline = outline_doc([outline_slide(n, [a], **copied) for n in (1, 2, 3)])
    ctx = _ctx(db, nb, [sources[0].id], format="presenter", language="English")
    await execute_agent(ctx, "workflow", await _deck(db))

    writes = llm.calls_of("write")
    assert len(writes) == 3
    for w in writes:
        assert _format_rule("presenter") in w["prompt"]
        assert _language_rule("English") in w["prompt"]
    items = [json.loads(ITEM_RE.search(w["prompt"]).group(1)) for w in writes]
    assert sorted(i["title"] for i in items) == ["장 1", "장 2", "장 3"]
    deck = _saved(_mock_runtime_deps, ctx, "slide-deck.json")
    assert deck["format"] == "presenter"


async def test_user_copy_with_old_loop_falls_back_to_slide_item_format_and_language(
    db, user_a, llm, search, _mock_runtime_deps
):
    """공통 params 전에 복제한 사용자 덱(옛 loop)도 새 본문으로 발표자·English를 지킨다(PR-6 독립
    리뷰 High). 옛 개요 프롬프트가 장마다 옮겨 쓴 형식·언어가 장 항목에 실리고, 본문은 비어 렌더된
    공통 값 대신 장 항목의 값을 쓰라는 지시를 받는다."""
    nb, sources = await _notebook(db, user_a, ["원두"])
    a = str(sources[0].id)
    copied = {"format": "presenter", "language": "English"}
    llm.outline = outline_doc([outline_slide(n, [a], **copied) for n in (1, 2)])
    ctx = _ctx(db, nb, [sources[0].id], **copied)
    await execute_agent(ctx, "workflow", await _deck(db, user_copy=True))

    writes = llm.calls_of("write")
    assert len(writes) == 2
    for w in writes:
        assert _format_rule("") in w["prompt"]
        assert _language_rule("") in w["prompt"]
        item = json.loads(ITEM_RE.search(w["prompt"]).group(1))
        assert (item["format"], item["language"]) == ("presenter", "English")


async def test_failed_slide_is_skipped_and_listed(db, user_a, llm, search, _mock_runtime_deps):
    """한 장이 재시도까지 실패하면 그 장만 빠지고 run은 성공한다 — 실패한 장은 결과에 남는다."""
    nb, sources = await _notebook(db, user_a, ["원두", "추출"])
    a, b = (str(s.id) for s in sources)
    llm.outline = outline_doc([outline_slide(1, [a]), outline_slide(2, [b]), outline_slide(3, [a])])
    llm.broken_titles = {"장 2"}
    ctx = _ctx(db, nb, [s.id for s in sources])
    await execute_agent(ctx, "workflow", await _deck(db))

    # 장 2: 첫 생성 + validate 재시도(같은 상한) 모두 틀렸다
    broken = [w for w in llm.calls_of("write") if '"장 2"' in w["prompt"]]
    assert len(broken) == 2
    assert "이전 시도 실패" in broken[1]["prompt"]
    assert {w["max_tokens"] for w in broken} == {_SLIDE_WRITE_MAX_TOKENS}
    deck = _saved(_mock_runtime_deps, ctx, "slide-deck.json")
    assert [s["heading"] for s in deck["slides"]] == ["장 1", "장 3"]
    [failed] = deck["coverage"]["failed"]
    assert failed["index"] == 1
    assert deck["coverage"]["outline"][failed["index"]] == "장 2"
    # 실패한 장의 배정은 반영되지 않았다 — 배정·인용 목록은 그린 장과 같은 순서다
    assert deck["coverage"]["assigned"] == [[a], [a]]
    assert len(deck["coverage"]["cited"]) == 2


async def test_all_slides_failing_fails_the_run(db, user_a, llm, search, _mock_runtime_deps):
    nb, sources = await _notebook(db, user_a, ["원두"])
    llm.outline = outline_doc([outline_slide(1, [str(sources[0].id)])])
    llm.broken_titles = {"장 1"}
    ctx = _ctx(db, nb, [sources[0].id])
    with pytest.raises(AgentRuntimeError, match="slide_deck"):
        await execute_agent(ctx, "workflow", await _deck(db))
    assert ctx.artifact_keys == []


async def test_old_outline_items_search_the_whole_run_scope(
    db, user_a, llm, search, _mock_runtime_deps
):
    """`query`·`source_ids`가 없는 옛 개요(이 변경 전 개요 run)도 계속할 수 있다 — run 범위 전체를
    장 항목으로 검색한다."""
    nb, sources = await _notebook(db, user_a, ["원두", "추출"])
    old = {"title": "옛 개요", "format": "detailed", "slides": [{"title": "표지", "summary": "s"}]}
    ctx = _ctx(db, nb, [s.id for s in sources], outline=json.dumps(old, ensure_ascii=False))
    await execute_agent(ctx, "workflow", await _deck(db))

    assert llm.calls_of("outline") == []
    [call] = search.calls
    assert sorted(map(str, call["source_ids"])) == sorted(str(s.id) for s in sources)
    assert '"표지"' in call["query"]
    deck = _saved(_mock_runtime_deps, ctx, "slide-deck.json")
    assert [s["heading"] for s in deck["slides"]] == ["표지"]
    assert deck["coverage"]["assigned"] == [None]


# ---------- 검토 경로(개요 → 의견 → 계속) ----------


@pytest.mark.parametrize("user_copy", [False, True])
async def test_legacy_presenter_outline_continues_in_presenter_format(
    db, user_a, llm, search, _mock_runtime_deps, user_copy
):
    """방안 ④ 전에 저장한 발표자 개요(장에 format·language 없음)를 의견 없이 계속해도 장은 발표자
    형식·English로 쓴다(#218 독립 리뷰 M).

    웹 「계속」이 장마다 개요 run의 형식·언어를 실어 보낸다(공유 fixture `continue_outline` — 웹
    flow.test.ts가 continueRequest 출력과 대조한다). 시드 덱은 loop 공통 params(코어 #390)로 같은
    값을 넘기고, 그 전에 복제한 사용자 덱(옛 loop)은 장 항목의 값으로 물러난다(PR-6 독립 리뷰).
    """
    saved = LEGACY_PRESENTER["saved_outline"]
    assert all(set(s) == {"title", "summary"} for s in saved["slides"])  # 옛 개요
    nb, sources = await _notebook(db, user_a, ["원두"])
    params = LEGACY_PRESENTER["outline_run_params"]
    outline = json.dumps(LEGACY_PRESENTER["continue_outline"], ensure_ascii=False)
    ctx = _ctx(db, nb, [sources[0].id], **params, outline=outline, feedback="")
    await execute_agent(ctx, "workflow", await _deck(db, user_copy=user_copy))

    assert llm.calls_of("outline") == []  # 의견이 없으면 개요를 다시 쓰지 않는다
    writes = llm.calls_of("write")
    assert len(writes) == len(saved["slides"])
    items = [json.loads(ITEM_RE.search(w["prompt"]).group(1)) for w in writes]
    assert sorted(i["title"] for i in items) == sorted(s["title"] for s in saved["slides"])
    assert {(i["format"], i["language"]) for i in items} == {("presenter", "English")}
    common = ("", "") if user_copy else ("presenter", "English")
    for w in writes:
        assert _format_rule(common[0]) in w["prompt"]
        assert _language_rule(common[1]) in w["prompt"]
    deck = _saved(_mock_runtime_deps, ctx, "slide-deck.json")
    assert deck["format"] == "presenter"
    assert [s["heading"] for s in deck["slides"]] == [s["title"] for s in saved["slides"]]


async def test_reviewed_outline_without_feedback_is_followed_as_is(
    db, user_a, llm, search, _mock_runtime_deps
):
    """의견이 없으면 개요를 다시 쓰지 않는다 — 개요 run이 만든 요약 캐시를 읽고(요약 LLM 0회),
    장 작성은 검토한 개요의 `query`·`source_ids`를 그대로 쓴다."""
    nb, sources = await _notebook(db, user_a, ["원두", "추출"])
    a, b = (str(s.id) for s in sources)
    llm.outline = outline_doc([outline_slide(1, [a]), outline_slide(2, [b])], title="검토한 개요")
    outline_ctx = _ctx(db, nb, [s.id for s in sources])
    await execute_agent(outline_ctx, "workflow", _spec(SLIDE_OUTLINE_AGENT_NAME)["definition"])
    reviewed = _saved(_mock_runtime_deps, outline_ctx, "slide-outline.json")
    digests = len(llm.digest_calls)
    llm.calls.clear()

    ctx = _ctx(
        db,
        nb,
        [s.id for s in sources],
        outline=json.dumps(reviewed, ensure_ascii=False),
        feedback="",
        outline_run_id=str(outline_ctx.run_id),
    )
    await execute_agent(ctx, "workflow", await _deck(db))

    assert len(llm.digest_calls) == digests
    assert llm.calls_of("outline") == []
    assert sorted((c["query"], [str(x) for x in c["source_ids"]]) for c in search.calls) == [
        ("검색어 1", [a]),
        ("검색어 2", [b]),
    ]
    deck = _saved(_mock_runtime_deps, ctx, "slide-deck.json")
    assert deck["title"] == "검토한 개요"
    assert [s["heading"] for s in deck["slides"]] == ["장 1", "장 2"]
    assert [s["id"] for s in deck["coverage"]["sources"]] == [a, b]


async def test_feedback_rewrites_the_reviewed_outline(db, user_a, llm, search, _mock_runtime_deps):
    nb, sources = await _notebook(db, user_a, ["원두"])
    a = str(sources[0].id)
    reviewed = outline_doc([outline_slide(1, [a]), outline_slide(2, [a])], title="검토한 개요")
    llm.outline = outline_doc([outline_slide(1, [a])], title="고친 개요")
    ctx = _ctx(
        db,
        nb,
        [sources[0].id],
        outline=json.dumps(reviewed, ensure_ascii=False),
        feedback="2장을 빼 주세요",
    )
    await execute_agent(ctx, "workflow", await _deck(db))

    [call] = llm.calls_of("outline")
    assert "2장을 빼 주세요" in call["prompt"]
    assert '"검토한 개요"' in call["prompt"]  # 검토한 개요를 보고 고친다
    assert f"요약 — {sources[0].title}" in call["prompt"]
    deck = _saved(_mock_runtime_deps, ctx, "slide-deck.json")
    assert deck["title"] == "고친 개요"
    assert [s["heading"] for s in deck["slides"]] == ["장 1"]


async def test_invalid_reviewed_outline_fails_without_calling_llm(
    db, user_a, llm, search, _mock_runtime_deps
):
    """검토한 개요(params)가 형식에 맞지 않으면 LLM으로 고치지 않고 run이 실패한다."""
    nb, sources = await _notebook(db, user_a, ["원두"])
    ctx = _ctx(db, nb, [sources[0].id], outline='{"title": "t", "slides": []}')
    with pytest.raises(AgentRuntimeError, match="slide_outline"):
        await execute_agent(ctx, "workflow", await _deck(db))
    assert llm.calls == []
    assert ctx.artifact_keys == []
