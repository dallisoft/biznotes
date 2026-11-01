"""프로바이더 모델 디스커버리 — 등록된 프로바이더의 API에서 사용 가능한 모델 목록을 가져온다.

어드민이 프로바이더만 등록한 뒤, 모델명을 직접 외워 입력하지 않고
프로바이더가 제공하는 모델 중에서 골라 등록할 수 있게 한다.

반환하는 model_name은 곧바로 LLMModel.model_name(litellm 식별자)으로 저장 가능한 형태다.
"""

import re
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from biznotes_core.config import get_settings
from biznotes_core.core.crypto import decrypt
from biznotes_core.models import LLMProvider
from biznotes_core.models.llm import MODALITIES
from biznotes_core.schemas.enums import Modality

_TIMEOUT = 15.0


def _default_base(provider_type: str) -> str:
    """프로바이더 타입별 기본 base URL.

    ollama/lmstudio는 호스트에서 도는 로컬 서버라, 도커 컨테이너에서 닿으려면
    settings.llm_local_host(기본 host.docker.internal)를 사용한다.
    """
    host = get_settings().llm_local_host
    bases = {
        "openai": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com/v1",
        "google": "https://generativelanguage.googleapis.com/v1beta",
        "ollama": f"http://{host}:11434",
        "lmstudio": f"http://{host}:1234/v1",
    }
    return bases.get(provider_type, bases["openai"])


class DiscoveryError(Exception):
    """디스커버리 실패 — 사용자에게 그대로 노출 가능한 메시지."""


# 비전(멀티모달) 모델은 텍스트(llm)와 이미지(vlm)를 모두 처리한다 — 모델명에서 추론.
_VISION_HINT = re.compile(
    r"vlm|vision|multimodal|llava|internvl|qwen[\d.]*-?vl|pixtral|fuyu|deplot"
    r"|paligemma|molmo|florence|cogvlm|minicpm-?v|kosmos|nvlm|[-/]vl\b",
    re.IGNORECASE,
)


# 이미지 생성 모델(#347) — 이미지 출력 Gemini(`gemini-*-flash-image`)·Bedrock
# `titan-image-generator`처럼 `-image`·`_image`로 끝나는 이름 조각과 알려진 계열 이름.
# 목록은 litellm 모델 맵의 `image_generation` 모델 이름으로 점검했다.
_IMAGE_HINT = re.compile(
    r"gpt-image|dall-e|\bimagen\b|stable-diffusion|sdxl|\bsd3|flux|ideogram|recraft|nova-canvas"
    r"|nano-banana|playground-v\d|[-_.]image(?![a-z])",
    re.IGNORECASE,
)


def _primary_modality(name: str) -> str | None:
    """모델명의 1차 능력. 게이트웨이가 라우팅하지 않는 음성 모델(TTS·STT)이면 None이다."""
    n = name.lower()
    # 임베딩보다 먼저 본다 — 리랭커 이름에 embed 계열 단어가 섞여도 리랭커다(#209)
    if "rerank" in n:
        return "reranker"
    if "embed" in n:
        return "embedding"
    # 임베딩 다음, 음성·채팅보다 먼저 본다 — `titan-embed-image`는 임베딩이다(#347)
    if _IMAGE_HINT.search(name):
        return "image"
    if "whisper" in n or "transcrib" in n or "stt" in n or "tts" in n or "speech" in n:
        return None
    return "llm"


def _capabilities(name: str, base: str | None = None) -> list[str]:
    """모델명으로부터 다중 능력을 추론한다.

    base가 주어지면 그것을 1차 능력으로 쓰고(예: google의 embedContent),
    그렇지 않으면 모델명에서 1차 능력을 판별한다.
    1차 능력이 llm이고 비전 모델명이면 vlm을 함께 부여한다. 음성 모델이면 빈 목록이다.
    """
    primary = base or _primary_modality(name)
    if primary is None:
        return []
    caps = [primary]
    if primary == "llm" and _VISION_HINT.search(name):
        caps.append("vlm")
    return caps


# OpenRouter `architecture.output_modalities` 값 → 게이트웨이 능력.
# video·음성(transcription·speech) 등 게이트웨이가 라우팅하지 않는 출력은 매핑하지 않는다.
# image는 v0.23.0(#338)부터 라우팅한다(#347).
_OUTPUT_MODALITY_CAPS = {
    "text": "llm",
    "image": "image",
    "embeddings": "embedding",
    "rerank": "reranker",
}


def _modalities(value: Any) -> list[str] | None:
    """외부 응답의 모달리티 목록 — 문자열 리스트가 아니면 None(모델명 추론으로 되돌린다)."""
    if not isinstance(value, list):
        return None
    return [v for v in value if isinstance(v, str)]


def _architecture_capabilities(arch: dict[str, Any]) -> list[str] | None:
    """OpenRouter 모델 메타데이터(architecture)로 능력을 판별한다.

    텍스트 출력 모델이 이미지 입력을 받으면 vlm을 함께 부여한다. 이미지를 출력하면 텍스트를
    함께 내도 `["image"]`(단독 능력, #338 D2)다 — 채팅 모델 선택에 섞이지 않게 한다(#347).
    매핑되는 출력이 없으면 빈 목록을, 메타데이터 형태가 예상과 다르거나 출력 모달리티가
    비었으면 None을 반환한다(호출자가 모델명 추론을 쓴다).
    """
    outputs = _modalities(arch.get("output_modalities"))
    if not outputs:
        return None
    inputs = _modalities(arch.get("input_modalities")) or []
    caps: list[str] = []
    for out in outputs:
        cap = _OUTPUT_MODALITY_CAPS.get(out)
        if cap and cap not in caps:
            caps.append(cap)
    if "image" in caps:
        return ["image"]
    if "llm" in caps and "image" in inputs:
        caps.insert(caps.index("llm") + 1, "vlm")
    return caps


# 게이트웨이 계열 서버가 `/models` 항목에 붙이는 `type` 값. 게이트웨이 능력(MODALITIES)과
# 같은 어휘(`reranker` 포함, #209)는 그대로 쓴다. 음성 모델은 게이트웨이가 라우팅하지 않아 뺀다.
_UNROUTED_TYPES = frozenset({"tts", "stt", "speech", "transcription", "audio"})


def _type_capabilities(raw_type: Any, raw_id: str) -> list[str] | None:
    """OpenAI 호환 `/models` 항목의 `type`으로 능력을 판별한다.

    `type`이 있으면 모델명 추론보다 우선한다(예: `bge-m3`는 이름만으로는 알 수 없다).
    `type`이 없거나 문자열이 아니거나 모르는 값이면 None을 반환한다(호출자가 모델명 추론을 쓴다).
    """
    if not isinstance(raw_type, str):
        return None
    t = raw_type.strip().lower()
    if t == "vlm":
        return ["llm", "vlm"]
    if t in _UNROUTED_TYPES:
        return []
    if t in MODALITIES:
        return _capabilities(raw_id, t)
    return None


def _entry(model_name: str, raw_id: str, modality: Modality | None = None) -> dict[str, Any]:
    # 호출 지점에 직접 쓴 능력 값을 타입으로 묶는다 — DiscoveredModel이 Modality 밖 값을 받으면
    # available-models 응답 전체가 500이다(#218)
    return {
        "model_name": model_name,
        "raw_id": raw_id,
        "display_name": raw_id,
        "capabilities": _capabilities(raw_id, modality),
    }


def _is_openrouter(base: str) -> bool:
    host = urlsplit(base).hostname or ""
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def _api_key(provider: LLMProvider) -> str | None:
    return decrypt(provider.api_key_encrypted) if provider.api_key_encrypted else None


async def _get_json(url: str, headers: dict[str, str] | None = None) -> Any:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers=headers or {})
    except httpx.HTTPError as e:
        raise DiscoveryError(f"프로바이더에 연결할 수 없습니다: {e}") from e
    if resp.status_code == 401 or resp.status_code == 403:
        raise DiscoveryError("인증 실패 — API 키를 확인하세요")
    if resp.status_code >= 400:
        raise DiscoveryError(f"프로바이더 응답 오류 {resp.status_code}: {resp.text[:200]}")
    return resp.json()


async def _openai_compatible(provider: LLMProvider, prefix: str) -> list[dict[str, Any]]:
    """OpenAI 호환 /models 엔드포인트 (openai, vllm, lmstudio, custom).

    OpenRouter(`openrouter.ai`)의 /models는 기본으로 텍스트 출력 모델만 돌려주므로
    `output_modalities=all`로 임베딩·STT·TTS 모델까지 받는다. OpenRouter 응답은
    `architecture` 메타데이터로 능력을 판별하고, 게이트웨이 능력으로 매핑되지 않는
    모델(비디오 생성 등)은 뺀다. 이미지 생성 모델은 `image`다(#347). 다른 OpenAI 호환
    서버는 `architecture`가 있어도 무시한다. 항목에 유효한 `type`이 있으면(어느 호스트든)
    `architecture`로 판별되지 않은 모델에 한해 그 값으로 능력을 정한다(`reranker` 포함, #209).
    `type`이 없거나 모르는 값이면 모델명으로 추론한다.
    """
    base = (provider.base_url or _default_base(provider.type)).rstrip("/")
    key = _api_key(provider)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    openrouter = _is_openrouter(base)
    url = f"{base}/models"
    if openrouter:
        url += "?output_modalities=all"
    data = await _get_json(url, headers)
    out = []
    for m in data.get("data", []):
        raw = m.get("id")
        if not raw:
            continue
        model_name = f"{prefix}/{raw}" if prefix else raw
        entry = _entry(model_name, raw)
        arch = m.get("architecture")
        caps = _architecture_capabilities(arch) if openrouter and isinstance(arch, dict) else None
        if caps is None:
            caps = _type_capabilities(m.get("type"), raw)
        if caps is not None:
            if not caps:
                continue
            entry["capabilities"] = caps
        out.append(entry)
    return out


async def _ollama(provider: LLMProvider) -> list[dict[str, Any]]:
    base = (provider.base_url or _default_base("ollama")).rstrip("/")
    data = await _get_json(f"{base}/api/tags")
    out = []
    for m in data.get("models", []):
        raw = m.get("name")
        if not raw:
            continue
        out.append(_entry(f"ollama/{raw}", raw))
    return out


async def _anthropic(provider: LLMProvider) -> list[dict[str, Any]]:
    base = (provider.base_url or _default_base("anthropic")).rstrip("/")
    key = _api_key(provider)
    if not key:
        raise DiscoveryError("Anthropic은 API 키가 필요합니다")
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    data = await _get_json(f"{base}/models", headers)
    out = []
    for m in data.get("data", []):
        raw = m.get("id")
        if not raw:
            continue
        out.append(_entry(f"anthropic/{raw}", raw, "llm"))
    return out


async def _google(provider: LLMProvider) -> list[dict[str, Any]]:
    base = (provider.base_url or _default_base("google")).rstrip("/")
    key = _api_key(provider)
    if not key:
        raise DiscoveryError("Google Gemini은 API 키가 필요합니다")
    data = await _get_json(f"{base}/models?key={quote(key)}")
    out = []
    for m in data.get("models", []):
        raw = (m.get("name") or "").removeprefix("models/")
        if not raw:
            continue
        methods = m.get("supportedGenerationMethods", [])
        # Imagen은 `predict`만 낸다(#347). 이미지 출력 Gemini는 `generateContent`라 이름으로 본다
        modality: Modality | None = None
        if "embedContent" in methods:
            modality = "embedding"
        elif "predict" in methods:
            modality = "image"
        out.append(_entry(f"gemini/{raw}", raw, modality))
    return out


def _bedrock_static() -> list[dict[str, Any]]:
    """Bedrock은 키 없이 라이브 조회가 어려워(AWS sigv4) litellm 정적 목록을 사용한다."""
    import litellm

    prefixes = ("anthropic.", "amazon.", "meta.", "mistral.", "cohere.", "ai21.", "deepseek.")
    out = []
    seen: set[str] = set()
    for raw in sorted(litellm.models_by_provider.get("bedrock", set())):
        # 가격표 변형 키(예: "1024-x-1024/...") 제외, 실제 모델 ID만
        if "/" in raw or not raw.startswith(prefixes):
            continue
        if raw in seen:
            continue
        seen.add(raw)
        out.append(_entry(f"bedrock/{raw}", raw))
    return out


async def discover_provider_models(provider: LLMProvider) -> list[dict[str, Any]]:
    """프로바이더 타입별로 사용 가능한 모델 목록을 조회한다.

    반환: [{model_name, raw_id, display_name, capabilities}, ...]. 게이트웨이가 라우팅할 능력이
    없는 모델(음성 등)은 뺀다.
    """
    return [m for m in await _discover(provider) if m["capabilities"]]


async def _discover(provider: LLMProvider) -> list[dict[str, Any]]:
    t = provider.type
    if t == "mock":
        raise DiscoveryError("mock 프로바이더는 모델 디스커버리를 지원하지 않습니다")
    if t in ("openai", "vllm", "lmstudio", "custom"):
        prefix = {
            "openai": "",
            "vllm": "hosted_vllm",
            "lmstudio": "lm_studio",
            "custom": "openai",
        }[t]
        return await _openai_compatible(provider, prefix)
    if t == "ollama":
        return await _ollama(provider)
    if t == "anthropic":
        return await _anthropic(provider)
    if t == "google":
        return await _google(provider)
    if t == "bedrock":
        return _bedrock_static()
    if t == "azure":
        raise DiscoveryError(
            "Azure는 배포(deployment) 이름이 사용자 지정이라 자동 조회가 불가합니다 — "
            "모델을 직접 등록하세요 (예: azure/<deployment-name>)"
        )
    raise DiscoveryError(f"지원하지 않는 프로바이더 타입: {t}")
