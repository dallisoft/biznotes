"""마인드맵 스킬 패키지(`skills/mindmap/`) 계약 — mindmap-agent-skill AC-3.1~AC-3.4.

- 디렉터리 파일이 코어 `validate_files`·`validate_manifest`·`load_skill_files`·
  `evaluate_package`를 통과한다.
- `build_nodes` = constant → source_digest → rag_search → llm → json_validate(mindmap) →
  save_artifact(`mindmap.json`·`application/json`), generate 노드 system에 `{search` 0(D-18).
  검색은 범위 소스마다 발췌 1개(`per_source`)이고 요약이 그 앞에 온다(코어 #461, #292). 코어
  `legacy_run_definition`의 채팅 출력은 검증본(`validate`)이다(P4 AC-4.2 — biznotes-core#302).
- zip 안 파일 목록·바이트 == 디렉터리.
- 실행(P4): LLM 대역이 펜스 감싼 JSON을 두 번 돌려주면 1회 재시도 뒤 run이 실패하고 산출물이 없다.
  `cite` 번호는 run 근거표(rag_search 인용)의 `(source_id, chunk_id)`로 해석돼 저장된다(#303).
"""

import hashlib
import json
import re
import uuid
import zipfile
from pathlib import Path

import pytest
from biznotes_core.services.agents.errors import AgentRuntimeError
from biznotes_core.services.agents.runtime import RunContext, execute_agent
from biznotes_core.services.skills.builder import (
    build_nodes,
    legacy_run_definition,
    load_skill_files,
)
from biznotes_core.services.skills.report import evaluate_package
from biznotes_core.services.skills.validator import validate_files, validate_manifest

ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = ROOT / "skills/mindmap"
SKILL_ZIP = ROOT / "skills/mindmap.zip"
FILES = ("SKILL.md", "biznotes.json")


def _files() -> dict[str, bytes]:
    return {name: (SKILL_DIR / name).read_bytes() for name in FILES}


def _manifest() -> dict:
    return json.loads((SKILL_DIR / "biznotes.json").read_text(encoding="utf-8"))


def test_package_passes_core_validation():
    files = _files()
    assert sorted(p.name for p in SKILL_DIR.iterdir()) == sorted(FILES)
    validate_files(files)
    manifest = validate_manifest(_manifest())
    assert manifest["mode"] == "instruction"
    loaded = load_skill_files(files)
    assert loaded.metadata_name == "mindmap"
    assert loaded.references == {}


def test_evaluation_has_no_code_execution_warning():
    evaluation = evaluate_package(_files())
    warnings = [w["code"] for w in evaluation.report["capability"]["warnings"]]
    assert "instruction_cannot_execute_code" not in warnings
    assert evaluation.has_product_manifest is True


def test_manifest_contract():
    m = _manifest()
    # 고른 소스마다 발췌 하나 + 모든 소스 요약(코어 #461, #292 · core-v0.31.0-upgrade D3)
    assert m["input"] == {
        "mode": "rag",
        "scope": "selected",
        "max_sources": 12,
        "coverage": "per_source",
        "per_source_limit": 1,
        "digest": True,
    }
    assert m["rag_query"] == "{params.topic}"
    assert "top_k" not in m  # 어드민 RAG 설정(코어 #45)
    assert m["params_schema"]["properties"]["topic"]["default"]
    assert "extra" in m["params_schema"]["properties"]
    assert m["permissions"] == {
        "rag": True,
        "llm": True,
        "tts": False,
        "mcp": False,
        "http": False,
        "scripts": False,
    }
    assert m["artifact"] == {
        "name": "mindmap.json",
        "content_type": "application/json",
        "schema": "mindmap",
    }
    raw = (SKILL_DIR / "biznotes.json").read_text(encoding="utf-8")
    assert raw.count("{search") == 0
    assert "{search" not in (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def test_skill_md_rules():
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert re.search(r"^name: mindmap$", body, re.MULTILINE)
    assert re.search(r"^description: .+", body, re.MULTILINE)
    for rule in (
        "JSON 객체 하나만",
        "최대 3단계",
        "60개 이하",
        "40자 이하",
        "서로 다르게",
        "cite",
        "<source_digests>",
        "<source_excerpts>",
    ):
        assert rule in body
    # 언어 태그 붙은 코드 블록·설치 명령 없음
    assert not re.search(r"^[ \t]*```[ \t]*\w", body, re.MULTILINE)


def test_build_nodes_pipeline():
    nodes, edges = build_nodes(load_skill_files(_files()))
    assert [n["type"] for n in nodes] == [
        "constant",
        "source_digest",
        "rag_search",
        "llm",
        "json_validate",
        "save_artifact",
    ]
    assert edges == [
        ["skill", "digest"],
        ["digest", "search"],
        ["search", "generate"],
        ["generate", "validate"],
        ["validate", "save"],
    ]
    generate = next(n for n in nodes if n["type"] == "llm")
    validate = next(n for n in nodes if n["type"] == "json_validate")
    save = next(n for n in nodes if n["type"] == "save_artifact")
    assert "{search" not in generate["system"]
    # 요약(인용 번호 없음)과 발췌(`[n]` 인용)가 함께 들어간다
    assert "{digest.context}" in generate["prompt"] and "{search.context}" in generate["prompt"]
    assert validate["schema"] == "mindmap"
    assert validate["source"] == "{generate.text}"
    # 재시도는 generate와 같은 프롬프트·system으로 한다
    assert (validate["prompt"], validate["system"]) == (generate["prompt"], generate["system"])
    assert save["content"] == "{validate.text}"
    assert save["name"] == "mindmap.json"
    assert save["content_type"] == "application/json"
    search = next(n for n in nodes if n["type"] == "rag_search")
    assert search["top_k"] is None
    assert (search["coverage"], search["per_source_limit"]) == ("per_source", 1)


def test_run_definition_chat_output_is_validated_doc():
    definition = legacy_run_definition(load_skill_files(_files()))
    assert definition["chat_output"] == {"node_id": "validate"}


def test_zip_matches_directory():
    with zipfile.ZipFile(SKILL_ZIP) as z:
        names = sorted(z.namelist())
        assert names == sorted(FILES)
        for name in FILES:
            assert (
                hashlib.sha256(z.read(name)).hexdigest()
                == hashlib.sha256((SKILL_DIR / name).read_bytes()).hexdigest()
            ), name


FENCED = (
    "```json\n"
    '{"version": 1, "root": {"id": "root", "label": "커피", "children": '
    '[{"id": "n1", "label": "에스프레소", "cite": [1]}]}}\n'
    "```"
)
VALID = (
    '{"version": 1, "root": {"id": "root", "label": "커피", "children": ['
    '{"id": "n1", "label": "에스프레소", "cite": [1]}, '
    '{"id": "n2", "label": "핸드 드립", "cite": [9]}, '
    '{"id": "n3", "label": "원두", "source_ref": {"source_id": "x", "chunk_id": "y"}}]}}'
)


@pytest.fixture
def llm_replies(monkeypatch):
    """LLM 게이트웨이 경계 대역 — 호출마다 목록의 다음 응답을 돌려준다(§12 R-1)."""
    replies: list[str] = []
    calls: list[int] = []

    async def fake_stream(db, model_id, messages, result, **kwargs):
        result.usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        text = replies[min(len(calls), len(replies) - 1)]
        calls.append(1)

        async def gen():
            yield text

        return gen()

    monkeypatch.setattr(
        "biznotes_core.services.gateway.llm.complete_stream_with_fallback", fake_stream
    )
    return replies, calls


@pytest.fixture
def cited_retrieve(monkeypatch):
    """검색 경계 대역 — 근거 번호 1번 발췌 하나(run 근거표의 원천).

    커버리지 옵션(`per_source`)을 받으면 실제 `retrieve_context`처럼 커버리지 요약도 낸다."""
    from biznotes_core.services.rag_chat import Citation, RetrievalResult, SourceCoverage

    async def fake_retrieve(db, notebook_id, query, source_ids, top_k=6, **options):
        coverage = SourceCoverage(scope_count=1, hit_sources=["s-1"], missing=[])
        return RetrievalResult(
            citations=[
                Citation(
                    n=1,
                    source_id="s-1",
                    chunk_id="c-1",
                    quote="에스프레소는 9기압",
                    source_title="커피",
                )
            ],
            insufficient_context=False,
            coverage=coverage if options else None,
        )

    monkeypatch.setattr("biznotes_core.services.rag_chat.retrieve_context", fake_retrieve)


def _ctx(db) -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={"topic": "커피", "extra": ""},
    )


async def test_fenced_reply_retries_once_then_fails(
    db, _mock_runtime_deps, cited_retrieve, llm_replies
):
    replies, calls = llm_replies
    replies.append(FENCED)
    ctx = _ctx(db)
    with pytest.raises(AgentRuntimeError, match="스키마 검증 최종 실패"):
        await execute_agent(ctx, "workflow", legacy_run_definition(load_skill_files(_files())))
    assert len(calls) == 2  # 첫 생성 + 재시도 1회
    assert ctx.artifact_keys == []
    assert _mock_runtime_deps == {}


async def test_retry_success_saves_validated_doc_with_resolved_refs(
    db, _mock_runtime_deps, cited_retrieve, llm_replies
):
    replies, calls = llm_replies
    replies.extend([FENCED, VALID])
    ctx = _ctx(db)
    await execute_agent(ctx, "workflow", legacy_run_definition(load_skill_files(_files())))
    assert len(calls) == 2
    assert [a["name"] for a in ctx.artifact_keys] == ["mindmap.json"]
    assert ctx.artifact_keys[0]["content_type"] == "application/json"
    doc = json.loads(_mock_runtime_deps[ctx.artifact_keys[0]["key"]])
    n1, n2, n3 = doc["root"]["children"]
    assert n1["source_ref"] == {"source_id": "s-1", "chunk_id": "c-1"}
    assert "source_ref" not in n2 and "source_ref" not in n3  # 표에 없는 번호·지어낸 쌍은 버린다
    assert all("cite" not in n for n in (n1, n2, n3))
    assert (doc["refs_resolved"], doc["refs_dropped"], doc["truncated"]) == (1, 2, False)
