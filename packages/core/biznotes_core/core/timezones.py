"""배포 표시 시간대와 IANA 시간대 변환 (#352).

사용자에게 보이는 날짜 경계(일일 한도의 「오늘」, 관리자 사용량의 「이번 달」)와 서버가 문자열로
찍는 시각은 설정 `display_timezone` 기준이다. API가 주고받는 시각 값은 계속 시간대가 붙은 ISO
형식이다. 이름 규칙은 `config.is_timezone_name` 하나를 쓴다.
"""

from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo

from biznotes_core.config import get_settings, is_timezone_name


def get_zone(name: str) -> tzinfo:
    """IANA 이름의 시간대. `UTC`는 시간대 DB 없이 `datetime.UTC`다. 모르는 이름은 ValueError."""
    if name == "UTC":
        return UTC
    if not is_timezone_name(name):
        raise ValueError(f"IANA 시간대 이름이 아닙니다: {name!r}")
    return ZoneInfo(name)


def display_zone() -> tzinfo:
    """배포 표시 시간대(`display_timezone`)."""
    return get_zone(get_settings().display_timezone)


def to_display(dt: datetime) -> datetime:
    """시각을 표시 시간대로 바꾼다. 시간대 없는 값은 UTC로 본다(DB 시각 열은 모두 UTC다)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(display_zone())


__all__ = ["display_zone", "get_zone", "to_display"]
