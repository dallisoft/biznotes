"""호출자 컨텍스트 에코 MCP 서버(#162).

`tools/call`이 받은 `params._meta`의 extra 키(없으면 null)와 `arguments`를 JSON으로 그대로 돌려준다.
인자 검증으로 키가 걸러지지 않게 저수준 서버를 쓴다.
"""

import json

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

server: Server = Server("echo-meta")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(name="whoami", description="echo caller meta", inputSchema={"type": "object"})
    ]


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    meta = server.request_context.meta
    payload = {"meta": meta.model_extra if meta is not None else None, "arguments": arguments}
    return [types.TextContent(type="text", text=json.dumps(payload))]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
