"""관리자 「사용 제한」 설정 계약.

분당 레이트 리밋 재정의(#386), 일일 사용 한도 기본값(#387), 소스 업로드 최대 크기(#388),
노트북당 소스 상한(#445).

값의 정본은 `system_settings`이고 기본값은 env(설정)·코드 상수다. 요청 본문은 부분 갱신이다 —
본문에 없는 키는 그대로 두고, 명시적 `null`은 재정의를 지워 기본값으로 돌린다.
"""

from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from biznotes_core.config import SOURCE_MAX_UPLOAD_BYTES_CEILING

# ---------- 분당 레이트 리밋(#386) ----------

#: 관리자가 재정의할 수 있는 키 — `CoreSettings`의 분당 한도 필드 이름(`RateLimiter.limit_setting`)
RateLimitKey = Literal[
    "rate_limit_chat",
    "rate_limit_source_create",
    "rate_limit_source_upload",
    "rate_limit_web_search",
    "rate_limit_search",
    "rate_limit_source_content",
    "rate_limit_agent_run",
]
RATE_LIMIT_KEYS: tuple[str, ...] = get_args(RateLimitKey)
# 재정의 범위. 0 이하(해당 리밋 비활성)는 env에서만 정한다 — 화면에서 남용 방지를 끄지 못하게
RATE_LIMIT_OVERRIDE_MIN = 1
RATE_LIMIT_OVERRIDE_MAX = 100_000

_RateLimitValue = Annotated[
    int | None, Field(ge=RATE_LIMIT_OVERRIDE_MIN, le=RATE_LIMIT_OVERRIDE_MAX)
]


class RateLimitItem(BaseModel):
    key: RateLimitKey
    env_value: int  # 설정(env) 값 — 0 이하면 env에서 비활성
    override: int | None  # 관리자 재정의(없으면 null)
    effective: int  # 지금 적용되는 값(재정의 > env)


class RateLimitsOut(BaseModel):
    # 전체 켜기·끄기(`RATE_LIMIT_ENABLED`) — env 전용, 읽기 전용이다
    enabled: bool
    # 다른 API 프로세스에 반영되기까지의 최대 지연(초)
    cache_ttl_seconds: float
    min_value: int = RATE_LIMIT_OVERRIDE_MIN
    max_value: int = RATE_LIMIT_OVERRIDE_MAX
    items: list[RateLimitItem]


class RateLimitsUpdate(BaseModel):
    """부분 갱신 — 없는 키는 그대로, `null`은 재정의 삭제(env 값으로 돌아간다)."""

    model_config = ConfigDict(extra="forbid")

    rate_limit_chat: _RateLimitValue = None
    rate_limit_source_create: _RateLimitValue = None
    rate_limit_source_upload: _RateLimitValue = None
    rate_limit_web_search: _RateLimitValue = None
    rate_limit_search: _RateLimitValue = None
    rate_limit_source_content: _RateLimitValue = None
    rate_limit_agent_run: _RateLimitValue = None


# ---------- 일일 사용 한도 기본값(#387) ----------

#: 일일 한도 필드 — `usage_limits` 열 이름과 같다
UsageLimitField = Literal["daily_tokens", "daily_agent_runs"]
USAGE_LIMIT_FIELDS: tuple[str, ...] = get_args(UsageLimitField)
# 사용자별 한도와 같은 범위 — 0은 막음, 상한은 `usage_limits` 열(INTEGER)의 최댓값
USAGE_LIMIT_MAX = 2_147_483_647

_UsageLimitValue = Annotated[int | None, Field(ge=0, le=USAGE_LIMIT_MAX)]


class UsageDefaults(BaseModel):
    """시스템 기본 일일 한도 — 사용자별 값이 없는(행 없음·null) 사용자에게 적용된다.

    응답에서 null은 기본값 없음(무제한)이다. 요청은 부분 갱신이다 — 없는 키는 그대로, `null`은
    기본값을 지운다.
    """

    model_config = ConfigDict(extra="forbid")

    daily_tokens: _UsageLimitValue = None
    daily_agent_runs: _UsageLimitValue = None


#: 적용값의 출처 — 사용자별 값 > 시스템 기본값 > 무제한
UsageLimitSource = Literal["user", "default", "unlimited"]


class EffectiveUsageLimit(BaseModel):
    value: int | None  # 실제 적용 한도(무제한이면 null)
    source: UsageLimitSource


class EffectiveUsageLimits(BaseModel):
    daily_tokens: EffectiveUsageLimit
    daily_agent_runs: EffectiveUsageLimit


# ---------- 소스 업로드 최대 크기(#388) ----------


class SourceUploadSettingsOut(BaseModel):
    max_upload_bytes: int  # 지금 적용되는 값(재정의 > env) — 공개 조회·업로드 판정과 같다
    env_max_upload_bytes: int  # env `SOURCE_MAX_UPLOAD_BYTES`(기본 50 MiB)
    override: int | None  # 관리자 재정의(없으면 null)
    hard_max_upload_bytes: int = SOURCE_MAX_UPLOAD_BYTES_CEILING  # 재정의·env 공통 하드 상한


class SourceUploadSettingsUpdate(BaseModel):
    """`null`은 재정의 삭제(env 값으로 돌아간다). 범위는 1~하드 상한(500 MiB)이다."""

    model_config = ConfigDict(extra="forbid")

    max_upload_bytes: int | None = Field(ge=1, le=SOURCE_MAX_UPLOAD_BYTES_CEILING)


# ---------- 노트북당 소스 상한(#445) ----------

#: 재정의 상한 — `usage_limits`와 같은 INTEGER 최댓값.
#: env(`MAX_SOURCES_PER_NOTEBOOK`)에는 상한이 없다
NOTEBOOK_SOURCE_LIMIT_OVERRIDE_MAX = 2_147_483_647


class NotebookSourceLimitOut(BaseModel):
    max_sources_per_notebook: int  # 지금 적용되는 값(재정의 > env) — 공개 조회·추가 판정과 같다
    env_max_sources_per_notebook: int  # env `MAX_SOURCES_PER_NOTEBOOK`(기본 1000)
    override: int | None  # 관리자 재정의(없으면 null)


class NotebookSourceLimitUpdate(BaseModel):
    """`null`은 재정의 삭제(env 값으로 돌아간다). 범위는 1~2,147,483,647이다."""

    model_config = ConfigDict(extra="forbid")

    max_sources_per_notebook: int | None = Field(ge=1, le=NOTEBOOK_SOURCE_LIMIT_OVERRIDE_MAX)
