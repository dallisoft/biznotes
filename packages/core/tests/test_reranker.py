"""리랭커 1급 모달리티 (#209) — 게이트웨이 클라이언트·점수 계약·RAG 분기·입력 검증.

게이트웨이 HTTP 경계만 `httpx.MockTransport`로 바꾼다. 재시도 분류·응답 검증·점수 정규화·
`retrieve_context` 분기는 실경로다. DB가 필요한 해석(`resolve_reranker`)·어드민 규칙은
`tests/test_reranker_pg.py`가 본다.
"""

import math
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.services import embedding_spaces, rag_chat, rerank, settings_store
from biznotes_core.services.embedding_spaces import LOCAL_SPACE
from biznotes_core.services.gateway import rerank as gw
from biznotes_core.services.gateway.llm import ResolvedModel
from biznotes_core.services.rerank import RerankTarget, RerankUnavailableError

# ── 게이트웨이 대역 ──────────────────────────────────────────────────────


class _Gateway:
    """응답을 차례로 돌려주는 `/rerank` 서버 대역. 요청을 기록한다."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *responses: Any) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        def client(timeout: float) -> httpx.Client:
            self.timeouts.append(timeout)
            return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)

        self.timeouts: list[float] = []
        monkeypatch.setattr(gw, "_http_client", client)
        monkeypatch.setattr(gw, "_sleep", self.sleeps.append)

    def body(self, i: int = 0) -> dict[str, Any]:
        import json

        return json.loads(self.requests[i].content)


def _ok(scores: list[float]) -> httpx.Response:
    return httpx.Response(
        200, json={"results": [{"index": i, "relevance_score": s} for i, s in enumerate(scores)]}
    )


def _call(timeout: float = 30.0) -> gw.RerankCall:
    return gw.RerankCall(
        api_base="http://gw.test/v1",
        model="bge-reranker-v2-m3",
        display_name="bge-reranker-v2-m3",
        timeout=timeout,
        api_key="test-secret",
    )


# ── 클라이언트: 점수 계약 ───────────────────────────────────────────────


def test_gateway_scores_pass_through_without_a_second_sigmoid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """이슈 검증 4 — 0.9는 0.9다. 0.71(sigmoid(0.9))이 나오면 이중 정규화다."""
    srv = _Gateway(monkeypatch, _ok([0.9, 0.1]))
    target = RerankTarget(key="k", display_name="d", gateway=_call())

    assert rerank.rerank_relevance(target, "q", ["a", "b"]) == [0.9, 0.1]
    req = srv.requests[0]
    assert str(req.url) == "http://gw.test/v1/rerank"
    assert req.headers["authorization"] == "Bearer test-secret"
    assert srv.body() == {
        "model": "bge-reranker-v2-m3",
        "query": "q",
        "documents": ["a", "b"],
        "top_n": 2,
    }


def test_results_are_mapped_back_to_input_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """서버는 점수 순으로 돌려준다 — 인덱스로 입력 순서에 맞춘다."""
    _Gateway(
        monkeypatch,
        httpx.Response(
            200,
            json={
                "results": [
                    {"index": 2, "relevance_score": 0.8},
                    {"index": 0, "relevance_score": 0.5},
                    {"index": 1, "relevance_score": 0.2},
                ]
            },
        ),
    )
    assert gw.rerank(_call(), "q", ["a", "b", "c"]) == [0.5, 0.2, 0.8]


def test_no_documents_means_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    srv = _Gateway(monkeypatch)
    assert gw.rerank(_call(), "q", []) == []
    assert srv.requests == []


@pytest.mark.parametrize(
    "results",
    [
        [{"index": 0, "relevance_score": 0.5}],  # 누락
        [{"index": 0, "relevance_score": 0.5}, {"index": 0, "relevance_score": 0.4}],  # 중복
        [{"index": 0, "relevance_score": 0.5}, {"index": 2, "relevance_score": 0.4}],  # 범위 밖
        [{"index": 0, "relevance_score": 0.5}, {"index": 1}],  # 점수 없음
        [{"index": 0, "relevance_score": 0.5}, {"index": 1, "relevance_score": "0.4"}],
        [{"index": 0, "relevance_score": 0.5}, {"index": 1, "relevance_score": True}],
        [{"index": 0, "relevance_score": 0.5}, {"index": 1, "relevance_score": "NaN!"}],
        [{"index": 0, "relevance_score": 0.5}, {"index": 1, "relevance_score": 1.5}],
        [{"index": 0, "relevance_score": 0.5}, {"index": 1, "relevance_score": -0.1}],
        [{"index": True, "relevance_score": 0.5}, {"index": 1, "relevance_score": 0.4}],
    ],
)
def test_contract_violations_are_errors_not_clamped(
    monkeypatch: pytest.MonkeyPatch, results: list[dict[str, Any]]
) -> None:
    """[0,1] 밖이나 누락을 잘라 내면 임계값 판정이 조용히 틀린다. 다시 보내지도 않는다."""
    import json

    # NaN은 JSON 리터럴이 아니지만 일부 서버가 그대로 낸다(파이썬 json은 읽는다)
    raw = json.dumps({"results": results}).replace('"NaN!"', "NaN")
    srv = _Gateway(monkeypatch, httpx.Response(200, content=raw.encode()))
    with pytest.raises(RerankUnavailableError, match="계약"):
        gw.rerank(_call(), "q", ["a", "b"])
    assert len(srv.requests) == 1


def test_missing_results_list_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _Gateway(monkeypatch, httpx.Response(200, json={"data": []}))
    with pytest.raises(RerankUnavailableError):
        gw.rerank(_call(), "q", ["a"])


# 서버가 index·relevance_score 자리에 보낸 값 — 어떤 출력에도 실려서는 안 된다(#217)
_SENTINEL = "SENTINEL-217-" + "x" * 64


@pytest.mark.parametrize(
    ("results", "kind"),
    [
        ([{"index": _SENTINEL, "relevance_score": 0.5}], "잘못된 index 형식(str)"),
        ([{"index": 0, "relevance_score": _SENTINEL}], "잘못된 relevance_score 형식(str)"),
        ([{"index": 0, "relevance_score": [_SENTINEL]}], "잘못된 relevance_score 형식(list)"),
        ([{"index": 217217217, "relevance_score": 0.5}], "index 범위 밖"),
        ([{"index": 0, "relevance_score": 217217.217}], "relevance_score가 [0,1] 밖"),
        (
            [{"index": 0, "relevance_score": 0.5}, {"index": 0, "relevance_score": 0.4}],
            "중복 index: 0",
        ),
        ([{"index": 0, "relevance_score": 0.5}], "점수가 빠진 문서 2건"),
    ],
)
def test_contract_violation_message_names_the_kind_without_server_values(
    monkeypatch: pytest.MonkeyPatch, results: list[dict[str, Any]], kind: str
) -> None:
    """#217 — 위반 종류·타입만 남기고 서버가 보낸 값은 메시지에 싣지 않는다."""
    _Gateway(monkeypatch, httpx.Response(200, json={"results": results}))
    with pytest.raises(RerankUnavailableError) as exc:
        gw.rerank(_call(), "q", ["a", "b", "c"])
    message = str(exc.value)
    assert kind in message
    for leaked in ("SENTINEL", "217217"):
        assert leaked not in message


async def test_probe_detail_does_not_echo_server_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#217 — 관리자 연결 테스트 응답 `detail`에 서버 값이 실리지 않는다."""
    from biznotes_core.api.v1.admin.llm import _probe_reranker

    body = {"results": [{"index": _SENTINEL, "relevance_score": _SENTINEL}]}
    _Gateway(monkeypatch, httpx.Response(200, json=body))

    out = await _probe_reranker(_resolved("custom", "openai/bge-reranker-v2-m3"))

    assert out["ok"] is False
    assert "잘못된 index 형식(str)" in out["detail"]
    assert "SENTINEL" not in out["detail"]


async def test_react_tool_failure_log_does_not_echo_server_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#217 — 자율 모드 rag_search의 리랭크 실패 경고 로그에 서버 값이 실리지 않는다.

    `retrieve_context`만 대역으로 두고, 그 안에서 실제 게이트웨이 클라이언트·파서를 탄다.
    """
    import structlog.testing

    from biznotes_core.services.agents.context import RunContext
    from biznotes_core.services.agents.react import loop
    from biznotes_core.services.gateway import llm as gateway_llm
    from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult

    body = {"results": [{"index": 0, "relevance_score": _SENTINEL}]}
    _Gateway(monkeypatch, httpx.Response(200, json=body))
    target = RerankTarget(key="k", display_name="d", gateway=_call())

    async def fake_retrieve(
        db: Any, notebook_id: Any, query: str, source_ids: Any, top_k: int = 6
    ) -> Any:
        rerank.rerank_relevance(target, query, ["a"])
        raise AssertionError("리랭크가 실패해야 한다")

    async def fake_setting(db: Any, key: str) -> dict[str, Any]:
        return dict(settings_store.DEFAULTS[key])

    turns: list[int] = []

    async def model(db: Any, model_id: Any, messages: Any, tools: Any, **kw: Any) -> Any:
        turns.append(1)
        if len(turns) == 1:
            fn = {"name": "rag_search", "arguments": '{"query": "q"}'}
            return ToolTurnResult(
                tool_calls=[ToolCall(id="m1", name="rag_search", arguments={"query": "q"})],
                assistant_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "m1", "type": "function", "function": fn}],
                },
            )
        done = {"role": "assistant", "content": "끝"}
        return ToolTurnResult(content="끝", assistant_message=done)

    async def no_log(*args: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(rag_chat, "retrieve_context", fake_retrieve)
    monkeypatch.setattr(settings_store, "get_setting", fake_setting)
    monkeypatch.setattr(gateway_llm, "complete_with_tools", model)
    monkeypatch.setattr(loop, "_log_node_start", no_log)
    monkeypatch.setattr(loop, "_log_node_finish", no_log)
    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=None,  # type: ignore[arg-type]
        params={},
        trigger="chat",
        chat_message="질문",
    )
    node = {"id": "r", "type": "react_agent", "goal": "답하라", "allowed_tools": ["rag_search"]}

    with structlog.testing.capture_logs() as logs:
        await loop.run_react_node(ctx, node, {})

    failures = [e for e in logs if e["event"] == "도구 실행 실패"]
    assert len(failures) == 1
    assert "잘못된 relevance_score 형식(str)" in failures[0]["error"]
    assert "SENTINEL" not in repr(logs)


# ── 클라이언트: 재시도·타임아웃 (벽시계 상한 하나) ─────────────────────


def test_503_then_success_retries_once_after_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    srv = _Gateway(
        monkeypatch,
        httpx.Response(503, headers={"Retry-After": "1"}),
        _ok([0.7]),
    )
    assert gw.rerank(_call(), "q", ["a"]) == [0.7]
    assert len(srv.requests) == 2
    assert srv.sleeps == [1.0]


def test_server_errors_stop_at_three_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """이슈 검증 5 — 500이 이어지면 관찰 가능한 오류로 끝난다."""
    srv = _Gateway(monkeypatch, *(httpx.Response(500) for _ in range(5)))
    with pytest.raises(RerankUnavailableError, match="HTTP 500"):
        gw.rerank(_call(), "q", ["a"])
    assert len(srv.requests) == 3
    assert len(srv.sleeps) == 2


def test_timeout_is_not_resent(monkeypatch: pytest.MonkeyPatch) -> None:
    """채팅 첫 토큰 경로 — 타임아웃 뒤 같은 요청을 다시 보내 서버 큐에 쌓지 않는다."""
    srv = _Gateway(monkeypatch, httpx.ReadTimeout("slow"), _ok([0.7]))
    with pytest.raises(RerankUnavailableError, match="timeout"):
        gw.rerank(_call(), "q", ["a"])
    assert len(srv.requests) == 1
    # 요청 timeout은 남은 벽시계 예산이다(상한 30초 하나)
    assert 29 < srv.timeouts[0] <= 30


def test_retry_after_beyond_the_budget_fails_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    srv = _Gateway(monkeypatch, httpx.Response(429, headers={"Retry-After": "60"}), _ok([0.7]))
    with pytest.raises(RerankUnavailableError, match="HTTP 429"):
        gw.rerank(_call(timeout=5), "q", ["a"])
    assert len(srv.requests) == 1
    assert srv.sleeps == []


@pytest.mark.parametrize("code", [400, 401, 404, 422])
def test_client_errors_are_not_retried(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    srv = _Gateway(monkeypatch, httpx.Response(code, text="secret-echo"), _ok([0.7]))
    with pytest.raises(RerankUnavailableError) as exc:
        gw.rerank(_call(), "q", ["a"])
    assert len(srv.requests) == 1
    # 응답 본문은 오류에 싣지 않는다
    assert "secret-echo" not in str(exc.value)


def test_snapshot_repr_hides_the_key() -> None:
    assert "test-secret" not in repr(_call())


# ── 해석 스냅샷: 접두사·지원 프로바이더 (B1·M2) ─────────────────────────


def _resolved(
    provider_type: str, model_name: str, base_url: str | None = "http://gw.test/v1"
) -> ResolvedModel:
    provider = LLMProvider(
        id=uuid.uuid4(), name="p", type=provider_type, base_url=base_url, api_key_encrypted=None
    )
    model = LLMModel(
        id=uuid.uuid4(),
        provider_id=provider.id,
        model_name=model_name,
        display_name=model_name,
        capabilities=["reranker"],
    )
    return ResolvedModel(model=model, provider=provider)


@pytest.mark.parametrize(
    ("provider_type", "model_name", "expected"),
    [
        ("custom", "openai/bge-reranker-v2-m3", "bge-reranker-v2-m3"),
        ("custom", "openai/BAAI/bge-reranker-v2-m3", "BAAI/bge-reranker-v2-m3"),
        ("vllm", "hosted_vllm/BAAI/bge-reranker-v2-m3", "BAAI/bge-reranker-v2-m3"),
        # 수동 등록 — 자기 타입의 접두사가 아니면 떼지 않는다(r1)
        ("vllm", "BAAI/bge-reranker-v2-m3", "BAAI/bge-reranker-v2-m3"),
        ("lmstudio", "lm_studio/jina-reranker", "jina-reranker"),
        ("openai", "rerank-1", "rerank-1"),
    ],
)
def test_request_model_drops_only_the_litellm_routing_prefix(
    provider_type: str, model_name: str, expected: str
) -> None:
    target = rerank.gateway_target(_resolved(provider_type, model_name), 30)
    assert target.gateway is not None
    assert target.gateway.model == expected
    assert target.key == f"gateway:{target_model_id(target)}:{model_name}"


def target_model_id(target: RerankTarget) -> str:
    return target.key.removeprefix("gateway:").partition(":")[0]


@pytest.mark.parametrize("provider_type", ["ollama", "anthropic", "google", "bedrock", "mock"])
def test_unsupported_provider_types_fail_at_resolution(provider_type: str) -> None:
    with pytest.raises(RerankUnavailableError, match=provider_type):
        rerank.gateway_target(_resolved(provider_type, "x-rerank"), 30)


@pytest.mark.parametrize("provider_type", ["custom", "vllm"])
def test_missing_base_url_does_not_fall_back_to_openai(provider_type: str) -> None:
    """기본 base로 떨어지면 사내 키가 외부로 나간다 — 보내지 않고 오류(r2)."""
    with pytest.raises(RerankUnavailableError, match="base_url"):
        rerank.gateway_target(_resolved(provider_type, "x-rerank", base_url=None), 30)


def test_keyless_compatible_provider_sends_the_placeholder_key() -> None:
    target = rerank.gateway_target(_resolved("custom", "openai/r"), 30)
    assert target.gateway is not None and target.gateway.api_key == "not-needed"


# ── 로컬 경로 ───────────────────────────────────────────────────────────


def test_local_logits_get_exactly_one_sigmoid(monkeypatch: pytest.MonkeyPatch) -> None:
    """이슈 검증 6 — `fastembed:` 명시는 기존 로컬 경로 그대로, 점수만 [0,1]."""
    seen: list[str] = []

    def scores(query: str, texts: list[str], model_name: str = "") -> list[float]:
        seen.append(model_name)
        return [0.0, 2.0, -800.0]

    monkeypatch.setattr(rerank, "rerank_scores", scores)
    target = RerankTarget(key="fastembed:m", display_name="m", local_model="m")

    out = rerank.rerank_relevance(target, "q", ["a", "b", "c"])

    assert seen == ["m"]
    assert out[0] == 0.5
    assert out[1] == pytest.approx(1 / (1 + math.exp(-2.0)))
    assert out[2] == 0.0  # 큰 음수에서 넘치지 않는다


def test_local_failure_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*a: Any, **kw: Any) -> list[float]:
        raise OSError("model download blocked")

    monkeypatch.setattr(rerank, "rerank_scores", broken)
    target = RerankTarget(key="fastembed:m", display_name="m", local_model="m")
    with pytest.raises(RerankUnavailableError) as exc:
        rerank.rerank_relevance(target, "q", ["a"])
    assert isinstance(exc.value.__cause__, OSError)


# ── 설정값 형식 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "normalized"),
    [
        ("", ""),
        ("BAAI/bge-reranker-base", "fastembed:BAAI/bge-reranker-base"),
        ("fastembed:x", "fastembed:x"),
        ("gateway:abc:n", "gateway:abc:n"),
    ],
)
def test_legacy_values_are_read_as_fastembed(value: str, normalized: str) -> None:
    assert rerank.normalize_rerank_key(value) == normalized


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("", True),
        ("BAAI/bge-reranker-base", True),
        ("fastembed:x", True),
        (f"gateway:{uuid.uuid4()}:bge-reranker-v2-m3", True),
        ("gateway:not-a-uuid:x", False),
        (f"gateway:{uuid.uuid4()}", False),
        (f"gateway:{uuid.uuid4()}:", False),
        ("fastembed:", False),
    ],
)
def test_admin_rerank_model_validation(value: str, valid: bool) -> None:
    from biznotes_core.api.v1.admin.rag import RagSettings

    base = {"chunk_size": 800, "chunk_overlap": 100, "top_k": 6}
    if valid:
        # 입력 그대로 저장한다 — 접두사 없는 값을 바꾸면 구 코어 롤백이 깨진다(r4)
        assert RagSettings(**base, rerank_model=value).rerank_model == value
    else:
        with pytest.raises(ValidationError):
            RagSettings(**base, rerank_model=value)


def test_rerank_timeout_is_bounded_and_defaulted() -> None:
    from biznotes_core.api.v1.admin.rag import _EXPLICIT_ONLY_FIELDS, RagSettings

    base = {"chunk_size": 800, "chunk_overlap": 100, "top_k": 6}
    assert RagSettings(**base).rerank_timeout_seconds == 30
    assert settings_store.DEFAULTS["rag"]["rerank_timeout_seconds"] == 30
    assert "rerank_timeout_seconds" in _EXPLICIT_ONLY_FIELDS
    for bad in (4, 181):
        with pytest.raises(ValidationError):
            RagSettings(**base, rerank_timeout_seconds=bad)


# ── 능력 검증 (M6) ──────────────────────────────────────────────────────


def test_reranker_capability_is_accepted_alone() -> None:
    """이슈 검증 3 — 등록·수정·가져오기 모두 같은 검증을 쓴다."""
    from biznotes_core.schemas.llm import ModelCreate

    body = ModelCreate(
        provider_id=uuid.uuid4(),
        model_name="openai/bge-reranker-v2-m3",
        display_name="r",
        capabilities=["reranker"],
    )
    assert body.capabilities == ["reranker"]


@pytest.mark.parametrize("caps", [["llm", "reranker"], ["reranker", "embedding"]])
def test_reranker_cannot_share_a_row_with_other_capabilities(caps: list[str]) -> None:
    """`is_default`는 능력 사이에 공유된다 — 섞이면 기본값이 다른 능력으로 샌다."""
    from biznotes_core.schemas.llm import ModelCreate

    with pytest.raises(ValidationError, match="reranker"):
        ModelCreate(
            provider_id=uuid.uuid4(), model_name="m", display_name="m", capabilities=caps
        )


def test_default_reranker_index_is_in_metadata() -> None:
    from biznotes_core.models import Base

    names = {i.name for i in Base.metadata.tables["llm_models"].indexes}
    assert "uq_llm_models_default_reranker" in names


# ── retrieve_context 분기 ───────────────────────────────────────────────


def _point(text: str, score: float) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(), score=score, payload={"source_id": str(uuid.uuid4()), "text": text}
    )


class _DB:
    async def execute(self, stmt: Any) -> Any:
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: []),
            tuples=lambda: SimpleNamespace(all=lambda: []),
        )


@pytest.fixture
def search(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"rag": {}, "points": [], "resolved": []}

    async def get_setting(db: Any, key: str) -> dict[str, Any]:
        return state["rag"]

    async def fake_spaces(db: Any, notebook_ids: Any, source_ids: Any = None) -> list[Any]:
        return [LOCAL_SPACE]

    async def fake_search(db: Any, spaces: Any, query: str, **kw: Any) -> list[Any]:
        return list(state["points"])

    async def fake_resolve(db: Any, value: Any, rerank_timeout: float = 30) -> RerankTarget:
        state["resolved"].append((value, rerank_timeout))
        return RerankTarget(key="gateway:x:y", display_name="y", gateway=_call(rerank_timeout))

    monkeypatch.setattr(settings_store, "get_setting", get_setting)
    monkeypatch.setattr(embedding_spaces, "spaces_in_scope", fake_spaces)
    monkeypatch.setattr(embedding_spaces, "search_spaces", fake_search)
    monkeypatch.setattr(rerank, "resolve_reranker", fake_resolve)
    return state


async def _retrieve() -> rag_chat.RetrievalResult:
    return await rag_chat.retrieve_context(_DB(), uuid.uuid4(), "q", source_ids=None)  # type: ignore[arg-type]


async def test_gateway_score_is_compared_to_the_threshold_as_is(
    search: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """임계값 0.85 — 0.9가 그대로면 근거 충분, sigmoid(0.9)=0.71이면 근거 부족으로 뒤집힌다."""
    search["rag"] = {
        "top_k": 2,
        "rerank_enabled": True,
        "insufficient_context_threshold_rerank": 0.85,
        "rerank_timeout_seconds": 12,
    }
    search["points"] = [_point("low", 0.99), _point("high", 0.01)]
    _Gateway(monkeypatch, _ok([0.2, 0.9]))

    result = await _retrieve()

    assert [c.quote for c in result.citations] == ["high", "low"]
    assert result.insufficient_context is False
    # 키가 없으면 출고 기본값으로 읽는다(자동 해석으로 DB를 조회하지 않는다, m3)
    assert search["resolved"] == [("BAAI/bge-reranker-base", 12.0)]


async def test_gateway_failure_is_raised_not_swallowed(
    search: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """이슈 검증 5 — 하이브리드 점수로 조용히 넘어가지 않는다."""
    search["rag"] = {"rerank_enabled": True}
    search["points"] = [_point("a", 0.9)]
    _Gateway(monkeypatch, *(httpx.Response(500) for _ in range(3)))

    with pytest.raises(RerankUnavailableError):
        await _retrieve()


async def test_no_candidates_means_no_resolution(search: dict[str, Any]) -> None:
    """후보가 없으면 리랭커를 해석하지 않는다 — 리랭커 장애가 빈 검색까지 막지 않는다."""
    search["rag"] = {"rerank_enabled": True}

    result = await _retrieve()

    assert result.insufficient_context is True
    assert search["resolved"] == []


async def test_rerank_disabled_never_resolves(search: dict[str, Any]) -> None:
    search["rag"] = {"rerank_enabled": False, "insufficient_context_threshold_base": 0.5}
    search["points"] = [_point("a", 0.9)]

    result = await _retrieve()

    assert result.insufficient_context is False
    assert search["resolved"] == []
