"""채팅과 노트 생성 근거의 정규화·화이트리스트 처리."""

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.schemas.provenance import ProvenanceItemOut, StoredProvenance, StoredProvenanceItem

if TYPE_CHECKING:
    from biznotes_core.models import AgentRun, Note
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.schemas.note import NoteCreate, NoteOut

# MCP 노출 함수명 규약(tool_registry.py::mcp_tool_name — 코어 공개 이름, #152)의 역파싱 상수.
# "mcp_<8hex 서버태그>_<정규화된 도구명>" — 서버태그 길이만 알면 서버명 조인 가능.
_MCP_TAG_LEN = 8
# 그래프 kind="mcp" 에이전트의 ExecutionLog.node_type(graph.py::kind_to_nodes) —
# react_tool_call과 달리 input_data가 {server_id, tool_name}이고 노출명 역파싱이 없다.
_GRAPH_MCP_NODE_TYPE = "mcp_tool"
_MAX_AGENT_NOTE_CHARS = 200_000
_AGENT_NOTE_TEXT_CONTENT_TYPES_EXACT = {"application/json"}
ORIGIN_REF_INVALID_DETAIL = "origin_ref가 유효하지 않습니다"

_KINDS = {"skill", "mcp", "tool", "source", "agent"}
_STATUSES = {"succeeded", "failed", "blocked", "unknown"}
_MODES = {"autonomous", "static", "agent_run", "unrecorded"}
_STRING_FIELDS = (
    "name",
    "server",
    "tool",
    "source_id",
    "title",
    "skill_version_id",
    "package_hash",
    "agent_id",
)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return str(value)[:200]
    except Exception:  # noqa: BLE001 - provenance must never break answer persistence
        return None


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _iter(value: Any) -> Iterable[Any]:
    if isinstance(value, (list, tuple)):
        return value
    return ()


def _status(value: Any, *, default: str = "unknown") -> str:
    return value if isinstance(value, str) and value in _STATUSES else default


def _item(raw: Any, *, default_status: str = "unknown") -> dict[str, Any] | None:
    data = _mapping(raw)
    if data is None or data.get("kind") not in _KINDS:
        return None
    clean: dict[str, Any] = {
        "kind": data["kind"],
        "status": _status(data.get("status"), default=default_status),
    }
    for field in _STRING_FIELDS:
        value = _text(data.get(field))
        if value is not None:
            clean[field] = value
    version = data.get("version")
    if isinstance(version, int) and not isinstance(version, bool):
        clean["version"] = version
    count = data.get("count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        clean["count"] = count
    try:
        return StoredProvenanceItem.model_validate(clean).model_dump(exclude_none=True)
    except Exception:  # noqa: BLE001 - skip malformed evidence item only
        return None


def _matches_failed_mcp(tool_name: str, evidence_items: list[dict[str, Any]]) -> bool:
    # 코어 공개 이름(v0.10.15 #152). 옛 비공개 이름은 경고 별칭이며 0.13.0에서 제거된다.
    from biznotes_core.services.agents.tool_registry import (
        MAX_TOOL_NAME_LEN,
        MCP_TOOL_PREFIX,
        mcp_tool_name,
    )

    if not tool_name.startswith(MCP_TOOL_PREFIX):
        return False
    exposed_parts = tool_name.removeprefix(MCP_TOOL_PREFIX).split("_", 1)
    if len(exposed_parts) != 2:
        return False
    server_tag = exposed_parts[0]

    # 충돌 접미사(`_<digits>`)를 역으로 한 번만 떼어내 O(1)로 재구성한다 —
    # `mcp_tool_name`이 접미사를 부여하는 규칙(base[:64-len(suffix)]+suffix)의 역연산.
    suffix = None
    if "_" in tool_name:
        _, _, candidate_suffix = tool_name.rpartition("_")
        if candidate_suffix.isdigit():
            suffix = f"_{candidate_suffix}"

    for item in evidence_items:
        if item.get("kind") != "mcp" or item.get("status") != "failed":
            continue
        evidence_tool = item.get("tool")
        if not isinstance(evidence_tool, str):
            continue
        base = mcp_tool_name(server_tag, evidence_tool, {})
        if tool_name == base:
            return True
        if suffix is not None:
            reconstructed = base[: MAX_TOOL_NAME_LEN - len(suffix)] + suffix
            if tool_name == reconstructed:
                return True
    return False


def _item_priority(item: dict[str, Any]) -> int:
    kind = item.get("kind")
    # "agent"는 채팅 경로에는 등장하지 않고(Phase 1 동작 무변경) origin=agent 노트의
    # 신원 항목(AC-12가 렌더할 에이전트 이름)이라 skill/mcp와 동급 최우선이어야
    # 한다 — 아니면 도구 로그 50건 초과 run에서 잘려 사라진다(B-04).
    if kind in {"skill", "mcp", "agent"}:
        return 0
    if kind == "tool":
        return 1
    return 2


def _prioritize_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """핵심 실행 근거(skill/mcp/agent > tool > source)를 앞세우되, 같은 우선순위 안의
    도착 순서는 보존한다 — 50개 절단 시 실패한 도구 호출이 대량의 source 인용
    evidence에 밀려 사라지지 않도록 한다."""
    return sorted(items, key=_item_priority)


def public_evidence_items(items: Any) -> list[dict[str, Any]]:
    """SSE evidence를 공개 계약 키로만 방어적으로 직렬화하고 저장 경로와 동일하게
    50개로 상한한다(AC-4)."""
    public: list[dict[str, Any]] = []
    for raw in _iter(items):
        try:
            item = _item(raw, default_status="succeeded")
            if item is None:
                continue
            # REST 응답(ChatMessageOut.provenance)과 동일하게 null 키도 유지한다 —
            # SSE evidence와 GET 재조회 결과의 항목 shape를 클라이언트에서 일치시키기 위함.
            public.append(ProvenanceItemOut.model_validate(item).model_dump())
        except Exception:  # noqa: BLE001 - malformed evidence must not break SSE completion
            continue
    return _prioritize_items(public)[:50]


def build_chat_provenance(
    tool_calls: Any,
    tool_evidence: Any,
    citations: Any,
    mode: str,
) -> dict[str, Any]:
    """채팅 실행 관측값을 v1 저장 dict로 변환한다.

    입력 한 항목의 타입·직렬화가 잘못돼도 답변 저장을 막지 않으며, 최악의 경우에도
    유효한 빈 items 스냅샷을 반환한다.
    """
    recorded_at = datetime.now(UTC)
    safe_mode = mode if isinstance(mode, str) and mode in _MODES else "unrecorded"
    try:
        items: list[dict[str, Any]] = []
        if safe_mode == "static":
            for citation in _iter(citations):
                data = _mapping(citation)
                if data is None:
                    continue
                item = _item(
                    {
                        "kind": "source",
                        "source_id": data.get("source_id"),
                        "title": data.get("source_title", data.get("title")),
                        "status": "succeeded",
                    },
                    default_status="succeeded",
                )
                if item is not None:
                    items.append(item)
        else:
            for evidence in _iter(tool_evidence):
                item = _item(evidence, default_status="succeeded")
                if item is not None:
                    items.append(item)

            for call in _iter(tool_calls):
                data = _mapping(call)
                if data is None:
                    continue
                tool_name = _text(data.get("tool"))
                call_status = _status(data.get("status"))
                if tool_name is None:
                    continue
                if call_status == "failed" and _matches_failed_mcp(tool_name, items):
                    continue
                item = _item({"kind": "tool", "tool": tool_name, "status": call_status})
                if item is not None:
                    items.append(item)

        stored = StoredProvenance.model_validate(
            {
                "mode": safe_mode,
                "recorded_at": recorded_at,
                "items": _prioritize_items(items)[:50],
            }
        )
        result = stored.model_dump(mode="json", exclude_none=True)
        result.setdefault("run_id", None)
        result.setdefault("artifact_name", None)
        result.setdefault("content_sha256", None)
        return result
    except Exception:  # noqa: BLE001 - answer persistence has priority over provenance
        return {
            "v": 1,
            "mode": safe_mode,
            "recorded_at": recorded_at.isoformat().replace("+00:00", "Z"),
            "run_id": None,
            "artifact_name": None,
            "content_sha256": None,
            "items": [],
        }


def _finalize_provenance(
    mode: str,
    items: list[dict[str, Any]],
    *,
    content_sha256: str,
    run_id: str | None = None,
    artifact_name: str | None = None,
) -> dict[str, Any]:
    """`build_chat_provenance`와 동일한 저장 형태(v1)를 조립 — 노트 결합 경로 전용.

    `run_id`/`artifact_name`은 `_text()`로 방어적으로 200자 절단한다(B-02) — 요청
    스키마 상한(`NoteCreate.artifact_name`)이 어긋나거나 우회되어도 여기서 다시
    한번 막아, `StoredProvenance` 검증 실패(pydantic `ValidationError`)가 호출부까지
    전파되어 내부 스키마 오류 문구가 그대로 422 detail로 새어나가는 경로를 원천 차단한다.
    """
    stored = StoredProvenance.model_validate(
        {
            "mode": mode,
            "recorded_at": datetime.now(UTC),
            "run_id": _text(run_id),
            "artifact_name": _text(artifact_name),
            "content_sha256": content_sha256,
            "items": items,
        }
    )
    result = stored.model_dump(mode="json", exclude_none=True)
    result.setdefault("run_id", None)
    result.setdefault("artifact_name", None)
    return result


async def filter_citations_to_notebook(
    db: "AsyncSession", notebook_id: uuid.UUID, citations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """citation의 source_id/chunk_id가 실제로 해당 노트북 소속인지 검증(F14, IDOR 방지).

    citation의 chunk_id는 청크 행 id(`SourceChunk.id`)다 — 검색 결과의 id가 곧 행 id다
    (pgvector). 코어 `_verify_chunk_membership`과 같은 기준으로 대조한다.
    소속이 확인되지 않는 citation은 조용히 제외한다. chunk_id가 같은 노트북의
    *다른* source에 속하는 조합(뒤섞인 citation)도 함께 거부한다. `notes.py`의
    manual 경로와 `bind_note_origin`의 chat 경로가 공유한다(둘 다 IDOR 표면).
    """
    from biznotes_core.models import Source, SourceChunk
    from sqlalchemy import select

    if not citations:
        return []

    source_ids: set[uuid.UUID] = set()
    chunk_ids: set[uuid.UUID] = set()
    for c in citations:
        try:
            if c.get("source_id"):
                source_ids.add(uuid.UUID(str(c["source_id"])))
            if c.get("chunk_id"):
                chunk_ids.add(uuid.UUID(str(c["chunk_id"])))
        except ValueError:
            continue

    valid_source_ids: set[uuid.UUID] = set()
    if source_ids:
        rows = await db.execute(
            select(Source.id).where(Source.id.in_(source_ids), Source.notebook_id == notebook_id)
        )
        valid_source_ids = set(rows.scalars().all())

    # 청크 id마다 소속 소스를 모은다 — 판정이 조회 순서에 기대지 않게 한다.
    chunk_sources: dict[uuid.UUID, set[uuid.UUID]] = {}
    if chunk_ids:
        rows = await db.execute(
            select(SourceChunk.id, SourceChunk.source_id)
            .join(Source, Source.id == SourceChunk.source_id)
            .where(SourceChunk.id.in_(chunk_ids), Source.notebook_id == notebook_id)
        )
        for row in rows.all():
            chunk_sources.setdefault(row.id, set()).add(row.source_id)

    filtered: list[dict[str, Any]] = []
    for c in citations:
        sid = c.get("source_id")
        cid = c.get("chunk_id")
        try:
            sid_uuid = uuid.UUID(str(sid)) if sid is not None else None
            cid_uuid = uuid.UUID(str(cid)) if cid is not None else None
        except ValueError:
            continue
        if sid_uuid is not None and sid_uuid not in valid_source_ids:
            continue
        if cid_uuid is not None:
            actual_sources = chunk_sources.get(cid_uuid)
            if not actual_sources:
                continue
            if sid_uuid is not None and sid_uuid not in actual_sources:
                continue
        filtered.append(c)
    return filtered


def _parse_mcp_exposed_name(tool_name: str) -> tuple[str, str] | None:
    """`mcp_<8hex>_<정규화된 도구명>` 노출 이름에서 서버 태그·도구명 조각을 역파싱한다.

    `mcp_tool_name`의 충돌 접미사(`_<digits>`)까지 완벽히 되돌리지는 않는다 —
    표시용 근사치다(핵심은 서버명 조인). 정확한 매칭이 필요한 실패 dedup 경로는
    `_matches_failed_mcp`를 그대로 사용한다.
    """
    from biznotes_core.services.agents.tool_registry import MCP_TOOL_PREFIX

    prefix_len = len(MCP_TOOL_PREFIX)
    if not tool_name.startswith(MCP_TOOL_PREFIX):
        return None
    if len(tool_name) <= prefix_len + _MCP_TAG_LEN + 1:
        return None
    tag = tool_name[prefix_len : prefix_len + _MCP_TAG_LEN]
    if tool_name[prefix_len + _MCP_TAG_LEN] != "_":
        return None
    remainder = tool_name[prefix_len + _MCP_TAG_LEN + 1 :]
    if not remainder:
        return None
    return tag, remainder


def _build_execution_log_item(
    tool_name: str, call_status: str, server_by_tag: Mapping[str, str]
) -> dict[str, Any] | None:
    parsed = _parse_mcp_exposed_name(tool_name)
    if parsed is not None:
        tag, remainder = parsed
        return _item(
            {
                "kind": "mcp",
                "server": server_by_tag.get(tag),
                "tool": remainder,
                "status": call_status,
            },
            default_status=call_status,
        )
    return _item(
        {"kind": "tool", "tool": tool_name, "status": call_status}, default_status=call_status
    )


def _build_mcp_node_item(
    server_id: Any, tool_name: Any, status: str, server_by_id: Mapping[str, str]
) -> dict[str, Any] | None:
    """그래프 `mcp_tool` 노드(kind="mcp" 에이전트) 로그 전용 — 노출명 역파싱이
    필요 없다. `input_data.server_id`가 곧 `MCPServer.id`, `tool_name`이 곧
    실제 도구명이며, 상태는 react 경로(`call_status`)와 달리 `ExecutionLog.status`
    행 컬럼에서 직접 온다(그래프 실행기가 `_log_node_finish(status=...)`로 마감)."""
    server_name = server_by_id.get(str(server_id)) if server_id is not None else None
    return _item(
        {"kind": "mcp", "server": server_name, "tool": tool_name, "status": status},
        default_status=status,
    )


async def extract_agent_run_items(db: "AsyncSession", run: "AgentRun") -> list[dict[str, Any]]:
    """완료된 AgentRun의 실행 근거를 v1 items로 재구성(R-02 typed extractor).

    실행 당시의 `ctx.tool_evidence`(휘발성)는 남아있지 않으므로 영속 데이터
    (`Agent`·`SkillVersion`·`ExecutionLog`)에서 사후 재구성한다. `ExecutionLog`는
    두 형태가 섞여 있다 — react 루프의 보조 로그(`react_tool_call`, `input_data.tool`
    + `input_data.call_status`)와 그래프 `mcp_tool` 노드 로그(`input_data.server_id`
    + `input_data.tool_name`, 상태는 `ExecutionLog.status` 행 컬럼) — 후자를 빠뜨리면
    `AgentKind="mcp"` 에이전트(정의상 mcp_tool→save_artifact 두 노드만으로 구성,
    graph.py::kind_to_nodes)로 만든 노트가 "MCP 미사용"으로 기록된다. `call_status`가
    없는 react 행(Phase 1 이전 과거 실행)은 `status="unknown"`으로 판정한다(R2-02).
    """
    from biznotes_core.models import Agent, ExecutionLog, MCPServer, SkillVersion
    from biznotes_core.services.agents.react.loop import _REACT_TOOL_NODE_TYPE
    from sqlalchemy import select

    items: list[dict[str, Any]] = []

    agent = await db.get(Agent, run.agent_id)
    if agent is not None:
        item = _item(
            {"kind": "agent", "name": agent.name, "status": "succeeded"}, default_status="succeeded"
        )
        if item is not None:
            items.append(item)

    if run.skill_version_id is not None:
        version = await db.get(SkillVersion, run.skill_version_id)
        skill_name = (
            run.skill_snapshot.get("name") if isinstance(run.skill_snapshot, dict) else None
        )
        item = _item(
            {
                "kind": "skill",
                "name": skill_name,
                "version": version.version if version is not None else None,
                "status": "succeeded",
                "skill_version_id": str(run.skill_version_id),
                "package_hash": run.skill_package_hash,
            },
            default_status="succeeded",
        )
        if item is not None:
            items.append(item)

    # id는 random UUID4(base.py::UUIDPrimaryKeyMixin)라 삽입 순서를 반영하지 않는다 —
    # 실제 호출 순서 보존을 위해 started_at으로 정렬한다.
    log_rows = await db.execute(
        select(ExecutionLog)
        .where(
            ExecutionLog.run_id == run.id,
            ExecutionLog.node_type.in_((_REACT_TOOL_NODE_TYPE, _GRAPH_MCP_NODE_TYPE)),
        )
        .order_by(ExecutionLog.started_at)
    )
    logs = list(log_rows.scalars().all())

    server_by_tag: dict[str, str] = {}
    server_by_id: dict[str, str] = {}
    if any(isinstance(log.input_data, Mapping) for log in logs):
        server_rows = await db.execute(select(MCPServer))
        servers = list(server_rows.scalars().all())
        server_by_tag = {
            str(server.id).replace("-", "")[:_MCP_TAG_LEN]: server.name for server in servers
        }
        server_by_id = {str(server.id): server.name for server in servers}

    for log in logs:
        data = log.input_data if isinstance(log.input_data, Mapping) else {}
        log_item: dict[str, Any] | None
        if log.node_type == _GRAPH_MCP_NODE_TYPE:
            tool_name = _text(data.get("tool_name"))
            if tool_name is None:
                continue
            log_item = _build_mcp_node_item(
                data.get("server_id"), tool_name, _status(log.status), server_by_id
            )
        else:
            tool_name = _text(data.get("tool"))
            if tool_name is None:
                continue
            call_status = _status(data.get("call_status"))
            log_item = _build_execution_log_item(tool_name, call_status, server_by_tag)
        if log_item is not None:
            items.append(log_item)

    return _prioritize_items(items)[:50]


async def _bind_chat_origin(
    db: "AsyncSession", notebook_id: uuid.UUID, user_id: uuid.UUID, body: "NoteCreate"
) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
    from biznotes_core.models import ChatMessage, ChatSession
    from sqlalchemy import select

    if body.origin_ref is None or body.artifact_name is not None:
        raise ValueError(ORIGIN_REF_INVALID_DETAIL)

    message = (
        await db.execute(
            select(ChatMessage)
            .join(ChatSession, ChatMessage.session_id == ChatSession.id)
            .where(
                ChatMessage.id == body.origin_ref,
                ChatMessage.role == "assistant",
                ChatSession.user_id == user_id,
                ChatSession.notebook_id == notebook_id,
            )
        )
    ).scalar_one_or_none()
    if message is None:
        raise ValueError(ORIGIN_REF_INVALID_DETAIL)

    content = message.content
    citations = await filter_citations_to_notebook(db, notebook_id, list(message.citations or []))
    content_sha256 = hashlib.sha256(content.encode()).hexdigest()

    stored: StoredProvenance | None = None
    if message.provenance is not None:
        try:
            stored = StoredProvenance.model_validate(message.provenance)
        except Exception as e:  # noqa: BLE001 - 손상된 저장값은 unrecorded로 대체(노트 생성 우선)
            # legacy(NULL)와 손상된 저장값은 사용자에게는 둘 다 "미기록"으로 보이는 게
            # 맞지만(D14), 운영자가 사후에 구분할 수 있도록 서버 로그는 남긴다(B-07).
            import structlog

            structlog.get_logger().warning(
                "chat_messages.provenance 검증 실패 — unrecorded로 대체",
                message_id=str(message.id),
                error=str(e),
            )
            stored = None

    if stored is None:
        provenance = _finalize_provenance("unrecorded", [], content_sha256=content_sha256)
    else:
        provenance = stored.model_dump(mode="json", exclude_none=True)
        provenance.setdefault("run_id", None)
        provenance.setdefault("artifact_name", None)
        provenance["content_sha256"] = content_sha256

    return content, citations, provenance


async def _bind_agent_origin(
    db: "AsyncSession",
    notebook_id: uuid.UUID,
    user_id: uuid.UUID,
    user_role: str,
    body: "NoteCreate",
) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
    import anyio
    from biznotes_core.models import AgentRun
    from biznotes_core.services.authz import private_chat_run_condition
    from biznotes_core.services.storage import get_object, stat_object_size
    from sqlalchemy import select

    from app.core.config import get_settings

    if body.origin_ref is None or not body.artifact_name:
        raise ValueError(ORIGIN_REF_INVALID_DETAIL)

    # 타인 채팅 진입 run은 없는 run과 같다 — 코어 열람 규칙(실행자 ∨ admin)을 ORM User 없이 쓴다
    # (`private_chat_run_condition`, #66). 거부 사유는 다른 무효 origin과 같은 422 하나다(D7)
    run = await db.scalar(
        select(AgentRun).where(
            AgentRun.id == body.origin_ref, private_chat_run_condition(user_id, user_role)
        )
    )
    if run is None or run.notebook_id != notebook_id or run.status != "succeeded":
        raise ValueError(ORIGIN_REF_INVALID_DETAIL)

    artifact = next(
        (
            a
            for a in run.artifact_keys or []
            if isinstance(a, Mapping) and a.get("name") == body.artifact_name
        ),
        None,
    )
    if artifact is None:
        raise ValueError(ORIGIN_REF_INVALID_DETAIL)

    content_type = str(artifact.get("content_type") or "")
    if not (
        content_type.startswith("text/") or content_type in _AGENT_NOTE_TEXT_CONTENT_TYPES_EXACT
    ):
        raise ValueError(ORIGIN_REF_INVALID_DETAIL)

    settings = get_settings()
    try:
        size = await anyio.to_thread.run_sync(
            lambda: stat_object_size(settings.storage_bucket_artifacts, artifact["key"])
        )
    except Exception as e:  # noqa: BLE001 - 스토리지 실패도 단일 422 detail로 통일(D7)
        raise ValueError(ORIGIN_REF_INVALID_DETAIL) from e
    # 상한 검사 전에 본문 전체를 메모리로 읽지 않는다(B-05) — UTF-8 최대 4바이트/문자
    # 기준 상한 바이트 수를 넘으면 디코딩 없이 즉시 거절한다.
    if size > _MAX_AGENT_NOTE_CHARS * 4:
        raise ValueError(ORIGIN_REF_INVALID_DETAIL)

    try:
        raw = await anyio.to_thread.run_sync(
            lambda: get_object(settings.storage_bucket_artifacts, artifact["key"])
        )
    except Exception as e:  # noqa: BLE001 - 스토리지 실패도 단일 422 detail로 통일(D7)
        raise ValueError(ORIGIN_REF_INVALID_DETAIL) from e

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(ORIGIN_REF_INVALID_DETAIL) from e

    if content_type == "application/json":
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except (ValueError, TypeError) as e:
            raise ValueError(ORIGIN_REF_INVALID_DETAIL) from e

    if len(text) > _MAX_AGENT_NOTE_CHARS:
        raise ValueError(ORIGIN_REF_INVALID_DETAIL)

    items = await extract_agent_run_items(db, run)
    content_sha256 = hashlib.sha256(text.encode()).hexdigest()
    provenance = _finalize_provenance(
        "agent_run",
        items,
        content_sha256=content_sha256,
        run_id=str(run.id),
        artifact_name=body.artifact_name,
    )
    return text, [], provenance


async def bind_note_origin(
    db: "AsyncSession",
    notebook_id: uuid.UUID,
    user_id: uuid.UUID,
    body: "NoteCreate",
    *,
    user_role: str = "user",
) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
    """origin에 따라 노트 본문·인용·provenance를 서버가 결정한다(R-01, D9/D10/D14).

    호출부는 이 함수가 던지는 `ValueError`를 422 단일 detail로 변환한다 — 어떤
    조건으로 거절됐는지는 응답에 드러내지 않는다(D7, 정보 최소 노출). `user_role`은 agent
    origin의 채팅 run 열람 판정(실행자 ∨ admin, #66)에 쓴다.
    """
    if body.origin == "manual":
        if body.origin_ref is not None or body.artifact_name is not None:
            raise ValueError(ORIGIN_REF_INVALID_DETAIL)
        citations = await filter_citations_to_notebook(
            db, notebook_id, [c.model_dump() for c in body.citations]
        )
        return body.content, citations, None

    if body.origin == "chat":
        return await _bind_chat_origin(db, notebook_id, user_id, body)

    if body.origin == "agent":
        return await _bind_agent_origin(db, notebook_id, user_id, user_role, body)

    raise ValueError(ORIGIN_REF_INVALID_DETAIL)


def note_out(note: "Note") -> "NoteOut":
    """`Note` 행을 `NoteOut`으로 직렬화하고 `content_intact`를 파생한다(D10).

    `content_sha256`은 저장 전용 내부 필드라 `ProvenanceOut`에는 없다(응답 노출 안 함)
    — 여기서 원시 `note.provenance` dict를 직접 읽어 편집 여부만 파생해 공개한다.
    """
    from app.schemas.note import NoteOut

    out = NoteOut.model_validate(note)
    content_sha256 = (
        note.provenance.get("content_sha256") if isinstance(note.provenance, dict) else None
    )
    if isinstance(content_sha256, str) and content_sha256:
        out.content_intact = hashlib.sha256(note.content.encode()).hexdigest() == content_sha256
    return out
