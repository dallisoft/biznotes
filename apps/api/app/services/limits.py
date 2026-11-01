"""관리자가 정하는 사용 제한(관리 › 사용 제한) — 코어 `system_settings`의 `limits` 키에 둔다.

코어 `settings_store.DEFAULTS`에는 이 키가 없어 기본값은 여기서 채운다. HITL 승인 대기 시간만은
코어가 정의한 `agents` 키(`approval_timeout_minutes`)에 그대로 둔다 — 워커(`tasks/agent.py`)와 옛
이미지가 그 키를 읽으므로, 다른 키로 옮기면 롤백 때 값이 갈린다.
"""

import math
from typing import Any

from biznotes_core.models import SystemSetting
from biznotes_core.services.agents.nodes.llm import (
    SOURCE_LOAD_TOOL_DEFAULTS,
    SOURCE_LOAD_TOOL_HARD_CAPS,
)
from biznotes_core.services.agents.react.loop import REACT_DEFAULTS, REACT_HARD_CAPS
from biznotes_core.services.settings_store import DEFAULTS as SETTING_DEFAULTS
from biznotes_core.services.settings_store import get_setting
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

LIMITS_SETTING_KEY = "limits"
AGENTS_SETTING_KEY = "agents"

# 자율 채팅 질문 1건의 토큰·비용 상한과 도구 반복 수(react_agent `token_budget`·`cost_budget_usd`·
# `max_iters`). 기본값·상한은 코어 공개 상수 `REACT_DEFAULTS`·`REACT_HARD_CAPS`(코어 v0.26.0 #385)를
# 따른다 — 상한을 넘기면 코어가 조용히 깎아 화면 값과 어긋난다.
CHAT_TOKEN_BUDGET_DEFAULT = int(REACT_DEFAULTS["token_budget"])
CHAT_TOKEN_BUDGET_MIN = 10_000
CHAT_TOKEN_BUDGET_MAX = int(REACT_HARD_CAPS["token_budget"])
CHAT_COST_BUDGET_USD_DEFAULT = float(REACT_DEFAULTS["cost_budget_usd"])
CHAT_COST_BUDGET_USD_MIN = 0.05
CHAT_COST_BUDGET_USD_MAX = float(REACT_HARD_CAPS["cost_budget_usd"])
CHAT_MAX_ITERS_DEFAULT = int(REACT_DEFAULTS["max_iters"])
CHAT_MAX_ITERS_MIN = 1
CHAT_MAX_ITERS_MAX = int(REACT_HARD_CAPS["max_iters"])
# 턴당 출력 토큰(`max_tokens`) — 기본값은 코어 react 루프의 기본값(4096)이다. 코어에는 이 값의 공개
# 상수도 하드 상한도 없어 그대로 제공자에 넘긴다. 상한은 흔한 모델의 출력 한도(16k) 안으로 둔다.
CHAT_MAX_TOKENS_DEFAULT = 4096
CHAT_MAX_TOKENS_MIN = 512
CHAT_MAX_TOKENS_MAX = 16_384
# 모델에 넘기는 최근 대화 메시지 수 — 기본값은 코어 `build_messages`의 기본값(6)과 같다. 0이면 이력
# 없이 답한다. 메시지마다 4,000자까지 넣으므로 크게 두면 토큰 한도를 먼저 쓴다.
CHAT_HISTORY_MESSAGES_DEFAULT = 6
CHAT_HISTORY_MESSAGES_MIN = 0
CHAT_HISTORY_MESSAGES_MAX = 20
# 자율 채팅의 source_load 1회 상한(코어 v0.26.0 #389). 기본값은 코어 기본(5개·60,000자)이다(D3).
SOURCE_LOAD_MAX_SOURCES_DEFAULT = SOURCE_LOAD_TOOL_DEFAULTS["max_sources"]
SOURCE_LOAD_MAX_SOURCES_MIN = 1
SOURCE_LOAD_MAX_SOURCES_MAX = SOURCE_LOAD_TOOL_HARD_CAPS["max_sources"]
SOURCE_LOAD_MAX_CHARS_DEFAULT = SOURCE_LOAD_TOOL_DEFAULTS["max_chars"]
SOURCE_LOAD_MAX_CHARS_MIN = 5_000
SOURCE_LOAD_MAX_CHARS_MAX = SOURCE_LOAD_TOOL_HARD_CAPS["max_chars"]
# 원문을 남은 토큰 한도의 이 비율 안으로 줄인다. 코어 기본은 0(끔)이지만 채팅은 0.3으로 켠다(D3) —
# 원문 읽기 한 번이 1회 토큰 한도를 다 쓴 stg 사례(#228)를 막는다. 코어는 1을 넘으면 1로 깎는다.
SOURCE_LOAD_BUDGET_RATIO_DEFAULT = 0.3
SOURCE_LOAD_BUDGET_RATIO_MIN = 0.0
SOURCE_LOAD_BUDGET_RATIO_MAX = 1.0
# HITL 승인 대기 시간(분) — 기본값은 코어 `settings_store.DEFAULTS["agents"]`다. 상한은 하루다.
APPROVAL_TIMEOUT_MINUTES_DEFAULT = int(SETTING_DEFAULTS["agents"]["approval_timeout_minutes"])
APPROVAL_TIMEOUT_MINUTES_MIN = 1
APPROVAL_TIMEOUT_MINUTES_MAX = 1_440

# 항목 → (기본값, 최솟값, 최댓값). 기본값이 float인 항목은 소수로 읽는다.
LIMIT_BOUNDS: dict[str, tuple[float, float, float]] = {
    "chat_token_budget": (CHAT_TOKEN_BUDGET_DEFAULT, CHAT_TOKEN_BUDGET_MIN, CHAT_TOKEN_BUDGET_MAX),
    "chat_cost_budget_usd": (
        CHAT_COST_BUDGET_USD_DEFAULT,
        CHAT_COST_BUDGET_USD_MIN,
        CHAT_COST_BUDGET_USD_MAX,
    ),
    "chat_max_iters": (CHAT_MAX_ITERS_DEFAULT, CHAT_MAX_ITERS_MIN, CHAT_MAX_ITERS_MAX),
    "chat_max_tokens": (CHAT_MAX_TOKENS_DEFAULT, CHAT_MAX_TOKENS_MIN, CHAT_MAX_TOKENS_MAX),
    "chat_history_messages": (
        CHAT_HISTORY_MESSAGES_DEFAULT,
        CHAT_HISTORY_MESSAGES_MIN,
        CHAT_HISTORY_MESSAGES_MAX,
    ),
    "source_load_max_sources": (
        SOURCE_LOAD_MAX_SOURCES_DEFAULT,
        SOURCE_LOAD_MAX_SOURCES_MIN,
        SOURCE_LOAD_MAX_SOURCES_MAX,
    ),
    "source_load_max_chars": (
        SOURCE_LOAD_MAX_CHARS_DEFAULT,
        SOURCE_LOAD_MAX_CHARS_MIN,
        SOURCE_LOAD_MAX_CHARS_MAX,
    ),
    "source_load_budget_ratio": (
        SOURCE_LOAD_BUDGET_RATIO_DEFAULT,
        SOURCE_LOAD_BUDGET_RATIO_MIN,
        SOURCE_LOAD_BUDGET_RATIO_MAX,
    ),
    "approval_timeout_minutes": (
        APPROVAL_TIMEOUT_MINUTES_DEFAULT,
        APPROVAL_TIMEOUT_MINUTES_MIN,
        APPROVAL_TIMEOUT_MINUTES_MAX,
    ),
}
# `agents` 키에 두는 항목 — 나머지는 `limits` 키다
_AGENTS_FIELDS = frozenset({"approval_timeout_minutes"})


def _clamp(value: Any, default: float, minimum: float, maximum: float) -> float:
    """저장값을 범위 안으로 맞춘다. 읽을 수 없는 값은 기본값이다."""
    convert = float if isinstance(default, float) else int
    try:
        n = convert(value) if value is not None and not isinstance(value, bool) else default
    except (TypeError, ValueError, OverflowError):
        n = default
    if isinstance(n, float) and math.isnan(n):
        n = default
    return min(max(n, minimum), maximum)


async def get_limit_settings(db: AsyncSession) -> dict[str, Any]:
    """저장값을 기본값 위에 얹어 돌려준다.

    저장값은 검증 없이 들어왔을 수 있어(DB 직접 수정) 범위 안으로 맞춘다.
    """
    agents = await get_setting(db, AGENTS_SETTING_KEY)
    stored = {
        **await get_setting(db, LIMITS_SETTING_KEY),
        **{name: agents.get(name) for name in _AGENTS_FIELDS},
    }
    return {name: _clamp(stored.get(name), *bounds) for name, bounds in LIMIT_BOUNDS.items()}


async def _lock_setting_row(db: AsyncSession, key: str, *, create: bool) -> SystemSetting | None:
    """설정 행을 잠가 최신 값으로 읽는다. `create`면 없는 행을 빈 값으로 먼저 만든다.

    코어 `set_setting`은 잠그지 않고 읽어 전체를 바꾸므로, 두 관리자가 다른 항목을 동시에 저장하면
    한쪽 변경이 사라진다. 동시 첫 저장은 `ON CONFLICT DO NOTHING`으로 한쪽만 넣고 다른 쪽은 그
    커밋을 기다려 같은 행을 잠근다(PK 충돌 없음). 앞서 읽은 행이 세션에 있어도 잠근 뒤 값을 다시
    읽는다(`populate_existing`).
    """
    if create:
        await db.execute(
            pg_insert(SystemSetting)
            .values(key=key, value={})
            .on_conflict_do_nothing(index_elements=[SystemSetting.key])
        )
    return await db.get(SystemSetting, key, with_for_update=True, populate_existing=True)


async def update_limit_settings(
    db: AsyncSession, changes: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """보낸 항목만 키별로 합쳐 저장하고 적용값 전후를 돌려준다(커밋은 호출측).

    두 키를 늘 `limits` → `agents` 순서로 잠가 교착을 막고, 잠근 뒤의 값으로 전후를 센다. 범위
    검증은 요청 스키마의 몫이다.
    """
    parts = {
        LIMITS_SETTING_KEY: {k: v for k, v in changes.items() if k not in _AGENTS_FIELDS},
        AGENTS_SETTING_KEY: {k: v for k, v in changes.items() if k in _AGENTS_FIELDS},
    }
    rows = {key: await _lock_setting_row(db, key, create=bool(part)) for key, part in parts.items()}
    before = await get_limit_settings(db)
    for key, part in parts.items():
        row = rows[key]
        if part and row is not None:
            stored = row.value if isinstance(row.value, dict) else {}
            row.value = {**stored, **part}  # 새 dict를 대입해야 JSONB 변경이 감지된다
    await db.flush()
    return before, await get_limit_settings(db)


async def chat_token_budget(db: AsyncSession) -> int:
    return int((await get_limit_settings(db))["chat_token_budget"])


async def approval_timeout_minutes(db: AsyncSession) -> int:
    """HITL 승인 대기 시간(분) — 워커가 승인 만료 시각을 정할 때 읽는다."""
    return int((await get_limit_settings(db))["approval_timeout_minutes"])
