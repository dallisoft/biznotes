"""Independent MCP wire fixture; deliberately imports no MCP SDK code."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_TRACE_WARNED = False


def _append_trace(direction: str, payload: bytes | str) -> None:
    global _TRACE_WARNED
    trace_path = os.environ.get("MCP_TRACE_FILE")
    if not trace_path:
        # Never fail silently: tracing is this fixture's contract with the tests, and a
        # silently-disabled trace surfaces later as an unrelated FileNotFoundError in
        # the caller (AGENTS.md section 6.2-3).
        if not _TRACE_WARNED:
            _TRACE_WARNED = True
            print(
                "protocol_server: MCP_TRACE_FILE is not set; wire tracing is DISABLED "
                "and no evidence will be recorded",
                file=sys.stderr,
                flush=True,
            )
        return
    raw = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    with Path(trace_path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"direction": direction, "wire": raw}, ensure_ascii=False) + "\n")


def _annotated_tools(revision: str) -> list[dict[str, Any]]:
    """`annotations`(2025-03-26+)와 최상위 `title`(2025-06-18+)을 그 리비전 스펙대로 싣는다."""
    if revision < "2025-03-26":
        raise SystemExit("annotated 시나리오는 2025-03-26 이상 리비전만 쓴다(annotations 없음)")
    schema = {"type": "object", "properties": {}}
    search: dict[str, Any] = {
        "name": "search",
        "description": "read-only search",
        "inputSchema": schema,
        "annotations": {"title": "Legacy Search", "readOnlyHint": True, "openWorldHint": False},
    }
    if revision >= "2025-06-18":
        search["title"] = "Search"
    return [
        search,
        {
            "name": "drop",
            "description": "destructive",
            "inputSchema": schema,
            "annotations": {"destructiveHint": True},
        },
        {"name": "plain", "description": "no annotations", "inputSchema": schema},
    ]


def _response(message: dict[str, Any], revision: str, scenario: str) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if scenario == "timeout" and method == "initialize":
        time.sleep(3600)
    if request_id is None:
        return None
    if method == "initialize":
        selected = "2099-01-01" if scenario == "version-mismatch" else revision
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": selected,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "independent-fixture", "version": "1"},
            },
        }
    if method == "tools/list" and scenario == "annotated":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": _annotated_tools(revision)}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [
                    {
                        "name": "echo",
                        "description": "wire echo",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                        },
                    }
                ]
            },
        }
    if method == "tools/call":
        text = str(message.get("params", {}).get("arguments", {}).get("text", ""))
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": False},
        }
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"unsupported method: {method}"},
    }


def run_stdio(revision: str, scenario: str) -> None:
    _append_trace("process", json.dumps({"pid": os.getpid()}))
    for raw in sys.stdin.buffer:
        _append_trace("client-to-server", raw)
        message = json.loads(raw)
        response = _response(message, revision, scenario)
        if response is not None:
            wire = json.dumps(response, separators=(",", ":")).encode() + b"\n"
            _append_trace("server-to-client", wire)
            sys.stdout.buffer.write(wire)
            sys.stdout.buffer.flush()


def run_http(revision: str, status: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("content-length", "0"))
            raw = self.rfile.read(length)
            _append_trace("client-to-server", raw)
            if status in (401, 403):
                body = json.dumps({"error": "fixture authorization failure"}).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            response = _response(json.loads(raw), revision, "valid")
            if response is None:
                self.send_response(202)
                self.send_header("content-length", "0")
                self.end_headers()
                return
            body = json.dumps(response, separators=(",", ":")).encode()
            _append_trace("server-to-client", body)
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("mcp-session-id", "fixture-session")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(405)
            self.send_header("content-length", "0")
            self.end_headers()

        def do_DELETE(self) -> None:  # noqa: N802
            self.send_response(204)
            self.send_header("content-length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    print(json.dumps({"port": server.server_port, "pid": os.getpid()}), flush=True)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--revision", required=True)
    parser.add_argument(
        "--scenario",
        choices=("valid", "version-mismatch", "timeout", "annotated"),
        default="valid",
    )
    parser.add_argument("--status", type=int, default=200)
    args = parser.parse_args()
    if args.transport == "stdio":
        run_stdio(args.revision, args.scenario)
    else:
        run_http(args.revision, args.status)


if __name__ == "__main__":
    main()
