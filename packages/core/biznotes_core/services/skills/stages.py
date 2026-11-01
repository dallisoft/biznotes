"""다단계 스킬 실행 — `SKILL.md`의 `metadata.stages` 파싱·검증·그래프 조립(#202).

한 단계가 `react_agent` 한 쌍이다. 단계는 순차로 고정이고 각자 예산을 갖는다.
선언은 **원시 `nodes`가 아니라 닫힌 스키마**로 받는다 — 스킬이 `http`·`mcp_tool`·
`agent_call` 같은 임의 노드를 선언해 새 공격면을 열지 못하게 하는 것이 목적이다.

선언 자리는 `SKILL.md` 프런트메터의 `metadata` 아래다. 제품 매니페스트(`slagents.json`)는
쓰지 않는다 — 그게 있으면 `runtime_profile_for`가 스킬을 `legacy_instruction_v1`로 강등시키고
승인 게이트를 통과하지 못한다.

검증은 **3중**이다(계획 §3.9):
1. 가져오기·평가(`report.evaluate_package`)
2. 실행 시 재검증(`parse_stages`·`stages_run_definition`이 스스로 한다)
3. freeze 사전 검증(`service.py`가 그래프를 1회 조립해 본다)
저장된 행이 시드·마이그레이션으로 들어왔을 수 있으므로 실행 경로도 방어선을 가져야 한다.
"""

import math
from dataclasses import dataclass
from typing import Any

from biznotes_core.config import get_settings
from biznotes_core.services.agents.graph import _STATE_RESERVED_KEYS
from biznotes_core.services.agents.templating import template_paths
from biznotes_core.services.skills.validator import (
    SLUG_RE,
    SkillValidationError,
    _check_artifact_name,
)

# 단계 배열의 코드 정본 상한 — `config.skill_max_stages`가 환경별로 더 좁힐 수 있다
MAX_STAGES = 8

# 노드 id로 쓸 수 없는 이름. `graph.py::_STATE_RESERVED_KEYS`에서 파생한다 — 두 곳이
# 어긋나면 그래프 조립 시점에 죽거나, 조용히 다른 노드에 덮어써진다.
# `_` 접두 private이라 직접 import가 필요하다.
# private 심볼 의존 사유는 "순환 방지"가 아니라 **정본 상수 재사용**이다 —
# 예약 키 목록을 여기서 다시 적으면 `graph.py` 에 키가 생겼을 때 조용히 어긋난다(#202 리뷰 m3).
# 이 값은 **임포트 시점에 복사**된다(그래서 아래 테스트가 갱신 누락을 잡는다).
RESERVED_NODE_IDS = frozenset(_STATE_RESERVED_KEYS) | {"skill", "save", "stage_stop"}
# `staged_run_definition` 이 단계마다 만드는 노드 id 접두. 단계 id가 이 접두로 시작하면
# 만들어진 id가 다른 단계의 id와 겹친다 — `parse_stages` 의 SLUG_RE 가 밑줄을 막아
# 선언 경로에서는 닫혀 있지만, `Stage` 를 직접 만드는 우회로는 열려 있다(#202 리뷰 M4)
GENERATED_NODE_PREFIXES = ("save_", "gate_")

# 허용 설정 키 — 이외는 거부한다(오타를 조용히 무시하지 않는다)
_STAGE_KEYS = frozenset(
    {"id", "goal", "system", "artifact", "max_iters", "token_budget", "cost_budget_usd"}
)
_ARTIFACT_KEYS = frozenset({"name"})


@dataclass(frozen=True)
class Stage:
    """다단계 스킬의 한 단계."""

    id: str
    goal: str
    system: str | None = None
    artifact_name: str = ""
    max_iters: int | None = None
    token_budget: int | None = None
    cost_budget_usd: float | None = None


def _reject(reason: str, message: str) -> SkillValidationError:
    return SkillValidationError(reason, message)


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise _reject(f"bad_stages_{field}", f"단계 {field}는 정수여야 합니다 (got {value!r})")
    try:
        n = int(value)
    except (TypeError, ValueError) as e:
        raise _reject(f"bad_stages_{field}", f"단계 {field}는 정수여야 합니다") from e
    if n <= 0:
        raise _reject(f"bad_stages_{field}", f"단계 {field}는 양수여야 합니다 (got {n})")
    return n


def _positive_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise _reject(f"bad_stages_{field}", f"단계 {field}는 숫자여야 합니다 (got {value!r})")
    try:
        f = float(value)
    except (TypeError, ValueError) as e:
        raise _reject(f"bad_stages_{field}", f"단계 {field}는 숫자여야 합니다") from e
    # `f <= 0` 은 nan을 통과시킨다. nan은 `min(nan, cap)` 에서도 nan이라 그대로 남아
    # 예산 가드가 **영원히 참이 되지 않는다** — 즉 비용 상한이 무한대가 된다(#202 리뷰 m2)
    if not math.isfinite(f) or f <= 0:
        raise _reject(f"bad_stages_{field}", f"단계 {field}는 유한한 양수여야 합니다 (got {f})")
    return f


def _artifact_name(raw: str, stage_id: str) -> str:
    """단계 산출물 이름을 검증한다.

    `save_artifact` 노드가 `name`을 **`render`로 템플릿 치환한다**(`nodes/composite.py`).
    그래서 이름에 `{...}`가 들어가면 **이전 단계의 모델 출력**이 storage key를 결정하고,
    `/`·`..`가 들어가면 run prefix를 벗어난다. 둘 다 선언 검증에서 막아야 한다
    (#202 리뷰 M3) — 주석만으로는 막히지 않는다. 규칙은 레거시 매니페스트와 공유한다(#302).
    """
    return _check_artifact_name(
        raw, label=f"단계 [{stage_id}]의 artifact.name", reason="bad_stages_artifact_name"
    )


def _validate_references(goal: str, seen_ids: list[str], stage_id: str) -> None:
    """goal의 `{...}` 참조는 `params.*`와 **앞에 나온** 단계의 `.text`만 허용한다.

    `render`는 못 찾는 경로를 조용히 빈 문자열로 만들기 때문에 가져오기 단계에서 막아야 한다.
    """
    # 템플릿 렌더와 같은 문법으로 찾는다 — `{경로|untrusted}`도 경로로 검사한다(#228)
    for ref in template_paths(goal):
        if ref.startswith("params."):
            continue
        node, _, field = ref.partition(".")
        if node not in seen_ids:
            raise _reject(
                "bad_stages_reference",
                f"단계 [{stage_id}]가 아직 실행되지 않은 [{ref}]를 참조합니다 — "
                "앞 단계만 참조할 수 있습니다",
            )
        if field != "text":
            raise _reject(
                "bad_stages_reference",
                f"단계 [{stage_id}]의 참조 [{ref}]는 `.text`만 허용합니다",
            )


def _parse_one(raw: Any, seen_ids: list[str], seen_names: dict[str, str], index: int) -> Stage:
    if not isinstance(raw, dict):
        raise _reject("bad_stages", f"{index + 1}번째 단계는 객체여야 합니다")
    unknown = set(raw) - _STAGE_KEYS
    if unknown:
        raise _reject("bad_stages_key", f"단계에 알 수 없는 키가 있습니다: {sorted(unknown)}")

    raw_id = raw.get("id")
    if not isinstance(raw_id, str) or not SLUG_RE.match(raw_id):
        raise _reject(
            "bad_stages_id",
            f"단계 id는 lowercase kebab-case여야 합니다: {raw_id!r}",
        )
    if raw_id in RESERVED_NODE_IDS:
        raise _reject(
            "bad_stages_id",
            f"단계 id [{raw_id}]는 예약어입니다 — 그래프 구성 시점에 거부됩니다",
        )
    if raw_id in seen_ids:
        raise _reject("bad_stages_id", f"단계 id [{raw_id}]가 중복됩니다")

    goal = raw.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise _reject("bad_stages_goal", f"단계 [{raw_id}]에 비어 있지 않은 goal이 필요합니다")
    _validate_references(goal, seen_ids, raw_id)

    system = raw.get("system")
    if system is not None and not isinstance(system, str):
        raise _reject("bad_stages_system", f"단계 [{raw_id}]의 system은 문자열이어야 합니다")

    artifact = raw.get("artifact")
    name = ""
    if artifact is not None:
        if not isinstance(artifact, dict):
            raise _reject("bad_stages_artifact", f"단계 [{raw_id}]의 artifact는 객체여야 합니다")
        unknown_artifact = set(artifact) - _ARTIFACT_KEYS
        if unknown_artifact:
            raise _reject(
                "bad_stages_artifact",
                f"단계 [{raw_id}]의 artifact에 알 수 없는 키: {sorted(unknown_artifact)}",
            )
        raw_name = artifact.get("name")
        if raw_name is not None and not isinstance(raw_name, str):
            raise _reject(
                "bad_stages_artifact", f"단계 [{raw_id}]의 artifact.name은 문자열이어야 합니다"
            )
        if raw_name:
            name = _artifact_name(raw_name, raw_id)
        else:
            name = f"{index + 1:02d}-{raw_id}.md"  # 기본 산출물 이름 — 순서가 드러난다
    else:
        name = f"{index + 1:02d}-{raw_id}.md"

    # 산출물 키는 `{run_id}/{name}`이라 같은 run 안에서 이름이 겹치면 서로 덮어쓴다
    if name in seen_names:
        raise _reject(
            "bad_stages_artifact",
            f"단계 [{raw_id}]의 산출물 이름 [{name}]이 단계 [{seen_names[name]}]와 겹칩니다",
        )
    if name == get_settings().skill_result_artifact_name:
        # 최종 결과는 **마지막 단계 뒤에** 저장된다. 이름이 겹치면 그 단계가 먼저 쓰고
        # 나중에 최종 결과가 덮어써 앞 단계 산출물이 조용히 사라진다(#202 리뷰 M3-2)
        raise _reject(
            "bad_stages_artifact",
            f"단계 [{raw_id}]의 산출물 이름 [{name}]은 최종 결과 이름과 겹칩니다",
        )
    seen_names[name] = raw_id

    def opt_int(key: str) -> int | None:
        return _positive_int(raw[key], key) if raw.get(key) is not None else None

    def opt_float(key: str) -> float | None:
        return _positive_float(raw[key], key) if raw.get(key) is not None else None

    seen_ids.append(raw_id)
    return Stage(
        id=raw_id,
        goal=goal,
        system=system,
        artifact_name=name,
        max_iters=opt_int("max_iters"),
        token_budget=opt_int("token_budget"),
        cost_budget_usd=opt_float("cost_budget_usd"),
    )


def parse_stages(frontmatter: dict[str, Any]) -> list[Stage]:
    """프런트메터에서 단계를 읽는다. 없으면 빈 리스트.

    **실행 경로에서도 부른다** — 저장된 행이 검증 우회 경로로 들어왔을 수 있으므로
    여기서 다시 fail-closed 한다(계획 §3.9).
    """
    raw_meta = frontmatter.get("metadata")
    if not isinstance(raw_meta, dict):
        # `metadata`는 skills-ref 표준에서 **자유 형식**이다. 문자열·숫자 표기가 흔하고
        # 그런 스킬은 오늘 정상 실행된다 — `stages`를 안 쓴다고 여기서 죽이면 코어
        # 버전업만으로 게시된 스킬이 깨진다(#202 리뷰 M2). "선언이 없으면 기존 동작"을
        # 지키려면 **키가 있을 때만** 형태를 강제해야 한다
        return []
    raw_stages = raw_meta.get("stages")
    if raw_stages is None:
        return []
    if not isinstance(raw_stages, list):
        raise _reject("bad_stages", "metadata.stages는 배열이어야 합니다")

    cap = min(MAX_STAGES, get_settings().skill_max_stages)
    if not 1 <= len(raw_stages) <= cap:
        raise _reject(
            "bad_stages_count",
            f"metadata.stages는 1~{cap}개여야 합니다 (got {len(raw_stages)})",
        )

    seen_ids: list[str] = []
    seen_names: dict[str, str] = {}
    stages = [_parse_one(raw, seen_ids, seen_names, i) for i, raw in enumerate(raw_stages)]
    return stages


def validate_stages(frontmatter: dict[str, Any]) -> None:
    """가져오기·freeze 사전 검증 지점. 실패는 `SkillValidationError`로 거부한다."""
    parse_stages(frontmatter)