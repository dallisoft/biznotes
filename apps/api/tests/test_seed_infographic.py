"""시드 워크플로우 「인포그래픽 데이터」 ↔ 웹 뷰어 입력 형식 계약(#181).

코어 json_validate는 등록된 이름 스키마(mindmap·flashcards·quiz·dialogue)만 받으므로 인포그래픽은
서버 검증 노드 없이 저장되고, 형식 판정은 웹 `lib/infographic/parse.ts`가 한다. 그래서 프롬프트가
요구하는 키와 뷰어가 읽는 키가 어긋나지 않게 양쪽을 대조한다. DB를 쓰지 않는다.
"""

from pathlib import Path

from biznotes_core.services.agents.templating import _VAR_RE

from scripts.seed import (
    _COVERAGE_CONTEXT,
    _EXCERPTS_PLACEHOLDER,
    _INFOGRAPHIC_PROMPT,
    SEED_AGENTS,
)

WEB_PARSER = Path(__file__).resolve().parents[2] / "web/src/lib/infographic/parse.ts"
VIEWER_REGISTRY = Path(__file__).resolve().parents[2] / "web/src/lib/artifact-viewers.ts"

# 뷰어 입력 형식의 키 — parse.ts 머리 주석·판정 코드와 같다.
CONTRACT_KEYS = (
    "title",
    "subtitle",
    "key_stats",
    "label",
    "value",
    "note",
    "sections",
    "heading",
    "points",
    "charts",
    "unit",
    "data",
)


def _infographic_spec() -> dict:
    [spec] = [s for s in SEED_AGENTS if s["name"] == "인포그래픽 데이터"]
    return spec


def test_workflow_saves_extract_output_as_infographic_json():
    definition = _infographic_spec()["definition"]
    nodes = {n["id"]: n for n in definition["nodes"]}
    # 모든 소스 반영(#283) — digest(전체 요약) → search(소스마다 발췌) → extract → save, 끝에
    # coverage.json
    assert [n["type"] for n in definition["nodes"]] == [
        "source_digest",
        "rag_search",
        "llm",
        "save_artifact",
        "json_compose",
        "save_artifact",
    ]
    assert ["search", "extract"] in definition["edges"]
    assert ["extract", "save"] in definition["edges"]
    assert nodes["search"]["coverage"] == "per_source"
    assert nodes["extract"]["prompt"] == _INFOGRAPHIC_PROMPT.replace(
        _EXCERPTS_PLACEHOLDER, _COVERAGE_CONTEXT
    )
    assert nodes["save"]["name"] == "infographic.json"
    assert nodes["save"]["content_type"] == "application/json"
    assert nodes["save"]["content"] == "{extract.text}"


def test_prompt_and_web_parser_share_contract_keys():
    parser_src = WEB_PARSER.read_text(encoding="utf-8")
    for key in CONTRACT_KEYS:
        assert f'"{key}"' in _INFOGRAPHIC_PROMPT, f"프롬프트에 {key} 없음"
        assert key in parser_src, f"웹 파서에 {key} 없음"
    # 차트 값은 숫자(뷰어가 막대 비율을 계산한다)
    assert '"value": number' in _INFOGRAPHIC_PROMPT
    assert '"infographic.json": "infographic"' in VIEWER_REGISTRY.read_text(encoding="utf-8")


def test_prompt_references_only_params_and_search_context():
    # 인용 메타·외부 출력은 없다 — |untrusted 대상 없음(test_seed_untrusted_filters 기준).
    refs = {m.group(1) for m in _VAR_RE.finditer(_INFOGRAPHIC_PROMPT)}
    assert refs == {"params.topic", "params.extra", "search.context"}
