"""이미지 생성 모달리티·게이트웨이·`image_generate` 노드 (#338).

계획 `docs/plans/image-generation-modality.md` 수락 기준이다. litellm은 치환하지 않는다 — 외부
경계(프로바이더 HTTP 서버)만 로컬 가짜 서버로 대신한다. 노드 시험은 저장(스토리지)과 기본 모델
조회(DB)만 대역을 쓰고, 게이트웨이·검증·비용 계산은 실경로다.
"""

import base64
import json
import struct
import threading
import uuid
import zlib
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from biznotes_core.core.crypto import encrypt
from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.models.llm import MODALITIES
from biznotes_core.schemas.llm import ModelCreate, ModelImportItem, ModelUpdate
from biznotes_core.services import storage
from biznotes_core.services.agents import graph, prompt_provenance
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError
from biznotes_core.services.agents.nodes import composite, retrieval
from biznotes_core.services.agents.nodes import image as image_node
from biznotes_core.services.gateway import discovery
from biznotes_core.services.gateway import image as gateway_image
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import GatewayCallError, GatewayError, ResolvedModel
from biznotes_core.services.untrusted import wrap_untrusted

# 노드 환경이 기본 모델 조회를 대역으로 바꾸기 전의 실함수
_REAL_RESOLVE = gateway_llm.resolve_model


def _png(side: int = 2) -> bytes:
    return gateway_image._mock_png(0, side)


PNG = _png()
# SOI · SOF0(1×1, 성분 1) · SOS · 압축 데이터 · EOI — 구조 검사를 통과하는 최소 JPEG
JPEG = (
    b"\xff\xd8"
    + b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    + b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
    + b"\x00\x00"
    + b"\xff\xd9"
)
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24


def _b64(data: bytes) -> dict[str, str]:
    return {"b64_json": base64.b64encode(data).decode()}


# ── 수락 기준 1·2: 모달리티·어드민 계약 ───────────────────────────────────


def test_image_is_a_modality_with_its_own_default_index() -> None:
    assert "image" in MODALITIES
    names = {index.name for index in LLMModel.__table__.indexes}
    assert "uq_llm_models_default_image" in names


@pytest.mark.parametrize("caps", [["llm", "image"], ["image", "vlm"], ["image", "reranker"]])
def test_image_cannot_share_a_row_with_other_capabilities(caps: list[str]) -> None:
    body = {"provider_id": str(uuid.uuid4()), "model_name": "m", "display_name": "d"}
    with pytest.raises(ValidationError, match="능력은 다른 능력과 함께 둘 수 없습니다"):
        ModelCreate.model_validate({**body, "capabilities": caps})


@pytest.mark.parametrize("caps", [["llm", "image"], ["image", "vlm"]])
def test_import_items_keep_image_exclusive(caps: list[str]) -> None:
    """모델 가져오기 항목도 단건 등록과 같은 단독 능력 규칙이다(#347)."""
    with pytest.raises(ValidationError, match="능력은 다른 능력과 함께 둘 수 없습니다"):
        ModelImportItem.model_validate(
            {"model_name": "m", "display_name": "d", "capabilities": caps}
        )
    item = ModelImportItem.model_validate(
        {"model_name": "gpt-image-1", "display_name": "d", "capabilities": ["image"]}
    )
    assert item.capabilities == ["image"]


def test_per_image_price_is_accepted_and_still_validated() -> None:
    body = {"provider_id": str(uuid.uuid4()), "model_name": "m", "display_name": "d"}
    created = ModelCreate.model_validate(
        {**body, "capabilities": ["image"], "cost_per_1k": {"per_image": 0.04}}
    )
    assert created.cost_per_1k == {"per_image": 0.04}
    assert ModelUpdate.model_validate({"cost_per_1k": {"per_image": 0}}).cost_per_1k == {
        "per_image": 0
    }

    with pytest.raises(ValidationError, match="per_image는 음수일 수 없습니다"):
        ModelUpdate.model_validate({"cost_per_1k": {"per_image": -0.01}})
    with pytest.raises(ValidationError, match=r"input/output/per_image 키만.*per_second"):
        ModelUpdate.model_validate({"cost_per_1k": {"per_second": 1}})


def test_structural_check_accepts_complete_files_only() -> None:
    assert gateway_image.image_content_type(PNG) == "image/png"
    assert gateway_image.image_content_type(JPEG) == "image/jpeg"
    assert gateway_image.image_content_type(b"\xff\xd8\xff\xd9") is None  # SOI·EOI만
    no_idat = PNG[:33] + PNG[-12:]  # IHDR 다음 바로 IEND
    zero_width = (
        PNG[:8]
        + gateway_image._png_chunk(b"IHDR", struct.pack(">IIBBBBB", 0, 8, 8, 2, 0, 0, 0))
        + PNG[33:]
    )
    bad_crc = PNG[:29] + bytes([PNG[29] ^ 1]) + PNG[30:]
    trailing = PNG + b"\x00"
    for data in (no_idat, zero_width, bad_crc, trailing):
        assert gateway_image.image_content_type(data) is None


def test_discovery_type_image_routes_to_the_image_modality() -> None:
    assert discovery._type_capabilities("image", "vendor/gen") == ["image"]


# ── 게이트웨이: 상한 ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("prompt", "n", "size", "reason"),
    [
        ("", 1, "1024x1024", "비어 있습니다"),
        ("  \n", 1, "1024x1024", "비어 있습니다"),
        ("x" * 4001, 1, "1024x1024", "상한\\(4000자\\)"),
        ("p", 0, "1024x1024", "1~4 정수"),
        ("p", 5, "1024x1024", "1~4 정수"),
        ("p", True, "1024x1024", "1~4 정수"),
        ("p", "2", "1024x1024", "1~4 정수"),
        ("p", 1.0, "1024x1024", "1~4 정수"),
        ("p", 1, "512x512", "크기는"),
        ("p", 1, "4096x4096", "크기는"),
        ("p", 1, "1024X1024", "크기는"),
    ],
)
async def test_requests_over_the_caps_are_rejected_before_any_call(
    prompt: str, n: Any, size: str, reason: str
) -> None:
    resolved = _mock_resolved()
    with pytest.raises(GatewayError, match=reason):
        await gateway_image.generate_images(resolved, prompt, n=n, size=size)


def test_prompt_at_the_cap_is_allowed() -> None:
    gateway_image.check_image_request("x" * 4000, 4, "1536x1024")


# ── 게이트웨이: mock 프로바이더(결정적) ───────────────────────────────────


def _mock_resolved(cost: dict[str, Any] | None = None) -> ResolvedModel:
    return ResolvedModel(
        model=LLMModel(
            model_name="mock-image",
            display_name="mock",
            capabilities=["image"],
            cost_per_1k=cost or {},
        ),
        provider=LLMProvider(name="mock", type="mock"),
    )


def _png_size(data: bytes) -> tuple[int, int]:
    """PNG를 청크 단위로 읽어 CRC와 IDAT 압축을 확인하고 (가로, 세로)를 돌려준다."""
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    pos, kinds, idat = 8, [], b""
    width = height = 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind, payload = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(kind + payload)
        if kind == b"IHDR":
            width, height = struct.unpack(">II", payload[:8])
        if kind == b"IDAT":
            idat += payload
        kinds.append(kind)
        pos += 12 + length
    assert kinds == [b"IHDR", b"IDAT", b"IEND"]
    assert len(zlib.decompress(idat)) == height * (1 + width * 3)
    return width, height


async def test_mock_provider_returns_n_distinct_valid_pngs() -> None:
    result = await gateway_image.generate_images(_mock_resolved(), "커피", n=3)

    assert [i.content_type for i in result.images] == ["image/png"] * 3
    assert all(_png_size(i.data) == (8, 8) for i in result.images)
    assert len({i.data for i in result.images}) == 3
    assert result.usage == {}


# ── 게이트웨이: 실제 프로바이더 경로(가짜 HTTP 서버) ───────────────────────


class _FakeProvider:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.items: list[dict[str, str]] = [_b64(PNG)]
        self.usage: dict[str, Any] | None = None
        self.status = 200
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                headers = {k.lower(): v for k, v in self.headers.items()}
                fake.requests.append({"path": self.path, "headers": headers, "body": body})
                if not self.path.endswith("/images/generations") or fake.status != 200:
                    self.send_response(fake.status if fake.status != 200 else 404)
                    self.end_headers()
                    return
                response: dict[str, Any] = {"created": 1, "data": fake.items}
                if fake.usage is not None:
                    response["usage"] = fake.usage
                self._reply(json.dumps(response).encode(), "application/json")

            def do_GET(self) -> None:
                self._reply(PNG, "image/png")

            def _reply(self, payload: bytes, content_type: str) -> None:
                self.send_response(200)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeProvider]:
    # 환경변수 키로 통과하는 경로를 막는다 — 키는 DB 프로바이더에서만 와야 한다
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    fake = _FakeProvider()
    thread = threading.Thread(target=fake.server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    yield fake
    fake.server.shutdown()
    fake.server.server_close()


def _openai(fake: _FakeProvider, cost: dict[str, Any] | None = None) -> ResolvedModel:
    return ResolvedModel(
        model=LLMModel(
            model_name="openai/gpt-image-1",
            display_name="gpt-image-1",
            capabilities=["image"],
            cost_per_1k=cost or {},
        ),
        provider=LLMProvider(
            name="openai",
            type="openai",
            base_url=f"{fake.base}/v1",
            api_key_encrypted=encrypt("test-secret"),
        ),
    )


async def test_provider_call_uses_the_db_key_and_returns_png(fake_provider: _FakeProvider) -> None:
    fake_provider.usage = {"input_tokens": 12, "output_tokens": 4160, "total_tokens": 4172}

    result = await gateway_image.generate_images(
        _openai(fake_provider), "커피 삽화", n=1, size="1536x1024"
    )

    [request] = fake_provider.requests
    assert request["path"].endswith("/v1/images/generations")
    assert request["headers"]["authorization"] == "Bearer test-secret"
    body = request["body"]
    assert (body["prompt"], body["n"], body["size"]) == ("커피 삽화", 1, "1536x1024")
    assert [(i.data, i.content_type) for i in result.images] == [(PNG, "image/png")]
    assert result.usage == {"prompt_tokens": 12, "completion_tokens": 4160, "total_tokens": 4172}


async def test_jpeg_is_detected_from_bytes(fake_provider: _FakeProvider) -> None:
    fake_provider.items = [_b64(JPEG), _b64(PNG)]

    result = await gateway_image.generate_images(_openai(fake_provider), "p", n=2)

    assert [i.content_type for i in result.images] == ["image/jpeg", "image/png"]


async def test_fewer_images_than_requested_are_returned_as_received(
    fake_provider: _FakeProvider,
) -> None:
    result = await gateway_image.generate_images(_openai(fake_provider), "p", n=3)

    assert len(result.images) == 1


@pytest.mark.parametrize(
    ("items", "reason"),
    [
        ([_b64(WEBP)], "PNG·JPEG가 아니"),
        ([_b64(b"")], "데이터가 없습니다"),
        ([{"b64_json": "!!!not-base64!!!"}], "해석할 수 없습니다"),
        ([_b64(PNG[:4])], "PNG·JPEG가 아니"),
        # 시그니처는 온전하지만 잘린 바이트(#340 리뷰)
        ([_b64(PNG[:8])], "잘렸습니다"),
        ([_b64(PNG[:-1])], "잘렸습니다"),
        ([_b64(PNG[:8] + PNG[16:])], "잘렸습니다"),  # 첫 청크가 IHDR이 아니다
        ([_b64(b"\xff\xd8\xff")], "잘렸습니다"),
        ([_b64(JPEG[:-2])], "잘렸습니다"),
        ([_b64(b"\xff\xd8\xff\xff\xd9")], "잘렸습니다"),  # 프레임·스캔 없음(#340 재리뷰)
        ([_b64(JPEG[:2] + JPEG[15:])], "잘렸습니다"),  # SOF 없이 SOS
        ([_b64(JPEG[:-4] + b"\xff\xd9")], "잘렸습니다"),  # 스캔 데이터 없음
        ([_b64(bytes.fromhex("ffd8ffc00002ffda000200ffd9"))], "잘렸습니다"),  # 빈 SOF·SOS
        ([_b64(JPEG[:9] + b"\x00\x00" + JPEG[11:])], "잘렸습니다"),  # 가로 0
        ([_b64(JPEG[:19] + b"\x02" + JPEG[20:])], "잘렸습니다"),  # SOS 성분 수와 길이 불일치
        ([_b64(PNG), _b64(PNG)], "요청\\(1장\\)보다 많은"),
        ([], "이미지가 없습니다"),
        ([{"revised_prompt": "x"}], "데이터가 없습니다"),
    ],
)
async def test_bad_provider_payloads_fail_the_call(
    fake_provider: _FakeProvider, items: list[dict[str, str]], reason: str
) -> None:
    fake_provider.items = items

    with pytest.raises(GatewayCallError, match=reason):
        await gateway_image.generate_images(_openai(fake_provider), "p", n=1)


async def test_image_over_the_byte_cap_is_rejected(
    fake_provider: _FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = _png(side=64)
    monkeypatch.setattr(gateway_image, "IMAGE_MAX_BYTES", len(big) - 1)
    fake_provider.items = [_b64(big)]

    with pytest.raises(GatewayCallError, match="상한"):
        await gateway_image.generate_images(_openai(fake_provider), "p")


async def test_provider_failure_is_a_model_call_error(fake_provider: _FakeProvider) -> None:
    fake_provider.status = 400

    with pytest.raises(GatewayCallError, match="이미지 생성 호출 실패"):
        await gateway_image.generate_images(_openai(fake_provider), "p")


# ── D4: URL 응답은 SSRF 관문으로 받는다 ───────────────────────────────────


async def test_url_response_is_fetched_through_the_ssrf_gate(
    fake_provider: _FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_provider.items = [{"url": "https://img.example.com/a.png"}]
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def gate(method: str, url: str, **kw: Any) -> httpx.Response:
        calls.append((method, url, kw))
        return httpx.Response(200, content=JPEG)

    monkeypatch.setattr(gateway_image, "ssrf_safe_request", gate)

    result = await gateway_image.generate_images(_openai(fake_provider), "p")

    [(method, url, kw)] = calls
    assert (method, url) == ("GET", "https://img.example.com/a.png")
    assert kw["allow_private"] is False and kw["max_bytes"] == gateway_image.IMAGE_MAX_BYTES
    assert [i.content_type for i in result.images] == ["image/jpeg"]


async def test_url_to_a_private_address_is_blocked(fake_provider: _FakeProvider) -> None:
    fake_provider.items = [{"url": f"{fake_provider.base}/a.png"}]  # 127.0.0.1

    with pytest.raises(GatewayCallError, match="이미지 URL을 받을 수 없습니다"):
        await gateway_image.generate_images(_openai(fake_provider), "p")


async def test_url_fetch_failure_status_is_an_error(
    fake_provider: _FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_provider.items = [{"url": "https://img.example.com/a.png"}]

    async def gate(method: str, url: str, **kw: Any) -> httpx.Response:
        return httpx.Response(404, content=b"no")

    monkeypatch.setattr(gateway_image, "ssrf_safe_request", gate)

    with pytest.raises(GatewayCallError, match="HTTP 404"):
        await gateway_image.generate_images(_openai(fake_provider), "p")


# ── 노드 ─────────────────────────────────────────────────────────────────


@pytest.fixture
def node_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """노드 환경 — 기본 이미지 모델 조회(DB)와 저장(스토리지)만 대역이다."""
    env: dict[str, Any] = {"model": _mock_resolved(), "resolved_with": [], "objects": []}

    async def resolve(db: Any, model_id: Any, modality: str = "llm") -> ResolvedModel:
        env["resolved_with"].append((model_id, modality))
        return env["model"]

    def put_object(bucket: str, key: str, data: bytes, content_type: str) -> None:
        env["objects"].append((key, data, content_type))

    monkeypatch.setattr(gateway_llm, "resolve_model", resolve)
    monkeypatch.setattr(storage, "ensure_buckets", lambda: None)
    monkeypatch.setattr(storage, "put_object", put_object)
    env["ctx"] = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=None,
        params={"topic": "커피"},  # type: ignore[arg-type]
    )
    return env


async def _run(env: dict[str, Any], node: dict[str, Any], state: dict[str, Any] | None = None):
    full = {"params": env["ctx"].params, **(state or {})}
    return await image_node._exec_image_generate(env["ctx"], node, full)


async def test_node_saves_png_artifacts_and_records_usage_and_cost(
    node_env: dict[str, Any],
) -> None:
    node_env["model"] = _mock_resolved({"per_image": 0.04})
    ctx: RunContext = node_env["ctx"]
    node = {"prompt": "{params.topic} 삽화", "name": "slide-{params.topic}", "n": 2}

    out = await _run(node_env, node)

    names = ["slide-커피-1.png", "slide-커피-2.png"]
    keys = [f"{ctx.run_id}/{n}" for n in names]
    assert out["names"] == names and out["keys"] == keys and out["key"] == keys[0]
    assert out["count"] == 2
    assert [i["content_type"] for i in out["images"]] == ["image/png"] * 2
    assert [(k, ct) for k, _, ct in node_env["objects"]] == [(k, "image/png") for k in keys]
    assert all(_png_size(data) == (8, 8) for _, data, _ in node_env["objects"])
    assert [i["bytes"] for i in out["images"]] == [len(d) for _, d, _ in node_env["objects"]]
    assert ctx.artifact_keys == [
        {"key": k, "name": n, "content_type": "image/png"} for k, n in zip(keys, names, strict=True)
    ]
    assert ctx.usage["images"] == 2
    assert ctx.cost_usd == pytest.approx(0.08) and ctx.cost_total[0] == pytest.approx(0.08)
    assert node_env["resolved_with"] == [(None, "image")]
    assert "data" not in json.dumps(out)  # 바이트는 상태에 들어가지 않는다


async def test_single_image_keeps_the_name_and_swaps_the_extension(
    node_env: dict[str, Any],
) -> None:
    out = await _run(node_env, {"prompt": "p", "name": "cover.JPEG"})
    assert out["names"] == ["cover.png"]

    out = await _run(node_env, {"prompt": "p"})
    assert out["names"] == ["image.png"]


async def test_no_registered_image_model_fails_clearly(
    node_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_models(db: Any, modality: str = "llm", enabled_only: bool = True) -> list:
        assert modality == "image"
        return []

    monkeypatch.setattr(gateway_llm, "resolve_model", _REAL_RESOLVE)
    monkeypatch.setattr(gateway_llm, "list_models", no_models)

    with pytest.raises(GatewayError, match="등록된 image 모델이 없습니다"):
        await _run(node_env, {"prompt": "p"})
    assert node_env["objects"] == []


async def test_a_non_image_model_id_is_rejected(node_env: dict[str, Any]) -> None:
    node_env["model"] = ResolvedModel(
        model=LLMModel(model_name="m", display_name="m", capabilities=["llm"]),
        provider=LLMProvider(name="mock", type="mock"),
    )
    model_id = uuid.uuid4()

    with pytest.raises(GatewayError, match="이미지 생성\\(image\\) 모델이 아닙니다"):
        await _run(node_env, {"prompt": "p", "model_id": str(model_id)})
    assert node_env["resolved_with"] == [(model_id, "image")]
    assert node_env["objects"] == []


@pytest.mark.parametrize(
    ("node", "error", "reason"),
    [
        ({}, AgentRuntimeError, "prompt가 없습니다"),
        ({"prompt": "p", "n": 0}, GatewayError, "1~4 정수"),
        ({"prompt": "p", "n": 5}, GatewayError, "1~4 정수"),
        ({"prompt": "p", "n": True}, AgentRuntimeError, "정수여야"),
        ({"prompt": "p", "n": "2"}, AgentRuntimeError, "정수여야"),
        ({"prompt": "p", "size": "2048x2048"}, GatewayError, "크기는"),
        ({"prompt": "p", "aspect_ratio": "21:9"}, AgentRuntimeError, "aspect_ratio는"),
        ({"prompt": "p", "size": "1024x1024", "aspect_ratio": "1:1"}, AgentRuntimeError, "함께"),
        ({"prompt": "x" * 4001}, GatewayError, "상한"),
        ({"prompt": "{params.missing}"}, GatewayError, "비어 있습니다"),
        ({"prompt": "p", "model_id": "nope"}, AgentRuntimeError, "model_id가 올바르지"),
        ({"prompt": "p", "name": "../x"}, AgentRuntimeError, "name이 올바르지"),
        ({"prompt": "p", "name": "a/b"}, AgentRuntimeError, "name이 올바르지"),
        ({"prompt": "p", "name": "a\\b"}, AgentRuntimeError, "name이 올바르지"),
        ({"prompt": "p", "name": ".hidden"}, AgentRuntimeError, "name이 올바르지"),
        ({"prompt": "p", "name": ".png"}, AgentRuntimeError, "name이 올바르지"),
        ({"prompt": "p", "name": "a\nb"}, AgentRuntimeError, "name이 올바르지"),
        ({"prompt": "p", "name": "x" * 201}, AgentRuntimeError, "name이 올바르지"),
        ({"prompt": "p", "name": "{params.missing}"}, AgentRuntimeError, "name이 올바르지"),
    ],
)
async def test_bad_definitions_fail_before_any_model_call(
    node_env: dict[str, Any], node: dict[str, Any], error: type[Exception], reason: str
) -> None:
    with pytest.raises(error, match=reason):
        await _run(node_env, node)
    assert node_env["resolved_with"] == []
    assert node_env["objects"] == []


# ── D1: 비신뢰 블록 거부 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("prompt", "state"),
    [
        ("{search.context}", {"search": {"context": wrap_untrusted("소스 본문", "source")}}),
        ("{gen.text|untrusted} 그림", {"gen": {"text": "평범한 글"}}),
        # 태그를 품은 앞 노드 출력은 통째로 감싸진다 — 그대로 그려지지 않게 거부한다
        ("{gen.text}", {"gen": {"text": '<untrusted_data kind="source">x</untrusted_data>'}}),
        ('<untrusted_data kind="source">{gen.text}</untrusted_data>', {"gen": {"text": "a"}}),
    ],
)
async def test_untrusted_blocks_are_rejected_before_the_provider(
    node_env: dict[str, Any], prompt: str, state: dict[str, Any]
) -> None:
    with pytest.raises(AgentRuntimeError, match="비신뢰 데이터"):
        await _run(node_env, {"prompt": prompt}, state)
    assert node_env["resolved_with"] == [] and node_env["objects"] == []


async def test_instruction_roots_and_plain_llm_output_reach_the_provider(
    fake_provider: _FakeProvider, node_env: dict[str, Any]
) -> None:
    node_env["model"] = _openai(fake_provider)
    node_env["ctx"].params["slide"] = {"heading": "전파 <untrusted_data> 경로"}
    state = {"gen": {"text": "따뜻한​ 커피잔"}}

    out = await _run(
        node_env,
        {"prompt": "{params.slide.heading}: {gen.text}", "aspect_ratio": "16:9"},
        state,
    )

    [request] = fake_provider.requests
    sent = request["body"]["prompt"]
    assert "따뜻한 커피잔" in sent and "​" not in sent  # 보이지 않는 글자는 지운다
    assert "<untrusted_data>" not in sent and "전파" in sent  # 지시 루트 값은 이스케이프만
    assert request["body"]["size"] == "1536x1024"
    assert out["names"] == ["image.png"]


async def test_cost_falls_back_to_token_usage_then_zero(
    fake_provider: _FakeProvider, node_env: dict[str, Any]
) -> None:
    fake_provider.usage = {"input_tokens": 1000, "output_tokens": 2000, "total_tokens": 3000}
    node_env["model"] = _openai(fake_provider, {"input": 0.005, "output": 0.04})

    await _run(node_env, {"prompt": "p"})

    ctx: RunContext = node_env["ctx"]
    assert ctx.cost_usd == pytest.approx(0.005 + 0.08)
    assert ctx.usage["total_tokens"] == 3000 and ctx.node_tokens["prompt_tokens"] == 1000

    fake_provider.usage = None
    node_env["model"] = _openai(fake_provider, {"input": 0.005})
    before = ctx.cost_usd
    await _run(node_env, {"prompt": "p"})
    assert ctx.cost_usd == pytest.approx(before)
    assert ctx.usage["images"] == 2


# ── 그래프 등록·실행 ──────────────────────────────────────────────────────


async def test_graph_runs_the_node_and_logs_its_cost(
    node_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    node_env["model"] = _mock_resolved({"per_image": 0.04})
    finished: list[dict[str, Any]] = []

    async def no_start(*_a: Any, **_k: Any) -> None:
        return None

    async def finish(log_id: Any, **kw: Any) -> bool:
        finished.append(kw)
        return True

    monkeypatch.setattr(graph, "_log_node_start", no_start)
    monkeypatch.setattr(graph, "_log_node_finish", finish)
    definition = {
        "nodes": [{"id": "img", "type": "image_generate", "prompt": "{params.topic}", "n": 3}],
        "edges": [],
    }

    assert graph.NODE_EXECUTORS["image_generate"] is image_node._exec_image_generate
    await graph.execute_agent(node_env["ctx"], "workflow", definition)

    [log] = finished
    assert log["status"] == "succeeded" and log["cost_usd"] == pytest.approx(0.12)
    assert len(node_env["ctx"].artifact_keys) == 3


# ── #340 리뷰: 저장이 실패해도 생성 비용은 남는다 ─────────────────────────


async def test_cost_is_recorded_even_if_storage_fails(
    node_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    node_env["model"] = _mock_resolved({"per_image": 0.04})

    def broken(*_a: Any, **_k: Any) -> None:
        raise OSError("storage down")

    monkeypatch.setattr(storage, "put_object", broken)
    ctx: RunContext = node_env["ctx"]

    with pytest.raises(OSError, match="storage down"):
        await _run(node_env, {"prompt": "p", "n": 2})

    assert ctx.cost_usd == pytest.approx(0.08) and ctx.usage["images"] == 2
    assert ctx.artifact_keys == []


# ── #340 리뷰(D1): 태그 없는 비신뢰 원문을 내는 노드는 프롬프트에 못 쓴다 ──


def _definition(upstream: dict[str, Any], prompt: str) -> dict[str, Any]:
    image = {"id": "img", "type": "image_generate", "prompt": prompt}
    return {"nodes": [upstream, image], "edges": [[upstream["id"], "img"]]}


@pytest.fixture
def no_node_runs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """어떤 노드도 실행되지 않았는지 — 실행기 호출을 기록한다."""
    ran: list[str] = []

    def spy(name: str) -> Any:
        async def executor(ctx: Any, node: dict, state: dict) -> dict:
            ran.append(name)
            return {}

        return executor

    for name in list(graph.NODE_EXECUTORS):
        monkeypatch.setitem(graph.NODE_EXECUTORS, name, spy(name))
    return ran


@pytest.mark.parametrize(
    ("upstream", "prompt"),
    [
        ({"id": "search", "type": "rag_search", "query": "q"}, "{search.citations}"),
        ({"id": "search", "type": "rag_search", "query": "q"}, "그림: {search.context}"),
        ({"id": "load", "type": "source_load"}, "{load.sources}"),
        ({"id": "h", "type": "http", "url": "https://x.test"}, "{h.text}"),
        ({"id": "m", "type": "mcp_tool", "server_id": "s", "tool_name": "t"}, "{m.text}"),
        ({"id": "sub", "type": "agent_call", "agent_id": "a"}, "{sub.text}"),
        ({"id": "gen", "type": "llm", "prompt": "p"}, "{gen.text|untrusted}"),
        ({"id": "gen", "type": "llm", "prompt": "p"}, "{ghost.text}"),  # 없는 노드
    ],
)
async def test_raw_untrusted_producers_are_rejected_before_any_node_runs(
    node_env: dict[str, Any], no_node_runs: list[str], upstream: dict[str, Any], prompt: str
) -> None:
    with pytest.raises(AgentRuntimeError, match="image_generate img"):
        await graph.execute_agent(node_env["ctx"], "workflow", _definition(upstream, prompt))

    assert no_node_runs == []


@pytest.mark.parametrize(
    ("upstream", "prompt"),
    [
        ({"id": "gen", "type": "llm", "prompt": "p"}, "{gen.text}, {params.topic}"),
        ({"id": "c", "type": "constant", "value": "x"}, "{c.text} {user.name}"),
        ({"id": "r", "type": "react_agent", "goal": "g"}, "{r.text}"),
    ],
)
async def test_derived_text_and_instruction_roots_are_allowed(
    node_env: dict[str, Any],
    no_node_runs: list[str],
    monkeypatch: pytest.MonkeyPatch,
    upstream: dict[str, Any],
    prompt: str,
) -> None:
    async def no_log(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(graph, "_log_node_start", no_log)
    monkeypatch.setattr(graph, "_log_node_finish", no_log)

    await graph.execute_agent(node_env["ctx"], "workflow", _definition(upstream, prompt))

    assert no_node_runs == [upstream["type"], "image_generate"]


# ── #340 재리뷰(D1): 출처 추적 — json_validate·하위 실행 params ─────────────


LLM = {"id": "gen", "type": "llm", "prompt": "p"}
HTTP = {"id": "h", "type": "http", "url": "https://x.test"}
SEARCH = {"id": "search", "type": "rag_search", "query": "q"}


def _validate(source: str) -> dict[str, Any]:
    return {"id": "v", "type": "json_validate", "source": source, "schema": "slide_deck"}


def _chain(*upstream: dict[str, Any], prompt: str) -> dict[str, Any]:
    nodes = [*upstream, {"id": "img", "type": "image_generate", "prompt": prompt}]
    ids = [n["id"] for n in nodes]
    return {"nodes": nodes, "edges": [list(e) for e in zip(ids, ids[1:], strict=False)]}


def _roots(*nodes: dict[str, Any], params_safe: bool = True) -> frozenset[str]:
    return prompt_provenance.image_safe_roots({n["id"]: n for n in nodes}, params_safe=params_safe)


def test_json_validate_is_safe_only_when_its_source_is() -> None:
    assert "v" in _roots(LLM, _validate("{gen.text}"))
    assert "v" in _roots(_validate("{params.deck}"))
    assert "v" not in _roots(HTTP, _validate("{h.text}"))
    assert "v" not in _roots(LLM, _validate("{gen.text|untrusted}"))
    assert "v" not in _roots(_validate("{params.deck}"), params_safe=False)
    assert "v" not in _roots(_validate("[]"))  # 참조 없는 source는 LLM 출력이 아니다
    cyc = {"id": "a", "type": "json_validate", "source": "{b.text}", "schema": "quiz"}
    cyc2 = {"id": "b", "type": "json_validate", "source": "{a.text}", "schema": "quiz"}
    assert not {"a", "b"} & _roots(cyc, cyc2)


@pytest.mark.parametrize(
    ("upstream", "prompt", "ok"),
    [
        ((HTTP, _validate("{h.text}")), "{v.json.root.label}", False),
        ((SEARCH, _validate("{search.citations}")), "{v.text}", False),
        ((LLM, _validate("{gen.text}")), "{v.json.slides}", True),
    ],
)
async def test_graph_follows_json_validate_provenance(
    node_env: dict[str, Any],
    no_node_runs: list[str],
    monkeypatch: pytest.MonkeyPatch,
    upstream: tuple[dict[str, Any], ...],
    prompt: str,
    ok: bool,
) -> None:
    async def no_log(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(graph, "_log_node_start", no_log)
    monkeypatch.setattr(graph, "_log_node_finish", no_log)
    definition = _chain(*upstream, prompt=prompt)

    if ok:
        await graph.execute_agent(node_env["ctx"], "workflow", definition)
        assert no_node_runs[-1] == "image_generate"
    else:
        with pytest.raises(AgentRuntimeError, match="image_generate img"):
            await graph.execute_agent(node_env["ctx"], "workflow", definition)
        assert no_node_runs == []


@pytest.mark.parametrize("params_safe", [True, False])
async def test_child_run_params_follow_the_parent_judgement(
    node_env: dict[str, Any],
    no_node_runs: list[str],
    monkeypatch: pytest.MonkeyPatch,
    params_safe: bool,
) -> None:
    async def no_log(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(graph, "_log_node_start", no_log)
    monkeypatch.setattr(graph, "_log_node_finish", no_log)
    child = node_env["ctx"].child(
        uuid.uuid4(), {"slide": {"heading": "h"}}, params_image_safe=params_safe
    )
    definition = _chain(prompt="{params.slide.heading} 삽화")

    if params_safe:
        await graph.execute_agent(child, "workflow", definition)
        assert no_node_runs == ["image_generate"]
    else:
        with pytest.raises(AgentRuntimeError, match="params의 값을 직접 쓸 수 없습니다"):
            await graph.execute_agent(child, "workflow", definition)


def test_child_params_are_unsafe_unless_the_parent_says_so() -> None:
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={})  # type: ignore[arg-type]
    assert ctx.params_image_safe is True  # 최상위 run
    assert ctx.child(uuid.uuid4(), {}).params_image_safe is False
    assert ctx.child(uuid.uuid4(), {}, params_image_safe=True).params_image_safe is True


@pytest.fixture
def subagent_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """자식 컨텍스트가 받은 params_image_safe를 기록한다 — 대상 조회(DB)·디스패치만 대역이다."""
    from biznotes_core.models import Agent

    env: dict[str, Any] = {"seen": []}

    class _Db:
        async def get(self, model: Any, ident: Any) -> Any:
            return Agent(id=ident, kind="workflow", name="body", definition={})

    async def visible(*_a: Any, **_k: Any) -> bool:
        return True

    async def dispatch(sub_ctx: RunContext, target: Any, **_k: Any) -> str:
        env["seen"].append(sub_ctx.params_image_safe)
        return "ok"

    for module in (composite, retrieval):
        monkeypatch.setattr(module, "skill_agent_visible_to", visible)
        monkeypatch.setattr(module, "dispatch_agent", dispatch)
        monkeypatch.setattr(module, "_reject_subagent_approval", lambda *a: None)
    monkeypatch.setattr(composite, "_visible_by_wrapper", lambda *a: True)
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=_Db(), params={})  # type: ignore[arg-type]
    ctx.image_safe_roots = _roots(SEARCH, LLM, _validate("{gen.text}"))
    env["ctx"] = ctx
    env["state"] = {
        "params": {},
        "search": {"citations": [{"quote": "원문"}]},
        "h": {"text": "HTTP 원문"},
        "v": {"json": {"slides": [{"heading": "a"}, {"heading": "b"}]}},
    }
    return env


@pytest.mark.parametrize(
    ("items", "safe"),
    [
        ("{v.json.slides}", True),
        ("{ v.json.slides }", True),
        ("{search.citations}", False),
        ("{ search.citations }", False),  # 루프는 공백을 떼고 경로로 읽는다(#340 재리뷰 2)
        ('[{"heading": "x"}]', True),
        # 리터럴 목록은 `str()`이 JSON일 때만 항목이 된다(따옴표 든 키). 키의 템플릿도 렌더된다
        ([{"heading'": "x'"}], True),
        ([{"{h.text}'": "safe'"}], False),
        ('[{"q": {search.citations}}]', False),
        ('{"heading": "x"}', True),  # 경로로 못 읽은 JSON 객체 — 작성자 상수(#340 재리뷰 3)
        ('{"q": "{h.text}"}', False),
    ],
)
async def test_loop_passes_item_provenance_to_children(
    subagent_env: dict[str, Any], items: str, safe: bool
) -> None:
    node = {"id": "each", "type": "loop", "items": items, "loop_body_agent_id": str(uuid.uuid4())}

    await retrieval._exec_loop(subagent_env["ctx"], node, subagent_env["state"])

    assert subagent_env["seen"] and set(subagent_env["seen"]) == {safe}


@pytest.mark.parametrize(
    ("items", "safe"), [("{v.json.slides}", True), ("{search.citations}", False)]
)
async def test_loop_max_items_does_not_change_item_provenance(
    subagent_env: dict[str, Any], items: str, safe: bool
) -> None:
    """`max_items`(#342)는 앞쪽 항목만 남길 뿐이라 출처 판정은 상한과 무관하다."""
    node = {
        "id": "each",
        "type": "loop",
        "items": items,
        "max_items": 1,
        "loop_body_agent_id": str(uuid.uuid4()),
    }

    await retrieval._exec_loop(subagent_env["ctx"], node, subagent_env["state"])

    assert subagent_env["seen"] == [safe]


@pytest.mark.parametrize(
    ("params", "safe"),
    [
        ({"slide": "{v.json.slides}"}, True),
        ({"slide": "{v.json.slides}", "quote": "{search.citations}"}, False),
        ({"topic": "고정 값"}, True),
        ({"slide": "{gen.text|untrusted}"}, False),
    ],
)
@pytest.mark.parametrize("node_type", ["agent_call", "sub_workflow"])
async def test_agent_call_and_sub_workflow_pass_param_provenance(
    subagent_env: dict[str, Any], node_type: str, params: dict[str, Any], safe: bool
) -> None:
    key = "params" if node_type == "agent_call" else "input_map"
    node = {"id": "call", "type": node_type, "agent_id": str(uuid.uuid4()), key: params}
    executor = graph.NODE_EXECUTORS[node_type]

    await executor(subagent_env["ctx"], node, subagent_env["state"])

    assert subagent_env["seen"] == [safe]
