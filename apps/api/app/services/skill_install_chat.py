"""채팅의 GitHub 스킬 설치 제안 스트림(#259 PR-2, 설계 노트 §5 PR-2 단계 3).

`send_message`가 이번 사용자 글에서 설치 요청을 감지하면 LLM 대신 이 스트림을 돈다:
`retrieval`(빈 인용) → `skill_install_offer` → `delta`(요약 문장) → user·assistant 저장 → `done`.
스킬은 만들지 않는다 — 설치는 카드 버튼이 부르는 `POST /skill-installs/github`만 한다.
일일 토큰을 쓰지 않는다(모델 호출 없음). 실패는 고정 문구 `error`이고 아무것도 저장하지 않는다.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import structlog
from biznotes_core.models import ChatMessage, ChatSession, User
from biznotes_core.services.events import format_sse
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.skill_install import SkillInstallOfferEvent
from app.services.skills.github_install import (
    GithubInstallError,
    InstallDetection,
    build_offer,
    offer_summary,
)

logger = structlog.get_logger()

OFFER_FAILED_DETAIL = "설치 제안을 만들지 못했습니다. 잠시 뒤 다시 시도해 주세요."


async def install_offer_stream(
    db: AsyncSession,
    *,
    session: ChatSession,
    user: User,
    content: str,
    detection: InstallDetection,
    history: list[ChatMessage],
    replaced: list[ChatMessage],
    assistant_id: uuid.UUID,
) -> AsyncGenerator[str, None]:
    """설치 제안(또는 고정 안내) SSE. `replaced`는 다시 생성으로 바꿀 마지막 질문·답변 쌍이다."""
    try:
        yield format_sse({"type": "retrieval", "citations": []})
        if detection.target is not None:
            offer = await build_offer(db, user, detection.target)
            event = SkillInstallOfferEvent(type="skill_install_offer", offer=offer)
            yield format_sse(event.model_dump(mode="json"))
            answer = offer_summary(offer)
        else:
            answer = detection.guidance or ""
        yield format_sse({"type": "delta", "text": answer})

        user_msg = ChatMessage(session_id=session.id, role="user", content=content)
        assistant_msg = ChatMessage(
            id=assistant_id,
            session_id=session.id,
            role="assistant",
            content=answer,
            citations=[],
            model_id=None,
            token_usage={},
        )
        for old in replaced:
            await db.delete(old)
        db.add(user_msg)
        db.add(assistant_msg)
        if not history:
            session.title = content[:60]
        session.updated_at = datetime.now(UTC)
        await db.commit()
        yield format_sse(
            {
                "type": "done",
                "message_id": str(assistant_msg.id),
                "citations": [],
                "token_usage": {},
                "model": "",
                "fallback_used": False,
                "insufficient_context": False,
                "provenance": None,
            }
        )
    except GithubInstallError as e:
        logger.info("skill_install_offer_failed", code=e.code)
        yield format_sse({"type": "error", "detail": e.message})
    except Exception:  # noqa: BLE001 — 스트림 중 오류는 고정 문구 SSE로 전달
        logger.exception("skill_install_offer_stream_failed")
        yield format_sse({"type": "error", "detail": OFFER_FAILED_DETAIL})
