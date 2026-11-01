"""채팅 `/` 진입 run(#66) 테스트 공용 픽스처·헬퍼.

채팅 run은 진입 API로 만들거나(`post_entry`) `trigger="chat"` 행을 직접 만든다(`chat_run`). 스킬은
코어 서비스로 만들고 동결한다 — 테스트 환경은 표준 런타임이 꺼져 있어 legacy 스킬(필수 문자열
파라미터 `topic` 하나)이 된다. 경계만 대체한다: 이벤트 발행(`_publish`), 승인 재개 발행
(`run_agent.apply_async`), 스테이징 커밋 뒤 발행(코어 `celery_app.send_task`).
"""

import uuid
from typing import Any

import httpx
import pytest
from biznotes_core.models import (
    Agent,
    AgentRun,
    ChatMessage,
    ChatSession,
    Notebook,
    Skill,
    SkillReviewRequest,
    SkillVersion,
    User,
)
from biznotes_core.services.skills import service as svc

import app.workers.tasks.agent as agent_task
from tests.conftest import bearer, login

DENIED = "에이전트가 실행 직전 인가 조건을 더 이상 만족하지 않습니다"


@pytest.fixture
def boundaries(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """발행 경계 — 스테이징 뒤 코어 발행과 승인 재개 발행을 기록만 한다."""
    import biznotes_core.services.agent_runs as agent_runs

    class _Task:
        id = "fake"

    sent: list[tuple[Any, ...]] = []
    monkeypatch.setattr(agent_task.run_agent, "apply_async", lambda *a, **k: _Task())
    monkeypatch.setattr(agent_task, "_publish", lambda run_id, event: None)
    monkeypatch.setattr(agent_runs.celery_app, "send_task", lambda *a, **k: sent.append(a))
    return sent


async def notebook(db, owner: User, title: str = "entry-nb") -> Notebook:
    nb = Notebook(title=title, owner_id=owner.id)
    db.add(nb)
    await db.commit()
    await db.refresh(nb)
    return nb


async def session(db, nb: Notebook, user: User) -> ChatSession:
    s = ChatSession(notebook_id=nb.id, user_id=user.id)
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s


async def legacy_skill(
    db, owner: User, name: str = "brief-writer"
) -> tuple[Skill, SkillVersion, Agent]:
    """본인 private legacy 스킬을 만들고 동결한다(실행 version = 최신 freeze)."""
    skill = await svc.create_skill(db, owner, name, f"{name} 설명")
    version, wrapper = await svc.freeze_skill(db, skill, owner)
    await db.commit()
    await db.refresh(skill)
    return skill, version, wrapper


async def expose(
    db, skill: Skill, version: SkillVersion, visibility: str, *, approved: bool
) -> None:
    """스킬을 system·org로 공개한다. org 비소유자는 approved `expose_*`가 있어야 진입한다(#410
    D1).
    """
    skill.visibility = visibility
    skill.published_version_id = version.id
    if approved:
        db.add(
            SkillReviewRequest(
                version_id=version.id,
                kind="expose_system" if visibility == "system" else "expose_org",
                target_visibility=visibility,
                status="approved",
            )
        )
    await db.commit()


def react_definition(
    *, tools: tuple[str, ...] = ("http_request",), auto_approve: dict[str, Any] | None = None
) -> dict[str, Any]:
    """채팅 진입 형태의 workflow 정의(코어 `is_chat_callable`) — react 노드 하나, 목표는 채팅
    본문.
    """
    node: dict[str, Any] = {
        "id": "react",
        "type": "react_agent",
        "goal": "{chat_message}",
        "allowed_tools": list(tools),
        "max_iters": 3,
    }
    if auto_approve is not None:
        node["auto_approve_tools"] = auto_approve
    return {"nodes": [node], "edges": []}


async def chat_workflow(
    db,
    owner: User | None,
    *,
    handle: str | None = None,
    scope: str = "user",
    definition: dict[str, Any] | None = None,
) -> Agent:
    agent = Agent(
        name=f"wf-{uuid.uuid4().hex[:6]}",
        description="채팅 워크플로",
        kind="workflow",
        icon="⚙️",
        definition=definition or react_definition(tools=("rag_search",)),
        params_schema={},
        scope=scope,
        owner_id=owner.id if owner is not None else None,
        enabled=True,
        handle=handle,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


async def chat_run(
    db,
    agent: Agent,
    nb: Notebook,
    user: User,
    *,
    status: str = "succeeded",
    prompt: str = "질문",
    chat_session: ChatSession | None = None,
    **fields: Any,
) -> AgentRun:
    """`trigger="chat"` run을 직접 만든다 — 세션·질문 행 연결(코어 `_validate_chat_run_link`
    형태).
    """
    chat_session = chat_session or await session(db, nb, user)
    question = ChatMessage(session_id=chat_session.id, role="user", content=prompt)
    db.add(question)
    await db.flush()
    run = AgentRun(
        agent_id=agent.id,
        notebook_id=nb.id,
        user_id=user.id,
        params=fields.pop("params", {}),
        status=status,
        trigger="chat",
        chat_session_id=chat_session.id,
        prompt_message_id=question.id,
        **fields,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def token(client: httpx.AsyncClient, user: User) -> dict[str, str]:
    return bearer(await login(client, user.email))


async def post_entry(
    client: httpx.AsyncClient, user: User, session_id: uuid.UUID | str, **body: Any
) -> httpx.Response:
    body.setdefault("content", "이번 주 요약")
    return await client.post(
        f"/api/v1/chat/sessions/{session_id}/entry-runs",
        json={k: str(v) if isinstance(v, uuid.UUID) else v for k, v in body.items()},
        headers=await token(client, user),
    )
