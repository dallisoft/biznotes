"""`save_sources`의 `notebook_rule`(#403 PR-C, `{date}` #434) — DB 없이 보는 규칙: 설정 검사·제목
계산·계열 판정.

실제 탐색·생성·넘김·잠금·소유자 범위는 `tests/test_save_sources_notebook_rule_pg.py`가 본다.
"""

import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from biznotes_core.services import notebook_series
from biznotes_core.services.agents import policy


def _rule(**kw: Any) -> dict[str, Any]:
    return {"title_template": "종목분석 {month}", **kw}


@pytest.mark.parametrize(
    "rule",
    [
        _rule(),
        _rule(timezone="Asia/Seoul", emoji="📈"),
        _rule(timezone="UTC"),
        {"title_template": "{year}년 {month} 모음"},
        {"title_template": "고정 제목"},  # 자리표시자가 없어도 된다(상한 넘김만 한다)
        {"title_template": "a" * 242 + " {month}"},  # 렌더 250자 — 접미사 ` (99)` 여유
        {"title_template": "종목분석 {date}"},  # 일별(#434)
        _rule(title_template="종목분석 {date}", timezone="Asia/Seoul", emoji="📈"),
        {"title_template": "{month} 모음/{date}"},  # 함께 써도 된다
        {"title_template": "a" * 239 + " {date}"},  # 렌더 250자
    ],
)
def test_valid_rules(rule: dict[str, Any]) -> None:
    assert notebook_series.notebook_rule_errors(rule) == []


@pytest.mark.parametrize(
    "rule",
    [
        "종목분석 {month}",  # 객체여야 한다
        {},
        {"title_template": ""},
        {"title_template": "   "},
        {"title_template": 3},
        {"title_template": " 앞 공백 {month}"},
        {"title_template": "줄\n바꿈 {month}"},
        {"title_template": "x {day}"},  # {year}·{month}만
        {"title_template": "x {params.name}"},  # 템플릿으로 바꿀 수 없다
        {"title_template": "x {month"},
        {"title_template": "x }"},
        {"title_template": "a" * 243 + " {month}"},  # 렌더 251자
        {"title_template": "a" * 240 + " {date}"},  # 렌더 251자
        {"title_template": "x {Date}"},
        {"title_template": "x {date:%d}"},
        _rule(timezone="Mars/Base"),
        _rule(timezone=9),
        _rule(emoji=""),
        _rule(emoji="x" * 17),
        _rule(emoji=1),
        _rule(owner="other"),  # 모르는 키
    ],
)
def test_invalid_rules(rule: Any) -> None:
    assert notebook_series.notebook_rule_errors(rule)


def test_unknown_placeholder_message_lists_every_placeholder() -> None:
    assert notebook_series.notebook_rule_errors({"title_template": "x {day}"}) == [
        "notebook_rule.title_template에는 {year}·{month}·{date}만 쓸 수 있습니다"
    ]


def test_save_helper_and_runtime_share_the_rule_check() -> None:
    node = {
        "id": "save",
        "type": "save_sources",
        "items": "{list.text}",
        "namespace": "ns",
        "requires_approval": False,
    }
    assert policy.save_sources_config_errors({**node, "notebook_rule": _rule()}) == []
    daily = {"title_template": "종목분석 {date}", "timezone": "Asia/Seoul"}
    assert policy.save_sources_config_errors({**node, "notebook_rule": daily}) == []
    found = policy.find_direct_node_approval_violations(
        [{**node, "notebook_rule": {"title_template": "x {day}"}}], [], "system"
    )
    assert found and all(f.startswith("save: ") and "notebook_rule" in f for f in found)


def test_title_is_computed_in_the_rule_timezone() -> None:
    # 2026-10-31 15:30 UTC = 2026-11-01 00:30 KST
    now = datetime(2026, 10, 31, 15, 30, tzinfo=UTC)
    assert notebook_series.render_title("종목분석 {month}", now, ZoneInfo("Asia/Seoul")) == (
        "종목분석 2026-11"
    )
    assert notebook_series.render_title("종목분석 {month}", now, UTC) == "종목분석 2026-10"
    assert notebook_series.render_title("{year}/{month}", now, UTC) == "2026/2026-10"


@pytest.mark.parametrize(
    ("now", "title"),
    [
        # KST 자정 경계 — 23:59는 그날, 00:00은 다음 날(#434)
        (datetime(2026, 10, 28, 14, 59, 59, 999999, tzinfo=UTC), "종목분석 2026-10-28"),
        (datetime(2026, 10, 28, 15, 0, tzinfo=UTC), "종목분석 2026-10-29"),
        # 달·해가 바뀌는 날
        (datetime(2026, 10, 31, 15, 30, tzinfo=UTC), "종목분석 2026-11-01"),
        (datetime(2026, 12, 31, 15, 0, tzinfo=UTC), "종목분석 2027-01-01"),
    ],
)
def test_date_is_the_wall_clock_day_in_the_rule_timezone(now: datetime, title: str) -> None:
    assert notebook_series.render_title("종목분석 {date}", now, ZoneInfo("Asia/Seoul")) == title


def test_date_renders_with_the_other_placeholders() -> None:
    now = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)  # UTC 10-28, KST 10-29
    assert notebook_series.render_title("종목분석 {date}", now, UTC) == "종목분석 2026-10-28"
    assert notebook_series.render_title("{year}/{month}/{date}", now, ZoneInfo("Asia/Seoul")) == (
        "2026/2026-10/2026-10-29"
    )


def test_series_title_comes_from_the_rule_timezone() -> None:
    rule = {"title_template": "종목분석 {date}", "timezone": "Asia/Seoul"}
    series = notebook_series.NotebookSeries.from_rule(
        rule, owner_id=uuid.uuid4(), now=datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
    )
    assert (series.title_template, series.title) == ("종목분석 {date}", "종목분석 2026-10-29")


@pytest.mark.parametrize(
    ("title", "n"),
    [
        ("종목분석 2026-10", 1),
        ("종목분석 2026-10 (2)", 2),
        ("종목분석 2026-10 (99)", 99),
        ("종목분석 2026-10 (1)", None),  # 첫 노트북은 접미사가 없다
        ("종목분석 2026-10 (100)", None),
        ("종목분석 2026-10 (02)", None),
        ("종목분석 2026-10(2)", None),
        ("종목분석 2026-10 백업", None),
        ("종목분석 2026-11", None),  # 다른 달
        ("x종목분석 2026-10", None),
    ],
)
def test_period_number(title: str, n: int | None) -> None:
    assert notebook_series.period_number(title, "종목분석 2026-10") == n


@pytest.mark.parametrize(
    ("title", "member"),
    [
        ("종목분석 2026-10", True),
        ("종목분석 2025-01 (3)", True),
        ("종목분석 2026-10 (100)", False),
        ("종목분석 2026-1", False),
        ("종목분석 2026-10 백업", False),
        ("종목분석 ", False),
    ],
)
def test_series_membership_spans_all_months(title: str, member: bool) -> None:
    assert notebook_series.in_series(title, "종목분석 {month}") is member


@pytest.mark.parametrize(
    ("title", "member"),
    [
        ("종목분석 2026-10-28", True),
        ("종목분석 2025-01-02 (3)", True),
        ("종목분석 2026-10-28 (99)", True),
        ("종목분석 2026-10-28 (100)", False),
        ("종목분석 2026-10-2", False),
        ("종목분석 2026-10-28 백업", False),
        # 월별·연별 계열 제목은 일별 계열이 아니다
        ("종목분석 2026-10", False),
        ("종목분석 2026-10 (2)", False),
        ("종목분석 2026", False),
    ],
)
def test_daily_series_spans_all_days_only(title: str, member: bool) -> None:
    assert notebook_series.in_series(title, "종목분석 {date}") is member


@pytest.mark.parametrize(
    "title", ["종목분석 2026-10-28", "종목분석 2026-10-28 (2)", "종목분석 2026-10-28 (99)"]
)
def test_daily_titles_are_outside_monthly_and_yearly_series(title: str) -> None:
    assert not notebook_series.in_series(title, "종목분석 {month}")
    assert not notebook_series.in_series(title, "종목분석 {year}")


def test_daily_period_number_counts_one_day_only() -> None:
    base = "종목분석 2026-10-28"
    assert notebook_series.period_number("종목분석 2026-10-28 (2)", base) == 2
    assert notebook_series.period_number("종목분석 2026-10-29", base) is None
    assert notebook_series.period_number("종목분석 2026-10", base) is None


def test_template_with_month_and_date_has_its_own_series() -> None:
    template = "리포트 {month}/{date}"
    assert notebook_series.in_series("리포트 2026-10/2026-10-28 (2)", template)
    assert not notebook_series.in_series("리포트 2026-10/2026-10", template)
    assert not notebook_series.in_series("리포트 2026-10/2026-10-28", "리포트 {month}/{month}")


def test_like_prefilter_escapes_wildcards() -> None:
    assert notebook_series.like_pattern("a_b%c\\d {month}") == "a\\_b\\%c\\\\d %%"
    assert notebook_series.like_pattern("{year} 모음") == "% 모음%"
    assert notebook_series.like_pattern("종목분석 {date}") == "종목분석 %%"
    assert notebook_series.in_series("a_b%c\\d 2026-10 (2)", "a_b%c\\d {month}")
    assert not notebook_series.in_series("aXb%c\\d 2026-10", "a_b%c\\d {month}")
