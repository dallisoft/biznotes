"""제목 템플릿 계열 노트북(#403) — `save_sources`의 `notebook_rule`(월별·일별 노트북, `(n)` 넘김).

계열(series)은 run 사용자가 **실제로 소유한**(`Notebook.owner_id`) 노트북 중 제목이 템플릿에 맞는 것
전부다 — 모든 기간(해·달·날), 접미사 ` (2)`~` (99)` 포함. 공유·admin 승격은 보지 않는다(다른
사용자의 같은 제목 노트북을 고르지 않는다). 이번 기간은 노드 시작 때 한 번 계산한
제목(`render_title`)과 그 접미사다.

항목 하나를 쓰는 순서(한 트랜잭션, `NotebookSeries.place`):
계열 잠금(소유자+템플릿) → 계열 탐색 → 계열 단위 중복 → 이번 기간의 가장 큰 (n) → 노트북 잠금·용량
(`admit_source`) → 꽉 찼으면 n+1을 run 사용자 명의로 만들고 다시. 탐색과 생성이 계열 잠금 안이라
동시 두 run이 같은 새 노트북을 두 번 만들지 않는다. 잠금 순서는 계열 → 노트북으로 고정이다.

스케줄의 `notebook_rule`(#435)은 같은 선택 규칙으로 이번 기간 노트북을 읽기만 한다
(`find_period_notebook`).
"""

import hashlib
import re
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfoNotFoundError

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.config import get_settings, is_timezone_name
from biznotes_core.core.timezones import get_zone
from biznotes_core.core.uow import UnitOfWork
from biznotes_core.models import Notebook, Source
from biznotes_core.services.ingestion.source_create import (
    PlaceSource,
    SourceAdmission,
    admit_source,
    find_external_source,
)
from biznotes_core.services.ingestion.source_warnings import clean_text
from biznotes_core.services.notebooks import create_notebook

# notebooks.title 길이와 접미사 상한 — 템플릿 제목은 ` (99)`가 붙을 자리를 남긴다
TITLE_MAX_CHARS = 255
MAX_SUFFIX = 99
_TEMPLATE_MAX_CHARS = TITLE_MAX_CHARS - len(f" ({MAX_SUFFIX})")
_EMOJI_MAX_CHARS = 16  # notebooks.emoji
_RULE_KEYS = frozenset({"title_template", "timezone", "emoji"})
_PLACEHOLDER_RE = re.compile(r"\{(year|month|date)\}")
# 길이 검사용 표본 값과 계열 판정용 정규식 조각. 판정은 fullmatch라 자리표시자가 다른 템플릿의
# 제목(`2026-10` ↔ `2026-10-28`)은 서로의 계열에 들지 않는다(#434)
_SAMPLE = {"year": "0000", "month": "0000-00", "date": "0000-00-00"}
_PATTERN = {"year": r"\d{4}", "month": r"\d{4}-\d{2}", "date": r"\d{4}-\d{2}-\d{2}"}
_SUFFIX = r"(?: \(([2-9]|[1-9][0-9])\))?"


def notebook_rule_errors(rule: Any) -> list[str]:
    """`notebook_rule` 리터럴 설정 오류 목록 — 저장 헬퍼와 실행기가 같이 쓴다.

    `{"title_template": "종목분석 {month}", "timezone": "Asia/Seoul", "emoji": "📈"}`:
    - `title_template`: 한 줄(앞뒤 공백·보이지 않는 글자 없음). `{year}`·`{month}`(YYYY-MM)·
      `{date}`(YYYY-MM-DD)만 받고 함께 써도 된다. 렌더한 제목은 접미사 자리를 빼고 250자 이하다.
    - `timezone`(선택): IANA 이름. 없으면 `display_timezone`이다.
    - `emoji`(선택): 1~16자. 없으면 노트북 기본값이다. 그 밖의 키는 받지 않는다.
    """
    if not isinstance(rule, dict):
        return ["notebook_rule은 객체여야 합니다"]
    errors: list[str] = []
    unknown = sorted(str(k) for k in rule.keys() - _RULE_KEYS)
    if unknown:
        errors.append(f"notebook_rule에 모르는 키가 있습니다: {', '.join(unknown)}")
    template = rule.get("title_template")
    if not isinstance(template, str) or not template.strip():
        errors.append("notebook_rule.title_template이 필요합니다")
    elif clean_text(template, len(template)) != template:
        errors.append(
            "notebook_rule.title_template은 앞뒤 공백·줄바꿈·보이지 않는 글자가 없어야 합니다"
        )
    elif re.search(r"[{}]", _PLACEHOLDER_RE.sub("", template)):
        errors.append("notebook_rule.title_template에는 {year}·{month}·{date}만 쓸 수 있습니다")
    elif len(_render(template, _SAMPLE)) > _TEMPLATE_MAX_CHARS:
        errors.append(
            f"notebook_rule.title_template 제목은 {_TEMPLATE_MAX_CHARS}자 이하여야 합니다"
        )
    if "timezone" in rule:
        tz = rule["timezone"]
        if not (isinstance(tz, str) and is_timezone_name(tz)):
            errors.append("notebook_rule.timezone은 IANA 시간대 이름이어야 합니다")
    if "emoji" in rule:
        emoji = rule["emoji"]
        if not (
            isinstance(emoji, str)
            and 1 <= len(emoji) <= _EMOJI_MAX_CHARS
            and emoji.strip() == emoji
            and not any(unicodedata.category(ch) == "Cc" for ch in emoji)
        ):
            errors.append(f"notebook_rule.emoji는 1~{_EMOJI_MAX_CHARS}자 문자열이어야 합니다")
    return errors


def _render(template: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)


def render_title(template: str, now: datetime, zone: tzinfo) -> str:
    """`now`를 `zone`의 벽시계로 바꿔 자리표시자를 채운다.

    `{year}`(YYYY)·`{month}`(YYYY-MM)·`{date}`(YYYY-MM-DD, 그 시간대의 날짜 — #434).
    """
    local = now.astimezone(zone)
    return _render(
        template,
        {"year": f"{local.year:04d}", "month": f"{local:%Y-%m}", "date": f"{local:%Y-%m-%d}"},
    )


def period_number(title: str, base: str) -> int | None:
    """`title`이 이번 기간 제목 `base`의 몇 번째 노트북인가 — `base`는 1, `base (n)`은 n(2~99)."""
    m = re.fullmatch(re.escape(base) + _SUFFIX, title)
    if m is None:
        return None
    return int(m.group(1)) if m.group(1) else 1


def _series_regex(template: str) -> re.Pattern[str]:
    parts = _PLACEHOLDER_RE.split(template)  # [글자, 이름, 글자, …]
    body = "".join(re.escape(p) if i % 2 == 0 else _PATTERN[p] for i, p in enumerate(parts))
    return re.compile(body + _SUFFIX)


def in_series(title: str, template: str) -> bool:
    """제목이 템플릿 계열(모든 기간·접미사)에 드는가."""
    return _series_regex(template).fullmatch(title) is not None


def like_pattern(template: str) -> str:
    """계열 후보를 좁히는 LIKE 패턴(이스케이프 `\\`) — 정확한 판정은 `in_series`가 한다.

    글자 부분의 `\\`·`%`·`_`를 이스케이프하고 자리표시자는 `%`, 끝에 접미사 자리 `%`를 둔다.
    """
    parts = _PLACEHOLDER_RE.split(template)
    escaped = (
        p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") if i % 2 == 0 else "%"
        for i, p in enumerate(parts)
    )
    return "".join(escaped) + "%"


def _series_lock_key(owner_id: uuid.UUID, template: str) -> int:
    namespace = get_settings().service_name
    digest = hashlib.sha256(f"{namespace}:notebook-series:{owner_id}:{template}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def lock_series(db: AsyncSession, owner_id: uuid.UUID, template: str) -> None:
    """한 소유자·템플릿 계열의 쓰기를 트랜잭션 끝까지 직렬화한다(advisory xact lock)."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _series_lock_key(owner_id, template)},
    )


async def list_series(
    db: AsyncSession, owner_id: uuid.UUID, template: str
) -> list[tuple[uuid.UUID, str]]:
    """소유자가 실제로 소유한 계열 노트북 `(id, 제목)` — 만든 순서."""
    rows = await db.execute(
        select(Notebook.id, Notebook.title)
        .where(
            Notebook.owner_id == owner_id,
            Notebook.title.like(like_pattern(template), escape="\\"),
        )
        .order_by(Notebook.created_at, Notebook.id)
    )
    regex = _series_regex(template)
    return [(row.id, row.title) for row in rows if regex.fullmatch(row.title)]


def _largest_in_period(
    members: list[tuple[uuid.UUID, str]], title: str
) -> tuple[int, uuid.UUID | None]:
    """이번 기간(`title`)의 가장 큰 `(n)`과 그 노트북 — 없으면 `(0, None)`.

    `members`는 만든 순서(`list_series`)라 같은 n이면 먼저 만든 것이다.
    """
    n, target = 0, None
    for nb_id, nb_title in members:
        k = period_number(nb_title, title)
        if k is not None and k > n:
            n, target = k, nb_id
    return n, target


@dataclass(frozen=True)
class NotebookSeries:
    """이번 실행의 계열 — 노드 시작 때 한 번 정한다(`from_rule`)."""

    owner_id: uuid.UUID
    title_template: str
    title: str  # 이번 기간의 첫 노트북 제목(접미사 없음)
    emoji: str | None = None

    @classmethod
    def from_rule(
        cls, rule: dict[str, Any], *, owner_id: uuid.UUID, now: datetime
    ) -> "NotebookSeries":
        zone = get_zone(rule.get("timezone") or get_settings().display_timezone)
        template = rule["title_template"]
        return cls(owner_id, template, render_title(template, now, zone), rule.get("emoji"))

    async def member_ids(self, db: AsyncSession) -> list[uuid.UUID]:
        return [nb_id for nb_id, _ in await list_series(db, self.owner_id, self.title_template)]

    def placement(self, external_ref: str | None) -> PlaceSource:
        """`create_text_source(place=…)`에 넘기는 대상 결정 — 항목 트랜잭션 안에서 돈다."""

        async def place(
            db: AsyncSession, uow: UnitOfWork, build: Callable[[uuid.UUID], Source]
        ) -> SourceAdmission:
            return await self._place(db, uow, build, external_ref)

        return place

    async def _place(
        self,
        db: AsyncSession,
        uow: UnitOfWork,
        build: Callable[[uuid.UUID], Source],
        external_ref: str | None,
    ) -> SourceAdmission:
        await lock_series(db, self.owner_id, self.title_template)
        members = await list_series(db, self.owner_id, self.title_template)
        ids = [nb_id for nb_id, _ in members]
        # 넘김 전에 계열 전체를 본다 — 중복 항목 때문에 빈 노트북을 만들지 않는다
        if external_ref is not None:
            existing = await find_external_source(db, ids, external_ref)
            if existing is not None:
                return existing
        n, target = _largest_in_period(members, self.title)
        if target is None:
            n, target = 1, await self._create(db, 1)
        result = await admit_source(db, uow, build(target), duplicate_scope=ids)
        if result.outcome != "full" or n >= MAX_SUFFIX:
            return result
        target = await self._create(db, n + 1)
        return await admit_source(db, uow, build(target), duplicate_scope=ids)

    async def _create(self, db: AsyncSession, n: int) -> uuid.UUID:
        title = self.title if n == 1 else f"{self.title} ({n})"
        notebook = await create_notebook(
            db, owner_id=self.owner_id, title=title, emoji=self.emoji
        )
        return notebook.id


async def find_period_notebook(
    db: AsyncSession, *, owner_id: uuid.UUID, rule: dict[str, Any], now: datetime
) -> uuid.UUID | None:
    """이번 기간 계열 노트북 하나 — 노트북 규칙 스케줄(#435)의 실행 대상. 없으면 None.

    save_sources가 지금 쓰는 노트북과 같다. 규칙 시간대(없으면 `display_timezone`)로 이번 기간
    제목을 정하고(`NotebookSeries.from_rule`), `owner_id`가 실제로 소유한 계열에서 가장 큰
    `(n)`을 고른다(같은 n이면 먼저 만든 것). 읽기만 한다 — 만들지 않고 계열 잠금도 잡지 않는다.
    `rule`은 `notebook_rule_errors`를 통과한 값이어야 한다. 이름은 맞는데 시간대 파일을 읽지
    못하면(손상·권한·삭제) DB에 닿기 전에 `ValueError`다.
    """
    try:
        series = NotebookSeries.from_rule(rule, owner_id=owner_id, now=now)
    except (OSError, ZoneInfoNotFoundError) as e:
        raise ValueError("notebook_rule 시간대를 읽지 못했습니다") from e
    members = await list_series(db, owner_id, series.title_template)
    return _largest_in_period(members, series.title)[1]


__all__ = [
    "MAX_SUFFIX",
    "TITLE_MAX_CHARS",
    "NotebookSeries",
    "find_period_notebook",
    "in_series",
    "like_pattern",
    "list_series",
    "lock_series",
    "notebook_rule_errors",
    "period_number",
    "render_title",
]
