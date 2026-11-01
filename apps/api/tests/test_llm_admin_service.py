"""`services/llm_admin.py` 도메인 규칙 테스트 (R3-3).

라우터에서 내려온 등록 판별 규칙을 HTTP 없이 직접 검증한다.
"""

import uuid
from dataclasses import dataclass

import pytest
from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.services.llm_admin import (
    DefaultModelConflict,
    default_conflict,
    embedding_model_exists,
    plan_model_import,
)
from sqlalchemy.exc import IntegrityError


@dataclass
class Item:
    model_name: str
    display_name: str
    capabilities: list[str]
    context_window: int = 8192


def _model(provider_id, name: str, caps: list[str]) -> LLMModel:
    return LLMModel(
        provider_id=provider_id, model_name=name, display_name=name, capabilities=caps
    )


@pytest.fixture
async def provider(db) -> LLMProvider:
    p = LLMProvider(name=f"p-{uuid.uuid4().hex[:6]}", type="mock", enabled=True)
    db.add(p)
    await db.commit()
    return p


def test_서비스_계층은_fastapi를_임포트하지_않는다() -> None:
    """라우터 씬화의 핵심 — 도메인 로직이 HTTP에 묶이면 재사용도 테스트도 어렵다."""
    import ast
    import inspect

    from biznotes_core.services import llm_admin

    # 경로를 하드코딩하지 않는다 — 모듈이 코어로 이관되면서 경로가 바뀌었고,
    # 하드코딩은 파일이 사라져도 테스트가 조용히 통과할 위험이 있다.
    tree = ast.parse(inspect.getsource(llm_admin))
    mods = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module]
    mods += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not [m for m in mods if "fastapi" in m]


class _DatabaseError(Exception):
    def __init__(self, message: str, constraint_name: str | None = None) -> None:
        self.constraint_name = constraint_name
        super().__init__(message)


def _integrity_error(message: str, constraint_name: str | None = None) -> IntegrityError:
    return IntegrityError("statement", {}, _DatabaseError(message, constraint_name))


@pytest.mark.parametrize(
    ("error", "expected_constraint"),
    [
        (
            _integrity_error("duplicate", "uq_llm_models_default_embedding"),
            "uq_llm_models_default_embedding",
        ),
        (
            _integrity_error(
                'duplicate key violates unique constraint "uq_llm_models_default_llm"'
            ),
            "uq_llm_models_default_llm",
        ),
    ],
)
def test_default_conflict는_default_제약명만_도메인_예외로_분류한다(
    error: IntegrityError, expected_constraint: str
) -> None:
    conflict = default_conflict(error)

    assert isinstance(conflict, DefaultModelConflict)
    assert conflict.constraint_name == expected_constraint


def test_default_conflict는_다른_integrity_error를_분류하지_않는다() -> None:
    error = _integrity_error("foreign key violation", "llm_models_provider_id_fkey")

    assert default_conflict(error) is None


async def test_이미_등록된_model_name은_건너뛴다(db, provider) -> None:
    db.add(_model(provider.id, "gpt-x", ["text"]))
    await db.commit()

    result = await plan_model_import(
        db, provider.id, [Item("gpt-x", "x", ["text"]), Item("gpt-y", "y", ["text"])]
    )

    assert result.skipped == ["gpt-x"]
    assert [m.model_name for m in result.created] == ["gpt-y"]


async def test_임베딩_모델은_전역_1개다(db, provider) -> None:
    db.add(_model(provider.id, "emb-1", ["embedding"]))
    await db.commit()

    result = await plan_model_import(db, provider.id, [Item("emb-2", "e2", ["embedding"])])

    assert result.created == []
    assert result.skipped == ["emb-2"]


async def test_한_배치_안에서도_임베딩은_하나만_받는다(db, provider) -> None:
    """전역 검사만 하면 같은 배치의 두 임베딩이 모두 통과한다 — 배치 내 카운터가 필요하다."""
    result = await plan_model_import(
        db, provider.id, [Item("emb-a", "a", ["embedding"]), Item("emb-b", "b", ["embedding"])]
    )

    assert [m.model_name for m in result.created] == ["emb-a"]
    assert result.skipped == ["emb-b"]
    assert result.created[0].is_default is True, "임베딩은 등록 즉시 default"


async def test_embedding_model_exists는_자기_자신을_제외할_수_있다(db, provider) -> None:
    m = _model(provider.id, "emb", ["embedding"])
    db.add(m)
    await db.commit()

    assert await embedding_model_exists(db) is True
    assert await embedding_model_exists(db, exclude_id=m.id) is False
