"""노트북 산출물 도구 — 다른 run이 만든 산출물을 나열하고 읽는다(#133).

`artifact_list`는 같은 노트북에서 실행 사용자가 볼 수 있는 run의 산출물을 최신순으로 낸다.
`artifact_read`는 산출물 하나의 본문을 텍스트 계열만 30,000자씩 나눠 준다. 바이너리는 메타만
주고, 스크립트 입력(`run_skill_script`의 `artifact_inputs`)으로 넘기게 안내한다.

경계는 `services.agents.notebook_artifacts`(run 열람 규칙)가 정하고, 여기서 두 가지를 더 좁힌다.

- 채팅 run의 산출물은 **채팅 run에서만** 본다. 도구 결과는 실행 로그에 남는데, 채팅이 아닌 run의
  로그·산출물은 노트북 열람자 전원에게 보인다 — 비공개 채팅 내용이 그쪽으로 새지 않게 한다(리뷰 B1).
- 실행 사용자의 노트북 권한을 호출마다 다시 확인한다. 스케줄 run은 권한 확인 없이 시작되고,
  승인 대기 run은 공유가 회수된 뒤 재개될 수 있다(리뷰 M1).

없는 산출물과 경계 밖 산출물은 같은 문구로 실패해 존재 여부를 흘리지 않는다. 산출물은 LLM·스킬이
만든 내용이라 결과는 공통 지점(`react/skill_guard.guard_tool_output`)이 비신뢰 블록으로 감싼다.
"""

import codecs
import json
import uuid
from typing import TYPE_CHECKING, Any

import anyio

from biznotes_core.config import get_settings
from biznotes_core.services.agents.errors import ToolUnavailableError
from biznotes_core.services.agents.notebook_artifacts import (
    NotebookArtifact,
    first_entry,
    get_notebook_artifact,
    list_notebook_artifacts,
)

if TYPE_CHECKING:
    from biznotes_core.services.agents.context import RunContext

ARTIFACT_LIST = "artifact_list"
ARTIFACT_READ = "artifact_read"
ARTIFACT_TOOL_NAMES: frozenset[str] = frozenset({ARTIFACT_LIST, ARTIFACT_READ})

LIST_DEFAULT = 50
LIST_MAX = 100
# 이름 상한 — `save_artifact` 이름은 템플릿 렌더 결과라 길 수 있다.
# 목록에 나온 이름은 읽을 수 있어야 한다
NAME_MAX_CHARS = 1024
FILTER_MAX_CHARS = 128
# 한 번에 돌려주는 본문 글자 수(hwp 구조 도구와 같은 상한)
READ_CHUNK_CHARS = 30_000

NOT_VISIBLE = "이 실행에서 볼 수 없는 산출물입니다 — artifact_list로 run_id와 name을 확인하세요."
BINARY_NOTE = (
    "텍스트가 아닌 산출물이라 본문을 보여 주지 않습니다. "
    "스크립트로 처리하려면 run_skill_script의 artifact_inputs로 넘기세요."
)

_TEXT_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/x-ndjson",
        "application/javascript",
    }
)


def is_text_type(content_type: str) -> bool:
    base = content_type.split(";", 1)[0].strip().lower()
    return (
        base.startswith("text/")
        or base in _TEXT_TYPES
        or base.endswith(("+json", "+xml", "+yaml"))
    )


def _run_id(raw: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError):
        raise ToolUnavailableError(NOT_VISIBLE) from None


def _name(raw: Any) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > NAME_MAX_CHARS or "\x00" in raw:
        raise ToolUnavailableError(NOT_VISIBLE)
    return raw


def _int_arg(raw: Any, default: int, lo: int, hi: int) -> int:
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    return max(lo, min(value, hi))


async def notebook_readable(ctx: "RunContext") -> bool:
    """실행 사용자가 지금도 이 노트북을 읽을 수 있는지. 시스템 실행(사용자 없음)은 그대로 둔다."""
    if ctx.user_id is None:
        return True
    from biznotes_core.models import Notebook, User
    from biznotes_core.services.notebooks import get_permission

    notebook = await ctx.db.get(Notebook, ctx.notebook_id)
    user = await ctx.db.get(User, ctx.user_id)
    if notebook is None or user is None:
        return False
    return await get_permission(ctx.db, notebook, user) is not None


def include_chat_runs(ctx: "RunContext") -> bool:
    """채팅 run 산출물은 결과가 비공개(채팅 run)로만 남을 때 본다(리뷰 B1)."""
    return ctx.trigger == "chat"


async def find_artifact(ctx: "RunContext", run_id: uuid.UUID, name: str) -> NotebookArtifact | None:
    """이 실행이 볼 수 있는 산출물 하나. 현재 run이면 진행 중인 `ctx.artifact_keys`에서 찾는다."""
    if run_id == ctx.run_id:
        entry = first_entry(ctx.artifact_keys, name)
        if entry is None:
            return None
        return NotebookArtifact(
            run_id=ctx.run_id,
            name=name,
            key=entry["key"],
            content_type=str(entry.get("content_type") or "application/octet-stream"),
            run_created_at=None,
            run_trigger=ctx.trigger,
            run_status="running",
        )
    if not await notebook_readable(ctx):
        return None
    return await get_notebook_artifact(
        ctx.db,
        ctx.notebook_id,
        run_id,
        name,
        user_id=ctx.user_id,
        user_role=ctx.user_role,
        include_chat_runs=include_chat_runs(ctx),
    )


def _read_prefix(key: str, max_bytes: int) -> tuple[bytes, bool]:
    """앞에서 `max_bytes`까지만 읽는다 → (본문, 원본이 더 긴지)."""
    from biznotes_core.services import storage

    stream = storage.open_object_stream(get_settings().storage_bucket_artifacts, key)
    buf = bytearray()
    try:
        for chunk in stream.chunks():
            buf.extend(chunk)
            if len(buf) > max_bytes:
                return bytes(buf[:max_bytes]), True
    finally:
        stream.close()
    return bytes(buf), False


def _decode(data: bytes, cut: bool) -> str:
    # 잘라 읽었으면 끝에 걸린 다바이트 문자 조각은 버린다(대체 문자로 남기지 않는다)
    return codecs.getincrementaldecoder("utf-8")("replace").decode(data, final=not cut)


def _stat_size(key: str) -> int:
    from biznotes_core.services import storage

    return storage.stat_object_size(get_settings().storage_bucket_artifacts, key)


async def _list(ctx: "RunContext", args: dict[str, Any]) -> str:
    limit = _int_arg(args.get("limit"), LIST_DEFAULT, 1, LIST_MAX)
    raw = args.get("name_contains")
    name_contains = raw[:FILTER_MAX_CHARS] if isinstance(raw, str) and raw else None
    if not await notebook_readable(ctx):
        raise ToolUnavailableError(NOT_VISIBLE)
    items, truncated = await list_notebook_artifacts(
        ctx.db,
        ctx.notebook_id,
        user_id=ctx.user_id,
        user_role=ctx.user_role,
        include_chat_runs=include_chat_runs(ctx),
        exclude_run_id=ctx.run_id,
        name_contains=name_contains,
        limit=limit,
    )
    ctx.tool_evidence.append({"kind": "artifact", "mode": "list", "result_count": len(items)})
    payload: dict[str, Any] = {
        "artifacts": [
            {
                "run_id": str(a.run_id),
                "name": a.name,
                "content_type": a.content_type,
                "readable": is_text_type(a.content_type),
                "run_created_at": a.run_created_at.isoformat() if a.run_created_at else None,
            }
            for a in items
        ],
        "truncated": truncated,
    }
    return _fit(payload)


def _fit(payload: dict[str, Any]) -> str:
    """목록 JSON을 `READ_CHUNK_CHARS` 안으로 — 넘으면 뒤쪽 항목을 잘라 내고 `truncated`를 켠다."""
    text = json.dumps(payload, ensure_ascii=False)
    items = payload["artifacts"]
    while len(text) > READ_CHUNK_CHARS and items:
        if len(items) > 1:
            del items[len(items) // 2 :]
        else:
            items.clear()
        payload["truncated"] = True
        text = json.dumps(payload, ensure_ascii=False)
    return text


async def _read(ctx: "RunContext", args: dict[str, Any]) -> str:
    run_id = _run_id(args.get("run_id"))
    name = _name(args.get("name"))
    art = await find_artifact(ctx, run_id, name)
    if art is None:
        raise ToolUnavailableError(NOT_VISIBLE)
    ctx.tool_evidence.append(
        {"kind": "artifact", "mode": "read", "run_id": str(art.run_id), "name": art.name}
    )
    head: dict[str, Any] = {
        "run_id": str(art.run_id),
        "name": art.name,
        "content_type": art.content_type,
    }
    if not is_text_type(art.content_type):
        size = await anyio.to_thread.run_sync(_stat_size, art.key)
        return json.dumps({**head, "size": size, "text": None, "note": BINARY_NOTE},
                          ensure_ascii=False)
    max_bytes = get_settings().artifact_read_max_bytes
    data, cut = await anyio.to_thread.run_sync(_read_prefix, art.key, max_bytes)
    text = _decode(data, cut)
    offset = _int_arg(args.get("offset"), 0, 0, len(text))
    end = offset + READ_CHUNK_CHARS
    payload = {
        **head,
        "offset": offset,
        "text": text[offset:end],
        "next_offset": end if end < len(text) else None,
        "total_chars": len(text),
        "truncated_source": cut,
    }
    return json.dumps(payload, ensure_ascii=False)


async def dispatch_artifact_tool(ctx: "RunContext", name: str, args: dict[str, Any]) -> str:
    if name == ARTIFACT_LIST:
        return await _list(ctx, args)
    if name == ARTIFACT_READ:
        return await _read(ctx, args)
    raise ToolUnavailableError(f"알 수 없는 산출물 도구입니다: {name}")
