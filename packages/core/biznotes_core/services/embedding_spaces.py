"""임베딩 공간 — 벡터를 만든 모델 (#7).

- 어드민이 등록한 기본 임베딩 모델(`embedding` 능력, 전역 1개)이 있으면 그 모델이
  **활성 공간**이다. 게이트웨이(LiteLLM)로 임베딩한다.
- 등록된 모델이 없으면 로컬 fastembed 기본 모델이 활성 공간이다 —
  API 키 없이·폐쇄망에서 동작하는 기본값.
- 소스는 자기 벡터를 만든 공간 키를 `sources.embedding_model`에 기록한다(NULL은
  기록 이전 = 로컬 기본 모델). 벡터는 청크 행에 있고(`source_chunks.embedding`), 질의는
  **소스가 속한 공간마다 그 공간의 모델로** 임베딩해 그 공간의 소스 청크에서만 찾는다
  (`vectorstore`). 서로 다른 공간의 벡터를 비교하는 일이 없고,
  모델을 바꾼 뒤 재인덱싱이 끝나기 전에도 기존 소스가 검색에서 빠지지 않는다.
- 공간의 모델을 더 이상 쓸 수 없으면(삭제·`model_name` 변경) 그 공간의
  소스는 질의할 수 없다 — 검색에서 빠지고 `/admin/rag/embedding`에
  `available=false`로 드러난다. 비활성 모델은 질의에 계속 쓸 수 있다 — 그래서
  **교체 절차는 "기존 모델 비활성화 → 새 모델 등록 → 기본값 지정 → stale_only
  재인덱싱 → 기존 모델 삭제"** 순서다. 옛 모델을 먼저 **지우면** 재인덱싱이 끝날
  때까지 그 벡터를 질의할 수 없다. 등록 제한이 활성 모델만 세는 이유가 이것이다
  (`services/llm_admin.embedding_model_exists`).

희소(BM25) 벡터는 모델과 무관한 로컬 계산이라 모든 공간이 같은 것을 쓴다.
"""

import hashlib
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import ColumnElement, and_, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.models import LLMModel, LLMProvider, Source
from biznotes_core.services.gateway.llm import ResolvedModel, list_models
from biznotes_core.services.gateway.retry import (  # noqa: F401 - 테스트가 이 모듈의 이름으로 본다
    _BACKOFF_BASE_SECONDS,
    _BACKOFF_CAP_SECONDS,
    _RATE_LIMITED_STATUSES,
    _SERVER_ERROR_STATUSES,
    _backoff,
    _classify,
    _Failure,
    _parse_retry_after,
)
from biznotes_core.services.ingestion.embedder import DEFAULT_DENSE_MODEL

logger = structlog.get_logger()

LOCAL_SPACE_KEY = f"fastembed:{DEFAULT_DENSE_MODEL}"
_GATEWAY_PREFIX = "gateway:"
# 게이트웨이 임베딩 요청 1회당 입력 수 — 제공자 배치 상한(수십~수천) 중 보수적인 값
GATEWAY_BATCH_SIZE = 64
# mock 제공자의 결정적 임베딩 차원 — 외부 키 없이 전체 파이프라인을 검증하는 용도
MOCK_EMBEDDING_DIM = 64
# 게이트웨이 임베딩 호출 1건의 상한(초) — 응답 없는 제공자가 워커를 잡아두지 않게 한다
GATEWAY_TIMEOUT_SECONDS = 60.0
# 일시 오류 재시도(#194). 배치 1건의 요청 상한(타임아웃 재전송 포함)과 대기 합계 상한(초).
# 인제스트는 워커가 기다리고, 질의는 사용자가 기다린다
_MAX_ATTEMPTS = 5
INGEST_RETRY_BUDGET_SECONDS = 300.0
_QUERY_RETRY_BUDGET_SECONDS = 10.0
# 배치 1건의 벽시계 상한(초) — 다음 대기와 다음 요청 timeout을 더해 넘으면 다시 보내지 않는다.
# 배치 사이 heartbeat 간격이 reaper 창(15분)보다 짧게 유지된다(rag 설정 timeout 상한 180초)
_BATCH_WALL_SECONDS = 600.0
# 프로바이더 동시성 게이트(#196)
# 리스 TTL = 요청 timeout의 2배 + 여유. TTL은 **누수 방지 장치**다 — 리스를 잡은 프로세스가
# 죽으면 다음 획득이 만료 멤버를 걷어낸다. 게이트웨이 대기는 최소 이만큼 이어야 한다
# (limit=1·워커 2개면 B는 A의 리스가 풀릴 때까지 기다려야 제대로 동작한다)
_GATE_LEASE_SLACK_SECONDS = 30.0
# 질의는 사용자가 기다리는 경로라 게이트 대기를 짧게 둔다. 실패하면 `TransientEmbeddingError`가
# 나고 `search_spaces`가 공간별로 삼킨다(공간이 하나면 재던지)
_QUERY_GATE_WAIT_SECONDS = 2.0


class EmbeddingError(Exception):
    pass


class TransientEmbeddingError(EmbeddingError):
    """일시 오류(429·5xx·타임아웃·연결 오류)가 재시도 상한까지 이어졌다.

    나중에 다시 시도할 수 있다.

    `retry_after`는 서버가 마지막으로 알려 준 대기 시간(초)이다. 없으면 None이다.

    `reason`은 **왜** 재시도 한도에 닿았는지다(#196). `"transient"`(기본값)는 네트워크·429·
    5xx처럼 원인이 Provider인 경우, `"gate"`는 프로바이더 동시성 상한에 밀려 대기 예산이 바닥난
    경우다. 인제스트는 이 둘의 재시도 예산을 따로 센다 — 상한에 걸린 것을 Provider 장애로
    세면 그 장애로 생긴 유예까지 소스의 실패로 닫힌다.
    """

    def __init__(
        self,
        message: str,
        retry_after: float | None = None,
        reason: str = "transient",
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.reason = reason


@dataclass
class RetryBudget:
    """일시 오류 재시도의 대기 예산(초) — 한 인제스트 시도의 배치 호출들이 나눠 쓴다."""

    seconds: float


class _NoSdkRetries(int):
    """참으로 평가되는 0 — LiteLLM에 `max_retries=0`을 그대로 전하기 위한 값.

    LiteLLM 1.100.1의 OpenAI 경로(`llms/openai/openai.py` `embedding`)는
    `max_retries or DEFAULT_MAX_RETRIES`로 0을 2로 바꾼다. 그러면 SDK가 타임아웃마다 같은
    배치를 곧바로 다시 보내 서버 큐에 고아 요청을 쌓는다(#194). 재시도는 이 모듈이 정한다.
    LiteLLM이 바뀌어 이 값이 통하지 않으면 tests/test_embedding_retry.py의 요청 수 검사가 깨진다.
    """

    def __bool__(self) -> bool:
        return True


_SDK_NO_RETRIES = _NoSdkRetries(0)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


@dataclass(eq=False)
class GatewayCall:
    """게이트웨이 임베딩 호출에 필요한 값만 담은 스냅샷.

    ORM 객체를 그대로 들고 다니지 않는 이유: 검색은 `anyio.to_thread`에서 돌고,
    세션이 커밋·롤백되면 ORM 속성이 만료돼 스레드에서 지연 로드(`MissingGreenlet`)가
    난다. 필요한 네 값만 미리 떠 둔다.
    """

    provider_type: str
    model: str
    display_name: str
    params: dict[str, Any]
    # [#196] 프로바이더 단위 동시성 상한. 둘 다 기본값이라 기존 생성자 호출은 그대로다.
    # `max_concurrency`는 **모델 행이 아니라 프로바이더 행**에서 읽는다 — 같은 프로바이더의
    # 임베딩 모델이 둘이어도(교체 절차상 이전 모델이 비활성으로 남아도) 서버는 하나다
    provider_id: uuid.UUID | None = None
    max_concurrency: int | None = None
    # 사용량 원장(#366)에 남길 모델 행 id와 단가 — 기본값이라 기존 생성자 호출은 그대로다
    model_id: uuid.UUID | None = None
    cost_per_1k: dict[str, Any] | None = None


@dataclass(frozen=True)
class EmbeddingSpace:
    key: str  # sources.embedding_model에 기록되는 값 — 검색은 이 값으로 소스를 거른다
    display_name: str
    call: GatewayCall | None = field(default=None, compare=False, hash=False)

    @property
    def is_local(self) -> bool:
        return self.call is None

    def embed(
        self,
        texts: list[str],
        *,
        batch_size: int | None = None,
        timeout: float | None = None,
        retry_budget: RetryBudget | None = None,
    ) -> list[list[float]]:
        """동기 호출 — 워커·스레드에서 부른다.

        키워드 인자는 게이트웨이 공간에만 쓴다. `batch_size`·`timeout`은 `rag` 설정
        `embedding_batch_size`·`embedding_timeout_seconds`이고, 생략하면 기본 상수를 쓴다.
        `retry_budget`을 넘기면 여러 호출이 대기 예산을 나눠 쓴다(생략하면 이 호출만의 예산).
        """
        if self.call is None:
            from biznotes_core.services.ingestion import embedder

            return embedder.embed_dense(texts)
        return _gateway_embed(
            self.call,
            texts,
            batch_size=batch_size or GATEWAY_BATCH_SIZE,
            timeout=timeout or GATEWAY_TIMEOUT_SECONDS,
            retry_budget=retry_budget or RetryBudget(INGEST_RETRY_BUDGET_SECONDS),
        )

    def embed_query(self, text: str) -> list[float]:
        if self.call is None:
            from biznotes_core.services.ingestion import embedder

            return embedder.embed_query_dense(text)
        # 사용자가 기다리는 경로다 — 짧은 예산만 쓰고 타임아웃 뒤에는 다시 보내지 않는다.
        # 게이트 대기도 짧게 둔다(#196). 상한에 밀리면 공간 단위로 삼킨다
        return _gateway_embed(
            self.call,
            [text],
            timeout=GATEWAY_TIMEOUT_SECONDS,
            retry_budget=RetryBudget(_QUERY_RETRY_BUDGET_SECONDS),
            resend_after_timeout=False,
            gate_wait_cap=_QUERY_GATE_WAIT_SECONDS,
        )[0]


LOCAL_SPACE = EmbeddingSpace(key=LOCAL_SPACE_KEY, display_name=DEFAULT_DENSE_MODEL)


def _gateway_key(resolved: ResolvedModel) -> str:
    # 모델 행 id(불변)와 model_name을 함께 담는다 — 같은 행에서 model_name만 바꾸면
    # 실제 모델이 바뀐 것이므로 다른 공간이 되어야 한다.
    return f"{_GATEWAY_PREFIX}{resolved.model.id}:{resolved.model.model_name}"


def _gateway_space(resolved: ResolvedModel) -> EmbeddingSpace:
    key = _gateway_key(resolved)
    params = resolved.litellm_params
    return EmbeddingSpace(
        key=key,
        display_name=resolved.model.display_name,
        call=GatewayCall(
            provider_type=resolved.provider.type,
            model=params.pop("model"),
            display_name=resolved.model.display_name,
            params=params,
            # 동시성 상한은 여기서, 비동기 컨텍스트에서 읽는다 — `GatewayCall`의 docstring이
            # 설명한 `MissingGreenlet` 이유 때문이다(#196)
            provider_id=resolved.provider.id,
            max_concurrency=resolved.provider.embedding_max_concurrency,
            model_id=resolved.model.id,
            cost_per_1k=dict(resolved.model.cost_per_1k or {}),
        ),
    )


def _mock_embed(text: str) -> list[float]:
    vec = [0.0] * MOCK_EMBEDDING_DIM
    for token in text.lower().split():
        h = int.from_bytes(hashlib.sha256(token.encode()).digest()[:4], "big")
        vec[h % MOCK_EMBEDDING_DIM] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


def _gateway_embed(
    call: GatewayCall,
    texts: list[str],
    *,
    batch_size: int = GATEWAY_BATCH_SIZE,
    timeout: float = GATEWAY_TIMEOUT_SECONDS,
    retry_budget: RetryBudget | None = None,
    resend_after_timeout: bool = True,
    gate_wait_cap: float | None = None,
) -> list[list[float]]:
    if call.provider_type == "mock":
        return [_mock_embed(t) for t in texts]

    budget = retry_budget or RetryBudget(INGEST_RETRY_BUDGET_SECONDS)
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        resp = _embed_batch(call, batch, timeout, budget, resend_after_timeout, gate_wait_cap)
        data = sorted(resp.data, key=lambda d: d["index"])
        if len(data) != len(batch):
            raise EmbeddingError(
                f"임베딩 응답 수가 입력과 다릅니다({len(data)} != {len(batch)})"
            )
        vectors.extend([float(x) for x in d["embedding"]] for d in data)
    return vectors


def _embed_batch(
    call: GatewayCall,
    batch: list[str],
    timeout: float,
    budget: RetryBudget,
    resend_after_timeout: bool,
    gate_wait_cap: float | None = None,
) -> Any:
    """배치 1건 — 재시도는 이 함수가 정한다(SDK 자동 재시도는 끈다, #194).

    - 429·503: `Retry-After`가 있으면 그만큼, 없으면 백오프만큼 기다린 뒤 다시 보낸다.
    - 500·502·504·연결 오류: 백오프 뒤 다시 보낸다.
    - 타임아웃: 서버는 아직 그 요청을 처리 중이다. 곧바로 다시 보내면 큐에 고아 요청이
      쌓이므로 `timeout`만큼 기다린 뒤 한 번만 다시 보낸다.
    - 그 밖(4xx·응답 없는 클라이언트 오류): 영구 오류 — `EmbeddingError`.

    요청은 `_MAX_ATTEMPTS`회까지다. 대기는 `budget`에서 빼고(타임아웃 재전송 대기 포함),
    배치 하나는 `_BATCH_WALL_SECONDS`에 마지막 요청의 `timeout`을 더한 시간 안에 끝난다.
    상한에 닿으면 `TransientEmbeddingError`를 낸다.

    **프로바이더 동시성 게이트(#196)**: `max_concurrency`가 설정된 프로바이더는 슬롯을 잡은 뒤에만
    요청을 보낸다. 슬롯이 없으면 서버 큐를 채우는 대신 여기서 기다린다.
    게이트 **대기**는 `budget`에서 차감하지 않는다 — 용량 대기는 재시도가 아니라 큐에 선 것이고
    `RetryBudget`은 "재시도 대기 예산"이다. 대신 매 시도마다 잔여 벽시계로 게이트 대기를 묶는다.
    게이트 소진 뒤의 **재시도 지연**은 기존대로 `budget`에서 차감된다.
    """
    import litellm

    from biznotes_core.core import concurrency_gate

    started = time.monotonic()
    timeouts = 0
    lease_ttl = timeout * 2 + _GATE_LEASE_SLACK_SECONDS
    wait_cap = gate_wait_cap if gate_wait_cap is not None else lease_ttl
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        error: Exception
        # 잔여 벽시계는 매 시도 다시 잰다 — 루프 밖에서 한 번만 재면 게이트 대기가 고정돼
        # `_BATCH_WALL_SECONDS` 상한이 깨진다
        wall_left = max(0.0, _BATCH_WALL_SECONDS - (time.monotonic() - started) - timeout)
        # 마지막 시도는 기다리지 않는다 — 기다렸다 실패할 요청이 없다
        gate_budget = 0.0 if attempt == _MAX_ATTEMPTS else min(wait_cap, wall_left)
        try:
            with concurrency_gate.lease(
                call.provider_id,
                call.max_concurrency,
                wait_budget=gate_budget,
                lease_ttl=lease_ttl,
            ):
                # 타임아웃을 명시한다 — 없으면 응답 없는 제공자가 워커 루프를 무한정 잡는다
                resp = litellm.embedding(
                    model=call.model,
                    input=batch,
                    timeout=timeout,
                    max_retries=_SDK_NO_RETRIES,
                    **call.params,
                )
            return resp
        except concurrency_gate.GateTimeout as e:
            # 순서가 의무다: `_classify`는 모르는 예외를 "permanent"로 돌려 `EmbeddingError`
            # (영구)를 던진다. 일반 분기 뒤에 두면 소스가 `failed`로 닫힌다
            failure = _Failure("gate", retry_after=e.retry_after)
            error = e
            logger.warning(
                "임베딩 동시성 상한에 밀렸다 — 다음 시도를 본다",
                model=call.display_name,
                attempt=attempt,
                waited=round(e.waited, 1),
                retry_after=round(e.retry_after, 1),
            )
        except Exception as e:
            failure = _classify(e)
            if failure.kind == "permanent":
                raise EmbeddingError(f"임베딩 모델 호출 실패({call.display_name}): {e}") from e
            error = e
        delay: float | None
        if failure.kind == "timeout":
            timeouts += 1
            delay = timeout if resend_after_timeout and timeouts == 1 else None
        elif failure.retry_after is not None:
            delay = failure.retry_after
        else:
            delay = _backoff(attempt)
        elapsed = time.monotonic() - started
        if (
            delay is None
            or attempt == _MAX_ATTEMPTS
            or delay > budget.seconds
            or elapsed + delay + timeout > _BATCH_WALL_SECONDS
        ):
            raise TransientEmbeddingError(
                f"임베딩 모델 일시 오류({call.display_name}): {error}",
                retry_after=failure.retry_after,
                # "gate"를 `_run`까지 실어주는 유일한 고리 — 인제스트는 이것으로 재시도 예산을
                # 분리한다(Provider 장애로 세면 상한이 만든 유예까지 실패로 닫힌다)
                reason="gate" if failure.kind == "gate" else "transient",
            ) from error
        logger.warning(
            "임베딩 일시 오류 — 다시 시도한다",
            model=call.display_name,
            attempt=attempt,
            kind=failure.kind,
            delay=round(delay, 1),
        )
        _sleep(delay)
        budget.seconds -= delay
    raise AssertionError("unreachable")  # 루프의 마지막 시도는 반환하거나 예외를 낸다


async def active_space(db: AsyncSession) -> EmbeddingSpace:
    """지금 인제스천이 쓸 공간 — 활성화된 기본 임베딩 모델, 없으면 로컬 기본값."""
    models = await list_models(db, "embedding")
    default = next((m for m in models if m.model.is_default), None)
    return _gateway_space(default) if default else LOCAL_SPACE


async def resolve_space(db: AsyncSession, key: str | None) -> EmbeddingSpace | None:
    """기록된 공간 키로 질의 가능한 공간을 찾는다. 쓸 수 없으면 None.

    NULL(출처 기록 이전)은 로컬 기본 모델이다 — 기록을 시작하기 전의 벡터는 모두
    로컬 fastembed로 만들어졌다.
    """
    if key is None or key == LOCAL_SPACE_KEY:
        return LOCAL_SPACE
    if not key.startswith(_GATEWAY_PREFIX):
        return None
    model_id, _, model_name = key.removeprefix(_GATEWAY_PREFIX).partition(":")
    try:
        mid = uuid.UUID(model_id)
    except ValueError:
        return None
    row = (
        await db.execute(
            select(LLMModel, LLMProvider)
            .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
            # 질의 전용 해석이라 비활성 모델도 받는다 — 교체 절차에서 이전 모델은
            # 비활성으로 남겨 두고, 그 모델로 만든 벡터를 재인덱싱이 끝날 때까지
            # 계속 질의해야 한다. 자격 증명이 죽었으면 호출이 실패하고, 그 공간만
            # 검색에서 빠진다(search_spaces).
            .where(LLMModel.id == mid, LLMModel.model_name == model_name)
        )
    ).first()
    if row is None or "embedding" not in row[0].capabilities:
        return None
    return _gateway_space(ResolvedModel(model=row[0], provider=row[1]))


async def spaces_in_scope(
    db: AsyncSession,
    notebook_ids: Sequence[uuid.UUID],
    source_ids: Sequence[uuid.UUID] | None = None,
) -> list[EmbeddingSpace]:
    """검색 범위의 소스들이 속한 공간 + 활성 공간 (활성 공간이 맨 앞).

    활성 공간은 범위에 소스가 없어도 넣는다 — 범위를 모를 때(빈 노트북 등)의
    기존 동작(활성 공간 검색)을 유지한다. 쓸 수 없는 공간은 경고만 남기고 뺀다.
    """
    active = await active_space(db)
    q = select(Source.embedding_model).where(
        Source.notebook_id.in_(list(notebook_ids)), Source.chunk_count > 0
    )
    if source_ids is not None:  # [] = 선택한 소스 없음 (#8)
        q = q.where(Source.id.in_(list(source_ids)))
    keys = set((await db.execute(q.distinct())).scalars().all())

    spaces = [active]
    for key in sorted(keys, key=lambda k: k or ""):
        if (key or LOCAL_SPACE_KEY) == active.key:
            continue
        space = await resolve_space(db, key)
        if space is None:
            logger.warning("임베딩 모델을 쓸 수 없어 해당 소스를 검색에서 뺍니다", space_key=key)
        elif space not in spaces:
            spaces.append(space)
    return spaces


async def search_spaces(
    db: AsyncSession,
    spaces: Sequence[EmbeddingSpace],
    query: str,
    *,
    notebook_id: uuid.UUID | None = None,
    notebook_ids: Sequence[uuid.UUID] | None = None,
    source_ids: list[uuid.UUID] | None = None,
    limit: int = 8,
    group_size: int | None = None,
) -> list[Any]:
    """공간마다 그 모델로 질의 벡터를 만들어 그 공간의 청크에서 하이브리드 검색 후 합친다.

    임베딩(동기, CPU·HTTP)은 스레드에서, 검색은 `db` 세션으로 한다. 한 공간의 실패(모델 API
    장애 등)는 경고로 남기고 나머지로 답한다. 시도한 공간이 모두 실패하면 첫 오류를 올린다.

    group_size: 주면 소스별 묶음 검색(#446, `vectorstore.hybrid_search_groups`)이다 — `limit`은
    소스 수이고, 소스마다 청크를 `group_size`개까지 돌려준다(노트북 하나만 지원).
    """
    import anyio

    from biznotes_core.services import vectorstore
    from biznotes_core.services.ingestion import embedder

    sparse = await anyio.to_thread.run_sync(embedder.embed_query_sparse, query)
    per_space: list[list[Any]] = []
    errors: list[Exception] = []
    attempted = 0
    for space in spaces:
        # 게이트웨이 공간은 첫 인제스천 전에는 찾을 것이 없다 — 질의 임베딩(API 호출)을 아낀다
        if not space.is_local and not await vectorstore.space_has_chunks(db, space.key):
            continue
        attempted += 1
        scope = {"space": space.key, "local_space": LOCAL_SPACE_KEY}
        try:
            dense = await anyio.to_thread.run_sync(space.embed_query, query)
            if notebook_ids is not None:
                found = await vectorstore.hybrid_search_multi(
                    db, list(notebook_ids), dense, sparse, limit=limit, **scope
                )
            elif group_size is not None:
                assert notebook_id is not None
                found = await vectorstore.hybrid_search_groups(
                    db,
                    notebook_id,
                    dense,
                    sparse,
                    group_size=group_size,
                    limit=limit,
                    source_ids=source_ids,
                    **scope,
                )
            else:
                assert notebook_id is not None
                found = await vectorstore.hybrid_search(
                    db, notebook_id, dense, sparse, source_ids=source_ids, limit=limit, **scope
                )
        except Exception as e:
            if len(spaces) == 1:
                raise
            logger.warning("임베딩 공간 검색 실패", space_key=space.key, error=str(e))
            errors.append(e)
            continue
        per_space.append(found)
    # 시도한 공간이 모두 실패했으면 빈 결과로 감추지 않는다(건너뛴 공간은 시도가 아니다)
    if errors and len(errors) == attempted:
        raise errors[0]
    points_limit = limit * group_size if group_size is not None else limit
    if len(per_space) <= 1:
        return per_space[0][:points_limit] if per_space else []
    return _merge_by_rank(per_space, points_limit)


def _merge_by_rank(per_space: list[list[Any]], limit: int) -> list[Any]:
    """공간별 결과를 순위로 번갈아 섞는다.

    RRF 점수는 **그 공간 안에서의 순위**만 담는다 — 청크 3개짜리 공간의 1위와
    20만 개 공간의 1위가 같은 점수를 받는다. 점수로 정렬해 합치면 막 옮겨 간 작은
    공간이 늘 위로 올라온다(모델 전환 중 한 소스만 재인덱싱된 상태 등). 그래서
    같은 순위끼리 묶어 담고, 같은 순위 안에서만 점수로 정렬한다.

    공간이 섞이는 것은 모델 전환이 끝나기 전까지의 과도기 상태다 — 재인덱싱이 끝나
    공간이 하나가 되면 그 공간의 원래 순위가 그대로 쓰인다.
    """

    def score(point: Any) -> float:
        return point.score if getattr(point, "score", None) is not None else 0.0

    merged: list[Any] = []
    for rank in range(max(len(found) for found in per_space)):
        tier = [found[rank] for found in per_space if rank < len(found)]
        tier.sort(key=score, reverse=True)
        merged.extend(tier)
        if len(merged) >= limit:
            break
    return merged[:limit]


def chunk_config_of(rag_cfg: dict[str, Any]) -> dict[str, int]:
    """소스에 기록하는 청킹 설정 스냅샷."""
    return {
        "chunk_size": int(rag_cfg.get("chunk_size", 800)),
        "chunk_overlap": int(rag_cfg.get("chunk_overlap", 100)),
    }


def stale_condition(active_key: str, chunk_config: dict[str, int]) -> ColumnElement[bool]:
    """현재 공간·청킹 설정과 다르게 색인된 소스 — `stale_only` 재인덱싱 대상.

    청킹 설정이 기록되지 않은(출처 기록 이전) 소스는 설정을 알 수 없어 설정 기준으로는
    stale로 보지 않는다. 공간 키는 NULL을 로컬 기본 모델로 본다.
    """
    return or_(
        func.coalesce(Source.embedding_model, LOCAL_SPACE_KEY) != active_key,
        and_(
            # SQL NULL과 JSON null 모두 "모름"으로 본다
            func.jsonb_typeof(Source.chunk_config) == "object",
            Source.chunk_config != cast(chunk_config, JSONB),
        ),
    )
