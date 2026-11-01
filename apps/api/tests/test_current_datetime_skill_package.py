"""현재 날짜·시각 스킬 패키지(`skills/current-datetime/`) 계약.

- 디렉터리 파일이 코어 `validate_files`·`evaluate_package`를 통과하고, 표준 런타임에서
  `standard_agent_v1`로 판정된다(제품 매니페스트가 없어야 번들 스크립트를 실행할 수 있다).
- zip 안 파일 목록·바이트 == 디렉터리.
- `scripts/now.py`: 고정 시각의 표시 문자열·시간대 처리·오류 종료 코드, 시간대 DB가 없을 때의
  고정 오프셋 대체.
"""

import importlib.util
import json
import subprocess
import sys
import zipfile
import zoneinfo
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from biznotes_core.config import get_settings
from biznotes_core.services.skills.report import evaluate_package
from biznotes_core.services.skills.validator import validate_files

ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = ROOT / "skills/current-datetime"
SKILL_ZIP = ROOT / "skills/current-datetime.zip"
FILES = ("SKILL.md", "scripts/now.py")
SCRIPT = SKILL_DIR / "scripts/now.py"


def _files() -> dict[str, bytes]:
    return {name: (SKILL_DIR / name).read_bytes() for name in FILES}


def _now_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("current_datetime_now", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_package_files_and_standard_validation():
    found = sorted(
        str(p.relative_to(SKILL_DIR))
        for p in SKILL_DIR.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )
    assert found == sorted(FILES)
    validate_files(_files())
    evaluation = evaluate_package(_files())
    assert evaluation.name == "current-datetime"
    assert evaluation.has_product_manifest is False
    assert evaluation.report["standard"]["errors"] == []
    assert evaluation.report["portability"]["warnings"] == []


def test_standard_runtime_detects_bundled_script(monkeypatch):
    """표준 런타임이 켜지면 `standard_agent_v1`이고 번들 스크립트가 있다고 본다."""
    monkeypatch.setattr(get_settings(), "skill_standard_runtime_enabled", True)
    evaluation = evaluate_package(_files())
    assert evaluation.runtime_profile == "standard_agent_v1"
    codes = [w["code"] for w in evaluation.report["capability"]["warnings"]]
    assert "no_bundled_scripts" not in codes
    assert "instruction_cannot_execute_code" not in codes


def test_zip_matches_directory():
    with zipfile.ZipFile(SKILL_ZIP) as zf:
        entries = {
            info.filename.removeprefix("current-datetime/"): zf.read(info)
            for info in zf.infolist()
            if not info.is_dir()
        }
    assert entries == _files()


def test_describe_fixed_instant_in_kst():
    now = _now_module()
    result = now.describe(datetime(2026, 10, 26, 16, 0, 5, tzinfo=UTC), "Asia/Seoul")
    assert result == {
        "timezone": "Asia/Seoul",
        "abbreviation": "KST",
        "display": "2026년 10월 27일 화요일 오전 1:00:05 KST",
        "date": "2026-10-27",
        "time": "01:00:05",
        "weekday": "화요일",
        "iso": "2026-10-27T01:00:05+09:00",
        "utc": "2026-10-26T16:00:05Z",
        "unix": 1793030405,
    }


def test_describe_other_zone_and_afternoon_clock():
    now = _now_module()
    result = now.describe(datetime(2026, 10, 28, 4, 30, 0, tzinfo=UTC), "UTC")
    assert result["display"] == "2026년 10월 28일 수요일 오전 4:30:00 UTC"
    noon = now.describe(datetime(2026, 10, 28, 3, 0, 0, tzinfo=UTC), "Asia/Seoul")
    assert noon["display"].endswith("오후 12:00:00 KST")
    ny = now.describe(datetime(2026, 10, 28, 2, 0, 0, tzinfo=UTC), "America/New_York")
    assert ny["iso"] == "2026-10-27T22:00:00-04:00"
    assert ny["weekday"] == "화요일"


def test_fixed_offset_fallback_without_timezone_db(monkeypatch):
    """시간대 DB가 없는 이미지에서도 기본값(KST)과 UTC는 계산하고, 그 밖은 오류다."""

    def _no_db(name: str):
        raise zoneinfo.ZoneInfoNotFoundError(name)

    monkeypatch.setattr(zoneinfo, "ZoneInfo", _no_db)
    now = _now_module()
    kst = now.resolve_zone("Asia/Seoul")
    assert kst.utcoffset(None) == timedelta(hours=9)
    assert kst.tzname(None) == "KST"
    assert now.resolve_zone("UTC").utcoffset(None) == timedelta(0)
    with pytest.raises(ValueError, match="알 수 없는 시간대"):
        now.resolve_zone("Europe/London")


def test_script_default_is_kst_json_line():
    res = _run()
    assert res.returncode == 0, res.stderr
    body = json.loads(res.stdout)
    assert body["timezone"] == "Asia/Seoul"
    assert body["iso"].endswith("+09:00")
    utc = datetime.strptime(body["utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert datetime.fromisoformat(body["iso"]) == utc
    assert abs((datetime.now(UTC) - utc).total_seconds()) < 60


@pytest.mark.parametrize(
    ("args", "message"),
    [(("Mars/Base",), "알 수 없는 시간대"), (("UTC", "extra"), "인자는 시간대 이름 하나만")],
)
def test_script_errors_exit_2(args, message):
    res = _run(*args)
    assert res.returncode == 2
    assert message in json.loads(res.stdout)["error"]
