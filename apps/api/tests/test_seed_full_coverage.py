"""스튜디오·샘플 에이전트가 모든 소스를 반영한다.

#282·#283·#217, core-v0.30.0-upgrade PR-3(AC-13·AC-14).

정적 계약: 바꾼 시드 정의마다 digest(source_digest) + search(rag_search 커버리지)를
쓰고, 프롬프트가 두 입력을 읽고, `coverage.json`을 남긴다. 은퇴한 스킬 패키지 slug가
목록에 있다. 모든 시드 정의가 사용자·관리자 저장 검증을 통과한다.

런타임: 「보고서 생성」을 코어 그래프로 실제 실행한다(대역은 LLM 게이트웨이·검색 경계뿐).
고른 소스 30개가 옛 상한(top_k 6~8)과 달리 모두 요약·발췌로 들어가고, `coverage.json`이
범위·근거 소스를 맞게 남긴다.

스킬(#292, core-v0.31.0-upgrade AC-3·AC-4): 마인드맵 시스템 스킬과 rag 샘플 스킬(핵심 요약·액션
아이템)의 매니페스트가 요약 + 소스별 발췌 옵션(코어 #461)을 두고 코어 검증을 통과한다. 마인드맵·
핵심 요약을 소스 30개로 실행하면 모든 소스가 요약·발췌로 프롬프트에 들어간다.
"""

import copy
import json
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from biznotes_core.models import Notebook, Source, SourceChunk
from biznotes_core.services.agents.runtime import RunContext, execute_agent, kind_to_nodes
from biznotes_core.services.skills.builder import legacy_run_definition, load_skill_files
from biznotes_core.services.skills.validator import validate_manifest

from app.services.definition_policy import definition_policy_problems
from scripts.seed import (
    _RETIRED_SKILL_PACKAGE_SLUGS,
    COVERAGE_ARTIFACT_NAME,
    MINDMAP_SKILL_DIR,
    SAMPLE_SKILL_PACKAGES,
    SAMPLE_WORKFLOW_AGENTS,
    SEED_AGENTS,
    SEED_SKILL_PACKAGES,
    sample_skill_manifest,
    sample_skill_md,
)

# 소스 전체(digest + per_source 발췌)를 반영하는 정의
FULL = {
    "보고서 생성",
    "브리핑 문서",
    "스터디 가이드",
    "슬라이드 개요",
    "인포그래픽 데이터",
    "[샘플] 타임라인 추출",
    "[샘플] 용어집 생성",
    "[샘플] FAQ 생성",
}
# 문항형 — 인용 메타를 그대로 넣으므로 서로 다른 소스 top_k개(per_source_limit 1)
DIVERSE = {"퀴즈 생성": 12, "플래시카드 생성": 16}


def _all_specs() -> dict[str, dict]:
    return {s["name"]: s for s in [*SEED_AGENTS, *SAMPLE_WORKFLOW_AGENTS]}


def _nodes(spec: dict) -> dict[str, dict]:
    return {n["id"]: n for n in spec["definition"]["nodes"]}


@pytest.mark.parametrize("name", sorted(FULL))
def test_full_coverage_definitions(name: str):
    spec = _all_specs()[name]
    assert spec["kind"] == "workflow"
    nodes = _nodes(spec)
    assert nodes["digest"]["type"] == "source_digest"
    assert nodes["search"]["coverage"] == "per_source"
    assert nodes["search"]["per_source_limit"] == 1
    assert "top_k" not in nodes["search"]
    assert ["digest", "search"] in spec["definition"]["edges"]
    prompts = [n["prompt"] for n in nodes.values() if isinstance(n.get("prompt"), str)]
    assert prompts
    for prompt in prompts:  # 재시도용 json_validate 사본도 같은 두 입력을 읽는다
        assert "{digest.context}" in prompt and "{search.context}" in prompt
    assert nodes["save_coverage"]["name"] == COVERAGE_ARTIFACT_NAME
    assert nodes["coverage"]["template"]["missing"] == "{search.missing}"


@pytest.mark.parametrize(("name", "top_k"), sorted(DIVERSE.items()))
def test_item_definitions_spread_over_distinct_sources(name: str, top_k: int):
    spec = _all_specs()[name]
    nodes = _nodes(spec)
    assert "digest" not in nodes
    assert nodes["search"]["top_k"] == top_k
    assert nodes["search"]["per_source_limit"] == 1
    assert "coverage" not in nodes["search"]  # ranked — 인용 메타가 창 맞춤 대상이 아니다
    assert nodes["save_coverage"]["name"] == COVERAGE_ARTIFACT_NAME
    for node in nodes.values():
        if isinstance(node.get("prompt"), str):
            assert "{search.citations|untrusted}" in node["prompt"]  # 인용 메타는 그대로


def test_no_seed_definition_still_uses_plain_top_k_search():
    """「선택한 소스 전체」를 다루는 시드 정의에 옛 top_k 8 검색이 남지 않는다.

    슬라이드 장 작성은 장마다 범위를 좁혀 검색하므로 뺀다."""
    for name, spec in _all_specs().items():
        for node in spec["definition"].get("nodes", []):
            if node.get("type") != "rag_search" or name == "슬라이드 장 작성":
                continue
            assert node.get("coverage") == "per_source" or node.get("per_source_limit") == 1, name


def test_retired_skill_packages():
    """보고서·브리핑·슬라이드 개요 스킬 패키지는 은퇴했다 — 같은 이름의 워크플로우가 대신한다."""
    assert SEED_SKILL_PACKAGES == []
    assert {"report-writer", "briefing-doc", "slide-outline"} <= set(_RETIRED_SKILL_PACKAGE_SLUGS)


@pytest.mark.parametrize("name", sorted(FULL | set(DIVERSE)))
def test_seed_definitions_pass_save_validation(name: str):
    spec = _all_specs()[name]
    nodes, _edges = kind_to_nodes(spec["kind"], spec["definition"])
    assert definition_policy_problems(spec["definition"], nodes) == []


# ---------- 런타임: 「보고서 생성」 ----------


class _LLM:
    """llm 노드·소스 요약 생성 대역 — 보고서 프롬프트를 남기고 마크다운을 낸다."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.digests: list[str] = []
        self.reply = "# 보고서\n\n본문 [1]"

    async def stream(self, db, model_id, messages, result, **kwargs):
        result.usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        self.prompts.append(messages[-1]["content"])

        reply = self.reply

        async def gen():
            yield reply

        return gen()

    async def digest_stream(self, db, model_id, messages, result, **kwargs):
        result.usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        body = messages[-1]["content"]
        title = re.search(r"문서 제목: (.*)", body)
        self.digests.append(title.group(1) if title else "")
        text = f"요약 — {title.group(1) if title else ''}"

        async def gen():
            yield text

        return gen()


class _Search:
    """`retrieve_context` 대역 — 범위 소스마다 발췌 하나와 커버리지를 낸다."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def retrieve(self, db, notebook_id, query, source_ids=None, top_k=None, **options):
        from biznotes_core.services.rag_chat import Citation, RetrievalResult, SourceCoverage

        self.calls.append({"query": query, "source_ids": source_ids, **options})
        rows = (
            await db.execute(
                Source.__table__.select().where(
                    Source.notebook_id == notebook_id, Source.id.in_(source_ids or [])
                )
            )
        ).all()
        titles = {str(r.id): r.title for r in rows}
        citations = [
            Citation(
                n=i,
                source_id=sid,
                chunk_id=str(uuid.uuid4()),
                quote=f"{titles[sid]} 발췌",
                source_title=titles[sid],
            )
            for i, sid in enumerate(titles, start=1)
        ]
        coverage = SourceCoverage(scope_count=len(titles), hit_sources=list(titles), missing=[])
        return RetrievalResult(citations=citations, insufficient_context=False, coverage=coverage)


@pytest.fixture
def llm(monkeypatch) -> _LLM:
    fake = _LLM()
    monkeypatch.setattr(
        "biznotes_core.services.gateway.llm.complete_stream_with_fallback", fake.stream
    )
    monkeypatch.setattr(
        "biznotes_core.services.source_digest.complete_stream_with_fallback", fake.digest_stream
    )
    return fake


@pytest.fixture
def search(monkeypatch, _mock_runtime_deps) -> _Search:
    fake = _Search()
    monkeypatch.setattr("biznotes_core.services.rag_chat.retrieve_context", fake.retrieve)
    return fake


async def _notebook(db, user, count: int) -> tuple[Notebook, list[Source]]:
    nb = Notebook(owner_id=user.id, title=f"cov-{uuid.uuid4().hex[:6]}")
    db.add(nb)
    await db.flush()
    base = datetime.now(UTC) - timedelta(hours=1)
    sources = []
    for i in range(count):
        src = Source(
            notebook_id=nb.id,
            type="text",
            title=f"소스 {i:02d}",
            status="ready",
            created_at=base + timedelta(seconds=i),
        )
        db.add(src)
        await db.flush()
        db.add(
            SourceChunk(
                source_id=src.id,
                seq=0,
                text=f"소스 {i:02d} 본문 {uuid.uuid4().hex}",
                meta={},

            )
        )
        sources.append(src)
    await db.commit()
    return nb, sources


async def test_report_workflow_reflects_every_selected_source(
    db, user_a, llm, search, mock_llm, _mock_runtime_deps
):
    nb, sources = await _notebook(db, user_a, 30)  # 옛 top_k(6~8)보다 훨씬 많다
    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=nb.id,
        db=db,
        source_ids=[s.id for s in sources],
        params={"topic": "핵심 주제", "detail": "보통", "language": "한국어", "extra": ""},
    )
    spec = _all_specs()["보고서 생성"]
    await execute_agent(ctx, spec["kind"], spec["definition"])

    # 검색은 범위 전체를 소스마다 한 개씩 요청했다
    [call] = search.calls
    assert call["coverage"] == "per_source" and call["per_source_limit"] == 1
    assert len(call["source_ids"]) == 30
    # 소스 30개가 모두 요약으로 만들어지고, 보고서 프롬프트에 모든 소스 제목이 요약·발췌로 들어갔다
    assert sorted(llm.digests) == sorted(s.title for s in sources)
    [prompt] = llm.prompts
    for src in sources:
        assert prompt.count(src.title) >= 2, src.title
    # 반영 현황(#217)
    names = {a["name"] for a in ctx.artifact_keys}
    assert {"report.md", COVERAGE_ARTIFACT_NAME} <= names
    [ref] = [a for a in ctx.artifact_keys if a["name"] == COVERAGE_ARTIFACT_NAME]
    coverage = json.loads(_mock_runtime_deps[ref["key"]])
    assert coverage["scope_count"] == 30
    assert sorted(coverage["hit_sources"]) == sorted(str(s.id) for s in sources)
    assert coverage["missing"] == []
    assert sorted(coverage["digest_sources"]) == sorted(str(s.id) for s in sources)
    assert coverage["degraded"] == []


# ---------- 스킬: 마인드맵·rag 샘플 스킬(#292, core-v0.31.0-upgrade) ----------

# 요약(모든 소스) + 소스별 발췌 1개 — 스튜디오 문서형(#282)과 같은 패턴(D3)
SKILL_COVERAGE = {"coverage": "per_source", "per_source_limit": 1, "digest": True}
RAG_SAMPLE_SKILLS = ("sample-action-items", "sample-key-summary")


def _sample_spec(slug: str) -> dict:
    return next(s for s in SAMPLE_SKILL_PACKAGES if s["slug"] == slug)


def _skill_files(slug: str) -> dict[str, bytes]:
    if slug == "mindmap":
        return {n: (MINDMAP_SKILL_DIR / n).read_bytes() for n in ("SKILL.md", "biznotes.json")}
    spec = _sample_spec(slug)
    return {
        "SKILL.md": sample_skill_md(spec).encode(),
        "biznotes.json": json.dumps(sample_skill_manifest(spec), ensure_ascii=False).encode(),
    }


@pytest.mark.parametrize("slug", ["mindmap", *RAG_SAMPLE_SKILLS])
def test_rag_skills_reflect_every_source(slug: str):
    files = _skill_files(slug)
    # 시드·가져오기와 같은 엄격 검증 — 잘못된 값이면 422 사유로 거부된다
    manifest = validate_manifest(json.loads(files["biznotes.json"]))
    assert {k: manifest["input"][k] for k in SKILL_COVERAGE} == SKILL_COVERAGE
    # 지시문이 두 입력을 구분한다 — 요약은 전체 파악용, 인용은 발췌 번호만
    body = files["SKILL.md"].decode()
    assert "<source_digests>" in body and "<source_excerpts>" in body


def test_full_mode_sample_skill_unchanged():
    """[샘플] 문서 번역은 고른 소스 원문을 번역한다 — 검색 옵션을 두지 않는다(#292)."""
    manifest = validate_manifest(sample_skill_manifest(_sample_spec("sample-doc-translate")))
    assert manifest["input"]["mode"] == "full"
    assert not set(SKILL_COVERAGE) & set(manifest["input"])


MINDMAP_REPLY = (
    '{"version": 1, "root": {"id": "root", "label": "핵심 주제", "children": ['
    '{"id": "n1", "label": "첫 가지", "cite": [1], "children": []}]}}'
)


@pytest.mark.parametrize(
    ("slug", "reply", "artifact"),
    [
        ("mindmap", MINDMAP_REPLY, "mindmap.json"),
        ("sample-key-summary", "요지\n- 핵심 [1]", "summary.md"),
    ],
)
async def test_skill_run_reflects_every_selected_source(
    db, user_a, llm, search, mock_llm, _mock_runtime_deps, slug, reply, artifact
):
    nb, sources = await _notebook(db, user_a, 30)  # 옛 top_k(6)보다 훨씬 많다
    llm.reply = reply
    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=nb.id,
        db=db,
        source_ids=[s.id for s in sources],
        params={"topic": "핵심 주제", "extra": ""},
    )
    definition = legacy_run_definition(load_skill_files(_skill_files(slug)))
    await execute_agent(ctx, "workflow", definition)

    # 검색은 범위 전체를 소스마다 한 개씩 요청했다
    [call] = search.calls
    assert call["coverage"] == "per_source" and call["per_source_limit"] == 1
    assert len(call["source_ids"]) == 30
    # 소스 30개가 모두 요약되고, 프롬프트에 모든 소스 제목이 요약·발췌로 들어갔다
    assert sorted(llm.digests) == sorted(s.title for s in sources)
    prompt = llm.prompts[0]
    assert "<source_digests>" in prompt and "<source_excerpts>" in prompt
    for src in sources:
        assert prompt.count(src.title) >= 2, src.title
    [ref] = [a for a in ctx.artifact_keys if a["name"] == artifact]
    if slug == "mindmap":
        # 발췌 번호 `cite`가 run 근거표로 해석된다(코어 #303·#461)
        doc = json.loads(_mock_runtime_deps[ref["key"]])
        [n1] = doc["root"]["children"]
        assert n1["source_ref"]["source_id"] in {str(s.id) for s in sources}
        assert doc["refs_resolved"] == 1


async def _skill_state() -> dict[str, tuple[int, dict]]:
    """slug → (버전 수, 공개 버전의 매니페스트 `input`)."""
    from biznotes_core.core.db import async_session_factory
    from biznotes_core.models import Skill, SkillVersion
    from sqlalchemy import func, select

    state: dict[str, tuple[int, dict]] = {}
    async with async_session_factory() as s:
        for skill in (await s.execute(select(Skill))).scalars():
            count = await s.scalar(
                select(func.count())
                .select_from(SkillVersion)
                .where(SkillVersion.skill_id == skill.id)
            )
            published = await s.get(SkillVersion, skill.published_version_id)
            state[skill.slug] = (count, published.manifest["input"])
    return state


async def test_seed_upgrades_rag_skills_once(db, monkeypatch, tmp_path):
    """옵션이 없던 이전 패키지 위에 다시 시드하면 rag 스킬마다 새 버전이 한 번 생기고, 또 시드해도
    늘지 않는다. 번역 샘플은 버전이 그대로다(core-v0.31.0-upgrade AC-5)."""
    from scripts import seed

    monkeypatch.setattr("biznotes_core.services.storage.ensure_buckets", lambda: None)
    # 이전 패키지 — 검색 옵션이 없는 매니페스트(v0.30.0 시드와 같은 input)
    old_dir = tmp_path / "mindmap"
    old_dir.mkdir()
    (old_dir / "SKILL.md").write_bytes((MINDMAP_SKILL_DIR / "SKILL.md").read_bytes())
    manifest = json.loads((MINDMAP_SKILL_DIR / "biznotes.json").read_text(encoding="utf-8"))
    for key in SKILL_COVERAGE:
        manifest["input"].pop(key)
    (old_dir / "biznotes.json").write_text(json.dumps(manifest, ensure_ascii=False))
    old_specs = copy.deepcopy(SAMPLE_SKILL_PACKAGES)
    for spec in old_specs:
        for key in SKILL_COVERAGE:
            spec["input"].pop(key, None)
    monkeypatch.setattr(seed, "SAMPLE_SKILL_PACKAGES", old_specs)
    await seed.upsert_mindmap_skill(old_dir)
    await seed.upsert_sample_skills()
    before = await _skill_state()
    slugs = ("mindmap", *RAG_SAMPLE_SKILLS, "sample-doc-translate")
    assert {slug: before[slug][0] for slug in slugs} == dict.fromkeys(slugs, 1)

    monkeypatch.setattr(seed, "SAMPLE_SKILL_PACKAGES", SAMPLE_SKILL_PACKAGES)
    for _ in range(2):  # 두 번째 시드는 새 버전을 만들지 않는다
        await seed.upsert_mindmap_skill()
        await seed.upsert_sample_skills()
    after = await _skill_state()
    for slug in ("mindmap", *RAG_SAMPLE_SKILLS):
        count, inp = after[slug]
        assert count == 2, slug
        assert {k: inp[k] for k in SKILL_COVERAGE} == SKILL_COVERAGE, slug
    assert after["sample-doc-translate"] == before["sample-doc-translate"]
