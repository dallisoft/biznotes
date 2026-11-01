from pydantic import BaseModel, Field

from app.services.limits import (
    APPROVAL_TIMEOUT_MINUTES_MAX,
    APPROVAL_TIMEOUT_MINUTES_MIN,
    CHAT_COST_BUDGET_USD_MAX,
    CHAT_COST_BUDGET_USD_MIN,
    CHAT_HISTORY_MESSAGES_MAX,
    CHAT_HISTORY_MESSAGES_MIN,
    CHAT_MAX_ITERS_MAX,
    CHAT_MAX_ITERS_MIN,
    CHAT_MAX_TOKENS_MAX,
    CHAT_MAX_TOKENS_MIN,
    CHAT_TOKEN_BUDGET_MAX,
    CHAT_TOKEN_BUDGET_MIN,
    SOURCE_LOAD_BUDGET_RATIO_MAX,
    SOURCE_LOAD_BUDGET_RATIO_MIN,
    SOURCE_LOAD_MAX_CHARS_MAX,
    SOURCE_LOAD_MAX_CHARS_MIN,
    SOURCE_LOAD_MAX_SOURCES_MAX,
    SOURCE_LOAD_MAX_SOURCES_MIN,
)


class LimitSettingsOut(BaseModel):
    """관리 › 사용 제한 — 적용 중인 값(저장값이 없으면 기본값)."""

    chat_token_budget: int
    chat_cost_budget_usd: float
    chat_max_iters: int
    chat_max_tokens: int
    chat_history_messages: int
    source_load_max_sources: int
    source_load_max_chars: int
    source_load_budget_ratio: float
    approval_timeout_minutes: int


class LimitSettingsUpdate(BaseModel):
    """보낸 필드만 저장한다 — 필드를 모르는 클라이언트의 PUT이 저장값을 기본값으로 되돌리지 않게."""

    chat_token_budget: int | None = Field(
        default=None, ge=CHAT_TOKEN_BUDGET_MIN, le=CHAT_TOKEN_BUDGET_MAX
    )
    chat_cost_budget_usd: float | None = Field(
        default=None,
        ge=CHAT_COST_BUDGET_USD_MIN,
        le=CHAT_COST_BUDGET_USD_MAX,
        allow_inf_nan=False,
    )
    chat_max_iters: int | None = Field(default=None, ge=CHAT_MAX_ITERS_MIN, le=CHAT_MAX_ITERS_MAX)
    chat_max_tokens: int | None = Field(
        default=None, ge=CHAT_MAX_TOKENS_MIN, le=CHAT_MAX_TOKENS_MAX
    )
    chat_history_messages: int | None = Field(
        default=None, ge=CHAT_HISTORY_MESSAGES_MIN, le=CHAT_HISTORY_MESSAGES_MAX
    )
    source_load_max_sources: int | None = Field(
        default=None, ge=SOURCE_LOAD_MAX_SOURCES_MIN, le=SOURCE_LOAD_MAX_SOURCES_MAX
    )
    source_load_max_chars: int | None = Field(
        default=None, ge=SOURCE_LOAD_MAX_CHARS_MIN, le=SOURCE_LOAD_MAX_CHARS_MAX
    )
    source_load_budget_ratio: float | None = Field(
        default=None,
        ge=SOURCE_LOAD_BUDGET_RATIO_MIN,
        le=SOURCE_LOAD_BUDGET_RATIO_MAX,
        allow_inf_nan=False,
    )
    approval_timeout_minutes: int | None = Field(
        default=None, ge=APPROVAL_TIMEOUT_MINUTES_MIN, le=APPROVAL_TIMEOUT_MINUTES_MAX
    )
