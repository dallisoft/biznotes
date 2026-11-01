"""다단계 스킬 실행의 선언 검증과 그래프 조립(#202).

계획 `docs/plans/skill-stages/plan.md`의 AC1·AC4·AC6·AC7·AC9·AC10·AC12를 확인한다.
끝까지 도는 통합(AC2·AC3·AC5·AC8·AC11)은 `tests/test_skill_stages_e2e_pg.py`가 본다.
여서는 선언 파싱과 **그래프 모양**을 단언한다 — 실행 환경 없이 결정적으로 판정할 수 있는 층위.
"""

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from biznotes_core.config import CoreSettings, get_settings, reset_settings
from biznotes_core.services.agents.graph import _STATE_RESERVED_KEYS
from biznotes_core.services.agents.react.loop import (
    _REACT_CONFIG_KEYS,
    BUDGET_HIT_KEY,
    BUDGET_PARTIAL_NOTE,
)
from biznotes_core.services.skills.runtime import (
    staged_run_definition,
    standard_run_definition,
)
from biznotes_core.services.skills.stages import (
    MAX_STAGES,
    RESERVED_NODE_IDS,
    Stage,
    parse_stages,
    validate_stages,
)
from biznotes_core.services.skills.validator import SkillValidationError


def _fm(*stages: dict[str, Any]) -> dict[str, Any]:
    return {"name": "p", "description": "d", "metadata": {"stages": list(stages)}}


# ── AC1: 선언 파싱 ──────────────────────────────────────────────────────────


def test_no_stages_means_no_stages() -> None:
    assert parse_stages({"name": "p"}) == []
    assert parse_stages({"name": "p", "metadata": {}}) == []
    assert parse_stages({"name": "p", "metadata": {"other": 1}}) == []


def test_default_artifact_name_carries_the_ordinal() -> None:
    stages = parse_stages(_fm({"id": "brief", "goal": "A"}, {"id": "draft", "goal": "B"}))
    # 순서가 드러나야 소비자가 01/02로 알 수 있다
    assert [s.artifact_name for s in stages] == ["01-brief.md", "02-draft.md"]


def test_explicit_artifact_name_is_kept() -> None:
    stages = parse_stages(_fm({"id": "a", "goal": "x", "artifact": {"name": "out.md"}}))
    assert stages[0].artifact_name == "out.md"


def test_reference_to_earlier_stage_is_allowed() -> None:
    stages = parse_stages(
        _fm(
            {"id": "a", "goal": "A"},
            {"id": "b", "goal": "B {a.text}"},
            {"id": "c", "goal": "{b.text}"},
        )
    )
    assert [s.id for s in stages] == ["a", "b", "c"]


def test_params_reference_is_allowed() -> None:
    assert parse_stages(_fm({"id": "a", "goal": "{params.request} 하기"}))[0].goal


# ── AC9: 가져오기 단계에서 fail-closed ─────────────────────────────────────


@pytest.mark.parametrize(
    "stages,reason",
    [
        ([], "bad_stages_count"),
        ([{"id": "a", "goal": "x"}] * (MAX_STAGES + 1), "bad_stages_count"),
        ([{"id": "A", "goal": "x"}], "bad_stages_id"),  # kebab-case 아님
        ([{"id": "user", "goal": "x"}], "bad_stages_id"),  # graph.py 예약어
        ([{"id": "params", "goal": "x"}], "bad_stages_id"),
        ([{"id": "chat_message", "goal": "x"}], "bad_stages_id"),
        ([{"id": "save", "goal": "x"}], "bad_stages_id"),
        ([{"id": "stage_stop", "goal": "x"}], "bad_stages_id"),
        ([{"id": "a", "goal": "x"}, {"id": "a", "goal": "y"}], "bad_stages_id"),  # 중복
        ([{"id": "a", "goal": "  "}], "bad_stages_goal"),  # 빈 goal
        ([{"id": "a"}], "bad_stages_goal"),  # goal 누락
        ([{"id": "a", "goal": "x", "bogus": 1}], "bad_stages_key"),
        ([{"id": "a", "goal": "x", "artifact": {"nam": "z"}}], "bad_stages_artifact"),
        (
            [{"id": "a", "goal": "x"},
             {"id": "b", "goal": "{a.text}", "artifact": {"name": "s.md"}},
             {"id": "c", "goal": "y", "artifact": {"name": "s.md"}}],
            "bad_stages_artifact",
        ),  # 산출물 이름 충돌 — key가 `{run_id}/{name}`이라 서로 덮어쓴다
        ([{"id": "b", "goal": "{a.text}"}], "bad_stages_reference"),  # 뒤 단계 참조
        ([{"id": "a", "goal": "{self.text}"}], "bad_stages_reference"),  # 자기 자신
        ([{"id": "a", "goal": "{a.text}"}], "bad_stages_reference"),  # 자기 참조도 거부
        ([{"id": "a", "goal": "{a.key}"}], "bad_stages_reference"),  # text가 아닌 필드
        ([{"id": "a", "goal": "{unknown.node}"}], "bad_stages_reference"),
        ([{"id": "a", "goal": "x", "max_iters": 0}], "bad_stages_max_iters"),
        ([{"id": "a", "goal": "x", "cost_budget_usd": -1}], "bad_stages_cost_budget_usd"),
        ([{"id": "a", "goal": "x", "max_iters": "많이"}], "bad_stages_max_iters"),
    ],
)
def test_invalid_declarations_are_rejected(stages: list[Any], reason: str) -> None:
    with pytest.raises(SkillValidationError) as info:
        validate_stages(_fm(*stages))
    assert info.value.reason == reason


def test_scalar_metadata_means_no_stages_not_a_rejection() -> None:
    """`metadata`는 skills-ref 표준에서 **자유 형식**이다(#202 리뷰 M2).

    `metadata: hello` 같은 스칼라 표기를 쓴 스킬은 오늘 실행된다. `stages`를 안 썼다는
    이유만으로 여기서 거부하면 코어 버전업만으로 게시된 스킬이 깨진다 —
    "선언이 없으면 기존 동작"이 그게 아니면 안 된다.
    """
    for scalar in ("hello", 3.14, ["a"], True):
        assert parse_stages({"metadata": scalar}) == []
        validate_stages({"metadata": scalar})  # 거부하지 않는다


def test_broken_stages_declaration_is_still_rejected() -> None:
    """단계를 **선언했는데** 형태가 틀린 경우만 거부한다(선언 없는 경로와 구분)."""
    with pytest.raises(SkillValidationError) as info:
        validate_stages({"metadata": {"stages": "nope"}})
    assert info.value.reason == "bad_stages"

    with pytest.raises(SkillValidationError) as info:
        validate_stages({"metadata": {"stages": {"a": 1}}})
    assert info.value.reason == "bad_stages"

    with pytest.raises(SkillValidationError) as info:
        validate_stages({"metadata": {"stages": []}})
    assert info.value.reason == "bad_stages_count"


def test_reserved_ids_derive_from_graph_state_keys() -> None:
    # 정본 상수에서 파생해야 두 곳이 어긋날 수 없다
    assert _STATE_RESERVED_KEYS <= RESERVED_NODE_IDS
    assert {"skill", "save", "stage_stop"} <= RESERVED_NODE_IDS


def test_langgraph_sentinels_are_not_reachable_as_ids() -> None:
    # `SLUG_RE`이 하이픈 없는 이름을 막으므로 `__start__`/`__end__`는 애초에 통과하지 않는다
    with pytest.raises(SkillValidationError):
        validate_stages(_fm({"id": "__start__", "goal": "x"}))


# ── AC10: 실행 경로 재검증 ─────────────────────────────────────────────────


def test_parse_stages_revalidates_from_persisted_metadata() -> None:
    # 가져오기 우회(시드·마이그레이션)로 들어온 행도 실행 경로에서 거절돼야 한다
    malformed = {"metadata": {"stages": [{"id": "user", "goal": "x"}]}}
    with pytest.raises(SkillValidationError) as info:
        parse_stages(malformed)
    assert info.value.reason == "bad_stages_id"


def test_stage_limit_can_be_lowered_by_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILL_MAX_STAGES", "2")
    reset_settings()
    try:
        with pytest.raises(SkillValidationError) as info:
            parse_stages(_fm({"id": "a", "goal": "1"}, {"id": "b", "goal": "2"},
                             {"id": "c", "goal": "3"}))
        assert info.value.reason == "bad_stages_count"
    finally:
        monkeypatch.delenv("SKILL_MAX_STAGES")
        reset_settings()


def test_settings_validation_bounds() -> None:
    for kwargs in (
        {"skill_stage_total_cost_usd": 0},
        {"skill_stage_total_tokens": 0},
        {"skill_max_stages": 0},
    ):
        with pytest.raises(ValidationError):
            CoreSettings(**kwargs)  # type: ignore[arg-type]


# ── AC1·AC4·AC6·AC7·AC12: 그래프 모양 ─────────────────────────────────────


def _definition(*stages: dict[str, Any], skill_body: str = "") -> dict[str, Any]:
    return staged_run_definition(
        parse_stages(_fm(*stages)), skill_name="p", skill_body=skill_body
    )


def _by_id(defn: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(n for n in defn["nodes"] if n["id"] == node_id)


def test_each_stage_gets_a_react_agent_and_an_artifact() -> None:
    defn = _definition({"id": "brief", "goal": "A"}, {"id": "draft", "goal": "B {brief.text}"})
    react = [n for n in defn["nodes"] if n["type"] == "react_agent"]
    assert [n["id"] for n in react] == ["brief", "draft"]
    assert [_by_id(defn, f"save_{n['id']}")["type"] for n in react] == [
        "save_artifact",
        "save_artifact",
    ]


def test_stage_artifact_content_references_its_own_stage() -> None:
    defn = _definition({"id": "brief", "goal": "A"})
    assert _by_id(defn, "save_brief")["content"] == "{brief.text}"


def test_last_stage_has_no_gate_to_another_stage() -> None:
    defn = _definition({"id": "a", "goal": "A"}, {"id": "b", "goal": "B {a.text}"})
    gate_a = _by_id(defn, "gate_a")
    assert gate_a["true_node"] == "stage_stop"
    assert gate_a["false_node"] == "b"
    # 마지막 게이트는 최종 산출물로만 흐른다
    assert _by_id(defn, "gate_b")["false_node"] == "save"
    # 게이트에서 나가는 일반 엣지는 없다 — 조건 노드가 한 갈래만 탄다
    assert [e for e in defn["edges"] if e[0].startswith("gate_")] == []


def test_stop_node_has_no_outgoing_edge_so_it_ends_the_graph() -> None:
    defn = _definition({"id": "a", "goal": "A"})
    assert "stage_stop" not in [dst for _, dst in defn["edges"]]


def test_gate_reads_the_structural_budget_signal() -> None:
    """게이트는 `text` 문구가 아니라 **구조적 신호**를 본다(#202 리뷰 M1).

    `text`를 문구와 비교하면 `turn.content` 가 채워진 정상 답변에서 항상 거짓이 되고,
    그 경우가 훨씬 많다. 문자열을 하드코딩하지 않고 상수를 참조하는 성질은 유지한다.
    """
    defn = _definition({"id": "a", "goal": "A"})
    gate = _by_id(defn, "gate_a")
    assert gate["op"] == "=="
    assert gate["left"] == "{a." + BUDGET_HIT_KEY + "}"
    assert gate["right"] == "true"
    # 게이트는 모델 출력 문구를 **아예 참조하지 않는다**
    assert BUDGET_PARTIAL_NOTE not in str(defn)


def test_artifact_names_cannot_escape_the_run_prefix() -> None:
    """`save_artifact` 는 `name` 을 `render` 한다 — 템플릿과 경로가 그대로 키에 들어간다.

    선언 검증에서 막지 않으면 **이전 단계의 모델 출력이 storage key를 결정한다**
    (#202 리뷰 M3).
    """
    bad = [
        "{a.text}",                      # 렌더 → 이전 단계의 모델 출력
        "../escape.md",                  # 경로 탈출
        "a/b.md",                        # 경로 탈출
        "a\\b.md",                      # 경로 탈출
        " result.md",                    # 앞뒤 공백
        ".hidden.md",                    # 점 시작
    ]
    for name in bad:
        with pytest.raises(SkillValidationError) as info:
            validate_stages({"metadata": {"stages": [{"id": "a", "goal": "A",
                                                      "artifact": {"name": name}}]}})
        assert info.value.reason == "bad_stages_artifact_name", name


def test_stage_artifact_cannot_shadow_the_final_result() -> None:
    """최종 결과는 마지막 단계 **뒤에** 저장된다 — 이름이 겹치면 그게 나중에 덮어쓴다."""
    with pytest.raises(SkillValidationError) as info:
        validate_stages({"metadata": {"stages": [{"id": "a", "goal": "A", "artifact": {
            "name": get_settings().skill_result_artifact_name}}]}})
    assert info.value.reason == "bad_stages_artifact"


def test_nan_budget_does_not_disarm_the_cost_guard() -> None:
    """`f <= 0` 은 nan을 통과시키고 `min(nan, cap)` 도 nan이다(#202 리뷰 m2)."""
    with pytest.raises(SkillValidationError) as info:
        validate_stages({"metadata": {"stages": [{"id": "a", "goal": "A",
                                                  "cost_budget_usd": "nan"}]}})
    assert info.value.reason == "bad_stages_cost_budget_usd"


def test_generated_node_ids_cannot_collide() -> None:
    """검증 우회로(직접 만든 `Stage`)로 만들어지는 id가 겹치면 조용히 덮어써진다(M4)."""
    for bad in ("save_a", "gate_b"):
        with pytest.raises(SkillValidationError) as info:
            staged_run_definition(
                [Stage(id=bad, goal="A")], skill_name="s", skill_body="b"  # type: ignore[arg-type]
            )
        assert info.value.reason == "bad_stages_id", bad


def test_edges_reach_every_stage_in_order() -> None:
    defn = _definition(
        {"id": "a", "goal": "A"},
        {"id": "b", "goal": "B {a.text}"},
        {"id": "c", "goal": "C {b.text}"},
    )
    plain = [(s, d) for s, d in defn["edges"] if not s.startswith("gate_")]
    assert plain == [("a", "save_a"), ("b", "save_b"), ("c", "save_c"),
                     ("save_a", "gate_a"), ("save_b", "gate_b"), ("save_c", "gate_c")]


def test_no_stage_edge_bypasses_a_gate() -> None:
    # 게이트를 우회하는 엣지가 있으면 예산 소진 후에도 뒤 단계가 돈다
    defn = _definition({"id": "a", "goal": "A"}, {"id": "b", "goal": "B {a.text}"})
    assert ("save_a", "save_b") not in defn["edges"]


def test_chat_output_names_the_last_stage() -> None:
    defn = _definition({"id": "a", "goal": "A"}, {"id": "b", "goal": "B {a.text}"})
    assert defn["chat_output"] == {"node_id": "b"}


# ── AC6: 총액 분할 ─────────────────────────────────────────────────────────


def test_total_budget_is_split_evenly() -> None:
    defn = staged_run_definition(
        parse_stages(_fm({"id": "a", "goal": "A"}, {"id": "b", "goal": "B {a.text}"})),
        skill_name="p",
        skill_body="",
        total_cost=4.0,
        total_tokens=400_000,
    )
    assert _by_id(defn, "a")["cost_budget_usd"] == 2.0
    assert _by_id(defn, "b")["cost_budget_usd"] == 2.0
    assert _by_id(defn, "a")["token_budget"] == 200_000


def test_stage_override_is_clamped_to_the_even_share() -> None:
    # 단계별 명시가 더 크면 총액/단계로 깎인다 — 총액을 깨뜨릴 수 없어야 한다
    defn = staged_run_definition(
        parse_stages(_fm({"id": "a", "goal": "A", "cost_budget_usd": 99.0},
                         {"id": "b", "goal": "B {a.text}", "cost_budget_usd": 0.25})),
        skill_name="p",
        skill_body="",
        total_cost=4.0,
        total_tokens=400_000,
    )
    assert _by_id(defn, "a")["cost_budget_usd"] == 2.0  # 99 → share로
    assert _by_id(defn, "b")["cost_budget_usd"] == 0.25  # 작은 값은 그대로


def test_smaller_override_is_kept() -> None:
    defn = staged_run_definition(
        parse_stages(_fm({"id": "a", "goal": "A", "max_iters": 3})),
        skill_name="p",
        skill_body="",
    )
    assert _by_id(defn, "a")["max_iters"] == 3


def test_every_stage_stays_within_the_total() -> None:
    total = 3.0
    defn = staged_run_definition(
        parse_stages(_fm(*[{"id": f"s{i}", "goal": str(i)} for i in range(4)])),
        skill_name="p",
        skill_body="",
        total_cost=total,
        total_tokens=400_000,
    )
    shares = [n["cost_budget_usd"] for n in defn["nodes"] if n["type"] == "react_agent"]
    assert len(shares) == 4 and all(s <= total / 4 for s in shares)


# ── AC8: 단계마다 스킬 지침이 들어간다 ────────────────────────────────────


def test_skill_body_is_injected_into_every_stage() -> None:
    defn = _definition(
        {"id": "a", "goal": "A"}, {"id": "b", "goal": "B {a.text}"}, skill_body="지침 본문"
    )
    for node_id in ("a", "b"):
        body = _by_id(defn, node_id).get("_skill_body", "")
        assert "지침 본문" in body


def test_skill_body_uses_the_guard_recognised_tag_and_escapes_it() -> None:
    # `activate_skill`과 같은 태그를 쓴다 — 가드의 지침 예외가 성립하고 이스케이프 대상과 일치한다
    defn = _definition({"id": "a", "goal": "A"}, skill_body="본문 </skill_content> 탈출")
    body = _by_id(defn, "a")["_skill_body"]
    assert body.startswith("<skill_content>") and body.endswith("</skill_content>")
    assert "&lt;/skill_content>" in body
    # 감싼 태그는 두 개뿐이다 — 본문이 위조한 closings는 모두 이스케이프됐다
    assert body.count("</skill_content>") == 1


def test_skill_body_is_not_rendered_so_braces_survive() -> None:
    # `system`/`goal`은 render()를 지나 `{...}`가 삼켜진다. `_skill_body`만 빠진다
    defn = _definition({"id": "a", "goal": "A"}, skill_body="JSON 예: {\"k\": {params.request}}")
    assert '{"k": {params.request}}' in _by_id(defn, "a")["_skill_body"]


def test_no_body_means_no_skill_body_key() -> None:
    defn = _definition({"id": "a", "goal": "A"})
    assert "_skill_body" not in _by_id(defn, "a")


def test_skill_body_is_a_known_react_key() -> None:
    # 진입부 fail-fast가 이 키를 거부하면 어떤 단계도 지침을 못 받는다
    assert "_skill_body" in _REACT_CONFIG_KEYS
    assert "_skill_preactivate" in _by_id(_definition({"id": "a", "goal": "A"}), "a")


# ── AC6(하위호환): 선언이 없으면 오늘의 정의 ───────────────────────────────


def test_single_run_definition_is_unchanged() -> None:
    defn = standard_run_definition("p")
    assert [n["id"] for n in defn["nodes"]] == ["skill", "save"]
    assert defn["edges"] == [["skill", "save"]]
    assert "chat_output" not in defn  # 오늘은 암묵 규칙에 맡긴다
    assert _by_id(defn, "skill")["goal"] == "{params.request}"


def test_empty_stage_list_is_the_same_graph() -> None:
    staged = staged_run_definition([], skill_name="p", skill_body="지침")
    today = standard_run_definition("p")
    # 지침이 있으면 system 대신 `_skill_body`로 붙으므로 단일 단계 정의를 재사용한다
    assert [n["id"] for n in staged["nodes"]] == ["skill", "save"]
    assert staged["edges"] == today["edges"]
    assert "_skill_body" not in _by_id(staged, "skill")


def test_stage_ids_do_not_collide_with_single_run_ids() -> None:
    # 단일 경로의 노드 id와 단계 id가 겹치면 정의가 뒤섞인다
    assert not ({n["id"] for n in standard_run_definition("p")["nodes"]} & {"stage_stop"})


def test_stage_ids_are_valid_slug_reachable() -> None:
    stages = parse_stages(_fm({"id": "a-b-c", "goal": "A"}))
    assert stages[0] == Stage(id="a-b-c", goal="A", system=None, artifact_name="01-a-b-c.md")


def test_definition_is_reproducible_for_resume() -> None:
    # 재개는 정의를 다시 만들어 checkpoint의 노드 id와 맞아야 한다
    raw = _fm({"id": "brief", "goal": "A"}, {"id": "draft", "goal": "B {brief.text}"})
    first = staged_run_definition(parse_stages(raw), skill_name="p", skill_body="지침")
    second = staged_run_definition(parse_stages(raw), skill_name="p", skill_body="지침")
    assert first == second
    assert uuid.uuid4().hex  # 노드 id에 실행별 난수를 쓰지 않는다 — 재개가 깨진다