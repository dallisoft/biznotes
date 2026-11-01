"""API 엔진의 연결 풀 설정 (#288).

엔진만 만들고 연결하지 않는다. `get_engine`·`get_session_factory`·설정은 `lru_cache`라
앞뒤로 비운다.
"""

from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from biznotes_core.config import CoreSettings, reset_settings
from biznotes_core.core import db


@pytest.fixture
def fresh_engine() -> Iterator[None]:
    def clear() -> None:
        reset_settings()
        db.get_session_factory.cache_clear()
        db.get_engine.cache_clear()

    clear()
    yield
    clear()


def _pool() -> tuple[int, int, float]:
    pool = db.get_engine().pool
    return pool.size(), pool._max_overflow, pool._timeout  # type: ignore[attr-defined]


def test_defaults_match_sqlalchemy_defaults(fresh_engine: None) -> None:
    settings = CoreSettings()
    assert (settings.db_pool_size, settings.db_max_overflow, settings.db_pool_timeout) == (
        5,
        10,
        30.0,
    )
    assert _pool() == (5, 10, 30.0)


def test_engine_uses_pool_settings(
    fresh_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "2")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "0")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "4.5")
    reset_settings()

    assert _pool() == (2, 0, 4.5)


@pytest.mark.parametrize(
    "bad",
    [{"db_pool_size": 0}, {"db_max_overflow": -1}, {"db_pool_timeout": 0}],
    ids=["size", "overflow", "timeout"],
)
def test_pool_settings_are_validated(bad: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        CoreSettings(**bad)
