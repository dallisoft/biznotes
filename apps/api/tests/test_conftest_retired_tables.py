"""conftest `drop_retired_tables` — 코어가 메타데이터에서 뺀 옛 테이블 정리(AC-1.5, HCI-13).

공유 테스트 DB `biznotes_test`에는 코어 v0.14.0(#168) 전의 `ingestion_jobs`(FK → `sources`)가
남아 있을 수 있다. `Base.metadata.drop_all`은 그 테이블을 모르므로 직렬 실행에서 `sources`
삭제가 FK 의존으로 실패한다. 세션 픽스처 `_setup_db`가 `drop_all` 전에 이 헬퍼를 부른다.

여기서는 워커 DB에 FK 달린 가짜 옛 테이블을 만들어 헬퍼가 지우는지, 그 뒤 `drop_all`이
통과하는지, 테스트 DB가 아니면 아무것도 지우지 않고 거부하는지를 고정한다. 모든 DDL은 한
트랜잭션 안에서 실행하고 롤백하므로 워커 DB 스키마는 그대로 남는다.
"""

import pytest
from biznotes_core.core.db import engine
from biznotes_core.models import Base
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.conftest import RETIRED_TABLES, drop_retired_tables, settings

LEGACY_TABLE_SQL = (
    "CREATE TABLE ingestion_jobs (id uuid PRIMARY KEY,"
    " source_id uuid NOT NULL REFERENCES sources(id) ON DELETE CASCADE)"
)
FK_TO_SOURCES_SQL = (
    "SELECT count(*) FROM pg_constraint WHERE contype = 'f'"
    " AND confrelid = 'sources'::regclass AND conrelid = to_regclass('ingestion_jobs')"
)


async def test_FK_달린_옛_ingestion_jobs를_지워_drop_all이_통과한다() -> None:
    assert RETIRED_TABLES == ("ingestion_jobs",)
    async with engine.connect() as conn:
        tx = await conn.begin()
        try:
            await conn.execute(text(LEGACY_TABLE_SQL))
            assert (await conn.execute(text(FK_TO_SOURCES_SQL))).scalar_one() == 1

            # 헬퍼 없이는 직렬 실행의 실패 그대로 — `sources` 삭제가 옛 FK에 막힌다
            nested = await conn.begin_nested()
            with pytest.raises(DBAPIError, match="ingestion_jobs_source_id_fkey"):
                await conn.run_sync(Base.metadata.drop_all)
            await nested.rollback()

            await drop_retired_tables(conn, settings.postgres_db)
            regclass = await conn.execute(text("SELECT to_regclass('ingestion_jobs')"))
            assert regclass.scalar_one() is None
            await conn.run_sync(Base.metadata.drop_all)
            assert (await conn.execute(text("SELECT to_regclass('sources')"))).scalar_one() is None
        finally:
            await tx.rollback()


async def test_테스트_DB가_아니면_아무것도_지우지_않고_거부한다() -> None:
    async with engine.connect() as conn:
        tx = await conn.begin()
        try:
            await conn.execute(text(LEGACY_TABLE_SQL))
            with pytest.raises(RuntimeError, match="drop_all 전에 중단합니다"):
                await drop_retired_tables(conn, "biznotes")
            regclass = await conn.execute(text("SELECT to_regclass('ingestion_jobs')"))
            assert regclass.scalar_one() == "ingestion_jobs"
        finally:
            await tx.rollback()
