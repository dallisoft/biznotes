"""스튜디오 실행 모델 선택·표시 (#216, core-v0.25.0-upgrade AC-12~AC-14 · D4, 코어 #359).

- 생성: `POST /notebooks/{id}/agent-runs`의 `model_id` → run 행 `model_id`. 쓸 수 없는
  모델(꺼진 모델·꺼진 프로바이더·llm 능력 없음·없는 모델)은 422이고 run 행도 발행도 없다.
  노트북 접근이 없으면 모델 검사보다 먼저 404다. 주지 않으면 `None`이다(지금처럼 기본 모델).
- 실행: 제품 워커 `_run`이 `RunContext(run_model_id=run.model_id)`를 넘긴다. 워커·코어
  `execute_agent`·폴백 체인은 실물이다. 대체는 LLM 게이트웨이의 후보별 호출 경계
  (`gateway.complete_stream` — 받은 모델을 기록한 뒤 실제 mock 프로바이더 경로로 넘긴다)와
  이벤트 발행 `_publish`뿐이다.
- 표시: `GET /agent-runs/{id}/logs`가 노드의 사용 모델·폴백 여부·시도한 모델을 코어가
  `output_data`에 남긴 키에서만 꺼낸다.
- 목록(#234, core-v0.27.0-upgrade AC-8·AC-10): `GET /notebooks/{id}/agent-runs`가 run마다
  실제로 응답한 모델(처음 응답한 순서, 중복 없음)과 폴백 여부를 상세 「사용 모델」과 같은 규칙으로
  싣는다. 생성·단건·취소 응답은 빈 값이고, 읽기 권한이 없으면 지금처럼 404다.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from biznotes_core.core.db import engine
from biznotes_core.models import (
    Agent,
    AgentRun,
    ExecutionLog,
    LLMModel,
    LLMProvider,
    Notebook,
)
from biznotes_core.services.gateway import llm as gateway
from biznotes_core.workers.celery_app import celery_app
from sqlalchemy import event, func, select

import app.workers.tasks.agent as agent_task
from tests.conftest import bearer, login, share_with

_UNUSABLE = "요청한 모델을 사용할 수 없습니다"
_CALL_FAILED = "model_call_failed"


async def _provider(db, *, enabled: bool = True) -> LLMProvider:
    provider = LLMProvider(name=f"mock-{uuid.uuid4().hex[:6]}", type="mock", enabled=enabled)
    db.add(provider)
    await db.flush()
    return provider


async def _model(
    db,
    display_name: str,
    *,
    provider: LLMProvider | None = None,
    capabilities: tuple[str, ...] = ("llm",),
    enabled: bool = True,
    is_default: bool = False,
    fallback_priority: int | None = None,
) -> LLMModel:
    """모델 하나 — 프로바이더를 주지 않으면 켜진 mock 프로바이더를 새로 만든다."""
    model = LLMModel(
        provider_id=(provider or await _provider(db)).id,
        model_name=f"mock-{uuid.uuid4().hex[:6]}",
        display_name=display_name,
        capabilities=list(capabilities),
        enabled=enabled,
        is_default=is_default,
        fallback_priority=fallback_priority,
    )
    db.add(model)
    await db.flush()
    return model


@pytest.fixture
async def models(db) -> SimpleNamespace:
    """기본 모델(폴백 후보 1순위)과 고를 모델.

    프로바이더를 따로 둬 서킷이 서로 섞이지 않게 한다.
    """
    default = await _model(db, "기본 Mock", is_default=True, fallback_priority=1)
    chosen = await _model(db, "선택 Mock")
    await db.commit()
    return SimpleNamespace(default=default, chosen=chosen)


@pytest.fixture
async def workflow(db) -> Agent:
    """`model_id`를 고정하지 않은 llm 노드 하나짜리 system 워크플로우."""
    agent = Agent(
        name=f"모델 선택 {uuid.uuid4().hex[:6]}",
        kind="workflow",
        icon="🧪",
        definition={"nodes": [{"id": "gen", "type": "llm", "prompt": "요약해 줘"}], "edges": []},
        params_schema={},
        scope="system",
        enabled=True,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


@pytest.fixture
async def notebook(db, user_a) -> Notebook:
    nb = Notebook(owner_id=user_a.id, title="run-model-nb")
    db.add(nb)
    await db.commit()
    await db.refresh(nb)
    return nb


@pytest.fixture
def published(monkeypatch) -> list[tuple[str, Any]]:
    """Celery 발행 경계 — 브로커로 보내지 않고 기록한다(코어는 커밋 뒤 이름으로 발행)."""
    calls: list[tuple[str, Any]] = []

    def _send_task(name: str, *args: Any, **kwargs: Any) -> None:
        calls.append((name, kwargs.get("args")))

    monkeypatch.setattr(celery_app, "send_task", _send_task)
    return calls


async def _run_count(db, notebook_id: uuid.UUID) -> int:
    return await db.scalar(
        select(func.count()).select_from(AgentRun).where(AgentRun.notebook_id == notebook_id)
    )


def _create(client: httpx.AsyncClient, token: str, notebook_id, agent: Agent, model_id=...):
    """run 생성 요청. `model_id`를 넘기지 않으면 본문에서 키를 뺀다."""
    body: dict[str, Any] = {"agent_id": str(agent.id)}
    if model_id is not ...:
        body["model_id"] = None if model_id is None else str(model_id)
    return client.post(
        f"/api/v1/notebooks/{notebook_id}/agent-runs", json=body, headers=bearer(token)
    )


# ---------- 생성: run.model_id (AC-12) ----------


async def test_create_run_with_model_sets_model_id(
    client: httpx.AsyncClient, db, user_a, models, workflow, notebook, published
):
    """고른 모델이 run 행·생성·단건·목록 응답의 `model_id`에 남고, run은 한 번 발행된다."""
    token = await login(client, user_a.email)
    res = await _create(client, token, notebook.id, workflow, models.chosen.id)
    assert res.status_code == 202, res.text
    run_id = res.json()["id"]
    assert res.json()["model_id"] == str(models.chosen.id)

    row = await db.get(AgentRun, uuid.UUID(run_id))
    assert row is not None and row.model_id == models.chosen.id
    assert [args for _name, args in published] == [[run_id]]

    detail = await client.get(f"/api/v1/agent-runs/{run_id}", headers=bearer(token))
    assert detail.json()["model_id"] == str(models.chosen.id)
    listed = await client.get(f"/api/v1/notebooks/{notebook.id}/agent-runs", headers=bearer(token))
    assert [r["model_id"] for r in listed.json()] == [str(models.chosen.id)]


async def test_create_run_without_model_keeps_default(
    client: httpx.AsyncClient, db, user_a, models, workflow, notebook, published
):
    """`model_id`를 빼거나 null이면 지금처럼 run 모델이 없다(기본 모델 + 폴백 체인)."""
    token = await login(client, user_a.email)
    omitted = await _create(client, token, notebook.id, workflow)
    explicit_null = await _create(client, token, notebook.id, workflow, None)

    for res in (omitted, explicit_null):
        assert res.status_code == 202, res.text
        assert res.json()["model_id"] is None
        row = await db.get(AgentRun, uuid.UUID(res.json()["id"]))
        assert row is not None and row.model_id is None
    assert len(published) == 2


# ---------- 생성: 쓸 수 없는 모델 → 422, run 없음 (AC-13) ----------


@pytest.mark.parametrize("case", ["disabled_model", "disabled_provider", "not_llm", "missing"])
async def test_create_run_rejects_unusable_model(
    client: httpx.AsyncClient, db, user_a, models, workflow, notebook, published, case
):
    """꺼진 모델·꺼진 프로바이더·llm 능력 없는 모델·없는 모델은 같은 문구의 422다.

    run 행도 발행도 없다.
    """
    if case == "disabled_model":
        model_id = (await _model(db, "꺼진 모델", enabled=False)).id
    elif case == "disabled_provider":
        off = await _provider(db, enabled=False)
        model_id = (await _model(db, "꺼진 공급자", provider=off)).id
    elif case == "not_llm":
        model_id = (await _model(db, "이미지 전용", capabilities=("image",))).id
    else:
        model_id = uuid.uuid4()
    await db.commit()

    token = await login(client, user_a.email)
    res = await _create(client, token, notebook.id, workflow, model_id)

    assert res.status_code == 422, res.text
    assert res.json() == {"detail": _UNUSABLE}
    assert await _run_count(db, notebook.id) == 0
    assert published == []


async def test_create_run_rejects_disabled_model_for_admin(
    client: httpx.AsyncClient, db, admin_user, models, workflow, notebook, published
):
    """admin(모든 노트북 owner 승격)도 꺼진 모델은 고를 수 없다 — 역할 예외가 없다."""
    disabled = await _model(db, "꺼진 모델", enabled=False)
    await db.commit()

    token = await login(client, admin_user.email)
    res = await _create(client, token, notebook.id, workflow, disabled.id)

    assert res.status_code == 422
    assert await _run_count(db, notebook.id) == 0
    assert published == []


@pytest.mark.parametrize("which", ["chosen", "disabled"])
async def test_other_users_notebook_is_404_before_model_check(
    client: httpx.AsyncClient, db, user_b, models, workflow, notebook, published, which
):
    """비인가: 남의 노트북이면 모델이 켜져 있든 꺼졌든 404다.

    모델 검사(422)보다 먼저라 노트북 밖에서 모델 존재·상태를 캐 볼 수 없다. run 행도 발행도 없다.
    """
    if which == "chosen":
        model_id = models.chosen.id
    else:
        model_id = (await _model(db, "꺼진 모델", enabled=False)).id
        await db.commit()

    token = await login(client, user_b.email)
    res = await _create(client, token, notebook.id, workflow, model_id)

    assert res.status_code == 404
    assert await _run_count(db, notebook.id) == 0
    assert published == []


async def test_read_shared_user_runs_with_model_like_before(
    client: httpx.AsyncClient, db, user_b, models, workflow, notebook, published
):
    """read 공유자는 지금처럼 실행할 수 있고(노트북 read 이상), 모델도 같은 규칙으로 받는다.

    꺼진 모델은 422, 켜진 모델은 202 — 모델 지정이 실행 인가를 넓히거나 좁히지 않는다.
    """
    await share_with(db, notebook.id, user_b, "read")
    disabled = await _model(db, "꺼진 모델", enabled=False)
    await db.commit()

    token = await login(client, user_b.email)
    rejected = await _create(client, token, notebook.id, workflow, disabled.id)
    accepted = await _create(client, token, notebook.id, workflow, models.chosen.id)

    assert rejected.status_code == 422
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["model_id"] == str(models.chosen.id)
    assert await _run_count(db, notebook.id) == 1


# ---------- 실행: 워커가 run 모델을 넘긴다 (AC-12) ----------


@pytest.fixture
def llm_calls(monkeypatch) -> SimpleNamespace:
    """LLM 게이트웨이 경계 — 후보별 호출(`complete_stream`)이 받은 모델 id를 기록한다.

    `fail`에 넣은 모델은 호출 실패(`GatewayCallError`)다. 나머지는 실제 mock 프로바이더
    경로로 넘긴다.
    """
    real = gateway.complete_stream
    calls: list[uuid.UUID] = []
    fail: set[uuid.UUID] = set()

    async def spy(resolved, messages, result, temperature=0.2, max_tokens=2048):
        calls.append(resolved.model.id)
        if resolved.model.id in fail:
            raise gateway.GatewayCallError("provider down")
        return await real(resolved, messages, result, temperature, max_tokens)

    monkeypatch.setattr(gateway, "complete_stream", spy)
    monkeypatch.setattr(agent_task, "_publish", lambda run_id, event: None)
    return SimpleNamespace(calls=calls, fail=fail)


def _queued(user, workflow: Agent, notebook: Notebook, model_id) -> AgentRun:
    return AgentRun(
        agent_id=workflow.id,
        notebook_id=notebook.id,
        user_id=user.id,
        params={},
        status="queued",
        trigger="manual",
        model_id=model_id,
    )


async def _run_worker(db, run: AgentRun) -> AgentRun:
    await agent_task._run(str(run.id))
    await db.refresh(run)
    return run


async def _start(db, user, workflow: Agent, notebook: Notebook, model_id) -> AgentRun:
    run = _queued(user, workflow, notebook, model_id)
    db.add(run)
    await db.commit()
    return await _run_worker(db, run)


async def _logs(client: httpx.AsyncClient, user, run_id) -> dict[str, dict[str, Any]]:
    token = await login(client, user.email)
    res = await client.get(f"/api/v1/agent-runs/{run_id}/logs", headers=bearer(token))
    assert res.status_code == 200, res.text
    return {row["node_id"]: row for row in res.json()}


def _ref(model: LLMModel) -> dict[str, str]:
    return {"id": str(model.id), "display_name": model.display_name}


async def test_worker_runs_llm_node_on_run_model(
    client: httpx.AsyncClient, db, user_a, models, workflow, notebook, llm_calls,
    _mock_runtime_deps,
):
    """run 모델이 llm 노드의 1차 후보다 — 게이트웨이가 그 모델을 받는다.

    실행 로그에 사용 모델과 「폴백 아님」이 남아 logs 응답으로 보인다.
    """
    run = await _start(db, user_a, workflow, notebook, models.chosen.id)

    assert run.status == "succeeded", run.error
    assert llm_calls.calls == [models.chosen.id]
    gen = (await _logs(client, user_a, run.id))["gen"]
    assert gen["model"] == _ref(models.chosen)
    assert gen["fallback_used"] is False
    assert gen["model_attempts"] is None


async def test_worker_without_run_model_uses_default(
    client: httpx.AsyncClient, db, user_a, models, workflow, notebook, llm_calls,
    _mock_runtime_deps,
):
    """run 모델이 없으면 지금처럼 기본 모델이 1차 후보다."""
    run = await _start(db, user_a, workflow, notebook, None)

    assert run.status == "succeeded", run.error
    assert llm_calls.calls == [models.default.id]
    gen = (await _logs(client, user_a, run.id))["gen"]
    assert gen["model"] == _ref(models.default)
    assert gen["fallback_used"] is False


async def test_worker_run_model_failure_falls_back_and_is_marked(
    client: httpx.AsyncClient, db, user_a, models, workflow, notebook, llm_calls,
    _mock_runtime_deps,
):
    """고른 모델이 실패하면 폴백 체인(기본 모델)이 잇고, 로그에 응답 모델과 「폴백」이 남는다."""
    llm_calls.fail.add(models.chosen.id)

    run = await _start(db, user_a, workflow, notebook, models.chosen.id)

    assert run.status == "succeeded", run.error
    assert llm_calls.calls[0] == models.chosen.id
    assert llm_calls.calls[-1] == models.default.id
    gen = (await _logs(client, user_a, run.id))["gen"]
    assert gen["model"] == _ref(models.default)
    assert gen["fallback_used"] is True


async def test_worker_all_candidates_fail_lists_attempted_models(
    client: httpx.AsyncClient, db, user_a, models, workflow, notebook, llm_calls,
    _mock_runtime_deps,
):
    """모든 후보가 실패하면 노드·run이 실패하고, 실패 로그에 시도한 모델이 순서대로 남는다."""
    llm_calls.fail.update({models.chosen.id, models.default.id})

    run = await _start(db, user_a, workflow, notebook, models.chosen.id)

    assert run.status == "failed"
    gen = (await _logs(client, user_a, run.id))["gen"]
    assert gen["status"] == "failed"
    assert gen["error_code"] == _CALL_FAILED
    assert gen["model"] is None and gen["fallback_used"] is None
    assert gen["model_attempts"] == [
        _ref(models.chosen) | {"error_code": _CALL_FAILED},
        _ref(models.default) | {"error_code": _CALL_FAILED},
    ]


async def test_worker_run_model_disabled_after_create_fails_node(
    client: httpx.AsyncClient, db, user_a, models, workflow, notebook, llm_calls,
    _mock_runtime_deps,
):
    """생성 뒤 고른 모델이 꺼지면 다른 모델로 조용히 넘어가지 않고 노드가 실패한다(코어 D3).

    게이트웨이 호출은 0이다.
    """
    run = _queued(user_a, workflow, notebook, models.chosen.id)
    db.add(run)
    models.chosen.enabled = False
    await db.commit()

    run = await _run_worker(db, run)

    assert run.status == "failed"
    assert llm_calls.calls == []
    gen = (await _logs(client, user_a, run.id))["gen"]
    assert gen["error_code"] == "gateway_error"
    assert gen["model"] is None


# ---------- 표시: 로그의 모델 필드는 코어가 남긴 키에서만 (AC-14) ----------


async def test_logs_expose_model_fields_only_from_core_keys(
    client: httpx.AsyncClient, db, user_a, user_b, workflow, notebook
):
    """사용 모델·폴백은 llm·react_agent 성공 행에서만, 시도한 모델은 실패 행에서만 꺼낸다.

    모양이 다르거나 다른 노드(사용자 데이터)의 같은 이름 키는 무시한다 — 500도, 잘못된
    표시도 없다. 실행 로그 인가는 그대로다.
    """
    run = AgentRun(
        agent_id=workflow.id,
        notebook_id=notebook.id,
        user_id=user_a.id,
        params={},
        status="failed",
    )
    db.add(run)
    await db.flush()
    model = {"id": str(uuid.uuid4()), "display_name": "모델 A"}
    attempt = model | {"error_code": "circuit_open"}
    rows: dict[str, tuple[str, str, dict[str, Any] | None]] = {
        "llm-ok": ("llm", "succeeded", {"text": "t", "model": model, "fallback_used": True}),
        "react-ok": ("react_agent", "succeeded", {"model": model, "fallback_used": False}),
        "llm-no-model": ("llm", "succeeded", {"text": "t", "model": None, "fallback_used": False}),
        "user-data": (
            "transform",
            "succeeded",
            {"model": model, "fallback_used": True, "model_attempts": [attempt]},
        ),
        "llm-malformed": ("llm", "succeeded", {"model": "gpt", "fallback_used": "yes"}),
        "llm-failed": ("llm", "failed", {"model_attempts": [attempt, "… (총 25개)", {"id": 1}]}),
        "ok-with-attempts-key": ("llm", "succeeded", {"model_attempts": [attempt]}),
        "no-output": ("llm", "failed", None),
    }
    for node_id, (node_type, status, output) in rows.items():
        db.add(
            ExecutionLog(
                run_id=run.id,
                node_id=node_id,
                node_type=node_type,
                status=status,
                output_data=output,
                started_at=datetime.now(UTC),
            )
        )
    await db.commit()

    logs = await _logs(client, user_a, run.id)

    def fields(node_id: str) -> tuple[Any, Any, Any]:
        row = logs[node_id]
        return row["model"], row["fallback_used"], row["model_attempts"]

    assert fields("llm-ok") == (model, True, None)
    assert fields("react-ok") == (model, False, None)
    assert fields("llm-no-model") == (None, False, None)
    assert fields("user-data") == (None, None, None)
    assert fields("llm-malformed") == (None, None, None)
    assert fields("llm-failed") == (None, None, [attempt])
    assert fields("ok-with-attempts-key") == (None, None, None)
    assert fields("no-output") == (None, None, None)

    token_b = await login(client, user_b.email)
    res = await client.get(f"/api/v1/agent-runs/{run.id}/logs", headers=bearer(token_b))
    assert res.status_code == 403
    assert "모델 A" not in res.text


# ---------- 목록: 실제 사용 모델·폴백 (#234, AC-8·AC-10) ----------


def _listed(res: httpx.Response) -> dict[str, dict[str, Any]]:
    assert res.status_code == 200, res.text
    return {row["id"]: row for row in res.json()}


async def _list_runs(client: httpx.AsyncClient, user, notebook_id) -> dict[str, dict[str, Any]]:
    client.cookies.clear()
    token = await login(client, user.email)
    return _listed(
        await client.get(f"/api/v1/notebooks/{notebook_id}/agent-runs", headers=bearer(token))
    )


def _models_of(row: dict[str, Any]) -> tuple[list[dict[str, str]], bool]:
    return row["used_models"], row["fallback_used"]


async def test_list_shows_models_actually_used_by_each_run(
    client: httpx.AsyncClient, db, user_a, models, workflow, notebook, llm_calls,
    _mock_runtime_deps,
):
    """모델을 고른 run·고르지 않은 run·폴백한 run 모두 실제로 응답한 모델이 목록에 실린다.

    워커·폴백 체인은 실물이다. 단건 응답은 채우지 않는다(목록만 실행 로그를 조회한다).
    """
    chosen = await _start(db, user_a, workflow, notebook, models.chosen.id)
    unspecified = await _start(db, user_a, workflow, notebook, None)
    llm_calls.fail.add(models.chosen.id)
    fell_back = await _start(db, user_a, workflow, notebook, models.chosen.id)
    for run in (chosen, unspecified, fell_back):
        assert run.status == "succeeded", run.error

    rows = await _list_runs(client, user_a, notebook.id)

    assert _models_of(rows[str(chosen.id)]) == ([_ref(models.chosen)], False)
    assert _models_of(rows[str(unspecified.id)]) == ([_ref(models.default)], False)
    assert _models_of(rows[str(fell_back.id)]) == ([_ref(models.default)], True)
    # 요청 모델은 그대로 둔다 — 웹이 「폴백」 옆에 요청 모델 이름을 찾는다
    assert rows[str(fell_back.id)]["model_id"] == str(models.chosen.id)

    detail = await client.get(
        f"/api/v1/agent-runs/{fell_back.id}",
        headers=bearer(await login(client, user_a.email)),
    )
    assert detail.status_code == 200, detail.text
    assert _models_of(detail.json()) == ([], False)


async def test_list_used_models_follow_detail_rule(
    client: httpx.AsyncClient, db, user_a, workflow, notebook
):
    """사용 모델은 llm·react_agent 성공 행의 `output_data.model`에서만 꺼낸다(응답 순, 중복 없음).

    폴백은 그 행의 `fallback_used`가 진짜 bool `true`일 때만이다. 다른 노드(사용자 데이터)·실패 행·
    모양이 다른 값·출력 없는 행은 무시한다 — 500도, 잘못된 표시도 없다. 모델 로그가 없는 run은
    빈 목록과 `false`다.
    """
    base = datetime.now(UTC)
    a = {"id": str(uuid.uuid4()), "display_name": "모델 A"}
    b = {"id": str(uuid.uuid4()), "display_name": "모델 B"}
    c = {"id": str(uuid.uuid4()), "display_name": "모델 C"}

    async def seed_run(status: str, logs: list[tuple[int, str, str, Any]]) -> AgentRun:
        run = AgentRun(
            agent_id=workflow.id,
            notebook_id=notebook.id,
            user_id=user_a.id,
            params={},
            status=status,
        )
        db.add(run)
        await db.flush()
        for offset, node_type, log_status, output in logs:
            db.add(
                ExecutionLog(
                    run_id=run.id,
                    node_id=f"n{offset}-{node_type}-{log_status}",
                    node_type=node_type,
                    status=log_status,
                    output_data=output,
                    started_at=base + timedelta(seconds=offset),
                )
            )
        return run

    # 삽입 순서가 아니라 시작 시각 순서다 — B(1초)가 A(3초)보다 먼저 응답했다
    mixed = await seed_run(
        "succeeded",
        [
            (3, "llm", "succeeded", {"text": "t", "model": a, "fallback_used": False}),
            (1, "react_agent", "succeeded", {"model": b, "fallback_used": False}),
            (2, "llm", "succeeded", {"model": b}),
            (4, "llm", "succeeded", {"model": a, "fallback_used": "yes"}),
            (0, "transform", "succeeded", {"model": c, "fallback_used": True}),
            (5, "llm", "failed", {"model": c, "fallback_used": True, "model_attempts": []}),
            (6, "llm", "succeeded", {"model": "gpt", "fallback_used": True}),
            (7, "llm", "succeeded", {"model": {"id": "not-a-uuid", "display_name": "X"}}),
            (8, "llm", "succeeded", None),
            (9, "llm", "succeeded", ["model"]),
            (10, "react_tool_call", "succeeded", {"model": c}),
        ],
    )
    fell_back = await seed_run(
        "succeeded",
        [
            (0, "llm", "succeeded", {"model": a, "fallback_used": False}),
            (1, "react_agent", "succeeded", {"model": b, "fallback_used": True}),
        ],
    )
    only_ignored = await seed_run(
        "failed",
        [
            (0, "transform", "succeeded", {"model": a, "fallback_used": True}),
            (1, "llm", "failed", {"model_attempts": [a | {"error_code": "circuit_open"}]}),
        ],
    )
    no_logs = await seed_run("running", [])
    await db.commit()

    rows = await _list_runs(client, user_a, notebook.id)

    assert _models_of(rows[str(mixed.id)]) == ([b, a], False)
    assert _models_of(rows[str(fell_back.id)]) == ([a, b], True)
    assert _models_of(rows[str(only_ignored.id)]) == ([], False)
    assert _models_of(rows[str(no_logs.id)]) == ([], False)


async def test_single_run_responses_leave_used_models_empty(
    client: httpx.AsyncClient, db, user_a, models, workflow, notebook, published
):
    """생성·단건·취소 응답은 사용 모델을 조회하지 않는다 — 빈 목록과 `false`다(하위 호환 기본값).

    응답한 llm 노드가 이미 있는 진행 중 run이라도 목록만 그 모델을 싣는다.
    """
    token = await login(client, user_a.email)
    created = await _create(client, token, notebook.id, workflow, models.chosen.id)
    assert created.status_code == 202, created.text
    assert _models_of(created.json()) == ([], False)

    run_id = uuid.UUID(created.json()["id"])
    run = await db.get(AgentRun, run_id)
    assert run is not None
    run.status = "running"
    db.add(
        ExecutionLog(
            run_id=run_id,
            node_id="gen",
            node_type="llm",
            status="succeeded",
            output_data={"model": _ref(models.chosen), "fallback_used": False},
            started_at=datetime.now(UTC),
        )
    )
    await db.commit()

    detail = await client.get(f"/api/v1/agent-runs/{run_id}", headers=bearer(token))
    assert _models_of(detail.json()) == ([], False)
    rows = await _list_runs(client, user_a, notebook.id)
    assert _models_of(rows[str(run_id)]) == ([_ref(models.chosen)], False)

    token = await login(client, user_a.email)
    cancelled = await client.post(f"/api/v1/agent-runs/{run_id}/cancel", headers=bearer(token))
    assert cancelled.status_code == 200, cancelled.text
    assert _models_of(cancelled.json()) == ([], False)


async def test_list_used_models_unauthorized_is_404(
    client: httpx.AsyncClient, db, user_a, user_b, workflow, notebook
):
    """비인가: 노트북 읽기 권한이 없으면 목록은 지금처럼 404이고 모델 이름도 새지 않는다.

    읽기 공유를 받으면 같은 목록에서 사용 모델·폴백이 보인다(권한 규칙은 그대로다).
    """
    run = AgentRun(
        agent_id=workflow.id,
        notebook_id=notebook.id,
        user_id=user_a.id,
        params={},
        status="succeeded",
    )
    db.add(run)
    await db.flush()
    model = {"id": str(uuid.uuid4()), "display_name": "비공개 모델"}
    db.add(
        ExecutionLog(
            run_id=run.id,
            node_id="gen",
            node_type="llm",
            status="succeeded",
            output_data={"model": model, "fallback_used": True},
            started_at=datetime.now(UTC),
        )
    )
    await db.commit()

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    res = await client.get(f"/api/v1/notebooks/{notebook.id}/agent-runs", headers=bearer(token_b))
    assert res.status_code == 404
    assert "비공개 모델" not in res.text

    await share_with(db, notebook.id, user_b, "read")
    await db.commit()
    rows = await _list_runs(client, user_b, notebook.id)
    assert _models_of(rows[str(run.id)]) == ([model], True)


async def test_list_reads_model_paths_in_one_log_query(
    client: httpx.AsyncClient, db, user_a, workflow, notebook
):
    """실행 로그 조회는 run 수와 무관하게 한 번이고, `output_data` 전체가 아니라 두 경로만 읽는다.

    노드 출력 원문(소스 발췌 등)은 크다 — 목록 50개마다 통째로 읽지 않는다.
    """
    model = {"id": str(uuid.uuid4()), "display_name": "모델 A"}

    async def seed(count: int) -> None:
        for _ in range(count):
            run = AgentRun(
                agent_id=workflow.id,
                notebook_id=notebook.id,
                user_id=user_a.id,
                params={},
                status="succeeded",
            )
            db.add(run)
            await db.flush()
            db.add(
                ExecutionLog(
                    run_id=run.id,
                    node_id="gen",
                    node_type="llm",
                    status="succeeded",
                    output_data={"text": "본문" * 100, "model": model, "fallback_used": False},
                    started_at=datetime.now(UTC),
                )
            )
        await db.commit()

    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    token = await login(client, user_a.email)
    log_queries: dict[int, list[str]] = {}
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    seeded = 0
    try:
        for total in (1, 4):
            await seed(total - seeded)
            seeded = total
            statements.clear()
            res = await client.get(
                f"/api/v1/notebooks/{notebook.id}/agent-runs", headers=bearer(token)
            )
            assert res.status_code == 200, res.text
            assert all(row["used_models"] == [model] for row in res.json())
            assert len(res.json()) == total
            log_queries[total] = [q for q in statements if "execution_logs" in q]
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert [len(qs) for qs in log_queries.values()] == [1, 1], log_queries
    for (query,) in log_queries.values():
        # 경로 읽기는 `output_data[...]`(JSONB 첨자)다 — 첨자 없는 열 전체는 읽지 않는다
        assert re.search(r"execution_logs\.output_data(?!\[)", query) is None, query
