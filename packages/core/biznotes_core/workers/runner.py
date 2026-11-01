"""워커 프로세스 공용 이벤트 루프 + DB 엔진.

Celery 태스크(동기)에서 코루틴을 실행할 때 기존에는 태스크마다
asyncio.run + NullPool 엔진을 새로 만들었다 (연결 풀이 매번 버려짐).
prefork 자식 프로세스당 루프/엔진을 1회만 만들어 재사용한다 —
루프가 태스크 사이에 살아있으므로 커넥션 풀도 유지된다.

주의: prefork/solo 풀 전제 (프로세스당 단일 스레드에서 태스크 순차 실행).
threads 풀로 바꾸면 루프 공유가 깨지므로 사용 금지.
엔진은 fork 이후 첫 태스크에서 lazy 생성 — 부모 프로세스와 연결 공유 없음.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from biznotes_core.config import get_settings

_loop: asyncio.AbstractEventLoop | None = None
_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """프로세스 공용 루프에서 코루틴 실행 (asyncio.run 대체)."""
    return get_loop().run_until_complete(coro)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """프로세스당 1회 생성되는 엔진/세션 팩토리 (공용 루프에서만 사용)."""
    global _engine, _factory
    if _factory is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url, pool_pre_ping=True, pool_size=5, max_overflow=5
        )
        _factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _factory
