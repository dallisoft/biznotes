# 신규(#359 2/2). 필요: PostgreSQL·Valkey(PG 테스트).
"""run 생성 때 고른 모델 — 검증·저장·모델 삭제 시 SET NULL.

Celery 발행만 대역이다. `create_all` 기반이라 Alembic 마이그레이션 검증이 아니다.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text

from biznotes_core.core.db import get_session_factory
from biznotes_core.models import Agent, AgentRun, LLMModel, LLMProvider, Notebook, User
from biznotes_core.services.agent_runs import ModelUnavailable, enqueue_agent_run
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.nodes.llm import _exec_llm
from biznotes_core.services.gateway.llm import GatewayError, resolve_model
from biznotes_core.workers.celery_app import celery_app


@pytest.fixture(autouse=True)
def published(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    sent: list[Any] = []
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: sent.append(k.get("args")))
    return sent


async def _target(db: Any, owner: User) -> tuple[Agent, Notebook]:
    nb = Notebook(owner_id=owner.id, title="모델")
    agent = Agent(
        name=f"a-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        definition={"nodes": [], "edges": []},
        params_schema={},
        scope="system",
        enabled=True,
    )
    db.add_all([nb, agent])
    await db.commit()
    return agent, nb


async def _model(
    db: Any,
    *,
    enabled: bool = True,
    provider_enabled: bool = True,
    capabilities: tuple[str, ...] = ("llm",),
) -> LLMModel:
    provider = LLMProvider(
        name=f"p-{uuid.uuid4().hex[:6]}", type="mock", enabled=provider_enabled
    )
    db.add(provider)
    await db.flush()
    model = LLMModel(
        provider_id=provider.id,
        model_name=f"m-{uuid.uuid4().hex[:6]}",
        display_name="고른 모델",
        capabilities=list(capabilities),
        enabled=enabled,
    )
    db.add(model)
    await db.commit()
    return model


async def _enqueue(db: Any, user: User, agent: Agent, nb: Notebook, model_id: Any) -> AgentRun:
    return await enqueue_agent_run(
        db,
        agent=agent,
        notebook_id=nb.id,
        user_id=user.id,
        user_role=user.role,
        params={},
        model_id=model_id,
    )


async def _run_count() -> int:
    async with get_session_factory()() as s:
        return int(await s.scalar(select(func.count()).select_from(AgentRun)) or 0)


async def test_chosen_model_is_stored_and_null_after_model_delete(db, user_a, published):
    agent, nb = await _target(db, user_a)
    model = await _model(db)

    run = await _enqueue(db, user_a, agent, nb, model.id)

    assert run.model_id == model.id and published == [[str(run.id)]]
    await db.execute(text("DELETE FROM llm_models WHERE id = :id"), {"id": model.id})
    await db.commit()
    async with get_session_factory()() as s:
        stored = await s.get(AgentRun, run.id)
        assert stored is not None and stored.model_id is None


async def test_omitted_model_is_null(db, user_a):
    agent, nb = await _target(db, user_a)
    run = await _enqueue(db, user_a, agent, nb, None)
    assert run.model_id is None


@pytest.mark.parametrize(
    "kind", ["missing", "disabled", "provider_disabled", "embedding_only"]
)
async def test_unusable_model_is_refused_before_the_run_exists(db, user_a, published, kind):
    agent, nb = await _target(db, user_a)
    if kind == "missing":
        model_id = uuid.uuid4()
    else:
        model = await _model(
            db,
            enabled=kind != "disabled",
            provider_enabled=kind != "provider_disabled",
            capabilities=("embedding",) if kind == "embedding_only" else ("llm",),
        )
        model_id = model.id

    with pytest.raises(ModelUnavailable) as raised:
        await _enqueue(db, user_a, agent, nb, model_id)

    # 없는 모델과 꺼진 모델을 같은 문구로 거부한다
    assert str(raised.value) == "요청한 모델을 사용할 수 없습니다"
    await db.rollback()
    assert await _run_count() == 0 and published == []


async def test_column_matches_the_revision_shape(db):
    row = (
        await db.execute(
            text(
                "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = 'agent_runs' AND column_name = 'model_id'"
            )
        )
    ).one()
    assert tuple(row) == ("uuid", "YES", None)


async def test_provider_disabled_after_creation_is_not_called(db, user_a):
    """리뷰 M2 — 만든 뒤 프로바이더를 끄면 실행 시점에도 쓰지 않는다(노드 실패, D3)."""
    agent, nb = await _target(db, user_a)
    model = await _model(db)
    run = await _enqueue(db, user_a, agent, nb, model.id)
    await db.execute(
        text("UPDATE llm_providers SET enabled = false WHERE id = :id"), {"id": model.provider_id}
    )
    await db.commit()

    with pytest.raises(GatewayError, match="사용할 수 없습니다"):
        await resolve_model(db, model.id)
    ctx = RunContext(
        run_id=run.id, notebook_id=nb.id, db=db, params={}, run_model_id=run.model_id
    )
    with pytest.raises(GatewayError, match="사용할 수 없습니다"):
        await _exec_llm(ctx, {"id": "gen", "type": "llm", "prompt": "요약"}, {})
