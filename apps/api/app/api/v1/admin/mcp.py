"""어드민 — MCP 서버 관리 (등록/헬스체크/도구 목록)."""

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from biznotes_core.api.deps import get_current_user
from biznotes_core.api.routing import RedactedValidationRoute
from biznotes_core.core.crypto import encrypt
from biznotes_core.core.db import get_db
from biznotes_core.models import MCPServer, User
from biznotes_core.services.agents.mcp_exposure import tool_pin_states
from biznotes_core.services.agents.mcp_tool_spec import (
    MCP_TOOL_TEXT_LIMIT,
    MCPToolSpecRejected,
    MCPToolSpecRejectReason,
    mcp_tool_function,
)
from biznotes_core.services.audit import record
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.mcp_pins import PinState, pin_state, spec_hash_or_none

router = APIRouter(prefix="/mcp-servers", route_class=RedactedValidationRoute)
logger = structlog.get_logger()

# 승인 테이블 `mcp_server_tools.tool_name`이 String(128)이다 — 헬스체크 화면도 그 길이로 자른다
_TOOL_NAME_MAX = 128


class MCPServerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    transport: str = Field(pattern="^(stdio|sse|streamable_http)$")
    endpoint_or_command: str = Field(min_length=1)
    env: dict[str, str] | None = None
    enabled: bool = True


class MCPServerUpdate(BaseModel):
    endpoint_or_command: str | None = None
    env: dict[str, str] | None = None
    enabled: bool | None = None


class MCPServerOut(BaseModel):
    id: uuid.UUID
    name: str
    transport: str
    endpoint_or_command: str
    has_env: bool
    enabled: bool
    last_health_check: datetime | None
    last_health_ok: bool | None


# 헬스체크 응답 모델(core-v0.20.0-upgrade §5.7·D-14, 코어 v0.20.0 #229). 외부 서버가 정한 원문
# 객체(`input_schema`·`title`·`annotations`·원문 설명)는 싣지 않는다 — 관리자는 런타임 tool spec과
# 같은 정화 결과를 보고 승인을 판단하고, 원문 스키마는 응답 직렬화를 깨뜨릴 수 있다(깊이 300 → 500).
class MCPToolSpecOut(BaseModel):
    """`ok` = 원문 그대로 노출, `sanitized` = 정화한 설명·스키마로 노출, `rejected` = 노출 안 함."""

    status: Literal["ok", "sanitized", "rejected"]
    reason: MCPToolSpecRejectReason | None


class MCPToolHealthOut(BaseModel):
    name: str
    description: str  # 정화 설명(코어 `mcp_tool_function`). `rejected`면 빈 문자열
    spec: MCPToolSpecOut
    # 승인 스펙 고정(코어 v0.21.0 #293). `spec_hash`는 지금 본 스펙의 `mcp_tool_spec_hash` — 승인
    # CLI(`scripts/mcp_approvals.py set TOOL=HASH`)에 그대로 넘긴다. `rejected`면 None(고정 불가).
    spec_hash: str | None = None
    approval: PinState = "not_approved"


class MCPHealthCheckOut(BaseModel):
    ok: bool
    tools: list[MCPToolHealthOut] = []
    error: str | None = None


def _out(s: MCPServer) -> MCPServerOut:
    return MCPServerOut(
        id=s.id,
        name=s.name,
        transport=s.transport,
        endpoint_or_command=s.endpoint_or_command,
        has_env=s.env_encrypted is not None,
        enabled=s.enabled,
        last_health_check=s.last_health_check,
        last_health_ok=s.last_health_ok,
    )


@router.get("", response_model=list[MCPServerOut])
async def list_servers(db: AsyncSession = Depends(get_db)) -> list[MCPServerOut]:
    rows = await db.execute(select(MCPServer).order_by(MCPServer.name))
    return [_out(s) for s in rows.scalars().all()]


@router.post("", response_model=MCPServerOut, status_code=status.HTTP_201_CREATED)
async def create_server(
    body: MCPServerCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> MCPServerOut:
    existing = (
        await db.execute(select(MCPServer).where(MCPServer.name == body.name))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 존재하는 서버명입니다")
    server = MCPServer(
        name=body.name,
        transport=body.transport,
        endpoint_or_command=body.endpoint_or_command,
        env_encrypted=encrypt(json.dumps(body.env)) if body.env else None,
        enabled=body.enabled,
    )
    db.add(server)
    await record(db, admin.id, "mcp.server_create", body.name, {"transport": body.transport})
    await db.commit()
    await db.refresh(server)
    return _out(server)


@router.patch("/{server_id}", response_model=MCPServerOut)
async def update_server(
    server_id: uuid.UUID,
    body: MCPServerUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> MCPServerOut:
    server = await db.get(MCPServer, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    if body.endpoint_or_command is not None:
        server.endpoint_or_command = body.endpoint_or_command
    if body.env is not None:
        server.env_encrypted = encrypt(json.dumps(body.env)) if body.env else None
    if body.enabled is not None:
        server.enabled = body.enabled
    await record(db, admin.id, "mcp.server_update", server.name)
    await db.commit()
    await db.refresh(server)
    return _out(server)


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(
    server_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> None:
    server = await db.get(MCPServer, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    await record(db, admin.id, "mcp.server_delete", server.name)
    await db.delete(server)
    await db.commit()


def _tool_health(
    server_id: uuid.UUID, tool: Mapping[str, Any], pins: Mapping[str, str | None]
) -> MCPToolHealthOut:
    """`list_tools` 항목 하나 → 런타임 디스커버리와 같은 판정(코어 `_discover_mcp_tools`의 비교).

    정화 규칙은 제품에서 복제하지 않는다 — 코어 `mcp_tool_function`이 정본이다. 판정이 예상 밖
    예외면 그 도구만 노출 불가로 두고 헬스체크 전체를 실패시키지 않는다(원문은 로그에 싣지 않는다).
    `pins`는 이 서버의 read-only 승인 고정값(코어 `tool_pin_states`)이다.
    """
    name = ""
    try:
        name = str(tool.get("name") or "")[:_TOOL_NAME_MAX]
        description, parameters = mcp_tool_function(tool)
        raw_description = str(tool.get("description") or "")[:MCP_TOOL_TEXT_LIMIT]
        raw_schema = tool.get("input_schema") or parameters  # 비었으면 기본 스키마다
        sanitized = description != raw_description or parameters != raw_schema
        spec_hash = spec_hash_or_none(tool)
    except MCPToolSpecRejected as e:
        return MCPToolHealthOut(
            name=name,
            description="",
            spec=MCPToolSpecOut(status="rejected", reason=e.reason),
            approval=pin_state(name, None, pins),
        )
    except Exception as e:  # noqa: BLE001 — 도구 하나가 응답 전체를 막지 않는다
        logger.warning(
            "mcp_health_tool_spec_failed", server_id=str(server_id), error_type=type(e).__name__
        )
        return MCPToolHealthOut(
            name=name,
            description="",
            spec=MCPToolSpecOut(status="rejected", reason="schema_unsupported_value"),
            approval=pin_state(name, None, pins),
        )
    return MCPToolHealthOut(
        name=name,
        description=description,
        spec=MCPToolSpecOut(status="sanitized" if sanitized else "ok", reason=None),
        spec_hash=spec_hash,
        approval=pin_state(name, spec_hash, pins),
    )


@router.post("/{server_id}/health-check", response_model=MCPHealthCheckOut)
async def health_check(
    server_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> MCPHealthCheckOut:
    """서버 연결 + 도구 목록 조회. 결과를 last_health_*에 기록."""
    # 도구마다 런타임과 같은 정화·노출 판정을 싣는다(`_tool_health`)
    server = await db.get(MCPServer, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")

    from biznotes_core.services.agents.mcp_client import list_tools

    server.last_health_check = datetime.now(UTC)
    try:
        tools = await list_tools(server)
        server.last_health_ok = True
        await db.commit()
    except Exception as e:
        server.last_health_ok = False
        await db.commit()
        return MCPHealthCheckOut(ok=False, error=str(e)[:500])
    pins = (await tool_pin_states(db, [server.id])).get(server.id, {})
    return MCPHealthCheckOut(ok=True, tools=[_tool_health(server.id, t, pins) for t in tools])
