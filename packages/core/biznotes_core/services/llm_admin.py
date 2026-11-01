"""어드민 LLM 관리 — HTTP에 의존하지 않는 도메인 로직 (R3-3).

라우터(`api/v1/admin/llm.py`)에 섞여 있던 규칙을 서비스로 내린다. 이 모듈은
`fastapi`를 임포트하지 않으며, 실패를 `HTTPException`이 아니라 도메인 예외로
표현한다.

라우터 씬화의 템플릿이다(PLAN.md R3-3: *"이후 sources/agents/chat 등으로 점진
확산 — 전 라우터 일괄 아님"*). 커밋 경계는 호출자(라우터)가 소유한다.
"""

import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import any_, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.models import LLMModel

_DEFAULT_CONSTRAINT_PATTERN = re.compile(r"uq_llm_models_default_[a-z0-9_]+")


class DefaultModelConflict(Exception):
    """능력별 default unique index와 충돌한 도메인 오류."""

    def __init__(self, constraint_name: str) -> None:
        self.constraint_name = constraint_name
        super().__init__(f"default model constraint violated: {constraint_name}")


def default_conflict(exc: IntegrityError) -> DefaultModelConflict | None:
    """능력별 default 제약 위반만 도메인 충돌로 분류한다."""
    causes = (exc.orig, getattr(exc.orig, "__cause__", None))
    for cause in causes:
        constraint_name = getattr(cause, "constraint_name", None)
        if isinstance(constraint_name, str) and constraint_name.startswith(
            "uq_llm_models_default_"
        ):
            return DefaultModelConflict(constraint_name)

    for cause in causes:
        match = _DEFAULT_CONSTRAINT_PATTERN.search(str(cause))
        if match is not None:
            return DefaultModelConflict(match.group(0))
    return None


async def embedding_model_exists(db: AsyncSession, exclude_id: uuid.UUID | None = None) -> bool:
    """**활성** 임베딩 모델이 이미 있는지 (동시에 쓰는 임베딩 모델은 1개).

    비활성(enabled=false) 모델은 세지 않는다. 모델을 교체할 때 이전 모델 행이
    남아 있어야 그 모델로 만든 벡터를 계속 질의할 수 있기 때문이다(#7,
    `services/embedding_spaces.py`). 교체 절차는 "기존 모델 비활성화 → 새 모델
    등록 → 기본값 지정 → stale 재인덱싱 → 기존 모델 삭제"다.

    `exclude_id`는 자기 자신을 제외하고 검사할 때 쓴다(수정 경로).
    """
    q = select(LLMModel.id).where(
        literal("embedding") == any_(LLMModel.capabilities), LLMModel.enabled.is_(True)
    )
    if exclude_id is not None:
        q = q.where(LLMModel.id != exclude_id)
    return (await db.execute(q)).first() is not None


async def embedding_default_exists(db: AsyncSession, exclude_id: uuid.UUID | None = None) -> bool:
    """기본(is_default) 임베딩 모델 행이 이미 있는지 (비활성 포함).

    기본값은 능력별로 1개라는 부분 유니크 인덱스가 걸려 있어, 기존 기본값이
    남아 있는 동안 새 모델을 기본값으로 만들 수 없다 — 그래서 등록은 비기본으로
    하고 `POST /models/{id}/set-default`로 전환한다.
    """
    q = select(LLMModel.id).where(
        literal("embedding") == any_(LLMModel.capabilities), LLMModel.is_default.is_(True)
    )
    if exclude_id is not None:
        q = q.where(LLMModel.id != exclude_id)
    return (await db.execute(q)).first() is not None


async def reranker_default_exists(db: AsyncSession) -> bool:
    """기본(is_default) 리랭커 행이 이미 있는지 (비활성 포함, #209).

    리랭커는 저장된 벡터가 없어 임베딩과 달리 여러 개를 동시에 활성화할 수 있다. 기본값만
    능력별 부분 유니크 인덱스로 1개다 — 첫 리랭커를 기본값으로 만들 때 이 함수로 확인한다.
    """
    q = select(LLMModel.id).where(
        literal("reranker") == any_(LLMModel.capabilities), LLMModel.is_default.is_(True)
    )
    return (await db.execute(q)).first() is not None


@dataclass
class ImportResult:
    """일괄 등록 결과 — 생성된 모델과 건너뛴 이름."""

    created: list[LLMModel] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


async def plan_model_import(
    db: AsyncSession,
    provider_id: uuid.UUID,
    items: list,
) -> ImportResult:
    """디스커버리에서 고른 모델들을 등록 대상으로 판별한다 (세션에 add만, 커밋은 호출자).

    두 규칙을 적용한다:
    - 이미 같은 `model_name`이 등록된 프로바이더면 건너뛴다.
    - 임베딩 모델은 일괄 등록에서 **1개만** 받아들인다. 제공자 목록에는 임베딩
      모델이 여러 개 있어 전부 등록하면 목록이 지저분해진다. 활성 임베딩 모델이
      이미 있으면 건너뛴다 — `embedding_model_exists` 참조.

    새 임베딩 모델은 기본(is_default) 임베딩 행이 없을 때만 즉시 기본값이 된다.
    교체 중이라 비활성화된 기존 모델이 기본값을 쥐고 있으면 비기본으로 만들고,
    `POST /models/{id}/set-default`로 전환한다(#109, 단건 등록과 같은 규칙).
    리랭커는 개수 제한이 없고, 기본 리랭커가 없을 때 배치의 첫 리랭커만 기본값이 된다(#209).
    이미지 생성 모델(`image` 단독 능력)은 단건 등록처럼 기본값을 자동으로 정하지 않는다(#338·
    #347) — 기본값이 없으면 `resolve_model`이 첫 활성 이미지 모델을 쓴다.
    """
    existing = {
        name
        for (name,) in (
            await db.execute(select(LLMModel.model_name).where(LLMModel.provider_id == provider_id))
        ).all()
    }
    has_embedding = await embedding_model_exists(db)
    has_embedding_default = await embedding_default_exists(db)
    has_reranker_default = await reranker_default_exists(db)

    result = ImportResult()
    for item in items:
        if item.model_name in existing:
            result.skipped.append(item.model_name)
            continue

        is_embedding = "embedding" in item.capabilities
        if is_embedding:
            if has_embedding:
                result.skipped.append(item.model_name)
                continue
            has_embedding = True  # 이 배치 안에서도 1개만 허용

        # 기존 기본값 행(비활성 포함)이 없을 때만 즉시 default
        is_default = is_embedding and not has_embedding_default
        if "reranker" in item.capabilities and not is_embedding and not has_reranker_default:
            is_default = True
            has_reranker_default = True  # 이 배치 안에서도 첫 리랭커만

        model = LLMModel(
            provider_id=provider_id,
            model_name=item.model_name,
            display_name=item.display_name,
            capabilities=item.capabilities,
            context_window=item.context_window,
            enabled=True,
            is_default=is_default,
            # 두 필드(#429·#428)를 모르는 이전 호출자의 항목도 받는다 — `items`는 덕 타입이다
            reasoning_effort=getattr(item, "reasoning_effort", None),
            max_output_tokens=getattr(item, "max_output_tokens", None),
        )
        db.add(model)
        existing.add(item.model_name)
        result.created.append(model)

    return result


__all__ = [
    "DefaultModelConflict",
    "ImportResult",
    "default_conflict",
    "embedding_default_exists",
    "reranker_default_exists",
    "embedding_model_exists",
    "plan_model_import",
]
