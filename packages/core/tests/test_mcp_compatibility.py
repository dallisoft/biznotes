"""P1 MCP wire characterization against an SDK-independent local fixture.

Two kinds of assertion live here and they must not be confused:

1. *Characterization* of the **current** product client (`biznotes_core.services.agents.mcp_client`,
   SDK `mcp==1.27.2`) over a real subprocess/socket wire. These pin today's behaviour,
   including behaviour that is not yet standard-compliant.
2. *Conformance* of the frozen fixture material (`fixtures/mcp/manifest.json`,
   `fixtures/mcp/traces.json`) against the **vendored official schemas**
   (`fixtures/mcp/schemas/<revision>.json`), whose bytes are verified offline against the
   SHA-256 pinned in the manifest. Nothing here ever touches the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import httpx
import pytest
from jsonschema import Draft7Validator, Draft202012Validator

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "mcp"
PROTOCOL_SERVER = FIXTURE_ROOT / "protocol_server.py"
LEGACY_REVISIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
ALL_REVISIONS = (*LEGACY_REVISIONS, "2026-07-28")
TOOL_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

# Frozen trace method -> official schema definition name. Used to validate each pinned
# wire message against the *specific* definition the spec assigns to it, rather than a
# permissive union that would accept almost anything.
REQUEST_DEF = {
    "initialize": "InitializeRequest",
    "tools/list": "ListToolsRequest",
    "tools/call": "CallToolRequest",
    "server/discover": "DiscoverRequest",
}
NOTIFICATION_DEF = {"notifications/initialized": "InitializedNotification"}
RESULT_DEF = {
    "initialize": "InitializeResult",
    "tools/list": "ListToolsResult",
    "tools/call": "CallToolResult",
    "server/discover": "DiscoverResult",
}

# Deterministic-cancellation budgets. These are *upper bounds on waiting*, never
# deadlines that a passing run is expected to approach: the test's ordering is
# established causally (observe the child's own trace lines) and only then cancelled.
CHILD_START_TIMEOUT_S = 30.0
CHILD_REAP_TIMEOUT_S = 15.0
_POLL_S = 0.02


def _server(transport: str, endpoint: str) -> SimpleNamespace:
    """테스트가 `MCP_TRACE_FILE`을 정했으면 서버 env(`env_encrypted`)로 넘긴다.

    stdio 자식은 워커 환경변수를 물려받지 않는다(#139 A) — 관리자가 등록한 env만 간다.
    """
    from biznotes_core.core.crypto import encrypt

    trace = os.environ.get("MCP_TRACE_FILE")
    return SimpleNamespace(
        transport=transport,
        endpoint_or_command=endpoint,
        env_encrypted=encrypt(json.dumps({"MCP_TRACE_FILE": trace})) if trace else None,
    )


def _trace_events(trace_path: Path, direction: str) -> list[dict[str, Any]]:
    if not trace_path.exists():
        return []
    events = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event["direction"] != direction:
            continue
        events.append(json.loads(event["wire"]))
    return events


def _wire_messages(trace_path: Path, direction: str) -> list[dict[str, Any]]:
    return _trace_events(trace_path, direction)


# ---------------------------------------------------------------------------
# Offline conformance of the frozen fixture material
# ---------------------------------------------------------------------------


def _manifest() -> dict[str, Any]:
    return json.loads((FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))


def _traces() -> dict[str, Any]:
    return json.loads((FIXTURE_ROOT / "traces.json").read_text(encoding="utf-8"))


def _load_vendored_schema(revision: str) -> tuple[dict[str, Any], str]:
    entry = next(e for e in _manifest()["schemas"] if e["revision"] == revision)
    schema = json.loads(
        (FIXTURE_ROOT / entry["vendored_path"]).read_text(encoding="utf-8")
    )
    defs_key = "$defs" if "$defs" in schema else "definitions"
    return schema, defs_key


def _def_validator(
    schema: dict[str, Any], defs_key: str, definition: str
) -> Draft202012Validator | Draft7Validator:
    assert definition in schema[defs_key], (
        f"pinned schema has no definition {definition!r}; the trace claims a message "
        "shape this revision does not define"
    )
    subschema = {
        "$schema": schema["$schema"],
        "$ref": f"#/{defs_key}/{definition}",
        defs_key: schema[defs_key],
    }
    cls = Draft202012Validator if "2020-12" in schema["$schema"] else Draft7Validator
    return cls(subschema)


@pytest.mark.parametrize("revision", ALL_REVISIONS)
def test_vendored_schema_copies_match_pinned_sha256(revision: str) -> None:
    """The pinned SHA-256 is tied to actual bytes inside the repo, offline.

    Without this, `manifest.json` could pin a hash that matches nothing (the previous
    baseline only checked `len(sha256) == 64`, which is true of any 64-char string).
    """
    entry = next(e for e in _manifest()["schemas"] if e["revision"] == revision)
    vendored = FIXTURE_ROOT / entry["vendored_path"]
    assert vendored.is_file(), f"vendored schema missing: {entry['vendored_path']}"
    digest = hashlib.sha256(vendored.read_bytes()).hexdigest()
    assert digest == entry["sha256"], (
        f"{revision}: vendored copy {digest} != pinned {entry['sha256']}; the frozen "
        "revision pin is no longer self-consistent"
    )
    # And the vendored bytes really are the schema the URL names.
    assert entry["url"].endswith(f"/schema/{revision}/schema.json")
    assert entry["url"].split("/")[5] == _manifest()["source_repository"]["commit"]


@pytest.mark.parametrize("revision", ALL_REVISIONS)
def test_frozen_traces_conform_to_pinned_schemas(revision: str) -> None:
    """Every frozen wire message validates against the official schema it claims.

    This is the gate that makes `traces.json` a *baseline* rather than a claim. P3
    implementers may treat these traces as the shape a conforming peer accepts.
    """
    schema, defs_key = _load_vendored_schema(revision)
    entry = next(e for e in _traces()["valid"] if e["revision"] == revision)

    method_by_id: dict[Any, str] = {}
    for message in entry["client"]:
        method = message["method"]
        definition = REQUEST_DEF.get(method) or NOTIFICATION_DEF[method]
        if "id" in message:
            method_by_id[message["id"]] = method
        errors = sorted(
            _def_validator(schema, defs_key, definition).iter_errors(message),
            key=lambda error: list(error.path),
        )
        assert not errors, (
            f"{revision} client {method} violates {definition}: "
            + "; ".join(f"{list(e.path)}: {e.message}" for e in errors)
        )

    for message in entry["server"]:
        method = method_by_id[message["id"]]
        definition = RESULT_DEF[method]
        errors = sorted(
            _def_validator(schema, defs_key, definition).iter_errors(message["result"]),
            key=lambda error: list(error.path),
        )
        assert not errors, (
            f"{revision} server result for {method} violates {definition}: "
            + "; ".join(f"{list(e.path)}: {e.message}" for e in errors)
        )


def test_modern_revision_trace_carries_per_request_version_negotiation() -> None:
    """PLAN section 3 names per-request version/capabilities as what defines the modern era.

    `test_frozen_traces_conform_to_pinned_schemas` already enforces this through
    `RequestMetaObject.required`; this test states the intent so a future edit that
    removes the mechanism fails with a message that says *why* it mattered.
    """
    schema, defs_key = _load_vendored_schema("2026-07-28")
    required = set(schema[defs_key]["RequestMetaObject"]["required"])
    assert required == {
        "io.modelcontextprotocol/clientCapabilities",
        "io.modelcontextprotocol/protocolVersion",
    }

    reserved_keys = set(schema[defs_key]["RequestMetaObject"]["properties"])
    entry = next(e for e in _traces()["valid"] if e["revision"] == "2026-07-28")
    for message in entry["client"]:
        meta = message["params"]["_meta"]
        assert required <= set(meta), f"{message['method']} drops per-request negotiation"
        assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
        # The `io.modelcontextprotocol/` prefix is reserved by the spec; the trace must
        # not invent keys under it (the previous baseline used a key that appears zero
        # times in the pinned schema).
        invented = {
            key
            for key in meta
            if key.startswith("io.modelcontextprotocol/") and key not in reserved_keys
        }
        assert not invented, f"invented reserved-prefix _meta keys: {sorted(invented)}"


def test_frozen_revision_manifest_and_transport_trace_matrix() -> None:
    manifest = _manifest()
    traces = _traces()

    expected_revisions = set(ALL_REVISIONS)
    assert {entry["revision"] for entry in manifest["schemas"]} == expected_revisions
    assert {entry["revision"] for entry in traces["valid"]} == expected_revisions

    transports = {
        transport for entry in manifest["schemas"] for transport in entry["transport"]
    }
    assert transports == {"stdio", "http+sse", "streamable-http"}
    failures = {entry["id"]: entry for entry in traces["failure"]}
    assert set(failures) >= {"version-mismatch", "timeout", "unauthorized", "forbidden"}
    # AC1 asks for mismatch/no-response coverage across the legacy revisions, not just one.
    assert set(failures["version-mismatch"]["revisions"]) == set(LEGACY_REVISIONS)
    assert set(failures["timeout"]["revisions"]) == set(LEGACY_REVISIONS)

    modern = next(entry for entry in traces["valid"] if entry["revision"] == "2026-07-28")
    assert modern["client"][0]["method"] == "server/discover"
    assert all(message.get("method") != "initialize" for message in modern["client"])


# ---------------------------------------------------------------------------
# Characterization of the current product client over a real wire
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("revision", LEGACY_REVISIONS)
async def test_current_stdio_client_completes_legacy_lifecycle_over_real_wire(
    revision: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biznotes_core.services.agents.mcp_client import call_tool, list_tools

    trace_path = tmp_path / f"{revision}.jsonl"
    monkeypatch.setenv("MCP_TRACE_FILE", str(trace_path))
    endpoint = f"{sys.executable} {PROTOCOL_SERVER} --revision {revision}"

    assert await list_tools(_server("stdio", endpoint)) == [
        {
            "name": "echo",
            "title": None,
            "description": "wire echo",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
            "annotations": dict.fromkeys(TOOL_HINTS),
        }
    ]
    assert await call_tool(_server("stdio", endpoint), "echo", {"text": "wire-byte"}) == (
        "wire-byte"
    )

    methods = [
        message.get("method")
        for message in _wire_messages(trace_path, "client-to-server")
    ]
    assert methods == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "initialize",
        "notifications/initialized",
        "tools/call",
        "tools/list",
    ]

    # Each parameter must be distinguishable: the client accepted *this* revision as the
    # negotiated protocol version. Without this the four parameters are four labels on
    # one behaviour, and an SDK that silently dropped one revision would still be green.
    negotiated = [
        message["result"]["protocolVersion"]
        for message in _wire_messages(trace_path, "server-to-client")
        if "protocolVersion" in message.get("result", {})
    ]
    assert negotiated and set(negotiated) == {revision}


@pytest.mark.parametrize("revision", ("2025-03-26", "2025-06-18", "2025-11-25"))
async def test_list_tools_keeps_title_and_annotations_over_real_wire(
    revision: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#146: 서버가 밝힌 힌트만 그대로 옮기고, 밝히지 않은 힌트는 None이다(기본값 추정 없음).

    `title`은 `Tool.title`(2025-06-18+)을 먼저, 없으면 옛 스펙의 `annotations.title`을 쓴다.
    """
    from biznotes_core.services.agents.mcp_client import list_tools

    trace_path = tmp_path / f"annotated-{revision}.jsonl"
    monkeypatch.setenv("MCP_TRACE_FILE", str(trace_path))
    endpoint = f"{sys.executable} {PROTOCOL_SERVER} --revision {revision} --scenario annotated"

    tools = {tool["name"]: tool for tool in await list_tools(_server("stdio", endpoint))}

    assert tools["search"]["title"] == ("Legacy Search" if revision == "2025-03-26" else "Search")
    assert tools["search"]["annotations"] == {
        "readOnlyHint": True,
        "destructiveHint": None,
        "idempotentHint": None,
        "openWorldHint": False,
    }
    assert tools["drop"]["title"] is None
    assert tools["drop"]["annotations"] == {**dict.fromkeys(TOOL_HINTS), "destructiveHint": True}
    assert tools["plain"]["annotations"] == dict.fromkeys(TOOL_HINTS)
    for tool in tools.values():
        assert list(tool) == ["name", "title", "description", "input_schema", "annotations"]
        assert list(tool["annotations"]) == list(TOOL_HINTS)

    # 픽스처가 보낸 결과가 그 리비전의 공식 스키마를 따르는지 본다. 스키마는 정의 밖 필드를
    # 막지 않으므로(additionalProperties 없음) 필드 이름도 그 리비전 정의 안에 있는지 따로 본다.
    schema, defs_key = _load_vendored_schema(revision)
    listed = [
        message["result"]
        for message in _wire_messages(trace_path, "server-to-client")
        if "tools" in message.get("result", {})
    ]
    assert len(listed) == 1
    errors = list(_def_validator(schema, defs_key, "ListToolsResult").iter_errors(listed[0]))
    assert errors == []
    defs = schema[defs_key]
    for tool in listed[0]["tools"]:
        assert set(tool) <= set(defs["Tool"]["properties"])
        assert set(tool.get("annotations", {})) <= set(defs["ToolAnnotations"]["properties"])


@pytest.mark.xfail(
    strict=True,
    reason="P3: the v1.27.2 client cannot negotiate the modern 2026-07-28 protocol",
)
async def test_current_client_supports_2026_07_28_over_real_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biznotes_core.services.agents.mcp_client import list_tools

    trace_path = tmp_path / "modern.jsonl"
    monkeypatch.setenv("MCP_TRACE_FILE", str(trace_path))
    endpoint = f"{sys.executable} {PROTOCOL_SERVER} --revision 2026-07-28"

    tools = await list_tools(_server("stdio", endpoint))
    assert tools[0]["name"] == "echo"


@pytest.mark.parametrize("revision", LEGACY_REVISIONS)
async def test_current_client_rejects_protocol_version_mismatch(
    revision: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biznotes_core.services.agents.mcp_client import list_tools

    trace_path = tmp_path / f"mismatch-{revision}.jsonl"
    monkeypatch.setenv("MCP_TRACE_FILE", str(trace_path))
    endpoint = (
        f"{sys.executable} {PROTOCOL_SERVER} --revision {revision} "
        "--scenario version-mismatch"
    )

    with pytest.raises(BaseExceptionGroup) as caught:
        await list_tools(_server("stdio", endpoint))
    runtime_errors = caught.value.subgroup(lambda error: isinstance(error, RuntimeError))
    assert runtime_errors is not None
    assert "Unsupported protocol version" in repr(runtime_errors)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _await_child_engaged(trace_path: Path) -> int:
    """Block until the stdio fixture child has started *and* received `initialize`.

    Returns its pid. This is the causal precondition for cancelling: it distinguishes
    "the child never started" from "the child is hanging as designed", which a bare
    time budget conflates into one indistinguishable failure.
    """
    deadline = CHILD_START_TIMEOUT_S / _POLL_S
    pid: int | None = None
    engaged = False
    for _ in range(int(deadline)):
        process_events = _trace_events(trace_path, "process")
        if process_events:
            pid = int(process_events[0]["pid"])
        if pid is not None:
            methods = [
                message.get("method")
                for message in _trace_events(trace_path, "client-to-server")
            ]
            engaged = "initialize" in methods
        if pid is not None and engaged:
            return pid
        await anyio.sleep(_POLL_S)
    raise AssertionError(
        "stdio fixture child never engaged within "
        f"{CHILD_START_TIMEOUT_S}s (pid seen: {pid!r}, initialize received: {engaged}); "
        "this is a fixture/startup failure, not a product cancellation failure"
    )


async def test_current_client_has_no_deadline_and_external_cancel_reaps_stdio_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pinned baseline: the current client has **no deadline of its own**.

    `open_session()`, `list_tools()` and `call_tool()` accept no timeout argument and
    construct `ClientSession(read, write)` with defaults, so against a server that never
    answers `initialize` the call hangs indefinitely. What this test fixes is therefore
    *not* "the product handles timeouts" — it is:

      * the product does not self-terminate (`completed` stays False, and the call is
        still pending when we cancel it), and
      * when an **external** caller cancels, the stdio child process is reaped.

    Adding a client-side deadline is P3a work (PLAN section 3 puts a 30s catalog
    deadline there). Do not read a pass here as that axis being covered.

    Determinism note: ordering is established causally — we wait for the child's own
    trace lines proving it started and received `initialize`, and only then cancel. The
    constants above are generous upper bounds on waiting, not timing thresholds a
    passing run is expected to approach, so suite load and test ordering cannot flip
    the result.
    """
    from biznotes_core.services.agents.mcp_client import list_tools

    trace_path = tmp_path / "timeout.jsonl"
    monkeypatch.setenv("MCP_TRACE_FILE", str(trace_path))
    endpoint = (
        f"{sys.executable} {PROTOCOL_SERVER} --revision 2025-11-25 --scenario timeout"
    )

    completed = False

    async def _hang() -> None:
        nonlocal completed
        await list_tools(_server("stdio", endpoint))
        completed = True

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(_hang)
        child_pid = await _await_child_engaged(trace_path)
        assert not completed, (
            "the fixture never answers initialize, so a completed call would mean the "
            "characterization target changed"
        )
        task_group.cancel_scope.cancel()

    assert not completed, "product client returned from a server that never responded"

    for _ in range(int(CHILD_REAP_TIMEOUT_S / _POLL_S)):
        if not _pid_exists(child_pid):
            break
        await anyio.sleep(_POLL_S)
    assert not _pid_exists(child_pid), f"stdio fixture child leaked: {child_pid}"


@pytest.mark.parametrize("revision", LEGACY_REVISIONS)
async def test_current_client_has_no_deadline_across_legacy_revisions(
    revision: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-deadline baseline holds on every legacy revision, not just one.

    AC1 asks for the no-response case across the pinned revisions. Same baseline as
    above: no product deadline, external cancellation reaps the child.
    """
    from biznotes_core.services.agents.mcp_client import list_tools

    trace_path = tmp_path / f"timeout-{revision}.jsonl"
    monkeypatch.setenv("MCP_TRACE_FILE", str(trace_path))
    endpoint = (
        f"{sys.executable} {PROTOCOL_SERVER} --revision {revision} --scenario timeout"
    )

    completed = False

    async def _hang() -> None:
        nonlocal completed
        await list_tools(_server("stdio", endpoint))
        completed = True

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(_hang)
        child_pid = await _await_child_engaged(trace_path)
        task_group.cancel_scope.cancel()

    assert not completed
    for _ in range(int(CHILD_REAP_TIMEOUT_S / _POLL_S)):
        if not _pid_exists(child_pid):
            break
        await anyio.sleep(_POLL_S)
    assert not _pid_exists(child_pid), f"stdio fixture child leaked: {child_pid}"


def _start_http_fixture(
    tmp_path: Path, status: int = 200
) -> tuple[subprocess.Popen[str], str, Path]:
    trace_path = tmp_path / f"http-{status}.jsonl"
    env = os.environ.copy()
    env["MCP_TRACE_FILE"] = str(trace_path)
    process = subprocess.Popen(
        [
            sys.executable,
            str(PROTOCOL_SERVER),
            "--transport",
            "streamable-http",
            "--revision",
            "2025-11-25",
            "--status",
            str(status),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert process.stdout is not None
    ready = json.loads(process.stdout.readline())
    return process, f"http://127.0.0.1:{ready['port']}/mcp", trace_path


async def test_current_streamable_http_client_completes_lifecycle_over_real_wire(
    tmp_path: Path,
) -> None:
    from biznotes_core.services.agents.mcp_client import list_tools

    process, endpoint, trace_path = _start_http_fixture(tmp_path)
    try:
        tools = await list_tools(_server("streamable_http", endpoint))
        assert tools[0]["name"] == "echo"
        methods = [
            message.get("method")
            for message in _wire_messages(trace_path, "client-to-server")
        ]
        assert methods[:3] == [
            "initialize",
            "notifications/initialized",
            "tools/list",
        ]
    finally:
        process.terminate()
        process.wait(timeout=3)


@pytest.mark.parametrize("status", (401, 403))
async def test_http_authorization_failure_is_not_hidden_by_era_downgrade(
    status: int, tmp_path: Path
) -> None:
    from biznotes_core.services.agents.mcp_client import list_tools

    process, endpoint, trace_path = _start_http_fixture(tmp_path, status=status)
    try:
        with pytest.raises(BaseExceptionGroup) as caught:
            await list_tools(_server("streamable_http", endpoint))
        status_errors = caught.value.subgroup(
            lambda error: isinstance(error, httpx.HTTPStatusError)
        )
        assert status_errors is not None
        assert str(status) in repr(status_errors)
        methods = [
            message.get("method")
            for message in _wire_messages(trace_path, "client-to-server")
        ]
        assert methods == ["initialize"]
    finally:
        process.terminate()
        process.wait(timeout=3)


def test_protocol_server_fixture_reports_missing_trace_configuration() -> None:
    """No silent failure path (AGENTS.md section 6.2-3).

    Tracing is this fixture's contract with the tests. If `MCP_TRACE_FILE` is unset the
    fixture must say so on stderr instead of running with tracing silently disabled and
    surfacing later as an unrelated `FileNotFoundError` in the caller.
    """
    env = {k: v for k, v in os.environ.items() if k != "MCP_TRACE_FILE"}
    process = subprocess.run(
        [sys.executable, str(PROTOCOL_SERVER), "--revision", "2025-11-25"],
        input="",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert "MCP_TRACE_FILE" in process.stderr
