"""biznotes 설정 — 필드 선언은 `biznotes_core.config.CoreSettings`에 있다.

여기에는 이 제품 고유의 값만 둔다. 공통 필드를 추가할 일이 생기면 코어에
추가해 두 제품이 함께 갖도록 한다.
"""

from biznotes_core.config import (
    VALKEY_URL_ALIASES,
    CoreSettings,
    assert_prod_secrets,
    find_insecure_defaults,
    get_settings,
    insecure_defaults_for,
    set_settings_class,
)
from pydantic import Field


class Settings(CoreSettings):
    """biznotes 식별자와 개발 스택 주소(호스트 포트 2xxxx)."""

    api_base_url: str = "http://localhost:28000"
    web_base_url: str = "http://localhost:23000"
    cors_origins: str = "http://localhost:23000"

    postgres_db: str = "biznotes"
    postgres_user: str = "biznotes"
    postgres_password: str = "biznotes"
    # 시크릿 기본값은 저장소가 공개한 개발 값(infra/compose.yml 기본값·infra/.env.example)과
    # 같아야 한다. prod 기동 가드(`assert_prod_secrets`)가 이 클래스의 기본값과 비교하므로, 다르면
    # 공개된 값으로 APP_ENV=prod가 뜬다(test_config의 대조 테스트가 막는다).
    jwt_secret: str = "dev-jwt-secret"
    encryption_key: str = "dev-encryption-key-32-bytes-pad!"

    redis_url: str = Field(
        default="redis://localhost:26379/0", validation_alias=VALKEY_URL_ALIASES
    )
    celery_app_name: str = "biznotes"
    # 아직 코어로 오지 않은 태스크 모듈 — worker가 import해야 등록된다.
    celery_extra_task_modules: list[str] = ["app.workers.tasks.agent"]
    # PostgreSQL advisory lock 네임스페이스 접두사로도 쓰인다 — 값을 바꾸면 배포 중 옛·새
    # 프로세스의 락 네임스페이스가 갈라지므로 v0.1.0의 값을 유지한다.
    service_name: str = "biznotes"
    skill_manifest_filename: str = "biznotes.json"
    searxng_url: str = "http://localhost:28080"


set_settings_class(Settings)

#: prod에서 기본값 그대로 기동하면 안 되는 시크릿 (필드명 → 이 제품의 기본값).
INSECURE_DEFAULTS: dict[str, str] = insecure_defaults_for(Settings)

__all__ = [
    "INSECURE_DEFAULTS",
    "Settings",
    "assert_prod_secrets",
    "find_insecure_defaults",
    "get_settings",
]
