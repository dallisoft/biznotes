"""`llm_providers.embedding_max_concurrency` 계약과 메타데이터(#196).

계획 `docs/plans/embedding-concurrency-limit/plan.md`의 AC7·AC8을 확인한다.
DB 세션은 없고 스키마와 메타데이터만 본다 — 마이그레이션은 소비 제품이 소유한다(AGENTS.md §2).
"""

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from biznotes_core.models import Base, LLMProvider
from biznotes_core.schemas.llm import EmbeddingGateOut, ProviderCreate, ProviderOut, ProviderUpdate


def _provider(**kw: Any) -> LLMProvider:
    # `enabled`는 컬럼 기본값이라 flush 전 인스턴스에는 없다 — ORM→DTO 매퍼를 보려면 명시한다
    return LLMProvider(
        id=uuid.uuid4(), name="p", type="ollama", api_key_encrypted=None, enabled=True, **kw
    )


# ── AC7: 스키마 ────────────────────────────────────────────────────────────


def test_create_accepts_limit_and_defaults_to_unset() -> None:
    assert ProviderCreate(name="p", type="ollama").embedding_max_concurrency is None
    assert ProviderCreate(name="p", type="ollama", embedding_max_concurrency=1).\
        embedding_max_concurrency == 1


@pytest.mark.parametrize("value", [0, -1, 65, 1000])
def test_create_rejects_out_of_range(value: int) -> None:
    # 0과 음수는 "무제한"으로 읽히면 안 된다 — 무제한은 NULL이다
    with pytest.raises(ValidationError):
        ProviderCreate(name="p", type="ollama", embedding_max_concurrency=value)


@pytest.mark.parametrize("value", [0, -1, 65])
def test_update_rejects_out_of_range(value: int) -> None:
    with pytest.raises(ValidationError):
        ProviderUpdate(embedding_max_concurrency=value)


def test_update_distinguishes_absent_from_explicit_null() -> None:
    # 값이 None이어도 "본문에 키가 있느냐"로 구분한다 — 없으면 바꾸지 않고, 있으면 NULL로 되돌린다
    assert "embedding_max_concurrency" not in ProviderUpdate(enabled=False).model_fields_set
    explicit_null = ProviderUpdate(embedding_max_concurrency=None)
    assert "embedding_max_concurrency" in explicit_null.model_fields_set


def test_out_field_keeps_default_so_older_constructors_work() -> None:
    # `schemas/`는 공유 계약 — 새 필드가 필수면 외부에서 만드는 `ProviderOut(...)`가 깨진다
    out = ProviderOut(
        id=uuid.uuid4(), name="p", type="ollama", base_url=None, has_api_key=False, enabled=True
    )
    assert out.embedding_max_concurrency is None
    assert out.model_count == 0


def test_provider_to_out_maps_the_column() -> None:
    from biznotes_core.schemas.llm import provider_to_out

    assert provider_to_out(_provider(embedding_max_concurrency=4)).embedding_max_concurrency == 4
    assert provider_to_out(_provider()).embedding_max_concurrency is None


def test_gate_out_carries_no_waiter_field() -> None:
    # 대기자는 ZSET에 남지 않으므로 대기 수를 노출할 수 없다(계획 §3.9)
    fields = set(EmbeddingGateOut.model_fields)
    assert fields == {"id", "name", "limit", "in_use", "earliest_expiry_in", "last_error"}


# ── AC8: 메타데이터 ────────────────────────────────────────────────────────


def test_column_is_nullable_integer() -> None:
    col = LLMProvider.__table__.c.embedding_max_concurrency
    assert col.nullable is True
    assert str(col.type) == "INTEGER"
    assert col.default is None


def test_existing_columns_and_indexes_are_untouched() -> None:
    existing = {"id", "name", "type", "base_url", "api_key_encrypted", "enabled", "created_at",
                "updated_at"}
    assert set(LLMProvider.__table__.c.keys()) == existing | {"embedding_max_concurrency"}
    assert LLMProvider.__table__.c.name.unique is True
    # 인덱스는 새로 생기지 않았다 — 컬럼 하나만 늘어난다
    assert {i.name for i in LLMProvider.__table__.indexes} == set()


def test_column_is_registered_on_base_metadata() -> None:
    table = Base.metadata.tables["llm_providers"]
    assert "embedding_max_concurrency" in table.c