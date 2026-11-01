"""stdio 격리 탐침 MCP 서버(#139 A).

자식 프로세스가 본 환경변수 키·값 일부와 작업 디렉터리를 돌려준다.
"""

import json
import os

from mcp.server.fastmcp import FastMCP

server = FastMCP("probe")


@server.tool()
def probe() -> str:
    return json.dumps(
        {
            "env_keys": sorted(os.environ),
            "api_token": os.environ.get("PROBE_API_TOKEN"),
            "cwd": os.getcwd(),
            "cwd_entries": sorted(os.listdir(".")),
        }
    )


if __name__ == "__main__":
    server.run("stdio")
