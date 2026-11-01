"""제품 실패 경로의 공개 문구 (core-v0.20.0-upgrade AC-2.4, §5.5·D-12·HCI-8).

코어 v0.19.0 #216은 노드 실행 로그에서 관리자 설정 원문(모델·공급자 이름, 공급자 응답)을 고정 문구로
바꿨다. 제품이 소유한 세 경로도 같은 규칙을 쓴다 — 원문은 서버 로그로만 간다.

- ⓐ 워커 `_run`의 실패 분기: 그래프 밖으로 새는 `GatewayCallError`도 `run.error`·발행 이벤트·
  반환값에 고정 문구, 경고 로그 `agent_run_failed_raw`에 코드와 원문.
- ⓑ 그래프 안 노드 실패는 코어가 이미 판정한 문구(「노드 [x] 실행 실패: …」) 그대로다.
- ⓒ 채팅 SSE(정적·자율): `GatewayCallError` → `error` 이벤트에 고정 문구, `GatewayError` 본체(모델
  해석·입력 검증 — 작성자가 고칠 안내) → 원문 그대로.
- ⓓ 전역 핸들러: `GatewayCallError` → 502 고정 문구, `GatewayError` 본체 → 502 원문
  (`test_chat_rerank_unavailable.py`의 `test_existing_mapping_keeps_exception_message_body` 유지).

대체(계획 §9 허용 대체): LLM 호출 경계 `litellm.acompletion`이 원문을 담은 오류를 던지게 한다(②) —
그래야 코어 게이트웨이 실물이 모델 표시명·원문을 담은 `GatewayCallError`를 만든다. 그 밖에 이벤트
발행 경계 `agent_task._publish`(②), `GatewayError` 본체를 던지는 게이트웨이 함수(②, 원문 유지 대조),
워커 실패 분기 한정으로 `GatewayCallError`를 던지는 `execute_agent`(⑦). 워커 재검사·상태 전이·
채팅 라우트·전역 핸들러는 실물이다.
"""

import json
import uuid
from typing import Any

import httpx
import litellm
import pytest
from biznotes_core.core.db import async_session_factory
from biznotes_core.models import Agent, AgentRun, LLMModel, LLMProvider, Notebook, User
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import GatewayCallError, GatewayError
from fastapi import FastAPI
from structlog.testing import capture_logs

import app.workers.tasks.agent as agent_task
from app.core.exception_handlers import register_exception_handlers
from app.main import app as product_app
from tests.conftest import bearer, login

FIXED = "모델 호출에 실패했습니다"
SECRET_MODEL = "secret-model-7f3a"
SECRET_DISPLAY = "Secret Display 7f3a"
SECRET_BASE = "http://10.0.0.1/v1"
SECRET_UPSTREAM = "upstream-body-9c1e"
SECRETS = (SECRET_MODEL, SECRET_DISPLAY, SECRET_BASE, "10.0.0.1", SECRET_UPSTREAM)
# 그래프 밖으로 새는 경우의 원문 — 코어 `complete_stream`의 형식(「LLM 호출 실패: …」)을 흉내 낸다
RAW = f"LLM 호출 실패: model={SECRET_MODEL} base={SECRET_BASE} {SECRET_UPSTREAM}"


def _assert_no_secret(text: str) -> None:
    for value in SECRETS:
        assert value not in text, value


def test_fixed_message_is_the_core_public_text() -> None:
    assert GatewayCallError.node_log_message == FIXED
    assert issubclass(GatewayCallError, GatewayError)


# ---------- 공용 준비 ----------


@pytest.fixture
async def secret_llm(db) -> LLMModel:
    """원문에 담길 값(모델명·표시명·base_url)을 가진 기본 LLM — 실제 호출은 아래 경계가 막는다."""
    provider = LLMProvider(
        name=f"secret-{uuid.uuid4().hex[:6]}", type="openai", base_url=SECRET_BASE, enabled=True
    )
    db.add(provider)
    await db.flush()
    model = LLMModel(
        provider_id=provider.id,
        model_name=SECRET_MODEL,
        display_name=SECRET_DISPLAY,
        capabilities=["llm"],
        is_default=True,
        enabled=True,
    )
    db.add(model)
    await db.commit()
    await db.refresh(model)
    return model


@pytest.fixture
def failing_llm_boundary(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """LLM 호출 경계(허용 대체 ②) — 공급자 응답 원문을 담은 오류를 던진다. 호출 인자를 기록한다."""
    calls: list[dict[str, Any]] = []

    async def failing_acompletion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        raise RuntimeError(f"{SECRET_UPSTREAM} from {kwargs.get('api_base')} for {kwargs['model']}")

    monkeypatch.setattr(litellm, "acompletion", failing_acompletion)
    return calls


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(agent_task, "_publish", lambda run_id, event: events.append(event))
    return events


async def _queued_run(db, owner: User, definition: dict[str, Any]) -> AgentRun:
    notebook = Notebook(owner_id=owner.id, title="public-failure-nb")
    db.add(notebook)
    await db.flush()
    agent = Agent(
        name=f"public-failure-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        definition=definition,
        params_schema={},
        scope="user",
        owner_id=owner.id,
        enabled=True,
    )
    db.add(agent)
    await db.flush()
    run = AgentRun(
        agent_id=agent.id,
        notebook_id=notebook.id,
        user_id=owner.id,
        params={},
        status="queued",
        trigger="manual",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


def _raw_events(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry.get("event") == "agent_run_failed_raw"]


# ---------- ⓐ 워커 — 그래프 밖으로 새는 GatewayCallError ----------


async def test_worker_gateway_call_error_outside_graph_uses_fixed_message(
    db, user_a, published, monkeypatch
):
    """허용 대체 ⑦: 제품 워커가 부르는 `execute_agent`가 그래프 밖에서 `GatewayCallError`를
    던진다."""

    async def leaking_execute_agent(*args: Any, **kwargs: Any) -> str:
        raise GatewayCallError(RAW)

    monkeypatch.setattr(
        "biznotes_core.services.agents.runtime.execute_agent", leaking_execute_agent
    )
    run = await _queued_run(
        db, user_a, {"nodes": [{"id": "c", "type": "constant", "value": "v"}], "edges": []}
    )

    with capture_logs() as logs:
        result = await agent_task._run(str(run.id))

    assert result == {"ok": False, "error": FIXED}
    await db.refresh(run)
    assert run.status == "failed"
    assert run.error == FIXED
    assert run.finished_at is not None
    assert published[-1] == {"type": "status", "status": "failed", "error": FIXED}
    _assert_no_secret(json.dumps(result, ensure_ascii=False))
    _assert_no_secret(run.error)
    _assert_no_secret(json.dumps(published, ensure_ascii=False))
    raw_logs = _raw_events(logs)
    assert len(raw_logs) == 1
    assert raw_logs[0]["run_id"] == str(run.id)
    assert raw_logs[0]["error_code"] == "model_call_failed"
    assert raw_logs[0]["error"] == RAW  # 원문은 서버 로그로만 간다


async def test_worker_other_failure_outside_graph_keeps_message_without_raw_log(
    db, user_a, published
):
    """고정 문구가 없는 실패는 그 문구 그대로이고 원문 경고 로그는 남기지 않는다(판정 결과 raw
    없음). 대체 없이 실경로로 만든다 — 순환 그래프는 코어 `execute_agent`가 노드 실행 전에
    `AgentRuntimeError`로 거부한다(그래프 밖 실패)."""
    run = await _queued_run(
        db,
        user_a,
        {
            "nodes": [
                {"id": "a", "type": "constant", "value": "v"},
                {"id": "b", "type": "constant", "value": "w"},
            ],
            "edges": [["a", "b"], ["b", "a"]],
        },
    )

    with capture_logs() as logs:
        result = await agent_task._run(str(run.id))

    message = "워크플로우 그래프에 순환이 있습니다"
    assert result == {"ok": False, "error": message}
    await db.refresh(run)
    assert run.status == "failed"
    assert run.error == message
    assert published[-1] == {"type": "status", "status": "failed", "error": message}
    assert _raw_events(logs) == []


# ---------- ⓑ 그래프 안 노드 실패 ----------


async def test_worker_node_failure_inside_graph_keeps_core_message(db, user_a, published):
    """실 그래프 — http 노드 `127.0.0.1`(SSRF 사전 검사가 요청 전에 막는다). 코어 문구 그대로."""
    run = await _queued_run(
        db,
        user_a,
        {
            "nodes": [{"id": "h", "type": "http", "url": "http://127.0.0.1:9/", "method": "GET"}],
            "edges": [],
        },
    )

    with capture_logs() as logs:
        result = await agent_task._run(str(run.id))

    await db.refresh(run)
    assert run.status == "failed"
    assert run.error.startswith("노드 [h] 실행 실패:")
    assert result == {"ok": False, "error": run.error}
    assert published[-1] == {"type": "status", "status": "failed", "error": run.error}
    assert _raw_events(logs) == []


async def test_worker_model_call_failure_inside_graph_is_fixed_everywhere(
    client: httpx.AsyncClient, db, user_a, secret_llm, failing_llm_boundary, published
):
    """실 그래프 + 코어 게이트웨이 실물 — llm 노드의 모델 호출 실패. 실행 로그(코어)·run 오류(워커)·
    로그 API 어디에도 원문이 없다. 사슬에 `GatewayCallError`가 있어 run 오류는 고정 문구 자체다
    (노드 접두 「노드 [g] 실행 실패:」 없이 — `describe_node_failure`가 가리는 쪽을 고른다)."""
    run = await _queued_run(
        db,
        user_a,
        {"nodes": [{"id": "g", "type": "llm", "prompt": "안녕"}], "edges": []},
    )

    with capture_logs() as logs:
        result = await agent_task._run(str(run.id))

    assert failing_llm_boundary, "LLM 호출 경계까지 가지 않았다 — 원문 부재 단언이 공허하다"
    await db.refresh(run)
    assert run.status == "failed"
    assert run.error == FIXED
    assert result == {"ok": False, "error": FIXED}
    assert published[-1] == {"type": "status", "status": "failed", "error": FIXED}
    raw_logs = _raw_events(logs)
    assert len(raw_logs) == 1 and raw_logs[0]["error_code"] == "model_call_failed"
    assert SECRET_UPSTREAM in raw_logs[0]["error"]  # 원문은 서버 로그에만 있다

    token = await login(client, user_a.email)
    res = await client.get(f"/api/v1/agent-runs/{run.id}/logs", headers=bearer(token))
    assert res.status_code == 200, res.text
    (row,) = res.json()
    assert (row["node_id"], row["status"]) == ("g", "failed")
    assert row["error_code"] == "model_call_failed"
    assert row["error_message"] == FIXED
    # 시도한 모델(#216 AC-14, 코어 #359 `model_attempts`)은 id·표시 이름·실패 코드만 담는다.
    # 표시 이름은 모델 선택 목록(`GET /models`)에도 보이는 공개 값이다. 모델 식별자·base_url·
    # 공급자 원문은 여기에도, 나머지 응답에도 없다
    attempt = {"id": str(secret_llm.id), "display_name": SECRET_DISPLAY}
    assert row.pop("model_attempts") == [attempt | {"error_code": "model_call_failed"}]
    _assert_no_secret(json.dumps(row, ensure_ascii=False))
    for value in SECRETS:
        if value != SECRET_DISPLAY:
            assert value not in res.text, value
    _assert_no_secret(run.error)
    _assert_no_secret(json.dumps(published, ensure_ascii=False))


# ---------- ⓒ 채팅 SSE ----------


def _parse_sse(text: str) -> list[dict[str, Any]]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


async def _session(client: httpx.AsyncClient, token: str) -> str:
    nb = await client.post("/api/v1/notebooks", json={"title": "sse-nb"}, headers=bearer(token))
    assert nb.status_code == 201, nb.text
    sess = await client.post(
        f"/api/v1/notebooks/{nb.json()['id']}/chat/sessions", headers=bearer(token)
    )
    assert sess.status_code in (200, 201), sess.text
    return sess.json()["id"]


async def _send(client: httpx.AsyncClient, token: str, session_id: str, **body: Any):
    return await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "에스프레소 추출 압력은?", **body},
        headers=bearer(token),
    )


async def test_raw_gateway_call_error_carries_secrets(db, secret_llm, failing_llm_boundary):
    """대조군 — 같은 설정에서 코어 게이트웨이가 만드는 `GatewayCallError` 원문은 표시명·공급자
    응답을 담는다. 아래 SSE·502 단언의 원문 부재가 공허하지 않다는 근거다."""
    result = gateway_llm.StreamResult()
    with pytest.raises(GatewayCallError) as raised:
        await gateway_llm.complete_stream_with_fallback(
            db, None, [{"role": "user", "content": "x"}], result
        )
    assert SECRET_DISPLAY in str(raised.value)
    assert SECRET_UPSTREAM in str(raised.value)


async def test_static_chat_model_call_failure_streams_fixed_error(
    client: httpx.AsyncClient, user_a, secret_llm, failing_llm_boundary
):
    """정적 채팅 — 실 검색(빈 노트북)·실 게이트웨이 폴백 체인이 `GatewayCallError`를 낸다."""
    token = await login(client, user_a.email)
    session_id = await _session(client, token)
    res = await _send(client, token, session_id)

    assert res.status_code == 200, res.text
    events = _parse_sse(res.text)
    assert [e["type"] for e in events] == ["retrieval", "error"]
    assert events[-1]["detail"] == FIXED
    assert failing_llm_boundary
    _assert_no_secret(res.text)


async def test_static_chat_gateway_error_body_streams_original_text(
    client: httpx.AsyncClient, user_a, secret_llm, monkeypatch
):
    """정적 채팅 — `GatewayError` 본체(모델 해석·입력 검증 안내)는 원문 그대로(허용 대체 ②)."""

    async def guidance_error(*args: Any, **kwargs: Any) -> Any:
        raise GatewayError("모델 설정 안내 원문: 입력이 비었습니다")

    monkeypatch.setattr("app.api.v1.chat.complete_stream_with_fallback", guidance_error)
    token = await login(client, user_a.email)
    session_id = await _session(client, token)
    res = await _send(client, token, session_id)

    events = _parse_sse(res.text)
    assert events[-1] == {"type": "error", "detail": "모델 설정 안내 원문: 입력이 비었습니다"}


async def test_autonomous_chat_model_call_failure_streams_fixed_error(
    client: httpx.AsyncClient, user_a, secret_llm, failing_llm_boundary
):
    """자율 채팅 — react 노드가 코어 게이트웨이 실물(`complete_with_tools`)로 모델을 부르다
    실패한다."""
    token = await login(client, user_a.email)
    session_id = await _session(client, token)
    res = await _send(client, token, session_id, autonomous=True)

    assert res.status_code == 200, res.text
    events = _parse_sse(res.text)
    assert events[-1] == {"type": "error", "detail": FIXED}
    assert failing_llm_boundary
    _assert_no_secret(res.text)


async def test_autonomous_chat_gateway_error_body_streams_original_text(
    client: httpx.AsyncClient, user_a, secret_llm, monkeypatch
):
    async def guidance_error(*args: Any, **kwargs: Any) -> Any:
        raise GatewayError("도구 호출 모델 설정 안내 원문")

    monkeypatch.setattr("biznotes_core.services.gateway.llm.complete_with_tools", guidance_error)
    token = await login(client, user_a.email)
    session_id = await _session(client, token)
    res = await _send(client, token, session_id, autonomous=True)

    events = _parse_sse(res.text)
    assert events[-1] == {"type": "error", "detail": "도구 호출 모델 설정 안내 원문"}


# ---------- ⓓ 전역 핸들러 ----------


def _probe_app() -> FastAPI:
    """제품과 같은 `register_exception_handlers`로 매핑하는 비스트림 라우트 — 코어 게이트웨이
    실물을 부른다."""
    probe = FastAPI()
    register_exception_handlers(probe)

    @probe.get("/complete")
    async def complete() -> dict[str, str]:
        async with async_session_factory() as session:
            await gateway_llm.complete_with_tools(
                session, None, [{"role": "user", "content": "x"}], []
            )
        return {"ok": "unreachable"}

    return probe


async def test_global_handler_maps_gateway_call_error_to_fixed_502(
    secret_llm, failing_llm_boundary
):
    transport = httpx.ASGITransport(app=_probe_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://probe") as probe:
        res = await probe.get("/complete")

    assert failing_llm_boundary
    assert res.status_code == 502
    assert res.json() == {"detail": FIXED}
    _assert_no_secret(res.text)


async def test_product_app_handler_separates_gateway_call_error_from_gateway_error():
    """제품 앱에 등록된 핸들러 — 하위 클래스 `GatewayCallError`는 고정 문구, 본체는 원문
    (둘 다 502)."""
    handler = product_app.exception_handlers[GatewayError]

    fixed = await handler(None, GatewayCallError(RAW))
    assert fixed.status_code == 502
    assert json.loads(fixed.body) == {"detail": FIXED}

    original = await handler(None, GatewayError("요청한 모델을 사용할 수 없습니다"))
    assert original.status_code == 502
    assert json.loads(original.body) == {"detail": "요청한 모델을 사용할 수 없습니다"}


async def test_product_route_gateway_error_body_is_502_with_original_text(
    client: httpx.AsyncClient, user_a, secret_llm
):
    """실 제품 라우트 — 스트림 전 모델 해석 실패(`GatewayError` 본체)는 전역 핸들러가 502
    원문으로."""
    token = await login(client, user_a.email)
    session_id = await _session(client, token)
    res = await _send(client, token, session_id, model_id=str(uuid.uuid4()))

    assert res.status_code == 502
    assert res.json() == {"detail": "요청한 모델을 사용할 수 없습니다"}
