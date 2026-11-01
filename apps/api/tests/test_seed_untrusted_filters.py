"""시드 템플릿의 `|untrusted` 필터 계약 (core-v0.20.0-upgrade AC-2.5, §5.6·D-13·HCI-9).

코어 v0.20.0 #228은 `{경로|untrusted}` 필터를 LLM 프롬프트 렌더(`render_prompt`)에서만 `kind="tool"`
데이터 블록으로 감싼다. 「시드·관리자 템플릿의 원시 외부 출력에 `|untrusted`를 단다」는 코어 요구를
시드가 지키는지 본다.

규칙(§5.6): 시드가 쓰는 에이전트 정의 전부를 코어 `templating._VAR_RE`로 훑어, LLM 프롬프트 필드
(llm `system`·`prompt`, react_agent `system`·`goal`, json_validate `prompt`)의 참조가

- `http`·`mcp_tool`·`agent_call`·`sub_workflow`·`error_handler`·`supervisor` 노드의 출력이거나
  `*.citations`(검색 인용 메타)면 `|untrusted`가 있어야 하고,
- `params.*`(사람이 쓴 지시 — 감싸면 「추가 지시사항」이 지시로 쓰이지 않는다)·`*.context`(생산자
  블록)에는 없어야 한다.

계획 §5.6의 `subagent`는 코어 v0.20.0 노드 타입(`graph.NODE_EXECUTORS`)이 아니다 — 하위 실행을
돌리는 노드 `sub_workflow`·`error_handler`로 읽는다(웹 편집기 안내 `node-catalog.ts`와 같은 집합 —
아래 `test_raw_external_node_types_match_core_and_web_guidance`가 고정한다).

수치는 런북 R-V ⑧과 P2 전환 4단계의 SQL 기대값(당시 `4|7`)에서 왔다 — 같은 SQL을 테스트 DB에서
그대로 돌려 대조한다. mindmap-agent-skill P4가 시드 워크플로우 「마인드맵」(필터 2곳)을
시스템 스킬로 바꿔 정의에서 뺐고, biznotes는 휴가 예제(「내 연차 잔여일 확인」, 필터 1곳)를
가져오지 않았으므로 기준은 `2|4`다. 렌더 대조: 코어 `render_prompt`로 시드 프롬프트를 렌더하면
인용 메타가 블록 안에 있고, 필터를 뗀 같은 프롬프트로는 블록 밖이다(음성 대조).
실측 수치는 junit property(`seed_filter_census`)로 남긴다.

시드 함수는 실경로 그대로 부른다(대체 없음). DB는 conftest의 격리 테스트 DB다.
"""

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from biznotes_core.core.db import async_session_factory
from biznotes_core.models import Agent
from biznotes_core.services.agents.graph import NODE_EXECUTORS
from biznotes_core.services.agents.runtime import kind_to_nodes
from biznotes_core.services.agents.templating import _VAR_RE, render_prompt
from biznotes_core.services.untrusted import UNTRUSTED_TAG
from sqlalchemy import select, text

from tests.test_seed import _seed_mcp_and_samples

PROMPT_FIELDS: dict[str, tuple[str, ...]] = {
    "llm": ("system", "prompt"),
    "react_agent": ("system", "goal"),
    "json_validate": ("prompt",),
}
RAW_EXTERNAL_NODE_TYPES = frozenset(
    {"http", "mcp_tool", "agent_call", "sub_workflow", "error_handler", "supervisor"}
)
NODE_CATALOG_TS = Path(__file__).resolve().parents[2] / "web/src/lib/workflow/node-catalog.ts"
TOOL_OPEN = f'<{UNTRUSTED_TAG} kind="tool">'
TOOL_CLOSE = f"</{UNTRUSTED_TAG}>"
# 런북 R-V ⑧·P2 전환 4단계와 같은 SQL(시스템 정의 중 필터를 가진 정의 수 | 필터 위치 수)
RUNBOOK_FILTER_SQL = (
    "SELECT count(*) || '|' || coalesce(sum(regexp_count(definition::text, "
    "'\\{[a-zA-Z_][a-zA-Z0-9_.]*\\|untrusted\\}')), 0) FROM agents "
    "WHERE scope = 'system' AND definition::text ~ '\\{[a-zA-Z_][a-zA-Z0-9_.]*\\|untrusted\\}'"
)
FILTER_TEXT_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_.]*\|untrusted\}")


async def _seeded_system_agents() -> list[Agent]:
    await _seed_mcp_and_samples()
    async with async_session_factory() as s:
        return list(
            (await s.execute(select(Agent).where(Agent.scope == "system").order_by(Agent.name)))
            .scalars()
            .all()
        )


def _web_untrusted_note_node_types(source: str) -> set[str]:
    """`NODE_CATALOG`에서 설명에 `${UNTRUSTED_OUTPUT_NOTE}`가 붙은 노드 타입(키) 집합."""
    block = source.split("export const NODE_CATALOG", 1)[1].split("\n};", 1)[0]
    found: set[str] = set()
    current = None
    for line in block.splitlines():
        if m := re.match(r"^  ([a-z_]+): \{$", line):
            current = m.group(1)
        elif line.startswith("    description:") and "${UNTRUSTED_OUTPUT_NOTE}" in line:
            assert current is not None, line
            found.add(current)
    return found


def test_raw_external_node_types_match_core_and_web_guidance() -> None:
    """판정 집합은 코어 노드 타입 안이고, 웹 편집기 안내가 붙는 노드와 같다."""
    assert RAW_EXTERNAL_NODE_TYPES <= set(NODE_EXECUTORS), RAW_EXTERNAL_NODE_TYPES - set(
        NODE_EXECUTORS
    )
    source = NODE_CATALOG_TS.read_text(encoding="utf-8")
    assert _web_untrusted_note_node_types(source) == RAW_EXTERNAL_NODE_TYPES
    # 음성 대조 — sub_workflow 설명의 안내를 떼면 그 타입이 빠진다
    note = " (${UNTRUSTED_OUTPUT_NOTE})"
    line = next(ln for ln in source.splitlines() if "입출력 매핑과 함께 호출합니다" in ln)
    assert note in line
    mutated = _web_untrusted_note_node_types(source.replace(line, line.replace(note, "")))
    assert mutated == RAW_EXTERNAL_NODE_TYPES - {"sub_workflow"}


def _prompt_refs(agent: Agent) -> list[dict[str, Any]]:
    """정의의 LLM 프롬프트 필드 참조 — 노드는 코어 `kind_to_nodes`로 정규화한다(모든 kind)."""
    nodes, _edges = kind_to_nodes(agent.kind, agent.definition)
    types = {n["id"]: n["type"] for n in nodes}
    refs: list[dict[str, Any]] = []
    for node in nodes:
        for field in PROMPT_FIELDS.get(node["type"], ()):
            value = node.get(field)
            if not isinstance(value, str):
                continue
            for m in _VAR_RE.finditer(value):
                path = m.group(1)
                root = path.split(".", 1)[0]
                refs.append(
                    {
                        "agent": agent.name,
                        "node": f"{node['id']}:{node['type']}",
                        "field": field,
                        "path": path,
                        "filtered": m.group(2) is not None,
                        "target": "(state:params)" if root == "params" else types.get(root),
                    }
                )
    return refs


async def test_seed_prompt_filters_follow_the_rule(record_property) -> None:
    agents = await _seeded_system_agents()
    refs = [ref for agent in agents for ref in _prompt_refs(agent)]
    assert refs, "프롬프트 참조가 0개다 — 검사가 공허하다"

    must, must_not = [], []
    for ref in refs:
        path, target = ref["path"], ref["target"]
        raw_external = target in RAW_EXTERNAL_NODE_TYPES or path.endswith(".citations")
        instruction_or_block = target == "(state:params)" or path.endswith(".context")
        assert not (raw_external and instruction_or_block), ref
        if raw_external:
            must.append(ref)
            assert ref["filtered"], f"원시 외부 출력에 |untrusted가 없다: {ref}"
        elif instruction_or_block:
            must_not.append(ref)
            assert not ref["filtered"], f"지시·생산자 블록에 |untrusted가 있다: {ref}"

    # 필터는 원시 외부 출력 자리에만 있다
    assert [r for r in refs if r["filtered"]] == must
    by_agent = Counter(r["agent"] for r in must)
    assert by_agent == {
        "퀴즈 생성": 2,
        "플래시카드 생성": 2,
    }
    assert sorted({r["path"] for r in must}) == ["search.citations"]
    assert {r["path"] for r in must_not} >= {"search.context"}
    assert any(r["path"].startswith("params.") for r in must_not)
    record_property(
        "seed_filter_census",
        f"agents={len(agents)} prompt_refs={len(refs)} filtered_definitions={len(by_agent)} "
        f"filtered_positions={len(must)} unfiltered_params_or_context={len(must_not)}",
    )


async def test_seed_filter_census_matches_runbook_sql(record_property) -> None:
    """런북 SQL 그대로 — 정의 2 · 위치 4. 필터는 프롬프트 필드 밖(검색어·인자)에도 없다."""
    agents = await _seeded_system_agents()
    async with async_session_factory() as s:
        census = (await s.execute(text(RUNBOOK_FILTER_SQL))).scalar_one()
    assert census == "2|4"

    total = sum(
        len(FILTER_TEXT_RE.findall(json.dumps(a.definition, ensure_ascii=False))) for a in agents
    )
    in_prompts = sum(1 for agent in agents for ref in _prompt_refs(agent) if ref["filtered"])
    assert total == in_prompts == 4
    record_property(
        "seed_filter_census_sql", f"runbook_sql={census} definition_text_positions={total}"
    )


async def test_seed_second_run_keeps_filtered_definitions() -> None:
    """`make seed`를 다시 돌려도(시스템 정의 덮어쓰기) 필터 수가 그대로다."""
    await _seeded_system_agents()
    agents = await _seeded_system_agents()
    async with async_session_factory() as s:
        assert (await s.execute(text(RUNBOOK_FILTER_SQL))).scalar_one() == "2|4"
    assert len({a.name for a in agents}) == len(agents)


def _tool_blocks(rendered: str) -> list[str]:
    blocks, pos = [], 0
    while (start := rendered.find(TOOL_OPEN, pos)) >= 0:
        end = rendered.index(TOOL_CLOSE, start)
        blocks.append(rendered[start + len(TOOL_OPEN) : end])
        pos = end + len(TOOL_CLOSE)
    return blocks


async def test_render_prompt_puts_seed_citations_in_tool_blocks() -> None:
    agents = {a.name: a for a in await _seeded_system_agents()}
    quiz = next(n for n in agents["퀴즈 생성"].definition["nodes"] if n["id"] == "gen")
    citations = [{"n": 1, "source_id": "s-1", "chunk_id": "c-1", "quote": "에스프레소는 9기압"}]
    citations_text = json.dumps(citations, ensure_ascii=False)
    state = {
        "params": {
            "topic": "커피",
            "difficulty": "보통",
            "count": 3,
            "extra": "표로 정리하세요",
        },
        "search": {"context": "", "citations": citations},
    }

    rendered = render_prompt(quiz["prompt"], state)
    blocks = _tool_blocks(rendered)
    assert any(citations_text in b for b in blocks), rendered
    assert "표로 정리하세요" in rendered
    assert all("표로 정리하세요" not in b for b in blocks)  # 사람의 지시는 블록 밖
    # 음성 대조 — 필터를 뗀 같은 프롬프트(태그 없는 값)는 인용 메타를 블록 밖에 그대로 둔다
    unfiltered = render_prompt(quiz["prompt"].replace("|untrusted", ""), state)
    assert citations_text in unfiltered and _tool_blocks(unfiltered) == []
