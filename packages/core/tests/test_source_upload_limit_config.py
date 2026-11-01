"""소스 업로드 최대 크기 설정(#388) — 기본값·하드 상한·기동 시 검증·문구.

업로드 라우터의 판정은 `tests/test_sources_upload_admission.py`, 관리자 API는
`tests/test_source_upload_settings_pg.py`가 본다.
"""

import pytest
from conftest import make_settings
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from biznotes_core import config
from biznotes_core.config import SOURCE_MAX_UPLOAD_BYTES_CEILING, CoreSettings
from biznotes_core.schemas.limits import SourceUploadSettingsUpdate
from biznotes_core.services.ingestion import admission
from biznotes_core.services.ingestion.admission import (
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_BYTES_CEILING,
    AdmissionError,
    admit_bytes,
    too_large_message,
)

MIB = 1024 * 1024


class _EnvOnly(CoreSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")


def test_default_is_unchanged_50_mib() -> None:
    assert MAX_UPLOAD_BYTES == 50 * MIB  # 공개 심볼은 기본값 의미로 남는다
    assert CoreSettings.model_fields["source_max_upload_bytes"].default == MAX_UPLOAD_BYTES
    assert MAX_UPLOAD_BYTES_CEILING == SOURCE_MAX_UPLOAD_BYTES_CEILING == 500 * MIB


def test_env_value_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_MAX_UPLOAD_BYTES", str(200 * MIB))
    assert _EnvOnly().source_max_upload_bytes == 200 * MIB


@pytest.mark.parametrize("value", [SOURCE_MAX_UPLOAD_BYTES_CEILING + 1, 0, -1])
def test_out_of_range_env_refuses_to_start(monkeypatch: pytest.MonkeyPatch, value: int) -> None:
    monkeypatch.setenv("SOURCE_MAX_UPLOAD_BYTES", str(value))
    with pytest.raises(ValidationError, match="source_max_upload_bytes"):
        _EnvOnly()


def test_ceiling_itself_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_MAX_UPLOAD_BYTES", str(SOURCE_MAX_UPLOAD_BYTES_CEILING))
    assert _EnvOnly().source_max_upload_bytes == SOURCE_MAX_UPLOAD_BYTES_CEILING


@pytest.mark.parametrize("value", [0, -1, SOURCE_MAX_UPLOAD_BYTES_CEILING + 1])
def test_admin_update_schema_rejects_out_of_range(value: int) -> None:
    with pytest.raises(ValidationError):
        SourceUploadSettingsUpdate(max_upload_bytes=value)


def test_too_large_message_reflects_limit() -> None:
    assert too_large_message(MAX_UPLOAD_BYTES) == "파일이 50MB를 초과합니다"
    assert too_large_message(200 * MIB) == "파일이 200MB를 초과합니다"
    assert too_large_message(int(1.5 * MIB)) == "파일이 1.5MB를 초과합니다"
    assert too_large_message(512 * 1024) == "파일이 512KB를 초과합니다"
    assert too_large_message(1000) == "파일이 1,000바이트를 초과합니다"


def test_admit_bytes_defaults_to_env_setting() -> None:
    """커넥터 경로(`max_bytes` 없음)는 env 상한을 쓴다."""
    settings = make_settings(source_max_upload_bytes=10)
    admit_bytes(b"a" * 10, "text/plain", settings=settings)
    with pytest.raises(AdmissionError) as exc:
        admit_bytes(b"a" * 11, "text/plain", settings=settings)
    assert exc.value.code == "too_large"
    assert exc.value.message == "파일이 10바이트를 초과합니다"


def test_admit_bytes_explicit_limit_wins() -> None:
    settings = make_settings(source_max_upload_bytes=10)
    admit_bytes(b"a" * 20, "text/plain", settings=settings, max_bytes=20)
    with pytest.raises(AdmissionError):
        admit_bytes(b"a" * 21, "text/plain", settings=settings, max_bytes=20)


def test_ceiling_lives_in_config_without_service_import() -> None:
    """config는 서비스 계층을 import하지 않는다 — 하드 상한의 정본은 config다."""
    assert admission.MAX_UPLOAD_BYTES_CEILING is config.SOURCE_MAX_UPLOAD_BYTES_CEILING
