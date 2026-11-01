"""리랭커 호출·파싱 실패의 제품 경계 정제 (core-v0.15.0 AC-2.1b·AC-2.2, PR-01·HCI-6).

게이트웨이 리랭크 실경로(`retrieve_context → resolve_reranker → rerank_relevance →
gateway.rerank.rerank → _parse`)를 그대로 돌리고 **HTTP 전송 경계만** 바꾼다
(`gateway.rerank._http_client`가 `httpx.MockTransport` 클라이언트를 돌려주고, 재시도 대기
`_sleep`은 no-op). 리랭커 서버가 돌려준 값(sentinel)이 정적 채팅 SSE·503 본문·제품 로그로 새지
않는지 본다.

코어 v0.15.1(#217)부터 파서 오류 메시지는 서버 값 대신 위반 종류와 타입만 담는다(core-v0.15.0
HCI-15의 잔여 위험을 코어가 해소). 제품 경계의 고정 공개 문구는 방어 심층으로 유지한다.
코어가 남기는 진단 이벤트(「게이트웨이 리랭크 실패」)의 모델명은 허용한다.
"""

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from biznotes_core.core.crypto import encrypt
from biznotes_core.core.db import async_session_factory
from biznotes_core.models import ChatMessage, LLMModel, LLMProvider, Notebook
from biznotes_core.services import rag_chat, usage_guard
from biznotes_core.services.gateway.llm import GatewayError
from biznotes_core.services.rerank import RerankUnavailableError
from biznotes_core.services.settings_store import set_setting
from fastapi import FastAPI
from sqlalchemy import func, select
from structlog.testing import capture_logs

from app.core.exception_handlers import RERANK_UNAVAILABLE_DETAIL, register_exception_handlers
from app.main import app as product_app
from tests.conftest import bearer, login

PROBE_BASE_URL = "http://rerank.invalid/v1"
PROBE_MODEL_NAME = "openai/rerank-probe-model"  # 요청 본문에는 접두사를 뗀 이름이 간다
PROBE_REQUEST_MODEL = "rerank-probe-model"
PROBE_DISPLAY_NAME = "Rerank Probe Model"
CORE_RERANK_EVENT = "게이트웨이 리랭크 실패"  # 코어 gateway/rerank.py 진단 이벤트
PRODUCT_EVENT = "chat_rerank_unavailable"

Responder = Callable[[httpx.Request], httpx.Response]

# 사례별 리랭커 서버 응답 — sentinel을 서버 반환값에 심는다
CASES: dict[str, Callable[[str], Responder]] = {
    "relevance_score_sentinel": lambda s: lambda req: httpx.Response(
        200, json={"results": [{"index": 0, "relevance_score": s}]}
    ),
    "index_sentinel": lambda s: lambda req: httpx.Response(
        200, json={"results": [{"index": s, "relevance_score": 0.5}]}
    ),
    "http_500": lambda s: lambda req: httpx.Response(500, text=f"upstream failure {s}"),
}
# 파싱 오류는 다시 보내지 않고, 5xx는 재시도 상한(코어 `_MAX_ATTEMPTS`)까지 보낸다
EXPECTED_ATTEMPTS = {"relevance_score_sentinel": 1, "index_sentinel": 1, "http_500": 3}


@dataclass
class _Point:
    id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


def _search_returning(points: list[Any], *, sliced: bool = False) -> Any:
    """`search_spaces` 대역(비동기, 세션을 먼저 받는다) — 정해 둔 결과를 돌려준다."""

    async def search(db: Any, spaces: Any, query: str, *, limit: int = 8, **kw: Any) -> list[Any]:
        return points[:limit] if sliced else points

    return search


@pytest.fixture
def sentinels() -> dict[str, str]:
    tag = uuid.uuid4().hex
    return {
        "server": f"SENTINEL-{tag}",
        "query": f"QUERY-{tag}",
        "doc": f"DOC-{tag}",
        "credential": f"CRED-{tag}",
    }


@pytest.fixture
async def gateway_reranker(db, sentinels) -> LLMModel:
    """`custom` 프로바이더 + `reranker` 모델 행, `rag` 설정이 그 모델을 가리킨다."""
    provider = LLMProvider(
        name=f"rerank-probe-{uuid.uuid4().hex[:6]}",
        type="custom",
        base_url=PROBE_BASE_URL,
        api_key_encrypted=encrypt(sentinels["credential"]),
        enabled=True,
    )
    db.add(provider)
    await db.flush()
    model = LLMModel(
        provider_id=provider.id,
        model_name=PROBE_MODEL_NAME,
        display_name=PROBE_DISPLAY_NAME,
        capabilities=["reranker"],
        is_default=False,
        enabled=True,
    )
    db.add(model)
    await db.flush()
    await set_setting(
        db,
        "rag",
        {"rerank_enabled": True, "rerank_model": f"gateway:{model.id}:{model.model_name}"},
    )
    await db.commit()
    return model


@pytest.fixture
def stub_search(monkeypatch, sentinels) -> None:
    """검색 경계(`search_spaces`)만 스텁 — 후보 2건, 발췌에 sentinel."""
    source_id = str(uuid.uuid4())
    points = [
        _Point(
            id=str(uuid.uuid4()),
            score=0.9,
            payload={"source_id": source_id, "text": f"{sentinels['doc']} 발췌 {i}"},
        )
        for i in range(2)
    ]
    monkeypatch.setattr(
        "biznotes_core.services.embedding_spaces.search_spaces",
        _search_returning(points, sliced=True),
    )


@pytest.fixture
def rerank_server(monkeypatch) -> dict[str, Any]:
    """`gateway.rerank`의 HTTP 전송 경계만 바꾼다 — 요청을 기록하고 사례별로 응답한다."""
    state: dict[str, Any] = {"requests": [], "respond": None}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        return state["respond"](request)

    monkeypatch.setattr(
        "biznotes_core.services.gateway.rerank._http_client",
        lambda timeout: httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ),
    )
    monkeypatch.setattr("biznotes_core.services.gateway.rerank._sleep", lambda seconds: None)
    return state


def _forbidden(sentinels: dict[str, str]) -> list[str]:
    """사용자 응답·제품 로그 어디에도 나오면 안 되는 값."""
    return [
        sentinels["server"],
        sentinels["query"],
        sentinels["doc"],
        sentinels["credential"],
        "rerank.invalid",
        PROBE_REQUEST_MODEL,
        PROBE_DISPLAY_NAME,
    ]


def _assert_logs_clean(logs: list[dict[str, Any]], sentinels: dict[str, str]) -> None:
    """ⓒ 전체 캡처에 sentinel·질의·발췌·URL·자격증명 없음. 코어 진단 이벤트의 `model`만 예외."""
    for entry in logs:
        scrubbed = {
            key: value
            for key, value in entry.items()
            if not (entry.get("event") == CORE_RERANK_EVENT and key == "model")
        }
        text = repr(scrubbed)
        for value in _forbidden(sentinels):
            assert value not in text, entry


def _assert_reached_transport(requests: list[httpx.Request], case: str, sentinels) -> None:
    """실경로 확인 — 해석·게이트웨이 호출이 실제로 전송 경계까지 왔다."""
    assert len(requests) == EXPECTED_ATTEMPTS[case]
    first = requests[0]
    assert str(first.url) == f"{PROBE_BASE_URL}/rerank"
    assert first.headers["authorization"] == f"Bearer {sentinels['credential']}"
    body = json.loads(first.content)
    assert body["model"] == PROBE_REQUEST_MODEL
    assert sentinels["query"] in body["query"]
    assert all(sentinels["doc"] in doc for doc in body["documents"])


@pytest.mark.parametrize("case", list(CASES))
async def test_static_chat_gateway_rerank_failure_is_sanitized(
    case,
    client: httpx.AsyncClient,
    db,
    user_a,
    mock_llm,
    gateway_reranker,
    stub_search,
    rerank_server,
    sentinels,
    monkeypatch,
):
    """AC-2.1b: 호출·파싱 실패 → SSE 고정 문구, 제품 로그는 `error_type`만, 저장·사용량 없음."""
    rerank_server["respond"] = CASES[case](sentinels["server"])
    usage_calls: list[tuple[Any, ...]] = []
    original_record_usage = usage_guard.record_usage

    async def spy_record_usage(*args: Any, **kwargs: Any) -> Any:  # 관찰용 — 본문 대체 아님
        usage_calls.append(args)
        return await original_record_usage(*args, **kwargs)

    monkeypatch.setattr(usage_guard, "record_usage", spy_record_usage)

    token = await login(client, user_a.email)
    nb = await client.post("/api/v1/notebooks", json={"title": "rr-nb"}, headers=bearer(token))
    session = await client.post(
        f"/api/v1/notebooks/{nb.json()['id']}/chat/sessions", headers=bearer(token)
    )
    session_id = session.json()["id"]
    with capture_logs() as logs:
        res = await client.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            json={"content": f"{sentinels['query']} 에스프레소 추출 압력은?"},
            headers=bearer(token),
        )

    assert res.status_code == 200
    _assert_reached_transport(rerank_server["requests"], case, sentinels)
    # ⓐ 정적 SSE: error 1건, 고정 문구, sentinel·모델명·URL 없음
    events = [json.loads(line[6:]) for line in res.text.splitlines() if line.startswith("data: ")]
    assert [e["type"] for e in events] == ["error"]
    assert events[0]["detail"] == RERANK_UNAVAILABLE_DETAIL
    for value in _forbidden(sentinels):
        assert value not in res.text
    # ⓑ 제품 이벤트 정확히 1건, 키는 event·error_type·log_level뿐(exc_info 없음)
    product = [entry for entry in logs if entry.get("event") == PRODUCT_EVENT]
    assert len(product) == 1
    assert set(product[0]) == {"event", "error_type", "log_level"}
    assert product[0]["error_type"] == "RerankUnavailableError"
    assert product[0]["log_level"] == "warning"
    # ⓒ 전체 캡처 정제(코어 진단 이벤트의 모델명만 허용)
    _assert_logs_clean(logs, sentinels)
    core_events = [entry for entry in logs if entry.get("event") == CORE_RERANK_EVENT]
    if case == "http_500":
        assert [entry["error"] for entry in core_events] == ["HTTP 500"] * 3
    else:
        assert core_events == []  # 파싱 오류는 코어가 로그 없이 바로 올린다
    # ⓓ 메시지·사용량 기록 없음
    count = await db.execute(
        select(func.count())
        .select_from(ChatMessage)
        .where(ChatMessage.session_id == uuid.UUID(session_id))
    )
    assert count.scalar_one() == 0
    assert usage_calls == []


def _probe_app(notebook_id: uuid.UUID) -> FastAPI:
    """테스트 전용 비스트림 라우트 — 제품과 같은 `register_exception_handlers`로 매핑한다."""
    probe = FastAPI()
    register_exception_handlers(probe)

    @probe.get("/retrieve")
    async def retrieve(q: str) -> dict[str, int]:
        async with async_session_factory() as session:
            result = await rag_chat.retrieve_context(session, notebook_id, q, None)
        return {"citations": len(result.citations)}

    @probe.get("/gateway-error")
    async def gateway_error() -> dict[str, int]:
        raise GatewayError("upstream gateway detail")

    return probe


@pytest.mark.parametrize("case", list(CASES))
async def test_non_stream_route_maps_rerank_failure_to_fixed_503(
    case, db, user_a, gateway_reranker, stub_search, rerank_server, sentinels
):
    """AC-2.2: 라우트 밖으로 나간 `RerankUnavailableError` → 503 + 고정 공개 문구."""
    rerank_server["respond"] = CASES[case](sentinels["server"])
    notebook = Notebook(owner_id=user_a.id, title="rr-probe")
    db.add(notebook)
    await db.commit()
    query = f"{sentinels['query']} 질문"

    # 대조군: 같은 설정에서 코어 예외 원문은 표시명을 담는다 — 아래 부재 단언이 공허하지 않다는
    # 근거. 코어 v0.15.1(#217)부터 파싱 오류는 서버 반환값 대신 위반 종류·타입만 담는다
    with pytest.raises(RerankUnavailableError) as raised:
        await rag_chat.retrieve_context(db, notebook.id, query, None)
    assert PROBE_DISPLAY_NAME in str(raised.value)
    if case != "http_500":
        assert sentinels["server"] not in str(raised.value)
        assert "형식(str)" in str(raised.value)
    rerank_server["requests"].clear()

    transport = httpx.ASGITransport(app=_probe_app(notebook.id))
    async with httpx.AsyncClient(transport=transport, base_url="http://probe") as probe:
        res = await probe.get("/retrieve", params={"q": query})

    _assert_reached_transport(rerank_server["requests"], case, sentinels)
    assert res.status_code == 503
    assert res.json() == {"detail": RERANK_UNAVAILABLE_DETAIL}
    for value in _forbidden(sentinels):
        assert value not in res.text


async def test_existing_mapping_keeps_exception_message_body():
    """회귀: 공개 문구 맵 밖의 예외는 기존 `{"detail": str(exc)}` 계약 그대로(GatewayError 502)."""
    transport = httpx.ASGITransport(app=_probe_app(uuid.uuid4()))
    async with httpx.AsyncClient(transport=transport, base_url="http://probe") as probe:
        res = await probe.get("/gateway-error")

    assert res.status_code == 502
    assert res.json() == {"detail": "upstream gateway detail"}


async def test_product_app_maps_rerank_unavailable_to_fixed_503():
    """제품 앱에 같은 핸들러가 등록돼 있다 — 원문 메시지 대신 고정 문구."""
    handler = product_app.exception_handlers[RerankUnavailableError]
    exc = RerankUnavailableError("리랭커 X 응답이 계약과 다릅니다: 'SENTINEL'")
    response = await handler(None, exc)

    assert response.status_code == 503
    assert json.loads(response.body) == {"detail": RERANK_UNAVAILABLE_DETAIL}


class _UnprintableRerankError(RerankUnavailableError):
    """`str()`이 불리면 실패하는 하위 타입 — 핸들러가 예외 문자열을 평가하는지 드러낸다."""

    def __str__(self) -> str:
        raise AssertionError("공개 문구 타입의 예외 문자열을 평가했다")


async def test_fixed_detail_never_evaluates_exception_string():
    """리뷰 P2-03: 고정 문구 타입은 `str(exc)`를 아예 부르지 않는다(하위 타입도 MRO로 같은 문구)."""
    handler = product_app.exception_handlers[RerankUnavailableError]

    response = await handler(None, _UnprintableRerankError())

    assert response.status_code == 503
    assert json.loads(response.body) == {"detail": RERANK_UNAVAILABLE_DETAIL}


async def test_autonomous_rag_search_rerank_failure_still_reaches_done(
    client: httpx.AsyncClient,
    user_a,
    mock_llm,
    gateway_reranker,
    stub_search,
    rerank_server,
    sentinels,
):
    """AC-2.2 특성: 자율 모드의 `rag_search`가 리랭크 실패로 예외를 내도 SSE `error` 없이 `done`.

    코어 react 루프 계약(도구 실패는 모델에 일반 문구로 전달)을 고정한다. 루프가 서버 로그에
    남기는 원문(`error=str(e)`)에서 서버 값은 코어 v0.15.1(#217)로 빠졌다(HCI-15 잔여 해소).
    """
    rerank_server["respond"] = CASES["relevance_score_sentinel"](sentinels["server"])
    token = await login(client, user_a.email)
    nb = await client.post("/api/v1/notebooks", json={"title": "rr-auto"}, headers=bearer(token))
    session = await client.post(
        f"/api/v1/notebooks/{nb.json()['id']}/chat/sessions", headers=bearer(token)
    )

    res = await client.post(
        f"/api/v1/chat/sessions/{session.json()['id']}/messages",
        json={"content": f"{sentinels['query']} 커피 추출 과정을 조사해줘", "autonomous": True},
        headers=bearer(token),
    )

    assert res.status_code == 200
    _assert_reached_transport(rerank_server["requests"], "relevance_score_sentinel", sentinels)
    events = [json.loads(line[6:]) for line in res.text.splitlines() if line.startswith("data: ")]
    types = [e["type"] for e in events]
    assert "error" not in types
    assert types[-1] == "done"
    assert events[-1]["tool_calls"][0]["tool"] == "rag_search"
    assert events[-1]["tool_calls"][0]["status"] == "failed"
    assert sentinels["server"] not in res.text
