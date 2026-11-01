"""R0-6 — runtime.py 특성화(characterization) 테스트.

R2(runtime.py 기계적 모듈 분해) 전에 **현재 동작을 골든으로 고정**한다
(docs/plans/refactoring/PLAN.md R0-6, 인벤토리:
docs/plans/refactoring/r0-6-characterization-inventory.md).

원칙: "지금 동작이 정답" — 버그로 보이는 동작도 현재 출력 그대로 단언한다.
기존 커버리지(CR-R09)와 중복하지 않는 **부족분만** 작성:
  1. 공개 임포트 호환성 — 역참조 사이트(라우터·워커·tool_registry) 전수 심볼 계약
  2. NODE_EXECUTORS 노드 타입 집합 기준선
  3. kind_to_nodes tool/plugin/mcp 정규화 + 알 수 없는 kind + _topological_order 순서
  4. execute_agent 요약 fallback(텍스트 없음 → 아티팩트 목록)
  5. 로깅 부수효과 — 실패 경로 status="failed", AgentRun 없을 때 best-effort None
  6. _exec_http — 요청 렌더링/응답 형태/100k 절단/SSRF 차단
  7. shape 검증기 순수 함수(runtime.py:1539-1710) 대표 입력/출력 골든

외부 네트워크 호출 없음: HTTP 실행기는 monkeypatch로 고정 — 결정적(deterministic).
"""

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from biznotes_core.services.agents import graph as graph_mod
from biznotes_core.services.agents.runtime import (
    AgentRuntimeError,
    ApprovalRequired,
    RunContext,
    _exec_http,
    _log_node_finish,
    _log_node_start,
    _topological_order,
    execute_agent,
    kind_to_nodes,
)

# ---------- 1. 공개 임포트 호환성 (재수출 셸 전환 후에도 유지될 계약) ----------


def test_public_reverse_reference_imports_stay_stable():
    """역참조 사이트가 실제로 임포트하는 심볼 전수를 고정.

    R2에서 runtime.py가 재수출 셸로 바뀌어도 이 임포트들은 깨지지 않아야 한다.
    (사이트 목록: r0-6-characterization-inventory.md "공개 임포트 계약" 표)
    """
    import dataclasses

    from biznotes_core.services.agents.runtime import (  # noqa: F401
        NODE_EXECUTORS,  # 테스트 monkeypatch seam (test_agent_approvals_api.py:290)
        AgentRuntimeError,  # workflows.py / admin/agents.py / workers/tasks/agent.py
        ApprovalRequired,  # execute_agent → worker checkpoint 영속화 예외 계약
        RunContext,  # chat.py / workers/tasks/agent.py / tool_registry.py
        _dispatch_builtin_tool,  # tool_registry.py:257 (호출 시점 조회)
        _mask_tool_args,  # tool_registry.py:108
        _run_subagent,  # tool_registry.py:265 (호출 시점 조회)
        _topological_order,  # workflows.py / admin/agents.py
        agent_visible_to,  # workflows.py:24
        compute_cost,  # 노드 비용(services/agents)
        execute_agent,  # workers/tasks/agent.py / skills/builder.py
        kind_to_nodes,  # workflows.py / admin/agents.py
        run_react_node,  # chat.py:277
    )

    assert issubclass(AgentRuntimeError, Exception)
    assert issubclass(ApprovalRequired, Exception)
    assert dataclasses.is_dataclass(RunContext)
    # RunContext의 파생 컨텍스트 API — tool_registry/서브에이전트 경로가 의존.
    assert callable(RunContext.child)
    assert callable(RunContext.branch_parallel)
    for fn in (
        _dispatch_builtin_tool,
        _mask_tool_args,
        _run_subagent,
        _topological_order,
        agent_visible_to,
        compute_cost,
        execute_agent,
        kind_to_nodes,
        run_react_node,
    ):
        assert callable(fn)
    assert isinstance(NODE_EXECUTORS, dict)


def test_node_executors_registry_baseline():
    """NODE_EXECUTORS 노드 타입 집합을 골든으로 고정 — R2 분해 후에도 동일해야 한다."""
    from biznotes_core.services.agents.runtime import NODE_EXECUTORS

    assert set(NODE_EXECUTORS.keys()) == {
        "constant",
        "condition",
        "loop",
        "rag_search",
        "source_load",
        "source_digest",  # 코어 v0.25.0 #360
        "llm",
        "json_validate",
        "json_compose",  # 코어 v0.25.0 #361
        "citation_verify",
        "save_artifact",
        "save_sources",  # 코어 v0.28.0 #403
        "http",
        "image_generate",  # 코어 v0.23.0 #338
        "mcp_tool",
        "agent_call",
        "sub_workflow",
        "error_handler",
        "supervisor",
        "react_agent",
    }
    assert all(callable(v) for v in NODE_EXECUTORS.values())


# ---------- 2. 그래프 실행 — kind_to_nodes / _topological_order / 요약 fallback ----------


def test_kind_to_nodes_tool_and_plugin_golden():
    """kind tool/plugin은 동일하게 http→save_artifact 2노드로 정규화된다."""
    definition = {
        "http": {"url": "https://api.example.com/v1/run", "method": "GET"},
        "artifact_name": "resp.json",
    }
    for kind in ("tool", "plugin"):
        nodes, edges = kind_to_nodes(kind, definition)
        assert nodes == [
            {
                "id": "call",
                "type": "http",
                "url": "https://api.example.com/v1/run",
                "method": "GET",
            },
            {
                "id": "save",
                "type": "save_artifact",
                "name": "resp.json",
                "content": "{call.text}",
                "content_type": "application/json",
            },
        ]
        assert edges == [["call", "save"]]


def test_kind_to_nodes_mcp_golden():
    """kind mcp는 mcp_tool→save_artifact 2노드로 정규화 (arguments/이름 기본값 포함)."""
    nodes, edges = kind_to_nodes("mcp", {"server_id": "srv-1", "tool_name": "fetch"})
    assert nodes == [
        {
            "id": "call",
            "type": "mcp_tool",
            "server_id": "srv-1",
            "tool_name": "fetch",
            "arguments": {},
        },
        {
            "id": "save",
            "type": "save_artifact",
            "name": "mcp-result.md",
            "content": "{call.text}",
            "content_type": "text/markdown",
        },
    ]
    assert edges == [["call", "save"]]


def test_kind_to_nodes_unknown_kind_raises():
    with pytest.raises(AgentRuntimeError, match="알 수 없는 kind"):
        kind_to_nodes("not-a-kind", {})


def test_topological_order_golden():
    """위상정렬의 결정적 성질 고정: 체인 순서, 무엣지 삽입순 유지, 다이아몬드 경계, 순환 거부.

    (다이아몬드 중간 분기의 상대 순서는 set 순회에 의존해 해시 시드에 따라 달라질 수
    있으므로 — 현재 동작 그대로 — 집합 동등성만 고정한다.)
    """

    def _nodes(*ids):
        return [{"id": i} for i in ids]

    # 체인 a→b→c: 정확한 순서.
    assert _topological_order(_nodes("a", "b", "c"), [["a", "b"], ["b", "c"]]) == ["a", "b", "c"]
    # 엣지 없음: 노드 삽입 순서 그대로 유지.
    assert _topological_order(_nodes("z", "y", "x"), []) == ["z", "y", "x"]
    # 다이아몬드 a→(b,c)→d: a가 처음, d가 마지막, 중간은 {b,c}.
    order = _topological_order(
        _nodes("a", "b", "c", "d"), [["a", "b"], ["a", "c"], ["b", "d"], ["c", "d"]]
    )
    assert order[0] == "a" and order[-1] == "d" and set(order[1:3]) == {"b", "c"}
    # 순환은 AgentRuntimeError.
    with pytest.raises(AgentRuntimeError, match="순환"):
        _topological_order(_nodes("a", "b"), [["a", "b"], ["b", "a"]])


async def test_execute_agent_summary_falls_back_to_artifacts(db, user_a, _mock_runtime_deps):
    """마지막까지 text 출력이 없으면 요약은 '아티팩트 N개 생성: <이름들>' 형태로 고정."""
    definition = {
        "nodes": [
            {"id": "c", "type": "constant", "value": {"v": "본문"}},
            {"id": "save", "type": "save_artifact", "name": "out.txt", "content": "{c.v}"},
        ],
        "edges": [["c", "save"]],
    }
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, user_id=user_a.id
    )
    summary = await execute_agent(ctx, "workflow", definition)
    assert summary == "아티팩트 1개 생성: out.txt"
    assert ctx.artifact_keys == [
        {"key": f"{ctx.run_id}/out.txt", "name": "out.txt", "content_type": "text/markdown"}
    ]


# ---------- 3. 로깅 부수효과 — _log_node_start / _log_node_finish ----------


async def test_log_node_start_returns_none_without_agent_run(db):
    """실제 AgentRun이 없으면 FK 위반을 삼키고 조용히 None을 반환(best-effort).

    _log_node_finish(None, ...)도 예외 없이 no-op이어야 한다.
    """
    from biznotes_core.models import ExecutionLog
    from sqlalchemy import select

    orphan_run_id = uuid.uuid4()  # agent_runs에 없는 id → FK 위반
    log_id = await _log_node_start(orphan_run_id, "n1", "constant", {}, datetime.now(UTC))
    assert log_id is None

    # None log_id로 finish 호출은 조용히 반환(예외 없음).
    await _log_node_finish(log_id, status="succeeded", finished_at=datetime.now(UTC))

    rows = (
        (await db.execute(select(ExecutionLog).where(ExecutionLog.run_id == orphan_run_id)))
        .scalars()
        .all()
    )
    assert rows == []


async def _make_run_for_logs(db, user, definition):
    """ExecutionLog FK를 만족하는 실제 Notebook/Agent/AgentRun 생성 (test_workflows 패턴)."""
    from biznotes_core.models import Agent, AgentRun, Notebook

    notebook = Notebook(owner_id=user.id, title="char-log-nb")
    db.add(notebook)
    await db.flush()
    agent = Agent(
        name="char-log-workflow",
        kind="workflow",
        definition=definition,
        params_schema={},
        scope="user",
        owner_id=user.id,
    )
    db.add(agent)
    await db.flush()
    run = AgentRun(
        agent_id=agent.id,
        notebook_id=notebook.id,
        user_id=user.id,
        params={},
        status="running",
    )
    db.add(run)
    await db.commit()
    return notebook, run


async def test_failed_node_persists_failed_execution_log(db, user_a, _mock_runtime_deps):
    """노드 최종 실패 시 ExecutionLog가 status='failed' + error_message로 영속화된다."""
    from biznotes_core.models import ExecutionLog
    from sqlalchemy import select

    definition = {
        "nodes": [
            {"id": "raw", "type": "constant", "value": {"text": "{}"}},
            {
                "id": "validate",
                "type": "json_validate",
                "schema": "not-a-real-schema",  # 등록되지 않은 스키마 → 결정적 실패
                "source": "{raw.text}",
            },
        ],
        "edges": [["raw", "validate"]],
    }
    notebook, run = await _make_run_for_logs(db, user_a, definition)
    ctx = RunContext(run_id=run.id, notebook_id=notebook.id, db=db, params={}, user_id=user_a.id)

    with pytest.raises(AgentRuntimeError, match=r"노드 \[validate\] 실행 실패"):
        await execute_agent(ctx, "workflow", definition)

    await db.commit()  # execution_logs는 격리 세션 커밋 — 새 트랜잭션으로 조회
    logs = (
        (await db.execute(select(ExecutionLog).where(ExecutionLog.run_id == run.id)))
        .scalars()
        .all()
    )
    by_node = {log.node_id: log for log in logs}
    assert set(by_node) == {"raw", "validate"}
    assert by_node["raw"].status == "succeeded"

    failed = by_node["validate"]
    assert failed.status == "failed"
    assert failed.node_type == "json_validate"
    assert failed.error_message is not None
    assert "알 수 없는 schema" in failed.error_message
    # 실패 경로에서도 종료 시각·소요 시간은 기록되고, 출력은 남지 않는다.
    assert failed.finished_at is not None
    assert failed.duration_ms is not None and failed.duration_ms >= 0
    assert failed.output_data is None
    # input_data는 _LOG_INPUT_KEYS 화이트리스트(source/schema)만 기록.
    assert failed.input_data == {"source": "{raw.text}", "schema": "not-a-real-schema"}


# ---------- 4. _exec_http 실행기 ----------
#
# 코어 v0.10.15 #153 A(직접 노드 HITL): http 노드는 기본으로 승인 게이트에 멈춘다. 게이트 없이
# 실행되는 조건은 `requires_approval`이 리터럴 False ∧ 실행 정의(ctx.agent_id)를 DB에서 다시 읽어
# scope=system ∧ url 대상(scheme·host·port)이 리터럴일 때뿐이다. 그래서 이 절은 두 축으로
# 특성화한다(core-v0.11.0-upgrade AC-1.8): ① 신뢰 정의가 없으면 게이트(HTTP 요청 0건)
# ② system·리터럴 host·False면 예전과 같은 렌더링·응답 형태·100k 절단·SSRF 차단.
#
# 게이트 전 사전 검사(`validate_external_url`, #153 A-2)는 호스트를 DNS로 해석한다 — 외부 경계라
# `_public_dns`가 테스트 호스트만 공인 IP로 답하고 나머지는 실제 해석에 맡긴다.

TEST_HOST = "api.example.com"


@pytest.fixture
def _public_dns(monkeypatch):
    import socket

    real = socket.getaddrinfo

    def fake_getaddrinfo(host, *args, **kwargs):
        if host == TEST_HOST:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        return real(host, *args, **kwargs)

    monkeypatch.setattr("biznotes_core.core.ssrf.socket.getaddrinfo", fake_getaddrinfo)


@pytest.fixture
def _http_stub(monkeypatch, _public_dns):
    """SSRF 안전 관문(ssrf_safe_request)을 고정 — 외부 네트워크 호출 금지.

    R5-1: _exec_http는 이제 ssrf_safe_request(매 홉 재검증 관문)를 경유한다.
    관문 자체는 tests/test_security.py에서 검증하고, 여기서는 _exec_http의
    관측 계약(렌더링·method 정규화·출력 형태·절단)만 특성화한다.

    반환 dict: captured(요청 인자), response_status/response_text(테스트가 조정 가능).
    """
    stub: dict = {"captured": None, "response_status": 201, "response_text": "pong"}

    async def fake_request(method, url, **kwargs):
        stub["captured"] = {
            "method": method,
            "url": url,
            "headers": kwargs.get("headers"),
            "json": kwargs.get("json"),
        }
        return httpx.Response(stub["response_status"], text=stub["response_text"])

    monkeypatch.setattr("biznotes_core.core.ssrf.ssrf_safe_request", fake_request)
    return stub


async def _definition(db, scope: str) -> uuid.UUID:
    """실행 정의 행 — 게이트 판정이 ctx.agent_id로 DB에서 다시 읽는 scope의 출처."""
    from biznotes_core.models import Agent

    agent = Agent(
        name=f"http-{scope}-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        icon="🌐",
        definition={"nodes": [], "edges": []},
        params_schema={},
        scope=scope,
        enabled=True,
    )
    db.add(agent)
    await db.commit()
    return agent.id


def _http_ctx(db, agent_id: uuid.UUID | None = None) -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, agent_id=agent_id
    )


@pytest.fixture
async def system_ctx(db) -> RunContext:
    """축 ② — system 정의(DB 행)를 실행 중인 ctx."""
    return _http_ctx(db, await _definition(db, "system"))


@pytest.mark.parametrize(
    ("scope", "requires_approval"),
    [
        pytest.param(None, False, id="no-definition-false"),
        pytest.param("user", False, id="user-definition-false"),
        pytest.param("system", None, id="system-definition-default"),
        pytest.param("system", "false", id="system-definition-string-false"),
    ],
)
async def test_exec_http_gated_without_trusted_definition(
    db, _http_stub, scope, requires_approval
):
    """축 ① — 신뢰 정의가 아니면(정의 없음·user 정의·False 아님) 승인 대기, HTTP 요청 0건."""
    agent_id = None if scope is None else await _definition(db, scope)
    node = {"id": "call", "url": f"https://{TEST_HOST}/v1/hook", "method": "post"}
    if requires_approval is not None:
        node["requires_approval"] = requires_approval

    with pytest.raises(ApprovalRequired):
        await _exec_http(_http_ctx(db, agent_id), node, {})

    assert _http_stub["captured"] is None  # 외부로 아무것도 보내지 않았다


async def test_exec_http_renders_request_and_returns_text_status(db, _http_stub, system_ctx):
    """url/headers/body 템플릿 렌더링, method 대문자화, 출력 {text, status} 형태 고정."""
    node = {
        "url": f"https://{TEST_HOST}/v1/{{params.path}}",  # host는 리터럴, path만 템플릿
        "method": "get",  # 소문자 입력 → 대문자로 정규화
        "headers": {"X-Token": "{params.token}"},
        "body": {"q": "{params.q}"},
        "requires_approval": False,
    }
    state = {"params": {"path": "echo", "token": "t-123", "q": "질문"}}

    out = await _exec_http(system_ctx, node, state)

    assert out == {"text": "pong", "status": 201}
    assert _http_stub["captured"] == {
        "method": "GET",
        "url": f"https://{TEST_HOST}/v1/echo",
        "headers": {"X-Token": "t-123"},
        "json": {"q": "질문"},
    }


async def test_exec_http_defaults_method_post_and_empty_headers(db, _http_stub, system_ctx):
    """method 미지정 시 POST, headers/body 미지정 시 {}/None이 전달된다."""
    node = {"url": f"https://{TEST_HOST}/hook", "requires_approval": False}
    out = await _exec_http(system_ctx, node, {})
    assert out["status"] == 201
    assert _http_stub["captured"] == {
        "method": "POST",
        "url": f"https://{TEST_HOST}/hook",
        "headers": {},
        "json": None,
    }


async def test_exec_http_truncates_body_at_100k(db, _http_stub, system_ctx):
    """응답 본문은 100,000자에서 절단된다."""
    _http_stub["response_text"] = "a" * 100_500
    node = {"url": f"https://{TEST_HOST}/big", "requires_approval": False}
    out = await _exec_http(system_ctx, node, {})
    assert len(out["text"]) == 100_000


async def test_exec_http_ssrf_blocked_raises_runtime_error(
    db, monkeypatch, _public_dns, system_ctx
):
    """SSRF 관문 차단은 AgentRuntimeError(원문 메시지 유지)로 변환된다.

    사설 주소는 이제 게이트 전 사전 검사(`validate_external_url`, #153 A-2)가 먼저 막는다 —
    그 경로도 같은 변환이다. 관문(`ssrf_safe_request`) 단계의 차단은 사전 검사를 통과하는
    공인 호스트로 확인한다.
    """
    from biznotes_core.core.ssrf import SSRFBlockedError

    async def _blocked(method, url, **kwargs):
        raise SSRFBlockedError(f"차단된 URL: {url}")

    monkeypatch.setattr("biznotes_core.core.ssrf.ssrf_safe_request", _blocked)

    with pytest.raises(AgentRuntimeError, match="차단된 URL"):
        await _exec_http(
            system_ctx, {"url": f"https://{TEST_HOST}/internal", "requires_approval": False}, {}
        )
    with pytest.raises(AgentRuntimeError, match="내부망 주소"):
        await _exec_http(
            system_ctx, {"url": "http://10.0.0.1/internal", "requires_approval": False}, {}
        )


# ---------- 5. shape 검증기 순수 함수 골든 (runtime.py:1539-1710) ----------


def _valid_citation() -> dict:
    return {"chunk_id": "ck-1", "quote": "인용문", "source_id": "s-1", "source_title": "제목"}


def test_validate_mindmap_shape_golden():
    from biznotes_core.services.agents.runtime import _validate_mindmap_shape as v

    ok = {
        "root": {
            "id": "r",
            "label": "루트",
            "children": [{"id": "c1", "label": "자식", "children": []}],
        }
    }
    assert v(ok) is None
    # children 키 자체는 선택적.
    assert v({"root": {"id": "r", "label": "루트"}}) is None

    assert v("문자열") == "최상위 값이 객체가 아닙니다"
    assert v({"root": []}) == "root 필드가 객체가 아닙니다"
    assert v({"root": {"id": "", "label": "x"}}) == "root.id가 비어있는 문자열입니다"
    assert v({"root": {"id": "r", "label": "  "}}) == "root.label이 비어있는 문자열입니다"
    assert (
        v({"root": {"id": "r", "label": "x", "children": "no"}})
        == "root.children이 배열이 아닙니다"
    )
    assert (
        v({"root": {"id": "r", "label": "x", "children": ["평문"]}})
        == "root.children[0]가 객체가 아닙니다"
    )
    dup = {"root": {"id": "r", "label": "x", "children": [{"id": "r", "label": "y"}]}}
    assert v(dup) == "root.children[0].id가 트리 내에서 중복됩니다: r"


def test_count_and_truncate_mindmap_golden():
    from biznotes_core.services.agents.runtime import _count_mindmap_nodes, _truncate_mindmap_bfs

    root = {
        "id": "r",
        "label": "루트",
        "children": [
            {"id": "a", "label": "a", "children": [{"id": "a1", "label": "a1", "children": []}]},
            {"id": "b", "label": "b", "children": []},
        ],
    }
    assert _count_mindmap_nodes(root) == 4

    # cap 미만이면 무절단 (children 보존, truncated=False).
    kept, truncated = _truncate_mindmap_bfs(root, cap=10)
    assert truncated is False
    assert [c["id"] for c in kept["children"]] == ["a", "b"]

    # cap=3: BFS 순서로 root+상위 2개만 보존, 초과분 절단.
    wide = {
        "id": "r",
        "label": "루트",
        "children": [{"id": f"c{i}", "label": f"c{i}", "children": []} for i in range(5)],
    }
    kept, truncated = _truncate_mindmap_bfs(wide, cap=3)
    assert truncated is True
    assert [c["id"] for c in kept["children"]] == ["c0", "c1"]


def test_validate_citation_shape_golden():
    from biznotes_core.services.agents.runtime import _validate_citation_shape as v

    assert v(_valid_citation(), "[0]") is None
    assert v("평문", "[0]") == "[0].citation이 객체가 아닙니다"
    missing = _valid_citation()
    missing["chunk_id"] = ""
    assert v(missing, "[0]") == "[0].citation.chunk_id이 비어있는 문자열입니다"


def test_validate_flashcards_shape_golden():
    from biznotes_core.services.agents.runtime import _validate_flashcards_shape as v

    ok = [{"id": "c1", "question": "q?", "answer": "a", "citation": _valid_citation()}]
    assert v(ok) is None

    assert v([]) == "최상위 값이 비어있지 않은 배열이 아닙니다"
    assert v({"root": {}}) == "최상위 값이 비어있지 않은 배열이 아닙니다"
    assert v(["평문"]) == "[0]가 객체가 아닙니다"
    no_answer = [{"id": "c1", "question": "q?", "answer": " ", "citation": _valid_citation()}]
    assert v(no_answer) == "[0].answer이 비어있는 문자열입니다"
    dup = ok + [{"id": "c1", "question": "q2?", "answer": "a2", "citation": _valid_citation()}]
    assert v(dup) == "[1].id가 중복됩니다: c1"
    bad_cit = [{"id": "c1", "question": "q?", "answer": "a", "citation": {}}]
    assert v(bad_cit) == "[0].citation.chunk_id이 비어있는 문자열입니다"


def test_validate_quiz_shape_golden():
    from biznotes_core.services.agents.runtime import _validate_quiz_shape as v

    def item(**over):
        base = {
            "id": "q1",
            "question": "무엇?",
            "choices": ["a", "b", "c", "d"],
            "answer_index": 0,
            "citation": _valid_citation(),
        }
        base.update(over)
        return base

    assert v([item()]) is None
    assert v([item(choices=["a", "b", "c"])]) == "[0].choices가 4개 항목의 배열이 아닙니다"
    assert v([item(choices=["a", "b", " ", "d"])]) == "[0].choices에 비어있는 항목이 있습니다"
    # bool은 int의 서브클래스지만 명시적으로 거부된다(현재 동작 고정).
    assert v([item(answer_index=True)]) == "[0].answer_index가 정수가 아닙니다"
    assert v([item(answer_index=4)]) == "[0].answer_index가 0~3 범위를 벗어납니다"
    assert v([item(question="")]) == "[0].question이 비어있는 문자열입니다"


# ---------- (참고) ApprovalRequired 예외 계약 ----------


def test_approval_required_carries_node_id_and_checkpoint():
    """ApprovalRequired(node_id, checkpoint) 생성 계약 — worker 영속화 경로가 의존."""
    exc = ApprovalRequired("n1", {"tool": "call_agent"})
    assert exc.node_id == "n1"
    assert exc.checkpoint == {"tool": "call_agent"}


# ---------- R5-2: 토큰 이중계상 해소 (per-node node_tokens 분리) ----------


def _bare_ctx() -> RunContext:
    return RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={})  # type: ignore[arg-type]


def test_add_tokens_updates_both_shared_and_per_node_cells():
    """add_tokens는 공유 usage(예산·표시)와 per-node node_tokens 양쪽에 가산."""
    ctx = _bare_ctx()
    ctx.add_tokens({"total_tokens": 100, "prompt_tokens": 60})
    assert ctx.usage["total_tokens"] == 100
    assert ctx.node_tokens["total_tokens"] == 100
    assert ctx.node_tokens["prompt_tokens"] == 60


def test_child_shares_usage_but_not_node_tokens():
    """핵심 이중계상 회귀: 자식(서브에이전트) 토큰은 공유 usage에는 누적되지만
    부모 node_tokens에는 섞이지 않는다 → monitoring SUM에서 자식 토큰이 부모 노드
    행 + 자식 행에 이중 계상되지 않는다(cost의 cost_total/cost_usd 분리와 동형)."""
    parent = _bare_ctx()
    parent.add_tokens({"total_tokens": 50})  # 부모 노드 자체 소비

    child = parent.child(uuid.uuid4(), params={})
    child.add_tokens({"total_tokens": 30})  # 서브에이전트 소비

    # 공유 usage: 부모+자식(예산 가드·run-level 기록이 봐야 함)
    assert parent.usage["total_tokens"] == 80
    assert child.usage is parent.usage  # 동일 셀 공유
    # per-node: 부모는 자기 50만, 자식은 자기 30만 — 미공유
    assert parent.node_tokens["total_tokens"] == 50
    assert child.node_tokens["total_tokens"] == 30
    assert child.node_tokens is not parent.node_tokens


def test_resume_node_token_base_yields_full_node_delta():
    """R5-2-01: 재개 시 tokens_before를 노드 진입 기준선으로 되돌리면, 최종 노드
    델타가 승인 전(T1)+후(T2) 소비를 모두 포함한다. make_node의 기준선 선택 로직을
    동일하게 재현해 검증한다."""
    _TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")
    ctx = _bare_ctx()
    # 재개 상태 재현: gate 누적 130(=진입 100 + 승인 전 30), 진입 기준선 100.
    ctx.node_tokens = {"total_tokens": 130}
    ctx.resume_node_token_base = {"total_tokens": 100}

    # make_node의 기준선 캡처 로직(발췌): resume base 우선, 1회 소비 후 None.
    if ctx.resume_node_token_base is not None:
        tokens_before = {k: ctx.resume_node_token_base.get(k, 0) for k in _TOKEN_KEYS}
        ctx.resume_node_token_base = None
    else:
        tokens_before = {k: ctx.node_tokens.get(k, 0) for k in _TOKEN_KEYS}

    assert tokens_before["total_tokens"] == 100  # 진입 기준선 사용
    assert ctx.resume_node_token_base is None  # 1회 소비됨

    # 재개 이후 T2=20 소비.
    ctx.add_tokens({"total_tokens": 20})
    token_delta = {k: ctx.node_tokens.get(k, 0) - tokens_before[k] for k in _TOKEN_KEYS}
    # 델타 = (130+20) - 100 = 50 = T1(30) + T2(20). 승인 전 소비 유실 없음.
    assert token_delta["total_tokens"] == 50


async def test_resume_node_cost_base_yields_full_node_delta_through_make_node(monkeypatch):
    """make_node가 비용 진입 기준선을 1회 소비해 T1+T2 전체를 종료 로그에 기록."""
    finishes: list[float | None] = []

    async def fake_start(run_id, node_id, node_type, input_data, started_at):
        return uuid.uuid4()

    async def fake_finish(log_id, *, cost_usd=None, **kwargs):
        finishes.append(cost_usd)

    async def add_resume_cost(ctx, node, state):
        ctx.add_cost(0.02)
        return {"ok": True}

    monkeypatch.setattr(graph_mod, "_log_node_start", fake_start)
    monkeypatch.setattr(graph_mod, "_log_node_finish", fake_finish)
    monkeypatch.setitem(graph_mod.NODE_EXECUTORS, "constant", add_resume_cost)

    ctx = _bare_ctx()
    ctx.cost_usd = 0.13  # gate 시점: base 0.10 + 승인 전 T1 0.03
    ctx.cost_total[0] = 0.77
    ctx.resume_node_cost_base = 0.10

    await execute_agent(
        ctx,
        "workflow",
        {"nodes": [{"id": "n", "type": "constant"}], "edges": []},
    )

    assert finishes == [pytest.approx(0.05)]  # T1 0.03 + 재개 후 T2 0.02
    assert ctx.resume_node_cost_base is None
    assert ctx.node_cost_base_snapshot == pytest.approx(0.10)
    assert ctx.cost_usd == pytest.approx(0.15)
    assert ctx.cost_total[0] == pytest.approx(0.79)


def test_child_shares_total_cost_but_keeps_per_node_cost_separate():
    parent = _bare_ctx()
    parent.add_cost(0.03)

    child = parent.child(uuid.uuid4(), params={})
    child.add_cost(0.07)

    assert parent.cost_usd == pytest.approx(0.03)
    assert child.cost_usd == pytest.approx(0.07)
    assert child.cost_total is parent.cost_total
    assert parent.cost_total[0] == pytest.approx(0.10)


def test_branch_parallel_isolates_node_tokens():
    """병렬 분기(CR-R5-02): node_tokens는 분기마다 독립 사본 — 형제 노드 토큰이
    서로의 per-node 델타에 섞이지 않는다. 단 공유 usage는 동일 셀 유지."""
    base = _bare_ctx()
    base.add_tokens({"total_tokens": 10})

    b1 = base.branch_parallel()
    b2 = base.branch_parallel()
    b1.add_tokens({"total_tokens": 5})
    b2.add_tokens({"total_tokens": 7})

    # node_tokens는 독립(10 기준선에서 각자 가산)
    assert b1.node_tokens["total_tokens"] == 15
    assert b2.node_tokens["total_tokens"] == 17
    assert b1.node_tokens is not b2.node_tokens
    # 공유 usage는 동일 셀 — 양쪽 가산이 합산됨
    assert base.usage is b1.usage is b2.usage
    assert base.usage["total_tokens"] == 10 + 5 + 7
