"""R2b — approval.py 순수 로직 단일 소유 특성화 테스트.

검증 축(R2b 설계 AC-1·AC-4·AC-5):
- import 순수성 3분할(파일 AST forbidden prefix / fresh subprocess sys.modules / path-bounded)
- CheckpointKeys 값이 R2b 이전 문자열과 byte-identical(하위 호환)
- is_consumable / resume_status_of 순수 predicate 골든
- restore_run_context 복원 골든(누락 키 기본값 포함)
"""

import ast
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from biznotes_core.services.agents.react import approval
from biznotes_core.services.agents.react.approval import CheckpointKeys

APPROVAL_PATH = Path(approval.__file__)
FORBIDDEN_PREFIXES = ("biznotes_core.models", "sqlalchemy", "celery", "fastapi", "app.workers")


# ── AC-1 (a): 파일 AST의 모든 import에 forbidden prefix 0건 ────────────────────
def test_approval_file_ast_has_no_forbidden_imports():
    tree = ast.parse(APPROVAL_PATH.read_text(), filename=str(APPROVAL_PATH))
    offenders: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            # from X import ... — level>0(상대 임포트)은 module이 None일 수 있음.
            names = [node.module] if node.module else []
        for name in names:
            if any(name == p or name.startswith(p + ".") for p in FORBIDDEN_PREFIXES):
                offenders.append(name)
    assert offenders == [], f"approval.py가 금지 모듈을 임포트함: {offenders}"


_FORBIDDEN_PROBE = (
    "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
    "{'sqlalchemy','celery','fastapi'} or m.startswith('biznotes_core.models') "
    "or m.startswith('app.workers'))\n"
    "print(';'.join(bad))\n"
)


def _run_purity_subprocess(body: str) -> list[str]:
    """clean baseline 서브프로세스에서 body를 실행한 뒤 로드된 금지 모듈 목록 반환."""
    result = subprocess.run(
        [sys.executable, "-c", body + _FORBIDDEN_PROBE],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),  # apps/api
    )
    assert result.returncode == 0, f"서브프로세스 실패: {result.stderr}"
    return [m for m in result.stdout.strip().split(";") if m]


# ── AC-1 (b): fresh subprocess에서 approval만 로드 시 forbidden 모듈 미로드 ──────
def test_approval_import_in_fresh_subprocess_loads_no_forbidden_modules():
    # 현 프로세스는 sqlalchemy가 이미 로드돼 delta 검사가 false-pass하므로,
    # clean baseline 서브프로세스에서 approval만 임포트한 뒤 sys.modules를 확인한다.
    loaded = _run_purity_subprocess(
        "import sys\nimport biznotes_core.services.agents.react.approval  # noqa\n"
    )
    assert loaded == [], f"approval 임포트가 금지 모듈을 로드함: {loaded}"


# ── AC-1 (b'): 게이트 경로를 실제로 **호출**해도 forbidden 모듈이 로드되지 않음 ────
def test_approval_gate_call_path_loads_no_forbidden_modules():
    # [R2B-02] import뿐 아니라 evaluate_gate/build_checkpoint를 실제 호출했을 때도
    # 순수해야 한다 — 과거에는 build_checkpoint→sanitize_for_approval_card→
    # runtime._mask_tool_args→RunContext→sqlalchemy 전이 임포트가 호출 시점에 걸렸다.
    # guards는 build_checkpoint가 token_base/cost_base만 읽으므로 SimpleNamespace로
    # 충분하다 — loop 모듈을 임포트하면 그 sqlalchemy 사슬이 딸려와 테스트가 무의미해진다.
    body = (
        "import sys\n"
        "from types import SimpleNamespace\n"
        "from biznotes_core.services.agents.react import approval\n"
        "ctx = SimpleNamespace(usage={'total_tokens': 0}, "
        "node_tokens={'total_tokens': 0}, node_token_base_snapshot={}, cost_usd=0.0, "
        "node_cost_base_snapshot=0.0, cost_total=[0.0], "
        "artifact_keys=[], react_subagent_calls=[0], register_source_calls=[0])\n"
        "guards = SimpleNamespace(token_base=0, cost_base=0.0)\n"
        "cp = approval.build_checkpoint(ctx, guards, 'http_request', "
        "{'url': 'https://x.example/?api_key=secret'}, {'id': 'r'}, messages=[], "
        "tool_log=[], aux_idx=0, iters_done=0, subagent_calls_in_node=0, "
        "remaining_calls=[], pending_call=None)\n"
        "assert cp['tool_name'] == 'http_request'\n"
        "gate = approval.evaluate_gate(ctx, guards, 'http_request', "
        "{'url': 'https://x.example'}, {'id': 'r'}, messages=[], tool_log=[], "
        "aux_idx=0, iters_done=0, subagent_calls_in_node=0, remaining_calls=[])\n"
        "assert gate is not None\n"
    )
    loaded = _run_purity_subprocess(body)
    assert loaded == [], f"게이트 호출 경로가 금지 모듈을 로드함: {loaded}"


# ── AC-1 (c): path-bounded — AgentRunApproval/update/AsyncSession은 worker만 ────
def test_hitl_db_symbols_are_worker_bounded_not_in_approval():
    # 문자열/주석(docstring)이 아니라 실제 **코드 식별자 참조**만 검사한다 —
    # AST의 Name/Attribute 노드로 판정(docstring 내 설명 텍스트 오탐 방지).
    tree = ast.parse(APPROVAL_PATH.read_text(), filename=str(APPROVAL_PATH))
    referenced_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced_names.add(node.attr)
        elif isinstance(node, ast.alias):
            referenced_names.add(node.asname or node.name.split(".")[-1])
    forbidden_syms = {"AgentRunApproval", "AsyncSession", "async_session_factory"}
    leaked = forbidden_syms & referenced_names
    assert leaked == set(), f"approval.py가 DB 심볼을 코드로 참조함(worker 잔류 위반): {leaked}"
    # 소비 CAS SQL은 worker에만 존재해야 한다(반대 방향 확인).
    # approval.py는 코어로 이관됐고 worker는 프로젝트에 남아 있다 — 더 이상
    # 서로의 상대 경로로 찾을 수 없으므로 각자 모듈에서 위치를 얻는다.
    from app.workers.tasks import agent as worker_agent

    worker_src = Path(worker_agent.__file__).read_text()
    assert "update(AgentRunApproval)" in worker_src, "CAS SQL이 worker에 있어야 함"


# ── AC-4: CheckpointKeys 값이 R2b 이전 문자열과 byte-identical ──────────────────
def test_checkpoint_keys_are_byte_identical_to_pre_r2b_literals():
    # 이 매핑은 R2b 이전 loop.py::_react_approval_gate·worker 인라인 복원이 쓰던
    # 문자열 리터럴을 그대로 옮긴 것 — in-flight checkpoint 하위 호환의 계약이다.
    expected = {
        "GRAPH_STATE": "graph_state",
        "REACT_NODE_ID": "react_node_id",
        "REACT_MESSAGES": "react_messages",
        "REACT_PENDING_TOOL_CALL": "react_pending_tool_call",
        "REACT_REMAINING_CALLS_IN_TURN": "react_remaining_calls_in_turn",
        "REACT_ITERS_DONE": "react_iters_done",
        "REACT_SUBAGENT_CALLS_IN_NODE": "react_subagent_calls_in_node",
        "REACT_GUARDS_AUX_IDX": "react_guards_aux_idx",
        "REACT_TOOL_LOG": "react_tool_log",
        "REACT_APPROVAL_STATUS": "react_approval_status",
        "GUARDS_TOKEN_BASE": "guards_token_base",
        "GUARDS_COST_BASE": "guards_cost_base",
        "CTX_USAGE": "ctx_usage",
        "CTX_NODE_TOKENS": "ctx_node_tokens",
        "CTX_NODE_TOKEN_BASE": "ctx_node_token_base",
        "CTX_NODE_COST": "ctx_node_cost",
        "CTX_NODE_COST_BASE": "ctx_node_cost_base",
        "CTX_COST_TOTAL": "ctx_cost_total",
        "CTX_ARTIFACT_KEYS": "ctx_artifact_keys",
        "CTX_REACT_SUBAGENT_CALLS": "ctx_react_subagent_calls",
        "CTX_REGISTER_SOURCE_CALLS": "ctx_register_source_calls",
        "TOOL_NAME": "tool_name",
        "ARGUMENTS_SUMMARY": "arguments_summary",
    }
    for attr, value in expected.items():
        assert getattr(CheckpointKeys, attr) == value, f"{attr} 드리프트"


# ── is_consumable / resume_status_of 순수 predicate 골든 ──────────────────────
@pytest.mark.parametrize(
    "status,consumed,expected",
    [
        ("approved", False, True),
        ("rejected", False, True),
        ("approved", True, False),  # 이미 소비됨
        ("rejected", True, False),
        ("pending", False, False),  # 미해결
        ("expired", False, False),
    ],
)
def test_is_consumable_golden(status, consumed, expected):
    assert approval.is_consumable(status, consumed) is expected


@pytest.mark.parametrize(
    "status,expected",
    [("approved", "approved"), ("rejected", "rejected"), ("pending", "rejected")],
)
def test_resume_status_of_golden(status, expected):
    assert approval.resume_status_of(status) == expected


# ── restore_run_context 복원 골든(누락 키 기본값 포함) ─────────────────────────
class _FakeCtx:
    """RunContext의 복원 대상 필드만 흉내내는 순수 스텁(DB 없이 검증)."""

    def __init__(self):
        self.usage: dict = {}
        self.node_tokens: dict = {}  # R5-2 per-node 토큰 셀
        self.resume_node_token_base: dict | None = None  # R5-2-01
        self.cost_total = [0.0]
        self.cost_usd = 0.0
        self.resume_node_cost_base: float | None = None
        self.artifact_keys: list = []
        self.react_subagent_calls = [0]
        self.register_source_calls = [0]


def test_restore_run_context_restores_all_mutable_state():
    ctx = _FakeCtx()
    checkpoint = {
        CheckpointKeys.CTX_USAGE: {"total_tokens": 1234},
        CheckpointKeys.CTX_COST_TOTAL: 0.42,
        CheckpointKeys.CTX_ARTIFACT_KEYS: [{"name": "a.md"}],
        CheckpointKeys.CTX_REACT_SUBAGENT_CALLS: 3,
        CheckpointKeys.CTX_REGISTER_SOURCE_CALLS: 2,
    }
    approval.restore_run_context(ctx, checkpoint)
    assert ctx.usage == {"total_tokens": 1234}
    # R5-2-01: 신규 키 없는 checkpoint(구버전)는 node_tokens를 CTX_USAGE로 폴백.
    assert ctx.node_tokens == {"total_tokens": 1234}
    assert ctx.resume_node_token_base is None
    assert ctx.cost_total[0] == 0.42
    assert ctx.cost_usd == 0.42
    assert ctx.resume_node_cost_base is None
    assert ctx.artifact_keys == [{"name": "a.md"}]
    assert ctx.react_subagent_calls[0] == 3
    assert ctx.register_source_calls[0] == 2


def test_restore_run_context_resume_node_token_base_spans_pre_approval():
    """R5-2-01: 신규 checkpoint는 gate 누적(CTX_NODE_TOKENS)과 노드 진입 기준선
    (CTX_NODE_TOKEN_BASE)을 복원해, 재개 노드 델타가 승인 전 소비(T1)까지 포함한다.

    시나리오: 노드 진입 기준선 B=100, 승인 전 소비 T1=30 → gate 시 node_tokens=130.
    재개 시 node_tokens=130으로 복원되고 make_node의 tokens_before=resume base(100)이면,
    이후 T2 소비를 더한 최종 델타 = (130+T2) - 100 = 30+T2 = T1+T2. (본 단위 테스트는
    복원값만 검증; 델타 산출은 test_runtime_characterization의 make_node 재현이 담당.)"""
    ctx = _FakeCtx()
    checkpoint = {
        CheckpointKeys.CTX_USAGE: {"total_tokens": 500},  # run 누적(자식 포함 가능)
        CheckpointKeys.CTX_NODE_TOKENS: {"total_tokens": 130},  # 노드 per-node 누적
        CheckpointKeys.CTX_NODE_TOKEN_BASE: {"total_tokens": 100},  # 노드 진입 기준선
        CheckpointKeys.CTX_COST_TOTAL: 0.0,
    }
    approval.restore_run_context(ctx, checkpoint)
    assert ctx.node_tokens == {"total_tokens": 130}  # gate 누적으로 복원
    assert ctx.resume_node_token_base == {"total_tokens": 100}  # 진입 기준선


def test_restore_run_context_resume_node_cost_base_spans_pre_approval():
    """신규 checkpoint는 부모 per-node 비용과 최초 진입 기준선을 따로 복원한다."""
    ctx = _FakeCtx()
    checkpoint = {
        CheckpointKeys.CTX_NODE_COST: 0.13,
        CheckpointKeys.CTX_NODE_COST_BASE: 0.10,
        CheckpointKeys.CTX_COST_TOTAL: 0.77,
    }

    approval.restore_run_context(ctx, checkpoint)

    assert ctx.cost_usd == pytest.approx(0.13)
    assert ctx.resume_node_cost_base == pytest.approx(0.10)
    assert ctx.cost_total[0] == pytest.approx(0.77)


def test_build_checkpoint_stores_per_node_cost_and_entry_baseline():
    ctx = SimpleNamespace(
        usage={"total_tokens": 7},
        node_tokens={"total_tokens": 7},
        node_token_base_snapshot={"total_tokens": 2},
        cost_usd=0.13,
        node_cost_base_snapshot=0.10,
        cost_total=[0.77],
        artifact_keys=[],
        react_subagent_calls=[0],
        register_source_calls=[0],
    )
    guards = SimpleNamespace(token_base=2, cost_base=0.10)

    checkpoint = approval.build_checkpoint(
        ctx,
        guards,
        "http_request",
        {"url": "https://example.test"},
        {"id": "react"},
        messages=[],
        tool_log=[],
        aux_idx=0,
        iters_done=0,
        subagent_calls_in_node=0,
        remaining_calls=[],
        pending_call=None,
    )

    assert checkpoint[CheckpointKeys.CTX_NODE_COST] == pytest.approx(0.13)
    assert checkpoint[CheckpointKeys.CTX_NODE_COST_BASE] == pytest.approx(0.10)
    assert checkpoint[CheckpointKeys.CTX_COST_TOTAL] == pytest.approx(0.77)


def test_restore_run_context_legacy_cost_falls_back_to_total_with_known_limitation():
    """구 checkpoint는 기존처럼 자식 비용 포함 가능 총계를 부모 셀에 복원한다."""
    ctx = _FakeCtx()

    approval.restore_run_context(ctx, {CheckpointKeys.CTX_COST_TOTAL: 0.77})

    assert ctx.cost_usd == pytest.approx(0.77)
    assert ctx.resume_node_cost_base is None


def test_restore_run_context_missing_keys_use_defaults():
    # pre-R2b 또는 부분 checkpoint에 키가 빠져도 기존 인라인 복원과 동일한 기본값.
    ctx = _FakeCtx()
    ctx.usage = {"stale": 9}
    ctx.node_tokens = {"stale": 9}
    approval.restore_run_context(ctx, {})
    assert ctx.usage == {"stale": 9}  # update({})는 기존값 보존
    # R5-2-01 하위호환: 신규 키 없으면 node_tokens는 CTX_USAGE(빈)로 폴백, base None.
    assert ctx.resume_node_token_base is None
    assert ctx.cost_total[0] == 0.0
    assert ctx.cost_usd == 0.0
    assert ctx.resume_node_cost_base is None
    assert ctx.artifact_keys == []
    assert ctx.react_subagent_calls[0] == 0
    assert ctx.register_source_calls[0] == 0


# ── AC-3 (CR19-07): side_effect가 spec에서 파생됨을 기계 증명 ──────────────────
def test_tool_side_effect_is_derived_from_specs_not_manual():
    # ① production TOOL_SIDE_EFFECT가 실제로 파생 함수의 출력과 동일(수동 dict 금지).
    from biznotes_core.services.agents import tool_registry as tr

    assert tr.TOOL_SIDE_EFFECT == tr.derive_side_effect_map()


def test_side_effect_exact_expected_map_golden():
    # ② 단일 expected map으로 exact key set + bool 값 골든(CR-R2B4-01). 신규 정적
    #    도구 추가 시 이 map도 갱신해야 통과 → 잘못된 bool도 리뷰를 강제한다.
    from biznotes_core.services.agents.tool_registry import derive_side_effect_map

    expected = {
        "rag_search": False,
        "source_load": False,
        # 코어 v0.30.0 #447 — run 범위 소스 목록을 읽기만 한다(읽기 전용)
        "source_list": False,
        "run_skill": False,
        # biznotes-core 표준 스킬 런타임(D6) — 지침·파일을 읽기만 한다
        "activate_skill": False,
        "read_skill_file": False,
        "web_search": False,
        "call_agent": True,
        "http_request": True,
        "mcp_call": True,
        "register_source": True,
        # biznotes-core 번들 스크립트 실행(D7) — 격리 환경에서 코드를 실행한다. always-gated
        "run_skill_script": True,
        # 코어 v0.10.12 #133 산출물 도구 — run 산출물을 나열·읽기만 한다(읽기 전용)
        "artifact_list": False,
        "artifact_read": False,
    }
    assert derive_side_effect_map() == expected


def test_side_effect_declaration_missing_spec_tool_raises():
    # ③ spec에 도구가 있는데 선언을 빠뜨리면 즉시 실패(누락 drift 차단).
    from biznotes_core.services.agents import tool_registry as tr

    original = dict(tr._SIDE_EFFECT_DECL)
    try:
        tr._SIDE_EFFECT_DECL.pop("http_request")
        with pytest.raises(RuntimeError, match="누락"):
            tr.derive_side_effect_map()
    finally:
        tr._SIDE_EFFECT_DECL.clear()
        tr._SIDE_EFFECT_DECL.update(original)


def test_unknown_and_mcp_tools_default_to_side_effect_true():
    # ④ 맵에 없는 도구(MCP 디스커버리·미지)는 안전 기본값 True.
    from biznotes_core.services.agents.tool_registry import is_side_effect_tool

    assert is_side_effect_tool("mcp_abc12345_search") is True
    assert is_side_effect_tool("totally_unknown_tool") is True
