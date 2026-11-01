"""biznotes-core 번들 스크립트 실행(Phase 4)의 제품 통합 — 워커 승인 재개·저장 검증·채팅 제외.

격리 자체는 biznotes-core의 샌드박스 시험과 스택 스모크가 실측한다. 여기서는 이 제품의 조립
(워커 `_run`, 정의 저장 검증, 채팅 allowlist)이 `run_skill_script`를 올바르게 다루는지 본다.
샌드박스 워커는 기록형 가짜다 — 발행 작업 내용을 확인하고 산출물 슬롯 하나를 채운다.
"""

from collections.abc import Iterator
from typing import Any

import anyio
import pytest
from biznotes_core.config import reset_settings
from biznotes_core.models import AgentRun, AgentRunApproval, Notebook, SkillReviewRequest
from biznotes_core.services import storage as core_storage
from biznotes_core.services.skills import package as pkg
from biznotes_core.services.skills import sandbox
from biznotes_core.services.skills import service as svc
from sqlalchemy import select

from tests.test_skill_standard_runtime import _final, _script, _turn

SKILL_MD = (
    "---\nname: form-filler\ndescription: 양식을 채운다\n---\n\n"
    "# 양식\n\nscripts/fill.py로 PDF를 만든다.\n"
)
SCRIPT = b"import sys\nopen('outputs/form.pdf', 'wb').write(b'%PDF-1.4')\n"


@pytest.fixture
def scripts_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SKILL_STANDARD_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("SKILL_SCRIPT_BACKEND", "process")
    reset_settings()
    yield
    reset_settings()


class FakeSandbox:
    def __init__(self, store: dict[tuple[str, str], bytes]) -> None:
        self.store = store
        self.jobs: list[dict[str, Any]] = []

    async def run(self, job: dict[str, Any], **_: float) -> dict[str, Any]:
        self.jobs.append(job)
        key = job["output_slots"][0].split("/artifacts/", 1)[1].split("?", 1)[0]
        self.store[("artifacts", key)] = b"%PDF-1.4"
        return {
            "status": "completed",
            "exit_code": 0,
            "stdout": "",
            "outputs": [{"slot": 0, "name": "form.pdf", "size": 8}],
        }


@pytest.fixture
def fake_sandbox(
    monkeypatch: pytest.MonkeyPatch, mem_storage: dict[tuple[str, str], bytes]
) -> FakeSandbox:
    box = FakeSandbox(mem_storage)
    monkeypatch.setattr(sandbox, "get_sandbox", lambda: box)
    monkeypatch.setattr(
        core_storage,
        "presigned_url_for",
        lambda method, bucket, key, **_: f"http://rustfs:9000/{bucket}/{key}?{method}",
    )

    def stat(bucket: str, key: str) -> int:
        return len(mem_storage[(bucket, key)])

    monkeypatch.setattr(core_storage, "stat_object_size", stat)
    return box


async def _scripted_skill(db, owner, *, approve: bool):
    skill = await svc.create_skill(db, owner, "form-filler", "양식을 채운다")
    await anyio.to_thread.run_sync(pkg.put_draft_file, skill.id, "SKILL.md", SKILL_MD.encode())
    await anyio.to_thread.run_sync(pkg.put_draft_file, skill.id, "scripts/fill.py", SCRIPT)
    version, wrapper = await svc.freeze_skill(db, skill, owner)
    if approve:
        db.add(
            SkillReviewRequest(
                version_id=version.id, kind="scripts", status="approved", requested_by=owner.id
            )
        )
    await db.commit()
    return version, wrapper


async def _queued_run(db, owner, version, wrapper) -> AgentRun:
    notebook = Notebook(owner_id=owner.id, title="scripts-nb")
    db.add(notebook)
    await db.flush()
    run = AgentRun(
        agent_id=wrapper.id,
        notebook_id=notebook.id,
        user_id=owner.id,
        params={"request": "양식 채워 줘"},
        status="queued",
        skill_version_id=version.id,
    )
    db.add(run)
    await db.commit()
    return run


async def test_worker_waits_for_approval_then_runs_the_script(
    db, user_a, mem_storage, mock_llm, scripts_on, fake_sandbox, monkeypatch
):
    """승인 대기 → 승인 → 재개에서 스크립트가 한 번 발행되고 산출물이 run에 붙는다."""
    import app.workers.tasks.agent as agent_task

    version, wrapper = await _scripted_skill(db, user_a, approve=True)
    run = await _queued_run(db, user_a, version, wrapper)
    monkeypatch.setattr(agent_task, "_publish", lambda run_id, event: None)
    seen = _script(
        monkeypatch,
        [
            _turn("run_skill_script", {"name": "form-filler", "script": "scripts/fill.py"}),
            _final("양식을 채웠습니다"),
        ],
    )

    assert await agent_task._run(str(run.id)) == {"ok": True, "awaiting_approval": True}
    assert fake_sandbox.jobs == []  # 승인 전에는 발행하지 않는다
    tools = [t["function"]["name"] for t in seen[0]["tools"]]
    assert "run_skill_script" in tools
    approval = (
        await db.execute(select(AgentRunApproval).where(AgentRunApproval.run_id == run.id))
    ).scalar_one()
    assert approval.tool_name == "run_skill_script"
    approval.status = "approved"
    await db.commit()

    assert await agent_task._run(str(run.id)) == {"ok": True}
    await db.refresh(run)
    assert run.status == "succeeded", run.error
    assert len(fake_sandbox.jobs) == 1
    assert fake_sandbox.jobs[0]["script"] == "scripts/fill.py"
    names = [a["name"] for a in run.artifact_keys]
    assert names == ["form.pdf", "result.md"]
    pdf = next(a for a in run.artifact_keys if a["name"] == "form.pdf")
    assert pdf["content_type"] == "application/pdf"
    assert pdf["key"].startswith(f"{run.id}/scripts/")


async def test_unapproved_version_does_not_see_the_tool(
    db, user_a, mem_storage, mock_llm, scripts_on, fake_sandbox, monkeypatch
):
    import app.workers.tasks.agent as agent_task

    version, wrapper = await _scripted_skill(db, user_a, approve=False)
    run = await _queued_run(db, user_a, version, wrapper)
    monkeypatch.setattr(agent_task, "_publish", lambda run_id, event: None)
    seen = _script(monkeypatch, [_final("스크립트 없이 끝냈습니다")])
    assert await agent_task._run(str(run.id)) == {"ok": True}
    tools = [t["function"]["name"] for t in seen[0]["tools"]]
    assert "run_skill_script" not in tools
    assert fake_sandbox.jobs == []


def test_chat_allowlist_excludes_script_execution():
    from biznotes_core.services.agents.tool_registry import CHAT_SAFE_TOOLS

    assert "run_skill_script" not in CHAT_SAFE_TOOLS


def test_admin_definition_rejects_auto_approving_scripts():
    from fastapi import HTTPException

    from app.api.v1.admin.agents import _validate_definition

    definition = {
        "nodes": [
            {
                "id": "r",
                "type": "react_agent",
                "goal": "x",
                "auto_approve_tools": {"run_skill_script": {}},
            }
        ],
        "edges": [],
    }
    with pytest.raises(HTTPException) as exc_info:
        _validate_definition("workflow", definition)
    assert exc_info.value.status_code == 422
    assert "run_skill_script" in str(exc_info.value.detail)


def test_workflow_structure_rejects_auto_approving_scripts():
    from fastapi import HTTPException

    from app.api.v1.workflows import _validate_structure

    definition = {
        "nodes": [
            {
                "id": "r",
                "type": "react_agent",
                "goal": "x",
                "auto_approve_tools": {"run_skill_script": {}},
            }
        ],
        "edges": [],
    }
    with pytest.raises(HTTPException) as exc_info:
        _validate_structure(definition, {})
    assert exc_info.value.status_code == 422
    assert "run_skill_script" in str(exc_info.value.detail)
