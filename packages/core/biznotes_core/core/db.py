from collections.abc import AsyncGenerator
from functools import lru_cache
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from biznotes_core.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    """엔진을 지연 생성한다.

    import 시점에 만들면 프로젝트가 설정 클래스를 등록하기 전에 코어 기본값으로
    DB URL이 굳는다. 코어 모듈은 `app`보다 먼저 import될 수 있으므로(ruff가
    `biznotes_core`를 서드파티로 정렬한다) 지연이 아니라 필수다. 풀 크기·overflow·대기 시간은
    설정(`db_pool_size`·`db_max_overflow`·`db_pool_timeout`)을 따른다(#288).
    """
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        echo=False,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
    )


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


def __getattr__(name: str) -> Any:
    """`engine` / `async_session_factory` 이름 접근 호환 (PEP 562)."""
    if name == "engine":
        return get_engine()
    if name == "async_session_factory":
        return get_session_factory()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with get_session_factory()() as session:
        yield session
