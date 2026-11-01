"""agents.handle 컬럼과 네임스페이스별 partial unique index 선언 검증 (issue #31).

코어에는 Alembic 리비전이 없으므로, 소비 제품 리비전(sl-agents f7a9c1b3d5e2)과
맞춰야 하는 선언을 오프라인 메타데이터·DDL 컴파일로 고정한다. 실제 PostgreSQL
적용 검증은 소비 제품 마이그레이션이 담당한다.
"""

from sqlalchemy import String
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from biznotes_core.models import Agent

OWNER_IX = "uq_agents_handle_owner"
SYSTEM_IX = "uq_agents_handle_system"


def _index(name: str):
    found = [ix for ix in Agent.__table__.indexes if ix.name == name]
    assert found, f"{name} 인덱스가 agents 테이블에 없다"
    return found[0]


def _ddl(name: str) -> str:
    return str(CreateIndex(_index(name)).compile(dialect=postgresql.dialect()))


def test_handle_column_is_nullable_varchar64() -> None:
    """표시 이름과 분리된 호출 키. 기존 행 호환을 위해 nullable이어야 한다."""
    col = Agent.__table__.c.handle
    assert col.nullable is True
    assert isinstance(col.type, String)
    assert col.type.length == 64


def test_handle_column_is_independent_of_name() -> None:
    """name(표시 이름)은 그대로 NOT NULL로 남는다 — handle 추가가 기존 계약을 바꾸지 않는다."""
    assert Agent.__table__.c.name.nullable is False
    assert Agent.__table__.c.name is not Agent.__table__.c.handle


def test_owner_namespace_index_is_partial_unique() -> None:
    ix = _index(OWNER_IX)
    assert ix.unique is True
    assert [c.name for c in ix.columns] == ["owner_id", "handle"]
    ddl = _ddl(OWNER_IX)
    assert "CREATE UNIQUE INDEX" in ddl
    assert "WHERE owner_id IS NOT NULL AND handle IS NOT NULL" in ddl


def test_system_namespace_index_is_partial_unique() -> None:
    ix = _index(SYSTEM_IX)
    assert ix.unique is True
    assert [c.name for c in ix.columns] == ["handle"]
    ddl = _ddl(SYSTEM_IX)
    assert "CREATE UNIQUE INDEX" in ddl
    assert "WHERE scope = 'system' AND owner_id IS NULL AND handle IS NOT NULL" in ddl


def test_orphan_rows_fall_outside_both_predicates() -> None:
    """owner 삭제(ON DELETE SET NULL)로 생긴 고아 행은 어느 predicate에도 걸리지 않는다.

    owner predicate는 owner_id IS NOT NULL을, system predicate는 scope = 'system'을
    요구하므로 (owner_id IS NULL, scope = 'user') 조합은 양쪽 모두에서 빠진다.
    이 설계가 깨지면 사용자 삭제가 unique 위반으로 실패한다.
    """
    owner_where = _ddl(OWNER_IX).split("WHERE", 1)[1].strip()
    system_where = _ddl(SYSTEM_IX).split("WHERE", 1)[1].strip()
    assert "owner_id IS NOT NULL" in owner_where
    assert "scope = 'system'" in system_where
    assert "owner_id IS NULL" in system_where


def test_handle_has_no_table_level_unique_constraint() -> None:
    """전역 unique는 네임스페이스 분리를 깨뜨린다 — partial index로만 강제해야 한다."""
    assert Agent.__table__.c.handle.unique is not True
    for constraint in Agent.__table__.constraints:
        cols = {c.name for c in getattr(constraint, "columns", [])}
        assert cols != {"handle"}, "handle에 테이블 수준 unique 제약이 붙었다"
