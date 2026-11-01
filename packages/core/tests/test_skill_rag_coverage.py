"""스킬 `input.mode: rag`의 검색 커버리지 옵션과 소스 요약 입력(#461).

매니페스트 검증·빌더·그래프 실행·`rag_search` 노드·`json_validate`(마인드맵 인용 해석)는
실경로다. 대역은 경계뿐이다: LLM 스트림, 검색(`retrieve_context`·RAG 설정), 산출물 저장, 실행
로그. `source_digest` 노드는 DB를 읽어 노드 실행기를 대역으로 둔다 — 노드 자체는
`tests/test_source_digest_pg.py`가 실 DB로 본다.
"""

import json
import uuid
from typing import Any

import pytest
from structlog.testing import capture_logs

from biznotes_core.schemas.agent import AgentInput
from biznotes_core.services import rag_chat, settings_store
from biznotes_core.services.agents import graph
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.rag_chat import Citation, RetrievalResult, SourceCoverage
from biznotes_core.services.skills.builder import LoadedSkill, build_nodes, legacy_run_definition
from biznotes_core.services.skills.validator import (
    SkillValidationError,
    resolve_input,
    validate_manifest,
)

S1, S2, S3 = (str(uuid.UUID(int=i)) for i in (1, 2, 3))
C1, C2, C3 = (str(uuid.UUID(int=100 + i)) for i in (1, 2, 3))
PER_SOURCE = {"mode": "rag", "coverage": "per_source", "per_source_limit": 2}


def _loaded(inp: dict[str, Any], **manifest: Any) -> LoadedSkill:
    return LoadedSkill(
        metadata_name="mindmap",
        metadata_description="d",
        instructions="노드마다 cite에 근거 번호를 단다.",
        manifest={"mode": "instruction", "input": inp, **manifest},
    )


def _node(nodes: list[dict[str, Any]], node_id: str) -> dict[str, Any]:
    [node] = [n for n in nodes if n["id"] == node_id]
    return node


# ── 매니페스트 검증 ─────────────────────────────────────────────────────


@pytest.mark.parametrize("inp", [{"mode": "rag"}, {"mode": "full"}, {"mode": "none"}, None])
def test_omitted_options_resolve_like_before(inp: dict[str, Any] | None) -> None:
    """수락 기준 3 — 옵션을 생략하면 해석 결과에 키가 없다(저장 매니페스트·API 모양 그대로)."""
    manifest = {} if inp is None else {"input": inp}

    resolved = resolve_input(manifest)

    assert set(resolved) == {"mode", "scope", "max_sources", "max_chars"}


def test_rag_options_resolve_and_round_trip() -> None:
    raw = {**PER_SOURCE, "digest": True}

    resolved = resolve_input({"input": raw})

    assert {k: resolved[k] for k in ("coverage", "per_source_limit", "digest")} == {
        "coverage": "per_source",
        "per_source_limit": 2,
        "digest": True,
    }
    # 저장된 해석본을 다시 해석해도 같다(`chat_entry`가 저장본을 다시 해석한다)
    assert resolve_input({"input": resolved}) == resolved
    # API 모양(`SkillOut.input`)으로도 키가 그대로 실린다
    assert AgentInput.model_validate(resolved).model_dump()["coverage"] == "per_source"


@pytest.mark.parametrize(
    ("inp", "reason"),
    [
        ({"coverage": "all"}, "bad_input_coverage"),
        ({"coverage": ["per_source"]}, "bad_input_coverage"),
        ({"per_source_limit": 0}, "bad_input_per_source_limit"),
        ({"per_source_limit": 6}, "bad_input_per_source_limit"),
        ({"per_source_limit": "2"}, "bad_input_per_source_limit"),
        ({"per_source_limit": True}, "bad_input_per_source_limit"),
        ({"digest": "yes"}, "bad_input_digest"),
        ({"digest": 1}, "bad_input_digest"),
        ({"mode": "full", "coverage": "per_source"}, "bad_input_coverage"),
        ({"mode": "none", "digest": True}, "bad_input_digest"),
        ({"mode": "full", "per_source_limit": 1}, "bad_input_per_source_limit"),
    ],
)
def test_import_path_rejects_bad_options(inp: dict[str, Any], reason: str) -> None:
    manifest = {"input": {"mode": "rag", **inp}}

    with pytest.raises(SkillValidationError) as e:
        validate_manifest(manifest)

    assert e.value.reason == reason


def test_run_path_ignores_bad_options_with_a_warning() -> None:
    """이미 게시된 패키지는 실행 때 그 값을 지금처럼 무시한다 — 사유 코드만 남긴다."""
    manifest = {"input": {"mode": "rag", "coverage": "all", "per_source_limit": 3}}

    with capture_logs() as logs:
        resolved = validate_manifest(manifest, strict=False)

    assert "coverage" not in resolved["input"] and resolved["input"]["per_source_limit"] == 3
    assert [(e["event"], e["reason"]) for e in logs if e["log_level"] == "warning"] == [
        ("skill.manifest.input_option_invalid", "bad_input_coverage")
    ]


def test_run_path_keeps_existing_input_rules() -> None:
    """옵션 밖의 `input` 규칙은 두 경로가 같다(바꾸지 않았다)."""
    with pytest.raises(SkillValidationError) as e:
        validate_manifest({"input": {"mode": "rag", "max_chars": 0}}, strict=False)

    assert e.value.reason == "bad_input_max_chars"


# ── 빌더 그래프 ─────────────────────────────────────────────────────────


def test_omitted_options_leave_the_search_node_unchanged() -> None:
    """수락 기준 3 — 노드 키가 늘지 않는다(실행 로그 input_data도 그대로다)."""
    nodes, edges = build_nodes(_loaded({"mode": "rag"}, top_k=6))

    assert [n["id"] for n in nodes] == ["skill", "search", "generate", "save"]
    assert _node(nodes, "search") == {
        "id": "search", "type": "rag_search", "query": "{params.topic}", "top_k": 6,
    }  # fmt: skip
    assert "<source_digests>" not in _node(nodes, "generate")["prompt"]


def test_options_reach_the_search_node() -> None:
    nodes, _ = build_nodes(_loaded(PER_SOURCE))

    search = _node(nodes, "search")
    assert (search["coverage"], search["per_source_limit"]) == ("per_source", 2)
    assert "top_k" in search and search["top_k"] is None  # 생략은 생략으로(#45)


def test_digest_puts_a_source_digest_node_before_the_search() -> None:
    nodes, edges = build_nodes(_loaded({**PER_SOURCE, "digest": True}))

    assert [n["id"] for n in nodes] == ["skill", "digest", "search", "generate", "save"]
    assert _node(nodes, "digest") == {"id": "digest", "type": "source_digest"}
    assert edges[:3] == [["skill", "digest"], ["digest", "search"], ["search", "generate"]]
    prompt = _node(nodes, "generate")["prompt"]
    assert "{digest.context}" in prompt and "{search.context}" in prompt
    assert prompt.index("{digest.context}") < prompt.index("{search.context}")


def test_without_note_access_neither_node_is_built() -> None:
    nodes, _ = build_nodes(_loaded({**PER_SOURCE, "digest": True}, permissions={"rag": False}))

    assert [n["id"] for n in nodes] == ["skill", "generate", "save"]


def test_run_path_drops_a_bad_option_from_the_node() -> None:
    """원문 매니페스트(실행 경로)의 잘못된 값은 노드에 실리지 않는다 — 지금 동작이다."""
    nodes, _ = build_nodes(_loaded({"mode": "rag", "coverage": "all", "digest": "yes"}))

    assert [n["id"] for n in nodes] == ["skill", "search", "generate", "save"]
    assert "coverage" not in _node(nodes, "search")


# ── 실행(그래프 실경로) ──────────────────────────────────────────────────


def _ctx() -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={"topic": "음료"}  # type: ignore[arg-type]
    )


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """LLM 응답 큐·받은 메시지·검색 인자·노드 로그·저장 산출물을 기록하는 경계 대역."""
    box: dict[str, Any] = {
        "replies": [], "messages": [], "retrieve_kwargs": [], "inputs": {}, "outputs": {},
        "saved": [],
    }  # fmt: skip
    citations = [
        Citation(n=1, source_id=S1, chunk_id=C1, quote="커피 본문", source_title="커피"),
        Citation(n=2, source_id=S2, chunk_id=C2, quote="차 본문", source_title="차"),
    ]

    async def stream(db: Any, model_id: Any, messages: Any, result: Any, **kw: Any) -> Any:
        box["messages"].append(messages)
        reply = box["replies"].pop(0)

        async def gen() -> Any:
            yield reply

        return gen()

    async def retrieve(
        db: Any, notebook_id: Any, query: Any, source_ids: Any, top_k: Any = None, **kw: Any
    ) -> RetrievalResult:
        box["retrieve_kwargs"].append(kw)
        coverage = None
        if kw:
            coverage = SourceCoverage(scope_count=3, hit_sources=[S1, S2], missing=[S3])
        return RetrievalResult(
            citations=list(citations), insufficient_context=False, coverage=coverage
        )

    async def rag_setting(db: Any, key: str) -> dict[str, Any]:
        return dict(settings_store.DEFAULTS["rag"])

    async def digest(ctx: RunContext, node: dict[str, Any], state: dict[str, Any]) -> dict:
        return {
            "context": "## 커피\n커피 요약\n\n## 차\n차 요약\n\n## 물\n물 요약",
            "sources": [{"id": S1, "title": "커피"}, {"id": S2, "title": "차"},
                        {"id": S3, "title": "물"}],
            "degraded": [], "dropped": [], "fit": "shrink_each",
        }  # fmt: skip

    async def save(ctx: RunContext, node: dict[str, Any], state: dict[str, Any]) -> dict:
        from biznotes_core.services.agents.templating import render

        box["saved"].append(render(node["content"], state))
        return {"key": "k"}

    async def log_start(run_id: Any, node_id: str, node_type: str, input_data: Any, _at: Any):
        box["inputs"][node_id] = input_data
        return node_id

    async def log_finish(log_id: Any, *, output_data: Any = None, **kw: Any) -> bool:
        box["outputs"][log_id] = output_data
        return True

    monkeypatch.setattr(gateway_llm, "complete_stream_with_fallback", stream)
    monkeypatch.setattr(rag_chat, "retrieve_context", retrieve)
    monkeypatch.setattr(settings_store, "get_setting", rag_setting)
    monkeypatch.setitem(graph.NODE_EXECUTORS, "source_digest", digest)
    monkeypatch.setitem(graph.NODE_EXECUTORS, "save_artifact", save)
    monkeypatch.setattr(graph, "_log_node_start", log_start)
    monkeypatch.setattr(graph, "_log_node_finish", log_finish)
    return box


async def test_per_source_skill_run_gets_excerpts_for_every_source(
    runtime: dict[str, Any],
) -> None:
    """수락 기준 1 — 범위 소스마다 발췌를 받고 노드 출력에 커버리지 요약이 남는다."""
    runtime["replies"] = ["요약 결과"]

    await graph.execute_agent(_ctx(), "workflow", legacy_run_definition(_loaded(PER_SOURCE)))

    assert runtime["retrieve_kwargs"] == [{"coverage": "per_source", "per_source_limit": 2}]
    out = runtime["outputs"]["search"]
    assert (out["scope_count"], out["hit_sources"], out["missing"]) == (3, [S1, S2], [S3])
    assert runtime["inputs"]["search"]["coverage"] == "per_source"
    # 소스 단위 블록(source_load 모양)이 노트 컨텍스트로 들어간다
    prompt = runtime["messages"][0][-1]["content"]
    assert "## 커피" in prompt and "## 차" in prompt and "[1]\n커피 본문" in prompt


async def test_omitted_options_run_like_before(runtime: dict[str, Any]) -> None:
    """수락 기준 3 — 검색 인자·노드 출력·실행 로그 입력이 지금과 같다."""
    runtime["replies"] = ["요약 결과"]

    await graph.execute_agent(_ctx(), "workflow", legacy_run_definition(_loaded({"mode": "rag"})))

    assert runtime["retrieve_kwargs"] == [{}]
    assert set(runtime["outputs"]["search"]) == {"context", "citations", "insufficient_context"}
    assert not {"coverage", "per_source_limit"} & set(runtime["inputs"]["search"])


async def test_mindmap_cites_resolve_from_per_source_excerpts(runtime: dict[str, Any]) -> None:
    """수락 기준 2 — 마인드맵 `cite: [n]`이 per_source 발췌의 근거표로 해석된다(#303)."""
    doc = {
        "root": {
            "id": "root", "label": "음료",
            "children": [{"id": "a", "label": "차", "cite": [2]},
                         {"id": "b", "label": "커피", "cite": [1]}],
        }
    }  # fmt: skip
    runtime["replies"] = [json.dumps(doc)]
    skill = _loaded(PER_SOURCE, artifact={"name": "mindmap.json", "schema": "mindmap"})

    await graph.execute_agent(_ctx(), "workflow", legacy_run_definition(skill))

    assert runtime["retrieve_kwargs"] == [{"coverage": "per_source", "per_source_limit": 2}]
    [saved] = runtime["saved"]
    children = json.loads(saved)["root"]["children"]
    assert [c.get("source_ref") for c in children] == [
        {"source_id": S2, "chunk_id": C2},
        {"source_id": S1, "chunk_id": C1},
    ]
    assert all("cite" not in c for c in children)


async def test_digest_and_excerpts_both_reach_the_model(runtime: dict[str, Any]) -> None:
    """요약은 모든 소스를 덮고 인용 번호가 없다 — 근거표는 검색 발췌만 채운다."""
    runtime["replies"] = ["요약 결과"]
    ctx = _ctx()

    await graph.execute_agent(
        ctx, "workflow", legacy_run_definition(_loaded({**PER_SOURCE, "digest": True}))
    )

    prompt = runtime["messages"][0][-1]["content"]
    assert prompt.index("물 요약") < prompt.index("[1]\n커피 본문")
    assert [e["citation"]["n"] for e in ctx.tool_evidence] == [1, 2]
