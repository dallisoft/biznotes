"""에이전트용 소스 요약(digest) 캐시와 생성(#360).

소스 가이드(`source_guide.py`, 앞 8청크 × 1,000자)와 달리 문서 **전체**를 덮는다 — 청크를
`source_digest_group_tokens` 묶음으로 나눠 묶음마다 메모를 만들고(map), 메모를 합쳐 요약 하나로
줄인다(reduce). 메모가 한 번에 들어가지 않으면 묶어서 다시 줄인다. 모델은 호출자가 고른 1차 모델
(없으면 기본 llm 모델) + 폴백 체인이다(결정 D3, #396). 원문·메모는 비신뢰 데이터 블록으로 넣는다
(HWP-05).

캐시(`SourceDigest`)의 지문은 요약을 만든 청크 텍스트(`seq` 순, `\\n`으로 이음)의 sha256이다(결정
D2) — 노드가 범위 전체의 지문을 PG 쿼리 하나로 구하고, 지문과 `PROMPT_VERSION`이 같은 행만 쓴다.
캐시는 모델 간에 공유한다(#396) — 다른 모델이 만든 행도 그대로 쓴다. 저장은 별도 세션의 upsert이고
실패해도 그 run은 만든 요약을 그대로 쓴다.

소스가 많아 소스마다 담을 몫이 작으면 노드가 소스 요약을 `(created_at, id)` 순 배치로 묶어 「배치
요약」을 만든다(계층 요약, #448). 배치 요약은 소스마다 항목을 하나 이상 두고(모델이 빠뜨린 제목은
덧붙인다) Redis에 캐시한다 — 키는 (항목 id·제목·본문 지문, 1차 모델, 목표 토큰, 배치 프롬프트
버전)이다. 캐시는 휘발성이다(TTL·축출되면 다시 만든다). Redis 오류는 캐시 미스로 다룬다.
"""

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.config import get_settings
from biznotes_core.models import Source, SourceChunk, SourceDigest
from biznotes_core.services.gateway.cost import compute_cost
from biznotes_core.services.gateway.llm import (
    GatewayCallError,
    ResolvedModel,
    StreamResult,
    complete_stream_with_fallback,
    resolve_model,
)
from biznotes_core.services.untrusted import UNTRUSTED_DATA_RULE, clean_label, wrap_untrusted

logger = structlog.get_logger()

#: 요약 프롬프트 버전 — 바꾸면 캐시가 모두 다시 만들어진다
PROMPT_VERSION = "1"
# 지문을 만들 때 청크 사이에 두는 구분 — PG 쿼리와 `content_hash_of`가 같아야 한다
_CHUNK_SEP = "\n"
# 생성에 실패했을 때 넣는 앞부분 발췌 길이(글자)
EXCERPT_CHARS = 2000
# 메모·요약 출력 상한 = 목표 × 이 배수 + 여유
_OUTPUT_FACTOR = 1.5
_OUTPUT_SLACK = 64

_COMMON_RULES = (
    "반드시 담을 것: 핵심 주장과 결론, 수치·날짜·고유명사(원문 표기 그대로), 문서의 절 구성"
    "(어느 부분에서 무엇을 다루는지). 원문에 없는 내용을 덧붙이지 않는다. 원문과 같은 언어로 쓴다. "
    "머리말·맺음말 없이 본문만 출력한다."
)
# 지시에는 코어가 정한 값(목표 길이·부분 번호)만 넣는다. 제목은 외부가 정한 문자열이라 비신뢰
# 데이터 블록 안(`문서 제목:` 줄)에 둔다(독립 리뷰 High)
_WHOLE_PROMPT = (
    "아래 데이터 블록의 문서 전체를 {target}토큰 안팎의 요약으로 정리하라. " + _COMMON_RULES
)
_MAP_PROMPT = (
    "아래 데이터 블록은 한 문서의 {part}/{parts} 부분이다. 이 부분을 {target}토큰 안팎의 메모로 "
    "정리하라. " + _COMMON_RULES
)
_REDUCE_PROMPT = (
    "아래 데이터 블록은 한 문서를 앞에서부터 부분별로 정리한 메모들이다. 이를 합쳐 문서 전체를 "
    "덮는 {target}토큰 안팎의 요약으로 만들어라. 앞뒤 부분의 내용이 고르게 남아야 한다. "
    + _COMMON_RULES
)
# 묶음이 모델 컨텍스트에 들어가도록 남기는 몫(지시·제목·채팅 템플릿)과 추정 오차 여유
_GROUP_OVERHEAD_TOKENS = 512
_GROUP_SAFETY = 0.9

#: 배치 요약 프롬프트 버전(#448) — 바꾸면 배치 캐시가 모두 다시 만들어진다
BATCH_PROMPT_VERSION = "1"
#: 배치 하나에 묶는 최대 항목 수
BATCH_MAX_ITEMS = 20
# 배치 캐시 Redis 키 — 값은 JSON `{text, model_id, model_name}`
_BATCH_CACHE_KEY = "source_digest:batch:{key}"
# 배치 캐시 Redis 연결·명령 시간 상한(초) — 느린 Redis가 run을 붙잡지 않게 한다
_BATCH_CACHE_TIMEOUT = 2.0
# 지시에는 코어가 정한 값(항목 수·목표 길이)만 넣는다. 제목은 데이터 블록 안(`문서 제목:` 줄)에 둔다
_BATCH_PROMPT = (
    "아래 데이터 블록은 문서 {count}개의 요약이다. 문서마다 `문서 제목:` 줄로 시작하고 "
    "`---`로 나뉜다. 문서마다 빠짐없이 `- 제목: 핵심` 꼴의 항목을 하나 이상 써서 전체를 "
    "{target}토큰 안팎(문서당 약 {per}토큰)으로 정리하라. 제목은 `문서 제목:` 줄의 표기를 "
    "그대로 쓰고 문서 순서를 지킨다. 결론과 수치·날짜·고유명사(원문 표기 그대로)를 먼저 남긴다. "
    "원문에 없는 내용을 덧붙이지 않는다. 원문과 같은 언어로 쓴다. 머리말·맺음말 없이 본문만 "
    "출력한다."
)


class DigestError(GatewayCallError):
    """요약 응답이 비었다 — 호출 실패와 같게 다룬다(노드는 대체 본문을 넣는다)."""


@dataclass
class DigestResult:
    """요약 하나와 그것을 만드는 데 든 사용량(모든 호출 합)."""

    text: str
    token_count: int
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    model: ResolvedModel | None = None
    calls: int = 0


def count_tokens(text: str) -> int:
    """cl100k 토큰 수 — 게이트웨이 추정(`estimate_prompt_tokens`)과 같은 토크나이저다."""
    import litellm

    return int(litellm.token_counter(text=text))


def content_hash_of(texts: Sequence[str]) -> str:
    """청크 텍스트들의 지문 — `content_hashes`의 PG 계산과 같다."""
    return hashlib.sha256(_CHUNK_SEP.join(texts).encode("utf-8")).hexdigest()


def _fingerprint() -> Any:
    """청크 텍스트(`seq` 순으로 이음)의 sha256 — `content_hash_of`와 같은 값을 PG에서 낸다."""
    joined = func.string_agg(
        SourceChunk.text, aggregate_order_by(literal(_CHUNK_SEP), SourceChunk.seq)
    )
    return func.encode(func.sha256(func.convert_to(joined, "UTF8")), "hex")


async def content_hashes(
    db: AsyncSession, source_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """소스마다 청크 텍스트 지문(PG에서 계산). 청크가 없는 소스는 빠진다."""
    if not source_ids:
        return {}
    rows = await db.execute(
        select(SourceChunk.source_id, _fingerprint())
        .where(SourceChunk.source_id.in_(list(source_ids)))
        .group_by(SourceChunk.source_id)
    )
    return {source_id: str(value) for source_id, value in rows.all()}


async def load_cached(
    db: AsyncSession, hashes: dict[uuid.UUID, str]
) -> dict[uuid.UUID, SourceDigest]:
    """지문과 프롬프트 버전이 지금과 같은 캐시 행만."""
    if not hashes:
        return {}
    rows = await db.scalars(select(SourceDigest).where(SourceDigest.source_id.in_(list(hashes))))
    return {
        row.source_id: row
        for row in rows
        if row.content_hash == hashes[row.source_id] and row.prompt_version == PROMPT_VERSION
    }


async def chunk_texts(db: AsyncSession, source_id: uuid.UUID) -> list[str]:
    rows = await db.execute(
        select(SourceChunk.text).where(SourceChunk.source_id == source_id).order_by(SourceChunk.seq)
    )
    return [text for (text,) in rows.all()]


def fallback_body(source: Source, texts: Sequence[str]) -> tuple[str, str]:
    """요약을 만들지 못한 소스의 대체 본문과 그 종류 — 소스 가이드 요약, 앞부분 발췌, 없음."""
    if source.summary:
        return source.summary, "source_guide"
    if texts:
        return "\n\n".join(texts)[:EXCERPT_CHARS], "excerpt"
    return "", "none"


def _batches(items: Sequence[str], limit_tokens: int) -> list[list[str]]:
    """앞에서부터 `limit_tokens` 안으로 묶는다. 묶음마다 최소 2개 — 합치기가 반드시 줄어든다."""
    batches: list[list[str]] = []
    current: list[str] = []
    used = 0
    for item in items:
        size = count_tokens(item)
        if current and used + size > limit_tokens and len(current) >= 2:
            batches.append(current)
            current, used = [], 0
        current.append(item)
        used += size
    if current:
        if len(current) == 1 and batches:
            batches[-1].append(current[0])
        else:
            batches.append(current)
    return batches


def _groups(texts: Sequence[str], limit_tokens: int) -> list[str]:
    """청크를 `limit_tokens` 안의 묶음으로(하나가 넘쳐도 혼자 한 묶음이다)."""
    groups: list[list[str]] = []
    used = 0
    for text in texts:
        size = count_tokens(text)
        if groups and used + size <= limit_tokens:
            groups[-1].append(text)
            used += size
        else:
            groups.append([text])
            used = size
    return ["\n\n".join(g) for g in groups]


class _Generator:
    def __init__(
        self, db: AsyncSession, title: str, target_tokens: int, model_id: uuid.UUID | None
    ) -> None:
        self.db = db
        self.title = title
        self.target = target_tokens
        self.model_id = model_id
        self.result = DigestResult(text="", token_count=0)

    async def call(self, instruction: str, body: str) -> str:
        return await self.call_data(instruction, f"문서 제목: {clean_label(self.title)}\n\n{body}")

    async def call_data(self, instruction: str, data: str) -> str:
        """`data`를 비신뢰 데이터 블록 하나로 넣어 부른다."""
        messages = [
            {"role": "system", "content": f"{UNTRUSTED_DATA_RULE}\n\n{instruction}"},
            {"role": "user", "content": wrap_untrusted(data, "source")},
        ]
        stream = StreamResult()
        gen = await complete_stream_with_fallback(
            self.db,
            self.model_id,
            messages,
            stream,
            temperature=0.2,
            max_tokens=int(self.target * _OUTPUT_FACTOR) + _OUTPUT_SLACK,
        )
        text = "".join([delta async for delta in gen]).strip()
        res = self.result
        res.calls += 1
        for key, value in stream.usage.items():
            res.usage[key] = res.usage.get(key, 0) + int(value or 0)
        if stream.model is not None:
            res.model = stream.model
            res.cost_usd += compute_cost(stream.usage, stream.model.model.cost_per_1k)
        if not text:
            raise DigestError("요약 응답이 비었습니다")
        return text


async def _group_limit(
    db: AsyncSession, model_id: uuid.UUID | None, group_tokens: int, output_tokens: int
) -> int:
    """한 번에 요약할 묶음 상한 — 설정값과 1차 모델 컨텍스트 중 작은 쪽(독립 리뷰 M3, #396).

    1차 모델은 `model_id`(없으면 기본 모델)다. 컨텍스트를 모르는 모델(없음·0)은 설정값을 쓴다.
    폴백 후보가 더 작으면 그 후보는 컨텍스트 초과로 실패하고 다음 후보로 넘어간다(#358).
    """
    try:
        primary = await resolve_model(db, model_id)
    except Exception:  # 모델 해석 실패는 첫 호출이 같은 이유로 실패하며 드러난다
        return group_tokens
    window = primary.model.context_window
    if not window or window <= 0:
        return group_tokens
    fit = int((window - output_tokens - _GROUP_OVERHEAD_TOKENS) * _GROUP_SAFETY)
    return max(min(group_tokens, fit), 200)


async def generate_digest(
    db: AsyncSession,
    title: str,
    texts: Sequence[str],
    *,
    target_tokens: int,
    group_tokens: int,
    into: DigestResult | None = None,
    model_id: uuid.UUID | None = None,
) -> DigestResult:
    """청크 전체를 덮는 요약을 만든다(map-reduce). 실패는 게이트웨이 예외(`GatewayError`)다.

    `model_id`는 모든 호출(map·reduce·whole)의 1차 모델이다(#396). 없으면 기본 모델이고, 어느 쪽이든
    폴백 체인을 탄다. 결과의 `model`은 실제로 응답한 모델이다.

    `into`를 주면 사용량을 거기에 쌓는다 — 중간 호출이 실패해도 이미 쓴 토큰·비용을 호출자가
    집계할 수 있다(독립 리뷰 M1).
    """
    gen = _Generator(db, title, target_tokens, model_id)
    if into is not None:
        gen.result = into
    group_tokens = await _group_limit(
        db, model_id, group_tokens, int(target_tokens * _OUTPUT_FACTOR) + _OUTPUT_SLACK
    )
    groups = _groups(texts, group_tokens)
    if not groups:
        raise DigestError("요약할 본문이 없습니다")
    fmt = {"target": target_tokens}
    if len(groups) == 1:
        text = await gen.call(_WHOLE_PROMPT.format(**fmt), groups[0])
    else:
        notes = [
            await gen.call(_MAP_PROMPT.format(**fmt, part=i + 1, parts=len(groups)), group)
            for i, group in enumerate(groups)
        ]
        while True:
            batches = _batches(notes, group_tokens)
            reduced = [
                await gen.call(_REDUCE_PROMPT.format(**fmt), "\n\n---\n\n".join(batch))
                for batch in batches
            ]
            if len(reduced) == 1:
                text = reduced[0]
                break
            notes = reduced
    gen.result.text = text
    gen.result.token_count = count_tokens(text)
    return gen.result


async def store_digest(source_id: uuid.UUID, content_hash: str, result: DigestResult) -> bool:
    """캐시 upsert — 그 소스의 **지금** 청크 지문이 `content_hash`일 때만 쓴다. 썼으면 True.

    지문 확인과 쓰기가 한 문장이다(#378 독립 리뷰 M) — 만드는 사이 재인제스트된 소스에 옛 내용의
    요약을 쓰지 않고, 늦게 저장하는 쪽이 그사이 저장된 새 내용의 요약을 덮지도 않는다. 별도 세션이고
    실패해도 예외를 올리지 않는다(그 run은 만든 요약을 그대로 쓴다).
    """
    from biznotes_core.core.db import async_session_factory

    model = result.model.model if result.model is not None else None
    values: dict[str, Any] = {
        "source_id": source_id,
        "content_hash": content_hash,
        "digest": result.text,
        "token_count": result.token_count,
        "model_id": model.id if model is not None else None,
        "model_name": model.display_name if model is not None else None,
        "prompt_version": PROMPT_VERSION,
    }
    columns = SourceDigest.__table__.c
    current = (
        select(_fingerprint()).where(SourceChunk.source_id == source_id).scalar_subquery()
    )
    # 지문이 다르거나 청크가 없으면(소스 삭제 포함) 넣을 행이 없다
    row = select(*(literal(v, columns[k].type) for k, v in values.items())).where(
        current == content_hash
    )
    stmt = pg_insert(SourceDigest).from_select(list(values), row)
    stmt = stmt.on_conflict_do_update(
        index_elements=[SourceDigest.source_id],
        set_={
            **{k: stmt.excluded[k] for k in values if k != "source_id"},
            "updated_at": func.now(),
        },
    )
    try:
        async with async_session_factory() as session:
            written = await session.execute(stmt)
            await session.commit()
    except Exception as e:  # DB 오류 — 캐시만 못 남긴다
        logger.warning(
            "source_digest 캐시 저장 실패", source_id=str(source_id), error=type(e).__name__
        )
        return False
    return bool(written.rowcount)  # type: ignore[attr-defined]


# ── 계층 요약(배치 reduce, #448) ─────────────────────────────────────────


@dataclass(frozen=True)
class BatchItem:
    """배치 요약 입력 하나 — `key`는 항목 식별(노드는 소스 id), `body`는 그 소스의 요약이나 대체
    본문이다."""

    key: str
    title: str
    body: str


@dataclass(frozen=True)
class DigestBatch:
    """배치 하나 — 순서 있는 항목들과 배치 요약의 목표 길이(항목마다 같은 몫 × 항목 수)."""

    items: tuple[BatchItem, ...]
    target_tokens: int


@dataclass(frozen=True)
class CachedBatch:
    """배치 캐시에서 읽은 요약과 그것을 만든 모델(표시용 스냅샷)."""

    text: str
    model_id: str | None
    model_name: str | None


def _truncate_tokens(text: str, cap: int) -> str:
    """`cap` 토큰 안으로 뒤를 자른다(글자 비율 추정)."""
    tokens = count_tokens(text) if text else 0
    if tokens <= cap:
        return text
    return text[: max(int(len(text) * cap / tokens), 1)]


def _batch_entry(item: BatchItem) -> str:
    """배치 요약 입력의 항목 하나 — 제목 줄과 본문."""
    return f"문서 제목: {clean_label(item.title)}\n\n{item.body}"


def _item_share(batch: DigestBatch) -> int:
    return max(batch.target_tokens // max(len(batch.items), 1), 1)


def _fallback_line(item: BatchItem, share: int) -> str:
    """항목 하나를 `- 제목: 본문 앞부분`(몫 안팎) 한 줄로."""
    body = _truncate_tokens(" ".join(item.body.split()), share)
    title = clean_label(item.title)
    return f"- {title}: {body}" if body else f"- {title}"


async def plan_batches(
    db: AsyncSession,
    items: Sequence[BatchItem],
    *,
    budget_tokens: int,
    group_tokens: int,
    model_id: uuid.UUID | None = None,
) -> list[DigestBatch]:
    """항목들을 순서대로 배치로 묶는다 — 묶음 입력은 상한 안, 항목 수는 `BATCH_MAX_ITEMS` 이하.

    배치 요약을 모두 합쳐 `budget_tokens` 안팎이 되게 항목마다 같은 몫(`budget_tokens // 항목 수`,
    최소 1)을 주고, 배치 목표 = 몫 × 항목 수다. 묶음 상한은 소스 요약 생성과 같은 규칙(설정
    `group_tokens`와 1차 모델 컨텍스트 중 작은 쪽)이다. 상한을 넘는 본문은 잘라 혼자 묶는다. 같은
    입력이면 같은 배치가 나온다 — 배치 캐시 키가 run마다 같다.
    """
    if not items:
        return []
    share = max(budget_tokens // len(items), 1)
    output = int(share * BATCH_MAX_ITEMS * _OUTPUT_FACTOR) + _OUTPUT_SLACK
    limit = await _group_limit(db, model_id, group_tokens, output)
    groups: list[list[BatchItem]] = []
    used = 0
    for item in items:
        size = count_tokens(_batch_entry(item))
        if size > limit:
            item = BatchItem(item.key, item.title, _truncate_tokens(item.body, limit))
            size = count_tokens(_batch_entry(item))
        if groups and len(groups[-1]) < BATCH_MAX_ITEMS and used + size <= limit:
            groups[-1].append(item)
            used += size
        else:
            groups.append([item])
            used = size
    return [DigestBatch(items=tuple(g), target_tokens=share * len(g)) for g in groups]


def batch_cache_key(batch: DigestBatch, model_id: uuid.UUID | None) -> str:
    """배치 캐시 키 — (항목 id·제목·본문 지문, 1차 모델, 목표 토큰, 배치 프롬프트 버전)의 sha256.

    본문 지문이라 소스 내용이 바뀌거나(새 소스 요약) 대체 본문이 요약으로 바뀌면 키가 달라진다.
    """
    payload = [
        BATCH_PROMPT_VERSION,
        str(model_id) if model_id is not None else "default",
        batch.target_tokens,
        [
            [item.key, item.title, hashlib.sha256(item.body.encode("utf-8")).hexdigest()]
            for item in batch.items
        ],
    ]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _redis_client() -> Any:
    from redis.asyncio import Redis

    return Redis.from_url(
        get_settings().redis_url,
        socket_connect_timeout=_BATCH_CACHE_TIMEOUT,
        socket_timeout=_BATCH_CACHE_TIMEOUT,
    )


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


async def load_batch_cached(keys: Sequence[str]) -> dict[str, CachedBatch]:
    """배치 캐시에서 `keys`를 한 번에 읽는다(`MGET`). Redis 오류·깨진 값은 캐시 미스다."""
    if not keys:
        return {}
    try:
        client = _redis_client()
        try:
            values = await client.mget([_BATCH_CACHE_KEY.format(key=k) for k in keys])
        finally:
            await client.aclose()
    except Exception as e:  # Redis 장애 — 캐시 없이 만든다(run을 실패시키지 않는다)
        logger.warning(
            "source_digest 배치 캐시 조회 실패, 캐시 미스로 처리", error=type(e).__name__
        )
        return {}
    hits: dict[str, CachedBatch] = {}
    for key, raw in zip(keys, values, strict=False):
        if raw is None:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str) or not text:
            continue
        hits[key] = CachedBatch(
            text=text,
            model_id=_optional_str(data.get("model_id")),
            model_name=_optional_str(data.get("model_name")),
        )
    return hits


async def store_batch_digest(key: str, result: DigestResult, *, ttl_seconds: int) -> bool:
    """배치 요약을 캐시에 쓴다(`SET EX`). 썼으면 True — Redis 오류는 예외 없이 False다."""
    model = result.model.model if result.model is not None else None
    value = json.dumps(
        {
            "text": result.text,
            "model_id": str(model.id) if model is not None else None,
            "model_name": model.display_name if model is not None else None,
        },
        ensure_ascii=False,
    )
    try:
        client = _redis_client()
        try:
            await client.set(_BATCH_CACHE_KEY.format(key=key), value, ex=ttl_seconds)
        finally:
            await client.aclose()
    except Exception as e:  # Redis 장애 — 캐시만 못 남긴다
        logger.warning("source_digest 배치 캐시 저장 실패", error=type(e).__name__)
        return False
    return True


async def generate_batch_digest(
    db: AsyncSession,
    batch: DigestBatch,
    *,
    model_id: uuid.UUID | None = None,
    into: DigestResult | None = None,
) -> DigestResult:
    """배치 하나의 요약을 만든다(호출 1번). 실패는 게이트웨이 예외(`GatewayError`)다.

    프롬프트는 항목마다 `- 제목: 핵심` 항목을 하나 이상 요구한다. 모델이 빠뜨린 항목은 본문 앞부분
    (항목 몫 안팎)으로 덧붙여, 결과에는 모든 항목의 제목이 한 번 이상 있다. 제목·본문은 비신뢰
    데이터 블록 하나로 넣는다. `model_id`·`into`는 `generate_digest`와 같다.
    """
    if not batch.items:
        raise DigestError("요약할 본문이 없습니다")
    share = _item_share(batch)
    gen = _Generator(db, "", batch.target_tokens, model_id)
    if into is not None:
        gen.result = into
    instruction = _BATCH_PROMPT.format(
        count=len(batch.items), target=batch.target_tokens, per=share
    )
    text = await gen.call_data(
        instruction, "\n\n---\n\n".join(_batch_entry(item) for item in batch.items)
    )
    missing = [item for item in batch.items if clean_label(item.title) not in text]
    if missing:
        text = "\n".join([text, *(_fallback_line(item, share) for item in missing)])
    gen.result.text = text
    gen.result.token_count = count_tokens(text)
    return gen.result


def batch_fallback_text(batch: DigestBatch) -> str:
    """배치 요약을 만들지 못했을 때 넣는 본문 — 항목마다 `- 제목: 본문 앞부분`(항목 몫 안팎)."""
    share = _item_share(batch)
    return "\n".join(_fallback_line(item, share) for item in batch.items)


__all__ = [
    "BATCH_MAX_ITEMS",
    "BATCH_PROMPT_VERSION",
    "EXCERPT_CHARS",
    "PROMPT_VERSION",
    "BatchItem",
    "CachedBatch",
    "DigestBatch",
    "DigestError",
    "DigestResult",
    "batch_cache_key",
    "batch_fallback_text",
    "chunk_texts",
    "content_hash_of",
    "content_hashes",
    "count_tokens",
    "fallback_body",
    "generate_batch_digest",
    "generate_digest",
    "load_batch_cached",
    "load_cached",
    "plan_batches",
    "store_batch_digest",
    "store_digest",
]
