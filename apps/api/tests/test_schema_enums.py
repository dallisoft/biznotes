"""R3b(R4-3a): 응답 스키마 `Literal` 좁힘의 회귀 테스트.

핵심 불변식: **응답 스키마는 정본 튜플의 모든 값을 직렬화할 수 있어야 한다.**
정본 밖 값으로 좁히면 실제로 기록되는 값이 `AgentRunOut.model_validate()`에서
`ValidationError` → HTTP 500이 된다.
"""

from __future__ import annotations

import datetime
import uuid
from typing import get_args

import pytest
from biznotes_core.models.agent import TRIGGER_SOURCES
from biznotes_core.models.source import SOURCE_TYPES
from biznotes_core.schemas import enums
from pydantic import ValidationError

from app.schemas.agent import AgentRunOut


def _run(**overrides: object) -> dict[str, object]:
    now = datetime.datetime.now(datetime.UTC)
    return {
        "id": uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "notebook_id": uuid.uuid4(),
        "params": {},
        "trigger": "manual",
        "status": "succeeded",
        "progress": 100,
        "result_summary": None,
        "error": None,
        "artifact_keys": [],
        "token_usage": {},
        "started_at": now,
        "finished_at": now,
        "created_at": now,
        "updated_at": now,
        **overrides,
    }


@pytest.mark.parametrize("trigger", TRIGGER_SOURCES)
def test_agent_run_out_accepts_every_canonical_trigger(trigger: str) -> None:
    """정본 TRIGGER_SOURCES 전 값(수동·채팅)이 직렬화돼야 한다."""
    assert AgentRunOut(**_run(trigger=trigger)).trigger == trigger


def test_agent_run_out_rejects_unknown_trigger() -> None:
    """좁힘이 실제로 동작하는지 — 정본 밖 값은 거부돼야 한다."""
    with pytest.raises(ValidationError):
        AgentRunOut(**_run(trigger="definitely-not-a-trigger"))


def test_source_type_alias_matches_canonical_tuple() -> None:
    """`SourceOut.type`은 모델 정본 튜플과 같다(파일·웹·텍스트)."""
    alias_values = set(get_args(enums.SourceType))
    assert alias_values == set(SOURCE_TYPES) == {"file", "web", "text"}


def test_every_alias_matches_its_canonical_tuple() -> None:
    """별칭 ↔ 정본 튜플 전건 일치.

    `app/schemas/enums.py`는 임포트 시점에도 검사하지만, 그 검사가 지워지거나
    약해지는 회귀를 막기 위해 테스트로도 고정한다.
    """
    for name, alias, canonical in enums._BINDINGS:
        assert set(get_args(alias)) == set(canonical), name


# --- R4-3b: 구조축 좁힘(list[Any] → list[CitationOut]) 회귀 고정 -------------
#
# `citations`/`token_usage`를 좁히면 생성 타입이 `unknown[]`을 벗어나 프론트가
# `Citation[]`을 되찾는다. 단 **과거 JSONB 행**이 새 스키마를 통과해야 한다 —
# 통과 못 하면 옛 메시지 조회가 ValidationError → HTTP 500이다.
# 실측(2026-07-10): chat_messages 인용 126건 중 24건, notes 16건 중 3건에
# `verified` 키가 없다.


def test_citation_out_accepts_legacy_row_without_verified() -> None:
    from app.schemas.chat import CitationOut

    legacy = {
        "n": 1,
        "source_id": "s",
        "chunk_id": "c",
        "quote": "q",
        "source_title": "t",
    }
    assert CitationOut.model_validate(legacy).verified is True
    assert CitationOut.model_validate({**legacy, "verified": False}).verified is False


def test_chat_message_out_accepts_legacy_and_flag_bearing_token_usage() -> None:
    import uuid
    from datetime import datetime

    from app.schemas.chat import ChatMessageOut

    msg = ChatMessageOut.model_validate(
        {
            "id": uuid.uuid4(),
            "session_id": uuid.uuid4(),
            "role": "assistant",
            "content": "x",
            # verified 없는 과거 행 + 있는 새 행이 섞여 있다
            "citations": [
                {"n": 1, "source_id": "s", "chunk_id": "c", "quote": "q", "source_title": "t"},
                {
                    "n": 2,
                    "source_id": "s2",
                    "chunk_id": "c2",
                    "quote": "q2",
                    "source_title": "t2",
                    "verified": False,
                },
            ],
            "model_id": None,
            # insufficient_context 플래그도 token_usage(JSONB)에 정수로 실린다
            "token_usage": {"prompt_tokens": 1, "total_tokens": 2, "insufficient_context": 1},
            "created_at": datetime.now(),
        }
    )
    assert len(msg.citations) == 2
    assert msg.token_usage["insufficient_context"] == 1


def test_note_out_accepts_legacy_citation_row() -> None:
    import uuid
    from datetime import datetime

    from app.schemas.note import NoteOut

    note = NoteOut.model_validate(
        {
            "id": uuid.uuid4(),
            "notebook_id": uuid.uuid4(),
            "author_id": uuid.uuid4(),
            "title": "t",
            "content": "c",
            "origin": "chat",
            "origin_ref": None,
            "citations": [
                {"n": 1, "source_id": "s", "chunk_id": "c", "quote": "q", "source_title": "t"}
            ],
            "created_at": datetime.now(),
            "updated_at": datetime.now(),
        }
    )
    assert note.citations[0].verified is True


# --- R4CR-02 (medium, codex 교차 리뷰) ----------------------------------------
#
# `AgentRunOut.artifact_keys`도 JSONB를 그대로 읽는다. 표시용 메타(name/content_type)를
# 필수로 두면 체크포인트 재개·과거 행에 필드가 빠졌을 때 **실행 목록 전체가 500**이 된다.
# key만 필수로 두고 나머지는 기본값으로 degrade시킨다.


def test_artifact_ref_out_degrades_on_partial_row() -> None:
    from app.schemas.agent import ArtifactRefOut

    full = {"key": "k", "name": "n", "content_type": "text/plain"}
    assert ArtifactRefOut.model_validate(full).name == "n"

    # 표시용 메타가 없어도 목록을 잃지 않는다.
    partial = ArtifactRefOut.model_validate({"key": "k"})
    assert partial.key == "k"
    assert partial.name == ""
    assert partial.content_type == ""


def test_artifact_ref_out_still_requires_key() -> None:
    """key가 없으면 아티팩트를 가리킬 수 없으므로 거부한다."""
    import pytest
    from pydantic import ValidationError

    from app.schemas.agent import ArtifactRefOut

    with pytest.raises(ValidationError):
        ArtifactRefOut.model_validate({"name": "n", "content_type": "text/plain"})
