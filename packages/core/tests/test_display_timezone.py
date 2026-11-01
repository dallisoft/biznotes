"""배포 표시 시간대(#352) — 설정 검증, 일일 한도의 「오늘」, 관리자 사용량 기본 기간,
내보내기 시각."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from biznotes_core import config
from biznotes_core.config import CoreSettings, reset_settings
from biznotes_core.core.timezones import display_zone, get_zone, to_display
from biznotes_core.services import usage_guard

USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def display_tz(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """`display_tz("Asia/Seoul")` — 환경 변수로 설정을 바꾸고 끝나면 되돌린다."""

    def use(name: str) -> None:
        monkeypatch.setenv("DISPLAY_TIMEZONE", name)
        reset_settings()

    yield use
    monkeypatch.delenv("DISPLAY_TIMEZONE", raising=False)
    reset_settings()


# ---------- 설정·이름 규칙 ----------


def test_default_is_utc_and_needs_no_tz_database(monkeypatch: pytest.MonkeyPatch) -> None:
    assert CoreSettings().display_timezone == "UTC"
    # 시간대 DB가 없어도 UTC는 쓴다 — 기본값 배포는 지금처럼 돈다
    monkeypatch.setattr(config, "_iana_names", lambda: frozenset())
    assert CoreSettings(display_timezone="UTC").display_timezone == "UTC"
    assert get_zone("UTC") is UTC
    assert not config.is_timezone_name("Asia/Seoul")


def test_iana_name_is_accepted() -> None:
    assert CoreSettings(display_timezone="Asia/Seoul").display_timezone == "Asia/Seoul"
    assert get_zone("Asia/Seoul") == ZoneInfo("Asia/Seoul")


@pytest.mark.parametrize(
    "name", ["Mars/Base", "asia/seoul", "/etc/passwd", "../UTC", "", "Asia/Seoul ", "localtime"]
)
def test_unknown_name_fails_settings_load(name: str) -> None:
    # 대소문자가 다른 이름은 macOS에서는 열리지만 Linux에서는 열리지 않는다 — 어디서나 거부한다
    with pytest.raises(ValidationError, match="display_timezone"):
        CoreSettings(display_timezone=name)
    with pytest.raises(ValueError):
        get_zone(name)


def test_to_display_treats_naive_as_utc(display_tz: Any) -> None:
    display_tz("Asia/Seoul")
    assert display_zone() == ZoneInfo("Asia/Seoul")
    naive = datetime(2026, 10, 26, 4, 34)
    assert to_display(naive) == datetime(2026, 10, 26, 13, 34, tzinfo=ZoneInfo("Asia/Seoul"))
    assert to_display(naive.replace(tzinfo=UTC)).isoformat() == "2026-10-26T13:34:00+09:00"


# ---------- 일일 한도의 「오늘」 ----------


class _FakeRedis:
    store: dict[str, int] = {}

    @classmethod
    def from_url(cls, _url: str) -> "_FakeRedis":
        return cls()

    async def incrby(self, key: str, amount: int) -> None:
        self.store[key] = self.store.get(key, 0) + amount

    async def expire(self, key: str, ttl: int) -> None:
        assert ttl == usage_guard._TTL

    async def get(self, key: str) -> bytes | None:
        value = self.store.get(key)
        return None if value is None else str(value).encode()

    async def mget(self, *keys: str) -> list[bytes | None]:
        return [await self.get(k) for k in keys]

    async def aclose(self) -> None:
        pass


def _freeze(monkeypatch: pytest.MonkeyPatch, at: datetime) -> None:
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "_Frozen":
            return cls.fromtimestamp(at.timestamp(), tz)

    monkeypatch.setattr(usage_guard, "datetime", _Frozen)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    _FakeRedis.store = {}
    monkeypatch.setattr(usage_guard, "Redis", _FakeRedis)
    return _FakeRedis.store


@pytest.mark.parametrize(
    ("tz", "day"),
    [("Asia/Seoul", "20261027"), ("UTC", "20261026")],
)
async def test_usage_accumulates_on_display_day(
    display_tz: Any,
    fake_redis: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    tz: str,
    day: str,
) -> None:
    display_tz(tz)
    # UTC 2026-10-26 16:00 = KST 10-27 01:00
    _freeze(monkeypatch, datetime(2026, 10, 26, 16, 0, tzinfo=UTC))
    await usage_guard.record_usage(USER, "tokens", 7)
    assert fake_redis == {f"usage:tokens:{USER}:{day}": 7}
    assert (await usage_guard.get_today_usage(USER))["tokens"] == 7


def test_day_key_boundary_is_display_midnight(display_tz: Any) -> None:
    display_tz("Asia/Seoul")
    before = usage_guard._key("tokens", USER, datetime(2026, 10, 26, 14, 59, tzinfo=UTC))
    after = usage_guard._key("tokens", USER, datetime(2026, 10, 26, 15, 0, tzinfo=UTC))
    assert before.endswith(":20261026") and after.endswith(":20261027")
