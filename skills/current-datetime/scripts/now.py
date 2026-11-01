"""현재 날짜·시각을 JSON 한 줄로 출력한다 — current-datetime 스킬의 번들 스크립트.

    python3 -I scripts/now.py [IANA 시간대]   # 기본 Asia/Seoul

표준 라이브러리만 쓴다(스킬 샌드박스는 네트워크·추가 패키지가 없다). 시간대 DB(tzdata)가 없는
이미지에서도 기본값(한국 표준시, 서머타임 없음)과 UTC는 고정 오프셋으로 계산한다.
"""

import json
import sys
from datetime import UTC, datetime, timedelta, timezone, tzinfo

DEFAULT_ZONE = "Asia/Seoul"
# 시간대 DB 없이도 계산할 수 있는 이름 — 서머타임이 없어 오프셋이 고정이다
FIXED_ZONES: dict[str, tuple[timedelta, str]] = {
    "Asia/Seoul": (timedelta(hours=9), "KST"),
    "UTC": (timedelta(0), "UTC"),
    "Etc/UTC": (timedelta(0), "UTC"),
}
WEEKDAYS = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")


def resolve_zone(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — 시간대 DB가 없거나 이름이 틀리다
        if name in FIXED_ZONES:
            offset, label = FIXED_ZONES[name]
            return timezone(offset, label)
        raise ValueError(f"알 수 없는 시간대입니다: {name!r} (IANA 이름 예: Asia/Seoul, UTC)") from None


def korean_clock(now: datetime) -> str:
    hour = now.hour % 12 or 12
    meridiem = "오전" if now.hour < 12 else "오후"
    return f"{meridiem} {hour}:{now.minute:02d}:{now.second:02d}"


def describe(now_utc: datetime, zone_name: str) -> dict[str, object]:
    now = now_utc.astimezone(resolve_zone(zone_name))
    label = now.tzname() or zone_name
    weekday = WEEKDAYS[now.weekday()]
    return {
        "timezone": zone_name,
        "abbreviation": label,
        "display": f"{now.year}년 {now.month}월 {now.day}일 {weekday} {korean_clock(now)} {label}",
        "date": now.date().isoformat(),
        "time": now.strftime("%H:%M:%S"),
        "weekday": weekday,
        "iso": now.isoformat(timespec="seconds"),
        "utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "unix": int(now_utc.timestamp()),
    }


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print(json.dumps({"error": "인자는 시간대 이름 하나만 받습니다"}, ensure_ascii=False))
        return 2
    zone_name = argv[1].strip() if len(argv) == 2 and argv[1].strip() else DEFAULT_ZONE
    try:
        result = describe(datetime.now(UTC).replace(microsecond=0), zone_name)
    except ValueError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
