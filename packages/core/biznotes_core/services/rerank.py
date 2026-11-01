"""리랭커 — 로컬 cross-encoder(fastembed TextCrossEncoder)와 게이트웨이 리랭커(#209).

임베딩 모델과 동일하게 지연 로드 + 전역 캐시 (워커/API 프로세스당 1회 로드).
하이브리드 검색 후보(상위 N)를 질의-문서 쌍 점수로 재정렬한다.

어드민 설정 `rag.rerank_model`의 값 형식(#209):

- `gateway:<model_id>:<model_name>` — 등록된 게이트웨이 리랭커(`reranker` 능력)
- `fastembed:<model>` — 로컬 cross-encoder
- 접두사 없는 값(예: `BAAI/bge-reranker-base`) — 기존 저장값. 읽을 때 `fastembed:`로 본다
- `""` — 자동: 활성 기본 리랭커가 있으면 그것, 없으면 로컬 `DEFAULT_RERANK_MODEL`

임계값(`insufficient_context_threshold_rerank`)과 비교하는 점수는 **항상 [0,1]**이다.
로컬은 raw logit에 sigmoid를 한 번 걸고, 게이트웨이 `relevance_score`는 이미 [0,1]이라 그대로
쓴다(`rerank_relevance`). 해석·호출이 실패하면 `RerankUnavailableError`를 낸다 — 하이브리드
점수로 조용히 넘어가지 않는다.
"""

import math
import os
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from biznotes_core.services.gateway.rerank import RerankCall

DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-base"
GATEWAY_RERANK_PREFIX = "gateway:"
FASTEMBED_RERANK_PREFIX = "fastembed:"
# 리랭크 1회의 벽시계 상한 기본값(초) — 어드민 설정 `rag.rerank_timeout_seconds`
DEFAULT_RERANK_TIMEOUT_SECONDS = 30

# Cohere 규격 `/rerank`를 부를 수 있는 프로바이더 타입과, 저장된 `model_name`에 붙는 litellm
# 라우팅 접두사(디스커버리가 붙인다). 요청 본문에는 접두사를 뗀 서버의 모델 id를 보낸다
_RERANK_PROVIDER_PREFIXES = {
    "openai": "",
    "vllm": "hosted_vllm/",
    "lmstudio": "lm_studio/",
    "custom": "openai/",
}

_CACHE_DIR = os.environ.get("FASTEMBED_CACHE_PATH")


class RerankUnavailableError(Exception):
    """리랭커를 해석하거나 호출하지 못했다 — 리랭크가 켜진 검색은 진행하지 않는다.

    메시지에 리랭커 이름·모델 이름이 담긴다(관리자 진단용). 노드 실행 로그에는 고정 문구만
    남긴다(#216).
    """

    node_error_code = "rerank_unavailable"
    node_log_message = "리랭커를 사용할 수 없어 검색을 진행하지 못했습니다"


@lru_cache
def _encoder(model_name: str):
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(model_name=model_name, cache_dir=_CACHE_DIR)


def rerank_scores(
    query: str, texts: list[str], model_name: str = DEFAULT_RERANK_MODEL
) -> list[float]:
    """로컬 cross-encoder의 질의-문서 raw logit (입력 순서 유지, 무경계)."""
    if not texts:
        return []
    return [float(s) for s in _encoder(model_name).rerank(query, texts)]


def rerank_order(
    query: str, texts: list[str], top_k: int, model_name: str = DEFAULT_RERANK_MODEL
) -> list[int]:
    """점수 내림차순 상위 top_k 인덱스."""
    scores = rerank_scores(query, texts, model_name)
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return order[:top_k]


def normalize_rerank_key(value: str | None) -> str:
    """저장값을 키 형식으로. 접두사 없는 기존 값은 `fastembed:`로 본다(데이터 이관 없음)."""
    key = (value or "").strip()
    if not key or key.startswith((GATEWAY_RERANK_PREFIX, FASTEMBED_RERANK_PREFIX)):
        return key
    return f"{FASTEMBED_RERANK_PREFIX}{key}"


def is_valid_rerank_key(value: str) -> bool:
    """어드민 입력 형식 검사 — `gateway:`는 UUID와 이름이 있어야 한다. 존재 여부는 보지 않는다."""
    key = normalize_rerank_key(value)
    if not key:
        return True
    if key.startswith(FASTEMBED_RERANK_PREFIX):
        return bool(key.removeprefix(FASTEMBED_RERANK_PREFIX))
    model_id, sep, model_name = key.removeprefix(GATEWAY_RERANK_PREFIX).partition(":")
    try:
        uuid.UUID(model_id)
    except ValueError:
        return False
    return bool(sep and model_name)


def gateway_rerank_key(model_id: uuid.UUID, model_name: str) -> str:
    return f"{GATEWAY_RERANK_PREFIX}{model_id}:{model_name}"


@dataclass(frozen=True)
class RerankTarget:
    """해석된 리랭커 — 로컬 모델명 또는 게이트웨이 호출 스냅샷 중 하나."""

    key: str
    display_name: str
    local_model: str | None = None
    gateway: "RerankCall | None" = None


def _request_model(provider_type: str, model_name: str) -> str:
    """저장된 `model_name`에서 litellm 라우팅 접두사를 뗀다(첫 세그먼트 하나만)."""
    prefix = _RERANK_PROVIDER_PREFIXES[provider_type]
    if prefix and model_name.startswith(prefix):
        return model_name.removeprefix(prefix)
    return model_name


def gateway_target(resolved: object, timeout: float) -> RerankTarget:
    """`ResolvedModel` → 게이트웨이 리랭크 스냅샷. 리랭크를 못 하는 프로바이더면 오류."""
    from biznotes_core.services.gateway.discovery import _default_base
    from biznotes_core.services.gateway.llm import ResolvedModel
    from biznotes_core.services.gateway.rerank import RerankCall

    assert isinstance(resolved, ResolvedModel)
    model, provider = resolved.model, resolved.provider
    display = model.display_name or model.model_name
    if provider.type not in _RERANK_PROVIDER_PREFIXES:
        raise RerankUnavailableError(
            f"리랭커 {display}: 프로바이더 타입 {provider.type}은 리랭크를 지원하지 않습니다 "
            f"(가능: {', '.join(_RERANK_PROVIDER_PREFIXES)})"
        )
    params = resolved.litellm_params
    api_base = params.get("api_base")
    if not api_base:
        if provider.type != "openai":
            # 기본 base로 떨어뜨리면 사내 게이트웨이 키가 외부(OpenAI)로 나간다 — 보내지 않는다
            raise RerankUnavailableError(f"리랭커 {display}: 프로바이더 base_url이 없습니다")
        api_base = _default_base("openai")
    return RerankTarget(
        key=gateway_rerank_key(model.id, model.model_name),
        display_name=display,
        gateway=RerankCall(
            api_base=str(api_base),
            model=_request_model(provider.type, model.model_name),
            display_name=display,
            timeout=float(timeout),
            api_key=params.get("api_key"),
            model_id=model.id,
            provider_type=provider.type,
        ),
    )


def _local_target(model_name: str) -> RerankTarget:
    return RerankTarget(
        key=f"{FASTEMBED_RERANK_PREFIX}{model_name}",
        display_name=model_name,
        local_model=model_name,
    )


async def resolve_reranker(
    db: "AsyncSession",
    value: str | None,
    rerank_timeout: float = DEFAULT_RERANK_TIMEOUT_SECONDS,
) -> RerankTarget:
    """설정값으로 리랭커를 해석한다. 쓸 수 없으면 `RerankUnavailableError`.

    검색 스레드에 들어가기 전에 부른다 — 반환값은 ORM 객체를 들고 있지 않은 스냅샷이다.
    """
    from sqlalchemy import select

    from biznotes_core.models import LLMModel, LLMProvider
    from biznotes_core.services.gateway.llm import ResolvedModel, list_models

    key = normalize_rerank_key(value)
    if not key:
        default = next(
            (m for m in await list_models(db, "reranker") if m.model.is_default), None
        )
        if default is None:
            return _local_target(DEFAULT_RERANK_MODEL)
        return gateway_target(default, rerank_timeout)
    if key.startswith(FASTEMBED_RERANK_PREFIX):
        return _local_target(key.removeprefix(FASTEMBED_RERANK_PREFIX))

    model_id, _, model_name = key.removeprefix(GATEWAY_RERANK_PREFIX).partition(":")
    try:
        mid = uuid.UUID(model_id)
    except ValueError as exc:
        raise RerankUnavailableError(f"리랭커 설정 형식이 잘못됐습니다: {key}") from exc
    row = (
        await db.execute(
            select(LLMModel, LLMProvider)
            .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
            .where(LLMModel.id == mid, LLMModel.model_name == model_name)
        )
    ).first()
    if row is None:
        raise RerankUnavailableError(f"설정된 리랭커가 없습니다(삭제·이름 변경): {model_name}")
    model, provider = row[0], row[1]
    if "reranker" not in model.capabilities:
        raise RerankUnavailableError(f"{model_name}은 리랭커 능력이 없습니다")
    if not model.enabled or not provider.enabled:
        raise RerankUnavailableError(f"설정된 리랭커 {model_name}이 비활성입니다")
    return gateway_target(ResolvedModel(model=model, provider=provider), rerank_timeout)


def rerank_relevance(target: RerankTarget, query: str, texts: list[str]) -> list[float]:
    """문서별 관련도([0,1], 입력 순서). 동기 — 검색 스레드에서 부른다.

    로컬은 raw logit에 sigmoid를 한 번 걸고, 게이트웨이 점수는 그대로 쓴다(이중 sigmoid 금지).
    """
    if not texts:
        return []
    if target.gateway is not None:
        from biznotes_core.services.gateway.rerank import rerank

        return rerank(target.gateway, query, texts)
    assert target.local_model is not None
    try:
        logits = rerank_scores(query, texts, model_name=target.local_model)
    except Exception as exc:  # noqa: BLE001 - 모델 다운로드 불가 등도 같은 종류로 드러낸다
        raise RerankUnavailableError(
            f"로컬 리랭커 {target.local_model}를 쓸 수 없습니다: {type(exc).__name__}"
        ) from exc
    return [_sigmoid(x) for x in logits]


def _sigmoid(x: float) -> float:
    # 큰 음수에서 exp가 넘치지 않게 부호로 나눈다
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


__all__ = [
    "DEFAULT_RERANK_MODEL",
    "DEFAULT_RERANK_TIMEOUT_SECONDS",
    "FASTEMBED_RERANK_PREFIX",
    "GATEWAY_RERANK_PREFIX",
    "RerankTarget",
    "RerankUnavailableError",
    "gateway_rerank_key",
    "gateway_target",
    "is_valid_rerank_key",
    "normalize_rerank_key",
    "rerank_order",
    "rerank_relevance",
    "rerank_scores",
    "resolve_reranker",
]
