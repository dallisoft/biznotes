"""데모 MCP 서버 (stdio) — MCP 연동 검증/시연용.

워커 컨테이너 내부에서 `python -m scripts.demo_mcp_server`로 실행된다.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("biznotes-demo")


@mcp.tool()
def word_count(text: str) -> str:
    """텍스트의 단어/문자 수를 계산합니다."""
    words = len(text.split())
    chars = len(text)
    return f"단어 수: {words}, 문자 수: {chars}"


@mcp.tool()
def echo_upper(text: str) -> str:
    """텍스트를 대문자로 변환해 반환합니다."""
    return text.upper()


@mcp.tool()
def reverse_text(text: str) -> str:
    """텍스트를 글자 단위로 뒤집어 반환합니다."""
    return text[::-1]


if __name__ == "__main__":
    mcp.run(transport="stdio")
