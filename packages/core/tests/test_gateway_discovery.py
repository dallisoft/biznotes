"""프로바이더 모델 디스커버리 — OpenRouter 능력 분류(#74)와 게이트웨이 `type` 분류(#89).

OpenRouter /models 응답(`tests/fixtures/openrouter_models.json`, 실제 응답에서 발췌)의
`architecture` 메타데이터와, 게이트웨이가 항목마다 붙이는 `type`으로 능력을 판별하는지
검증한다. 네트워크 경계(`_get_json`)만 가짜로 바꾼다.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from biznotes_core.models import LLMProvider
from biznotes_core.models.llm import MODALITIES
from biznotes_core.services.gateway import discovery

FIXTURE = Path(__file__).parent / "fixtures" / "openrouter_models.json"


@pytest.fixture
def fake_get_json(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """요청 URL을 기록하고 base URL별로 준비한 응답을 돌려준다."""
    requested: list[str] = []
    openrouter = json.loads(FIXTURE.read_text())
    plain = {
        "data": [
            {"id": "gpt-4o"},
            {"id": "text-embedding-3-small"},
            {"id": "whisper-1"},
            {"id": "llava-v1.6"},
        ]
    }

    async def _fake(url: str, headers: dict[str, str] | None = None) -> Any:
        requested.append(url)
        return openrouter if url.startswith("https://openrouter.ai/") else plain

    monkeypatch.setattr(discovery, "_get_json", _fake)
    return requested


def _provider(type_: str, base_url: str | None) -> LLMProvider:
    return LLMProvider(name="p", type=type_, base_url=base_url, api_key_encrypted=None)


def _caps(found: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {m["raw_id"]: m["capabilities"] for m in found}


@pytest.mark.parametrize("type_", ["custom", "openai"])
async def test_openrouter_capabilities_from_architecture(
    fake_get_json: list[str], type_: str
) -> None:
    found = await discovery.discover_provider_models(
        _provider(type_, "https://openrouter.ai/api/v1/")
    )

    assert fake_get_json == ["https://openrouter.ai/api/v1/models?output_modalities=all"]
    assert _caps(found) == {
        "z-ai/glm-5.3-prime": ["llm"],
        "anthropic/claude-sonnet-5.5": ["llm", "vlm"],
        # 오디오 입출력 채팅 모델은 채팅 경로로 라우팅된다 — stt·tts가 아니다.
        "openai/gpt-audio": ["llm"],
        "mistralai/voxtral-small-24b-2507": ["llm"],
        # 이미지를 출력하면 텍스트를 함께 내도 이미지 생성 모델이다(#347, image 단독 능력).
        "google/gemini-3.1-flash-lite-image": ["image"],
        "recraft/recraft-v4.1-flash": ["image"],
        "liquid/lfm-2.5-embedding-350m:free": ["embedding"],
        "voyageai/voyage-multimodal-3.5": ["embedding"],
        # 음성 출력(transcription·speech) 모델은 게이트웨이가 라우팅하지 않아 목록에서 빠진다
        # rerank 출력은 리랭커 능력이다(#209). 목록에 보이는 것까지만 주장한다 — OpenRouter의
        # `/rerank` 지원 여부는 확인하지 않았다
        "qwen/qwen3-reranker-8b": ["reranker"],
    }


async def test_openrouter_skips_models_without_gateway_capability(
    fake_get_json: list[str],
) -> None:
    """비디오 생성 모델은 게이트웨이가 라우팅할 수 없어 목록에서 뺀다. 이미지 생성 모델은
    v0.23.0(#338)부터 라우팅하므로 남는다(#347)."""
    found = await discovery.discover_provider_models(
        _provider("custom", "https://openrouter.ai/api/v1")
    )

    ids = {m["raw_id"] for m in found}
    assert "minimax/hailuo-3-max" not in ids
    assert "recraft/recraft-v4.1-flash" in ids


async def test_openrouter_model_name_uses_provider_prefix(fake_get_json: list[str]) -> None:
    found = await discovery.discover_provider_models(
        _provider("custom", "https://openrouter.ai/api/v1")
    )

    entry = next(m for m in found if m["raw_id"] == "qwen/qwen3-reranker-8b")
    assert entry["model_name"] == "openai/qwen/qwen3-reranker-8b"
    assert entry["display_name"] == "qwen/qwen3-reranker-8b"


@pytest.mark.parametrize(
    "base_url",
    [None, "https://api.example.com/v1", "https://openrouter.ai.evil.example/v1"],
)
async def test_non_openrouter_keeps_name_inference(
    fake_get_json: list[str], base_url: str | None
) -> None:
    """architecture가 없는 OpenAI 호환 응답은 기존처럼 모델명으로 추론한다."""
    found = await discovery.discover_provider_models(_provider("openai", base_url))

    assert all("output_modalities" not in url for url in fake_get_json)
    assert _caps(found) == {
        "gpt-4o": ["llm"],
        "text-embedding-3-small": ["embedding"],
        "llava-v1.6": ["llm", "vlm"],
    }


@pytest.mark.parametrize(
    "arch",
    [
        {"output_modalities": "text"},  # 문자열 — 글자 단위로 돌면 조용히 빠진다
        {"output_modalities": 5},
        {"output_modalities": [{}]},
        {"output_modalities": []},
        {"input_modalities": ["text"]},
        "text->text",
    ],
    ids=["str", "int", "unhashable", "empty", "no-outputs", "not-a-dict"],
)
async def test_malformed_architecture_falls_back_to_name_inference(
    monkeypatch: pytest.MonkeyPatch, arch: Any
) -> None:
    """외부 응답의 메타데이터 형태가 예상과 다르면 예외 없이 모델명 추론으로 되돌린다."""

    async def fake(url: str, headers: dict[str, str] | None = None) -> Any:
        return {"data": [{"id": "vendor/some-embedding-model", "architecture": arch}]}

    monkeypatch.setattr(discovery, "_get_json", fake)

    found = await discovery.discover_provider_models(
        _provider("custom", "https://openrouter.ai/api/v1")
    )

    assert _caps(found) == {"vendor/some-embedding-model": ["embedding"]}


async def test_non_openrouter_host_ignores_architecture(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenRouter식 메타데이터를 돌려주는 다른 호환 서버는 지금처럼 모델명으로만 추론하고,
    어떤 모델도 빼지 않는다."""

    async def fake(url: str, headers: dict[str, str] | None = None) -> Any:
        return {
            "data": [
                {"id": "vendor-gen", "architecture": {"output_modalities": ["image"]}},
                {"id": "llava-v1.6", "architecture": {"output_modalities": ["Text"]}},
            ]
        }

    monkeypatch.setattr(discovery, "_get_json", fake)

    found = await discovery.discover_provider_models(
        _provider("custom", "https://proxy.example.com/v1")
    )

    assert _caps(found) == {"vendor-gen": ["llm"], "llava-v1.6": ["llm", "vlm"]}


def _serve(monkeypatch: pytest.MonkeyPatch, models: list[dict[str, Any]]) -> None:
    async def fake(url: str, headers: dict[str, str] | None = None) -> Any:
        return {"data": models}

    monkeypatch.setattr(discovery, "_get_json", fake)


@pytest.mark.parametrize("type_", ["custom", "openai", "vllm", "lmstudio"])
async def test_gateway_type_classifies_models(
    monkeypatch: pytest.MonkeyPatch, type_: str
) -> None:
    """게이트웨이 `/v1/models` 응답(#89): 이름만으로는 알 수 없는 임베딩·TTS를 `type`으로
    분류한다. reranker도 리랭커 능력으로 보인다(#209)."""
    _serve(
        monkeypatch,
        [
            {"id": "bge-m3", "type": "embedding"},
            {"id": "bge-reranker-v2-m3", "type": "reranker"},
            {"id": "ko_KO-kss_low", "type": "tts"},
            {"id": "whisper-large-v3-turbo", "type": "stt"},
        ],
    )

    found = await discovery.discover_provider_models(
        _provider(type_, "https://gateway.example.com/v1")
    )

    # 음성 타입(tts·stt)은 이름 추론으로 되돌리지 않고 뺀다 — `ko_KO-kss_low`가 llm이 되지 않는다
    assert _caps(found) == {
        "bge-m3": ["embedding"],
        "bge-reranker-v2-m3": ["reranker"],
    }


async def test_reranker_is_recognised_by_name_without_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`type`을 주지 않는 서버의 리랭커도 이름으로 리랭커가 된다 — `llm`로 오분류하지 않는다."""
    _serve(
        monkeypatch,
        [
            {"id": "bge-reranker-v2-m3"},
            {"id": "jina-reranker-v2-base-multilingual"},
            {"id": "bge-m3-embed"},
        ],
    )

    found = await discovery.discover_provider_models(
        _provider("custom", "https://gateway.example.com/v1")
    )

    assert _caps(found) == {
        "bge-reranker-v2-m3": ["reranker"],
        "jina-reranker-v2-base-multilingual": ["reranker"],
        "bge-m3-embed": ["embedding"],
    }


async def test_gateway_type_wins_over_model_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """`type`은 모델명 키워드보다 우선한다. 비전은 `vlm` 타입이나 비전 모델명에서 온다."""
    _serve(
        monkeypatch,
        [
            {"id": "embed-chat", "type": "llm"},  # 이름은 embedding, 타입은 llm
            {"id": "plain-model", "type": "embedding"},  # 이름은 llm, 타입은 embedding
            {"id": "llava-v1.6", "type": "llm"},  # 비전 모델명 힌트는 유지된다
            {"id": "plain-vision", "type": "vlm"},
            {"id": "whisper-x", "type": "  EMBEDDING "},  # 공백·대소문자는 무시한다
        ],
    )

    found = await discovery.discover_provider_models(
        _provider("custom", "https://gateway.example.com/v1")
    )

    assert _caps(found) == {
        "embed-chat": ["llm"],
        "plain-model": ["embedding"],
        "llava-v1.6": ["llm", "vlm"],
        "plain-vision": ["llm", "vlm"],
        "whisper-x": ["embedding"],
    }


@pytest.mark.parametrize(
    "raw_type",
    ["unknown-kind", "", "   ", 5, True, None, ["embedding"], {"kind": "embedding"}],
    ids=["unknown", "empty", "blank", "int", "bool", "null", "list", "dict"],
)
async def test_unknown_or_malformed_type_falls_back_to_name_inference(
    monkeypatch: pytest.MonkeyPatch, raw_type: Any
) -> None:
    """모르는 값이거나 문자열이 아닌 `type`은 예외 없이 모델명 추론으로 되돌린다."""
    _serve(
        monkeypatch,
        [
            {"id": "vendor/some-embedding-model", "type": raw_type},
            {"id": "gpt-4o", "type": raw_type},
        ],
    )

    found = await discovery.discover_provider_models(
        _provider("custom", "https://gateway.example.com/v1")
    )

    assert _caps(found) == {"vendor/some-embedding-model": ["embedding"], "gpt-4o": ["llm"]}


@pytest.mark.parametrize(
    ("architecture", "expected"),
    [
        ({"output_modalities": ["text"]}, ["llm"]),  # 판별된 architecture가 type보다 우선
        ({"output_modalities": "text"}, ["embedding"]),  # 비정상 architecture는 type으로 되돌린다
    ],
    ids=["architecture-wins", "malformed-architecture-uses-type"],
)
async def test_openrouter_architecture_takes_precedence_over_type(
    monkeypatch: pytest.MonkeyPatch, architecture: Any, expected: list[str]
) -> None:
    _serve(monkeypatch, [{"id": "vendor/m", "type": "embedding", "architecture": architecture}])

    found = await discovery.discover_provider_models(
        _provider("custom", "https://openrouter.ai/api/v1")
    )

    assert _caps(found) == {"vendor/m": expected}


# --- 디스커버리 출력은 Modality 안에만 있다 (#218) ---
# DiscoveredModel.capabilities가 list[Modality]라, 매핑 표에 오탈자가 하나만 있어도
# available-models 응답 전체가 500이 된다. 손으로 쓴 매핑을 정본에 묶는다.


def test_output_modality_map_stays_within_modalities() -> None:
    assert set(discovery._OUTPUT_MODALITY_CAPS.values()) <= set(MODALITIES)


@pytest.mark.parametrize(
    "name",
    [
        "bge-reranker-v2",
        "text-embedding-3-small",
        "llava-1.6",
        "qwen2.5-vl-7b",
        "gpt-4o",
        "gpt-image-1",
        "gemini-2.5-flash-image",
    ],
)
def test_name_based_capabilities_stay_within_modalities(name: str) -> None:
    assert discovery._primary_modality(name) in MODALITIES
    assert set(discovery._capabilities(name)) <= set(MODALITIES)


@pytest.mark.parametrize("name", ["whisper-1", "gpt-4o-transcribe", "tts-1", "speech-02"])
def test_audio_models_have_no_capability(name: str) -> None:
    """음성 모델(TTS·STT)은 게이트웨이가 라우팅하지 않는다 — 능력이 없어 목록에서 빠진다."""
    assert discovery._primary_modality(name) is None
    assert discovery._capabilities(name) == []


@pytest.mark.parametrize("raw_type", [*MODALITIES, "vlm", " LLM ", "image", None])
def test_type_based_capabilities_stay_within_modalities(raw_type: Any) -> None:
    caps = discovery._type_capabilities(raw_type, "vendor/m")

    assert caps is None or set(caps) <= set(MODALITIES)


def test_architecture_capabilities_stay_within_modalities() -> None:
    outputs = [*discovery._OUTPUT_MODALITY_CAPS, "image", "video"]
    caps = discovery._architecture_capabilities(
        {"output_modalities": outputs, "input_modalities": ["text", "image"]}
    )

    assert caps is not None and set(caps) <= set(MODALITIES)


# --- 이미지 생성 모델 분류 (#347) ---


@pytest.mark.parametrize(
    "name",
    [
        # 이슈 재현 4종
        "gpt-image-1",
        "dall-e-3",
        "imagen-3.0-generate-002",
        "gemini-2.5-flash-image",
        # 이름 조각·계열 힌트
        "gemini-2.5-flash-image-preview",
        "gemini-2.0-flash-preview-image-generation",
        "gen4_image_turbo",
        "Qwen-Image-Edit",
        "black-forest-labs/flux.2-pro",
        "stable-diffusion-xl-base-1.0",
        "sd3.5-large",
        "amazon.titan-image-generator-v2:0",
        "amazon.nova-canvas-v1:0",
        "nano-banana-pro",
    ],
)
def test_image_generation_model_names_are_image_only(name: str) -> None:
    assert discovery._capabilities(name) == ["image"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("amazon.titan-embed-image-v1", ["embedding"]),  # 임베딩 힌트가 먼저다
        ("llama-3.2-11b-vision-instruct", ["llm", "vlm"]),  # 비전 입력은 채팅 모델이다
        ("imagenet-classifier", ["llm"]),  # imagen 조각만으로는 이미지 모델이 아니다
        ("images-api", ["llm"]),
        ("gpt-4o-mini-tts", []),  # 음성 모델은 능력이 없다(목록에서 빠진다)
        ("gemini-2.5-flash", ["llm"]),
    ],
)
def test_image_hint_does_not_capture_other_models(name: str, expected: list[str]) -> None:
    assert discovery._capabilities(name) == expected


@pytest.mark.parametrize(
    ("arch", "expected"),
    [
        ({"input_modalities": ["text"], "output_modalities": ["image"]}, ["image"]),
        (
            {"input_modalities": ["text", "image"], "output_modalities": ["text", "image"]},
            ["image"],
        ),
        ({"input_modalities": ["text", "image"], "output_modalities": ["text"]}, ["llm", "vlm"]),
        ({"input_modalities": ["text"], "output_modalities": ["video"]}, []),
    ],
)
def test_openrouter_image_output_is_image_only(arch: dict[str, Any], expected: list[str]) -> None:
    assert discovery._architecture_capabilities(arch) == expected


async def test_google_imagen_predict_and_image_output_gemini_are_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Imagen은 `predict` 메서드로, 이미지 출력 Gemini는 이름으로 이미지 모델이 된다."""

    async def fake(url: str, headers: dict[str, str] | None = None) -> Any:
        return {
            "models": [
                {
                    "name": "models/imagen-4.0-generate-001",
                    "supportedGenerationMethods": ["predict"],
                },
                {"name": "models/vendor-predict-only", "supportedGenerationMethods": ["predict"]},
                {
                    "name": "models/gemini-2.5-flash-image",
                    "supportedGenerationMethods": ["generateContent", "countTokens"],
                },
                {
                    "name": "models/gemini-2.5-flash",
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/gemini-embedding-001",
                    "supportedGenerationMethods": ["embedContent"],
                },
                {
                    "name": "models/veo-3.0-generate-001",
                    "supportedGenerationMethods": ["predictLongRunning"],
                },
            ]
        }

    monkeypatch.setattr(discovery, "_get_json", fake)
    monkeypatch.setattr(discovery, "_api_key", lambda provider: "test-secret")

    found = await discovery.discover_provider_models(_provider("google", None))

    assert _caps(found) == {
        "imagen-4.0-generate-001": ["image"],
        "vendor-predict-only": ["image"],
        "gemini-2.5-flash-image": ["image"],
        "gemini-2.5-flash": ["llm"],
        "gemini-embedding-001": ["embedding"],
        "veo-3.0-generate-001": ["llm"],
    }
    assert {m["model_name"] for m in found} >= {"gemini/imagen-4.0-generate-001"}


async def test_openai_listing_classifies_image_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAI `/models`는 `type`·`architecture`가 없다 — 이름으로 이미지 모델을 가른다."""
    _serve(monkeypatch, [{"id": "gpt-image-1"}, {"id": "dall-e-3"}, {"id": "gpt-4o"}])

    found = await discovery.discover_provider_models(_provider("openai", None))

    assert _caps(found) == {"gpt-image-1": ["image"], "dall-e-3": ["image"], "gpt-4o": ["llm"]}
