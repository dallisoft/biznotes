# 출처: 원본 제품 저장소 `apps/api/tests/test_llm_admin_service.py` @ 1079bbf
#       (sl-agents 사본의 상위집합)
# #1 이관(docs/plans/opt-in-pg-suite/). 필요: PostgreSQL·Valkey(PG 테스트).
# 본문은 원본 그대로 — 바꾼 것: import 정렬(ruff I001).
"""`services/llm_admin.py` 도메인 규칙 테스트 (R3-3).

라우터에서 내려온 등록 판별 규칙을 HTTP 없이 직접 검증한다.
"""

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy.exc import IntegrityError

from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.services.llm_admin import (
    DefaultModelConflict,
    default_conflict,
    embedding_model_exists,
    plan_model_import,
)


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


# --- #109에서 추가 (원본 이관분 아님) ---


async def test_비활성_기본_임베딩이_남아_있으면_새_임베딩은_비기본으로_만든다(db, provider) -> None:
    """교체 중 기존 모델은 비활성이지만 is_default를 쥐고 있다 — 새 모델까지 default면
    커밋에서 `uq_llm_models_default_embedding`과 충돌한다(#109)."""
    old = _model(provider.id, "emb-old", ["embedding"])
    old.enabled = False
    old.is_default = True
    db.add(old)
    await db.commit()

    result = await plan_model_import(db, provider.id, [Item("emb-new", "n", ["embedding"])])

    assert [m.model_name for m in result.created] == ["emb-new"]
    assert result.created[0].is_default is False
    await db.commit()  # 부분 유니크 인덱스와 충돌하지 않는다


async def test_디스커버리_등록으로_임베딩을_교체할_수_있다(db, provider, admin_user) -> None:
    """이슈 재현 순서 그대로: import → 비활성화 → import(이전엔 409) → set-default."""
    from biznotes_core.api.v1.admin.llm import import_models, set_default_model, update_model
    from biznotes_core.schemas.llm import ModelImport, ModelUpdate

    async def _import(name: str) -> dict:
        body = ModelImport.model_validate(
            {"items": [{"model_name": name, "display_name": name, "capabilities": ["embedding"]}]}
        )
        return await import_models(provider.id, body, db, admin_user)

    assert await _import("old-emb") == {"created": 1, "skipped": []}
    old = await _by_name(db, provider.id, "old-emb")
    assert old.is_default is True, "첫 임베딩은 등록 즉시 default"

    await update_model(old.id, ModelUpdate(enabled=False), db, admin_user)
    assert await _import("new-emb") == {"created": 1, "skipped": []}
    new = await _by_name(db, provider.id, "new-emb")
    assert new.is_default is False

    await set_default_model(new.id, db, admin_user)
    await db.refresh(old)
    await db.refresh(new)
    assert (old.is_default, new.is_default) == (False, True)


async def _by_name(db, provider_id: uuid.UUID, name: str) -> LLMModel:
    from sqlalchemy import select

    q = select(LLMModel).where(LLMModel.provider_id == provider_id, LLMModel.model_name == name)
    return (await db.execute(q)).scalar_one()
