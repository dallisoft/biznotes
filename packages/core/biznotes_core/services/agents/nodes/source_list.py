"""소스 목록 도구 — `source_list` (#447, 읽기 전용).

채팅 에이전트가 노트북에 어떤 소스가 있는지 쪽 단위로 본다. 고른 소스는 `source_load`
(`source_ids`)로 읽거나 `rag_search`(`source_ids`)로 검색한다.

범위는 `source_load`와 같다 — 이 run의 노트북, 선택된 소스(`ctx.source_ids`), `ready` 상태.
순서도 같다(`(created_at, id)` 오름차순). 제목·요약은 외부가 정한 문자열이라 목록을 `source`
비신뢰 블록으로 감싼다(HWP-05). 블록 밖의 머리 한 줄(전체 수·쪽 위치)은 코어가 쓴다.
"""

import json
from typing import Any

from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.untrusted import clean_label, wrap_untrusted

SOURCE_LIST = "source_list"

LIST_DEFAULT = 50
LIST_MAX = 200
# 한 줄 요약 길이 — 소스 가이드 요약(없으면 다이제스트) 앞부분
SUMMARY_MAX_CHARS = 160
QUERY_MAX_CHARS = 200


def _int_arg(raw: Any, default: int, lo: int, hi: int | None = None) -> int:
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    value = max(lo, value)
    return value if hi is None else min(value, hi)


async def dispatch_source_list(ctx: RunContext, args: dict[str, Any]) -> str:
    """`source_list` 한 건 — 머리 메타 한 줄 + 소스마다 JSON 한 줄을 담은 `source` 블록."""
    from sqlalchemy import func, select

    from biznotes_core.models import Source, SourceDigest

    offset = _int_arg(args.get("offset"), 0, 0)
    limit = _int_arg(args.get("limit"), LIST_DEFAULT, 1, LIST_MAX)
    raw_query = args.get("query")
    query = (
        clean_label(raw_query, QUERY_MAX_CHARS).casefold()
        if isinstance(raw_query, str)
        else ""
    )

    rows: list[Any] = []
    if ctx.source_ids is None or ctx.source_ids:
        q = (
            select(
                Source.id,
                Source.title,
                Source.type,
                Source.token_count,
                # 요약은 앞부분만 읽는다 — 다이제스트는 문서 전체 요약이라 길다
                func.substr(Source.summary, 1, SUMMARY_MAX_CHARS * 2),
                func.substr(SourceDigest.digest, 1, SUMMARY_MAX_CHARS * 2),
            )
            .outerjoin(SourceDigest, SourceDigest.source_id == Source.id)
            .where(Source.notebook_id == ctx.notebook_id, Source.status == "ready")
        )
        if ctx.source_ids is not None:
            q = q.where(Source.id.in_(ctx.source_ids))
        q = q.order_by(Source.created_at, Source.id)
        rows = list((await ctx.db.execute(q)).all())

    items: list[dict[str, Any]] = []
    for source_id, title, source_type, token_count, summary, digest in rows:
        item = {
            "id": str(source_id),
            "title": clean_label(title or ""),
            "type": source_type,
            "token_count": int(token_count or 0),
            "summary": clean_label(summary or digest or "", SUMMARY_MAX_CHARS),
        }
        # 모델이 보는 제목·요약에서 찾는다(대소문자 무시)
        if query and query not in item["title"].casefold() + "\n" + item["summary"].casefold():
            continue
        items.append(item)

    total = len(items)
    page = items[offset : offset + limit]
    next_offset = offset + limit if offset + limit < total else None
    meta = {"total": total, "offset": offset, "count": len(page), "next_offset": next_offset}
    head = json.dumps(meta, ensure_ascii=False)
    if not page:
        return head
    body = "\n".join(json.dumps(i, ensure_ascii=False) for i in page)
    return f"{head}\n{wrap_untrusted(body, 'source')}"
