"""키 없는 OpenAI 호환 프로바이더 호출과 능력별 연결 테스트 (#193).

인증을 요구하지 않는 OpenAI 호환 서버를 로컬 HTTP 서버로 띄우고, 실제 LiteLLM으로
호출한다. 외부 경계(원격 임베딩·채팅 서버)만 가짜이고, 게이트웨이 파라미터 조립과
LiteLLM·OpenAI SDK 경로는 실경로다. 라우터 테스트의 DB 세션만 기록형 가짜다.
"""

import json
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest

from biznotes_core.api.v1.admin import llm as admin_llm
from biznotes_core.core.crypto import encrypt
from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.services import embedding_spaces as es
from biznotes_core.services.gateway.llm import ResolvedModel, probe_connection

EMBED_DIM = 3


class _Server:
    """`/v1` 아래에만 엔드포인트가 있는 무인증 OpenAI 호환 서버."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str | None]] = []
        self.bodies: list[dict[str, Any]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                body = json.loads(self.rfile.read(length))
                server.requests.append((self.path, self.headers.get("Authorization")))
                server.bodies.append(body)
                if self.path == "/v1/rerank":
                    results = [
                        {"index": i, "relevance_score": 0.9}
                        for i, _ in enumerate(body["documents"])
                    ]
                    self._send(200, {"results": results})
                elif self.path == "/v1/embeddings":
                    data = [
                        {"object": "embedding", "index": i, "embedding": [0.1] * EMBED_DIM}
                        for i, _ in enumerate(body["input"])
                    ]
                    self._send(200, {"object": "list", "model": body["model"], "data": data,
                                     "usage": {"prompt_tokens": 1, "total_tokens": 1}})
                elif self.path == "/v1/chat/completions":
                    self._send(200, {
                        "id": "c1", "object": "chat.completion", "created": 0,
                        "model": body["model"],
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": "pong"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    })
                else:
                    self._send(404, {"error": {"message": "Not Found"}})

            def _send(self, code: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args: Any) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.root = f"http://127.0.0.1:{self._httpd.server_port}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Server]:
    # 환경변수 키가 있으면 SDK가 그 값을 써서 재현이 가려진다
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    srv = _Server()
    yield srv
    srv.close()


def _resolved(
    provider_type: str,
    base_url: str | None = None,
    api_key: str | None = None,
    model_name: str = "openai/bge-m3",
    capability: str = "embedding",
) -> ResolvedModel:
    provider = LLMProvider(
        id=uuid.uuid4(),
        name=f"p-{provider_type}",
        type=provider_type,
        base_url=base_url,
        api_key_encrypted=encrypt(api_key) if api_key else None,
    )
    model = LLMModel(
        id=uuid.uuid4(),
        provider_id=provider.id,
        model_name=model_name,
        display_name=model_name,
        capabilities=[capability],
    )
    return ResolvedModel(model=model, provider=provider)


# ── 파라미터 조립 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("provider_type", ["custom", "vllm", "lmstudio"])
def test_keyless_openai_compatible_gets_placeholder_key(provider_type: str) -> None:
    params = _resolved(provider_type, base_url="http://gw/v1").litellm_params
    assert params["api_key"] == "not-needed"


@pytest.mark.parametrize("provider_type", ["custom", "vllm", "lmstudio"])
def test_stored_key_wins_over_placeholder(provider_type: str) -> None:
    params = _resolved(provider_type, base_url="http://gw/v1", api_key="test-secret").litellm_params
    assert params["api_key"] == "test-secret"


@pytest.mark.parametrize("provider_type", ["openai", "ollama", "anthropic"])
def test_other_types_without_key_pass_no_key(provider_type: str) -> None:
    # openai·anthropic은 환경변수 키를 쓸 수 있고, ollama는 OpenAI SDK를 타지 않는다
    assert "api_key" not in _resolved(provider_type).litellm_params


# ── 수락 기준: 무인증 서버에 키 없이 등록한 custom 프로바이더 ──────────────


def test_keyless_custom_embedding_space_embeds(server: _Server) -> None:
    space = es._gateway_space(_resolved("custom", base_url=f"{server.root}/v1"))

    vectors = space.embed(["가", "나"])

    assert vectors == [[0.1] * EMBED_DIM] * 2
    assert server.requests == [("/v1/embeddings", "Bearer not-needed")]


# ── 연결 테스트 ────────────────────────────────────────────────────────


async def test_probe_embedding_reports_real_call(server: _Server) -> None:
    result = await probe_connection(_resolved("custom", base_url=f"{server.root}/v1"), "embedding")

    assert result == {"ok": True, "detail": f"임베딩 수신: {EMBED_DIM}차원"}
    assert server.requests == [("/v1/embeddings", "Bearer not-needed")]


async def test_probe_embedding_without_v1_hints_base_url(server: _Server) -> None:
    # 탐색은 `/models`로 성공해도 `/embeddings`는 `/v1` 아래에만 있다
    result = await probe_connection(_resolved("custom", base_url=server.root), "embedding")

    assert result["ok"] is False
    assert result["detail"].startswith("404 — 모델명과 base_url에 `/v1`이 필요한지 확인하세요")
    assert server.requests == [("/embeddings", "Bearer not-needed")]


async def test_probe_llm_keeps_completion_check(server: _Server) -> None:
    resolved = _resolved(
        "custom", base_url=f"{server.root}/v1", model_name="openai/chat", capability="llm"
    )

    result = await probe_connection(resolved, "llm")

    assert result == {"ok": True, "detail": "응답 수신: chat"}
    assert server.requests == [("/v1/chat/completions", "Bearer not-needed")]


class _FakeDB:
    """`db.get`과 능력별 `db.execute(...).scalars().first()`만 흉내 낸다."""

    def __init__(self, provider: LLMProvider, by_capability: dict[str, LLMModel]) -> None:
        self.provider = provider
        self.by_capability = by_capability
        self.asked: list[str] = []

    async def get(self, cls: Any, ident: Any) -> LLMProvider:
        return self.provider

    async def execute(self, stmt: Any) -> Any:
        capability = next(
            v for k, v in stmt.compile().params.items() if k.startswith("param")
        )
        self.asked.append(capability)
        model = self.by_capability.get(capability)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: model))


async def test_router_tests_embedding_only_provider(server: _Server) -> None:
    resolved = _resolved("custom", base_url=f"{server.root}/v1")
    db = _FakeDB(resolved.provider, {"embedding": resolved.model})

    result = await admin_llm.test_provider(resolved.provider.id, db=db)  # type: ignore[arg-type]

    assert result == {"ok": True, "detail": f"임베딩 수신: {EMBED_DIM}차원"}
    assert db.asked == ["llm", "embedding"]


async def test_router_prefers_llm_model(server: _Server) -> None:
    emb = _resolved("custom", base_url=f"{server.root}/v1")
    chat = _resolved("custom", model_name="openai/chat", capability="llm").model
    db = _FakeDB(emb.provider, {"llm": chat, "embedding": emb.model})

    result = await admin_llm.test_provider(emb.provider.id, db=db)  # type: ignore[arg-type]

    assert result == {"ok": True, "detail": "응답 수신: chat"}
    assert db.asked == ["llm"]


async def test_router_without_testable_model(server: _Server) -> None:
    image = _resolved("custom", base_url=f"{server.root}/v1", capability="image")
    db = _FakeDB(image.provider, {"image": image.model})

    result = await admin_llm.test_provider(image.provider.id, db=db)  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["detail"].startswith("테스트할 LLM·임베딩·리랭커 모델이 등록되어 있지 않습니다")
    assert db.asked == ["llm", "embedding", "reranker"]
    assert server.requests == []


async def test_router_explains_image_only_provider(server: _Server) -> None:
    """이미지 생성 모델만 있으면 생성 호출 없이(비용) 이미지 모델을 안내한다(#347)."""
    img = _resolved("custom", base_url=f"{server.root}/v1", capability="image")
    db = _FakeDB(img.provider, {"image": img.model})

    result = await admin_llm.test_provider(img.provider.id, db=db)  # type: ignore[arg-type]

    assert result["ok"] is False
    assert (
        "이미지 생성·STT·TTS 모델은 호출마다 비용이 들어 연결 테스트를 하지 않습니다"
        in (result["detail"])
    )
    assert "모델 가져오기 목록" in result["detail"]
    assert db.asked == ["llm", "embedding", "reranker"]  # image는 시험 대상이 아니다
    assert server.requests == []


async def test_router_tests_reranker_only_provider(server: _Server) -> None:
    """리랭커만 있는 프로바이더는 `/rerank` 1건으로 시험한다(#209). 요청 본문의 모델은
    litellm 접두사(`openai/`)를 뗀 서버 id이고, 키 없는 프로바이더는 자리표시 키를 쓴다."""
    rr = _resolved(
        "custom",
        base_url=f"{server.root}/v1",
        model_name="openai/bge-reranker-v2-m3",
        capability="reranker",
    )
    db = _FakeDB(rr.provider, {"reranker": rr.model})

    result = await admin_llm.test_provider(rr.provider.id, db=db)  # type: ignore[arg-type]

    assert result == {"ok": True, "detail": "리랭크 점수 수신: 0.900"}
    assert db.asked == ["llm", "embedding", "reranker"]
    assert server.requests == [("/v1/rerank", "Bearer not-needed")]
    assert server.bodies == [
        {"model": "bge-reranker-v2-m3", "query": "ping", "documents": ["pong"], "top_n": 1}
    ]


async def test_router_reranker_probe_reports_unsupported_provider(server: _Server) -> None:
    rr = _resolved("ollama", base_url=server.root, model_name="x-rerank", capability="reranker")
    db = _FakeDB(rr.provider, {"reranker": rr.model})

    result = await admin_llm.test_provider(rr.provider.id, db=db)  # type: ignore[arg-type]

    assert result["ok"] is False
    assert "ollama" in result["detail"]
    assert server.requests == []
