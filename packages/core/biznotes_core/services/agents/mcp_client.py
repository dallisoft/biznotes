"""MCP 클라이언트 — 어드민 등록 서버의 도구 디스커버리/호출.

transport: stdio(명령 실행) | sse | streamable_http
stdio 명령은 어드민만 등록 가능하며 워커 컨테이너 내에서 실행된다.

**stdio 격리(#139 A)**: 워커 환경변수(DB URL·암호화 키·LLM 키)를 물려주지 않는다. 자식에는 MCP SDK의
안전 기본 집합(HOME·LOGNAME·PATH·SHELL·TERM·USER)과 관리자가 등록한 `env_encrypted`만 간다(SDK가
합친다). 작업 디렉터리는 세션마다 만든 빈 임시 디렉터리다 — 워커 cwd의 `.env`를 상대 경로로 읽지
못한다. 같은 uid라 `/proc/<워커 PID>/environ`·`HOME` 아래 파일은 여전히 읽힐 수 있으므로(잔여 위험),
일반 사용자에게 연 stdio 서버에는 파일·셸 계열 도구를 승인하지 않는다.

**호출자 컨텍스트(#162)**: 코어 실행 경로는 `tools/call`의 `params._meta`에
`caller_meta(ctx, server)`를 싣는다. `MCP_META_RUN_ID`(run 식별자)는 모든 서버에 가고,
`MCP_META_USER_ID`(run 실행 사용자)는 **stdio 서버에만** 간다. 시스템 실행(`user_id=None`)이면
사용자 키가 없다. 값은 run 컨텍스트에서만 만들고, LLM·정의가 정하는 도구 `arguments`와 섞지
않는다. 역할은 싣지 않는다 — 서버가 "관리자면 아무 사용자나" 같은 우회를 만들지 않게, 서버는
`user_id` 본인 데이터만 돌려준다.

신뢰 범위: `_meta`의 사용자 id는 코어가 **주장**하는 값이고 서명하지 않는다. 이 값을 **인가 근거**로
쓸 수 있는 것은 코어가 호출마다 띄우는 stdio 자식뿐이다(같은 신뢰 경계라 서명이 막을 위협이 없다).
명령이 원격 브리지(`mcp-remote`·`ssh`·`docker run` 등)면 신뢰는 그 브리지의 전송으로 넘어간다.
sse·streamable_http에는 사용자 id를 보내지 않는다 — 코어는 HTTP 인증 헤더를 보내지 않고, 네트워크
격리는 워커 안의 LLM 제어 egress(`ssrf_allow_private=true`일 때 `http_request` 도구·`http` 노드)와
다른 stdio 자식을 막지 못해 `_meta`를 위조할 수 있다. 외부 서버로 내부 사용자 식별자를 내보내지
않는 효과도 있다. `run_id`는 제품이 정한 실행 식별자이고 `AgentRun` 행이 있다고 보장하지 않는다
(코어 감사 행 `mcp.call`의 `run_id`와 같은 값) — 감사·상관용이다.

서버는 `ctx.request_context.meta`(없으면 None)의 `model_extra.get(MCP_META_USER_ID)`로 읽는다.
"""

import json
import shlex
import tempfile
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from biznotes_core.core.crypto import decrypt
from biznotes_core.models import MCPServer

if TYPE_CHECKING:
    from mcp.types import Tool

    from biznotes_core.services.agents.context import RunContext

# `tools/call` `_meta` 키 — MCP `<prefix>/<name>` 이름공간. 접두어는 스펙 권고대로 reverse-DNS
# (저장소 github.com/dalli/biznotes)다. 서버는 이 상수의 문자열로 읽는다. 공개 계약이라 바꾸면
# 이 키를 읽는 MCP 서버도 함께 바꿔야 한다
MCP_META_RUN_ID = "io.github.dalli.biznotes/run_id"
MCP_META_USER_ID = "io.github.dalli.biznotes/user_id"


class MCPClientError(Exception):
    pass


def _server_env(server: MCPServer) -> dict[str, str] | None:
    """관리자가 등록한 env만 돌려준다. SDK가 안전 기본 집합과 합친다(None이면 기본 집합만)."""
    if not server.env_encrypted:
        return None
    return {str(k): str(v) for k, v in json.loads(decrypt(server.env_encrypted)).items()}


@asynccontextmanager
async def open_session(server: MCPServer):
    from mcp import ClientSession

    if server.transport == "stdio":
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        parts = shlex.split(server.endpoint_or_command)
        # Celery 워커는 sys.stderr를 LoggingProxy(fileno 없음)로 교체하므로
        # 서브프로세스 stderr 대상을 실제 파일 객체로 지정해야 한다
        import os
        import sys

        errlog = sys.__stderr__
        if errlog is None or not hasattr(errlog, "fileno"):
            errlog = open(os.devnull, "w")  # noqa: SIM115, ASYNC230
        # 정리 실패(자식이 남긴 파일)가 이미 끝난 호출 결과를 예외로 바꾸지 않게 한다
        workspace = tempfile.TemporaryDirectory(prefix="mcp-stdio-", ignore_cleanup_errors=True)
        with workspace as workdir:
            params = StdioServerParameters(
                command=parts[0], args=parts[1:], env=_server_env(server), cwd=workdir
            )
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
    elif server.transport == "sse":
        from mcp.client.sse import sse_client

        async with sse_client(server.endpoint_or_command) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    elif server.transport == "streamable_http":
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(server.endpoint_or_command) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:
        raise MCPClientError(f"지원하지 않는 transport: {server.transport}")


# 도구 annotations 중 보존하는 힌트(MCP 스펙 ToolAnnotations). 순서는 결과 dict 키 순서다.
_TOOL_ANNOTATION_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def _tool_title(tool: "Tool") -> str | None:
    """표시 이름 — 스펙 우선순위대로 `Tool.title`, 없으면 `annotations.title`(옛 스펙 서버).

    빈 문자열·공백뿐인 값은 없는 것으로 본다. 둘 다 없으면 None이다. `name`으로 채우지 않는다.
    """
    annotations = tool.annotations
    for title in (tool.title, annotations.title if annotations is not None else None):
        if isinstance(title, str) and title.strip():
            return title
    return None


def _tool_annotations(tool: "Tool") -> dict[str, bool | None]:
    """힌트 4개를 항상 담는다. 서버가 밝히지 않은 값은 None이다(스펙 기본값을 추정하지 않는다).

    SDK(pydantic 느슨한 모드)가 `"yes"`·`1` 같은 값을 이미 bool로 바꿔 둔다 — 참고용 값이라
    되돌리지 않는다. bool 검사는 SDK 밖에서 만든 객체에 대한 반환 타입 보장이다.
    """
    annotations = tool.annotations
    hints: dict[str, bool | None] = {}
    for key in _TOOL_ANNOTATION_HINTS:
        value = getattr(annotations, key, None) if annotations is not None else None
        hints[key] = value if isinstance(value, bool) else None
    return hints


async def list_tools(server: MCPServer) -> list[dict[str, Any]]:
    """서버가 광고하는 도구 목록.

    각 항목의 키는 `name`·`title`·`description`·`input_schema`·`annotations`다. `annotations`는
    `readOnlyHint`·`destructiveHint`·`idempotentHint`·`openWorldHint` 4개 키를 항상 담고, 값은
    `bool | None`이다(None = 서버가 밝히지 않음). `title`은 `str | None`이다.

    `title`·`annotations`(#146)는 **서버의 자기 신고라 신뢰하지 않는다** — 관리자 승인 화면의
    참고로만 쓴다. 승인 판정(`mcp_exposure.approved_tools`·`check_mcp_call`)과 디스커버리 스펙
    합성은 이 값을 보지 않는다.
    """
    async with open_session(server) as session:
        result = await session.list_tools()
        return [
            {
                "name": t.name,
                "title": _tool_title(t),
                "description": t.description or "",
                "input_schema": t.inputSchema,
                "annotations": _tool_annotations(t),
            }
            for t in result.tools
        ]


def caller_meta(ctx: "RunContext", server: MCPServer) -> dict[str, str]:
    """run 컨텍스트 → `tools/call` `_meta`(#162).

    - `run_id`: 모든 서버. 값이 없으면 키를 넣지 않는다.
    - `user_id`: stdio 서버에만. 시스템 실행(`user_id=None`)이면 넣지 않는다 — 빈 값·가짜 값으로
      채우지 않는다. HTTP 계열에 보내지 않는 이유는 모듈 docstring의 신뢰 범위를 본다.
    """
    meta: dict[str, str] = {}
    if ctx.run_id is not None:
        meta[MCP_META_RUN_ID] = str(ctx.run_id)
    if ctx.user_id is not None and server.transport == "stdio":
        meta[MCP_META_USER_ID] = str(ctx.user_id)
    return meta


async def call_tool(
    server: MCPServer,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    meta: dict[str, Any] | None = None,
) -> str:
    """도구 호출. 결과 text 콘텐츠를 줄바꿈으로 잇고, `isError`면 `MCPClientError`.

    `meta`는 `params._meta`로 간다. 호출자가 책임지는 값이며, 코어 경로는
    `caller_meta(ctx, server)`만 넘긴다. 키는 MCP `<prefix>/<name>` 규칙을 따른다.
    `meta=None`이면 SDK에 넘기지 않아 `_meta`가 wire에 없다(#162 이전과 같다).
    `{}`는 빈 `_meta: {}`를 보낸다.
    """
    async with open_session(server) as session:
        if meta is None:
            result = await session.call_tool(tool_name, arguments=arguments)
        else:
            result = await session.call_tool(tool_name, arguments=arguments, meta=meta)
        parts: list[str] = []
        for content in result.content:
            if content.type == "text":
                parts.append(content.text)
            else:
                parts.append(f"[{content.type} content]")
        if result.isError:
            raise MCPClientError("\n".join(parts) or "MCP 도구 실행 실패")
        return "\n".join(parts)
