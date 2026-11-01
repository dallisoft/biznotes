"""마인드맵 근거 번호의 서버 해석·검증 (#303).

계획 `docs/plans/mindmap-cite-resolution/plan.md` 수락 기준 1~7이다. 해석 함수와 `json_validate`는
실경로다. 그래프 경로는 실제 `execute_agent`에 경계(LLM 스트림·검색·저장·실행 로그)만 대역을 쓴다.
"""

import json
import uuid
from typing import Any

import pytest

from biznotes_core.services import rag_chat, settings_store
from biznotes_core.services.agents import graph
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.nodes import validation
from biznotes_core.services.agents.nodes.retrieval import _record_run_citations
from biznotes_core.services.agents.validators import (
    _resolve_mindmap_refs,
    _validate_mindmap_shape,
)
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.rag_chat import Citation, RetrievalResult
from biznotes_core.services.skills.builder import LoadedSkill, build_nodes

S1, C1 = "11111111-1111-1111-1111-111111111111", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
S2, C2 = "22222222-2222-2222-2222-222222222222", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
# 같은 노트북이지만 이 run이 검색하지 않은 청크(노트북 소속만으로는 남기지 않는다)
S3, C3 = "33333333-3333-3333-3333-333333333333", "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _evidence() -> list[dict[str, Any]]:
    """`_record_run_citations`가 쌓는 모양 + 해석 대상이 아닌 항목들."""
    return [
        {"kind": "source", "source_id": S1, "title": "커피",
         "citation": {"n": 1, "source_id": S1, "chunk_id": C1, "quote": "q", "verified": True}},
        {"kind": "source", "source_id": S2, "title": "차",
         "citation": {"n": 2, "source_id": S2, "chunk_id": C2, "quote": "q", "verified": True}},
        {"kind": "source", "source_id": S3, "title": "전문", "mode": "full"},  # source_load 도구
        {"kind": "web", "url": "https://example.com", "title": "웹"},
    ]  # fmt: skip


def _doc(*children: dict[str, Any]) -> dict[str, Any]:
    nodes = [{"label": f"가지 {i}", **c} for i, c in enumerate(children)]
    for i, node in enumerate(nodes):
        node.setdefault("id", f"n{i}")
    return {"root": {"id": "root", "label": "주제", "children": nodes}}


def _refs(doc: dict[str, Any]) -> list[Any]:
    return [c.get("source_ref") for c in doc["root"]["children"]]


# ── 수락 기준 1: cite 해석 ───────────────────────────────────────────────


def test_cite_numbers_become_the_server_pair() -> None:
    doc = _doc({"cite": [2]}, {"cite": [9]}, {"cite": [9, 1]}, {"cite": [1, 1]})

    resolved, dropped = _resolve_mindmap_refs(doc["root"], _evidence())

    assert _refs(doc) == [
        {"source_id": S2, "chunk_id": C2},
        None,
        {"source_id": S1, "chunk_id": C1},
        {"source_id": S1, "chunk_id": C1},
    ]
    assert (resolved, dropped) == (3, 2)  # [9] 하나, [9, 1]의 9 하나 — 중복 번호는 한 번만 본다


# ── 수락 기준 2: 직접 쓴 source_ref ─────────────────────────────────────


@pytest.mark.parametrize(
    "ref",
    [
        {"source_id": S2, "chunk_id": C1},  # ① 표의 chunk_id + 틀린 source_id
        {"source_id": str(uuid.uuid4()), "chunk_id": str(uuid.uuid4())},  # ② 선택 범위 밖
        {"source_id": S3, "chunk_id": C3},  # ③ 같은 노트북, 이 run이 검색하지 않음
        {"source_id": S1, "chunk_id": C1.upper()},  # 서버 쌍과 글자가 다르다
        "garbage",  # ⑥ 형식 오류
        {"source_id": S1},  # 부분 참조
        {"source_id": 1, "chunk_id": C1},
    ],
)
def test_direct_refs_outside_the_run_evidence_are_dropped(ref: Any) -> None:
    doc = _doc({"source_ref": ref})

    assert _resolve_mindmap_refs(doc["root"], _evidence()) == (0, 1)
    assert "source_ref" not in doc["root"]["children"][0]


def test_null_source_ref_is_absent_not_dropped() -> None:
    """`null`은 「없음」이다 — 키만 지우고 버린 수로 세지 않는다(노드마다 null을 내는 LLM)."""
    doc = _doc({"source_ref": None}, {"source_ref": None}, {"cite": None})

    assert _resolve_mindmap_refs(doc["root"], _evidence()) == (0, 0)
    assert all("source_ref" not in c and "cite" not in c for c in doc["root"]["children"])


def test_direct_ref_matching_the_table_is_kept_with_only_the_pair() -> None:
    doc = _doc({"source_ref": {"source_id": S1, "chunk_id": C1, "page": 3}})

    assert _resolve_mindmap_refs(doc["root"], _evidence()) == (0, 0)
    assert _refs(doc) == [{"source_id": S1, "chunk_id": C1}]  # ⑤


def test_cite_wins_over_a_direct_ref() -> None:
    """④ cite와 직접 source_ref가 다른 쌍이면 cite 쌍이다(덮어쓰기는 버린 수로 세지 않는다)."""
    doc = _doc({"cite": [2], "source_ref": {"source_id": S1, "chunk_id": C1}})

    assert _resolve_mindmap_refs(doc["root"], _evidence()) == (1, 0)
    assert _refs(doc) == [{"source_id": S2, "chunk_id": C2}]


def test_unresolvable_cite_falls_back_to_checking_the_direct_ref() -> None:
    doc = _doc(
        {"cite": [9], "source_ref": {"source_id": S1, "chunk_id": C1}},
        {"cite": [9], "source_ref": {"source_id": S3, "chunk_id": C3}},
    )

    assert _resolve_mindmap_refs(doc["root"], _evidence()) == (0, 3)
    assert _refs(doc) == [{"source_id": S1, "chunk_id": C1}, None]


# ── 수락 기준 3·4: 문서·노드 출력 메타, 절단 뒤 해석 ─────────────────────


def _ctx(evidence: list[dict[str, Any]] | None = None) -> RunContext:
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={})  # type: ignore[arg-type]
    ctx.tool_evidence = list(evidence or [])
    return ctx


async def _validate(doc: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    node = {"id": "v", "type": "json_validate", "schema": "mindmap", "source": "{gen.text}"}
    return await validation._exec_json_validate(
        _ctx(evidence), node, {"gen": {"text": json.dumps(doc)}}
    )


async def test_saved_document_has_no_cite_and_carries_the_counts() -> None:
    doc = _doc({"cite": [1]}, {"cite": [7]}, {"source_ref": {"source_id": S2, "chunk_id": C2}})
    doc["refs_resolved"] = 99  # LLM이 낸 같은 키는 서버 값이 덮는다

    out = await _validate(doc, _evidence())

    saved = json.loads(out["text"])
    assert "cite" not in json.dumps(saved)
    assert (saved["refs_resolved"], saved["refs_dropped"], saved["truncated"]) == (1, 1, False)
    assert (out["refs_resolved"], out["refs_dropped"], out["truncated"]) == (1, 1, False)
    assert out["json"] == saved


async def test_truncated_nodes_are_not_counted() -> None:
    children = [{"cite": [9]} for _ in range(250)]  # 199개만 남는다(루트 + 199 = 200)

    out = await _validate(_doc(*children), _evidence())

    assert out["truncated"] is True
    assert (out["refs_resolved"], out["refs_dropped"]) == (0, 199)


async def test_without_evidence_every_ref_is_dropped() -> None:
    """수락 기준 7 — 전문 모드(source_load)·검색 없는 실행은 근거표가 비어 있다."""
    doc = _doc({"cite": [1]}, {"source_ref": {"source_id": S1, "chunk_id": C1}})

    out = await _validate(doc, [])

    assert _refs(out["json"]) == [None, None]
    assert (out["refs_resolved"], out["refs_dropped"]) == (0, 2)


async def test_other_schemas_do_not_get_the_mindmap_meta() -> None:
    node = {"id": "v", "type": "json_validate", "schema": "quiz", "source": "{gen.text}"}
    lines = [
        {
            "id": "q1",
            "question": "질문",
            "choices": ["가", "나", "다", "라"],
            "answer_index": 0,
            "citation": {
                "chunk_id": "c1",
                "quote": "인용",
                "source_id": "s1",
                "source_title": "소스",
            },
            "cite": [1],
        }
    ]

    out = await validation._exec_json_validate(
        _ctx(_evidence()), node, {"gen": {"text": json.dumps(lines)}}
    )

    assert set(out) == {"json", "text", "truncated"}
    assert out["json"] == lines  # 배열 스키마는 건드리지 않는다


# ── 수락 기준 5: cite 형식 오류는 버리고 센다(사용자 결정, run을 실패시키지 않는다) ──


@pytest.mark.parametrize(
    ("cite", "ref", "counts"),
    [
        (["2"], {"source_id": S2, "chunk_id": C2}, (1, 0)),  # 정수로 읽히는 문자열
        ([" 1 ", "x"], {"source_id": S1, "chunk_id": C1}, (1, 1)),
        ([True, 1.0, "²", None], None, (0, 4)),
        (["9" * 5000, "1"], {"source_id": S1, "chunk_id": C1}, (1, 1)),  # int() 자릿수 상한
        ("1", None, (0, 1)),  # 배열이 아니다
        ({"n": 1}, None, (0, 1)),
        ([9, 8, 7, 6, 5, 4, 3, 10, 1], None, (0, 9)),  # 앞 8개만 본다 — 1은 상한 밖
        ([9, 8, 7, 6, 5, 4, 3, 2, 1], {"source_id": S2, "chunk_id": C2}, (1, 8)),
    ],
)
def test_malformed_cite_entries_are_dropped_and_counted(
    cite: Any, ref: Any, counts: tuple[int, int]
) -> None:
    doc = _doc({"cite": cite})

    assert _resolve_mindmap_refs(doc["root"], _evidence()) == counts
    assert _refs(doc) == [ref]


@pytest.mark.parametrize("cite", ["1", [1.0], [True], list(range(20)), {"n": 1}])
def test_cite_never_fails_the_shape_check(cite: Any) -> None:
    assert _validate_mindmap_shape(_doc({"cite": cite})) is None


async def test_malformed_cite_does_not_retry_or_fail_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """선택 보조 정보다 — 재시도(LLM 재호출) 없이 저장되고 버린 수만 남는다."""
    calls: list[Any] = []

    async def no_llm(*a: Any, **kw: Any) -> Any:
        calls.append(a)
        raise AssertionError("cite 형식 오류로 재시도하면 안 된다")

    monkeypatch.setattr(gateway_llm, "complete_stream_with_fallback", no_llm)
    doc = _doc({"cite": ["1"]}, {"cite": [1.0]}, {"cite": "x"}, {"cite": ["9" * 5000]})
    node = {
        "id": "v",
        "type": "json_validate",
        "schema": "mindmap",
        "source": "{gen.text}",
        "prompt": "마인드맵",
    }  # fmt: skip  — 재시도 경로가 열려 있다

    out = await validation._exec_json_validate(
        _ctx(_evidence()), node, {"gen": {"text": json.dumps(doc)}}
    )

    assert calls == []
    assert _refs(out["json"]) == [{"source_id": S1, "chunk_id": C1}, None, None, None]
    assert (out["refs_resolved"], out["refs_dropped"]) == (1, 3)


# ── 근거표 범위: run 트리 전체(하위 실행 공유, 계획 D4) ────────────────────


def _citation(n: int, source_id: str, chunk_id: str) -> Citation:
    return Citation(n=n, source_id=source_id, chunk_id=chunk_id, quote="q", source_title="t")


async def test_child_run_resolves_a_chunk_its_parent_numbered_first() -> None:
    """하위 실행이 부모가 먼저 번호를 붙인 청크를 다시 검색하면 번호를 재사용하고 표에 새 항목을
    넣지 않는다 — 표를 하위 실행 몫으로 좁히면 하위 실행 LLM이 본 `[n]`이 해석되지 않는다."""
    parent = _ctx()
    _record_run_citations(parent, [_citation(1, S1, C1), _citation(2, S2, C2)])
    child = parent.child(uuid.uuid4(), {})
    shown = _record_run_citations(child, [_citation(1, S2, C2)])  # 호출마다 [1]부터 온다
    assert [c.n for c in shown] == [2] and len(child.tool_evidence) == 2  # 재사용, 새 항목 없음

    node = {"id": "v", "type": "json_validate", "schema": "mindmap", "source": "{gen.text}"}
    out = await validation._exec_json_validate(
        child, node, {"gen": {"text": json.dumps(_doc({"cite": [2]}, {"cite": [1]}))}}
    )

    assert _refs(out["json"]) == [
        {"source_id": S2, "chunk_id": C2},  # 하위 실행이 다시 본 청크
        {"source_id": S1, "chunk_id": C1},  # 부모만 검색한 번호도 해석된다(받아들인 위험)
    ]


# ── 수락 기준 6: 경로 e2e(워크플로우·레거시 스킬) ────────────────────────


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    box: dict[str, Any] = {"replies": [], "saved": []}

    async def stream(db: Any, model_id: Any, messages: Any, result: Any, **kw: Any) -> Any:
        reply = box["replies"].pop(0)

        async def gen() -> Any:
            yield reply

        return gen()

    async def retrieve(db: Any, notebook_id: Any, query: Any, source_ids: Any, top_k: int = 6):
        return RetrievalResult(
            citations=[
                Citation(n=1, source_id=S1, chunk_id=C1, quote="커피 본문", source_title="커피"),
                Citation(n=2, source_id=S2, chunk_id=C2, quote="차 본문", source_title="차"),
            ],
            insufficient_context=False,
        )

    async def rag_setting(db: Any, key: str) -> dict[str, Any]:
        return dict(settings_store.DEFAULTS["rag"])

    async def save(ctx: RunContext, node: dict[str, Any], state: dict[str, Any]) -> dict:
        from biznotes_core.services.agents.templating import render

        box["saved"].append(json.loads(render(node["content"], state)))
        return {"key": "k"}

    async def no_log(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(gateway_llm, "complete_stream_with_fallback", stream)
    monkeypatch.setattr(rag_chat, "retrieve_context", retrieve)
    monkeypatch.setattr(settings_store, "get_setting", rag_setting)
    monkeypatch.setitem(graph.NODE_EXECUTORS, "save_artifact", save)
    monkeypatch.setattr(graph, "_log_node_start", no_log)
    monkeypatch.setattr(graph, "_log_node_finish", no_log)
    return box


WORKFLOW = {
    "nodes": [
        {"id": "search", "type": "rag_search", "query": "커피"},
        {"id": "map", "type": "llm", "prompt": "근거:\n{search.context}"},
        {"id": "check", "type": "json_validate", "schema": "mindmap", "source": "{map.text}"},
        {"id": "save", "type": "save_artifact", "name": "mindmap.json",
         "content": "{check.text}", "content_type": "application/json"},
    ],
    "edges": [["search", "map"], ["map", "check"], ["check", "save"]],
}  # fmt: skip

LLM_DOC = _doc(
    {"cite": [2]},
    {"source_ref": {"source_id": S1, "chunk_id": "지어낸-청크"}},  # 옮겨 적다 틀린 값
    {"cite": [1], "source_ref": {"source_id": S3, "chunk_id": C3}},
)


async def test_workflow_resolves_cites_from_the_search_evidence(runtime: dict[str, Any]) -> None:
    runtime["replies"] = [json.dumps(LLM_DOC)]

    await graph.execute_agent(_ctx(), "workflow", WORKFLOW)

    [saved] = runtime["saved"]
    assert _refs(saved) == [
        {"source_id": S2, "chunk_id": C2},
        None,
        {"source_id": S1, "chunk_id": C1},
    ]
    assert (saved["refs_resolved"], saved["refs_dropped"]) == (2, 1)


async def test_legacy_skill_with_schema_resolves_cites_too(runtime: dict[str, Any]) -> None:
    """#302의 `artifact.schema` 경로 — 워크플로우와 같은 규칙을 얻는다."""
    runtime["replies"] = [json.dumps(LLM_DOC)]
    loaded = LoadedSkill(
        metadata_name="mindmap",
        metadata_description="d",
        instructions="노드마다 cite에 근거 번호를 단다.",
        manifest={"artifact": {"name": "mindmap.json", "schema": "mindmap"}},
    )
    nodes, edges = build_nodes(loaded)

    await graph.execute_agent(_ctx(), "workflow", {"nodes": nodes, "edges": edges})

    [saved] = runtime["saved"]
    assert _refs(saved)[0] == {"source_id": S2, "chunk_id": C2}
    assert (saved["refs_resolved"], saved["refs_dropped"]) == (2, 1)
