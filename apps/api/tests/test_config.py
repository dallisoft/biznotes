import pathlib
import re

import pytest

from app.core.config import (
    INSECURE_DEFAULTS,
    Settings,
    assert_prod_secrets,
    find_insecure_defaults,
)


def test_database_url_assembled():
    s = Settings(
        postgres_host="db",
        postgres_port=5433,
        postgres_db="x",
        postgres_user="u",
        postgres_password="p",
    )
    assert s.database_url == "postgresql+asyncpg://u:p@db:5433/x"


def test_cors_origin_list_parses_csv():
    s = Settings(cors_origins="http://a.com, http://b.com")
    assert s.cors_origin_list == ["http://a.com", "http://b.com"]


_SECURE_OVERRIDES = {
    "jwt_secret": "real-secret",
    "encryption_key": "real-32-bytes-encryption-key!!",
    "postgres_password": "real-pg-pw",
    "s3_access_key": "real-access",
    "s3_secret_key": "real-secret-key",
}


def test_prod_with_default_secrets_refuses_boot():
    # conftest가 JWT_SECRET 등 환경변수를 주입하므로 기본값을 명시적으로 지정
    s = Settings(app_env="prod", _env_file=None, **INSECURE_DEFAULTS)
    with pytest.raises(RuntimeError) as exc:
        assert_prod_secrets(s)
    msg = str(exc.value)
    assert "JWT_SECRET" in msg and "ENCRYPTION_KEY" in msg


def test_prod_with_overridden_secrets_boots():
    s = Settings(app_env="prod", _env_file=None, **_SECURE_OVERRIDES)
    assert_prod_secrets(s)  # 예외 없음


def test_partial_default_lists_only_missing():
    overrides = {**_SECURE_OVERRIDES}
    overrides.pop("s3_secret_key")
    s = Settings(app_env="prod", storage_backend="s3", _env_file=None, **overrides)
    assert find_insecure_defaults(s) == ["s3_secret_key"]


def test_local_storage_does_not_need_s3_credentials():
    """기본 로컬 스토리지는 S3 자격 증명을 쓰지 않는다 — 비워 둬도 prod가 뜬다."""
    overrides = {
        k: v for k, v in _SECURE_OVERRIDES.items() if k not in ("s3_access_key", "s3_secret_key")
    }
    s = Settings(app_env="prod", storage_backend="local", _env_file=None, **overrides)
    assert find_insecure_defaults(s) == []
    assert_prod_secrets(s)


def test_s3_storage_with_default_credentials_refuses_boot():
    overrides = {
        k: v for k, v in _SECURE_OVERRIDES.items() if k not in ("s3_access_key", "s3_secret_key")
    }
    s = Settings(app_env="prod", storage_backend="s3", _env_file=None, **overrides)
    with pytest.raises(RuntimeError, match="S3_ACCESS_KEY"):
        assert_prod_secrets(s)


def test_non_prod_passes_with_defaults():
    assert_prod_secrets(Settings(app_env="dev", _env_file=None))  # 예외 없음


def test_published_dev_secrets_are_the_guarded_defaults():
    """compose 기본값과 .env.example의 시크릿이 prod 가드가 비교하는 기본값과 같다.

    다르면 저장소에 공개된 개발 값으로 APP_ENV=prod가 가드를 통과해 뜬다(JWT 위조 가능).
    """
    root = pathlib.Path(__file__).resolve().parents[3]
    compose = (root / "infra/compose.yml").read_text(encoding="utf-8")
    example = (root / "infra/.env.example").read_text(encoding="utf-8")
    for field in ("jwt_secret", "encryption_key", "postgres_password"):
        env = field.upper()
        in_compose = re.findall(rf"{env}: \$\{{{env}:-([^}}]*)\}}", compose)
        in_example = re.findall(rf"^{env}=(.*)$", example, re.M)
        assert in_compose and set(in_compose) == {INSECURE_DEFAULTS[field]}, field
        assert in_example == [INSECURE_DEFAULTS[field]], field
