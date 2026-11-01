"""노트북 단위 산출물 조회 — run을 넘어 열람할 수 있는 산출물을 찾는다(#133).

산출물은 run마다 `AgentRun.artifact_keys`(`[{key, name, content_type}]`)에 남는다. 이 모듈은
같은 노트북에서 열람자가 볼 수 있는 run의 산출물을 나열하고 하나를 찾는다. 런타임 도구
(`artifact_list`·`artifact_read`·`run_skill_script`의 `artifact_inputs`)와 제품의 목록 API가 같은
경계를 쓰게 한다.

경계는 run 열람 규칙(`authz.private_chat_run_exclusion`·`assert_can_read_run`)과 같다. 채팅 run은
본인과 관리자만 보고, 시스템 실행(`user_id` None)에는 채팅 run이 보이지 않는다.
`include_chat_runs=False`면 채팅 run을 아예 뺀다 — 결과가 노트북 전체에 보이는 곳(채팅이 아닌 run의
실행 로그·산출물)으로 갈 때 쓴다(#133 리뷰 B1).

**노트북 권한은 확인하지 않는다.** 호출자가 먼저 확인한다(제품 라우터의 노트북 권한 의존성,
런타임 도구의 호출마다 재확인).

한 run에 같은 이름이 여러 번 있으면 **첫 것**이 그 이름의 산출물이다. 이름·키가 없는 항목은
건너뛴다. 제품의 산출물 다운로드·`create_skill_from_artifact`와 같은 규칙이다. 두 제품 모두 run이
성공했을 때만 `artifact_keys`를 기록하므로, 진행 중이거나 실패한 run의 산출물은 보이지 않는다.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, cast, func, literal, select, true
from sqlalchemy.dialects.postgresql import JSONB

from biznotes_core.models import AgentRun
from biznotes_core.services.authz import private_chat_run_condition

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

MAX_LIST_LIMIT = 200


@dataclass(frozen=True)
class NotebookArtifact:
    run_id: uuid.UUID
    name: str
    key: str
    content_type: str
    run_created_at: datetime | None
    run_trigger: str
    run_status: str


def first_entry(entries: Any, name: str) -> dict[str, Any] | None:
    """`artifact_keys`에서 이 이름의 첫 항목 — 형식이 어긋난 항목은 건너뛴다."""
    for entry in entries if isinstance(entries, list) else []:
        if (
            isinstance(entry, dict)
            and entry.get("name") == name
            and isinstance(entry.get("key"), str)
            and entry["key"]
        ):
            return entry
    return None


def chat_condition(user_id: uuid.UUID | None, user_role: str, include_chat_runs: bool):
    if not include_chat_runs:
        return AgentRun.trigger != "chat"
    return private_chat_run_condition(user_id, user_role)


async def list_notebook_artifacts(
    db: "AsyncSession",
    notebook_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None,
    user_role: str,
    include_chat_runs: bool = True,
    exclude_run_id: uuid.UUID | None = None,
    name_contains: str | None = None,
    limit: int = 50,
) -> tuple[list[NotebookArtifact], bool]:
    """열람자가 볼 수 있는 산출물을 최신 run부터 나열한다 → (목록, 잘렸는지).

    같은 run 안에서는 기록한 순서다. `name_contains`는 대소문자를 가리지 않는 부분 일치다.
    """
    limit = max(1, min(int(limit), MAX_LIST_LIMIT))
    # 배열이 아닌 값(JSON null 등)에 jsonb_array_elements를 부르면 오류다 — WHERE보다 먼저
    # 평가될 수 있으니 함수 인자에서 빈 배열로 바꾼다
    keys = case(
        (func.jsonb_typeof(AgentRun.artifact_keys) == "array", AgentRun.artifact_keys),
        else_=cast(literal("[]"), JSONB),
    )
    elem = func.jsonb_array_elements(keys).table_valued("value", with_ordinality="ordinality")
    name = elem.c.value.op("->>")("name")
    key = elem.c.value.op("->>")("key")
    content_type = elem.c.value.op("->>")("content_type")
    conds = [
        AgentRun.notebook_id == notebook_id,
        chat_condition(user_id, user_role, include_chat_runs),
        func.jsonb_typeof(elem.c.value) == "object",
        # 문자열만 — `->>`는 숫자도 글자로 바꾸지만, 읽기(`first_entry`)와
        # 제품 다운로드는 문자열만 받는다
        func.jsonb_typeof(elem.c.value.op("->")("name")) == "string",
        func.jsonb_typeof(elem.c.value.op("->")("key")) == "string",
        key != "",
    ]
    if exclude_run_id is not None:
        conds.append(AgentRun.id != exclude_run_id)
    # run마다 이름별 첫 항목만 남긴다(DISTINCT ON — PostgreSQL)
    latest = (
        select(
            AgentRun.id.label("run_id"),
            AgentRun.created_at.label("created_at"),
            AgentRun.trigger.label("trigger"),
            AgentRun.status.label("status"),
            name.label("name"),
            key.label("key"),
            content_type.label("content_type"),
            elem.c.ordinality.label("ordinality"),
        )
        .select_from(AgentRun)
        .join(elem, true())
        .where(*conds)
        .distinct(AgentRun.id, name)
        .order_by(AgentRun.id, name, elem.c.ordinality)
        .subquery()
    )
    stmt = select(latest)
    if name_contains:
        stmt = stmt.where(
            func.lower(latest.c.name).contains(name_contains.lower(), autoescape=True)
        )
    stmt = stmt.order_by(
        latest.c.created_at.desc(), latest.c.run_id, latest.c.ordinality
    ).limit(limit + 1)
    rows = (await db.execute(stmt)).all()
    items = [
        NotebookArtifact(
            run_id=r.run_id,
            name=r.name,
            key=r.key,
            content_type=r.content_type or "application/octet-stream",
            run_created_at=r.created_at,
            run_trigger=r.trigger,
            run_status=r.status,
        )
        for r in rows[:limit]
    ]
    return items, len(rows) > limit


async def get_notebook_artifact(
    db: "AsyncSession",
    notebook_id: uuid.UUID,
    run_id: uuid.UUID,
    name: str,
    *,
    user_id: uuid.UUID | None,
    user_role: str,
    include_chat_runs: bool = True,
) -> NotebookArtifact | None:
    """run 하나의 산출물 하나. 없거나 경계 밖이면 None — 둘을 구분하지 않는다."""
    stmt = select(AgentRun).where(
        AgentRun.id == run_id,
        AgentRun.notebook_id == notebook_id,
        chat_condition(user_id, user_role, include_chat_runs),
    )
    run = (await db.execute(stmt)).scalar_one_or_none()
    if run is None:
        return None
    entry = first_entry(run.artifact_keys, name)
    if entry is None:
        return None
    return NotebookArtifact(
        run_id=run.id,
        name=name,
        key=entry["key"],
        content_type=str(entry.get("content_type") or "application/octet-stream"),
        run_created_at=run.created_at,
        run_trigger=run.trigger,
        run_status=run.status,
    )


__all__ = [
    "MAX_LIST_LIMIT",
    "NotebookArtifact",
    "get_notebook_artifact",
    "chat_condition",
    "first_entry",
    "list_notebook_artifacts",
]
