import asyncio
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Literal, cast

import structlog
from biznotes_core.api.deps import get_current_user
from biznotes_core.core.db import get_db
from biznotes_core.core.rate_limit import RateLimiter
from biznotes_core.core.uow import publishing_unit_of_work
from biznotes_core.models import Agent, ChatMessage, ChatSession, Notebook, User
from biznotes_core.schemas.enums import ProviderType
from biznotes_core.services.agent_runs import (
    ActiveChatRunExists,
    recover_chat_run_conflict,
    stage_agent_run,
)
from biznotes_core.services.agents.chat_entry import (
    ChatEntry,
    assert_chat_entry_allowed,
    chat_entry_params,
    list_chat_entry_agents,
    resolve_chat_entry_by_name,
)
from biznotes_core.services.agents.node_errors import describe_node_failure
from biznotes_core.services.events import format_sse, sse_response
from biznotes_core.services.gateway.llm import (
    GatewayError,
    StreamResult,
    complete_stream_with_fallback,
    list_models,
    resolve_model,
)
from biznotes_core.services.notebooks import get_permission
from biznotes_core.services.rag_chat import (
    Citation,
    build_messages,
    filter_cited,
    retrieve_context,
    verify_citations,
)
from biznotes_core.services.rerank import RerankUnavailableError
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exception_handlers import RERANK_UNAVAILABLE_DETAIL
from app.schemas.agent import AgentRunOut
from app.schemas.chat import (
    ChatEntryAmbiguousOut,
    ChatEntryConflictOut,
    ChatEntryOut,
    ChatEntryRunCreate,
    ChatEntryRunOut,
    ChatEntryRunRefOut,
    ChatMessageOut,
    ChatSessionOut,
    ChatSessionUpdate,
    LLMModelOut,
    RecentChatOut,
    SendMessageRequest,
)
from app.schemas.provenance import ProvenanceOut
from app.services.chat_entry import MINDMAP_HISTORY_PLACEHOLDER as MINDMAP_HISTORY_PLACEHOLDER
from app.services.chat_entry import (
    MINDMAP_PROMPT_MARK,
    EntryRunRef,
    has_active_entry_run,
    is_mindmap_prompt,
    session_entry_runs,
)
from app.services.chat_entry import history_for_llm as _history_for_llm
from app.services.limits import get_limit_settings
from app.services.provenance import (
    build_chat_provenance,
    filter_citations_to_notebook,
    public_evidence_items,
)
from app.services.skill_install_chat import install_offer_stream
from app.services.skills.github_install import detect_github_skill_install

router = APIRouter(tags=["chat"])
logger = structlog.get_logger()

# 취소된 스트림이 떼어 낸 사용량 기록 태스크 — 끝날 때까지 참조를 잡아 GC로 사라지지 않게 한다
_pending_usage_records: set[asyncio.Task[None]] = set()


async def _record_usage_safely(user_id: uuid.UUID, tokens: int) -> None:
    from biznotes_core.services.usage_guard import record_usage

    try:
        await record_usage(user_id, "tokens", tokens)
    except Exception:  # noqa: BLE001 - 취소 뒤 기록 실패가 다른 요청에 번지지 않게 로그만 남긴다
        logger.exception("chat_stop_usage_record_failed")


async def _record_usage_shielded(user_id: uuid.UUID, tokens: int) -> None:
    """쓴 토큰을 일일 한도에 넣는다 — 이 await가 취소돼도 기록은 끝까지 간다.

    Starlette는 연결이 끊기면 anyio 취소 범위로 스트림을 취소한다. 그 범위 안에서는 이후
    await가 다시 취소되므로, 기록을 별도 태스크로 떼어 shield한다.
    """
    if tokens <= 0:
        return
    task = asyncio.ensure_future(_record_usage_safely(user_id, tokens))
    _pending_usage_records.add(task)
    task.add_done_callback(_pending_usage_records.discard)
    await asyncio.shield(task)


async def _record_stopped_usage(user_id: uuid.UUID, tokens: int, *, estimated: bool) -> None:
    """답변 중지(연결 끊김)로 취소된 스트림이 쓴 토큰을 일일 한도에 넣는다(ui-ux-improvements
    B3)."""
    logger.info("chat_stream_stopped", tokens=tokens, estimated=estimated)
    await _record_usage_shielded(user_id, tokens)


# 마인드맵 질문 표식·이력 대체 문구와 LLM 이력 규칙(`_history_for_llm`)은 진입 run 워커와 함께
# 쓰므로 `app.services.chat_entry`에 있다(#66 D5). 이 모듈 이름으로도 그대로 읽힌다.

# 자율 답변의 인용 규칙(#161) — 코어 정적 경로 `rag_chat.SYSTEM_PROMPT` 규칙 2번과 같은 문구다
# (`test_autonomous_citation_rule_matches_static_rag_chat_wording`이 드리프트를 막는다).
# rag_search 발췌는 `[n] (제목)` 머리 줄로 보이고 번호는 run 안에서 유일하다
# (`_record_run_citations`).
AUTONOMOUS_CITATION_RULE = (
    "근거 발췌 번호를 문장 끝에 [n] 형식으로 인용하세요. 복수 인용은 [1][3]처럼 나란히 표기합니다."
)
# 인용 번호의 범위 — 코어는 `rag_search` 발췌에만 번호를 매기고 `source_load`(전체 원문)에는
# 매기지 않는다. 라이브에서 source_load만 쓴 답변에 근거 없는 `[1]`이 남아 범위를 못박는다.
AUTONOMOUS_CITATION_SCOPE = (
    "[n]은 rag_search 발췌 머리 줄에 붙은 번호만 쓰고, source_load로 읽은 원문이나 다른 "
    "도구 결과에는 번호를 지어내지 마세요. 근거를 짚어야 하면 rag_search로 발췌를 찾아 인용하세요."
)

# 소스 도달 규칙(#284, 코어 v0.30.0 #447) — 소스가 수백 개인 노트북에서도 필요한 소스를
# 골라 읽게 한다. `source_list`는 run 범위 소스의 id·제목·요약을 페이지로 낸다.
# `source_load`·`rag_search`는 `source_ids`로 범위를 좁힐 수 있고(run 범위 밖은 넓히지 않는다),
# `source_load`는 개수 상한으로 남은 소스가 있으면 `next_offset`을 낸다. 인자 없이
# `source_load`를 부르면 가장 오래된 소스부터 읽으므로 특정·최근 소스는 목록으로 id를 먼저
# 찾게 한다.
AUTONOMOUS_SOURCE_RULE = (
    "소스 규칙: 어떤 소스가 있는지, 특정 소스나 최근에 추가한 소스가 필요하면 먼저 "
    "source_list로 후보(id·제목·요약)를 확인하세요(next_offset이 있으면 다음 페이지가 "
    "있습니다). 그 뒤 source_load나 rag_search에 source_ids를 지정해 필요한 소스만 "
    "읽으세요. source_load 결과에 next_offset이 있으면 남은 소스가 있다는 뜻이니 필요하면 "
    "offset으로 이어 읽으세요. 한도 안에서 모든 소스를 읽지 못했다면 답변에 읽은 범위를 "
    "밝히세요."
)


def _budget_exhausted_detail(budget: dict[str, Any]) -> str:
    """모델 본문 없이 1회 한도에 걸린 질문의 오류 문구.

    한도와 사용량은 코어가 소진 판정에 쓴 값(`_react_budget`, 코어 v0.26.0 #385)을 그대로 싣는다 —
    두 한도를 함께 넘기면 코어 사유는 토큰이다.
    """
    if budget.get("reason") == "token":
        return (
            f"채팅 1회 토큰 한도({int(budget['token_budget']):,})를 넘었습니다 — "
            f"{int(budget['used_tokens']):,}토큰 사용. "
            "소스를 골라 범위를 좁히거나 질문을 나눠 다시 시도해 주세요. "
            "한도는 관리자가 「관리 › 사용 제한」에서 바꿀 수 있습니다."
        )
    return (
        f"채팅 1회 비용 한도(${float(budget['cost_budget_usd']):.2f})를 넘었습니다 — "
        f"${float(budget['used_cost_usd']):.2f} 사용. "
        "소스를 골라 범위를 좁히거나 질문을 나눠 다시 시도해 주세요. "
        "한도는 관리자가 「관리 › 사용 제한」에서 바꿀 수 있습니다."
    )


def _run_citations(tool_evidence: Any) -> list[Citation]:
    """코어 run 근거표(`ctx.tool_evidence`의 `kind="source"` + `citation`)를 `Citation`으로 읽는다.

    `rag_search`가 `_record_run_citations`로 쌓은 항목만 해당한다 — `source_load`(full 모드)
    항목은 `citation`이 없어 건너뛴다. 번호는 run 안에서 유일하지만 방어적으로 첫 항목만 쓴다.
    """
    by_n: dict[int, Citation] = {}
    for item in tool_evidence if isinstance(tool_evidence, list) else ():
        if not isinstance(item, dict) or item.get("kind") != "source":
            continue
        raw = item.get("citation")
        if not isinstance(raw, dict):
            continue
        n = raw.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or n in by_n:
            continue
        source_id, chunk_id = raw.get("source_id"), raw.get("chunk_id")
        if source_id is None or chunk_id is None:
            continue
        by_n[n] = Citation(
            n=n,
            source_id=str(source_id),
            chunk_id=str(chunk_id),
            quote=str(raw.get("quote") or ""),
            source_title=str(raw.get("source_title") or ""),
        )
    return [by_n[n] for n in sorted(by_n)]


async def _autonomous_citations(
    db: AsyncSession, notebook_id: uuid.UUID, tool_evidence: Any, answer: str
) -> list[dict[str, Any]]:
    """자율 답변의 인용(#161) — 정적 경로와 같은 `filter_cited` → `verify_citations`에 run
    노트북 소속 확인(`filter_citations_to_notebook`, IDOR 방어)을 더한다.

    소속 확인을 검증보다 먼저 해서 다른 노트북 청크로 임베딩 검증을 돌리지 않는다.
    """
    cited = filter_cited(_run_citations(tool_evidence), answer)
    if not cited:
        return []
    allowed = await filter_citations_to_notebook(db, notebook_id, [c.to_dict() for c in cited])
    pairs = {(str(c.get("source_id")), str(c.get("chunk_id"))) for c in allowed}
    owned = [c for c in cited if (c.source_id, c.chunk_id) in pairs]
    if not owned:
        return []
    return [c.to_dict() for c in await verify_citations(db, owned, answer)]


def _sse_provenance(provenance: dict[str, Any] | None) -> dict[str, Any] | None:
    """저장 dict를 `ChatMessageOut.provenance`와 **같은 경로**로 직렬화한다(BL-22).

    `ChatMessageOut.provenance`의 타입은 `ProvenanceOut | None`이므로 REST 재조회 응답은
    `ProvenanceOut` 검증 + JSON 직렬화를 거친다. SSE `done`도 같은 모델을 거치게 해서
    라이브 페이로드와 재조회 결과가 **구조적으로** dict 동등이 되게 한다(AC-14).
    빌더가 실패해 provenance가 없으면 `null`을 보낸다 — 답변 전달이 우선이다.
    """
    if not provenance:
        return None
    try:
        return ProvenanceOut.model_validate(provenance).model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001 - 답변 전달은 provenance 직렬화보다 우선
        logger.warning("채팅 SSE provenance 직렬화 실패", error=str(exc))
        return None


@router.get("/models", response_model=list[LLMModelOut])
async def list_available_models(
    modality: Literal["llm", "image"] = Query(
        "llm", description="llm = 채팅 모델 선택, image = 슬라이드 삽화 등 이미지 생성 가능 여부"
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[LLMModelOut]:
    """사용자가 고를 수 있는 모델 목록(어드민이 enable한 모델). 기본은 채팅 LLM이다.

    `image`는 「이미지 넣기」를 켤 수 있는지 판단하는 용도다(core-v0.23.0-upgrade D4). 목록이 비어
    있지 않으면 코어 `resolve_model(modality="image")`가 기본 모델을 고를 수 있다.
    """
    models = await list_models(db, modality=modality)
    return [
        LLMModelOut(
            id=m.model.id,
            display_name=m.model.display_name,
            provider_name=m.provider.name,
            provider_type=cast(ProviderType, m.provider.type),
            is_default=m.model.is_default,
        )
        for m in models
    ]


async def _notebook_readable(db: AsyncSession, notebook_id: uuid.UUID, user: User) -> Notebook:
    nb = await db.get(Notebook, notebook_id)
    if nb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    perm = await get_permission(db, nb, user)
    if perm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notebook not found")
    return nb


@router.get("/notebooks/{notebook_id}/chat/sessions", response_model=list[ChatSessionOut])
async def list_sessions(
    notebook_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ChatSessionOut]:
    await _notebook_readable(db, notebook_id, user)
    rows = await db.execute(
        select(ChatSession)
        .where(ChatSession.notebook_id == notebook_id, ChatSession.user_id == user.id)
        .order_by(ChatSession.pinned.desc(), ChatSession.updated_at.desc())
    )
    return [ChatSessionOut.model_validate(s) for s in rows.scalars().all()]


# 권한을 다시 확인하며 걸러 내므로 limit보다 넉넉히 읽는다
# (공유 해제된 노트북의 대화가 섞여 있어도 채운다)
_RECENT_SCAN_FACTOR = 5


@router.get("/chat/recent", response_model=list[RecentChatOut])
async def list_recent_chats(
    limit: int = Query(3, ge=1, le=10),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RecentChatOut]:
    """홈 「최근 대화 이어가기」(ui-ux-improvements ⑥).

    내 대화 중 메시지가 있는 것을 최근 활동순으로 돌려준다. 지금 읽을 수 없는 노트북
    (공유 해제·비공개 전환)의 대화는 제목과 노트북 이름을 노출하지 않도록 뺀다 — 노트북
    권한은 항목마다 다시 판정한다.
    """
    has_messages = exists().where(ChatMessage.session_id == ChatSession.id)
    rows = await db.execute(
        select(ChatSession, Notebook)
        .join(Notebook, Notebook.id == ChatSession.notebook_id)
        .where(ChatSession.user_id == user.id, has_messages)
        .order_by(ChatSession.updated_at.desc())
        .limit(limit * _RECENT_SCAN_FACTOR)
    )
    out: list[RecentChatOut] = []
    for session, nb in rows.all():
        if await get_permission(db, nb, user) is None:
            continue
        out.append(
            RecentChatOut(
                session_id=session.id,
                notebook_id=nb.id,
                notebook_title=nb.title,
                notebook_emoji=nb.emoji,
                title=session.title,
                updated_at=session.updated_at,
            )
        )
        if len(out) >= limit:
            break
    return out


@router.post(
    "/notebooks/{notebook_id}/chat/sessions",
    response_model=ChatSessionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    notebook_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatSessionOut:
    await _notebook_readable(db, notebook_id, user)
    session = ChatSession(notebook_id=notebook_id, user_id=user.id)
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return ChatSessionOut.model_validate(session)


async def _get_session(db: AsyncSession, session_id: uuid.UUID, user: User) -> ChatSession:
    session = await db.get(ChatSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return session


@router.patch("/chat/sessions/{session_id}", response_model=ChatSessionOut)
async def update_session(
    session_id: uuid.UUID,
    body: ChatSessionUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatSessionOut:
    session = await _get_session(db, session_id, user)
    session.pinned = body.pinned
    await db.commit()
    await db.refresh(session)
    return ChatSessionOut.model_validate(session)


@router.get("/chat/sessions/{session_id}/messages", response_model=list[ChatMessageOut])
async def list_messages(
    session_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ChatMessageOut]:
    session = await _get_session(db, session_id, user)
    rows = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.seq)
    )
    entry_runs = await session_entry_runs(db, session.id)
    return [_message_out(m, entry_runs) for m in rows.scalars().all()]


def _message_out(message: ChatMessage, entry_runs: dict[uuid.UUID, EntryRunRef]) -> ChatMessageOut:
    """메시지 응답 — 진입 질문 행이면 run 요약(`entry_run`)을 붙인다(#66).

    결과 행의 `run_id`는 열 값 그대로다.
    """
    out = ChatMessageOut.model_validate(message)
    ref = entry_runs.get(message.id) if message.role == "user" else None
    if ref is not None:
        out.entry_run = ChatEntryRunRefOut.model_validate(asdict(ref))
    return out


def _entry_out(agent: Agent, entry: ChatEntry) -> ChatEntryOut:
    """코어 진입 판정을 팔레트 항목으로 직렬화한다 — 판정·표시값을 다시 계산하지 않는다.

    스킬 wrapper는 `ChatEntry.display_*`(실행 version 기준)를 쓴다. `source`는 허용 항목에 늘 있다 —
    없으면 응답 검증이 실패해 노출하지 않는다(fail-closed).
    """
    if agent.source_kind == "skill_package":
        name, description = entry.display_name or "", entry.display_description or ""
    else:
        name, description = agent.name, agent.description
    return ChatEntryOut.model_validate(
        {
            "agent_id": agent.id,
            "kind": "skill" if agent.source_kind == "skill_package" else "workflow",
            "slash_name": entry.slash_name,
            "source": entry.source,
            "display_name": name,
            "description": description,
        }
    )


@router.get("/chat/entries", response_model=list[ChatEntryOut])
async def list_chat_entries(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ChatEntryOut]:
    """`/` 팔레트 항목(#66) — 코어 `list_chat_entry_agents`가 허용한 항목만, 그 순서대로.

    본인 스킬·system·승인된 org 스킬과 채팅 진입 형태의 workflow다. 노트북과 무관하다. admin에게도
    타인 private 항목은 없다(코어 판정에 admin 승격이 없다).
    """
    return [_entry_out(agent, entry) for agent, entry in await list_chat_entry_agents(db, user)]


async def _entry_agent(db: AsyncSession, user: User, body: ChatEntryRunCreate) -> Agent:
    """진입 항목을 고른다. `name`은 코어 `resolve_chat_entry_by_name`으로 푼다(#410 D3).

    none·없는 `agent_id`는 404 하나다(판정 사유를 싣지 않는다). many는 실행하지 않고 409와 후보를
    돌려준다 — 후보는 목록과 같은 판정을 통과한 항목뿐이라 타인 private은 섞이지 않는다.
    """
    if body.name is not None:
        match = await resolve_chat_entry_by_name(db, user, body.name)
        if match.status == "many":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                ChatEntryAmbiguousOut(
                    code="chat_entry_ambiguous",
                    candidates=[_entry_out(agent, entry) for agent, entry in match.matches],
                ).model_dump(mode="json"),
            )
        if match.status == "one":
            return match.matches[0][0]
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    agent = await db.get(Agent, body.agent_id) if body.agent_id is not None else None
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    return agent


@router.post(
    "/chat/sessions/{session_id}/entry-runs",
    response_model=ChatEntryRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={status.HTTP_409_CONFLICT: {"model": ChatEntryConflictOut}},
    dependencies=[
        Depends(RateLimiter("agent-run", scope="user", limit_setting="rate_limit_agent_run"))
    ],
)
async def create_entry_run(
    session_id: uuid.UUID,
    body: ChatEntryRunCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatEntryRunOut:
    """채팅 `/` 진입 실행(#66 D1) — 질문 행과 `trigger="chat"` run을 한 커밋으로 만들고 202를 준다.

    판정 순서는 다음과 같다.
    1. 세션 본인(아니면 404).
    2. 노트북 read(아니면 404). 읽기 공유 사용자도 실행한다(결정 2).
    3. 항목 결정(`agent_id`·`name`) → 코어 관문 `assert_chat_entry_allowed`(404, 존재 은닉).
    4. 스테이징.
    진행은 `GET /agent-runs/{id}` 폴링으로 보고, 결과는 워커가 assistant 행(`run_id`)으로 쓴다.

    세션당 활성 run은 1개다(코어 `uq_agent_runs_chat_active` → `ActiveChatRunExists` 409). 같은
    `idempotency_key`로 다시 보내면 새로 만들지 않고 그 run과 질문 행을 돌려준다(202).
    한도·파라미터·모델 오류(429·422)는 전역 핸들러가 맡는다.
    """
    session = await _get_session(db, session_id, user)
    await _notebook_readable(db, session.notebook_id, user)
    agent = await _entry_agent(db, user, body)
    entry = await assert_chat_entry_allowed(db, user, agent)
    # 채팅 본문은 코어 예약 이름공간으로만 들어간다 — 스킬은 `param_key`, workflow는
    # `{chat_message}`(워커가 `RunContext.chat_message`로 싣는다). 정의 템플릿에 잇지 않는다
    params = (
        chat_entry_params(entry, body.content) if agent.source_kind == "skill_package" else {}
    )

    # 충돌 복구는 롤백 뒤라 ORM 객체가 만료된다 — 필요한 값을 먼저 고정한다(코어 D-10b)
    sid, notebook_id = session.id, session.notebook_id
    user_id, user_role = user.id, user.role
    agent_name, agent_icon = agent.name, agent.icon
    first_message = not await db.scalar(
        select(exists().where(ChatMessage.session_id == sid))
    )
    from biznotes_core.services.usage_guard import record_usage

    try:
        async with publishing_unit_of_work(db) as uow:
            message = ChatMessage(session_id=sid, role="user", content=body.content)
            db.add(message)
            await db.flush()
            run = await stage_agent_run(
                db,
                uow,
                agent=agent,
                notebook_id=notebook_id,
                user_id=user_id,
                user_role=user_role,
                params=params,
                trigger="chat",
                source_ids=body.source_ids,
                chat_session_id=sid,
                prompt_message_id=message.id,
                idempotency_key=body.idempotency_key,
                model_id=body.model_id,
            )
            if first_message:
                session.title = body.content[:60]
            session.updated_at = datetime.now(UTC)
    except IntegrityError as exc:
        # 활성 run이 있으면 `ActiveChatRunExists`(409), 같은 키의 run이 있으면 그 run이다
        run = await recover_chat_run_conflict(
            db, exc, user_id=user_id, chat_session_id=sid, idempotency_key=body.idempotency_key
        )
        recovered = (
            await db.get(ChatMessage, run.prompt_message_id)
            if run.prompt_message_id is not None
            else None
        )
        if recovered is None:
            raise ActiveChatRunExists() from exc
        message = recovered
    else:
        await db.refresh(run)
        await db.refresh(message)
        await record_usage(user_id, "agent_runs", 1)

    run_out = AgentRunOut.model_validate(run)
    run_out.agent_name = agent_name
    run_out.agent_icon = agent_icon
    run_out.can_cancel = True  # 실행 소유자 본인
    return ChatEntryRunOut(
        user_message=_message_out(message, await session_entry_runs(db, sid)), run=run_out
    )


@router.post(
    "/chat/sessions/{session_id}/messages",
    dependencies=[Depends(RateLimiter("chat", scope="user", limit_setting="rate_limit_chat"))],
)
async def send_message(
    session_id: uuid.UUID,
    body: SendMessageRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """SSE 스트리밍: retrieval → delta* → done."""
    session = await _get_session(db, session_id, user)
    await _notebook_readable(db, session.notebook_id, user)
    # 진입 run이 끝나기 전에는 일반 메시지를 받지 않는다(#66 D3) — 결과가 나중 메시지 뒤에 끼지
    # 않게 대화 순서를 지킨다. 코어도 세션당 활성 run을 1개로 막는다(같은 409 문구)
    if await has_active_entry_run(db, session.id):
        raise ActiveChatRunExists()

    # 일일 토큰 한도 — 이미 초과한 사용자는 스트림 시작 전에 차단.
    # UsageLimitExceeded(→429)·GatewayError(→502)는 전역 예외 핸들러가 매핑한다(R1-1).
    from biznotes_core.services.usage_guard import check_usage, record_usage

    await check_usage(db, user.id, user.role, "tokens")
    resolved = await resolve_model(db, body.model_id)

    # 이력 로드 (스트림 시작 전)
    history_rows = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.seq)
    )
    history = list(history_rows.scalars().all())
    # 진입 턴(질문 행 → run) — 이력 규칙과 다시 생성 가드가 쓴다(#66)
    entry_runs = await session_entry_runs(db, session.id)
    entry_prompts = {prompt_id: ref.run_id for prompt_id, ref in entry_runs.items()}

    # 다시 생성: 마지막 질문·답변 한 쌍을 바꿀 대상으로 떼어 두고, LLM 이력에서 뺀다.
    # 새 답변을 저장하는 커밋에서 함께 지우므로, 실패·중지하면 이전 쌍이 그대로 남는다.
    replaced: list[ChatMessage] = []
    if body.regenerate:
        last_turn = history[-2:]
        if (
            len(last_turn) != 2
            or last_turn[0].role != "user"
            or last_turn[1].role != "assistant"
            or last_turn[0].content != body.content
        ):
            raise HTTPException(status.HTTP_409_CONFLICT, "다시 생성할 마지막 답변이 없습니다")
        # 진입 턴의 결과는 run이 만든 것이라 일반 답변으로 다시 만들지 않는다(#66 §3 이력)
        if last_turn[1].run_id is not None or last_turn[0].id in entry_prompts:
            raise HTTPException(status.HTTP_409_CONFLICT, "실행 결과는 다시 생성할 수 없습니다")
        replaced = last_turn
        history = history[:-2]

    # 마인드맵 질문은 이전 대화 없이 run 소스 검색만으로 답한다(이번 턴 이력 없음). 세션 제목 판정은
    # 저장 이력(history) 기준 그대로다. 표식 붙은 질문을 다시 생성하면 요청의 origin과 무관하게
    # 마인드맵 질문으로 다룬다 — 다시 생성이 라벨을 표식 없는 user 메시지로 바꿔 저장하지 않게 한다.
    mindmap = body.origin == "mindmap" or (bool(replaced) and is_mindmap_prompt(replaced[0]))
    # 이력 길이·출력 토큰·1회 한도는 관리자가 정한다(「사용 제한」, #228) — 요청마다 읽어 저장 즉시
    # 다음 질문부터 적용된다. 이력 0은 이력 없이 답한다(`history[-0:]`은 전체라 따로 비운다).
    limits = await get_limit_settings(db)
    history_limit = int(limits["chat_history_messages"])
    llm_history = (
        []
        if mindmap or not history_limit
        else _history_for_llm(history, entry_prompts)[-history_limit:]
    )

    sse = format_sse  # SSE 데이터 프레임 포맷터 (R1-4 공용 헬퍼)
    # 답변 메시지 id를 먼저 정한다 — 원장의 `chat_message_id`가 저장될 답변을 가리킨다(#366)
    assistant_id = uuid.uuid4()

    # GitHub 저장소 스킬 설치 요청(#259) — 이번 사용자 글만 보고 LLM 없이 확인 카드를 띄운다.
    # 이력·소스·도구 결과·모델 출력은 감지 입력이 아니다(프롬프트 인젝션 차단).
    # 설치는 카드 버튼만 한다.
    install = None if mindmap else detect_github_skill_install(body.content)
    if install is not None:
        return sse_response(
            install_offer_stream(
                db,
                session=session,
                user=user,
                content=body.content,
                detection=install,
                history=history,
                replaced=replaced,
                assistant_id=assistant_id,
            )
        )

    async def stream():
        # 중지(취소) 시 사용량 추정용 — 제공자 usage는 스트림이 끝나야 채워진다
        # (정확한 값은 코어 biznotes-core#333)
        prompt_messages: list[dict[str, str]] = []
        answer_parts: list[str] = []
        result = StreamResult()
        usage_recorded = False
        try:
            # 1. 검색
            retrieval = await retrieve_context(
                db, session.notebook_id, body.content, body.source_ids
            )
            citations = retrieval.citations
            yield sse(
                {
                    "type": "retrieval",
                    "citations": [c.to_dict() for c in citations],
                    "insufficient_context": retrieval.insufficient_context,
                }
            )

            # 2. LLM 스트리밍 — 폴백 체인 + 서킷 브레이커 적용
            messages = build_messages(
                body.content,
                citations,
                llm_history,
                # 코어 기본(6)으로 다시 자르지 않게 이미 자른 길이를 넘긴다
                max_history=history_limit,
                insufficient_context=retrieval.insufficient_context,
            )
            prompt_messages = messages
            gen = await complete_stream_with_fallback(
                db, body.model_id, messages, result, max_tokens=int(limits["chat_max_tokens"])
            )
            async for delta in gen:
                answer_parts.append(delta)
                yield sse({"type": "delta", "text": delta})

            answer = "".join(answer_parts)
            final_citations = await verify_citations(db, filter_cited(citations, answer), answer)
            used = result.model or resolved
            if result.fallback_used:
                result.usage["fallback"] = 1  # 비용 추적: 폴백 발생 플래그
            if retrieval.insufficient_context:
                # [codex D3] 신규 컬럼/마이그레이션 없이 기존 token_usage(JSONB)에 실어
                # 완료·재로드된 대화에서도 배너를 복원할 수 있게 한다(fallback 플래그와 동일 패턴).
                result.usage["insufficient_context"] = 1

            # 3. 저장
            try:
                provenance = build_chat_provenance(
                    tool_calls=[],
                    tool_evidence=[],
                    citations=[c.to_dict() for c in final_citations],
                    mode="static",
                )
            except Exception as exc:  # noqa: BLE001 - 답변 저장은 provenance보다 우선
                provenance = None
                logger.warning("채팅 provenance 생성 실패", mode="static", error=str(exc))
            user_msg = ChatMessage(
                session_id=session.id,
                role="user",
                content=body.content,
                # 신규 컬럼 없이 기존 token_usage(JSONB)에 출처 표식을 싣는다
                # (insufficient_context와 같은 패턴)
                token_usage={MINDMAP_PROMPT_MARK: 1} if mindmap else {},
            )
            assistant_msg = ChatMessage(
                id=assistant_id,
                session_id=session.id,
                role="assistant",
                content=answer,
                citations=[c.to_dict() for c in final_citations],
                model_id=used.model.id,
                token_usage=result.usage,
                provenance=provenance,
            )
            for old in replaced:
                await db.delete(old)
            db.add(user_msg)
            db.add(assistant_msg)
            # 첫 메시지면 세션 제목 갱신
            if not history:
                session.title = body.content[:60]
            # 최근 활동순 정렬을 위해 updated_at 갱신
            session.updated_at = datetime.now(UTC)
            await db.commit()

            yield sse(
                {
                    "type": "done",
                    "message_id": str(assistant_msg.id),
                    "citations": [c.to_dict() for c in final_citations],
                    "token_usage": result.usage,
                    "model": used.model.display_name,
                    "fallback_used": result.fallback_used,
                    "insufficient_context": retrieval.insufficient_context,
                    # BL-22: 라이브 배지의 단일 진실 공급원 — 저장된 provenance와 같은 값.
                    "provenance": _sse_provenance(provenance),
                }
            )

            usage_recorded = True
            await record_usage(user.id, "tokens", result.usage.get("total_tokens", 0))
        except asyncio.CancelledError:
            if not usage_recorded and prompt_messages:
                total = result.usage.get("total_tokens", 0)
                estimated = total == 0
                if estimated:
                    from biznotes_core.services.ingestion.chunker import count_tokens

                    total = count_tokens(
                        "\n".join(str(m.get("content", "")) for m in prompt_messages)
                    ) + count_tokens("".join(answer_parts))
                await _record_stopped_usage(user.id, total, estimated=estimated)
            raise
        except RerankUnavailableError as exc:
            # 리랭크가 켜진 검색이 리랭커를 해석·호출하지 못했다(코어 v0.15.0 #209 — 하이브리드
            # 폴백 없음). 예외 문자열은 리랭커 서버가 돌려준 값을 담을 수 있는 비신뢰 데이터라
            # 사용자에게는 고정 문구, 로그에는 예외 타입만 남긴다(질의·모델명·URL 금지 — PR-01)
            logger.warning("chat_rerank_unavailable", error_type=type(exc).__name__)
            yield sse({"type": "error", "detail": RERANK_UNAVAILABLE_DETAIL})
        except GatewayError as e:
            # 모델 호출 실패(`GatewayCallError`)는 모델명·공급자 응답 원문을 담는다 — 고정 문구만
            # 보낸다. 모델 해석·입력 검증(`GatewayError` 본체)은 작성자가 고칠 안내라 원문 그대로다
            # (코어 #216과 같은 구분, core-v0.20.0-upgrade §5.5 ②·HCI-8)
            yield sse({"type": "error", "detail": describe_node_failure(e).message})
        except Exception as e:  # 스트림 중 오류는 SSE로 전달
            import structlog

            structlog.get_logger().exception("chat stream failed")
            yield sse({"type": "error", "detail": f"내부 오류: {e}"})

    async def autonomous_stream():
        """자율 모드(Phase 3, Q1=옵션 B): react_agent 노드로 라우팅.

        정적 사전 검색 대신 LLM이 명시적 chat-safe allowlist(`CHAT_SAFE_TOOLS`) 내에서
        소스 원문·승인된 MCP·published system skill을 선택해 목표를 달성한다.
        예산/반복/fan-out 가드는 Phase 2 `_exec_react_agent`가 강제한다.
        토큰 단위 스트리밍은 제공하지 않고(최소 노출), 결과 텍스트를 단일 delta로 전달한다.
        """
        # 중지(취소) 시 그때까지의 사용량 — RunContext.usage는 LLM 호출마다 누적된다
        run_usage: dict[str, int] = {}
        usage_recorded = False
        try:
            from biznotes_core.services.agents.react.loop import BUDGET_HIT_KEY, BUDGET_INFO_KEY
            from biznotes_core.services.agents.runtime import RunContext, run_react_node
            from biznotes_core.services.agents.tool_registry import (
                CHAT_SAFE_TOOLS,
                assert_no_side_effect_tools,
            )

            # 자율 모드는 도구로 컨텍스트를 발견하므로 사전 검색 인용은 비운다.
            yield sse({"type": "retrieval", "citations": []})

            # 채팅 본문은 임의 사용자 텍스트라 react goal 템플릿 렌더링을 거치면 안 된다.
            # render는 단일 패스라, goal을 `{params.chat_message}`로 두고 실제 내용을
            # params로 주입하면 치환 결과(내용의 중괄호 포함)는 재렌더되지 않는다(codex D5).
            chat_params = {"chat_message": body.content}
            ctx = RunContext(
                run_id=uuid.uuid4(),
                notebook_id=session.notebook_id,
                db=db,
                params=chat_params,
                user_id=user.id,
                user_email=user.email,
                user_name=user.name,
                user_role=user.role,
                source_ids=body.source_ids,
            )
            run_usage = ctx.usage
            # [R5-4] 하드코딩 리터럴 대신 명시적 chat-safe allowlist 상수 사용.
            # side-effect 필터 파생 금지(HCI-R5-4) — web_search(외부 웹)·call_agent
            # (한도·감사 우회) 재허용 방지. assert_no_side_effect_tools는 부작용 도구
            # 혼입을 막는 보조 방어(allowlist 출처는 CHAT_SAFE_TOOLS 명시 열거).
            assert_no_side_effect_tools(CHAT_SAFE_TOOLS)
            node: dict[str, Any] = {
                "id": "react",
                "type": "react_agent",
                "goal": "{params.chat_message}",
                "system": (
                    "도구 선택 규칙: 소스에 답이 있을 때는 rag_search 또는 source_load를 "
                    "먼저 사용하고, 실제 호출하지 않은 도구나 근거를 사용했다고 말하지 마세요.\n"
                    f"{AUTONOMOUS_SOURCE_RULE}\n"
                    # 인용 규칙(#161) — rag_search 발췌를 근거로 쓸 때 `[n]`을 단다
                    f"인용 규칙: {AUTONOMOUS_CITATION_RULE} {AUTONOMOUS_CITATION_SCOPE}"
                ),
                "allowed_tools": list(CHAT_SAFE_TOOLS),
                # 1회 한도는 관리자 값이다. 비용 한도도 명시해 오류 문구에 실제 한도를 싣는다.
                "token_budget": limits["chat_token_budget"],
                "cost_budget_usd": limits["chat_cost_budget_usd"],
                "max_iters": limits["chat_max_iters"],
                "max_tokens": limits["chat_max_tokens"],
                # source_load 1회 상한과 남은 토큰 한도 맞춤(코어 v0.26.0 #389, D3)
                "source_load_max_sources": limits["source_load_max_sources"],
                "source_load_max_chars": limits["source_load_max_chars"],
                "source_load_budget_ratio": limits["source_load_budget_ratio"],
            }
            if body.model_id:
                node["model_id"] = str(body.model_id)
            # 후속 턴 맥락 유지 — 정적 경로의 build_messages(..., history)와 동등하게
            # 최근 대화 이력을 react 프롬프트에 주입한다(codex D2).
            # 표식 붙은 마인드맵 질문은 고정 문구로 바뀐다(_history_for_llm).
            if llm_history:
                node["history"] = [{"role": m.role, "content": m.content} for m in llm_history]

            # execute_agent는 500자 요약만 반환하므로 전체 텍스트를 보존하는
            # run_react_node를 직접 호출한다(codex D1). goal 템플릿 치환용 params를 state로 전달.
            result = await run_react_node(ctx, node, {"params": chat_params})
            answer = result.get("text", "")
            budget = result.get(BUDGET_INFO_KEY)
            if result.get(BUDGET_HIT_KEY) and budget and not budget.get("has_content"):
                # 모델이 본문을 내기 전에 1회 한도를 다 썼다. 코어는 안내 문구를 답변 자리에 넣지만
                # 답변이 아니므로 저장하지 않고 어느 한도를 넘었는지 오류로 알린다. 본문 유무는
                # 문구가 아니라 `has_content`로 본다(코어 v0.26.0 #385). 쓴 토큰은 일일 한도에
                # 넣는다(중지와 같다 — 오류로 한도를 우회하지 않는다). 기록 중 연결이 끊겨도
                # 기록이 끝나도록 shield한다 — 아래 취소 처리는 `usage_recorded`를 보고 다시
                # 기록하지 않는다.
                usage_recorded = True
                await _record_usage_shielded(user.id, ctx.usage.get("total_tokens", 0))
                yield sse({"type": "error", "detail": _budget_exhausted_detail(budget)})
                return
            yield sse({"type": "delta", "text": answer})

            # 실제 답변을 만든 모델/폴백을 보고한다 — 루프 소진 마무리가 폴백 모델로
            # 넘어간 경우에도 메타데이터가 일치하도록(codex D6). model은 직렬화 가능한
            # dict({id, display_name}) 또는 None(codex D8). 미관측 시 요청 모델로 폴백.
            react_model = result.get("model")
            if react_model:
                used_model_id = uuid.UUID(react_model["id"])
                used_model_name = react_model["display_name"]
            else:
                used_model_id = resolved.model.id
                used_model_name = resolved.model.display_name
            fallback_used = bool(result.get("fallback_used"))

            usage = dict(ctx.usage)
            # 코어 근거표에서 답변이 인용한 [n]만 검증·소속 확인 뒤 싣는다(#161).
            # 인용 단계는 같은 세션으로 조회한다 — DB 오류가 나면 트랜잭션이 깨져 아래 답변 저장까지
            # 실패하므로 SAVEPOINT로 감싸 그 단계만 되돌린다. 대기 중인 변경은 먼저 flush해
            # 세이브포인트 롤백에 휩쓸리지 않게 한다(현재는 이 시점에 대기 변경이 없다).
            await db.flush()
            try:
                async with db.begin_nested():
                    citations = await _autonomous_citations(
                        db, session.notebook_id, ctx.tool_evidence, answer
                    )
            except Exception as exc:  # noqa: BLE001 - 답변(이미 delta로 보냄) 저장이 인용보다 우선
                citations = []
                logger.warning("자율 채팅 인용 생성 실패", error_type=type(exc).__name__)
            try:
                provenance = build_chat_provenance(
                    ctx.tool_calls,
                    ctx.tool_evidence,
                    citations=[],
                    mode="autonomous",
                )
            except Exception as exc:  # noqa: BLE001 - 답변 저장은 provenance보다 우선
                provenance = None
                logger.warning("채팅 provenance 생성 실패", mode="autonomous", error=str(exc))
            user_msg = ChatMessage(session_id=session.id, role="user", content=body.content)
            assistant_msg = ChatMessage(
                id=assistant_id,
                session_id=session.id,
                role="assistant",
                content=answer,
                citations=citations,
                model_id=used_model_id,
                token_usage=usage,
                provenance=provenance,
            )
            for old in replaced:
                await db.delete(old)
            db.add(user_msg)
            db.add(assistant_msg)
            if not history:
                session.title = body.content[:60]
            session.updated_at = datetime.now(UTC)
            await db.commit()

            yield sse(
                {
                    "type": "done",
                    "message_id": str(assistant_msg.id),
                    "citations": citations,
                    "token_usage": usage,
                    "model": used_model_name,
                    "fallback_used": fallback_used,
                    "autonomous": True,
                    # BL-22: 라이브 배지의 단일 진실 공급원 — 저장된 provenance와 같은 값.
                    "provenance": _sse_provenance(provenance),
                    # HCI-P2-5: deprecated — 1 릴리스 동안 유지(BL-41).
                    "tool_calls": ctx.tool_calls,
                    "evidence": public_evidence_items(ctx.tool_evidence),
                }
            )

            usage_recorded = True
            await record_usage(user.id, "tokens", usage.get("total_tokens", 0))
        except asyncio.CancelledError:
            if not usage_recorded:
                await _record_stopped_usage(
                    user.id, run_usage.get("total_tokens", 0), estimated=False
                )
            raise
        except GatewayError as e:
            # 정적 경로와 같다 — `GatewayCallError`는 고정 문구, `GatewayError` 본체는 원문(§5.5 ②)
            yield sse({"type": "error", "detail": describe_node_failure(e).message})
        except Exception as e:  # 스트림 중 오류는 SSE로 전달
            logger.exception("autonomous chat stream failed")
            yield sse({"type": "error", "detail": f"내부 오류: {e}"})

    # 경로 선택을 클라이언트 값에만 맡기지 않는다 — 마인드맵 질문은 언제나 정적 경로(도구 없음)다.
    return sse_response(autonomous_stream() if body.autonomous and not mindmap else stream())
