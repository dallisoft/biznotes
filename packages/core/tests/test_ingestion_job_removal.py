"""`IngestionJob`·`ingestion_jobs` 제거 (#168, #163 3단계).

1단계(#163)에서 쓰기를 멈추고 deprecation 별칭을 두었던 모델을 지웠다. 테이블은 두 제품의
drop 리비전이 지운다 — 코어 `Base.metadata`에 남아 있으면 제품 `alembic check`가 드리프트를 본다.
"""

import ast
import pathlib
import re
import uuid

import pytest

import biznotes_core.models as models
from biznotes_core.models import Base
from biznotes_core.models import source as source_module


def test_table_is_gone_from_metadata() -> None:
    assert "ingestion_jobs" not in Base.metadata.tables
    # 같은 이름을 쓰는 인덱스도 남지 않는다 — drop 리비전 뒤 `alembic check`가 깨끗해야 한다
    assert not any(
        index.name == "ix_ingestion_jobs_source_id"
        for table in Base.metadata.tables.values()
        for index in table.indexes
    )


def test_public_and_private_names_are_gone() -> None:
    assert "IngestionJob" not in models.__all__
    for module in (models, source_module):
        for name in ("IngestionJob", "_IngestionJob", "_deprecated_ingestion_job"):
            assert not hasattr(module, name), f"{module.__name__}.{name}"

    with pytest.raises(ImportError):
        from biznotes_core.models import IngestionJob  # noqa: F401
    with pytest.raises(ImportError):
        from biznotes_core.models.source import IngestionJob  # noqa: F401, F811


def test_star_import_no_longer_exports_the_name() -> None:
    namespace: dict[str, object] = {}
    exec("from biznotes_core.models import *", namespace)

    assert "IngestionJob" not in namespace


def test_core_never_references_the_ingestion_job_model() -> None:
    """발행 경로(upload·create·retry·capture·register_source·reindex·admin rag)는 모두
    `stage_ingest_job`을 거친다. 코어 어디에도 모델 이름이나, 독스트링이 아닌 문자열(원시 SQL 등)
    안의 테이블 이름이 남지 않았는지 AST로 확인한다 — 제품이 테이블을 지우면
    `UndefinedTable`이 된다."""
    table_name = re.compile(r"\bingestion_jobs\b")
    root = pathlib.Path(models.__file__).parent.parent
    offenders = []
    for path in root.rglob("*.py"):
        rel = str(path.relative_to(root))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            name = getattr(node, "id", None) or getattr(node, "attr", None)
            if isinstance(node, ast.alias):
                name = node.name
            if name in {"IngestionJob", "_IngestionJob"}:
                offenders.append(f"{rel}:{node.lineno}")
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and table_name.search(node.value)
            ):
                offenders.append(f"{rel}:{node.lineno}")

    assert offenders == []


def test_stage_ingest_job_rejects_unflushed_source() -> None:
    """job 행의 NOT NULL FK가 하던 fail-loud 검사를 대신한다 — `"None"`을 발행하지 않는다."""
    from biznotes_core.models import Source
    from biznotes_core.services.ingestion import enqueue

    callbacks: list[object] = []
    uow = type("Uow", (), {"after_commit": lambda self, fn: callbacks.append(fn)})()

    with pytest.raises(ValueError, match="flush"):
        enqueue.stage_ingest_job(None, Source(type="text", title="s"), uow)  # type: ignore[arg-type]
    assert callbacks == []

    enqueue.stage_ingest_job(None, Source(id=uuid.uuid4(), type="text", title="s"), uow)  # type: ignore[arg-type]
    assert len(callbacks) == 1
