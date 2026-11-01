"""LLM 게이트웨이 — DB(llm_providers/llm_models) 설정 기반 동적 라우팅.

모든 LLM 호출은 이 모듈을 경유한다 (사용량 기록·폴백·정책의 단일 지점).
provider.type == "mock"은 외부 키 없이 전체 파이프라인을 검증하기 위한
결정적 추출 응답 생성기다 (검색 컨텍스트에서 인용 포함 답변 조립).
"""

import asyncio
import copy
import hashlib
import json
import re
import uuid
from collections import OrderedDict
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import any_, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.core.crypto import decrypt
from biznotes_core.models import LLMModel, LLMProvider
from biznotes_core.models.llm import REASONING_EFFORTS
from biznotes_core.services.gateway.retry import classify_call_failure

logger = structlog.get_logger()

# 키 없이 쓰는 OpenAI 호환 서버 타입. OpenAI SDK(`openai/` 접두사)는 키가 없으면
# 서버가 인증을 요구하지 않아도 요청을 만들지 않는다(#193). 그래서 자리표시 키를
# 넘긴다. 탐색(`discovery`)은 키가 없으면 Authorization 헤더 없이 호출한다.
_KEYLESS_OPENAI_COMPATIBLE = ("custom", "vllm", "lmstudio")
_PLACEHOLDER_API_KEY = "not-needed"
# 연결 테스트 1건의 상한(초)
_PROBE_TIMEOUT_SECONDS = 15


class GatewayError(Exception):
    """게이트웨이 오류 — 모델 해석·입력 검증(TTS 빈 입력 등)·호출 실패. 노드 실패 코드는
    `gateway_error`이고(#216), 메시지는 그대로 실행 로그에 남는다(작성자가 고칠 수 있는 안내)."""

    node_error_code = "gateway_error"
    # 시도한 모델(#359) — `{id, display_name, error_code}`. LLM 호출이 모두 실패했을 때 채운다
    model_attempts: list[dict[str, str]] | None = None

    @property
    def node_log_output(self) -> dict[str, Any] | None:
        """실패한 노드의 실행 로그에 남길 값 — 시도한 모델(표시 이름·id·실패 코드)만 담는다."""
        return {"model_attempts": self.model_attempts} if self.model_attempts else None


class GatewayCallError(GatewayError):
    """모델 호출 실패. 메시지에 모델 이름·공급자 응답 원문이 담기므로 노드 실행
    로그에는 고정 문구만 남긴다(#216)."""

    node_error_code = "model_call_failed"
    node_log_message = "모델 호출에 실패했습니다"


class GatewayContextLengthError(GatewayCallError):
    """입력이 모델 컨텍스트 한도를 넘어 거부됐다(#358). 요청 문제라 같은 후보에 다시 보내지 않고
    제공자 장애(서킷)로 세지 않는다. `GatewayCallError`를 잡는 기존 처리도 그대로 탄다."""

    node_error_code = "context_length_exceeded"
    node_log_message = "입력이 모델 컨텍스트 한도를 넘었습니다"


# 컨텍스트 초과 응답 문구(#358). LiteLLM이 `ContextWindowExceededError`로 매핑하지 않는 서버가
# 있다 — vLLM `max_num_tokens`는 일반 400(`BadRequestError`)으로 온다(litellm 1.105.1 실측).
_CONTEXT_LENGTH_RE = re.compile(
    r"context[_ ]length[_ ]exceeded|maximum context length|max_num_tokens"
    r"|prompt is too long|input is too long|exceed context limit"
    r"|exceeds the maximum number of tokens|too many input tokens"
    r"|greater than the context length",
    re.IGNORECASE,
)
# 문구로 판별하는 응답 상태 — 요청 거부(400·413)만 본다. 429·5xx는 일시 오류 경로다
_CONTEXT_LENGTH_STATUSES = frozenset({400, 413})


def is_context_length_error(exc: BaseException) -> bool:
    """예외 사슬이 컨텍스트 초과 거부인지(#358).

    `litellm.ContextWindowExceededError`면 참이다. 그 밖에는 사슬의 첫 상태 코드가 400·413일 때
    알려진 문구(`max_num_tokens`·`maximum context length`·`prompt is too long` 등)로 판별한다.
    """
    import litellm

    status: int | None = None
    texts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, litellm.ContextWindowExceededError):
            return True
        code = getattr(cur, "status_code", None)
        if status is None and isinstance(code, int):
            status = code
        texts.append(str(cur))
        cur = cur.__cause__ or cur.__context__
    if status not in _CONTEXT_LENGTH_STATUSES:
        return False
    return any(_CONTEXT_LENGTH_RE.search(t) for t in texts)


def _failure_kind(error: GatewayError) -> str:
    """후보 하나의 실패를 어떻게 다룰지(#380) — 스트리밍·tool 경로가 같은 규칙을 쓴다.

    - `context`: 컨텍스트 초과(#358). 다시 보내지 않고 서킷에 세지 않는다.
    - `transient`: 타임아웃·429·503·5xx·연결 오류. 백오프 1회 재시도하고 서킷에 센다.
    - `auth`: 401·403. 다시 보내도 같아 재시도하지 않지만, 키·권한 문제라 서킷에는 센다(고칠
      때까지 같은 프로바이더를 계속 부르지 않게).
    - `permanent`: 400·404·422 등 영구 요청 오류(도구를 받지 않는 모델, 잘못된 모델 이름). 다시
      보내지 않고 서킷에 세지 않는다 — 같은 프로바이더의 다른 모델·채팅까지 막지 않는다.

    분류는 `retry.classify_call_failure`다 — 임베딩·리랭크와 같이 예외 사슬의 실제 HTTP 응답·전송
    오류·타임아웃으로 보고, 그 근거가 없을 때만(첫 조각 전 SSE 오류 등) LiteLLM `status_code`를
    보조로 쓴다. 둘 다 없는 예외는 `permanent`다.
    """
    if isinstance(error, GatewayContextLengthError):
        return "context"
    return classify_call_failure(error)


async def _count_provider_failure(candidate: "ResolvedModel") -> None:
    """프로바이더 실패 1건 — 오류 지표와 서킷에 센다(`transient`·`auth`, #380)."""
    from biznotes_core.core.metrics import llm_circuit_open_total, llm_provider_error_total
    from biznotes_core.services.gateway import circuit

    llm_provider_error_total.labels(provider=candidate.provider.name).inc()
    if await circuit.record_failure(candidate.provider.id):
        llm_circuit_open_total.labels(provider=candidate.provider.name).inc()


@dataclass
class ResolvedModel:
    model: LLMModel
    provider: LLMProvider

    @property
    def litellm_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"model": self.model.model_name}
        # base_url 미지정 시 로컬 LLM(ollama/lmstudio)은 settings의 호스트로 폴백
        # (도커 컨테이너에서 호스트의 로컬 서버에 닿기 위함)
        base_url = self.provider.base_url
        if not base_url and self.provider.type in ("ollama", "lmstudio"):
            from biznotes_core.config import get_settings

            host = get_settings().llm_local_host
            base_url = f"http://{host}:11434" if self.provider.type == "ollama" else f"http://{host}:1234/v1"
        if base_url:
            params["api_base"] = base_url
        if self.provider.api_key_encrypted:
            params["api_key"] = decrypt(self.provider.api_key_encrypted)
        elif self.provider.type in _KEYLESS_OPENAI_COMPATIBLE:
            params["api_key"] = _PLACEHOLDER_API_KEY
        return params


@dataclass
class StreamResult:
    """스트리밍 종료 후 채워지는 사용량 정보."""

    usage: dict[str, int] = field(default_factory=dict)
    # 폴백 체인 사용 시 실제로 응답한 모델 (None = 스트림 미시작)
    model: "ResolvedModel | None" = None
    fallback_used: bool = False
    # 공급자의 종료 사유(#422) — 스트림에서 None이 아닌 마지막 값. LiteLLM이 정규화한 `length`면
    # 출력 상한(`max_tokens`)에서 잘렸다. 값을 주지 않으면 None이다
    finish_reason: str | None = field(default=None, kw_only=True)
    # 응답한 후보에 보낸 출력 상한(#428) — 호출 계획(`plan_call`)이나 모델 최대 출력 토큰
    # (`max_output_tokens`)이 줄였으면 그 값이다
    max_tokens_used: int | None = field(default=None, kw_only=True)


# 호출 계획이 후보를 건너뛴 사유(#428) — 출력 하한을 지킬 몫이 남지 않는다. 시도 기록 코드이고,
# 컨텍스트 초과와 같이 센다(모두 이 사유면 `GatewayContextLengthError`)
OUTPUT_FLOOR_UNMET = "output_floor_unmet"


@dataclass(frozen=True)
class CallPlan:
    """후보 하나에 보낼 호출(#428) — `complete_stream_with_fallback`의 `plan_call`이 후보마다
    정한다.

    - `messages`·`max_tokens`: 이 후보에 보낼 메시지와 출력 상한
    - `skip_code`: 있으면 부르지 않고 다음 후보로 간다. 시도 기록에 이 코드가 남고 서킷·원장에는
      넣지 않는다
    - `budget`: 기록용 계산 근거(입력 예산 등). 게이트웨이는 건너뛸 때 로그에만 싣는다
    """

    messages: list[dict[str, str]]
    max_tokens: int
    skip_code: str | None = None
    budget: dict[str, Any] | None = None


@dataclass
class ToolCall:
    """모델이 요청한 단일 도구 호출."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolTurnResult:
    """tool-calling 1턴 결과 — 최종 텍스트 또는 도구 호출 목록."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # 다음 턴 대화에 다시 넣을 assistant 메시지(원본 tool_calls 포함). 공급자가 사고 블록을 주면
    # `thinking_blocks`와 그 블록을 만든 모델 표시를 싣는다(#429) — 게이트웨이에 다시 넘겨야 한다
    assistant_message: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    model: "ResolvedModel | None" = None
    # 체인의 1차 모델이 아닌 후보가 응답했다(#374)
    fallback_used: bool = False
    # 공급자의 종료 사유(#422, `StreamResult.finish_reason`과 같다)
    finish_reason: str | None = field(default=None, kw_only=True)
    # 이 턴에 보낸 출력 상한(#428) — 모델 최대 출력 토큰(`max_output_tokens`)으로 줄였으면 그 값
    max_tokens_used: int | None = field(default=None, kw_only=True)


async def list_models(
    db: AsyncSession, modality: str = "llm", enabled_only: bool = True
) -> list[ResolvedModel]:
    q = (
        select(LLMModel, LLMProvider)
        .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
        # 다중 능력: 요청 modality를 capabilities 배열에 포함하는 모델
        .where(literal(modality) == any_(LLMModel.capabilities))
        .order_by(LLMModel.is_default.desc(), LLMModel.display_name)
    )
    if enabled_only:
        q = q.where(LLMModel.enabled.is_(True), LLMProvider.enabled.is_(True))
    rows = await db.execute(q)
    return [ResolvedModel(model=m, provider=p) for m, p in rows.all()]


async def resolve_model(
    db: AsyncSession, model_id: uuid.UUID | None, modality: str = "llm"
) -> ResolvedModel:
    if model_id is not None:
        # 프로바이더가 꺼졌어도 쓰지 않는다 — 폴백 후보(`resolve_chain`)·run 모델 검증(#359)과
        # 같은 규칙이다
        row = await db.execute(
            select(LLMModel, LLMProvider)
            .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
            .where(
                LLMModel.id == model_id,
                LLMModel.enabled.is_(True),
                LLMProvider.enabled.is_(True),
            )
        )
        pair = row.first()
        if pair is None:
            raise GatewayError("요청한 모델을 사용할 수 없습니다")
        return ResolvedModel(model=pair[0], provider=pair[1])

    models = await list_models(db, modality)
    default = next((m for m in models if m.model.is_default), None)
    if default is None and models:
        default = models[0]
    if default is None:
        raise GatewayError(f"등록된 {modality} 모델이 없습니다 — 관리자가 모델을 등록해야 합니다")
    return default


async def resolve_chain(
    db: AsyncSession, model_id: uuid.UUID | None, modality: str = "llm"
) -> list[ResolvedModel]:
    """1차 모델 + fallback_priority 순서의 폴백 후보 목록."""
    primary = await resolve_model(db, model_id, modality)
    rows = await db.execute(
        select(LLMModel, LLMProvider)
        .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
        .where(
            literal(modality) == any_(LLMModel.capabilities),
            LLMModel.enabled.is_(True),
            LLMProvider.enabled.is_(True),
            LLMModel.fallback_priority.is_not(None),
            LLMModel.id != primary.model.id,
        )
        .order_by(LLMModel.fallback_priority)
    )
    return [primary] + [ResolvedModel(model=m, provider=p) for m, p in rows.all()]


def _preferred_first(
    chain: list[ResolvedModel], prefer_model_id: uuid.UUID | None
) -> list[ResolvedModel]:
    """시도 순서(#374) — `prefer_model_id` 후보부터 체인을 한 바퀴 돈다. 체인에 없으면 그대로다."""
    for idx, candidate in enumerate(chain):
        if prefer_model_id is not None and candidate.model.id == prefer_model_id:
            return chain[idx:] + chain[:idx]
    return chain


async def probe_connection(resolved: ResolvedModel, capability: str) -> dict[str, Any]:
    """연결 테스트 1건 — `llm`은 1토큰 완성, `embedding`은 1건 임베딩을 호출한다.

    반환: `{"ok": bool, "detail": str}`.
    """
    import litellm

    params = resolved.litellm_params
    try:
        if capability == "embedding":
            emb: Any = await litellm.aembedding(
                input=["ping"], timeout=_PROBE_TIMEOUT_SECONDS, **params
            )
            return {"ok": True, "detail": f"임베딩 수신: {len(emb.data[0]['embedding'])}차원"}
        res: Any = await litellm.acompletion(
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
            timeout=_PROBE_TIMEOUT_SECONDS,
            **params,
        )
        return {"ok": True, "detail": f"응답 수신: {res.model}"}
    except Exception as e:
        detail = str(e)
        if isinstance(e, litellm.NotFoundError):
            # 탐색(`{base}/models`)은 `/v1` 없이도 응답하는 서버가 있어, 경로 누락이
            # 실제 호출에서야 404로 드러난다. 잘림에 지지 않게 힌트를 앞에 둔다.
            detail = f"404 — 모델명과 base_url에 `/v1`이 필요한지 확인하세요: {detail}"
        return {"ok": False, "detail": detail[:500]}


def estimate_prompt_tokens(messages: list[dict[str, str]]) -> int:
    """메시지의 프롬프트 토큰 추정(#357) — LiteLLM 기본 토크나이저(cl100k)로 센다.

    모델별 토크나이저를 고르지 않는다 — 일부 모델은 토크나이저를 내려받으려 해 네트워크를 탄다.
    cl100k는 한국어를 글자당 약 1토큰으로 세어 o200k 계열(약 0.67)보다 크게 잡는다(보수적).
    """
    import litellm

    return int(litellm.token_counter(messages=messages))


async def complete_stream_with_fallback(
    db: AsyncSession,
    model_id: uuid.UUID | None,
    messages: list[dict[str, str]],
    result: StreamResult,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    modality: str = "llm",
    fit_messages: "Callable[[ResolvedModel], list[dict[str, str]]] | None" = None,
    *,
    prefer_model_id: uuid.UUID | None = None,
    reasoning_effort: str | None = None,
    plan_call: "Callable[[ResolvedModel], CallPlan] | None" = None,
) -> AsyncGenerator[str, None]:
    """폴백 체인 + 서킷 브레이커를 적용한 스트리밍.

    후보별로 첫 델타 수신까지를 시도 단위로 보고, 실패 시 다음 후보로 넘어간다
    (스트림 시작 후 실패는 폴백하지 않고 에러로 전파 — 부분 응답 중복 방지).
    재시도·서킷 집계는 실패 종류(`_failure_kind`, #380)를 따른다 — 일시 오류만 재시도한다.
    출력 상한에서 잘린 응답(`finish_reason == "length"`, #422)은 응답이다 — 실패로 세지 않는다.
    result.model / result.fallback_used에 실제 사용 모델이 기록된다.

    fit_messages: 후보마다 보낼 메시지를 정한다(#357, 모델 컨텍스트 맞춤). 없으면 모든 후보에
    `messages`를 보낸다. 서킷이 열린 후보에는 부르지 않는다.

    prefer_model_id: 이 후보부터 체인 순서로 시도한다(#374 — tool 루프가 폴백한 모델을 마무리
    호출에도 먼저 쓴다). `fallback_used`는 여전히 체인의 1차 모델과 견준다.

    reasoning_effort: 사고 양(#429, `REASONING_EFFORTS`). 후보마다 `complete_stream`이 정한다 —
    없으면 그 후보 모델의 기본값(`reasoning_effort` 열)이다. 허용 밖 값은 후보를 부르기 전에
    `GatewayError`다.

    plan_call: 후보마다 보낼 메시지·출력 상한을 정하거나 그 후보를 건너뛴다(#428, `CallPlan`).
    `fit_messages`보다 먼저다. 사고 양 맞춤과 temperature 판정도 그 후보의 상한으로 한다. 건너뛴
    후보는 부르지 않는다 — 시도 기록에 `skip_code`가 남고 원장·서킷에는 넣지 않는다.
    `OUTPUT_FLOOR_UNMET`은 컨텍스트 초과로 센다. 응답한 후보에 보낸 상한은
    `result.max_tokens_used`다.

    후보마다 출력 상한은 그 모델의 최대 출력 토큰(`max_output_tokens`, #428)을 넘지 않는다 —
    `max_tokens`와 계획의 상한 모두 그 값으로 줄인다. 열이 비었으면 지금과 같다.
    """
    from biznotes_core.services.gateway import circuit

    _check_reasoning_effort(reasoning_effort)
    effort_kw = _reasoning_effort_kw(reasoning_effort)
    chain = await resolve_chain(db, model_id, modality)
    errors: list[str] = []
    # 실패한 후보(#359) — 모두 실패하면 예외에 실어 노드 실행 로그로 보낸다
    attempts: list[dict[str, str]] = []
    # 시도한 후보 중 컨텍스트 초과로 실패한 수와 그 밖의 이유로 실패한 수(#358)
    context_failures = 0
    other_failures = 0
    _SENTINEL = object()

    for idx, candidate in enumerate(_preferred_first(chain, prefer_model_id)):
        if await circuit.is_open(candidate.provider.id):
            errors.append(f"{candidate.model.display_name}: 차단됨(circuit open)")
            attempts.append(_attempt(candidate, "circuit_open"))
            continue

        plan = plan_call(candidate) if plan_call is not None else None
        if plan is not None and plan.skip_code is not None:
            # 요청을 보내지 않았다 — 원장·서킷에 넣지 않는다. 출력 하한 미달은 입력 과다와 같은
            # 원인이라 컨텍스트 초과로 센다(#428)
            if plan.skip_code == OUTPUT_FLOOR_UNMET:
                context_failures += 1
            else:
                other_failures += 1
            errors.append(f"{candidate.model.display_name}: 건너뜀({plan.skip_code})")
            attempts.append(_attempt(candidate, plan.skip_code))
            logger.warning(
                "llm candidate skipped by call plan",
                model=candidate.model.model_name,
                position=idx,
                code=plan.skip_code,
                budget=plan.budget,
            )
            continue

        gen: AsyncGenerator[str, None] | None = None
        first: object = _SENTINEL
        last_error: GatewayError | None = None
        kind = ""
        if plan is not None:
            sent, call_max_tokens = plan.messages, plan.max_tokens
        else:
            sent = fit_messages(candidate) if fit_messages is not None else messages
            call_max_tokens = max_tokens
        call_max_tokens = _model_max_tokens(candidate, call_max_tokens)
        for attempt in range(2):  # 일시 오류(429/timeout 등)만 백오프 1회 재시도(#380)
            try:
                gen = await complete_stream(
                    candidate, sent, result, temperature, call_max_tokens, **effort_kw
                )
                first = await anext(gen.__aiter__(), _SENTINEL)
                last_error = None
                break
            except GatewayError as e:
                last_error = e
                kind = _failure_kind(e)
                if kind != "transient":
                    # 같은 요청이면 같은 결과가 다시 온다 — 재시도하지 않는다(#358·#380)
                    break
                if attempt == 0:
                    await asyncio.sleep(0.5)
        if last_error is not None and kind == "context":
            # 요청 문제라 제공자 실패(서킷·오류 지표)로 세지 않는다. 컨텍스트가 더 큰 후보가 받을 수
            # 있으니 다음 후보로 넘어간다(#358)
            context_failures += 1
            errors.append(f"{candidate.model.display_name}: {last_error}")
            attempts.append(_attempt(candidate, GatewayContextLengthError.node_error_code))
            logger.warning(
                "llm candidate context length exceeded, trying next",
                model=candidate.model.model_name,
                position=idx,
            )
            continue
        if last_error is not None or gen is None:
            other_failures += 1
            attempts.append(
                _attempt(candidate, getattr(last_error, "node_error_code", "model_call_failed"))
            )
            # 영구 요청 오류는 그 요청의 문제라 프로바이더 실패로 세지 않는다(#380)
            if kind != "permanent":
                await _count_provider_failure(candidate)
            errors.append(f"{candidate.model.display_name}: {last_error}")
            logger.warning(
                "llm candidate failed, trying next",
                model=candidate.model.model_name,
                position=idx,
                kind=kind,
                error=str(last_error),
            )
            continue

        await circuit.record_success(candidate.provider.id)
        result.model = candidate
        result.fallback_used = candidate is not chain[0]
        result.max_tokens_used = call_max_tokens
        if result.fallback_used:
            from biznotes_core.core.metrics import llm_fallback_total

            llm_fallback_total.labels(modality=modality).inc()

        async def _relay(
            first_delta: object = first,
            stream: AsyncGenerator[str, None] = gen,
        ) -> AsyncGenerator[str, None]:
            if first_delta is not _SENTINEL:
                yield str(first_delta)
                async for delta in stream:
                    yield delta

        return _relay()

    message = "모든 LLM 후보가 실패했습니다: " + " / ".join(errors)
    # 시도한 후보가 모두 컨텍스트 초과면 원인(입력 과다)을 노드 실패 코드로 알린다(#358)
    error_cls = (
        GatewayContextLengthError if context_failures and not other_failures else GatewayCallError
    )
    error = error_cls(message)
    error.model_attempts = attempts
    raise error


def _attempt(candidate: ResolvedModel, error_code: str) -> dict[str, str]:
    """실행 로그에 남길 후보 하나(#359) — 모델 id·표시 이름과 실패 코드만(공급자·원문 제외)."""
    return {
        "id": str(candidate.model.id),
        "display_name": candidate.model.display_name,
        "error_code": error_code,
    }


def _usage_dict(usage: Any) -> dict[str, int]:
    """공급자 사용량 → 게이트웨이 사용량. 사고 토큰(`completion_tokens_details.reasoning_tokens`,
    #422)은 0보다 클 때만 싣는다 — LiteLLM 스트림은 보고하지 않은 값을 0으로 채운다."""
    out = {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None)
    if isinstance(reasoning, int) and reasoning > 0:
        out["reasoning_tokens"] = reasoning
    return out


# 사고 블록을 만든 모델(#429) — 게이트웨이가 assistant 메시지에 다는 내부 표시라 요청 전에 뺀다
_THINKING_MODEL_KEY = "_thinking_model_id"
# LiteLLM 변환을 미리 보지 못해 사고 양을 맞추지 않는다는 경고는 프로세스에서 한 번만 남긴다
_reasoning_preview_unavailable_warned = False


def _model_reasoning_effort(resolved: ResolvedModel) -> str | None:
    """후보 모델의 사고 양 기본값(#429 ③, 관리자 설정). 허용 밖 저장 값은 보내지 않고 경고한다 —
    관리자 API가 막지만 열에 CHECK 제약이 없다(D1)."""
    # 대역 모델(`ResolvedModel`은 공개 표면이다)이 새 열을 모르면 NULL과 같다
    value = getattr(resolved.model, "reasoning_effort", None)
    if value is None or value in REASONING_EFFORTS:
        return value
    logger.warning("llm_model_reasoning_effort_ignored", model=resolved.model.model_name)
    return None


def _model_max_tokens(resolved: ResolvedModel, max_tokens: Any) -> Any:
    """이 후보에 보낼 출력 상한(#428 ③) — 모델 최대 출력 토큰(`max_output_tokens`)을 넘지 않는다.

    열이 비었거나 상한이 정수가 아니면 그대로다(지금과 같다).
    """
    limit = getattr(resolved.model, "max_output_tokens", None)
    if isinstance(limit, int) and limit > 0 and isinstance(max_tokens, int) and max_tokens > limit:
        return limit
    return max_tokens


def _check_reasoning_effort(value: Any) -> None:
    """사고 양이 허용 값(`REASONING_EFFORTS`)이 아니면 호출 전에 거부한다(#429). None은 미설정."""
    if value is not None and value not in REASONING_EFFORTS:
        raise GatewayError(
            f"reasoning_effort는 {'·'.join(REASONING_EFFORTS)} 중 하나여야 합니다: "
            f"{str(value)[:32]!r}"
        )


def _reasoning_effort_kw(value: str | None) -> dict[str, Any]:
    """후보 호출에 넘길 인자 — 값이 없으면 넘기지 않는다(지금 호출과 같다)."""
    return {"reasoning_effort": value} if value is not None else {}


def _preview_params(
    resolved: ResolvedModel,
    effort: str,
    max_tokens: int,
    api_base: str | None,
    temperature: float | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """LiteLLM이 이 호출에 만들 프로바이더 형식 인자(#429) — (모델, 프로바이더, 인자).

    `litellm.completion`이 요청마다 쓰는 변환(`get_optional_params`)을 요청 없이 부른다. 판정 근거를
    LiteLLM에 두려고 쓴다 — 모델 맵·단계별 상수를 옮겨 오지 않는다.
    """
    import litellm
    from litellm.utils import get_optional_params

    model, provider, _, _ = litellm.get_llm_provider(
        model=resolved.model.model_name, api_base=api_base
    )
    params = get_optional_params(
        model=model,
        custom_llm_provider=provider,
        max_tokens=max_tokens,
        temperature=temperature,
        reasoning_effort=effort,
        drop_params=True,
    )
    return model, provider, params


def _thinking_budget(
    resolved: ResolvedModel, effort: str, max_tokens: int, api_base: str | None
) -> int | None:
    """이 사고 양을 보낼 때 LiteLLM이 만드는 사고 예산(토큰)(#429). 예산 형식이 아니면 None.

    Anthropic·Bedrock은 `thinking.budget_tokens`, Gemini 2.5는 `thinkingConfig.thinkingBudget`이다.
    그 밖의 형식(OpenAI `reasoning_effort`, Anthropic adaptive, Gemini 3 `thinkingLevel`)은 예산이
    없다.
    """
    _, _, params = _preview_params(resolved, effort, max_tokens, api_base)
    for key, budget_key in (("thinking", "budget_tokens"), ("thinkingConfig", "thinkingBudget")):
        config = params.get(key)
        budget = config.get(budget_key) if isinstance(config, dict) else None
        if isinstance(budget, int):
            return budget
    return None


def _reasoning_effort_for(
    resolved: ResolvedModel, requested: str | None, max_tokens: Any, api_base: str | None
) -> str | None:
    """이 후보에 보낼 사고 양(#429) — 호출자(노드) 값이 먼저이고, 없으면 그 후보 모델의 기본값
    (`reasoning_effort` 열, ③)이다. 둘 다 없으면 보내지 않는다.

    사고 예산이 출력 상한(`max_tokens`) 이상이면 한 단계씩 낮추고, 가장 낮은 단계도 넘치면
    뺀다(D3 ①). 단계 순서는 `REASONING_EFFORTS`(낮은 것부터)다. Anthropic은 그 요청을 400으로
    거부하고, Gemini는 출력 전체를 사고에 쓸 수 있다. 바꿨으면 경고 로그
    `reasoning_effort_adjusted`를 남긴다. 미지원 모델에서 값을 빼는 것은 LiteLLM이 한다
    (`drop_params`). LiteLLM 변환을 미리 보지 못하면 값을 그대로 보낸다 — 판정은 LiteLLM이 한다.
    """
    global _reasoning_preview_unavailable_warned
    if requested is None:
        requested = _model_reasoning_effort(resolved)
    if requested is None or not isinstance(max_tokens, int):
        return requested
    effort: str | None = requested
    while effort is not None:
        try:
            budget = _thinking_budget(resolved, effort, max_tokens, api_base)
        except Exception as e:  # LiteLLM 내부 구조가 바뀌면 맞추지 않는다
            if not _reasoning_preview_unavailable_warned:
                _reasoning_preview_unavailable_warned = True
                logger.warning("llm_reasoning_preview_unavailable", error=type(e).__name__)
            return requested
        if budget is None or budget < max_tokens:
            break
        step = REASONING_EFFORTS.index(effort)
        effort = REASONING_EFFORTS[step - 1] if step else None
    if effort != requested:
        logger.warning(
            "reasoning_effort_adjusted",
            model=resolved.model.model_name,
            requested=requested,
            effective=effort,
            max_tokens=max_tokens,
            reason="budget_exceeds_max_tokens",
        )
    return effort


def _is_anthropic_thinking(model: str, provider: str, params: dict[str, Any]) -> bool:
    """LiteLLM 변환 결과가 Anthropic 확장 사고를 켜는가 — budget·adaptive 모두(#429).

    Anthropic 형식은 `thinking: {type: enabled, budget_tokens}`나 `{type: adaptive}`다. 같은 키를
    다른 형식으로 쓰는 프로바이더가 있어(DeepSeek `{type: enabled}`, Tencent) Claude 경로로 좁힌다 —
    LiteLLM 프로바이더 `anthropic`, 또는 Bedrock(converse·invoke)·Vertex의 Claude 모델이다.
    """
    thinking = params.get("thinking")
    if not isinstance(thinking, dict):
        return False
    kind = thinking.get("type")
    anthropic_shape = kind == "adaptive" or (
        kind == "enabled" and isinstance(thinking.get("budget_tokens"), int)
    )
    return anthropic_shape and (provider == "anthropic" or "claude" in model.lower())


def _anthropic_thinking_params(
    resolved: ResolvedModel,
    effort: str | None,
    temperature: float,
    max_tokens: Any,
    api_base: str | None,
) -> dict[str, Any] | None:
    """이 호출에서 Anthropic 사고가 켜지면 그 LiteLLM 변환 결과, 아니면 None(#429).

    사고 양이 없거나, Anthropic 사고가 아니거나, 미리 보지 못하면 None이다(지금 동작). 미리 보기
    실패 경고는 `_reasoning_effort_for`가 한 번 남긴다.
    """
    if effort is None or not isinstance(max_tokens, int):
        return None
    try:
        model, provider, params = _preview_params(
            resolved, effort, max_tokens, api_base, temperature
        )
    except Exception:  # LiteLLM 내부 구조가 바뀌면 맞추지 않는다
        return None
    return params if _is_anthropic_thinking(model, provider, params) else None


def _has_tool_turn_without_thinking(messages: list[Any]) -> bool:
    """도구 호출을 낸 assistant 턴 중 사고 블록이 없는 턴이 있는가(#429)."""
    return any(
        isinstance(m, dict)
        and m.get("role") == "assistant"
        and m.get("tool_calls")
        and not m.get("thinking_blocks")
        for m in messages
    )


def _reasoning_request(
    resolved: ResolvedModel,
    requested: str | None,
    messages: list[Any],
    temperature: float,
    max_tokens: Any,
    api_base: str | None,
) -> tuple[dict[str, Any], list[Any]]:
    """이 후보 호출의 사고 관련 인자와 보낼 메시지(#429) — `complete_stream`·`_tool_turn`이 쓴다.

    1. 사고 양은 `_reasoning_effort_for`가 정한다(D3 ①).
    2. Anthropic 사고(budget·adaptive)가 켜질 호출인데, 보낼 대화에 이 모델의 사고 블록이 없는
       도구 호출 턴이 있으면 사고를 켜지 않는다. 사고가 켜진 요청에서 도구 호출 턴이 사고 블록으로
       시작하지 않으면 Anthropic이 400으로 거부한다(LiteLLM
       `utils.last_assistant_with_tool_calls_has_no_thinking_blocks` 주석). 필수 초기 도구가 넣은
       턴과 다른 모델의 블록을 뺀 턴이 그렇다. 이때 이 모델의 블록도 뺀다 — 사고가 꺼진 요청에
       사고 블록이 있어도 거부한다(`utils.any_assistant_message_has_thinking_blocks` 주석). 경고
       `reasoning_effort_adjusted`(reason=`tool_turn_without_thinking`)를 남긴다.
    3. Anthropic 사고가 켜지면 temperature를 뺀다. Anthropic은 사고 중 1이 아닌 temperature를
       거부하고 1로 고정한다(LiteLLM `llms/anthropic/pass_through/messages/transformation.py`
       `_drop_incompatible_temperature_for_thinking`). LiteLLM chat 경로는 이 처리를 하지 않는다.
       LiteLLM이 이미 빼는 모델(변환 결과에 temperature가 없다)은 지금과 같다. info 로그
       `llm_temperature_dropped_for_thinking`.

    반환: (LiteLLM에 넘길 `reasoning_effort`·`temperature` 중 보낼 것, 보낼 메시지). 사고 양이 없는
    호출은 `{"temperature": t}`와 `_messages_for` 결과다(지금과 같다).
    """
    effort = _reasoning_effort_for(resolved, requested, max_tokens, api_base)
    sampling: dict[str, Any] = {"temperature": temperature}
    sent = _messages_for(resolved, messages)
    params = _anthropic_thinking_params(resolved, effort, temperature, max_tokens, api_base)
    if params is None:
        return {**_reasoning_effort_kw(effort), **sampling}, sent
    if _has_tool_turn_without_thinking(sent):
        logger.warning(
            "reasoning_effort_adjusted",
            model=resolved.model.model_name,
            requested=effort,
            effective=None,
            max_tokens=max_tokens,
            reason="tool_turn_without_thinking",
        )
        return sampling, _messages_for(resolved, messages, keep_own_thinking=False)
    if "temperature" in params and temperature != 1:
        logger.info(
            "llm_temperature_dropped_for_thinking",
            model=resolved.model.model_name,
            temperature=temperature,
            reasoning_effort=effort,
        )
        sampling = {}
    return {"reasoning_effort": effort, **sampling}, sent


def _messages_for(
    resolved: ResolvedModel, messages: list[Any], *, keep_own_thinking: bool = True
) -> list[Any]:
    """이 후보에 보낼 메시지(#429) — 사고 블록은 그 블록을 만든 모델에만 다시 보낸다.

    서명은 블록을 만든 모델만 검증하고(Anthropic·Gemini), OpenAI 호환 경로는 메시지의 모르는 키를
    본문에 그대로 싣는다. 게이트웨이 표시(`_THINKING_MODEL_KEY`)는 모든 후보에서 뺀다.
    `keep_own_thinking=False`면 이 모델의 블록도 뺀다(사고를 끈 호출). 표시가 없는 메시지(호출자가
    만든 것)는 그대로 둔다. 받은 목록과 메시지는 바꾸지 않는다.
    """
    if not any(isinstance(m, dict) and _THINKING_MODEL_KEY in m for m in messages):
        return messages
    own = str(resolved.model.id)
    sent: list[Any] = []
    for message in messages:
        if isinstance(message, dict) and _THINKING_MODEL_KEY in message:
            message = dict(message)
            if message.pop(_THINKING_MODEL_KEY) != own or not keep_own_thinking:
                message.pop("thinking_blocks", None)
        sent.append(message)
    return sent


def _thinking_blocks(message: Any) -> list[dict[str, Any]]:
    """LiteLLM 응답 메시지의 사고 블록(#429) — JSON으로 저장할 수 있는 사본. 없으면 []."""
    blocks = getattr(message, "thinking_blocks", None)
    if not isinstance(blocks, list):
        return []
    copied = [dict(block) for block in blocks if isinstance(block, dict)]
    try:
        json.dumps(copied)  # HITL checkpoint(JSONB)에 그대로 들어간다
    except (TypeError, ValueError):
        return []
    return copied


def _mock_limit(max_tokens: Any) -> int | None:
    """mock 출력의 글자 상한 — 토큰 추정(글자 ÷ 4)과 같은 비율이다(#422). 상한이 없으면 None."""
    return max_tokens * 4 if isinstance(max_tokens, int) and max_tokens > 0 else None


def _mock_stream(messages: list[dict[str, str]]) -> AsyncGenerator[str, None]:
    """컨텍스트 추출 기반 결정적 응답 — 외부 키 없이 파이프라인 검증용.

    시스템 프롬프트에 포함된 [n] 컨텍스트 블록에서 핵심 문장을 뽑아
    인용 마커와 함께 요약 형태로 반환한다.
    """
    question = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    # 컨텍스트 [n] 블록은 system(채팅) 또는 user(에이전트 프롬프트) 어디든 올 수 있다.
    # 발췌는 비신뢰 데이터 블록 안에 있으므로(HWP-05) 닫는 태그에서도 끊는다.
    all_text = "\n".join(m["content"] for m in messages)
    blocks = re.findall(
        r"^\[(\d+)\]\s*(.+?)(?=^\[\d+\]|^</untrusted_data>|\Z)", all_text, re.M | re.S
    )

    async def gen() -> AsyncGenerator[str, None]:
        if not blocks:
            yield "선택된 소스에서 관련 내용을 찾지 못했습니다. 소스를 추가해 보세요."
            return
        yield f'"{question.strip()}"에 대해 소스에서 찾은 내용입니다.\n\n'
        for n, text in blocks[:4]:
            snippet = " ".join(text.split())[:180]
            yield f"- {snippet} [{n}]\n"
        notice = (
            "(참고: mock 모델 응답입니다 — 관리자가 실제 LLM을 등록하면 실제 답변이 생성됩니다.)"
        )
        yield f"\n{notice}"

    return gen()


async def complete_stream(
    resolved: ResolvedModel,
    messages: list[dict[str, str]],
    result: StreamResult,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    *,
    reasoning_effort: str | None = None,
) -> AsyncGenerator[str, None]:
    """텍스트 델타를 yield. 종료 후 result.usage·result.finish_reason이 채워짐. 요청을 보내면
    result.max_tokens_used에 보낸 출력 상한이 남는다(#428 — 폴백 경로의 값과 같다).

    temperature를 받지 않는 모델에는 temperature를 보내지 않는다(#224).
    mock 프로바이더도 출력 상한을 지킨다 — 넘치면 자르고 `length`를 싣는다(#422).
    reasoning_effort(#429)는 `_reasoning_request`로 이 모델과 대화에 맞춰 보낸다(사고 양 맞춤,
    사고 블록 없는 도구 턴, Anthropic 사고의 temperature). 없으면 모델 기본값이고, 그것도 없으면
    보내지 않는다. mock은 무시한다. 사고 블록은 그 블록을 만든 모델에만 보낸다(`_messages_for`).
    `max_tokens`는 모델 최대 출력 토큰(`max_output_tokens`, #428)을 넘으면 그 값으로 줄인다.
    """
    _check_reasoning_effort(reasoning_effort)
    max_tokens = _model_max_tokens(resolved, max_tokens)
    if resolved.provider.type == "mock":
        total_in = sum(len(m["content"]) for m in messages) // 4
        limit = _mock_limit(max_tokens)

        async def mock_gen() -> AsyncGenerator[str, None]:
            result.max_tokens_used = max_tokens
            out_chars = 0
            finish = "stop"
            async for delta in _mock_stream(messages):
                if limit is not None and out_chars + len(delta) > limit:
                    delta = delta[: limit - out_chars]
                    finish = "length"
                if delta:
                    out_chars += len(delta)
                    yield delta
                if finish == "length":
                    break
            result.usage = {
                "prompt_tokens": total_in,
                "completion_tokens": out_chars // 4,
                "total_tokens": total_in + out_chars // 4,
            }
            result.finish_reason = finish

        return mock_gen()

    import litellm

    params = resolved.litellm_params
    reasoning, sent = _reasoning_request(
        resolved, reasoning_effort, messages, temperature, max_tokens, params.get("api_base")
    )

    async def llm_gen() -> AsyncGenerator[str, None]:
        try:
            response = await litellm.acompletion(
                messages=sent,
                **reasoning,
                max_tokens=max_tokens,
                stream=True,
                stream_options={"include_usage": True},
                # 프로바이더·모델이 받지 않는 파라미터는 litellm이 뺀다. temperature는
                # 모델 맵으로 모델마다 판별한다(예: claude-sonnet-5-5·o3). 빼지 않으면
                # 요청 전 UnsupportedParamsError나 서버 400으로 실패한다(#224). 사고 양도
                # 같다 — 받지 않는 모델에서는 빠진다(#429)
                drop_params=True,
                **params,
            )
            # 모델 최대 출력 토큰으로 줄였으면 그 값이다 — 직접 호출도 보낸 상한을 안다
            result.max_tokens_used = max_tokens
            async for chunk in response:
                usage = getattr(chunk, "usage", None)
                if usage:
                    result.usage = _usage_dict(usage)
                # None이 아닌 마지막 값을 쓴다(#422) — 맨 끝 사용량 청크의 종료 사유는 None이다
                finish = chunk.choices[0].finish_reason if chunk.choices else None
                if isinstance(finish, str) and finish:
                    result.finish_reason = finish
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as e:
            logger.error("llm stream failed", model=resolved.model.model_name, error=str(e))
            if is_context_length_error(e):
                raise GatewayContextLengthError(f"LLM 호출 실패(컨텍스트 초과): {e}") from e
            raise GatewayCallError(f"LLM 호출 실패: {e}") from e

    return llm_gen()


def _mock_tool_turn(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_tokens: int = 2048
) -> ToolTurnResult:
    """외부 키 없이 tool-calling 루프를 검증하는 결정적 mock.

    아직 도구를 호출한 적이 없으면(대화에 role==tool 메시지 없음) 첫 도구를 1회 호출하고,
    도구 결과가 들어온 뒤에는 그 결과를 요약한 최종 텍스트를 반환한다. 최종 텍스트는 출력 상한을
    지킨다 — 넘치면 자르고 `length`를 싣는다(#422, 스트림 mock과 같은 비율).
    """
    has_tool_result = any(m.get("role") == "tool" for m in messages)
    in_tokens = sum(len(str(m.get("content") or "")) for m in messages) // 4

    if not has_tool_result and tools:
        first = tools[0]["function"]["name"]
        question = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
        )
        call = ToolCall(id="mock-call-1", name=first, arguments={"query": str(question)[:120]})
        assistant = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": first, "arguments": json.dumps(call.arguments)},
                }
            ],
        }
        return ToolTurnResult(
            content="",
            tool_calls=[call],
            assistant_message=assistant,
            usage={
                "prompt_tokens": in_tokens,
                "completion_tokens": 8,
                "total_tokens": in_tokens + 8,
            },
            finish_reason="tool_calls",
        )

    # 도구 결과의 비신뢰 블록 태그는 걷어낸다(HWP-05) — mock 답변에 잘린 태그가 남지 않게
    tool_outputs = [
        re.sub(r"</?untrusted_data[^>]*>\n?", "", str(m.get("content") or ""))
        for m in messages
        if m.get("role") == "tool"
    ]
    summary = "도구 결과를 종합한 답변입니다.\n" + "\n".join(
        f"- {str(t)[:160]}" for t in tool_outputs
    )
    limit = _mock_limit(max_tokens)
    finish = "stop"
    if limit is not None and len(summary) > limit:
        summary, finish = summary[:limit], "length"
    out_tokens = len(summary) // 4
    return ToolTurnResult(
        content=summary,
        tool_calls=[],
        assistant_message={"role": "assistant", "content": summary},
        usage={
            "prompt_tokens": in_tokens,
            "completion_tokens": out_tokens,
            "total_tokens": in_tokens + out_tokens,
        },
        finish_reason=finish,
    )


# Vertex(Gemini) 스키마 변환에서 도구별 판정 캐시(#314) — 정규화 JSON의 SHA-256 → 통과 여부
_VERTEX_TOOL_VERDICTS: "OrderedDict[str, bool]" = OrderedDict()
_VERTEX_TOOL_VERDICT_LIMIT = 1024
# litellm Vertex 변환을 불러오지 못해 거르지 않는다는 경고는 프로세스에서 한 번만 남긴다
_vertex_probe_unavailable_warned = False


def _uses_vertex_schema(model_name: str) -> bool:
    """도구 스키마가 LiteLLM의 Vertex(Gemini) 변환을 타는 모델인지(#314).

    LiteLLM `get_optional_params`의 분기와 같은 조건이다 — `gemini`·`vertex_ai_beta`, 그리고
    `vertex_ai` 중 모델 이름에 `gemini`가 있거나 LiteLLM의 Vertex 모델 목록에 있는 모델. 그 밖의
    `vertex_ai` 경로(Model Garden의 xai·gemma·숫자 엔드포인트, 파트너 모델)는 각자의 형식을 써서
    이 변환을 타지 않는다. 제공자를 판정하지 못하면 아니라고 본다 — 호출의 성패는 LiteLLM이 정한다.
    """
    import litellm

    try:
        model, provider, _, _ = litellm.get_llm_provider(model=model_name)
    except Exception:
        return False
    if provider in ("gemini", "vertex_ai_beta"):
        return True
    if provider != "vertex_ai":
        return False
    if "gemini" in model:
        return True
    lists = (
        "vertex_chat_models",
        "vertex_code_chat_models",
        "vertex_text_models",
        "vertex_code_text_models",
        "vertex_language_models",
        "vertex_vision_models",
    )
    return any(model in getattr(litellm, name, ()) for name in lists)


def _vertex_accepts(tool: dict[str, Any], probe: Any) -> bool:
    """도구 하나를 실제 LiteLLM Vertex 변환에 넣어 예외 없이 지나는지 본다. 결과는 캐시한다.

    LiteLLM 변환은 받은 dict를 제자리에서 바꾸므로 깊은 사본으로 시험한다. 판정 기준은 이식하지
    않고 LiteLLM 자신이다 — LiteLLM이 형태를 고치거나 바꾸면 판정도 따라간다.
    """
    key = hashlib.sha256(
        json.dumps(tool, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()
    verdict = _VERTEX_TOOL_VERDICTS.get(key)
    if verdict is None:
        try:
            probe([copy.deepcopy(tool)], {})
            verdict = True
        except Exception:
            verdict = False
        _VERTEX_TOOL_VERDICTS[key] = verdict
        if len(_VERTEX_TOOL_VERDICTS) > _VERTEX_TOOL_VERDICT_LIMIT:
            _VERTEX_TOOL_VERDICTS.popitem(last=False)
    else:
        _VERTEX_TOOL_VERDICTS.move_to_end(key)
    return verdict


def _vertex_compatible_tools(
    model_name: str, tools: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Vertex(Gemini) 경로면 LiteLLM 변환이 예외를 내는 도구를 뺀다(#314). (남은 도구, 뺀 이름).

    MCP 서버가 준 스키마 중 Vertex 후처리에서만 예외가 나는 형태(튜플 `items`·빈 `anyOf`·불리언
    서브스키마 등)가 있다. 스펙 정화(`mcp_tool_spec`)는 모델을 정하기 전이라 제공자를 가리지
    않으므로, 모델이 정해진 여기서 그 도구만 뺀다. 그대로 두면 요청 전체가 실패해 그 모델의
    모든 턴이 막힌다. 다른 제공자는 입력을 그대로 돌려준다.
    """
    global _vertex_probe_unavailable_warned
    if not tools or not _uses_vertex_schema(model_name):
        return tools, []
    try:
        from litellm.llms.vertex_ai.gemini.vertex_and_google_ai_studio_gemini import (
            VertexGeminiConfig,
        )

        probe = VertexGeminiConfig()._map_function
    except Exception as e:  # LiteLLM 내부 구조가 바뀌면 거르지 않는다(지금 동작)
        if not _vertex_probe_unavailable_warned:
            _vertex_probe_unavailable_warned = True
            logger.warning("llm_vertex_tool_probe_unavailable", error=type(e).__name__)
        return tools, []
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for tool in tools:
        if _vertex_accepts(tool, probe):
            kept.append(tool)
        else:
            function = tool.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            dropped.append(str(name) if name is not None else "?")
    return kept, dropped


async def _tool_turn(
    resolved: ResolvedModel,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    temperature: float,
    max_tokens: int,
    *,
    reasoning_effort: str | None = None,
) -> ToolTurnResult:
    """후보 하나에 tool-calling 1턴을 보낸다. 실패는 게이트웨이 예외(컨텍스트 초과는
    `GatewayContextLengthError`)다. 사고 양·temperature·사고 블록과 모델 최대 출력 토큰은
    `complete_stream`과 같다(#429·#428). 보낸 상한은 `max_tokens_used`다."""
    max_tokens = _model_max_tokens(resolved, max_tokens)
    if resolved.provider.type == "mock":
        res = _mock_tool_turn(messages, tools, max_tokens)
        res.model = resolved
        res.max_tokens_used = max_tokens
        return res

    import litellm

    params = resolved.litellm_params
    reasoning, messages = _reasoning_request(
        resolved, reasoning_effort, messages, temperature, max_tokens, params.get("api_base")
    )
    tools, dropped = _vertex_compatible_tools(resolved.model.model_name, tools)
    if dropped:
        # 스키마 원문·예외 메시지는 남기지 않는다(외부 MCP 서버 입력)
        logger.warning(
            "llm_tools_dropped_for_provider", model=resolved.model.model_name, tools=dropped
        )
    try:
        response = await litellm.acompletion(
            messages=messages,
            tools=tools,
            tool_choice="auto",
            **reasoning,
            max_tokens=max_tokens,
            drop_params=True,  # complete_stream과 같은 이유(#224)
            # tools는 빼지 않는다. 도구를 못 쓰는 모델(예: Bedrock llama3-8b)에서도 도구
            # 정의를 실어, 도구 없이 조용히 답하는 대신 실패 여부를 프로바이더가 정하게 한다.
            # tool_choice는 빠져도 기본값이 auto다. 사고 양은 넣지 않는다 — 넣으면 받지 않는
            # 모델에도 강제로 실린다(#429)
            allowed_openai_params=["tools"],
            **params,
        )
    except Exception as e:
        logger.error("llm tool completion failed", model=resolved.model.model_name, error=str(e))
        error: GatewayCallError = (
            GatewayContextLengthError(f"LLM tool 호출 실패(컨텍스트 초과): {e}")
            if is_context_length_error(e)
            else GatewayCallError(f"LLM tool 호출 실패: {e}")
        )
        raise error from e

    choice = response.choices[0]
    msg = choice.message
    usage = getattr(response, "usage", None)
    usage_dict = _usage_dict(usage) if usage else {}
    finish = getattr(choice, "finish_reason", None)

    tool_calls: list[ToolCall] = []
    raw_tool_calls = getattr(msg, "tool_calls", None) or []
    for tc in raw_tool_calls:
        try:
            args = json.loads(tc.function.arguments or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

    # 다음 턴에 그대로 다시 넣을 assistant 메시지 (litellm 직렬화 형태)
    assistant_message: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
    if raw_tool_calls:
        assistant_message["content"] = msg.content
        assistant_message["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in raw_tool_calls
        ]
    blocks = _thinking_blocks(msg)
    if blocks:
        # 사고가 켜진 tool 2턴째에 직전 사고 블록이 없으면 Anthropic이 거부한다(#429). 블록을 만든
        # 모델을 달아 둔다 — 다른 후보에는 보내지 않는다(`_messages_for`)
        assistant_message["thinking_blocks"] = blocks
        assistant_message[_THINKING_MODEL_KEY] = str(resolved.model.id)

    return ToolTurnResult(
        content=msg.content or "",
        tool_calls=tool_calls,
        assistant_message=assistant_message,
        usage=usage_dict,
        model=resolved,
        finish_reason=finish if isinstance(finish, str) and finish else None,
        max_tokens_used=max_tokens,
    )


async def complete_with_tools(
    db: AsyncSession,
    model_id: uuid.UUID | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    temperature: float = 0.2,
    max_tokens: int = 2048,
    *,
    prefer_model_id: uuid.UUID | None = None,
    reasoning_effort: str | None = None,
) -> ToolTurnResult:
    """tool-calling 1턴 (비스트리밍) — 최종 텍스트 또는 tool_calls 반환.

    litellm의 네이티브 function-calling을 사용한다. mock 프로바이더는 결정적 mock 턴.
    런타임이 이 함수를 루프로 호출해 도구 실행 → 결과 주입 → 재호출을 반복한다.
    temperature를 받지 않는 모델에는 temperature를 보내지 않는다(#224).

    폴백 체인·재시도·서킷은 스트리밍 경로(`complete_stream_with_fallback`)와 같다(#374·#380,
    `_failure_kind`): 일시 오류(429·5xx·타임아웃)는 백오프 1회 재시도하고 서킷에 센다. 401·403은
    재시도하지 않고 서킷에 센다. 영구 요청 오류(400·404·422 등)와 컨텍스트 초과(#358)는 같은 후보에
    다시 보내지 않고 서킷에 세지 않는다. 어느 경우든 다음 후보로 넘어간다. 모두 실패하면 시도한
    후보를 `model_attempts`에 싣는다. 출력 상한에서 잘린 응답(#422)은 실패가 아니다(스트리밍과
    같다). `prefer_model_id`는 먼저 시도할 후보다 — tool 루프가 앞 턴에
    폴백한 모델을 다음 턴에도 먼저 쓴다. `fallback_used`는 체인의 1차 모델과 견준다.
    `reasoning_effort`(#429)는 스트리밍 경로와 같다 — 허용 밖 값은 후보를 부르기 전에 거부한다.
    모델 기본값(`reasoning_effort`)과 최대 출력 토큰(`max_output_tokens`, #428)도 같다.
    """
    from biznotes_core.services.gateway import circuit

    _check_reasoning_effort(reasoning_effort)
    effort_kw = _reasoning_effort_kw(reasoning_effort)
    chain = await resolve_chain(db, model_id, "llm")
    errors: list[str] = []
    attempts: list[dict[str, str]] = []
    context_failures = 0
    other_failures = 0

    for idx, candidate in enumerate(_preferred_first(chain, prefer_model_id)):
        if await circuit.is_open(candidate.provider.id):
            errors.append(f"{candidate.model.display_name}: 차단됨(circuit open)")
            attempts.append(_attempt(candidate, "circuit_open"))
            continue

        turn: ToolTurnResult | None = None
        last_error: GatewayError | None = None
        kind = ""
        for attempt in range(2):  # 일시 오류(429/timeout 등)만 백오프 1회 재시도(#380)
            try:
                turn = await _tool_turn(
                    candidate, messages, tools, temperature, max_tokens, **effort_kw
                )
                last_error = None
                break
            except GatewayError as e:
                last_error = e
                kind = _failure_kind(e)
                if kind != "transient":
                    # 같은 요청이면 같은 결과가 다시 온다 — 재시도하지 않는다(#358·#380)
                    break
                if attempt == 0:
                    await asyncio.sleep(0.5)
        if last_error is not None and kind == "context":
            # 요청 문제라 제공자 실패로 세지 않는다. 컨텍스트가 더 큰 후보가 받을 수 있다(#358)
            context_failures += 1
            errors.append(f"{candidate.model.display_name}: {last_error}")
            attempts.append(_attempt(candidate, GatewayContextLengthError.node_error_code))
            logger.warning(
                "llm tool candidate context length exceeded, trying next",
                model=candidate.model.model_name,
                position=idx,
            )
            continue
        if turn is None:
            other_failures += 1
            attempts.append(
                _attempt(candidate, getattr(last_error, "node_error_code", "model_call_failed"))
            )
            # 영구 요청 오류(도구를 받지 않는 모델의 400 등)는 프로바이더 실패로 세지 않는다(#380)
            if kind != "permanent":
                await _count_provider_failure(candidate)
            errors.append(f"{candidate.model.display_name}: {last_error}")
            logger.warning(
                "llm tool candidate failed, trying next",
                model=candidate.model.model_name,
                position=idx,
                kind=kind,
                error=str(last_error),
            )
            continue

        await circuit.record_success(candidate.provider.id)
        turn.fallback_used = candidate is not chain[0]
        if turn.fallback_used:
            from biznotes_core.core.metrics import llm_fallback_total

            llm_fallback_total.labels(modality="llm").inc()
        return turn

    message = "모든 LLM 후보가 실패했습니다: " + " / ".join(errors)
    # 시도한 후보가 모두 컨텍스트 초과면 원인(입력 과다)을 노드 실패 코드로 알린다(#358)
    error_cls = (
        GatewayContextLengthError if context_failures and not other_failures else GatewayCallError
    )
    error = error_cls(message)
    error.model_attempts = attempts
    raise error
