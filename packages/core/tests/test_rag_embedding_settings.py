"""`rag` 설정의 임베딩 배치 크기·타임아웃 키 (#194).

기본값은 지금 상수와 같다. PUT은 새 키를 본문에 있을 때만 저장한다 — 이 키를 모르는
클라이언트가 운영자가 정한 값을 기본값으로 되돌리지 않게 한다. 저장소(`set_setting`)와
감사 기록만 기록형 가짜다.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from biznotes_core.api.v1.admin import rag
from biznotes_core.services import embedding_spaces as es
from biznotes_core.services.settings_store import DEFAULTS

BASE = {"chunk_size": 800, "chunk_overlap": 100, "top_k": 6}
ADMIN = SimpleNamespace(id=uuid.uuid4())


def test_defaults_match_gateway_constants() -> None:
    assert DEFAULTS["rag"]["embedding_batch_size"] == es.GATEWAY_BATCH_SIZE
    assert DEFAULTS["rag"]["embedding_timeout_seconds"] == es.GATEWAY_TIMEOUT_SECONDS
    body = rag.RagSettings(**BASE)
    assert body.embedding_batch_size == es.GATEWAY_BATCH_SIZE
    assert body.embedding_timeout_seconds == es.GATEWAY_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "field,value",
    [
        ("embedding_batch_size", 0),
        ("embedding_batch_size", 2049),
        ("embedding_timeout_seconds", 4),
        ("embedding_timeout_seconds", 181),
    ],
)
def test_ranges(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        rag.RagSettings(**BASE, **{field: value})


class _DB:
    async def commit(self) -> None:
        return None


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    stored: dict[str, Any] = {**DEFAULTS["rag"], "embedding_batch_size": 8}
    writes: list[dict[str, Any]] = []

    async def set_setting(db: Any, key: str, value: dict[str, Any]) -> dict[str, Any]:
        writes.append(dict(value))
        stored.update(value)
        return dict(stored)

    async def record(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(rag, "set_setting", set_setting)
    monkeypatch.setattr(rag, "record", record)
    return writes


async def test_put_without_new_keys_keeps_stored_values(saved: list[dict[str, Any]]) -> None:
    out = await rag.update_rag_settings(rag.RagSettings(**BASE), db=_DB(), admin=ADMIN)  # type: ignore[arg-type]

    assert "embedding_batch_size" not in saved[0]
    assert "embedding_timeout_seconds" not in saved[0]
    assert out.embedding_batch_size == 8


async def test_put_with_new_keys_saves_them(saved: list[dict[str, Any]]) -> None:
    body = rag.RagSettings(**BASE, embedding_batch_size=16, embedding_timeout_seconds=120)

    out = await rag.update_rag_settings(body, db=_DB(), admin=ADMIN)  # type: ignore[arg-type]

    assert saved[0]["embedding_batch_size"] == 16
    assert saved[0]["embedding_timeout_seconds"] == 120
    assert out.embedding_batch_size == 16
