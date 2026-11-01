"""system_settings 키-값 저장소 헬퍼."""

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.models import SystemSetting
from biznotes_core.services.audit import record

# RAG 기본값 — 어드민에서 변경 가능
DEFAULTS: dict[str, Any] = {
    "rag": {
        "chunk_size": 800,
        "chunk_overlap": 100,
        "top_k": 6,
        "rerank_enabled": False,
        "rerank_candidates": 20,
        "rerank_model": "BAAI/bge-reranker-base",
        # [Phase 18a] 인용-청크 검증 2단계 임계값. 1차 어휘 겹침이 미달이면
        # 2차로 임베딩 코사인 유사도를 계산해 패러프레이즈 답변을 구제한다.
        "verify_lexical_threshold": 0.25,
        "verify_embedding_threshold": 0.55,
        # [Phase 18b] top-1 검색 점수가 이 값 미달이면 "근거 부족"으로 판단한다.
        # RRF 융합 점수는 검색 모드(리랭킹 on/off)에 따라 분포가 달라 별도 키로 분리한다.
        "insufficient_context_threshold_base": 0.5,
        "insufficient_context_threshold_rerank": 0.5,
        # [#194] 게이트웨이 임베딩 요청 1건의 입력 수와 타임아웃(초). 느린 로컬 서버는 배치를
        # 줄여 요청 하나가 타임아웃 안에 끝나게 한다. 로컬 fastembed 공간에는 쓰지 않는다
        "embedding_batch_size": 64,
        "embedding_timeout_seconds": 60,
        # [#209] 리랭크 1회(재시도 포함)의 벽시계 상한(초) — 게이트웨이 리랭커에만 쓴다
        "rerank_timeout_seconds": 30,
    },
    "agents": {
        # [Phase 19a] HITL 승인 대기 타임아웃(분) — 미해결 승인은 만료 처리된다.
        "approval_timeout_minutes": 10,
    },
}


async def get_setting(db: AsyncSession, key: str) -> dict[str, Any]:
    row = await db.get(SystemSetting, key)
    if row is None:
        return dict(DEFAULTS.get(key, {}))
    merged = dict(DEFAULTS.get(key, {}))
    merged.update(row.value)
    return merged


async def _lock_row(db: AsyncSession, key: str) -> SystemSetting:
    """키 행을 `FOR UPDATE`로 잠그고 커밋된 최신 값으로 다시 읽는다(#401).

    행이 없으면 빈 값으로 먼저 만든다(`ON CONFLICT DO NOTHING`) — 처음 저장하는 두 요청이
    PK 충돌로 실패하지 않고, 뒤 요청은 앞 요청의 커밋을 기다렸다가 그 값 위에 합친다.
    빈 행은 읽는 쪽(`get_setting`·`get_overrides`)에서 행이 없는 것과 같다.
    """
    await db.execute(
        pg_insert(SystemSetting)
        .values(key=key, value={})
        .on_conflict_do_nothing(index_elements=["key"])
    )
    # populate_existing — 같은 세션이 앞서 읽은 낡은 값(identity map)을 쓰지 않는다
    row = await db.get(SystemSetting, key, with_for_update=True, populate_existing=True)
    if row is None:  # 넣은 직후 다른 트랜잭션이 지우고 커밋한 경우뿐이다
        raise RuntimeError(f"system_settings[{key!r}] 행을 잠그지 못했습니다")
    return row


async def set_setting(db: AsyncSession, key: str, value: dict[str, Any]) -> dict[str, Any]:
    """보낸 항목만 저장값에 합친다 — 행을 잠근 뒤 읽으므로 동시 부분 갱신이 서로의 변경을
    되돌리지 않는다(#401). 커밋은 호출측이 한다.
    """
    row = await _lock_row(db, key)
    stored = row.value if isinstance(row.value, dict) else {}
    row.value = {**stored, **value}  # 새 dict를 대입해야 JSONB 변경이 감지된다
    await db.flush()
    return await get_setting(db, key)


# ---------- 재정의 키(#386·#387·#388) ----------
# 기본값이 env(설정)나 코드 상수에 있고, system_settings에는 관리자가 바꾼 항목만 두는 키다.
# `get_setting`과 달리 DEFAULTS를 섞지 않는다 — 「재정의 없음」과 「기본값과 같은 재정의」를
# 구분한다.


def valid_int(value: Any, minimum: int, maximum: int) -> int | None:
    """저장된 값이 [minimum, maximum] 범위의 정수면 그 값, 아니면 None(bool 제외).

    API가 범위를 검증하지만, DB를 직접 고친 값이 런타임 판정을 깨지 않게 읽을 때도 거른다.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if minimum <= value <= maximum else None


async def get_overrides(db: AsyncSession, key: str) -> dict[str, Any]:
    """재정의 키의 저장값 그대로(없으면 빈 dict)."""
    row = await db.get(SystemSetting, key)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


async def update_overrides(
    db: AsyncSession,
    key: str,
    changes: Mapping[str, Any],
    *,
    actor_id: uuid.UUID | None,
    action: str,
) -> dict[str, Any]:
    """재정의 항목을 바꾸고 감사 로그(`detail = {changes, before, after}`)를 남긴다.

    값이 None인 항목은 재정의를 지운다(기본값으로 돌아간다). `changes`에 없는 항목은 그대로다.
    범위 검증은 호출측(요청 스키마)의 몫이다. 커밋은 호출측이 한다.
    """
    row = await _lock_row(db, key)
    before = dict(row.value) if isinstance(row.value, dict) else {}
    after = dict(before)
    for name, value in changes.items():
        if value is None:
            after.pop(name, None)
        else:
            after[name] = value
    row.value = after  # 새 dict를 대입해야 JSONB 변경이 감지된다
    await record(
        db,
        actor_id,
        action,
        resource=key,
        detail={"changes": dict(changes), "before": before, "after": after},
    )
    await db.flush()
    return after
