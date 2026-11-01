"""정의 저장 검증 중 코어 v0.29.0이 더한 규칙 — 사용자(`/workflows`)·관리자(`/admin/agents`) 공용.

코어는 이 헬퍼들을 스스로 부르지 않는다. 저장 시 거부는 제품 배선의 몫이고, 실행 시점에는 코어
런타임이 같은 규칙으로 노드를 실패시키거나(사고 양·출력 하한) 정의를 닫는다(`audience`).
- `agent_audience_error`(#436 ③): 최상위 `audience`는 없음·null·`"admin"`만 받는다. 다른 값도
  런타임은 관리자 전용으로 보므로(닫힌 쪽) 저장에서 막아 의도를 드러낸다.
- `find_reasoning_effort_violations`(#429): llm·react_agent·json_validate의 `reasoning_effort`는
  `low`·`medium`·`high` 리터럴만.
- `find_min_output_tokens_violations`(#428): llm·json_validate의 `min_output_tokens`는 양의 정수이고
  노드 `max_tokens` 이하, 도구를 쓰는 llm 노드에는 둘 수 없다.

계획: docs/plans/core-v0.30.0-upgrade.md §2(AC-5).
"""

from typing import Any

from biznotes_core.services.agents.policy import (
    agent_audience_error,
    find_min_output_tokens_violations,
    find_reasoning_effort_violations,
)


def definition_policy_problems(
    definition: dict[str, Any], nodes: list[dict[str, Any]]
) -> list[str]:
    """위반 문구 목록 — 비어 있으면 통과다. `nodes`는 `kind_to_nodes` 결과다."""
    problems: list[str] = []
    audience = agent_audience_error(definition)
    if audience:
        problems.append(audience)
    effort = find_reasoning_effort_violations(nodes)
    if effort:
        problems.append(f"reasoning_effort를 저장할 수 없습니다: {', '.join(effort)}")
    floor = find_min_output_tokens_violations(nodes)
    if floor:
        problems.append(f"min_output_tokens를 저장할 수 없습니다: {', '.join(floor)}")
    return problems
