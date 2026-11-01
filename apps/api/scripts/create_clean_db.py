"""마이그레이션 검증용 clean DB 재생성 (drop + create).

사용: uv run python -m scripts.create_clean_db <db_name>
R0-3 게이트(clean DB `alembic upgrade head` + `alembic check`)의 전처리 단계로,
로컬(make migrate-check)과 CI가 동일하게 사용한다.
"""

import asyncio
import re
import sys

import asyncpg

from app.core.config import get_settings


async def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "biznotes_migrate"
    if not re.fullmatch(r"[a-zA-Z0-9_]+", name):
        raise SystemExit(f"invalid database name: {name!r}")

    settings = get_settings()
    conn = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password,
        database="postgres",
    )
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    print(f"clean database ready: {name}")


if __name__ == "__main__":
    asyncio.run(main())
