"""biznotes-core 표준 스킬 런타임(Phase 3)의 제품 통합 — 워커 재개·자율 채팅·산출물 서빙.

코어 규칙 자체는 biznotes-core `tests/test_skill_runtime.py`가 고정한다. 여기서는 이 제품의
조립(워커 `_run`, 채팅 라우터, 산출물 다운로드)이 표준 런타임을 올바르게 태우는지 본다.
"""

import json
import uuid
from collections.abc import Iterator
from typing import Any

import anyio
import httpx
import pytest
from biznotes_core.config import get_settings, reset_settings
from biznotes_core.models import AgentRun, AgentRunApproval, Notebook
from biznotes_core.models.skill import SKILL_RUNTIME_STANDARD
from biznotes_core.services.agents import runtime as agents_runtime
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult
from biznotes_core.services.skills import package as pkg
from biznotes_core.services.skills import runtime as skill_runtime
from biznotes_core.services.skills import service as svc
from sqlalchemy import select

from tests.conftest import bearer, login
from tests.test_chat_api import _parse_sse, _setup_notebook_session, llm_seed  # noqa: F401

SKILL_MD = (
    "---\nname: brief-writer\ndescription: 짧은 브리핑을 쓴다\n---\n\n"
    "# 브리핑\n\n항상 답변 끝에 [BRIEF-OK]를 붙인다. 자세한 형식은 references/format.md.\n"
)


@pytest.fixture
def runtime_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SKILL_STANDARD_RUNTIME_ENABLED", "true")
    reset_settings()
    # 제품 설정 클래스가 그대로여야 한다(매니페스트 파일명 등)
    assert get_settings().skill_manifest_filename == "biznotes.json"
    yield
    monkeypatch.delenv("SKILL_STANDARD_RUNTIME_ENABLED")
    reset_settings()


def _turn(name: str, args: dict[str, Any]) -> ToolTurnResult:
    call_id = uuid.uuid4().hex[:9]
    return ToolTurnResult(
        content="",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=args)],
        assistant_message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
        },
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        model=None,
    )


def _final(text: str) -> ToolTurnResult:
    return ToolTurnResult(
        content=text,
        tool_calls=[],
        assistant_message={"role": "assistant", "content": text},
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        model=None,
    )


def _script(monkeypatch: pytest.MonkeyPatch, turns: list[ToolTurnResult]) -> list[dict]:
    seen: list[dict] = []

    async def fake(_db, _model_id, messages, tools, **_kw):
        seen.append({"messages": [dict(m) for m in messages], "tools": list(tools)})
        return turns[min(len(seen) - 1, len(turns) - 1)]

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake)
    return seen


async def _standard_skill(db, owner):
    skill = await svc.create_skill(db, owner, "brief-writer", "짧은 브리핑을 쓴다")
    await anyio.to_thread.run_sync(pkg.put_draft_file, skill.id, "SKILL.md", SKILL_MD.encode())
    await anyio.to_thread.run_sync(
        pkg.put_draft_file, skill.id, "references/format.md", "- 세 줄 요약".encode()
    )
    version, wrapper = await svc.freeze_skill(db, skill, owner)
    await db.commit()
    assert version.runtime_profile == SKILL_RUNTIME_STANDARD
    return skill, version, wrapper


async def test_worker_resumes_a_standard_skill_after_approval(
    db, user_a, mem_storage, mock_llm, runtime_on, monkeypatch
):
    """승인 뒤 재개가 같은 version의 표준 런타임으로 이어진다(수락 기준 27).

    재개를 스킬 wrapper의 빈 정의로 `execute_agent`에 넘기면 구 prompt 경로가 돈다 — 워커가
    `execute_skill_package(..., resume_checkpoint=…)`로 들어가야 한다.
    """
    import app.workers.tasks.agent as agent_task

    _, version, wrapper = await _standard_skill(db, user_a)
    notebook = Notebook(owner_id=user_a.id, title="std-nb")
    db.add(notebook)
    await db.flush()
    run = AgentRun(
        agent_id=wrapper.id,
        notebook_id=notebook.id,
        user_id=user_a.id,
        params={"request": "이번 주 요약"},
        status="queued",
        skill_version_id=version.id,
    )
    db.add(run)
    await db.commit()

    # 부작용 도구를 하나 노출해 승인 게이트를 태운다(Phase 3 직접 실행 도구는 모두 읽기 전용)
    monkeypatch.setattr(
        skill_runtime, "DIRECT_RUN_TOOLS", (*skill_runtime.DIRECT_RUN_TOOLS, "http_request")
    )

    async def http(_ctx, _name, _args):
        return "200 OK"

    monkeypatch.setattr(agents_runtime, "_dispatch_builtin_tool", http)
    monkeypatch.setattr(agent_task, "_publish", lambda run_id, event: None)
    seen = _script(
        monkeypatch,
        [
            _turn("read_skill_file", {"name": "brief-writer", "path": "references/format.md"}),
            _turn("http_request", {"url": "https://example.com"}),
            _turn("read_skill_file", {"name": "brief-writer", "path": "references/format.md"}),
            _final("세 줄 요약 [BRIEF-OK]"),
        ],
    )

    assert await agent_task._run(str(run.id)) == {"ok": True, "awaiting_approval": True}
    approval = (
        await db.execute(select(AgentRunApproval).where(AgentRunApproval.run_id == run.id))
    ).scalar_one()
    assert approval.tool_name == "http_request"
    approval.status = "approved"
    await db.commit()

    assert await agent_task._run(str(run.id)) == {"ok": True}
    await db.refresh(run)
    assert run.status == "succeeded", run.error
    assert run.result_summary == "세 줄 요약 [BRIEF-OK]"
    assert [a["name"] for a in run.artifact_keys] == ["result.md"]
    # 재개 뒤에도 활성화가 유지되어 파일을 다시 읽었다
    assert "- 세 줄 요약" in [m for m in seen[-1]["messages"] if m.get("role") == "tool"][-1][
        "content"
    ]


async def test_autonomous_chat_activates_the_users_standard_skill(
    client: httpx.AsyncClient, db, user_a, llm_seed, mem_storage, runtime_on, monkeypatch  # noqa: F811
):
    """채팅 경로의 두 도구(S-HCI-10) — 본인 스킬이 카탈로그에 있고 지침 블록이 모델에 간다."""
    await _standard_skill(db, user_a)
    seen = _script(
        monkeypatch,
        [_turn("activate_skill", {"name": "brief-writer"}), _final("요약입니다 [BRIEF-OK]")],
    )
    token = await login(client, user_a.email)
    _, session_id = await _setup_notebook_session(client, token)

    response = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "브리핑 스킬로 정리해줘", "autonomous": True},
        headers=bearer(token),
    )

    assert response.status_code == 200, response.text
    tools = {t["function"]["name"] for t in seen[0]["tools"]}
    assert {"activate_skill", "read_skill_file"} <= tools
    assert "http_request" not in tools and "call_agent" not in tools
    tool_message = [m for m in seen[1]["messages"] if m.get("role") == "tool"][-1]["content"]
    assert tool_message.startswith('<skill_content name="brief-writer">')
    done = next(event for event in _parse_sse(response.text) if event["type"] == "done")
    assert any(
        item["kind"] == "skill" and item["name"] == "brief-writer" for item in done["evidence"]
    )


@pytest.mark.parametrize("caller", ["owner", "other"])
async def test_autonomous_chat_activates_an_imported_skill_only_for_its_owner(
    client: httpx.AsyncClient,
    db,
    user_a,
    user_b,
    llm_seed,  # noqa: F811
    mem_storage,
    runtime_on,
    monkeypatch,
    caller,
):
    """#65 — 가져온(imported) private 표준 스킬은 소유자의 자율 채팅에서만 켜진다.

    소유자(양성 대조)는 카탈로그로 이름·설명을 받고 `activate_skill`로 지침 블록을 받는다. 다른
    사용자에게는 이름·설명이 모델에 가는 어디에도(도구 정의·메시지) 없고, 이름을 맞혀 불러도
    지침 블록이 돌아오지 않는다.
    """
    from biznotes_core.models import User
    from biznotes_core.services.skills import service as skill_service

    from tests.test_skills import make_zip

    owner = await db.get(User, user_a.id)
    skill = await skill_service.import_zip(
        db, owner, make_zip({"brief-writer/SKILL.md": SKILL_MD})
    )
    version, _wrapper = await skill_service.freeze_skill(db, skill, owner)
    await db.commit()
    assert skill.origin == "imported"
    assert version.runtime_profile == SKILL_RUNTIME_STANDARD
    seen = _script(
        monkeypatch,
        [_turn("activate_skill", {"name": "brief-writer"}), _final("답변 [BRIEF-OK]")],
    )
    caller_user = user_a if caller == "owner" else user_b
    token = await login(client, caller_user.email)
    _, session_id = await _setup_notebook_session(client, token)

    response = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "브리핑 스킬로 정리해줘", "autonomous": True},
        headers=bearer(token),
    )

    assert response.status_code == 200, response.text
    first_request = json.dumps(seen[0], ensure_ascii=False)
    tool_message = [m for m in seen[1]["messages"] if m.get("role") == "tool"][-1]["content"]
    done = next(event for event in _parse_sse(response.text) if event["type"] == "done")
    skill_evidence = [item for item in done["evidence"] if item["kind"] == "skill"]
    if caller == "owner":
        assert "brief-writer" in first_request and "짧은 브리핑을 쓴다" in first_request
        assert tool_message.startswith('<skill_content name="brief-writer">')
        assert [item["name"] for item in skill_evidence] == ["brief-writer"]
    else:
        assert "brief-writer" not in first_request
        assert "짧은 브리핑을 쓴다" not in first_request
        assert not tool_message.startswith("<skill_content")
        assert "[BRIEF-OK]" not in tool_message
        assert skill_evidence == []


async def test_artifact_download_follows_safe_serving_rules(
    client: httpx.AsyncClient, db, user_a, mem_storage
):
    """수락 기준 31 — png는 inline·바이트 동일, html·svg는 attachment·octet-stream, 모두 nosniff."""
    notebook = Notebook(owner_id=user_a.id, title="artifact-nb")
    db.add(notebook)
    await db.flush()
    from biznotes_core.models import Agent

    agent = Agent(
        name=f"artifact-{uuid.uuid4().hex[:6]}",
        kind="workflow",
        icon="⚙️",
        definition={"nodes": [], "edges": []},
        params_schema={},
        scope="user",
        owner_id=user_a.id,
        enabled=True,
    )
    db.add(agent)
    await db.flush()
    png = b"\x89PNG\r\n\x1a\n\x00\xff"
    run_id = uuid.uuid4()
    artifacts = {
        "chart.png": ("image/png", png),
        "page.html": ("text/html", b"<script>alert(1)</script>"),
        "icon.svg": ("image/svg+xml", b"<svg onload=alert(1)/>"),
    }
    bucket = get_settings().storage_bucket_artifacts
    for name, (_, data) in artifacts.items():
        mem_storage[(bucket, f"{run_id}/{name}")] = data
    run = AgentRun(
        id=run_id,
        agent_id=agent.id,
        notebook_id=notebook.id,
        user_id=user_a.id,
        params={},
        status="succeeded",
        artifact_keys=[
            {"key": f"{run_id}/{name}", "name": name, "content_type": ctype}
            for name, (ctype, _) in artifacts.items()
        ],
    )
    db.add(run)
    await db.commit()
    token = await login(client, user_a.email)

    res = await client.get(
        f"/api/v1/agent-runs/{run_id}/artifacts/chart.png", headers=bearer(token)
    )
    assert res.status_code == 200
    assert res.content == png
    assert res.headers["content-type"] == "image/png"
    assert "content-disposition" not in res.headers
    assert res.headers["x-content-type-options"] == "nosniff"

    for name in ("page.html", "icon.svg"):
        res = await client.get(
            f"/api/v1/agent-runs/{run_id}/artifacts/{name}", headers=bearer(token)
        )
        assert res.status_code == 200
        assert res.headers["content-type"] == "application/octet-stream"
        assert res.headers["content-disposition"].startswith("attachment;")
        assert res.headers["x-content-type-options"] == "nosniff"


def test_skill_creator_writes_standard_skills_when_the_runtime_is_on(runtime_on):
    """새 스킬 기본 형태 전환(S-HCI-6) — 생성 도우미 출력이 매니페스트 없는 표준 스킬이 된다."""
    from biznotes_core.services.skills.report import evaluate_package

    from scripts.seed import SKILL_CREATOR_BODY, skill_creator_body

    body = skill_creator_body()
    assert body != SKILL_CREATOR_BODY
    assert "biznotes_json" not in body

    artifact = json.dumps({"skill_md": SKILL_MD}, ensure_ascii=False).encode()
    files = svc.parse_package_artifact(artifact)
    assert set(files) == {"SKILL.md"}
    assert evaluate_package(files).runtime_profile == SKILL_RUNTIME_STANDARD


def test_skill_creator_keeps_the_manifest_body_when_the_runtime_is_off():
    reset_settings()
    from scripts.seed import SKILL_CREATOR_BODY, skill_creator_body

    assert skill_creator_body() == SKILL_CREATOR_BODY
