"""다단계 스킬 실행의 끝까지 도는 동작(#202).

계획 `docs/plans/skill-stages/plan.md`의 AC2·AC3·AC5·AC8·AC11을 확인한다.
선언 파싱·그래프 모양은 `tests/test_skill_stages.py`가 본다. 여기서는 **실제 LangGraph를
돌려** 연쇄와 중단이 실제로 동작하는지 본다.

PG 테스트다 — `tests/conftest.py` docstring의 전제(PostgreSQL·Valkey)가 필요하다.
"""

import json
import uuid

import pytest

from biznotes_core.models import Skill, SkillVersion
from biznotes_core.models.skill import SKILL_RUNTIME_STANDARD
from biznotes_core.services.agents.react.loop import BUDGET_PARTIAL_NOTE
from biznotes_core.services.agents.runtime import RunContext, execute_agent
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.llm import ToolTurnResult
from biznotes_core.services.skills.runtime import staged_run_definition
from biznotes_core.services.skills.stages import Stage


@pytest.fixture(autouse=True)
def _standard_runtime_on(monkeypatch) -> None:
    """표준 런타임은 기본 꺼짐이다(`skill_standard_runtime_enabled=False`).

    꺼져 있으면 스킬 도구가 아예 등록되지 않아 `_skill_preactivate`가 실패한다.
    """
    from biznotes_core.config import reset_settings

    monkeypatch.setenv("SKILL_STANDARD_RUNTIME_ENABLED", "true")
    reset_settings()
    yield
    reset_settings()


async def _standard_skill(db, owner_id) -> SkillVersion:
    """`_skill_preactivate`의 강제 `activate_skill`가 통과할 수 있는 표준 version 행.

    도구 등록에는 `ctx.skill_pins`만 있으면 되지만, 강제 호출은 version까지 찾으므로
    행이 있어야 한다.
    """
    skill = Skill(
        name=f"s-{uuid.uuid4().hex[:6]}",
        slug=f"s-{uuid.uuid4().hex[:6]}",
        description="테스트 스킬",
        visibility="system",
        enabled=True,
        owner_id=owner_id,
    )
    db.add(skill)
    await db.flush()
    version = SkillVersion(
        skill_id=skill.id,
        version=1,
        package_uri="",
        skill_md_hash="x" * 64,
        package_hash="y" * 64,
        status="frozen",
        validation_profile="standard_v1",
        runtime_profile=SKILL_RUNTIME_STANDARD,
        standard_metadata={"name": "p", "description": "d"},
    )
    db.add(version)
    await db.commit()
    await db.refresh(version)
    return version


def _ctx(db, user_id, version) -> RunContext:
    ctx = RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={},
        user_id=user_id,
    )
    # 고정이면 도구가 등록되고, 활성 상태면 `activate_skill`이 "이미 활성화"로 즉시 반환한다.
    # 다단계 2단계 이후가 받는 경로와 같아(1단계의 완전한 활성화는 기존 테스트가 본다)
    # 이 테스트가 보는 연쇄·지침 전달과 분리된다
    ctx.skill_pins["p"] = str(version.id)
    ctx.skill_activations["p"] = str(version.id)
    return ctx


def _final(text: str, total: int = 2) -> ToolTurnResult:
    return ToolTurnResult(
        content=text,
        tool_calls=[],
        assistant_message={"role": "assistant", "content": text},
        usage={"prompt_tokens": total, "completion_tokens": 0, "total_tokens": total},
        model=None,
    )


def _scripted(monkeypatch, script, record=None):
    """complete_with_tools를 script(호출 인덱스) → ToolTurnResult로 교체한다."""
    calls = {"n": 0}

    async def fake(db, model_id, messages, tools, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        if record is not None:
            record.append(messages)
        return script(i)

    monkeypatch.setattr(gateway_llm, "complete_with_tools", fake)
    return calls


def _stages(*ids: str) -> list[Stage]:
    return [
        Stage(id=i, goal=f"{i} 작업", artifact_name=f"{i}.md") for i in ids
    ]


def _run(
    stages, monkeypatch, *, skill_body: str = "", total_tokens: int | None = None
) -> tuple[dict, list[list[dict]]]:
    defn = staged_run_definition(
        stages, skill_name="p", skill_body=skill_body, total_tokens=total_tokens
    )
    return defn, [n for n in defn["nodes"]]


# ── AC2·AC3: 연쇄가 돌아가고 단계마다 산출물이 남는다 ───────────────────────


async def test_stages_chain_and_each_one_leaves_an_artifact(
    db, user_a, _mock_runtime_deps, mock_llm, monkeypatch
) -> None:
    texts = ["브리프 완료", "개요 완료", "초안 완료", "검토 완료"]
    seen: list[list[dict]] = []
    _scripted(monkeypatch, lambda i: _final(texts[i]), record=seen)

    defn, _ = _run(_stages("brief", "outline", "draft", "review"), monkeypatch)
    version = await _standard_skill(db, user_a.id)
    ctx = _ctx(db, user_a.id, version)
    await execute_agent(ctx, "workflow", defn)

    # 4단계가 모두 실행된다 — 각 react_agent이 마무리 턴을 한 번씩 쓴다
    assert len(seen) == 4
    # 단계별 산출물 4개 + 최종 결과 1개
    names = [entry["name"] for entry in ctx.artifact_keys]
    assert names == ["brief.md", "outline.md", "draft.md", "review.md", "result.md"]


async def test_later_stage_receives_the_earlier_stage_output(
    db, user_a, _mock_runtime_deps, mock_llm, monkeypatch
) -> None:
    texts = ["브리프 내용", "개요 내용"]
    seen: list[list[dict]] = []
    _scripted(monkeypatch, lambda i: _final(texts[i]), record=seen)

    stages = [
        Stage(id="brief", goal="브리프를 써", artifact_name="01.md"),
        Stage(id="outline", goal="개요를 써\n\n{brief.text}", artifact_name="02.md"),
    ]
    defn, _ = _run(stages, monkeypatch)
    version = await _standard_skill(db, user_a.id)
    ctx = _ctx(db, user_a.id, version)
    await execute_agent(ctx, "workflow", defn)

    # 마지막 메시지는 activate_skill의 도구 결과다 — 모델에 내려간 **goal**을 본다
    second_goal = next(m["content"] for m in reversed(seen[1]) if m["role"] == "user")
    assert "브리프 내용" in second_goal
    assert "개요를 써" in second_goal
    # 산출물에도 앞 단계 출력이 들어간다
    stored = _mock_runtime_deps[f"{ctx.run_id}/02.md"].decode()
    assert stored == "개요 내용"


# ── AC5: 예산 소진은 연쇄를 끊는다 ─────────────────────────────────────────


async def test_real_budget_exhaustion_stops_the_chain_with_a_non_empty_answer(
    db, user_a, _mock_runtime_deps, mock_llm, monkeypatch
) -> None:
    """**실제 예산 소진**이 연쇄를 끊는다(#202 리뷰 M1).

    이전 테스트는 가짜 모델이 `BUDGET_PARTIAL_NOTE` 문구 자체를 뱉게 해서 통과했다.
    그런데 루프가 예산 초과로 끝낼 때 `final_text` 는 `turn.content` 를 **우선**한다 —
    즉 실제 상황에서는 정상 답변(비어 있지 않은 문자열)이 돌아온다. 문구를 비교하는
    게이트는 이때 항상 거짓이 되고 연쇄가 끝까지 돈다. 게이트가 **구조적 신호**를
    보는지 확인한다.
    """
    seen: list[list[dict]] = []
    # 내용이 비어 있지 않은 정상 답변. 예산만으로 멈춰야 한다.
    _scripted(monkeypatch, lambda i: _final(f"단계{i} 완료"), record=seen)

    # 4단계, 총 8토큰 → 단계당 2토큰. 가짜 모델은 한 턴에 2토큰을 쓴다.
    defn, _ = _run(_stages("brief", "outline", "draft", "review"), monkeypatch, total_tokens=8)
    version = await _standard_skill(db, user_a.id)
    ctx = _ctx(db, user_a.id, version)
    await execute_agent(ctx, "workflow", defn)

    # 첫 단계에서 예산이 닿고 연쇄가 끊긴다
    assert len(seen) == 1, "예산이 닿았는데 뒤 단계가 실행됐다"
    assert [entry["name"] for entry in ctx.artifact_keys] == ["brief.md"]
    # 최종 결과 산출물은 만들어지지 않는다(마지막 단계에 도달하지 않았다)
    assert "result.md" not in [entry["name"] for entry in ctx.artifact_keys]


async def test_budget_exhaustion_stops_the_chain_on_a_later_stage(
    db, user_a, _mock_runtime_deps, mock_llm, monkeypatch
) -> None:
    """몇 번째 단계에서 닿든, 거기서 멈춘다 — brief와 outline만 실행된다."""
    seen: list[list[dict]] = []
    # 4단계, 총 8토큰 → 단계당 2토큰. 1단계는 1토큰(통과), 2단계는 4토큰(초과).
    _scripted(
        monkeypatch,
        lambda i: _final(f"단계{i} 완료", total=1 if i == 0 else 4),
        record=seen,
    )

    defn, _ = _run(_stages("brief", "outline", "draft", "review"), monkeypatch, total_tokens=8)
    version = await _standard_skill(db, user_a.id)
    ctx = _ctx(db, user_a.id, version)
    await execute_agent(ctx, "workflow", defn)

    assert len(seen) == 2
    assert [entry["name"] for entry in ctx.artifact_keys] == ["brief.md", "outline.md"]
    assert "result.md" not in [entry["name"] for entry in ctx.artifact_keys]


async def test_an_exception_leaves_the_earlier_artifacts_and_no_result(
    db, user_a, _mock_runtime_deps, mock_llm, monkeypatch
) -> None:
    """AC5 — **예외**로 멈춘 경우에도 앞 단계 산출물은 남아 있고 `result.md` 는 없다.

    예산 소진 경로(위 테스트)와 다른 경로다. 이 경로에는 게이트가 없고, 뒤 단계도
    실행되지 않는다 — 그러나 앞 단계가 이미 쓴 객체는 그대로 남는다(#202 리뷰 m9).
    """
    def script(i: int) -> ToolTurnResult:
        if i == 1:
            raise RuntimeError("모델 폭주")
        return _final(f"단계{i} 완료")

    _scripted(monkeypatch, script)
    defn, _ = _run(_stages("brief", "outline"), monkeypatch)
    version = await _standard_skill(db, user_a.id)
    ctx = _ctx(db, user_a.id, version)

    with pytest.raises(Exception):  # noqa: B017 — 어떤 예외형인지는 AC5의 대상이 아니다
        await execute_agent(ctx, "workflow", defn)

    names = [entry["name"] for entry in ctx.artifact_keys]
    assert names == ["brief.md"], "예외 경로에서 앞 단계 산출물이 사라졌다"
    assert "result.md" not in names


async def test_a_normal_answer_named_like_the_budget_note_does_not_stop_the_chain(
    db, user_a, _mock_runtime_deps, mock_llm, monkeypatch
) -> None:
    """모델이 그 문구를 **인용만** 하면 멈추지 않는다(문구 게이트의 반대 방향 결함)."""
    _scripted(monkeypatch, lambda i: _final(BUDGET_PARTIAL_NOTE))
    defn, _ = _run(_stages("a", "b"), monkeypatch)
    version = await _standard_skill(db, user_a.id)
    ctx = _ctx(db, user_a.id, version)
    await execute_agent(ctx, "workflow", defn)

    assert [entry["name"] for entry in ctx.artifact_keys] == ["a.md", "b.md", "result.md"]


async def test_chain_completes_when_no_budget_is_hit(
    db, user_a, _mock_runtime_deps, mock_llm, monkeypatch
) -> None:
    _scripted(monkeypatch, lambda i: _final(f"단계{i}"))
    defn, _ = _run(_stages("a", "b"), monkeypatch)
    version = await _standard_skill(db, user_a.id)
    ctx = _ctx(db, user_a.id, version)
    await execute_agent(ctx, "workflow", defn)

    assert [entry["name"] for entry in ctx.artifact_keys] == ["a.md", "b.md", "result.md"]


# ── AC8: 모든 단계가 스킬 지침을 받는다 ────────────────────────────────────


async def test_every_stage_receives_the_skill_instructions(
    db, user_a, _mock_runtime_deps, mock_llm, monkeypatch
) -> None:
    """1단계가 아니라 **모든** 단계의 system에 지침이 있어야 한다(1차 리뷰 M1).

    `activate_skill`은 첫 단계 이후 '이미 활성화'로 즉시 반환하므로 도구로는 전달되지 않는다.
    """
    seen: list[list[dict]] = []
    _scripted(monkeypatch, lambda i: _final(f"단계{i}"), record=seen)

    defn, _ = _run(_stages("a", "b", "c"), monkeypatch, skill_body="ARTICLE 지침 본문")
    version = await _standard_skill(db, user_a.id)
    ctx = _ctx(db, user_a.id, version)
    await execute_agent(ctx, "workflow", defn)

    assert len(seen) == 3
    for messages in seen:
        system = messages[0]["content"]
        assert "ARTICLE 지침 본문" in system, "단계의 system에 스킬 지침이 없다"


async def test_skill_body_braces_are_not_rendered_away(
    db, user_a, _mock_runtime_deps, mock_llm, monkeypatch
) -> None:
    seen: list[list[dict]] = []
    _scripted(monkeypatch, lambda i: _final(f"단계{i}"), record=seen)

    defn, _ = _run(_stages("a"), monkeypatch, skill_body='형식: {"주제": "{값}"}')
    version = await _standard_skill(db, user_a.id)
    ctx = _ctx(db, user_a.id, version)
    await execute_agent(ctx, "workflow", defn)

    system = seen[0][0]["content"]
    # 그대로 살아 있다 — 삼켜지면 지시가 조용히 변형된다
    assert '{"주제": "{값}"}' in system


# ── AC11: 승인 재개가 그 지점부터 이어진다 ────────────────────────────────


async def test_resume_anchor_resolves_to_exactly_one_node(
    db, user_a, _mock_runtime_deps, mock_llm, monkeypatch
) -> None:
    """재개 분기는 checkpoint의 `react_node_id`로 노드를 찾아 `_resume`을 **그 노드에만** 붙인다.

    그 전제 두 개를 확인한다: 정의가 재현되고, 앵커 id가 **유일하게** 존재한다.
    (1차 리뷰 m7)
    """
    from biznotes_core.services.agents.react.approval import CheckpointKeys

    stages = [
        Stage(id="brief", goal="브리프", artifact_name="01.md"),
        Stage(id="outline", goal="개요 {brief.text}", artifact_name="02.md"),
        Stage(id="draft", goal="초안 {outline.text}", artifact_name="03.md"),
    ]
    first, _ = _run(stages, monkeypatch)
    again, _ = _run(stages, monkeypatch)
    assert first == again  # 재개는 정의를 다시 만들어 앵커와 맞춰야 한다

    ids = [n["id"] for n in first["nodes"]]
    for stage_id in ("brief", "outline", "draft"):
        assert ids.count(stage_id) == 1, f"앵커 {stage_id}가 유일하지 않다"

    # 내부 노드 이름이 단계 id와 겹치지 않아 앵커가 모호해지지 않는다
    assert len(set(ids)) == len(ids)
    assert CheckpointKeys.REACT_NODE_ID == "react_node_id"


def test_definition_is_json_serializable_for_checkpointing() -> None:
    # 재개 checkpoint에 그래프 상태가 실리므로 정의가 직렬화 가능해야 한다
    defn, _ = _run(_stages("a", "b"), None)  # type: ignore[arg-type]
    json.dumps(defn)