"""이미지 프롬프트 출처의 저장 시점 검사 — 사용자·관리자 저장 경로 공용(core-v0.23.0-upgrade D2).

코어 v0.23.0(#338 D1)은 `image_generate` 프롬프트가 출처가 안전하지 않은 값(검색·소스·HTTP·MCP
출력, `|untrusted`)을 참조하면 그래프 구성 때 run을 실패시킨다. 저장 시점에도 같은 규칙을 걸어
"저장은 되는데 실행 전에 멈추는" 정의를 막는다(D-26과 같은 원칙).

params는 안전으로 본다(`params_safe=True`). 최상위 run에서는 params가 안전하다. 하위 실행에서
쓰일 때의 안전 여부는 부모 정의가 정하므로, 저장 시점에는 어느 맥락에서도 실패하는 정의만 거부한다.
판정 규칙은 복제하지 않는다 — 코어 `prompt_provenance`가 정본이다.
"""

from typing import Any

from biznotes_core.services.agents.errors import AgentRuntimeError
from biznotes_core.services.agents.prompt_provenance import (
    check_image_prompt_sources,
    image_safe_roots,
)


def find_unsafe_image_prompts(nodes: list[dict[str, Any]]) -> list[str]:
    """출처가 안전하지 않은 값을 참조하는 `image_generate` 노드마다 코어 사유 문구 하나."""
    image_nodes = [n for n in nodes if n.get("type") == "image_generate"]
    if not image_nodes:
        return []
    roots = image_safe_roots({str(n.get("id")): n for n in nodes}, params_safe=True)
    problems: list[str] = []
    for node in image_nodes:
        try:
            check_image_prompt_sources(node, roots)
        except AgentRuntimeError as e:
            problems.append(str(e))
    return problems
