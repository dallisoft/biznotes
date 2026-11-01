"""Phase 1 — 도구 레지스트리 동적화 + `call_agent` 도구 단위 테스트.

REACT_AGENT_PLAN.md §3 Phase 1 수락기준에 1:1 대응한다.
- build_tool_specs 기본/allowed/MCP 노출 정책(T4·Q2)
- call_agent → _run_subagent 가드 위임(D3)
- 도구 오류 일반화 + 인자 시크릿 마스킹(T7·SEC-07/08)
- Phase 0 골든 회귀 없음은 test_tool_calling_characterization.py가 별도 보장.

mock 의존(`_mock_runtime_deps`)은 test_workflows.py 패턴을 재사용한다.
"""

import uuid

import pytest
from biznotes_core.models import Agent
from biznotes_core.services.agents import runtime
from biznotes_core.services.agents.errors import ToolUnavailableError
from biznotes_core.services.agents.mcp_client import MCP_META_RUN_ID, MCP_META_USER_ID
from biznotes_core.services.agents.mcp_exposure import approved_tools, set_approved_tools
from biznotes_core.services.agents.mcp_tool_spec import mcp_tool_spec_hash
from biznotes_core.services.agents.runtime import RunContext, execute_agent
from biznotes_core.services.agents.tool_registry import (
    CHAT_SAFE_TOOLS,
    DEFAULT_TOOLS,
    _make_mcp_dispatcher,
    _run_skill_dispatcher,
    build_tool_specs,
)


async def _make_workflow_agent(db, owner_id, *, scope="user", enabled=True):
    agent = Agent(
        name=f"sub-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        icon="⚙️",
        definition={
            "nodes": [
                {"id": "gen", "type": "llm", "prompt": "sub: {params.x}"},
            ],
            "edges": [],
        },
        params_schema={},
        scope=scope,
        owner_id=owner_id,
        enabled=enabled,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


def _ctx(db, user_id=None, agent_id=None):
    return RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=user_id,
        agent_id=agent_id,
    )


# ---------- build_tool_specs 노출 정책 (T4·Q2) ----------


async def test_default_exposes_only_rag_and_call_agent(db):
    """기본 호출(allowed=None)은 rag_search·call_agent만 — MCP/stdio/http/web 제외."""
    specs, dispatchers = await build_tool_specs(_ctx(db), None)
    names = [s["function"]["name"] for s in specs]
    assert names == list(DEFAULT_TOOLS) == ["rag_search", "call_agent"]
    assert "http_request" not in names
    assert "web_search" not in names
    assert not any(n.startswith("mcp__") for n in names)
    assert set(dispatchers) == {"rag_search", "call_agent"}


async def test_default_preserves_order_for_mock_determinism(db):
    """기본 노출은 allowed 순서를 보존(첫 도구=rag_search — mock 루프 결정성)."""
    specs, _ = await build_tool_specs(_ctx(db), None)
    assert specs[0]["function"]["name"] == "rag_search"


async def test_allowed_whitelist_excludes_others(db):
    """allowed에 명시한 도구만 노출 — 그 외(call_agent 포함)는 제외."""
    specs, dispatchers = await build_tool_specs(_ctx(db), ["rag_search"])
    names = [s["function"]["name"] for s in specs]
    assert names == ["rag_search"]
    assert set(dispatchers) == {"rag_search"}


async def test_chat_safe_allowlist_includes_sources_mcp_and_published_skills(db):
    """채팅 자율 모드는 노트 원문·승인된 MCP·시스템 스킬을 사용할 수 있어야 한다."""
    assert CHAT_SAFE_TOOLS == (
        "rag_search",
        "source_load",
        # 코어 v0.30.0 #447 — 소스 목록 도구(읽기 전용). 제품 채팅은 이 튜플을 그대로 넘기므로
        # 자동 노출된다(core-v0.30.0-upgrade #284)
        "source_list",
        "mcp",
        "run_skill",
        # biznotes-core 표준 계획 S-HCI-10(인간 승인 2026-09-22)
        "activate_skill",
        "read_skill_file",
        # 코어 v0.10.12 #133(코어 인간 승인 2026-10-07) — 읽기 전용 산출물 도구.
        # 제품 채팅은 이 튜플을 그대로 넘기므로 자동 노출된다(core-v0.11.0-upgrade HCI-12 (a))
        "artifact_list",
        "artifact_read",
    )
    specs, dispatchers = await build_tool_specs(_ctx(db), list(CHAT_SAFE_TOOLS))
    names = [spec["function"]["name"] for spec in specs]
    assert "rag_search" in names
    assert "source_load" in names
    assert "run_skill" not in names  # published system skill이 없으면 capability도 없음
    assert "mcp" not in names  # 승인 행이 없는 서버는 deny-by-default(#139 B — 정본은 DB)
    assert "source_list" in names
    assert set(dispatchers) == {
        "rag_search",
        "source_load",
        "source_list",
        "artifact_list",
        "artifact_read",
    }


async def test_run_skill_exposes_only_published_system_wrappers(db, user_a):
    """private/draft 스킬은 숨기고 published system wrapper만 capability로 노출한다."""
    from biznotes_core.models import Agent, Skill, SkillVersion

    skill = Skill(
        name="건물 조사",
        description="건물 정보 조사",
        slug=f"building-{uuid.uuid4().hex[:6]}",
        visibility="system",
        status="published",
        enabled=True,
    )
    db.add(skill)
    await db.flush()
    version = SkillVersion(
        skill_id=skill.id,
        version=1,
        package_uri="skills/building/1",
        skill_md_hash="a" * 64,
        package_hash="b" * 64,
        manifest={"mode": "instruction"},
        file_tree=[],
        security_report={},
        status="published",
        created_by=user_a.id,
    )
    db.add(version)
    await db.flush()
    # 노출 정본은 공개 포인터다(biznotes-core 표준 계획 D8) — status만으로는 노출되지 않는다
    skill.published_version_id = version.id
    wrapper = Agent(
        name="건물 조사",
        kind="skill",
        icon="🧭",
        definition={},
        params_schema={},
        scope="system",
        owner_id=None,
        enabled=True,
        source_kind="skill_package",
        skill_version_id=version.id,
    )
    db.add(wrapper)
    await db.commit()

    specs, dispatchers = await build_tool_specs(_ctx(db, user_a.id), ["run_skill"])
    assert [s["function"]["name"] for s in specs] == ["run_skill"]
    assert str(wrapper.id) in specs[0]["function"]["parameters"]["properties"]["agent_id"]["enum"]
    assert "run_skill" in dispatchers


async def test_run_skill_appends_skill_evidence_on_success_and_failure(
    db, user_a, monkeypatch
):
    """run_skill의 실제 wrapper 메타데이터와 성공/실패 상태를 evidence에 남긴다."""
    from biznotes_core.models import Agent, Skill, SkillVersion

    skill = Skill(
        name="근거 검토 스킬",
        description="근거 검토",
        slug=f"provenance-{uuid.uuid4().hex[:6]}",
        visibility="system",
        status="published",
        enabled=True,
    )
    db.add(skill)
    await db.flush()
    version = SkillVersion(
        skill_id=skill.id,
        version=7,
        package_uri="skills/provenance/7",
        skill_md_hash="a" * 64,
        package_hash="b" * 64,
        manifest={"mode": "instruction"},
        file_tree=[],
        security_report={},
        status="published",
        created_by=user_a.id,
    )
    db.add(version)
    await db.flush()
    # 노출 정본은 공개 포인터다(biznotes-core 표준 계획 D8) — status만으로는 노출되지 않는다
    skill.published_version_id = version.id
    wrapper = Agent(
        name=skill.name,
        kind="skill",
        icon="🧭",
        definition={},
        params_schema={},
        scope="system",
        owner_id=None,
        enabled=True,
        source_kind="skill_package",
        skill_version_id=version.id,
    )
    db.add(wrapper)
    await db.commit()

    _, dispatchers = await build_tool_specs(_ctx(db, user_a.id), ["run_skill"])

    # run_skill은 노출 version을 명시로 넘긴다(biznotes-core D8, R5-04)
    async def succeed(ctx, target_id, params, *, version_id=None):
        return "완료"

    monkeypatch.setattr(runtime, "_run_subagent", succeed)
    success_ctx = _ctx(db, user_a.id)
    assert await dispatchers["run_skill"](
        success_ctx, "run_skill", {"agent_id": str(wrapper.id)}
    ) == "완료"
    assert success_ctx.tool_evidence == [
        {
            "kind": "skill",
            "name": skill.name,
            "version": 7,
            "status": "succeeded",
            "skill_version_id": str(version.id),
            "package_hash": "b" * 64,
            "agent_id": str(wrapper.id),
        }
    ]

    async def fail(ctx, target_id, params, *, version_id=None):
        raise RuntimeError("skill failed")

    monkeypatch.setattr(runtime, "_run_subagent", fail)
    failed_ctx = _ctx(db, user_a.id)
    with pytest.raises(RuntimeError, match="skill failed"):
        await dispatchers["run_skill"](
            failed_ctx, "run_skill", {"agent_id": str(wrapper.id)}
        )
    assert failed_ctx.tool_evidence[0]["kind"] == "skill"
    assert failed_ctx.tool_evidence[0]["status"] == "failed"
    assert failed_ctx.tool_evidence[0]["name"] == skill.name
    assert failed_ctx.tool_evidence[0]["version"] == 7


async def test_run_skill_manifest_rejection_appends_failed_evidence_and_reraises(db, user_a):
    from biznotes_core.models import Agent, Skill, SkillVersion

    skill = Skill(
        name="쓰기 권한 스킬",
        description="허용되지 않는 스킬",
        slug=f"write-skill-{uuid.uuid4().hex[:6]}",
        visibility="system",
        status="published",
        enabled=True,
    )
    db.add(skill)
    await db.flush()
    version = SkillVersion(
        skill_id=skill.id,
        version=1,
        package_uri="skills/write/1",
        skill_md_hash="a" * 64,
        package_hash="b" * 64,
        manifest={"permissions": {"mcp": True}},
        file_tree=[],
        security_report={},
        status="published",
        created_by=user_a.id,
    )
    db.add(version)
    await db.flush()
    # 노출 정본은 공개 포인터다(biznotes-core 표준 계획 D8) — status만으로는 노출되지 않는다
    skill.published_version_id = version.id
    wrapper = Agent(
        name=skill.name,
        kind="skill",
        icon="🧭",
        definition={},
        params_schema={},
        scope="system",
        owner_id=None,
        enabled=True,
        source_kind="skill_package",
        skill_version_id=version.id,
    )
    db.add(wrapper)
    await db.commit()

    ctx = _ctx(db, user_a.id)
    with pytest.raises(RuntimeError, match="published system skill이 아니거나 실행할 수 없습니다"):
        await _run_skill_dispatcher()(ctx, "run_skill", {"agent_id": str(wrapper.id)})

    assert ctx.tool_evidence == [
        {
            "kind": "skill",
            "name": skill.name,
            "version": 1,
            "status": "failed",
            "skill_version_id": str(version.id),
            "package_hash": "b" * 64,
            "agent_id": str(wrapper.id),
        }
    ]


async def test_optin_http_and_web_exposed_only_when_listed(db):
    """http_request·web_search는 명시 opt-in일 때만 노출된다."""
    specs, _ = await build_tool_specs(_ctx(db), ["http_request", "web_search"])
    names = {s["function"]["name"] for s in specs}
    assert names == {"http_request", "web_search"}


async def test_unknown_tool_name_ignored(db):
    """미지 도구 이름은 무시(스펙·디스패처에 포함되지 않음)."""
    specs, dispatchers = await build_tool_specs(_ctx(db), ["rag_search", "does_not_exist"])
    names = [s["function"]["name"] for s in specs]
    assert names == ["rag_search"]
    assert "does_not_exist" not in dispatchers


async def test_mcp_call_backward_compat_optin(db):
    """[C1] 레지스트리 수준 하위호환(`build_tool_specs(ctx, ["mcp_call"])` 직접 호출) — 명시
    allowlist에 `mcp_call`이 있으면 opt-in 별칭으로 스펙·디스패처가 만들어진다.

    이것은 도구 레지스트리의 동작만 고정한다. llm 노드 `tools`의 `mcp_call`은 코어 v0.10.3부터
    저장(제품 `_validate_structure`·`_validate_definition` 422)과 실행(`_exec_llm_with_tools`)
    양쪽에서 거부된다(계획 AC-25). 승인 게이트가 있는 react_agent의 `allowed_tools` 경로는
    이 별칭을 계속 쓴다.
    """
    specs, dispatchers = await build_tool_specs(_ctx(db), ["mcp_call"])
    names = [s["function"]["name"] for s in specs]
    assert names == ["mcp_call"]
    assert "mcp_call" in dispatchers
    # 기본(allowed=None)에는 여전히 노출되지 않는다(자율 디스커버리 보류, Q6).
    default_specs, _ = await build_tool_specs(_ctx(db), None)
    assert "mcp_call" not in [s["function"]["name"] for s in default_specs]


def test_mcp_tool_name_within_limit_and_unique():
    """[C2] 디스커버리 MCP 함수명이 64자 상한을 넘지 않고 충돌 시 유일화된다."""
    # 공개 이름(#152 — 옛 비공개 별칭은 0.13.0 제거)
    from biznotes_core.services.agents.tool_registry import MAX_TOOL_NAME_LEN, mcp_tool_name

    taken: dict[str, object] = {}
    # 긴 도구명 — mcp_<8hex>_<tool>이 64자를 넘는 경우.
    long_tool = "a_very_long_mcp_tool_name_that_definitely_exceeds_the_limit_xxxxxxxx"
    n1 = mcp_tool_name("deadbeef", long_tool, taken)
    assert len(n1) <= MAX_TOOL_NAME_LEN
    taken[n1] = 1
    # 동일 server_tag+tool로 절단 충돌 시 다른 이름으로 유일화.
    n2 = mcp_tool_name("deadbeef", long_tool, taken)
    assert len(n2) <= MAX_TOOL_NAME_LEN
    assert n2 != n1


def test_mcp_tool_name_normalizes_invalid_chars():
    """[C3] 함수명 패턴 밖 문자(`.`/`/` 등)를 `_`로 정규화한다."""
    import re

    from biznotes_core.services.agents.tool_registry import mcp_tool_name

    for raw in ("github.search", "filesystem/read_file", "weird name!@#"):
        name = mcp_tool_name("deadbeef", raw, {})
        # OpenAI 호환 함수명 패턴: [a-zA-Z0-9_-]{1,64}
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name), name


# ---------- call_agent → _run_subagent 가드 위임 (D3) ----------


async def test_call_agent_dispatch_executes_subagent(db, user_a, _mock_runtime_deps, mock_llm):
    """call_agent 디스패처가 _run_subagent 경유로 서브에이전트를 실행한다."""
    target = await _make_workflow_agent(db, user_a.id, scope="system")
    _, dispatchers = await build_tool_specs(_ctx(db, user_a.id), None)
    ctx = _ctx(db, user_a.id, agent_id=uuid.uuid4())
    out = await dispatchers["call_agent"](
        ctx, "call_agent", {"agent_id": str(target.id), "params": {"x": "v"}}
    )
    assert isinstance(out, str) and out  # 서브에이전트 요약 문자열


async def test_call_agent_invalid_id_returns_guard_error(db, user_a):
    """잘못된 agent_id는 _run_subagent 가드가 AgentRuntimeError를 던진다.

    (루프 호출자는 이를 잡아 도구 결과 문자열로 LLM에 전달 — 별도 루프 테스트에서 검증)
    """
    _, dispatchers = await build_tool_specs(_ctx(db, user_a.id), None)
    ctx = _ctx(db, user_a.id, agent_id=uuid.uuid4())
    with pytest.raises(runtime.AgentRuntimeError):
        await dispatchers["call_agent"](ctx, "call_agent", {"agent_id": "not-a-uuid"})


async def test_call_agent_cycle_blocked_via_run_subagent(db, user_a):
    """자기 자신 호출은 _run_subagent 순환 가드로 차단된다."""
    target = await _make_workflow_agent(db, user_a.id, scope="system")
    _, dispatchers = await build_tool_specs(_ctx(db, user_a.id), None)
    ctx = _ctx(db, user_a.id, agent_id=target.id)  # caller == target
    with pytest.raises(runtime.AgentRuntimeError, match="순환"):
        await dispatchers["call_agent"](ctx, "call_agent", {"agent_id": str(target.id)})


async def test_call_agent_invisible_target_blocked(db, user_a, user_b):
    """비인가(타인 소유 user-scope) 대상은 가시성 가드로 차단된다."""
    target = await _make_workflow_agent(db, user_b.id, scope="user")  # user_b 소유
    _, dispatchers = await build_tool_specs(_ctx(db, user_a.id), None)
    ctx = _ctx(db, user_a.id, agent_id=uuid.uuid4())
    with pytest.raises(runtime.AgentRuntimeError, match="권한"):
        await dispatchers["call_agent"](ctx, "call_agent", {"agent_id": str(target.id)})


# ---------- 도구 오류 일반화 + 인자 마스킹 (T7·SEC-07/08) ----------


def test_mask_tool_args_redacts_secret_keys():
    """authorization/token/key/password/secret/credential 키를 마스킹(중첩 포함)."""
    masked = runtime._mask_tool_args(
        {
            "Authorization": "Bearer xyz",
            "api_key": "sk-123",
            "user_token": "t",
            "password": "p",
            "query": "안전한 값",
            "nested": {"secret": "s", "ok": 1},
        }
    )
    assert masked["Authorization"] == "***"
    assert masked["api_key"] == "***"
    assert masked["user_token"] == "***"
    assert masked["password"] == "***"
    assert masked["query"] == "안전한 값"  # 비밀 아님 — 보존
    assert masked["nested"] == {"secret": "***", "ok": 1}


def test_mask_tool_args_recurses_into_lists():
    """[C5] 리스트 내부 dict의 시크릿 키도 마스킹된다."""
    masked = runtime._mask_tool_args(
        {
            "headers": [{"Authorization": "Bearer xyz"}, {"X-Trace": "ok"}],
            "items": [{"token": "t"}, "plain"],
        }
    )
    assert masked["headers"][0]["Authorization"] == "***"
    assert masked["headers"][1]["X-Trace"] == "ok"
    assert masked["items"][0]["token"] == "***"
    assert masked["items"][1] == "plain"


# ---------- MCP 디스커버리 격리 게이트 (C4·Q6) ----------


async def _make_mcp_server(db):
    from biznotes_core.models import MCPServer

    server = MCPServer(
        name=f"mcp-{uuid.uuid4().hex[:6]}",
        transport="sse",
        endpoint_or_command="https://mcp.example/sse",
        enabled=True,
    )
    db.add(server)
    await db.commit()
    await db.refresh(server)
    return server


# 옛 `MCP_READ_ONLY_TOOLS["archhub"]`의 값 — 리비전 0b6d3f57db6a가 리터럴로 시드하는 12개와 같다.
# 테스트 DB는 `create_all`이라 리비전 시드가 돌지 않으므로 픽스처가 승인 행을 넣는다(#139 B).
ARCHHUB_APPROVED = (
    "find_region",
    "building_data",
    "building_profile",
    "old_buildings",
    "district_stats",
    "building_floors",
    "price_history",
    "demolitions",
    "permits_pipeline",
    "seismic_check",
    "parcel_history",
    "temp_buildings",
)


def _spec(name: str, description: str = "d") -> dict:
    """`list_tools` 항목 — 승인 고정 해시(`_approve`)와 디스커버리 대조가 같은 스펙을 쓴다."""
    return {"name": name, "description": description, "input_schema": {"type": "object"}}


async def _approve(db, server, names=ARCHHUB_APPROVED):
    """승인 행 시드 — 코어 공개 쓰기 `set_approved_tools`(테스트 데이터 준비).

    코어 v0.21.0 #293부터 고정 해시가 있는 승인만 노출된다 — `_spec(name)`의 해시로 고정한다.
    """
    hashes = {n: mcp_tool_spec_hash(_spec(n)) for n in names}
    await set_approved_tools(
        db, server, {n: True for n in names}, admin_id=None, spec_hashes=hashes
    )
    await db.commit()


async def _make_archhub_server(db, *, approve=True):
    from biznotes_core.models import MCPServer

    server = MCPServer(
        name="archhub",
        transport="streamable_http",
        endpoint_or_command="https://mcp.example.com/archhub",
        enabled=True,
    )
    db.add(server)
    await db.commit()
    await db.refresh(server)
    if approve:
        await _approve(db, server)
    return server


async def test_mcp_discovery_blocked_for_regular_user(db, user_a, monkeypatch):
    """[C4] 일반 사용자 워크플로우의 `tools:["mcp"]`는 디스커버리가 차단된다(Q6 보류).

    admin env 자격증명 기반 전역 MCP 서버를 일반 사용자가 호출 못 하게 한다.
    승인 행이 있어도 막힌다 — 차단 근거가 승인 부재가 아니라 공개 범위(admin_only)임을 고정한다.
    """
    server = await _make_mcp_server(db)
    await _approve(db, server, ("search",))

    called = {"n": 0}

    async def fake_list(server):
        called["n"] += 1
        return [{"name": "search", "description": "d", "input_schema": {}}]

    monkeypatch.setattr("biznotes_core.services.agents.mcp_client.list_tools", fake_list)

    specs, dispatchers = await build_tool_specs(_ctx(db, user_a.id), ["mcp"])
    assert called["n"] == 0, "일반 사용자에게는 디스커버리가 호출되지 않아야 한다"
    assert specs == [] and dispatchers == {}


async def test_mcp_discovery_allowed_for_admin(db, admin_user, monkeypatch):
    """[C4] admin 실행은 MCP 디스커버리가 허용된다."""
    server = await _make_archhub_server(db)

    async def fake_list(s):
        return [
            _spec("find_region"),
            _spec("building_profile"),
            _spec("delete_building", "mutating"),
        ]

    monkeypatch.setattr("biznotes_core.services.agents.mcp_client.list_tools", fake_list)

    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=admin_user.id,
        user_role="admin",
    )
    specs, dispatchers = await build_tool_specs(ctx, ["mcp"])
    names = [s["function"]["name"] for s in specs]
    assert names == [
        f"mcp_{str(server.id).replace('-', '')[:8]}_find_region",
        f"mcp_{str(server.id).replace('-', '')[:8]}_building_profile",
    ]
    assert "delete_building" not in " ".join(names)
    # 승인 정본은 DB(#139 B) — 승인 read-only 행에 두 도구가 있고 delete_building은 없다
    approved = (await approved_tools(db, [server.id]))[server.id]
    assert approved >= {"find_region", "building_profile"}
    assert "delete_building" not in approved
    import re

    # 노출 이름은 모두 OpenAI 호환 패턴을 만족하고 디스패처가 바인딩된다.
    server_tag = str(server.id).replace("-", "")[:8]
    assert any(n.startswith(f"mcp_{server_tag}_") for n in names)
    for n in names:
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", n)
        assert n in dispatchers


async def test_mcp_discovery_allowed_for_system_run(db, monkeypatch):
    """[C4] 시스템 실행(user_id=None)은 MCP 디스커버리가 허용된다."""
    server = await _make_archhub_server(db)

    async def fake_list(s):
        return [_spec("find_region")]

    monkeypatch.setattr("biznotes_core.services.agents.mcp_client.list_tools", fake_list)

    specs, _ = await build_tool_specs(_ctx(db, user_id=None), ["mcp"])
    # 시스템 실행은 디스커버리 허용 — 승인된 archhub read-only 도구만 노출.
    assert specs, "시스템 실행은 MCP 디스커버리가 허용되어야 한다"
    assert specs[0]["function"]["name"] == (
        f"mcp_{str(server.id).replace('-', '')[:8]}_find_region"
    )


async def test_mcp_dispatcher_raises_tool_unavailable_when_server_missing(db):
    ctx = _ctx(db)
    dispatcher = _make_mcp_dispatcher(uuid.uuid4(), "find_region")

    with pytest.raises(ToolUnavailableError, match="MCP 서버를 찾을 수 없거나 비활성화됨"):
        await dispatcher(ctx, "mcp_missing_find_region", {})

    assert ctx.tool_evidence == [
        {"kind": "mcp", "server": None, "tool": "find_region", "status": "failed"}
    ]


async def test_mcp_dispatcher_raises_tool_unavailable_when_server_disabled(db):
    server = await _make_archhub_server(db)
    server.enabled = False
    await db.commit()
    ctx = _ctx(db)
    dispatcher = _make_mcp_dispatcher(server.id, "find_region")

    with pytest.raises(ToolUnavailableError, match="MCP 서버를 찾을 수 없거나 비활성화됨"):
        await dispatcher(ctx, "mcp_archhub_find_region", {})

    assert ctx.tool_evidence == [
        {"kind": "mcp", "server": "archhub", "tool": "find_region", "status": "failed"}
    ]


async def test_mcp_dispatcher_raises_tool_unavailable_when_tool_disallowed(db):
    server = await _make_archhub_server(db)
    ctx = _ctx(db)
    dispatcher = _make_mcp_dispatcher(server.id, "delete_building")

    with pytest.raises(ToolUnavailableError, match="MCP 도구가 read-only 승인 목록에 없습니다"):
        await dispatcher(ctx, "mcp_archhub_delete_building", {})

    assert ctx.tool_evidence == [
        {
            "kind": "mcp",
            "server": "archhub",
            "tool": "delete_building",
            "status": "failed",
        }
    ]


async def test_mcp_dispatcher_appends_failed_evidence_on_call_tool_error(db, monkeypatch):
    server = await _make_archhub_server(db)

    seen: list[dict] = []

    # 코어 v0.11.0 #162 — 디스패처가 `meta=caller_meta(...)` 키워드로 부른다(**_kw로 받는다)
    async def fail_call(server_, tool_name, arguments, **_kw):
        seen.append(_kw)
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr("biznotes_core.services.agents.mcp_client.call_tool", fail_call)
    ctx = _ctx(db)
    dispatcher = _make_mcp_dispatcher(server.id, "find_region")

    with pytest.raises(RuntimeError, match="upstream unavailable"):
        await dispatcher(ctx, "mcp_archhub_find_region", {})

    assert ctx.tool_evidence == [
        {"kind": "mcp", "server": "archhub", "tool": "find_region", "status": "failed"}
    ]
    assert seen == [{"meta": {MCP_META_RUN_ID: str(ctx.run_id)}}]  # 실제로 meta가 실렸다


async def test_mcp_dispatcher_passes_caller_meta(db, admin_user, monkeypatch):
    """#162 — `_meta`의 run_id는 모든 서버, user_id는 stdio 서버에만 실린다(디스패처 실경로)."""
    from biznotes_core.models import MCPServer

    http_server = await _make_archhub_server(db)
    stdio_server = MCPServer(
        name=f"local-{uuid.uuid4().hex[:6]}",
        transport="stdio",
        endpoint_or_command="/app/.venv/bin/python /app/scripts/demo_mcp_server.py",
        enabled=True,
    )
    db.add(stdio_server)
    await db.commit()
    await db.refresh(stdio_server)
    await _approve(db, stdio_server, ("word_count",))

    calls: list[tuple[str, str, dict]] = []

    async def record_call(server_, tool_name, arguments, *, meta=None):
        calls.append((server_.name, tool_name, meta))
        return "ok"

    monkeypatch.setattr("biznotes_core.services.agents.mcp_client.call_tool", record_call)
    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=admin_user.id,
        user_role="admin",
    )

    assert await _make_mcp_dispatcher(http_server.id, "find_region")(ctx, "x", {}) == "ok"
    assert await _make_mcp_dispatcher(stdio_server.id, "word_count")(ctx, "y", {}) == "ok"

    assert calls == [
        ("archhub", "find_region", {MCP_META_RUN_ID: str(ctx.run_id)}),
        (
            stdio_server.name,
            "word_count",
            {MCP_META_RUN_ID: str(ctx.run_id), MCP_META_USER_ID: str(admin_user.id)},
        ),
    ]


async def test_tool_error_generalized_to_llm(db, user_a, _mock_runtime_deps, mock_llm):
    """도구 실행 예외는 내부 상세 대신 일반화 메시지로 LLM(tool 메시지)에 전달된다.

    rag_search 디스패치가 내부 호스트가 담긴 예외를 던지도록 모킹 →
    루프는 이를 잡아 "도구 '...' 실행에 실패했습니다."로 치환해야 한다.
    """
    sent_messages: list[dict] = []

    async def boom_dispatch(ctx, name, args):
        raise RuntimeError("connect to internal-db.prod.svc:5432 failed")

    # 디스패처 seam(런타임 모듈 함수)을 교체 — 레지스트리 builtin_dispatcher가 호출 시점 조회.
    orig = runtime._dispatch_builtin_tool
    runtime._dispatch_builtin_tool = boom_dispatch  # type: ignore[assignment]

    from biznotes_core.services.gateway import llm as gateway_llm

    real_complete = gateway_llm.complete_with_tools

    async def spy_complete(db_, model_id, messages, tools, **kwargs):
        # 도구 결과가 주입된 이후(턴2) messages 스냅샷을 보존해 tool 메시지 내용을 검사.
        for m in messages:
            if m.get("role") == "tool":
                sent_messages.append(dict(m))
        return await real_complete(db_, model_id, messages, tools, **kwargs)

    gateway_llm.complete_with_tools = spy_complete  # type: ignore[assignment]
    try:
        ctx = _ctx(db, user_a.id)
        definition = {
            "nodes": [
                {
                    "id": "gen",
                    "type": "llm",
                    "prompt": "조사",
                    "tools": ["rag_search"],
                }
            ],
            "edges": [],
        }
        await execute_agent(ctx, "workflow", definition)
    finally:
        runtime._dispatch_builtin_tool = orig  # type: ignore[assignment]
        gateway_llm.complete_with_tools = real_complete  # type: ignore[assignment]

    assert sent_messages, "tool 결과 메시지가 LLM에 전달되어야 한다"
    tool_content = sent_messages[0]["content"]
    assert "실행에 실패했습니다" in tool_content
    # 내부 호스트/포트가 LLM 입력에 새어나가지 않아야 한다.
    assert "internal-db.prod.svc" not in tool_content
    assert "5432" not in tool_content


# ---------- R5-4: chat-safe allowlist (HCI-R5-4) ----------


def test_chat_safe_tools_includes_local_sources_approved_mcp_and_skills():
    """채팅 자율 모드는 소스·승인 MCP·published system skill·산출물 읽기를 사용한다."""
    from biznotes_core.services.agents.tool_registry import CHAT_SAFE_TOOLS

    assert CHAT_SAFE_TOOLS == (
        "rag_search",
        "source_load",
        # 코어 v0.30.0 #447 — 소스 목록 도구(읽기 전용)
        "source_list",
        "mcp",
        "run_skill",
        # biznotes-core 표준 계획 S-HCI-10(인간 승인 2026-09-22)
        "activate_skill",
        "read_skill_file",
        # 코어 v0.10.12 #133 — 읽기 전용 산출물 도구(core-v0.11.0-upgrade HCI-12 (a))
        "artifact_list",
        "artifact_read",
    )


def test_chat_safe_tools_excludes_web_search_and_call_agent():
    """blocking(CR-R5-01) 회귀 가드: side_effect=False인 web_search가 채팅에 노출되면
    외부 웹 검색까지 확장된다. call_agent는 한도·감사 우회. 둘 다 allowlist 밖."""
    from biznotes_core.services.agents.tool_registry import CHAT_SAFE_TOOLS

    assert "web_search" not in CHAT_SAFE_TOOLS
    assert "call_agent" not in CHAT_SAFE_TOOLS


def test_assert_no_side_effect_tools_passes_for_chat_safe():
    """현 chat-safe allowlist는 부작용 도구 혼입 가드를 통과한다."""
    from biznotes_core.services.agents.tool_registry import (
        CHAT_SAFE_TOOLS,
        assert_no_side_effect_tools,
    )

    assert_no_side_effect_tools(CHAT_SAFE_TOOLS)  # 예외 없음


def test_assert_no_side_effect_tools_rejects_side_effect_tool():
    """부작용 도구(register_source)가 allowlist에 혼입되면 즉시 실패."""
    from biznotes_core.services.agents.tool_registry import assert_no_side_effect_tools

    with pytest.raises(ValueError, match="부작용 도구 혼입"):
        assert_no_side_effect_tools(("rag_search", "register_source"))


# ---------- core-v0.30.0-upgrade #284: 채팅 소스 도달 도구의 범위 (비인가) ----------


async def _notebook_with_source(db, owner, title: str):
    from biznotes_core.models import Notebook, Source

    notebook = Notebook(owner_id=owner.id, title=f"nb-{uuid.uuid4().hex[:6]}")
    db.add(notebook)
    await db.flush()
    source = Source(notebook_id=notebook.id, type="text", title=title, status="ready")
    db.add(source)
    await db.commit()
    return notebook, source


async def test_chat_source_list_never_reaches_other_notebooks(db, user_a, user_b):
    """`source_list`는 run 노트북 ∩ 선택 범위만 낸다.

    타인 노트북 id를 범위에 넣어도 보이지 않는다."""
    mine, my_source = await _notebook_with_source(db, user_a, "내 소스")
    _theirs, their_source = await _notebook_with_source(db, user_b, "남의 비밀 소스")

    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=mine.id, db=db, params={}, user_id=user_a.id)
    _specs, dispatchers = await build_tool_specs(ctx, list(CHAT_SAFE_TOOLS))
    listed = await dispatchers["source_list"](ctx, "source_list", {})
    assert "내 소스" in listed
    assert "남의 비밀 소스" not in listed and str(their_source.id) not in listed

    # 범위(source_ids)에 타인 소스를 넣어도 run 노트북 밖은 넓히지 않는다
    scoped = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=mine.id,
        db=db,
        params={},
        user_id=user_a.id,
        source_ids=[their_source.id],
    )
    listed = await dispatchers["source_list"](scoped, "source_list", {})
    assert "남의 비밀 소스" not in listed and str(their_source.id) not in listed


async def test_chat_source_load_ignores_foreign_source_ids(db, user_a, user_b):
    """`source_load(source_ids=[타인 소스])`는 읽지 않는다 — 존재 여부도 드러내지 않는다."""
    mine, _my_source = await _notebook_with_source(db, user_a, "내 소스")
    _theirs, their_source = await _notebook_with_source(db, user_b, "남의 비밀 소스")

    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=mine.id, db=db, params={}, user_id=user_a.id)
    _specs, dispatchers = await build_tool_specs(ctx, list(CHAT_SAFE_TOOLS))
    loaded = await dispatchers["source_load"](
        ctx, "source_load", {"source_ids": [str(their_source.id)]}
    )
    assert "남의 비밀 소스" not in loaded
