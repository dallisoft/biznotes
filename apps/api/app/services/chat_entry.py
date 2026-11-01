"""채팅 `/` 진입 run(#66)의 제품 규칙 — 결과 메시지·LLM 이력·세션의 진입 run 조회.

라우터(`api/v1/chat.py`·`agents.py`)와 워커(`workers/tasks/agent.py`)가 함께 쓴다. 진입 허용과
run 열람 판정은 코어(`services.agents.chat_entry`·`services.authz`)가 하고, 여기서는 그 판정을
다시 계산하지 않는다. 표시·저장·이력 규칙만 둔다(docs/plans/chat-slash-entry.md §3).

- 진입 턴은 user 질문 행(`agent_runs.prompt_message_id`)과 assistant 결과 행(`chat_messages.run_id`,
  run마다 1행 — 코어 부분 유니크 인덱스 `uq_chat_messages_run_assistant`)이다.
- 실패·중지 결과 행에는 `token_usage`에 표식 `chat_entry_no_result: 1`을 단다(마인드맵 표식과 같은
  방식, 새 열 없음). 표식이 있거나 결과 행이 없는 진입 턴은 질문까지 LLM 이력에서 뺀다(D5).
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from biznotes_core.models import Agent, AgentRun, ChatMessage, ChatSession, Skill, SkillVersion
from biznotes_core.services.agent_runs import ACTIVE_RUN_STATUSES
from sqlalchemy import exists, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.limits import get_limit_settings

# 마인드맵 노드 질문(origin=mindmap)의 저장 표식과 이력 대체 문구(mindmap-agent-skill D-17, §6.4.4).
# 라벨은 LLM이 소스에서 만든 비신뢰 문자열이다 — 표식 붙은 user 메시지는 이후 모든 턴의 LLM 이력에서
# 원문 대신 고정 문구로 넘긴다. 저장 원문은 그대로라 사용자는 대화에서 원래 질문을 본다.
MINDMAP_PROMPT_MARK = "mindmap_prompt"
MINDMAP_HISTORY_PLACEHOLDER = "(마인드맵 항목 설명 요청)"

# 진입 run의 실패·중지 결과 행 표식과 고정 문구(#66 §3 워커)
ENTRY_NO_RESULT_MARK = "chat_entry_no_result"
ENTRY_CANCELLED_TEXT = "중지했습니다"
ENTRY_FAILED_PREFIX = "실행하지 못했습니다 — "

# 결과 행 부분 유니크 인덱스의 조건 — `INSERT … ON CONFLICT DO NOTHING`의 대상 지정에 쓴다
_RESULT_INDEX_WHERE = text("run_id IS NOT NULL AND role = 'assistant'")


def entry_failed_text(reason: str) -> str:
    """실패 결과 행 본문 — 「실행하지 못했습니다 — <코어·워커 고정 문구>」.

    예외 원문은 넣지 않는다.
    """
    return f"{ENTRY_FAILED_PREFIX}{reason}"


def is_mindmap_prompt(message: ChatMessage) -> bool:
    usage = message.token_usage
    return (
        message.role == "user" and isinstance(usage, dict) and usage.get(MINDMAP_PROMPT_MARK) == 1
    )


def is_no_result(message: ChatMessage) -> bool:
    """진입 run의 실패·중지 결과 행인가 — 표식은 assistant 역할에만 의미가 있다."""
    usage = message.token_usage
    return (
        message.role == "assistant"
        and isinstance(usage, dict)
        and usage.get(ENTRY_NO_RESULT_MARK) == 1
    )


def history_for_llm(
    history: Sequence[ChatMessage], entry_prompts: Mapping[uuid.UUID, uuid.UUID] | None = None
) -> list[ChatMessage]:
    """LLM에 넘길 이력(저장 행은 그대로).

    - 표식 붙은 마인드맵 질문은 content만 고정 문구로 바꾼다. 대체본은 세션에 붙지 않은 임시
      객체다 — 코어 조립기는 `role`·`content`만 읽는다.
    - 진입 턴(`entry_prompts`: 질문 행 id → run id)은 「질문 + 결과 텍스트」 두 줄로 둔다. 결과 행이
      없거나(진행 중·결과 미기록) 실패·중지 표식이 있으면 질문까지 뺀다(#66 D5). 질문이 이력에
      없는 결과 행도 뺀다.

    정적(`build_messages`)·자율(`node["history"]`)·진입 run(`RunContext.chat_history`) 세 경로가
    모두 이 함수를 거친다.
    """
    prompts = entry_prompts or {}
    results = {m.run_id: m for m in history if m.role == "assistant" and m.run_id is not None}
    asked = {prompts[m.id] for m in history if m.role == "user" and m.id in prompts}
    kept = {
        run_id
        for run_id in asked
        if (result := results.get(run_id)) is not None and not is_no_result(result)
    }
    out: list[ChatMessage] = []
    for m in history:
        if m.role == "user" and m.id in prompts:
            if prompts[m.id] in kept:
                out.append(m)
            continue
        if m.role == "assistant" and m.run_id is not None:
            if m.run_id in kept:
                out.append(m)
            continue
        if is_mindmap_prompt(m):
            out.append(ChatMessage(role=m.role, content=MINDMAP_HISTORY_PLACEHOLDER))
        else:
            out.append(m)
    return out


@dataclass(frozen=True)
class EntryRunRef:
    """user 질문 행에 붙는 진입 run 요약(`ChatMessageOut.entry_run`).

    표시 이름은 실행한 스킬 version의 박제 이름(`skill_snapshot.name`)이고 workflow는 정의 이름이다.
    스킬 wrapper의 `name`은 최신 freeze 값이라 쓰지 않는다(코어 skill-chat-entry §5.2 N2).
    """

    run_id: uuid.UUID
    agent_id: uuid.UUID
    display_name: str
    slash_name: str | None
    status: str


async def session_entry_runs(
    db: AsyncSession, session_id: uuid.UUID
) -> dict[uuid.UUID, EntryRunRef]:
    """세션의 진입 run을 질문 행 id로 묶어 돌려준다(쿼리 1회)."""
    rows = await db.execute(
        select(
            AgentRun.prompt_message_id,
            AgentRun.id,
            AgentRun.agent_id,
            AgentRun.status,
            AgentRun.skill_snapshot,
            Agent.name,
            Agent.handle,
            Skill.slug,
        )
        .join(Agent, AgentRun.agent_id == Agent.id)
        .outerjoin(SkillVersion, SkillVersion.id == AgentRun.skill_version_id)
        .outerjoin(Skill, Skill.id == SkillVersion.skill_id)
        .where(
            AgentRun.chat_session_id == session_id,
            AgentRun.trigger == "chat",
            AgentRun.prompt_message_id.is_not(None),
        )
    )
    out: dict[uuid.UUID, EntryRunRef] = {}
    for prompt_id, run_id, agent_id, status, snapshot, agent_name, handle, slug in rows.all():
        snapshot_name = snapshot.get("name") if isinstance(snapshot, dict) else None
        out[prompt_id] = EntryRunRef(
            run_id=run_id,
            agent_id=agent_id,
            display_name=str(snapshot_name or agent_name),
            slash_name=slug if slug is not None else handle,
            status=status,
        )
    return out


async def has_active_entry_run(db: AsyncSession, session_id: uuid.UUID) -> bool:
    """세션에 끝나지 않은 진입 run이 있는가(D3) — 코어 `uq_agent_runs_chat_active`와 같은 조건."""
    return bool(
        await db.scalar(
            select(
                exists().where(
                    AgentRun.chat_session_id == session_id,
                    AgentRun.status.in_(ACTIVE_RUN_STATUSES),
                )
            )
        )
    )


def _int_usage(usage: Mapping[str, Any]) -> dict[str, int]:
    """`ChatMessageOut.token_usage`(dict[str, int])로 읽히는 값만 남긴다."""
    return {
        k: v for k, v in usage.items() if isinstance(v, int) and not isinstance(v, bool)
    }


async def write_entry_result(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    session_id: uuid.UUID | None,
    content: str,
    model_id: uuid.UUID | None = None,
    token_usage: Mapping[str, Any] | None = None,
    no_result: bool = False,
) -> None:
    """진입 run의 assistant 결과 행을 쓴다(커밋은 호출자). run마다 한 번만 쓴다.

    이미 결과 행이 있으면(취소 API와 워커가 겹친 경우 등) 아무것도 하지 않는다 — 코어 부분 유니크
    인덱스에 `ON CONFLICT DO NOTHING`으로 맞춘다. 세션이 지워졌으면(`chat_session_id` NULL) 쓰지
    않는다. 세션 `updated_at`을 갱신해 「최근 대화」 순서에 반영한다.
    """
    if session_id is None:
        return
    usage = _int_usage(token_usage or {})
    if no_result:
        usage[ENTRY_NO_RESULT_MARK] = 1
    await db.execute(
        pg_insert(ChatMessage)
        .values(
            id=uuid.uuid4(),
            session_id=session_id,
            role="assistant",
            content=content,
            citations=[],
            model_id=model_id,
            token_usage=usage,
            run_id=run_id,
        )
        .on_conflict_do_nothing(
            index_elements=[ChatMessage.run_id], index_where=_RESULT_INDEX_WHERE
        )
    )
    await db.execute(
        update(ChatSession)
        .where(ChatSession.id == session_id)
        .values(updated_at=datetime.now(UTC))
    )


@dataclass(frozen=True)
class ChatRunContext:
    """워커가 `RunContext`에 싣는 채팅 입력(D5) — 질문 본문과 질문 앞의 LLM 이력."""

    message: str
    history: list[dict[str, Any]]


class ChatRunContextInvalid(ValueError):
    """진입 run의 질문 행이 없거나 세션·사용자와 맞지 않는다(스테이징 뒤 지워진 경우 등)."""


async def load_chat_run_context(db: AsyncSession, run: AgentRun) -> ChatRunContext:
    """진입 run의 질문과 그 앞 이력을 읽는다.

    연결은 스테이징 때 코어 `_validate_chat_run_link`가 확인했지만, 그 뒤 행이 지워지거나 옮겨졌을
    수 있어 다시 본다. 이력은 질문보다 앞선 행(`seq` 엄격 비교 — 같은 턴의 결과를 넣지 않는다)을
    `history_for_llm`에 통과시킨 뒤 관리자 「대화 이력 길이」만큼 자른다(0이면 비운다).
    """
    if run.chat_session_id is None or run.prompt_message_id is None:
        raise ChatRunContextInvalid("chat run 연결 필드가 없습니다")
    session = await db.get(ChatSession, run.chat_session_id)
    prompt = await db.get(ChatMessage, run.prompt_message_id)
    if session is None or session.user_id != run.user_id:
        raise ChatRunContextInvalid("chat session 소유자가 run 사용자와 일치하지 않습니다")
    if prompt is None or prompt.role != "user" or prompt.session_id != session.id:
        raise ChatRunContextInvalid("prompt message가 chat session의 user 메시지가 아닙니다")

    rows = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id, ChatMessage.seq < prompt.seq)
        .order_by(ChatMessage.seq)
    )
    earlier = list(rows.scalars().all())
    entry_runs = await session_entry_runs(db, session.id)
    entry_prompts = {prompt_id: ref.run_id for prompt_id, ref in entry_runs.items()}
    limit = int((await get_limit_settings(db))["chat_history_messages"])
    history = history_for_llm(earlier, entry_prompts)[-limit:] if limit else []
    return ChatRunContext(
        message=prompt.content,
        history=[{"role": m.role, "content": m.content} for m in history],
    )
