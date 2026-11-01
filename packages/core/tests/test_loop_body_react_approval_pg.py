# 신규(#80). 필요: PostgreSQL·Valkey(PG 테스트).
"""loop 본문 react_agent의 승인 요청은 조용히 사라지지 않는다 (#80).

순차 loop 본문에 부작용 도구(`http_request`)를 부르는 react_agent를 두면, 예전에는 자식 ctx에만
`pending_approval`이 채워지고 부모 run이 정상 완료됐다(승인 요청도, 도구 실행도 없이). 이제
`_run_subagent` 경로와 같은 `SubagentApprovalNotSupportedError`로 실패한다. 이벤트 순서·병렬
분기·세션 해제는 `tests/test_loop_body_approval.py`가 DB 없이 고정한다(병렬 loop의 react_agent는
`in_parallel_branch` 가드가 먼저 막으므로 여기서는 순차만 본다).

`create_all` 기반이라 Alembic 마이그레이션 검증이 아니다.
"""

import json
import uuid
from typing import Any

import pytest

from biznotes_core.models import Agent
from biznotes_core.services.agents.errors import SubagentApprovalNotSupportedError
from biznotes_core.services.agents.runtime import RunContext, execute_agent
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult

HTTP_ARGS = {"url": "https://example.com/x", "method": "POST"}


def _http_turn() -> ToolTurnResult:
    call = ToolCall(id="c", name="http_request", arguments=HTTP_ARGS)
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "c",
                "type": "function",
                "function": {"name": "http_request", "arguments": json.dumps(HTTP_ARGS)},
            }
        ],
    }
    return ToolTurnResult(
        content="",
        tool_calls=[call],
        assistant_message=message,
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        model=None,
    )


async def _body_agent(db: Any, owner_id: uuid.UUID) -> Agent:
    body = Agent(
        name=f"loop-body-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        icon="🔁",
        definition={
            "nodes": [
                {
                    "id": "inner",
                    "type": "react_agent",
                    "goal": "본문 부작용",
                    "allowed_tools": ["http_request"],
                }
            ],
            "edges": [],
        },
        params_schema={},
        scope="system",
        owner_id=owner_id,
        enabled=True,
    )
    db.add(body)
    await db.commit()
    await db.refresh(body)
    return body


def _loop_parent(body: Agent, items: list[int]) -> dict[str, Any]:
    return {
        "nodes": [
            {
                "id": "lp",
                "type": "loop",
                "loop_body_agent_id": str(body.id),
                "items": items,
                "parallel": False,
            }
        ],
        "edges": [],
    }


async def test_loop_body_react_approval_fails_instead_of_completing_silently(
    db: Any, user_a: Any, mock_llm: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    turns = {"n": 0}

    async def fake_completer(db: Any, model_id: Any, messages: Any, tools: Any, **kw: Any) -> Any:
        turns["n"] += 1
        return _http_turn()

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake_completer)
    body = await _body_agent(db, user_a.id)
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, user_id=user_a.id
    )

    with pytest.raises(SubagentApprovalNotSupportedError, match="서브에이전트.*내부"):
        await execute_agent(ctx, "workflow", _loop_parent(body, [1, 2]))

    # 승인 요청은 부모로 새지 않고(재개할 수 없는 checkpoint), 도구는 승인 대기에서 멈췄다.
    # 첫 항목에서 실패하므로 두 번째 항목의 모델 호출은 없다.
    assert ctx.pending_approval is None
    assert [(c["tool"], c["status"]) for c in ctx.tool_calls] == [
        ("http_request", "awaiting_approval")
    ]
    assert turns["n"] == 1
